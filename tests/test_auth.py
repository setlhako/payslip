from datetime import timedelta

from fastapi.routing import APIRoute
from sqlalchemy import select

from app.models import AuditLog, LoginAttempt
from app.models.mixins import utcnow
from app.security import sessions
from app.security.auth import require_login
from tests.conftest import PASSWORD, USERNAME, csrf_from, do_login


def audit_actions(db):
    return [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]


# ---- successful login -------------------------------------------------------------------------

def test_successful_login_reaches_dashboard(client, admin, db):
    r = do_login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = client.get("/")
    assert page.status_code == 200 and "Dashboard" in page.text and USERNAME in page.text
    assert "login_success" in audit_actions(db)


def test_session_cookie_flags(client, admin):
    r = do_login(client)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie  # off for local http


def test_secure_flag_follows_setting(monkeypatch, admin):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    get_settings.cache_clear()
    try:
        c = TestClient(create_app(), base_url="https://testserver")
        r = c.get("/login")
        assert "secure" in r.headers["set-cookie"].lower()
    finally:
        monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
        get_settings.cache_clear()


# ---- failures ---------------------------------------------------------------------------------

def test_wrong_password_and_unknown_user_give_same_message(client, admin, db):
    wrong = do_login(client, password="not-the-password-1")
    unknown = do_login(client, username="nobody", password="not-the-password-1")
    assert wrong.status_code == unknown.status_code == 401
    assert "Invalid username or password." in wrong.text
    assert "Invalid username or password." in unknown.text
    assert audit_actions(db).count("login_failure") == 2
    assert client.get("/", follow_redirects=False).status_code == 303  # still logged out


def test_passwords_never_end_up_in_audit_log(client, admin, db):
    do_login(client, password="secret-guess-12345")
    do_login(client)
    dump = " ".join(f"{a.details} {a.ip_address}" for a in db.scalars(select(AuditLog)))
    assert "secret-guess-12345" not in dump and PASSWORD not in dump
    assert "testclient" not in dump


def test_inactive_user_cannot_log_in(client, admin, db):
    admin.is_active = False
    db.commit()
    assert do_login(client).status_code == 401


# ---- lockout ----------------------------------------------------------------------------------

def test_lockout_after_five_failures_blocks_even_correct_password(client, admin, db, monkeypatch):
    for _ in range(4):
        assert do_login(client, password="bad-password-000").status_code == 401
    assert do_login(client, password="bad-password-000").status_code == 429  # 5th failure triggers lock
    r = do_login(client)  # correct password, but locked
    assert r.status_code == 429 and "Too many failed attempts" in r.text
    actions = audit_actions(db)
    assert actions.count("login_failure") == 4 and actions.count("login_lockout") == 1
    assert "login_success" not in actions

    # the lock expires after 15 minutes
    later = utcnow() + timedelta(minutes=16)
    monkeypatch.setattr("app.services.login_limiter.utcnow", lambda: later)
    assert do_login(client).status_code == 303


def test_lockout_applies_to_unknown_usernames_too(client, admin):
    for _ in range(5):
        r = do_login(client, username="ghost", password="whatever-123456")
    assert r.status_code == 429


def test_lockout_is_stored_in_database(client, admin, db):
    for _ in range(5):
        do_login(client, password="bad-password-000")
    assert db.query(LoginAttempt).filter_by(succeeded=False).count() == 5


# ---- CSRF -------------------------------------------------------------------------------------

def test_login_post_without_csrf_token_is_rejected(client, admin):
    client.get("/login")
    r = client.post("/login", data={"username": USERNAME, "password": PASSWORD})
    assert r.status_code == 403
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_post_with_wrong_csrf_token_is_rejected(client, admin):
    client.get("/login")
    r = client.post("/login", data={"username": USERNAME, "password": PASSWORD, "csrf_token": "forged"})
    assert r.status_code == 403


def test_logout_requires_csrf_token(logged_in):
    assert logged_in.post("/logout", data={}).status_code == 403
    assert logged_in.get("/").status_code == 200  # still logged in


def test_csrf_token_also_accepted_in_header(logged_in):
    token = csrf_from(logged_in.get("/").text)
    r = logged_in.post("/logout", headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert r.status_code == 303


def test_logout_clears_session(logged_in, db):
    token = csrf_from(logged_in.get("/").text)
    r = logged_in.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert logged_in.get("/", follow_redirects=False).status_code == 303
    assert "logout" in audit_actions(db)


# ---- protected routes ------------------------------------------------------------------------

def test_dashboard_redirects_to_login_when_logged_out(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_health_is_public_and_minimal(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_every_route_requires_login_except_the_public_ones(app):
    """Safety net: a new route added without require_login makes this test fail."""
    public = {("/health", "GET"), ("/login", "GET"), ("/login", "POST")}

    def uses_login(dep):
        return dep.call is require_login or any(uses_login(d) for d in dep.dependencies)

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (route.path, method) not in public:
                assert uses_login(route.dependant), f"{method} {route.path} is not protected"


# ---- session expiry --------------------------------------------------------------------------

def test_session_expires_after_30_minutes_idle(logged_in, monkeypatch):
    t0 = sessions.now()
    monkeypatch.setattr(sessions, "now", lambda: t0 + 29 * 60)
    assert logged_in.get("/").status_code == 200  # activity at minute 29 keeps it alive
    monkeypatch.setattr(sessions, "now", lambda: t0 + 29 * 60 + 31 * 60)  # then 31 idle minutes
    r = logged_in.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_session_expires_after_8_hours_even_if_active(logged_in, monkeypatch):
    t0 = sessions.now()
    # stay active every 20 minutes for just under 8 hours
    for minutes in range(20, 8 * 60, 20):
        monkeypatch.setattr(sessions, "now", lambda m=minutes: t0 + m * 60)
        assert logged_in.get("/").status_code == 200
    monkeypatch.setattr(sessions, "now", lambda: t0 + 8 * 3600 + 60)
    assert logged_in.get("/", follow_redirects=False).status_code == 303


def test_password_change_invalidates_existing_session(logged_in, admin, db):
    from app.services.admins import reset_password

    reset_password(db, USERNAME, "a-brand-new-password-77")
    assert logged_in.get("/", follow_redirects=False).status_code == 303
    assert "admin_password_reset" in audit_actions(db)


# ---- security headers ------------------------------------------------------------------------

def test_security_headers_present(client):
    for path in ("/login", "/health", "/static/style.css"):
        h = client.get(path).headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"]
        assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert client.get("/login").headers["cache-control"] == "no-store"
