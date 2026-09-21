from flask import Flask, request, jsonify, render_template, session, redirect, url_for, Response
from dotenv import load_dotenv
import re
import requests
import logging
import os
import sqlite3
import json
import hmac
import hashlib
import subprocess
import shutil
from datetime import datetime
from pathlib import Path
from functools import wraps
import threading
import queue

load_dotenv()

from email_reports import bp as reports_bp
import rollmaster

app = Flask(__name__)
_secret = os.environ.get("FLASK_SECRET_KEY", "")
if not _secret:
    import secrets as _secrets
    _secret = _secrets.token_hex(32)
    logging.warning("FLASK_SECRET_KEY not set — sessions will not persist across restarts")
app.secret_key = _secret

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONFIG — set these as environment variables on the Pi
# ---------------------------------------------------------------------------
PIPEDRIVE_API_TOKEN       = os.environ.get("PIPEDRIVE_API_TOKEN", "")
ARRIVY_API_KEY            = os.environ.get("ARRIVY_API_KEY", "")
ARRIVY_AUTH_TOKEN         = os.environ.get("ARRIVY_AUTH_TOKEN", "")
GITHUB_WEBHOOK_SECRET     = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
REPO_PATH                 = os.environ.get("REPO_PATH", "/home/admin/CreativeBot")
SERVICE_NAME              = os.environ.get("SERVICE_NAME", "arrivy-sync")
DB_PATH                   = os.environ.get("DB_PATH", "/home/admin/CreativeBot/data/sync.db")
# Only webhooks (Arrivy + Pipedrive) for deals with id > MIN_DEAL_ID will be acted
# on. Historical deals are still logged but never touched in Pipedrive. Bump this
# if we ever need to roll back the rollout cutoff.
MIN_DEAL_ID               = 30243

_REQUIRED_ENV = {
    "PIPEDRIVE_API_TOKEN": PIPEDRIVE_API_TOKEN,
    "ARRIVY_API_KEY":      ARRIVY_API_KEY,
    "ARRIVY_AUTH_TOKEN":   ARRIVY_AUTH_TOKEN,
    "GITHUB_WEBHOOK_SECRET": GITHUB_WEBHOOK_SECRET,
}
for _var, _val in _REQUIRED_ENV.items():
    if not _val:
        logging.warning(f"Environment variable {_var} is not set — related features will fail")

# Arrivy template IDs → task type
# Note: repair is treated as install (shares Pipedrive install fields & stages).
# Pickup and customer pickup are treated as delivery (share delivery_date field).
REPAIR_TEMPLATE_ID = 4649593254445056
TEMPLATE_MAP = {
    5395407346073600: "install",
    5627485400596480: "measure",
    5278551184506880: "delivery",
    5546469558321152: "inspection",
    REPAIR_TEMPLATE_ID: "install",    # repair — same process as install
    6631019675910144: "delivery",     # pickup
    538634486456320:  "delivery",     # customer pickup
}

# Pipedrive custom field keys
PD_FIELDS = {
    "install_start":   "197d71fa84fd5221fa4a875fbac9526c1d554139",
    "install_part2":   "7492f008b747af364836514d752961176f1f0307",
    "install_phase":   "cdf1c74d66c5796284a2bbcfcef8080975d0f19e",
    "measure_date":    "e23dc895627529b276d3b1b0ec7c8acc75317b1c",
    "delivery_date":   "d0d424fcacbdf264297a050ff96a799823316d9f",
    "delivery_status": "adacf74cda1c48bfc6fa4df2c064a1b257f3b284",
}

INSTALL_COMPLETE_STAGE_ID      = 12
INSTALL_SCHEDULED_STAGE_ID     = 10
INSTALL_READY_TO_SCHEDULE_ID   = 9
MEASURE_COMPLETE_STAGE_ID      = 5
MEASURE_SCHEDULED_STAGE_ID     = 4    # "Measure scheduled" — the only stage a measure rollback may leave
MEASURE_ROLLBACK_STAGE_ID      = 70   # "in store contact" — when measure is cancelled/deleted
# Scheduling a measure only advances a deal that is still in an early sales stage; anything
# further along keeps the stage it already has.
MEASURE_SCHEDULABLE_STAGE_IDS  = {1, 70, 2}   # Lead In, In Store - Contact, Via Phone - Contact
INSPECTION_SCHEDULED_STAGE_ID  = 36
INSPECTION_COMPLETE_STAGE_ID   = 37
INSPECTION_ROLLBACK_STAGE_ID   = 50   # "Customer Contacted/Attempted" — when inspection is cancelled/deleted
DELIVERY_STATUS_COMPLETE_ID    = 114  # option ID for "Complete" in the delivery_status dropdown

INSTALL_PHASE_OPTIONS = {
    "final":   37,
    "partial": 65,
}

# ---------------------------------------------------------------------------
# PIPEDRIVE → ROLLMASTER CUSTOMER SYNC
# ---------------------------------------------------------------------------
# Shared secret the Pipedrive automation must present (X-Sync-Key header or ?key=).
# Unlike the other webhooks, this one CREATES records in the ERP, so it is closed
# by default: with no secret configured the endpoint refuses every request.
RM_SYNC_SECRET = os.environ.get("RM_SYNC_SECRET", "")
# Master switch. Off means the endpoint validates, maps and mints the customer id
# but does NOT write to Rollmaster — it returns exactly what it would have sent,
# so the automation can be tested end to end without creating anything.
RM_CUSTOMER_SYNC_ENABLED = os.environ.get("RM_CUSTOMER_SYNC_ENABLED", "0") == "1"
# Fixed values every synced customer gets. These are account-setup fields with no
# Pipedrive equivalent — confirm each against how a customer is keyed in by hand
# before enabling the sync. Any of them can be overridden per-field from the
# webhook payload.
RM_CUSTOMER_DEFAULTS = {
    "C_WHSE":                os.environ.get("RM_C_WHSE", ""),
    "C_STAT":                os.environ.get("RM_C_STAT", ""),
    "C_CUSTTYPE":            os.environ.get("RM_C_CUSTTYPE", ""),
    "C_TERRFLAG":            os.environ.get("RM_C_TERRFLAG", ""),
    "C_TERR":                os.environ.get("RM_C_TERR", ""),
    "C_PRICE_LEVEL_DEFAULT": os.environ.get("RM_C_PRICE_LEVEL", ""),
    "C_PROMPMGMT_CO":        os.environ.get("RM_C_PROMPMGMT_CO", ""),
    # Salesperson every synced customer is filed under (e.g. "HA" = house account).
    "C_SLSID":               os.environ.get("RM_C_SLSID", ""),
}
# Pipedrive PERSON custom field ("Customer ID") that receives the new Rollmaster
# customer id. The automation can't capture the webhook response, so the app
# writes it back itself after a successful create. Blank disables the write-back.
RM_PD_CID_FIELD = os.environ.get("RM_PD_CID_FIELD", "509740a9dad0eab9c7c83842beefb2eda43ae199")
# Optional: Pipedrive deal owner → Rollmaster salesperson id (C_SLSID, e.g. "MRB",
# "AMB"). JSON object in the env var, keyed by owner name or Pipedrive user id. A
# matching owner overrides RM_C_SLSID; leave it {} to file everyone under the default.
try:
    RM_SALESPERSON_MAP = json.loads(os.environ.get("RM_SALESPERSON_MAP", "{}"))
except ValueError:
    RM_SALESPERSON_MAP = {}
    logging.warning("RM_SALESPERSON_MAP is not valid JSON — salesperson mapping disabled")

ALLOWED_DASHBOARD_IPS = {"127.0.0.1", "::1", "10.54.10.135"}
DASHBOARD_ENDPOINTS  = {"landing", "sync_dashboard", "logs", "users", "pin_page", "verify_pin", "change_pin", "logout",
                        "api_stats", "api_stream", "api_logs", "settings_page",
                        "api_settings", "api_sync_all", "api_users", "api_user_delete", "api_access_log",
                        "reports.reports_home"}

# SSE client queues
_sse_clients      = set()
_sse_clients_lock = threading.Lock()

def sse_notify():
    """Push a 'refresh' event to every connected dashboard SSE client; drop any whose queue is full."""
    with _sse_clients_lock:
        dead = set()
        for q in _sse_clients:
            try:
                q.put_nowait("refresh")
            except queue.Full:
                dead.add(q)
        _sse_clients.difference_update(dead)

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
def get_db():
    """Open a sqlite connection to DB_PATH (creating the parent dir), with Row access by column name."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create all tables/indexes if absent and run in-place migrations (legacy PIN → users, current_date → task_date, new columns). Idempotent; called at startup."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT    NOT NULL,
                deal_id     INTEGER,
                task_id     INTEGER,
                event_type  TEXT,
                task_type   TEXT,
                raw_json    TEXT    NOT NULL,
                action      TEXT,
                archived    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS task_state (
                task_id       INTEGER PRIMARY KEY,
                deal_id       INTEGER NOT NULL,
                task_type     TEXT    NOT NULL,
                task_date     TEXT,
                install_phase TEXT,
                status        TEXT    NOT NULL DEFAULT 'active',
                last_updated  TEXT    NOT NULL,
                archived      INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT    NOT NULL UNIQUE,
                pin        TEXT    NOT NULL UNIQUE,
                role       TEXT    NOT NULL DEFAULT 'user',
                created_at TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS access_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                username     TEXT    NOT NULL,
                logged_in_at TEXT    NOT NULL,
                ip_address   TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_events_deal  ON events(deal_id);
            CREATE INDEX IF NOT EXISTS idx_task_deal    ON task_state(deal_id);
            CREATE INDEX IF NOT EXISTS idx_access_user  ON access_log(username);

            INSERT OR IGNORE INTO settings (key, value) VALUES ('pin', '0000');
        """)
        # Migrate legacy PIN from settings → admin user
        pin_row = conn.execute("SELECT value FROM settings WHERE key='pin'").fetchone()
        if pin_row:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO users (username, pin, role, created_at) VALUES (?,?,?,?)",
                    ("admin", pin_row["value"], "admin", datetime.utcnow().isoformat())
                )
            except Exception:
                pass
        # Migrate: rename current_date → task_date if needed
        cols = [r[1] for r in conn.execute("PRAGMA table_info(task_state)").fetchall()]
        if "current_date" in cols and "task_date" not in cols:
            conn.execute("ALTER TABLE task_state ADD COLUMN task_date TEXT")
            conn.execute('UPDATE task_state SET task_date = current_date')
        elif "task_date" not in cols:
            conn.execute("ALTER TABLE task_state ADD COLUMN task_date TEXT")
        if "install_phase" not in cols:
            conn.execute("ALTER TABLE task_state ADD COLUMN install_phase TEXT")
        # Migrate: add action column to events if needed
        ev_cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
        if "action" not in ev_cols:
            conn.execute("ALTER TABLE events ADD COLUMN action TEXT")
    logger.info(f"Database initialised at {DB_PATH}")

def get_setting(key):
    """Return the value for a settings key, or None if unset."""
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

def set_setting(key, value):
    """Insert or overwrite a single settings key/value pair."""
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

def store_event(conn, deal_id, task_id, event_type, task_type, raw_payload, action=None):
    """Append a row to the events audit log (raw payload stored as JSON); returns the new event id."""
    cur = conn.execute(
        """INSERT INTO events (received_at, deal_id, task_id, event_type, task_type, raw_json, action)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (datetime.utcnow().isoformat(), deal_id, task_id, event_type, task_type,
         json.dumps(raw_payload), action)
    )
    return cur.lastrowid

def update_event_action(conn, event_id, action):
    """Set the human-readable action summary on an already-stored event."""
    conn.execute("UPDATE events SET action=? WHERE id=?", (action, event_id))

def get_task_state(conn, task_id):
    """Return the task_state row for a task id, or None."""
    return conn.execute("SELECT * FROM task_state WHERE task_id = ?", (task_id,)).fetchone()

def upsert_task_state(conn, task_id, deal_id, task_type, task_date, status="active", install_phase=None):
    """Insert or update a task's tracked state. task_type follows the incoming event so a task whose template was corrected in Arrivy (e.g. measure → install) is re-filed correctly. install_phase is preserved (COALESCE) when the new value is None so it's never accidentally cleared."""
    conn.execute(
        """INSERT INTO task_state (task_id, deal_id, task_type, task_date, install_phase, status, last_updated)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(task_id) DO UPDATE SET
               task_type     = excluded.task_type,
               task_date     = excluded.task_date,
               install_phase = COALESCE(excluded.install_phase, task_state.install_phase),
               status        = excluded.status,
               last_updated  = excluded.last_updated""",
        (task_id, deal_id, task_type, task_date, install_phase, status, datetime.utcnow().isoformat())
    )

def archive_deal(conn, deal_id):
    """Mark all events and task_state rows for a deal as archived (hidden from dashboard/sync), e.g. when the deal is won or lost."""
    conn.execute("UPDATE events     SET archived = 1 WHERE deal_id = ?", (deal_id,))
    conn.execute("UPDATE task_state SET archived = 1 WHERE deal_id = ?", (deal_id,))
    logger.info(f"Archived deal {deal_id}")

# ---------------------------------------------------------------------------
# PIPEDRIVE
# ---------------------------------------------------------------------------
PD_BASE = "https://api.pipedrive.com/v1"

def pd_get_deal(deal_id):
    """Fetch a single deal from the Pipedrive API; raises if the request or API response fails."""
    r = requests.get(f"{PD_BASE}/deals/{deal_id}", params={"api_token": PIPEDRIVE_API_TOKEN})
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise Exception(f"Pipedrive get deal failed: {data}")
    return data["data"]

def pd_update_deal(deal_id, fields):
    """Update fields on a Pipedrive deal. Historical deals (id <= MIN_DEAL_ID) are blocked and only logged, never written. Returns the updated deal data, or None if blocked."""
    if int(deal_id) <= MIN_DEAL_ID:
        logger.warning(f"BLOCKED Pipedrive update on historical deal {deal_id} (<= {MIN_DEAL_ID}): {fields}")
        with get_db() as conn:
            store_event(conn, deal_id, None, "BLOCKED", None, {"fields": {k: v for k, v in fields.items()}},
                        action=f"BLOCKED — historical deal ({deal_id} <= {MIN_DEAL_ID})")
        sse_notify()
        return None
    r = requests.put(f"{PD_BASE}/deals/{deal_id}",
                     params={"api_token": PIPEDRIVE_API_TOKEN}, json=fields)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise Exception(f"Pipedrive update deal failed: {data}")
    logger.info(f"Updated deal {deal_id}: {fields}")
    return data["data"]

def pd_update_person(person_id, fields):
    """Update fields on a Pipedrive person; raises if the request or API response fails."""
    r = requests.put(f"{PD_BASE}/persons/{person_id}",
                     params={"api_token": PIPEDRIVE_API_TOKEN}, json=fields)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise Exception(f"Pipedrive update person failed: {data}")
    logger.info(f"Updated person {person_id}: {fields}")
    return data["data"]


def pd_get_person(person_id):
    """Fetch a single person from the Pipedrive API; raises if the request or API response fails."""
    r = requests.get(f"{PD_BASE}/persons/{person_id}", params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise Exception(f"Pipedrive get person failed: {data}")
    return data["data"]


def _scrub_token(text):
    """Remove the Pipedrive API token from a message before it reaches a log or the events table."""
    return str(text).replace(PIPEDRIVE_API_TOKEN, "***") if PIPEDRIVE_API_TOKEN else str(text)


def pd_move_stage(deal_id, stage_id):
    """Move a deal to a different pipeline stage (subject to the same MIN_DEAL_ID gating as pd_update_deal)."""
    pd_update_deal(deal_id, {"stage_id": stage_id})

# ---------------------------------------------------------------------------
# DATE HELPERS
# ---------------------------------------------------------------------------
def parse_arrivy_date(date_str):
    """Normalize an Arrivy ISO datetime string to a 'YYYY-MM-DD' date (timezone stripped for Python <3.11). Returns None on empty/unparseable input."""
    if not date_str:
        return None
    try:
        # Strip timezone offset so fromisoformat works on Python < 3.11
        clean = date_str[:19]
        return datetime.fromisoformat(clean).strftime("%Y-%m-%d")
    except Exception:
        return None

def dates_match(a, b):
    """True if two Arrivy datetime strings resolve to the same calendar day (both must be non-empty and parseable)."""
    da = parse_arrivy_date(a) if a else None
    db = parse_arrivy_date(b) if b else None
    return bool(da and db and da == db)

# ---------------------------------------------------------------------------
# TASK HANDLERS
# ---------------------------------------------------------------------------
def move_stage_if_in(deal_id, from_stage_ids, to_stage_id, label=None):
    """Move a deal to to_stage_id, but ONLY if it currently sits in one of from_stage_ids.
    Arrivy events are a poor authority on where a deal belongs: a deal that has already moved
    on (measure complete, install, delivery, …) must not be dragged backwards because someone
    cleaned up a stale task, nor pulled sideways out of a later stage because a task was
    scheduled. Returns a short summary fragment for the event log. If the current stage can't
    be read the move is skipped — leaving the stage alone is always the safer failure."""
    dest = label or f"stage {to_stage_id}"
    try:
        stage_id = pd_get_deal(deal_id).get("stage_id")
    except Exception as e:
        logger.warning(f"Could not read stage for deal {deal_id}, skipping move to {dest}: {e}")
        return "stage left alone (stage unreadable)"
    if stage_id is not None and int(stage_id) in from_stage_ids:
        pd_move_stage(deal_id, to_stage_id)
        return f"moved to {dest}"
    logger.info(f"Deal {deal_id} is in stage {stage_id}, not {sorted(from_stage_ids)} — skipped move to {dest}")
    return f"stage left alone (stage {stage_id})"

def handle_measure(conn, event_type, deal_id, task_id, object_date):
    """Apply an Arrivy measure-task event to the deal: set/clear the measure_date field and advance/roll back the stage by event type. Returns a short action summary string."""
    date = parse_arrivy_date(object_date)
    if event_type in ("TASK_CREATED", "TASK_UPDATED", "TASK_RESCHEDULED"):
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: date})
        upsert_task_state(conn, task_id, deal_id, "measure", date)
        moved = move_stage_if_in(deal_id, MEASURE_SCHEDULABLE_STAGE_IDS,
                                 MEASURE_SCHEDULED_STAGE_ID, "Measure scheduled")
        return f"Set measure date → {date}, {moved}"
    elif event_type == "TASK_DELETED":
        delete_task_state(conn, task_id)
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: None})
        return f"Cleared measure date, {move_stage_if_in(deal_id, {MEASURE_SCHEDULED_STAGE_ID}, MEASURE_ROLLBACK_STAGE_ID, 'In Store - Contact')}"
    elif event_type == "TASK_CANCELLED":
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: None})
        upsert_task_state(conn, task_id, deal_id, "measure", date, status="cancelled")
        return f"Cleared measure date, {move_stage_if_in(deal_id, {MEASURE_SCHEDULED_STAGE_ID}, MEASURE_ROLLBACK_STAGE_ID, 'In Store - Contact')}"
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "measure", date, status="completed")
        pd_move_stage(deal_id, MEASURE_COMPLETE_STAGE_ID)
        return "Moved to Measure Complete"

def handle_inspection(conn, event_type, deal_id, task_id, object_date):
    """Apply an Arrivy inspection-task event to the deal: set/clear the date (reuses the measure_date field) and move between scheduled/complete/rollback stages. Returns an action summary string."""
    date = parse_arrivy_date(object_date)
    if event_type in ("TASK_CREATED", "TASK_UPDATED", "TASK_RESCHEDULED"):
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: date})
        upsert_task_state(conn, task_id, deal_id, "inspection", date)
        pd_move_stage(deal_id, INSPECTION_SCHEDULED_STAGE_ID)
        return f"Set inspection date → {date}, moved to Scheduled"
    elif event_type == "TASK_DELETED":
        delete_task_state(conn, task_id)
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: None})
        return f"Cleared inspection date, {move_stage_if_in(deal_id, {INSPECTION_SCHEDULED_STAGE_ID}, INSPECTION_ROLLBACK_STAGE_ID, 'Customer Contacted/Attempted')}"
    elif event_type == "TASK_CANCELLED":
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: None})
        upsert_task_state(conn, task_id, deal_id, "inspection", date, status="cancelled")
        return f"Cleared inspection date, {move_stage_if_in(deal_id, {INSPECTION_SCHEDULED_STAGE_ID}, INSPECTION_ROLLBACK_STAGE_ID, 'Customer Contacted/Attempted')}"
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "inspection", date, status="completed")
        pd_move_stage(deal_id, INSPECTION_COMPLETE_STAGE_ID)
        return "Moved to Inspection Complete"

def delete_task_state(conn, task_id):
    """Hard-delete a task's tracked state row (used on TASK_DELETED)."""
    conn.execute("DELETE FROM task_state WHERE task_id=?", (task_id,))

def handle_delivery(conn, event_type, deal_id, task_id, object_date):
    """Apply an Arrivy delivery/pickup-task event to the deal: set/clear delivery_date or mark delivery_status complete. Returns an action summary string."""
    date = parse_arrivy_date(object_date)
    if event_type in ("TASK_CREATED", "TASK_UPDATED", "TASK_RESCHEDULED"):
        pd_update_deal(deal_id, {PD_FIELDS["delivery_date"]: date})
        upsert_task_state(conn, task_id, deal_id, "delivery", date)
        return f"Set delivery date → {date}"
    elif event_type == "TASK_DELETED":
        delete_task_state(conn, task_id)
        pd_update_deal(deal_id, {PD_FIELDS["delivery_date"]: None})
        return "Cleared delivery date"
    elif event_type == "TASK_CANCELLED":
        pd_update_deal(deal_id, {PD_FIELDS["delivery_date"]: None})
        upsert_task_state(conn, task_id, deal_id, "delivery", date, status="cancelled")
        return "Cleared delivery date"
    elif event_type == "TASK_COMPLETED":
        pd_update_deal(deal_id, {PD_FIELDS["delivery_status"]: DELIVERY_STATUS_COMPLETE_ID})
        upsert_task_state(conn, task_id, deal_id, "delivery", date, status="completed")
        return "Marked delivery complete"

def recalc_install(conn, deal_id):
    """Recompute a deal's two install date fields (and install_phase) from its active install tasks, earliest first. Rows with a cleared date are dropped; install_phase is only written, never cleared. Returns the ordered list of install dates."""
    rows = conn.execute(
        "SELECT task_date, install_phase FROM task_state WHERE deal_id=? AND task_type='install' AND status='active' AND archived=0 ORDER BY task_date",
        (deal_id,)
    ).fetchall()
    # Drop rows whose task_date was cleared so dates[0]/[1] stay accurate when a
    # TASK_UPDATED webhook clears one of several scheduled installs.
    rows = [r for r in rows if r["task_date"]]
    dates  = [r["task_date"] for r in rows]
    phase  = rows[0]["install_phase"] if rows else None
    phase_id = INSTALL_PHASE_OPTIONS.get(phase.lower()) if phase else None
    logger.info(f"recalc_install: deal={deal_id} dates={dates} phase={phase!r} phase_id={phase_id}")
    update = {
        PD_FIELDS["install_start"]: dates[0]  if len(dates) > 0 else None,
        PD_FIELDS["install_part2"]: dates[1]  if len(dates) > 1 else None,
    }
    # Never clear install_phase — only write it when we have a known phase.
    if phase_id is not None:
        update[PD_FIELDS["install_phase"]] = phase_id
    pd_update_deal(deal_id, update)
    return dates

# recalc_measure / recalc_delivery exist for one situation: a scheduler picks the
# wrong Arrivy template, saves it, then corrects it and saves again. The first save
# files the task under the wrong type (e.g. "measure") and pushes that type's date to
# Pipedrive. When the corrected event re-files the task to its real type, the old
# type's date field is now stale. Rather than blanking it, we recompute it from the
# deal's OTHER tasks of that type that we already tracked from earlier webhooks —
# restoring the legitimate date (or None if no such task remains).
def recalc_measure(conn, deal_id):
    """Recompute a deal's measure_date from its tracked measure tasks, choosing the most
    recent non-cancelled one (latest task_date; completed tasks count). Returns that date,
    or None if no measure task remains — used when a task's template is corrected away
    from measure so the field reflects the real measure instead of the abandoned one."""
    row = conn.execute(
        "SELECT task_date FROM task_state "
        "WHERE deal_id=? AND task_type='measure' AND status!='cancelled' "
        "AND archived=0 AND task_date IS NOT NULL "
        "ORDER BY task_date DESC LIMIT 1",
        (deal_id,)
    ).fetchone()
    date = row["task_date"] if row else None
    logger.info(f"recalc_measure: deal={deal_id} measure_date={date}")
    pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: date})
    return date

def recalc_delivery(conn, deal_id):
    """Recompute a deal's delivery_date from its tracked delivery tasks, choosing the most
    recent non-cancelled one (latest task_date; completed tasks count). Returns that date,
    or None if no delivery task remains — the delivery-side mirror of recalc_measure, used
    when a task's template is corrected away from delivery."""
    row = conn.execute(
        "SELECT task_date FROM task_state "
        "WHERE deal_id=? AND task_type='delivery' AND status!='cancelled' "
        "AND archived=0 AND task_date IS NOT NULL "
        "ORDER BY task_date DESC LIMIT 1",
        (deal_id,)
    ).fetchone()
    date = row["task_date"] if row else None
    logger.info(f"recalc_delivery: deal={deal_id} delivery_date={date}")
    pd_update_deal(deal_id, {PD_FIELDS["delivery_date"]: date})
    return date

def get_extra_field(extra_fields, name):
    """Return the value of a named field from OBJECT_TEMPLATE_EXTRA_FIELDS."""
    for field in (extra_fields or []):
        if field.get("name") == name:
            return field.get("value")
    return None

def handle_install(conn, event_type, deal_id, task_id, object_date, extra_fields=None):
    """Apply an Arrivy install/repair-task event to the deal: update task state, recalc install dates, and move to Install Complete or back to Ready to Schedule as appropriate. Returns an action summary string."""
    date          = parse_arrivy_date(object_date)
    # The repair template carries the same "Installation Phase" dropdown as the install
    # template (Arrivy requires a value, defaulting to Partial), so repairs are read the
    # same way: a partial repair holds the deal open exactly like a partial install.
    # Arrivy sends OBJECT_TEMPLATE_EXTRA_FIELDS as [] on some events (completions
    # included) — that's no news rather than a cleared phase, so leave it None and let
    # upsert_task_state's COALESCE keep the phase we already have.
    install_phase = get_extra_field(extra_fields, "Installation Phase")
    logger.info(f"handle_install: event={event_type} task={task_id} date={date} phase={install_phase!r}")
    if event_type in ("TASK_CREATED", "TASK_UPDATED", "TASK_RESCHEDULED", "TASK_TEMPLATE_EXTRA_FIELDS_UPDATED"):
        upsert_task_state(conn, task_id, deal_id, "install", date, install_phase=install_phase)
        dates = recalc_install(conn, deal_id)
        # If this update cleared the last remaining install date for the deal,
        # roll the stage back to "Ready to Schedule" — same as cancel/delete.
        if not dates:
            pd_move_stage(deal_id, INSTALL_READY_TO_SCHEDULE_ID)
            return "Cleared install dates, moved to Ready to Schedule"
        return f"Recalculated install dates (phase: {install_phase or '—'})"
    elif event_type == "TASK_DELETED":
        delete_task_state(conn, task_id)
        dates = recalc_install(conn, deal_id)
        if not dates:
            pd_move_stage(deal_id, INSTALL_READY_TO_SCHEDULE_ID)
            return "Removed task, moved to Ready to Schedule"
        return "Removed task, recalculated install dates"
    elif event_type == "TASK_CANCELLED":
        upsert_task_state(conn, task_id, deal_id, "install", date, status="cancelled")
        dates = recalc_install(conn, deal_id)
        if not dates:
            pd_move_stage(deal_id, INSTALL_READY_TO_SCHEDULE_ID)
            return "Cancelled task, moved to Ready to Schedule"
        return "Cancelled task, recalculated install dates"
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "install", date, status="completed")
        # Only close the deal out (→ Install Complete) when this completion is truly
        # the last install. Another install still scheduled for a LATER date always
        # holds it back. Installs on the SAME day hold it back too — UNLESS this task
        # is the "final" phase, the one designated to close the job out. (Phase is
        # read from task_state, which preserves it even if the completion event omits
        # the extra field.) These checks read
        # task_state only, so they don't depend on recalc_install having run yet.
        later = conn.execute(
            "SELECT 1 FROM task_state WHERE deal_id=? AND task_type='install' "
            "AND status='active' AND archived=0 AND task_date > ? LIMIT 1",
            (deal_id, date),
        ).fetchone()
        same_day = conn.execute(
            "SELECT 1 FROM task_state WHERE deal_id=? AND task_type='install' "
            "AND status='active' AND archived=0 AND task_date = ? LIMIT 1",
            (deal_id, date),
        ).fetchone()
        phase_row = conn.execute(
            "SELECT install_phase FROM task_state WHERE task_id=? LIMIT 1", (task_id,)
        ).fetchone()
        is_final = bool(phase_row and (phase_row["install_phase"] or "").lower() == "final")
        close_out = not (later or (same_day and not is_final))
        # Push the stage change BEFORE recalculating the date fields. The Pipedrive
        # automation that fires on entering Install Complete must see the completed
        # install's dates — so move the stage first, then let recalc_install rewrite
        # install_start/part2 from the remaining (next) install tasks.
        if close_out:
            pd_move_stage(deal_id, INSTALL_COMPLETE_STAGE_ID)
        recalc_install(conn, deal_id)
        if close_out:
            return "Moved to Install Complete"
        return "Install task completed; a later/same-day install is still scheduled — staying put"

# ---------------------------------------------------------------------------
# IP RESTRICTION — only accessible from the Pi itself
# ---------------------------------------------------------------------------
@app.before_request
def restrict_dashboard_by_ip():
    """before_request guard: redirect any non-allowlisted client IP away from dashboard endpoints (webhooks stay public)."""
    if request.endpoint in DASHBOARD_ENDPOINTS:
        # Use X-Forwarded-For if behind a reverse proxy (e.g. nginx), otherwise remote_addr
        client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr
        if client_ip not in ALLOWED_DASHBOARD_IPS:
            logger.warning(f"Blocked {client_ip} from {request.endpoint}")
            return redirect("https://www.creativecarpetinc.com")

# ---------------------------------------------------------------------------
# PIN AUTH
# ---------------------------------------------------------------------------
def login_required(f):
    """Route decorator: redirect to the PIN page unless the session is authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        """Wrapper: enforce authentication before calling the wrapped view."""
        if not session.get("authenticated"):
            return redirect(url_for("pin_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    """Route decorator: require an authenticated session with the 'admin' role (else redirect to PIN page or landing)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        """Wrapper: enforce authentication and the admin role before calling the wrapped view."""
        if not session.get("authenticated"):
            return redirect(url_for("pin_page"))
        if session.get("role") != "admin":
            return redirect(url_for("landing"))
        return f(*args, **kwargs)
    return decorated

@app.route("/pin", methods=["GET"])
def pin_page():
    """Render the PIN entry screen."""
    return render_template("pin.html")

@app.route("/pin/verify", methods=["POST"])
def verify_pin():
    """Validate a submitted PIN against the users table; on success set the session, log the access, and return the user's role."""
    data    = request.get_json(force=True)
    entered = data.get("pin", "")
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE pin=?", (entered,)).fetchone()
    if user:
        session["authenticated"] = True
        session["username"]       = user["username"]
        session["role"]           = user["role"]
        ip = request.remote_addr
        with get_db() as conn:
            conn.execute(
                "INSERT INTO access_log (username, logged_in_at, ip_address) VALUES (?,?,?)",
                (user["username"], datetime.utcnow().isoformat(), ip)
            )
        logger.info(f"Login: {user['username']} ({user['role']}) from {ip}")
        return jsonify({"status": "ok", "role": user["role"]}), 200
    return jsonify({"status": "error", "message": "Incorrect PIN"}), 401

@app.route("/pin/change", methods=["POST"])
@login_required
def change_pin():
    """Change the logged-in user's PIN (must be 4 digits and not already in use by another user)."""
    data    = request.get_json(force=True)
    new_pin = data.get("pin", "")
    if not new_pin.isdigit() or len(new_pin) != 4:
        return jsonify({"status": "error", "message": "PIN must be 4 digits"}), 400
    username = session.get("username", "admin")
    with get_db() as conn:
        conflict = conn.execute(
            "SELECT id FROM users WHERE pin=? AND username!=?", (new_pin, username)
        ).fetchone()
        if conflict:
            return jsonify({"status": "error", "message": "PIN already in use"}), 400
        conn.execute("UPDATE users SET pin=? WHERE username=?", (new_pin, username))
    return jsonify({"status": "ok"}), 200

@app.route("/logout")
def logout():
    """Clear the session and return to the PIN page."""
    session.clear()
    return redirect(url_for("pin_page"))

# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def landing():
    """Render the home/landing page with the user's name and admin flag."""
    return render_template(
        "landing.html",
        username=session.get("username", ""),
        is_admin=session.get("role") == "admin",
    )

@app.route("/sync")
@login_required
def sync_dashboard():
    """Render the sync dashboard with recent events, per-type counts, and active-task/total-event tallies."""
    with get_db() as conn:
        recent_events = conn.execute(
            """SELECT * FROM events WHERE archived = 0
               ORDER BY received_at DESC LIMIT 20"""
        ).fetchall()
        event_counts = conn.execute(
            """SELECT event_type, COUNT(*) as count FROM events
               WHERE archived = 0 GROUP BY event_type"""
        ).fetchall()
        active_tasks = conn.execute(
            "SELECT COUNT(*) as count FROM task_state WHERE status = 'active' AND archived = 0"
        ).fetchone()
        total_events = conn.execute(
            "SELECT COUNT(*) as count FROM events WHERE archived = 0"
        ).fetchone()
    return render_template("dashboard.html",
                           recent_events=recent_events,
                           event_counts=event_counts,
                           active_tasks=active_tasks,
                           total_events=total_events,
                           username=session.get("username", ""),
                           is_admin=session.get("role") == "admin")

@app.route("/api/stats")
@login_required
def api_stats():
    """JSON endpoint: total events, active task count, the 5 most recent events, and DB-disk usage for the dashboard widgets."""
    with get_db() as conn:
        total  = conn.execute("SELECT COUNT(*) as c FROM events WHERE archived=0").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) as c FROM task_state WHERE status='active' AND archived=0").fetchone()["c"]
        recent = conn.execute(
            "SELECT event_type, task_type, deal_id, received_at FROM events WHERE archived=0 ORDER BY received_at DESC LIMIT 5"
        ).fetchall()
    try:
        du = shutil.disk_usage(os.path.dirname(DB_PATH) or "/")
        disk = {
            "total_bytes": du.total,
            "used_bytes":  du.used,
            "free_bytes":  du.free,
            "percent_used": round(du.used / du.total * 100, 1) if du.total else 0,
        }
    except Exception as e:
        logger.warning(f"disk_usage failed: {e}")
        disk = None
    return jsonify({
        "total_events": total,
        "active_tasks": active,
        "recent": [dict(r) for r in recent],
        "disk": disk,
    })

@app.route("/api/stream")
@login_required
def api_stream():
    """Server-Sent Events stream that pushes a 'refresh' to the dashboard whenever sse_notify() fires; sends a keepalive ping every 25s."""
    def generate():
        """SSE generator: register a per-client queue and yield messages (or pings) until the client disconnects."""
        q = queue.Queue(maxsize=10)
        with _sse_clients_lock:
            _sse_clients.add(q)
        try:
            yield "data: connected\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield "data: ping\n\n"  # keepalive
        finally:
            with _sse_clients_lock:
                _sse_clients.discard(q)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/logs")
@login_required
def api_logs():
    """JSON endpoint: events filtered by optional from/to date range and limit, flattened into display-friendly rows for the logs page."""
    date_from = request.args.get("from", "")   # YYYY-MM-DD
    date_to   = request.args.get("to", "")     # YYYY-MM-DD
    clauses = []
    params  = []
    if date_from:
        clauses.append("received_at >= ?")
        params.append(date_from + "T00:00:00")
    if date_to:
        clauses.append("received_at < ?")
        params.append(date_to + "T23:59:59.999999")
    limit = request.args.get("limit", "")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    sql = f"SELECT id, received_at, deal_id, task_id, event_type, task_type, raw_json, action FROM events {where} ORDER BY received_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    logs = []
    for row in rows:
        raw = json.loads(row["raw_json"])
        received = row["received_at"] or ""
        logs.append({
            "id":         row["id"],
            "time":       received[11:19] if received else "—",
            "date":       received[:10] if received else "—",
            "deal_id":    row["deal_id"],
            "event_type": (row["event_type"] or "—").replace("TASK_", ""),
            "task_type":  row["task_type"] or "unknown",
            "task_date":  (raw.get("OBJECT_DATE") or "")[:10] or "—",
            "title":      raw.get("TITLE") or "—",
            "action":     row["action"] or "—",
        })
    return jsonify({"logs": logs})

# @app.route("/api/clear-db", methods=["POST"])
# @login_required
# def api_clear_db():
#     with get_db() as conn:
#         conn.execute("DELETE FROM events")
#         conn.execute("DELETE FROM task_state")
#     sse_notify()
#     return jsonify({"status": "ok"})

@app.route("/logs")
@login_required
def logs():
    """Render the event-logs page (data loaded client-side from /api/logs)."""
    return render_template("logs.html")

@app.route("/users")
@admin_required
def users():
    """Render the admin-only user management page."""
    return render_template("users.html", username=session.get("username", ""))

@app.route("/api/users", methods=["GET", "POST"])
@admin_required
def api_users():
    """Admin JSON endpoint: GET lists users; POST creates a user (requires username + unique 4-digit PIN, role admin/user)."""
    if request.method == "GET":
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, username, role, created_at FROM users ORDER BY created_at"
            ).fetchall()
        return jsonify({"users": [dict(r) for r in rows]})
    data     = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    pin      = (data.get("pin") or "").strip()
    role     = data.get("role", "user")
    if not username or not pin.isdigit() or len(pin) != 4:
        return jsonify({"error": "Username and 4-digit PIN required"}), 400
    if role not in ("admin", "user"):
        role = "user"
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, pin, role, created_at) VALUES (?,?,?,?)",
                (username, pin, role, datetime.utcnow().isoformat())
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    logger.info(f"User created: {username} ({role})")
    return jsonify({"status": "ok"}), 201

@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_user_delete(user_id):
    """Admin JSON endpoint: delete a user. Refuses to delete admin accounts or the caller's own account."""
    with get_db() as conn:
        user = conn.execute("SELECT username, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user["role"] == "admin":
            return jsonify({"error": "Cannot delete admin users"}), 400
        if user["username"] == session.get("username"):
            return jsonify({"error": "Cannot delete your own account"}), 400
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    logger.info(f"User deleted: {user['username']}")
    return jsonify({"status": "ok"}), 200

@app.route("/api/access-log")
@admin_required
def api_access_log():
    """Admin JSON endpoint: most recent login records (capped at 1000)."""
    limit = min(int(request.args.get("limit", 100)), 1000)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT username, logged_in_at, ip_address FROM access_log ORDER BY logged_in_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return jsonify({"entries": [dict(r) for r in rows]})

@app.route("/api/sync-all", methods=["POST"])
@login_required
def api_sync_all():
    """Manual full resync: for every non-archived deal above MIN_DEAL_ID, push install/measure/delivery dates from local task_state back into Pipedrive. Returns synced count + per-deal errors."""
    synced = []
    errors = []
    with get_db() as conn:
        deal_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT deal_id FROM task_state WHERE archived=0 AND deal_id > ?",
            (MIN_DEAL_ID,)
        ).fetchall()]
        for deal_id in deal_ids:
            try:
                recalc_install(conn, deal_id)
                active = conn.execute(
                    "SELECT task_type, task_date FROM task_state WHERE deal_id=? AND status='active' AND archived=0",
                    (deal_id,)
                ).fetchall()
                for row in active:
                    if row["task_type"] == "measure":
                        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: row["task_date"]})
                    elif row["task_type"] == "delivery":
                        pd_update_deal(deal_id, {PD_FIELDS["delivery_date"]: row["task_date"]})
                synced.append(deal_id)
            except Exception as e:
                logger.exception(f"Sync failed for deal {deal_id}: {e}")
                errors.append({"deal_id": deal_id, "error": str(e)})
    logger.info(f"Manual sync complete: {len(synced)} deals synced, {len(errors)} errors")
    return jsonify({"synced": len(synced), "errors": errors})

@app.route("/settings")
@login_required
def settings_page():
    """Render the device/kiosk settings page (auto-lock, screen sleep)."""
    return render_template("settings.html")

@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    """JSON endpoint: GET returns the kiosk settings (defaulting to '0'); POST persists any of the known SETTING_KEYS."""
    SETTING_KEYS = {"auto_lock_minutes", "screen_sleep_minutes"}
    if request.method == "GET":
        return jsonify({k: get_setting(k) or "0" for k in SETTING_KEYS})
    data = request.get_json(force=True)
    for key, val in data.items():
        if key in SETTING_KEYS:
            set_setting(key, str(val))
    return jsonify({"status": "ok"})

# ---------------------------------------------------------------------------
# WEBHOOK ENDPOINTS
# ---------------------------------------------------------------------------
@app.route("/arrivy-webhook", methods=["POST"])
def arrivy_webhook():
    """Arrivy webhook entry point: normalize the event, map the template to a task type, dispatch to the matching handle_* function (only for deals above MIN_DEAL_ID), and log the event. Always returns 200 unless it errors."""
    try:
        payload = request.get_json(force=True)
        if not payload:
            return jsonify({"error": "empty payload"}), 400

        raw_event_type = payload.get("EVENT_TYPE")
        sub_type    = payload.get("EVENT_SUB_TYPE", "")
        template_id = payload.get("OBJECT_TEMPLATE_ID")
        object_date = payload.get("OBJECT_DATE")
        external_id = payload.get("OBJECT_EXTERNAL_ID")
        task_id     = payload.get("OBJECT_ID")
        extra_fields = payload.get("OBJECT_TEMPLATE_EXTRA_FIELDS", [])

        # Map Arrivy's TASK_STATUS + subtype to internal event types
        STATUS_SUBTYPE_MAP = {
            "COMPLETE":  "TASK_COMPLETED",
            "CANCEL":    "TASK_CANCELLED",
            "CANCELLED": "TASK_CANCELLED",
        }
        if raw_event_type == "TASK_STATUS":
            event_type = STATUS_SUBTYPE_MAP.get(sub_type.upper())
        else:
            event_type = raw_event_type

        logger.info(f"Arrivy raw payload: {json.dumps(payload)}")
        logger.info(f"Arrivy: {event_type} (raw={raw_event_type}/{sub_type}) | template={template_id} | deal={external_id} | task={task_id}")

        deal_id   = int(external_id) if external_id else None
        task_type = TEMPLATE_MAP.get(template_id)

        with get_db() as conn:
            # Delete events arrive with no external_id or template_id — look up from DB
            if event_type == "TASK_DELETED" and (not deal_id or not task_type):
                row = get_task_state(conn, task_id)
                if row:
                    deal_id   = deal_id or row["deal_id"]
                    task_type = task_type or row["task_type"]

            if not deal_id:
                return jsonify({"status": "ignored", "reason": "no external id"}), 200

            action = None
            if deal_id > MIN_DEAL_ID:
                if event_type in ("TASK_CREATED", "TASK_UPDATED", "TASK_CANCELLED", "TASK_COMPLETED", "TASK_DELETED", "TASK_RESCHEDULED", "TASK_TEMPLATE_EXTRA_FIELDS_UPDATED") and task_type:
                    # Capture what type this task was BEFORE the handler re-files it, so
                    # we can detect a template correction (e.g. measure → install) below.
                    prev = get_task_state(conn, task_id)
                    if task_type == "measure":
                        action = handle_measure(conn, event_type, deal_id, task_id, object_date)
                    elif task_type == "delivery":
                        action = handle_delivery(conn, event_type, deal_id, task_id, object_date)
                    elif task_type == "install":
                        action = handle_install(conn, event_type, deal_id, task_id, object_date, extra_fields)
                    elif task_type == "inspection":
                        action = handle_inspection(conn, event_type, deal_id, task_id, object_date)
                    # Template was corrected in Arrivy: the task's type changed, so the
                    # field its OLD type owned is now stale. Recompute it from the deal's
                    # other tasks of that type (measure & inspection share measure_date).
                    if prev and prev["task_type"] != task_type:
                        old = prev["task_type"]
                        if old in ("measure", "inspection"):
                            recalc_measure(conn, deal_id)
                        elif old == "delivery":
                            recalc_delivery(conn, deal_id)
                        elif old == "install":
                            recalc_install(conn, deal_id)
                else:
                    action = "Logged (no action needed)"
            else:
                action = "Logged only (below threshold)"

            store_event(conn, deal_id, task_id, event_type, task_type, payload, action)

        sse_notify()
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.exception(f"Arrivy webhook error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/pipedrive-webhook/", methods=["POST"])
@app.route("/pipedrive-webhook", methods=["POST"])
def pipedrive_webhook():
    """Pipedrive deal-update webhook: archive the deal when won/lost, or recalc install dates when it lands in the Install Scheduled stage. Ignores historical deals (id <= MIN_DEAL_ID)."""
    try:
        payload = request.get_json(force=True)
        if not payload:
            return jsonify({"error": "empty payload"}), 400
        event    = payload.get("event")
        current  = payload.get("current", {})
        status   = current.get("status")
        deal_id  = current.get("id")
        stage_id = current.get("stage_id")
        logger.info(f"Pipedrive webhook: event={event!r} deal_id={deal_id!r} stage_id={stage_id!r} ({type(stage_id).__name__}) status={status!r}")
        if event == "updated.deal" and deal_id:
            if int(deal_id) <= MIN_DEAL_ID:
                logger.info(f"Pipedrive webhook ignored for historical deal {deal_id} (<= MIN_DEAL_ID {MIN_DEAL_ID})")
                return jsonify({"status": "ignored", "reason": "historical deal"}), 200
            if status in ("won", "lost"):
                with get_db() as conn:
                    archive_deal(conn, deal_id)
            elif int(stage_id) == INSTALL_SCHEDULED_STAGE_ID if stage_id is not None else False:
                with get_db() as conn:
                    recalc_install(conn, deal_id)
                logger.info(f"Stage 10 recalc ran for deal {deal_id}")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.exception(f"Pipedrive webhook error: {e}")
        return jsonify({"error": str(e)}), 500


# --- Pipedrive → Rollmaster customer sync -----------------------------------
# Field aliases, so the Pipedrive automation's payload doesn't have to match one
# exact spelling. Pipedrive's own address subfield names are included, since an
# automation that sends an org/person address emits those verbatim.
_PD_ALIASES = {
    "name":        ("name", "person_name", "customer_name", "full_name", "title"),
    "first_name":  ("first_name", "firstname", "given_name"),
    "last_name":   ("last_name", "lastname", "surname", "family_name"),
    "org":         ("org_name", "organization", "organisation", "company", "company_name"),
    "email":       ("email", "person_email", "primary_email", "email_address"),
    "phone":       ("phone", "person_phone", "primary_phone", "phone_number"),
    "phone2":      ("phone2", "phone_2", "mobile", "secondary_phone", "cell"),
    "fax":         ("fax", "fax_number"),
    "address1":    ("address1", "address", "street", "street_address", "address_street",
                    "postal_address", "address_route"),
    "street_no":   ("street_number", "street_no", "house_number", "address_street_number",
                    "postal_address_street_number"),
    "address2":    ("address2", "address_2", "unit", "apt", "suite", "address_subpremise"),
    "city":        ("city", "address_locality", "postal_address_locality"),
    "state":       ("state", "address_admin_area_level_1", "postal_address_admin_area_level_1"),
    "zip":         ("zip", "zipcode", "zip_code", "postal_code", "address_postal_code",
                    "postal_address_postal_code"),
    "contact":     ("contact", "contact_name"),
    "taxid":       ("taxid", "tax_id", "tax_number"),
    "salesperson": ("slsid", "c_slsid", "salesperson", "sales_rep", "owner", "owner_name"),
    "deal_id":     ("deal_id", "dealid", "deal"),
    "person_id":   ("person_id", "personid", "person", "id"),
    "cid":         ("cid", "c_cid", "customer_id", "customerid", "rm_customer_id", "rollmaster_id"),
}


def _pd_value(payload, key):
    """
    Return the first non-empty value for a logical field from the webhook payload.

    Looks through that field's aliases case-insensitively, and follows one level
    of nesting (payload["person"]["email"]) since Pipedrive automations commonly
    send the person and org as sub-objects.
    """
    flat = {}
    for k, v in (payload or {}).items():
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                flat.setdefault(str(sub_k).strip().lower(), sub_v)
        flat.setdefault(str(k).strip().lower(), v)
    for alias in _PD_ALIASES.get(key, (key,)):
        val = flat.get(alias)
        if isinstance(val, (str, int, float)) and str(val).strip():
            return str(val).strip()
    return ""


_US_STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN",
    "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC",
}


def _format_phone(raw):
    """Rollmaster keys phones as 999-999-9999; reformat a 10-digit (or 1+10) number, else pass through."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return (raw or "").strip()


def _street_only(address, city, state, zipcd):
    """
    Reduce a Pipedrive formatted address to its street line.

    Pipedrive's address field arrives as "123 Maple St #4, Frankfort, IL 60423, USA";
    Rollmaster wants only "123 Maple St #4" in C_ADDR1 since city/state/zip have
    their own fields. Cut at the city when we know it, otherwise at the first
    comma. An address with no comma is left alone.
    """
    addr = (address or "").strip()
    if "," not in addr:
        return addr
    if city:
        i = addr.upper().find("," + " " + city.upper())
        if i < 0:
            i = addr.upper().find("," + city.upper())
        if i > 0:
            return addr[:i].strip()
    return addr.split(",", 1)[0].strip()


def _normalize_customer_fields(fields):
    """
    Bring mapped values in line with how Rollmaster stores them: street-only
    ADDR1, 2-letter state, 999-999-9999 phones, upper-case text (email as typed),
    and no unit number repeated in ADDR2 when ADDR1 already carries it.
    """
    f = dict(fields)
    f["C_ADDR1"] = _street_only(f.get("C_ADDR1"), f.get("C_CITY"), f.get("C_STATE"), f.get("C_ZIPCD"))
    unit = (f.get("C_ADDR2") or "").strip()
    if unit and re.search(r"(^|[\s#])" + re.escape(unit) + r"$", f["C_ADDR1"]):
        f["C_ADDR2"] = ""
    st = (f.get("C_STATE") or "").strip().upper()
    f["C_STATE"] = _US_STATES.get(st, st)
    for k in ("C_PHONE", "C_PHONE2", "C_FAX"):
        f[k] = _format_phone(f.get(k))
    for k in ("C_NAME", "C_NAME_LONG", "C_CONTACT", "C_ADDR1", "C_ADDR2", "C_CITY"):
        f[k] = (f.get(k) or "").strip().upper()
    return f


def map_pipedrive_customer(payload):
    """
    Turn a Pipedrive automation payload into (rollmaster_fields, name_for_cid).

    Returns the /customer form values; the caller mints C_CID from the name. This
    handles PEOPLE only — the account is always keyed under the person's name, and
    an organization on the payload is ignored rather than guessed at, since a
    business account uses a different id convention (8-character company names
    like CITYWIDE) that we haven't specified yet. Fixed account-setup values come
    from RM_CUSTOMER_DEFAULTS and can be overridden by a field in the payload.
    """
    first = _pd_value(payload, "first_name")
    last  = _pd_value(payload, "last_name")
    name  = " ".join(p for p in (first, last) if p) or _pd_value(payload, "name")

    contact = _pd_value(payload, "contact")
    owner   = _pd_value(payload, "salesperson")
    # Mapped owner > fixed default > a bare code passed in the payload (≤6 chars,
    # so a full owner name never lands in C_SLSID by accident).
    slsid   = (RM_SALESPERSON_MAP.get(owner)
               or RM_CUSTOMER_DEFAULTS["C_SLSID"]
               or (owner if len(owner) <= 6 else ""))

    # Pipedrive splits an address into "street number" and "street/road name"
    # subfields; when the automation sends the number separately, put it back
    # in front of the street unless it's already there.
    addr1  = _pd_value(payload, "address1")
    street_no = _pd_value(payload, "street_no")
    if street_no and not addr1.startswith(street_no):
        addr1 = f"{street_no} {addr1}".strip()

    fields = dict(RM_CUSTOMER_DEFAULTS)
    fields.update({
        "C_NAME":    name,
        "C_ADDR1":   addr1,
        "C_ADDR2":   _pd_value(payload, "address2"),
        "C_CITY":    _pd_value(payload, "city"),
        "C_STATE":   _pd_value(payload, "state"),
        "C_ZIPCD":   _pd_value(payload, "zip"),
        "C_PHONE":   _pd_value(payload, "phone"),
        "C_PHONE2":  _pd_value(payload, "phone2"),
        "C_FAX":     _pd_value(payload, "fax"),
        "C_EMAIL":   _pd_value(payload, "email"),
        "C_CONTACT": contact,
        "C_TAXID":   _pd_value(payload, "taxid"),
        "C_SLSID":   slsid,
    })
    # Let the payload set any Rollmaster field outright (C_WHSE, C_CUSTTYPE, …),
    # which also covers fields we haven't given an alias.
    for k, v in (payload or {}).items():
        key = str(k).strip().upper()
        if key in rollmaster.CUSTOMER_FIELDS and key != "C_CID" and str(v or "").strip():
            fields[key] = str(v).strip()

    return _normalize_customer_fields(fields), name


def _cid_write_back_target(person_id):
    """Where the new customer id will be written, or None when nothing is configured for it."""
    if not (person_id and RM_PD_CID_FIELD and PIPEDRIVE_API_TOKEN):
        return None
    return {"person_id": person_id, "field": RM_PD_CID_FIELD}


def _write_cid_to_person(person_id, cid, deal_id=None):
    """
    Store a freshly created Rollmaster customer id on the Pipedrive person
    ("Customer ID" field).

    The customer already exists in the ERP by the time this runs, so a failure
    here is logged and reported but never fails the sync call. Returns a short
    status string for the response body.
    """
    if not _cid_write_back_target(person_id):
        return "skipped (no person id or write-back not configured)"
    try:
        pd_update_person(person_id, {RM_PD_CID_FIELD: cid})
        return f"updated person {person_id}"
    except Exception as e:
        msg = _scrub_token(e)
        logger.error(f"rm-customer-sync: created {cid} but failed to write it to person {person_id}: {msg}")
        with get_db() as conn:
            store_event(conn, deal_id, None, "RM_CUSTOMER_WRITEBACK_FAILED", "customer",
                        {"cid": cid, "person_id": person_id, "field": RM_PD_CID_FIELD},
                        f"Created {cid} but Pipedrive update failed: {msg}")
        sse_notify()
        return f"failed: {msg}"


def _sync_key_ok():
    """True when the request carries the shared secret (X-Sync-Key header or ?key=); always False if none is configured."""
    if not RM_SYNC_SECRET:
        return False
    supplied = request.headers.get("X-Sync-Key", "") or request.args.get("key", "")
    # Pipedrive's automation builder appends "/" to the whole URL, which lands on
    # the end of the query-string key ("?key=abc/"), so strip it before comparing.
    return hmac.compare_digest(supplied.strip().rstrip("/"), RM_SYNC_SECRET)


@app.route("/rm-customer-sync/", methods=["POST"])
@app.route("/rm-customer-sync", methods=["POST"])
def rm_customer_sync():
    """
    Pipedrive → Rollmaster customer create.

    Takes the customer fields a Pipedrive automation posts (JSON or form-encoded),
    maps them to the /customer form, mints a C_CID from the name (SMIJOH, with a
    numeric suffix on collision) and creates the record. Returns the new customer
    id so it can be written back to the deal.

    Guarded twice over, because this is the one endpoint that creates records in
    the ERP: a shared secret is required, and RM_CUSTOMER_SYNC_ENABLED must be on.
    With the switch off it does everything except the write and returns the exact
    payload it would have sent — the safe way to test the automation.
    """
    if not _sync_key_ok():
        logger.warning(f"Rejected rm-customer-sync from {request.remote_addr} (bad or missing key)")
        return jsonify({"error": "unauthorized"}), 401
    try:
        payload = request.get_json(silent=True) or request.form.to_dict() or {}
        if not payload:
            return jsonify({"error": "empty payload"}), 400

        fields, name = map_pipedrive_customer(payload)
        if not fields["C_NAME"]:
            return jsonify({"error": "no customer name in payload"}), 400

        deal_id = person_id = None
        raw_deal = _pd_value(payload, "deal_id")
        if str(raw_deal).isdigit():
            deal_id = int(raw_deal)
        raw_person = _pd_value(payload, "person_id")
        if str(raw_person).isdigit():
            person_id = int(raw_person)

        # A person that already carries a Rollmaster customer id is an UPDATE of
        # that record, never a second create. The id itself is never changed.
        # Trust the payload's customer_id when sent; otherwise ask Pipedrive for
        # the person's Customer ID field, so an automation that forgets to send
        # it (or fires on every edit) can't create duplicates.
        existing_cid = _pd_value(payload, "cid").strip().upper()
        matched_how = None
        if not existing_cid and _cid_write_back_target(person_id):
            try:
                existing_cid = str(pd_get_person(person_id).get(RM_PD_CID_FIELD) or "").strip().upper()
                if existing_cid:
                    logger.info(f"rm-customer-sync: person {person_id} already has Customer ID {existing_cid} in Pipedrive")
            except Exception as e:
                # Can't tell — refuse to guess. A create here could be a duplicate.
                msg = _scrub_token(e)
                logger.error(f"rm-customer-sync: could not read person {person_id} from Pipedrive: {msg}")
                with get_db() as conn:
                    store_event(conn, deal_id, None, "RM_CUSTOMER_FAILED", "customer", payload,
                                f"Refused: could not check person {person_id} for an existing Customer ID ({msg})")
                sse_notify()
                return jsonify({"error": f"could not read person {person_id} from Pipedrive: {msg}"}), 502
        # Legacy customers: a person with no Customer ID may still be an existing
        # Rollmaster account (everyone from before the sync). Match on phone or
        # email before creating; several equally good matches is a job for a
        # human, not a coin toss.
        if not existing_cid:
            try:
                found = rollmaster.find_existing_customer(
                    name, [fields.get("C_PHONE"), fields.get("C_PHONE2")], fields.get("C_EMAIL"))
            except rollmaster.AmbiguousMatch as e:
                action = f"Refused: {fields['C_NAME']} {e}"
                logger.warning(f"rm-customer-sync {action}")
                with get_db() as conn:
                    store_event(conn, deal_id, None, "RM_CUSTOMER_AMBIGUOUS", "customer", payload, action)
                sse_notify()
                return jsonify({"error": str(e), "candidates": [c[0] for c in e.candidates]}), 409
            if found:
                existing_cid, matched_how = found
                logger.info(f"rm-customer-sync: {fields['C_NAME']} matched existing customer {existing_cid} by {matched_how}")

        if existing_cid:
            params = rollmaster.update_params(existing_cid, fields)
            if not RM_CUSTOMER_SYNC_ENABLED:
                action = f"DRY RUN — would update {existing_cid} ({fields['C_NAME']})"
                logger.info(f"rm-customer-sync {action}")
                with get_db() as conn:
                    store_event(conn, deal_id, None, "RM_CUSTOMER_DRYRUN", "customer", payload, action)
                sse_notify()
                return jsonify({"status": "dry-run", "action": "update", "cid": existing_cid,
                                "matched_by": matched_how, "would_send": params,
                                "would_write_back": _cid_write_back_target(person_id) if matched_how else None}), 200
            resp = rollmaster.update_customer(existing_cid, fields)
            action = f"Updated Rollmaster customer {existing_cid} ({fields['C_NAME']})"
            if matched_how:
                action += f" — matched legacy customer by {matched_how}"
            logger.info(f"rm-customer-sync {action}")
            with get_db() as conn:
                store_event(conn, deal_id, None, "RM_CUSTOMER_UPDATED", "customer", payload, action)
            sse_notify()
            out = {"status": "ok", "action": "update", "cid": existing_cid, "response": resp}
            if matched_how:
                # A legacy match links the person for good by stamping the id on it.
                out["matched_by"] = matched_how
                out["pipedrive"]  = _write_cid_to_person(person_id, existing_cid, deal_id)
            return jsonify(out), 200

        if not RM_CUSTOMER_SYNC_ENABLED:
            first, last = rollmaster.split_name(name)
            try:
                cid = rollmaster.next_free_cid(
                    rollmaster.cid_base(first, last), rollmaster.load_known_cids()
                )
            except Exception as e:
                logger.exception("dry-run cid preview failed")
                cid = f"(unavailable: {e})"
            action = f"DRY RUN — would create {cid} ({fields['C_NAME']})"
            logger.info(f"rm-customer-sync {action}")
            with get_db() as conn:
                store_event(conn, deal_id, None, "RM_CUSTOMER_DRYRUN", "customer", payload, action)
            sse_notify()
            return jsonify({"status": "dry-run", "cid": cid,
                            "would_send": rollmaster.build_customer_form({**fields, "C_CID": cid}),
                            "would_write_back": _cid_write_back_target(person_id)}), 200

        cid, resp = rollmaster.create_customer(fields, name=name)
        action = f"Created Rollmaster customer {cid} ({fields['C_NAME']})"
        logger.info(f"rm-customer-sync {action}")
        with get_db() as conn:
            store_event(conn, deal_id, None, "RM_CUSTOMER_CREATED", "customer", payload, action)
        sse_notify()
        return jsonify({"status": "ok", "action": "create", "cid": cid, "response": resp,
                        "pipedrive": _write_cid_to_person(person_id, cid, deal_id)}), 200

    except rollmaster.RollmasterError as e:
        logger.exception(f"rm-customer-sync rejected by Rollmaster: {e}")
        with get_db() as conn:
            store_event(conn, None, None, "RM_CUSTOMER_FAILED", "customer",
                        request.get_json(silent=True) or {}, f"Rollmaster rejected: {e}")
        sse_notify()
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        logger.exception(f"rm-customer-sync error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/deploy", methods=["POST"])
def deploy():
    """GitHub push webhook: verify the HMAC-SHA256 signature, then git-pull and restart the service in a background thread."""
    try:
        secret    = GITHUB_WEBHOOK_SECRET.encode()
        signature = request.headers.get("X-Hub-Signature-256", "")
        body      = request.get_data()
        expected  = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return jsonify({"error": "unauthorized"}), 401

        def _restart():
            """Pull the latest code and restart the systemd service (runs off-thread so the webhook can return immediately)."""
            subprocess.run(["git", "-C", REPO_PATH, "pull"], check=True)
            logger.info("Auto-deploy: pulled latest code, restarting service")
            subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME], check=True)

        threading.Thread(target=_restart, daemon=True).start()
        return jsonify({"status": "deploying"}), 200
    except Exception as e:
        logger.exception(f"Deploy failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    """Liveness probe — always returns {"status": "ok"}."""
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
init_db()
app.register_blueprint(reports_bp)
if RM_SYNC_SECRET:
    # Pull the customer-id list off-thread so the first sync webhook doesn't have
    # to wait on a multi-minute /customers walk.
    rollmaster.warm_cid_cache()
else:
    logging.warning("RM_SYNC_SECRET not set — /rm-customer-sync will refuse every request")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
