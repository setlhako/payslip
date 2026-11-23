"""My account: any signed-in user can change their OWN password and see their own recent logins.

A user on a temporary password is sent to /account/password by every other page until they choose their own
(security/auth.py: PENDING_ALLOWED_PATHS)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.flash import flash
from app.models import User
from app.routers.auth import client_ip
from app.security.passwords import MIN_PASSWORD_LENGTH
from app.security.permissions import ROLE_HELP, ROLE_LABELS, require
from app.security.sessions import start_session
from app.services import users as svc
from app.templating import templates

router = APIRouter(prefix="/account", dependencies=[Depends(require("account.self"))])


@router.get("", response_class=HTMLResponse)
def account(request: Request, db: Session = Depends(get_db), user: User = Depends(require("account.self"))):
    return templates.TemplateResponse(request, "account.html", {
        "current_user": user, "logins": svc.recent_logins(db, user, 8), "role_text": ROLE_LABELS.get(user.role, user.role),
        "role_help": ROLE_HELP.get(user.role, ""),
    })


def _password_page(request, user, error=None, status_code=200):
    return templates.TemplateResponse(request, "account_password.html", {
        "current_user": user, "error": error, "forced": user.must_change_password, "min_length": MIN_PASSWORD_LENGTH,
    }, status_code=status_code)


@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request, user: User = Depends(require("account.self"))):
    return _password_page(request, user)


@router.post("/password", response_class=HTMLResponse)
def password_submit(
    request: Request, current_password: str = Form(""), new_password: str = Form(""), confirm_password: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require("account.self")),
):
    try:
        svc.change_own_password(db, user, current_password, new_password, confirm_password, ip=client_ip(request))
    except svc.UserError as exc:
        db.rollback()
        return _password_page(request, user, str(exc), 422)
    start_session(request, user)                       # a fresh session for THIS browser; every other session ended
    flash(request, "Your password was changed.", "success")
    return RedirectResponse("/", status_code=303)
