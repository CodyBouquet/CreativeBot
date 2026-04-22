"""
BMS (Rollmaster) API smoke test.

Goals:
  1. Confirm the tenant-scoped token endpoint accepts our credentials
     and returns a session token.
  2. Hit /get-catalog-items-below-safety-stock-limit and dump the raw
     response so we know the field names.

Run:  ./venv/bin/python bms_smoke_test.py
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.rmaster.com/api"
TOKEN_URL = f"{BASE_URL}/creativecarpets/token"
INVENTORY_URL = f"{BASE_URL}/get-catalog-items-below-safety-stock-limit"

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


def get_token():
    """Try several header shapes — server returned 'Invalid Legacy API key'
    when we used x-api-key. CORS allow-headers list hints at Blm-Api-Key
    and Blm-Secret-Key."""
    print(f"[1/2] POST {TOKEN_URL}")
    body = {"username": USERNAME, "password": PASSWORD}
    candidates = [
        {"Blm-Api-Key": API_KEY},
        {"Blm-Secret-Key": API_KEY},
        {"Blm-Api-Key": API_KEY, "Blm-Secret-Key": API_KEY},
        {"x-api-key": API_KEY},  # original
    ]
    for extra in candidates:
        h = {"Content-Type": "application/json", **extra}
        print(f"      trying headers: {list(extra.keys())}")
        r = requests.post(TOKEN_URL, headers=h, json=body, timeout=30)
        print(f"      status: {r.status_code}")
        try:
            data = r.json()
            print(f"      body: {pretty(data)}")
        except ValueError:
            print(f"      body (text): {r.text[:800]}")
            continue

        if r.status_code >= 400:
            continue

        for key in ("token", "session_token", "sessionToken", "access_token", "Token"):
            if isinstance(data, dict) and key in data:
                return data[key]
        if isinstance(data, str):
            return data
        print("      WARN: 200 but could not locate token field — full body above")
        return None
    return None


def get_inventory(token):
    print(f"\n[2/2] GET {INVENTORY_URL}")
    base_headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}
    candidates = [
        {"Authorization": f"Bearer {token}"},
        {"session-token": token},
        {"x-session-token": token},
        {"token": token},
    ]
    for extra in candidates:
        h = {**base_headers, **extra}
        first = list(extra.keys())[0]
        print(f"      trying header: {first}")
        r = requests.get(INVENTORY_URL, headers=h, timeout=60)
        print(f"      status: {r.status_code}")
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                print(f"      body (text): {r.text[:2000]}")
                return
            if isinstance(data, list):
                print(f"      list of {len(data)} items, first item:")
                print(pretty(data[0] if data else {}))
            elif isinstance(data, dict):
                print(f"      keys: {list(data.keys())}")
                print(pretty(data)[:3000])
            else:
                print(pretty(data)[:2000])
            return
        else:
            print(f"      body: {r.text[:300]}")
    print("      ERROR: no auth header shape worked")


def main():
    token = get_token()
    if not token:
        sys.exit(2)
    print(f"\n      got token: {str(token)[:32]}...")
    get_inventory(token)


if __name__ == "__main__":
    main()
