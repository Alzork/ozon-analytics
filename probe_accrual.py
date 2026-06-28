#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_accrual.py — разведка новых finance/accrual методов Ozon.

ТОЛЬКО ЧИТАЕТ И ПЕЧАТАЕТ. Ничего не пишет в Sheets/PG/файлы.

Цель — до отключения старых методов (3-6 июля 2026) снять:
  1. справочник /v1/finance/accrual/types         (type_id -> название)
  2. /v1/finance/accrual/by-day за дату            (структура + суммы по type_id)
  3. /v1/finance/accrual/postings по 1-2 отправл.  (структура)
и сразу сверить с устаревающими /v3/finance/transaction/{totals,list}
за ту же дату, чтобы понять, какое новое поле = старому accruals_for_sale.

Запуск:
    python probe_accrual.py            # за вчера
    python probe_accrual.py 2026-06-15 # за конкретную дату
"""

import os
import sys
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
BASE = "https://api-seller.ozon.ru"

if not OZON_CLIENT_ID or not OZON_API_KEY:
    sys.exit("❌ OZON_CLIENT_ID / OZON_API_KEY не заданы в .env")

HEADERS = {
    "Client-Id": OZON_CLIENT_ID,
    "Api-Key": OZON_API_KEY,
    "Content-Type": "application/json",
}


def post(path, body, timeout=60, retries=3):
    """POST с простым ретраем. Возвращает распарсенный JSON или кидает."""
    url = f"{BASE}{path}"
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, headers=HEADERS, json=body, timeout=timeout)
            if r.status_code >= 400:
                # печатаем тело — у Ozon там осмысленная ошибка
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
            return r.json()
        except Exception as e:
            last = e
            print(f"  [retry {attempt}/{retries}] {path} -> {e}")
            if attempt < retries:
                time.sleep(2)
    raise last


def money(obj):
    """{'amount': '123.45', 'currency': 'RUB'} -> float. None/пусто -> 0.0"""
    if not isinstance(obj, dict):
        return 0.0
    try:
        return float(obj.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def hr(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


# --------------------------------------------------------------------------
# 1. Справочник типов начислений
# --------------------------------------------------------------------------
def probe_types():
    hr("1) /v1/finance/accrual/types — справочник типов начислений")
    data = post("/v1/finance/accrual/types", {})
    types = data.get("accrual_types", []) or []
    id2name = {}
    print(f"Всего типов: {len(types)}\n")
    for t in sorted(types, key=lambda x: x.get("id", 0)):
        tid = t.get("id")
        name = t.get("name", "")
        desc = t.get("description", "")
        id2name[tid] = name
        print(f"  type_id={tid:>4}  {name}")
        if desc and desc != name:
            print(f"              ↳ {desc}")
    return id2name


# --------------------------------------------------------------------------
# 2. Начисления за день (главный кандидат на замену totals + list)
# --------------------------------------------------------------------------
def probe_by_day(date, id2name, max_pages=20):
    hr(f"2) /v1/finance/accrual/by-day — начисления за {date}")

    type_sums = defaultdict(float)        # type_id -> сумма accrued
    cat_counts = defaultdict(int)         # accrued_category -> кол-во записей
    total_amount_sum = 0.0
    # кандидаты на старый accruals_for_sale, агрегаты по posting.products[].commission
    comm_fields = defaultdict(float)      # field -> сумма
    sales_by_sku = defaultdict(float)     # sku -> sale_amount (один из кандидатов)
    sample_printed = False

    last_id = ""
    pages = 0
    total_records = 0
    while True:
        pages += 1
        body = {"date": date, "last_id": last_id}
        data = post("/v1/finance/accrual/by-day", body)
        accruals = data.get("accruals", []) or []
        total_records += len(accruals)

        for acc in accruals:
            cat = acc.get("accrued_category", "UNSPECIFIED")
            cat_counts[cat] += 1
            total_amount_sum += money(acc.get("total_amount"))

            # item_fees: начисления по товарам
            for fee_group in (acc.get("item_fees") or {}).get("fees", []) or []:
                for fee in fee_group.get("fees", []) or []:
                    type_sums[fee.get("type_id")] += money(fee.get("accrued"))

            # non_item_fee: по продавцу без привязки к товару
            nif = acc.get("non_item_fee")
            if nif:
                type_sums[nif.get("type_id")] += money(nif.get("accrued"))

            # posting: начисления по отправлению (товары + доставка)
            posting = acc.get("posting")
            if posting:
                for prod in posting.get("products", []) or []:
                    sku = prod.get("sku")
                    comm = prod.get("commission") or {}
                    for f in ("sale_amount", "seller_price", "sale_price",
                              "sale_commission", "commission", "bonus", "coinvestment"):
                        comm_fields[f] += money(comm.get(f))
                    sales_by_sku[sku] += money(comm.get("sale_amount"))
                    delivery = prod.get("delivery") or {}
                    type_sums["delivery.total_accrued"] += money(delivery.get("total_accrued"))
                    for srv in delivery.get("services", []) or []:
                        type_sums[srv.get("type_id")] += money(srv.get("accrued"))

            # печатаем один полный сырой пример каждой категории — для глаз
            if not sample_printed and posting and posting.get("products"):
                print("\n--- сырой пример записи (accrued_category=%s) ---" % cat)
                print(json.dumps(acc, ensure_ascii=False, indent=2)[:2000])
                print("--- конец примера ---\n")
                sample_printed = True

        last_id = data.get("last_id") or ""
        if not last_id or not accruals or pages >= max_pages:
            break
        time.sleep(0.3)

    print(f"Страниц: {pages}, записей: {total_records}")
    print(f"Категории (accrued_category): {dict(cat_counts)}")
    print(f"Σ total_amount по всем записям: {round(total_amount_sum, 2)}")

    print("\nΣ accrued по type_id (расход = минус):")
    for tid, s in sorted(type_sums.items(), key=lambda x: x[1]):
        name = id2name.get(tid, "") if not isinstance(tid, str) else "(доставка)"
        print(f"  type_id={str(tid):>20}  {round(s, 2):>14}  {name}")

    print("\nΣ по полям posting.products[].commission (кандидаты на 'продажи'):")
    for f, s in comm_fields.items():
        print(f"  {f:>16}: {round(s, 2)}")

    print(f"\nSKU с продажами (по sale_amount): {len(sales_by_sku)}")
    return {
        "sale_amount_total": comm_fields.get("sale_amount", 0.0),
        "seller_price_total": comm_fields.get("seller_price", 0.0),
        "sale_price_total": comm_fields.get("sale_price", 0.0),
        "total_amount_sum": total_amount_sum,
        "sales_by_sku": sales_by_sku,
    }


# --------------------------------------------------------------------------
# 3. Начисления по конкретным отправлениям
# --------------------------------------------------------------------------
def get_posting_numbers(date, limit=2):
    """Берём пару номеров FBO-отправлений за дату для теста postings."""
    body = {
        "dir": "ASC",
        "filter": {
            "since": f"{date}T00:00:00.000Z",
            "to": f"{date}T23:59:59.999Z",
        },
        "limit": limit,
        "offset": 0,
        "with": {"financial_data": False, "analytics_data": False},
    }
    try:
        data = post("/v2/posting/fbo/list", body)
        result = data.get("result", []) or []
        return [p.get("posting_number") for p in result if p.get("posting_number")]
    except Exception as e:
        print(f"  не удалось получить номера отправлений: {e}")
        return []


def probe_postings(date):
    hr("3) /v1/finance/accrual/postings — начисления по отправлениям")
    numbers = get_posting_numbers(date, limit=2)
    if not numbers:
        print("Нет номеров отправлений за дату — пропускаю.")
        return
    print(f"Тестовые отправления: {numbers}")
    data = post("/v1/finance/accrual/postings", {"posting_numbers": numbers})
    pa = data.get("posting_accruals", []) or []
    print(f"Получено отправлений: {len(pa)}\n")
    for p in pa:
        print(f"  posting_number={p.get('posting_number')}")
        for a in p.get("accruals", []) or []:
            print(f"    type_id={a.get('type_id')} sku={a.get('sku')} "
                  f"qty={a.get('quantity')} accrued={money(a.get('accrued'))} "
                  f"seller_price={money(a.get('seller_price'))} date={a.get('accrual_date')}")
    print("\n--- сырой ответ postings (первые 2000 симв) ---")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])


# --------------------------------------------------------------------------
# 4. Сверка со старыми (ещё живыми) методами за ту же дату
# --------------------------------------------------------------------------
def probe_old_totals(date):
    hr(f"4) СВЕРКА: старый /v3/finance/transaction/totals за {date}")
    body = {
        "date": {"from": f"{date}T00:00:00.000Z", "to": f"{date}T23:59:59.000Z"},
        "posting_number": "",
        "transaction_type": "all",
    }
    try:
        data = post("/v3/finance/transaction/totals", body)
        result = data.get("result", {}) or {}
        print("Старые именованные агрегаты:")
        for k, v in result.items():
            print(f"  {k:>28}: {v}")
        return result
    except Exception as e:
        print(f"Старый метод недоступен (возможно уже отключён): {e}")
        return {}


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else \
        (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"Дата разведки: {date}")

    id2name = {}
    try:
        id2name = probe_types()
    except Exception as e:
        print(f"types упал: {e}")

    new = {}
    try:
        new = probe_by_day(date, id2name)
    except Exception as e:
        print(f"by-day упал: {e}")

    try:
        probe_postings(date)
    except Exception as e:
        print(f"postings упал: {e}")

    old = probe_old_totals(date)

    # Итоговая сверка «продаж»
    hr("ИТОГ: какое новое поле ≈ старому accruals_for_sale")
    old_sale = old.get("accruals_for_sale")
    print(f"  Старое accruals_for_sale (totals): {old_sale}")
    if new:
        print(f"  Новое Σ sale_amount  (by-day):     {round(new.get('sale_amount_total', 0), 2)}")
        print(f"  Новое Σ seller_price (by-day):     {round(new.get('seller_price_total', 0), 2)}")
        print(f"  Новое Σ sale_price   (by-day):     {round(new.get('sale_price_total', 0), 2)}")
        print(f"  Новое Σ total_amount (by-day):     {round(new.get('total_amount_sum', 0), 2)}")
    print("\nСравни числа выше → выбери поле, дающее старое accruals_for_sale.")


if __name__ == "__main__":
    main()
