"""Creating admins and resetting passwords. Used ONLY by the command-line scripts, never by a web route."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from app.security.passwords import (
    hash_password,
    normalize_username,
    password_problem,
    username_problem,
)
from app.services import audit


class AdminError(Exception):
    """A problem with the input, with a message that is safe to show."""


def create_admin(db: Session, username: str, password: str) -> User:
    username = normalize_username(username)
    if msg := username_problem(username):
        raise AdminError(msg)
    if msg := password_problem(password, username):
        raise AdminError(msg)
    if db.scalar(select(User).where(User.username == username)):
        raise AdminError("That username already exists. Use reset_admin_password to change its password.")
    user = User(username=username, password_hash=hash_password(password), is_active=True)
    db.add(user)
    db.commit()
    audit.record(db, "admin_created", user_id=user.id, entity_type="user", entity_id=user.id,
                 details=f"admin '{username}' created via command line")
    return user


def reset_password(db: Session, username: str, password: str) -> User:
    from app.services import login_limiter

    username = normalize_username(username)
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise AdminError("No admin with that username.")
    if msg := password_problem(password, username):
        raise AdminError(msg)
    user.password_hash = hash_password(password)  # also logs out any existing session (see sessions.py)
    db.commit()
    login_limiter.clear_for_username(db, username)
    audit.record(db, "admin_password_reset", user_id=user.id, entity_type="user", entity_id=user.id,
                 details=f"password for '{username}' reset via command line")
    return user
