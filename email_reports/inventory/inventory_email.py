"""
BMS stocked-SKU reorder report.

Pulls current stock from the Rollmaster (Broadlume BMS) API and evaluates EVERY
stocked SKU (CAT_SAFTYSTK > 0) against the safety stock ENTERED IN BMS — nothing
is computed. The stocked universe comes from the weekly catalog_scan.py cache
(cfg.STOCKED_CATALOG_CACHE), falling back to /lowstock when that cache is
missing/stale.

Notify policy (per SKU), keyed off available balance = on_hand − reserved
(the per-roll BMS AVAILABLE_FLOAT, summed across a SKU's rolls):

    NOTIFY    when available < safety stock (CAT_SAFTYSTK)

(BMS also has CAT_REORDER, but that is the reorder QUANTITY — how much to buy
once a SKU is below safety — not a trigger, so it plays no part here.)

Columns (.txt full audit):
    SEQUENCE   CAT_SEQUENCE
    VENDOR     catalog CAT_VENDORID
    ON_HAND    sum of current rolls (/productstock ONHAND_FLOAT)
    COMMITTED  sold on open orders but NOT yet assigned to a roll and NOT on a
               PO (/orderline DMI_WQUANTITY − DMI_QTYASSIGNED, lines without a
               DMI_PONO) — demand nothing is covering yet
    RESERVED   qty assigned to specific rolls (/productstock RESERVED_FLOAT)
    AVAIL      available balance = ON_HAND − RESERVED (/productstock AVAILABLE_FLOAT)
    SAFETY     entered safety stock (catalog CAT_SAFTYSTK, or /lowstock fallback)
    NOTIFY     "YES" when available < safety, else blank

Usage:
    ./venv/bin/python inventory_email.py
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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


# Roll yardage (square yards per roll) is not a clean catalog field — BMS embeds
# it in free text, and which text field carries it varies by product. So we scrape
# a "<number> SY/SYD/SQYD" token out of the descriptive fields. SYD/SQ.YD come
# before SY in the alternation so "30 SYD" matches the longer unit, not a bare SY.
# (Kept for catalog_scan.py, which caches per-roll yardage.)
_YARDAGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:SQ\.?\s?YD|SYD|SY|S/?Y)\b", re.IGNORECASE)


def parse_yardage(*texts):
    """
    Scrape the per-roll yardage (square yards) out of free-text catalog fields.

    Pass the descriptive fields in priority order (e.g. style, desc, color); the
    first one containing a yardage token wins — so "BLACK DIAMOND … 6LB PAD" with
    no token falls through to its "30 SY PER ROLL" description. Returns the parsed
    float, or 0.0 when nothing matches (per-LF base shoe, tile, cleaners, etc.).
    """
    for t in texts:
        if not t:
            continue
        m = _YARDAGE_RE.search(str(t))
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return 0.0


def _norm_seq(x):
    """Catalog sequence as a bare string: /orderline zero-pads it to 13 digits ('0000000684588'), /productstock and the catalog cache don't."""
    return str(x or "").strip().lstrip("0") or "0"


def _parse_iso(s):
    """Parse an ISO-8601 timestamp (e.g. the cache's scanned_at) to a datetime, or None."""
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


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

    We judge EVERY stocked SKU (CAT_SAFTYSTK > 0), not just the ones /lowstock
    currently flags, so that thresholds set too low get caught. That universe is
    built by the weekly catalog_scan.py job and cached in cfg.STOCKED_CATALOG_CACHE;
    here we just read it.

    The cache is trusted ONLY when it is a COMPLETE scan AND fresh (scanned within
    cfg.STOCKED_CATALOG_MAX_AGE_DAYS). A missing / partial / stale / unreadable
    cache falls back to /lowstock — the old, narrower behavior — rather than
    silently evaluating an incomplete universe (every fallback is logged loudly).
    Both paths return the same shape so build_report can treat them uniformly; the
    fallback leaves catalog-only fields (vendor/reorder/style/...) blank/default.
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
            "safety":  _f(it.get("CAT_SAFETY_STOCK")),  # entered safety threshold
            "vendor":  "",
            "style":   "",
            "color":   "",
        }
    return universe, False


def pull_productstock(S, seq):
    """Fetch current roll-level stock records for one catalog sequence; [] on non-200."""
    r = _get(S, "productstock", {"company": COMPANY, "catseq": seq}, timeout=30)
    return r.json() if r.status_code == 200 else []


def pull_open_orders(S, end):
    """Fetch open sales-order headers from cfg.ORDER_HISTORY_FLOOR through `end` (BMS returns open orders only unless ordstatus is passed)."""
    return _get(
        S, "orders",
        {"company": COMPANY, "startdate": cfg.ORDER_HISTORY_FLOOR, "enddate": end},
    ).json()


def pull_orderline_bulk(S, branch, end):
    """Fetch every order line for one branch over the history window; [] on non-200. /orderline has no per-SKU filter, so we pull the branch and filter locally."""
    r = _get(
        S, "orderline",
        {"company": COMPANY, "branch": branch,
         "startdate": cfg.ORDER_HISTORY_FLOOR, "enddate": end},
        timeout=300,
    )
    return r.json() if r.status_code == 200 else []


def pull_committed(S, target):
    """
    Return {seq: committed qty} for the SKUs in `target` — how much is SOLD on open
    sales orders that nothing is covering yet: not assigned to a roll, and not on
    a purchase order.

    Per open-order line that is (DMI_WQUANTITY − DMI_QTYASSIGNED), skipped entirely
    when the line carries a PO number (DMI_PONO) — that demand is on order already.
    DMI_WQUANTITY is in warehouse units, the same as on hand / reserved (unlike
    DMI_SQUANTITY, which is in selling units).

    A fully assigned SKU therefore shows 0 even with a lot sold, and a positive
    committed means someone has to find stock or cut a PO for it.

    Open-ness comes from the order header (/orders returns open orders only), which
    is how the pre-rewrite report scoped the same line pull.
    """
    end = datetime.now().strftime("%Y%m%d")
    open_orders = pull_open_orders(S, end)
    open_ordnos = {str(o.get("DMO_ORDNO", "")).strip() for o in open_orders}
    branches = sorted(
        {str(o.get("DMO_WHSE", "")).strip() for o in open_orders
         if str(o.get("DMO_WHSE", "")).strip()}
    )
    print(
        f"open orders={len(open_orders)} across branches={branches or '—'}",
        file=sys.stderr,
    )

    target_norm = {_norm_seq(t): t for t in target}
    committed = {}
    if not branches:
        return committed
    with ThreadPoolExecutor(max_workers=len(branches)) as ex:
        for lines in ex.map(lambda br: pull_orderline_bulk(S, br, end), branches):
            for ln in lines:
                seq = target_norm.get(_norm_seq(ln.get("DMI_CAT_SEQUENCE")))
                if seq is None:
                    continue
                if str(ln.get("DMI_ORDNO", "")).strip() not in open_ordnos:
                    continue
                # On a PO already → covered, not committed.
                pono = str(ln.get("DMI_PONO", "")).strip()
                if pono and pono.strip("0"):
                    continue
                unassigned = _f(ln.get("DMI_WQUANTITY")) - _f(ln.get("DMI_QTYASSIGNED"))
                if unassigned > 0:
                    committed[seq] = committed.get(seq, 0.0) + unassigned
    return committed


# ---- Report build ---------------------------------------------------------

def build_report():
    """
    Evaluate every stocked SKU against its entered BMS safety stock and return a
    list of per-item dicts.

    Pulls the stocked universe (weekly catalog cache, or /lowstock fallback), then
    the live roll stock per SKU (/productstock, in parallel) to get on_hand,
    reserved and available. Available = on_hand − reserved. A SKU is flagged
    order_now when available drops below its safety stock. Nothing is computed —
    the threshold is the value entered in BMS.
    """
    t0 = time.time()

    token = authenticate()
    S = make_session(token)
    print(f"[{time.time()-t0:5.1f}s] authenticated", file=sys.stderr)

    # --- Stocked universe (the SKUs we evaluate).
    universe, _from_cache = load_stocked_universe(S)
    print(f"[{time.time()-t0:5.1f}s] stocked SKUs={len(universe)}", file=sys.stderr)
    target = set(universe.keys())

    # Seed one record per stocked SKU. Entered thresholds and descriptive fields
    # come from the catalog cache; live stock is filled from /productstock below.
    items = {
        seq: {
            "seq":         seq,
            "safety_cur":  _f(u.get("safety")),                  # entered CAT_SAFTYSTK
            "vendor":      str(u.get("vendor", "")).strip(),     # catalog CAT_VENDORID
            "on_hand":     0.0,
            "reserved":    0.0,                                  # assigned to rolls
            "available":   0.0,                                  # on_hand − reserved
            "committed":   0.0,                                  # sold, unassigned, not on a PO
            "style":       str(u.get("style", "")).strip(),
            "color":       str(u.get("color", "")).strip(),
        }
        for seq, u in universe.items()
    }

    # --- Current stock per seq (parallel). on_hand / reserved / available all come
    # straight from BMS per-roll fields; we sum the rolls for each SKU. BMS already
    # computes AVAILABLE_FLOAT = ONHAND_FLOAT − RESERVED_FLOAT per roll.
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
                items[seq]["on_hand"]   = sum(_f(x.get("ONHAND_FLOAT")) for x in data)
                items[seq]["reserved"]  = sum(_f(x.get("RESERVED_FLOAT")) for x in data)
                items[seq]["available"] = sum(_f(x.get("AVAILABLE_FLOAT")) for x in data)
                # Prefer live roll style/color, but never overwrite a populated
                # cache value with a blank one.
                st = (data[0].get("STYLE") or "").strip()
                co = (data[0].get("COLOR") or "").strip()
                if st:
                    items[seq]["style"] = st
                if co:
                    items[seq]["color"] = co
    print(f"[{time.time()-t0:5.1f}s] productstock pulled", file=sys.stderr)

    # --- Committed: sold on open orders, not assigned to a roll, not on a PO (see
    # pull_committed). Display-only — the notify policy below turns on available
    # balance; a positive committed is its own call to action (find stock or cut a PO).
    for seq, qty in pull_committed(S, target).items():
        items[seq]["committed"] = qty
    print(f"[{time.time()-t0:5.1f}s] committed pulled", file=sys.stderr)

    # --- Notify policy (uniform for every SKU, straight off the entered threshold):
    #   order_now when available < safety stock (CAT_SAFTYSTK).
    for r in items.values():
        r["order_now"] = r["available"] < r["safety_cur"]

    return list(items.values())


def write_report(rows, path):
    """
    Write the full stocked-universe audit as a fixed-width text file.

    Sorted below-safety first (deepest below first), then by sequence. Unlike the
    email — which lists only SKUs to reorder — this .txt is the complete audit of
    every stocked SKU, so thresholds set too low stay visible even when nothing
    needs reordering yet.
    """
    rows = sorted(
        rows,
        key=lambda r: (not r["order_now"], r["available"] - r["safety_cur"], r["seq"]),
    )
    ms = max((len(r["style"])  for r in rows), default=5)
    mc = max((len(r["color"])  for r in rows), default=5)
    mv = max((len(r["vendor"]) for r in rows), default=6)
    notify_count = sum(1 for r in rows if r["order_now"])
    hdr = (
        f"{'SEQUENCE':<14} {'VENDOR':<{mv}} "
        f"{'ON_HAND':>8} {'COMMITTED':>10} {'RESERVED':>9} {'AVAIL':>8} "
        f"{'SAFETY':>8} {'NOTIFY':>7} {'STYLE':<{ms}} {'COLOR':<{mc}}"
    )
    with open(path, "w") as w:
        w.write(
            f"Stocked-SKU reorder report — company {COMPANY}, {len(rows)} SKUs, "
            f"{notify_count} below safety stock\n"
        )
        w.write(
            "NOTIFY when available (on_hand − reserved) < safety stock (SAFETY), the "
            "value entered in BMS — nothing is computed. COMMITTED is open-order qty "
            "sold that is neither assigned to a roll nor on a PO — uncovered demand.\n"
        )
        w.write("=" * len(hdr) + "\n" + hdr + "\n" + "-" * len(hdr) + "\n")
        for r in rows:
            flag = "YES" if r["order_now"] else ""
            w.write(
                f"{r['seq']:<14} {r['vendor']:<{mv}} "
                f"{r['on_hand']:>8.2f} {r['committed']:>10.2f} "
                f"{r['reserved']:>9.2f} {r['available']:>8.2f} "
                f"{r['safety_cur']:>8.2f} {flag:>7} "
                f"{r['style']:<{ms}} {r['color']:<{mc}}\n"
            )


def main():
    """CLI entry point: build the report and write it to cfg.OUTPUT_PATH."""
    rows = build_report()
    write_report(rows, cfg.OUTPUT_PATH)
    print(f"wrote {cfg.OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
