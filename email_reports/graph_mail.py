"""
Send mail through Microsoft Graph using a single Azure AD app registration
with the Application permission `Mail.Send`, scoped to one shared mailbox by
an Exchange Application Access Policy.

Required env vars (in .env):
    MS_TENANT_ID       Directory (tenant) ID
    MS_CLIENT_ID       Application (client) ID
    MS_CLIENT_SECRET   Client secret value (the long opaque one, not the secret ID)
    MS_SENDER_EMAIL    Mailbox to send from, e.g. reports@creativecarpetinc.com

CLI sanity check (run from project root):
    ./venv/bin/python -m email_reports.graph_mail --to you@example.com --subject "test"
"""
import argparse
import logging
import os
import sys
import time
from typing import Iterable, Sequence

import msal
import requests
from dotenv import load_dotenv

load_dotenv()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]
log = logging.getLogger(__name__)


class GraphMailError(RuntimeError):
    pass


_msal_app: msal.ConfidentialClientApplication | None = None
_token_cache: dict = {"value": None, "expires_at": 0.0}


def _get_app() -> msal.ConfidentialClientApplication:
    """Lazy-build the MSAL app once per process."""
    global _msal_app
    if _msal_app is not None:
        return _msal_app
    tenant = os.environ["MS_TENANT_ID"]
    client_id = os.environ["MS_CLIENT_ID"]
    client_secret = os.environ["MS_CLIENT_SECRET"]
    _msal_app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant}",
    )
    return _msal_app


def get_access_token() -> str:
    """Return a Graph access token, refreshing 60s before expiry."""
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["value"]

    app = _get_app()
    result = app.acquire_token_for_client(scopes=SCOPE)
    if "access_token" not in result:
        raise GraphMailError(
            f"token acquisition failed: {result.get('error')} — "
            f"{result.get('error_description', '')}"
        )
    _token_cache["value"] = result["access_token"]
    _token_cache["expires_at"] = now + int(result.get("expires_in", 3600))
    return _token_cache["value"]


def _as_list(x: str | Iterable[str] | None) -> list[str]:
    if not x:
        return []
    if isinstance(x, str):
        return [x]
    return list(x)


def send_mail(
    to: str | Sequence[str],
    subject: str,
    body_html: str,
    *,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    sender: str | None = None,
    save_to_sent: bool = False,
) -> None:
    """
    POST /users/{sender}/sendMail. Raises GraphMailError on non-2xx.

    sender defaults to MS_SENDER_EMAIL. Only mailboxes covered by the Exchange
    Application Access Policy will succeed; others return 403 ErrorAccessDenied.

    save_to_sent defaults to False per the project's send-and-forget policy:
    the bot does not retain copies of outgoing reports in the reports@ mailbox.
    Pass save_to_sent=True for ad-hoc tests where a record is wanted.
    """
    sender = sender or os.environ["MS_SENDER_EMAIL"]
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients":  [{"emailAddress": {"address": a}} for a in _as_list(to)],
            "ccRecipients":  [{"emailAddress": {"address": a}} for a in _as_list(cc)],
            "bccRecipients": [{"emailAddress": {"address": a}} for a in _as_list(bcc)],
        },
        "saveToSentItems": bool(save_to_sent),
    }
    if not payload["message"]["toRecipients"]:
        raise GraphMailError("send_mail: at least one recipient is required")

    token = get_access_token()
    r = requests.post(
        f"{GRAPH_BASE}/users/{sender}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if r.status_code == 202:
        return
    raise GraphMailError(
        f"sendMail {r.status_code} for sender={sender}: {r.text[:500]}"
    )


def _cli():
    p = argparse.ArgumentParser(description="Send a test mail via Microsoft Graph.")
    p.add_argument("--to", required=True, help="Recipient (comma-separated for many)")
    p.add_argument("--subject", default="CreativeBot Graph mail test")
    p.add_argument("--body", default="This is a test message from CreativeBot.")
    p.add_argument("--sender", default=None, help="Override MS_SENDER_EMAIL")
    args = p.parse_args()

    recipients = [a.strip() for a in args.to.split(",") if a.strip()]
    try:
        send_mail(recipients, args.subject, f"<p>{args.body}</p>", sender=args.sender)
    except GraphMailError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"sent to {recipients} from {args.sender or os.environ.get('MS_SENDER_EMAIL')}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _cli()
