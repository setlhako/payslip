"""Login sessions stored in a signed cookie.

The cookie holds only: user id, when the session started, when it was last used, a fingerprint of the
password hash (so changing the password logs old sessions out), the user's session version ("sign out everywhere"
bumps it) and the CSRF token. No secrets.
Rules: expires after SESSION_IDLE_MINUTES without activity, and after SESSION_ABSOLUTE_HOURS in total.
"""
import hashlib
import secrets
import time

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User

UID, STARTED, LAST_SEEN, PW_FP, CSRF, VERSION = "uid", "iat", "seen", "pwf", "csrf_token", "sv"


def now() -> float:
    """Current time in seconds. A separate function so tests can fast-forward the clock."""
    return time.time()


def _fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def start_session(request: Request, user: User) -> None:
    """Begin a fresh session for `user` (the old session, if any, is thrown away)."""
    t = now()
    request.session.clear()
    request.session.update(
        {
            UID: user.id,
            STARTED: t,
            LAST_SEEN: t,
            PW_FP: _fingerprint(user.password_hash),
            VERSION: user.session_version,
            CSRF: secrets.token_urlsafe(32),
        }
    )


def end_session(request: Request) -> None:
    request.session.clear()


def get_session_user(request: Request, db: Session) -> User | None:
    """The logged-in user for this request, or None (and the session is cleared if it is no longer valid)."""
    s = request.session
    uid = s.get(UID)
    if uid is None:
        return None
    settings = get_settings()
    t = now()
    try:
        too_old = t - float(s[STARTED]) > settings.session_absolute_hours * 3600
        idle = t - float(s[LAST_SEEN]) > settings.session_idle_minutes * 60
    except (KeyError, TypeError, ValueError):
        s.clear()
        return None
    if too_old or idle:
        s.clear()
        return None
    user = db.get(User, uid)
    if (user is None or not user.is_active or s.get(PW_FP) != _fingerprint(user.password_hash)
            or s.get(VERSION, 0) != user.session_version):          # "sign out everywhere" bumps session_version
        s.clear()
        return None
    s[LAST_SEEN] = t
    return user
