"""What HR can do to a batch. Every function checks the batch is editable, changes the session and writes the audit
entry (ids and counts only, never file names or personal data). The CALLER commits once (`db.commit()`).
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Batch, Employee, Payslip
from app.models.enums import BatchStatus, MatchMethod, MatchStatus, PdfCheck, ProcessingState
from app.models.mixins import utcnow
from app.services import audit, batch_storage
from app.services.pdf_check import check_text, extract_text, text_word_set
from app.services.readiness import build_context, ready_blockers


class ReviewError(Exception):
    """Something HR asked for is not allowed right now. The message is plain language."""


def require_editable(batch: Batch) -> None:
    if batch.files_deleted_at:
        raise ReviewError("The files of this batch were deleted after the retention period, so it can no longer be changed.")
    if batch.status == BatchStatus.ready.value:
        raise ReviewError("This batch is marked ready. Press Reopen to change it.")
    if batch.status != BatchStatus.draft.value:
        raise ReviewError("This batch can no longer be changed.")
    if batch.processing_state == ProcessingState.processing.value:
        raise ReviewError("Matching is still running. Please wait until it has finished.")


def _usable(p: Payslip) -> None:
    if p.match_status in (MatchStatus.invalid.value, MatchStatus.pending.value):
        raise ReviewError("This file cannot be matched (it is invalid or still being checked).")


def set_match(db: Session, batch: Batch, p: Payslip, employee_id: int, *, user_id: int, ip: str | None) -> None:
    """HR chooses the employee (changing a match or accepting a suggestion). Recorded as match_method='manual'."""
    require_editable(batch)
    _usable(p)
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise ReviewError("That employee was not found.")
    before = p.employee_id
    p.employee_id, p.match_method, p.confirmed = employee.id, MatchMethod.manual.value, True
    # the PDF text is checked again for the NEW employee; HR has decided, so a warning is shown but does not block
    path = batch_storage.stored_path(batch.id, p.stored_filename)
    active = list(db.scalars(select(Employee).where(Employee.active.is_(True))))
    check = check_text(text_word_set(extract_text(path) if path and path.exists() else None), employee, active)
    p.pdf_check, p.text_names_employee_id = check.result, check.other_employee_id
    audit.record(db, "batch_match_changed", user_id=user_id, entity_type="payslip", entity_id=p.id, ip=ip, commit=False,
                 details=f"batch={batch.id} from_employee={before} to_employee={employee.id}")


def confirm_match(db: Session, batch: Batch, p: Payslip, *, user_id: int, ip: str | None) -> None:
    require_editable(batch)
    _usable(p)
    if p.employee_id is None:
        raise ReviewError("Choose an employee for this file first.")
    p.confirmed = True
    audit.record(db, "batch_match_confirmed", user_id=user_id, entity_type="payslip", entity_id=p.id, ip=ip, commit=False,
                 details=f"batch={batch.id} employee={p.employee_id}")


def remove_file(db: Session, batch: Batch, p: Payslip, *, user_id: int, ip: str | None) -> None:
    require_editable(batch)
    batch_storage.delete_stored(batch.id, p.stored_filename)
    audit.record(db, "batch_file_removed", user_id=user_id, entity_type="payslip", entity_id=p.id, ip=ip, commit=False,
                 details=f"batch={batch.id}")
    db.delete(p)


def blockers_for(db: Session, batch: Batch) -> list[str]:
    rows = list(db.scalars(select(Payslip).where(Payslip.batch_id == batch.id)))
    ids = {p.employee_id for p in rows if p.employee_id} | {p.text_names_employee_id for p in rows if p.text_names_employee_id}
    employees = {e.id: e for e in db.scalars(select(Employee).where(Employee.id.in_(ids)))} if ids else {}
    return ready_blockers(batch, rows, build_context(rows, employees))


def mark_ready(db: Session, batch: Batch, checked: bool, *, user_id: int, ip: str | None) -> None:
    blockers = blockers_for(db, batch)
    if blockers:
        raise ReviewError(blockers[0])
    if not checked:
        raise ReviewError("Please tick \"I have checked the matches\" first.")
    batch.status, batch.ready_at = BatchStatus.ready.value, utcnow()
    count = db.scalar(select(func.count()).where(Payslip.batch_id == batch.id)) or 0
    audit.record(db, "batch_marked_ready", user_id=user_id, entity_type="batch", entity_id=batch.id, ip=ip, commit=False,
                 details=f"files={count}")


def reopen(db: Session, batch: Batch, *, user_id: int, ip: str | None) -> None:
    if batch.status != BatchStatus.ready.value:
        raise ReviewError("This batch is not marked ready.")
    batch.status, batch.ready_at = BatchStatus.draft.value, None
    audit.record(db, "batch_reopened", user_id=user_id, entity_type="batch", entity_id=batch.id, ip=ip, commit=False)


def delete_batch(db: Session, batch: Batch, *, user_id: int, ip: str | None) -> None:
    """Deleting (or cancelling) a batch removes its files from disk as well. Not allowed once sending has started."""
    if batch.status in (BatchStatus.sending.value, BatchStatus.completed.value):
        raise ReviewError("A batch that is being sent or was sent cannot be deleted here.")
    count = db.scalar(select(func.count()).where(Payslip.batch_id == batch.id)) or 0
    audit.record(db, "batch_deleted", user_id=user_id, entity_type="batch", entity_id=batch.id, ip=ip, commit=False,
                 details=f"files={count}")
    db.delete(batch)
