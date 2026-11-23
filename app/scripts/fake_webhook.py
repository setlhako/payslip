"""Send a correctly SIGNED fake WhatsApp status event to your local server, to try the webhook without Meta.

    python -m app.scripts.fake_webhook                         newest sent payslip -> "delivered", then "read"
    python -m app.scripts.fake_webhook --status read           one event
    python -m app.scripts.fake_webhook --status failed --error-code 131026
    python -m app.scripts.fake_webhook --message-id wamid.XYZ  a specific message
    python -m app.scripts.fake_webhook --bad-signature         should be REJECTED with 403 (nothing changes)
    python -m app.scripts.fake_webhook --verify                the GET handshake Meta does when you set the webhook URL
    python -m app.scripts.fake_webhook --url http://127.0.0.1:8000/webhook/whatsapp

The signature is HMAC-SHA256 of the exact body using WHATSAPP_APP_SECRET from your .env (the secret is never printed).
Payslips are picked from the database by message id only; no names or phone numbers are printed.
"""
import argparse
import hmac
import json
import sys
import time
from hashlib import sha256

import httpx

from app.config import get_settings

DEFAULT_URL = "http://127.0.0.1:8000/webhook/whatsapp"


def build_payload(message_id: str, status: str, error_code: int | None = None, when: int | None = None) -> bytes:
    """A status webhook shaped like Meta's (checked against its docs): object / entry / changes / value / statuses."""
    entry_status = {
        "id": message_id, "status": status, "timestamp": str(when or int(time.time())), "recipient_id": "26771234567",
    }
    if status == "failed":
        entry_status["errors"] = [{
            "code": error_code or 131026, "title": "Message undeliverable", "message": "Message undeliverable",
            "error_data": {"details": "Fake event made by fake_webhook.py"}, "href": "/documentation/business-messaging/whatsapp/support/error-codes",
        }]
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "0000000000000000", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550000000", "phone_number_id": "1234567890"},
            "statuses": [entry_status],
        }}]}],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def post_event(client: httpx.Client, url: str, secret: str, body: bytes, bad_signature: bool = False) -> httpx.Response:
    signature = sign(secret + "-wrong" if bad_signature else secret, body)
    return client.post(url, content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})


def newest_message_id() -> str | None:
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Payslip

    with SessionLocal() as db:
        return db.scalar(select(Payslip.whatsapp_message_id).where(Payslip.whatsapp_message_id.is_not(None)).order_by(Payslip.id.desc()))


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a signed fake WhatsApp status event to the local server.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--status", choices=["sent", "delivered", "read", "failed"])
    parser.add_argument("--message-id")
    parser.add_argument("--error-code", type=int)
    parser.add_argument("--bad-signature", action="store_true", help="send a wrong signature (must be rejected)")
    parser.add_argument("--verify", action="store_true", help="test the GET verification handshake instead")
    args = parser.parse_args(argv)
    settings = get_settings()
    secret = settings.whatsapp_app_secret.get_secret_value()
    http = client or httpx.Client(timeout=15)

    if args.verify:
        challenge = "fake-challenge-12345"
        r = http.get(args.url, params={"hub.mode": "subscribe", "hub.challenge": challenge,
                                       "hub.verify_token": settings.whatsapp_verify_token.get_secret_value()})
        print(f"Verification: HTTP {r.status_code}, challenge echoed: {r.text == challenge}")
        r = http.get(args.url, params={"hub.mode": "subscribe", "hub.challenge": challenge, "hub.verify_token": "definitely-wrong"})
        print(f"With a wrong verify token: HTTP {r.status_code} (should be 403)")
        return 0
    if not secret:
        print("WHATSAPP_APP_SECRET is empty in .env, so the server would reject everything. Set it first.")
        return 1

    message_id = args.message_id or newest_message_id()
    if not message_id:
        print("No sent payslip found in the database. Send a batch first (mock mode is fine), or pass --message-id.")
        return 1
    statuses = [args.status] if args.status else ["delivered", "read"]
    for status in statuses:
        body = build_payload(message_id, status, args.error_code)
        r = post_event(http, args.url, secret, body, args.bad_signature)
        print(f"{status}: HTTP {r.status_code} {r.text.strip()[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
