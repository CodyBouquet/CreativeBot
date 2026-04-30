"""
Sync M365 group members into the local DB and manage per-user report
subscriptions. Reads the group via Microsoft Graph using the same app
registration as graph_mail.py (Application permission GroupMember.Read.All
required, scoped to whatever group MS_REPORT_GROUP_ID names).

Schema (added to data/sync.db on first sync):
    m365_users
        graph_id       Graph user object id (GUID) — stable primary key
        email          mail or userPrincipalName
        display_name
        last_synced_at ISO 8601 UTC
        active         1 if currently in the group, 0 if removed since last sync

    report_subscriptions
        user_email
        report_key     'inventory_low_stock', 'aging_invoices', etc.
        PRIMARY KEY(user_email, report_key)

Removed users keep their row (active=0) and their subscriptions, so a brief
remove/re-add round trip doesn't drop their config.
"""
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import requests

try:
    from .graph_mail import get_access_token  # when imported as email_reports.m365_directory
except ImportError:  # when run as `python -m email_reports.m365_directory` from project root
    from email_reports.graph_mail import get_access_token

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DB_PATH = os.environ.get("DB_PATH", "data/sync.db")
log = logging.getLogger(__name__)


# ---- Schema ---------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS m365_users (
    graph_id       TEXT PRIMARY KEY,
    email          TEXT NOT NULL,
    display_name   TEXT,
    last_synced_at TEXT NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1,
    rm_sales_id    TEXT
);

CREATE TABLE IF NOT EXISTS report_subscriptions (
    user_email TEXT NOT NULL,
    report_key TEXT NOT NULL,
    PRIMARY KEY (user_email, report_key)
);

CREATE TABLE IF NOT EXISTS report_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_key      TEXT    NOT NULL,
    ran_at          TEXT    NOT NULL,
    recipient_count INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_m365_users_email ON m365_users(email);
CREATE INDEX IF NOT EXISTS idx_subs_report      ON report_subscriptions(report_key);
CREATE INDEX IF NOT EXISTS idx_runs_key_at      ON report_runs(report_key, ran_at);
"""


def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_schema():
    with _conn() as c:
        c.executescript(SCHEMA)
        # Add rm_sales_id to existing m365_users tables (no-op if already there).
        cols = [r[1] for r in c.execute("PRAGMA table_info(m365_users)").fetchall()]
        if "rm_sales_id" not in cols:
            c.execute("ALTER TABLE m365_users ADD COLUMN rm_sales_id TEXT")


# ---- Graph pull -----------------------------------------------------------

def _fetch_group_members(group_id: str) -> list[dict]:
    """GET /groups/{id}/members, following @odata.nextLink for paging."""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url = (
        f"{GRAPH_BASE}/groups/{group_id}/members"
        "?$select=id,displayName,mail,userPrincipalName,accountEnabled"
        "&$top=999"
    )
    out: list[dict] = []
    while url:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"group/members {r.status_code}: {r.text[:400]}")
        data = r.json()
        for m in data.get("value", []):
            # Skip nested groups, devices, etc. — only @odata.type #microsoft.graph.user.
            if m.get("@odata.type", "").lower().endswith("user"):
                out.append(m)
        url = data.get("@odata.nextLink")
    return out


# ---- Sync -----------------------------------------------------------------

def sync_users(group_id: str | None = None) -> dict:
    """
    Pull current members of the configured group and reconcile m365_users.

    Returns a summary dict: {added, updated, deactivated, total_active}.
    """
    group_id = group_id or os.environ["MS_REPORT_GROUP_ID"]
    init_schema()

    members = _fetch_group_members(group_id)
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    seen_ids: set[str] = set()
    added = updated = 0

    with _conn() as c:
        for m in members:
            graph_id = m["id"]
            email = (m.get("mail") or m.get("userPrincipalName") or "").lower()
            if not email:
                log.warning("skipping member with no email: %s", graph_id)
                continue
            seen_ids.add(graph_id)
            display_name = m.get("displayName") or email

            row = c.execute(
                "SELECT email, display_name, active FROM m365_users WHERE graph_id = ?",
                (graph_id,),
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO m365_users (graph_id, email, display_name, last_synced_at, active) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (graph_id, email, display_name, now),
                )
                added += 1
            else:
                if (row["email"], row["display_name"], row["active"]) != (email, display_name, 1):
                    updated += 1
                c.execute(
                    "UPDATE m365_users SET email=?, display_name=?, last_synced_at=?, active=1 "
                    "WHERE graph_id=?",
                    (email, display_name, now, graph_id),
                )

        # Anyone in the table not seen this run is no longer in the group.
        if seen_ids:
            placeholders = ",".join("?" * len(seen_ids))
            c.execute(
                f"UPDATE m365_users SET active=0, last_synced_at=? "
                f"WHERE active=1 AND graph_id NOT IN ({placeholders})",
                (now, *seen_ids),
            )
        deactivated = c.total_changes - added - updated  # rough; only used for reporting
        total_active = c.execute("SELECT COUNT(*) FROM m365_users WHERE active=1").fetchone()[0]

    return {
        "added": added,
        "updated": updated,
        "deactivated": max(deactivated, 0),
        "total_active": total_active,
    }


# ---- Queries --------------------------------------------------------------

def list_active_users() -> list[sqlite3.Row]:
    init_schema()
    with _conn() as c:
        return c.execute(
            "SELECT graph_id, email, display_name, rm_sales_id FROM m365_users "
            "WHERE active=1 ORDER BY display_name"
        ).fetchall()


def get_subscribers(report_key: str) -> list[str]:
    """Return active subscriber emails for a given report key."""
    init_schema()
    with _conn() as c:
        return [
            r[0] for r in c.execute(
                "SELECT s.user_email FROM report_subscriptions s "
                "JOIN m365_users u ON u.email = s.user_email "
                "WHERE s.report_key = ? AND u.active = 1",
                (report_key,),
            ).fetchall()
        ]


def get_user_subscriptions(email: str) -> set[str]:
    """Return the set of report_keys this user is currently subscribed to."""
    init_schema()
    with _conn() as c:
        return {
            r[0] for r in c.execute(
                "SELECT report_key FROM report_subscriptions WHERE user_email = ?",
                (email.lower(),),
            ).fetchall()
        }


def set_subscriptions_for_user(email: str, report_keys: list[str]) -> None:
    """Replace this user's subscription set."""
    init_schema()
    email = email.lower()
    with _conn() as c:
        c.execute("DELETE FROM report_subscriptions WHERE user_email = ?", (email,))
        c.executemany(
            "INSERT INTO report_subscriptions (user_email, report_key) VALUES (?, ?)",
            [(email, k) for k in report_keys],
        )


def set_subscribers_for_report(report_key: str, emails: list[str]) -> None:
    """Replace the subscriber set for a single report (per-report card UI)."""
    init_schema()
    emails = [e.lower() for e in emails]
    with _conn() as c:
        c.execute("DELETE FROM report_subscriptions WHERE report_key = ?", (report_key,))
        c.executemany(
            "INSERT INTO report_subscriptions (user_email, report_key) VALUES (?, ?)",
            [(e, report_key) for e in emails],
        )


def set_rm_sales_id(email: str, rm_sales_id: str | None) -> None:
    """Set or clear the RM sales id for a user (used by sales reports)."""
    init_schema()
    val = (rm_sales_id or "").strip() or None
    with _conn() as c:
        c.execute(
            "UPDATE m365_users SET rm_sales_id = ? WHERE email = ?",
            (val, email.lower()),
        )


# ---- Run audit ------------------------------------------------------------

def record_run(report_key: str, recipient_count: int, status: str, error: str | None = None) -> None:
    """Append a row to report_runs. Stores counts only — no PII."""
    init_schema()
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with _conn() as c:
        c.execute(
            "INSERT INTO report_runs (report_key, ran_at, recipient_count, status, error) "
            "VALUES (?, ?, ?, ?, ?)",
            (report_key, now, int(recipient_count), status, error),
        )


def recent_runs(report_key: str, limit: int = 5) -> list[sqlite3.Row]:
    init_schema()
    with _conn() as c:
        return c.execute(
            "SELECT ran_at, recipient_count, status, error FROM report_runs "
            "WHERE report_key = ? ORDER BY ran_at DESC LIMIT ?",
            (report_key, int(limit)),
        ).fetchall()


# ---- Settings (piggybacks on app.py's settings table in sync.db) ----------

def get_setting(key: str, default: str | None = None) -> str | None:
    init_schema()
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    init_schema()
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---- CLI ------------------------------------------------------------------

def _cli():
    import argparse, json
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="Pull current members from MS_REPORT_GROUP_ID")
    sub.add_parser("list", help="Show active users in DB")
    s = sub.add_parser("subs", help="Show subscribers for a report key")
    s.add_argument("report_key")
    args = p.parse_args()

    if args.cmd == "sync":
        print(json.dumps(sync_users(), indent=2))
    elif args.cmd == "list":
        for row in list_active_users():
            print(f"{row['email']:40s} {row['display_name']}")
    elif args.cmd == "subs":
        for email in get_subscribers(args.report_key):
            print(email)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _cli()
