"""
BMS low-stock reorder report.

Pulls demand + supply data from the Rollmaster (Broadlume BMS) API and writes a
txt report for every stocking item currently below its safety-stock threshold.

Columns:
    SEQUENCE   CAT_SEQUENCE
    VENDOR     from /purchaseorderlines (blank if no PO history)
    LT         lead time in days, from inventory_email_config.VENDOR_LEAD_TIMES
    ON_HAND    sum of current rolls (/productstock ONHAND_FLOAT)
    AVAIL      sum of current rolls (/productstock AVAILABLE_FLOAT)
    UNASN      open-order demand not yet allocated to rolls
    ON_PO      open PO QTYORD minus QTYREC
    SOLD_1YR   sum of IVL_SQUAN on invoices dated within DEMAND_WINDOW_DAYS
    PEAK_WK    max single-week DMI_SQUANTITY bucketed by DMH_DATE ISO week
    σ_WK       std-dev of weekly invoiced qty over DEMAND_WINDOW_DAYS
    BOX        CAT_UNIT_PER_BOX — round recommendations up to multiples of this
    SAF_CUR    current safety stock per /lowstock (CAT_SAFETY_STOCK)
    SAF_REC    recommended safety = Z × σ × √(lead_time_weeks), ceil to box
    ROP_REC    recommended reorder point = daily_demand × lead_time + SAF_REC, ceil to box
    QTY_REC    recommended order qty = daily_demand × REORDER_PERIOD_DAYS, ceil to box

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
from datetime import datetime, timedelta
from statistics import stdev

import requests
from dotenv import load_dotenv
from tqdm import tqdm

import inventory_email_config as cfg

load_dotenv()

API_KEY  = os.environ["BMS_API_KEY"]
USERNAME = os.environ["BMS_USERNAME"]
PASSWORD = os.environ["BMS_PASSWORD"]
BASE_URL = "https://api.rmaster.com/api"
ALIAS    = cfg.BMS_ALIAS
COMPANY  = cfg.BMS_COMPANY


# ---- Helpers ---------------------------------------------------------------

def _f(x):
    try:
        return float(str(x).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(s):
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


def _iso_week(dt):
    return dt.strftime("%G-W%V")


def _ceil_to_box(value, box_qty):
    """Round up to the next multiple of box_qty. box_qty <= 0 means no boxes → ceil to int."""
    if value <= 0:
        return 0.0
    q = box_qty if box_qty and box_qty > 0 else 1
    return math.ceil(value / q) * q


def authenticate():
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
    s = requests.Session()
    s.headers.update(
        {"Accept": "application/json", "x-api-key": API_KEY, "token": token}
    )
    return s


def _get(session, path, params, timeout=180):
    return session.get(f"{BASE_URL}/{ALIAS}/{path}", params=params, timeout=timeout)


# ---- Data pulls -----------------------------------------------------------

def pull_lowstock(S):
    return _get(S, "lowstock", {"company": COMPANY}).json()


def pull_orders(S, open_only=True, end=None):
    params = {"company": COMPANY, "startdate": cfg.ORDER_HISTORY_FLOOR, "enddate": end}
    if not open_only:
        params["ordstatus"] = "A"
    return _get(S, "orders", params).json()


def pull_productstock(S, seq):
    r = _get(S, "productstock", {"company": COMPANY, "catseq": seq}, timeout=30)
    return r.json() if r.status_code == 200 else []


def pull_orderline_bulk(S, branch, end):
    r = _get(
        S,
        "orderline",
        {"company": COMPANY, "branch": branch, "startdate": cfg.ORDER_HISTORY_FLOOR, "enddate": end},
        timeout=300,
    )
    return r.json() if r.status_code == 200 else []


def pull_purchaseorderlines(S):
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
    t0 = time.time()
    today = datetime.now()
    end_yyyymmdd = today.strftime("%Y%m%d")
    start_yyyymmdd = (today - timedelta(days=cfg.DEMAND_WINDOW_DAYS)).strftime("%Y%m%d")

    token = authenticate()
    S = make_session(token)
    print(f"[{time.time()-t0:5.1f}s] authenticated", file=sys.stderr)

    # --- Initial parallel pulls
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_low = ex.submit(pull_lowstock, S)
        f_ord = ex.submit(pull_orders, S, True, end_yyyymmdd)
        f_pol = ex.submit(pull_purchaseorderlines, S)
        low_items = f_low.result()
        open_orders = f_ord.result()
        po_lines = f_pol.result()
    print(
        f"[{time.time()-t0:5.1f}s] lowstock={len(low_items)}, open_orders={len(open_orders)}, po_lines={len(po_lines)}",
        file=sys.stderr,
    )

    target = {it["CAT_SEQUENCE"] for it in low_items}

    # Best-effort vendor map from /purchaseorderlines
    vendor_by_seq = {}
    for p in po_lines:
        seq = str(p.get("CATSEQUENCE", "")).strip()
        v = (p.get("VENDOR") or "").strip()
        if seq and v and seq not in vendor_by_seq:
            vendor_by_seq[seq] = v

    items = {
        it["CAT_SEQUENCE"]: {
            "seq":          it["CAT_SEQUENCE"],
            "safety_cur":   _f(it.get("CAT_SAFETY_STOCK")),
            "vendor":       vendor_by_seq.get(it["CAT_SEQUENCE"], ""),
            "on_hand":      0.0,
            "avail":        0.0,
            "style":        "",
            "color":        "",
            "unassign":     0.0,
            "on_po":        0.0,
            "sold_1yr":     0.0,
            "peak_wk":      0.0,
            "peak_wk_date": "",
            "weekly_sigma": 0.0,
            "box_qty":      1.0,
        }
        for it in low_items
    }

    # --- Box quantities (cached)
    box_qty_map = pull_box_quantities(S, list(target))
    for seq, bq in box_qty_map.items():
        if seq in items:
            items[seq]["box_qty"] = bq if bq and bq > 0 else 1.0
    print(f"[{time.time()-t0:5.1f}s] box qty resolved", file=sys.stderr)

    # --- Current stock per seq (parallel)
    def stock_one(seq):
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
                items[seq]["avail"] = sum(_f(x.get("AVAILABLE_FLOAT")) for x in data)
                items[seq]["style"] = (data[0].get("STYLE") or "").strip()
                items[seq]["color"] = (data[0].get("COLOR") or "").strip()

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

    # Invoice header pulls for the SOLD_1YR / σ step run in parallel with orderline pulls
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

    week_qty = defaultdict(lambda: defaultdict(float))
    for ln in all_order_lines:
        seq = str(ln.get("DMI_CAT_SEQUENCE", "")).strip()
        if seq not in target:
            continue
        ordno = str(ln.get("DMI_ORDNO", "")).strip()
        if ordno in open_ordnos:
            u = _f(ln.get("DMI_WQUANTITY")) - _f(ln.get("DMI_QTYASSIGNED"))
            if u > 0:
                items[seq]["unassign"] += u
        d = order_date.get(ordno)
        if d:
            qty = _f(ln.get("DMI_SQUANTITY"))
            if qty > 0:
                week_qty[seq][_iso_week(d)] += qty

    for seq, weeks in week_qty.items():
        if not weeks:
            continue
        best_wk, best_q = max(weeks.items(), key=lambda kv: kv[1])
        items[seq]["peak_wk"] = best_q
        items[seq]["peak_wk_date"] = best_wk

    # --- Invoice-line walk (the slow one) for SOLD_1YR and weekly σ
    seq_events = walk_invoice_lines(S, invno_dates, target)
    cutoff = today - timedelta(days=cfg.DEMAND_WINDOW_DAYS)

    # Pre-compute week labels in the window so we include zero weeks in σ
    window_weeks = []
    w = cutoff
    while w <= today:
        window_weeks.append(_iso_week(w))
        w += timedelta(days=7)
    window_weeks = sorted(set(window_weeks))

    for seq, events in seq_events.items():
        events = [(d, q) for d, q in events if d >= cutoff]
        items[seq]["sold_1yr"] = sum(q for _, q in events)
        per_week = defaultdict(float)
        for d, q in events:
            per_week[_iso_week(d)] += q
        series = [per_week.get(wk, 0.0) for wk in window_weeks]
        if len(series) >= 2:
            items[seq]["weekly_sigma"] = stdev(series)
    print(f"[{time.time()-t0:5.1f}s] invoicelines walked, σ computed", file=sys.stderr)

    # --- Algorithm: recommended safety, reorder point, reorder quantity
    # All recommendations rounded UP to multiples of CAT_UNIT_PER_BOX.
    for r in items.values():
        lt_days = cfg.VENDOR_LEAD_TIMES.get(r["vendor"], cfg.DEFAULT_LEAD_TIME_DAYS)
        r["lead_time"] = lt_days
        r["daily_demand"] = r["sold_1yr"] / 365.0

        raw_safety = cfg.SERVICE_LEVEL_Z * r["weekly_sigma"] * math.sqrt(lt_days / 7.0)
        raw_rop = r["daily_demand"] * lt_days + raw_safety
        raw_qty = r["daily_demand"] * cfg.REORDER_PERIOD_DAYS

        box = r["box_qty"]
        r["rec_safety"] = _ceil_to_box(raw_safety, box)
        r["rec_rop"]    = _ceil_to_box(raw_rop,    box)
        r["rec_qty"]    = _ceil_to_box(raw_qty,    box)
        r["safety_delta"] = r["rec_safety"] - r["safety_cur"]

    return list(items.values())


def write_report(rows, path):
    rows = sorted(rows, key=lambda r: (-r["safety_cur"], r["seq"]))
    ms = max((len(r["style"])  for r in rows), default=5)
    mc = max((len(r["color"])  for r in rows), default=5)
    mv = max((len(r["vendor"]) for r in rows), default=6)
    hdr = (
        f"{'SEQUENCE':<14} {'VENDOR':<{mv}} {'LT':>4} "
        f"{'ON_HAND':>8} {'AVAIL':>8} {'UNASN':>8} {'ON_PO':>8} "
        f"{'SOLD_1YR':>9} {'PEAK_WK':>8} {'SIGMA_WK':>9} "
        f"{'BOX':>5} {'SAF_CUR':>8} {'SAF_REC':>8} {'ROP_REC':>8} {'QTY_REC':>8} "
        f"{'STYLE':<{ms}} {'COLOR':<{mc}}"
    )
    with open(path, "w") as w:
        w.write(f"Low-stock reorder report — company {COMPANY}, {len(rows)} items\n")
        w.write(
            f"Z={cfg.SERVICE_LEVEL_Z}, demand window={cfg.DEMAND_WINDOW_DAYS}d, "
            f"default lead time={cfg.DEFAULT_LEAD_TIME_DAYS}d, "
            f"reorder period={cfg.REORDER_PERIOD_DAYS}d; recs rounded up to BOX multiples\n"
        )
        w.write("=" * len(hdr) + "\n" + hdr + "\n" + "-" * len(hdr) + "\n")
        for r in rows:
            w.write(
                f"{r['seq']:<14} {r['vendor']:<{mv}} {r['lead_time']:>4} "
                f"{r['on_hand']:>8.2f} {r['avail']:>8.2f} {r['unassign']:>8.2f} {r['on_po']:>8.2f} "
                f"{r['sold_1yr']:>9.2f} {r['peak_wk']:>8.2f} {r['weekly_sigma']:>9.2f} "
                f"{r['box_qty']:>5.0f} {r['safety_cur']:>8.2f} {r['rec_safety']:>8.2f} {r['rec_rop']:>8.2f} {r['rec_qty']:>8.2f} "
                f"{r['style']:<{ms}} {r['color']:<{mc}}\n"
            )


def main():
    rows = build_report()
    write_report(rows, cfg.OUTPUT_PATH)
    print(f"wrote {cfg.OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
