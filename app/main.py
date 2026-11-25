import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.database import SessionLocal
from app.logging_setup import configure_logging
from app.routers import account, audit_page, auth, batches, employees, health, pages, sending, session, system_page, users, webhook
from app.security.auth import LoginRequired, PasswordChangeRequired
from app.security.csrf import verify_csrf
from app.security.headers import security_headers
from app.services import housekeeping
from app.services.body_limit import BodySizeLimitMiddleware
from app.single_instance import acquire_instance_lock
from app.templating import templates

STATIC_DIR = Path(__file__).resolve().parent / "static"  # public CSS only. Payslip PDFs never go here.
log = logging.getLogger(__name__)
configure_logging(get_settings())                         # structured, redacted, levels from settings (see logging_setup.py)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start-up and shut-down of the ONE app process (TestClient without `with` does not run this).

    1. Production: take the single-instance lock (a second process refuses to start).
    2. Recovery, safe on EVERY restart and idempotent (running it twice changes nothing the second time):
       a payslip left in "sending" becomes "uncertain" (never retried automatically), queued payslips of a running batch
       continue, and matching jobs that were cut off are marked failed so HR can re-run them.
    3. Housekeeping every few minutes (expired previews / one-time passwords / old login attempts).
    """
    settings = get_settings()
    lock = None
    if settings.app_env == "production":
        lock = acquire_instance_lock(settings.storage_path / ".instance.lock")      # raises AlreadyRunning: the app does not start
    app.state.instance_lock = lock
    try:
        from app.services.batch_jobs import reset_interrupted_matching
        from app.services.sender import SendLauncher, recover_after_restart

        reset_interrupted_matching(SessionLocal)
        recover_after_restart(SessionLocal, SendLauncher(SessionLocal))
    except Exception as exc:       # e.g. the database is not migrated yet: the app must still start
        log.error("Send recovery could not run: %s", type(exc).__name__)
    cleaner = asyncio.create_task(housekeeping.loop(SessionLocal))
    try:
        yield
    finally:
        cleaner.cancel()
        if lock is not None:
            lock.release()


def create_app(settings=None) -> FastAPI:
    settings = settings or get_settings()
    # verify_csrf runs on every POST/PUT/PATCH/DELETE in the whole app, so future forms are covered automatically.
    app = FastAPI(
        title="Payslip Sender",
        debug=False,                                              # never show tracebacks to visitors, in any environment
        docs_url=None, redoc_url=None, openapi_url=None,          # /docs, /redoc and /openapi.json do not exist (any environment)
        dependencies=[Depends(verify_csrf)],
        lifespan=lifespan,
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie="payslip_session",
        max_age=settings.session_absolute_hours * 3600,
        same_site="lax",
        https_only=settings.secure_cookies,  # the Secure flag; HttpOnly is always on
    )
    app.add_middleware(BodySizeLimitMiddleware)  # 413 for oversized POST bodies; see services/body_limit.py
    app.middleware("http")(security_headers)  # added last = outermost, so it covers every response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(PasswordChangeRequired)
    async def _password_change_required(request: Request, exc: PasswordChangeRequired):
        return RedirectResponse("/account/password", status_code=303)

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
    app.include_router(employees.router)  # protected
    app.include_router(batches.router)    # protected
    app.include_router(sending.router)    # protected
    app.include_router(users.router)      # admin only
    app.include_router(account.router)    # any signed-in user: own password and logins
    app.include_router(session.router)    # any signed-in user: the inactivity warning's status / keep-alive
    app.include_router(audit_page.router) # admin only
    app.include_router(system_page.router)  # admin only
    app.include_router(webhook.router)    # public on purpose: Meta cannot log in; it is signature-checked instead
    return app


app = create_app()
