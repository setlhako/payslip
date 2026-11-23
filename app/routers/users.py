"""Users (admin only): list, add, edit, disable/enable, reset password, unlock, sign out everywhere.

Everything needs the users.manage permission (admin). Temporary passwords are shown ONCE on a separate page, reached by a
redirect after the action (so a page refresh cannot repeat the action), and are never stored, logged or put in a cookie.
"""
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.flash import flash
from app.models import User
from app.routers.auth import client_ip
from app.security.permissions import ROLE_HELP, ROLE_LABELS, ROLES, require
from app.services import users as svc
from app.services.one_time import temp_passwords
from app.templating import templates

router = APIRouter(prefix="/users", dependencies=[Depends(require("users.manage"))])
log = logging.getLogger(__name__)


def _target(db: Session, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    return target


@router.get("", response_class=HTMLResponse)
def list_users(request: Request, q: str = Query(""), db: Session = Depends(get_db), user: User = Depends(require("users.manage"))):
    return templates.TemplateResponse(request, "users.html", {
        "current_user": user, "rows": svc.list_users(db, q), "q": q.strip()[:100], "role_labels": ROLE_LABELS,
        "total": len(svc.list_users(db, "")),
    })


def _form(request, user, target, values, error=None, status_code=200):
    return templates.TemplateResponse(request, "user_form.html", {
        "current_user": user, "target": target, "values": values, "error": error, "roles": ROLES, "role_labels": ROLE_LABELS,
        "role_help": ROLE_HELP, "is_self": target is not None and target.id == user.id,
    }, status_code=status_code)


@router.get("/new", response_class=HTMLResponse)
def new_form(request: Request, user: User = Depends(require("users.manage"))):
    return _form(request, user, None, {"username": "", "full_name": "", "role": "viewer"})


@router.post("/new", response_class=HTMLResponse)
def new_submit(
    request: Request, username: str = Form(""), full_name: str = Form(""), role: str = Form("viewer"),
    db: Session = Depends(get_db), user: User = Depends(require("users.manage")),
):
    values = {"username": username.strip(), "full_name": full_name.strip(), "role": role}
    try:
        created, temp = svc.create_user(db, user, username, full_name, role, ip=client_ip(request))
    except svc.UserError as exc:
        db.rollback()
        return _form(request, user, None, values, str(exc), 422)
    token = temp_passwords.put(user.id, {"username": created.username, "name": created.display_name, "password": temp, "kind": "created"})
    return RedirectResponse(f"/users/temp-password?t={token}", status_code=303)


@router.get("/temp-password", response_class=HTMLResponse)
def show_temp_password(request: Request, t: str = Query(""), user: User = Depends(require("users.manage"))):
    data = temp_passwords.pop(t, user.id)                  # ONCE: a refresh finds nothing
    if not isinstance(data, dict):
        flash(request, "That temporary password was already shown or has expired. Use Reset password to make a new one.", "info")
        return RedirectResponse("/users", status_code=303)
    return templates.TemplateResponse(request, "user_temp_password.html", {"current_user": user, **data})


@router.get("/{user_id:int}/edit", response_class=HTMLResponse)
def edit_form(request: Request, user_id: int, db: Session = Depends(get_db), user: User = Depends(require("users.manage"))):
    target = _target(db, user_id)
    return _form(request, user, target, {"username": target.username, "full_name": target.full_name, "role": target.role})


@router.post("/{user_id:int}/edit", response_class=HTMLResponse)
def edit_submit(
    request: Request, user_id: int, full_name: str = Form(""), role: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require("users.manage")),
):
    target = _target(db, user_id)
    try:
        changes = svc.update_user(db, user, target, full_name, role, ip=client_ip(request))
    except svc.UserError as exc:
        db.rollback()
        return _form(request, user, target, {"username": target.username, "full_name": full_name.strip(), "role": role}, str(exc), 422)
    flash(request, "Changes saved." if changes else "Nothing was changed.", "success" if changes else "info")
    return RedirectResponse("/users", status_code=303)


def _act(request, db, user, user_id, action, ok_message, **kwargs):
    target = _target(db, user_id)
    try:
        action(db, user, target, ip=client_ip(request), **kwargs)
        flash(request, ok_message, "success")
    except svc.UserError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse("/users", status_code=303)


@router.post("/{user_id:int}/disable")
def disable(request: Request, user_id: int, db: Session = Depends(get_db), user: User = Depends(require("users.manage"))):
    return _act(request, db, user, user_id, svc.set_active, "User disabled. They are signed out and cannot log in.", active=False)


@router.post("/{user_id:int}/enable")
def enable(request: Request, user_id: int, db: Session = Depends(get_db), user: User = Depends(require("users.manage"))):
    return _act(request, db, user, user_id, svc.set_active, "User enabled.", active=True)


@router.post("/{user_id:int}/unlock")
def unlock(request: Request, user_id: int, db: Session = Depends(get_db), user: User = Depends(require("users.manage"))):
    return _act(request, db, user, user_id, svc.unlock, "Account unlocked. They can try to log in again.")


@router.post("/{user_id:int}/signout")
def signout(request: Request, user_id: int, db: Session = Depends(get_db), user: User = Depends(require("users.manage"))):
    return _act(request, db, user, user_id, svc.sign_out_everywhere, "Signed out everywhere. Any open sessions of this user have ended.")


@router.post("/{user_id:int}/reset-password")
def reset_password(request: Request, user_id: int, db: Session = Depends(get_db), user: User = Depends(require("users.manage"))):
    target = _target(db, user_id)
    temp = svc.reset_password(db, user, target, ip=client_ip(request))
    token = temp_passwords.put(user.id, {"username": target.username, "name": target.display_name, "password": temp, "kind": "reset"})
    return RedirectResponse(f"/users/temp-password?t={token}", status_code=303)
