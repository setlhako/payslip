"""Helpers for the role / user-management tests."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditLog, User
from tests.conftest import _HASH, PASSWORD, csrf_from, do_login


def make_user(db, username, role, *, password_hash=None, must_change=False, active=True, full_name=None):
    user = User(username=username, full_name=full_name if full_name is not None else username.replace(".", " ").title(), role=role,
                password_hash=password_hash or _HASH, is_active=active, must_change_password=must_change)
    db.add(user)
    db.commit()
    return user


def login_as(app, username, password=PASSWORD) -> TestClient:
    """A NEW browser (its own cookies) logged in as this user. Fails the test if the login does not succeed."""
    client = TestClient(app)
    response = do_login(client, username, password)
    assert response.status_code == 303, f"login failed for {username}: {response.status_code}"
    return client


def token(client) -> str:
    return csrf_from(client.get("/account").text if "payslip_session" in client.cookies else client.get("/login").text)


def post(client, path, **data):
    return client.post(path, data={"csrf_token": token(client), **data}, follow_redirects=False)


def audit(db, *actions):
    db.expire_all()
    rows = list(db.scalars(select(AuditLog).order_by(AuditLog.id)))
    return [a for a in rows if not actions or a.action in actions]


def temp_password_from(response_text: str) -> str:
    match = re.search(r'<code id="temp-pw">([^<]+)</code>', response_text)
    assert match, "no temporary password on the page"
    return match.group(1)


@pytest.fixture
def staff(app, db, admin):
    """One logged-in client per role: staff['admin'], staff['hr'], staff['viewer'] (the admin is the shared test admin)."""
    make_user(db, "the.hr", "hr")
    make_user(db, "the.viewer", "viewer")
    return {"admin": login_as(app, admin.username), "hr": login_as(app, "the.hr"), "viewer": login_as(app, "the.viewer")}
