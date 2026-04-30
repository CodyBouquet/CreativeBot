"""
Email Reports blueprint — scheduled reports from Rollmaster via Microsoft Graph.

Phase 1: scaffold only. No external dependencies.
"""

from flask import Blueprint, render_template, session, redirect, url_for
from functools import wraps
import sqlite3
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

bp = Blueprint("reports", __name__)

REPORTS_DB_PATH = os.environ.get("REPORTS_DB_PATH", "/home/admin/CreativeBot/data/reports.db")


# ---------------------------------------------------------------------------
# AUTH (mirrors app.py decorators — blueprint can't import from app)
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("pin_page"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# DATABASE — separate from sync.db
# ---------------------------------------------------------------------------
def get_reports_db():
    Path(REPORTS_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(REPORTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_reports_db():
    with get_reports_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS report_subscriptions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                report_type    TEXT    NOT NULL,
                frequency      TEXT    NOT NULL DEFAULT 'daily',
                send_time      TEXT    NOT NULL DEFAULT '08:00',
                entra_group_id TEXT,
                entra_group_name TEXT,
                enabled        INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT    NOT NULL,
                updated_at     TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS send_history (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id   INTEGER NOT NULL REFERENCES report_subscriptions(id),
                sent_at           TEXT    NOT NULL,
                recipients_count  INTEGER NOT NULL DEFAULT 0,
                status            TEXT    NOT NULL DEFAULT 'ok',
                error_message     TEXT
            );

            CREATE TABLE IF NOT EXISTS report_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_history_sub
                ON send_history(subscription_id);
            CREATE INDEX IF NOT EXISTS idx_history_sent
                ON send_history(sent_at);
        """)
    logger.info(f"Reports database initialised at {REPORTS_DB_PATH}")


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@bp.route("/reports")
@login_required
def reports_home():
    return render_template("reports.html", username=session.get("username", ""))
