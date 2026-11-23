"""Payslip delivery status: ONE code path used by the sender, the mock client and the webhook.

Rules (idempotent, never backwards):
- sent < delivered < read only move forward. A late or repeated "delivered" after "read" changes nothing.
- "read" may arrive without "delivered" (WhatsApp skips it when the chat is open): it sets delivered_at too.
- "failed" is accepted only while the payslip is sending / uncertain / sent. A failure never overwrites delivered or read.
- A webhook "sent/delivered/read" resolves an "uncertain" row (we learn the message did go out) and clears its error.
- Timestamps are set once. Events for unknown message ids are ignored.
"""
import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Batch, Payslip
from app.models.enums import BatchStatus, PayslipStatus, SendState
from app.models.mixins import utcnow
from app.services.whatsapp_errors import clean_error_text

log = logging.getLogger(__name__)

RANK = {"pending": 0, "queued": 1, "sending": 2, "uncertain": 2, "failed": 2, "sent": 3, "delivered": 4, "read": 5}
FORWARD = ("sent", "delivered", "read")
OPEN_STATES = (PayslipStatus.pending.value, PayslipStatus.queued.value, PayslipStatus.sending.value)


def may_apply(current: str, new: str) -> bool:
    if new in FORWARD:
        return RANK.get(current, 0) < RANK[new]
    if new == "failed":
        return current in ("sending", "uncertain", "sent")
    return False


def apply_status(p: Payslip, new: str, when: datetime | None = None, code=None, text: str | None = None) -> bool:
    """Change the payslip if the rules allow it. Returns True when something changed. Does not commit."""
    if not may_apply(p.status, new):
        return False
    when = when or utcnow()
    p.status = new
    if new == "failed":
        p.error_code = str(code) if code is not None else None
        p.error = clean_error_text(text)
    else:
        p.error = p.error_code = None
        if new == "sent":
            p.sent_at = p.sent_at or when
        elif new == "delivered":
            p.delivered_at = p.delivered_at or when
        elif new == "read":
            p.delivered_at = p.delivered_at or when
            p.read_at = p.read_at or when
    return True


def maybe_complete(db: Session, batch: Batch) -> bool:
    """A batch that is sending becomes 'completed' once nothing is pending, queued or sending any more."""
    if batch.status != BatchStatus.sending.value:
        return False
    open_rows = db.scalar(select(func.count()).where(Payslip.batch_id == batch.id, Payslip.status.in_(OPEN_STATES))) or 0
    if open_rows:
        return False
    batch.status, batch.send_state, batch.completed_at = BatchStatus.completed.value, SendState.idle.value, utcnow()
    return True


def _when(timestamp) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(timestamp), UTC).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def apply_status_event(session_factory, message_id: str, status: str, timestamp=None, errors: list | None = None) -> bool:
    """A status report for one WhatsApp message id (from the webhook or the mock). Opens its own session."""
    if not message_id or status not in ("sent", "delivered", "read", "failed"):
        return False
    with session_factory() as db:
        p = db.scalar(select(Payslip).where(Payslip.whatsapp_message_id == message_id))
        if p is None:
            return False
        error = (errors or [None])[0] or {}
        text = (error.get("error_data") or {}).get("details") or error.get("message") or error.get("title")
        changed = apply_status(p, status, _when(timestamp), error.get("code"), text)
        if changed:
            batch = db.get(Batch, p.batch_id)
            if batch is not None:
                maybe_complete(db, batch)
            db.commit()
        return changed
