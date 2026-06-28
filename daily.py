import os
import math
from datetime import datetime, timedelta, timezone
from constants import offer_to_sku
import requests
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

import finance_accrual

load_dotenv()

# ---- TIME HELPERS ----
MSK = timezone(timedelta(hours=3))

# ---- ENV / CONFIG ----
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
GOOGLE_SHEET_NAME_DAY = os.getenv("GOOGLE_SHEET_NAME_DAY")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "День")
# same as in other scripts: GOOGLE_CREDS points to creds.json path
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_CREDS") or os.getenv("GOOGLE_CREDS_PATH") or "creds.json"
DAYS_BACK = int(os.getenv("DAYS_BACK", "1"))  # 1=yesterday, 2=day before yesterday
DEBUG_DAILY = os.getenv("DEBUG_DAILY", "0") == "1"
DEBUG_OFFERS = {"1081", "1118", "1102", "1129", "1053"}

_missing = [k for k, v in {
    "OZON_CLIENT_ID": OZON_CLIENT_ID,
    "OZON_API_KEY": OZON_API_KEY,
    "GOOGLE_SHEET_NAME_DAY": GOOGLE_SHEET_NAME_DAY,
}.items() if not v]

if _missing:
    raise RuntimeError(
        "Missing required environment variables: " + ", ".join(_missing)
    )

HEADERS = {
    "Client-Id": OZON_CLIENT_ID,
    "Api-Key": OZON_API_KEY,
    "Content-Type": "application/json",
}

# ----------------------------------------------------
# HTTP RETRY (survive 5xx / timeouts)
# ----------------------------------------------------

def post_with_retry(url, headers, body, timeout=60):
    attempt = 0
    while True:
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.exceptions.HTTPError(f"Server error {resp.status_code}", response=resp)
            return resp
        except Exception as e:
            attempt += 1
            wait = min(30 * attempt, 300)
            print(f"RETRY {attempt}: {url} -> {e}. Sleep {wait}s")
            time.sleep(wait)

# ----------------------------------------------------
# GOOGLE SHEETS
# ----------------------------------------------------

def connect_sheet():
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]

    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_PATH, scope)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEET_NAME_DAY).worksheet(WORKSHEET_NAME)


def read_table_structure(sheet):
    # начиная с 3 строки (индекс 2)
    rows = sheet.get_all_values()[2:]
    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        offer = str(r[0]).strip()
        metric = str(r[1]).strip() if len(r) > 1 else ""
        out.append((offer, metric))
    return out


def get_or_create_date_column(sheet, date_label):
    # читаем первую строку до последнего столбца
    header = sheet.row_values(1)

    # начиная с колонки C (индекс 2 в 0-based)
    for i in range(2, len(header)):
        if header[i] == date_label:
            return i + 1  # 1-based

    # если не нашли — создаём следующий столбец, но не раньше C
    last_col = max(len(header), 2)  # минимум до B
    col = last_col + 1
    if col < 3:
        col = 3

    sheet.update_cell(1, col, date_label)
    return col


def write_values(sheet, values, col):
    # очищаем старые данные в колонке даты (с 3 строки до конца листа)
    col_letter = gspread.utils.rowcol_to_a1(1, col).rstrip('1')
    max_rows = sheet.row_count
    clear_range = f"{col_letter}3:{col_letter}{max_rows}"
    sheet.update(values=[[""]]* (max_rows-2), range_name=clear_range)

    # пишем новые данные
    end_row = len(values) + 2
    rng = gspread.utils.rowcol_to_a1(3, col) + ":" + gspread.utils.rowcol_to_a1(end_row, col)
    sheet.update(values=[[v] for v in values], range_name=rng)

# ----------------------------------------------------
# OZON API — ПОСТИНГИ (ЦЕНА)
# ----------------------------------------------------

def get_postings(date_from, date_to):

    url = "https://api-seller.ozon.ru/v3/posting/fbo/list"

    postings = []
    cursor = ""

    while True:
        body = {
            "sort_dir": "ASC",
            "filter": {
                "since": date_from,
                "to": date_to,
            },
            "limit": 100,
            "cursor": cursor,
            "translit": True,
            "with": {
                "analytics_data": True,
                "financial_data": True,
                "legal_info": False
            }
        }

        resp = post_with_retry(url, HEADERS, body, timeout=60)
        resp.raise_for_status()
        r = resp.json()

        batch = r.get("postings", [])
        if not batch:
            break

        postings.extend(batch)
        cursor = r.get("cursor", "")
        if not r.get("has_next", False):
            break

    return postings


def extract_real_prices(postings):

    prices = {}

    # build normalized reverse map: sku(int/str) -> our offer_id
    const_sku_to_offer = {}
    for offer_id, sku_val in offer_to_sku.items():
        try:
            sku_int = int(sku_val)
            const_sku_to_offer[sku_int] = str(offer_id)
            const_sku_to_offer[str(sku_int)] = str(offer_id)
        except Exception:
            const_sku_to_offer[str(sku_val)] = str(offer_id)

    def _map_offer(sku):
        if sku in const_sku_to_offer:
            return const_sku_to_offer[sku]
        try:
            return const_sku_to_offer.get(int(sku))
        except Exception:
            pass
        return const_sku_to_offer.get(str(sku))

    for posting in postings:

        used_financial = set()

        # ---- 1. try real financial price ----
        for fin in posting.get("financial_data", {}).get("products", []) or []:
            sku = fin.get("product_id")
            price = fin.get("price")

            offer = _map_offer(sku)
            if not offer or price in (None, 0):
                continue

            prices.setdefault(offer, []).append(price)
            used_financial.add(offer)

        # ---- 2. fallback to products price (Ozon sometimes returns quantity=0 in financial_data) ----
        for item in posting.get("products", []):
            sku = item.get("sku")
            offer = _map_offer(sku)
            if not offer or offer in used_financial:
                continue

            price_raw = item.get("price")
            if isinstance(price_raw, dict):
                price_raw = price_raw.get("amount")
            if price_raw not in (None, 0):
                prices.setdefault(offer, []).append(float(price_raw))

    return {k: sum(v)/len(v) for k, v in prices.items() if v}

# ----------------------------------------------------
# OZON API — ГАБАРИТЫ (ОВХ)
# ----------------------------------------------------

def get_dimensions(offers):

    url = "https://api-seller.ozon.ru/v4/product/info/attributes"
    result = {}

    # build offer->sku and sku->offer maps using constants
    offer_list = [str(o) for o in offers]
    sku_map = {str(k): v for k, v in offer_to_sku.items() if str(k) in offer_list}
    sku_to_offer = {v: k for k, v in sku_map.items()}
    skus = list(sku_to_offer.keys())

    for i in range(0, len(skus), 100):
        chunk = skus[i:i+100]
        body = {
            "filter": {
                "sku": chunk,
                "visibility": "ALL"
            },
            "limit": 100,
            "sort_dir": "ASC"
        }
        resp = post_with_retry(url, HEADERS, body, timeout=60)
        resp.raise_for_status()
        r = resp.json()

        for item in r.get("result", []):
            sku = item.get("sku")
            offer = sku_to_offer.get(sku)
            w = item.get("width", 0)
            h = item.get("height", 0)
            d = item.get("depth", 0)

            ovh = (w*h*d)/1_000_000 if w and h and d else 0
            if offer:
                result[str(offer)] = ovh

    return result

# ----------------------------------------------------
# OZON API — FINANCE TRANSACTIONS (ЗАТРАТЫ)
# ----------------------------------------------------

def get_costs_per_offer(date):
    # МИГРИРОВАНО с устаревающего /v3/finance/transaction/list на
    # finance/accrual: продажи и затраты по SKU из by-day
    # (finance_accrual.breakdown_by_sku), агрегируем по нашим offer_id.
    #   продажи = seller_price;
    #   затраты  = |sale_commission| + |delivery| + |item_fees по sku|.
    # Раньше затраты операции делились между SKU по весу price*qty — теперь
    # начисления приходят уже по SKU напрямую (точнее).

    # sku -> our offer (поддерживаем ключи и как int, и как str)
    const_sku_to_offer = {}
    for offer_id, sku_val in offer_to_sku.items():
        try:
            sku_int = int(sku_val)
            const_sku_to_offer[sku_int] = str(offer_id)
            const_sku_to_offer[str(sku_int)] = str(offer_id)
        except Exception:
            const_sku_to_offer[str(sku_val)] = str(offer_id)

    pos = {}  # продажи (seller_price) по offer
    neg = {}  # расходы (abs) по offer
    dbg_sku_total = 0
    dbg_sku_mapped = 0
    dbg_sales_sum = 0.0
    dbg_costs_sum = 0.0

    for sku, vals in finance_accrual.breakdown_by_sku(date).items():
        dbg_sku_total += 1
        offer = const_sku_to_offer.get(sku) or const_sku_to_offer.get(str(sku))
        if not offer:
            continue
        dbg_sku_mapped += 1
        if vals["sales"]:
            pos[offer] = pos.get(offer, 0.0) + vals["sales"]
            dbg_sales_sum += vals["sales"]
        if vals["costs"]:
            neg[offer] = neg.get(offer, 0.0) + vals["costs"]
            dbg_costs_sum += vals["costs"]

    if DEBUG_DAILY:
        print(
            "ACCRUAL COVERAGE:",
            f"sku_total={dbg_sku_total}",
            f"sku_mapped={dbg_sku_mapped}",
            f"sales_sum={round(dbg_sales_sum, 2)}",
            f"costs_sum={round(dbg_costs_sum, 2)}",
        )

    # возвращаем расходы и положительные начисления по SKU
    return neg, pos


# ----------------------------------------------------
# ГЛАВНАЯ ЛОГИКА
# ----------------------------------------------------

def get_yesterday_msk_utc_range():
    # Строгие UTC-сутки: YYYY-MM-DDT00:00:00.000Z .. YYYY-MM-DDT23:59:59.000Z
    target_date_utc = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).date()
    date_str = target_date_utc.strftime("%Y-%m-%d")
    return f"{date_str}T00:00:00.000Z", f"{date_str}T23:59:59.000Z"

def main():

    sheet = connect_sheet()
    structure = read_table_structure(sheet)

    offers = list({s[0] for s in structure})

    date_from, date_to = get_yesterday_msk_utc_range()
    print(f"LIST WINDOW UTC: from={date_from} to={date_to}")
    # дата для шапки в МСК
    date_label = (datetime.now(MSK) - timedelta(days=DAYS_BACK)).strftime("%d.%m.%Y")
    col = get_or_create_date_column(sheet, date_label)

    postings = get_postings(date_from, date_to)

    print(f"POSTINGS COUNT: {len(postings)}")
    if postings:
        print(f"SAMPLE KEYS: {list(postings[0].keys())}")

    prices = extract_real_prices(postings)
    print(f"PRICES FOUND: {len(prices)}")
    ovh = get_dimensions(offers)
    costs, sales_pos = get_costs_per_offer(date_from[:10])

    if DEBUG_DAILY:
        print("SKU TOTALS:")
        all_offers = sorted(set(sales_pos.keys()) | set(costs.keys()), key=lambda x: str(x))
        for offer in all_offers:
            sales_v = float(sales_pos.get(offer, 0.0))
            cost_v = float(costs.get(offer, 0.0))
            print(f"  offer={offer} sales={round(sales_v, 2)} costs={round(cost_v, 2)}")

        print("SKU DEBUG:")
        for offer in sorted(DEBUG_OFFERS):
            sales_v = float(sales_pos.get(offer, 0.0))
            cost_v = float(costs.get(offer, 0.0))
            pct_v = (cost_v / sales_v * 100.0) if sales_v > 0 else 0.0
            print(
                f"  offer={offer} sales={round(sales_v, 2)} "
                f"cost={round(cost_v, 2)} pct={round(pct_v, 2)}"
            )

    values = []

    for offer, metric in structure:
        if metric == "Цена (по акции)":
            v = prices.get(offer, "")
            values.append(v if v != 0 else 0)
        elif metric == "ОВХ":
            v = ovh.get(offer, "")
            if isinstance(v, (int, float)) and v > 0:
                values.append(math.ceil(v))
            else:
                values.append(v if v != 0 else 0)
        elif metric == "Тотал для затрат":
            offer_cost = costs.get(offer, 0.0)
            offer_sales = sales_pos.get(offer, 0.0)
            if offer_sales > 0:
                v = (offer_cost / offer_sales) * 100
                values.append(round(v, 2))
            else:
                values.append("")
        else:
            values.append("")

    write_values(sheet, values, col)


# ----------------------------------------------------

if __name__ == "__main__":
    main()
