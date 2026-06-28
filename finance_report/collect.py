#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finance_report/collect.py — сбор финансовых начислений за день в строки для PG.

Источник — finance_accrual (Ozon finance/accrual/by-day). За один проход по
дню собираем два среза:
  • overall — свод по всему магазину (scope='overall', sku=NULL);
  • sku     — по каждому артикулу (scope='sku').

Каждое начисление кладётся ОДНОЙ строкой на своём естественном уровне и
всегда помечается категорией:
  • продажи      → category='sales',           type_id=NULL (seller_price);
  • комиссия     → category='sale_commission',  type_id=NULL;
  • доставка     → delivery.services по type_id → категория из CATEGORY_MAP;
  • услуги/прочее→ item_fees / non_item_fee по type_id → категория из CATEGORY_MAP.
Категория = SUM(value) по строкам (без отдельных rollup-строк, без двойного
счёта). Знаки сырые: продажи +, расходы −.

ВАЖНО: начисления уровня продавца без привязки к товару (non_item_fee:
эквайринг, реклама-нонитем, подписки) есть только в overall. Поэтому
overall ≠ сумма по sku — так и задумано (см. README).

collect_date(date) -> list[tuple] в порядке db.COLUMNS без source.
"""

import os
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import finance_accrual as fa
from constants import offer_to_sku

# sku (int и str) -> наш offer_id
_SKU_TO_OFFER = {}
for _offer, _sku in offer_to_sku.items():
    try:
        _si = int(_sku)
        _SKU_TO_OFFER[_si] = str(_offer)
        _SKU_TO_OFFER[str(_si)] = str(_offer)
    except Exception:
        _SKU_TO_OFFER[str(_sku)] = str(_offer)


def _offer_for(sku):
    return _SKU_TO_OFFER.get(sku) or _SKU_TO_OFFER.get(str(sku))


def collect_date(date: str) -> list:
    """Собирает все строки финансовых начислений за дату (YYYY-MM-DD)."""
    # Русские названия типов (поле description из /types) — для менеджеров МП.
    type_names = fa.accrual_type_descriptions()

    # ключ -> сумма
    overall = defaultdict(float)   # (category, type_id, accrual_type)
    persku = defaultdict(float)    # (sku, category, type_id, accrual_type)

    def add(category, type_id, accrual_type, value, sku=None):
        if not value:
            return
        overall[(category, type_id, accrual_type)] += value
        if sku is not None:
            persku[(sku, category, type_id, accrual_type)] += value

    for acc in fa.iter_accruals(date):
        posting = acc.get("posting")
        if posting:
            for prod in posting.get("products", []) or []:
                sku = prod.get("sku")
                comm = prod.get("commission") or {}
                # продажи и комиссия — прямые метрики (без type_id), подписи на русском
                add("sales", None, "Продажа товара", fa.parse_money(comm.get("seller_price")), sku)
                add("sale_commission", None, "Комиссия за продажу",
                    fa.parse_money(comm.get("sale_commission")), sku)
                # доставка — по типам услуг
                for srv in (prod.get("delivery") or {}).get("services", []) or []:
                    tid = srv.get("type_id")
                    add(fa.category_for(tid), tid, type_names.get(tid, str(tid)),
                        fa.parse_money(srv.get("accrued")), sku)

        # услуги по товару (item_fees) — с привязкой к sku
        for grp in (acc.get("item_fees") or {}).get("fees", []) or []:
            sku = grp.get("sku")
            for fee in grp.get("fees", []) or []:
                tid = fee.get("type_id")
                add(fa.category_for(tid), tid, type_names.get(tid, str(tid)),
                    fa.parse_money(fee.get("accrued")), sku)

        # начисления уровня продавца без sku — только overall
        nif = acc.get("non_item_fee")
        if nif:
            tid = nif.get("type_id")
            add(fa.category_for(tid), tid, type_names.get(tid, str(tid)),
                fa.parse_money(nif.get("accrued")), sku=None)

    rows = []
    # overall: (metric_date, scope, sku_id, offer_id, category, accrual_type, type_id, value)
    for (category, type_id, accrual_type), value in overall.items():
        rows.append((date, "overall", None, None, category, accrual_type, type_id, round(value, 2)))
    # sku
    for (sku, category, type_id, accrual_type), value in persku.items():
        rows.append((date, "sku", str(sku), _offer_for(sku),
                     category, accrual_type, type_id, round(value, 2)))

    return rows


if __name__ == "__main__":
    # Быстрая проверка без записи в БД: python collect.py [YYYY-MM-DD]
    from datetime import date as _d, timedelta
    d = sys.argv[1] if len(sys.argv) > 1 else (_d.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = collect_date(d)
    overall = [r for r in rows if r[1] == "overall"]
    sku = [r for r in rows if r[1] == "sku"]
    print(f"Дата {d}: строк всего={len(rows)} (overall={len(overall)}, sku={len(sku)})")
    cat = defaultdict(float)
    for r in overall:
        cat[r[4]] += r[7]
    print("Overall по категориям:")
    for c, v in sorted(cat.items(), key=lambda x: x[1]):
        print(f"  {c:>26}: {round(v, 2)}")
    print(f"Net overall (Σ value): {round(sum(cat.values()), 2)}")
