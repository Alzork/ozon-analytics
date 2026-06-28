import os
import gspread
from datetime import datetime, timedelta
from constants import sku_list
from dotenv import load_dotenv
import requests
import time
import random
load_dotenv()
# ============================================================
#  HELPERS
# ============================================================

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

def col_index_to_letter(n: int) -> str:
    """Convert 1-based column index to Google Sheets column letters."""
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters

def col_letter_to_index(col: str) -> int:
    idx = 0
    for c in col:
        idx = idx * 26 + (ord(c) - 64)
    return idx


def replace_single_column_refs(formula: str, from_col: str, to_col: str) -> str:
    """
    Заменяет ссылки вида V5, $V5, V$5, $V$5 на новый столбец (например W),
    не трогая номер строки.
    """
    import re
    pattern = re.compile(rf"(?<![A-Z])(\$?){from_col}(?=(\$?\d))")
    return pattern.sub(rf"\1{to_col}", formula)


WEEKLY_API_OVERRIDE_METRICS = {
    "Уникальные посетители, всего": "session_view",
    "Уникальные посетители  в поиске или каталоге": "session_view_search",
    "Уникальные посетители карточки товара": "session_view_pdp",
    "Конверсия в корзину, общая": "conv_tocart",
    "Конверсия в корзину из карточки товара": "conv_tocart_pdp",
    "Конверсия в корзину из поиска или каталога": "conv_tocart_search",
}


def safe_number(x):
    try:
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str):
            return float(x.replace(",", "."))
    except Exception:
        return 0.0
    return 0.0


def fetch_weekly_api_metrics(monday, sunday):
    client_id = os.getenv("OZON_CLIENT_ID")
    api_key = os.getenv("OZON_API_KEY")
    if not client_id or not api_key:
        print("OZON_CLIENT_ID/OZON_API_KEY не найдены — метрики уников оставляю по формулам.")
        return {}

    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key
    }
    metric_codes = list(WEEKLY_API_OVERRIDE_METRICS.values())
    body = {
        "date_from": monday.isoformat(),
        "date_to": sunday.isoformat(),
        "metrics": metric_codes,
        "dimensions": ["day"],
        "filters": [
            {"key": "state", "value": "ACTIVE"},
            {"key": "visibility", "value": "ALL"}
        ],
        "limit": 1000
    }

    url = "https://api-seller.ozon.ru/v1/analytics/data"
    resp_json = None
    for attempt in range(5):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            if resp.status_code == 200:
                resp_json = resp.json()
                break
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.5 * (attempt + 1))
                continue
            resp.raise_for_status()
        except Exception as e:
            if attempt == 4:
                print(f"Ошибка запроса weekly уников: {e}")
            time.sleep(0.5 * (attempt + 1))

    if not resp_json:
        return {}

    result = {}
    totals = (resp_json.get("result", {}) or {}).get("totals") or []
    if len(totals) >= len(metric_codes):
        for metric_name, metric_code in WEEKLY_API_OVERRIDE_METRICS.items():
            metric_idx = metric_codes.index(metric_code)
            result[metric_name] = safe_number(totals[metric_idx])
        return result

    # fallback если totals не пришли:
    # для conv_* берём среднее по дням, для остальных — сумму
    data = (resp_json.get("result", {}) or {}).get("data") or []
    sums = {code: 0.0 for code in metric_codes}
    counts = {code: 0 for code in metric_codes}
    for row in data:
        values = row.get("metrics") or []
        for i, code in enumerate(metric_codes):
            val = safe_number(values[i]) if i < len(values) else 0.0
            sums[code] += val
            if code.startswith("conv_"):
                counts[code] += 1

    for metric_name, metric_code in WEEKLY_API_OVERRIDE_METRICS.items():
        if metric_code.startswith("conv_"):
            result[metric_name] = (sums.get(metric_code, 0.0) / counts.get(metric_code, 1)) if counts.get(metric_code, 0) > 0 else 0.0
        else:
            result[metric_name] = sums.get(metric_code, 0.0)
    return result


def override_weekly_api_rows(ws_wow, new_col_letter, monday, sunday):
    weekly_values = fetch_weekly_api_metrics(monday, sunday)
    if not weekly_values:
        return

    row_labels = google_call("WOW col_values(1)", ws_wow.col_values, 1)
    updates = []
    for row_idx, label in enumerate(row_labels, start=1):
        if label in weekly_values:
            updates.append({
                "range": f"{new_col_letter}{row_idx}",
                "values": [[weekly_values[label]]]
            })

    if updates:
        google_call("WOW batch_update weekly API rows", ws_wow.batch_update, updates, value_input_option="USER_ENTERED")
        print("Основной WOW: 6 метрик (уники + конверсии) обновлены из Ozon Analytics за неделю.")


# ============================================================
#  GOOGLE SHEETS LOADING
# ============================================================

def get_sheet():
    gc = gspread.service_account(filename="google-creds.json")
    sheet_name = os.getenv("GOOGLE_SHEET_NAME")
    if not sheet_name:
        raise RuntimeError("GOOGLE_SHEET_NAME не найден в .env")
    return google_call("open spreadsheet", gc.open, sheet_name)


# ============================================================
#  DATE LOGIC — previous full week Mon–Sun
# ============================================================

def get_last_full_week():
    today = datetime.now().date()
    weekday = today.weekday()  # Monday = 0
    this_week_monday = today - timedelta(days=weekday)
    last_week_monday = this_week_monday - timedelta(days=7)
    last_week_sunday = last_week_monday + timedelta(days=6)
    return last_week_monday, last_week_sunday


# ============================================================
#  FIND COLUMN RANGE IN SVOD FOR LAST WEEK (Mon–Sun)
# ============================================================

def find_week_columns_in_svod(ws_svod, monday, sunday):
    header = google_call(f"{ws_svod.title} row_values(2)", ws_svod.row_values, 2)

    monday_str = monday.isoformat()
    sunday_str = sunday.isoformat()

    try:
        start_idx = header.index(monday_str) + 1  # convert to 1-based
        end_idx = header.index(sunday_str) + 1
    except ValueError:
        raise RuntimeError("Не найдены даты понедельника/воскресенья в Свод!")

    return col_index_to_letter(start_idx), col_index_to_letter(end_idx)

# ============================
#  FIND WEEK COLUMNS IN DATA (SKU/SVOD) BY DATE
# ============================
def find_week_columns_in_data(ws_data, monday, sunday, header_row: int = 2):
    """
    Ищет колонки с датами (ISO: YYYY-MM-DD) в строке header_row листа данных (SKU / Свод).
    Возвращает буквы старт/конец.
    """
    header = google_call(f"{ws_data.title} row_values({header_row})", ws_data.row_values, header_row)

    monday_str = monday.isoformat()
    sunday_str = sunday.isoformat()

    try:
        start_idx = header.index(monday_str) + 1
        end_idx = header.index(sunday_str) + 1
    except ValueError:
        raise RuntimeError(f"Не найдены даты {monday_str} / {sunday_str} в листе данных: {ws_data.title}")

    return col_index_to_letter(start_idx), col_index_to_letter(end_idx)


# ============================================================
#  COPY LAST COLUMN IN WOW AND UPDATE FORMULAS
# ============================================================

def write_formulas_to_wow(ss, ws_wow, new_start, new_end, monday, sunday):

    print("Проверяем основной WOW...")
    # Determine last filled column
    header = google_call(f"{ws_wow.title} row_values(1)", ws_wow.row_values, 1)

    # find last real week column, not calculation ones
    last_week_idx = 0
    for idx, name in enumerate(header, start=1):
        if name.startswith("w") and "(" in name:
            last_week_idx = idx
    if last_week_idx == 0:
        raise RuntimeError("Не найден ни один недельный столбец wXX (...).")
    last_col_index = last_week_idx

    # Prepare new header title
    new_header = f"w{monday.isocalendar()[1]} ({monday.strftime('%d.%m')}-{sunday.strftime('%d.%m')})"

    # Duplicate protection and logic for overwriting formulas if column exists
    if new_header in header:
        new_col_index = header.index(new_header) + 1
        new_col_letter = col_index_to_letter(new_col_index)
        print(f"Основной WOW: колонка {new_header} существует — перезаписываю формулы.")
    else:
        # Add new column
        google_call(f"{ws_wow.title} add_cols week", ws_wow.add_cols, 1)
        new_col_index = last_col_index + 1
        new_col_letter = col_index_to_letter(new_col_index)
        # Copy formatting from previous week column to new column
        sheet_id = ws_wow._properties['sheetId']
        copy_requests = [{
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_wow.row_count,
                    "startColumnIndex": last_col_index - 1,
                    "endColumnIndex": last_col_index,
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_wow.row_count,
                    "startColumnIndex": new_col_index - 1,
                    "endColumnIndex": new_col_index,
                },
                "pasteType": "PASTE_FORMAT",
            }
        }]
        google_call("spreadsheet batch_update copy week format", ss.batch_update, {"requests": copy_requests})
        # Write header
        google_call(
            f"{ws_wow.title} update week header",
            ws_wow.update,
            values=[[new_header]],
            range_name=f"{col_index_to_letter(new_col_index)}1",
            value_input_option="USER_ENTERED",
        )

    # ============================
    # CREATE OR UPDATE DELTA COLUMN (wX - wX-1): Δ всегда пересчитывается
    # ============================
    # Δ-столбец всегда справа от недельного
    delta_col_index = new_col_index + 1
    delta_col_letter = col_index_to_letter(delta_col_index)
    delta_header = f"Δ% {new_header.split(' ')[0]}"
    # Предыдущий недельный столбец = последний недельный слева от новой недели
    week_indices = [i + 1 for i, name in enumerate(header) if name.startswith("w") and "(" in name]
    prev_week_idx = None

    # если мы ДОБАВИЛИ новую неделю, то предыдущая = last_col_index
    if new_header not in header:
        prev_week_idx = last_col_index
    else:
        # если колонка существовала (перезапись), берём ближайшую недельную слева от неё
        left_weeks = [i for i in week_indices if i < new_col_index]
        prev_week_idx = max(left_weeks) if left_weeks else None
    # Проверяем, есть ли уже Δ-столбец
    need_create_delta = False
    if len(header) < delta_col_index or header[delta_col_index-1] != delta_header:
        # Δ-столбца нет, надо создать
        google_call(f"{ws_wow.title} add_cols delta", ws_wow.add_cols, 1)
        need_create_delta = True
        # Copy formatting from new week column
        sheet_id = ws_wow._properties['sheetId']
        copy_requests = [{
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_wow.row_count,
                    "startColumnIndex": new_col_index - 1,
                    "endColumnIndex": new_col_index,
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_wow.row_count,
                    "startColumnIndex": delta_col_index - 1,
                    "endColumnIndex": delta_col_index,
                },
                "pasteType": "PASTE_FORMAT",
            }
        }]
        google_call("spreadsheet batch_update copy delta format", ss.batch_update, {"requests": copy_requests})
        # Write delta header
        google_call(
            f"{ws_wow.title} update delta header",
            ws_wow.update,
            values=[[delta_header]],
            range_name=f"{delta_col_letter}1",
            value_input_option="USER_ENTERED"
        )
    # ВСЕГДА записываем формулы Δ
    delta_formulas = []
    for row in range(2, ws_wow.row_count + 1):
        if prev_week_idx is not None:
            prev_col_letter = col_index_to_letter(prev_week_idx)
        else:
            prev_col_letter = col_index_to_letter(new_col_index)  # fallback: 0
        delta_formulas.append([
            f"=IFERROR(({col_index_to_letter(new_col_index)}{row}-{prev_col_letter}{row})/{prev_col_letter}{row};0)"
        ])
    google_call(
        f"{ws_wow.title} update delta formulas",
        ws_wow.update,
        values=delta_formulas,
        range_name=f"{delta_col_letter}2:{delta_col_letter}{1 + len(delta_formulas)}",
        value_input_option="USER_ENTERED"
    )
    # Format delta column as percent if created
    if need_create_delta:
        google_call("spreadsheet batch_update delta percent format", ss.batch_update, {
            "requests": [{
                "repeatCell": {
                    "range": {
                        "sheetId": ws_wow._properties['sheetId'],
                        "startRowIndex": 1,
                        "endRowIndex": ws_wow.row_count,
                        "startColumnIndex": delta_col_index - 1,
                        "endColumnIndex": delta_col_index
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "PERCENT",
                                "pattern": "0.00%"
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat"
                }
            }]
        })
    # Условное форматирование только если Δ создавался
    if need_create_delta:
        format_requests = {
            "requests": [
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": ws_wow._properties['sheetId'],
                                "startRowIndex": 1,
                                "endRowIndex": ws_wow.row_count,
                                "startColumnIndex": delta_col_index - 1,
                                "endColumnIndex": delta_col_index
                            }],
                            "booleanRule": {
                                "condition": {
                                    "type": "NUMBER_GREATER",
                                    "values": [{"userEnteredValue": "0"}]
                                },
                                "format": {
                                    "backgroundColor": {"red": 0.8, "green": 1.0, "blue": 0.8}
                                }
                            }
                        },
                        "index": 0
                    }
                },
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": ws_wow._properties['sheetId'],
                                "startRowIndex": 1,
                                "endRowIndex": ws_wow.row_count,
                                "startColumnIndex": delta_col_index - 1,
                                "endColumnIndex": delta_col_index
                            }],
                            "booleanRule": {
                                "condition": {
                                    "type": "NUMBER_LESS",
                                    "values": [{"userEnteredValue": "0"}]
                                },
                                "format": {
                                    "backgroundColor": {"red": 1.0, "green": 0.8, "blue": 0.8}
                                }
                            }
                        },
                        "index": 0
                    }
                }
            ]
        }
        google_call("spreadsheet batch_update delta conditional format", ss.batch_update, format_requests)

    # === Determine source column for copying formulas ===
    if new_header not in header:
        # New week added → copy formulas from previous rightmost week
        source_week_idx = last_col_index
    else:
        # Existing week overwritten → copy formulas from week to the LEFT
        if prev_week_idx is None:
            raise RuntimeError("Не удалось определить предыдущую неделю для копирования формул.")
        source_week_idx = prev_week_idx

    source_col_letter = col_index_to_letter(source_week_idx)
    range_str = f"{source_col_letter}2:{source_col_letter}239"
    formulas_raw = google_call(f"{ws_wow.title} get source formulas", ws_wow.get, range_str, value_render_option="FORMULA")
    formulas = [row[0] if row else "" for row in formulas_raw]

    import re
    # Диапазон колонок для новой недели берём из параметров функции (из листа "Свод")
    # new_start/new_end уже буквы колонок, которые соответствуют last full week Mon–Sun
    start_col, end_col = new_start, new_end

    # шаблон для замены любого диапазона БУКВЫ:БУКВЫ (цифры строки не трогаем)
    range_pattern = re.compile(r"('Свод'!)([A-Z]+)(\d+):([A-Z]+)(\d+)")
    # шаблон для одиночных ссылок =H30/H15 и т.п.
    prev_col = col_index_to_letter(source_week_idx)
    next_col = col_index_to_letter(new_col_index)

    updated_formulas = []
    for f in formulas:
        # пустая ячейка
        if f in (None, ""):
            updated_formulas.append([""])
            continue

        # если это не формула (число, текст) — копируем как есть
        if not isinstance(f, str) or not f.startswith("="):
            updated_formulas.append([f])
            continue

        # 1. диапазонная замена
        def _repl(match):
            return f"{match.group(1)}{start_col}{match.group(3)}:{end_col}{match.group(5)}"

        updated = range_pattern.sub(_repl, f)

        # 2. одиночные ссылки
        updated = replace_single_column_refs(updated, prev_col, next_col)

        updated_formulas.append([updated])

    # Batch-write formulas (only once)
    if updated_formulas:
        start_row = 2
        end_row = 2 + len(updated_formulas) - 1
        range_new = f"{new_col_letter}{start_row}:{new_col_letter}{end_row}"
        google_call(
            f"{ws_wow.title} update weekly formulas",
            ws_wow.update,
            values=updated_formulas,
            range_name=range_new,
            value_input_option="USER_ENTERED",
        )

    # Для 6 метрик (уники + конверсии) берём недельные значения напрямую из Ozon Analytics API
    override_weekly_api_rows(ws_wow, new_col_letter, monday, sunday)


# ============================================================
#  SKU WEEKLY UPDATE
# ============================================================

def update_sku_weekly(ss, sku, monday, sunday, header_cache):
    """Обновляет недельный столбец для одного SKU."""
    sheet_name_data = sku
    sheet_name_wow = f"{sku} WOW"

    print(f"Проверяем {sheet_name_wow}...")

    try:
        ws_data = google_call(f"open worksheet {sheet_name_data}", ss.worksheet, sheet_name_data)
    except:
        print(f"{sheet_name_wow}: лист данных {sheet_name_data} не найден — пропускаю.")
        return

    # Колонки новой недели на листе данных SKU (обычно даты лежат во 2-й строке как в "Свод")
    try:
        data_start_col, data_end_col = find_week_columns_in_data(ws_data, monday, sunday, header_row=2)
    except Exception as e:
        print(f"{sheet_name_wow}: не удалось найти колонки недели в листе данных {sheet_name_data}: {e}")
        return

    try:
        ws_wow = google_call(f"open worksheet {sheet_name_wow}", ss.worksheet, sheet_name_wow)
    except:
        print(f"{sheet_name_wow}: лист WOW не найден — пропускаю.")
        return

    header = google_call(f"{ws_wow.title} row_values(1)", ws_wow.row_values, 1)

    # Find last real week column
    last_week_idx = 0
    for idx, name in enumerate(header, start=1):
        if name.startswith("w") and "(" in name:
            last_week_idx = idx
    if last_week_idx == 0:
        print(f"{sheet_name_wow}: не найден ни один недельный столбец wXX (...), пропускаю.")
        return

    # New header
    new_header = f"w{monday.isocalendar()[1]} ({monday.strftime('%d.%m')}-{sunday.strftime('%d.%m')})"
    if new_header in header:
        new_col_index = header.index(new_header) + 1
        new_col_letter = col_index_to_letter(new_col_index)
        print(f"{sheet_name_wow}: колонка {new_header} существует — перезаписываю.")
    else:
        # Add new column
        google_call(f"{ws_wow.title} add_cols week", ws_wow.add_cols, 1)
        new_col_index = last_week_idx + 1
        new_col_letter = col_index_to_letter(new_col_index)
        # Copy formatting from previous week column to new column
        sheet_id = ws_wow._properties['sheetId']
        copy_requests = [{
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_wow.row_count,
                    "startColumnIndex": last_week_idx - 1,
                    "endColumnIndex": last_week_idx,
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_wow.row_count,
                    "startColumnIndex": new_col_index - 1,
                    "endColumnIndex": new_col_index,
                },
                "pasteType": "PASTE_FORMAT",
            }
        }]
        google_call("spreadsheet batch_update copy SKU week format", ss.batch_update, {"requests": copy_requests})
        # Write header
        google_call(
            f"{ws_wow.title} update week header",
            ws_wow.update,
            values=[[new_header]],
            range_name=f"{col_index_to_letter(new_col_index)}1",
            value_input_option="USER_ENTERED",
        )

    # Determine old formulas column
    last_col_letter = col_index_to_letter(last_week_idx)

    # Batch-read formulas (SKU WOW has up to 85 rows)
    range_str = f"{last_col_letter}2:{last_col_letter}85"
    formulas_raw = google_call(f"{ws_wow.title} get source formulas", ws_wow.get, range_str, value_render_option="FORMULA")
    formulas = [row[0] if row else "" for row in formulas_raw]

    import re
    # Диапазон колонок для новой недели берём из найденных дат на листе данных SKU
    start_col, end_col = data_start_col, data_end_col

    prev_col = col_index_to_letter(last_week_idx)
    next_col = col_index_to_letter(new_col_index)
    # шаблон для диапазонов
    sheet_escaped = re.escape(sheet_name_data)
    range_pattern = re.compile(rf"('{sheet_escaped}'!)([A-Z]+)(\d+):([A-Z]+)(\d+)")
    # шаблон для одиночных ссылок

    updated_formulas = []
    for f in formulas:
        if not f:
            updated_formulas.append([""])
            continue
        # 2. Сначала диапазонная замена
        def _repl(match):
            return f"{match.group(1)}{start_col}{match.group(3)}:{end_col}{match.group(5)}"
        updated = range_pattern.sub(_repl, f)
        # 3. Затем одиночные ссылки
        updated = replace_single_column_refs(updated, prev_col, next_col)
        updated_formulas.append([updated])

    # Batch-write formulas
    if updated_formulas:
        start_row = 2
        end_row = 2 + len(updated_formulas) - 1
        range_new = f"{new_col_letter}{start_row}:{new_col_letter}{end_row}"
        google_call(
            f"{ws_wow.title} update weekly formulas",
            ws_wow.update,
            values=updated_formulas,
            range_name=range_new,
            value_input_option="USER_ENTERED",
        )

    # ============================
    # CREATE OR UPDATE DELTA COLUMN (wX - wX-1): Δ всегда пересчитывается
    # ============================
    delta_col_index = new_col_index + 1
    delta_col_letter = col_index_to_letter(delta_col_index)
    delta_header = f"Δ% {new_header.split(' ')[0]}"
    # Предыдущий недельный столбец = последний недельный слева от новой недели
    week_indices = [i + 1 for i, name in enumerate(header) if name.startswith("w") and "(" in name]
    prev_week_idx = None

    if new_header not in header:
        prev_week_idx = last_week_idx
    else:
        left_weeks = [i for i in week_indices if i < new_col_index]
        prev_week_idx = max(left_weeks) if left_weeks else None
    need_create_delta = False
    if len(header) < delta_col_index or header[delta_col_index-1] != delta_header:
        google_call(f"{ws_wow.title} add_cols delta", ws_wow.add_cols, 1)
        need_create_delta = True
        # Copy formatting from new week column
        sheet_id = ws_wow._properties['sheetId']
        copy_requests = [{
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_wow.row_count,
                    "startColumnIndex": new_col_index - 1,
                    "endColumnIndex": new_col_index,
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_wow.row_count,
                    "startColumnIndex": delta_col_index - 1,
                    "endColumnIndex": delta_col_index,
                },
                "pasteType": "PASTE_FORMAT",
            }
        }]
        google_call("spreadsheet batch_update copy SKU delta format", ss.batch_update, {"requests": copy_requests})
        # Write delta header
        google_call(
            f"{ws_wow.title} update delta header",
            ws_wow.update,
            values=[[delta_header]],
            range_name=f"{delta_col_letter}1",
            value_input_option="USER_ENTERED"
        )
    # ВСЕГДА записываем формулы Δ
    delta_formulas = []
    for row in range(2, ws_wow.row_count + 1):
        if prev_week_idx is not None:
            prev_col_letter = col_index_to_letter(prev_week_idx)
        else:
            prev_col_letter = col_index_to_letter(new_col_index)  # fallback: 0
        delta_formulas.append([
            f"=IFERROR(({col_index_to_letter(new_col_index)}{row}-{prev_col_letter}{row})/{prev_col_letter}{row};0)"
        ])
    google_call(
        f"{ws_wow.title} update delta formulas",
        ws_wow.update,
        values=delta_formulas,
        range_name=f"{delta_col_letter}2:{delta_col_letter}{1 + len(delta_formulas)}",
        value_input_option="USER_ENTERED"
    )
    # Format delta column as percent if created
    if need_create_delta:
        google_call("spreadsheet batch_update SKU delta percent format", ss.batch_update, {
            "requests": [{
                "repeatCell": {
                    "range": {
                        "sheetId": ws_wow._properties['sheetId'],
                        "startRowIndex": 1,
                        "endRowIndex": ws_wow.row_count,
                        "startColumnIndex": delta_col_index - 1,
                        "endColumnIndex": delta_col_index
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "PERCENT",
                                "pattern": "0.00%"
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat"
                }
            }]
        })
    # Условное форматирование только если Δ создавался
    if need_create_delta:
        format_requests = {
            "requests": [
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": ws_wow._properties['sheetId'],
                                "startRowIndex": 1,
                                "endRowIndex": ws_wow.row_count,
                                "startColumnIndex": delta_col_index - 1,
                                "endColumnIndex": delta_col_index
                            }],
                            "booleanRule": {
                                "condition": {
                                    "type": "NUMBER_GREATER",
                                    "values": [{"userEnteredValue": "0"}]
                                },
                                "format": {
                                    "backgroundColor": {"red": 0.8, "green": 1.0, "blue": 0.8}
                                }
                            }
                        },
                        "index": 0
                    }
                },
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": ws_wow._properties['sheetId'],
                                "startRowIndex": 1,
                                "endRowIndex": ws_wow.row_count,
                                "startColumnIndex": delta_col_index - 1,
                                "endColumnIndex": delta_col_index
                            }],
                            "booleanRule": {
                                "condition": {
                                    "type": "NUMBER_LESS",
                                    "values": [{"userEnteredValue": "0"}]
                                },
                                "format": {
                                    "backgroundColor": {"red": 1.0, "green": 0.8, "blue": 0.8}
                                }
                            }
                        },
                        "index": 0
                    }
                }
            ]
        }
        google_call("spreadsheet batch_update SKU delta conditional format", ss.batch_update, format_requests)

    # (Удалён дублирующийся и ошибочный код обновления формул, см. выше)
    print(f"{sheet_name_wow}: неделя {new_header} обновлена.")


# ============================================================
#  MAIN
# ============================================================

def generate_weekly_formulas():
    ss = get_sheet()

    try:
        ws_svod = google_call("open worksheet Свод", ss.worksheet, "Свод")
    except:
        raise RuntimeError("Не найден лист 'Свод'!")

    try:
        ws_wow = google_call("open worksheet WOW", ss.worksheet, "WOW")
    except:
        ws_wow = google_call("add worksheet WOW", ss.add_worksheet, "WOW", rows=400, cols=200)

    # Get last full week dates
    monday, sunday = get_last_full_week()

    # Find column range in Svod for the last week
    start_col_letter, end_col_letter = find_week_columns_in_svod(ws_svod, monday, sunday)

    # Copy last WOW column and update formulas
    write_formulas_to_wow(ss, ws_wow, start_col_letter, end_col_letter, monday, sunday)

    print(f"Основной WOW: неделя {monday} — {sunday} записана.")


# ============================================================
#  UPDATE ALL SKU WEEKLY
# ============================================================

def update_all_sku_weekly():
    print("Проверяем все SKU WOW...")
    ss = get_sheet()
    monday, sunday = get_last_full_week()

    header_cache = {}
    for sku in sku_list:
        time.sleep(1.5)
        update_sku_weekly(ss, sku, monday, sunday, header_cache)

    print("Все SKU WOW обработаны.")

if __name__ == "__main__":
    generate_weekly_formulas()
    update_all_sku_weekly()
