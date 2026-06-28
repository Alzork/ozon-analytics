# Смещение между svod и weeklive (Свод начинается с 3 строки, weeklive — с 2)
SVOD_TO_WEEKLIVE_OFFSET = 1  # Свод: строка 3 = weeklive: строка 2
# дополнительный сдвиг после строки 106 в Своде
SVOD_EXTRA_SHIFT_START = 106
SVOD_EXTRA_SHIFT_VALUE = 6  # 106 -> 111
import datetime as dt
from datetime import date, timedelta

import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================

SPREADSHEET_NAME = "Аналитика Ozon"   # поменяй если нужно
WOW_SHEET = "WOW"
SVOD_SHEET = "Свод"
WEEKLIVE_SHEET = "weeklive"

SERVICE_ACCOUNT_FILE = "google-creds.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# --- SVOD row-based aggregation rules (1-based row numbers in SVOD) ---

SVOD_SUM_RANGES = [
    (3, 7),
    (11, 13),
    (15, 16),
    (19, 20),
    (40, 103),
    (238, 263),
]

SVOD_AVG_RANGES = [
    (8, 10),
    (14, 14),
    (17, 18),
    (21, 37),
    (106, 169),
    (271, 294),
]

def get_svod_mode(svod_row: int) -> str | None:
    for a, b in SVOD_SUM_RANGES:
        if a <= svod_row <= b:
            return "SUM"
    for a, b in SVOD_AVG_RANGES:
        if a <= svod_row <= b:
            return "AVG"
    return None

# --- Rules for calculation ---

BLOCK_RULES = {
    "Данные SKU по продажам": "SUM",
    "Соотеношение продаж СКЮ от всех продаж": "AVG",
    "География продаж, шт": "SUM",
    "Среднее время доставки, общее": "AVG",
}

SUM_METRICS = {
    "Уникальные посетители, всего",
    "Уникальные посетители  в поиске или каталоге",
    "Уникальные посетители карточки товара",
    "Заказано на сумму",
    "Заказано товаров",
    "Показы, всего",
    "Показы на карточке товара",
    "Показы в поиске и каталоге",
    "Продажи. руб",
    "Расход на рекламу. руб",
    "Доставлено товаров",
    "В корзину, всего",
}

AVG_METRICS = {
    "Конверсия в корзину, общая",
    "Конверсия в корзину из карточки товара",
    "Конверсия в корзину из поиска или каталога",
    "Средняя цена",
    "Средний чек (продажи/штуки)",
    "Позиция в поиске и каталоге",
    "ДРР от выручки. %",
    "ДРР от оборота. %",
    "Показы к уникам",
    "Ср сумма заказа",
    "Показы к уникам",
    "Сумма заказа с 1 уника. руб.",
    "Показы на 1 уника. всего",
    "Показы на 1 уника. в карточке",
    "Конверсия из карточки в корзину. уники",
    "Конверсия из карточки в корзину. показы",
    "Конверсия в корзину из уников",
    "Конверсия в заказ из уников",
    "Конверсия в заказ из корзины",
    "Руб рекламы за 1 показ (всего)",
    "Руб рекламы за 1 уник (всего)",
    "Руб рекламы за 1 показ карточки",
    "Руб рекламы за 1 уник карточки"
}

# =========================================


def parse_date(value: str) -> date | None:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def main():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=SCOPES
    )
    gc = gspread.authorize(creds)

    sh = gc.open(SPREADSHEET_NAME)

    ws_weeklive = sh.worksheet(WEEKLIVE_SHEET)
    ws_wow = sh.worksheet(WOW_SHEET)
    ws_svod = sh.worksheet(SVOD_SHEET)

    # ---------- WOW: последняя закрытая неделя ----------
    wow_header = ws_wow.row_values(1)

    if len(wow_header) < 3:
        raise RuntimeError("В WOW недостаточно столбцов")

    last_week_col_idx = len(wow_header) - 2  # предпоследний столбец (0-based)

    wow_metrics = ws_wow.col_values(1)
    wow_values = ws_wow.col_values(last_week_col_idx + 1)


    # ---------- Свод: текущая неполная неделя (C) ----------
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    svod_data = ws_svod.get_all_values()
    dates_row = svod_data[1]  # строка 2 — даты

    date_cols = []

    for idx, v in enumerate(dates_row):
        d = None

        # строка YYYY-MM-DD
        if isinstance(v, str):
            try:
                d = dt.datetime.strptime(v.strip(), "%Y-%m-%d").date()
            except Exception:
                pass

        # Excel serial date
        elif isinstance(v, (int, float)) and v > 40000:
            d = date(1899, 12, 30) + timedelta(days=int(v))

        if d and monday <= d <= today:
            date_cols.append(idx)

    # УДАЛЕНО: старый блок агрегации по метрикам и блокам

    # ---------- weeklive: column C (strict row-based from SVOD) ----------

    current_week_by_row = {}  # key: weeklive_row (1-based), value: number

    for svod_idx in range(2, len(svod_data)):
        svod_row = svod_idx + 1  # 1-based row in SVOD
        # --- SVOD -> weeklive row mapping ---
        # Блоки в конце листа (география) имеют отдельное соответствие строк:
        #   svod 238 -> weeklive 181  (SUM: 238–263)
        #   svod 271 -> weeklive 213  (AVG: 271–294)
        if svod_row >= 271:
            weeklive_row = svod_row - 56
        elif svod_row >= 238:
            weeklive_row = svod_row - 56
        else:
            weeklive_row = svod_row - SVOD_TO_WEEKLIVE_OFFSET
            if svod_row >= SVOD_EXTRA_SHIFT_START:
                weeklive_row += SVOD_EXTRA_SHIFT_VALUE

        mode = get_svod_mode(svod_row)
        if mode is None:
            continue

        values = []
        for c in date_cols:
            try:
                v = svod_data[svod_idx][c]
                is_percent = False
                if isinstance(v, str):
                    if "%" in v:
                        is_percent = True
                    v = (
                        v.replace("\xa0", "")
                         .replace(" ", "")
                         .replace("%", "")
                         .replace(",", ".")
                    )

                num = float(v)

                # если AVG и это процент — нормализуем
                if mode == "AVG" and is_percent:
                    num = num / 100

                values.append(num)
            except Exception:
                pass

        if mode == "SUM":
            result = sum(values)
        else:  # AVG
            result = sum(values) / len(values) if values else 0

        current_week_by_row[weeklive_row] = result

    # ---------- weeklive: запись ----------
    weeklive_metrics = ws_weeklive.col_values(1)

    col_b = []
    col_c = []

    # WOW значения уже считаны в wow_values
    # wow_values: список значений столбца последней полной недели (1-based)

    max_rows = len(weeklive_metrics)

    for r in range(1, max_rows):  # начиная со 2 строки (индекс 1)
        # --- B: строго по номеру строки ---
        wow_val = wow_values[r] if r < len(wow_values) else 0
        col_b.append([wow_val])

        # --- C: с учётом смещения между svod и weeklive ---
        # r — индекс строки в weeklive (0-based)
        # weeklive строка (1-based)
        weeklive_row = r + 1
        col_c.append([current_week_by_row.get(weeklive_row, 0)])

    ws_weeklive.update(range_name="B2", values=col_b)
    ws_weeklive.update(range_name="C2", values=col_c)

    print("weeklive updated successfully")


if __name__ == "__main__":
    main()