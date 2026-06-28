#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finance_accrual.py — единый слой доступа к новым finance/accrual методам Ozon.

================================ ЗАЧЕМ ===================================
Методы /v3/finance/transaction/list и /v3/finance/transaction/totals
устаревают и отключаются 3-6 июля 2026. Ozon переводит на:
    /v1/finance/accrual/types     — справочник типов начислений
    /v1/finance/accrual/by-day    — все начисления за ОДИН день
    /v1/finance/accrual/postings  — начисления по конкретным отправлениям

Старые методы отдавали готовые именованные агрегаты (accruals_for_sale,
sale_commission, processing_and_delivery, ...). Новые отдают «сырые»
начисления с числовым type_id, которые надо самому свести в категории.

Этот модуль прячет всю эту разницу за функциями, повторяющими СТАРЫЙ
интерфейс, чтобы скрипты менялись минимально:
    totals(date)            -> dict с теми же ключами, что старый totals
    totals_period(d1, d2)   -> то же, просуммированное по диапазону дней
    sales_by_sku(date)      -> {sku: ₽ продаж}   (замена fetch_*_from_finance)
    sales_total(date)       -> ₽ продаж за день  (замена fetch_sales_total)
    accrual_types()         -> {type_id: name}   (справочник, кэшируется)
    postings(numbers)       -> сырые начисления по списку отправлений

============================ КАК ЭТО РАБОТАЕТ ============================
1) by-day отдаёт записи постранично через курсор last_id (НЕ page/page_size).
   _iter_by_day() прокручивает все страницы за дату.
2) Каждая запись имеет accrued_category: POSTING / ITEM / NON_ITEM.
   - posting.products[] — товары отправления: продажи и комиссия;
   - item_fees / non_item_fee / delivery.services — сервисные начисления
     с type_id (реклама, логистика, хранение, возвраты, эквайринг...).
3) Все суммы — СТРОКИ вида {"amount": "-42.7", "currency": "RUB"}.
   _money() парсит их в float. Знак сохраняется: расход = минус
   (как и в старом API), поэтому формулы прибыли в скриптах не трогаем.

==================== МАППИНГ КАТЕГОРИЙ (CATEGORY_MAP) ====================
type_id -> старая категория totals. Сверено до копейки на 2026-06-17
скриптом probe_accrual.py (старый totals == свод по этим type_id):

    accruals_for_sale         = Σ posting.products[].commission.seller_price
    sale_commission           = Σ posting.products[].commission.sale_commission
    processing_and_delivery   = type_id 32, 29, 98
    services_amount           = type_id 41,77,15,46,84,96,39,38,78,48
    refunds_and_cancellations = type_id 59, 45
    others_amount             = type_id 1  (Acquiring)
    compensation_amount       = type_id 10 (Compensation)

ВАЖНО: в справочнике 115 типов, а в один день встречается ~17. Любой
type_id, которого нет в CATEGORY_MAP, попадает в others_amount и
ПЕЧАТАЕТСЯ предупреждением "[accrual] UNMAPPED type_id=...". Это значит:
общая сумма потерь всегда верна, но новый тип нужно вручную отнести в
нужную категорию здесь, в CATEGORY_MAP. Перед окончательным переключением
прогоните probe_accrual.py на нескольких датах и закройте все UNMAPPED.

Сверка на 31.05 / 05.06 / 10.06 / 14.06.2026: все категории затрат и
sale_commission сходятся со старым totals в ноль. Известная мелкая дельта:
accruals_for_sale в отдельные дни на 0.03-0.07% ниже старого (краевой
эффект датировки начислений у границы суток; в «чистые» дни совпадает
точно). На периоде сглаживается; seller_price — корректное поле
(sale_amount стабильно переоценивает).

============================== ИСПОЛЬЗОВАНИЕ =============================
    from finance_accrual import totals, sales_by_sku, sales_total

    t = totals("2026-06-17")            # {'accruals_for_sale': ..., ...}
    sku_rub = sales_by_sku("2026-06-17") # {123456789: 1990.0, ...}

Требует в .env: OZON_CLIENT_ID, OZON_API_KEY.
Модуль ничего не пишет — только читает Ozon API.
=========================================================================
"""

import os
import time
from collections import defaultdict
from datetime import date as _date, datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
BASE = "https://api-seller.ozon.ru"

HEADERS = {
    "Client-Id": OZON_CLIENT_ID,
    "Api-Key": OZON_API_KEY,
    "Content-Type": "application/json",
}

# Старые именованные категории totals. Порядок и имена 1:1 со старым API.
TOTALS_KEYS = (
    "accruals_for_sale",
    "sale_commission",
    "processing_and_delivery",
    "refunds_and_cancellations",
    "services_amount",
    "compensation_amount",
    "money_transfer",
    "others_amount",
)

# type_id -> категория totals. Сверено probe_accrual.py на 2026-06-17.
# Незнакомые type_id -> others_amount + предупреждение (см. шапку).
CATEGORY_MAP = {
    # --- processing_and_delivery (логистика и доставка до покупателя) ---
    32: "processing_and_delivery",   # Logistic
    29: "processing_and_delivery",   # LastMileCourier
    98: "processing_and_delivery",   # DeliveryToHandoverPlaceByOzon
    # --- services_amount (реклама, хранение, утилизация, упаковка, ...) ---
    41: "services_amount",           # PayPerClick
    77: "services_amount",           # SupplyInbound
    15: "services_amount",           # Disposal
    46: "services_amount",           # Placements
    84: "services_amount",           # ItemPacking
    96: "services_amount",           # AcceleratedReviewCollection
    39: "services_amount",           # PackingFee
    38: "services_amount",           # PackageCost
    78: "services_amount",           # TemporaryPlacement
    48: "services_amount",           # PremiumCashbackIndividualPoints
    12: "services_amount",           # CrossDock        (сверено 2026-05-31)
    54: "services_amount",           # Promotion        (сверено 2026-06-10)
    # --- refunds_and_cancellations (возвраты/отмены) ---
    59: "refunds_and_cancellations", # ReturnFlowLogistic
    45: "refunds_and_cancellations", # PickUpPointReturnAcceptance
    6: "refunds_and_cancellations",  # Cancellation (семантически; на 2026-06-18 был =0, сверить на ненулевом дне)
    # --- others_amount ---
    1: "others_amount",              # Acquiring
    # --- compensation_amount ---
    10: "compensation_amount",       # Compensation
    25: "compensation_amount",       # ItemCompensation (сверено 2026-05-31)
}

# type_id, по которым уже предупредили — чтобы не спамить в лог.
_warned_type_ids = set()


def _money(obj):
    """{'amount': '123.45', 'currency': 'RUB'} -> float. Пусто/None -> 0.0"""
    if not isinstance(obj, dict):
        return 0.0
    try:
        return float(obj.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _post(path, body, timeout=60, retries=5, delay=5):
    """POST к Ozon с ретраями. Возвращает распарсенный JSON или кидает."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        raise RuntimeError("OZON_CLIENT_ID / OZON_API_KEY не заданы в .env")
    url = f"{BASE}{path}"
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, headers=HEADERS, json=body, timeout=timeout)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
            return r.json()
        except Exception as e:
            last = e
            print(f"[accrual] retry {attempt}/{retries} {path} -> {e}")
            if attempt < retries:
                time.sleep(delay)
    raise last


# ------------------------------------------------------------------ types
_types_cache = None
_descr_cache = None


def accrual_types(force=False):
    """Справочник типов начислений: {type_id: name} (англ.). Кэшируется на процесс."""
    global _types_cache
    if _types_cache is not None and not force:
        return _types_cache
    data = _post("/v1/finance/accrual/types", {})
    _types_cache = {
        t.get("id"): t.get("name", "")
        for t in (data.get("accrual_types", []) or [])
    }
    return _types_cache


def accrual_type_descriptions(force=False):
    """Справочник {type_id: русское описание} (поле description; fallback на name)."""
    global _descr_cache
    if _descr_cache is not None and not force:
        return _descr_cache
    data = _post("/v1/finance/accrual/types", {})
    _descr_cache = {
        t.get("id"): (t.get("description") or t.get("name", ""))
        for t in (data.get("accrual_types", []) or [])
    }
    return _descr_cache


def _category_for(type_id):
    """type_id -> категория totals. Неизвестный -> others_amount + warning."""
    cat = CATEGORY_MAP.get(type_id)
    if cat is None:
        if type_id not in _warned_type_ids:
            _warned_type_ids.add(type_id)
            name = accrual_types().get(type_id, "?")
            print(f"[accrual] UNMAPPED type_id={type_id} ({name}) "
                  f"-> отнесён в others_amount. Добавьте в CATEGORY_MAP.")
        return "others_amount"
    return cat


# ----------------------------------------------------------------- by-day
def _iter_by_day(date, max_pages=200, pause=0.3):
    """Генератор всех записей accruals за дату (прокручивает last_id)."""
    last_id = ""
    pages = 0
    while True:
        pages += 1
        data = _post("/v1/finance/accrual/by-day", {"date": date, "last_id": last_id})
        accruals = data.get("accruals", []) or []
        for acc in accruals:
            yield acc
        last_id = data.get("last_id") or ""
        if not last_id or not accruals or pages >= max_pages:
            break
        time.sleep(pause)


def totals(date):
    """
    Замена /v3/finance/transaction/totals за один день.
    Возвращает dict с теми же ключами и знаками, что старый API
    (расходы — отрицательные). Дата: 'YYYY-MM-DD'.
    """
    out = {k: 0.0 for k in TOTALS_KEYS}

    for acc in _iter_by_day(date):
        # Продажи и комиссия — из товаров отправления.
        posting = acc.get("posting")
        if posting:
            for prod in posting.get("products", []) or []:
                comm = prod.get("commission") or {}
                out["accruals_for_sale"] += _money(comm.get("seller_price"))
                out["sale_commission"] += _money(comm.get("sale_commission"))
                # Доставка по товару -> processing_and_delivery (по type_id услуг).
                for srv in (prod.get("delivery") or {}).get("services", []) or []:
                    out[_category_for(srv.get("type_id"))] += _money(srv.get("accrued"))

        # Сервисные начисления по товарам.
        for grp in (acc.get("item_fees") or {}).get("fees", []) or []:
            for fee in grp.get("fees", []) or []:
                out[_category_for(fee.get("type_id"))] += _money(fee.get("accrued"))

        # Начисление по продавцу без привязки к товару.
        nif = acc.get("non_item_fee")
        if nif:
            out[_category_for(nif.get("type_id"))] += _money(nif.get("accrued"))

    return {k: round(v, 2) for k, v in out.items()}


def totals_period(date_from, date_to):
    """totals() просуммированный по всем дням диапазона [date_from; date_to]."""
    out = {k: 0.0 for k in TOTALS_KEYS}
    for d in _daterange(date_from, date_to):
        day = totals(d)
        for k in TOTALS_KEYS:
            out[k] += day[k]
    return {k: round(v, 2) for k, v in out.items()}


def sales_by_sku(date):
    """
    Замена fetch_sku_sales_from_finance / продаж по SKU из transaction/list.
    {sku(int): сумма продаж ₽} за день. Продажи = seller_price (сверено:
    старый accruals_for_sale == Σ seller_price).
    """
    res = defaultdict(float)
    for acc in _iter_by_day(date):
        posting = acc.get("posting")
        if not posting:
            continue
        for prod in posting.get("products", []) or []:
            sku = prod.get("sku")
            if sku is None:
                continue
            res[sku] += _money((prod.get("commission") or {}).get("seller_price"))
    return {sku: round(v, 2) for sku, v in res.items()}


def sales_total(date):
    """Замена fetch_sales_total: суммарные продажи ₽ за день (accruals_for_sale)."""
    return round(sum(sales_by_sku(date).values()), 2)


def breakdown_by_sku(date):
    """
    Продажи и затраты по каждому SKU за день. Для daily.py (затраты по offer).

    {sku(int): {"sales": ₽, "costs": ₽>=0}}, где:
      sales = Σ seller_price (как accruals_for_sale);
      costs = Σ модулей ОТРИЦАТЕЛЬНЫХ начислений по товару:
              |sale_commission| + |delivery.total_accrued| + |item_fees по sku|.
    Аналог старого op_cost (комиссия + доставка + сервисы), но напрямую по SKU.
    """
    res = defaultdict(lambda: {"sales": 0.0, "costs": 0.0})

    for acc in _iter_by_day(date):
        posting = acc.get("posting")
        if posting:
            for prod in posting.get("products", []) or []:
                sku = prod.get("sku")
                if sku is None:
                    continue
                comm = prod.get("commission") or {}
                res[sku]["sales"] += _money(comm.get("seller_price"))
                sc = _money(comm.get("sale_commission"))
                if sc < 0:
                    res[sku]["costs"] += -sc
                deliv = _money((prod.get("delivery") or {}).get("total_accrued"))
                if deliv < 0:
                    res[sku]["costs"] += -deliv

        # item_fees: сервисные начисления по товару (по sku)
        for grp in (acc.get("item_fees") or {}).get("fees", []) or []:
            sku = grp.get("sku")
            if sku is None:
                continue
            for fee in grp.get("fees", []) or []:
                a = _money(fee.get("accrued"))
                if a < 0:
                    res[sku]["costs"] += -a

    return {
        sku: {"sales": round(v["sales"], 2), "costs": round(v["costs"], 2)}
        for sku, v in res.items()
    }


# --------------------------------------------------------------- postings
def postings(posting_numbers):
    """
    /v1/finance/accrual/postings — сырые начисления по списку отправлений.
    Принимает список номеров (НЕ даты). Возвращает posting_accruals как есть.
    ВНИМАНИЕ: по свежим отправлениям начисления могут быть ещё пустыми.
    Для регулярных отчётов используйте by-day, а это — для точечных сверок.
    """
    data = _post("/v1/finance/accrual/postings", {"posting_numbers": list(posting_numbers)})
    return data.get("posting_accruals", []) or []


# ----------------------------------------------------------------- utils
def _daterange(date_from, date_to):
    """Список дат 'YYYY-MM-DD' включительно."""
    d0 = datetime.strptime(date_from, "%Y-%m-%d").date()
    d1 = datetime.strptime(date_to, "%Y-%m-%d").date()
    cur = d0
    while cur <= d1:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


# --------------------------------------------- публичные хелперы для отчётов
# Используются finance_report/ для собственной агрегации (по типам/категориям).
def iter_accruals(date):
    """Публичный генератор записей accruals за день (обёртка _iter_by_day)."""
    yield from _iter_by_day(date)


def parse_money(obj):
    """Публичный парсер {'amount': '..','currency': '..'} -> float."""
    return _money(obj)


def category_for(type_id):
    """Публичный маппинг type_id -> категория totals (UNMAPPED -> others_amount)."""
    return _category_for(type_id)


if __name__ == "__main__":
    # Ручная проверка: python finance_accrual.py [YYYY-MM-DD]
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else \
        (_date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"Дата: {d}")
    t = totals(d)
    print("totals():")
    for k in TOTALS_KEYS:
        print(f"  {k:>28}: {t[k]}")
    skus = sales_by_sku(d)
    print(f"sales_by_sku(): {len(skus)} SKU, Σ = {round(sum(skus.values()), 2)}")
