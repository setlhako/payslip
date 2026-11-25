"""Sending a ready batch: confirmation, start, stop, resume, retries, live progress (JSON) and the CSV report.

Login + CSRF on everything (router dependency + the app-wide CSRF check). Flash messages and audit entries hold counts only.
"""
import logging
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.flash import flash
from app.models import Batch, Employee, Payslip, User
from app.models.enums import BatchStatus, PayslipStatus
from app.routers.auth import client_ip
from app.routers.batches import get_session_factory
from app.security.auth import require_login
from app.security.permissions import require
from app.services import audit, sender, send_view
from app.services.whatsapp import payslip_filename
from app.templating import templates

router = APIRouter(prefix="/batches", dependencies=[Depends(require_login)])
log = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


def get_send_launcher(factory=Depends(get_session_factory)) -> sender.SendLauncher:
    """Starts the background worker. Tests replace this with an InlineLauncher that runs it right away."""
    return sender.SendLauncher(factory)


def _batch(db: Session, batch_id: int) -> Batch:
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(404)
    return batch


def _back(batch_id: int) -> RedirectResponse:
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


SAMPLE_NAME = "Thabo Molefe"      # a made-up person: the preview never shows a real employee


def message_preview(batch: Batch, settings) -> dict:
    """Exactly what an employee will get, with a SAMPLE name. Template mode: the wording from WHATSAPP_TEMPLATE_PREVIEW_TEXT
    (WhatsApp sends the approved template itself, so that text must match it). Document mode: the caption is the pay month."""
    parts = []
    if settings.whatsapp_send_kind == "template":
        for piece in re.split(r"(\{\{\s*[12]\s*\}\})", settings.whatsapp_template_preview_text):
            if re.fullmatch(r"\{\{\s*1\s*\}\}", piece):
                parts.append(("var", SAMPLE_NAME))
            elif re.fullmatch(r"\{\{\s*2\s*\}\}", piece):
                parts.append(("var", batch.pay_month_label))
            elif piece:
                parts.append(("text", piece))
    else:
        parts.append(("var", batch.pay_month_label))
    return {"parts": parts, "filename": payslip_filename(batch.pay_month_label), "kind": settings.whatsapp_send_kind,
            "sample_name": SAMPLE_NAME}


@router.get("/{batch_id:int}/send", response_class=HTMLResponse, dependencies=[Depends(require("batches.send"))])
def send_confirmation(request: Request, batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    batch = _batch(db, batch_id)
    if batch.status != BatchStatus.ready.value:
        flash(request, "Only a batch that is marked ready can be sent.", "error")
        return _back(batch_id)
    settings = get_settings()
    rows = list(db.scalars(select(Payslip).where(Payslip.batch_id == batch.id)))
    phones = {e.id: e.phone for e in db.scalars(select(Employee).where(Employee.id.in_({p.employee_id for p in rows if p.employee_id})))}
    allowed = settings.allowed_recipients
    blocked = sum(1 for p in rows if settings.whatsapp_mode == "live" and allowed and phones.get(p.employee_id) not in allowed)
    active = db.scalar(select(func.count()).where(Employee.active.is_(True))) or 0
    return templates.TemplateResponse(request, "batch_send.html", {
        "current_user": user, "batch": batch, "files": len(rows), "without_count": max(0, active - len({p.employee_id for p in rows})),
        "mode": settings.whatsapp_mode, "allowed_count": len(allowed), "blocked_count": blocked,
        "send_kind": settings.whatsapp_send_kind, "template_name": settings.whatsapp_template_name,
        "preview": message_preview(batch, settings), "language": settings.whatsapp_template_language,
    })


@router.post("/{batch_id:int}/send", dependencies=[Depends(require("batches.send"))])
def send(
    request: Request, batch_id: int, confirm_label: str = Form(""), db: Session = Depends(get_db),
    user: User = Depends(require_login), launcher=Depends(get_send_launcher),
):
    batch = _batch(db, batch_id)
    try:
        count = sender.start_send(db, batch, confirm_label, mode=get_settings().whatsapp_mode, user_id=user.id, ip=client_ip(request))
        db.commit()
    except sender.SendError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(f"/batches/{batch_id}/send", status_code=303)
    launcher.start(batch_id)
    flash(request, f"Sending started: {count} payslip{'s' if count != 1 else ''} queued.", "success")
    return _back(batch_id)


def _run(request, db, user, batch_id, action, ok_message, launcher=None, **kwargs):
    batch = _batch(db, batch_id)
    try:
        count = action(db, batch, user_id=user.id, ip=client_ip(request), **kwargs)
        db.commit()
    except sender.SendError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back(batch_id)
    if launcher is not None:
        launcher.start(batch_id)
    flash(request, ok_message.format(n=count), "success")
    return _back(batch_id)


@router.post("/{batch_id:int}/stop", dependencies=[Depends(require("batches.send"))])
def stop(request: Request, batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    return _run(request, db, user, batch_id, sender.stop_send, "Stopped. {n} queued payslip(s) were put back; ones already being sent finish normally.")


@router.post("/{batch_id:int}/resume", dependencies=[Depends(require("batches.send"))])
def resume(request: Request, batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_login),
           launcher=Depends(get_send_launcher)):
    return _run(request, db, user, batch_id, sender.resume_send, "Resumed: {n} payslip(s) queued.", launcher)


@router.post("/{batch_id:int}/retry-failed", dependencies=[Depends(require("batches.send"))])
def retry_failed(request: Request, batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_login),
                 launcher=Depends(get_send_launcher)):
    return _run(request, db, user, batch_id, sender.retry_failed, "Retrying {n} failed payslip(s).", launcher)


@router.post("/{batch_id:int}/payslips/{payslip_id:int}/retry", dependencies=[Depends(require("batches.send"))])
def retry_one(request: Request, batch_id: int, payslip_id: int, db: Session = Depends(get_db),
              user: User = Depends(require_login), launcher=Depends(get_send_launcher)):
    p = db.get(Payslip, payslip_id)
    if p is None or p.batch_id != batch_id:
        raise HTTPException(404)
    return _run(request, db, user, batch_id, sender.retry_failed, "Retrying {n} payslip.", launcher, only_id=payslip_id)


@router.post("/{batch_id:int}/retry-uncertain", dependencies=[Depends(require("batches.send"))])
def retry_uncertain(
    request: Request, batch_id: int, payslip_ids: list[int] = Form(default=[]), understood: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require_login), launcher=Depends(get_send_launcher),
):
    return _run(request, db, user, batch_id, sender.retry_uncertain, "Retrying {n} uncertain payslip(s). They may be delivered twice.",
                launcher, ids=payslip_ids, understood=understood == "on")


@router.get("/{batch_id:int}/status.json", dependencies=[Depends(require("batches.view"))])
def status_json(batch_id: int, db: Session = Depends(get_db)):
    return JSONResponse(send_view.status_payload(db, _batch(db, batch_id)), headers=NO_STORE)


@router.get("/{batch_id:int}/report.csv", dependencies=[Depends(require("batches.view"))])
def report(request: Request, batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    batch = _batch(db, batch_id)
    if batch.status not in (BatchStatus.sending.value, BatchStatus.completed.value):
        flash(request, "This batch has not been sent yet, so there is no report.", "error")
        return _back(batch_id)
    body = send_view.report_csv(db, batch)
    audit.record(db, "report_downloaded", user_id=user.id, entity_type="batch", entity_id=batch.id, ip=client_ip(request),
                 details=f"rows={body.count(chr(10)) - 1}")
    return Response(body, media_type="text/csv; charset=utf-8", headers={
        **NO_STORE, "Content-Disposition": f'attachment; filename="send-report-{batch.id}.csv"'})
