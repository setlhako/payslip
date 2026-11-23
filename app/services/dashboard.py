"""Numbers for the dashboard. Personal data: employee names appear only where the page already shows them to this role;
the activity feed shows who did what, never details."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Batch, Payslip, User
from app.models.enums import BatchStatus
from app.security.permissions import can
from app.services import send_view
from app.services.audit_view import label
from app.services.employees import overview
from app.services.readiness import summary

QUIET_ACTIONS = {"employee_edit_opened", "login_failure"}      # too noisy for a "recent activity" list


def _batch_card(db: Session, batch: Batch | None) -> dict | None:
    if batch is None:
        return None
    rows = list(db.scalars(select(Payslip).where(Payslip.batch_id == batch.id)))
    sent = batch.status in (BatchStatus.sending.value, BatchStatus.completed.value)
    review = summary(rows) if not sent else None
    counts = send_view.counts([(p, None) for p in rows]) if sent else None
    attempted = (counts["sent"] + counts["delivered"] + counts["read"] + counts["failed"] + counts["uncertain"]) if counts else 0
    good = (counts["delivered"] + counts["read"]) if counts else 0
    attention = (review["needs_review"] + review["possible_mismatch"] + review["unmatched"] + review["invalid"] + review["pending"]) if review else 0
    if batch.files_deleted_at:
        kind, text = "inactive", "Files deleted"
    elif batch.status == BatchStatus.completed.value:
        kind, text = "new", "Sent"
    elif batch.status == BatchStatus.sending.value:
        kind, text = "update", "Sending"
    elif batch.status == BatchStatus.ready.value:
        kind, text = "new", "Ready to send"
    else:
        kind, text = ("warning" if attention else "unchanged"), "Draft"
    return {"batch": batch, "sent": sent, "review": review, "counts": counts, "total": len(rows), "attention": attention,
            "state_kind": kind, "state_label": text, "good": good, "attempted": attempted,
            "rate": round(100 * good / attempted) if attempted else None}


def dashboard_context(db: Session, user: User) -> dict:
    batches = list(db.scalars(select(Batch).order_by(Batch.created_at.desc(), Batch.id.desc()).limit(25)))
    latest = batches[0] if batches else None
    last_sent = next((b for b in batches if b.status in (BatchStatus.sending.value, BatchStatus.completed.value)), None)
    feed = []
    if can(user, "audit.view"):
        entries = db.execute(select(AuditLog, User.username).outerjoin(User, User.id == AuditLog.user_id)
                             .where(AuditLog.action.not_in(QUIET_ACTIONS)).order_by(AuditLog.id.desc()).limit(8)).all()
        feed = [{"when": e.created_at, "who": name or "system", "what": label(e.action)} for e, name in entries]
    return {
        "stats": overview(db), "latest": _batch_card(db, latest), "last_sent": _batch_card(db, last_sent) if last_sent and last_sent is not latest else None,
        "feed": feed, "has_batches": bool(batches),
    }
