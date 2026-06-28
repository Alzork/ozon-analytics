import os
import gspread
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests
import time
import random
load_dotenv()
# ============================================================
#  HELPERS
# ============================================================

RETRYABLE_GOOGLE_CODES = {404, 408, 409, 429, 500, 502, 503, 504}


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


MONTHLY_API_OVERRIDE_METRICS = {
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


def fetch_monthly_api_metrics(first_day, last_day):
    client_id = os.getenv("OZON_CLIENT_ID")
    api_key = os.getenv("OZON_API_KEY")
    if not client_id or not api_key:
        print("OZON_CLIENT_ID/OZON_API_KEY не найдены — метрики уников оставляю по формулам.")
        return {}

    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key
    }
    metric_codes = list(MONTHLY_API_OVERRIDE_METRICS.values())
    body = {
        "date_from": first_day.isoformat(),
        "date_to": last_day.isoformat(),
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
                print(f"Ошибка запроса monthly уников: {e}")
            time.sleep(0.5 * (attempt + 1))

    if not resp_json:
        return {}

    result = {}
    totals = (resp_json.get("result", {}) or {}).get("totals") or []
    if len(totals) >= len(metric_codes):
        for metric_name, metric_code in MONTHLY_API_OVERRIDE_METRICS.items():
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

    for metric_name, metric_code in MONTHLY_API_OVERRIDE_METRICS.items():
        if metric_code.startswith("conv_"):
            result[metric_name] = (sums.get(metric_code, 0.0) / counts.get(metric_code, 1)) if counts.get(metric_code, 0) > 0 else 0.0
        else:
            result[metric_name] = sums.get(metric_code, 0.0)
    return result


def override_monthly_api_rows(ws_mom, new_col_letter, first_day, last_day):
    monthly_values = fetch_monthly_api_metrics(first_day, last_day)
    if not monthly_values:
        return

    row_labels = google_call("MOM col_values(1)", ws_mom.col_values, 1)
    updates = []
    for row_idx, label in enumerate(row_labels, start=1):
        if label in monthly_values:
            updates.append({
                "range": f"{new_col_letter}{row_idx}",
                "values": [[monthly_values[label]]]
            })

    if updates:
        google_call("MOM batch_update monthly API rows", ws_mom.batch_update, updates, value_input_option="USER_ENTERED")
        print("Основной MOM: 6 метрик (уники + конверсии) обновлены из Ozon Analytics за месяц.")


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
#  DATE LOGIC — previous full calendar month (1-е → последнее число)
# ============================================================

def get_last_full_month():
    today = datetime.now().date()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    first_of_prev_month = last_of_prev_month.replace(day=1)
    return first_of_prev_month, last_of_prev_month


# ============================================================
#  FIND COLUMN RANGE IN SVOD FOR LAST MONTH (1-е → последнее число)
# ============================================================

def find_month_columns_in_svod(ws_svod, first_day, last_day):
    header = google_call(f"{ws_svod.title} row_values(2)", ws_svod.row_values, 2)

    first_str = first_day.isoformat()
    last_str = last_day.isoformat()

    try:
        start_idx = header.index(first_str) + 1  # convert to 1-based
        end_idx = header.index(last_str) + 1
    except ValueError:
        raise RuntimeError(f"Не найдены даты {first_str} / {last_str} в Свод!")

    return col_index_to_letter(start_idx), col_index_to_letter(end_idx)


# ============================================================
#  COPY LAST COLUMN IN MOM AND UPDATE FORMULAS
# ============================================================

def bootstrap_first_month_from_wow(ss, ws_mom, new_start, new_end, first_day, last_day):
    """
    Если на листе MOM ещё нет ни одной месячной колонки, создаём первую колонку,
    взяв ШАБЛОН формул из листа WOW (там та же структура строк). Недельный диапазон
    'Свод' в формулах заменяется на месячный (new_start..new_end за 1-е..последнее число).
    """
    print("Лист MOM пуст (нет колонки мXX) — создаю первую колонку из шаблона WOW...")
    try:
        ws_wow = google_call("open worksheet WOW", ss.worksheet, "WOW")
    except Exception:
        raise RuntimeError("Для bootstrap нужен лист 'WOW' как шаблон, но он не найден.")

    wow_header = google_call("WOW row_values(1)", ws_wow.row_values, 1)
    wow_src_idx = 0
    for idx, name in enumerate(wow_header, start=1):
        if name.startswith("w") and "(" in name:
            wow_src_idx = idx
    if wow_src_idx == 0:
        raise RuntimeError("В листе 'WOW' нет недельной колонки wXX (...) для шаблона.")
    wow_src_letter = col_index_to_letter(wow_src_idx)

    # читаем формулы шаблонной недели из WOW
    range_str = f"{wow_src_letter}2:{wow_src_letter}239"
    formulas_raw = google_call("WOW get template formulas", ws_wow.get, range_str, value_render_option="FORMULA")
    formulas = [row[0] if row else "" for row in formulas_raw]

    # первая колонка данных MOM = B (сразу после подписей в A)
    new_col_index = 2
    new_col_letter = col_index_to_letter(new_col_index)
    new_header = f"м{first_day.month:02d} ({first_day.strftime('%d.%m')}-{last_day.strftime('%d.%m')})"

    # копируем числовые форматы строк из шаблонной колонки WOW в колонку MOM
    rows = min(ws_wow.row_count, ws_mom.row_count)
    copy_requests = [{
        "copyPaste": {
            "source": {
                "sheetId": ws_wow._properties['sheetId'],
                "startRowIndex": 0,
                "endRowIndex": rows,
                "startColumnIndex": wow_src_idx - 1,
                "endColumnIndex": wow_src_idx,
            },
            "destination": {
                "sheetId": ws_mom._properties['sheetId'],
                "startRowIndex": 0,
                "endRowIndex": rows,
                "startColumnIndex": new_col_index - 1,
                "endColumnIndex": new_col_index,
            },
            "pasteType": "PASTE_FORMAT",
        }
    }]
    google_call("copy WOW format to MOM", ss.batch_update, {"requests": copy_requests})

    # заголовок
    google_call(
        "MOM write first header",
        ws_mom.update,
        values=[[new_header]],
        range_name=f"{new_col_letter}1",
        value_input_option="USER_ENTERED",
    )

    import re
    start_col, end_col = new_start, new_end
    range_pattern = re.compile(r"('Свод'!)([A-Z]+)(\d+):([A-Z]+)(\d+)")
    prev_col = wow_src_letter
    next_col = new_col_letter

    updated_formulas = []
    for f in formulas:
        if f in (None, ""):
            updated_formulas.append([""])
            continue
        if not isinstance(f, str) or not f.startswith("="):
            updated_formulas.append([f])
            continue

        def _repl(match):
            return f"{match.group(1)}{start_col}{match.group(3)}:{end_col}{match.group(5)}"

        updated = range_pattern.sub(_repl, f)
        updated = replace_single_column_refs(updated, prev_col, next_col)
        updated_formulas.append([updated])

    if updated_formulas:
        end_row = 2 + len(updated_formulas) - 1
        google_call(
            "MOM write first formulas",
            ws_mom.update,
            values=updated_formulas,
            range_name=f"{new_col_letter}2:{new_col_letter}{end_row}",
            value_input_option="USER_ENTERED",
        )

    # 6 метрик (уники + конверсии) — напрямую из Ozon Analytics API за месяц
    override_monthly_api_rows(ws_mom, new_col_letter, first_day, last_day)
    print(f"Основной MOM: первая колонка {new_header} создана из шаблона WOW.")


def write_formulas_to_mom(ss, ws_mom, new_start, new_end, first_day, last_day):

    print("Проверяем основной MOM...")
    # Determine last filled column
    header = google_call(f"{ws_mom.title} row_values(1)", ws_mom.row_values, 1)

    # find last real month column, not calculation ones
    last_month_idx = 0
    for idx, name in enumerate(header, start=1):
        if name.startswith("м") and "(" in name:
            last_month_idx = idx
    if last_month_idx == 0:
        # На листе ещё нет ни одной месячной колонки — создаём первую из шаблона WOW
        bootstrap_first_month_from_wow(ss, ws_mom, new_start, new_end, first_day, last_day)
        return
    last_col_index = last_month_idx

    # Prepare new header title
    new_header = f"м{first_day.month:02d} ({first_day.strftime('%d.%m')}-{last_day.strftime('%d.%m')})"

    # Duplicate protection and logic for overwriting formulas if column exists
    if new_header in header:
        new_col_index = header.index(new_header) + 1
        new_col_letter = col_index_to_letter(new_col_index)
        print(f"Основной MOM: колонка {new_header} существует — перезаписываю формулы.")
    else:
        # Add new column
        google_call(f"{ws_mom.title} add_cols month", ws_mom.add_cols, 1)
        new_col_index = last_col_index + 1
        new_col_letter = col_index_to_letter(new_col_index)
        # Copy formatting from previous month column to new column
        sheet_id = ws_mom._properties['sheetId']
        copy_requests = [{
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_mom.row_count,
                    "startColumnIndex": last_col_index - 1,
                    "endColumnIndex": last_col_index,
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_mom.row_count,
                    "startColumnIndex": new_col_index - 1,
                    "endColumnIndex": new_col_index,
                },
                "pasteType": "PASTE_FORMAT",
            }
        }]
        google_call("spreadsheet batch_update copy month format", ss.batch_update, {"requests": copy_requests})
        # Write header
        google_call(
            f"{ws_mom.title} update month header",
            ws_mom.update,
            values=[[new_header]],
            range_name=f"{col_index_to_letter(new_col_index)}1",
            value_input_option="USER_ENTERED",
        )

    # ============================
    # CREATE OR UPDATE DELTA COLUMN (мX - мX-1): Δ всегда пересчитывается
    # ============================
    # Δ-столбец всегда справа от месячного
    delta_col_index = new_col_index + 1
    delta_col_letter = col_index_to_letter(delta_col_index)
    delta_header = f"Δ% {new_header.split(' ')[0]}"
    # Предыдущий месячный столбец = последний месячный слева от нового месяца
    month_indices = [i + 1 for i, name in enumerate(header) if name.startswith("м") and "(" in name]
    prev_month_idx = None

    # если мы ДОБАВИЛИ новый месяц, то предыдущий = last_col_index
    if new_header not in header:
        prev_month_idx = last_col_index
    else:
        # если колонка существовала (перезапись), берём ближайшую месячную слева от неё
        left_months = [i for i in month_indices if i < new_col_index]
        prev_month_idx = max(left_months) if left_months else None
    # Проверяем, есть ли уже Δ-столбец
    need_create_delta = False
    if len(header) < delta_col_index or header[delta_col_index-1] != delta_header:
        # Δ-столбца нет, надо создать
        google_call(f"{ws_mom.title} add_cols delta", ws_mom.add_cols, 1)
        need_create_delta = True
        # Copy formatting from new month column
        sheet_id = ws_mom._properties['sheetId']
        copy_requests = [{
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_mom.row_count,
                    "startColumnIndex": new_col_index - 1,
                    "endColumnIndex": new_col_index,
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": ws_mom.row_count,
                    "startColumnIndex": delta_col_index - 1,
                    "endColumnIndex": delta_col_index,
                },
                "pasteType": "PASTE_FORMAT",
            }
        }]
        google_call("spreadsheet batch_update copy delta format", ss.batch_update, {"requests": copy_requests})
        # Write delta header
        google_call(
            f"{ws_mom.title} update delta header",
            ws_mom.update,
            values=[[delta_header]],
            range_name=f"{delta_col_letter}1",
            value_input_option="USER_ENTERED"
        )
    # ВСЕГДА записываем формулы Δ
    delta_formulas = []
    for row in range(2, ws_mom.row_count + 1):
        if prev_month_idx is not None:
            prev_col_letter = col_index_to_letter(prev_month_idx)
        else:
            prev_col_letter = col_index_to_letter(new_col_index)  # fallback: 0
        delta_formulas.append([
            f"=IFERROR(({col_index_to_letter(new_col_index)}{row}-{prev_col_letter}{row})/{prev_col_letter}{row};0)"
        ])
    google_call(
        f"{ws_mom.title} update delta formulas",
        ws_mom.update,
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
                        "sheetId": ws_mom._properties['sheetId'],
                        "startRowIndex": 1,
                        "endRowIndex": ws_mom.row_count,
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
                                "sheetId": ws_mom._properties['sheetId'],
                                "startRowIndex": 1,
                                "endRowIndex": ws_mom.row_count,
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
                                "sheetId": ws_mom._properties['sheetId'],
                                "startRowIndex": 1,
                                "endRowIndex": ws_mom.row_count,
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
        # New month added → copy formulas from previous rightmost month
        source_month_idx = last_col_index
    else:
        # Existing month overwritten → copy formulas from month to the LEFT
        if prev_month_idx is None:
            raise RuntimeError("Не удалось определить предыдущий месяц для копирования формул.")
        source_month_idx = prev_month_idx

    source_col_letter = col_index_to_letter(source_month_idx)
    range_str = f"{source_col_letter}2:{source_col_letter}239"
    formulas_raw = google_call(f"{ws_mom.title} get source formulas", ws_mom.get, range_str, value_render_option="FORMULA")
    formulas = [row[0] if row else "" for row in formulas_raw]

    import re
    # Диапазон колонок для нового месяца берём из параметров функции (из листа "Свод")
    # new_start/new_end уже буквы колонок, которые соответствуют прошлому месяцу 1-е..последнее
    start_col, end_col = new_start, new_end

    # шаблон для замены любого диапазона БУКВЫ:БУКВЫ (цифры строки не трогаем)
    range_pattern = re.compile(r"('Свод'!)([A-Z]+)(\d+):([A-Z]+)(\d+)")
    # шаблон для одиночных ссылок =H30/H15 и т.п.
    prev_col = col_index_to_letter(source_month_idx)
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
            f"{ws_mom.title} update monthly formulas",
            ws_mom.update,
            values=updated_formulas,
            range_name=range_new,
            value_input_option="USER_ENTERED",
        )

    # Для 6 метрик (уники + конверсии) берём месячные значения напрямую из Ozon Analytics API
    override_monthly_api_rows(ws_mom, new_col_letter, first_day, last_day)


# ============================================================
#  MAIN
# ============================================================

def generate_monthly_formulas():
    ss = get_sheet()

    try:
        ws_svod = google_call("open worksheet Свод", ss.worksheet, "Свод")
    except:
        raise RuntimeError("Не найден лист 'Свод'!")

    try:
        ws_mom = google_call("open worksheet MOM", ss.worksheet, "MOM")
    except:
        raise RuntimeError("Не найден лист 'MOM'!")

    # Get last full calendar month dates
    first_day, last_day = get_last_full_month()

    # Find column range in Svod for the last month
    start_col_letter, end_col_letter = find_month_columns_in_svod(ws_svod, first_day, last_day)

    # Copy last MOM column and update formulas
    write_formulas_to_mom(ss, ws_mom, start_col_letter, end_col_letter, first_day, last_day)

    print(f"Основной MOM: месяц {first_day} — {last_day} записан.")


if __name__ == "__main__":
    generate_monthly_formulas()
