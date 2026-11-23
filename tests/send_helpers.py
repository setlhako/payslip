"""Helpers for the sending tests. Nothing here ever touches the real network."""
import io
import json

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import AuditLog, Batch, Employee, Payslip
from app.routers.sending import get_send_launcher
from app.services.sender import InlineLauncher, SendConfig
from app.services.whatsapp import (
    AMBIGUOUS, AUTH, PERMANENT, TRANSIENT, LiveWhatsAppClient, WhatsAppClient, WhatsAppError,
)
from tests.batch_helpers import batch_id_of, create, post
from tests.conftest import make_employee
from tests.pdfs import named_pdf

TOKEN = "EAAB-secret-access-token-0123456789"
PHONE_ID = "1234567890"
CFG = SendConfig(concurrency=1, interval=0, max_retries=3)

# employees: (first, surname, phone, id_last4)
PEOPLE = [
    ("Thabo", "Molefe", "+26771234567", "6789"),
    ("Neo", "Dube", "+26772345678", "4321"),
    ("Chipo", "Fakename", "+26773456789", "AB12"),
]


def people(db, rows=PEOPLE):
    return [make_employee(db, f, s, phone, last4) for f, s, phone, last4 in rows]


def ready_batch(client, db, rows=PEOPLE, label="November 2026"):
    """People + a batch with one exact-matching file each, marked ready. Returns the batch id."""
    people(db, rows)
    bid = batch_id_of(create(client, [(f"{f}_{s}.pdf", named_pdf(f, s)) for f, s, _, _ in rows], label=label))
    post(client, f"/batches/{bid}/ready", checked="on")
    db.expire_all()
    assert db.get(Batch, bid).status == "ready"
    return bid


def send_now(client, bid, label="November 2026"):
    return post(client, f"/batches/{bid}/send", confirm_label=label)


def payslips(db, bid):
    db.expire_all()
    return list(db.scalars(select(Payslip).where(Payslip.batch_id == bid).order_by(Payslip.id)))


def statuses(db, bid):
    return [p.status for p in payslips(db, bid)]


def batch(db, bid) -> Batch:
    db.expire_all()
    return db.get(Batch, bid)


def audit_actions(db):
    db.expire_all()
    return [(a.action, a.details) for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]


def fire_events(app):
    """Run the mock client's delayed status updates (sent / delivered / read / failed) now."""
    events = list(app.state.mock_events)
    app.state.mock_events.clear()
    for _, fire in events:
        fire()


class ScriptedClient(WhatsAppClient):
    """A client that does what a test tells it to. Records every call."""
    mode = "live"

    def __init__(self, upload_errors=None, send_errors=None, allowed=()):
        self.uploads, self.sends, self.checked = [], [], []
        self.upload_errors, self.send_errors, self.allowed = list(upload_errors or []), list(send_errors or []), set(allowed)
        self.on_send = None

    def check_recipient(self, to):
        self.checked.append(to)
        if self.allowed and to not in self.allowed:
            raise WhatsAppError(PERMANENT, "LOCAL_NOT_ALLOWED", "not allowed")

    def upload_media(self, pdf, filename):
        self.uploads.append((pdf, filename))
        if self.upload_errors:
            error = self.upload_errors.pop(0)
            if error:
                raise error
        return f"media-{len(self.uploads)}"

    def send_template(self, to, media_id, filename, template_name, language, employee_name, pay_month):
        self.sends.append(dict(to=to, media_id=media_id, filename=filename, template=template_name, language=language,
                               name=employee_name, month=pay_month))
        if self.on_send:
            self.on_send(len(self.sends), to)
        if self.send_errors:
            error = self.send_errors.pop(0)
            if error:
                raise error
        return f"wamid.SCRIPTED{len(self.sends)}"

    def send_document(self, to, media_id, filename, caption=""):
        return self.send_template(to, media_id, filename, "", "", "", caption)


def use_client(app, db_factory, client, cfg=CFG, sleeps=None):
    """Make the Send button use this client (inline, no real waiting). `sleeps` collects the backoff waits."""
    sleeps = sleeps if sleeps is not None else []
    app.dependency_overrides[get_send_launcher] = lambda: InlineLauncher(db_factory, client, cfg, sleep=sleeps.append)
    return sleeps


def err(kind, code=None, status=None, retry_after=None, message="x"):
    return WhatsAppError(kind, code, message, status, retry_after)


def live_client(handler, allowed=(), version="v23.0"):
    """The REAL live client class over a mocked HTTP layer. `handler(request) -> httpx.Response`."""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = LiveWhatsAppClient(base_url="https://graph.facebook.com", version=version, phone_number_id=PHONE_ID, token=TOKEN,
                                timeout=5, allowed=set(allowed), transport=httpx.MockTransport(wrapped))
    return client, seen


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/media"):
        return httpx.Response(200, json={"id": "MEDIA123"})
    to = json.loads(request.content)["to"]
    return httpx.Response(200, json={"messaging_product": "whatsapp", "contacts": [{"input": to, "wa_id": to.lstrip("+")}],
                                     "messages": [{"id": "wamid.LIVE" + to[-3:]}]})


def error_response(status, code, message="problem", details=None, headers=None):
    body = {"error": {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "abc"}}
    if details:
        body["error"]["error_data"] = {"messaging_product": "whatsapp", "details": details}
    return httpx.Response(status, json=body, headers=headers or {})


@pytest.fixture
def live_mode(monkeypatch):
    """Pretend WHATSAPP_MODE=live for the pages (the client itself is always a mocked one in tests)."""
    s = get_settings()
    monkeypatch.setattr(s, "whatsapp_mode", "live")
    return s
