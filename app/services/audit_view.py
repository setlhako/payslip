"""Reading the audit log for the admin page and its CSV export: filters, search, paging, plain-language labels.

Audit entries are written without ID numbers, tokens or full phone numbers. This module ALSO scrubs what it shows or exports
(long digit runs and token-looking strings are masked), so even a mistake elsewhere cannot leak through this page.
"""
import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import AuditLog, User

PER_PAGE = 50
EXPORT_LIMIT = 50_000
_DIGITS = re.compile(r"\d{7,}")
_TOKEN = re.compile(r"\b(?:EAA[A-Za-z0-9]{8,}|sha256=[0-9a-f]{16,}|[A-Za-z0-9_\-]{32,})\b")

LABELS = {
    "login_success": "Signed in", "login_failure": "Failed sign-in", "login_lockout": "Locked out after failed sign-ins", "logout": "Signed out",
    "admin_created": "Admin created (command line)", "admin_password_reset": "Password reset (command line)",
    "password_changed": "Changed own password", "permission_denied": "Blocked: not allowed for this role",
    "user_created": "User created", "user_updated": "User changed", "user_disabled": "User disabled", "user_enabled": "User enabled",
    "user_password_reset": "Temporary password issued", "user_unlocked": "User unlocked", "user_sessions_revoked": "Signed user out everywhere",
    "employee_created": "Employee added", "employee_edited": "Employee edited", "employee_deactivated": "Employee deactivated",
    "employee_activated": "Employee reactivated", "employee_edit_opened": "Employee edit page opened", "employees_import_confirmed": "Employees imported",
    "ids_revealed": "Full ID numbers shown", "id_keys_rotated": "ID encryption key rotated",
    "batch_created": "Batch created", "batch_files_uploaded": "Files uploaded", "batch_match_changed": "Match changed", "batch_match_confirmed": "Match confirmed",
    "batch_file_removed": "File removed", "batch_marked_ready": "Batch marked ready", "batch_reopened": "Batch reopened", "batch_deleted": "Batch deleted",
    "batch_files_purged": "Old files purged", "send_started": "Sending started", "batch_stopped": "Sending stopped", "send_resumed": "Sending resumed",
    "retry_clicked": "Retry clicked", "send_token_error": "WhatsApp token rejected", "report_downloaded": "Send report downloaded",
    "webhook_rejected": "Webhook rejected (bad signature)", "audit_exported": "Audit log exported",
}


def label(action: str) -> str:
    return LABELS.get(action) or action.replace("_", " ").capitalize()


def scrub(text: str | None) -> str:
    out = _DIGITS.sub("***", text or "")
    return _TOKEN.sub("[hidden]", out)


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@dataclass
class Filters:
    date_from: date | None = None
    date_to: date | None = None
    user: str = ""            # "" = everyone, "system" = no user, or a user id
    action: str = ""
    q: str = ""
    page: int = 1

    def params(self, **changes) -> dict[str, str]:
        """Query-string values for links that keep the filters (empty ones left out)."""
        values = {"date_from": self.date_from.isoformat() if self.date_from else "", "date_to": self.date_to.isoformat() if self.date_to else "",
                  "user": self.user, "action": self.action, "q": self.q, "page": str(self.page)}
        values.update({k: str(v) for k, v in changes.items()})
        return {k: v for k, v in values.items() if v and not (k == "page" and v == "1")}


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat((text or "").strip())
    except ValueError:
        return None


def parse_filters(params) -> tuple[Filters, list[str]]:
    problems = []
    raw_from, raw_to = params.get("date_from", ""), params.get("date_to", "")
    f = Filters(date_from=_date(raw_from), date_to=_date(raw_to), q=(params.get("q") or "").strip()[:100],
                action=(params.get("action") or "").strip()[:50])
    if raw_from.strip() and f.date_from is None:
        problems.append("The 'from' date was not understood (use year-month-day), so it was ignored.")
    if raw_to.strip() and f.date_to is None:
        problems.append("The 'to' date was not understood (use year-month-day), so it was ignored.")
    if f.date_from and f.date_to and f.date_from > f.date_to:
        f.date_from, f.date_to = f.date_to, f.date_from
        problems.append("The dates were the wrong way round, so they were swapped.")
    user = (params.get("user") or "").strip()
    f.user = user if user == "system" or user.isdigit() else ""
    try:
        f.page = max(1, int(params.get("page") or 1))
    except ValueError:
        f.page = 1
    return f, problems


def _query(f: Filters):
    stmt = select(AuditLog, User.username).outerjoin(User, User.id == AuditLog.user_id)
    if f.date_from:
        stmt = stmt.where(AuditLog.created_at >= datetime.combine(f.date_from, datetime.min.time()))
    if f.date_to:
        stmt = stmt.where(AuditLog.created_at < datetime.combine(f.date_to + timedelta(days=1), datetime.min.time()))
    if f.user == "system":
        stmt = stmt.where(AuditLog.user_id.is_(None))
    elif f.user:
        stmt = stmt.where(AuditLog.user_id == int(f.user))
    if f.action:
        stmt = stmt.where(AuditLog.action == f.action)
    if f.q:
        pattern = _like(f.q)
        stmt = stmt.where(or_(AuditLog.action.ilike(pattern, escape="\\"), AuditLog.details.ilike(pattern, escape="\\"),
                              AuditLog.entity_type.ilike(pattern, escape="\\"), User.username.ilike(pattern, escape="\\")))
    return stmt.order_by(AuditLog.id.desc())


def _row(entry: AuditLog, username: str | None) -> dict:
    target = f"{entry.entity_type} #{entry.entity_id}" if entry.entity_type and entry.entity_id else (entry.entity_type or "")
    return {"when": entry.created_at, "user": username or ("system" if entry.user_id is None else f"user #{entry.user_id}"),
            "action": entry.action, "label": label(entry.action), "target": target, "details": scrub(entry.details),
            "ip": entry.ip_address or ""}


def page_rows(db: Session, f: Filters) -> tuple[list[dict], int, int]:
    """(rows for this page, total matching, number of pages). The page number is clamped to the real range."""
    stmt = _query(f)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    pages = max(1, -(-total // PER_PAGE))
    f.page = min(f.page, pages)
    rows = db.execute(stmt.limit(PER_PAGE).offset((f.page - 1) * PER_PAGE)).all()
    return [_row(e, name) for e, name in rows], total, pages


def known_actions(db: Session) -> list[tuple[str, str]]:
    names = db.scalars(select(AuditLog.action).distinct().order_by(AuditLog.action))
    return sorted(((a, label(a)) for a in names), key=lambda t: t[1].lower())


def known_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(func.lower(User.username))))


def _safe_cell(text: str) -> str:
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text      # spreadsheet formula guard


def export_csv(db: Session, f: Filters) -> tuple[str, int]:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["When (UTC)", "User", "What happened", "Action code", "About", "Details", "Address (masked)"])
    count = 0
    for entry, username in db.execute(_query(f).limit(EXPORT_LIMIT)):
        r = _row(entry, username)
        writer.writerow([r["when"].strftime("%Y-%m-%d %H:%M:%S"), _safe_cell(r["user"]), r["label"], r["action"], r["target"],
                         _safe_cell(r["details"]), r["ip"]])
        count += 1
    return out.getvalue(), count
