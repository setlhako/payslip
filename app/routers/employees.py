import csv
import io
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.flash import flash
from app.models import Employee, User
from app.routers.auth import client_ip
from app.security.auth import require_login
from app.security.permissions import require
from app.services import audit, id_reveal
from app.services.employees import (
    DEFAULT_PER_PAGE, FILTERS, PER_PAGE_CHOICES, SORTS, FormResult, apply_values, export_rows, filter_counts, last_payslips,
    search_employees, status_counts, validate_form,
)
from app.services.import_parser import MAX_FILE_BYTES, ImportFileError, parse_upload
from app.services.import_planner import ERROR, NEW, UPDATE, apply_import, plan_import, summarize
from app.services.import_preview import store
from app.services.import_template import build_template
from app.templating import templates

router = APIRouter(prefix="/employees", dependencies=[Depends(require_login)])
log = logging.getLogger(__name__)

IMPORT_KEY = "import_token"
IMPORT_RESULT_KEY = "import_result"   # counts only (no personal data), shown on the "Done" step
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _int(text: str, default: int = 1) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def _get_employee(db: Session, employee_id: int) -> Employee:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404)
    return employee


# ---- list --------------------------------------------------------------------------------------------------------

def _clean_list_params(q, status, page, per_page, sort, direction, show=""):
    """The list settings from the URL (or the reveal form), cleaned. One place, so the list and the reveal agree."""
    q = (q or "").strip()[:100]
    status = status if status in ("active", "inactive", "all") else "active"
    sort = sort if sort in SORTS else "name"
    direction = direction if direction in ("asc", "desc") else "asc"
    per_page_n = _int(per_page, DEFAULT_PER_PAGE)
    per_page_n = per_page_n if per_page_n in PER_PAGE_CHOICES else DEFAULT_PER_PAGE
    show = show if show in FILTERS else ""
    return q, status, _int(page), per_page_n, sort, direction, show


def _list_url(q="", status="active", sort="name", direction="asc", per_page=DEFAULT_PER_PAGE, show="", page=1) -> str:
    """The list URL for these settings (defaults left out for tidy links)."""
    params = {"q": q, "status": status, "sort": sort, "dir": direction, "per_page": per_page, "show": show, "page": page}
    defaults = {"q": "", "status": "active", "sort": "name", "dir": "asc", "per_page": DEFAULT_PER_PAGE, "show": "", "page": 1}
    params = {k: v for k, v in params.items() if v != defaults[k]}
    return "/employees" + ("?" + urlencode(params) if params else "")


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require("employees.view"))])
def list_employees(
    request: Request, q: str = "", status: str = "active", page: str = "1", per_page: str = str(DEFAULT_PER_PAGE),
    sort: str = "name", direction: str = Query("asc", alias="dir"), show: str = "",
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    q, status, page_n, per_page_n, sort, direction, show = _clean_list_params(q, status, page, per_page, sort, direction, show)

    result = search_employees(db, q, status, page_n, per_page_n, sort, direction, show)
    counts = status_counts(db, q, show)
    anything_saved = bool(status_counts(db, "")["all"])
    recent = last_payslips(db, [e.id for e in result.items])

    def url(**changes) -> str:
        """The list URL with some settings changed (everything else kept; defaults left out for tidy links)."""
        params = {"q": q, "status": status, "sort": sort, "direction": direction, "per_page": per_page_n, "show": show, "page": 1}
        params.update({("direction" if k == "dir" else k): v for k, v in changes.items()})
        return _list_url(**params)

    def sort_link(column: str) -> str:
        flipped = "desc" if sort == column and direction == "asc" else "asc"
        return url(sort=column, dir=flipped, page=1)

    return templates.TemplateResponse(request, "employees.html", {
        "current_user": user, "result": result, "counts": counts, "q": q, "status": status, "sort": sort,
        "direction": direction, "per_page": per_page_n, "per_page_choices": PER_PAGE_CHOICES, "show": show,
        "filters": FILTERS, "filter_counts": filter_counts(db, q, status), "recent": recent, "month_status": MONTH_STATUS_LABELS,
        "anything_saved": anything_saved, "url": url, "sort_link": sort_link,
        "export_url": "/employees/export.csv" + _export_query(q, status, sort, direction, show),
        "aria_sort": lambda column: ("ascending" if direction == "asc" else "descending") if sort == column else "none",
    })


def _export_query(q, status, sort, direction, show) -> str:
    params = {k: v for k, v in {"q": q, "status": status, "sort": sort, "dir": direction, "show": show}.items()
              if v and v != {"status": "active", "sort": "name", "dir": "asc"}.get(k)}
    return "?" + urlencode(params) if params else ""


MONTH_STATUS_LABELS = {"queued": "Queued", "sending": "Sending", "sent": "Sent", "delivered": "Delivered", "read": "Read",
                       "failed": "Failed", "uncertain": "Uncertain"}


# ---- export (names, phones and status only: never an ID number) ---------------------------------------------------

def _csv_cell(value: str) -> str:
    """Spreadsheet formula guard: a cell starting with = + - @ would be run as a formula by Excel."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


@router.get("/export.csv", dependencies=[Depends(require("employees.view"))])
def export_list(
    request: Request, q: str = "", status: str = "active", sort: str = "name", direction: str = Query("asc", alias="dir"),
    show: str = "", db: Session = Depends(get_db), user: User = Depends(require_login),
):
    q, status, _, _, sort, direction, show = _clean_list_params(q, status, 1, DEFAULT_PER_PAGE, sort, direction, show)
    people = export_rows(db, q, status, show, sort, direction)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["First name", "Surname", "Phone", "Status"])
    for e in people:
        writer.writerow([_csv_cell(e.first_name), _csv_cell(e.surname), e.phone or "", "Active" if e.active else "Inactive"])
    audit.record(db, "employees_exported", user_id=user.id, details=f"rows={len(people)}", ip=client_ip(request))
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="employees.csv"', **NO_STORE})


# ---- bulk deactivate -------------------------------------------------------------------------------------------------

BULK_LIMIT = 200


@router.post("/bulk-deactivate", dependencies=[Depends(require("employees.edit"))])
def bulk_deactivate(
    request: Request, employee_ids: list[str] = Form([]), q: str = Form(""), status: str = Form("active"),
    sort: str = Form("name"), direction: str = Form("asc", alias="dir"), per_page: str = Form(str(DEFAULT_PER_PAGE)),
    page: str = Form("1"), show: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    q, status, page_n, per_page_n, sort, direction, show = _clean_list_params(q, status, page, per_page, sort, direction, show)
    back = RedirectResponse(_list_url(q, status, sort, direction, per_page_n, show, page_n), status_code=303)
    ids = sorted({_int(i, 0) for i in employee_ids[:BULK_LIMIT * 2]} - {0})
    if not ids:
        flash(request, "Tick at least one employee first.", "error")
        return back
    if len(ids) > BULK_LIMIT:
        flash(request, f"Select at most {BULK_LIMIT} employees at a time.", "error")
        return back
    changed = 0
    for employee in db.scalars(select(Employee).where(Employee.id.in_(ids))):
        if employee.active:
            employee.active = False
            changed += 1
            audit.record(db, "employee_deactivated", user_id=user.id, entity_type="employee", entity_id=employee.id,
                         details="bulk", ip=client_ip(request), commit=False)
    db.commit()
    flash(request, f"{changed} employee{'' if changed == 1 else 's'} deactivated. Nothing is deleted, so this can be undone."
                   if changed else "Those employees were already inactive.", "success" if changed else "info")
    return back


# ---- reveal full IDs ---------------------------------------------------------------------------------------------

NO_STORE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


@router.post("/reveal-ids", dependencies=[Depends(require("ids.reveal"))])
def reveal_ids(
    request: Request, employee_id: str = Form(""), q: str = Form(""), status: str = Form("active"),
    page: str = Form("1"), per_page: str = Form(str(DEFAULT_PER_PAGE)), sort: str = Form("name"),
    direction: str = Form("asc", alias="dir"), show: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    """Full ID numbers for the employees on ONE list page (the same settings the page was drawn with), or for one
    employee (the edit page). CSRF-checked app-wide, login required, rate-limited, audited, never cached."""
    if id_reveal.is_rate_limited(db, user):
        return JSONResponse({"error": "Too many requests. Please wait a minute and try again."}, 429, headers=NO_STORE)

    single = None
    if employee_id.strip():
        single = db.get(Employee, _int(employee_id, 0))
        if single is None:
            raise HTTPException(404)
        employees = [single]
    else:
        employees = search_employees(db, *_clean_list_params(q, status, page, per_page, sort, direction, show)).items

    ids = id_reveal.reveal(db, user, employees, client_ip(request), entity_id=single.id if single else None)
    return JSONResponse({"ids": {str(k): v for k, v in ids.items()}}, headers=NO_STORE)


# ---- add / edit / (de)activate -----------------------------------------------------------------------------------

def _form_page(request, user, employee, result: FormResult, ack="", status_code=200):
    return templates.TemplateResponse(request, "employee_form.html", {
        "current_user": user, "employee": employee, "form": result.shown, "errors": result.errors,
        "warnings": result.warnings, "ack": ack,
    }, status_code=status_code)


def _blank_result() -> FormResult:
    return FormResult(shown={"first_name": "", "surname": "", "phone": "", "id_type": "", "employee_code": ""})


def _existing_result(employee: Employee) -> FormResult:
    return FormResult(shown={
        "first_name": employee.first_name, "surname": employee.surname, "phone": employee.phone or "",
        "id_type": employee.id_type or "", "employee_code": employee.employee_code or "",
    })


def _save_form(request, db, user, employee, first_name, surname, phone, id_type, id_number, employee_code, ack,
               remove_id=""):
    raw = {"first_name": first_name, "surname": surname, "phone": phone, "id_type": id_type,
           "id_number": id_number, "employee_code": employee_code, "remove_id": remove_id}
    result = validate_form(db, raw, employee)
    if result.errors:
        return _form_page(request, user, employee, result, status_code=422)
    if result.warnings and ack != result.fingerprint:
        return _form_page(request, user, employee, result, ack=result.fingerprint)  # show warnings, ask to confirm

    creating = employee is None
    if creating:
        employee = Employee(active=True)
    v = result.values
    apply_values(employee, v["first_name"], v["surname"], v["phone"], v["id_type"], v["id_last4"], v["employee_code"],
                 id_number_encrypted=v["id_number_encrypted"], id_number_hash=v["id_number_hash"])
    if creating:
        db.add(employee)
    try:
        db.commit()
    except IntegrityError:
        # Someone else saved the same phone / ID / code between our check and our save. The database constraint wins:
        # nothing was saved. Check again so the message names the person who got there first.
        db.rollback()
        log.warning("Employee save refused by a database uniqueness rule")
        if not creating:
            db.refresh(employee)
        again = validate_form(db, raw, employee if not creating else None)
        if not again.errors:
            again.errors["phone"] = "This phone number, ID number or employee code already belongs to another employee."
        return _form_page(request, user, employee if not creating else None, again, status_code=422)
    audit.record(db, "employee_created" if creating else "employee_edited", user_id=user.id,
                 entity_type="employee", entity_id=employee.id, ip=client_ip(request))
    flash(request, "Employee added." if creating else "Changes saved.", "success")
    return RedirectResponse("/employees", status_code=303)


@router.get("/new", response_class=HTMLResponse, dependencies=[Depends(require("employees.edit"))])
def new_form(request: Request, user: User = Depends(require_login)):
    return _form_page(request, user, None, _blank_result())


@router.post("/new", response_class=HTMLResponse, dependencies=[Depends(require("employees.edit"))])
def new_submit(
    request: Request, first_name: str = Form(""), surname: str = Form(""), phone: str = Form(""),
    id_type: str = Form(""), id_number: str = Form(""), employee_code: str = Form(""), ack: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    return _save_form(request, db, user, None, first_name, surname, phone, id_type, id_number, employee_code, ack)


@router.get("/{employee_id:int}/edit", response_class=HTMLResponse, dependencies=[Depends(require("employees.edit"))])
def edit_form(request: Request, employee_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    employee = _get_employee(db, employee_id)
    # Opening the edit form puts the stored ID last-4 on the page, so it is recorded (employee id only).
    audit.record(db, "employee_edit_opened", user_id=user.id, entity_type="employee", entity_id=employee.id,
                 ip=client_ip(request))
    return _form_page(request, user, employee, _existing_result(employee))


@router.post("/{employee_id:int}/edit", response_class=HTMLResponse, dependencies=[Depends(require("employees.edit"))])
def edit_submit(
    request: Request, employee_id: int, first_name: str = Form(""), surname: str = Form(""), phone: str = Form(""),
    id_type: str = Form(""), id_number: str = Form(""), employee_code: str = Form(""), ack: str = Form(""),
    remove_id: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(require_login),
):
    employee = _get_employee(db, employee_id)
    return _save_form(request, db, user, employee, first_name, surname, phone, id_type, id_number, employee_code, ack,
                      remove_id)


def _set_active(request, db, user, employee_id: int, active: bool):
    employee = _get_employee(db, employee_id)
    if employee.active != active:
        employee.active = active
        db.commit()
        audit.record(db, "employee_activated" if active else "employee_deactivated", user_id=user.id,
                     entity_type="employee", entity_id=employee.id, ip=client_ip(request))
    flash(request, f"{employee.full_name} is now {'active' if active else 'inactive'}. "
                   "Nothing is ever deleted, so this can be undone.", "success")
    return RedirectResponse("/employees", status_code=303)


@router.post("/{employee_id:int}/deactivate", dependencies=[Depends(require("employees.edit"))])
def deactivate(request: Request, employee_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    return _set_active(request, db, user, employee_id, False)


@router.post("/{employee_id:int}/activate", dependencies=[Depends(require("employees.edit"))])
def activate(request: Request, employee_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    return _set_active(request, db, user, employee_id, True)


# ---- import (step 1 Upload, step 2 Review, step 3 Done) ----------------------------------------------------------

def _upload_page(request, user, error=None, status_code=200):
    return templates.TemplateResponse(request, "import_upload.html", {
        "current_user": user, "error": error, "max_rows": get_settings().import_max_rows,
    }, status_code=status_code)


@router.get("/import", response_class=HTMLResponse, dependencies=[Depends(require("employees.edit"))])
def import_form(request: Request, user: User = Depends(require_login)):
    return _upload_page(request, user)


@router.get("/import/template", dependencies=[Depends(require("employees.edit"))])
def import_template():
    return Response(
        build_template(), media_type=XLSX,
        headers={"Content-Disposition": 'attachment; filename="employee_import_template.xlsx"'},
    )


@router.post("/import", response_class=HTMLResponse, dependencies=[Depends(require("employees.edit"))])
def import_upload(
    request: Request, file: UploadFile = File(...), user: User = Depends(require_login),
):
    # The file is read straight into memory and parsed there. It is never saved to disk.
    data = file.file.read(MAX_FILE_BYTES + 1)
    filename = file.filename or ""
    file.file.close()
    try:
        result = parse_upload(filename, data, get_settings().import_max_rows)
    except ImportFileError as exc:
        return _upload_page(request, user, str(exc), 400)
    finally:
        del data
    store.discard(request.session.get(IMPORT_KEY))
    request.session[IMPORT_KEY] = store.put(user.id, result)
    return RedirectResponse("/employees/import/preview", status_code=303)


def _problem_rank(p) -> int:
    return 0 if p.action == ERROR else 1 if p.warnings else 2


@router.get("/import/preview", response_class=HTMLResponse, dependencies=[Depends(require("employees.edit"))])
def import_preview(
    request: Request, show: str = "all", db: Session = Depends(get_db), user: User = Depends(require_login),
):
    parsed = store.get(request.session.get(IMPORT_KEY), user.id)
    if parsed is None:
        request.session.pop(IMPORT_KEY, None)
        flash(request, "The import preview has expired or was already used. Please upload the file again.", "error")
        return RedirectResponse("/employees/import", status_code=303)
    planned = plan_import(db, parsed.rows)
    summary = summarize(planned)
    planned.sort(key=lambda p: (_problem_rank(p), p.row.row_number))   # problems first, then in file order
    if show == "problems":
        shown = [p for p in planned if p.errors or p.warnings]
    elif show == "changes":
        shown = [p for p in planned if p.action in (NEW, UPDATE)]
    else:
        show, shown = "all", planned
    return templates.TemplateResponse(request, "import_preview.html", {
        "current_user": user, "planned": shown, "summary": summary, "notices": parsed.notices, "show": show,
        "to_save": summary["new"] + summary["updated"],
    })


@router.post("/import/confirm", dependencies=[Depends(require("employees.edit"))])
def import_confirm(request: Request, db: Session = Depends(get_db), user: User = Depends(require_login)):
    token = request.session.get(IMPORT_KEY)
    parsed = store.get(token, user.id)
    if parsed is None:
        request.session.pop(IMPORT_KEY, None)
        flash(request, "The import preview has expired or was already used. Please upload the file again.", "error")
        return RedirectResponse("/employees/import", status_code=303)

    store.discard(token)             # a preview can be confirmed only once
    request.session.pop(IMPORT_KEY, None)
    try:
        planned = plan_import(db, parsed.rows)   # re-checked against the current data
        counts = apply_import(db, planned)
        audit.record(
            db, "employees_import_confirmed", user_id=user.id, ip=client_ip(request), commit=False,
            details=f"new={counts['new']} updated={counts['updated']} unchanged={counts['unchanged']} "
                    f"skipped_errors={counts['errors']} with_warnings={counts['warnings']}",
        )
        db.commit()   # ONE commit: everything is saved together, or nothing is
    except IntegrityError:
        # The database's UNIQUE rules (phone, ID number, employee code) won a race with another save. Nothing was saved.
        db.rollback()
        log.warning("Employee import refused by a database uniqueness rule")
        flash(request, "Someone saved an employee with the same phone number, ID number or code while you were reviewing. "
                       "Nothing was changed. Upload the file again to see which rows clash.", "error")
        return RedirectResponse("/employees/import", status_code=303)
    except Exception as exc:
        db.rollback()
        log.error("Employee import failed: %s", type(exc).__name__)  # type only: messages can contain row data
        flash(request, "The import could not be saved, and nothing was changed. Please try again.", "error")
        return RedirectResponse("/employees/import", status_code=303)

    log.info("Employee import saved: new=%s updated=%s skipped=%s", counts["new"], counts["updated"], counts["errors"])
    request.session[IMPORT_RESULT_KEY] = {k: counts[k] for k in ("new", "updated", "unchanged", "errors", "warnings")}
    return RedirectResponse("/employees/import/done", status_code=303)


@router.get("/import/done", response_class=HTMLResponse, dependencies=[Depends(require("employees.edit"))])
def import_done(request: Request, user: User = Depends(require_login)):
    result = request.session.pop(IMPORT_RESULT_KEY, None)
    if not result:
        return RedirectResponse("/employees/import", status_code=303)
    return templates.TemplateResponse(request, "import_done.html", {"current_user": user, "result": result})


@router.post("/import/cancel", dependencies=[Depends(require("employees.edit"))])
def import_cancel(request: Request, user: User = Depends(require_login)):
    store.discard(request.session.pop(IMPORT_KEY, None))
    flash(request, "Import cancelled. Nothing was saved.", "info")
    return RedirectResponse("/employees", status_code=303)
