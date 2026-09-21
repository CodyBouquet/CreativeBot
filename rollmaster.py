"""
Rollmaster (Broadlume BMS) write client — currently customer creation only.

The inventory report talks to the same API but only ever reads, and it lives in
the email_reports package behind a tqdm/report dependency chain. This module is
the write side: small, importable from the Flask app, no report machinery.

Two things about this API drive the shape of everything below:

  * It reports failures as HTTP 200 with an ERRORMSG in the body. /customer is
    the exception that also uses 400 ("IMPROPER DATA"), so both have to be
    checked — see api_error(). Trusting the status code alone would make a
    rejected create look like a success.
  * There is no "next customer id" call. C_CID is a human-style code the caller
    invents (first 3 of last name + first 3 of first name, e.g. John Smith →
    SMIJOH), with a numeric suffix on collision. Checking a candidate means
    holding the existing id list locally — see load_known_cids().
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.rmaster.com/api"
ALIAS    = os.environ.get("BMS_ALIAS",   "creativecarpets")
COMPANY  = os.environ.get("BMS_COMPANY", "99")
API_KEY  = os.environ.get("BMS_API_KEY", "")
USERNAME = os.environ.get("BMS_USERNAME", "")
PASSWORD = os.environ.get("BMS_PASSWORD", "")

# Cache of every C_CID already in Rollmaster. /customers ignores pagelimit and
# returns all ~21k customers as a ~5MB response that can take minutes, so it is
# nowhere near fast enough to call per webhook. We hold the id set on disk and
# refresh it in the background; newly minted ids are added as we create them.
CID_CACHE          = os.environ.get(
    "RM_CID_CACHE",
    str(Path(__file__).resolve().parent / "data" / ".rm_cid_cache.json"),
)
CID_CACHE_MAX_AGE_HOURS = 12

_token_lock = threading.Lock()
_token      = None
_token_at   = 0.0
TOKEN_TTL_SECONDS = 30 * 60

_cid_lock       = threading.Lock()
_refresh_lock   = threading.Lock()
_refresh_running = False


class RollmasterError(Exception):
    """Raised when Rollmaster rejects a call (including a 200 carrying an ERRORMSG)."""


# ---------------------------------------------------------------------------
# TRANSPORT
# ---------------------------------------------------------------------------
def _fetch_token():
    """Exchange BMS credentials for an API token; raises RollmasterError if none comes back."""
    r = requests.post(
        f"{BASE_URL}/{ALIAS}/token",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "x-api-key": API_KEY,
        },
        data={"username": USERNAME, "password": PASSWORD, "granttype": "application"},
        timeout=30,
    )
    r.raise_for_status()
    token = r.json().get("TOKEN")
    if not token:
        raise RollmasterError(f"no TOKEN in /token response: {r.json()}")
    return token


def get_token(force=False):
    """Return a cached API token, re-authenticating when it's missing, expired, or force=True."""
    global _token, _token_at
    with _token_lock:
        if force or not _token or (time.time() - _token_at) > TOKEN_TTL_SECONDS:
            _token    = _fetch_token()
            _token_at = time.time()
            logger.info("Rollmaster: authenticated")
        return _token


def _headers(token):
    """Auth headers for a BMS call."""
    return {"Accept": "application/json", "x-api-key": API_KEY, "token": token}


def api_error(data):
    """
    Return the error message carried by a BMS response body, or None if it looks clean.

    The API answers a rejected write with HTTP 200 and an ERRORMSG/ERRORSTRING
    field (e.g. " NOT ENOUGH DATA", " REQUIRED FIELDS NOT FILLED."), so callers
    must inspect the body rather than the status code. Messages come back padded
    with a lot of trailing whitespace; they're stripped here.
    """
    recs = data if isinstance(data, list) else [data]
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        for key in ("ERRORMSG", "ERRORSTRING", "ERROR"):
            msg = str(rec.get(key) or "").strip()
            if msg:
                return msg
    return None


def get(path, params, timeout=300):
    """GET a BMS endpoint and return parsed JSON; raises RollmasterError on HTTP or in-body errors."""
    r = requests.get(f"{BASE_URL}/{ALIAS}/{path}",
                     headers=_headers(get_token()), params=params, timeout=timeout)
    if r.status_code != 200:
        raise RollmasterError(f"GET /{path} -> HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    err = api_error(data)
    if err:
        raise RollmasterError(f"GET /{path} -> {err}")
    return data


def post_form(path, fields, timeout=120):
    """
    POST multipart/form-data to a BMS endpoint (the shape /customer expects) and
    return parsed JSON.

    Every value is sent as a form part, empty strings included — the documented
    call passes all fields whether or not they carry a value. Raises
    RollmasterError on an HTTP error or an ERRORMSG in an otherwise-200 body.
    """
    files = {k: (None, "" if v is None else str(v)) for k, v in fields.items()}
    r = requests.post(f"{BASE_URL}/{ALIAS}/{path}",
                      headers=_headers(get_token()), files=files, timeout=timeout)
    body = r.text[:400]
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code != 200:
        raise RollmasterError(f"POST /{path} -> HTTP {r.status_code}: {body}")
    if data is None:
        raise RollmasterError(f"POST /{path} -> non-JSON response: {body}")
    err = api_error(data)
    if err:
        raise RollmasterError(f"POST /{path} -> {err}")
    return data


# ---------------------------------------------------------------------------
# CUSTOMER IDs
# ---------------------------------------------------------------------------
def split_name(full_name):
    """
    Split a display name into (first, last).

    Pipedrive sends one person name, so the first token is the first name and the
    last token is the surname; anything between (middle names, initials) is
    dropped, which is what the CID rule wants. A single-token name — usually a
    business — comes back as (token, "") and cid_base falls back to using it
    alone.
    """
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def _letters(s):
    """Uppercase a name fragment and drop everything that isn't a letter (apostrophes, hyphens, periods)."""
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def cid_base(first, last):
    """
    Build the un-suffixed customer id: first 3 letters of the surname followed by
    the first 3 of the given name (John Smith → SMIJOH).

    Short names simply yield a shorter base — "Al Ng" → NGAL — which matches the
    5- and 4-character ids already in Rollmaster. A name with no surname (a
    business, or a single-word person) uses the first 6 letters of what it has,
    the convention the existing 8-character business ids loosely follow.
    """
    f, l = _letters(first), _letters(last)
    if not l:
        return f[:6]
    if not f:
        return l[:6]
    return (l[:3] + f[:3])


def next_free_cid(base, taken):
    """
    Return the next id for a base: the bare base if unused, otherwise the
    HIGHEST existing suffix + 1 (SMIJOH, SMIJOH1 → SMIJOH2, SMIJOH4 → SMIJOH5).

    Gaps in the numbering are deliberately not reused: a missing SMIKAT2 between
    SMIKAT1 and SMIKAT3 is where a deleted or inactive record lives, which the
    /customers list doesn't show and a create would silently overwrite.
    `taken` is the set of ids already in use.
    """
    base = base.strip().upper()
    if not base:
        raise RollmasterError("cannot build a customer id from an empty name")
    if base not in taken:
        return base
    highest = 0
    for cid in taken:
        if cid.startswith(base) and cid[len(base):].isdigit():
            highest = max(highest, int(cid[len(base):]))
    return f"{base}{highest + 1}"


# Contact details kept per customer in the cache, so a Pipedrive person can be
# matched to an existing Rollmaster account without a live API call.
_CACHE_COLS = ("C_CID", "C_NAME", "C_PHONE", "C_PHONE2", "C_EMAIL")


def _read_cache():
    """Return (cids, customers, age_hours) from the on-disk cache, or (None, None, None) if missing/unreadable."""
    try:
        with open(CID_CACHE) as f:
            data = json.load(f)
        cids = set(data.get("cids") or [])
        if not cids:
            return None, None, None
        stamped = datetime.fromisoformat(data["fetched_at"])
        age = (datetime.now(timezone.utc) - stamped).total_seconds() / 3600.0
        return cids, data.get("customers") or [], age
    except Exception:
        return None, None, None


def _read_cid_cache():
    """Return (cids, age_hours) from the on-disk cache, or (None, None) if it's missing/unreadable."""
    cids, _, age = _read_cache()
    return cids, age


def _write_cid_cache(cids, customers=None):
    """
    Persist the customer-id set (and, when given, the compact customer records)
    to disk with a fetch timestamp. Called without records it keeps whatever
    records are already on disk.
    """
    if customers is None:
        _, customers, _ = _read_cache()
    Path(CID_CACHE).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{CID_CACHE}.tmp"
    with open(tmp, "w") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(),
                   "cids": sorted(cids), "customers": customers or []}, f)
    os.replace(tmp, CID_CACHE)   # atomic, so a crash mid-write can't leave a half file


def _compact(row):
    """One customer as a short list in _CACHE_COLS order, values stripped."""
    return [str(row.get(k) or "").strip() for k in _CACHE_COLS]


def refresh_known_cids():
    """Pull every customer from /customers and replace the disk cache; returns the id set."""
    t0 = time.time()
    rows = get("customers", {"company": COMPANY}, timeout=600)
    customers = [_compact(r) for r in rows if str(r.get("C_CID", "")).strip()]
    cids = {c[0].upper() for c in customers}
    _write_cid_cache(cids, customers)
    logger.info(f"Rollmaster: cached {len(cids)} customer ids in {time.time()-t0:.1f}s")
    return cids


def _refresh_cids_async():
    """Kick off a background cache refresh unless one is already running."""
    global _refresh_running
    with _refresh_lock:
        if _refresh_running:
            return
        _refresh_running = True

    def _run():
        """Refresh the id cache off-thread, always clearing the in-flight flag."""
        global _refresh_running
        try:
            refresh_known_cids()
        except Exception:
            logger.exception("Rollmaster: background cid refresh failed")
        finally:
            with _refresh_lock:
                _refresh_running = False

    threading.Thread(target=_run, daemon=True).start()


def load_known_cids(max_age_hours=CID_CACHE_MAX_AGE_HOURS):
    """
    Return the set of customer ids already in Rollmaster.

    A stale cache is served immediately and refreshed in the background: the pull
    is ~5MB and can take minutes, which would blow past the webhook timeout on the
    Pipedrive side. The only cost of a slightly old list is that a very recently
    added id isn't known yet, and the suffix retry in create_customer covers that.

    Only the very first call — with no cache on disk at all — blocks on the pull.
    warm_cid_cache() at startup keeps that out of the request path.
    """
    cids, age = _read_cid_cache()
    if cids is not None:
        if age is None or age > max_age_hours:
            _refresh_cids_async()
        return cids
    with _cid_lock:
        # Re-read under the lock: a concurrent caller may have populated it while
        # we waited, and the pull is far too expensive to run twice.
        cids, _ = _read_cid_cache()
        if cids is not None:
            return cids
        return refresh_known_cids()


def warm_cid_cache():
    """Populate the customer-id cache in the background at startup so no webhook has to wait for it."""
    _refresh_cids_async()


def remember_cid(cid, fields=None):
    """
    Add a freshly created customer to the disk cache so back-to-back creates
    don't collide and a second webhook for the same person matches it.
    """
    with _cid_lock:
        cids, customers, _ = _read_cache()
        if cids is None:
            return
        cid = cid.strip().upper()
        cids.add(cid)
        if fields is not None:
            customers = [c for c in customers if c[0].upper() != cid]
            customers.append(_compact({**fields, "C_CID": cid}))
        _write_cid_cache(cids, customers)


# ---------------------------------------------------------------------------
# MATCHING A PERSON TO AN EXISTING CUSTOMER
# ---------------------------------------------------------------------------
class AmbiguousMatch(RollmasterError):
    """More than one existing customer fits the person and nothing tells them apart."""
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__("matches several customers: " + ", ".join(f"{c[0]} ({c[1]})" for c in candidates))


def _digits(s):
    """Phone as bare digits, minus a leading US 1; '' when not a usable 10-digit number."""
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return d if len(d) == 10 else ""


def find_existing_customer(name, phones=(), email=""):
    """
    Find the Rollmaster customer a Pipedrive person already is, from the cached
    customer list. Returns (cid, how) or None; raises AmbiguousMatch when several
    fit and the surname can't settle it.

    Phone (either number on the record) or email must match exactly — a name
    alone is never enough, since the same name recurs many times. When several
    customers share a phone (families, landlords) the one whose name carries
    the person's surname wins.
    """
    _, customers, _ = _read_cache()
    if not customers:
        return None
    want_phones = {p for p in (_digits(x) for x in phones) if p}
    want_email  = (email or "").strip().lower()
    hits = {}
    for c in customers:
        cid, cname, p1, p2, em = c[0].upper(), c[1], c[2], c[3], c[4]
        if want_phones and ( _digits(p1) in want_phones or _digits(p2) in want_phones):
            hits.setdefault(cid, (c, "phone"))
        elif want_email and em.strip().lower() == want_email:
            hits.setdefault(cid, (c, "email"))
    if not hits:
        return None
    if len(hits) == 1:
        (c, how), = hits.values()
        return c[0].upper(), how
    # Rollmaster flags dead accounts by prefixing the name with INACTIVE; when a
    # live record also fits, it's the one to keep updating.
    active = {cid: v for cid, v in hits.items() if not v[0][1].upper().startswith("INACTIVE")}
    if active:
        hits = active
        if len(hits) == 1:
            (c, how), = hits.values()
            return c[0].upper(), how + "+active"
    surname = _letters(split_name(name)[1]) if name else ""
    if surname:
        narrowed = {cid: v for cid, v in hits.items()
                    if surname in {_letters(t) for t in re.split(r"[\s,]+", v[0][1])}}
        if len(narrowed) == 1:
            (c, how), = narrowed.values()
            return c[0].upper(), how + "+surname"
        if narrowed:
            hits = narrowed
    raise AmbiguousMatch([v[0] for v in hits.values()])


# ---------------------------------------------------------------------------
# CUSTOMER CREATE
# ---------------------------------------------------------------------------
# Every field /customer accepts, in the documented order. Anything the caller
# doesn't supply is sent as an empty string, matching the documented call.
CUSTOMER_FIELDS = (
    "COMPANY", "C_CID", "C_WHSE", "C_NAME", "C_ADDR1", "C_ADDR2", "C_ZIPCD",
    "C_PHONE", "C_TERRFLAG", "C_TERR", "C_STAT", "C_CONTACT", "C_PHONE2",
    "C_FAX", "C_TAXID", "C_SLSID", "C_EMAIL", "C_PRICE_LEVEL_DEFAULT",
    "C_CUSTTYPE", "C_PROMPMGMT_CO", "C_NAME_LONG", "C_CITY", "C_STATE",
)


def build_customer_form(fields):
    """Return the full form dict for /customer — every documented field, blank where unset, COMPANY defaulted."""
    form = {k: "" for k in CUSTOMER_FIELDS}
    form["COMPANY"] = COMPANY
    for k, v in (fields or {}).items():
        if k in form:
            form[k] = "" if v is None else str(v).strip()
    return form


def create_customer(fields, cid=None, name=None, retries=3):
    """
    Create a customer in Rollmaster and return (cid, response).

    Pass an explicit `cid` to use it verbatim, or a `name` to have one minted from
    the known-id list. Because that list can be slightly stale — and because two
    webhooks can race — a rejection that looks like a duplicate id is retried with
    the next suffix (SMIJOH → SMIJOH1), up to `retries` times.

    Raises RollmasterError on any other rejection, including the HTTP-200-with-
    ERRORMSG responses this API returns for validation failures.
    """
    if not cid:
        if not name:
            raise RollmasterError("create_customer needs either cid or name")
        first, last = split_name(name)
        cid = next_free_cid(cid_base(first, last), load_known_cids())

    attempt_cid = cid
    for attempt in range(retries):
        form = build_customer_form({**fields, "C_CID": attempt_cid})
        try:
            resp = post_form("customer", form)
        except RollmasterError as e:
            # Only an id collision is worth retrying; everything else (missing
            # required field, bad value) would fail identically on the next try.
            if attempt < retries - 1 and _looks_like_duplicate(str(e)):
                taken = load_known_cids() | {attempt_cid}
                base  = re.sub(r"\d+$", "", attempt_cid)
                attempt_cid = next_free_cid(base, taken)
                logger.warning(f"Rollmaster: id in use, retrying as {attempt_cid}")
                continue
            raise
        remember_cid(attempt_cid, fields)
        logger.info(f"Rollmaster: created customer {attempt_cid}")
        return attempt_cid, resp
    raise RollmasterError(f"could not create customer after {retries} attempts")


# Fields a Pipedrive-driven update may change. Everything else on the record
# (id, warehouse, salesperson, job type, status, terms…) is account setup that
# lives in Rollmaster and must survive a contact-detail change in the CRM.
UPDATABLE_FIELDS = (
    "C_NAME", "C_NAME_LONG", "C_CONTACT", "C_ADDR1", "C_ADDR2", "C_CITY",
    "C_STATE", "C_ZIPCD", "C_PHONE", "C_PHONE2", "C_FAX", "C_EMAIL",
)


def update_customer(cid, fields, timeout=120):
    """
    Update contact details on an existing Rollmaster customer; returns the response.

    /customer only accepts an update as PATCH with the fields in the QUERY STRING —
    a form or JSON body is answered with "IMPROPER DATA". It is a partial update:
    fields left out are untouched (verified live), so only UPDATABLE_FIELDS are
    sent and C_CID is never changed. Raises RollmasterError when the record does
    not exist ("CUSTOMER RECORD X DOES NOT EXIST") or the API rejects the call.
    """
    cid = (cid or "").strip().upper()
    if not cid:
        raise RollmasterError("update_customer needs a cid")
    params = {"COMPANY": COMPANY, "C_CID": cid}
    for k in UPDATABLE_FIELDS:
        if k in fields:
            params[k] = "" if fields[k] is None else str(fields[k]).strip()
    r = requests.patch(f"{BASE_URL}/{ALIAS}/customer",
                       headers=_headers(get_token()), params=params, timeout=timeout)
    body = r.text[:400]
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code != 200:
        raise RollmasterError(f"PATCH /customer -> HTTP {r.status_code}: {body}")
    if data is None:
        raise RollmasterError(f"PATCH /customer -> non-JSON response: {body}")
    err = api_error(data)
    if err:
        raise RollmasterError(f"PATCH /customer -> {err}")
    logger.info(f"Rollmaster: updated customer {cid}")
    return data


def update_params(cid, fields):
    """The exact query parameters update_customer would send — for dry-run previews."""
    params = {"COMPANY": COMPANY, "C_CID": (cid or "").strip().upper()}
    for k in UPDATABLE_FIELDS:
        if k in fields:
            params[k] = "" if fields[k] is None else str(fields[k]).strip()
    return params


def _looks_like_duplicate(message):
    """True when a rejection message reads like the customer id is already taken."""
    m = message.upper()
    return any(t in m for t in ("DUPLICATE", "ALREADY EXIST", "ALREADY EXISTS",
                                "IN USE", "EXISTS"))
