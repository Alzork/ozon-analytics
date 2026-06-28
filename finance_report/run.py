#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finance_report/run.py — наполнение таблицы public.ozon_finance.

Собирает финансовые начисления Ozon (через finance_accrual) и идемпотентно
пишет в PostgreSQL для дашборда Grafana.

Запуск:
    python finance_report/run.py                 # за вчера (по REPORT_TZ)
    python finance_report/run.py 2026-06-17       # за конкретную дату
    python finance_report/run.py --backfill 90    # последние 90 дней (вкл. вчера)

Cron (за вчера, ежедневно) — по образцу grafana_report.py.
"""

import os
import sys
import time
import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import db
import collect

REPORT_TZ = ZoneInfo(os.getenv("REPORT_TZ", "Asia/Novosibirsk"))


def yesterday() -> str:
    return (datetime.now(REPORT_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


def process_date(conn, date: str):
    rows = collect.collect_date(date)
    db.replace_date(conn, date, rows)
    overall = sum(1 for r in rows if r[1] == "overall")
    sku = sum(1 for r in rows if r[1] == "sku")
    print(f"[run] {date}: overall={overall} sku={sku}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD (по умолчанию вчера)")
    ap.add_argument("--backfill", type=int, default=0,
                    help="Загрузить последние N дней (включая вчера)")
    args = ap.parse_args()

    conn = db.get_conn()
    db.ensure_schema(conn)

    if args.backfill > 0:
        end = datetime.now(REPORT_TZ).date() - timedelta(days=1)
        dates = [(end - timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(args.backfill)]
        dates.sort()  # от старых к новым
        print(f"[run] backfill {len(dates)} дней: {dates[0]} .. {dates[-1]}")
        for i, d in enumerate(dates, 1):
            try:
                process_date(conn, d)
            except Exception as e:
                print(f"[run] ❌ {d}: {e}")
            time.sleep(0.3)
    else:
        d = args.date or yesterday()
        process_date(conn, d)

    conn.close()
    print("[run] done")


if __name__ == "__main__":
    main()
