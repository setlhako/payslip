import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routers import auth, health, pages
from app.security.auth import LoginRequired
from app.security.csrf import verify_csrf
from app.security.headers import security_headers
from app.templating import templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).resolve().parent / "static"  # public CSS only. Payslip PDFs never go here.


def create_app() -> FastAPI:
    settings = get_settings()
    # verify_csrf runs on every POST/PUT/PATCH/DELETE in the whole app, so future forms are covered automatically.
    app = FastAPI(
        title="Payslip Sender",
        docs_url=None, redoc_url=None, openapi_url=None,
        dependencies=[Depends(verify_csrf)],
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie="payslip_session",
        max_age=settings.session_absolute_hours * 3600,
        same_site="lax",
        https_only=settings.secure_cookies,  # the Secure flag; HttpOnly is always on
    )
    app.middleware("http")(security_headers)  # added last = outermost, so it covers every response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        message = exc.detail if exc.status_code == 403 else "Sorry, that page was not found." if exc.status_code == 404 else "Something went wrong."
        return templates.TemplateResponse(
            request, "error.html", {"status_code": exc.status_code, "message": message}, status_code=exc.status_code
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)   # public
    app.include_router(auth.router)     # /login public, /logout protected
    app.include_router(pages.router)    # protected
    return app


app = create_app()
