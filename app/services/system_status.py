"""Read-only facts for the System status page. No secret value is ever returned: only yes/no for "is it set"."""
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

import app
from app.config import get_settings
from app.models import Batch
from app.models.enums import BatchStatus
from app.models.mixins import utcnow
from app.services import system_state


def _present(value: str) -> bool:
    return bool(value.strip()) and not value.strip().lower().startswith("your-")      # empty or the .env.example placeholder


def database_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def status_context(db: Session) -> dict:
    s = get_settings()
    live_needs = [
        ("Access token (WHATSAPP_ACCESS_TOKEN)", _present(s.whatsapp_access_token.get_secret_value())),
        ("Phone number ID (WHATSAPP_PHONE_NUMBER_ID)", _present(s.whatsapp_phone_number_id)),
        ("App secret (WHATSAPP_APP_SECRET)", _present(s.whatsapp_app_secret.get_secret_value())),
        ("Webhook verify token (WHATSAPP_VERIFY_TOKEN)", _present(s.whatsapp_verify_token.get_secret_value())),
        ("Graph API version (GRAPH_API_VERSION)", _present(s.graph_api_version)),
    ]
    core = [
        ("Session secret (SESSION_SECRET)", True),                                  # the app cannot start without it
        ("ID encryption key (ID_ENCRYPTION_KEY)", True),                            # likewise
        ("Business account ID (WHATSAPP_BUSINESS_ACCOUNT_ID), only for the preflight check", _present(s.whatsapp_business_account_id)),
    ]
    cutoff = utcnow() - timedelta(days=s.batch_retention_days)
    open_batches = db.scalar(select(func.count()).select_from(Batch).where(Batch.status.in_([BatchStatus.draft.value, BatchStatus.ready.value]))) or 0
    sending = db.scalar(select(func.count()).select_from(Batch).where(Batch.status == BatchStatus.sending.value)) or 0
    old = db.scalar(select(func.count()).select_from(Batch).where(Batch.created_at < cutoff, Batch.files_deleted_at.is_(None))) or 0
    last_webhook = system_state.get_time(db, "last_webhook_at")
    allowed = len(s.allowed_recipients)
    return {
        "mode": s.whatsapp_mode, "template_name": s.whatsapp_template_name, "template_language": s.whatsapp_template_language,
        "send_kind": s.whatsapp_send_kind, "graph_version": s.graph_api_version,
        "allowed_active": allowed > 0, "allowed_count": allowed,
        "settings": live_needs + core, "live_ready": all(ok for _, ok in live_needs),
        "last_webhook": last_webhook, "database_ok": database_ok(db), "version": app.__version__, "environment": s.app_env,
        "open_batches": open_batches, "sending_batches": sending, "old_batches": old, "retention_days": s.batch_retention_days,
    }
