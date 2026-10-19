"""
People directory: who each CreativeBot user is in M365, Arrivy, Pipedrive and
Rollmaster, and whether they may sign in to the Pi's UI.

The `users` table (app.py) is the spine — one row per person. A row may have a
PIN and Pi access, or be a mapping-only record (an installer who never touches
the kiosk). Each external link is an id, never a name, so renames upstream
don't break it:

    m365_graph_id      → m365_users.graph_id (local copy of the M365 group)
    arrivy_entity_id   → Arrivy /entities id
    pipedrive_user_id  → Pipedrive /users id
    rm_sales_id        → Rollmaster salesperson code (C_SLSID, e.g. "MRB")
    rm_branch          → branch number they work out of (not the RM warehouse)

rm_sales_id is also kept on m365_users, where the sales reports read it, so a
change on either side is written to both (see set_rm_sales_id here and in
email_reports.m365_directory).
"""
import logging
import os
import re
import time

import requests

from email_reports import m365_directory

log = logging.getLogger(__name__)

ARRIVY_BASE = "https://app.arrivy.com/api"
PD_BASE     = "https://api.pipedrive.com/v1"

LINK_FIELDS = ("m365_graph_id", "arrivy_entity_id", "pipedrive_user_id", "rm_sales_id")

_cache = {"at": 0.0, "data": None}
CACHE_SECONDS = 300


# ---- Directory pulls --------------------------------------------------------

def _arrivy_people():
    """Arrivy entities (crew, sales, office) as [{id, name, email, type, active}]."""
    r = requests.get(f"{ARRIVY_BASE}/entities", timeout=20, headers={
        "X-Auth-Key": os.environ.get("ARRIVY_API_KEY", ""),
        "X-Auth-Token": os.environ.get("ARRIVY_AUTH_TOKEN", ""),
    })
    r.raise_for_status()
    return [{
        "id":     str(e["id"]),
        "name":   (e.get("name") or "").strip(),
        "email":  (e.get("email") or "").strip().lower(),
        "type":   e.get("type") or "",
        "active": bool(e.get("is_active", True)) and not e.get("is_disabled"),
    } for e in r.json()]


def _pipedrive_people():
    """Pipedrive users as [{id, name, email, active}]."""
    r = requests.get(f"{PD_BASE}/users", timeout=20,
                     params={"api_token": os.environ.get("PIPEDRIVE_API_TOKEN", "")})
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError("Pipedrive /users failed")
    return [{
        "id":     str(u["id"]),
        "name":   (u.get("name") or "").strip(),
        "email":  (u.get("email") or "").strip().lower(),
        "active": bool(u.get("active_flag")),
    } for u in data["data"]]


def _m365_people():
    """Active members of the M365 group, from the local copy (refreshed by m365_directory.sync_users)."""
    return [{
        "id":     r["graph_id"],
        "name":   r["display_name"] or r["email"],
        "email":  r["email"],
        "active": True,
    } for r in m365_directory.list_active_users()]


def directory(refresh=False):
    """
    Every person each system knows about, for the mapping dropdowns:
    {m365: [...], arrivy: [...], pipedrive: [...], errors: {system: msg}}.

    Cached for CACHE_SECONDS; refresh=True also re-pulls the M365 group first.
    A system that can't be reached comes back empty with its error, so one
    outage doesn't blank the whole page.
    """
    if not refresh and _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    out, errors = {}, {}
    if refresh:
        try:
            m365_directory.sync_users()
        except Exception as e:
            log.warning(f"M365 group sync failed: {e}")
            errors["m365"] = f"Sync failed: {str(e)[:80]}"
    for key, fn in (("m365", _m365_people), ("arrivy", _arrivy_people), ("pipedrive", _pipedrive_people)):
        try:
            out[key] = sorted(fn(), key=lambda p: (not p["active"], norm_name(p["name"])))
        except Exception as e:
            log.warning(f"{key} directory lookup failed: {e}")
            out[key] = []
            errors[key] = str(e)[:120]
    out["errors"] = errors
    _cache.update(at=time.time(), data=out)
    return out


# ---- Matching ---------------------------------------------------------------

def norm_name(name):
    """'1.  Dave Ackman' / 'dave ackman' → 'dave ackman' (drops Arrivy's sort-order prefixes)."""
    name = re.sub(r"^\s*\d+\s*[.)-]\s*", "", name or "")
    return " ".join(re.sub(r"[^a-z]", " ", name.lower()).split())


def username_for(name, taken):
    """A unique lowercase username ('dave_ackman', 'dave_ackman2') for an imported person."""
    base = (norm_name(name).replace(" ", "_") or "user")[:28]
    cand, n = base, 2
    while cand in taken:
        cand, n = f"{base}{n}", n + 1
    return cand


def match_people(dir_data):
    """
    Group the people in every system into one cluster per human: same email
    first, else same normalized name. Inactive Pipedrive/Arrivy accounts are
    left out. Returns [{name, email, m365_graph_id, arrivy_entity_id, pipedrive_user_id}].
    """
    clusters = []
    by_email, by_name = {}, {}
    field = {"m365": "m365_graph_id", "arrivy": "arrivy_entity_id", "pipedrive": "pipedrive_user_id"}
    # M365 first so its display name wins (Arrivy names carry sort prefixes,
    # and some Pipedrive users are named by their email address).
    for system in ("m365", "pipedrive", "arrivy"):
        for p in dir_data.get(system, []):
            if not p["active"]:
                continue
            nm = norm_name(p["name"]) if "@" not in p["name"] else ""
            c = (by_email.get(p["email"]) if p["email"] else None) or (by_name.get(nm) if nm else None)
            if c is None or c.get(field[system]):
                c = {"name": "", "email": ""}
                clusters.append(c)
            c[field[system]] = p["id"]
            if not c["name"] and nm:
                c["name"] = re.sub(r"^\s*\d+\s*[.)-]\s*", "", p["name"]).strip()
            if not c["email"] and p["email"]:
                c["email"] = p["email"]
            if c["email"]:
                by_email.setdefault(c["email"], c)
            if nm:
                by_name.setdefault(nm, c)
    for c in clusters:
        if not c["name"]:
            c["name"] = c["email"].split("@")[0]
    return clusters
