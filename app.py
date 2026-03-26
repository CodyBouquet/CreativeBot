from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from dotenv import load_dotenv
import requests
import logging
import os
import sqlite3
import json
import hmac
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path
from functools import wraps
import threading

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-in-production")

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

# Arrivy template IDs → task type
TEMPLATE_MAP = {
    5395407346073600: "install",
    5627485400596480: "measure",
    5278551184506880: "delivery",
}

# Pipedrive custom field keys
PD_FIELDS = {
    "install_start":  "197d71fa84fd5221fa4a875fbac9526c1d554139",
    "install_part2":  "7492f008b747af364836514d752961176f1f0307",
    "measure_date":   "e23dc895627529b276d3b1b0ec7c8acc75317b1c",
    "delivery_date":  "d0d424fcacbdf264297a050ff96a799823316d9f",
}

INSTALL_COMPLETE_STAGE_ID      = 12
INSTALL_SCHEDULED_STAGE_ID     = 10
INSTALL_UNSCHEDULED_STAGE_ID   = 9

ALLOWED_DASHBOARD_IP = "127.0.0.1"
DASHBOARD_ENDPOINTS  = {"dashboard", "logs", "users", "settings_page", "pin_page", "verify_pin",
                        "change_pin", "logout", "api_stats", "api_logs", "api_users",
                        "api_user_delete", "api_access_log", "api_settings"}

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
                outcome     TEXT,
                archived    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS task_state (
                task_id      INTEGER PRIMARY KEY,
                deal_id      INTEGER NOT NULL,
                task_type    TEXT    NOT NULL,
                task_date    TEXT,
                status       TEXT    NOT NULL DEFAULT 'active',
                last_updated TEXT    NOT NULL,
                archived     INTEGER NOT NULL DEFAULT 0
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
    # Migrations for existing DBs
    with get_db() as conn:
        for migration in [
            "ALTER TABLE events ADD COLUMN outcome TEXT",
        ]:
            try:
                conn.execute(migration)
            except Exception:
                pass
        # Migrate legacy PIN from settings → admin user
        row = conn.execute("SELECT value FROM settings WHERE key='pin'").fetchone()
        if row:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO users (username, pin, role, created_at) VALUES (?,?,?,?)",
                    ("admin", row["value"], "admin", datetime.utcnow().isoformat())
                )
            except Exception:
                pass
    logger.info(f"Database initialised at {DB_PATH}")

def get_setting(key):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

def set_setting(key, value):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

def delete_setting(key):
    with get_db() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))

def store_event(conn, deal_id, task_id, event_type, task_type, raw_payload):
    cur = conn.execute(
        """INSERT INTO events (received_at, deal_id, task_id, event_type, task_type, raw_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (datetime.utcnow().isoformat(), deal_id, task_id, event_type, task_type,
         json.dumps(raw_payload))
    )
    return cur.lastrowid

def set_event_outcome(conn, event_id, outcome):
    conn.execute("UPDATE events SET outcome=? WHERE id=?", (outcome, event_id))


def upsert_task_state(conn, task_id, deal_id, task_type, task_date, status="active"):
    conn.execute(
        """INSERT INTO task_state (task_id, deal_id, task_type, task_date, status, last_updated)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(task_id) DO UPDATE SET
               task_date    = excluded.task_date,
               status       = excluded.status,
               last_updated = excluded.last_updated""",
        (task_id, deal_id, task_type, task_date, status, datetime.utcnow().isoformat())
    )

def archive_deal(conn, deal_id):
    conn.execute("UPDATE events     SET archived = 1 WHERE deal_id = ?", (deal_id,))
    conn.execute("UPDATE task_state SET archived = 1 WHERE deal_id = ?", (deal_id,))
    logger.info(f"Archived deal {deal_id}")

# ---------------------------------------------------------------------------
# PIPEDRIVE
# ---------------------------------------------------------------------------
PD_BASE = "https://api.pipedrive.com/v1"


def pd_update_deal(deal_id, fields):
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


# ---------------------------------------------------------------------------
# TASK HANDLERS
# ---------------------------------------------------------------------------
def handle_measure(conn, event_type, deal_id, task_id, object_date):
    task_date = parse_arrivy_date(object_date)
    if event_type in ("TASK_CREATED", "TASK_UPDATED"):
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: task_date})
        upsert_task_state(conn, task_id, deal_id, "measure", task_date)
        return f"Set measure_date → {task_date}"
    elif event_type == "TASK_CANCELLED":
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: None})
        upsert_task_state(conn, task_id, deal_id, "measure", task_date, status="cancelled")
        return "Cleared measure_date"
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "measure", task_date, status="completed")
        return "Marked completed"
    return "No action"

def handle_delivery(conn, event_type, deal_id, task_id, object_date):
    task_date = parse_arrivy_date(object_date)
    if event_type in ("TASK_CREATED", "TASK_UPDATED"):
        pd_update_deal(deal_id, {PD_FIELDS["delivery_date"]: task_date})
        upsert_task_state(conn, task_id, deal_id, "delivery", task_date)
        return f"Set delivery_date → {task_date}"
    elif event_type == "TASK_CANCELLED":
        pd_update_deal(deal_id, {PD_FIELDS["delivery_date"]: None})
        upsert_task_state(conn, task_id, deal_id, "delivery", task_date, status="cancelled")
        return "Cleared delivery_date"
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "delivery", task_date, status="completed")
        return "Marked completed"
    return "No action"

def handle_install(conn, event_type, deal_id, task_id, object_date):
    task_date = parse_arrivy_date(object_date)
    if event_type in ("TASK_CREATED", "TASK_UPDATED"):
        upsert_task_state(conn, task_id, deal_id, "install", task_date)
        dates = recalc_install(conn, deal_id)
        s = dates[0] if len(dates) > 0 else None
        p = dates[1] if len(dates) > 1 else None
        return f"Recalculated → install_start={s}, part2={p}"
    elif event_type == "TASK_CANCELLED":
        upsert_task_state(conn, task_id, deal_id, "install", task_date, status="cancelled")
        dates = recalc_install(conn, deal_id)
        if len(dates) == 0:
            pd_move_stage(deal_id, INSTALL_UNSCHEDULED_STAGE_ID)
            return "Cancelled. No active installs → moved to stage 9"
        s = dates[0]; p = dates[1] if len(dates) > 1 else None
        return f"Cancelled. Recalculated → install_start={s}, part2={p}"
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "install", task_date, status="completed")
        pd_move_stage(deal_id, INSTALL_COMPLETE_STAGE_ID)
        set_setting(f"pending_recalc_{deal_id}", "1")
        return f"Completed {task_date} → moved to stage 12"
    return "No action"

def recalc_measure(conn, deal_id):
    rows = conn.execute(
        "SELECT task_date FROM task_state WHERE deal_id=? AND task_type='measure' AND status='active' AND archived=0 ORDER BY task_date",
        (deal_id,)
    ).fetchall()
    date = rows[0]["task_date"] if rows else None
    pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: date})

def recalc_delivery(conn, deal_id):
    rows = conn.execute(
        "SELECT task_date FROM task_state WHERE deal_id=? AND task_type='delivery' AND status='active' AND archived=0 ORDER BY task_date",
        (deal_id,)
    ).fetchall()
    date = rows[0]["task_date"] if rows else None
    pd_update_deal(deal_id, {PD_FIELDS["delivery_date"]: date})

def recalc_install(conn, deal_id):
    rows = conn.execute(
        "SELECT task_date FROM task_state WHERE deal_id=? AND task_type='install' AND status='active' AND archived=0 ORDER BY task_date",
        (deal_id,)
    ).fetchall()
    dates = [r["task_date"] for r in rows]
    pd_update_deal(deal_id, {
        PD_FIELDS["install_start"]: dates[0] if len(dates) > 0 else None,
        PD_FIELDS["install_part2"]: dates[1] if len(dates) > 1 else None,
    })
    return dates

def handle_deleted(conn, task_id, deal_id, task_type):
    conn.execute("DELETE FROM task_state WHERE task_id=?", (task_id,))
    logger.info(f"Deleted task {task_id} (deal={deal_id}, type={task_type}) from task_state")
    if task_type == "measure":
        recalc_measure(conn, deal_id)
        return "Deleted. Recalculated measure_date"
    elif task_type == "delivery":
        recalc_delivery(conn, deal_id)
        return "Deleted. Recalculated delivery_date"
    elif task_type == "install":
        dates = recalc_install(conn, deal_id)
        if len(dates) == 0:
            pd_move_stage(deal_id, INSTALL_UNSCHEDULED_STAGE_ID)
            return "Deleted. No active installs → moved to stage 9"
        s = dates[0]; p = dates[1] if len(dates) > 1 else None
        return f"Deleted. Recalculated → install_start={s}, part2={p}"
    return "Deleted (unknown type — no Pipedrive action)"


# ---------------------------------------------------------------------------
# IP RESTRICTION
# ---------------------------------------------------------------------------
@app.before_request
def restrict_dashboard_by_ip():
    if request.endpoint in DASHBOARD_ENDPOINTS:
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        client_ip = client_ip.split(",")[0].strip()
        if client_ip != ALLOWED_DASHBOARD_IP:
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
            return redirect(url_for("dashboard"))
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
        session["username"]      = user["username"]
        session["role"]          = user["role"]
        ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
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
        existing = conn.execute("SELECT id FROM users WHERE pin=? AND username!=?", (new_pin, username)).fetchone()
        if existing:
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
def dashboard():
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

@app.route("/api/clear-db", methods=["POST"])
@login_required
def clear_db():
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM task_state")
        logger.warning("Database cleared via dashboard")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.exception(f"Clear DB failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats")
@login_required
def api_stats():
    with get_db() as conn:
        total  = conn.execute("SELECT COUNT(*) as c FROM events WHERE archived=0").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) as c FROM task_state WHERE status='active' AND archived=0").fetchone()["c"]
        recent = conn.execute(
            "SELECT event_type, task_type, deal_id, received_at FROM events WHERE archived=0 ORDER BY received_at DESC LIMIT 5"
        ).fetchall()
    return jsonify({
        "total_events": total,
        "active_tasks": active,
        "recent": [dict(r) for r in recent]
    })

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
    return jsonify({"logs": [dict(r) for r in rows]})

@app.route("/settings")
@admin_required
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

@app.route("/logs")
@login_required
def logs():
    return render_template("logs.html")

@app.route("/api/logs")
@login_required
def api_logs():
    limit = min(int(request.args.get("limit", 100)), 1000)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, received_at, deal_id, task_id, event_type, task_type, outcome, raw_json
               FROM events WHERE archived=0 ORDER BY received_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    logs = []
    for row in rows:
        raw = json.loads(row["raw_json"])
        logs.append({
            "id":         row["id"],
            "time":       row["received_at"][11:19] if row["received_at"] else "—",
            "deal_id":    row["deal_id"],
            "event_type": (row["event_type"] or "—").replace("TASK_", ""),
            "task_type":  row["task_type"] or "unknown",
            "task_date":  (raw.get("OBJECT_DATE") or "")[:10] or "—",
            "title":      raw.get("TITLE") or "—",
            "outcome":    row["outcome"] or "—",
        })
    return jsonify({"logs": logs})

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

        if event_type not in ("TASK_CREATED", "TASK_UPDATED", "TASK_CANCELLED", "TASK_COMPLETED", "TASK_DELETED"):
            return jsonify({"status": "ignored"}), 200

        if not external_id:
            return jsonify({"status": "ignored", "reason": "no external id"}), 200

        if str(external_id) != "29905":
            return jsonify({"status": "ignored", "reason": "not test deal"}), 200

        deal_id   = int(external_id)
        task_type = TEMPLATE_MAP.get(template_id)

        with get_db() as conn:
            event_id = store_event(conn, deal_id, task_id, event_type, task_type, payload)
            if event_type == "TASK_DELETED":
                outcome = handle_deleted(conn, task_id, deal_id, task_type)
            elif not task_type:
                outcome = "Stored — unknown template, no Pipedrive action"
            elif task_type == "measure":
                outcome = handle_measure(conn, event_type, deal_id, task_id, object_date)
            elif task_type == "delivery":
                outcome = handle_delivery(conn, event_type, deal_id, task_id, object_date)
            elif task_type == "install":
                outcome = handle_install(conn, event_type, deal_id, task_id, object_date)
            else:
                outcome = "No handler"
            set_event_outcome(conn, event_id, outcome)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.exception(f"Arrivy webhook error: {e}")
        return jsonify({"error": str(e)}), 500


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

        if event == "updated.deal" and deal_id:
            if status in ("won", "lost"):
                with get_db() as conn:
                    archive_deal(conn, deal_id)
            elif stage_id == INSTALL_SCHEDULED_STAGE_ID:
                flag_key = f"pending_recalc_{deal_id}"
                if get_setting(flag_key):
                    with get_db() as conn:
                        recalc_install(conn, deal_id)
                    delete_setting(flag_key)
                    logger.info(f"Post-completion recalc ran for deal {deal_id}")

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
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5001)
