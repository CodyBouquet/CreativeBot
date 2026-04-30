"""
Probe: given a job/order number, fetch the order, the customer, and any
balance-due info from BMS. Prints raw responses so we can see the field shape
before committing to a parser.

Usage:
    ./venv/bin/python lookup_job.py <ordno>
"""
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.rmaster.com/api"
ALIAS = os.environ.get("BMS_ALIAS", "creativecarpets")
COMPANY = os.environ.get("BMS_COMPANY", "99")
API_KEY = os.environ["BMS_API_KEY"]
USERNAME = os.environ["BMS_USERNAME"]
PASSWORD = os.environ["BMS_PASSWORD"]


def authenticate():
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
    return r.json()["TOKEN"]


def get(session, path, params):
    r = session.get(f"{BASE_URL}/{ALIAS}/{path}", params=params, timeout=60)
    return r


def show(label, r):
    print(f"\n=== {label}  ({r.request.url}) ===")
    print(f"status: {r.status_code}")
    try:
        body = r.json()
        snippet = body if not isinstance(body, list) else body[:3]
        print(json.dumps(snippet, indent=2, default=str)[:4000])
        if isinstance(body, list):
            print(f"... ({len(body)} records total)")
    except ValueError:
        print(r.text[:800])


def main():
    if len(sys.argv) < 2:
        print("usage: lookup_job.py <ordno>")
        sys.exit(2)
    ordno = sys.argv[1].strip()

    token = authenticate()
    S = requests.Session()
    S.headers.update({"Accept": "application/json", "x-api-key": API_KEY, "token": token})

    # 1. Try a few endpoint/param shapes. /orders requires startdate/enddate.
    DATES = {"startdate": "20200101", "enddate": "21000101"}
    order = None
    attempts = [
        ("order",  {"company": COMPANY, "ordno": ordno}),
        ("order",  {"company": COMPANY, "ordno": ordno, **DATES}),
        ("orders", {"company": COMPANY, "ordno": ordno, **DATES}),
        ("orders", {"company": COMPANY, "orderno": ordno, **DATES}),
        ("orders", {"company": COMPANY, "DMO_ORDNO": ordno, **DATES}),
    ]
    for path, params in attempts:
        r = get(S, path, params)
        show(f"{path} {params}", r)
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except ValueError:
            continue
        if isinstance(data, dict) and data and "ERROR" not in data:
            order = data
            break
        if isinstance(data, list) and data:
            # Filter to the exact ordno in case the endpoint ignored our filter.
            match = [x for x in data if str(x.get("DMO_ORDNO", "")).strip() == ordno]
            if match:
                order = match[0]
                break

    if order is None:
        print("\n!! could not pull order with any tried shape; aborting")
        sys.exit(1)

    print("\n--- order keys ---")
    print(sorted(order.keys()))

    # 2. Pull lines for that order to see the totals/customer linkage.
    r = get(S, "orderline", {"company": COMPANY, "ordno": ordno})
    show("orderline?ordno=", r)

    # 3. Customer lookup — id lives in DMH_CUSTID on the order.
    cust_id = str(order.get("DMH_CUSTID") or "").strip()
    if cust_id:
        print(f"\nfound customer id = {cust_id}")
        for path, key in [
            ("customer", "custid"),
            ("customer", "custno"),
            ("customers", "custid"),
            ("customerinfo", "custid"),
        ]:
            r = get(S, path, {"company": COMPANY, key: cust_id})
            show(f"{path}?{key}={cust_id}", r)
            if r.status_code == 200:
                break

    # 4. Balance / AR — try common spellings.
    DATES = {"startdate": "20200101", "enddate": "21000101"}
    ar_attempts = [
        ("aging",            {"company": COMPANY, "custid": cust_id, **DATES}),
        ("aging",            {"company": COMPANY, "custno": cust_id, **DATES}),
        ("invoiceaging",     {"company": COMPANY, "custid": cust_id, **DATES}),
        ("aragingsummary",   {"company": COMPANY, "custid": cust_id}),
        ("aropen",           {"company": COMPANY, "custid": cust_id}),
        ("aropenitems",      {"company": COMPANY, "custid": cust_id}),
        ("arquicklist",      {"company": COMPANY}),
        ("arquicklist",      {"company": COMPANY, "custid": cust_id}),
        ("aragingquicklist", {"company": COMPANY}),
        ("invoice",          {"company": COMPANY, "ordno": ordno, **DATES}),
        ("invoice",          {"company": COMPANY, "branch": order.get("DMO_WHSE", "1"), "ordno": ordno, **DATES}),
        ("invoice",          {"company": COMPANY, "branch": order.get("DMO_WHSE", "1"), "custid": cust_id, **DATES}),
    ]
    for path, params in ar_attempts:
        if "custid" in params and not cust_id:
            continue
        r = get(S, path, params)
        show(f"{path} {params}", r)


if __name__ == "__main__":
    main()
