"""System status (admin only): a read-only page. No secret is ever shown; settings stay in .env."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security.permissions import require
from app.services import system_status
from app.templating import templates

router = APIRouter(prefix="/system", dependencies=[Depends(require("system.view"))])


@router.get("", response_class=HTMLResponse)
def system(request: Request, db: Session = Depends(get_db), user: User = Depends(require("system.view"))):
    return templates.TemplateResponse(request, "system.html", {"current_user": user, **system_status.status_context(db)})
