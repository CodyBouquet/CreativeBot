"""
Email Reports blueprint.

Owns the settings UI (which users get which scheduled reports). User list is
synced from a single M365 group via m365_directory; subscriptions live in
data/sync.db. The bot does NOT retain copies of sent mail beyond a one-line
operational log entry per send.
"""
import logging
import threading
from functools import wraps

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from . import m365_directory

logger = logging.getLogger(__name__)

bp = Blueprint("reports", __name__)


# Catalog of reports the dashboard knows about. Add a new entry here when a
# new module ships; everything (cards, detail pages, scheduler dispatch) keys
# off this list.
#   key:         stable identifier; used in DB and module imports
#   label:       short title for cards and column headers
#   description: one-line subtitle on the dashboard card
#   icon:        single emoji shown on the card
#   implemented: True if the module's build_section returns real data
AVAILABLE_REPORTS: list[dict] = [
    {
        "key": "sales_report",
        "label": "Sales Report",
        "description": "Per-salesperson totals (uses each user's RM Sales ID)",
        "icon": "💼",
        "implemented": False,
    },
    {
        "key": "master_sales_report",
        "label": "Master Sales Report",
        "description": "Per-branch, overall, and per-rep breakdown",
        "icon": "📊",
        "implemented": False,
    },
    {
        "key": "inventory_low_stock",
        "label": "Inventory — Low Stock",
        "description": "Items below safety stock with reorder qty",
        "icon": "📦",
        "implemented": True,
    },
    {
        "key": "late_net30",
        "label": "Late Net30",
        "description": "Net30 invoices aged past due",
        "icon": "💸",
        "implemented": False,
    },
]


def report_by_key(key: str) -> dict | None:
    """Look up a report's catalog entry by its key, or None if unknown."""
    return next((r for r in AVAILABLE_REPORTS if r["key"] == key), None)


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
    """Route decorator: require an authenticated admin session (403 otherwise)."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        """Wrapper: require the admin role (login is enforced by the stacked @login_required)."""
        if session.get("role") != "admin":
            return ("Forbidden", 403)
        return f(*args, **kwargs)
    return decorated


# ---- Routes ---------------------------------------------------------------

@bp.route("/reports")
@login_required
def reports_home():
    """Reports dashboard — one card per report plus a Settings card."""
    cards = []
    for r in AVAILABLE_REPORTS:
        recipients = len(m365_directory.get_subscribers(r["key"]))
        cards.append({**r, "recipient_count": recipients})
    return render_template(
        "reports.html",
        username=session.get("username", ""),
        cards=cards,
    )


@bp.route("/reports/<report_key>", methods=["GET"])
@admin_required
def report_detail(report_key: str):
    """Admin page for one report: subscriber checkboxes (pre-checked from current subs) plus recent run history."""
    report = report_by_key(report_key)
    if not report:
        return ("Unknown report", 404)
    users = m365_directory.list_active_users()
    selected = set(m365_directory.get_subscribers(report_key))
    runs = m365_directory.recent_runs(report_key, limit=5)
    return render_template(
        "report_detail.html",
        username=session.get("username", ""),
        report=report,
        users=users,
        selected=selected,
        runs=runs,
    )


@bp.route("/reports/<report_key>", methods=["POST"])
@admin_required
def report_save(report_key: str):
    """Persist the subscriber set for one report from the checkbox form, then redirect back to its detail page."""
    if not report_by_key(report_key):
        return ("Unknown report", 404)
    chosen = [
        u["email"] for u in m365_directory.list_active_users()
        if request.form.get(f"sub:{u['email']}") == "on"
    ]
    m365_directory.set_subscribers_for_report(report_key, chosen)
    logger.info("subscriptions for %s set to %d users", report_key, len(chosen))
    return redirect(url_for("reports.report_detail", report_key=report_key))


@bp.route("/reports/<report_key>/run", methods=["POST"])
@admin_required
def report_run_now(report_key: str):
    """
    Manual trigger — sends this report (only) to its current subscribers.

    The build can be slow (the inventory report does a multi-minute BMS pull), so
    we run it in a background thread and return immediately rather than blocking the
    request until it times out at the proxy. The outcome is recorded by
    run_one_report into report_runs, visible in the detail page's "Last 5 Runs".
    """
    if not report_by_key(report_key):
        return jsonify(ok=False, error="unknown report"), 404
    try:
        from .scheduler import run_one_report
    except ImportError:
        return jsonify(ok=False, error="scheduler not yet wired"), 500

    def _run():
        """Build + send off-thread so the HTTP request can return right away."""
        try:
            run_one_report(report_key)
        except Exception:
            logger.exception("manual run for %s failed", report_key)

    threading.Thread(target=_run, daemon=True, name=f"manual-run-{report_key}").start()
    logger.info("manual run for %s started (background)", report_key)
    return jsonify(ok=True, started=True), 202


@bp.route("/reports/settings", methods=["GET"])
@admin_required
def settings_page():
    """Render the global report settings page: dispatch schedule plus per-user RM Sales ID inputs."""
    users = m365_directory.list_active_users()
    schedule = {
        "hour":   m365_directory.get_setting("reports.schedule.hour", "6"),
        "minute": m365_directory.get_setting("reports.schedule.minute", "0"),
        "tz":     m365_directory.get_setting("reports.schedule.tz", "America/Chicago"),
        "days":   m365_directory.get_setting("reports.schedule.days", "mon,tue,wed,thu,fri,sat"),
    }
    return render_template(
        "report_settings.html",
        username=session.get("username", ""),
        users=users,
        schedule=schedule,
    )


@bp.route("/reports/settings", methods=["POST"])
@admin_required
def settings_save():
    """Persist the dispatch schedule and per-user RM Sales IDs from the settings form."""
    # Schedule
    if "schedule_hour" in request.form:
        m365_directory.set_setting("reports.schedule.hour",   request.form.get("schedule_hour", "6"))
        m365_directory.set_setting("reports.schedule.minute", request.form.get("schedule_minute", "0"))
        m365_directory.set_setting("reports.schedule.days",   request.form.get("schedule_days", "mon,tue,wed,thu,fri,sat"))
        m365_directory.set_setting("reports.schedule.tz",     request.form.get("schedule_tz", "America/Chicago"))

    # RM Sales IDs (one input per active user)
    for u in m365_directory.list_active_users():
        field = f"rm_sales_id:{u['email']}"
        if field in request.form:
            m365_directory.set_rm_sales_id(u["email"], request.form.get(field, "").strip() or None)

    logger.info("global report settings saved")
    return redirect(url_for("reports.settings_page"))


@bp.route("/reports/sync_users", methods=["POST"])
@admin_required
def sync_users_now():
    """Manual trigger: re-sync the M365 group membership into the local user table; returns the sync summary as JSON."""
    try:
        result = m365_directory.sync_users()
        logger.info("M365 group sync: %s", result)
        return jsonify(ok=True, **result)
    except Exception as e:
        logger.exception("M365 group sync failed")
        return jsonify(ok=False, error=str(e)), 500
