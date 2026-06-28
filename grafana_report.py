import os
import math
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta
from datetime import date
from zoneinfo import ZoneInfo
import time
from constants import (
    sku_list,
    region_to_warehouses,
    clusters_id,
    offer_to_sku,
    clean_offer_to_product_id,
    row_map_sku,
)
import psycopg2

import finance_accrual
# === 1. Загружаем переменные из .env ===
load_dotenv()

DB_CONFIG = {
    "host": os.getenv("PG_HOST"),
    "port": int(os.getenv("PG_PORT", 5432)),
    "dbname": os.getenv("PG_DB"),
    "user": os.getenv("PG_USER"),
    "password": os.getenv("PG_PASSWORD"),
}

WEEKLY_UNIQUE_METRICS = [
    "session_view",
    "session_view_search",
    "session_view_pdp",
]

def delete_metrics_for_date(metric_date: date):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM public.ozon_metrics
            WHERE metric_date = %s
            """,
            (metric_date,)
        )
    print(f"[INFO] Удалены старые данные за {metric_date}")


def delete_weekly_metrics_for_date(metric_date: date, source: str = "ozon_weekly"):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM public.ozon_metrics
            WHERE metric_date = %s
              AND source = %s
            """,
            (metric_date, source)
        )
    print(f"[INFO] Удалены старые недельные данные за {metric_date} | source={source}")


pg_conn = psycopg2.connect(**DB_CONFIG)
pg_conn.autocommit = True

def insert_metric(
    metric_date: date,
    metric: str,
    value: float,
    sku_id: str | None = None,
    region: str | None = None,
    source: str = "ozon"
):
    with pg_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.ozon_metrics
            (metric_date, sku_id, region, metric, value, source)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (metric_date, sku_id, region, metric, value, source))


def replace_weekly_metrics_for_date(
    metric_date: date,
    weekly_values: dict,
    weekly_sku_values: dict,
    source: str = "ozon_weekly",
    cleanup_dates: list[date] | None = None
):
    rows = []

    for metric, value in weekly_values.items():
        rows.append((metric_date, None, None, metric, float(value), source))

    for sku_id, metrics in weekly_sku_values.items():
        for metric, value in metrics.items():
            rows.append((metric_date, str(sku_id), None, metric, float(value), source))

    if not rows:
        print(
            f"[WARN] Weekly replace skipped: no rows prepared | "
            f"metric_date={metric_date} | source={source}"
        )
        return False

    delete_dates = list(dict.fromkeys([metric_date] + (cleanup_dates or [])))
    old_autocommit = pg_conn.autocommit
    try:
        pg_conn.autocommit = False
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM public.ozon_metrics
                WHERE metric_date = ANY(%s)
                  AND source = %s
                """,
                (delete_dates, source)
            )
            deleted_rows = cur.rowcount
            cur.executemany(
                """
                INSERT INTO public.ozon_metrics
                (metric_date, sku_id, region, metric, value, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                rows
            )
        pg_conn.commit()
    except Exception as e:
        pg_conn.rollback()
        print(
            f"[ERROR] Weekly replace failed and was rolled back | "
            f"metric_date={metric_date} | source={source} | error={e}"
        )
        raise
    finally:
        pg_conn.autocommit = old_autocommit

    print(
        f"[INFO] Weekly replace committed | metric_date={metric_date} | "
        f"source={source} | cleanup_dates={delete_dates} | "
        f"deleted_rows={deleted_rows} | inserted_rows={len(rows)}"
    )
    return True


DAYS_SHIFT = 1  # 1 = вчера, 2 = позавчера, 14 = две недели назад и т.д.
REPORT_TZ = ZoneInfo(os.getenv("REPORT_TZ", "Asia/Novosibirsk"))
target_date = (datetime.now(REPORT_TZ) - timedelta(days=DAYS_SHIFT)).date()

# --- Управление режимами работы ---
SKIP_SVD_OVH = os.getenv("SKIP_SVD_OVH", "0") == "1"
print(f"[INFO] Используется дата: {target_date} | SKIP_SVD_OVH = {SKIP_SVD_OVH}")

# === Safe request helper ===
def safe_request(url, headers=None, json=None, retries=5, method="post", timeout=30):
    import inspect
    caller_name = inspect.stack()[1].function

    if "analytics/data" in url:
        print(
            f"\n📡 Analytics API call → {caller_name} | "
            f"metrics_count={len(json.get('metrics', [])) if json and 'metrics' in json else 'n/a'}, "
            f"metrics={json.get('metrics', []) if json and 'metrics' in json else 'n/a'}"
        )
    for attempt in range(retries):
        try:
            if method.lower() == "post":
                resp = requests.post(url, headers=headers, json=json, timeout=timeout)
            else:
                resp = requests.get(url, headers=headers, params=json, timeout=timeout)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                wait = min(5 * (2 ** attempt), 60)
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                wait = min(5 * (2 ** attempt), 60)
                time.sleep(wait)
                continue

            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            wait = 0.5 * (attempt + 1)
            time.sleep(wait)
            if attempt == retries - 1:
                print(f"❌ Ответ сервера при ошибке: {getattr(resp, 'text', None)}")
                raise e
            time.sleep(1)
            continue
    raise Exception(f"API error after {retries} retries for {url}")



# Создаём обратный словарь: product_id (sku) → offer_id
product_to_offer = {str(v): k for k, v in offer_to_sku.items()}

CLIENT_ID = os.getenv("OZON_CLIENT_ID")
API_KEY = os.getenv("OZON_API_KEY")
OZON_PERF_CLIENT_ID = os.getenv("OZON_PERF_CLIENT_ID")
OZON_PERF_CLIENT_SECRET = os.getenv("OZON_PERF_CLIENT_SECRET")

headers = {
    "Client-Id": CLIENT_ID,
    "Api-Key": API_KEY
}

# Определяем дату "вчера"
date_from = target_date
date_to = target_date

SVOD_METRICS = [
    "revenue",
    "ordered_units",
    "delivered_units",

    "hits_view",
    "hits_view_search",
    "hits_view_pdp",

    "hits_tocart",
    #"hits_tocart_search",
    #"hits_tocart_pdp",

    "session_view",
    "session_view_search",
    "session_view_pdp",

    "conv_tocart",
    "conv_tocart_search",
    "conv_tocart_pdp",

    "position_category"
]



SKU_METRICS = SVOD_METRICS.copy()



# === Helpers for date/time and numbers ===
def to_zulu(d: str, end: bool = False) -> str:
    return f"{d}T23:59:59Z" if end else f"{d}T00:00:00Z"

def safe_number(x):
    try:
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str):
            return float(x.replace(',', '.'))
    except Exception:
        return 0.0
    return 0.0


def align_cart_conversions_with_lk(values):
    """
    Ozon Analytics API returns conv_tocart* against session_view*,
    while the seller cabinet tiles use hits_view* as denominator.
    Recalculate conversions so Grafana matches the values from LK.
    """
    if not values:
        return values

    hits_tocart = safe_number(values.get("hits_tocart"))
    hits_view = safe_number(values.get("hits_view"))
    hits_view_pdp = safe_number(values.get("hits_view_pdp"))
    hits_view_search = safe_number(values.get("hits_view_search"))
    session_view_pdp = safe_number(values.get("session_view_pdp"))
    session_view_search = safe_number(values.get("session_view_search"))
    conv_tocart_pdp_api = safe_number(values.get("conv_tocart_pdp"))
    conv_tocart_search_api = safe_number(values.get("conv_tocart_search"))

    if hits_view > 0:
        values["conv_tocart"] = round((hits_tocart / hits_view) * 100, 2)

    hits_tocart_pdp = (conv_tocart_pdp_api / 100) * session_view_pdp
    hits_tocart_search = (conv_tocart_search_api / 100) * session_view_search

    if hits_view_pdp > 0:
        values["conv_tocart_pdp"] = round((hits_tocart_pdp / hits_view_pdp) * 100, 2)
    if hits_view_search > 0:
        values["conv_tocart_search"] = round((hits_tocart_search / hits_view_search) * 100, 2)

    return values


def get_week_bounds(day: date) -> tuple[date, date]:
    weekday = day.weekday()  # Monday = 0
    week_monday = day - timedelta(days=weekday)
    week_sunday = week_monday + timedelta(days=6)
    return week_monday, week_sunday


def get_analytics_result(j, context: str):
    if not isinstance(j, dict):
        print(f"[ERROR] {context}: unexpected response type={type(j).__name__}")
        return None

    result = j.get("result")
    if not isinstance(result, dict):
        print(f"[ERROR] {context}: missing/invalid result | response_keys={list(j.keys())}")
        return None

    return result


def map_weekly_metrics(metrics_list, response_metrics):
    result = {metric: 0.0 for metric in WEEKLY_UNIQUE_METRICS}
    for idx, metric_name in enumerate(response_metrics):
        if metric_name in result:
            result[metric_name] = safe_number(metrics_list[idx]) if idx < len(metrics_list) else 0.0
    return result


def fetch_weekly_unique_visitors(monday: date, sunday: date):
    body = {
        "date_from": monday.isoformat(),
        "date_to": sunday.isoformat(),
        "metrics": WEEKLY_UNIQUE_METRICS,
        "dimensions": ["day"],
        "filters": [
            {"key": "state", "value": "ACTIVE"},
            {"key": "visibility", "value": "ALL"}
        ],
        "limit": 1000
    }

    try:
        j = safe_request(
            "https://api-seller.ozon.ru/v1/analytics/data",
            headers=headers,
            json=body,
            method="post",
            timeout=30
        )
    except Exception as e:
        print(f"Ошибка запроса weekly уников: {e}")
        return None

    api_result = get_analytics_result(j, "weekly aggregate")
    if api_result is None:
        return None

    response_metrics = api_result.get("metrics") or WEEKLY_UNIQUE_METRICS
    totals = api_result.get("totals") or []
    data = api_result.get("data") or []

    print(
        f"[INFO] Weekly aggregate API response | week={monday}..{sunday} | "
        f"totals_len={len(totals)} | data_rows={len(data)} | metrics={response_metrics}"
    )

    if totals:
        if len(totals) < len(response_metrics):
            print(
                f"[WARN] Weekly aggregate totals shorter than metrics | "
                f"totals_len={len(totals)} | metrics_len={len(response_metrics)}"
            )
        return map_weekly_metrics(totals, response_metrics)

    if not data:
        print(
            f"[WARN] Weekly aggregate API returned no totals and no data | "
            f"week={monday}..{sunday}; keep old weekly rows"
        )
        return None

    print(
        f"[WARN] Weekly aggregate totals missing; falling back to data sum | "
        f"week={monday}..{sunday}"
    )
    sums = {metric: 0.0 for metric in WEEKLY_UNIQUE_METRICS}
    for row in data:
        metrics_list = row.get("metrics") or []
        row_values = map_weekly_metrics(metrics_list, response_metrics)
        for metric_name, value in row_values.items():
            sums[metric_name] += value
    return sums


def fetch_weekly_unique_visitors_by_sku(monday: date, sunday: date):
    body = {
        "date_from": monday.isoformat(),
        "date_to": sunday.isoformat(),
        "metrics": WEEKLY_UNIQUE_METRICS,
        "dimensions": ["sku"],
        "filters": [
            {"key": "state", "value": "ACTIVE"},
            {"key": "visibility", "value": "ALL"}
        ],
        "limit": 1000
    }

    try:
        j = safe_request(
            "https://api-seller.ozon.ru/v1/analytics/data",
            headers=headers,
            json=body,
            method="post",
            timeout=30
        )
    except Exception as e:
        print(f"Ошибка запроса weekly уников по SKU: {e}")
        return None

    results = {}
    api_result = get_analytics_result(j, "weekly by SKU")
    if api_result is None:
        return None

    data = api_result.get("data") or []
    response_metrics = api_result.get("metrics") or WEEKLY_UNIQUE_METRICS
    skipped_without_dimensions = 0
    skipped_without_mapping = 0

    print(
        f"[INFO] Weekly by SKU API response | week={monday}..{sunday} | "
        f"data_rows={len(data)} | metrics={response_metrics}"
    )

    if not data:
        print(
            f"[WARN] Weekly by SKU API returned no data | "
            f"week={monday}..{sunday}; keep old weekly rows"
        )
        return None

    for row in data:
        dimensions = row.get("dimensions") or []
        if not dimensions:
            skipped_without_dimensions += 1
            continue

        product_id = str(dimensions[0].get("id"))
        offer_id = product_to_offer.get(product_id)
        if not offer_id:
            skipped_without_mapping += 1
            continue

        metrics_list = row.get("metrics") or []
        results[str(offer_id)] = map_weekly_metrics(metrics_list, response_metrics)

    if skipped_without_dimensions or skipped_without_mapping:
        print(
            f"[WARN] Weekly by SKU skipped rows | "
            f"without_dimensions={skipped_without_dimensions} | "
            f"without_mapping={skipped_without_mapping} | mapped_rows={len(results)}"
        )

    if data and not results:
        print(
            f"[ERROR] Weekly by SKU API returned rows, but none mapped to offer_id | "
            f"data_rows={len(data)}; keep old weekly rows"
        )
        return None

    return results


def upsert_last_full_week_unique_metrics():
    # Скрипт обычно запускается в понедельник с DAYS_SHIFT=1,
    # поэтому target_date указывает на воскресенье только что закрытой недели.
    # Weekly-метрики пишем датой начала недели: Grafana WOW-грид строит
    # колонки по понедельникам недель.
    if target_date.weekday() != 6:
        print(
            f"[INFO] target_date={target_date} не воскресенье, "
            "weekly source не обновляю."
        )
        return

    week_start, week_end = get_week_bounds(target_date)
    metric_date = week_start
    weekly_values = fetch_weekly_unique_visitors(week_start, week_end)
    if weekly_values is None:
        print(
            f"[WARN] Weekly aggregate is not reliable; skip DB replace | "
            f"week={week_start}..{week_end}"
        )
        return

    weekly_sku_values = fetch_weekly_unique_visitors_by_sku(week_start, week_end)
    if weekly_sku_values is None:
        print(
            f"[WARN] Weekly by SKU is not reliable; skip DB replace | "
            f"week={week_start}..{week_end}"
        )
        return

    if not weekly_values and not weekly_sku_values:
        print(
            f"[WARN] Недельные уники пустые, пропускаю запись weekly source | "
            f"week={week_start}..{week_end}"
        )
        return

    if not replace_weekly_metrics_for_date(
        metric_date=metric_date,
        weekly_values=weekly_values,
        weekly_sku_values=weekly_sku_values,
        source="ozon_weekly",
        cleanup_dates=[week_end]
    ):
        return

    print(
        f"[INFO] Записаны недельные уники за {week_start}..{week_end} "
        f"в source=ozon_weekly | metric_date={metric_date} | "
        f"total_metrics={len(weekly_values)} | sku_rows={len(weekly_sku_values)}"
    )


# === Monthly unique visitors (source='ozon_monthly') ===
# Уники не суммируются, поэтому месячные значения тянем напрямую из Ozon Analytics
# за период 1-е..последнее число (как недельные за неделю), а не агрегируем из дневных.
def get_month_bounds(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    next_first = date(first.year + (first.month // 12), (first.month % 12) + 1, 1)
    return first, next_first - timedelta(days=1)


def fetch_monthly_unique_visitors(first: date, last: date):
    """Месячные уники (totals за период). Метрики те же, что и недельные."""
    return fetch_weekly_unique_visitors(first, last)


def _write_monthly_uniques(month_start: date, month_end: date) -> bool:
    monthly_values = fetch_monthly_unique_visitors(month_start, month_end)
    if not monthly_values:
        print(f"[WARN] Monthly uniques пустые, пропускаю | month={month_start}..{month_end}")
        return False
    ok = replace_weekly_metrics_for_date(
        metric_date=month_start,
        weekly_values=monthly_values,
        weekly_sku_values={},
        source="ozon_monthly",
        cleanup_dates=[month_end],
    )
    if ok:
        print(
            f"[INFO] Записаны месячные уники за {month_start}..{month_end} "
            f"в source=ozon_monthly | metrics={len(monthly_values)}"
        )
    return ok


def upsert_last_full_month_unique_metrics():
    # Daily collector работает с DAYS_SHIFT=1 → target_date = вчера.
    # Месячные уники обновляем в 1-й день месяца: target_date тогда = последнее
    # число прошлого месяца. Пишем датой 1-го числа этого месяца (Grafana MoM-грид
    # строит колонки по началу месяца), source='ozon_monthly'.
    if (target_date + timedelta(days=1)).day != 1:
        print(
            f"[INFO] target_date={target_date} не последний день месяца, "
            "monthly source не обновляю."
        )
        return
    month_start, month_end = get_month_bounds(target_date)
    _write_monthly_uniques(month_start, month_end)


def backfill_monthly_uniques(months):
    """months: список (year, month). Бэкфилл месячных уников в source='ozon_monthly'."""
    for (y, m) in sorted(months):
        month_start, month_end = get_month_bounds(date(y, m, 1))
        _write_monthly_uniques(month_start, month_end)


# === Fetch FBS/FBO postings and aggregate units, revenue ===
def fetch_postings(schema: str, date_from: str, date_to: str):
    url = f"https://api-seller.ozon.ru/v3/posting/{schema}/list"
    body = {
        "filter": {
            "processed_at": {
                "from": to_zulu(date_from, False),
                "to": to_zulu(date_to, True)
            }
        },
        "limit": 1000
    }
    try:
        data = safe_request(url, headers=headers, json=body, method="post", timeout=30)
    except Exception:
        return 0, 0.0, 0, 0.0

    # API can return {result: {postings: [...]}} или {result: [...]}
    postings = data.get("result", {}).get("postings") if isinstance(data.get("result"), dict) else data.get("result", [])
    postings = postings or []

    ordered_units_total = 0
    ordered_revenue_total = 0.0
    delivered_units_total = 0
    delivered_revenue_total = 0.0

    for p in postings:
        status = p.get("status", "")
        prods = p.get("products", [])
        for pr in prods:
            qty = pr.get("quantity") or pr.get("qty") or 1
            price = pr.get("price") or pr.get("offer_price") or pr.get("sale_price") or 0
            try:
                qty = int(qty)
            except Exception:
                qty = 1
            price = safe_number(price.get("amount", 0) if isinstance(price, dict) else price)
            amount = qty * price

            ordered_units_total += qty
            ordered_revenue_total += amount
            if status == "delivered":
                delivered_units_total += qty
                delivered_revenue_total += amount

    return ordered_units_total, ordered_revenue_total, delivered_units_total, delivered_revenue_total

def fetch_one_metric(metric: str):
    body = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": [metric],
        "dimensions": ["day"],
        "limit": 1000
    }
    try:
        j = safe_request("https://api-seller.ozon.ru/v1/analytics/data", headers=headers, json=body, method="post", timeout=30)
        data = j.get("result", {}).get("data", [])
        if data and data[0].get("metrics"):
            return data[0]["metrics"][0]
    except Exception:
        pass
    return 0

def fetch_all_metrics(metrics, date_from, date_to):
    body = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": metrics,
        "dimensions": ["day"],
        "limit": 1000
    }

    j = safe_request(
        "https://api-seller.ozon.ru/v1/analytics/data",
        headers=headers,
        json=body,
        method="post",
        timeout=30
    )
    data = j.get("result", {}).get("data", [])

    if not data:
        return {m: 0 for m in metrics}

    # OZON analytics may not return result.metrics; fallback to requested metrics order
    response_metrics = j.get("result", {}).get("metrics") or metrics
    values = {}

    metrics_list = data[0].get("metrics", [])
    for i, name in enumerate(response_metrics):
        if i < len(metrics_list):
            values[name] = safe_number(metrics_list[i])
        else:
            values[name] = 0.0

    return align_cart_conversions_with_lk(values)
    print("RAW RESPONSE:", j)

def fetch_sku_metrics(sku_id: int, date_from: str, date_to: str):
    """
    Забирает метрики по конкретному SKU (product_id от Озон)
    """
    body = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": list(row_map_sku.values()),
        "dimensions": ["day", "sku"],
        "filters": [
            {"key": "sku", "value": str(sku_id)}
        ],
        "limit": 1000
    }
    try:
        j = safe_request("https://api-seller.ozon.ru/v1/analytics/data", headers=headers, json=body, method="post", timeout=30)
        data = j.get("result", {}).get("data", [])
        if not data:
            return {}

        metrics_keys = list(row_map_sku.values())
        sum_metrics = {m: 0.0 for m in metrics_keys}
        count_metrics = {m: 0 for m in metrics_keys}

        for row in data:
            metrics_list = row.get("metrics", [])
            for i, m in enumerate(metrics_keys):
                val = safe_number(metrics_list[i]) if i < len(metrics_list) else 0.0
                if m.startswith("conv_"):
                    sum_metrics[m] += val
                    count_metrics[m] += 1
                else:
                    sum_metrics[m] += val

        result = {}
        for m in metrics_keys:
            if m.startswith("conv_"):
                result[m] = (sum_metrics[m] / count_metrics[m]) if count_metrics[m] > 0 else 0.0
            else:
                result[m] = sum_metrics[m]
        return align_cart_conversions_with_lk(result)

    except Exception as e:
        print(f"❌ Ошибка при получении метрик для SKU {sku_id}: {e}")
        return {}

def fetch_prices_by_offer_ids(offer_ids):
    """
    Получает marketing_seller_price и min_price через v5/product/info/prices.
    Запрос ТОЛЬКО по product_id.
    product_id берётся из словаря clean_offer_to_product_id.
    Возвращает: {offer_id: {"price": ..., "old_price": ...}}
    """

    offer_ids = [str(oid) for oid in offer_ids]

    # Собираем product_id по словарю
    product_ids = []
    for oid in offer_ids:
        pid = clean_offer_to_product_id.get(oid)
        if pid:
            product_ids.append(pid)

    if not product_ids:
        return {oid: {"price": None, "old_price": None} for oid in offer_ids}

    url = "https://api-seller.ozon.ru/v5/product/info/prices"

    body = {
        "cursor": "",
        "filter": {
            "product_id": product_ids,
            "visibility": "ALL"
        },
        "limit": 100
    }

    try:
        j = safe_request(url, headers=headers, json=body, method="post", timeout=60)
        items = j.get("items", [])
    except Exception as e:
        print(f"❌ Ошибка получения цен v5: {e}")
        return {oid: {"price": None, "old_price": None} for oid in offer_ids}

    # Готовим структуру под результат
    prices_data = {oid: {"price": None, "old_price": None} for oid in offer_ids}

    # Делаем обратный словарь product_id → offer_id
    product_to_offer = {v: k for k, v in clean_offer_to_product_id.items()}

    for item in items:
        pid = item.get("product_id")
        offer_id = product_to_offer.get(pid)

        if not offer_id:
            continue

        price_block = item.get("price", {}) or {}
        marketing = price_block.get("marketing_seller_price")
        min_price = price_block.get("min_price")

        prices_data[offer_id] = {
            "marketing_price": safe_number(marketing) if marketing is not None else None,
            "price": safe_number(min_price) if min_price is not None else None
        }

    return prices_data


def fetch_all_sku_metrics(sku_list, date_from, date_to):
    body = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": list(row_map_sku.values()),
        "dimensions": ["sku", "day"],
        "filters": [],
        "limit": 1000
    }
    metrics_keys = list(row_map_sku.values())
    # Для каждого offer_id: sum обычных, sum conv_ и count conv_
    results = {offer_id: {m: 0.0 for m in metrics_keys} for offer_id in sku_list}
    conv_counts = {offer_id: {m: 0 for m in metrics_keys if m.startswith("conv_")} for offer_id in sku_list}
    try:
        j = safe_request("https://api-seller.ozon.ru/v1/analytics/data", headers=headers, json=body, method="post", timeout=30)
        data = j.get("result", {}).get("data", [])
        for row in data:
            dims = row.get("dimensions", [])
            if not dims:
                continue

            # OZON returns product_id (SKU), convert to offer_id
            product_id = str(dims[0].get("id"))
            offer_id = product_to_offer.get(product_id)

            if not offer_id:
                continue

            if offer_id not in results:
                continue

            metrics_list = row.get("metrics", [])
            response_metrics = j.get("result", {}).get("metrics") or metrics_keys

            for i, metric_name in enumerate(response_metrics):
                if metric_name not in results[offer_id]:
                    continue
                val = safe_number(metrics_list[i]) if i < len(metrics_list) else 0.0
                if metric_name.startswith("conv_"):
                    results[offer_id][metric_name] += val
                    conv_counts[offer_id][metric_name] = conv_counts[offer_id].get(metric_name, 0) + 1
                else:
                    results[offer_id][metric_name] += val
        # Теперь усредняем conv_ метрики
        for offer_id in sku_list:
            for m in metrics_keys:
                if m.startswith("conv_"):
                    count = conv_counts[offer_id].get(m, 0)
                    results[offer_id][m] = (results[offer_id][m] / count) if count > 0 else 0.0
    except Exception as e:
        print(f"Ошибка при пакетном получении метрик по SKU: {e}")
    for offer_id in sku_list:
        results[offer_id] = align_cart_conversions_with_lk(results[offer_id])
    return results

def fetch_delivered_units(date_from, date_to):
    # Используем fetch_postings для подсчёта доставленных товаров
    ordered_units, revenue, delivered_units, delivered_revenue = 0, 0.0, 0, 0.0
    for schema in ["fbs", "fbo"]:
        ou, rev, du, dr = fetch_postings(schema, date_from, date_to)
        ordered_units += ou
        revenue += rev
        delivered_units += du
        delivered_revenue += dr
    return delivered_units

# === Performance API: Получение токена и расходов на рекламу ===
def get_performance_access_token():
    url = "https://api-performance.ozon.ru/api/client/token"
    print(f"Requesting Performance API token from URL: {url}")
    body = {
        "client_id": OZON_PERF_CLIENT_ID,
        "client_secret": OZON_PERF_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    try:
        # Performance API token endpoint requires form data, not JSON
        resp = requests.post(url, data=body)
        print("Token response:", resp.json())
        resp.raise_for_status()
        token_data = resp.json()
        return token_data.get("access_token")
    except Exception as e:
        print(f"Ошибка получения токена Performance API: {e}, текст: {resp.text if 'resp' in locals() else ''}")
        return None

def fetch_ad_spend_yesterday():
    token = get_performance_access_token()
    if not token:
        return 0
    url = "https://api-performance.ozon.ru/api/client/statistics/expense/json"
    date_yesterday = target_date
    headers_perf = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    params = {
        "dateFrom": date_yesterday,
        "dateTo": date_yesterday
    }
    try:
        data = safe_request(url, headers=headers_perf, json=params, method="get", timeout=30)
        print("Perf API expense response:", data)
        rows = data.get("rows", [])
        total_spend = 0.0
        seen_ids = set()

        for row in rows:
            row_id = row.get("id")
            title = str(row.get("title", "")).strip()

            # Пропускаем дубликаты и агрегированные пакеты
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            if "пакет" in title.lower() or "спецпроект" in title.lower() or not any(ch.isdigit() for ch in title):
                continue

            total_spend += safe_number(row.get("moneySpent", 0))

        # Проверка на аномалии
        if total_spend > 100000:
            print(f"[ALERT] Возможное дублирование или агрегат: {total_spend} ₽ за {date_yesterday}")

        return total_spend
    except Exception as e:
        print(f"Ошибка получения расходов на рекламу: {e}")
        return 0


# --- Новый метод: Получение заказов по каждому SKU через /v1/analytics/data ---
def fetch_revenue_by_sku_analytics(date_from: str, date_to: str):
    url = "https://api-seller.ozon.ru/v1/analytics/data"
    metric = "revenue"
    sales_by_sku = {}

    body = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": [metric],
        "dimensions": ["sku", "day"],
        "limit": 1000
    }

    j = safe_request(url, headers=headers, json=body, method="post", timeout=60)
    data = j.get("result", {}).get("data", [])

    print("[DEBUG][ANALYTICS RAW] rows =", len(data))

    for row in data:
        dims = row.get("dimensions", [])
        if not dims:
            continue

        sku_id = str(dims[0].get("id"))
        metrics_list = row.get("metrics", [])
        revenue = safe_number(metrics_list[0]) if metrics_list else 0.0

        offer_id = product_to_offer.get(sku_id)
        if not offer_id:
            continue

        sales_by_sku[offer_id] = sales_by_sku.get(offer_id, 0) + revenue

        print(f"[DEBUG][ANALYTICS SKU] offer_id={offer_id} sku_id={sku_id} revenue={revenue}")

    return sales_by_sku

def fetch_geography_sales(date_from: str, date_to: str):
    """
    Получает географию продаж по каждому offer_id через /v3/posting/fbo/list.
    Возвращает dict: {offer_id: {region_name: units_sold, ...}, ...}
    """
    geo_by_sku = {}

    def fetch():
        url = f"https://api-seller.ozon.ru/v3/posting/fbo/list"
        cursor = ""
        while True:
            body = {
                "sort_dir": "ASC",
                "filter": {
                    "since": f"{date_from}T00:00:00.000Z",
                    "to": f"{date_to}T23:59:59.000Z"
                },
                "limit": 100,
                "cursor": cursor,
                "with": {
                    "analytics_data": True,
                    "financial_data": False,
                    "legal_info": False
                }
            }
            try:
                j = safe_request(url, headers=headers, json=body, method="post", timeout=120)
                data = j.get("postings", [])
            except Exception as e:
                print(f"❌ Ошибка fbo/list (география): {e}")
                break

            if not data:
                break

            for posting in data:
                analytics = posting.get("analytics_data", {}) or {}
                warehouse_id = str(analytics.get("warehouse_id", ""))

                # Определяем регион по складу
                region_name = None
                for region, wh_list in region_to_warehouses.items():
                    if warehouse_id in wh_list:
                        region_name = region
                        break
                if not region_name:
                    region_name = "Прочее"

                for it in posting.get("products", []):
                    product_id = str(it.get("sku") or it.get("product_id") or "")
                    if not product_id:
                        continue
                    offer_id = product_to_offer.get(product_id)
                    if not offer_id:
                        continue
                    qty = it.get("quantity") or 1
                    try:
                        qty = int(qty)
                    except Exception:
                        qty = 1

                    if offer_id not in geo_by_sku:
                        geo_by_sku[offer_id] = {}
                    geo_by_sku[offer_id][region_name] = geo_by_sku[offer_id].get(region_name, 0) + qty

            cursor = j.get("cursor", "")
            if not j.get("has_next", False):
                break
            time.sleep(0.2)

    # Собираем только fbo
    fetch()

    return geo_by_sku

def fetch_delivery_speed_geo():
    """
    Получает среднее время доставки (СВД) по регионам для листа 'Свод'.
    Использует API метод /v1/analytics/average-delivery-time.
    """
    import requests, os, time

    url = "https://api-seller.ozon.ru/v1/analytics/average-delivery-time"
    headers = {
        "Client-Id": os.getenv("OZON_CLIENT_ID"),
        "Api-Key": os.getenv("OZON_API_KEY"),
        "Content-Type": "application/json"
    }
    payload = {
        "delivery_schema": "ALL",
        "supply_period": "EIGHT_WEEKS",
        "sku": [],
    }

    # обратное сопоставление (delivery_cluster_id → регион)
    id_to_region = {v: k for k, v in clusters_id.items()}
    result = {}

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            print("⚠️ Нет данных СВД для географии.")
            return {}

        for entry in data:
            delivery_cluster_id = entry.get("delivery_cluster_id")
            metrics = entry.get("metrics", {})
            avg_time = metrics.get("average_delivery_time", 0)

            region_name = id_to_region.get(delivery_cluster_id)
            if not region_name:
                continue

            result[region_name] = round(float(avg_time or 0), 2)
            print(f"✅ {region_name}: {avg_time} ч")

        print(f"📦 Всего регионов: {len(result)}")
        return result

    except Exception as e:
        print(f"❌ Ошибка при получении географии СВД: {e}")
        return {}

# --- Новый метод: Получение общего среднего времени доставки через summary ---
def fetch_delivery_speed_summary():
    """
    Получает общее среднее время доставки (СВД) по всему магазину.
    Использует API метод /v1/analytics/average-delivery-time/summary.
    Применяется для листа 'Свод' — строка 'Среднее время доставки, общее'.
    """
    import requests, os

    url = "https://api-seller.ozon.ru/v1/analytics/average-delivery-time/summary"
    headers = {
        "Client-Id": os.getenv("OZON_CLIENT_ID"),
        "Api-Key": os.getenv("OZON_API_KEY"),
        "Content-Type": "application/json"
    }
    payload = {
        "delivery_schema": "ALL",
        "supply_period": "EIGHT_WEEKS"
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        avg = data.get("average_delivery_time", 0)
        print(f"✅ fetch_delivery_speed_summary: {avg} ч")
        return {"avg": avg}

    except Exception as e:
        print(f"❌ Ошибка запроса summary СВД: {e}")
        return {"avg": 0}

# --- Новый метод: Получение средней скорости доставки по каждому SKU и регионам ---
def fetch_delivery_speed_details(sku_id=None):
    """
    Вариант А — оптимизированный сбор СВД:
    • Один цикл по всем кластерам (region_name → cluster_id)
    • Один запрос на кластер = получаем данные по ВСЕМ SKU сразу
    • Для каждого SKU собираем средневзвешенное время доставки
    """
    import time
    import os

    url = "https://api-seller.ozon.ru/v1/analytics/average-delivery-time/details"
    headers = {
        "Client-Id": os.getenv("OZON_CLIENT_ID"),
        "Api-Key": os.getenv("OZON_API_KEY"),
        "Content-Type": "application/json"
    }

    # sku: {region: time}
    result = {}
    # Общий СВД по SKU — отдельный проход "как в daily" (all clusters, FOUR_WEEKS -> EIGHT_WEEKS)
    overall_weighted_sum = {}
    overall_orders_sum = {}
    overall_simple_sum = {}
    overall_simple_cnt = {}

    def _num(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    def _orders_total(metrics):
        oc = (metrics or {}).get("orders_count", 0)
        if isinstance(oc, dict):
            return _num(oc.get("total", 0))
        return _num(oc)

    def _collect_overall_like_daily():
        # Нормализация карты offer<->sku как в daily
        target_offers = {str(sku_id)} if sku_id else {str(k) for k in offer_to_sku.keys()}
        offer_to_target_sku = {}
        for offer in target_offers:
            sku_val = offer_to_sku.get(offer)
            if sku_val in (None, ""):
                continue
            try:
                offer_to_target_sku[offer] = int(sku_val)
            except Exception:
                continue
        if not offer_to_target_sku:
            return target_offers

        sku_to_offer = {v: k for k, v in offer_to_target_sku.items()}
        used_clusters = {int(v) for v in clusters_id.values() if str(v).isdigit()}
        svd_periods = ["FOUR_WEEKS", "EIGHT_WEEKS"]

        for period in svd_periods:
            need_offers = {o for o in offer_to_target_sku if o not in overall_simple_cnt}
            if not need_offers:
                break
            for cid in used_clusters:
                offset = 0
                while True:
                    body = {
                        "cluster_id": int(cid),
                        "filters": {
                            "delivery_schema": "ALL",
                            "supply_period": period
                        },
                        "limit": 1000,
                        "offset": offset
                    }
                    try:
                        data = safe_request(url, headers=headers, json=body, method="post", timeout=30)
                    except Exception:
                        break

                    rows = data.get("data", [])
                    if not rows:
                        break

                    for row in rows:
                        item = row.get("item") or {}
                        metrics = row.get("metrics") or {}
                        t = _num(metrics.get("average_delivery_time"))
                        if t <= 0:
                            continue

                        offer_raw = str(item.get("offer_id") or "").strip()
                        sku_raw = item.get("sku")
                        offer_by_sku = None
                        if sku_raw not in (None, ""):
                            try:
                                offer_by_sku = sku_to_offer.get(int(sku_raw))
                            except Exception:
                                pass

                        matched_offer = None
                        if offer_raw in need_offers:
                            matched_offer = offer_raw
                        elif offer_by_sku in need_offers:
                            matched_offer = offer_by_sku

                        if not matched_offer:
                            continue

                        orders = _orders_total(metrics)
                        if orders > 0:
                            overall_weighted_sum[matched_offer] = overall_weighted_sum.get(matched_offer, 0.0) + (t * orders)
                            overall_orders_sum[matched_offer] = overall_orders_sum.get(matched_offer, 0.0) + orders
                        overall_simple_sum[matched_offer] = overall_simple_sum.get(matched_offer, 0.0) + t
                        overall_simple_cnt[matched_offer] = overall_simple_cnt.get(matched_offer, 0) + 1

                    if len(rows) < 1000:
                        break
                    offset += 1000
        return target_offers

    target_offers = _collect_overall_like_daily()

    for region_name, cluster_id in clusters_id.items():
        offset = 0
        seen = {}

        while True:
            body = {
                "cluster_id": cluster_id,
                "filters": {
                    "delivery_schema": "ALL",
                    "supply_period": "ONE_DAY"
                },
                "limit": 1000,
                "offset": offset
            }

            try:
                data = safe_request(url, headers=headers, json=body, method="post", timeout=30)
            except Exception as e:
                err_text = str(e).lower()
                if "no such cluster" in err_text:
                    print(f"⚠️ Пропускаем кластер {region_name} — не поддерживается /details")
                    break
                else:
                    print(f"❌ Ошибка при запросе к кластеру {region_name}: {e}")
                    break

            rows = data.get("data", [])
            if not rows:
                break

            for row in rows:
                item = row.get("item", {})
                sku_raw = str(item.get("sku") or "")
                if not sku_raw:
                    continue

                offer_id = product_to_offer.get(sku_raw)
                if not offer_id:
                    continue

                sku_val = str(offer_id)

                # фильтр только нужного SKU, если sku_id указан
                if sku_id and sku_val != str(sku_id):
                    continue

                clusters_data = row.get("clusters_data", [])
                if clusters_data:
                    total_time = 0
                    total_orders = 0
                    for c in clusters_data:
                        t = c.get("delivery_time_FBO") or c.get("delivery_time_FBS") or 0
                        cnt = c.get("orders_count", 0)
                        total_time += t * cnt
                        total_orders += cnt
                    time_avg = round(total_time / total_orders, 2) if total_orders else 0
                else:
                    time_avg = round(float(row.get("metrics", {}).get("average_delivery_time", 0)), 2)

                if sku_val not in result:
                    result[sku_val] = {}
                result[sku_val][region_name] = time_avg

            if len(rows) < 1000:
                break
            offset += 1000
            time.sleep(0.2)

    # Финализация общего СВД по SKU — как в dailyhour.py
    offer_final = {}
    for offer in overall_simple_cnt:
        if overall_orders_sum.get(offer, 0) > 0:
            offer_final[offer] = overall_weighted_sum[offer] / overall_orders_sum[offer]
        else:
            offer_final[offer] = overall_simple_sum[offer] / overall_simple_cnt[offer]

    if offer_final:
        store_avg = int(math.ceil((sum(offer_final.values()) / len(offer_final)) - 1e-9))
    else:
        store_avg = 0

    # Формирование финального ответа
    if sku_id:
        sku_str = str(sku_id)
        by_region = result.get(sku_str, {})
        if sku_str in offer_final:
            overall = int(math.ceil(offer_final[sku_str] - 1e-9))
        else:
            overall = store_avg
        return {"overall": {"avg": overall}, "by_region": by_region}

    # если не указан sku_id, вернуть данные для всех целевых offer (как в daily-подходе)
    output = {}
    for sku_val in sorted(target_offers):
        reg_times = result.get(sku_val, {})
        if sku_val in offer_final:
            avg = int(math.ceil(offer_final[sku_val] - 1e-9))
        else:
            avg = store_avg
        output[sku_val] = {"overall": {"avg": avg}, "by_region": reg_times}

    return output

def fetch_performance_stats(date_from: str, date_to: str):
    import requests, os

    # === 1️⃣ Получаем токен ===
    token_url = "https://api-performance.ozon.ru/api/client/token"
    data = {
        "client_id": os.getenv("OZON_PERF_CLIENT_ID"),
        "client_secret": os.getenv("OZON_PERF_CLIENT_SECRET"),
        "grant_type": "client_credentials"
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    token_resp = requests.post(token_url, data=data, headers=headers, timeout=30)
    token_resp.raise_for_status()
    access_token = token_resp.json().get("access_token")
    print("✅ Performance API token получен")

    # === 2️⃣ Запрашиваем статистику по кампаниям ===
    stats_url = "https://api-performance.ozon.ru/api/client/statistics/campaign/media/json"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "dateFrom": date_from,
        "dateTo": date_to
    }

    r = requests.get(stats_url, headers=headers, params=params, timeout=60)
    if not r.text.strip():
        print(f"⚠️ Пустой ответ Performance API (status={r.status_code})")
        return {"ctr": 0, "shows": 0}

    try:
        data = r.json()
    except Exception:
        print(f"❌ Ошибка декодирования JSON Performance API: {r.status_code}, text={r.text[:300]}")
        return {"ctr": 0, "shows": 0}

    rows = data.get("rows", [])
    if not rows:
        print(f"⚠️ Нет данных Performance API за период {date_from}–{date_to}")
        return {"ctr": 0, "shows": 0}

    total_shows = sum(int(row.get("shows", 0)) for row in rows)
    total_clicks = sum(int(row.get("clicks", 0)) for row in rows)
    ctr = round(total_clicks / total_shows * 100, 2) if total_shows else 0

    print(f"✅ Performance (campaign-level): CTR={ctr}%, Показы={total_shows:,}")
    return {"ctr": ctr, "shows": total_shows}

def fetch_ad_spend_by_offer_ids(date_from: str, date_to: str):
    """
    Получает расходы на рекламу по каждому offer_id через Performance API.
    Возвращает dict {offer_id: ad_spent_float}
    """
    token = get_performance_access_token()
    if not token:
        return {}

    url = "https://api-performance.ozon.ru/api/client/statistics/expense/json"
    headers_perf = {"Authorization": f"Bearer {token}"}
    params = {"dateFrom": date_from, "dateTo": date_to}

    try:
        data = safe_request(url, headers=headers_perf, json=params, method="get", timeout=60)
    except Exception as e:
        print(f"❌ Ошибка при получении расходов: {e}")
        return {}

    result = {}
    for row in data.get("rows", []):
        title = str(row.get("title", "")).strip()
        parts = title.split()
        if not parts:
            continue
        offer_id = parts[0]
        spent = safe_number(row.get("moneySpent", 0))
        result[offer_id] = result.get(offer_id, 0) + spent
    return result


def fetch_sku_sales_from_finance(date: str) -> dict:
    """
    Возвращает словарь {offer_id: сумма продаж ₽} за день.

    МИГРИРОВАНО с устаревающего /v3/finance/transaction/list на
    finance/accrual: продажи по SKU = Σ seller_price (finance_accrual.sales_by_sku),
    затем агрегируем по нашим offer_id через offer_to_sku.
    """
    # normalized map: Ozon sku(int/str) -> our offer_id
    sku_to_offer = {}
    for offer_id, sku_val in offer_to_sku.items():
        try:
            sku_int = int(sku_val)
            sku_to_offer[sku_int] = str(offer_id)
            sku_to_offer[str(sku_int)] = str(offer_id)
        except Exception:
            sku_to_offer[str(sku_val)] = str(offer_id)

    def _map_offer_by_sku(sku_raw):
        if sku_raw in sku_to_offer:
            return sku_to_offer[sku_raw]
        try:
            return sku_to_offer.get(int(sku_raw))
        except Exception:
            pass
        return sku_to_offer.get(str(sku_raw))

    sku_sales = {}
    for sku, rub in finance_accrual.sales_by_sku(date).items():
        offer_id = _map_offer_by_sku(sku)
        if not offer_id:
            continue
        sku_sales[offer_id] = sku_sales.get(offer_id, 0.0) + rub

    print(f"[Finance] Найдено {len(sku_sales)} offer_id с продажами за {date}")
    return sku_sales


# --- Сумма продаж за день (accruals_for_sale) ---
def fetch_sales_total(date: str) -> float:
    """
    Суммарные продажи ₽ за день.

    МИГРИРОВАНО с устаревающего /v3/finance/transaction/totals на
    finance/accrual (sales_total = Σ seller_price по дню).
    """
    try:
        return safe_number(finance_accrual.sales_total(date))
    except Exception as e:
        print(f"❌ Ошибка fetch_sales_total (accrual): {e}")
        return 0.0


# --- Новый метод: Получение ОВХ данных по списку offer_id (через SKU) ---
def fetch_ovh_data_by_offer_ids(offer_ids):
    """
    Получает ширину, высоту, глубину и вес для списка offer_id через v4/product/info/attributes.
    ⚠️ ВАЖНО: запрос идёт по SKU, т.к. именно SKU стабильны, а offer_id могут иметь суффиксы ("П", "К", "ир" и т.п.).
    Используем offer_to_sku и product_to_offer для маппинга:
      offer_id -> sku  и  sku -> offer_id
    Возвращает словарь {offer_id: {"width":..., "height":..., "depth":..., "weight":...}}
    """
    url = "https://api-seller.ozon.ru/v4/product/info/attributes"

    # Собираем список SKU только для тех offer_id, что реально есть в словаре
    sku_list = []
    for oid in offer_ids:
        sku_val = offer_to_sku.get(oid)
        if sku_val:
            sku_list.append(str(sku_val))

    if not sku_list:
        print("⚠️ fetch_ovh_data_by_offer_ids: sku_list пуст, нечего запрашивать")
        return {}

    body = {
        "filter": {
            "sku": sku_list,
            "visibility": "ALL"
        },
        "limit": len(sku_list),
        "sort_dir": "ASC"
    }
    try:
        j = safe_request(url, headers=headers, json=body, method="post", timeout=120)
        items = j.get("result", [])
    except Exception as e:
        print(f"❌ Ошибка получения ОВХ: {e}")
        return {}

    ovh_data = {}
    for it in items:
        # SKU из ответа
        sku_val = it.get("sku")
        if not sku_val:
            continue

        # В product_to_offer ключи — это str(sku)
        offer_id = product_to_offer.get(str(sku_val))
        if not offer_id:
            # На всякий случай пробуем прямой обратный маппинг
            fallback_offer = {v: k for k, v in offer_to_sku.items()}.get(sku_val)
            offer_id = fallback_offer
        if not offer_id:
            continue

        entry = {
            "width": safe_number(it.get("width", 0)),
            "height": safe_number(it.get("height", 0)),
            "depth": safe_number(it.get("depth", 0)),
            "weight": safe_number(it.get("weight", 0)),
        }

        # fallback через attributes (если width/height/depth/weight пустые)
        for a in it.get("attributes", []):
            name = a.get("name", "").lower()
            values_list = a.get("values", [])
            if not values_list:
                continue
            val = safe_number(values_list[0].get("value", ""))
            if ("ширина" in name or "width" in name) and not entry["width"]:
                entry["width"] = val
            elif ("высота" in name or "height" in name) and not entry["height"]:
                entry["height"] = val
            elif ("глубина" in name or "depth" in name) and not entry["depth"]:
                entry["depth"] = val
            elif ("вес" in name or "weight" in name) and not entry["weight"]:
                entry["weight"] = val

        # Кладём по offer_id, т.к. дальше вся логика и таблицы работают с ним
        ovh_data[str(offer_id)] = entry

    return ovh_data


# --- География продаж (общая) для листа "общий" ---
# Ensure normalize_sku exists
def normalize_sku(value: str) -> str:
    import re
    if not value:
        return ""
    digits = re.findall(r"\d+", str(value))
    return digits[0] if digits else ""


def fetch_avg_price_from_fbo_list(date_str: str) -> dict:
    """
    Возвращает {sku: avg_price} за дату date_str
    (по дате доставки client_delivery_date_begin).
    Работает БЕЗ fbo/get — только через fbo/list.
    """

    url = "https://api-seller.ozon.ru/v3/posting/fbo/list"
    sums = {}  # sku -> {"qty": int, "rev": float}
    cursor = ""

    while True:
        body = {
            "sort_dir": "ASC",
            "filter": {
                "since": f"{date_str}T00:00:00.000Z",
                "to": f"{date_str}T23:59:59.999Z",
            },
            "limit": 100,
            "cursor": cursor,
            "translit": True,
            "with": {
                "analytics_data": True,
                "financial_data": False,
                "legal_info": False
            }
        }

        data = safe_request(url, headers=headers, json=body, method="post", timeout=60)
        postings = data.get("postings", [])

        if not isinstance(postings, list):
            print("[AVG] result не list — стоп")
            break
        if not postings:
            break

        for posting in postings:
            # --- Filter by actual sale/creation date ---


            for prod in posting.get("products", []):
                sku = normalize_sku(str(prod.get("sku")))
                # --- Offer ID and mapping logic ---
                offer_id_raw = prod.get("offer_id", "")
                offer_norm = normalize_sku(offer_id_raw)
                my_sku = offer_norm  # default if direct match
                # map offer_norm to product id if exists
                if offer_norm in offer_to_sku:
                    my_sku = str(offer_to_sku[offer_norm])
                else:
                    # reverse map if product_id matches
                    if sku in offer_to_sku.values():
                        for k, v in offer_to_sku.items():
                            if str(v) == sku:
                                my_sku = k
                                break
                # normalize final key
                my_sku = normalize_sku(my_sku)
                sku = my_sku

                qty = int(prod.get("quantity", 0))
                price_raw = prod.get("price", 0)
                price = float(price_raw.get("amount", 0) if isinstance(price_raw, dict) else price_raw or 0)

                if qty <= 0:
                    continue

                if sku not in sums:
                    sums[sku] = {"qty": 0, "rev": 0.0}

                sums[sku]["qty"] += qty
                sums[sku]["rev"] += qty * price

        cursor = data.get("cursor", "")
        if not data.get("has_next", False):
            break

    avg = {}
    for sku, d in sums.items():
        avg[sku] = d["rev"] / d["qty"] if d["qty"] > 0 else 0.0

    # === LOGGING START ===
    print("---------- AVG PRICE DEBUG ----------")
    print(f"Дата: {date_str}")
    print(f"Всего SKU в sums: {len(sums)}")

    total_qty = sum(d['qty'] for d in sums.values())
    total_rev = sum(d['rev'] for d in sums.values())
    print(f"Суммарно qty: {total_qty}, revenue: {total_rev}")

    for sku, d in sums.items():
        try:
            avg_val = d['rev'] / d['qty'] if d['qty'] else 0
        except Exception:
            avg_val = 0
        print(f"SKU {sku}: qty={d['qty']}, rev={d['rev']}, avg={avg_val}")

    print("-------------- END ------------------")
    # === LOGGING END ===

    return avg

def fetch_general_geography_units(date_from: str, date_to: str):
    """
    Собирает количество заказанных единиц по регионам (без фильтра по SKU)
    из /v3/posting/fbo/list. Возвращает кортеж:
      (units_by_region: dict[str,int], shares_by_region: dict[str,float])
    Где shares_by_region — доля региона от общего количества (0..1).
    """
    url = "https://api-seller.ozon.ru/v3/posting/fbo/list"
    cursor = ""
    units_by_region = {region: 0 for region in region_to_warehouses.keys()}
    units_by_region["Прочее"] = 0

    while True:
        body = {
            "sort_dir": "ASC",
            "filter": {
                "since": f"{date_from}T00:00:00.000Z",
                "to": f"{date_to}T23:59:59.000Z"
            },
            "limit": 100,
            "cursor": cursor,
            "with": {
                "analytics_data": True,
                "financial_data": False,
                "legal_info": False
            }
        }
        try:
            j = safe_request(url, headers=headers, json=body, method="post", timeout=120)
            data = j.get("postings", [])
        except Exception as e:
            print(f"❌ Ошибка fbo/list (общая география): {e}")
            break

        if not data:
            break

        for posting in data:
            analytics = posting.get("analytics_data", {}) or {}
            wh_id = str(analytics.get("warehouse_id", ""))

            # Определяем регион
            region_name = None
            for region, wh_list in region_to_warehouses.items():
                if wh_id in wh_list:
                    region_name = region
                    break
            if not region_name:
                region_name = "Прочее"

            # Суммируем количество товаров в постинге
            qty_sum = 0
            for pr in posting.get("products", []):
                q = pr.get("quantity") or 1
                try:
                    q = int(q)
                except Exception:
                    q = 1
                qty_sum += q

            units_by_region[region_name] = units_by_region.get(region_name, 0) + qty_sum

        cursor = j.get("cursor", "")
        if not j.get("has_next", False):
            break
        time.sleep(0.2)

    total_units = sum(units_by_region.values())
    shares_by_region = {}
    for region, qty in units_by_region.items():
        shares_by_region[region] = (qty / total_units) if total_units > 0 else 0.0

    return units_by_region, shares_by_region

def run_daily_collector():
    print(f"[START] OZON collector | date={target_date}")

    # 🔁 Idempotency: перезаписываем данные за дату
    delete_metrics_for_date(target_date)
    # ===== 1️⃣ СВОД: ВСЕ метрики (analytics/data) =====
    while True:
        try:
            general_metrics = fetch_all_metrics(
                metrics=SVOD_METRICS,
                date_from=target_date.isoformat(),
                date_to=target_date.isoformat(),
            )
            break
        except Exception as e:
            print(f"[RETRY] fetch_all_metrics failed: {e} — повтор через 60 сек")
            time.sleep(60)

    for metric, value in general_metrics.items():
        insert_metric(
            metric_date=target_date,
            metric=metric,
            value=float(value),
        )
    # ===== SALES TOTAL (Finance) =====
    sales_total = fetch_sales_total(target_date.isoformat())
    insert_metric(
        metric_date=target_date,
        metric="sales_rub",
        value=float(sales_total),
    )


    # ===== 2️⃣ SKU: ВСЕ метрики (analytics/data) =====
    all_sku_metrics = fetch_all_sku_metrics(
        sku_list=sku_list,
        date_from=target_date.isoformat(),
        date_to=target_date.isoformat(),
    )

    for sku_id, metrics in all_sku_metrics.items():
        for metric, value in metrics.items():
            if metric not in SKU_METRICS:
                continue
            insert_metric(
                metric_date=target_date,
                sku_id=str(sku_id),
                metric=metric,
                value=float(value),
            )

    # ===== 3️⃣ География продаж, шт — СВОД =====
    units_by_region, _ = fetch_general_geography_units(
        target_date.isoformat(),
        target_date.isoformat(),
    )

    for region, qty in units_by_region.items():
        insert_metric(
            metric_date=target_date,
            region=region,
            metric="ordered_units",
            value=float(qty),
        )

    # ===== 4️⃣ География продаж, шт — ПО SKU =====
    geo_by_sku = fetch_geography_sales(
        target_date.isoformat(),
        target_date.isoformat(),
    )

    for offer_id, regions in geo_by_sku.items():
        for region, qty in regions.items():
            insert_metric(
                metric_date=target_date,
                sku_id=str(offer_id),
                region=region,
                metric="ordered_units",
                value=float(qty),
            )

    # ===== 5️⃣ WOW helper: недельные уники отдельным source =====
    upsert_last_full_week_unique_metrics()
    # ===== MoM helper: месячные уники отдельным source =====
    upsert_last_full_month_unique_metrics()
    # ===== SALES BY SKU (Finance) =====
    sku_sales = fetch_sku_sales_from_finance(target_date.isoformat())

    for offer_id, amount in sku_sales.items():
        insert_metric(
            metric_date=target_date,
            sku_id=str(offer_id),
            metric="sales_rub",
            value=float(amount),
        )
    # ===== AD SPEND TOTAL =====
    ad_total = fetch_ad_spend_yesterday()
    insert_metric(
        metric_date=target_date,
        metric="ad_spent",
        value=float(ad_total),
    )

    # ===== AD SPEND BY SKU =====
    ad_by_sku = fetch_ad_spend_by_offer_ids(
        target_date.isoformat(),
        target_date.isoformat()
    )

    for offer_id, spent in ad_by_sku.items():
        insert_metric(
            metric_date=target_date,
            sku_id=str(offer_id),
            metric="ad_spent",
            value=float(spent),
        )
    # ===== 5️⃣ СВД — СВОД + SKU =====
    if not SKIP_SVD_OVH:
        # SKU — ОВХ (габариты/вес)
        ovh_by_offer = fetch_ovh_data_by_offer_ids(list(offer_to_sku.keys()))
        for offer_id, d in ovh_by_offer.items():
            width = safe_number(d.get("width", 0))
            height = safe_number(d.get("height", 0))
            depth = safe_number(d.get("depth", 0))
            weight = safe_number(d.get("weight", 0))
            ovh = (width * height * depth) / 1_000_000 if width and height and depth else 0.0

            insert_metric(
                metric_date=target_date,
                sku_id=str(offer_id),
                metric="ovh",
                value=float(ovh),
            )
            insert_metric(
                metric_date=target_date,
                sku_id=str(offer_id),
                metric="width",
                value=float(width),
            )
            insert_metric(
                metric_date=target_date,
                sku_id=str(offer_id),
                metric="height",
                value=float(height),
            )
            insert_metric(
                metric_date=target_date,
                sku_id=str(offer_id),
                metric="depth",
                value=float(depth),
            )
            insert_metric(
                metric_date=target_date,
                sku_id=str(offer_id),
                metric="weight",
                value=float(weight),
            )

        # Свод — общее
        svd_summary = fetch_delivery_speed_summary()
        insert_metric(
            metric_date=target_date,
            metric="delivery_time_avg",
            value=float(svd_summary.get("avg", 0)),
        )

        # Свод — регионы
        svd_geo = fetch_delivery_speed_geo()
        for region, hours in svd_geo.items():
            insert_metric(
                metric_date=target_date,
                region=region,
                metric="delivery_time_avg",
                value=float(hours),
            )

        # SKU — общее + регионы
        svd_sku = fetch_delivery_speed_details()
        for sku_id, data in svd_sku.items():
            overall = data.get("overall", {}).get("avg", 0)
            insert_metric(
                metric_date=target_date,
                sku_id=str(sku_id),
                metric="delivery_time_avg",
                value=float(overall),
            )

            for region, hours in data.get("by_region", {}).items():
                insert_metric(
                    metric_date=target_date,
                    sku_id=str(sku_id),
                    region=region,
                    metric="delivery_time_avg",
                    value=float(hours),
                )

    print("[DONE] OZON collector finished successfully")

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args and args[0] == "--backfill-monthly":
        months = []
        for a in args[1:]:
            try:
                y, mth = a.split("-")
                months.append((int(y), int(mth)))
            except ValueError:
                raise SystemExit(f"Неверный формат месяца: '{a}'. Ожидается YYYY-MM, напр. 2026-03.")
        if not months:
            print("Использование: python grafana_report.py --backfill-monthly 2026-03 2026-04 2026-05")
        else:
            backfill_monthly_uniques(months)
    else:
        run_daily_collector()
