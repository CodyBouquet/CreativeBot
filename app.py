from flask import Flask, request, jsonify, render_template, session, redirect, url_for, Response
from dotenv import load_dotenv
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
TEMPLATE_MAP = {
    5395407346073600: "install",
    5627485400596480: "measure",
    5278551184506880: "delivery",
    5546469558321152: "inspection",
    4649593254445056: "install",      # repair — same process as install
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
MEASURE_ROLLBACK_STAGE_ID      = 70   # "in store contact" — when measure is cancelled/deleted
INSPECTION_SCHEDULED_STAGE_ID  = 36
INSPECTION_COMPLETE_STAGE_ID   = 37
INSPECTION_ROLLBACK_STAGE_ID   = 50   # "Customer Contacted/Attempted" — when inspection is cancelled/deleted
DELIVERY_STATUS_COMPLETE_ID    = 114  # option ID for "Complete" in the delivery_status dropdown

INSTALL_PHASE_OPTIONS = {
    "final":   37,
    "partial": 65,
}

ALLOWED_DASHBOARD_IPS = {"127.0.0.1", "::1", "10.54.10.135"}
DASHBOARD_ENDPOINTS  = {"landing", "sync_dashboard", "logs", "users", "pin_page", "verify_pin", "change_pin", "logout",
                        "api_stats", "api_stream", "api_logs", "settings_page",
                        "api_settings", "api_sync_all", "api_users", "api_user_delete", "api_access_log",
                        "reports.reports_home"}

# SSE client queues
_sse_clients      = set()
_sse_clients_lock = threading.Lock()

def sse_notify():
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
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

def set_setting(key, value):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

def store_event(conn, deal_id, task_id, event_type, task_type, raw_payload, action=None):
    cur = conn.execute(
        """INSERT INTO events (received_at, deal_id, task_id, event_type, task_type, raw_json, action)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (datetime.utcnow().isoformat(), deal_id, task_id, event_type, task_type,
         json.dumps(raw_payload), action)
    )
    return cur.lastrowid

def update_event_action(conn, event_id, action):
    conn.execute("UPDATE events SET action=? WHERE id=?", (action, event_id))

def get_task_state(conn, task_id):
    return conn.execute("SELECT * FROM task_state WHERE task_id = ?", (task_id,)).fetchone()

def upsert_task_state(conn, task_id, deal_id, task_type, task_date, status="active", install_phase=None):
    conn.execute(
        """INSERT INTO task_state (task_id, deal_id, task_type, task_date, install_phase, status, last_updated)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(task_id) DO UPDATE SET
               task_date     = excluded.task_date,
               install_phase = COALESCE(excluded.install_phase, task_state.install_phase),
               status        = excluded.status,
               last_updated  = excluded.last_updated""",
        (task_id, deal_id, task_type, task_date, install_phase, status, datetime.utcnow().isoformat())
    )

def archive_deal(conn, deal_id):
    conn.execute("UPDATE events     SET archived = 1 WHERE deal_id = ?", (deal_id,))
    conn.execute("UPDATE task_state SET archived = 1 WHERE deal_id = ?", (deal_id,))
    logger.info(f"Archived deal {deal_id}")

# ---------------------------------------------------------------------------
# PIPEDRIVE
# ---------------------------------------------------------------------------
PD_BASE = "https://api.pipedrive.com/v1"

def pd_get_deal(deal_id):
    r = requests.get(f"{PD_BASE}/deals/{deal_id}", params={"api_token": PIPEDRIVE_API_TOKEN})
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise Exception(f"Pipedrive get deal failed: {data}")
    return data["data"]

def pd_update_deal(deal_id, fields):
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

def pd_move_stage(deal_id, stage_id):
    pd_update_deal(deal_id, {"stage_id": stage_id})

# ---------------------------------------------------------------------------
# DATE HELPERS
# ---------------------------------------------------------------------------
def parse_arrivy_date(date_str):
    if not date_str:
        return None
    try:
        # Strip timezone offset so fromisoformat works on Python < 3.11
        clean = date_str[:19]
        return datetime.fromisoformat(clean).strftime("%Y-%m-%d")
    except Exception:
        return None

def dates_match(a, b):
    da = parse_arrivy_date(a) if a else None
    db = parse_arrivy_date(b) if b else None
    return bool(da and db and da == db)

# ---------------------------------------------------------------------------
# TASK HANDLERS
# ---------------------------------------------------------------------------
def handle_measure(conn, event_type, deal_id, task_id, object_date):
    date = parse_arrivy_date(object_date)
    if event_type in ("TASK_CREATED", "TASK_UPDATED", "TASK_RESCHEDULED"):
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: date})
        upsert_task_state(conn, task_id, deal_id, "measure", date)
        return f"Set measure date → {date}"
    elif event_type == "TASK_DELETED":
        delete_task_state(conn, task_id)
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: None})
        pd_move_stage(deal_id, MEASURE_ROLLBACK_STAGE_ID)
        return "Cleared measure date, rolled back stage"
    elif event_type == "TASK_CANCELLED":
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: None})
        upsert_task_state(conn, task_id, deal_id, "measure", date, status="cancelled")
        pd_move_stage(deal_id, MEASURE_ROLLBACK_STAGE_ID)
        return "Cleared measure date, rolled back stage"
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "measure", date, status="completed")
        pd_move_stage(deal_id, MEASURE_COMPLETE_STAGE_ID)
        return "Moved to Measure Complete"

def handle_inspection(conn, event_type, deal_id, task_id, object_date):
    date = parse_arrivy_date(object_date)
    if event_type in ("TASK_CREATED", "TASK_UPDATED", "TASK_RESCHEDULED"):
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: date})
        upsert_task_state(conn, task_id, deal_id, "inspection", date)
        pd_move_stage(deal_id, INSPECTION_SCHEDULED_STAGE_ID)
        return f"Set inspection date → {date}, moved to Scheduled"
    elif event_type == "TASK_DELETED":
        delete_task_state(conn, task_id)
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: None})
        pd_move_stage(deal_id, INSPECTION_ROLLBACK_STAGE_ID)
        return "Cleared inspection date, rolled back stage"
    elif event_type == "TASK_CANCELLED":
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: None})
        upsert_task_state(conn, task_id, deal_id, "inspection", date, status="cancelled")
        pd_move_stage(deal_id, INSPECTION_ROLLBACK_STAGE_ID)
        return "Cleared inspection date, rolled back stage"
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "inspection", date, status="completed")
        pd_move_stage(deal_id, INSPECTION_COMPLETE_STAGE_ID)
        return "Moved to Inspection Complete"

def delete_task_state(conn, task_id):
    conn.execute("DELETE FROM task_state WHERE task_id=?", (task_id,))

def handle_delivery(conn, event_type, deal_id, task_id, object_date):
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
    pd_update_deal(deal_id, {
        PD_FIELDS["install_start"]: dates[0]  if len(dates) > 0 else None,
        PD_FIELDS["install_part2"]: dates[1]  if len(dates) > 1 else None,
        PD_FIELDS["install_phase"]: phase_id,
    })
    return dates

def get_extra_field(extra_fields, name):
    """Return the value of a named field from OBJECT_TEMPLATE_EXTRA_FIELDS."""
    for field in (extra_fields or []):
        if field.get("name") == name:
            return field.get("value")
    return None

def handle_install(conn, event_type, deal_id, task_id, object_date, extra_fields=None):
    date          = parse_arrivy_date(object_date)
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
        current_dates = recalc_install(conn, deal_id)
        is_part1 = bool(current_dates and current_dates[0] == date)
        upsert_task_state(conn, task_id, deal_id, "install", date, status="completed")
        if is_part1:
            pd_move_stage(deal_id, INSTALL_COMPLETE_STAGE_ID)
            return "Moved to Install Complete"
        return "Marked install task completed"

# ---------------------------------------------------------------------------
# IP RESTRICTION — only accessible from the Pi itself
# ---------------------------------------------------------------------------
@app.before_request
def restrict_dashboard_by_ip():
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
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("pin_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("pin_page"))
        if session.get("role") != "admin":
            return redirect(url_for("landing"))
        return f(*args, **kwargs)
    return decorated

@app.route("/pin", methods=["GET"])
def pin_page():
    return render_template("pin.html")

@app.route("/pin/verify", methods=["POST"])
def verify_pin():
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
    session.clear()
    return redirect(url_for("pin_page"))

# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def landing():
    return render_template("landing.html", username=session.get("username", ""))

@app.route("/sync")
@login_required
def sync_dashboard():
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
    def generate():
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
    return render_template("logs.html")

@app.route("/users")
@admin_required
def users():
    return render_template("users.html", username=session.get("username", ""))

@app.route("/api/users", methods=["GET", "POST"])
@admin_required
def api_users():
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
    return render_template("settings.html")

@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
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
                    if task_type == "measure":
                        action = handle_measure(conn, event_type, deal_id, task_id, object_date)
                    elif task_type == "delivery":
                        action = handle_delivery(conn, event_type, deal_id, task_id, object_date)
                    elif task_type == "install":
                        action = handle_install(conn, event_type, deal_id, task_id, object_date, extra_fields)
                    elif task_type == "inspection":
                        action = handle_inspection(conn, event_type, deal_id, task_id, object_date)
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


@app.route("/deploy", methods=["POST"])
def deploy():
    try:
        secret    = GITHUB_WEBHOOK_SECRET.encode()
        signature = request.headers.get("X-Hub-Signature-256", "")
        body      = request.get_data()
        expected  = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return jsonify({"error": "unauthorized"}), 401

        def _restart():
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
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
init_db()
app.register_blueprint(reports_bp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
