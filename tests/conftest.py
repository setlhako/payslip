"""Test setup. Tests use a throw-away in-memory SQLite database and a fake secret:
they never touch your real MariaDB data or your real .env secrets."""
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "test-secret-" + "x" * 40
os.environ["ID_ENCRYPTION_KEY"] = "dGVzdC1rZXktZm9yLXRlc3RzLW9ubHktMzItYnl0ZXM="   # a throw-away key that only the tests use
os.environ["ID_ENCRYPTION_KEY_PREVIOUS"] = ""
os.environ["REVEAL_MAX_PER_MINUTE"] = "10"
os.environ["WHATSAPP_MODE"] = "mock"                       # tests NEVER use live mode (and never the real network, see below)
os.environ["WHATSAPP_ALLOWED_RECIPIENTS"] = ""
os.environ["WHATSAPP_APP_SECRET"] = "test-app-secret-not-real"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token-not-real"
os.environ["WHATSAPP_ACCESS_TOKEN"] = "test-access-token-not-real"
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "1234567890"
os.environ["APP_ENV"] = "development"
os.environ["SESSION_COOKIE_SECURE"] = "false"

import re  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import User  # noqa: E402
from app.routers.batches import get_session_factory  # noqa: E402
from app.routers.sending import get_send_launcher  # noqa: E402
from app.services.send_status import apply_status_event  # noqa: E402
from app.services.sender import InlineLauncher, SendConfig  # noqa: E402
from app.services.whatsapp import MockWhatsAppClient  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402

USERNAME = "hr.admin"
PASSWORD = "correct-horse-battery-9"
_HASH = hash_password(PASSWORD)  # hashing is slow on purpose, so do it once


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(db_factory):
    with db_factory() as session:
        yield session


@pytest.fixture
def app(db_factory):
    application = create_app()

    def _get_db():
        with db_factory() as session:
            yield session

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_session_factory] = lambda: db_factory   # background matching uses the test database

    # Sending: the mock WhatsApp client, run inline. Its delayed statuses (sent/delivered/read) are COLLECTED, not timed:
    # a test fires them with `for _, fire in app.state.mock_events: fire()`.
    events: list = []
    mock = MockWhatsAppClient(lambda mid, st, ts, errs: apply_status_event(db_factory, mid, st, ts, errs),
                              schedule=lambda delay, fn: events.append((delay, fn)))
    application.state.mock_events, application.state.mock_client = events, mock
    application.dependency_overrides[get_send_launcher] = lambda: InlineLauncher(
        db_factory, mock, SendConfig(concurrency=1, interval=0, max_retries=3))
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def admin(db):
    user = User(username=USERNAME, password_hash=_HASH, is_active=True, role="admin")
    db.add(user)
    db.commit()
    return user


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no CSRF hidden field found in page"
    return m.group(1)


def do_login(client, username=USERNAME, password=PASSWORD, follow=False):
    token = csrf_from(client.get("/login").text)
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=follow,
    )


@pytest.fixture
def logged_in(client, admin):
    r = do_login(client)
    assert r.status_code == 303
    return client


def make_employee(db, first, surname, phone=None, id_last4=None, code=None, active=True, id_type=None, id_number=None):
    """id_number (a FULL number) gives the employee an encrypted full ID; id_type and id_last4 are derived from it."""
    from app.models import Employee
    from app.services.employees import apply_values
    from app.services.id_crypto import encrypt_id, id_blind_index
    from app.services.ids import parse_id

    encrypted = id_hash = None
    if id_number:
        parsed = parse_id(id_number, id_type or "omang")
        id_type, id_last4, encrypted = parsed.id_type, parsed.last4, encrypt_id(parsed.number)
        id_hash = id_blind_index(parsed.number)
    e = Employee(active=active)
    apply_values(e, first, surname, phone, (id_type or "omang") if id_last4 else None, id_last4, code,
                 id_number_encrypted=encrypted, id_number_hash=id_hash)
    db.add(e)
    db.commit()
    return e


@pytest.fixture(autouse=True)
def _fresh_preview_store():
    from app.services.import_preview import store

    store.clear()
    yield
    store.clear()


@pytest.fixture(autouse=True)
def _private_storage_in_tmp(tmp_path, monkeypatch):
    """Uploaded PDFs go to a throw-away folder in every test: the real private_storage/ is never touched."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "private_storage_dir", tmp_path / "private_storage")
    return tmp_path / "private_storage"


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """A test must never reach the real network (and so never the real WhatsApp). Mocked transports are unaffected."""
    import httpx

    def blocked(self, request):
        raise AssertionError("a test tried to make a REAL network request to " + request.url.host)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
