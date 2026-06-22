"""
Cron-driven report dispatcher.

Run from cron on the Pi (every 15 min during business hours is fine — the
script self-gates so it dispatches at most once per day):

    */15 5-10 * * 1-6 cd /home/admin/CreativeBot && \
        /home/admin/CreativeBot/venv/bin/python -m email_reports.scheduler \
        2>&1 | logger -t creativebot-reports

The schedule (time, days, timezone) lives in the `settings` table and is
edited via /reports/settings — change it in the UI, no crontab edits needed.

What it does on every cron tick:
  1. Read schedule (HH:MM, days, tz) from the settings table.
  2. Skip if today's day-of-week isn't enabled.
  3. Skip if local time is before HH:MM.
  4. Skip if `reports.last_dispatched_date` matches today (already ran).
  5. Otherwise: build a per-user digest from each recipient's subscribed
     report sections and send one email per recipient. Each report records
     a `report_runs` row with its recipient count.

`run_one_report(report_key)` is a separate manual-trigger path called by
the per-report "Run Now" button; it sends just that one report's section
to its current subscribers.

No email body or recipient list is persisted anywhere — only counts.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import AVAILABLE_REPORTS, m365_directory
from .graph_mail import GraphMailError, send_mail
from .modules import MODULES, ReportContext

log = logging.getLogger(__name__)

# 0=Mon..6=Sun (matches datetime.weekday())
_DAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ---- Schedule gate --------------------------------------------------------

def _read_schedule() -> dict:
    """Load the dispatch schedule (hour, minute, tz, enabled day codes) from the settings table."""
    return {
        "hour":   int(m365_directory.get_setting("reports.schedule.hour", "6")),
        "minute": int(m365_directory.get_setting("reports.schedule.minute", "0")),
        "tz":     m365_directory.get_setting("reports.schedule.tz", "America/Chicago"),
        "days":   set((m365_directory.get_setting("reports.schedule.days", "mon,tue,wed,thu,fri,sat") or "").split(",")),
    }


def _should_dispatch(now: datetime, schedule: dict) -> tuple[bool, str]:
    """Decide whether this cron tick should send: gates on enabled day, time-of-day, and the once-per-day already-dispatched marker. Returns (should_dispatch, reason)."""
    today_code = _DAY_CODES[now.weekday()]
    if today_code not in schedule["days"]:
        return False, f"day {today_code} not enabled"

    target = schedule["hour"] * 60 + schedule["minute"]
    current = now.hour * 60 + now.minute
    if current < target:
        return False, f"before scheduled time {schedule['hour']:02d}:{schedule['minute']:02d}"

    last = m365_directory.get_setting("reports.last_dispatched_date")
    today_iso = now.strftime("%Y-%m-%d")
    if last == today_iso:
        return False, f"already dispatched today ({today_iso})"

    return True, "due"


# ---- Dispatch -------------------------------------------------------------

def _wrap_digest(user, sections: list[tuple[str, str]]) -> str:
    """
    Compose the multi-section HTML body for one recipient.

    Each section already renders itself as a self-contained card (see
    modules.base.report_card), so the wrapper just supplies the outer shell —
    a centered, max-width column on a light backdrop — and stacks the cards.
    """
    first_name = (user["display_name"] or user["email"]).split(" ")[0]
    date_str = datetime.now().strftime("%b %d, %Y")
    parts = [
        '<html><body style="margin:0; padding:0; background:#f4f5f7;">',
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f4f5f7;"><tr><td align="center" style="padding:24px 12px;">',
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:680px; font-family:Arial,Helvetica,sans-serif; color:#222;">',
        '<tr><td style="padding:0 4px 4px;">',
        f'<p style="margin:0; font-size:15px;">Hi {first_name},</p>',
        f'<p style="margin:4px 0 0; color:#888; font-size:13px;">Your reports for {date_str}</p>',
        '</td></tr>',
    ]
    for _key, html in sections:
        parts.append(f'<tr><td>{html}</td></tr>')
    parts.append(
        '<tr><td style="padding:18px 4px 0; color:#aaa; font-size:11px;">'
        '— CreativeBot · manage subscriptions in /reports/settings</td></tr>'
    )
    parts.append('</table></td></tr></table></body></html>')
    return "\n".join(parts)


def _dispatch_for_user(user, subscribed_keys: set[str], ctx: ReportContext,
                      counts: dict, errors: dict) -> bool:
    """Build and send one digest. Returns True on send (or a no-op skip)."""
    sections: list[tuple[str, str]] = []
    for r in AVAILABLE_REPORTS:  # preserve display order in the digest
        key = r["key"]
        if key not in subscribed_keys:
            continue
        try:
            html = MODULES[key](user, ctx)
        except Exception as e:
            log.exception("module %s failed for %s", key, user["email"])
            errors[key] = errors[key] or f"{type(e).__name__}: {e}"
            continue
        if html:
            sections.append((key, html))
            counts[key] = counts.get(key, 0) + 1

    if not sections:
        return False

    body = _wrap_digest(user, sections)
    subject = f"CreativeBot Reports — {datetime.now().strftime('%b %d, %Y')}"
    try:
        send_mail(to=user["email"], subject=subject, body_html=body)
        return True
    except GraphMailError as e:
        log.exception("send failed for %s", user["email"])
        for key, _ in sections:
            errors[key] = errors[key] or f"send failed: {e}"
        return False


def run_scheduled() -> dict:
    """Cron entry point. Returns a summary dict (also logged)."""
    schedule = _read_schedule()
    now = datetime.now(ZoneInfo(schedule["tz"]))
    due, why = _should_dispatch(now, schedule)
    if not due:
        log.info("scheduler tick: skipped (%s)", why)
        return {"dispatched": False, "reason": why}

    log.info("scheduler tick: dispatching")
    ctx = ReportContext()
    users = m365_directory.list_active_users()
    counts: dict[str, int] = {key: 0 for key in MODULES}
    errors: dict[str, str | None] = {key: None for key in MODULES}
    sent_users = 0

    for user in users:
        subs = m365_directory.get_user_subscriptions(user["email"])
        if not subs:
            continue
        if _dispatch_for_user(user, subs, ctx, counts, errors):
            sent_users += 1

    for key in MODULES:
        m365_directory.record_run(
            report_key=key,
            recipient_count=counts[key],
            status="ok" if errors[key] is None else "fail",
            error=errors[key],
        )

    m365_directory.set_setting("reports.last_dispatched_date", now.strftime("%Y-%m-%d"))
    summary = {"dispatched": True, "users_sent": sent_users, "counts": counts}
    log.info("scheduler tick: %s", summary)
    return summary


def run_one_report(report_key: str) -> dict:
    """Manual trigger path — sends just one report's section to its subscribers."""
    if report_key not in MODULES:
        raise ValueError(f"unknown report: {report_key}")

    label = next(r["label"] for r in AVAILABLE_REPORTS if r["key"] == report_key)
    ctx = ReportContext()
    subscribers = set(m365_directory.get_subscribers(report_key))
    sent = 0
    last_error: str | None = None

    for user in m365_directory.list_active_users():
        if user["email"] not in subscribers:
            continue
        try:
            html = MODULES[report_key](user, ctx)
        except Exception as e:
            log.exception("module %s failed for %s", report_key, user["email"])
            last_error = f"{type(e).__name__}: {e}"
            continue
        if not html:
            continue
        try:
            send_mail(
                to=user["email"],
                subject=f"CreativeBot — {label}",
                body_html=_wrap_digest(user, [(report_key, html)]),
            )
            sent += 1
        except GraphMailError as e:
            log.exception("send failed for %s", user["email"])
            last_error = f"send failed: {e}"

    m365_directory.record_run(
        report_key=report_key,
        recipient_count=sent,
        status="ok" if last_error is None else "fail",
        error=last_error,
    )
    return {"recipient_count": sent, "error": last_error}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = run_scheduled()
    sys.exit(0 if result.get("dispatched") or result.get("reason") else 1)
