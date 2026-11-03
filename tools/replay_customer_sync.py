#!/usr/bin/env python3
"""
Re-send a Pipedrive → Rollmaster customer sync that failed.

Every call to /rm-customer-sync is logged in the events table with the exact
payload Pipedrive sent. When a create was refused for a reason that has since
cleared (Rollmaster's one client session was taken, Rollmaster was down…) the
stored payload can be posted again, through the same endpoint, so the customer
gets created, matched against legacy records, and written back to Pipedrive
exactly as if the automation had fired. Replaying one that has since been
added by hand is safe: the endpoint matches on phone/email first and updates
instead of creating.

Runs on the Pi, next to the app:

    venv/bin/python tools/replay_customer_sync.py --list [--since 2026-11-02]
    venv/bin/python tools/replay_customer_sync.py --replay 31562 31617 [--dry-run]

--list shows failed/refused customer events, one line per customer (the last
attempt), so you can pick the ids. --replay posts them to the local app with
the shared secret from .env; --dry-run prints the payloads instead.
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH   = Path(os.environ.get("DB_PATH", ROOT / "data" / "sync.db"))
APP_URL   = os.environ.get("REPLAY_APP_URL", "http://127.0.0.1:5001")
SECRET    = os.environ.get("RM_SYNC_SECRET", "")
FAILED    = ("RM_CUSTOMER_FAILED", "RM_CUSTOMER_AMBIGUOUS")


def payload_name(payload):
    """The customer name a payload carries, the way the app reads it (top level or nested one deep)."""
    flat = {}
    for k, v in (payload or {}).items():
        if isinstance(v, dict):
            for sk, sv in v.items():
                flat.setdefault(str(sk).strip().lower(), sv)
        flat.setdefault(str(k).strip().lower(), v)
    first = str(flat.get("first_name") or "").strip()
    last  = str(flat.get("last_name") or "").strip()
    return " ".join(p for p in (first, last) if p) or str(flat.get("name") or "").strip() or "?"


def load_event(conn, event_id):
    row = conn.execute("SELECT id, received_at, event_type, raw_json, action FROM events WHERE id=?",
                       (event_id,)).fetchone()
    if not row:
        sys.exit(f"event {event_id} not found in {DB_PATH}")
    return row


def list_failed(conn, since):
    rows = conn.execute(
        "SELECT id, received_at, event_type, raw_json, action FROM events "
        "WHERE event_type IN (?, ?) AND received_at >= ? ORDER BY id",
        (*FAILED, since)).fetchall()
    latest = {}   # name -> (row, attempts)
    for row in rows:
        try:
            payload = json.loads(row[3])
        except ValueError:
            payload = {}
        name = payload_name(payload).upper()
        prev = latest.get(name)
        latest[name] = (row, (prev[1] if prev else 0) + 1)
    if not latest:
        print(f"no failed customer syncs since {since}")
        return
    print(f"{'event':>7}  {'last attempt (UTC)':19}  {'tries':>5}  name  —  why")
    for name, (row, tries) in sorted(latest.items(), key=lambda kv: kv[1][0][0]):
        why = (row[4] or "")[:90]
        print(f"{row[0]:>7}  {row[1][:19]:19}  {tries:>5}  {name}  —  {why}")


def replay(conn, event_ids, dry_run):
    if not SECRET and not dry_run:
        sys.exit("RM_SYNC_SECRET is not set in .env; nothing posted")
    for event_id in event_ids:
        row = load_event(conn, event_id)
        payload = json.loads(row[3])
        name = payload_name(payload)
        if row[2] not in FAILED:
            print(f"[{event_id}] {name}: skipped — event type is {row[2]}, not a failed sync")
            continue
        if dry_run:
            print(f"[{event_id}] {name}: would POST {APP_URL}/rm-customer-sync")
            print(json.dumps(payload, indent=2))
            continue
        r = requests.post(f"{APP_URL}/rm-customer-sync", json=payload,
                          headers={"X-Sync-Key": SECRET}, timeout=180)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        status = body.get("status") or body.get("error") or "?"
        detail = body.get("cid") or body.get("candidates") or ""
        print(f"[{event_id}] {name}: HTTP {r.status_code} {status} {body.get('action', '')} {detail}"
              + ("  (branch session was busy; created on the shared session — check ship-to tax code)"
                 if body.get("branch_fallback") else ""))
        if r.status_code != 200:
            print("   ", json.dumps(body)[:400])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show failed customer syncs, one line per customer")
    ap.add_argument("--since", default="2000-01-01", help="with --list: only events on/after this date (UTC)")
    ap.add_argument("--replay", nargs="+", type=int, metavar="EVENT_ID", help="event ids to re-send")
    ap.add_argument("--dry-run", action="store_true", help="with --replay: print the payloads, post nothing")
    args = ap.parse_args()
    if not args.list and not args.replay:
        ap.error("give --list or --replay")
    if not DB_PATH.exists():
        sys.exit(f"no database at {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    if args.list:
        list_failed(conn, args.since)
    if args.replay:
        replay(conn, args.replay, args.dry_run)


if __name__ == "__main__":
    main()
