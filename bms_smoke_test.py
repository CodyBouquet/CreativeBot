"""
BMS (Rollmaster) API smoke test.

Shape per the live BMS spec (developer.broadlume.com/bms):
  Token endpoint:
    POST https://api.rmaster.com/api/{alias}/token
    Header: x-api-key
    Body:   multipart/form-data with username, password, granttype
            (username/password <= 8 chars; granttype in {client, application})
  Authenticated endpoints:
    Header: x-api-key AND token: <session token from /token response>

Run:  ./venv/bin/python bms_smoke_test.py
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.rmaster.com/api"
ALIAS = os.environ.get("BMS_ALIAS", "creativecarpets")
TOKEN_URL = f"{BASE_URL}/{ALIAS}/token"
INVENTORY_URL = f"{BASE_URL}/{ALIAS}/lowstock"
COMPANY = os.environ.get("BMS_COMPANY", "99")
GRANT_TYPE = os.environ.get("BMS_GRANT_TYPE", "application")  # "client" or "application"

API_KEY = os.environ.get("BMS_API_KEY")
USERNAME = os.environ.get("BMS_USERNAME")
PASSWORD = os.environ.get("BMS_PASSWORD")

if not (API_KEY and USERNAME and PASSWORD):
    print("ERROR: BMS_API_KEY, BMS_USERNAME, BMS_PASSWORD must be set in .env")
    sys.exit(1)


def pretty(obj):
    try:
        return json.dumps(obj, indent=2, default=str)
    except Exception:
        return repr(obj)


def show_response(r):
    print(f"      status: {r.status_code}")
    try:
        print(f"      body: {pretty(r.json())}")
    except ValueError:
        print(f"      body (text): {r.text[:800]}")


def extract_token(data):
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        wanted = {"token", "session_token", "sessiontoken", "access_token"}
        for k, v in data.items():
            if isinstance(k, str) and k.lower() in wanted and isinstance(v, str):
                return v
        for v in data.values():
            if isinstance(v, dict):
                t = extract_token(v)
                if t:
                    return t
    return None


def get_token():
    print(f"[1/2] POST {TOKEN_URL}")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "x-api-key": API_KEY,
    }
    body = {"username": USERNAME, "password": PASSWORD, "granttype": GRANT_TYPE}
    r = requests.post(TOKEN_URL, headers=headers, data=body, timeout=30)
    show_response(r)
    if not r.ok:
        return None
    try:
        tok = extract_token(r.json())
    except ValueError:
        tok = r.text.strip().strip('"') or None
    if not tok:
        print("      WARN: 2xx but no token field found")
    return tok


def get_inventory(token):
    print(f"\n[2/2] GET {INVENTORY_URL}?company={COMPANY}")
    headers = {
        "Accept": "application/json",
        "x-api-key": API_KEY,
        "token": token,
    }
    r = requests.get(INVENTORY_URL, headers=headers, params={"company": COMPANY}, timeout=60)
    show_response(r)


def main():
    token = get_token()
    if not token:
        print("\nERROR: could not obtain token")
        sys.exit(2)
    print(f"\n      got token: {str(token)[:32]}...")
    get_inventory(token)


if __name__ == "__main__":
    main()
