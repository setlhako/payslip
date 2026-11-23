"""Who may do what. The ONE place permissions are defined; every protected route declares the permission it needs
with `dependencies=[Depends(require("..."))]`, so the SERVER enforces it (hiding a button is only a courtesy).
A test walks every route and fails if one has no permission, and checks every role against every route.

    admin   everything, including users, the audit log and the system status page
    hr      employees (view/add/edit/import/deactivate, reveal full IDs) and batches (create/upload/review/send/retry)
    viewer  read-only: the employee list with IDs always masked, batches and send progress; cannot change or send anything
"""
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security.auth import require_login

ADMIN, HR, VIEWER = "admin", "hr", "viewer"
ROLES = (ADMIN, HR, VIEWER)
ROLE_LABELS = {ADMIN: "Admin", HR: "HR", VIEWER: "Viewer"}
ROLE_HELP = {
    ADMIN: "Everything, including users, the audit log and system status.",
    HR: "Employees and batches: add, edit, import, upload, review, send and retry. Can show full ID numbers.",
    VIEWER: "Read-only: employees (ID numbers always hidden), batches and send progress.",
}
_ALL = frozenset(ROLES)
_MANAGERS = frozenset({ADMIN, HR})

PERMISSIONS: dict[str, frozenset[str]] = {
    "dashboard.view": _ALL,
    "account.self": _ALL,              # own password and own recent logins, log out
    "employees.view": _ALL,            # the list (IDs masked unless ids.reveal)
    "employees.edit": _MANAGERS,       # add, edit, deactivate/activate, import
    "ids.reveal": _MANAGERS,           # show full ID numbers (and the last-4 password column)
    "batches.view": _ALL,              # batch list/detail/confirmation, send progress, status.json, report.csv
    "batches.manage": _MANAGERS,       # create, upload, match, confirm, remove, mark ready, reopen, delete
    "batches.send": _MANAGERS,         # send, stop, resume, retry
    "users.manage": frozenset({ADMIN}),
    "audit.view": frozenset({ADMIN}),
    "system.view": frozenset({ADMIN}),
}


def can(user: User | None, permission: str) -> bool:
    """True when the user's role has this permission. Unknown permissions and unknown roles are always refused."""
    return user is not None and user.role in PERMISSIONS.get(permission, frozenset())


def require(permission: str):
    """A route dependency: logs the user in (redirects when not), then refuses with 403 unless their role may do this."""
    if permission not in PERMISSIONS:
        raise ValueError(f"Unknown permission: {permission}")

    def dependency(request: Request, user: User = Depends(require_login), db: Session = Depends(get_db)) -> User:
        if not can(user, permission):
            from app.services import audit            # imported here: audit does not need the security package at import time

            audit.record(db, "permission_denied", user_id=user.id, details=f"permission={permission} method={request.method}")
            raise HTTPException(status_code=403, detail="Your role does not allow you to do that.")
        return user

    dependency.permission = permission               # lets the safety-net test see which permission a route needs
    return dependency
