"""Login rate limiting, stored in the database so a restart does not reset it.

Rule: LOGIN_MAX_ATTEMPTS failed logins for the same username + IP within LOGIN_LOCKOUT_MINUTES
locks that username + IP for LOGIN_LOCKOUT_MINUTES (counted from the last failure). While locked,
even the correct password is refused and further attempts are not recorded (so they do not extend the lock).
The same rules apply to usernames that do not exist, so the lock reveals nothing.
"""
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import LoginAttempt
from app.models.mixins import utcnow


def _key(username: str, ip: str):
    return (LoginAttempt.username == username[:150], LoginAttempt.ip_address == ip[:45])


def recent_failures(db: Session, username: str, ip: str) -> int:
    since = utcnow() - timedelta(minutes=get_settings().login_lockout_minutes)
    stmt = select(func.count()).select_from(LoginAttempt).where(
        *_key(username, ip), LoginAttempt.succeeded.is_(False), LoginAttempt.attempted_at > since
    )
    return db.scalar(stmt) or 0


def is_locked(db: Session, username: str, ip: str) -> bool:
    return recent_failures(db, username, ip) >= get_settings().login_max_attempts


def record_failure(db: Session, username: str, ip: str) -> bool:
    """Record a failed login. Returns True if this failure triggered the lockout."""
    db.add(LoginAttempt(username=username[:150], ip_address=ip[:45], succeeded=False))
    db.commit()
    return recent_failures(db, username, ip) >= get_settings().login_max_attempts


def record_success(db: Session, username: str, ip: str) -> None:
    """Clear the failure count for this username + IP, and tidy up old rows."""
    db.execute(delete(LoginAttempt).where(*_key(username, ip)))
    db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < utcnow() - timedelta(days=1)))
    db.commit()


def purge_old(db: Session, days: int = 1) -> int:
    """Delete login-attempt rows older than `days` (they only matter inside the lockout window). Returns how many."""
    result = db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < utcnow() - timedelta(days=days)))
    db.commit()
    return result.rowcount or 0


def clear_for_username(db: Session, username: str) -> None:
    """Used when an admin's password is reset: remove any lockout for that username."""
    db.execute(delete(LoginAttempt).where(LoginAttempt.username == username[:150]))
    db.commit()
