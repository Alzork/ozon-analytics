# Copy this file to constants.py and fill it with your real values.
# constants.py is gitignored on purpose: it holds your product catalog
# (SKUs, offer/product-id mappings, warehouse ids), which is private data.
# Below is the structure with synthetic sample values.

# Ozon warehouse ids grouped by region/city.
region_to_warehouses = {
    "City A": ["1000000000001", "1000000000002"],
    "City B": ["1000000000003"],
}

# Ozon cluster ids by city (public Ozon clusters).
clusters_id = {
    "City A": 1,
    "City B": 2,
}

# Your product article list (offer ids as strings).
sku_list = [
    "1001", "1002", "1003",
]

# Map: your offer id (string) -> Ozon SKU (int).
offer_to_sku = {
    "1001": 100000001,
    "1002": 100000002,
}

# Map: your offer id (string) -> Ozon product id (int).
clean_offer_to_product_id = {
    "1001": 200000001,
    "1002": 200000002,
}

# Ozon analytics metric keys (public API metric names). Safe to keep as-is.
analytics_metrics = [
    "session_view",          # unique visitors total
    "session_view_search",   # unique visitors from search/catalog
    "session_view_pdp",      # unique visitors on product page
    "revenue",               # ordered amount
    "ordered_units",         # ordered units
    "conv_tocart",           # conversion to cart, total
    "conv_tocart_pdp",       # conversion to cart from product page
    "conv_tocart_search",    # conversion to cart from search/catalog
    "hits_view",             # impressions total
    "hits_view_pdp",         # impressions on product page
    "hits_view_search",      # impressions in search/catalog
    "position_category",     # position in category
    "hits_tocart",           # adds to cart total
    "delivered_units",       # delivered units
]

# Map: Russian column label in the analytics export -> metric key.
# Fill with the exact labels your Ozon export uses.
row_map_sku = {
    "Заказано на сумму": "revenue",
    "Заказано товаров": "ordered_units",
    # ...
}

row_map = {
    "Уникальные посетители, всего": "session_view",
    "Заказано на сумму": "revenue",
    "Заказано товаров": "ordered_units",
    # ...
}
