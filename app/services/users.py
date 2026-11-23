"""User management (admin area). Plain passwords are never stored or logged: a temporary password is generated, hashed at
once, and handed to the caller ONCE (the router shows it through services/one_time.py).

Safeguards (enforced here, so they hold for every caller):
- usernames are unique, case-insensitive and stored lower-case
- the last active admin can never be disabled or demoted; an admin can never disable or demote THEMSELVES
- every action writes an audit entry: acting user, target user id, what happened, never a password
- new and reset passwords are temporary: must_change_password is set, and the change rules reject reusing the current password
"""
import secrets
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditLog, LoginAttempt, User
from app.models.mixins import utcnow
from app.security.passwords import (
    hash_password, normalize_username, password_problem, username_problem, verify_password,
)
from app.security.permissions import ADMIN, ROLES
from app.services import audit, login_limiter

TEMP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"     # no 0 O 1 l I: easy to read out loud
NAME_MAX = 150


class UserError(Exception):
    """Something HR asked for is not allowed or not valid. The message is plain language and safe to show."""


def generate_temp_password() -> str:
    """16 random characters in 4 groups: 'Kd7M-qrT9-xWc4-hJ2p' (19 characters with the dashes: over the 12 minimum)."""
    groups = ["".join(secrets.choice(TEMP_ALPHABET) for _ in range(4)) for _ in range(4)]
    return "-".join(groups)


def _clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise UserError("Enter the person's full name.")
    if len(name) > NAME_MAX or not name.isprintable():
        raise UserError(f"The name must be plain text of at most {NAME_MAX} characters.")
    return name


def _check_role(role: str) -> str:
    if role not in ROLES:
        raise UserError("Choose a role: admin, HR or viewer.")
    return role


def active_admin_count(db: Session, excluding_id: int | None = None) -> int:
    stmt = select(func.count()).select_from(User).where(User.role == ADMIN, User.is_active.is_(True))
    if excluding_id is not None:
        stmt = stmt.where(User.id != excluding_id)
    return db.scalar(stmt) or 0


def _record(db: Session, actor: User, action: str, target: User, details: str | None = None, ip: str | None = None) -> None:
    audit.record(db, action, user_id=actor.id, entity_type="user", entity_id=target.id, details=details, ip=ip, commit=False)


def _new_temporary_password(user: User) -> str:
    """Make a temporary password for this user, store only its hash, and return the plain text to the caller."""
    while True:
        temp = generate_temp_password()
        if password_problem(temp, user.username) is None:
            break
    user.password_hash = hash_password(temp)
    user.must_change_password = True
    user.password_changed_at = None
    user.session_version += 1                   # every existing session of this user ends
    return temp


# ---- create / edit ------------------------------------------------------------------------------------------------

def create_user(db: Session, actor: User, username: str, full_name: str, role: str, ip: str | None = None) -> tuple[User, str]:
    username = normalize_username(username)
    if msg := username_problem(username):
        raise UserError(msg)
    if db.scalar(select(User).where(func.lower(User.username) == username)):
        raise UserError("That username is already taken (usernames are not case-sensitive).")
    user = User(username=username, full_name=_clean_name(full_name), role=_check_role(role), password_hash="x", is_active=True)
    db.add(user)
    db.flush()
    temp = _new_temporary_password(user)
    _record(db, actor, "user_created", user, f"role={user.role}", ip)
    db.commit()
    return user, temp


def update_user(db: Session, actor: User, user: User, full_name: str, role: str, ip: str | None = None) -> list[str]:
    name, role = _clean_name(full_name), _check_role(role)
    changes = []
    if role != user.role:
        if user.id == actor.id:
            raise UserError("You cannot change your own role. Ask another admin.")
        if user.role == ADMIN and user.is_active and active_admin_count(db, excluding_id=user.id) == 0:
            raise UserError("This is the last active admin, so its role cannot be changed.")
        changes.append(f"role {user.role}->{role}")
        user.role = role
        user.session_version += 1               # the old role's pages must not stay open
    if name != user.full_name:
        changes.append("name")
        user.full_name = name
    if changes:
        _record(db, actor, "user_updated", user, ", ".join(changes), ip)
        db.commit()
    return changes


def set_active(db: Session, actor: User, user: User, active: bool, ip: str | None = None) -> None:
    if active == user.is_active:
        return
    if not active:
        if user.id == actor.id:
            raise UserError("You cannot disable your own account.")
        if user.role == ADMIN and active_admin_count(db, excluding_id=user.id) == 0:
            raise UserError("This is the last active admin, so it cannot be disabled.")
    user.is_active = active
    if not active:
        user.session_version += 1
    _record(db, actor, "user_enabled" if active else "user_disabled", user, None, ip)
    db.commit()


def reset_password(db: Session, actor: User, user: User, ip: str | None = None) -> str:
    temp = _new_temporary_password(user)
    _record(db, actor, "user_password_reset", user, None, ip)
    db.commit()
    login_limiter.clear_for_username(db, user.username)
    return temp


def unlock(db: Session, actor: User, user: User, ip: str | None = None) -> None:
    login_limiter.clear_for_username(db, user.username)
    _record(db, actor, "user_unlocked", user, None, ip)
    db.commit()


def sign_out_everywhere(db: Session, actor: User, user: User, ip: str | None = None) -> None:
    user.session_version += 1
    _record(db, actor, "user_sessions_revoked", user, None, ip)
    db.commit()


# ---- listing -----------------------------------------------------------------------------------------------------

def locked_usernames(db: Session) -> set[str]:
    """Usernames that are locked out right now (too many failed logins from one address within the lockout window)."""
    s = get_settings()
    since = utcnow() - timedelta(minutes=s.login_lockout_minutes)
    rows = db.execute(
        select(LoginAttempt.username).where(LoginAttempt.succeeded.is_(False), LoginAttempt.attempted_at > since)
        .group_by(LoginAttempt.username, LoginAttempt.ip_address).having(func.count() >= s.login_max_attempts)
    )
    return {username for (username,) in rows}


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_users(db: Session, q: str = "") -> list[tuple[User, bool]]:
    stmt = select(User).order_by(func.lower(User.username))
    q = (q or "").strip()[:100]
    if q:
        stmt = stmt.where(or_(User.username.ilike(_like(q), escape="\\"), User.full_name.ilike(_like(q), escape="\\"),
                              User.role.ilike(_like(q), escape="\\")))
    locked = locked_usernames(db)
    return [(u, u.username in locked) for u in db.scalars(stmt)]


def recent_logins(db: Session, user: User, limit: int = 5) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).where(AuditLog.user_id == user.id, AuditLog.action == "login_success")
                           .order_by(AuditLog.id.desc()).limit(limit)))


# ---- a user changing their OWN password --------------------------------------------------------------------------

def change_own_password(db: Session, user: User, current: str, new: str, confirm: str, ip: str = "unknown") -> None:
    """Checks, then changes. Raises UserError with a plain message. The caller starts a fresh session afterwards."""
    key = f"pwchange:{user.username}"
    if login_limiter.is_locked(db, key, ip):
        raise UserError(f"Too many wrong attempts. Please try again in {get_settings().login_lockout_minutes} minutes.")
    if not verify_password(user.password_hash, current[:1000]):
        login_limiter.record_failure(db, key, ip)
        raise UserError("Your current password is not correct.")
    if new != confirm:
        raise UserError("The two new passwords do not match.")
    if msg := password_problem(new, user.username):
        raise UserError(msg)
    if verify_password(user.password_hash, new):
        raise UserError("The new password must be different from your current password.")
    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.password_changed_at = utcnow()
    user.session_version += 1                   # every other session of this user ends
    audit.record(db, "password_changed", user_id=user.id, entity_type="user", entity_id=user.id, ip=ip, commit=False)
    db.commit()
    login_limiter.record_success(db, key, ip)
