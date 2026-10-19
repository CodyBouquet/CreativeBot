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
    ahead, which also does the first backfill and is the only thing that
    catches notes edited in Arrivy (Arrivy sends no webhook for those).

A multi-day task becomes one event per day with that day's hours (Arrivy's
start time to end time), so a three-day install doesn't block nights.

State: calendar_events(task_id, graph_id, day) → Outlook event id + a
fingerprint of what was written, so unchanged events aren't re-sent.

Outlook → Arrivy: every READBACK_MINUTES the synced events are read back
from Outlook (poll_outlook). For someone with "Can reschedule from Outlook"
on the Users page, on a single-day Measure or Inspection: a time change is
pushed to Arrivy as a reschedule, and deleting the event deletes the task in
Arrivy (a delete, not a cancel, because the Pipedrive automations act on
deletes). Arrivy's own webhook then carries either on to Pipedrive and the
other assignees' calendars. Any other change (no permission, other task types, multi-day
jobs) is put back to what Arrivy says.

Notes: the event body is the task's Arrivy Details and nothing else. On
Measures and Inspections anyone mapped may edit it in Outlook; the read-back
pushes the whole body to Arrivy's Details (and so to every other assignee's
copy). On other task types the body is shown but edits aren't synced.

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
# Arrivy sends no webhook when only a task's Details (notes) change, so the sweep
# is what carries those to Outlook — keep it short.
SWEEP_MINUTES = int(os.environ.get("M365_CALENDAR_SWEEP_MINUTES", "5"))
SWEEP_DAYS    = int(os.environ.get("M365_CALENDAR_SWEEP_DAYS", "60"))
READBACK_MINUTES = int(os.environ.get("M365_CALENDAR_READBACK_MINUTES", "2"))
# Circuit breaker: more Outlook→Arrivy writes than this in one read-back means
# something systematic is wrong (not people editing), so none are sent.
MAX_ARRIVY_WRITES_PER_POLL = int(os.environ.get("M365_CALENDAR_MAX_WRITES", "3"))
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
# Templates an Outlook reschedule may write back to Arrivy (Measure, Inspection).
READBACK_TEMPLATES = {5627485400596480, 5546469558321152}
# Arrivy statuses whose task no longer happens.
GONE_STATUSES = {"CANCELLED", "CANCEL"}

_namespace = uuid.UUID("6f1d3c52-6c7e-4c38-9a53-0a1f3e0c2b11")
_queue: "queue.Queue[int]" = queue.Queue()
_pending: set[int] = set()
_pending_lock = threading.Lock()
_task_locks: dict[int, threading.Lock] = {}
_started = False
# Set by app.py: on_event(deal_id, task_id, event_type, payload, action) logs to the dashboard.
on_event = None


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
        # What was last written (UTC) and the task's shape, for reading Outlook changes back.
        cols = {r[1] for r in c.execute("PRAGMA table_info(calendar_events)")}
        for col in ("start_utc TEXT", "end_utc TEXT", "template INTEGER", "days INTEGER", "notes TEXT",
                    "notes_written TEXT"):
            if col.split()[0] not in cols:
                c.execute(f"ALTER TABLE calendar_events ADD COLUMN {col}")


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
    # The body is the Arrivy notes and nothing else: the name is in the subject,
    # the address in the location.
    notes = (task.get("details") or "").strip()
    parts = [html.escape(notes).replace("\n", "<br>")]
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
        meta = {}
        if task:
            people = _mapped_people(task.get("entity_ids"))
            slots = task_slots(task) if people else []
            for gid in people:
                for i, (day, s, e) in enumerate(slots):
                    ev = event_body(task, i, len(slots), people.values())
                    ev["start"] = {"dateTime": s.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}
                    ev["end"]   = {"dateTime": e.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}
                    want[(gid, day)] = {k: v for k, v in ev.items() if v is not None}
            meta = {"template": task.get("template"), "days": len(slots),
                    "notes_written": norm_notes(task.get("details"))}
        with _conn() as c:
            have = {(r["graph_id"], r["day"]): dict(r) for r in c.execute(
                "SELECT * FROM calendar_events WHERE task_id=?", (task_id,))}
        done = {"created": 0, "updated": 0, "deleted": 0, "failed": 0}
        backfill = []
        for key, ev in want.items():
            fp = hashlib.sha1(json.dumps(ev, sort_keys=True).encode()).hexdigest()
            row = have.get(key)
            if row and row["fingerprint"] == fp:
                if (row["start_utc"] is None or row.get("notes_written") is None) and MODE == "on":   # older rows
                    backfill.append((ev["start"]["dateTime"], ev["end"]["dateTime"], meta.get("template"),
                                     meta.get("days"), meta.get("notes_written"), task_id, *key))
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
                    c.execute("INSERT INTO calendar_events (task_id, graph_id, day, event_id, fingerprint, updated_at, "
                              "start_utc, end_utc, template, days, notes, notes_written) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                              "ON CONFLICT(task_id, graph_id, day) DO UPDATE SET event_id=excluded.event_id, "
                              "fingerprint=excluded.fingerprint, updated_at=excluded.updated_at, "
                              "start_utc=excluded.start_utc, end_utc=excluded.end_utc, "
                              "template=excluded.template, days=excluded.days, notes=excluded.notes, "
                              "notes_written=excluded.notes_written",
                              (task_id, gid, day, event_id, fp, datetime.utcnow().isoformat(),
                               ev["start"]["dateTime"], ev["end"]["dateTime"], meta.get("template"),
                               meta.get("days"), None, meta.get("notes_written")))
                done["updated" if row else "created"] += 1
            except Exception as e:
                done["failed"] += 1
                log.warning(f"calendar: task {task_id} {day} for {gid} failed: {str(e)[:200]}")
        if backfill:   # one short transaction, not a commit per row
            with _conn() as c:
                c.executemany("UPDATE calendar_events SET start_utc=?, end_utc=?, template=?, days=?, "
                              "notes_written=? WHERE task_id=? AND graph_id=? AND day=?", backfill)
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


# ---- Outlook → Arrivy ---------------------------------------------------------

def _record(task, action, event_type="CAL_OUTLOOK"):
    """Log a read-back outcome to the app log and the dashboard."""
    log.info(f"calendar read-back: {action}")
    if on_event:
        try:
            deal = _deal_id(task) if task else None
            on_event(int(deal) if deal else None, int(task["id"]) if task else None, event_type,
                     {"source": "outlook", "TITLE": (task or {}).get("title")}, action)
        except Exception:
            log.exception("calendar: dashboard log failed")


def _batch_get(paths):
    """GET many Graph paths via /$batch (20 per call); returns {path: (status, body)}.
    Bodies come back as plain text (Prefer header), which is what notes are read from."""
    out = {}
    for i in range(0, len(paths), 20):
        chunk = paths[i:i + 20]
        r = _graph("POST", "/$batch", json={"requests": [
            {"id": str(n), "method": "GET", "url": p,
             "headers": {"Prefer": 'outlook.body-content-type="text"'}} for n, p in enumerate(chunk)]})
        r.raise_for_status()
        for resp in r.json().get("responses", []):
            out[chunk[int(resp["id"])]] = (resp.get("status"), resp.get("body") or {})
    return out


def _utc(graph_dt):
    """Graph {'dateTime': '2026-10-20T17:00:00.0000000', 'timeZone': 'UTC'} → 'YYYY-MM-DDTHH:MM:SS'."""
    return (graph_dt or {}).get("dateTime", "")[:19]


def norm_notes(text):
    """Notes as compared across Arrivy and Outlook: no nbsp, no trailing spaces, at most one blank line."""
    lines = [ln.replace("\xa0", " ").rstrip() for ln in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out = []
    for ln in lines:
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def notes_key(text):
    """What counts as a change: the words on each non-blank line. Outlook shows the body as HTML,
    which collapses runs of spaces and tabs and adds blank lines, so none of that is an edit."""
    return "\n".join(" ".join(ln.split()) for ln in norm_notes(text).split("\n") if ln.strip())


def notes_from_body(text):
    """The notes from an event body read as text — the whole body."""
    return norm_notes(text or "")


def push_notes(task_id, notes):
    """Replace an Arrivy task's Details with notes edited in Outlook."""
    r = requests.put(f"{ARRIVY_BASE}/tasks/{task_id}", headers=_arrivy_headers(), timeout=30,
                     json={"details": notes})
    if r.status_code >= 400:
        raise RuntimeError(f"Arrivy PUT /tasks/{task_id} -> HTTP {r.status_code}: {r.text[:200]}")


def _can_reschedule(graph_id):
    with _conn() as c:
        row = c.execute("SELECT COALESCE(display_name, username) AS name, cal_can_reschedule FROM users "
                        "WHERE m365_graph_id=?", (graph_id,)).fetchone()
    return (row["name"], bool(row["cal_can_reschedule"])) if row else (graph_id, False)


def push_reschedule(task_id, start_utc, end_utc):
    """Move an Arrivy task to new times (UTC 'YYYY-MM-DDTHH:MM:SS'); returns the updated task."""
    task = fetch_task(task_id)
    tz = _zone(task.get("start_datetime_timezone")) if task else _zone(None)
    s = datetime.fromisoformat(start_utc).replace(tzinfo=timezone.utc).astimezone(tz)
    e = datetime.fromisoformat(end_utc).replace(tzinfo=timezone.utc).astimezone(tz)
    r = requests.put(f"{ARRIVY_BASE}/tasks/{task_id}", headers=_arrivy_headers(), timeout=30,
                     json={"start_datetime": s.isoformat(), "end_datetime": e.isoformat(),
                           "duration": int((e - s).total_seconds() // 60)})
    if r.status_code >= 400:
        raise RuntimeError(f"Arrivy PUT /tasks/{task_id} -> HTTP {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


def push_delete(task_id):
    """Delete an Arrivy task. Arrivy then sends TASK_DELETED, which the Pipedrive
    automations act on (a cancel they ignore). Already gone counts as done."""
    r = requests.delete(f"{ARRIVY_BASE}/tasks/{task_id}", headers=_arrivy_headers(), timeout=30)
    if r.status_code >= 400 and r.status_code != 404:
        raise RuntimeError(f"Arrivy DELETE /tasks/{task_id} -> HTTP {r.status_code}: {r.text[:200]}")


def _put_back(row, task, why):
    """Undo an Outlook-side change by rewriting the event from Arrivy on the next reconcile."""
    with _conn() as c:
        c.execute("UPDATE calendar_events SET fingerprint='' WHERE task_id=? AND graph_id=? AND day=?",
                  (row["task_id"], row["graph_id"], row["day"]))
    sync_task(row["task_id"])
    _record(task, why, "CAL_OUTLOOK_REVERTED")


def _would_write(row, status, body):
    """Which Arrivy write this event's Outlook state would cause ('delete', 'notes',
    'reschedule'), or None. Mirrors poll_outlook's rules without touching anything."""
    editable = row["template"] in READBACK_TEMPLATES and (row["days"] or 1) <= 1
    if status == 404 or (status == 200 and body.get("isCancelled")):
        return "delete" if editable and _can_reschedule(row["graph_id"])[1] else None
    if status != 200:
        return None
    if row["template"] in READBACK_TEMPLATES:
        ref = _notes_reference(row, body)
        edited = notes_from_body((body.get("body") or {}).get("content"))
        if ref is not None and notes_key(edited) != notes_key(ref):
            return "notes"
    new = (_utc(body.get("start")), _utc(body.get("end")))
    if all(new) and new != (row["start_utc"], row["end_utc"]) and editable and _can_reschedule(row["graph_id"])[1]:
        return "reschedule"
    return None


USER_EDIT_GRACE_SECONDS = 20   # Exchange stamps our own write a second or two after we send it


def _edited_after_write(row, body):
    """True when Outlook says the event changed after our last write to it — i.e. a person edited it."""
    lm = (body.get("lastModifiedDateTime") or "")[:19]
    if not lm or not row.get("updated_at"):
        return False
    try:
        return (datetime.fromisoformat(lm) - datetime.fromisoformat(row["updated_at"][:19])).total_seconds() \
            > USER_EDIT_GRACE_SECONDS
    except ValueError:
        return False


def _notes_reference(row, body):
    """What the notes should be compared against: the Outlook baseline once we have one; before that,
    what we wrote — but only if a person has touched the event (otherwise any difference is just how
    Outlook rendered our text, and becomes the baseline)."""
    if row["notes"] is not None:
        return row["notes"]
    if row.get("notes_written") is not None and _edited_after_write(row, body):
        return row["notes_written"]
    return None


def _unchanged_since_read(row):
    """True when the event hasn't been rewritten (by a sweep or webhook) since this read-back
    loaded it — otherwise what we read from Outlook may predate the rewrite."""
    with _conn() as c:
        cur = c.execute("SELECT updated_at FROM calendar_events WHERE task_id=? AND graph_id=? AND day=?",
                        (row["task_id"], row["graph_id"], row["day"])).fetchone()
    return bool(cur) and cur["updated_at"] == row["updated_at"]


def poll_outlook():
    """Read the synced, upcoming events back from Outlook and act on any changes (see module doc)."""
    if MODE != "on":
        return None
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM calendar_events WHERE day >= ? AND start_utc IS NOT NULL",
            (date.today().isoformat(),))]
    if not rows:
        return {"checked": 0}
    paths = {f"/users/{r['graph_id']}/events/{r['event_id']}?$select=start,end,isCancelled,lastModifiedDateTime"
             + (",body" if r["template"] in READBACK_TEMPLATES else ""): r for r in rows}
    results = _batch_get(list(paths))
    # Events rewritten while we were reading are skipped this round (next pass sees them fresh).
    paths = {p: r for p, r in paths.items() if _unchanged_since_read(r)}
    pending = [(row, kind) for path, row in paths.items()
               if (kind := _would_write(row, *results.get(path, (None, {}))))]
    if len(pending) > MAX_ARRIVY_WRITES_PER_POLL:
        # Too many at once to be people editing: send nothing to Arrivy, put every
        # one of those events back from Arrivy (which also resets its baseline).
        tasks = sorted({row["task_id"] for row, _ in pending})
        with _conn() as c:
            c.executemany("UPDATE calendar_events SET fingerprint='', notes=NULL WHERE task_id=?",
                          [(t,) for t in tasks])
        for t in tasks:
            sync_task(t)
        kinds = ", ".join(f"{k} {sum(1 for _, x in pending if x == k)}" for k in ("notes", "reschedule", "delete")
                          if any(x == k for _, x in pending))
        log.warning(f"calendar read-back: {len(pending)} Outlook changes in one pass ({kinds}) — "
                    f"over the limit of {MAX_ARRIVY_WRITES_PER_POLL}; nothing sent to Arrivy, events put back")
        _record(None, f"Outlook read-back paused: {len(pending)} changes at once ({kinds}) is more than people make "
                      f"in {READBACK_MINUTES} min — nothing sent to Arrivy, {len(tasks)} events put back from Arrivy",
                "CAL_OUTLOOK_PAUSED")
        return {"checked": len(rows), "paused": len(pending)}
    moved = reverted = recreated = deleted = noted = 0
    notes_done = set()
    for path, row in paths.items():
        status, body = results.get(path, (None, {}))
        if status == 404 or (status == 200 and body.get("isCancelled")):
            # Deleted in Outlook: deletes the task in Arrivy when permitted, else it's put back.
            task = fetch_task(row["task_id"])
            who, allowed = _can_reschedule(row["graph_id"])
            label = TEMPLATE_LABELS.get(row["template"]) or "task"
            if (task and allowed and row["template"] in READBACK_TEMPLATES and (row["days"] or 1) <= 1
                    and str(task.get("status") or "").upper() not in GONE_STATUSES):
                try:
                    push_delete(row["task_id"])
                except Exception as e:
                    _put_back(row, task, f"Arrivy refused {who}'s Outlook delete of {label} {row['task_id']}: {str(e)[:160]} — put back")
                    reverted += 1
                    continue
                with _conn() as c:   # the event is already gone from Outlook
                    c.execute("DELETE FROM calendar_events WHERE task_id=? AND graph_id=? AND day=?",
                              (row["task_id"], row["graph_id"], row["day"]))
                _record(task, f"{who} deleted {label} {row['task_id']} in Outlook — deleted in Arrivy",
                        "CAL_OUTLOOK_DELETED")
                enqueue(row["task_id"])   # clear the other assignees' copies
                deleted += 1
                continue
            with _conn() as c:
                c.execute("UPDATE calendar_events SET fingerprint='' WHERE task_id=? AND graph_id=? AND day=?",
                          (row["task_id"], row["graph_id"], row["day"]))
            sync_task(row["task_id"])
            if task and str(task.get("status") or "").upper() not in GONE_STATUSES:
                why = ("can't change appointments from Outlook" if not allowed else
                       "only single-day measures and inspections can be deleted there")
                _record(task, f"{who} deleted {label} {row['task_id']} in Outlook but {why} — put back",
                        "CAL_OUTLOOK_REVERTED")
            recreated += 1
            continue
        if status != 200:
            continue
        edited = notes_from_body((body.get("body") or {}).get("content")) \
            if row["template"] in READBACK_TEMPLATES and row["task_id"] not in notes_done else None
        ref = _notes_reference(row, body) if edited is not None else None
        if edited is not None and ref is None:
            # First look since the event was written and nobody has touched it: what Outlook
            # shows is the baseline — unless a rewrite landed after we read it (next pass takes it).
            with _conn() as c:
                c.execute("UPDATE calendar_events SET notes=? WHERE task_id=? AND graph_id=? AND day=? "
                          "AND notes IS NULL AND updated_at=?",
                          (edited, row["task_id"], row["graph_id"], row["day"], row["updated_at"]))
        elif edited is not None:
            if notes_key(edited) != notes_key(ref) and _unchanged_since_read(row):
                who, _ = _can_reschedule(row["graph_id"])
                label = TEMPLATE_LABELS.get(row["template"]) or "task"
                task = fetch_task(row["task_id"])
                if task and str(task.get("status") or "").upper() not in GONE_STATUSES:
                    try:
                        push_notes(row["task_id"], notes_key(edited))   # without the blank lines Outlook adds
                        notes_done.add(row["task_id"])   # other copies still show the old text this round
                        _record(task, f"{who} edited the notes on {label} {row['task_id']} in Outlook — updated in Arrivy",
                                "CAL_OUTLOOK_NOTES")
                        if (_utc(body.get("start")), _utc(body.get("end"))) == (row["start_utc"], row["end_utc"]):
                            sync_task(row["task_id"])   # rewrite every copy now, so none can push stale notes back
                        else:
                            enqueue(row["task_id"])     # the time edit below is handled first
                        noted += 1
                    except Exception as e:
                        _put_back(row, task, f"Arrivy refused {who}'s Outlook notes on {label} {row['task_id']}: {str(e)[:160]} — put back")
                        reverted += 1
                        continue
        new_s, new_e = _utc(body.get("start")), _utc(body.get("end"))
        if (new_s, new_e) == (row["start_utc"], row["end_utc"]) or not new_s or not new_e:
            continue
        task = fetch_task(row["task_id"])
        who, allowed = _can_reschedule(row["graph_id"])
        label = (TEMPLATE_LABELS.get(row["template"]) or "task")
        if not task:
            continue   # task deleted in Arrivy; the next reconcile removes the event
        if not allowed:
            _put_back(row, task, f"{who} moved {label} {row['task_id']} in Outlook but can't reschedule from Outlook — put back")
            reverted += 1
        elif row["template"] not in READBACK_TEMPLATES or (row["days"] or 1) > 1:
            _put_back(row, task, f"{who} moved {label} {row['task_id']} in Outlook — only single-day measures and inspections can be rescheduled there — put back")
            reverted += 1
        elif new_e <= new_s:
            _put_back(row, task, f"{who} gave {label} {row['task_id']} an end before its start in Outlook — put back")
            reverted += 1
        else:
            try:
                push_reschedule(row["task_id"], new_s, new_e)
            except Exception as e:
                _put_back(row, task, f"Arrivy refused {who}'s Outlook reschedule of {label} {row['task_id']}: {str(e)[:160]} — put back")
                reverted += 1
                continue
            with _conn() as c:   # Outlook already shows the new time; don't treat it as a change again
                c.execute("UPDATE calendar_events SET start_utc=?, end_utc=? WHERE task_id=? AND graph_id=? AND day=?",
                          (new_s, new_e, row["task_id"], row["graph_id"], row["day"]))
            tz = _zone(task.get("start_datetime_timezone"))
            fmt = lambda u: datetime.fromisoformat(u).replace(tzinfo=timezone.utc).astimezone(tz).strftime("%a %m/%d %I:%M %p")
            _record(task, f"{who} rescheduled {label} {row['task_id']} from Outlook to {fmt(new_s)}–{fmt(new_e)[-8:]} — updated in Arrivy",
                    "CAL_OUTLOOK_RESCHEDULED")
            enqueue(row["task_id"])   # bring every other assignee's copy along
            moved += 1
    if moved or reverted or recreated or deleted or noted:
        log.info(f"calendar read-back: checked {len(rows)}, rescheduled {moved}, deleted {deleted}, "
                 f"notes {noted}, put back {reverted}, recreated {recreated}")
    return {"checked": len(rows), "rescheduled": moved, "deleted": deleted, "notes": noted,
            "reverted": reverted, "recreated": recreated}


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


def _reader():
    time.sleep(90)
    while True:
        try:
            poll_outlook()
        except Exception:
            log.exception("calendar read-back crashed")
        time.sleep(READBACK_MINUTES * 60)


def start():
    """Start the worker and sweeper threads (once per process). No-op when off."""
    global _started
    if _started or MODE == "off":
        return
    _started = True
    init_schema()
    # Re-read every notes baseline from Outlook before trusting it: the body layout
    # (or how it's read) may have changed with this deploy.
    with _conn() as c:
        c.execute("UPDATE calendar_events SET notes=NULL")
    threading.Thread(target=_worker, name="calendar-worker", daemon=True).start()
    threading.Thread(target=_sweeper, name="calendar-sweeper", daemon=True).start()
    if MODE == "on":
        threading.Thread(target=_reader, name="calendar-reader", daemon=True).start()
    log.info(f"calendar sync started ({MODE}); sweep every {SWEEP_MINUTES} min, {SWEEP_DAYS} days ahead")
