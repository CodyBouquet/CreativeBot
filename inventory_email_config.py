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

# Lead time per vendor (days from PO placed to available on shelf).
# Keys match the VENDOR field returned by /purchaseorderlines.
# Pre-populated from your PO history — just edit the day values.
# Anything not listed uses DEFAULT_LEAD_TIME_DAYS.
VENDOR_LEAD_TIMES = {
    "ALLSURF":  7,    # most PO history is special orders; stocking items ship ~1 week
    "ALTRO":    14,
    "DALTILE":  14,
    "DDCC":     14,
    "DIXIE":    14,
    "DREAM":    14,
    "FABRICA":  14,
    "FIRSTGRA": 14,
    "FLORSTAR": 14,
    "GLASSTIL": 14,
    "HAPPY":    14,
    "HAPPYFEE": 14,
    "HERREGAN": 14,
    "HOME":     14,
    "INTERFAC": 14,
    "JAEDIS":   14,
    "JUNCKERS": 14,
    "KANE":     14,
    "KARNDEAN": 14,
    "MASLAND":  14,
    "MIRAGE":   14,
    "MOHAWK":   14,
    "MSI":      14,
    "NOURISON": 14,
    "PHOENTIL": 14,
    "PREVERCO": 14,
    "ROCA":     14,
    "SHAW":     14,
    "STANTON":  14,
    "SURYA":    14,
    "VERSATRI": 14,
    "VIRGINIA": 14,
}
DEFAULT_LEAD_TIME_DAYS = 14

# Trailing window for SOLD_1YR, weekly sigma, PEAK_WK
DEMAND_WINDOW_DAYS = 365

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

# ---- Output ----
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "safety_stock_items.txt",
)
