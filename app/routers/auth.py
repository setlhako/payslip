import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.models.mixins import utcnow
from app.security import passwords
from app.security.auth import require_login
from app.security.permissions import require
from app.security.sessions import end_session, get_session_user, start_session
from app.services import audit, login_limiter
from app.services.import_preview import store as import_store
from app.templating import templates

router = APIRouter()
log = logging.getLogger(__name__)

GENERIC_ERROR = "Invalid username or password."


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_page(request: Request, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=status_code)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)):
    if get_session_user(request, db):
        return RedirectResponse("/", status_code=303)
    return _login_page(request)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    ip = client_ip(request)
    name = passwords.normalize_username(username)[:150]
    lock_msg = f"Too many failed attempts. Please try again in {settings.login_lockout_minutes} minutes."

    if login_limiter.is_locked(db, name, ip):
        return _login_page(request, lock_msg, 429)

    user = db.scalar(select(User).where(User.username == name))
    # Always run a full password check (against a dummy hash if the user is unknown) so timing is similar.
    ok = passwords.verify_password(user.password_hash if user else None, password[:1000])
    if user is not None and not user.is_active:
        ok = False

    if not ok:
        locked_now = login_limiter.record_failure(db, name, ip)
        # Only log the username when it is a real one: people sometimes type their password into this box.
        who = f"username '{user.username}'" if user else "unknown username"
        audit.record(
            db,
            "login_lockout" if locked_now else "login_failure",
            user_id=user.id if user else None,
            details=who,
            ip=ip,
        )
        if locked_now:
            return _login_page(request, lock_msg, 429)
        return _login_page(request, GENERIC_ERROR, 401)

    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    user.last_login_at = utcnow()
    db.commit()
    login_limiter.record_success(db, name, ip)
    audit.record(db, "login_success", user_id=user.id, details=f"username '{user.username}'", ip=ip)
    start_session(request, user)  # brand-new session + new CSRF token
    return RedirectResponse("/account/password" if user.must_change_password else "/", status_code=303)


@router.post("/logout", dependencies=[Depends(require("account.self"))])
def logout(request: Request, user: User = Depends(require_login), db: Session = Depends(get_db)):
    audit.record(db, "logout", user_id=user.id, details=f"username '{user.username}'", ip=client_ip(request))
    import_store.discard_for_user(user.id)
    end_session(request)
    return RedirectResponse("/login", status_code=303)
