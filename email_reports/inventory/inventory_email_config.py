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
# Service-level Z-score. Higher Z = bigger safety buffer.
#   95% fill = 1.65   97% fill = 1.88   99% fill = 2.33
SERVICE_LEVEL_Z = 1.88

# Lead time assumption (days from PO placed to available on shelf). Stocking
# items run ~1–2 weeks regardless of vendor, so we assume a flat 7–14 day window
# and use its midpoint in the reorder-point math rather than tracking per-vendor
# times. Bump this if deliveries trend slower.
ASSUMED_LEAD_TIME_DAYS = 10

# Trailing window for SOLD_1YR and the busy-month figure.
DEMAND_WINDOW_DAYS = 365

# Safety-stock cap. The raw busy_month (peak rolling-30-day shipped qty) can be
# blown up by a single one-off project, so we cap the *effective* busy month used
# for safety / reorder / order-up-to at this many months of AVERAGE demand
# (SOLD_1YR / 12 × SAFETY_CAP_MONTHS). One month aligns with the ~1–2 week restock
# ability — no need to hoard a freak peak you can replenish quickly.
SAFETY_CAP_MONTHS = 1.0

# Lower bound on order history we pull for UNASSIGN / PEAK_WK / bulk orderline
ORDER_HISTORY_FLOOR = "20240101"

# Reorder period (days) — how much demand one PO should cover.
# REC_QTY = daily_demand * REORDER_PERIOD_DAYS, rounded up to box multiples.
REORDER_PERIOD_DAYS = 30

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
