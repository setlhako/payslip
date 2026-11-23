"""Payslip batches: list, create + upload, review/match, confirm. (Sending, PDF passwords and the webhook come later.)

Every route needs a login (router dependency) and every POST is CSRF-checked app-wide. Flash messages and audit
details contain counts and ids only: never file names, employee names, phone numbers or IDs.
"""
import json
import logging
import re
from dataclasses import dataclass, field

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.flash import flash
from app.models import Batch, Employee, Payslip, User
from app.models.enums import BatchStatus, MatchMethod, MatchStatus, PdfCheck, ProcessingState
from app.routers.auth import client_ip
from app.security.permissions import can
from app.security.auth import require_login
from app.security.permissions import require
from app.services import audit, batch_review, batch_storage, send_view
from app.services.batch_ingest import IngestResult, UploadRejected, ingest_pdfs, ingest_zip, start_processing
from app.services.batch_jobs import heal_if_interrupted, process_batch, queue_rematch
from app.services.phones import mask_phone
from app.services.readiness import STATUS_LABELS, STATUS_ORDER, build_context, display_status, problems, ready_blockers, summary
from app.templating import templates

router = APIRouter(prefix="/batches", dependencies=[Depends(require_login)])
log = logging.getLogger(__name__)

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec")
LABEL_RE = re.compile(r"^[\w .,'()/&+-]{3,60}$")


def get_session_factory():
    """The background job opens its own database session. Tests replace this with their in-memory factory."""
    return SessionLocal


def validate_label(raw: str) -> tuple[str, str | None]:
    """The pay month label, e.g. 'November 2026'. Needs a month name and a 4-digit year."""
    label = re.sub(r"\s+", " ", raw or "").strip()
    if not label:
        return label, "Enter the pay month, for example November 2026."
    if not LABEL_RE.match(label):
        return label, "Use 3 to 60 letters, numbers and spaces, for example November 2026."
    lowered = label.lower()
    year = re.search(r"\b(20\d\d|21\d\d)\b", label)
    if not year or not any(re.search(rf"\b{m}\b", lowered) for m in MONTHS):
        return label, "Include the month and the year, for example November 2026."
    return label, None


def _get_batch(db: Session, batch_id: int) -> Batch:
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(404)
    return batch


def _get_payslip(db: Session, batch: Batch, payslip_id: int) -> Payslip:
    p = db.get(Payslip, payslip_id)
    if p is None or p.batch_id != batch.id:
        raise HTTPException(404)
    return p


def _upload_summary(result: IngestResult) -> str:
    text = f"{result.accepted} file{'s' if result.accepted != 1 else ''} added"
    if result.invalid:
        text += f", {result.invalid} invalid (see the list)"
    return text + "."


def _ingest(db: Session, batch: Batch, mode: str, files: list[UploadFile], zip_file: UploadFile | None) -> IngestResult:
    if mode == "zip":
        return ingest_zip(db, batch, zip_file)
    if mode == "pdfs":
        return ingest_pdfs(db, batch, files)
    raise UploadRejected("Choose how you are uploading: PDF files or one ZIP.")


# ---- list --------------------------------------------------------------------------------------------------------

@dataclass
class BatchRow:
    batch: Batch
    files: int = 0
    matched: int = 0
    review: int = 0
    unmatched: int = 0
    invalid: int = 0
    delivery: dict | None = None      # status counts once the batch has been sent (for the little ribbon)

    @property
    def good(self) -> int:
        return (self.delivery["delivered"] + self.delivery["read"]) if self.delivery else 0

    @property
    def state(self) -> tuple[str, str]:
        b = self.batch
        if b.files_deleted_at:
            return "inactive", "Files deleted"
        if b.processing_state == ProcessingState.processing.value:
            return "update", "Matching"
        if b.status == BatchStatus.sending.value:
            return "update", "Sending"
        if b.status == BatchStatus.completed.value:
            return "new", "Sent"
        if b.status == BatchStatus.ready.value:
            return "new", "Ready to send"
        return "warning" if (self.review or self.unmatched or self.invalid) else "unchanged", "Draft"


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require("batches.view"))])
def list_batches(request: Request, db: Session = Depends(get_db), user: User = Depends(require_login)):
    batches = list(db.scalars(select(Batch).order_by(Batch.created_at.desc(), Batch.id.desc())))
    rows = {b.id: BatchRow(b) for b in batches}
    stmt = select(
        Payslip.batch_id, Payslip.match_status, Payslip.employee_id.is_not(None), Payslip.confirmed, func.count()
    ).group_by(Payslip.batch_id, Payslip.match_status, Payslip.employee_id.is_not(None), Payslip.confirmed)
    for batch_id, status, has_employee, confirmed, n in db.execute(stmt):
        row = rows.get(batch_id)
        if row is None:
            continue
        row.files += n
        if status == MatchStatus.invalid.value:
            row.invalid += n
        elif status == MatchStatus.unmatched.value and not has_employee:
            row.unmatched += n
        elif has_employee and confirmed:
            row.matched += n
        else:
            row.review += n          # needs review, possible mismatch (and files still being checked)
    sent_ids = {b.id for b in batches if b.status in (BatchStatus.sending.value, BatchStatus.completed.value)}
    if sent_ids:
        for batch_id, status, n in db.execute(select(Payslip.batch_id, Payslip.status, func.count()).where(Payslip.batch_id.in_(sent_ids))
                                              .group_by(Payslip.batch_id, Payslip.status)):
            row = rows[batch_id]
            row.delivery = row.delivery or {**{k: 0 for k in send_view.ORDER}, "total": 0}
            row.delivery[status] = row.delivery.get(status, 0) + n
            row.delivery["total"] += n
    return templates.TemplateResponse(request, "batches.html", {
        "current_user": user, "batches": [rows[b.id] for b in batches], "order": send_view.ORDER})


# ---- create ------------------------------------------------------------------------------------------------------

def _new_page(request, user, label="", mode="pdfs", error=None, label_error=None, status_code=200):
    s = get_settings()
    return templates.TemplateResponse(request, "batch_new.html", {
        "current_user": user, "label": label, "mode": mode, "error": error, "label_error": label_error,
        "limits": {"files": s.max_files_per_batch, "pdf_mb": s.max_upload_mb, "total_mb": s.max_batch_total_mb},
        "retention_days": s.batch_retention_days,
    }, status_code=status_code)


@router.get("/new", response_class=HTMLResponse, dependencies=[Depends(require("batches.manage"))])
def new_form(request: Request, user: User = Depends(require_login)):
    return _new_page(request, user)


@router.post("/new", response_class=HTMLResponse, dependencies=[Depends(require("batches.manage"))])
def new_submit(
    request: Request, background: BackgroundTasks, pay_month_label: str = Form(""), mode: str = Form("pdfs"),
    files: list[UploadFile] = File(default=[]), zip_file: UploadFile | None = File(default=None),
    db: Session = Depends(get_db), user: User = Depends(require_login), factory=Depends(get_session_factory),
):
    label, label_error = validate_label(pay_month_label)
    if label_error:
        return _new_page(request, user, label, mode, label_error=label_error, status_code=422)

    batch = Batch(pay_month_label=label, status=BatchStatus.draft.value, created_by_id=user.id)
    db.add(batch)
    db.flush()
    batch_id = batch.id
    try:
        result = _ingest(db, batch, mode, files, zip_file)
    except UploadRejected as exc:
        db.rollback()
        batch_storage.delete_batch_files(batch_id)
        return _new_page(request, user, label, mode, error=str(exc), status_code=400)
    except Exception as exc:
        db.rollback()
        batch_storage.delete_batch_files(batch_id)
        log.error("Batch upload failed: %s", type(exc).__name__)
        return _new_page(request, user, label, mode, error="The upload could not be processed. Nothing was saved.", status_code=500)

    ip = client_ip(request)
    audit.record(db, "batch_created", user_id=user.id, entity_type="batch", entity_id=batch_id, ip=ip, commit=False)
    audit.record(db, "batch_files_uploaded", user_id=user.id, entity_type="batch", entity_id=batch_id, ip=ip, commit=False,
                 details=f"accepted={result.accepted} invalid={result.invalid} ignored={result.ignored}")
    if result.accepted:
        start_processing(batch, result.accepted)
    db.commit()
    if result.accepted:
        background.add_task(process_batch, batch_id, factory)
    flash(request, "Batch created. " + _upload_summary(result), "success")
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


# ---- review page -------------------------------------------------------------------------------------------------

@dataclass
class RowView:
    p: Payslip
    status: str
    label: str
    employee: Employee | None
    phone: str
    problems: list
    suggestions: list = field(default_factory=list)      # [(Employee, score)]
    note: str = ""                                       # information only (never blocks)

    @property
    def needs_attention(self) -> bool:
        return bool(self.problems)


def _note(p: Payslip) -> str:
    if p.pdf_check == PdfCheck.no_text.value and p.employee_id:
        return "Could not verify against PDF content (no readable text, for example a scan)."
    if p.pdf_check == PdfCheck.name_not_found.value and p.employee_id:
        return "The employee's name was not found in the PDF text, so it could not be checked."
    if p.pdf_check == PdfCheck.possible_mismatch.value and p.confirmed:
        return "The PDF text names a different employee. Make sure this is the right match."
    return ""


def build_review(db: Session, batch: Batch):
    rows = list(db.scalars(select(Payslip).where(Payslip.batch_id == batch.id).order_by(Payslip.id)))
    wanted: set[int] = set()
    for p in rows:
        if p.employee_id:
            wanted.add(p.employee_id)
        if p.text_names_employee_id:
            wanted.add(p.text_names_employee_id)
        if p.suggestions:
            try:
                wanted.update(int(i) for i, _ in json.loads(p.suggestions))
            except (ValueError, TypeError):
                p.suggestions = None
    by_id = {e.id: e for e in db.scalars(select(Employee).where(Employee.id.in_(wanted)))} if wanted else {}
    ctx = build_context(rows, by_id)
    views: list[RowView] = []
    for p in rows:
        status = display_status(p)
        suggestions = []
        if p.employee_id is None and p.suggestions:
            for eid, score in json.loads(p.suggestions):
                e = by_id.get(int(eid))
                if e is not None and e.active:
                    suggestions.append((e, int(score)))
        employee = by_id.get(p.employee_id) if p.employee_id else None
        views.append(RowView(p, status, STATUS_LABELS[status], employee, mask_phone(employee.phone) if employee and employee.phone else "",
                             problems(p, ctx), suggestions, _note(p)))
    order = {k: i for i, k in enumerate((*STATUS_ORDER, "pending"))}
    views.sort(key=lambda v: (0 if v.problems else 1, order.get(v.status, 9), v.p.original_filename.lower()))
    return rows, views, ctx, by_id


@router.get("/{batch_id:int}", response_class=HTMLResponse, dependencies=[Depends(require("batches.view"))])
def batch_detail(
    request: Request, batch_id: int, show: str = "all", db: Session = Depends(get_db), user: User = Depends(require_login),
):
    batch = _get_batch(db, batch_id)
    if batch.status in (BatchStatus.sending.value, BatchStatus.completed.value):
        return templates.TemplateResponse(request, "batch_progress.html", {"current_user": user, **send_view.progress_context(db, batch)})
    heal_if_interrupted(db, batch)
    rows, views, ctx, by_id = build_review(db, batch)
    counts = summary(rows)
    matched_ids = {p.employee_id for p in rows if p.employee_id and p.match_status != MatchStatus.invalid.value}
    active = list(db.scalars(select(Employee).where(Employee.active.is_(True)).order_by(func.lower(Employee.surname), func.lower(Employee.first_name))))
    show = show if show in ("all", "problems") else "all"
    shown = [v for v in views if v.problems] if show == "problems" else views
    s = get_settings()
    editable = not (batch.files_deleted_at or batch.status != BatchStatus.draft.value
                    or batch.processing_state == ProcessingState.processing.value)
    return templates.TemplateResponse(request, "batch_detail.html", {
        "current_user": user, "batch": batch, "views": shown, "counts": counts, "show": show,
        "problem_count": sum(1 for v in views if v.problems), "total": len(views),
        "without_payslip": [e for e in active if e.id not in matched_ids], "active_employees": active,
        "blockers": ready_blockers(batch, rows, ctx), "editable": editable and can(user, "batches.manage"),
        "processing": batch.processing_state == ProcessingState.processing.value,
        "failed": batch.processing_state == ProcessingState.failed.value,
        "limits": {"files": s.max_files_per_batch, "pdf_mb": s.max_upload_mb, "total_mb": s.max_batch_total_mb},
        "mask_phone": mask_phone,
    })


@router.post("/{batch_id:int}/upload", dependencies=[Depends(require("batches.manage"))])
def add_files(
    request: Request, batch_id: int, background: BackgroundTasks, mode: str = Form("pdfs"),
    files: list[UploadFile] = File(default=[]), zip_file: UploadFile | None = File(default=None),
    db: Session = Depends(get_db), user: User = Depends(require_login), factory=Depends(get_session_factory),
):
    batch = _get_batch(db, batch_id)
    back = RedirectResponse(f"/batches/{batch_id}", status_code=303)
    try:
        batch_review.require_editable(batch)
        result = _ingest(db, batch, mode, files, zip_file)
    except (batch_review.ReviewError, UploadRejected) as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return back
    except Exception as exc:
        db.rollback()
        log.error("Adding files failed: %s", type(exc).__name__)
        flash(request, "The upload could not be processed. Nothing was added.", "error")
        return back
    audit.record(db, "batch_files_uploaded", user_id=user.id, entity_type="batch", entity_id=batch_id, ip=client_ip(request),
                 commit=False, details=f"accepted={result.accepted} invalid={result.invalid} ignored={result.ignored}")
    if result.accepted:
        start_processing(batch, result.accepted)
    db.commit()
    if result.accepted:
        background.add_task(process_batch, batch_id, factory)
    flash(request, _upload_summary(result), "success")
    return back


# ---- review actions ----------------------------------------------------------------------------------------------

def _act(request, db, user, batch_id, payslip_id, action, ok_message, **kwargs):
    batch = _get_batch(db, batch_id)
    p = _get_payslip(db, batch, payslip_id)
    try:
        action(db, batch, p, user_id=user.id, ip=client_ip(request), **kwargs)
        db.commit()
        flash(request, ok_message, "success")
    except batch_review.ReviewError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse(f"/batches/{batch_id}#file-{payslip_id}", status_code=303)


@router.post("/{batch_id:int}/payslips/{payslip_id:int}/match", dependencies=[Depends(require("batches.manage"))])
def change_match(
    request: Request, batch_id: int, payslip_id: int, employee_id: str = Form(""), employee_label: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    """`employee_id` (a suggestion button) or `employee_label` (text picked from the searchable list: '... (#12)')."""
    raw = employee_id.strip() or (re.search(r"\(#(\d+)\)\s*$", employee_label or "") or [None, ""])[1]
    if not raw.isdigit():
        flash(request, "Pick an employee from the list.", "error")
        return RedirectResponse(f"/batches/{batch_id}#file-{payslip_id}", status_code=303)
    return _act(request, db, user, batch_id, payslip_id, batch_review.set_match, "Match saved.", employee_id=int(raw))


@router.post("/{batch_id:int}/payslips/{payslip_id:int}/confirm", dependencies=[Depends(require("batches.manage"))])
def confirm(request: Request, batch_id: int, payslip_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    return _act(request, db, user, batch_id, payslip_id, batch_review.confirm_match, "Match confirmed.")


@router.post("/{batch_id:int}/payslips/{payslip_id:int}/remove", dependencies=[Depends(require("batches.manage"))])
def remove(request: Request, batch_id: int, payslip_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    batch = _get_batch(db, batch_id)
    p = _get_payslip(db, batch, payslip_id)
    try:
        batch_review.remove_file(db, batch, p, user_id=user.id, ip=client_ip(request))
        db.commit()
        flash(request, "File removed from the batch.", "success")
    except batch_review.ReviewError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@router.post("/{batch_id:int}/rematch", dependencies=[Depends(require("batches.manage"))])
def rematch(
    request: Request, batch_id: int, background: BackgroundTasks, db: Session = Depends(get_db),
    user: User = Depends(require_login), factory=Depends(get_session_factory),
):
    batch = _get_batch(db, batch_id)
    try:
        batch_review.require_editable(batch)
    except batch_review.ReviewError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/batches/{batch_id}", status_code=303)
    count = queue_rematch(db, batch)
    start_processing(batch, count)
    db.commit()
    background.add_task(process_batch, batch_id, factory)
    flash(request, "Matching started again. Choices you made by hand are kept.", "info")
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


# ---- confirmation ------------------------------------------------------------------------------------------------

@router.get("/{batch_id:int}/confirm", response_class=HTMLResponse, dependencies=[Depends(require("batches.view"))])
def confirm_page(request: Request, batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    batch = _get_batch(db, batch_id)
    heal_if_interrupted(db, batch)
    rows, views, ctx, by_id = build_review(db, batch)
    listing = sorted((v for v in views if v.status != "invalid"), key=lambda v: v.p.original_filename.lower())
    matched_ids = {v.employee.id for v in views if v.employee and v.status != "invalid"}
    without = db.scalar(select(func.count()).where(Employee.active.is_(True), Employee.id.not_in(matched_ids or {0}))) or 0
    return templates.TemplateResponse(request, "batch_confirm.html", {
        "current_user": user, "batch": batch, "listing": listing, "counts": summary(rows),
        "blockers": ready_blockers(batch, rows, ctx), "without_count": without,
    })


@router.post("/{batch_id:int}/ready", dependencies=[Depends(require("batches.manage"))])
def mark_ready(
    request: Request, batch_id: int, checked: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_login),
):
    batch = _get_batch(db, batch_id)
    try:
        batch_review.mark_ready(db, batch, checked == "on", user_id=user.id, ip=client_ip(request))
        db.commit()
        flash(request, "The batch is marked ready to send. Sending comes next.", "success")
    except batch_review.ReviewError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse(f"/batches/{batch_id}/confirm", status_code=303)


@router.post("/{batch_id:int}/reopen", dependencies=[Depends(require("batches.manage"))])
def reopen(request: Request, batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    batch = _get_batch(db, batch_id)
    try:
        batch_review.reopen(db, batch, user_id=user.id, ip=client_ip(request))
        db.commit()
        flash(request, "The batch is open for changes again.", "info")
    except batch_review.ReviewError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@router.post("/{batch_id:int}/delete", dependencies=[Depends(require("batches.manage"))])
def delete(request: Request, batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    batch = _get_batch(db, batch_id)
    try:
        batch_review.delete_batch(db, batch, user_id=user.id, ip=client_ip(request))
        db.commit()
    except batch_review.ReviewError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(f"/batches/{batch_id}", status_code=303)
    batch_storage.delete_batch_files(batch_id)       # after the database commit, so a failure never leaves a broken batch
    flash(request, "Batch deleted, including its files.", "success")
    return RedirectResponse("/batches", status_code=303)
