"""Numbers for the dashboard. Personal data: employee names appear only where the page already shows them to this role;
the activity feed shows who did what, never details."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditLog, Batch, Payslip, User
from app.models.enums import BatchStatus
from app.models.mixins import utcnow
from app.security.permissions import can
from app.services import send_view, system_state
from app.services.audit_view import label
from app.services.employees import attention, overview
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


WEBHOOK_QUIET_AFTER = timedelta(minutes=10)    # a sent message normally gets its delivery report within minutes
TOKEN_ALERT_DAYS = 30


def admin_alerts(db: Session) -> list[dict]:
    """Big banners for admins: WhatsApp refused our access token, or Meta's delivery reports are not arriving.
    Each is {kind, title, text, link, link_label}. Only facts from our own tables: nothing is asked of Meta here."""
    alerts: list[dict] = []
    now = utcnow()

    # 1. The access token was rejected, and nothing has been sent successfully since.
    last_token_error = db.scalar(select(AuditLog.created_at).where(AuditLog.action == "send_token_error")
                                 .order_by(AuditLog.id.desc()).limit(1))
    if last_token_error and now - last_token_error < timedelta(days=TOKEN_ALERT_DAYS):
        worked_since = db.scalar(select(Payslip.id).where(Payslip.sent_at > last_token_error).limit(1))
        if not worked_since:
            halted = db.scalar(select(Batch.id).where(Batch.send_state == "halted").order_by(Batch.id.desc()).limit(1))
            alerts.append({
                "kind": "token", "title": "WhatsApp rejected the access token",
                "text": f"On {last_token_error.strftime('%d %b %Y at %H:%M')} (UTC) WhatsApp refused our access token, so sending stopped, "
                        "and nothing has been sent successfully since. Put a new token in WHATSAPP_ACCESS_TOKEN in the .env file, "
                        "restart the app, then press Resume on the stopped batch.",
                "link": f"/batches/{halted}" if halted else "/system", "link_label": "Open the stopped batch" if halted else "Open System status",
            })

    # 2. Live messages were sent a while ago, but no delivery report (webhook) has arrived since.
    if get_settings().whatsapp_mode == "live":
        last_webhook = system_state.get_time(db, "last_webhook_at")
        waiting_query = (select(Payslip.id).join(Batch, Batch.id == Payslip.batch_id)
                         .where(Payslip.status == "sent", Batch.send_mode == "live", Payslip.sent_at < now - WEBHOOK_QUIET_AFTER))
        if last_webhook:
            waiting_query = waiting_query.where(Payslip.sent_at > last_webhook)
        waiting = len(db.scalars(waiting_query.limit(1000)).all())
        if waiting:
            since = f"since {last_webhook.strftime('%d %b %Y at %H:%M')} (UTC)" if last_webhook else "yet"
            alerts.append({
                "kind": "webhook", "title": "No delivery reports are arriving from WhatsApp",
                "text": f"{waiting} message{'' if waiting == 1 else 's'} were accepted by WhatsApp more than 10 minutes ago, but no delivery "
                        f"report has been received {since}. Check that Meta's webhook points to https://your-domain/webhook/whatsapp "
                        "and that the verify token matches WHATSAPP_VERIFY_TOKEN.",
                "link": "/system", "link_label": "Open System status",
            })
    return alerts


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
        "feed": feed, "has_batches": bool(batches), "attention": attention(db),
        "alerts": admin_alerts(db) if can(user, "system.view") else [],
    }
