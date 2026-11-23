"""Creating users and resetting passwords from the COMMAND LINE: the emergency path when nobody can log in.
Used ONLY by the command-line scripts, never by a web route. (Day-to-day user management is the admin Users page.)

Passwords given here are chosen by the person at the keyboard, so they are NOT forced to change by default; use
--must-change for that. A reset also ends every open session of that user and clears their lockout.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import User
from app.models.mixins import utcnow
from app.security.passwords import (
    hash_password,
    normalize_username,
    password_problem,
    username_problem,
)
from app.security.permissions import ADMIN, ROLES
from app.services import audit


class AdminError(Exception):
    """A problem with the input, with a message that is safe to show."""


def create_admin(db: Session, username: str, password: str, role: str = ADMIN, full_name: str = "",
                 must_change: bool = False) -> User:
    """Create a user (an admin unless another role is given)."""
    username = normalize_username(username)
    if role not in ROLES:
        raise AdminError("Role must be admin, hr or viewer.")
    if msg := username_problem(username):
        raise AdminError(msg)
    if msg := password_problem(password, username):
        raise AdminError(msg)
    if db.scalar(select(User).where(func.lower(User.username) == username)):
        raise AdminError("That username already exists. Use reset_admin_password to change its password.")
    user = User(username=username, full_name=" ".join(full_name.split())[:150], role=role, password_hash=hash_password(password),
                is_active=True, must_change_password=must_change)
    db.add(user)
    db.commit()
    audit.record(db, "admin_created", user_id=user.id, entity_type="user", entity_id=user.id,
                 details=f"{'admin' if role == ADMIN else role} '{username}' created via command line")
    return user


def reset_password(db: Session, username: str, password: str, must_change: bool = False, enable: bool = False) -> User:
    from app.services import login_limiter

    username = normalize_username(username)
    user = db.scalar(select(User).where(func.lower(User.username) == username))
    if user is None:
        raise AdminError("No user with that username.")
    if msg := password_problem(password, username):
        raise AdminError(msg)
    user.password_hash = hash_password(password)  # also logs out any existing session (see sessions.py)
    user.must_change_password = must_change
    user.password_changed_at = None if must_change else utcnow()
    user.session_version += 1
    if enable:
        user.is_active = True
    db.commit()
    login_limiter.clear_for_username(db, username)
    audit.record(db, "admin_password_reset", user_id=user.id, entity_type="user", entity_id=user.id,
                 details=f"password for '{username}' reset via command line" + (" (account re-enabled)" if enable else ""))
    return user
