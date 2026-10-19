"""
Arrivy → Microsoft 365 calendars.

Every Arrivy task is copied into the Outlook calendar of each assigned person
who is mapped on the Users page (Arrivy entity → M365 account). Events are
written straight into the person's calendar through Graph (application
permission Calendars.ReadWrite) with no attendees, so nobody gets an invite.

The sync is a reconcile, not a replay: for a task it works out the events that
SHOULD exist (one per assigned M365 person per day the task runs) and makes
the calendars match — create, update, delete. That keeps it correct when a
webhook is missed, a crew is reassigned or a mapping changes. It runs:

  * per task, from the Arrivy webhook (queued to a worker thread, so a slow
    Graph call never holds up the webhook), and
  * as a sweep every SWEEP_MINUTES over tasks from yesterday to SWEEP_DAYS
    ahead, which also does the first backfill.

A multi-day task becomes one event per day with that day's hours (Arrivy's
start time to end time), so a three-day install doesn't block nights.

State: calendar_events(task_id, graph_id, day) → Outlook event id + a
fingerprint of what was written, so unchanged events aren't re-sent.

M365_CALENDAR_SYNC: "off" (default), "dry" (log what it would do, write
nothing), "on".
"""
import hashlib
import html
import json
import logging
import os
import queue
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from email_reports.graph_mail import get_access_token

log = logging.getLogger(__name__)

MODE          = os.environ.get("M365_CALENDAR_SYNC", "off").strip().lower()
DB_PATH       = os.environ.get("DB_PATH", "/home/admin/CreativeBot/data/sync.db")
ARRIVY_BASE   = "https://app.arrivy.com/api"
GRAPH_BASE    = "https://graph.microsoft.com/v1.0"
SWEEP_MINUTES = int(os.environ.get("M365_CALENDAR_SWEEP_MINUTES", "30"))
SWEEP_DAYS    = int(os.environ.get("M365_CALENDAR_SWEEP_DAYS", "60"))
LOCAL_TZ      = "America/Chicago"

# Arrivy template → what the event is called. Unlisted templates use the task title.
TEMPLATE_LABELS = {
    5395407346073600: "Install",
    5627485400596480: "Measure",
    5278551184506880: "Delivery",
    5546469558321152: "Inspection",
    4649593254445056: "Repair",
    6631019675910144: "Pickup",
    538634486456320:  "Customer Pickup",
}
# Arrivy statuses whose task no longer happens.
GONE_STATUSES = {"CANCELLED", "CANCEL"}

_namespace = uuid.UUID("6f1d3c52-6c7e-4c38-9a53-0a1f3e0c2b11")
_queue: "queue.Queue[int]" = queue.Queue()
_pending: set[int] = set()
_pending_lock = threading.Lock()
_task_locks: dict[int, threading.Lock] = {}
_started = False


# ---- DB ---------------------------------------------------------------------

def _conn():
    """sqlite connection to the shared sync.db (Row access by column name)."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_schema():
    """Create calendar_events if absent. Idempotent."""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                task_id     INTEGER NOT NULL,
                graph_id    TEXT    NOT NULL,
                day         TEXT    NOT NULL,
                event_id    TEXT    NOT NULL,
                fingerprint TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL,
                PRIMARY KEY (task_id, graph_id, day)
            );
            CREATE INDEX IF NOT EXISTS idx_cal_day ON calendar_events(day);
        """)


def _mapped_people(entity_ids):
    """{graph_id: display name} for the assigned Arrivy entities that are mapped to an active M365 account."""
    ids = [str(e) for e in entity_ids or []]
    if not ids:
        return {}
    with _conn() as c:
        rows = c.execute(
            f"SELECT u.m365_graph_id, COALESCE(u.display_name, m.display_name, u.username) AS name "
            f"FROM users u JOIN m365_users m ON m.graph_id = u.m365_graph_id AND m.active = 1 "
            f"WHERE u.arrivy_entity_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["m365_graph_id"]: r["name"] for r in rows}


# ---- Arrivy -----------------------------------------------------------------

def _arrivy_headers():
    return {"X-Auth-Key": os.environ.get("ARRIVY_API_KEY", ""),
            "X-Auth-Token": os.environ.get("ARRIVY_AUTH_TOKEN", "")}


def fetch_task(task_id):
    """The Arrivy task, or None when it has been deleted."""
    r = requests.get(f"{ARRIVY_BASE}/tasks/{task_id}", headers=_arrivy_headers(), timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    task = r.json()
    if not isinstance(task, dict) or not task.get("id"):
        return None
    return task


def fetch_tasks_between(start: date, end: date):
    """Every Arrivy task touching [start, end), fetched a week at a time."""
    tasks, day = {}, start
    while day < end:
        stop = min(day + timedelta(days=7), end)
        r = requests.get(f"{ARRIVY_BASE}/tasks", headers=_arrivy_headers(), timeout=120,
                         params={"from": f"{day}T00:00:00", "to": f"{stop}T00:00:00"})
        r.raise_for_status()
        for t in r.json() or []:
            if isinstance(t, dict) and t.get("id"):
                tasks[t["id"]] = t
        day = stop
    return list(tasks.values())


# ---- What the calendars should hold --------------------------------------------

def _zone(name):
    """ZoneInfo for Arrivy's zone name; legacy aliases like 'US/Central' aren't installed everywhere."""
    try:
        return ZoneInfo(name or LOCAL_TZ)
    except Exception:
        return ZoneInfo(LOCAL_TZ)


def _parse(iso, tz):
    """Arrivy '2026-10-26T08:00:00-05:00' → aware datetime (naive values read in tz)."""
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def task_slots(task):
    """
    [(day 'YYYY-MM-DD', start_utc, end_utc)] the task occupies, or [] when it
    isn't on the schedule (cancelled, unscheduled, no time). Multi-day tasks
    get one slot per day running from the start time to the end time.
    """
    if (str(task.get("status") or "").upper() in GONE_STATUSES or task.get("unscheduled")
            or task.get("is_without_datetime") or task.get("to_be_deleted") or task.get("is_archived")):
        return []
    start_s = task.get("start_datetime_original_iso_str") or task.get("start_datetime")
    end_s   = task.get("end_datetime_original_iso_str") or task.get("end_datetime")
    if not start_s:
        return []
    tz = _zone(task.get("start_datetime_timezone"))
    start = _parse(start_s, tz if task.get("start_datetime_original_iso_str") else timezone.utc).astimezone(tz)
    end = _parse(end_s, tz if task.get("end_datetime_original_iso_str") else timezone.utc).astimezone(tz) if end_s \
        else start + timedelta(minutes=int(task.get("duration") or 60))
    if end <= start:
        end = start + timedelta(minutes=int(task.get("duration") or 60))
    if start.date() == end.date():
        return [(start.date().isoformat(), start.astimezone(timezone.utc), end.astimezone(timezone.utc))]
    slots, d = [], start.date()
    day_start, day_end = start.time(), end.time()
    if day_end <= day_start:   # odd hours (e.g. ends before it starts): a plain 8h block, same day
        day_end = min(datetime.combine(d, day_start) + timedelta(hours=8),
                      datetime.combine(d, dtime(23, 59))).time()
    while d <= end.date():
        s = datetime.combine(d, day_start).replace(tzinfo=tz)
        e = datetime.combine(d, day_end).replace(tzinfo=tz)
        slots.append((d.isoformat(), s.astimezone(timezone.utc), e.astimezone(timezone.utc)))
        d += timedelta(days=1)
    return slots


def _deal_id(task):
    for i in task.get("external_integration_info") or []:
        if i.get("external_type") == "PIPEDRIVE" and str(i.get("external_id") or "").isdigit():
            return i["external_id"]
    ref = str(task.get("linked_external_ref") or task.get("external_id") or "")
    return ref if ref.isdigit() else None


def event_body(task, day_index, day_count, crew_names):
    """The Graph event resource for one day of a task (start/end are filled in by the caller)."""
    label = TEMPLATE_LABELS.get(task.get("template")) or ""
    customer = (task.get("customer_name") or " ".join(
        p for p in (task.get("customer_first_name"), task.get("customer_last_name")) if p) or "").strip()
    subject = f"{label}: {customer}" if label and customer else (task.get("title") or label or "Arrivy task")
    if day_count > 1:
        subject += f" (day {day_index + 1} of {day_count})"
    address = (task.get("customer_address") or ", ".join(p for p in (
        task.get("customer_address_line_1"), task.get("customer_city"),
        task.get("customer_state"), task.get("customer_zipcode")) if p) or "").strip()
    phone = task.get("customer_mobile_number") or task.get("customer_phone") or ""
    deal = _deal_id(task)
    rows = [
        ("Customer", customer), ("Phone", phone), ("Address", address),
        ("Arrivy", task.get("title") or ""), ("Assigned", ", ".join(sorted(crew_names))),
    ]
    parts = [f"<b>{html.escape(k)}:</b> {html.escape(str(v))}<br>" for k, v in rows if v]
    if deal:
        parts.append(f'<b>Pipedrive:</b> <a href="https://app.pipedrive.com/deal/{deal}">deal {deal}</a><br>')
    notes = (task.get("details") or "").strip()
    if notes:
        parts.append("<br>" + html.escape(notes).replace("\n", "<br>"))
    parts.append("<br><i>Synced from Arrivy by CreativeBot — changes made here are overwritten.</i>")
    return {
        "subject": subject[:255],
        "body": {"contentType": "HTML", "content": "".join(parts)},
        "location": {"displayName": address[:255]} if address else None,
        "showAs": "busy",
        "categories": ["Arrivy"],
    }


# ---- Graph ------------------------------------------------------------------

def _graph(method, path, **kw):
    r = requests.request(method, f"{GRAPH_BASE}{path}", timeout=30,
                         headers={"Authorization": f"Bearer {get_access_token()}",
                                  "Content-Type": "application/json"}, **kw)
    if r.status_code == 429 or r.status_code >= 500:   # throttled / transient: one polite retry
        time.sleep(min(int(r.headers.get("Retry-After", "5")), 30))
        r = requests.request(method, f"{GRAPH_BASE}{path}", timeout=30,
                             headers={"Authorization": f"Bearer {get_access_token()}",
                                      "Content-Type": "application/json"}, **kw)
    return r


# ---- Reconcile ----------------------------------------------------------------

def sync_task(task_id, task=None, fetched=False):
    """
    Make the calendars match one Arrivy task. Pass task (with fetched=True) when
    the caller already has it, e.g. from the sweep. Returns a short summary.
    """
    if MODE == "off":
        return "off"
    task_id = int(task_id)
    lock = _task_locks.setdefault(task_id, threading.Lock())
    with lock:
        if not fetched:
            task = fetch_task(task_id)
        want = {}   # (graph_id, day) → event resource
        if task:
            people = _mapped_people(task.get("entity_ids"))
            slots = task_slots(task) if people else []
            for gid in people:
                for i, (day, s, e) in enumerate(slots):
                    ev = event_body(task, i, len(slots), people.values())
                    ev["start"] = {"dateTime": s.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}
                    ev["end"]   = {"dateTime": e.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}
                    want[(gid, day)] = {k: v for k, v in ev.items() if v is not None}
        with _conn() as c:
            have = {(r["graph_id"], r["day"]): dict(r) for r in c.execute(
                "SELECT * FROM calendar_events WHERE task_id=?", (task_id,))}
        done = {"created": 0, "updated": 0, "deleted": 0, "failed": 0}
        for key, ev in want.items():
            fp = hashlib.sha1(json.dumps(ev, sort_keys=True).encode()).hexdigest()
            row = have.get(key)
            if row and row["fingerprint"] == fp:
                continue
            gid, day = key
            if MODE == "dry":
                log.info(f"calendar DRY RUN: would {'update' if row else 'create'} '{ev['subject']}' on {day} for {gid}")
                done["updated" if row else "created"] += 1
                continue
            try:
                event_id = row["event_id"] if row else None
                if event_id:
                    r = _graph("PATCH", f"/users/{gid}/events/{event_id}", json=ev)
                    if r.status_code == 404:          # deleted in Outlook — put it back
                        event_id = None
                    else:
                        r.raise_for_status()
                if not event_id:
                    # transactionId makes a retried create return the same event, not a duplicate.
                    tx = str(uuid.uuid5(_namespace, f"{task_id}:{gid}:{day}"))
                    r = _graph("POST", f"/users/{gid}/events", json={**ev, "transactionId": tx})
                    r.raise_for_status()
                    event_id = r.json()["id"]
                with _conn() as c:
                    c.execute("INSERT INTO calendar_events (task_id, graph_id, day, event_id, fingerprint, updated_at) "
                              "VALUES (?,?,?,?,?,?) ON CONFLICT(task_id, graph_id, day) DO UPDATE SET "
                              "event_id=excluded.event_id, fingerprint=excluded.fingerprint, updated_at=excluded.updated_at",
                              (task_id, gid, day, event_id, fp, datetime.utcnow().isoformat()))
                done["updated" if row else "created"] += 1
            except Exception as e:
                done["failed"] += 1
                log.warning(f"calendar: task {task_id} {day} for {gid} failed: {str(e)[:200]}")
        for key, row in have.items():
            if key in want:
                continue
            gid, day = key
            if MODE == "dry":
                log.info(f"calendar DRY RUN: would delete event for task {task_id} on {day} for {gid}")
                done["deleted"] += 1
                continue
            try:
                r = _graph("DELETE", f"/users/{gid}/events/{row['event_id']}")
                if r.status_code not in (204, 404):
                    r.raise_for_status()
                with _conn() as c:
                    c.execute("DELETE FROM calendar_events WHERE task_id=? AND graph_id=? AND day=?",
                              (task_id, gid, day))
                done["deleted"] += 1
            except Exception as e:
                done["failed"] += 1
                log.warning(f"calendar: deleting task {task_id} {day} for {gid} failed: {str(e)[:200]}")
        if any(done.values()):
            log.info(f"calendar: task {task_id} → {done}")
        return done


def sweep(days_back=1, days_ahead=None):
    """Reconcile every task from days_back ago to days_ahead out, plus any task we hold
    events for in that window that Arrivy no longer lists (deleted)."""
    if MODE == "off":
        return None
    days_ahead = SWEEP_DAYS if days_ahead is None else days_ahead
    start = date.today() - timedelta(days=days_back)
    end = date.today() + timedelta(days=days_ahead)
    tasks = fetch_tasks_between(start, end)
    seen = set()
    totals = {"tasks": 0, "created": 0, "updated": 0, "deleted": 0, "failed": 0}
    for t in tasks:
        seen.add(int(t["id"]))
        res = sync_task(t["id"], t, fetched=True)
        totals["tasks"] += 1
        for k in ("created", "updated", "deleted", "failed"):
            totals[k] += res.get(k, 0) if isinstance(res, dict) else 0
    with _conn() as c:
        orphans = [r[0] for r in c.execute(
            "SELECT DISTINCT task_id FROM calendar_events WHERE day >= ? AND day < ?",
            (start.isoformat(), end.isoformat()))]
    for tid in orphans:
        if tid not in seen:
            res = sync_task(tid)
            for k in ("created", "updated", "deleted", "failed"):
                totals[k] += res.get(k, 0) if isinstance(res, dict) else 0
    log.info(f"calendar sweep ({MODE}): {totals}")
    return totals


# ---- Background workers ------------------------------------------------------

def enqueue(task_id):
    """Queue one task for a reconcile (called from the Arrivy webhook). Duplicates collapse."""
    if MODE == "off" or not str(task_id or "").isdigit():
        return
    tid = int(task_id)
    with _pending_lock:
        if tid in _pending:
            return
        _pending.add(tid)
    _queue.put(tid)


def _worker():
    while True:
        tid = _queue.get()
        with _pending_lock:
            _pending.discard(tid)
        try:
            sync_task(tid)
        except Exception:
            log.exception(f"calendar: task {tid} sync crashed")


def _sweeper():
    time.sleep(60)   # let startup settle
    while True:
        try:
            sweep()
        except Exception:
            log.exception("calendar sweep crashed")
        time.sleep(SWEEP_MINUTES * 60)


def start():
    """Start the worker and sweeper threads (once per process). No-op when off."""
    global _started
    if _started or MODE == "off":
        return
    _started = True
    init_schema()
    threading.Thread(target=_worker, name="calendar-worker", daemon=True).start()
    threading.Thread(target=_sweeper, name="calendar-sweeper", daemon=True).start()
    log.info(f"calendar sync started ({MODE}); sweep every {SWEEP_MINUTES} min, {SWEEP_DAYS} days ahead")
