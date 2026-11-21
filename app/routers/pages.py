from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.models import User
from app.security.auth import require_login
from app.templating import templates

# Every route on this router needs a login.
router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_login)):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"current_user": user, "whatsapp_mode": get_settings().whatsapp_mode},
    )
