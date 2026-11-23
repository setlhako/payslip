"""Showing FULL ID numbers to HR. This is the ONLY place stored full numbers are decrypted for display.

Every reveal is audited (one `ids_revealed` entry per request: who, how many employees, never the numbers) and
rate-limited per admin. The audit rows double as the rate-limit counter, so the limit survives a restart.
"""
import logging
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditLog, Employee, User
from app.models.mixins import utcnow
from app.services import audit
from app.services.id_crypto import IdCryptoError, decrypt_id

log = logging.getLogger(__name__)
ACTION = "ids_revealed"


def is_rate_limited(db: Session, user: User) -> bool:
    since = utcnow() - timedelta(minutes=1)
    recent = db.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == ACTION, AuditLog.user_id == user.id, AuditLog.created_at > since
        )
    ) or 0
    return recent >= get_settings().reveal_max_per_minute


def reveal(db: Session, user: User, employees: Iterable[Employee], ip: str | None, entity_id: int | None = None) -> dict[int, str]:
    """Decrypt the full IDs of these employees. Returns {employee id: full number} for those that have one.

    Writes ONE audit entry (and commits it) BEFORE returning, so nothing is shown without a record.
    Employees without a full ID, or whose value cannot be decrypted, are simply left out.
    """
    revealed: dict[int, str] = {}
    unreadable = 0
    for e in employees:
        if not e.id_number_encrypted:
            continue
        try:
            revealed[e.id] = decrypt_id(e.id_number_encrypted)
        except IdCryptoError:
            unreadable += 1
    if unreadable:
        log.error("%d stored ID(s) could not be decrypted (wrong or missing ID_ENCRYPTION_KEY?)", unreadable)
    if revealed:
        audit.record(
            db, ACTION, user_id=user.id, entity_type="employee" if entity_id else None, entity_id=entity_id,
            details=f"username '{user.username}', employees={len(revealed)}", ip=ip,
        )
    return revealed
