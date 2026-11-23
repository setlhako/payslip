from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.security.auth import require_login
from app.security.permissions import require
from app.services.dashboard import dashboard_context
from app.templating import templates

# Every route on this router needs a login.
router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require("dashboard.view"))])
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_login)):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"current_user": user, "whatsapp_mode": get_settings().whatsapp_mode, **dashboard_context(db, user)},
    )
