import os
import re
import time
from datetime import datetime, timedelta, timezone, date

import gspread
import requests
from dotenv import load_dotenv

import finance_accrual

load_dotenv()

MSK = timezone(timedelta(hours=3))

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
GOOGLE_CREDS = os.getenv("GOOGLE_CREDS", "google-creds.json")
SPREADSHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
WORKSHEET_NAME = os.getenv("WEEKFINDYN_WORKSHEET", "Нед Динамика")
BACKFILL_WEEKS = int(os.getenv("WEEKFINDYN_BACKFILL_WEEKS", "1"))

if not OZON_CLIENT_ID or not OZON_API_KEY:
    raise RuntimeError("Missing OZON_CLIENT_ID / OZON_API_KEY in env")
if not SPREADSHEET_NAME:
    raise RuntimeError("Missing WEEKFINDYN_SHEET_NAME or GOOGLE_SHEET_NAME in env")

HEADERS = {
    "Client-Id": OZON_CLIENT_ID,
    "Api-Key": OZON_API_KEY,
    "Content-Type": "application/json",
}


def post_with_retry(url, headers, body, timeout=60):
    attempt = 0
    while True:
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
            if r.status_code >= 500:
                raise requests.HTTPError(f"Server error {r.status_code}", response=r)
            if r.status_code == 429:
                raise requests.HTTPError("429 Too Many Requests", response=r)
            r.raise_for_status()
            return r
        except Exception as e:
            attempt += 1
            wait = min(30 * attempt, 180)
            print(f"RETRY {attempt}: {url} -> {e}. Sleep {wait}s")
            time.sleep(wait)


def connect_sheet():
    gc = gspread.service_account(filename=GOOGLE_CREDS)
    sh = gc.open(SPREADSHEET_NAME)
    # 1) exact match
    try:
        return sh.worksheet(WORKSHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        pass

    # 2) case-insensitive / trim fallback
    target = WORKSHEET_NAME.strip().lower()
    for ws in sh.worksheets():
        if ws.title.strip().lower() == target:
            return ws

    available = ", ".join([ws.title for ws in sh.worksheets()])
    raise RuntimeError(
        f"Worksheet '{WORKSHEET_NAME}' not found in '{SPREADSHEET_NAME}'. "
        f"Available sheets: {available}"
    )


def col_to_letter(col: int) -> str:
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


def week_start_for_back(weeks_back: int) -> date:
    today_msk = datetime.now(MSK).date()
    this_monday = today_msk - timedelta(days=today_msk.weekday())
    return this_monday - timedelta(days=7 * weeks_back)


def week_window_utc(week_start_msk: date) -> tuple[str, str, date]:
    # week_start_msk = Monday (MSK), week_end_msk = Sunday
    week_end_msk = week_start_msk + timedelta(days=6)

    start_msk = datetime.combine(week_start_msk, datetime.min.time(), tzinfo=MSK)
    end_msk = datetime.combine(week_end_msk, datetime.max.time().replace(microsecond=0), tzinfo=MSK)

    start_utc = start_msk.astimezone(timezone.utc)
    end_utc = end_msk.astimezone(timezone.utc)

    return (
        start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        week_end_msk,
    )


def week_label(week_start_msk: date, week_end_msk: date) -> str:
    iso_week = week_start_msk.isocalendar().week
    return f"w{iso_week:02d} ({week_start_msk.strftime('%d.%m')}-{week_end_msk.strftime('%d.%m')})"


def fetch_week_totals(week_start: date, week_end: date) -> dict:
    # МИГРИРОВАНО с устаревающего /v3/finance/transaction/totals на
    # finance/accrual: свод начислений по дням недели (totals_period).
    # Отдаёт те же ключи и знаки, что старый totals.
    return finance_accrual.totals_period(
        week_start.strftime("%Y-%m-%d"),
        week_end.strftime("%Y-%m-%d"),
    )


def to_float(v) -> float:
    try:
        if isinstance(v, bool):
            return 0.0
        return float(v)
    except Exception:
        return 0.0


def calc_expenses_from_totals(result: dict) -> float:
    parts = {
        "sale_commission": to_float(result.get("sale_commission", 0)),
        "processing_and_delivery": to_float(result.get("processing_and_delivery", 0)),
        "refunds_and_cancellations": to_float(result.get("refunds_and_cancellations", 0)),
        "services_amount": to_float(result.get("services_amount", 0)),
        "compensation_amount": to_float(result.get("compensation_amount", 0)),
        "money_transfer": to_float(result.get("money_transfer", 0)),
        "others_amount": to_float(result.get("others_amount", 0)),
    }
    # как в operational totals(all)
    exp = 0.0
    for k in (
        "sale_commission",
        "processing_and_delivery",
        "services_amount",
        "compensation_amount",
        "money_transfer",
        "others_amount",
    ):
        if parts[k] < 0:
            exp += abs(parts[k])
    exp += abs(parts["refunds_and_cancellations"])
    return exp


_WEEK_LABEL_RE = re.compile(r"^w(\d{1,2})\s*\((\d{2})\.(\d{2})-(\d{2})\.(\d{2})\)\s*$")


def _parse_week_start_from_label(label: str, target_start: date) -> date | None:
    """
    Parse label like 'w09 (23.02-01.03)' into start date.
    Year inferred around target_start to handle year boundaries.
    """
    m = _WEEK_LABEL_RE.match((label or "").strip())
    if not m:
        return None

    day = int(m.group(2))
    month = int(m.group(3))
    year = target_start.year
    try:
        d = date(year, month, day)
    except Exception:
        return None

    # adjust around year boundary
    if d > target_start + timedelta(days=180):
        d = date(year - 1, month, day)
    elif d < target_start - timedelta(days=180):
        d = date(year + 1, month, day)
    return d


def ensure_col_for_week(ws, label: str, week_start: date) -> int:
    """
    If week exists -> return its column (overwrite mode).
    Else insert by chronological order among existing week columns.
    """
    header = ws.row_values(1)
    if len(header) < 2:
        ws.update_cell(1, 2, label)
        return 2

    # exact label exists -> overwrite
    for i in range(2, len(header) + 1):
        if str(header[i - 1]).strip() == label:
            return i

    # collect parsable week columns (col_idx, start_date)
    week_cols = []
    for i in range(2, len(header) + 1):
        cell = str(header[i - 1]).strip()
        d = _parse_week_start_from_label(cell, week_start)
        if d is not None:
            week_cols.append((i, d))

    # try insertion point among existing week columns
    insert_col = None
    for col_idx, d in week_cols:
        if d > week_start:
            insert_col = col_idx
            break

    if insert_col is None:
        # append to the right
        insert_col = max(2, len(header) + 1)
        ws.update_cell(1, insert_col, label)
        return insert_col

    # insert between older/newer columns
    ws.insert_cols([[]], col=insert_col)
    ws.update_cell(1, insert_col, label)
    return insert_col


def write_week_values(ws, col: int, accrued: float, sales: float, percent: float):
    letter = col_to_letter(col)
    ws.batch_update([
        {
            "range": f"A2:A4",
            "values": [["Начислено"], ["Продажи"], ["%"]],
        },
        {
            "range": f"{letter}2:{letter}4",
            "values": [
                [round(accrued, 2)],
                [round(sales, 2)],
                [round(percent, 2)],
            ],
        },
    ])


def process_week(ws, week_start: date):
    week_end = week_start + timedelta(days=6)
    label = week_label(week_start, week_end)

    print(f"\n=== PROCESS WEEK {label} ===")
    print(f"[DEBUG week window] {week_start} .. {week_end} (MSK, посуточно)")

    totals = fetch_week_totals(week_start, week_end)
    sales = to_float(totals.get("accruals_for_sale", 0))
    expenses = calc_expenses_from_totals(totals)
    accrued = sales - expenses
    percent = (accrued / sales * 100.0) if sales > 0 else 0.0

    print(f"[DEBUG totals all] {totals}")
    print(f"[DEBUG week finance] sales={sales:.2f} expenses={expenses:.2f} accrued={accrued:.2f} percent={percent:.2f}%")

    col = ensure_col_for_week(ws, label, week_start)
    write_week_values(ws, col, accrued, sales, percent)
    print(f"✅ Updated {WORKSHEET_NAME}: column {col} ({label})")


def main():
    ws = connect_sheet()

    # oldest -> newest for stable update order
    for back in range(BACKFILL_WEEKS, 0, -1):
        start = week_start_for_back(back)
        process_week(ws, start)


if __name__ == "__main__":
    main()
