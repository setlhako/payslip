"""The WhatsApp client: ONE interface, two implementations chosen by WHATSAPP_MODE.

    upload_media(pdf bytes, filename) -> media id
    send_template(...)  -> message id   (the real payslip message: approved template, document header, 2 body variables)
    send_document(...)  -> message id   (plain document message: only works inside WhatsApp's 24-hour window; for testing)
    check_recipient(to)                 (the WHATSAPP_ALLOWED_RECIPIENTS safety switch)

Live (httpx against https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}) follows Meta's current
docs (checked Nov 2026); see CLAUDE.md "Sending" for what the docs do and do not say.
Mock simulates sends and statuses locally with NO network, so it is the default.

Errors are raised as WhatsAppError with a `kind` that tells the sender what is safe to do:
    transient  clear failure BEFORE the message was accepted (HTTP 429, 5xx, cannot connect): retry with backoff
    ambiguous  the request may have been accepted (timeout while waiting, unreadable answer): NEVER retry automatically
    auth       the access token / permissions are rejected: stop the whole batch
    permanent  a clear refusal: do not retry automatically
Never logged here: access tokens, phone numbers, file contents. str(error) holds only the kind and code.
"""
import logging
import os
import random
import threading
import uuid
from collections.abc import Callable

import httpx

from app.services.whatsapp_errors import (
    LOCAL_NOT_ALLOWED, LOCAL_TIMEOUT, RATE_LIMIT_CODES, TOKEN_CODES, TRANSIENT_CODES, clean_error_text,
)

# Request logging from httpx would show URLs: keep it out of the logs entirely.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TRANSIENT, AMBIGUOUS, AUTH, PERMANENT = "transient", "ambiguous", "auth", "permanent"
MAX_RETRY_AFTER = 60


class WhatsAppError(Exception):
    def __init__(self, kind: str, code: str | None = None, message: str | None = None,
                 http_status: int | None = None, retry_after: float | None = None):
        super().__init__(f"{kind}:{code}")                      # no message text: it could hold personal data
        self.kind, self.code, self.http_status, self.retry_after = kind, (str(code) if code is not None else None), http_status, retry_after
        self.message = clean_error_text(message)


class WhatsAppConfigError(Exception):
    """Live mode is missing something it needs. Names only, never values."""


# ---- shared helpers ----------------------------------------------------------------------------------------------

def clean_param(text: str, limit: int = 1000) -> str:
    """A template text parameter: no newlines/tabs, no long runs of spaces (Meta rejects them in template text)."""
    return " ".join((text or "").split())[:limit] or "-"


def payslip_filename(pay_month_label: str) -> str:
    """'Payslip - November 2026.pdf' (ASCII letters, digits, spaces and - _ . ( ) only)."""
    safe = "".join(c for c in pay_month_label if c.isascii() and (c.isalnum() or c in " -_.()"))
    return f"Payslip - {' '.join(safe.split())[:60] or 'payslip'}.pdf"


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(float(response.headers.get("retry-after", "")), MAX_RETRY_AFTER)
    except ValueError:
        return None


def error_from_response(response: httpx.Response) -> WhatsAppError:
    """Turn a non-2xx Graph API answer into a WhatsAppError of the right kind."""
    status, code, message = response.status_code, None, None
    try:
        error = response.json().get("error") or {}
        code = error.get("code")
        message = (error.get("error_data") or {}).get("details") or error.get("message")
    except Exception:
        pass
    code_s = str(code) if code is not None else None
    if status == 401 and code_s is None:
        code_s = "190"
    if code_s in TOKEN_CODES or status == 401:
        kind = AUTH
    elif status == 429 or status >= 500 or code_s in RATE_LIMIT_CODES or code_s in TRANSIENT_CODES:
        kind = TRANSIENT
    else:
        kind = PERMANENT
    return WhatsAppError(kind, code_s, message, status, _retry_after(response))


_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
               httpx.LocalProtocolError, httpx.ProxyError)


# ---- the interface -----------------------------------------------------------------------------------------------

class WhatsAppClient:
    mode = "base"

    def check_recipient(self, to: str) -> None:
        """Raise WhatsAppError(permanent, LOCAL_NOT_ALLOWED) when this number may not be sent to."""

    def upload_media(self, pdf: bytes, filename: str) -> str:
        raise NotImplementedError

    def send_template(self, to: str, media_id: str, filename: str, template_name: str, language: str,
                      employee_name: str, pay_month: str) -> str:
        raise NotImplementedError

    def send_document(self, to: str, media_id: str, filename: str, caption: str = "") -> str:
        raise NotImplementedError


# ---- live --------------------------------------------------------------------------------------------------------

class LiveWhatsAppClient(WhatsAppClient):
    mode = "live"

    def __init__(self, *, base_url: str, version: str, phone_number_id: str, token: str, timeout: float = 30,
                 allowed: set[str] | frozenset[str] = frozenset(), transport: httpx.BaseTransport | None = None):
        if transport is None and os.environ.get("PYTEST_CURRENT_TEST"):
            raise RuntimeError("The live WhatsApp client must never be created without a mocked transport during tests.")
        self._root = f"{base_url.rstrip('/')}/{version}/{phone_number_id}"
        self._allowed = frozenset(allowed)
        self._http = httpx.Client(
            transport=transport, timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"Authorization": f"Bearer {token}"},
        )

    def check_recipient(self, to: str) -> None:
        if self._allowed and to not in self._allowed:
            raise WhatsAppError(PERMANENT, LOCAL_NOT_ALLOWED, "not on the allowed recipients list")

    def upload_media(self, pdf: bytes, filename: str) -> str:
        """POST /{phone-number-id}/media (multipart). A failure here is always safe to retry: no message exists yet."""
        try:
            response = self._http.post(
                f"{self._root}/media",
                data={"messaging_product": "whatsapp", "type": "application/pdf"},
                files={"file": (filename, pdf, "application/pdf")},
            )
        except httpx.HTTPError as exc:
            raise WhatsAppError(TRANSIENT, LOCAL_TIMEOUT, type(exc).__name__) from None
        if response.status_code // 100 != 2:
            raise error_from_response(response)
        try:
            return str(response.json()["id"])
        except Exception:
            raise WhatsAppError(TRANSIENT, "LOCAL_UNKNOWN", "unreadable upload answer") from None

    def _post_message(self, payload: dict) -> str:
        self.check_recipient(payload["to"])
        try:
            response = self._http.post(f"{self._root}/messages", json=payload)
        except _NEVER_SENT as exc:
            raise WhatsAppError(TRANSIENT, "LOCAL_NETWORK", type(exc).__name__) from None
        except httpx.HTTPError as exc:     # timed out or dropped AFTER the request may have been accepted
            raise WhatsAppError(AMBIGUOUS, LOCAL_TIMEOUT, type(exc).__name__) from None
        if response.status_code // 100 != 2:
            raise error_from_response(response)
        try:
            return str(response.json()["messages"][0]["id"])
        except Exception:                  # accepted, but we cannot tell which message it is
            raise WhatsAppError(AMBIGUOUS, "LOCAL_UNKNOWN", "unreadable send answer", response.status_code) from None

    def send_template(self, to, media_id, filename, template_name, language, employee_name, pay_month) -> str:
        return self._post_message({
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "template",
            "template": {
                "name": template_name, "language": {"code": language},
                "components": [
                    {"type": "header", "parameters": [{"type": "document", "document": {"id": media_id, "filename": filename}}]},
                    {"type": "body", "parameters": [
                        {"type": "text", "text": clean_param(employee_name)},     # {{1}}
                        {"type": "text", "text": clean_param(pay_month)},         # {{2}}
                    ]},
                ],
            },
        })

    def send_document(self, to, media_id, filename, caption="") -> str:
        document = {"id": media_id, "filename": filename}
        if caption:
            document["caption"] = clean_param(caption, 1024)
        return self._post_message({
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "document", "document": document,
        })


# ---- mock --------------------------------------------------------------------------------------------------------

MOCK_DELAYS = ((0.5, 1.5), (1.0, 3.0), (2.0, 5.0))      # seconds between: accepted->sent, sent->delivered, delivered->read
StatusSink = Callable[[str, str, int | None, list | None], object]


def _thread_schedule(delay: float, fn: Callable[[], None]) -> None:
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()


class MockWhatsAppClient(WhatsAppClient):
    """Simulates WhatsApp locally. The last 3 digits of the recipient choose a scenario:
        ...000  not on WhatsApp  -> accepted, then a FAILED status with code 131026 (like the real thing)
        ...111  rate limit       -> HTTP 429 every time (the sender backs off, then fails the row)
        ...222  timeout          -> ambiguous: the row becomes "uncertain" and is never retried automatically
        others  normal           -> sent, then delivered, then read, a few seconds apart
    Statuses are delivered through `status_sink`, the SAME code path the webhook uses."""

    mode = "mock"

    def __init__(self, status_sink: StatusSink, *, schedule: Callable[[float, Callable[[], None]], None] = _thread_schedule,
                 rng: random.Random | None = None, delays=MOCK_DELAYS):
        self._sink, self._schedule, self._rng, self._delays = status_sink, schedule, rng or random.Random(), delays

    def upload_media(self, pdf: bytes, filename: str) -> str:
        return f"mock-media-{uuid.uuid4().hex[:12]}"

    def _send(self, to: str) -> str:
        tail = "".join(c for c in to if c.isdigit())[-3:]
        if tail == "111":
            raise WhatsAppError(TRANSIENT, "130429", "simulated rate limit (mock mode)", 429)
        if tail == "222":
            raise WhatsAppError(AMBIGUOUS, LOCAL_TIMEOUT, "simulated timeout (mock mode)")
        message_id = "wamid.MOCK" + uuid.uuid4().hex
        t = 0.0
        t += self._rng.uniform(*self._delays[0])
        self._emit(message_id, "sent", t)
        t += self._rng.uniform(*self._delays[1])
        if tail == "000":
            self._emit(message_id, "failed", t, [{"code": 131026, "title": "Message undeliverable",
                                                  "message": "Message undeliverable (simulated: not on WhatsApp)",
                                                  "error_data": {"details": "Simulated by mock mode: the number is not on WhatsApp."}}])
        else:
            self._emit(message_id, "delivered", t)
            self._emit(message_id, "read", t + self._rng.uniform(*self._delays[2]))
        return message_id

    def _emit(self, message_id: str, status: str, delay: float, errors: list | None = None) -> None:
        self._schedule(delay, lambda: self._sink(message_id, status, None, errors))

    def send_template(self, to, media_id, filename, template_name, language, employee_name, pay_month) -> str:
        return self._send(to)

    def send_document(self, to, media_id, filename, caption="") -> str:
        return self._send(to)


# ---- choosing the implementation ---------------------------------------------------------------------------------

def build_client(settings, status_sink: StatusSink | None = None, transport: httpx.BaseTransport | None = None) -> WhatsAppClient:
    """The client for WHATSAPP_MODE. Live mode refuses (WhatsAppConfigError) if anything it needs is missing."""
    if settings.whatsapp_mode != "live":
        if status_sink is None:
            raise WhatsAppConfigError("The mock client needs somewhere to send its status updates.")
        return MockWhatsAppClient(status_sink)
    needed = {
        "WHATSAPP_ACCESS_TOKEN": settings.whatsapp_access_token.get_secret_value(),
        "WHATSAPP_PHONE_NUMBER_ID": settings.whatsapp_phone_number_id,
        "WHATSAPP_APP_SECRET": settings.whatsapp_app_secret.get_secret_value(),
        "WHATSAPP_VERIFY_TOKEN": settings.whatsapp_verify_token.get_secret_value(),
        "GRAPH_API_VERSION": settings.graph_api_version,
    }
    missing = [name for name, value in needed.items() if not value.strip() or value.strip().lower().startswith("your-")]
    if missing:
        raise WhatsAppConfigError("Live mode needs these settings: " + ", ".join(missing))
    return LiveWhatsAppClient(
        base_url=settings.whatsapp_base_url, version=settings.graph_api_version.strip(),
        phone_number_id=settings.whatsapp_phone_number_id.strip(), token=needed["WHATSAPP_ACCESS_TOKEN"].strip(),
        timeout=settings.whatsapp_timeout_seconds, allowed=settings.allowed_recipients, transport=transport,
    )
