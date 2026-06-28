import requests, time, random


import gspread
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import os

import finance_accrual

load_dotenv()

# подключение к сервисному аккаунту
gc = gspread.service_account(filename=os.getenv("GOOGLE_CREDS"))

DAYS_SHIFT = 1  # 1 = вчера
MSK = timezone(timedelta(hours=3))
TARGET_DATE_OVERRIDE = os.getenv("TARGET_DATE", "").strip()
target_date = TARGET_DATE_OVERRIDE or (datetime.now(MSK) - timedelta(days=DAYS_SHIFT)).strftime("%Y-%m-%d")

print(f"Работаем с датой: {target_date}")

RETRYABLE_GOOGLE_CODES = {408, 409, 429, 500, 502, 503, 504}


def _google_error_code(exc):
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            return int(response.status_code)
        except Exception:
            pass
    try:
        return int(getattr(exc, "code"))
    except Exception:
        pass
    text = str(exc)
    for code in RETRYABLE_GOOGLE_CODES:
        if f"'code': {code}" in text or f'"code": {code}' in text:
            return code
    return None


def google_call(label, func, *args, retries=6, base_wait=3, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            code = _google_error_code(exc)
            retryable = code in RETRYABLE_GOOGLE_CODES or code is None
            if attempt == retries or not retryable:
                raise
            wait = min(base_wait * (2 ** (attempt - 1)), 60) + random.uniform(0, 2)
            print(f"⚠️ Google Sheets {label}: {exc}. Ждём {wait:.1f} сек (попытка {attempt}/{retries})...")
            time.sleep(wait)


def get_operational_sheet():
    sheet_name = os.getenv("GOOGLE_SHEET_NAME_OP")
    if not sheet_name:
        raise RuntimeError("GOOGLE_SHEET_NAME_OP не найден в .env")
    spreadsheet = google_call("open spreadsheet", gc.open, sheet_name)
    return google_call("open first worksheet", lambda: spreadsheet.sheet1)

# --- Универсальный безопасный HTTP-запрос с повтором при 429 и 5xx ошибках
def safe_request(url, method="post", headers=None, json=None, data=None, timeout=60, max_retries=4, base_wait=5):
    """
    Универсальный безопасный HTTP-запрос с повтором при 429 и 5xx ошибках.
    Возвращает объект Response (requests), либо выбрасывает исключение после исчерпания попыток.
    """
    for attempt in range(max_retries):
        try:
            # Отправляем запрос
            if method.lower() == "post":
                r = requests.post(url, headers=headers, json=json, data=data, timeout=timeout)
            elif method.lower() == "get":
                r = requests.get(url, headers=headers, params=json, timeout=timeout)
            else:
                raise ValueError("Unsupported HTTP method")

            # Если API вернул 429 — ждём и повторяем
            if r.status_code == 429:
                wait = base_wait * (2 ** attempt) + random.uniform(0, 2)
                print(f"⚠️ Лимит Ozon API (429). Ждём {wait:.1f} сек перед повтором (попытка {attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue

            # Если ошибка 5xx — также повторяем
            if 500 <= r.status_code < 600:
                wait = base_wait * (2 ** attempt) + random.uniform(0, 2)
                print(f"⚠️ Ошибка сервера {r.status_code}. Ждём {wait:.1f} сек перед повтором (попытка {attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue

            # Всё хорошо — возвращаем ответ
            r.raise_for_status()
            return r

        except requests.RequestException as e:
            wait = base_wait * (2 ** attempt) + random.uniform(0, 2)
            print(f"⚠️ Ошибка сети: {e}. Ждём {wait:.1f} сек перед повтором (попытка {attempt + 1}/{max_retries})...")
            time.sleep(wait)

    # Если всё плохо — выбрасываем исключение
    raise RuntimeError(f"❌ Не удалось выполнить запрос после {max_retries} попыток: {url}")

def fetch_ozon_revenue(date):
    """Получает сумму продаж (revenue) за конкретную дату из Ozon Analytics API"""
    import os

    url = "https://api-seller.ozon.ru/v1/analytics/data"
    headers = {
        "Client-Id": os.getenv("OZON_CLIENT_ID"),
        "Api-Key": os.getenv("OZON_API_KEY"),
        "Content-Type": "application/json"
    }
    body = {
        "date_from": date,
        "date_to": date,
        "metrics": ["revenue"],
        "dimension": ["day"],
        "limit": 1000,
        "offset": 0
    }

    try:
        r = safe_request(url, method="post", headers=headers, json=body, timeout=60)
        r.raise_for_status()
        data = r.json().get("result", {})
        totals = data.get("totals", [])
        if totals and isinstance(totals[0], (int, float)):
            revenue = totals[0]
        else:
            revenue = 0
        print(f"✅ Продажи за {date}: {revenue}")
        return revenue
    except Exception as e:
        print(f"❌ Ошибка при запросе продаж: {e}")
        return 0

def _utc_day_range(date_str: str) -> tuple[str, str]:
    # Ozon Finance API игнорирует timezone в строке и читает время как МСК.
    # Передаём 00:00–23:59 с суффиксом Z — API воспримет это как московские сутки.
    return f"{date_str}T00:00:00.000Z", f"{date_str}T23:59:59.000Z"


def fetch_ozon_finance_totals(date):
    """Финансовые итоги (продажи и расходы) за день.

    МИГРИРОВАНО с устаревающего /v3/finance/transaction/totals на новые
    finance/accrual методы через модуль finance_accrual (totals() отдаёт
    те же ключи и знаки, что старый API). Контракт возврата и формула
    расходов не изменились.
    """
    try:
        def _to_float(v):
            try:
                if isinstance(v, bool):
                    return None
                return float(v)
            except Exception:
                return None

        def _signed_components(result_obj: dict) -> dict:
            return {
                "sale_commission": _to_float(result_obj.get("sale_commission", 0)) or 0.0,
                "processing_and_delivery": _to_float(result_obj.get("processing_and_delivery", 0)) or 0.0,
                "refunds_and_cancellations": _to_float(result_obj.get("refunds_and_cancellations", 0)) or 0.0,
                "services_amount": _to_float(result_obj.get("services_amount", 0)) or 0.0,
                "compensation_amount": _to_float(result_obj.get("compensation_amount", 0)) or 0.0,
                "money_transfer": _to_float(result_obj.get("money_transfer", 0)) or 0.0,
                "others_amount": _to_float(result_obj.get("others_amount", 0)) or 0.0,
            }

        def _expense_from_components(parts: dict) -> float:
            # Для ЛК итоговых начислений:
            # - базовые статьи считаем как расходы только когда они отрицательные;
            # - refunds_and_cancellations берём по модулю (в API может приходить разным знаком).
            base = 0.0
            for key in (
                "sale_commission",
                "processing_and_delivery",
                "services_amount",
                "compensation_amount",
                "money_transfer",
                "others_amount",
            ):
                v = parts.get(key, 0.0) or 0.0
                if v < 0:
                    base += abs(v)
            base += abs(parts.get("refunds_and_cancellations", 0.0) or 0.0)
            return base

        # Новый источник: свод начислений по дню (finance/accrual/by-day).
        result = finance_accrual.totals(date)
        print(f"[DEBUG totals all] {result}")

        sales = _to_float(result.get("accruals_for_sale", 0)) or 0.0
        parts_all = _signed_components(result)
        expenses_all = _expense_from_components(parts_all)

        # Строго как в ручном /totals all: расходы считаем только из all-компонент.
        expenses = expenses_all

        print(f"[DEBUG expense components all] {parts_all}")
        print(f"[DEBUG expense total all] {expenses_all:.2f}")
        print(f"[DEBUG expense source] all")

        print(f"✅ Финансы за {date}: Продажи={sales:.2f} ₽ | Расходы={expenses:.2f} ₽ | Прибыль={sales-expenses:.2f} ₽")
        return {"sales": sales, "expenses": expenses, "services_amount": parts_all.get("services_amount", 0.0)}

    except Exception as e:
        print(f"❌ Ошибка при запросе финансов: {e}")
        return {"sales": 0, "expenses": 0}


def _safe_number(value) -> float:
    try:
        if isinstance(value, bool):
            return 0.0
        if isinstance(value, str):
            value = value.replace(" ", "").replace(",", ".")
        return float(value or 0)
    except Exception:
        return 0.0


def get_performance_access_token():
    url = "https://api-performance.ozon.ru/api/client/token"
    body = {
        "client_id": os.getenv("OZON_PERF_CLIENT_ID"),
        "client_secret": os.getenv("OZON_PERF_CLIENT_SECRET"),
        "grant_type": "client_credentials"
    }
    try:
        resp = requests.post(url, data=body, timeout=30)
        resp.raise_for_status()
        token_data = resp.json()
        return token_data.get("access_token")
    except Exception as e:
        print(f"❌ Ошибка получения токена Performance API: {e}")
        return None


def fetch_ad_spend(date):
    """
    Получает расход на рекламу за дату через Ozon Performance API.
    Возвращает только сумму рекламных кампаний без агрегированных пакетов/спецпроектов.
    """
    token = get_performance_access_token()
    if not token:
        return 0.0

    url = "https://api-performance.ozon.ru/api/client/statistics/expense/json"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    params = {
        "dateFrom": date,
        "dateTo": date
    }

    try:
        response = safe_request(url, method="get", headers=headers, json=params, timeout=30)
        data = response.json()
        rows = data.get("rows", [])
        total_spend = 0.0
        seen_ids = set()

        for row in rows:
            row_id = row.get("id")
            title = str(row.get("title", "")).strip()
            spent = _safe_number(row.get("moneySpent", 0))

            # Агрегированные/итоговые строки без ID — пропускаем
            if row_id is None or row_id == 0:
                print(f"  [PERF SKIP aggregate] id={row_id!r} title={title!r} spent={spent:.2f}")
                continue

            if row_id in seen_ids:
                print(f"  [PERF SKIP duplicate] id={row_id!r} title={title!r}")
                continue
            seen_ids.add(row_id)

            # Пакеты и спецпроекты — агрегаты, могут дублировать кампании
            if "пакет" in title.lower() or "спецпроект" in title.lower():
                print(f"  [PERF SKIP bundle] id={row_id!r} title={title!r} spent={spent:.2f}")
                continue

            print(f"  [PERF INCLUDE] id={row_id!r} title={title!r} spent={spent:.2f}")
            total_spend += spent

        print(f"✅ Реклама за {date} (Performance API): {total_spend:.2f} ₽")
        return total_spend
    except Exception as e:
        print(f"❌ Ошибка получения расходов на рекламу: {e}")
        return 0.0


# --- Новая функция для получения среднего времени доставки
def fetch_ozon_delivery_speed(date):
    """
    Получает среднее время доставки (СВД) по API Ozon (average-delivery-time).
    Возвращает число (часы) или 0, если ошибка.
    """
    import os
    url = "https://api-seller.ozon.ru/v1/analytics/average-delivery-time"
    headers = {
        "Client-Id": os.getenv("OZON_CLIENT_ID"),
        "Api-Key": os.getenv("OZON_API_KEY"),
        "Content-Type": "application/json"
    }
    body = {
        "delivery_schema": "ALL",
        "supply_period": "ONE_WEEK"
    }
    try:
        r = safe_request(url, method="post", headers=headers, json=body, timeout=60)
        r.raise_for_status()
        data = r.json()
        # Ищем поле data["total"]["average_delivery_time"]
        avg = 0
        if "data" in data and "total" in data["data"]:
            avg = data["data"]["total"].get("average_delivery_time", 0)
        elif "total" in data:
            avg = data["total"].get("average_delivery_time", 0)
        # Приводим к int, если возможно
        try:
            delivery_speed = int(round(float(avg)))
        except Exception:
            delivery_speed = 0
        print(f"✅ Среднее время доставки (Ozon API /average-delivery-time): {delivery_speed} ч")
        return delivery_speed
    except Exception as e:
        print(f"❌ Ошибка при запросе среднего времени доставки (average-delivery-time): {e}")
        return 0


# --- Финальная функция обновления гугл-таблицы
def update_sheets(date):
    """
    Получает данные по продажам, расходам, СВД, считает метрики и пишет в Google Sheet.
    """
    finance = fetch_ozon_finance_totals(date)
    sales = float(finance.get("sales", 0))
    expenses = float(finance.get("expenses", 0))
    services_amount_raw = float(finance.get("services_amount", 0.0) or 0.0)
    services_abs = abs(services_amount_raw)

    # Умная проверка рекламы: Finance API иногда включает рекламу в services_amount, иногда нет.
    # Если services_amount (по модулю) >= 50% от рекламного расхода — реклама уже внутри.
    # Иначе — добавляем из Performance API отдельно.
    ad_spend = fetch_ad_spend(date)
    if ad_spend > 100 and services_abs < ad_spend * 0.5:
        print(f"[INFO] Реклама не найдена в Finance API (services={services_abs:.2f} < ad*0.5={ad_spend*0.5:.2f}), добавляем из Performance API: +{ad_spend:.2f} ₽")
        expenses += ad_spend
    else:
        print(f"[INFO] Реклама уже в Finance API или отсутствует (services={services_abs:.2f}, ad={ad_spend:.2f})")

    orders = fetch_ozon_revenue(date)
    # Получаем среднее время доставки
    delivery_speed = fetch_ozon_delivery_speed(date)
    # Доля МП = расходы / продажи
    share = (expenses / sales * 100) if sales > 0 else 0
    # Прибыль
    profit = sales - expenses

    # Новая логика: вставка по дате в нужный столбец, чтобы даты были по возрастанию
    try:
        sheet = get_operational_sheet()
        # Получаем все даты из первой строки (заголовок)
        header_dates = google_call("row_values(1)", sheet.row_values, 1)
        target_col = None
        # Приводим к строке для сравнения (Google Sheets может возвращать пустые ячейки)
        header_dates_clean = [d.strip() for d in header_dates]
        # Если дата уже есть в заголовке
        if date in header_dates_clean:
            target_col = header_dates_clean.index(date) + 1  # gspread columns start at 1
            print(f"[DEBUG] Дата {date} уже есть в колонке {target_col}")
        else:
            # Найти первую дату, которая меньше текущей (по возрастанию дат)
            # Все даты в формате YYYY-MM-DD
            insert_pos = None
            for idx, d in enumerate(header_dates_clean):
                try:
                    if d:
                        d_dt = datetime.strptime(d, "%Y-%m-%d")
                        cur_dt = datetime.strptime(date, "%Y-%m-%d")
                        if d_dt > cur_dt:
                            insert_pos = idx + 1  # вставлять перед этим столбцом
                            break
                except Exception:
                    continue
            if insert_pos is None:
                # Все даты раньше, вставляем в конец
                insert_pos = len(header_dates_clean) + 1
            # Вставляем пустой столбец для новой даты
            google_call("insert_cols", sheet.insert_cols, [[]], col=insert_pos)
            target_col = insert_pos
            print(f"[DEBUG] Вставляем новую дату {date} в колонку {target_col}")
        # Записываем значения в нужные строки
        import gspread.utils
        google_call("batch_update", sheet.batch_update, [{
            "range": f"{gspread.utils.rowcol_to_a1(1, target_col)}:{gspread.utils.rowcol_to_a1(7, target_col)}",
            "values": [
                [date],
                [round(orders, 2)],
                [round(sales, 2)],
                [round(expenses, 2)],
                [round(share, 2)],
                [round(profit, 2)],
                [delivery_speed],
            ]
        }])
        print(f"🟢 Данные за {date} обновлены в Google Sheet (колонка {target_col}):")
        print(f"  Заказы: {orders:.2f} ₽")
        print(f"  Продажи: {sales:.2f} ₽")
        print(f"  Расходы: {expenses:.2f} ₽")
        print(f"  Доля МП: {share:.2f} %")
        print(f"  Прибыль: {profit:.2f} ₽")
        print(f"  СВД: {delivery_speed} ч")
    except Exception as e:
        print(f"❌ Ошибка при записи в Google Sheet: {e}")

if __name__ == "__main__":
    update_sheets(target_date)
