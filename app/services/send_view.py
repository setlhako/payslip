"""What the progress screen, the polling endpoint and the CSV report show. Read-only; no personal data beyond what the
screen already shows to the logged-in admin (employee name, masked phone). Never ID numbers, never full phone numbers."""
import csv
import io
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Batch, Employee, Payslip
from app.models.enums import SendState
from app.services.phones import mask_phone
from app.services.whatsapp_errors import explain

ORDER = ("pending", "queued", "sending", "sent", "delivered", "read", "failed", "uncertain")
LABELS = {"pending": "Not sent", "queued": "Queued", "sending": "Sending", "sent": "Sent", "delivered": "Delivered",
          "read": "Read", "failed": "Failed", "uncertain": "Uncertain"}
ACTIVE = ("queued", "sending")


def current_mode() -> str:
    return get_settings().whatsapp_mode


def detail_for(p: Payslip) -> str:
    """The plain-language explanation for a failed / uncertain payslip (empty otherwise)."""
    if p.status in ("failed", "uncertain") and (p.error_code or p.error):
        return explain(p.error_code, p.error)
    return ""


def load_rows(db: Session, batch: Batch) -> list[tuple[Payslip, Employee | None]]:
    rows = list(db.scalars(select(Payslip).where(Payslip.batch_id == batch.id)))
    ids = {p.employee_id for p in rows if p.employee_id}
    people = {e.id: e for e in db.scalars(select(Employee).where(Employee.id.in_(ids)))} if ids else {}
    pairs = [(p, people.get(p.employee_id)) for p in rows]
    pairs.sort(key=lambda t: ((t[1].surname.lower(), t[1].first_name.lower()) if t[1] else ("", ""), t[0].id))
    return pairs


def counts(rows) -> dict[str, int]:
    out = {k: 0 for k in ORDER}
    for p, _ in rows:
        out[p.status] = out.get(p.status, 0) + 1
    out["total"] = len(rows)
    return out


def is_active(batch: Batch, c: dict[str, int]) -> bool:
    return batch.send_state == SendState.running.value or bool(c["queued"] or c["sending"])


def status_payload(db: Session, batch: Batch) -> dict:
    rows = load_rows(db, batch)
    c = counts(rows)
    return {
        "batch": {"status": batch.status, "send_state": batch.send_state, "halted_reason": batch.halted_reason or "",
                  "mode": batch.send_mode or current_mode(), "active": is_active(batch, c)},
        "counts": c,
        "rows": [{"id": p.id, "status": p.status, "label": LABELS.get(p.status, p.status), "detail": detail_for(p)} for p, _ in rows],
    }


def _stamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _safe(cell: str) -> str:
    """Spreadsheet formula guard: a cell starting with = + - @ would be run as a formula by Excel."""
    return "'" + cell if cell[:1] in ("=", "+", "-", "@") else cell


def report_csv(db: Session, batch: Batch) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Employee", "Phone (last 3 digits)", "Status", "Sent at (UTC)", "Delivered at (UTC)", "Read at (UTC)",
                     "Error code", "What happened"])
    for p, e in load_rows(db, batch):
        writer.writerow([
            _safe(e.full_name) if e else "", mask_phone(e.phone) if e and e.phone else "", LABELS.get(p.status, p.status),
            _stamp(p.sent_at), _stamp(p.delivered_at), _stamp(p.read_at), p.error_code or "", _safe(detail_for(p)),
        ])
    return out.getvalue()


def progress_context(db: Session, batch: Batch) -> dict:
    """Everything batch_progress.html needs."""
    rows = load_rows(db, batch)
    c = counts(rows)
    return {
        "batch": batch, "counts": c, "labels": LABELS, "order": ORDER, "active": is_active(batch, c),
        "mode": batch.send_mode or current_mode(),
        "rows": [{"p": p, "e": e, "phone": mask_phone(e.phone) if e and e.phone else "", "detail": detail_for(p)} for p, e in rows],
    }
