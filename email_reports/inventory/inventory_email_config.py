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

# General (non-pad) reorder policy — days-of-supply on average demand
# (daily_demand = SOLD_1YR / DEMAND_WINDOW_DAYS), ceil to box. Replaces the older
# busy-month model. Pad has its own cadence below (PAD_*_DAYS). The busy-month
# figure is still computed and shown in the .txt as context (peak 30-day demand),
# but no longer drives the recommendation.
GENERAL_SAFETY_DAYS = 7        # safety floor (~1 week of demand)
GENERAL_REORDER_DAYS = 21      # ORDER NOW when inventory position drops to ~3 weeks
GENERAL_ORDER_UP_TO_DAYS = 35  # restock target / max held (~5 weeks)

# PAD items (carpet padding/cushion) take a large warehouse footprint AND arrive
# on a ~weekly truck, so the busy-month model is the wrong basis entirely. Instead
# size them to a days-of-supply target keyed to that delivery cadence: keep ~2
# weeks max, reorder when ~12 days remain, safety floor ~1 week. All three are
# daily_demand (SOLD_1YR/365) × the day count, ceil to box.
#
# Identified by BMS product code (CAT_PRODCODE from the catalog cache; /productstock
# PRODCODE only as a fallback when the catalog left it blank). Pad spans MORE THAN
# ONE code: "18" (e.g. FIRM GRIP rug pad) and "az" (the cushion rolls — GLACIER,
# EMERALD, TITAN — plus heavier roll goods CC files under the same code). Any SKU
# whose code is in this set is treated as pad and counts toward the roll cap.
PAD_PRODCODES = frozenset({"18", "az"})

# Catalog sequences to force OUT of pad handling even though their product code is
# in PAD_PRODCODES. FIRM GRIP (CAT_SEQUENCE 0000000232689) is an area rug pad sold
# per square yard, not a roll good — it shouldn't be sized as pad or charged against
# the roll capacity. Excluded SKUs fall back to the normal busy-month model.
PAD_EXCLUDE_SEQS = frozenset({"0000000232689"})  # FIRM GRIP - AREA RUG PAD
PAD_SAFETY_DAYS = 7       # safety floor (days of avg demand)
PAD_REORDER_DAYS = 12     # ORDER NOW when inventory position drops to this many days
PAD_ORDER_UP_TO_DAYS = 14 # replenish target / max held (~2 weeks, one+ truck cycle)

# Total physical warehouse capacity for carpet pad, in ROLLS, shared across every
# pad SKU. The days-of-supply targets above are sized per SKU and can sum past what
# the building holds, so when the combined pad order-up-to (converted SY→rolls via
# each SKU's scraped roll_sy) exceeds this, the budget is allocated proportional to
# demand — fast movers keep the most space, slow movers absorb the cut.
PAD_TOTAL_ROLL_CAPACITY = 288
# Fallback roll size (SY/roll) when a pad SKU's yardage can't be scraped from its
# catalog text. Most pad rolls are 30 SY.
PAD_DEFAULT_ROLL_SY = 30.0

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
