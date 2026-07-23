"""
Configuration for inventory_email.py.

BMS credentials live in .env (BMS_API_KEY, BMS_USERNAME, BMS_PASSWORD).
Everything else sits here.
"""
import os

# ---- BMS connection ----
BMS_ALIAS = "creativecarpets"
BMS_COMPANY = "99"

# ---- Notify policy ----
# A stocked SKU (CAT_SAFTYSTK > 0) is evaluated against the thresholds ENTERED IN
# BMS — nothing is computed. Using available balance = on_hand − reserved
# (/productstock AVAILABLE_FLOAT):
#   NOTIFY when available < reorder point (CAT_REORDER), OR below safety stock.
#   CRITICAL (red, top of list) when available < safety stock (CAT_SAFTYSTK).
# The OR keeps a below-safety SKU visible even when its reorder point is still
# unset (0) in BMS — a set reorder point is always >= safety stock.

# ---- Stocked-catalog cache (full /catalogitems scan) ----
# The reorder report evaluates EVERY stocked SKU (CAT_SAFTYSTK > 0), not just the
# items currently below safety stock. /catalogitems has no per-item filter and is
# ~1M rows, so a separate weekly job (catalog_scan.py) walks the whole catalog and
# caches the stocked universe here; the report reads this cache and falls back to
# /lowstock if it is missing. Delete the file (or let it age out) to force a rescan.
STOCKED_CATALOG_CACHE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".stocked_catalog_cache.json",
)
STOCKED_CATALOG_MAX_AGE_DAYS = 7
# Pages fetched concurrently per batch. Concurrency 20 read-timed-out mid-catalog;
# 6 stays under the API's sustained-pagination throttle.
CATALOG_SCAN_BATCH = 6

# ---- Output ----
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "safety_stock_items.txt",
)
