#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finance_report/offer_dim.py — справочник артикулов для финансового дашборда.

Наполняет таблицу public.ozon_offer_dim связкой:
    offer_id -> category (тип товара) / brand / weight
из Ozon (v4/product/info/attributes). Дашборд джойнит ozon_finance с этой
таблицей, чтобы давать разрезы по Категории / Бренду / Весу.
Структуру ozon_finance НЕ трогаем — это отдельная dim-таблица.

Категория = тип товара из Ozon type_id (монолитный источник, не зависит от
ручных Google-таблиц). Маппинг type_id -> название — в TYPE_CATEGORY ниже;
новый незнакомый type_id попадает как "Тип <id>" + предупреждение.

Бренд = атрибут Ozon id=85 (сырой; отдел МП приводит названия к одному
стилю — после этого просто перезапустить скрипт).

Запуск:  python finance_report/offer_dim.py        (полное обновление справочника)
"""

import os
import re
import sys

import requests
from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
load_dotenv(os.path.join(_ROOT, ".env"))

import db
from constants import offer_to_sku

HEADERS = {
    "Client-Id": os.getenv("OZON_CLIENT_ID"),
    "Api-Key": os.getenv("OZON_API_KEY"),
    "Content-Type": "application/json",
}
ATTR_URL = "https://api-seller.ozon.ru/v4/product/info/attributes"
BRAND_ATTR_ID = 85
TABLE = "public.ozon_offer_dim"

# Ozon type_id -> человекочитаемая категория (тип товара). Заполнено по
# фактическим товарам магазина (probe 2026-06-19). Новый type_id -> "Тип <id>".
TYPE_CATEGORY = {
    92713: "Порошок стиральный автомат",
    92695: "Гель для стирки",
    92697: "Кондиционер-ополаскиватель",
    97769: "Жидкое мыло для рук",
    92704: "Средство для мытья посуды",
    92701: "Средство для мытья пола",
    92700: "Пятновыводитель/отбеливатель",
    97133: "Лимонная кислота бытовая",
    971083507: "Сода каустическая",
}


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_products():
    sku_list = [str(v) for v in offer_to_sku.values() if v]
    body = {"filter": {"sku": sku_list, "visibility": "ALL"},
            "limit": len(sku_list), "sort_dir": "ASC"}
    r = requests.post(ATTR_URL, headers=HEADERS, json=body, timeout=120)
    r.raise_for_status()
    return r.json().get("result", []) or []


# Бренд и вес берём ИЗ НАИМЕНОВАНИЯ (там номинальные и чище, чем отдельные поля).
# Известные бренды (токен в имени -> нормализованное имя), длинные первыми.
_BRANDS = [
    ("Brand-D", "Brand-D"), ("Brand-E", "Brand-E"), ("Brand-B", "Brand-B"),
    ("Spectr", "Spectr"), ("Спектр", "Spectr"), ("Safir", "Safir"),
    ("Brand-A", "Brand-A"), ("Brand-A", "Brand-A"), ("Brand-G", "Brand-G"), ("Brand-G", "Brand-G"),
    ("LOTOC", "LOTOC"), ("Neobi", "Neobi"), ("Бирюса", "Бирюса"), ("BIRYUSA", "Бирюса"),
    ("Brand-C", "Brand-C"), ("Brand-C", "Brand-C"), ("Brand-F", "Brand-F"), ("Brand-X", "Brand-X"),
    ("Brand-X", "Brand-X"), ("Brand-H", "Brand-H"),
    ("Brand-D", "Brand-D"), ("Brand-D", "Brand-D"),
]
_BRAND_NORM = {"Brand-A": "Brand-A", "Brand-D": "Brand-D", "Brand-D": "Brand-D",
               "Brand-X": "Brand-X", "Brand-C": "Brand-C", "Нет бренда": "", "": ""}
_UNIT_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*(килограмм|кг|грамм|гр|мл|литр\w*|л|г)\b', re.I)


def _attr_brand(item):
    for a in item.get("attributes", []) or []:
        if a.get("id") == BRAND_ATTR_ID:
            vals = a.get("values") or []
            if vals:
                return (vals[0].get("value") or "").strip()
    return ""


def _brand(item):
    """Бренд: из имени -> из атрибута 85 -> 'Brand-X' (по умолчанию)."""
    name = (item.get("name") or "").lower()
    for token, norm in _BRANDS:
        if token.lower() in name:
            return norm
    attr = _attr_brand(item)
    return _BRAND_NORM.get(attr, attr) or "Brand-X"


def _weight_label(item):
    """Номинальный вес из имени, всё в кг (литры 1:1, граммы/мл -> /1000). '6 кг'."""
    best = None
    for m in _UNIT_RE.finditer(item.get("name") or ""):
        val = float(m.group(1).replace(",", "."))
        u = m.group(2).lower()
        if u in ("кг", "килограмм") or u.startswith("литр") or u == "л":
            kg = val
        elif u.startswith("грамм") or u in ("гр", "г") or u == "мл":
            kg = val / 1000
        else:
            continue
        if best is None or kg > best:
            best = kg
    if best is None:
        return None
    return f"{('%.1f' % best).rstrip('0').rstrip('.')} кг"


def build_rows(items):
    product_to_offer = {str(v): k for k, v in offer_to_sku.items()}
    rows = []
    unknown = set()
    for it in items:
        sku = it.get("sku")
        offer_id = product_to_offer.get(str(sku)) or it.get("offer_id")
        if not offer_id:
            continue
        type_id = it.get("type_id")
        category = TYPE_CATEGORY.get(type_id)
        if category is None:
            category = f"Тип {type_id}"
            unknown.add(type_id)
        rows.append((
            str(offer_id), str(sku) if sku else None, type_id,
            category, _brand(it), _safe_float(it.get("weight")), _weight_label(it),
        ))
    if unknown:
        print(f"[offer_dim] ⚠️ незнакомые type_id (попали как 'Тип <id>'): "
              f"{sorted(unknown)} — добавьте в TYPE_CATEGORY")
    return rows


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                offer_id   text PRIMARY KEY,
                sku        text,
                type_id    integer,
                category   text,
                brand        text,
                weight       numeric,
                weight_label text,
                updated_at   timestamptz NOT NULL DEFAULT now()
            );
        """)
        # на случай уже существующей таблицы без новой колонки
        cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS weight_label text;")
        cur.execute(f"CREATE INDEX IF NOT EXISTS ozon_offer_dim_cat_idx ON {TABLE} (category);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS ozon_offer_dim_brand_idx ON {TABLE} (brand);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS ozon_offer_dim_weight_idx ON {TABLE} (weight_label);")
    print(f"[offer_dim] schema ready: {TABLE}")


def upsert(conn, rows):
    with conn.cursor() as cur:
        cur.executemany(f"""
            INSERT INTO {TABLE} (offer_id, sku, type_id, category, brand, weight, weight_label, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (offer_id) DO UPDATE SET
                sku=EXCLUDED.sku, type_id=EXCLUDED.type_id, category=EXCLUDED.category,
                brand=EXCLUDED.brand, weight=EXCLUDED.weight, weight_label=EXCLUDED.weight_label,
                updated_at=now();
        """, rows)
    conn.commit()
    print(f"[offer_dim] upserted {len(rows)} строк")


def main():
    items = fetch_products()
    rows = build_rows(items)
    conn = db.get_conn()
    conn.autocommit = False
    try:
        ensure_schema(conn)
        upsert(conn, rows)
    finally:
        conn.close()
    # сводка
    from collections import Counter
    cat = Counter(r[3] for r in rows)
    print("[offer_dim] по категориям:")
    for c, n in cat.most_common():
        print(f"    {c}: {n}")


if __name__ == "__main__":
    main()
