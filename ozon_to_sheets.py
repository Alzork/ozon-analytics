import os
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from datetime import datetime, timedelta
import time
from constants import (
    sku_list,
    region_to_warehouses,
    clusters_id,
    offer_to_sku,
    clean_offer_to_product_id,
    analytics_metrics,
    row_map_sku,
    row_map
)

import finance_accrual

DAYS_SHIFT = 1  # 1 = вчера, 2 = позавчера, 14 = две недели назад и т.д.
target_date = (datetime.now() - timedelta(days=DAYS_SHIFT)).strftime("%Y-%m-%d")

# --- Управление режимами работы ---
SKIP_SVD_OVH = os.getenv("SKIP_SVD_OVH", "0") == "1"
print(f"[INFO] Используется дата: {target_date} | SKIP_SVD_OVH = {SKIP_SVD_OVH}")

# === Safe request helper ===
def safe_request(url, headers=None, json=None, retries=5, method="post", timeout=30):
    import inspect
    caller_name = inspect.stack()[1].function

    if "analytics/data" in url:
        print(
            f"\n📡 Analytics API call → {caller_name} | "
            f"metrics_count={len(json.get('metrics', [])) if json and 'metrics' in json else 'n/a'}, "
            f"metrics={json.get('metrics', []) if json and 'metrics' in json else 'n/a'}"
        )
    resp = None
    for attempt in range(retries):
        try:
            if method.lower() == "post":
                resp = requests.post(url, headers=headers, json=json, timeout=timeout)
            else:
                resp = requests.get(url, headers=headers, params=json, timeout=timeout)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                wait = min(5 * (2 ** attempt), 60)
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                wait = min(5 * (2 ** attempt), 60)
                time.sleep(wait)
                continue

            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            status = getattr(resp, "status_code", None)
            # 4xx (кроме 429) — постоянная ошибка клиента (404 и т.п.),
            # повторять бессмысленно: сразу пробрасываем без ретраев.
            if status is not None and 400 <= status < 500 and status != 429:
                raise e
            wait = 0.5 * (attempt + 1)
            time.sleep(wait)
            if attempt == retries - 1:
                print(f"❌ Ответ сервера при ошибке: {getattr(resp, 'text', None)}")
                raise e
            time.sleep(1)
            continue
    raise Exception(f"API error after {retries} retries for {url}")

# === 1. Загружаем переменные из .env ===
load_dotenv()

# Создаём обратный словарь: product_id (sku) → offer_id
product_to_offer = {str(v): k for k, v in offer_to_sku.items()}
known_sku_ids = {str(v) for v in offer_to_sku.values()}
known_offer_ids = set(offer_to_sku.keys())

CLIENT_ID = os.getenv("OZON_CLIENT_ID")
API_KEY = os.getenv("OZON_API_KEY")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_CREDS = os.getenv("GOOGLE_CREDS")

OZON_PERF_CLIENT_ID = os.getenv("OZON_PERF_CLIENT_ID")
OZON_PERF_CLIENT_SECRET = os.getenv("OZON_PERF_CLIENT_SECRET")

# === 2. Авторизация в Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]

creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS, scope)
client = gspread.authorize(creds)
spreadsheet = client.open(SHEET_NAME)


headers = {
    "Client-Id": CLIENT_ID,
    "Api-Key": API_KEY
}

# Определяем дату "вчера"
date_from = target_date
date_to = target_date

# === Helpers for date/time and numbers ===
def to_zulu(d: str, end: bool = False) -> str:
    return f"{d}T23:59:59Z" if end else f"{d}T00:00:00Z"

def safe_number(x):
    try:
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str):
            return float(x.replace(',', '.'))
    except Exception:
        return 0.0
    return 0.0


def align_cart_conversions_with_lk(values):
    """
    Ozon Analytics API returns conv_tocart* against session_view*,
    while the seller cabinet tiles use hits_view* as denominator.
    Recalculate conversions so Google Sheets matches the values from LK.
    """
    if not values:
        return values

    hits_tocart = safe_number(values.get("hits_tocart"))
    hits_view = safe_number(values.get("hits_view"))
    hits_view_pdp = safe_number(values.get("hits_view_pdp"))
    hits_view_search = safe_number(values.get("hits_view_search"))
    session_view_pdp = safe_number(values.get("session_view_pdp"))
    session_view_search = safe_number(values.get("session_view_search"))
    conv_tocart_pdp_api = safe_number(values.get("conv_tocart_pdp"))
    conv_tocart_search_api = safe_number(values.get("conv_tocart_search"))

    if hits_view > 0:
        values["conv_tocart"] = round((hits_tocart / hits_view) * 100, 2)

    # Restore add-to-cart counts by source from API conversions,
    # then recalculate cabinet-style conversions against impressions.
    hits_tocart_pdp = (conv_tocart_pdp_api / 100) * session_view_pdp
    hits_tocart_search = (conv_tocart_search_api / 100) * session_view_search

    if hits_view_pdp > 0:
        values["conv_tocart_pdp"] = round((hits_tocart_pdp / hits_view_pdp) * 100, 2)
    if hits_view_search > 0:
        values["conv_tocart_search"] = round((hits_tocart_search / hits_view_search) * 100, 2)

    return values


# === Fetch FBS/FBO postings and aggregate units, revenue ===
def fetch_postings(schema: str, date_from: str, date_to: str):
    url = f"https://api-seller.ozon.ru/v3/posting/{schema}/list"
    body = {
        "filter": {
            "processed_at": {
                "from": to_zulu(date_from, False),
                "to": to_zulu(date_to, True)
            }
        },
        "limit": 1000
    }
    try:
        data = safe_request(url, headers=headers, json=body, method="post", timeout=30)
    except Exception:
        return 0, 0.0, 0, 0.0

    # API can return {result: {postings: [...]}} или {result: [...]}
    postings = data.get("result", {}).get("postings") if isinstance(data.get("result"), dict) else data.get("result", [])
    postings = postings or []

    ordered_units_total = 0
    ordered_revenue_total = 0.0
    delivered_units_total = 0
    delivered_revenue_total = 0.0

    for p in postings:
        status = p.get("status", "")
        prods = p.get("products", [])
        for pr in prods:
            qty = pr.get("quantity") or pr.get("qty") or 1
            price = pr.get("price") or pr.get("offer_price") or pr.get("sale_price") or 0
            try:
                qty = int(qty)
            except Exception:
                qty = 1
            price = safe_number(price)
            amount = qty * price

            ordered_units_total += qty
            ordered_revenue_total += amount
            if status == "delivered":
                delivered_units_total += qty
                delivered_revenue_total += amount

    return ordered_units_total, ordered_revenue_total, delivered_units_total, delivered_revenue_total

def fetch_one_metric(metric: str):
    body = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": [metric],
        "dimensions": ["day"],
        "limit": 1000
    }
    try:
        j = safe_request("https://api-seller.ozon.ru/v1/analytics/data", headers=headers, json=body, method="post", timeout=30)
        data = j.get("result", {}).get("data", [])
        if data and data[0].get("metrics"):
            return data[0]["metrics"][0]
    except Exception:
        pass
    return 0

def fetch_all_metrics(metrics, date_from, date_to):
    body = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": metrics,
        "dimensions": ["day"],
        "filters": [
            {"key": "state", "value": "ACTIVE"},
            {"key": "visibility", "value": "ALL"}
        ],
        "limit": 1000
    }
    values = {}
    try:
        j = safe_request("https://api-seller.ozon.ru/v1/analytics/data", headers=headers, json=body, method="post", timeout=30)
        data = j.get("result", {}).get("data", [])
        if data:
            # Подсчёт всех метрик по всем дням
            sum_metrics = {m: 0.0 for m in metrics}
            count_metrics = {m: 0 for m in metrics}
            for row in data:
                metrics_list = row.get("metrics", [])
                for i, m in enumerate(metrics):
                    val = safe_number(metrics_list[i]) if i < len(metrics_list) else 0.0
                    if m.startswith("conv_"):
                        sum_metrics[m] += val
                        count_metrics[m] += 1
                    else:
                        sum_metrics[m] += val
            for m in metrics:
                if m.startswith("conv_"):
                    values[m] = (sum_metrics[m] / count_metrics[m]) if count_metrics[m] > 0 else 0.0
                else:
                    values[m] = sum_metrics[m]
    except Exception as e:
        print(f"Ошибка при получении метрик: {e}")
    return align_cart_conversions_with_lk(values)


def fetch_sku_metrics(sku_id: int, date_from: str, date_to: str):
    """
    Забирает метрики по конкретному SKU (product_id от Озон)
    """
    body = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": list(row_map_sku.values()),
        "dimensions": ["day", "sku"],
        "filters": [
            {"key": "sku", "value": str(sku_id)}
        ],
        "limit": 1000
    }
    try:
        j = safe_request("https://api-seller.ozon.ru/v1/analytics/data", headers=headers, json=body, method="post", timeout=30)
        data = j.get("result", {}).get("data", [])
        if not data:
            return {}

        metrics_keys = list(row_map_sku.values())
        sum_metrics = {m: 0.0 for m in metrics_keys}
        count_metrics = {m: 0 for m in metrics_keys}

        for row in data:
            metrics_list = row.get("metrics", [])
            for i, m in enumerate(metrics_keys):
                val = safe_number(metrics_list[i]) if i < len(metrics_list) else 0.0
                if m.startswith("conv_"):
                    sum_metrics[m] += val
                    count_metrics[m] += 1
                else:
                    sum_metrics[m] += val

        result = {}
        for m in metrics_keys:
            if m.startswith("conv_"):
                result[m] = (sum_metrics[m] / count_metrics[m]) if count_metrics[m] > 0 else 0.0
            else:
                result[m] = sum_metrics[m]
        return align_cart_conversions_with_lk(result)

    except Exception as e:
        print(f"❌ Ошибка при получении метрик для SKU {sku_id}: {e}")
        return {}

def fetch_prices_by_offer_ids(offer_ids):
    """
    Получает marketing_seller_price и min_price через v5/product/info/prices.
    Запрос ТОЛЬКО по product_id.
    product_id берётся из словаря clean_offer_to_product_id.
    Возвращает: {offer_id: {"price": ..., "old_price": ...}}
    """

    offer_ids = [str(oid) for oid in offer_ids]

    # Собираем product_id по словарю
    product_ids = []
    for oid in offer_ids:
        pid = clean_offer_to_product_id.get(oid)
        if pid:
            product_ids.append(pid)

    if not product_ids:
        return {oid: {"price": None, "old_price": None} for oid in offer_ids}

    url = "https://api-seller.ozon.ru/v5/product/info/prices"

    body = {
        "cursor": "",
        "filter": {
            "product_id": product_ids,
            "visibility": "ALL"
        },
        "limit": 100
    }

    try:
        j = safe_request(url, headers=headers, json=body, method="post", timeout=60)
        items = j.get("items", [])
    except Exception as e:
        print(f"❌ Ошибка получения цен v5: {e}")
        return {oid: {"price": None, "old_price": None} for oid in offer_ids}

    # Готовим структуру под результат
    prices_data = {oid: {"price": None, "old_price": None} for oid in offer_ids}

    # Делаем обратный словарь product_id → offer_id
    product_to_offer = {v: k for k, v in clean_offer_to_product_id.items()}

    for item in items:
        pid = item.get("product_id")
        offer_id = product_to_offer.get(pid)

        if not offer_id:
            continue

        price_block = item.get("price", {}) or {}
        marketing = price_block.get("marketing_seller_price")
        min_price = price_block.get("min_price")

        prices_data[offer_id] = {
            "marketing_price": safe_number(marketing) if marketing is not None else None,
            "price": safe_number(min_price) if min_price is not None else None
        }

    return prices_data


def fetch_all_sku_metrics(sku_list, date_from, date_to):
    body = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": list(row_map_sku.values()),
        "dimensions": ["sku", "day"],
        "filters": [
            {"key": "state", "value": "ACTIVE"},
            {"key": "visibility", "value": "ALL"}
        ],
        "limit": 1000
    }
    metrics_keys = list(row_map_sku.values())
    # Для каждого SKU: sum обычных, sum conv_ и count conv_
    results = {sku: {m: 0.0 for m in metrics_keys} for sku in sku_list}
    conv_counts = {sku: {m: 0 for m in metrics_keys if m.startswith("conv_")} for sku in sku_list}
    try:
        j = safe_request("https://api-seller.ozon.ru/v1/analytics/data", headers=headers, json=body, method="post", timeout=30)
        data = j.get("result", {}).get("data", [])
        for row in data:
            dims = row.get("dimensions", [])
            sku = dims[0]["id"] if dims else None
            if sku and sku in results:
                metrics_list = row.get("metrics", [])
                for i, m in enumerate(metrics_keys):
                    val = safe_number(metrics_list[i]) if i < len(metrics_list) else 0.0
                    if m.startswith("conv_"):
                        results[sku][m] += val
                        conv_counts[sku][m] = conv_counts[sku].get(m, 0) + 1
                    else:
                        results[sku][m] += val
        # Теперь усредняем conv_ метрики
        for sku in sku_list:
            for m in metrics_keys:
                if m.startswith("conv_"):
                    count = conv_counts[sku].get(m, 0)
                    results[sku][m] = (results[sku][m] / count) if count > 0 else 0.0
    except Exception as e:
        print(f"Ошибка при пакетном получении метрик по SKU: {e}")
    for sku in sku_list:
        results[sku] = align_cart_conversions_with_lk(results[sku])
    return results

def fetch_delivered_units(date_from, date_to):
    # Используем fetch_postings для подсчёта доставленных товаров
    ordered_units, revenue, delivered_units, delivered_revenue = 0, 0.0, 0, 0.0
    for schema in ["fbs", "fbo"]:
        ou, rev, du, dr = fetch_postings(schema, date_from, date_to)
        ordered_units += ou
        revenue += rev
        delivered_units += du
        delivered_revenue += dr
    return delivered_units

# === Performance API: Получение токена и расходов на рекламу ===
def get_performance_access_token():
    url = "https://api-performance.ozon.ru/api/client/token"
    print(f"Requesting Performance API token from URL: {url}")
    body = {
        "client_id": OZON_PERF_CLIENT_ID,
        "client_secret": OZON_PERF_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    try:
        # Performance API token endpoint requires form data, not JSON
        resp = requests.post(url, data=body)
        print("Token response:", resp.json())
        resp.raise_for_status()
        token_data = resp.json()
        return token_data.get("access_token")
    except Exception as e:
        print(f"Ошибка получения токена Performance API: {e}, текст: {resp.text if 'resp' in locals() else ''}")
        return None

def fetch_ad_spend_yesterday():
    token = get_performance_access_token()
    if not token:
        return 0
    url = "https://api-performance.ozon.ru/api/client/statistics/expense/json"
    date_yesterday = target_date
    headers_perf = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    params = {
        "dateFrom": date_yesterday,
        "dateTo": date_yesterday
    }
    try:
        data = safe_request(url, headers=headers_perf, json=params, method="get", timeout=30)
        print("Perf API expense response:", data)
        rows = data.get("rows", [])
        total_spend = 0.0
        seen_ids = set()

        for row in rows:
            row_id = row.get("id")
            title = str(row.get("title", "")).strip()

            # Пропускаем дубликаты и агрегированные пакеты
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            if "пакет" in title.lower() or "спецпроект" in title.lower() or not any(ch.isdigit() for ch in title):
                continue

            total_spend += safe_number(row.get("moneySpent", 0))

        # Проверка на аномалии
        if total_spend > 100000:
            print(f"[ALERT] Возможное дублирование или агрегат: {total_spend} ₽ за {date_yesterday}")

        return total_spend
    except Exception as e:
        print(f"Ошибка получения расходов на рекламу: {e}")
        return 0


# --- Новый метод: Получение заказов по каждому SKU через /v1/analytics/data ---
def fetch_revenue_by_sku_analytics(date_from: str, date_to: str):
    url = "https://api-seller.ozon.ru/v1/analytics/data"
    metric = "revenue"
    sales_by_sku = {}

    body = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": [metric],
        "dimensions": ["sku", "day"],
        "limit": 1000
    }

    j = safe_request(url, headers=headers, json=body, method="post", timeout=60)
    data = j.get("result", {}).get("data", [])

    print("[DEBUG][ANALYTICS RAW] rows =", len(data))

    for row in data:
        dims = row.get("dimensions", [])
        if not dims:
            continue

        sku_id = ""
        for dim in dims:
            dim_name = str(dim.get("name") or dim.get("key") or "").strip().lower()
            dim_id = dim.get("id")
            if dim_name == "sku" and dim_id is not None:
                sku_id = str(dim_id)
                break
        if not sku_id:
            sku_id = str(dims[0].get("id") or "")
        if not sku_id:
            continue

        metrics_list = row.get("metrics", [])
        revenue = safe_number(metrics_list[0]) if metrics_list else 0.0

        sales_by_sku[sku_id] = sales_by_sku.get(sku_id, 0.0) + revenue

        offer_id = product_to_offer.get(sku_id, "")
        print(f"[DEBUG][ANALYTICS SKU] offer_id={offer_id} sku_id={sku_id} revenue={revenue}")

    return sales_by_sku

def fetch_geography_sales(date_from: str, date_to: str):
    """
    Получает географию продаж по каждому offer_id через /v3/posting/fbo/list.
    Возвращает dict: {offer_id: {region_name: units_sold, ...}, ...}
    """
    geo_by_sku = {}

    def fetch():
        url = f"https://api-seller.ozon.ru/v3/posting/fbo/list"
        cursor = ""
        while True:
            body = {
                "sort_dir": "ASC",
                "filter": {
                    "since": f"{date_from}T00:00:00.000Z",
                    "to": f"{date_to}T23:59:59.000Z"
                },
                "limit": 100,
                "cursor": cursor,
                "with": {
                    "analytics_data": True,
                    "financial_data": False,
                    "legal_info": False
                }
            }
            try:
                j = safe_request(url, headers=headers, json=body, method="post", timeout=120)
                data = j.get("postings", [])
            except Exception as e:
                print(f"❌ Ошибка fbo/list (география): {e}")
                break

            if not data:
                break

            for posting in data:
                analytics = posting.get("analytics_data", {}) or {}
                warehouse_id = str(analytics.get("warehouse_id", ""))

                # Определяем регион по складу
                region_name = None
                for region, wh_list in region_to_warehouses.items():
                    if warehouse_id in wh_list:
                        region_name = region
                        break
                if not region_name:
                    region_name = "Прочее"

                for it in posting.get("products", []):
                    product_id = str(it.get("sku") or it.get("product_id") or "")
                    if not product_id:
                        continue
                    offer_id = product_to_offer.get(product_id)
                    if not offer_id:
                        continue
                    qty = it.get("quantity") or 1
                    try:
                        qty = int(qty)
                    except Exception:
                        qty = 1

                    if offer_id not in geo_by_sku:
                        geo_by_sku[offer_id] = {}
                    geo_by_sku[offer_id][region_name] = geo_by_sku[offer_id].get(region_name, 0) + qty

            cursor = j.get("cursor", "")
            if not j.get("has_next", False):
                break
            time.sleep(0.2)

    # Собираем только fbo
    fetch()

    return geo_by_sku

def fetch_delivery_speed_geo():
    """
    Получает среднее время доставки (СВД) по регионам для листа 'Свод'.
    Использует API метод /v1/analytics/average-delivery-time.
    """
    import requests, os, time

    url = "https://api-seller.ozon.ru/v1/analytics/average-delivery-time"
    headers = {
        "Client-Id": os.getenv("OZON_CLIENT_ID"),
        "Api-Key": os.getenv("OZON_API_KEY"),
        "Content-Type": "application/json"
    }
    payload = {
        "delivery_schema": "ALL",
        "supply_period": "EIGHT_WEEKS",
        "sku": [],
    }

    # обратное сопоставление (delivery_cluster_id → регион)
    id_to_region = {v: k for k, v in clusters_id.items()}
    result = {}

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            print("⚠️ Нет данных СВД для географии.")
            return {}

        for entry in data:
            delivery_cluster_id = entry.get("delivery_cluster_id")
            metrics = entry.get("metrics", {})
            avg_time = metrics.get("average_delivery_time", 0)

            region_name = id_to_region.get(delivery_cluster_id)
            if not region_name:
                continue

            result[region_name] = round(float(avg_time or 0), 2)
            print(f"✅ {region_name}: {avg_time} ч")

        print(f"📦 Всего регионов: {len(result)}")
        return result

    except Exception as e:
        print(f"❌ Ошибка при получении географии СВД: {e}")
        return {}

# --- Новый метод: Получение общего среднего времени доставки через summary ---
def fetch_delivery_speed_summary():
    """
    Получает общее среднее время доставки (СВД) по всему магазину.
    Использует API метод /v1/analytics/average-delivery-time/summary.
    Применяется для листа 'Свод' — строка 'Среднее время доставки, общее'.
    """
    import requests, os

    url = "https://api-seller.ozon.ru/v1/analytics/average-delivery-time/summary"
    headers = {
        "Client-Id": os.getenv("OZON_CLIENT_ID"),
        "Api-Key": os.getenv("OZON_API_KEY"),
        "Content-Type": "application/json"
    }
    payload = {
        "delivery_schema": "ALL",
        "supply_period": "EIGHT_WEEKS"
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        avg = data.get("average_delivery_time", 0)
        print(f"✅ fetch_delivery_speed_summary: {avg} ч")
        return {"avg": avg}

    except Exception as e:
        print(f"❌ Ошибка запроса summary СВД: {e}")
        return {"avg": 0}

# --- Новый метод: Получение средней скорости доставки по каждому SKU и регионам ---
# Флаг: endpoint /details недоступен (404). Ставится один раз за запуск,
# чтобы не дёргать мёртвый метод по всем кластерам для каждого SKU.
_SVD_DETAILS_DEAD = False


def fetch_delivery_speed_details(sku_id=None):
    """
    Вариант А — оптимизированный сбор СВД:
    • Один цикл по всем кластерам (region_name → cluster_id)
    • Один запрос на кластер = получаем данные по ВСЕМ SKU сразу
    • Для каждого SKU собираем средневзвешенное время доставки
    """
    import time
    import os

    global _SVD_DETAILS_DEAD
    # endpoint уже признан мёртвым в этом запуске — мгновенно возвращаем пусто
    if _SVD_DETAILS_DEAD:
        return {"overall": {"avg": 0}, "by_region": {}}

    url = "https://api-seller.ozon.ru/v1/analytics/average-delivery-time/details"
    headers = {
        "Client-Id": os.getenv("OZON_CLIENT_ID"),
        "Api-Key": os.getenv("OZON_API_KEY"),
        "Content-Type": "application/json"
    }

    # sku: {region: time}
    result = {}
    # sku: list of times for overall avg
    overall_accum = {}

    for region_name, cluster_id in clusters_id.items():
        offset = 0
        seen = {}

        while True:
            body = {
                "cluster_id": cluster_id,
                "filters": {
                    "delivery_schema": "ALL",
                    "supply_period": "ONE_DAY"
                },
                "limit": 1000,
                "offset": offset
            }

            try:
                data = safe_request(url, headers=headers, json=body, method="post", timeout=30)
            except Exception as e:
                err_text = str(e).lower()
                if "no such cluster" in err_text:
                    print(f"⚠️ Пропускаем кластер {region_name} — не поддерживается /details")
                    break
                if "404" in err_text or "not found" in err_text:
                    # метод удалён на стороне Ozon — отключаем его до конца запуска
                    print("⚠️ Endpoint /average-delivery-time/details недоступен (404) — "
                          "отключаю СВД-детализацию до конца запуска.")
                    _SVD_DETAILS_DEAD = True
                    return {"overall": {"avg": 0}, "by_region": {}}
                print(f"❌ Ошибка при запросе к кластеру {region_name}: {e}")
                break

            rows = data.get("data", [])
            if not rows:
                break

            for row in rows:
                item = row.get("item", {})
                sku_val = str(item.get("sku") or "")
                if not sku_val:
                    continue

                # фильтр только нужного SKU, если sku_id указан
                if sku_id and sku_val != str(sku_id):
                    continue

                clusters_data = row.get("clusters_data", [])
                if clusters_data:
                    total_time = 0
                    total_orders = 0
                    for c in clusters_data:
                        t = c.get("delivery_time_FBO") or c.get("delivery_time_FBS") or 0
                        cnt = c.get("orders_count", 0)
                        total_time += t * cnt
                        total_orders += cnt
                    time_avg = round(total_time / total_orders, 2) if total_orders else 0
                else:
                    time_avg = round(float(row.get("metrics", {}).get("average_delivery_time", 0)), 2)

                if sku_val not in result:
                    result[sku_val] = {}
                result[sku_val][region_name] = time_avg

                overall_accum.setdefault(sku_val, []).append(time_avg)

            if len(rows) < 1000:
                break
            offset += 1000
            time.sleep(0.2)

    # Формирование финального ответа
    if sku_id:
        sku_str = str(sku_id)
        by_region = result.get(sku_str, {})
        arr = overall_accum.get(sku_str, [])
        overall = round(sum(arr) / len(arr), 2) if arr else 0
        return {"overall": {"avg": overall}, "by_region": by_region}

    # если не указан sku_id, вернуть данные для всех
    output = {}
    for sku_val, reg_times in result.items():
        arr = overall_accum.get(sku_val, [])
        avg = round(sum(arr) / len(arr), 2) if arr else 0
        output[sku_val] = {"overall": {"avg": avg}, "by_region": reg_times}

    return output




# --- Helper: safe update to Google Sheets with retries ---
def sheets_update_with_retry(ws, range_name, values, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        try:
            ws.update(range_name=range_name, values=values)
            return True
        except Exception as e:
            print(f"Sheets update failed (attempt {attempt}/{max_attempts}): {e}")
            time.sleep(2 * attempt)
    return False

def fetch_performance_stats(date_from: str, date_to: str):
    import requests, os

    # === 1️⃣ Получаем токен ===
    token_url = "https://api-performance.ozon.ru/api/client/token"
    data = {
        "client_id": os.getenv("OZON_PERF_CLIENT_ID"),
        "client_secret": os.getenv("OZON_PERF_CLIENT_SECRET"),
        "grant_type": "client_credentials"
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    token_resp = requests.post(token_url, data=data, headers=headers, timeout=30)
    token_resp.raise_for_status()
    access_token = token_resp.json().get("access_token")
    print("✅ Performance API token получен")

    # === 2️⃣ Запрашиваем статистику по кампаниям ===
    stats_url = "https://api-performance.ozon.ru/api/client/statistics/campaign/media/json"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "dateFrom": date_from,
        "dateTo": date_to
    }

    r = requests.get(stats_url, headers=headers, params=params, timeout=60)
    if not r.text.strip():
        print(f"⚠️ Пустой ответ Performance API (status={r.status_code})")
        return {"ctr": 0, "shows": 0}

    try:
        data = r.json()
    except Exception:
        print(f"❌ Ошибка декодирования JSON Performance API: {r.status_code}, text={r.text[:300]}")
        return {"ctr": 0, "shows": 0}

    rows = data.get("rows", [])
    if not rows:
        print(f"⚠️ Нет данных Performance API за период {date_from}–{date_to}")
        return {"ctr": 0, "shows": 0}

    total_shows = sum(int(row.get("shows", 0)) for row in rows)
    total_clicks = sum(int(row.get("clicks", 0)) for row in rows)
    ctr = round(total_clicks / total_shows * 100, 2) if total_shows else 0

    print(f"✅ Performance (campaign-level): CTR={ctr}%, Показы={total_shows:,}")
    return {"ctr": ctr, "shows": total_shows}

def fetch_ad_spend_by_offer_ids(date_from: str, date_to: str):
    """
    Получает расходы на рекламу по каждому offer_id через Performance API.
    Возвращает dict {offer_id: ad_spent_float}
    """
    token = get_performance_access_token()
    if not token:
        return {}

    url = "https://api-performance.ozon.ru/api/client/statistics/expense/json"
    headers_perf = {"Authorization": f"Bearer {token}"}
    params = {"dateFrom": date_from, "dateTo": date_to}

    try:
        data = safe_request(url, headers=headers_perf, json=params, method="get", timeout=60)
    except Exception as e:
        print(f"❌ Ошибка при получении расходов: {e}")
        return {}

    result = {}
    for row in data.get("rows", []):
        title = str(row.get("title", "")).strip()
        parts = title.split()
        if not parts:
            continue
        offer_id = parts[0]
        spent = safe_number(row.get("moneySpent", 0))
        result[offer_id] = result.get(offer_id, 0) + spent
    return result

def update_sku_sheets():
    existing_sheets = {ws.title: ws for ws in spreadsheet.worksheets()}
    pause_sec = int(os.getenv("PAUSE_BETWEEN_SKU_SECONDS", "0"))

    # Получаем продажи по каждому SKU через finance/accrual (by-day)
    yesterday = target_date
    sales_by_sku = fetch_revenue_by_sku_analytics(yesterday, yesterday)
    # Получаем продажи по данным Finance для каждого SKU (для "Продажи, руб")
    finance_sales_by_sku = fetch_sku_sales_from_finance(yesterday)

    # Получаем ОВХ данные сразу для всех offer_id
    all_offer_ids = list(offer_to_sku.keys())
    ovh_data = {} if SKIP_SVD_OVH else fetch_ovh_data_by_offer_ids(all_offer_ids)
    # Получаем цены (marketing_price, min_price) для всех offer_id
    prices_data = fetch_prices_by_offer_ids(all_offer_ids)

    for idx, offer_id in enumerate(sku_list, start=1):
        sku_id = offer_to_sku.get(offer_id)
        if not sku_id:
            print(f"❌ Нет соответствия product_id для offer_id {offer_id}, пропускаем.")
            continue

        # 1) Получаем/создаём лист под оффер
        if offer_id in existing_sheets:
            ws = existing_sheets[offer_id]
        else:
            ws = spreadsheet.add_worksheet(title=offer_id, rows="100", cols="50")
            labels = [[label] for label in row_map_sku.keys()]
            sheets_update_with_retry(ws, f"A1:A{len(labels)}", labels)

        # --- Read row_labels and col_headers ONCE per sheet ---
        row_labels = ws.col_values(1)
        col_headers = ws.row_values(2)
        label_to_row = {str(lbl).strip(): idx+1 for idx, lbl in enumerate(row_labels)}
        def find_row(label):
            return label_to_row.get(label)

        # 2) Данные за вчера
        if idx == 1:
            update_sku_sheets.all_metrics = fetch_all_sku_metrics(
                [str(v) for v in offer_to_sku.values()],
                yesterday, yesterday
            )
        metrics = update_sku_sheets.all_metrics.get(str(sku_id), {})
        # Получаем реальные данные total из sales_by_sku по реальному sku Ozon
        metrics["total"] = sales_by_sku.get(str(sku_id), 0)

        # --- Определяем колонку для текущей даты (по возрастанию) ---
        from datetime import date

        col_headers = ws.row_values(2)[1:]  # пропускаем первую колонку (A)
        target_dt = date.fromisoformat(yesterday)

        existing_dates = []
        for val in col_headers:
            try:
                existing_dates.append(date.fromisoformat(val))
            except Exception:
                existing_dates.append(None)

        # Проверка: есть ли уже текущая дата
        for idx2, dt in enumerate(existing_dates, start=2):
            if dt == target_dt:
                next_col = idx2
                print(f"[DEBUG] Колонка {next_col} уже соответствует дате {yesterday}")
                break
        else:
            # Определяем, куда вставить новую дату — после самой поздней
            valid_dates = [d for d in existing_dates if d]
            last_date = max(valid_dates) if valid_dates else None

            if last_date and target_dt < last_date:
                # если дата раньше — вставляем перед ближайшей большей
                insert_pos = next(
                    i + 2 for i, d in enumerate(existing_dates) if d and d > target_dt
                )
            else:
                # если дата самая свежая — добавляем справа
                insert_pos = len(existing_dates) + 2

            num_rows = len(row_labels) + 5
            left_col = insert_pos - 1 if insert_pos > 2 else None
            right_col = insert_pos if insert_pos <= len(col_headers) + 1 else None

            formulas_to_copy = []
            for ref_col in [left_col, right_col]:
                if not ref_col:
                    continue
                try:
                    rng = f"{gspread.utils.rowcol_to_a1(3, ref_col)}:{gspread.utils.rowcol_to_a1(num_rows, ref_col)}"
                    formulas = ws.get(rng, value_render_option='FORMULA')
                    if formulas and any(row and any(cell for cell in row) for row in formulas):
                        formulas_to_copy = formulas
                        break
                except Exception:
                    continue

            ws.insert_cols(col=insert_pos, values=[[]])
            ws.update_cell(2, insert_pos, yesterday)
            next_col = insert_pos

            if formulas_to_copy:
                sheet_id = ws.id
                spreadsheet.batch_update({
                    "requests": [{
                        "copyPaste": {
                            "source": {
                                "sheetId": sheet_id,
                                "startColumnIndex": insert_pos - 2,
                                "endColumnIndex": insert_pos - 1,
                                "startRowIndex": 2,
                                "endRowIndex": num_rows
                            },
                            "destination": {
                                "sheetId": sheet_id,
                                "startColumnIndex": insert_pos - 1,
                                "endColumnIndex": insert_pos,
                                "startRowIndex": 2,
                                "endRowIndex": num_rows
                            },
                            "pasteType": "PASTE_FORMULA"
                        }
                    }]
                })

            print(f"[DEBUG] Безопасно вставляем новую дату {yesterday} в колонку {next_col} (формулы сохранены)")

        # --- Prepare batch_updates for batch_update ---
        batch_updates = []

        # Жестко задаём SKIP_LABELS по инструкции
        SKIP_LABELS = {
            "Средняя цена",
            "Конверсия из Уников в заказы",
            "Литраж",
            "Геогрефия продаж, шт",
            "ОВХ", "Ширина", "Высота", "Глубина", "Вес"
        }

        from gspread.utils import rowcol_to_a1

        # собираем все значения для обновления (каждое в отдельной строке)
        col_values = []
        for label, metric_key in row_map_sku.items():
            if label in SKIP_LABELS:
                continue
            row_num = find_row(label)
            if not row_num:
                continue
            if label == "Заказано на сумму":
                val = metrics.get("revenue", 0)
            else:
                val = metrics.get(metric_key, 0)
            col_values.append((row_num, val))

        # Для batch_update: обновляем блоками подряд идущих строк (чтобы не затирать формулы)
        if col_values:
            col_values_sorted = sorted(col_values)
            current_block = [col_values_sorted[0]]
            for (row_num, val) in col_values_sorted[1:]:
                if row_num == current_block[-1][0] + 1:
                    current_block.append((row_num, val))
                else:
                    # сохраняем предыдущий блок
                    min_row = current_block[0][0]
                    max_row = current_block[-1][0]
                    values_only = [[v] for (_, v) in current_block]
                    range_str = f"{rowcol_to_a1(min_row, next_col)}:{rowcol_to_a1(max_row, next_col)}"
                    batch_updates.append({"range": range_str, "values": values_only})
                    # начинаем новый блок
                    current_block = [(row_num, val)]
            # последний блок
            if current_block:
                min_row = current_block[0][0]
                max_row = current_block[-1][0]
                values_only = [[v] for (_, v) in current_block]
                range_str = f"{rowcol_to_a1(min_row, next_col)}:{rowcol_to_a1(max_row, next_col)}"
                batch_updates.append({"range": range_str, "values": values_only})

        # --- Вставка ОВХ (ширина, глубина, высота, вес) ---
        if not SKIP_SVD_OVH:
            try:
                ovh_labels = {
                    "Ширина": "width",
                    "Глубина": "depth",
                    "Высота": "height",
                    "Вес": "weight"
                }
                ovh = ovh_data.get(str(offer_id), {})
                ovh_updates = []
                for rus_label, api_key in ovh_labels.items():
                    row_num = find_row(rus_label)
                    if row_num:
                        val = ovh.get(api_key, 0)
                        ovh_updates.append((row_num, val))
                if ovh_updates:
                    ovh_updates_sorted = sorted(ovh_updates)
                    min_row = ovh_updates_sorted[0][0]
                    max_row = ovh_updates_sorted[-1][0]
                    ovh_values = [[v] for (_, v) in ovh_updates_sorted]
                    ovh_range = f"{rowcol_to_a1(min_row, next_col)}:{rowcol_to_a1(max_row, next_col)}"
                    batch_updates.append({"range": ovh_range, "values": ovh_values})
            except Exception as e:
                print(f"❌ Не удалось записать OVH для {offer_id}: {e}")
        else:
            print(f"[DEBUG] Пропускаем ОВХ для {offer_id} (SKIP_SVD_OVH=True)")

        # --- Вставка marketing_price и price ---
        if not SKIP_SVD_OVH:
            try:
                marketing_row = find_row("Средняя цена с учетом акций")
                min_row = find_row("Средняя цена с учетом акций и по карте клиента")
                price_rec = prices_data.get(str(offer_id), {})
                marketing_price = price_rec.get("marketing_price")
                regular_price = price_rec.get("price")
                price_updates = []
                if marketing_row:
                    price_updates.append((marketing_row, marketing_price if marketing_price is not None else ""))
                if min_row:
                    price_updates.append((min_row, regular_price if regular_price is not None else ""))
                if price_updates:
                    price_updates_sorted = sorted(price_updates)
                    min_price_row = price_updates_sorted[0][0]
                    max_price_row = price_updates_sorted[-1][0]
                    price_values = [[v] for (_, v) in price_updates_sorted]
                    price_range = f"{rowcol_to_a1(min_price_row, next_col)}:{rowcol_to_a1(max_price_row, next_col)}"
                    batch_updates.append({"range": price_range, "values": price_values})
            except Exception as e:
                print(f"❌ Не удалось записать цены для {offer_id}: {e}")
        else:
            print(f"[DEBUG] Пропускаем цены для {offer_id} (SKIP_SVD_OVH=True)")

        # --- Вставка расходов на рекламу ---
        try:
            if idx == 1:
                update_sku_sheets.ad_spend_data = fetch_ad_spend_by_offer_ids(yesterday, yesterday)
            ad_spend_data = getattr(update_sku_sheets, "ad_spend_data", {})
            ad_val = ad_spend_data.get(str(offer_id), 0)
            ad_row = find_row("Расходы на рекламу")
            if ad_row:
                batch_updates.append({
                    "range": f"{rowcol_to_a1(ad_row, next_col)}",
                    "values": [[ad_val]]
                })
        except Exception as e:
            print(f"❌ Не удалось записать расходы на рекламу для {offer_id}: {e}")

        # --- Вставка продаж по данным Finance (Продажи, руб) ---
        try:
            finance_row = find_row("Продажи, руб")
            if finance_row:
                sku_str = str(offer_to_sku.get(offer_id))
                finance_val = finance_sales_by_sku.get(sku_str, 0)
                batch_updates.append({
                    "range": f"{rowcol_to_a1(finance_row, next_col)}",
                    "values": [[finance_val]]
                })
        except Exception as e:
            print(f"❌ Не удалось записать Продажи, руб для {offer_id}: {e}")

        # --- Вставка географии продаж (динамическая) ---
        try:
            if idx == 1:
                update_sku_sheets.geo_sales = fetch_geography_sales(yesterday, yesterday)
            geo_sales = getattr(update_sku_sheets, "geo_sales", {})
            geo = geo_sales.get(str(offer_id), {})

            geo_start = find_row("География продаж, шт")
            geo_end = find_row("Прочее")
            if geo_start and geo_end and geo_end > geo_start:
                geo_rows = list(range(geo_start + 1, geo_end + 1))
                geo_values = []
                for row_num in geo_rows:
                    if row_num > len(row_labels):
                        continue
                    label = row_labels[row_num - 1].strip()
                    val = geo.get(label, 0)
                    geo_values.append([val])
                if geo_values:
                    geo_range = f"{rowcol_to_a1(geo_rows[0], next_col)}:{rowcol_to_a1(geo_rows[-1], next_col)}"
                    batch_updates.append({"range": geo_range, "values": geo_values})
            else:
                print(f"⚠️ География не найдена для {offer_id}")
        except Exception as e:
            print(f"❌ Не удалось записать географию продаж для {offer_id}: {e}")

        # --- Среднее время доставки (общее) и География СВД ---
        try:
            svd = {"avg": 0, "by_region": {}} if SKIP_SVD_OVH else fetch_delivery_speed_details(sku_id)
            avg_val = svd.get("avg", 0)
            region_vals = svd.get("by_region", {})

            # 1) Среднее время доставки, общее
            avg_row = find_row("Среднее время доставки, общее")
            if avg_row:
                batch_updates.append({
                    "range": f"{rowcol_to_a1(avg_row, next_col)}",
                    "values": [[avg_val]]
                })

            # 2) География СВД, ч
            geo_start = find_row("География СВД, ч")
            if geo_start:
                geo_rows, geo_values = [], []
                for i in range(geo_start + 1, len(row_labels) + 1):
                    region = row_labels[i - 1].strip()
                    if not region:
                        break
                    val = 0
                    for key, v in region_vals.items():
                        if key.strip().lower() == region.lower():
                            val = v
                            break
                    geo_rows.append(i)
                    geo_values.append([val])
                if geo_rows:
                    geo_range = f"{rowcol_to_a1(geo_rows[0], next_col)}:{rowcol_to_a1(geo_rows[-1], next_col)}"
                    batch_updates.append({"range": geo_range, "values": geo_values})

        except Exception as e:
            print(f"❌ Ошибка при записи СВД для {offer_id}: {e}")
        # --- Выполнить batch_update одним запросом ---
        if batch_updates:
            try:
                ws.batch_update([{"range": upd["range"], "values": upd["values"]} for upd in batch_updates])
                print(f"✅ Успешно записано {len(batch_updates)} диапазонов в колонку {next_col}")
            except Exception as e:
                print(f"❌ Ошибка batch_update для {offer_id}: {e}")
        print(f"✅ [{idx}/{len(sku_list)}] {offer_id} ({sku_id}): записан столбец {next_col}")
        import time
        time.sleep(pause_sec or 2)



def fetch_sku_sales_from_finance(date: str) -> dict:
    """
    Возвращает словарь {sku(str): сумма продаж ₽} за день.

    МИГРИРОВАНО с устаревающего /v3/finance/transaction/list на
    finance/accrual: продажи по SKU = Σ seller_price (finance_accrual.sales_by_sku).
    Ключи приводим к str — потребитель ниже ищет по str(sku).
    """
    sku_sales = {str(sku): rub for sku, rub in finance_accrual.sales_by_sku(date).items()}
    print(f"[Finance] Найдено {len(sku_sales)} SKU с продажами за {date}")
    return sku_sales


# --- Сумма продаж за день (accruals_for_sale) ---
def fetch_sales_total(date: str) -> float:
    """
    Суммарные продажи ₽ за день.

    МИГРИРОВАНО с устаревающего /v3/finance/transaction/totals на
    finance/accrual (sales_total = Σ seller_price по дню).
    """
    try:
        return safe_number(finance_accrual.sales_total(date))
    except Exception as e:
        print(f"❌ Ошибка fetch_sales_total (accrual): {e}")
        return 0.0


# --- Новый метод: Получение ОВХ данных по списку offer_id (через SKU) ---
def fetch_ovh_data_by_offer_ids(offer_ids):
    """
    Получает ширину, высоту, глубину и вес для списка offer_id через v4/product/info/attributes.
    ⚠️ ВАЖНО: запрос идёт по SKU, т.к. именно SKU стабильны, а offer_id могут иметь суффиксы ("П", "К", "ир" и т.п.).
    Используем offer_to_sku и product_to_offer для маппинга:
      offer_id -> sku  и  sku -> offer_id
    Возвращает словарь {offer_id: {"width":..., "height":..., "depth":..., "weight":...}}
    """
    url = "https://api-seller.ozon.ru/v4/product/info/attributes"

    # Собираем список SKU только для тех offer_id, что реально есть в словаре
    sku_list = []
    for oid in offer_ids:
        sku_val = offer_to_sku.get(oid)
        if sku_val:
            sku_list.append(str(sku_val))

    if not sku_list:
        print("⚠️ fetch_ovh_data_by_offer_ids: sku_list пуст, нечего запрашивать")
        return {}

    body = {
        "filter": {
            "sku": sku_list,
            "visibility": "ALL"
        },
        "limit": len(sku_list),
        "sort_dir": "ASC"
    }
    try:
        j = safe_request(url, headers=headers, json=body, method="post", timeout=120)
        items = j.get("result", [])
    except Exception as e:
        print(f"❌ Ошибка получения ОВХ: {e}")
        return {}

    ovh_data = {}
    for it in items:
        # SKU из ответа
        sku_val = it.get("sku")
        if not sku_val:
            continue

        # В product_to_offer ключи — это str(sku)
        offer_id = product_to_offer.get(str(sku_val))
        if not offer_id:
            # На всякий случай пробуем прямой обратный маппинг
            fallback_offer = {v: k for k, v in offer_to_sku.items()}.get(sku_val)
            offer_id = fallback_offer
        if not offer_id:
            continue

        entry = {
            "width": safe_number(it.get("width", 0)),
            "height": safe_number(it.get("height", 0)),
            "depth": safe_number(it.get("depth", 0)),
            "weight": safe_number(it.get("weight", 0)),
        }

        # fallback через attributes (если width/height/depth/weight пустые)
        for a in it.get("attributes", []):
            name = a.get("name", "").lower()
            values_list = a.get("values", [])
            if not values_list:
                continue
            val = safe_number(values_list[0].get("value", ""))
            if ("ширина" in name or "width" in name) and not entry["width"]:
                entry["width"] = val
            elif ("высота" in name or "height" in name) and not entry["height"]:
                entry["height"] = val
            elif ("глубина" in name or "depth" in name) and not entry["depth"]:
                entry["depth"] = val
            elif ("вес" in name or "weight" in name) and not entry["weight"]:
                entry["weight"] = val

        # Кладём по offer_id, т.к. дальше вся логика и таблицы работают с ним
        ovh_data[str(offer_id)] = entry

    return ovh_data


# --- География продаж (общая) для листа "общий" ---
# Ensure normalize_sku exists
def normalize_sku(value: str) -> str:
    import re
    if not value:
        return ""
    digits = re.findall(r"\d+", str(value))
    return digits[0] if digits else ""


def resolve_row_label_to_sku(row_label: str) -> str:
    """
    Приводит подпись строки в блоке SKU на листе "Свод" к реальному sku Ozon.
    Поддерживает как offer_id, так и product sku, включая текстовые подписи.
    """
    raw = str(row_label or "").strip()
    if not raw:
        return ""

    if raw in known_sku_ids:
        return raw

    if raw in offer_to_sku:
        return str(offer_to_sku[raw])

    norm = normalize_sku(raw)
    if not norm:
        return ""

    if norm in known_sku_ids:
        return norm

    if norm in offer_to_sku:
        return str(offer_to_sku[norm])

    return ""

def detect_offer_id_column(ws, sku_ids_set: set) -> int:
    col1 = ws.col_values(1)
    col2 = ws.col_values(2)

    def score(col):
        c = 0
        for v in col:
            k = normalize_sku(v)
            if k and k in sku_ids_set:
                c += 1
        return c

    s1 = score(col1)
    s2 = score(col2)
    chosen = 1 if s1 >= s2 else 2
    print(f"[DEBUG][SVOD] offer column = {chosen} (A={s1}, B={s2})")
    return chosen

def fetch_avg_price_from_fbo_list(date_str: str) -> dict:
    """
    Возвращает {sku: avg_price} за дату date_str
    (по дате доставки client_delivery_date_begin).
    Работает БЕЗ fbo/get — только через fbo/list.
    """

    url = "https://api-seller.ozon.ru/v3/posting/fbo/list"
    sums = {}  # sku -> {"qty": int, "rev": float}
    cursor = ""

    while True:
        body = {
            "sort_dir": "ASC",
            "filter": {
                "since": f"{date_str}T00:00:00.000Z",
                "to": f"{date_str}T23:59:59.999Z",
            },
            "limit": 100,
            "cursor": cursor,
            "translit": True,
            "with": {
                "analytics_data": True,
                "financial_data": False,
                "legal_info": False
            }
        }

        data = safe_request(url, headers=headers, json=body, method="post", timeout=60)
        postings = data.get("postings", [])

        if not isinstance(postings, list):
            print("[AVG] result не list — стоп")
            break
        if not postings:
            break

        for posting in postings:
            # --- Filter by actual sale/creation date ---


            for prod in posting.get("products", []):
                sku = normalize_sku(str(prod.get("sku")))
                # --- Offer ID and mapping logic ---
                offer_id_raw = prod.get("offer_id", "")
                offer_norm = normalize_sku(offer_id_raw)
                my_sku = offer_norm  # default if direct match
                # map offer_norm to product id if exists
                if offer_norm in offer_to_sku:
                    my_sku = str(offer_to_sku[offer_norm])
                else:
                    # reverse map if product_id matches
                    if sku in offer_to_sku.values():
                        for k, v in offer_to_sku.items():
                            if str(v) == sku:
                                my_sku = k
                                break
                # normalize final key
                my_sku = normalize_sku(my_sku)
                sku = my_sku

                qty = int(prod.get("quantity", 0))
                price_raw = prod.get("price", 0)
                price = float(price_raw.get("amount", 0) if isinstance(price_raw, dict) else price_raw or 0)

                if qty <= 0:
                    continue

                if sku not in sums:
                    sums[sku] = {"qty": 0, "rev": 0.0}

                sums[sku]["qty"] += qty
                sums[sku]["rev"] += qty * price

        cursor = data.get("cursor", "")
        if not data.get("has_next", False):
            break

    avg = {}
    for sku, d in sums.items():
        avg[sku] = d["rev"] / d["qty"] if d["qty"] > 0 else 0.0

    # === LOGGING START ===
    print("---------- AVG PRICE DEBUG ----------")
    print(f"Дата: {date_str}")
    print(f"Всего SKU в sums: {len(sums)}")

    total_qty = sum(d['qty'] for d in sums.values())
    total_rev = sum(d['rev'] for d in sums.values())
    print(f"Суммарно qty: {total_qty}, revenue: {total_rev}")

    for sku, d in sums.items():
        try:
            avg_val = d['rev'] / d['qty'] if d['qty'] else 0
        except Exception:
            avg_val = 0
        print(f"SKU {sku}: qty={d['qty']}, rev={d['rev']}, avg={avg_val}")

    print("-------------- END ------------------")
    # === LOGGING END ===

    return avg

def fetch_general_geography_units(date_from: str, date_to: str):
    """
    Собирает количество заказанных единиц по регионам (без фильтра по SKU)
    из /v3/posting/fbo/list. Возвращает кортеж:
      (units_by_region: dict[str,int], shares_by_region: dict[str,float])
    Где shares_by_region — доля региона от общего количества (0..1).
    """
    url = "https://api-seller.ozon.ru/v3/posting/fbo/list"
    cursor = ""
    units_by_region = {region: 0 for region in region_to_warehouses.keys()}
    units_by_region["Прочее"] = 0

    while True:
        body = {
            "sort_dir": "ASC",
            "filter": {
                "since": f"{date_from}T00:00:00.000Z",
                "to": f"{date_to}T23:59:59.000Z"
            },
            "limit": 100,
            "cursor": cursor,
            "with": {
                "analytics_data": True,
                "financial_data": False,
                "legal_info": False
            }
        }
        try:
            j = safe_request(url, headers=headers, json=body, method="post", timeout=120)
            data = j.get("postings", [])
        except Exception as e:
            print(f"❌ Ошибка fbo/list (общая география): {e}")
            break

        if not data:
            break

        for posting in data:
            analytics = posting.get("analytics_data", {}) or {}
            wh_id = str(analytics.get("warehouse_id", ""))

            # Определяем регион
            region_name = None
            for region, wh_list in region_to_warehouses.items():
                if wh_id in wh_list:
                    region_name = region
                    break
            if not region_name:
                region_name = "Прочее"

            # Суммируем количество товаров в постинге
            qty_sum = 0
            for pr in posting.get("products", []):
                q = pr.get("quantity") or 1
                try:
                    q = int(q)
                except Exception:
                    q = 1
                qty_sum += q

            units_by_region[region_name] = units_by_region.get(region_name, 0) + qty_sum

        cursor = j.get("cursor", "")
        if not j.get("has_next", False):
            break
        time.sleep(0.2)

    total_units = sum(units_by_region.values())
    shares_by_region = {}
    for region, qty in units_by_region.items():
        shares_by_region[region] = (qty / total_units) if total_units > 0 else 0.0

    return units_by_region, shares_by_region

# --- Новый вызов: update_general ---
def update_general():
    # Получить или создать лист "общий"
    try:
        ws = spreadsheet.worksheet("Свод")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="Свод", rows="100", cols="50")

    # Определить дату (вчерашнюю) как строку
    yesterday = target_date

    # Получить сводные метрики из общего daily total Analytics API.
    # Уникальных посетителей нельзя корректно суммировать по SKU из-за дублей.
    values = fetch_all_metrics(analytics_metrics, yesterday, yesterday)
    # Продажи (через finance/accrual by-day)
    realization_total = fetch_sales_total(yesterday)
    values["total"] = realization_total
    # Средняя цена
    delivered_units = values.get("delivered_units", 0)
    avg_price = realization_total / delivered_units if delivered_units > 0 else 0
    values["avg_price"] = avg_price
    # Расходы на рекламу
    ad_spend = fetch_ad_spend_yesterday()
    values["ad_spend"] = ad_spend
    # ---- Средняя цена по SKU ----
    real_avg_prices = fetch_avg_price_from_fbo_list(yesterday)
    # Получаем дату для записи


    # Определяем следующую свободную колонку (вставка даты по возрастанию)
    from datetime import date

    col_headers = ws.row_values(2)[1:]  # пропускаем первую колонку
    target_dt = date.fromisoformat(yesterday)

    existing_dates = []
    for val in col_headers:
        try:
            existing_dates.append(date.fromisoformat(val))
        except Exception:
            existing_dates.append(None)

    # Проверка: есть ли уже дата
    for idx, dt in enumerate(existing_dates, start=2):
        if dt == target_dt:
            next_col = idx
            print(f"[DEBUG] Колонка {next_col} уже соответствует дате {yesterday}")
            break
    else:
        # --- Безопасная вставка новой даты с сохранением формул слева/справа ---
        insert_pos = len(existing_dates) + 2
        for i, dt in enumerate(existing_dates):
            if dt and dt > target_dt:
                insert_pos = i + 2
                break

        num_rows = len(ws.col_values(1)) + 5
        left_col = insert_pos - 1 if insert_pos > 2 else None
        right_col = insert_pos if insert_pos <= len(existing_dates) + 1 else None

        formulas_to_copy = []
        for ref_col in [left_col, right_col]:
            if not ref_col:
                continue
            try:
                rng = f"{gspread.utils.rowcol_to_a1(3, ref_col)}:{gspread.utils.rowcol_to_a1(num_rows, ref_col)}"
                formulas = ws.get(rng, value_render_option='FORMULA')
                if formulas and any(row and any(cell for cell in row) for row in formulas):
                    formulas_to_copy = formulas
                    break
            except Exception:
                continue

        ws.insert_cols(col=insert_pos, values=[[]])
        ws.update_cell(2, insert_pos, yesterday)
        next_col = insert_pos

        if formulas_to_copy:
            sheet_id = ws.id
            spreadsheet.batch_update({
                "requests": [{
                    "copyPaste": {
                        "source": {
                            "sheetId": sheet_id,
                            "startColumnIndex": insert_pos - 2,
                            "endColumnIndex": insert_pos - 1,
                            "startRowIndex": 2,
                            "endRowIndex": num_rows
                        },
                        "destination": {
                            "sheetId": sheet_id,
                            "startColumnIndex": insert_pos - 1,
                            "endColumnIndex": insert_pos,
                            "startRowIndex": 2,
                            "endRowIndex": num_rows
                        },
                        "pasteType": "PASTE_FORMULA"
                    }
                }]
            })

        print(f"[DEBUG] Безопасно вставляем новую дату {yesterday} в колонку {next_col} (формулы сохранены)")
    # Получаем список названий метрик из колонки A
    row_labels = ws.col_values(1)
    # Если пусто — записать row_labels
    if not row_labels or all(not r for r in row_labels):
        row_labels = list(row_map.keys())
        ws.update(f"A3:A{len(row_labels)+2}", [[lbl] for lbl in row_labels])
    # Обновляем только строки, которые есть в row_map (по названиям метрик)
    row_labels = ws.col_values(1)
    for i, label in enumerate(row_labels, start=1):
        # Пропускаем строки 78–113
        if 78 <= i <= 113:
            continue
        metric_key = row_map.get(label)
        if metric_key:
            val = values.get(metric_key, "")
            try:
                ws.update_cell(i, next_col, val)
                time.sleep(1)  # пауза для обхода лимита Google Sheets API
            except Exception as e:
                print(f"❌ Ошибка обновления строки {i} ({label}): {e}")

    # ---- Заполнение блока "Средняя цена СКЮ" ----
    row_labels = ws.col_values(1)
    if "Средняя цена СКЮ" in row_labels:
        start_row = row_labels.index("Средняя цена СКЮ") + 2
        sku_ids_set = known_offer_ids | known_sku_ids
        offer_col = detect_offer_id_column(ws, sku_ids_set)
        offer_col_values = ws.col_values(offer_col)
        end_row = start_row

        while end_row <= len(row_labels):
            label_a = row_labels[end_row - 1].strip() if end_row - 1 < len(row_labels) else ""
            label_offer = offer_col_values[end_row - 1].strip() if end_row - 1 < len(offer_col_values) else ""

            if label_a in {"Среднее время доставки, общее", "География продаж, шт"}:
                break
            if not label_a and not label_offer:
                break
            end_row += 1

        avg_values = []

        for row_idx in range(start_row, end_row):
            label_a = row_labels[row_idx - 1].strip() if row_idx - 1 < len(row_labels) else ""
            label_offer = offer_col_values[row_idx - 1].strip() if row_idx - 1 < len(offer_col_values) else ""

            sku_id = resolve_row_label_to_sku(label_offer) or resolve_row_label_to_sku(label_a)
            if not sku_id:
                avg_values.append([""])
                continue

            offer_id = product_to_offer.get(sku_id, "")
            val = real_avg_prices.get(sku_id)
            if val is None and offer_id:
                val = real_avg_prices.get(normalize_sku(offer_id))

            avg_values.append([val if val is not None else ""])

        if avg_values:
            start_a1 = gspread.utils.rowcol_to_a1(start_row, next_col)
            end_a1 = gspread.utils.rowcol_to_a1(end_row - 1, next_col)
            ws.update(f"{start_a1}:{end_a1}", avg_values)
            print(f"[AVG] ✓ Средняя цена СКЮ обновлена диапазоном {start_a1}:{end_a1} (col={offer_col})")
        else:
            print("[AVG] ⚠ Блок 'Средняя цена СКЮ' найден, но строки SKU не распознаны")
    else:
        print("[AVG] ⚠ Не найден заголовок 'Средняя цена СКЮ' на листе!")

    # --- Среднее время доставки, общее (через summary) ---
    try:
        if not SKIP_SVD_OVH:
            svd_summary = fetch_delivery_speed_summary()
            svd_avg = svd_summary.get("avg", 0)
            def find_row(label):
                row_labels_inner = ws.col_values(1)
                for idx, lbl in enumerate(row_labels_inner, start=1):
                    if str(lbl).strip() == label:
                        return idx
                return None
            svd_row = find_row("Среднее время доставки, общее")
            if svd_row:
                ws.update_cell(svd_row, next_col, svd_avg)
            print(f"✅ Среднее время доставки (общее) обновлено: {svd_avg} ч")
        else:
            print("⏭️ Пропуск обновления СВД (SKIP_SVD_OVH=True)")
    except Exception as e:
        print(f"❌ Ошибка при записи общего СВД: {e}")

    # --- География продаж (шт) для общего листа ---
    try:
        units_by_region, shares_by_region = fetch_general_geography_units(yesterday, yesterday)
        all_labels = ws.col_values(1)
        geo_start = None
        geo_end = None
        for i, label in enumerate(all_labels, start=1):
            if str(label).strip().lower().startswith("география продаж"):
                geo_start = i + 1
            elif geo_start and not str(label).strip():
                geo_end = i - 1
                break
        if not geo_end and geo_start:
            geo_end = len(all_labels)

        if geo_start and geo_end and geo_end >= geo_start:
            geo_labels = all_labels[geo_start - 1:geo_end]
            geo_values = []
            for label in geo_labels:
                region = str(label).strip()
                qty = int(units_by_region.get(region, 0))
                geo_values.append([qty])
            ws.update(
                range_name=f"{gspread.utils.rowcol_to_a1(geo_start, next_col)}:{gspread.utils.rowcol_to_a1(geo_end, next_col)}",
                values=geo_values
            )
        else:
            print("⚠️ Не удалось определить диапазон географии в 'Свод'")
    except Exception as e:
        print(f"❌ Не удалось записать географию продаж (общий): {e}")

    # ===== REVENUE ТОЛЬКО В SKU-БЛОКЕ "Данные SKU по продажам" =====

    sales_by_sku = fetch_revenue_by_sku_analytics(yesterday, yesterday)

    row_labels = ws.col_values(1)

    try:
        start = row_labels.index("Данные SKU по продажам") + 1
    except ValueError:
        raise RuntimeError("❌ Не найден блок 'Данные SKU по продажам' в Своде")

    # ищем конец блока (пустая строка или следующий заголовок)
    end = start
    while end < len(row_labels):
        label = row_labels[end].strip()
        if not label:
            break
        if label.lower().startswith(("процент", "средн", "итог", "географ")):
            break
        end += 1

    from gspread.utils import rowcol_to_a1

    updates = []
    revenue_sum = 0.0

    for row_idx in range(start + 1, end + 1):
        offer_raw = row_labels[row_idx - 1]
        sku_id = resolve_row_label_to_sku(offer_raw)
        if not sku_id:
            continue

        val = float(sales_by_sku.get(sku_id, 0.0))
        revenue_sum += val
        updates.append({
            "range": rowcol_to_a1(row_idx, next_col),
            "values": [[val]]
        })

    if updates:
        ws.batch_update(updates)

    print(f"[SVOD] ✓ revenue записан ТОЛЬКО в SKU-блок ({len(updates)} строк)")
    print(f"[SVOD] Σ revenue SKU-блока = {revenue_sum:.2f}; общий 'Заказано на сумму' = {safe_number(values.get('revenue')):.2f}")
    # ============================================================

    try:
        # --- География СВД, ч ---
        if not SKIP_SVD_OVH:
            geo_data = fetch_delivery_speed_geo()

            geo_start = find_row("География СВД, ч")
            if geo_start:
                rows, vals = [], []
                for i in range(geo_start + 1, len(row_labels) + 1):
                    region = row_labels[i - 1].strip()
                    if not region:
                        break
                    val = geo_data.get(region, 0)
                    rows.append(i)
                    vals.append([val])

                if rows:
                    ws.update(
                        range_name=f"{gspread.utils.rowcol_to_a1(rows[0], next_col)}:{gspread.utils.rowcol_to_a1(rows[-1], next_col)}",
                        values=vals
                    )

            print("✅ География СВД обновлена по данным API /average-delivery-time")
        else:
            print("⏭️ Пропуск обновления СВД (SKIP_SVD_OVH=True)")
    except Exception as e:
        print(f"❌ Ошибка при записи географии СВД: {e}")


    perf_data = fetch_performance_stats(yesterday, yesterday)
    ctr_val = perf_data["ctr"]
    shows_val = perf_data["shows"]

# update_sku_sheets()
# update_general()

if __name__ == "__main__":
    update_general()
    update_sku_sheets()
