#!/usr/bin/env python3
"""
One-time backfill: link existing Rollmaster customers to their Pipedrive person
by stamping the Rollmaster id into the person's "Customer ID" field.

Everyone from before the Pipedrive → Rollmaster sync exists in both systems but
carries no link, so the sync would create a duplicate the first time they were
edited. This walks every Rollmaster customer, finds the matching person by
phone, email and name, and writes the id where the match is safe:

  strong        phone or email matches AND the name agrees          -> stamped
  dup-recent    several RM records for one person (RM-side dupes)   -> the one
                with the most recent invoice is stamped, all listed for review
  name-blank    exact unique full-name match and RM has no phone    -> stamped
  name-differs  exact unique full-name match but RM phone differs   -> review
  business      the RM name is a company (INC, BUILDERS, HOMES…)    -> review;
                never linked to a person, whatever else matches
  contact-only  phone/email matches but the name does not           -> review
  ambiguous     phone shared by several people, no name tiebreak    -> review
  no-match      nothing in Pipedrive                                -> review

Persons that already carry a Customer ID are never overwritten.

Dry run by default: prints the counts and writes plan/review CSVs. Nothing
reaches Pipedrive without --apply.

    PIPEDRIVE_API_TOKEN=... python3 tools/backfill_customer_ids.py \\
        --customers data/customers.json --persons data/persons.json \\
        --plan plan.csv --review review.csv [--apply]

Without --customers/--persons the lists are pulled live (Rollmaster takes ~2
minutes; Pipedrive ~1 minute).
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PD_BASE   = "https://api.pipedrive.com/v1"
CID_FIELD = os.environ.get("RM_PD_CID_FIELD", "509740a9dad0eab9c7c83842beefb2eda43ae199")
TOKEN     = os.environ.get("PIPEDRIVE_API_TOKEN", "")

NOISE = {"INACTIVE", "LLC", "INC", "THE", "AND", "MR", "MRS", "DR", "CO", "CORP"}

# A Rollmaster account whose name is a company must never be linked to a person:
# the sync would rename the account after the person on their next edit.
# Business accounts need their own convention and are out of scope.
BUSINESS_WORDS = {
    "INC", "LLC", "LTD", "CORP", "CO", "COMPANY", "CORPORATION", "ENTERPRISES", "GROUP",
    "CONSTRUCTION", "BUILDERS", "BUILDING", "CONTRACTORS", "CONTRACTING", "REMODELING",
    "HOMES", "HOME", "DESIGN", "DESIGNS", "INTERIORS", "DEVELOPMENT", "DEVELOPERS",
    "PROPERTIES", "PROPERTY", "MANAGEMENT", "REALTY", "REAL", "ESTATE", "SERVICES",
    "SERVICE", "FLOORING", "FLOORS", "CARPET", "TILE", "CABINETS", "KITCHENS", "BATH",
    "CHURCH", "SCHOOL", "ACADEMY", "HOSPITAL", "CLINIC", "DENTAL", "MEDICAL", "BANK",
    "RESTAURANT", "HOTEL", "APARTMENTS", "CONDO", "CONDOS", "ASSOCIATION", "ASSOC",
    "HOSPITALITY", "CLEANING", "PAINTING", "ROOFING", "PLUMBING", "ELECTRIC", "HVAC",
    "INDUSTRIES", "INDUSTRIAL", "SUPPLY", "SYSTEMS", "SOLUTIONS", "PARTNERS", "HOLDINGS",
    "INVESTMENTS", "VENTURES", "STUDIO", "STUDIOS", "SHOP", "STORE", "MARKET", "CENTER",
    "VILLAGE", "CITY", "COUNTY", "TOWNSHIP", "DISTRICT", "DEPT", "DEPARTMENT", "UNIVERSITY",
    "COLLEGE", "LIBRARY", "PARK", "CLUB", "FOUNDATION", "MINISTRIES", "TRUST", "ARCHITECTS",
    "ARCHITECTURE", "ENGINEERING", "CUSTOM", "RENOVATIONS", "RENOVATION", "CARPENTRY",
    "HANDYMAN", "MAINTENANCE", "INSTALLATION", "INSTALLATIONS", "IMPROVEMENT", "IMPROVEMENTS",
}


def looks_like_business(name):
    """True when a Rollmaster customer name reads as a company rather than a person."""
    words = set(re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()).split())
    return bool(words & BUSINESS_WORDS) or bool(re.search(r"\b(INC|LLC|LTD|CORP)\b\.?", (name or "").upper()))


# ---------------------------------------------------------------------------
# NORMALISATION
# ---------------------------------------------------------------------------
def digits(s):
    """Phone as bare 10 digits (leading US 1 dropped), or '' if not usable."""
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return d if len(d) == 10 else ""


def tokens(s):
    """Upper-case word set of a name minus punctuation and filler words."""
    return set(re.sub(r"[^A-Z ]", "", (s or "").upper().replace("-", " ")).split()) - NOISE


def name_sim(a, b):
    """0..1 similarity of two names: token overlap or sequence ratio, whichever is kinder."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    overlap = len(ta & tb) / max(len(ta), len(tb))
    return max(overlap, SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio())


def rm_date(s):
    """Sortable YYYYMMDD int from a Rollmaster date, which is MMDDYYYY on some fields and YYYYMMDD on others; 0 if blank."""
    s = (s or "").strip()
    if not s.isdigit() or len(s) != 8 or s == "00000000":
        return 0
    if int(s[:4]) >= 1900:                 # already YYYYMMDD
        return int(s)
    return int(s[4:] + s[:2] + s[2:4])     # MMDDYYYY -> YYYYMMDD


# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------
def load_customers(path):
    """Rollmaster customers from a saved /customers JSON, or a live pull."""
    if path:
        rows = json.load(open(path))
    else:
        from dotenv import load_dotenv
        load_dotenv()
        import rollmaster
        rows = rollmaster.get("customers", {"company": rollmaster.COMPANY}, timeout=600)
    return [r for r in rows if str(r.get("C_CID", "")).strip()]


def load_persons(path):
    """Pipedrive persons (compact) from a saved JSON, or a live paged pull."""
    if path:
        return json.load(open(path))
    out, start = [], 0
    while True:
        d = requests.get(f"{PD_BASE}/persons", params={"api_token": TOKEN, "limit": 500, "start": start}, timeout=60).json()
        for p in d.get("data") or []:
            out.append({"id": p["id"], "name": p.get("name") or "",
                        "phones": [x["value"] for x in p.get("phone", []) if x.get("value")],
                        "emails": [x["value"] for x in p.get("email", []) if x.get("value")],
                        "cid": p.get(CID_FIELD) or ""})
        pag = d.get("additional_data", {}).get("pagination", {})
        if not pag.get("more_items_in_collection"):
            return out
        start = pag["next_start"]
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# MATCHING
# ---------------------------------------------------------------------------
def match_all(customers, persons):
    """Classify every Rollmaster customer against the Pipedrive persons; returns a list of result dicts."""
    by_id = {p["id"]: p for p in persons}
    by_phone, by_email, by_token = defaultdict(list), defaultdict(list), defaultdict(list)
    for p in persons:
        for ph in p["phones"]:
            if digits(ph):
                by_phone[digits(ph)].append(p["id"])
        for e in p["emails"]:
            by_email[e.strip().lower()].append(p["id"])
        for t in tokens(p["name"]):
            by_token[t].append(p["id"])

    results = []
    for c in customers:
        cid   = c["C_CID"].strip().upper()
        cname = c["C_NAME"].strip()
        phones = {d for d in (digits(c.get("C_PHONE")), digits(c.get("C_PHONE2"))) if d}
        email  = (c.get("C_EMAIL") or "").strip().lower()

        cands = {}
        for d in phones:
            for pid in by_phone.get(d, []):
                cands.setdefault(pid, set()).add("phone")
        if email:
            for pid in by_email.get(email, []):
                cands.setdefault(pid, set()).add("email")
        scored = [(pid, how, name_sim(cname, by_id[pid]["name"])) for pid, how in cands.items()]

        if not scored:
            tk = tokens(cname)
            if len(tk) >= 2 and all(t in by_token for t in tk):
                pool = set.intersection(*[set(by_token[t]) for t in tk])
                scored = [(pid, {"name"}, 1.0) for pid in pool if tokens(by_id[pid]["name"]) == tk]

        if looks_like_business(cname):
            cls = "business"
        elif not scored:
            cls = "no-match"
        else:
            strong = [s for s in scored if s[1] & {"phone", "email"} and s[2] >= 0.6]
            if len(strong) == 1:
                cls = "strong"
            elif len(strong) > 1:
                cls = "ambiguous"
            elif len(scored) == 1 and scored[0][1] & {"phone", "email"}:
                cls = "contact-only"
            elif len(scored) == 1 and scored[0][1] == {"name"}:
                cls = "name-blank" if not phones and not email else "name-differs"
            else:
                cls = "ambiguous"

        best = max(scored, key=lambda s: (len(s[1] & {"phone", "email"}), s[2])) if scored else None
        results.append({
            "cid": cid, "rm_name": cname, "rm_phone": (c.get("C_PHONE") or "").strip(),
            "rm_email": (c.get("C_EMAIL") or "").strip(),
            "rm_last_inv": rm_date(c.get("C_DTLSTINV")), "rm_last_edit": rm_date(c.get("C_LAST_EDIT_DATE")),
            "inactive": cname.upper().startswith("INACTIVE"),
            "class": cls, "n_cands": len(scored),
            "pid": best[0] if best else None,
            "pd_name": by_id[best[0]]["name"] if best else "",
            "pd_cid": by_id[best[0]]["cid"] if best else "",
            "how": "+".join(sorted(best[1])) if best else "",
            "sim": round(best[2], 2) if best else "",
            "candidates": "; ".join(f"{by_id[s[0]]['name']} (#{s[0]})" for s in scored[:5]),
        })
    return results


def plan_writes(results):
    """
    Decide which persons get stamped with which id. Returns (writes, review):
    writes = [(person_id, cid, reason)], review = result rows needing a human.
    """
    stampable = defaultdict(list)          # pid -> rows that could stamp it
    review = []
    for r in results:
        if r["class"] in ("strong", "name-blank"):
            stampable[r["pid"]].append(r)
        else:
            review.append(r)

    writes = []
    for pid, rows in stampable.items():
        if rows[0]["pd_cid"]:
            for r in rows:
                review.append({**r, "class": "already-linked"})
            continue
        if len(rows) == 1:
            r = rows[0]
            writes.append((pid, r["cid"], r["class"]))
            continue
        # RM-side duplicates: the most recently invoiced active record wins;
        # every record in the group goes on the review list for a later merge.
        rows.sort(key=lambda r: (not r["inactive"], r["rm_last_inv"], r["rm_last_edit"]), reverse=True)
        writes.append((pid, rows[0]["cid"], "dup-recent"))
        for r in rows:
            review.append({**r, "class": "dup-group", "candidates": ", ".join(x["cid"] for x in rows),
                           "chosen": rows[0]["cid"]})
    return writes, review


# ---------------------------------------------------------------------------
# WRITING
# ---------------------------------------------------------------------------
def stamp(person_id, cid, session):
    """PUT the Customer ID onto one person, backing off on 429; returns True on success."""
    for attempt in range(6):
        r = session.put(f"{PD_BASE}/persons/{person_id}", params={"api_token": TOKEN},
                        json={CID_FIELD: cid}, timeout=30)
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        if r.ok and r.json().get("success"):
            return True
        print(f"  FAILED person {person_id} <- {cid}: {r.status_code} {r.text[:120]}", flush=True)
        return False
    print(f"  FAILED person {person_id} <- {cid}: rate limited", flush=True)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customers", help="saved Rollmaster /customers JSON (else pulled live)")
    ap.add_argument("--persons", help="saved Pipedrive persons JSON (else pulled live)")
    ap.add_argument("--plan", help="CSV of every write that would be / was made")
    ap.add_argument("--review", help="CSV of customers needing a human decision")
    ap.add_argument("--apply", action="store_true", help="actually write Customer IDs to Pipedrive")
    args = ap.parse_args()

    if args.apply and not TOKEN:
        sys.exit("PIPEDRIVE_API_TOKEN is not set")

    customers = load_customers(args.customers)
    persons   = load_persons(args.persons)
    print(f"{len(customers)} Rollmaster customers, {len(persons)} Pipedrive persons")

    results = match_all(customers, persons)
    print("match classes:", dict(Counter(r["class"] for r in results)))
    writes, review = plan_writes(results)
    print(f"planned writes: {len(writes)}  ({dict(Counter(w[2] for w in writes))})")
    print(f"review items:   {len(review)}  ({dict(Counter(r['class'] for r in review))})")

    if args.plan:
        with open(args.plan, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["person_id", "customer_id", "reason"]); w.writerows(writes)
    if args.review:
        cols = ["class", "cid", "rm_name", "rm_phone", "rm_email", "inactive", "pd_name", "pid", "how", "sim",
                "n_cands", "candidates", "chosen", "pd_cid"]
        with open(args.review, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader()
            w.writerows(sorted(review, key=lambda r: (r["class"], r["rm_name"])))

    if not args.apply:
        print("dry run — nothing written (add --apply)")
        return

    session = requests.Session()
    ok = fail = 0
    t0 = time.time()
    for i, (pid, cid, reason) in enumerate(writes, 1):
        if stamp(pid, cid, session):
            ok += 1
        else:
            fail += 1
        if i % 500 == 0:
            print(f"  {i}/{len(writes)}  ok={ok} fail={fail}  {time.time()-t0:.0f}s", flush=True)
        time.sleep(0.15)                    # ~6/s, well inside Pipedrive's limit
    print(f"done: {ok} stamped, {fail} failed in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
