"""The audit log (admin only): filters, search, paging and a CSV download of the filtered view.
Nothing sensitive is shown or exported: see services/audit_view.py (it scrubs digits and tokens as well)."""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routers.auth import client_ip
from app.security.permissions import require
from app.services import audit, audit_view
from app.templating import templates

router = APIRouter(prefix="/audit", dependencies=[Depends(require("audit.view"))])
NO_STORE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


@router.get("", response_class=HTMLResponse)
def audit_log(request: Request, db: Session = Depends(get_db), user: User = Depends(require("audit.view"))):
    filters, problems = audit_view.parse_filters(request.query_params)
    rows, total, pages = audit_view.page_rows(db, filters)

    def link(**changes) -> str:
        query = urlencode(filters.params(**changes))
        return "/audit" + ("?" + query if query else "")

    export = "/audit/export.csv" + ("?" + urlencode(filters.params(page=1)) if filters.params(page=1) else "")
    return templates.TemplateResponse(request, "audit.html", {
        "current_user": user, "f": filters, "rows": rows, "total": total, "pages": pages, "problems": problems,
        "actions": audit_view.known_actions(db), "users": audit_view.known_users(db), "link": link, "export_url": export,
        "per_page": audit_view.PER_PAGE, "filtered": bool(filters.params(page=1)),
    })


@router.get("/export.csv")
def audit_export(request: Request, db: Session = Depends(get_db), user: User = Depends(require("audit.view"))):
    filters, _ = audit_view.parse_filters(request.query_params)
    body, count = audit_view.export_csv(db, filters)
    audit.record(db, "audit_exported", user_id=user.id, ip=client_ip(request), details=f"rows={count}")
    return Response(body, media_type="text/csv; charset=utf-8", headers={**NO_STORE, "Content-Disposition": 'attachment; filename="audit-log.csv"'})
