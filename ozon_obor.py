import os
import re
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import time
from constants import (
    sku_list,
    region_to_warehouses,
    clusters_id,
    offer_to_sku,
    clean_offer_to_product_id,
)

# ===== NORMALIZATION OF CLUSTER NAMES =====
clusters_id_normalized = {str(k).lower().strip(): v for k, v in clusters_id.items()}

load_dotenv()

sheet_name = os.getenv("GOOGLE_SHEET_NAME_OBOR")


# Inverse mapping from cluster_id to cluster_name
clusters_id_inv = {v: k for k, v in clusters_id.items()}

# inverse map: sku (int) -> offer_id (str)
sku_to_offer = {v: k for k, v in offer_to_sku.items()}

# =====================
# CONFIG
# =====================
OZON_URL_STOCKS = "https://api-seller.ozon.ru/v1/analytics/stocks"
WORKSHEET_NAME = "Расчет"  # имя листа в таблице

# Слияние кластеров: cluster_id из API → целевой cluster_id в таблице
# Алматы(195) + Астана(196) → Казахстан(108)
# Беларусь(109)             → Москва, МО и Дальние регионы(154)
# Калининград(12)            → Санкт-Петербург и СЗО(2)
CLUSTER_MERGE = {
    195: 108,
    196: 108,
    109: 154,
    12:  2,
}

# =====================
# Helpers
# =====================

# =====================
# FBO SALES AGGREGATION (for RUS clusters)
# =====================


# Helper to build last full N days range (excluding today)
def _last_full_days_range(days: int):
    """Return (since_iso_date, to_iso_date) for the last *full* N days in UTC.
    Example: if today is 2025-11-11, for days=7 -> since=2025-11-04, to=2025-11-10
    """
    today = datetime.now(timezone.utc).date()
    to_date = today - timedelta(days=1)
    since = to_date - timedelta(days=days-1)
    return since.isoformat(), to_date.isoformat()

def fbo_fetch_range_daily(date_from, date_to):
    import re
    url = "https://api-seller.ozon.ru/v3/posting/fbo/list"
    headers = {
        "Client-Id": os.getenv("OZON_CLIENT_ID", ""),
        "Api-Key": os.getenv("OZON_API_KEY", ""),
    }

    # -----------------------------
    # ВАРИАНТ: 30 отдельных запросов
    # -----------------------------
    daily = {}
    start_date = datetime.fromisoformat(date_from).date()
    end_date = datetime.fromisoformat(date_to).date()

    delta_days = (end_date - start_date).days + 1
    all_dates = [(start_date + timedelta(days=i)).isoformat()
                 for i in range(delta_days)]

    for single_date in all_dates:
        day_start = f"{single_date}T00:00:00.000Z"
        day_end   = f"{single_date}T23:59:59.000Z"

        cursor = ""
        while True:
            body = {
                "sort_dir": "ASC",
                "filter": {
                    "since": day_start,
                    "to": day_end
                },
                "limit": 100,
                "cursor": cursor,
                "with": {
                    "analytics_data": True,
                    "financial_data": True
                }
            }

            j = _safe_post(url, headers, body)
            data = j.get("postings", [])
            if not data:
                break

            for posting in data:
                fd = posting.get("financial_data") or {}
                cluster_to_raw = fd.get("cluster_to", "")
                cluster_to_key = str(cluster_to_raw).lower().strip()
                cluster_id = clusters_id_normalized.get(cluster_to_key)
                if not cluster_id:
                    continue
                # Сливаем кластеры по таблице CLUSTER_MERGE
                cluster_id = CLUSTER_MERGE.get(cluster_id, cluster_id)

                for p in posting.get("products", []):
                    raw_offer = str(p.get("offer_id", "")).strip()
                    m = re.match(r"(\d+)", raw_offer)
                    offer_id = m.group(1) if m else raw_offer
                    if not offer_id:
                        continue

                    qty = int(p.get("quantity", 0))
                    if qty <= 0:
                        continue

                    key = (offer_id, cluster_id)

                    if key not in daily:
                        daily[key] = {d: 0 for d in all_dates}

                    daily[key][single_date] += qty

            cursor = j.get("cursor", "")
            if not j.get("has_next", False):
                break
            time.sleep(0.15)

    return daily


def compute_avg_from_daily(daily_map, days):
    result = {}
    for key, daymap in daily_map.items():
        sorted_dates = sorted(daymap.keys())[-days:]
        qty_list = [daymap[d] for d in sorted_dates]

        total = sum(qty_list)
        active = sum(1 for x in qty_list if x > 0)

        if active == 0:
            result[key] = 0
        else:
            result[key] = total / active

    return result


def fbo_avg_30_days():
    since, to = _last_full_days_range(30)
    daily = fbo_fetch_range_daily(since, to)
    return compute_avg_from_daily(daily, 30)

def fbo_avg_7_days():
    since, to = _last_full_days_range(7)
    daily = fbo_fetch_range_daily(since, to)
    return compute_avg_from_daily(daily, 7)

def fbo_avg_90_days():
    since, to = _last_full_days_range(90)
    daily = fbo_fetch_range_daily(since, to)
    return compute_avg_from_daily(daily, 90)

def fbo_avg_3_days():
    since, to = _last_full_days_range(3)
    daily = fbo_fetch_range_daily(since, to)
    return compute_avg_from_daily(daily, 3)

def _safe_post(url, headers, json_body, retries=5, timeout=60):
    """POST с простым экспоненциальным бэкоффом (на случай 429/5xx)."""
    for attempt in range(retries):
        try:
            resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            # если лимиты или временные ошибки — подождать и повторить
            if resp.status_code in (429, 500, 502, 503, 504):
                sleep_s = min(30, 2 ** attempt)
                time.sleep(sleep_s)
                continue
            # иная ошибка — проброс
            resp.raise_for_status()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(min(30, 2 ** attempt))
    return {"items": []}


def fetch_stocks_all():
    """Тянем все кластеры для всех SKU одним запросом и собираем удобный индекс."""
    headers = {
        "Client-Id": os.getenv("OZON_CLIENT_ID", ""),
        "Api-Key": os.getenv("OZON_API_KEY", ""),
    }
    if not headers["Client-Id"] or not headers["Api-Key"]:
        raise RuntimeError("Отсутствуют OZON_CLIENT_ID / OZON_API_KEY в окружении")

    # тело: список внутренних sku (числа). Берём из offer_to_sku
    skus = list(offer_to_sku.values())
    body = {"skus": skus}

    data = _safe_post(OZON_URL_STOCKS, headers, body)
    items = data.get("items", [])
    print("DEBUG: total items from API:", len(items))
    print("DEBUG: unique clusters from API:", sorted(set((it.get("cluster_id"), it.get("cluster_name")) for it in items)))

    # Индекс по паре (offer_id, cluster_id)
    # Храним данные, которые нужны в таблицу: ads_cluster, available, transit
    idx = {}
    for it in items:
        offer_id = it.get("offer_id", "")
        cluster_id = it.get("cluster_id")
        if not offer_id or cluster_id is None:
            continue
        offer_raw = str(offer_id)
        m = re.match(r"(\d+)", offer_raw)
        offer_id = m.group(1) if m else offer_raw
        # Нормализуем пробелы
        offer_id = offer_id.strip()
        # Сливаем кластеры по таблице CLUSTER_MERGE
        cluster_id = CLUSTER_MERGE.get(cluster_id, cluster_id)
        key = (offer_id, cluster_id)
        if key not in idx:
            idx[key] = {
                "ads_cluster": 0,
                "available": 0,
                "transit": 0,
                "requested": 0,
            }
        idx[key]["ads_cluster"] += float(it.get("ads_cluster", 0) or 0)
        idx[key]["available"] += int(it.get("available_stock_count", 0) or 0)
        idx[key]["transit"] += int(it.get("transit_stock_count", 0) or 0)
        idx[key]["requested"] += int(it.get("requested_stock_count", 0) or 0)
    print("DEBUG: idx_map sample:", list(idx.items())[:20])
    return idx


def get_worksheet():
    gc = gspread.service_account(filename=os.getenv("GOOGLE_CREDS"))
    ss = gc.open(sheet_name)
    ws = ss.worksheet(WORKSHEET_NAME)
    return ss, ws


def compute_last_data_row(values):
    """Определяем последнюю строку с данными по столбцам A/B.
    values — это get_all_values(), первая строка — заголовки.
    """
    # ищем снизу первую строку, где в A или B что-то есть
    last = len(values)
    for i in range(len(values), 1, -1):  # от конца к началу, не трогаем заголовок (i=1)
        row = values[i-1]
        a = row[0].strip() if len(row) > 0 else ""
        b = row[1].strip() if len(row) > 1 else ""
        if a or b:
            last = i
            break
    return max(2, last)  # минимум 2 (первая строка данных)


def build_column_updates(ws, idx_map):
    """Готовим три диапазона для батч-обновления: H, I, J (с 2-й строки до последней).
    idx_map: {(offer_id, cluster_name): {...}}
    Возвращаем список dict для sheet.batch_update.
    """
    values = ws.get_all_values()
    if not values:
        return []

    # Последняя строка с данными
    last_row = compute_last_data_row(values)
    print("DEBUG: last_row =", last_row)
    # Длина массивов для обновления
    n = last_row - 1  # без заголовка

    # Precompute FBO averages
    fbo30 = fbo_avg_30_days()
    fbo7 = fbo_avg_7_days()
    fbo90 = fbo_avg_90_days()
    fbo3 = fbo_avg_3_days()

    colH, colI, colJ, colK = [], [], [], []
    colN, colO, colR = [], [], []
    colU, colX, colAA = [], [], []

    # Идём по строкам 2..last_row, читаем A (offer) и B (cluster)
    for r in range(2, last_row + 1):
        row = values[r-1]
        offer_raw = row[0].strip() if len(row) > 0 else ""
        # Оставляем только первые подряд идущие цифры
        m = re.match(r"(\d+)", offer_raw)
        offer = m.group(1) if m else offer_raw
        cluster_name_raw = row[5].strip() if len(row) > 5 else ""
        cluster_key = cluster_name_raw.lower().strip()
        cluster_id = clusters_id_normalized.get(cluster_key)
        print(f"DEBUG ROW {r}: offer='{offer}', cluster_name='{cluster_name_raw}', cluster_id={cluster_id}")

        rec = idx_map.get((offer, cluster_id))
        if rec:
            colH.append([fbo90.get((offer, cluster_id), 0)])
            colI.append([fbo30.get((offer, cluster_id), 0)])
            colJ.append([fbo7.get((offer, cluster_id), 0)])
            colK.append([fbo3.get((offer, cluster_id), 0)])

            colN.append([rec.get("available", 0)])
            colO.append([rec.get("transit", 0)])
            colR.append([rec.get("requested", 0)])

            row_idx = r
            # ===== STATUS 30 DAYS (U) =====
            colU.append([(
                f'=IF(N{row_idx}>30*I{row_idx};'
                f'"избыток";'
                f'IF(N{row_idx}<15*I{row_idx};'
                f'"дефицит";'
                f'"норма"))'
            )])

            # ===== STATUS 60 DAYS (X) =====
            colX.append([(
                f'=IF(N{row_idx}>60*I{row_idx};'
                f'"избыток";'
                f'IF(N{row_idx}<45*I{row_idx};'
                f'"дефицит";'
                f'"норма"))'
            )])

            # ===== STATUS 120 DAYS (AA) =====
            colAA.append([(
                f'=IF(N{row_idx}>120*I{row_idx};'
                f'"избыток";'
                f'IF(N{row_idx}<90*I{row_idx};'
                f'"дефицит";'
                f'"норма"))'
            )])
        else:
            colH.append([0])
            colI.append([0])
            colJ.append([0])
            colK.append([0])

            colN.append([0])
            colO.append([0])
            colR.append([0])

            row_idx = r
            # ===== STATUS 30 DAYS (U) =====
            colU.append([(
                f'=IF(N{row_idx}>30*I{row_idx};'
                f'"избыток";'
                f'IF(N{row_idx}<15*I{row_idx};'
                f'"дефицит";'
                f'"норма"))'
            )])

            # ===== STATUS 60 DAYS (X) =====
            colX.append([(
                f'=IF(N{row_idx}>60*I{row_idx};'
                f'"избыток";'
                f'IF(N{row_idx}<45*I{row_idx};'
                f'"дефицит";'
                f'"норма"))'
            )])

            # ===== STATUS 120 DAYS (AA) =====
            colAA.append([(
                f'=IF(N{row_idx}>120*I{row_idx};'
                f'"избыток";'
                f'IF(N{row_idx}<90*I{row_idx};'
                f'"дефицит";'
                f'"норма"))'
            )])

    updates = [
        {"range": f"H2:H{last_row}", "values": colH},
        {"range": f"I2:I{last_row}", "values": colI},
        {"range": f"J2:J{last_row}", "values": colJ},
        {"range": f"K2:K{last_row}", "values": colK},
        {"range": f"N2:N{last_row}", "values": colN},
        {"range": f"O2:O{last_row}", "values": colO},
        {"range": f"R2:R{last_row}", "values": colR},
        {"range": f"U2:U{last_row}", "values": colU},
        {"range": f"X2:X{last_row}", "values": colX},
        {"range": f"AA2:AA{last_row}", "values": colAA},
    ]
    return updates


def update_sheet():
    ss, ws = get_worksheet()
    idx_map = fetch_stocks_all()
    updates = build_column_updates(ws, idx_map)
    print("DEBUG: updates sample:", updates[:5])
    if not updates:
        print("Нет данных для обновления.")
        return
    body = {
        "valueInputOption": "USER_ENTERED",
        "data": updates,
    }
    ss.values_batch_update(body)
    print("Готово ✅")

    # Conditional formatting for statuses in N and Q
    start_row = 2
    end_row = compute_last_data_row(ws.get_all_values())

    def letter_to_index(col):
        col = col.upper()
        result = 0
        for char in col:
            result = result * 26 + (ord(char) - ord('A') + 1)
        return result - 1

    def _cf_rule(col_letter, text, red, green, blue):
        start_col = letter_to_index(col_letter)
        end_col = start_col + 1
        return {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": ws.id,
                        "startRowIndex": start_row-1,
                        "endRowIndex": end_row,
                        "startColumnIndex": start_col,
                        "endColumnIndex": end_col,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": text}]
                        },
                        "format": {"backgroundColor": {"red": red, "green": green, "blue": blue}}
                    }
                },
                "index": 0
            }
        }

    # Сносим ВСЕ существующие правила условного форматирования на листе,
    # иначе addConditionalFormatRule копит их при каждом запуске (+9 за прогон).
    meta = ss.fetch_sheet_metadata()
    existing_cf = 0
    for sh in meta.get("sheets", []):
        if sh.get("properties", {}).get("sheetId") == ws.id:
            existing_cf = len(sh.get("conditionalFormats", []) or [])
            break
    # Удаляем по индексу 0 столько раз, сколько правил было (после каждого
    # удаления правила переиндексируются, поэтому всегда индекс 0).
    clear_requests = [
        {"deleteConditionalFormatRule": {"sheetId": ws.id, "index": 0}}
        for _ in range(existing_cf)
    ]

    fmt_requests = [
        _cf_rule("U", "норма", 0.80, 0.94, 0.80),      # light green
        _cf_rule("U", "дефицит", 0.96, 0.80, 0.80),    # light red
        _cf_rule("U", "избыток", 1.00, 0.90, 0.70),    # light orange
        _cf_rule("X", "норма", 0.80, 0.94, 0.80),
        _cf_rule("X", "дефицит", 0.96, 0.80, 0.80),
        _cf_rule("X", "избыток", 1.00, 0.90, 0.70),
        _cf_rule("AA", "норма", 0.80, 0.94, 0.80),
        _cf_rule("AA", "дефицит", 0.96, 0.80, 0.80),
        _cf_rule("AA", "избыток", 1.00, 0.90, 0.70),
    ]
    ss.batch_update({"requests": clear_requests + fmt_requests})


if __name__ == "__main__":
    update_sheet()
