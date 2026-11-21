"""Test setup. Tests use a throw-away in-memory SQLite database and a fake secret:
they never touch your real MariaDB data or your real .env secrets."""
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "test-secret-" + "x" * 40
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
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def admin(db):
    user = User(username=USERNAME, password_hash=_HASH, is_active=True)
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
