"""CSRF protection.

How it works: each session has a random token. Every form includes it as a hidden field
(`{{ csrf_input(request) }}` in the template). `verify_csrf` runs on every POST/PUT/PATCH/DELETE
for the whole app and rejects the request unless the token matches. Non-form requests may send it
in an `X-CSRF-Token` header instead.
"""
import hmac
import secrets

from fastapi import HTTPException, Request
from markupsafe import Markup, escape

from app.security.sessions import CSRF

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Paths that are NOT called by a browser with our cookie. They must authenticate another way
# (the WhatsApp webhook checks Meta's X-Hub-Signature-256 instead).
EXEMPT_PATHS = {"/webhook/whatsapp"}


def get_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF] = token
    return token


def csrf_input(request: Request) -> Markup:
    """Hidden form field for templates: {{ csrf_input(request) }}"""
    return Markup('<input type="hidden" name="csrf_token" value="{}">').format(
        escape(get_csrf_token(request))
    )


async def verify_csrf(request: Request) -> None:
    if request.method in SAFE_METHODS or request.url.path in EXEMPT_PATHS:
        return
    expected = request.session.get(CSRF)
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        form = await request.form()
        supplied = form.get("csrf_token")
    if (
        not expected
        or not isinstance(supplied, str)
        or not hmac.compare_digest(supplied.encode(), str(expected).encode())
    ):
        raise HTTPException(status_code=403, detail="Security check failed. Please go back, reload the page and try again.")
