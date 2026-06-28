#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finance_report/db.py — слой PostgreSQL для финансового дашборда.

Таблица public.ozon_finance — длинный формат (как ozon_metrics), но про
финансы: каждая строка = одно начисление за день в разрезе
scope/sku/категория/тип.

    metric_date  — день начислений
    scope        — 'overall' (свод по магазину) | 'sku' (по артикулу)
    sku_id       — Ozon SKU (NULL для overall)
    offer_id     — наш артикул (NULL для overall или если SKU не маппится)
    category     — укрупнённая категория (7 шт.): sales | sale_commission |
                   processing_and_delivery | services_amount |
                   refunds_and_cancellations | compensation_amount | others_amount
    accrual_type — имя type_id (Logistic, PayPerClick...) или 'sales'/'sale_commission'
    type_id      — id типа начисления (NULL для продаж/комиссии)
    value        — сумма, знак как в API (продажи +, расходы −)
    source       — 'ozon_finance'

Дашборд по умолчанию агрегирует по category (7 шт.); type_id/accrual_type
нужны только для drill-down «состав категории по типам».

Запись идемпотентна: replace_date() удаляет строки за дату и source, затем
вставляет заново (как replace_weekly_metrics в grafana_report.py).
"""

import os
import sys

import psycopg2
from dotenv import load_dotenv

# .env и модули лежат в корне проекта (на уровень выше finance_report/)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
load_dotenv(os.path.join(_ROOT, ".env"))

SOURCE = "ozon_finance"
TABLE = "public.ozon_finance"

DB_CONFIG = {
    "host": os.getenv("PG_HOST"),
    "port": int(os.getenv("PG_PORT", 5432)),
    "dbname": os.getenv("PG_DB"),
    "user": os.getenv("PG_USER"),
    "password": os.getenv("PG_PASSWORD"),
}

# Колонки строки в порядке вставки.
COLUMNS = (
    "metric_date", "scope", "sku_id", "offer_id",
    "category", "accrual_type", "type_id", "value", "source",
)


def get_conn():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    return conn


def ensure_schema(conn):
    """Создаёт таблицу и индексы, если их нет."""
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id           bigserial PRIMARY KEY,
                metric_date  date     NOT NULL,
                scope        text     NOT NULL,
                sku_id       text,
                offer_id     text,
                category     text     NOT NULL,
                accrual_type text,
                type_id      integer,
                value        numeric  NOT NULL,
                source       text     NOT NULL DEFAULT '{SOURCE}'
            );
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS ozon_finance_date_idx
                ON {TABLE} (metric_date);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS ozon_finance_scope_cat_idx
                ON {TABLE} (scope, category, metric_date);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS ozon_finance_offer_idx
                ON {TABLE} (offer_id, metric_date);
        """)
    print(f"[db] schema ready: {TABLE}")


def replace_date(conn, metric_date, rows, source=SOURCE):
    """
    Идемпотентно перезаписывает данные за дату: DELETE по (date, source),
    затем INSERT всех строк в одной транзакции.

    rows — список кортежей в порядке COLUMNS, но БЕЗ source
    (source проставляется здесь): (metric_date, scope, sku_id, offer_id,
    category, accrual_type, type_id, value).
    """
    payload = [tuple(r) + (source,) for r in rows]

    old_autocommit = conn.autocommit
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {TABLE} WHERE metric_date = %s AND source = %s",
                (metric_date, source),
            )
            deleted = cur.rowcount
            if payload:
                cur.executemany(
                    f"""
                    INSERT INTO {TABLE}
                        ({", ".join(COLUMNS)})
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    payload,
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = old_autocommit

    print(f"[db] {metric_date}: deleted={deleted} inserted={len(payload)}")
    return len(payload)
