"""
Rollmaster -> Pipedrive "Material Received" sync.

Marks a deal's Material Received field as Costed once every material line on
its Rollmaster order is accounted for. The deal is linked to the order by the
"RM Job #" custom field.

An order's material is all in when every material line on it is one of:

    assigned      line status J: DMI_QTYASSIGNED covers DMI_WQUANTITY — pulled
                  from stock, or a PO that has been received (receiving assigns
                  the material and flips the line from O to J)
    stocked SKU   unassigned and not on a PO, but the SKU is one we stock
                  (CAT_SAFTYSTK > 0): stock items are deliberately left
                  unassigned until a day or two before install so counts stay
                  easy to read, so this is not outstanding material
    labor only    an order with no material lines at all has nothing to wait
                  for and is Costed straight away

Anything else — a line still on a PO (status O), or a special-order SKU that is
unassigned with no PO — means the order is still waiting.

The sync only ever SETS Costed. It never clears it: Rollmaster entry can lag a
physical delivery, and a deal flipping backwards would be worse than one
flipping late. Deals already marked Costed are left alone.

Runs from cron on the Pi (see SETUP.md). Dry run unless RM_COSTING_SYNC_ENABLED=1
in .env; every flip (or would-be flip) is written to the events table so it
shows on the dashboard logs page as RM_MATERIAL_COSTED / RM_MATERIAL_DRYRUN.

    venv/bin/python costing_sync.py            # honours RM_COSTING_SYNC_ENABLED
    venv/bin/python costing_sync.py --dry-run  # never writes, whatever .env says
"""
import json
import logging
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

import rollmaster                                        # noqa: E402  (needs .env loaded first)

logger = logging.getLogger("costing_sync")

PD_BASE            = "https://api.pipedrive.com/v1"
PIPEDRIVE_API_TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN", "")
DB_PATH            = os.environ.get("DB_PATH", "/home/admin/CreativeBot/data/sync.db")
ENABLED            = os.environ.get("RM_COSTING_SYNC_ENABLED", "0") == "1"

# Pipedrive deal custom fields (keys from /dealFields).
PD_JOB_FIELD   = os.environ.get("PD_RM_JOB_FIELD",   "90775bc3828d314573699aab24c6c7fb8c9019ec")
PD_MAT_FIELD   = os.environ.get("PD_MATERIAL_FIELD", "93b963a5979add976477b833fcab6803ea30bdbe")
PD_MAT_COSTED  = os.environ.get("PD_MATERIAL_COSTED_OPTION", "29")     # option id of "Costed"

# Same window the inventory report uses for open orders; anything open but older
# than this would be invisible to the sync.
ORDER_HISTORY_FLOOR = "20240101"

# The stocked-SKU universe (CAT_SAFTYSTK > 0), built weekly by the inventory
# report's catalog scan.
STOCKED_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "email_reports", "inventory", ".stocked_catalog_cache.json")

# Order-line DMI_STATUS: J = assigned to stock/roll, O = on a PO not yet received,
# blank = unassigned and not ordered. L/S/I are labor, sundry and install lines.
MATERIAL_LINE_STATUSES = {"J", "O", ""}


def _f(x):
    """Coerce a possibly-messy BMS value to float; blank/None/unparseable becomes 0.0."""
    try:
        return float(str(x).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm_seq(x):
    """Catalog sequence without the zero-padding /orderline adds ('0000000684588' -> '684588')."""
    return str(x or "").strip().lstrip("0") or "0"


# ---------------------------------------------------------------------------
# ROLLMASTER
# ---------------------------------------------------------------------------
def load_stocked_seqs():
    """The set of stocked catalog sequences from the weekly scan cache; None (not empty) when the cache is missing/incomplete."""
    try:
        with open(STOCKED_CACHE) as f:
            data = json.load(f)
        if not data.get("complete") or not data.get("items"):
            return None
        return {_norm_seq(s) for s in data["items"]}
    except Exception:
        return None


def rm_material_status(stocked):
    """
    Return {order_no: (status, detail)} for every open Rollmaster order, where
    status is 'all_in', 'labor_only' or 'waiting'.

    `stocked` is the stocked-SKU set, or None when the catalog cache isn't
    available — in which case an unassigned line is treated as waiting, which
    can only delay a flip, never cause a wrong one.
    """
    end = datetime.now().strftime("%Y%m%d")
    orders = rollmaster.get("orders", {"company": rollmaster.COMPANY,
                                       "startdate": ORDER_HISTORY_FLOOR, "enddate": end})
    open_nos = {str(o.get("DMO_ORDNO", "")).strip() for o in orders}
    branches = sorted({str(o.get("DMO_WHSE", "")).strip() for o in orders if str(o.get("DMO_WHSE", "")).strip()})
    lines_by_order = {}
    for br in branches:
        for ln in rollmaster.get("orderline", {"company": rollmaster.COMPANY, "branch": br,
                                               "startdate": ORDER_HISTORY_FLOOR, "enddate": end}):
            ordno = str(ln.get("DMI_ORDNO", "")).strip()
            if ordno in open_nos:
                lines_by_order.setdefault(ordno, []).append(ln)

    result = {}
    for ordno in open_nos:
        material = [ln for ln in lines_by_order.get(ordno, [])
                    if str(ln.get("DMI_STATUS", "")).strip() in MATERIAL_LINE_STATUSES
                    and _f(ln.get("DMI_WQUANTITY")) > 0]
        if not material:
            result[ordno] = ("labor_only", "no material lines")
            continue
        waiting = []
        for ln in material:
            sold, assigned = _f(ln.get("DMI_WQUANTITY")), _f(ln.get("DMI_QTYASSIGNED"))
            on_po = (str(ln.get("DMI_STATUS", "")).strip() == "O"
                     or bool(str(ln.get("DMI_PONO", "")).strip().strip("0")))
            if assigned >= sold - 0.01 and str(ln.get("DMI_STATUS", "")).strip() != "O":
                continue                                              # assigned (or received)
            seq = _norm_seq(ln.get("DMI_CAT_SEQUENCE"))
            if not on_po and stocked is not None and seq in stocked:
                continue                                              # stock item, pulled later
            what = (str(ln.get("DMI_STYLE", "")).strip() or seq)[:40]
            waiting.append(f"{what} ({'on PO ' + str(ln.get('DMI_PONO')).strip() if on_po else 'not ordered'})")
        result[ordno] = ("waiting", "; ".join(waiting[:4])) if waiting else ("all_in", f"{len(material)} material lines in")
    return result


# ---------------------------------------------------------------------------
# PIPEDRIVE
# ---------------------------------------------------------------------------
def pd_open_deals_with_job():
    """Every open deal that carries an RM Job #, as {job_no: deal}."""
    out, start = {}, 0
    while True:
        r = requests.get(f"{PD_BASE}/deals", params={"api_token": PIPEDRIVE_API_TOKEN, "status": "open",
                                                    "limit": 500, "start": start}, timeout=60)
        r.raise_for_status()
        d = r.json()
        for deal in d.get("data") or []:
            job = deal.get(PD_JOB_FIELD)
            if job:
                out[str(int(float(job)))] = deal
        pag = d.get("additional_data", {}).get("pagination", {})
        if not pag.get("more_items_in_collection"):
            return out
        start = pag["next_start"]
        time.sleep(0.2)


def pd_mark_costed(deal_id):
    """Set Material Received = Costed on one deal; raises on failure."""
    r = requests.put(f"{PD_BASE}/deals/{deal_id}", params={"api_token": PIPEDRIVE_API_TOKEN},
                     json={PD_MAT_FIELD: PD_MAT_COSTED}, timeout=30)
    r.raise_for_status()
    if not r.json().get("success"):
        raise RuntimeError(f"Pipedrive update failed: {r.text[:200]}")


# ---------------------------------------------------------------------------
# EVENTS (dashboard logs page)
# ---------------------------------------------------------------------------
def log_event(deal_id, event_type, payload, action):
    """Append to the app's events table so the flip shows on the dashboard; never raises."""
    try:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            # Same schema app.py creates; only matters when running somewhere the app never has.
            conn.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, deal_id INTEGER,
                task_id INTEGER, event_type TEXT, task_type TEXT, raw_json TEXT NOT NULL,
                action TEXT, archived INTEGER NOT NULL DEFAULT 0)""")
            conn.execute(
                "INSERT INTO events (received_at, deal_id, task_id, event_type, task_type, raw_json, action) "
                "VALUES (?, ?, NULL, ?, 'costing', ?, ?)",
                (datetime.utcnow().isoformat(), deal_id, event_type, json.dumps(payload), action))
    except Exception:
        logger.exception("could not write event")


# ---------------------------------------------------------------------------
def run(dry_run=False):
    """One pass: compute material status per open order, flip linked deals to Costed. Returns a summary dict."""
    write = ENABLED and not dry_run
    stocked = load_stocked_seqs()
    if stocked is None:
        logger.warning("stocked-catalog cache missing/incomplete — unassigned stock items will count as waiting")
    status = rm_material_status(stocked)
    deals = pd_open_deals_with_job()
    summary = Counter()
    for job, deal in deals.items():
        st = status.get(job)
        if st is None:
            summary["deal has job # but order not open"] += 1
            continue
        kind, detail = st
        if str(deal.get(PD_MAT_FIELD) or "") == PD_MAT_COSTED:
            summary["already costed"] += 1
            continue
        if kind == "waiting":
            summary["waiting"] += 1
            continue
        action = f"{'Marked' if write else 'DRY RUN — would mark'} deal {deal['id']} Costed: RM job {job} {kind} ({detail})"
        payload = {"deal_id": deal["id"], "title": deal.get("title"), "rm_job": job, "status": kind, "detail": detail}
        if write:
            try:
                pd_mark_costed(deal["id"])
            except Exception as e:
                logger.error(f"deal {deal['id']} (job {job}): {e}")
                log_event(deal["id"], "RM_MATERIAL_FAILED", payload, f"Could not mark Costed: {e}")
                summary["failed"] += 1
                continue
            summary["marked costed"] += 1
            log_event(deal["id"], "RM_MATERIAL_COSTED", payload, action)
        else:
            summary["would mark costed"] += 1
            log_event(deal["id"], "RM_MATERIAL_DRYRUN", payload, action)
        logger.info(action)
    logger.info(f"costing sync ({'LIVE' if write else 'dry run'}): {dict(summary)}")
    return dict(summary)


def main():
    """CLI entry point; --dry-run forces a no-write pass regardless of .env."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    if not PIPEDRIVE_API_TOKEN:
        sys.exit("PIPEDRIVE_API_TOKEN is not set")
    run(dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    main()
