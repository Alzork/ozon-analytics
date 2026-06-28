import os
import gspread
from datetime import datetime, timedelta
from constants import sku_list
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import time
import requests
from constants import offer_to_sku
load_dotenv()

def normalize_cell(val):
    """
    Приводит значение из Google Sheets к числу:
    - "12,34%" -> 0.1234
    - "1 234,56" -> 1234.56
    - 1234 -> 1234.0
    - "" / None -> 0.0
    """
    if val in ("", None):
        return 0.0

    if isinstance(val, (int, float)):
        return float(val)

    s = str(val).strip()

    # проценты
    if s.endswith("%"):
        s = s.replace("%", "").replace(" ", "").replace(",", ".")
        try:
            return float(s) / 100
        except ValueError:
            return 0.0

    # обычные числа
    s = s.replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


CLIENT_ID = os.getenv("OZON_CLIENT_ID")
API_KEY = os.getenv("OZON_API_KEY")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_CREDS = os.getenv("GOOGLE_CREDS")

headers = {
    "Client-Id": CLIENT_ID,
    "Api-Key": API_KEY
}

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

def normalize_sku(value: str) -> str:
    import re
    if not value:
        return ""
    digits = re.findall(r"\d+", str(value))
    return digits[0] if digits else ""


def fetch_avg_price_from_fbo_list(date_str: str, return_sums=False) -> dict:
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
            created_dt = posting.get("created_at", "")
            created_date = created_dt.split("T")[0]
            if created_date != date_str:
                continue

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
    if return_sums:
        return sums
    return avg

def segment_by_price(sums: dict):
    """
    Принимает:
       {
          "SKU": {"qty": int, "rev": float},
          ...
       }

    Возвращает:
       {
         "100-200": {"qty": X, "rev": Y, "share": Z},
         ...
         "2401-2500": {...}
         "2501+": {...}
       }
    """

    # --- формируем сегменты по 100 от 100 до 2500 ---
    segments = {}

    step = 100
    min_price = 100
    max_price = 2500

    for start in range(min_price, max_price, step):
        end = start + step
        segments[f"{start}-{end}"] = {"qty": 0, "rev": 0.0}

    segments["2501+"] = {"qty": 0, "rev": 0.0}

    # --- считаем общий оборот ---
    total_rev = sum(d["rev"] for d in sums.values())

    # --- распределяем SKU по сегментам ---
    for sku, d in sums.items():
        qty = d["qty"]
        rev = d["rev"]

        if qty <= 0:
            continue

        avg_price = rev / qty

        placed = False
        for start in range(min_price, max_price, step):
            end = start + step
            if start < avg_price <= end:
                key = f"{start}-{end}"
                segments[key]["qty"] += qty
                segments[key]["rev"] += rev
                placed = True
                break

        if not placed:
            if avg_price > max_price:
                segments["2501+"]["qty"] += qty
                segments["2501+"]["rev"] += rev

    # --- добавляем % от общего оборота ---
    for seg, d in segments.items():
        if total_rev > 0:
            d["share"] = d["rev"] / total_rev
        else:
            d["share"] = 0.0

    return segments

def write_segments_to_sheet(date_str: str, segments: dict):
    gc = gspread.service_account(filename=GOOGLE_CREDS)
    sh = gc.open(SHEET_NAME)
    ws = sh.worksheet("Сегмент")

    # читаем строку 1 — заголовки с датами
    dates_row = ws.row_values(1)

    # если пустая строка (новый лист)
    if not dates_row:
        ws.update_cell(1, 2, date_str)
        col = 2
    else:
        # определяем — есть ли дата уже в таблице
        existing_col = None
        for col_idx, val in enumerate(dates_row, start=1):
            if val.strip() == date_str:
                existing_col = col_idx
                break

        if existing_col:
            print(f"⚠️ Дата {date_str} уже существует в колонке {existing_col}. Обновляю...")
            col = existing_col
        else:
            # находим куда вставить новую дату
            insert_at = max(2, len(dates_row) + 1)  # по умолчанию — в конец

            for i, d in enumerate(dates_row, start=1):
                if d.strip() > date_str:
                    insert_at = i
                    break

            # вставляем новую колонку
            sh.batch_update({
                "requests": [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": ws.id,
                                "dimension": "COLUMNS",
                                "startIndex": insert_at - 1,
                                "endIndex": insert_at
                            },
                            "inheritFromBefore": False
                        }
                    }
                ]
            })
            ws.update_cell(1, insert_at, date_str)
            col = insert_at
            print(f"🆕 Вставлена новая колонка {col} для даты {date_str}")

    # === Batch-запись сегментов (rev / qty / share) ===
    values = []
    for seg_name, data in segments.items():
        values.append([data["rev"]])
        values.append([data["qty"]])
        values.append([data["share"]])

    start_row = 2
    end_row = start_row + len(values) - 1

    cell_range = f"{gspread.utils.rowcol_to_a1(start_row, col)}:" \
                 f"{gspread.utils.rowcol_to_a1(end_row, col)}"

    ws.update(cell_range, values, value_input_option="RAW")

    print(f"📊 Сегменты за {date_str} записаны batch-обновлением в колонку {col}")

if __name__ == "__main__":
    # === считаем ТОЛЬКО вчера ===
    yesterday = datetime.now().date() - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")

    print(f"\n=== SEGMENTATION FOR {date_str} ===")

    sums = fetch_avg_price_from_fbo_list(date_str, return_sums=True)
    segments = segment_by_price(sums)

    print("\n--- SEGMENTS ---")
    for seg, d in segments.items():
        print(f"{seg}: qty={d['qty']}, rev={d['rev']}")

    write_segments_to_sheet(date_str, segments)