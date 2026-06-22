"""
Configuration for inventory_email.py.

BMS credentials live in .env (BMS_API_KEY, BMS_USERNAME, BMS_PASSWORD).
Everything else sits here.
"""
import os

# ---- BMS connection ----
BMS_ALIAS = "creativecarpets"
BMS_COMPANY = "99"

# ---- Algorithm parameters ----
# Trailing window for SOLD_1YR and the busy-month figure.
DEMAND_WINDOW_DAYS = 365

# General reorder policy — days-of-supply on average demand
# (daily_demand = SOLD_1YR / DEMAND_WINDOW_DAYS), ceil to box. Applies to every
# item NOT in the manual-safety group below. The busy-month figure is still
# computed and shown in the .txt as context (peak 30-day demand), but no longer
# drives the recommendation.
GENERAL_SAFETY_DAYS = 7        # safety floor (~1 week of demand)
GENERAL_REORDER_DAYS = 21      # ORDER NOW when inventory position drops to ~3 weeks
GENERAL_ORDER_UP_TO_DAYS = 35  # restock target / max held (~5 weeks)

# Manual-safety group — product codes whose stock is managed by hand in BMS. For
# these we DON'T compute a reorder point or suggest an order qty; we simply notify
# when stock falls below the manually entered safety stock (CAT_SAFTYSTK): once on
# inventory position, and again (more urgently) on physical on-hand. Identified by
# BMS product code (CAT_PRODCODE from the catalog cache; /productstock PRODCODE only
# as a fallback when the catalog left it blank):
#   "az" = carpet cushion rolls (GLACIER, EMERALD, MILLENIA, TITAN, VALENCIA)
#   "19" = trim pieces (BURKE-MERCER T-CAP, METAL EDGE)
MANUAL_SAFETY_PRODCODES = frozenset({"19", "az"})

# Catalog sequences forced OUT of the manual-safety group even if their code matches
# — they fall back to the general days-of-supply policy. FIRM GRIP (0000000232689)
# is an area rug pad sold per square yard, managed like normal stock.
MANUAL_SAFETY_EXCLUDE_SEQS = frozenset({"0000000232689"})  # FIRM GRIP - AREA RUG PAD

# Lower bound on order history we pull for UNASSIGN / PEAK_WK / bulk orderline
ORDER_HISTORY_FLOOR = "20240101"

# Box-quantity cache. Scanning /catalogitems for CAT_UNIT_PER_BOX takes ~2 min,
# so we cache the result here and refresh weekly. Delete the file to force refresh.
BOX_QTY_CACHE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".box_qty_cache.json",
)
BOX_QTY_CACHE_MAX_AGE_DAYS = 7

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
