"""POST/GET /webhook/whatsapp: Meta's delivery status reports. NO login (Meta cannot log in), so everything depends on:
- GET:  the verification handshake: hub.mode=subscribe and hub.verify_token equal to WHATSAPP_VERIFY_TOKEN -> echo hub.challenge.
- POST: X-Hub-Signature-256 must equal sha256=HMAC-SHA256(raw body bytes, WHATSAPP_APP_SECRET), compared in constant time.
        Anything else is rejected with 403 BEFORE the body is parsed.
Valid reports go through the same status code path as the sender (send_status.apply_status_event): idempotent and never
backwards. Inbound customer messages (value.messages) are ignored without being read or logged. Answers 200 quickly.
"""
import hmac
import json
import logging
import threading
import time
from hashlib import sha256

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.routers.auth import client_ip
from app.routers.batches import get_session_factory
from app.services import audit, system_state
from app.services.send_status import apply_status_event

router = APIRouter()
log = logging.getLogger(__name__)

_reject_lock = threading.Lock()
_last_reject_audit = 0.0
REJECT_AUDIT_EVERY = 60.0     # an attacker must not be able to fill the audit table: one rejected-webhook entry per minute


def valid_signature(secret: str, raw_body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), header[len("sha256="):].strip().lower().encode("ascii", "ignore"))


def process_payload(session_factory, payload) -> int:
    """Apply every status in a webhook payload. Returns how many changed a payslip. Ignores everything else."""
    changed = 0
    if not isinstance(payload, dict):
        return 0
    for entry in payload.get("entry") or []:
        for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
            value = change.get("value") if isinstance(change, dict) else None
            for status in (value.get("statuses") or []) if isinstance(value, dict) else []:   # value.messages is never touched
                try:
                    if isinstance(status, dict) and apply_status_event(
                            session_factory, status.get("id"), status.get("status"), status.get("timestamp"), status.get("errors")):
                        changed += 1
                except Exception as exc:
                    log.error("A webhook status could not be applied: %s", type(exc).__name__)
    return changed


@router.get("/webhook/whatsapp")
def verify(
    mode: str = Query("", alias="hub.mode"), token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    expected = get_settings().whatsapp_verify_token.get_secret_value()
    if mode == "subscribe" and expected and challenge and hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        return PlainTextResponse(challenge[:200])
    return PlainTextResponse("Forbidden", status_code=403)


@router.post("/webhook/whatsapp")
async def receive(request: Request, db: Session = Depends(get_db), factory=Depends(get_session_factory)):
    raw = await request.body()
    secret = get_settings().whatsapp_app_secret.get_secret_value()
    if not valid_signature(secret, raw, request.headers.get("x-hub-signature-256")):
        await run_in_threadpool(_audit_rejected, db, client_ip(request))
        return JSONResponse({"status": "rejected"}, status_code=403)
    await run_in_threadpool(system_state.set_value, db, "last_webhook_at", "valid")      # when a SIGNED event last arrived
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"status": "ignored"})            # signed but not JSON: nothing to do, and no retry storm
    changed = await run_in_threadpool(process_payload, factory, payload)
    return JSONResponse({"status": "ok", "updated": changed})


def _audit_rejected(db: Session, ip: str) -> None:
    global _last_reject_audit
    with _reject_lock:
        now = time.monotonic()
        if now - _last_reject_audit < REJECT_AUDIT_EVERY and _last_reject_audit:
            return
        _last_reject_audit = now
    audit.record(db, "webhook_rejected", ip=ip, details="reason=bad_signature")
    log.warning("A WhatsApp webhook with a bad or missing signature was rejected.")
