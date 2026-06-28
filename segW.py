import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

def normalize_cell(val):
    """
    Приводит значение из Google Sheets к числу:
    - "12,34%" -> 0.1234
    - "77128800,00%" -> 771288.0 (если такое реально лежит как строка процентов)
    - "1 234,56" -> 1234.56
    - "" / None -> 0.0
    """
    if val in ("", None):
        return 0.0

    if isinstance(val, (int, float)):
        return float(val)

    s = str(val).strip()

    # проценты
    if s.endswith("%"):
        s = s.replace("%", "").replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            return float(s) / 100.0
        except ValueError:
            return 0.0

    # обычные числа
    s = s.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0

# ================================
# AUTH
# ================================
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = ServiceAccountCredentials.from_json_keyfile_name(
    "google-creds.json",
    scope
)
gc = gspread.authorize(creds)

SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

sh = gc.open(SHEET_NAME)
ws_day = sh.worksheet("Сегмент")
ws_week = sh.worksheet("СегментН")

# ================================
# ДАТЫ ПОСЛЕДНЕЙ ПОЛНОЙ НЕДЕЛИ
# ================================
def get_last_full_week():
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    last_monday = monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday

# ================================
# НАЙТИ КОЛОНКУ ПО ДАТЕ
# ================================
def find_col(ws, date_str):
    header = ws.row_values(1)
    for i, v in enumerate(header, start=1):
        if v.strip() == date_str:
            return i
    return None

# ================================
# ОСНОВНАЯ ЛОГИКА
# ================================
def write_week_from_daily_segments():
    week_start, week_end = get_last_full_week()
    dates = [(week_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    print(f"=== Считаем неделю {week_start} — {week_end} ===")

    # колонки дней
    day_cols = []
    for d in dates:
        col = find_col(ws_day, d)
        if col:
            day_cols.append(col)

    if len(day_cols) == 0:
        print("❌ Нет ни одной даты недели")
        return

    labels = ws_week.col_values(1)[1:]  # строки сегментов

    # ===== ПАКЕТНО ЧИТАЕМ ВСЕ НУЖНЫЕ ЯЧЕЙКИ ОДНИМ ЗАПРОСОМ =====
    max_row = len(labels) + 1
    first_col = min(day_cols)
    last_col = max(day_cols)

    data_range = f"{gspread.utils.rowcol_to_a1(2, first_col)}:{gspread.utils.rowcol_to_a1(max_row, last_col)}"
    raw_data = ws_day.get(data_range)

    # нормализуем в числа
    matrix = []
    for row in raw_data:
        matrix.append([normalize_cell(x) for x in row])

    result = []

    for i, label in enumerate(labels):
        label_l = label.lower()
        day_values = matrix[i]

        if "руб" in label_l or "шт" in label_l:
            result.append([sum(day_values)])

        elif "%" in label_l:
            # day_values уже в долях (0..1). Берём среднее только по непустым дням,
            # чтобы "0" в несуществующих колонках не занижал процент.
            nonzero = [v for v in day_values if v != 0]
            avg = (sum(nonzero) / len(nonzero)) if nonzero else 0
            result.append([avg])

        else:
            result.append([0])

    # ================================
    # ЗАПИСЬ В СегментН
    # ================================
    week_name = f"w{week_start.isocalendar().week} ({week_start.strftime('%d.%m')}–{week_end.strftime('%d.%m')})"

    header = ws_week.row_values(1)
    if week_name in header:
        col = header.index(week_name) + 1
        print(f"♻️ Обновляем {week_name}")
    else:
        col = len(header) + 1
        ws_week.update_cell(1, col, week_name)
        print(f"🆕 Добавлена колонка {week_name}")

    start = 2
    end = start + len(result) - 1
    rng = f"{gspread.utils.rowcol_to_a1(start, col)}:{gspread.utils.rowcol_to_a1(end, col)}"

    ws_week.update(values=result, range_name=rng, value_input_option="RAW")
    print("✅ Недельные сегменты записаны")

# ================================
# RUN
# ================================
if __name__ == "__main__":
    write_week_from_daily_segments()