"""
BMS stocked-SKU reorder report (busy-month policy).

Pulls demand + supply data from the Rollmaster (Broadlume BMS) API and evaluates
EVERY stocked SKU (CAT_SAFTYSTK > 0), not just the ones currently below safety —
so thresholds set too low get caught. The stocked universe comes from the weekly
catalog_scan.py cache (cfg.STOCKED_CATALOG_CACHE), falling back to /lowstock when
that cache is missing/stale.

Reorder policy (per SKU), all stock figures rounded up to CAT_UNIT_PER_BOX:
    busy_month   = peak rolling-30-day shipped qty over the trailing window
    busy_eff     = min(busy_month, SOLD_1YR/12 × SAFETY_CAP_MONTHS)   # cap one-off spikes
    safety       = busy_eff                          # keep one (capped) busy month
    reorder      = busy_eff + daily_demand × lead_time   # lead_time = flat 7–14d assumption
    order_up_to  = 2 × busy_eff
    inv_position = on_hand + on_po − backorders(unassign)
    ORDER NOW    when inv_position ≤ reorder
    suggested    = ceil_to_box(max(0, order_up_to − inv_position))

Columns (.txt full audit):
    SEQUENCE   CAT_SEQUENCE
    VENDOR     PO-history vendor, else catalog CAT_VENDORID
    ON_HAND    sum of current rolls (/productstock ONHAND_FLOAT)
    UNASN      open-order demand not yet allocated to rolls (= backorders)
    ON_PO      open PO QTYORD minus QTYREC
    INV_POS    inventory position = ON_HAND + ON_PO − UNASN
    SOLD_1YR   sum of IVL_SQUAN on invoices dated within DEMAND_WINDOW_DAYS
    BUSY_MO    peak rolling-30-day shipped qty (the policy anchor)
    BOX        CAT_UNIT_PER_BOX — round recommendations up to multiples of this
    SAF_CUR    current safety stock (catalog CAT_SAFTYSTK, or /lowstock fallback)
    SAF_REC    recommended safety = busy_eff (busy_month capped at
               SAFETY_CAP_MONTHS months of avg demand), ceil to box
    ROP_REC    recommended reorder = busy_month + daily_demand × lead_time, ceil to box
    UPTO       order-up-to level = 2 × busy_month, ceil to box
    QTY_REC    suggested order = max(0, UPTO − INV_POS), ceil to box
    ORDER      "YES" when INV_POS ≤ ROP_REC

Usage:
    ./venv/bin/python inventory_email.py
"""
import json
import math
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from tqdm import tqdm

try:
    from . import inventory_email_config as cfg  # imported as email_reports.inventory.inventory_email
except ImportError:
    import inventory_email_config as cfg  # run directly: python email_reports/inventory/inventory_email.py

load_dotenv()

API_KEY  = os.environ["BMS_API_KEY"]
USERNAME = os.environ["BMS_USERNAME"]
PASSWORD = os.environ["BMS_PASSWORD"]
BASE_URL = "https://api.rmaster.com/api"
ALIAS    = cfg.BMS_ALIAS
COMPANY  = cfg.BMS_COMPANY


# ---- Helpers ---------------------------------------------------------------

def _f(x):
    """Coerce a possibly-messy BMS value to float; blank/None/unparseable becomes 0.0."""
    try:
        return float(str(x).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(s):
    """Parse an 8-digit BMS date, trying both YYYYMMDD and MMDDYYYY and keeping whichever gives a plausible year. Returns a datetime or None."""
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit() or s == "00000000":
        return None
    # BMS mixes YYYYMMDD and MMDDYYYY; pick whichever yields a plausible year
    for fmt in ("%Y%m%d", "%m%d%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            if 2000 <= dt.year <= 2099:
                return dt
        except ValueError:
            continue
    return None


def _parse_iso(s):
    """Parse an ISO-8601 timestamp (e.g. the cache's scanned_at) to a datetime, or None."""
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _rolling_30day_max(events):
    """
    Largest total shipped quantity inside any 30-day window of `events`.

    `events` is a date-sorted list of (date, qty). We slide a 30-day window whose
    left edge sits on each event in turn and sum every event within
    [d, d + 30 days). The maximum such sum is the SKU's "busy month" — the peak
    rolling-30-day demand the policy keeps on hand as safety stock. Because the
    list is sorted, the window's right edge only ever moves forward, so the whole
    scan is a single linear two-pointer pass. Returns 0.0 for an empty history.
    """
    if not events:
        return 0.0
    window = timedelta(days=30)
    best = running = 0.0
    j = 0
    n = len(events)
    for i in range(n):
        # Extend the right edge to include every event within 30 days of events[i].
        while j < n and events[j][0] < events[i][0] + window:
            running += events[j][1]
            j += 1
        best = max(best, running)
        # Slide the left edge off events[i] before advancing to events[i+1].
        running -= events[i][1]
    return best


def _ceil_to_box(value, box_qty):
    """Round up to the next multiple of box_qty. box_qty <= 0 means no boxes → ceil to int."""
    if value <= 0:
        return 0.0
    q = box_qty if box_qty and box_qty > 0 else 1
    return math.ceil(value / q) * q


def authenticate():
    """Exchange BMS credentials for an API token; raises if no TOKEN comes back."""
    r = requests.post(
        f"{BASE_URL}/{ALIAS}/token",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "x-api-key": API_KEY,
        },
        data={"username": USERNAME, "password": PASSWORD, "granttype": "application"},
        timeout=30,
    )
    r.raise_for_status()
    token = r.json().get("TOKEN")
    if not token:
        raise RuntimeError(f"no TOKEN in /token response: {r.json()}")
    return token


def make_session(token):
    """Build a requests.Session pre-loaded with the BMS auth headers for reuse across calls."""
    s = requests.Session()
    s.headers.update(
        {"Accept": "application/json", "x-api-key": API_KEY, "token": token}
    )
    return s


def _get(session, path, params, timeout=180):
    """Thin GET wrapper that prefixes the BMS base URL + company alias onto a path."""
    return session.get(f"{BASE_URL}/{ALIAS}/{path}", params=params, timeout=timeout)


# ---- Data pulls -----------------------------------------------------------

def pull_lowstock(S):
    """Fetch the BMS low-stock list — the set of items currently below their safety-stock threshold."""
    return _get(S, "lowstock", {"company": COMPANY}).json()


def load_stocked_universe(S):
    """
    Return (universe, from_cache): the SKUs to evaluate as {seq: {cache fields}},
    plus a flag telling whether they came from the full-catalog cache.

    The busy-month policy must judge EVERY stocked SKU (CAT_SAFTYSTK > 0), not
    just the ones /lowstock currently flags, so that thresholds set too low get
    caught. That universe is built by the weekly catalog_scan.py job and cached in
    cfg.STOCKED_CATALOG_CACHE; here we just read it.

    The cache is trusted ONLY when it is a COMPLETE scan AND fresh (scanned within
    cfg.STOCKED_CATALOG_MAX_AGE_DAYS). A missing / partial / stale / unreadable
    cache falls back to /lowstock — the old, narrower behavior — rather than
    silently evaluating an incomplete universe (every fallback is logged loudly).
    Both paths return the same shape so build_report can treat them uniformly; the
    fallback leaves catalog-only fields (vendor/box/style/...) blank/default. The
    from_cache flag lets build_report skip the costly box-qty catalog scan when the
    cache already carries CAT_UNIT_PER_BOX for every SKU.
    """
    path = cfg.STOCKED_CATALOG_CACHE

    # --- Preferred source: the cached full-catalog scan.
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            data = None
        if data and data.get("complete") and data.get("items"):
            # Freshness guard: a complete-but-ancient scan is not trustworthy.
            scanned = _parse_iso(data.get("scanned_at"))
            age_days = (datetime.now(timezone.utc) - scanned).days if scanned else None
            if age_days is not None and age_days <= cfg.STOCKED_CATALOG_MAX_AGE_DAYS:
                print(
                    f"stocked universe: {len(data['items'])} SKUs from cache "
                    f"(scanned {age_days}d ago)",
                    file=sys.stderr,
                )
                return data["items"], True
            print(
                f"WARNING: stocked-catalog cache is stale "
                f"(age {age_days}d > {cfg.STOCKED_CATALOG_MAX_AGE_DAYS}d max) — "
                f"falling back to /lowstock",
                file=sys.stderr,
            )
        else:
            print(
                "WARNING: stocked-catalog cache is incomplete/unreadable — "
                "falling back to /lowstock",
                file=sys.stderr,
            )
    else:
        print(
            "WARNING: no stocked-catalog cache found — falling back to /lowstock "
            "(run: python -m email_reports.inventory.catalog_scan)",
            file=sys.stderr,
        )

    # --- Fallback: the narrower "currently below safety" list. Map it onto the
    # same dict shape, leaving catalog-only fields blank for downstream code.
    universe = {}
    for it in pull_lowstock(S):
        seq = str(it.get("CAT_SEQUENCE", "")).strip()
        if not seq:
            continue
        universe[seq] = {
            "safety":  _f(it.get("CAT_SAFETY_STOCK")),  # current safety threshold
            "reorder": 0.0,
            "vendor":  "",   # filled later from the PO-derived vendor map
            "box":     1.0,  # filled later from the box-qty cache
            "style":   "",
            "stynum":  "",
            "color":   "",
            "desc":    "",
        }
    return universe, False


def pull_orders(S, open_only=True, end=None):
    """Fetch order headers from ORDER_HISTORY_FLOOR through `end`; open_only=False adds active-status orders."""
    params = {"company": COMPANY, "startdate": cfg.ORDER_HISTORY_FLOOR, "enddate": end}
    if not open_only:
        params["ordstatus"] = "A"
    return _get(S, "orders", params).json()


def pull_productstock(S, seq):
    """Fetch current roll-level stock records for one catalog sequence; [] on non-200."""
    r = _get(S, "productstock", {"company": COMPANY, "catseq": seq}, timeout=30)
    return r.json() if r.status_code == 200 else []


def pull_orderline_bulk(S, branch, end):
    """Fetch all order lines for one branch over the history window (used for UNASSIGN and PEAK_WK); [] on non-200."""
    r = _get(
        S,
        "orderline",
        {"company": COMPANY, "branch": branch, "startdate": cfg.ORDER_HISTORY_FLOOR, "enddate": end},
        timeout=300,
    )
    return r.json() if r.status_code == 200 else []


def pull_purchaseorderlines(S):
    """Fetch PO lines — source of the per-seq vendor map and open-PO (ON_PO) quantities."""
    return _get(S, "purchaseorderlines", {"company": COMPANY, "pagelimit": "1000"}).json()


def pull_invoice_headers(S, branch, start, end):
    """Return {IVC_INVNO: datetime} for invoices in [start, end]."""
    invs = {}
    page = 1
    while True:
        r = _get(
            S,
            "invoice",
            {"company": COMPANY, "branch": branch, "startdate": start, "enddate": end,
             "page": str(page), "pagelimit": "1000"},
            timeout=120,
        )
        if r.status_code != 200:
            break
        d = r.json()
        if not isinstance(d, list) or not d:
            break
        for rec in d:
            invno = str(rec.get("IVC_INVNO", "")).strip()
            dt = _parse_date(rec.get("IVC_DATE"))
            if invno and dt:
                invs[invno] = dt
        if len(d) < 1000:
            break
        page += 1
    return invs


def pull_box_quantities(S, target_seqs, batch=16):
    """
    Return {seq: CAT_UNIT_PER_BOX (float)} for each target seq.

    Uses a disk cache (cfg.BOX_QTY_CACHE) refreshed every
    BOX_QTY_CACHE_MAX_AGE_DAYS to avoid a 2-min catalog scan every run.
    """
    # Load cache if fresh
    cache = {}
    if os.path.exists(cfg.BOX_QTY_CACHE):
        age = time.time() - os.path.getmtime(cfg.BOX_QTY_CACHE)
        if age < cfg.BOX_QTY_CACHE_MAX_AGE_DAYS * 86400:
            try:
                with open(cfg.BOX_QTY_CACHE) as f:
                    cache = json.load(f)
            except Exception:
                cache = {}

    missing = [s for s in target_seqs if s not in cache]
    if not missing:
        return {s: cache[s] for s in target_seqs}

    # Scan /catalogitems across the full history until every missing seq is found
    # or pagination ends. Early-stop keeps this from walking the whole catalog
    # when the targets are concentrated.
    def fetch_page(p):
        """Fetch one page of /catalogitems; [] on non-200."""
        r = _get(
            S,
            "catalogitems",
            {
                "company": COMPANY,
                "startdate": "19000101",
                "enddate": "21000101",
                "page": str(p),
                "pagelimit": "1000",
            },
            timeout=300,
        )
        return r.json() if r.status_code == 200 else []

    missing_set = set(missing)
    found_here = {}
    page = 1
    bar = tqdm(
        total=len(missing_set),
        desc="box qty (catalog scan)",
        unit="seq",
        file=sys.stderr,
        dynamic_ncols=True,
    )
    try:
        while missing_set:
            with ThreadPoolExecutor(max_workers=batch) as ex:
                results = list(ex.map(fetch_page, range(page, page + batch)))
            end_of_catalog = False
            for d in results:
                if not d:
                    end_of_catalog = True
                    continue
                for it in d:
                    seq = str(it.get("CAT_SEQUENCE", "")).strip()
                    if seq in missing_set:
                        found_here[seq] = _f(it.get("CAT_UNIT_PER_BOX"))
                        missing_set.discard(seq)
                        bar.update(1)
                if len(d) < 1000:
                    end_of_catalog = True
            page += batch
            if end_of_catalog:
                break
    finally:
        bar.close()

    # Any seq we couldn't find in the catalog gets 1 (no box)
    for s in missing_set:
        found_here[s] = 1.0

    cache.update(found_here)
    try:
        with open(cfg.BOX_QTY_CACHE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"(could not write box-qty cache: {e})", file=sys.stderr)

    return {s: cache.get(s, 1.0) for s in target_seqs}


def walk_invoice_lines(S, invno_dates, target_seqs, batch=16):
    """
    Walk /invoicelines (paginated) and return {seq: [(invoice_date, qty), ...]}
    for lines whose invoice is in invno_dates and whose catseq is in target_seqs.
    """
    events = defaultdict(list)
    page = 1

    def fetch_page(p):
        """Fetch one page of /invoicelines; [] on non-200."""
        r = _get(
            S,
            "invoicelines",
            {"company": COMPANY, "page": str(p), "pagelimit": "1000"},
            timeout=120,
        )
        return r.json() if r.status_code == 200 else []

    # Unknown total; tqdm shows a rolling rate + running count.
    bar = tqdm(desc="invoicelines", unit="page", file=sys.stderr, dynamic_ncols=True)
    try:
        while True:
            with ThreadPoolExecutor(max_workers=batch) as ex:
                results = list(ex.map(fetch_page, range(page, page + batch)))
            done = False
            pages_with_data = 0
            for d in results:
                if not d:
                    done = True
                    continue
                pages_with_data += 1
                for rec in d:
                    invno = str(rec.get("IVL_INVNO", "")).strip()
                    dt = invno_dates.get(invno)
                    if not dt:
                        continue
                    seq = str(rec.get("IVL_CAT_SEQUENCE", "")).strip()
                    if seq not in target_seqs:
                        continue
                    events[seq].append((dt, _f(rec.get("IVL_SQUAN"))))
                if len(d) < 1000:
                    done = True
            bar.update(pages_with_data)
            page += batch
            if done:
                break
    finally:
        bar.close()
    return events


# ---- Report build ---------------------------------------------------------

def build_report():
    """Orchestrate the full low-stock analysis: pull lowstock/orders/PO/stock/invoice data (largely in parallel), compute demand stats per seq, and derive recommended safety stock, reorder point, and order qty. Returns a list of per-item dicts."""
    t0 = time.time()
    today = datetime.now()
    end_yyyymmdd = today.strftime("%Y%m%d")
    start_yyyymmdd = (today - timedelta(days=cfg.DEMAND_WINDOW_DAYS)).strftime("%Y%m%d")

    token = authenticate()
    S = make_session(token)
    print(f"[{time.time()-t0:5.1f}s] authenticated", file=sys.stderr)

    # --- Stocked universe (the SKUs we evaluate) + parallel supply pulls.
    # load_stocked_universe reads the weekly catalog cache (or falls back to
    # /lowstock); orders/PO pulls run alongside it for UNASSIGN, vendor and ON_PO.
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_uni = ex.submit(load_stocked_universe, S)
        f_ord = ex.submit(pull_orders, S, True, end_yyyymmdd)
        f_pol = ex.submit(pull_purchaseorderlines, S)
        universe, universe_from_cache = f_uni.result()
        open_orders = f_ord.result()
        po_lines = f_pol.result()
    print(
        f"[{time.time()-t0:5.1f}s] stocked SKUs={len(universe)}, open_orders={len(open_orders)}, po_lines={len(po_lines)}",
        file=sys.stderr,
    )

    target = set(universe.keys())

    # Best-effort vendor map from /purchaseorderlines, used for the VENDOR display
    # column. Prefer the PO-history vendor; fall back to the catalog's CAT_VENDORID
    # when a SKU has no PO history. (Lead time is now a flat assumption, so vendor
    # no longer drives the math.)
    vendor_by_seq = {}
    for p in po_lines:
        seq = str(p.get("CATSEQUENCE", "")).strip()
        v = (p.get("VENDOR") or "").strip()
        if seq and v and seq not in vendor_by_seq:
            vendor_by_seq[seq] = v

    # Seed one record per stocked SKU. Current safety, box and descriptive fields
    # come straight from the catalog cache; live supply/demand numbers are filled
    # in by the pulls below.
    items = {
        seq: {
            "seq":          seq,
            "safety_cur":   _f(u.get("safety")),                       # current CAT_SAFTYSTK
            "vendor":       vendor_by_seq.get(seq) or str(u.get("vendor", "")).strip(),
            "on_hand":      0.0,
            "unassign":     0.0,                                       # = backorders
            "on_po":        0.0,
            "sold_1yr":     0.0,
            "busy_month":   0.0,                                       # policy anchor
            "box_qty":      _f(u.get("box")) or 1.0,                   # CAT_UNIT_PER_BOX
            "style":        str(u.get("style", "")).strip(),
            "color":        str(u.get("color", "")).strip(),
        }
        for seq, u in universe.items()
    }

    # --- Box quantities. The stocked-catalog cache already carries CAT_UNIT_PER_BOX
    # for every SKU, so we only run the (slow, page-by-page) box-qty catalog scan on
    # the /lowstock fallback path — where box defaulted to 1. Skipping it on the
    # cache path avoids a ~45-min scan that would re-derive box sizes we already have.
    if universe_from_cache:
        print(f"[{time.time()-t0:5.1f}s] box qty from catalog cache (scan skipped)", file=sys.stderr)
    else:
        box_qty_map = pull_box_quantities(S, list(target))
        for seq, bq in box_qty_map.items():
            if seq in items and bq and bq > 0:
                items[seq]["box_qty"] = bq
        print(f"[{time.time()-t0:5.1f}s] box qty resolved", file=sys.stderr)

    # --- Current stock per seq (parallel)
    def stock_one(seq):
        """Pull one seq's stock and return it paired with the seq (for the threaded map)."""
        return seq, pull_productstock(S, seq)

    with ThreadPoolExecutor(max_workers=16) as ex:
        for seq, data in tqdm(
            ex.map(stock_one, target),
            total=len(target),
            desc="productstock",
            unit="seq",
            file=sys.stderr,
            dynamic_ncols=True,
        ):
            if isinstance(data, list) and data:
                items[seq]["on_hand"] = sum(_f(x.get("ONHAND_FLOAT")) for x in data)
                # Prefer live roll style/color, but never overwrite a populated
                # cache value with a blank one.
                st = (data[0].get("STYLE") or "").strip()
                co = (data[0].get("COLOR") or "").strip()
                if st:
                    items[seq]["style"] = st
                if co:
                    items[seq]["color"] = co

    # --- ON_PO from open /purchaseorderlines records
    for p in po_lines:
        if str(p.get("STATUS", "")).strip().upper() != "O":
            continue
        seq = str(p.get("CATSEQUENCE", "")).strip()
        if seq not in target:
            continue
        q = _f(p.get("QTYORD")) - _f(p.get("QTYREC"))
        if q > 0:
            items[seq]["on_po"] += q

    # --- Bulk /orderline per branch (parallel) for UNASSIGN and PEAK_WK
    open_ordnos = {str(o.get("DMO_ORDNO", "")).strip() for o in open_orders}
    order_date = {}
    for o in open_orders:
        ordno = str(o.get("DMO_ORDNO", "")).strip()
        dt = _parse_date(o.get("DMH_DATE"))
        if ordno and dt:
            order_date[ordno] = dt
    branches = sorted({str(o.get("DMO_WHSE", "")).strip() for o in open_orders if str(o.get("DMO_WHSE", "")).strip()})

    # Invoice header pulls (for SOLD_1YR / busy_month) run in parallel with the
    # per-branch orderline pulls (for UNASSIGN / backorders).
    with ThreadPoolExecutor(max_workers=max(len(branches) * 2, 2)) as ex:
        line_futs = {br: ex.submit(pull_orderline_bulk, S, br, end_yyyymmdd) for br in branches}
        inv_futs  = {br: ex.submit(pull_invoice_headers, S, br, start_yyyymmdd, end_yyyymmdd) for br in branches}

        all_order_lines = []
        invno_dates = {}
        futs_all = list(line_futs.values()) + list(inv_futs.values())
        bar = tqdm(
            total=len(futs_all),
            desc="orderline+invoice hdrs",
            unit="br",
            file=sys.stderr,
            dynamic_ncols=True,
        )
        for fut in as_completed(futs_all):
            result = fut.result()
            if isinstance(result, tuple):
                # pull_invoice_headers returns dict, pull_orderline_bulk returns list
                pass
            if isinstance(result, list):
                all_order_lines.extend(result)
            elif isinstance(result, dict):
                invno_dates.update(result)
            bar.update(1)
        bar.close()

    print(
        f"[{time.time()-t0:5.1f}s] order lines={len(all_order_lines)}, invoice hdrs={len(invno_dates)}",
        file=sys.stderr,
    )

    # Backorders (UNASSIGN): open-order demand not yet allocated to rolls. Summed
    # per SKU from the open orders' lines; this is subtracted from inventory
    # position so committed-but-unfilled demand counts against what we have.
    for ln in all_order_lines:
        seq = str(ln.get("DMI_CAT_SEQUENCE", "")).strip()
        if seq not in target:
            continue
        ordno = str(ln.get("DMI_ORDNO", "")).strip()
        if ordno in open_ordnos:
            u = _f(ln.get("DMI_WQUANTITY")) - _f(ln.get("DMI_QTYASSIGNED"))
            if u > 0:
                items[seq]["unassign"] += u

    # --- Invoice-line walk (the slow one): shipped history for SOLD_1YR and the
    # busy-month figure. seq_events is {seq: [(invoice_date, qty), ...]}.
    seq_events = walk_invoice_lines(S, invno_dates, target)
    cutoff = today - timedelta(days=cfg.DEMAND_WINDOW_DAYS)

    # Per SKU: total shipped in the window (SOLD_1YR) and the peak rolling-30-day
    # shipped qty (busy_month) — computed from the same date-sorted event list.
    for seq, events in seq_events.items():
        if seq not in items:
            continue
        ev = sorted((d, q) for d, q in events if d >= cutoff and q > 0)
        items[seq]["sold_1yr"]   = sum(q for _, q in ev)
        items[seq]["busy_month"] = _rolling_30day_max(ev)
    print(f"[{time.time()-t0:5.1f}s] invoicelines walked, busy_month computed", file=sys.stderr)

    # --- Reorder policy (busy-month model). For every stocked SKU:
    #     safety        = one busy month on hand
    #     reorder point = busy month + demand expected during the lead time
    #     order-up-to   = two busy months
    #     inv position  = what we effectively have: on_hand + on_po − backorders
    #     ORDER NOW     when inventory position has fallen to/below the reorder point
    #     suggested qty = top back up to order-up-to, rounded up to a full box
    # All stock figures are rounded UP to multiples of CAT_UNIT_PER_BOX.
    for r in items.values():
        # Flat lead-time assumption (7–14 day window, midpoint) for every SKU.
        lt_days = cfg.ASSUMED_LEAD_TIME_DAYS
        r["daily_demand"] = r["sold_1yr"] / float(cfg.DEMAND_WINDOW_DAYS)
        box = r["box_qty"]

        # Effective busy month: the raw peak, but capped at SAFETY_CAP_MONTHS of
        # average demand so a single one-off project month can't inflate the
        # numbers. (We can restock in ~1–2 weeks, so hoarding a freak peak isn't
        # warranted.) This capped value drives safety, reorder and order-up-to.
        cap = r["sold_1yr"] * (cfg.SAFETY_CAP_MONTHS / 12.0)
        busy_eff = min(r["busy_month"], cap) if cap > 0 else r["busy_month"]
        r["busy_eff"] = busy_eff

        # Safety is one (capped) busy month; the reorder point adds the demand we
        # expect to ship while a replenishment PO is in transit; order-up-to is two.
        r["rec_safety"]  = _ceil_to_box(busy_eff, box)
        r["rec_rop"]     = _ceil_to_box(busy_eff + r["daily_demand"] * lt_days, box)
        r["order_up_to"] = _ceil_to_box(2.0 * busy_eff, box)

        # Inventory position nets incoming POs in and unfilled backorders out, so
        # the trigger reflects our true committed position — not just on-hand.
        r["inv_pos"]   = r["on_hand"] + r["on_po"] - r["unassign"]
        r["order_now"] = r["inv_pos"] <= r["rec_rop"]
        r["rec_qty"]   = _ceil_to_box(max(0.0, r["order_up_to"] - r["inv_pos"]), box)

        # Display delta of current vs recommended safety threshold (cur → rec).
        r["safety_delta"] = r["rec_safety"] - r["safety_cur"]

    return list(items.values())


def write_report(rows, path):
    """
    Write the full stocked-universe reorder report as a fixed-width text file.

    Sorted ORDER NOW first (deepest below the reorder point at the top), then the
    rest by sequence. Unlike the email — which lists only SKUs that need ordering
    — this .txt is the complete audit of every stocked SKU, so under-set thresholds
    stay visible even when nothing needs reordering yet.
    """
    # ORDER NOW first; within each group, most-negative inv_pos − reorder first.
    rows = sorted(
        rows,
        key=lambda r: (not r["order_now"], r["inv_pos"] - r["rec_rop"], r["seq"]),
    )
    ms = max((len(r["style"])  for r in rows), default=5)
    mc = max((len(r["color"])  for r in rows), default=5)
    mv = max((len(r["vendor"]) for r in rows), default=6)
    order_now_count = sum(1 for r in rows if r["order_now"])
    hdr = (
        f"{'SEQUENCE':<14} {'VENDOR':<{mv}} "
        f"{'ON_HAND':>8} {'UNASN':>8} {'ON_PO':>8} {'INV_POS':>8} "
        f"{'SOLD_1YR':>9} {'BUSY_MO':>8} "
        f"{'BOX':>5} {'SAF_CUR':>8} {'SAF_REC':>8} {'ROP_REC':>8} {'UPTO':>8} {'QTY_REC':>8} "
        f"{'ORDER':>6} {'STYLE':<{ms}} {'COLOR':<{mc}}"
    )
    with open(path, "w") as w:
        w.write(
            f"Stocked-SKU reorder report — company {COMPANY}, {len(rows)} SKUs, "
            f"{order_now_count} to order now\n"
        )
        w.write(
            "Busy-month policy: busy_month = peak rolling-30-day shipped qty (trailing "
            f"{cfg.DEMAND_WINDOW_DAYS}d), capped at {cfg.SAFETY_CAP_MONTHS:g} month(s) of avg "
            "demand (SOLD_1YR/12) to damp one-off spikes; safety = capped busy_month; "
            f"reorder = +daily_demand×lead_time (assumes {cfg.ASSUMED_LEAD_TIME_DAYS}d, 7–14d window); "
            "order-up-to = 2×capped; ORDER NOW when on_hand+on_po−backorder ≤ reorder; "
            "recs rounded up to BOX multiples\n"
        )
        w.write("=" * len(hdr) + "\n" + hdr + "\n" + "-" * len(hdr) + "\n")
        for r in rows:
            w.write(
                f"{r['seq']:<14} {r['vendor']:<{mv}} "
                f"{r['on_hand']:>8.2f} {r['unassign']:>8.2f} {r['on_po']:>8.2f} {r['inv_pos']:>8.2f} "
                f"{r['sold_1yr']:>9.2f} {r['busy_month']:>8.2f} "
                f"{r['box_qty']:>5.0f} {r['safety_cur']:>8.2f} {r['rec_safety']:>8.2f} {r['rec_rop']:>8.2f} {r['order_up_to']:>8.2f} {r['rec_qty']:>8.2f} "
                f"{('YES' if r['order_now'] else ''):>6} {r['style']:<{ms}} {r['color']:<{mc}}\n"
            )


def main():
    """CLI entry point: build the report and write it to cfg.OUTPUT_PATH."""
    rows = build_report()
    write_report(rows, cfg.OUTPUT_PATH)
    print(f"wrote {cfg.OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
