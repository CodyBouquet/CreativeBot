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
    "install_part2":  "17492f008b747af364836514d752961176f1f0307",
    "measure_date":   "e23dc895627529b276d3b1b0ec7c8acc75317b1c",
    "delivery_date":  "d0d424fcacbdf264297a050ff96a799823316d9f",
}

INSTALL_COMPLETE_STAGE_ID = 12

ALLOWED_DASHBOARD_IP = "127.0.0.1"
DASHBOARD_ENDPOINTS  = {"dashboard", "pin_page", "verify_pin", "change_pin", "logout", "api_stats"}

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
                archived    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS task_state (
                task_id      INTEGER PRIMARY KEY,
                deal_id      INTEGER NOT NULL,
                task_type    TEXT    NOT NULL,
                current_date TEXT,
                status       TEXT    NOT NULL DEFAULT 'active',
                last_updated TEXT    NOT NULL,
                archived     INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_deal ON events(deal_id);
            CREATE INDEX IF NOT EXISTS idx_task_deal   ON task_state(deal_id);

            INSERT OR IGNORE INTO settings (key, value) VALUES ('pin', '0000');
        """)
    logger.info(f"Database initialised at {DB_PATH}")

def get_setting(key):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

def set_setting(key, value):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

def store_event(conn, deal_id, task_id, event_type, task_type, raw_payload):
    conn.execute(
        """INSERT INTO events (received_at, deal_id, task_id, event_type, task_type, raw_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (datetime.utcnow().isoformat(), deal_id, task_id, event_type, task_type,
         json.dumps(raw_payload))
    )

def get_task_state(conn, task_id):
    return conn.execute("SELECT * FROM task_state WHERE task_id = ?", (task_id,)).fetchone()

def upsert_task_state(conn, task_id, deal_id, task_type, current_date, status="active"):
    conn.execute(
        """INSERT INTO task_state (task_id, deal_id, task_type, current_date, status, last_updated)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(task_id) DO UPDATE SET
               current_date = excluded.current_date,
               status       = excluded.status,
               last_updated = excluded.last_updated""",
        (task_id, deal_id, task_type, current_date, status, datetime.utcnow().isoformat())
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
        return datetime.fromisoformat(date_str).strftime("%Y-%m-%d")
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
    if event_type in ("TASK_CREATED", "TASK_UPDATED"):
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: date})
        upsert_task_state(conn, task_id, deal_id, "measure", date)
    elif event_type == "TASK_CANCELLED":
        pd_update_deal(deal_id, {PD_FIELDS["measure_date"]: None})
        upsert_task_state(conn, task_id, deal_id, "measure", date, status="cancelled")
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "measure", date, status="completed")

def handle_delivery(conn, event_type, deal_id, task_id, object_date):
    date = parse_arrivy_date(object_date)
    if event_type in ("TASK_CREATED", "TASK_UPDATED"):
        pd_update_deal(deal_id, {PD_FIELDS["delivery_date"]: date})
        upsert_task_state(conn, task_id, deal_id, "delivery", date)
    elif event_type == "TASK_CANCELLED":
        pd_update_deal(deal_id, {PD_FIELDS["delivery_date"]: None})
        upsert_task_state(conn, task_id, deal_id, "delivery", date, status="cancelled")
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "delivery", date, status="completed")

def handle_install(conn, event_type, deal_id, task_id, object_date):
    date           = parse_arrivy_date(object_date)
    previous_state = get_task_state(conn, task_id)
    previous_date  = previous_state["current_date"] if previous_state else None
    deal           = pd_get_deal(deal_id)
    install_start  = deal.get(PD_FIELDS["install_start"])
    install_part2  = deal.get(PD_FIELDS["install_part2"])

    if event_type == "TASK_CREATED":
        _install_slot_date(date, install_start, install_part2, deal_id)
        upsert_task_state(conn, task_id, deal_id, "install", date)
    elif event_type == "TASK_UPDATED":
        if previous_date:
            if dates_match(previous_date, install_start):
                pd_update_deal(deal_id, {PD_FIELDS["install_start"]: None})
                install_start = None
            elif dates_match(previous_date, install_part2):
                pd_update_deal(deal_id, {PD_FIELDS["install_part2"]: None})
                install_part2 = None
        _install_slot_date(date, install_start, install_part2, deal_id)
        upsert_task_state(conn, task_id, deal_id, "install", date)
    elif event_type == "TASK_CANCELLED":
        if dates_match(date, install_start):
            pd_update_deal(deal_id, {
                PD_FIELDS["install_start"]: install_part2,
                PD_FIELDS["install_part2"]: None,
            })
        elif dates_match(date, install_part2):
            pd_update_deal(deal_id, {PD_FIELDS["install_part2"]: None})
        upsert_task_state(conn, task_id, deal_id, "install", date, status="cancelled")
    elif event_type == "TASK_COMPLETED":
        upsert_task_state(conn, task_id, deal_id, "install", date, status="completed")
        if dates_match(date, install_start):
            pd_move_stage(deal_id, INSTALL_COMPLETE_STAGE_ID)

def _install_slot_date(date, install_start, install_part2, deal_id):
    if not install_start:
        pd_update_deal(deal_id, {PD_FIELDS["install_start"]: date})
        return
    try:
        new_dt   = datetime.strptime(date, "%Y-%m-%d")
        start_dt = datetime.strptime(install_start[:10], "%Y-%m-%d")
    except Exception as e:
        logger.error(f"Date parse error: {e}")
        return
    if new_dt < start_dt:
        pd_update_deal(deal_id, {
            PD_FIELDS["install_start"]: date,
            PD_FIELDS["install_part2"]: install_start[:10],
        })
    else:
        pd_update_deal(deal_id, {PD_FIELDS["install_part2"]: date})

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

@app.route("/pin", methods=["GET"])
def pin_page():
    return render_template("pin.html")

@app.route("/pin/verify", methods=["POST"])
def verify_pin():
    data = request.get_json(force=True)
    entered = data.get("pin", "")
    stored  = get_setting("pin") or "0000"
    if entered == stored:
        session["authenticated"] = True
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "error", "message": "Incorrect PIN"}), 401

@app.route("/pin/change", methods=["POST"])
@login_required
def change_pin():
    data    = request.get_json(force=True)
    new_pin = data.get("pin", "")
    if not new_pin.isdigit() or len(new_pin) != 4:
        return jsonify({"status": "error", "message": "PIN must be 4 digits"}), 400
    set_setting("pin", new_pin)
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
                           total_events=total_events)

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

# ---------------------------------------------------------------------------
# WEBHOOK ENDPOINTS
# ---------------------------------------------------------------------------
@app.route("/arrivy-webhook", methods=["POST"])
def arrivy_webhook():
    try:
        payload = request.get_json(force=True)
        if not payload:
            return jsonify({"error": "empty payload"}), 400

        event_type  = payload.get("EVENT_TYPE")
        template_id = payload.get("OBJECT_TEMPLATE_ID")
        object_date = payload.get("OBJECT_DATE")
        external_id = payload.get("OBJECT_EXTERNAL_ID")
        task_id     = payload.get("OBJECT_ID")

        logger.info(f"Arrivy: {event_type} | template={template_id} | deal={external_id} | task={task_id}")

        if event_type not in ("TASK_CREATED", "TASK_UPDATED", "TASK_CANCELLED", "TASK_COMPLETED"):
            return jsonify({"status": "ignored"}), 200

        if not external_id:
            return jsonify({"status": "ignored", "reason": "no external id"}), 200

        if str(external_id) != "29905":
            return jsonify({"status": "ignored", "reason": "not test deal"}), 200

        deal_id   = int(external_id)
        task_type = TEMPLATE_MAP.get(template_id)

        with get_db() as conn:
            store_event(conn, deal_id, task_id, event_type, task_type, payload)
            if not task_type:
                return jsonify({"status": "stored", "reason": "unknown template"}), 200
            if task_type == "measure":
                handle_measure(conn, event_type, deal_id, task_id, object_date)
            elif task_type == "delivery":
                handle_delivery(conn, event_type, deal_id, task_id, object_date)
            elif task_type == "install":
                handle_install(conn, event_type, deal_id, task_id, object_date)

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
        event   = payload.get("event")
        current = payload.get("current", {})
        status  = current.get("status")
        deal_id = current.get("id")
        if event == "updated.deal" and status in ("won", "lost") and deal_id:
            with get_db() as conn:
                archive_deal(conn, deal_id)
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

        subprocess.run(["git", "-C", REPO_PATH, "pull"], check=True)
        subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME], check=True)
        logger.info("Auto-deploy successful")
        return jsonify({"status": "deployed"}), 200
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
