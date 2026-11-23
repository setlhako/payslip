"""Production hardening: the app refuses unsafe settings, /docs is gone, cookies are Secure, exactly one process, safe restarts,
redacted logs, the retention rule and the housekeeping that runs inside the app."""
import ast
import io
import json
import logging
import secrets
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import server
from app.config import PROJECT_ROOT, Settings, get_settings
from app.database import get_db
from app.logging_setup import JsonFormatter, RedactingFormatter, configure_logging, redact
from app.main import create_app
from app.models import Batch, LoginAttempt, Payslip
from app.models.mixins import utcnow
from app.scripts.purge_old_batches import purge
from app.services import housekeeping, sender
from app.services.batch_jobs import reset_interrupted_matching
from app.services.import_preview import store as preview_store
from app.services.one_time import temp_passwords
from app.single_instance import AlreadyRunning, InstanceLock, acquire_instance_lock
from tests.conftest import do_login
from tests.test_batch_tools import make_batch

GOOD_DB = "mysql+pymysql://payslip_app:Zx81qLmN4pRt7VbC@127.0.0.1:3306/payslip_sender?charset=utf8mb4"


def production(**changes) -> dict:
    """Settings arguments that production ACCEPTS; tests change one thing at a time."""
    values = dict(app_env="production", session_secret=secrets.token_urlsafe(48), id_encryption_key=Fernet.generate_key().decode(),
                  database_url=GOOD_DB, session_cookie_secure=None)       # (the test environment sets it to false: undo that)
    values.update(changes)
    return values


def build(**changes) -> Settings:
    return Settings(_env_file=None, **production(**changes))


# ---- 1. production refuses to start with unsafe settings -----------------------------------------------------------------

def test_a_properly_configured_production_is_accepted():
    s = build()
    assert s.app_env == "production" and s.secure_cookies is True and s.app_host == "127.0.0.1"


@pytest.mark.parametrize("changes, names", [
    ({"session_secret": "change-me"}, "SESSION_SECRET"),
    ({"session_secret": ""}, "SESSION_SECRET"),
    ({"session_secret": "a" * 40}, "SESSION_SECRET"),                                    # long but predictable
    ({"id_encryption_key": "dGVzdC1rZXktZm9yLXRlc3RzLW9ubHktMzItYnl0ZXM="}, "ID_ENCRYPTION_KEY"),   # valid Fernet key but readable text
    ({"database_url": "mysql+pymysql://payslip_app:YOUR_DB_PASSWORD@127.0.0.1:3306/payslip_sender"}, "DATABASE_URL"),
    ({"database_url": ""}, "DATABASE_URL"),
    ({"database_url": "sqlite:///x.db"}, "DATABASE_URL"),
    ({"session_cookie_secure": False}, "SESSION_COOKIE_SECURE"),
    ({"app_host": "0.0.0.0"}, "APP_HOST"),
])
def test_production_refuses_unsafe_settings_and_names_the_setting(changes, names):
    with pytest.raises(ValueError) as caught:
        build(**changes)
    message = str(caught.value)
    assert names in message                                             # (the field-level rule may speak first for a very short secret)
    for value in changes.values():                                                       # never echoes the bad value back
        if isinstance(value, str) and len(value) > 8:
            assert value not in message


def test_development_still_accepts_placeholders_so_local_work_is_easy():
    s = Settings(_env_file=None, app_env="development", session_secret="change-me" * 6, id_encryption_key=Fernet.generate_key().decode(),
                 database_url="sqlite://", session_cookie_secure=None)
    assert s.app_env == "development" and s.secure_cookies is False


def test_all_problems_are_listed_at_once():
    with pytest.raises(ValueError) as caught:
        build(session_secret="change-me" * 6, database_url="sqlite://", app_host="0.0.0.0")
    assert all(n in str(caught.value) for n in ("SESSION_SECRET", "DATABASE_URL", "APP_HOST"))


# ---- 2. docs are gone, cookies are Secure ---------------------------------------------------------------------------------

@pytest.fixture
def prod_app(monkeypatch, db_factory, admin):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "production")
    monkeypatch.setattr(s, "session_cookie_secure", None)                  # "not set": production default = Secure
    application = create_app()

    def _get_db():
        with db_factory() as session:
            yield session

    application.dependency_overrides[get_db] = _get_db
    return application


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_the_api_docs_do_not_exist_in_any_mode(client, prod_app, path):
    assert client.get(path).status_code in (303, 404)                      # (a visitor who is not logged in is sent to the login page)
    assert TestClient(prod_app, base_url="https://testserver").get(path, follow_redirects=False).status_code in (303, 404)
    assert create_app().docs_url is None and create_app().redoc_url is None and create_app().openapi_url is None


def test_production_login_cookie_is_secure_httponly_and_samesite_lax(prod_app):
    browser = TestClient(prod_app, base_url="https://testserver")
    response = do_login(browser)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert "payslip_session=" in cookie and "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert response.headers["strict-transport-security"].startswith("max-age=")


def test_development_cookie_is_not_secure_and_sends_no_hsts(client, admin):
    response = do_login(client)
    assert "secure" not in response.headers["set-cookie"].lower().replace("samesite", "") and "httponly" in response.headers["set-cookie"].lower()
    assert "strict-transport-security" not in response.headers


def test_the_app_is_never_in_debug_mode(app):
    assert app.debug is False


# ---- 3. exactly one process ------------------------------------------------------------------------------------------------

def test_uvicorn_is_started_with_one_worker_local_only_and_trusted_proxy_only_from_localhost():
    options = server.run_options(build())
    assert options["workers"] == 1 and options["reload"] is False
    assert options["host"] == "127.0.0.1" and options["forwarded_allow_ips"] == "127.0.0.1" and options["proxy_headers"] is True
    assert options["access_log"] is False and options["log_config"] is None


@pytest.mark.parametrize("value, ok", [(None, True), ("", True), ("1", True), (" 1 ", True), ("2", False), ("4", False), ("0", False), ("auto", False)])
def test_more_than_one_worker_is_refused(value, ok):
    environ = {} if value is None else {"WEB_CONCURRENCY": value}
    if ok:
        server.refuse_multiple_workers(environ)
    else:
        with pytest.raises(SystemExit) as caught:
            server.refuse_multiple_workers(environ)
        assert "exactly one" in str(caught.value).lower()


def test_a_bad_production_config_stops_the_server_with_plain_words_not_a_traceback(monkeypatch):
    def refuse():
        return build(session_secret="change-me" * 6, app_host="0.0.0.0")

    monkeypatch.setattr("app.config.get_settings", refuse)
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: pytest.fail("the server must not start"))
    with pytest.raises(SystemExit) as caught:
        server.main()
    message = str(caught.value)
    assert message.startswith("Refusing to start:") and "SESSION_SECRET" in message and "APP_HOST" in message and "Traceback" not in message


def test_a_good_config_starts_uvicorn_once_with_the_run_options(monkeypatch):
    good = build()
    calls = []
    monkeypatch.setattr("app.config.get_settings", lambda: good)
    monkeypatch.setattr(server.uvicorn, "run", lambda target, **opts: calls.append((target, opts)))
    server.main()
    assert calls == [("app.main:app", server.run_options(good))]


def test_a_second_process_cannot_take_the_instance_lock_until_the_first_lets_go(tmp_path):
    path = tmp_path / "x" / ".instance.lock"
    first = acquire_instance_lock(path)
    with pytest.raises(AlreadyRunning) as caught:
        acquire_instance_lock(path)
    assert "ONE app process" in str(caught.value)
    first.release()
    InstanceLock(path).acquire().release()                                   # free again: a restart always works
    first.release()                                                         # releasing twice is harmless


def test_production_start_takes_the_lock_and_a_second_start_is_refused(monkeypatch, prod_app, db_factory):
    second = create_app()
    with TestClient(prod_app):
        assert (get_settings().storage_path / ".instance.lock").exists()
        with pytest.raises(AlreadyRunning):
            with TestClient(second):
                pass
    with TestClient(second):                                                 # the first one ended: now it may start
        pass


def test_development_start_does_not_take_a_lock(app, tmp_path):
    with TestClient(app):
        assert not (get_settings().storage_path / ".instance.lock").exists()


# ---- 4. restarts are safe: recovery repeats harmlessly ----------------------------------------------------------------------

def test_recovery_can_run_on_every_restart_without_changing_anything_twice(db, db_factory):
    b = Batch(pay_month_label="X", status="sending", send_state="running")
    db.add(b)
    db.flush()
    for status in ("sending", "queued", "sent", "delivered", "failed", "uncertain"):
        db.add(Payslip(batch_id=b.id, original_filename=f"{status}.pdf", status=status, match_status="exact"))
    db.commit()
    started = []
    launcher = type("L", (), {"start": lambda self, i: started.append(i)})()
    assert sender.recover_after_restart(db_factory, launcher) == {"uncertain": 1, "resumed": 1}
    after_first = sorted((p.original_filename, p.status) for p in db.scalars(select(Payslip)))
    assert sender.recover_after_restart(db_factory, launcher) == {"uncertain": 0, "resumed": 1}      # the queued row is still resumed, nothing else moves
    db.expire_all()
    assert sorted((p.original_filename, p.status) for p in db.scalars(select(Payslip))) == after_first
    assert dict(after_first) == {"sending.pdf": "uncertain", "queued.pdf": "queued", "sent.pdf": "sent", "delivered.pdf": "delivered",
                                 "failed.pdf": "failed", "uncertain.pdf": "uncertain"}


def test_matching_that_was_cut_off_by_a_restart_becomes_failed_so_hr_can_re_run_it(db, db_factory):
    running = Batch(pay_month_label="A", processing_state="processing")
    done = Batch(pay_month_label="B", processing_state="done")
    db.add_all([running, done])
    db.commit()
    assert reset_interrupted_matching(db_factory) == 1 and reset_interrupted_matching(db_factory) == 0
    db.expire_all()
    assert db.get(Batch, running.id).processing_state == "failed" and db.get(Batch, done.id).processing_state == "done"


# ---- 5. retention: PDFs go N days after a batch is fully sent ------------------------------------------------------------------

def completed(db, days_since_completed, *, status="sent", **kw):
    b = make_batch(db, 3, status="completed", **kw)
    b.completed_at = utcnow() - timedelta(days=days_since_completed)
    for p in db.scalars(select(Payslip).where(Payslip.batch_id == b.id)):
        p.status = status
    db.commit()
    return b


def purged(db, b) -> bool:
    db.expire_all()
    return db.get(Batch, b.id).files_deleted_at is not None


def test_the_default_is_seven_days_after_the_batch_was_fully_sent():
    assert Settings(_env_file=None, session_secret="s" * 40, database_url="sqlite://", id_encryption_key=Fernet.generate_key().decode()).sent_retention_days == 7


def test_pdfs_of_a_fully_sent_batch_are_deleted_after_seven_days_not_before(db):
    old, fresh = completed(db, 8), completed(db, 6)
    assert purge(db)["batches"] == 1
    assert purged(db, old) and not purged(db, fresh)


def test_a_batch_with_failed_or_uncertain_payslips_keeps_its_files_so_hr_can_retry(db):
    failed, uncertain = completed(db, 20, status="failed"), completed(db, 20, status="uncertain")
    assert purge(db)["batches"] == 0 and not purged(db, failed) and not purged(db, uncertain)
    stale = completed(db, 40, status="failed")
    stale.created_at = utcnow() - timedelta(days=40)
    db.commit()
    assert purge(db)["batches"] == 1 and purged(db, stale)                   # the 30-day limit still applies to everything


def test_the_sent_retention_setting_is_honoured(db, monkeypatch):
    b = completed(db, 3)
    assert purge(db)["batches"] == 0
    monkeypatch.setattr(get_settings(), "sent_retention_days", 2)
    assert purge(db)["batches"] == 1 and purged(db, b)


def test_a_batch_still_being_sent_is_never_purged(db):
    b = make_batch(db, 90, status="sending")
    b.completed_at = utcnow() - timedelta(days=90)
    db.commit()
    assert purge(db)["batches"] == 0


# ---- 6. housekeeping inside the app ---------------------------------------------------------------------------------------------

def test_housekeeping_clears_expired_previews_one_time_secrets_and_old_login_attempts(db, db_factory, monkeypatch):
    import app.services.import_preview as ip
    import app.services.one_time as ot
    from app.services.import_parser import ParseResult

    preview_store.put(1, ParseResult(rows=[]))
    temp_passwords.put(1, "value")
    db.add_all([LoginAttempt(username="a", ip_address="1.2.3.x", attempted_at=utcnow() - timedelta(days=3)),
                LoginAttempt(username="b", ip_address="1.2.3.x", attempted_at=utcnow())])
    db.commit()
    assert housekeeping.run_once(db_factory) == {"import_previews": 0, "one_time_secrets": 0, "login_attempts": 1}   # nothing has expired yet
    monkeypatch.setattr(ip, "_now", lambda: 10 ** 9)
    monkeypatch.setattr(ot, "_now", lambda: 10 ** 9)
    assert housekeeping.run_once(db_factory) == {"import_previews": 1, "one_time_secrets": 1, "login_attempts": 0}
    assert [a.username for a in db.scalars(select(LoginAttempt))] == ["b"]


# ---- 7. logging ----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, gone", [
    ("send to +26771234567 failed", "71234567"),
    ("omang 123456789 seen", "123456789"),
    ("Authorization: Bearer EAAGm0PX4ZCpsBAKexample123456", "EAAGm0PX4ZCpsBAKexample123456"),
    ("header sha256=" + "ab" * 32, "ab" * 32),
    ("password=hunter2hunter2 ok", "hunter2hunter2"),
    ("id gAAAAABk" + "x" * 60, "x" * 60),
    ("key " + "q" * 50, "q" * 50),
])
def test_redact_masks_secrets_and_personal_numbers(text, gone):
    assert gone not in redact(text)


def test_redact_leaves_ordinary_messages_alone():
    assert redact("Batch matching failed: OperationalError") == "Batch matching failed: OperationalError"
    assert redact("sent=3 failed=1") == "sent=3 failed=1"


def _settings(**changes):
    values = dict(session_secret=secrets.token_urlsafe(48), database_url=GOOD_DB, id_encryption_key=Fernet.generate_key().decode(),
                  session_cookie_secure=None)
    values.update(changes)
    return Settings(_env_file=None, **values)


@pytest.fixture
def root_logger_restored():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    access_disabled = logging.getLogger("uvicorn.access").disabled
    yield root
    logging.getLogger("uvicorn.access").disabled = access_disabled
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)


def test_production_logs_are_json_and_redacted(root_logger_restored):
    stream = io.StringIO()
    configure_logging(_settings(app_env="production"), stream=stream)
    logging.getLogger("payslip.test").error("could not message +26771234567 with Bearer abcdefghijklmnop")
    try:
        raise ValueError("bad id 987654321")
    except ValueError:
        logging.getLogger("payslip.test").exception("boom")
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["level"] == "ERROR" and lines[0]["logger"] == "payslip.test" and lines[0]["time"].endswith("Z")
    assert "71234567" not in lines[0]["message"] and "abcdefghijklmnop" not in lines[0]["message"]
    assert "987654321" not in lines[1]["exception"] and "ValueError" in lines[1]["exception"]


def test_development_logs_are_plain_text_with_an_intact_timestamp(root_logger_restored):
    stream = io.StringIO()
    configure_logging(_settings(app_env="development"), stream=stream)
    logging.getLogger("payslip.test").warning("number +26771234567")
    line = stream.getvalue()
    assert line[:4].isdigit() and "WARNING payslip.test" in line and "71234567" not in line


def test_log_level_comes_from_settings_and_the_file_rotates_privately(root_logger_restored, tmp_path):
    configure_logging(_settings(log_level="WARNING", log_dir=tmp_path / "logs", log_file_max_mb=1, log_file_backups=2), stream=io.StringIO())
    assert logging.getLogger().level == logging.WARNING
    logging.getLogger("x").warning("to the file +26771234567")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (tmp_path / "logs" / "payslip-sender.log").read_text()
    assert "to the file" in text and "71234567" not in text
    rotating = [h for h in logging.getLogger().handlers if h.__class__.__name__ == "RotatingFileHandler"]
    assert rotating and rotating[0].backupCount == 2 and rotating[0].maxBytes == 1024 * 1024


def test_configuring_logging_twice_does_not_duplicate_lines(root_logger_restored):
    stream = io.StringIO()
    configure_logging(_settings(), stream=stream)
    configure_logging(_settings(), stream=stream)
    logging.getLogger("x").error("once")
    assert stream.getvalue().count("once") == 1


def test_uvicorn_access_log_is_off_in_production_only(root_logger_restored):
    configure_logging(_settings(app_env="production"), stream=io.StringIO())
    assert logging.getLogger("uvicorn.access").disabled is True
    configure_logging(_settings(app_env="development"), stream=io.StringIO())
    assert logging.getLogger("uvicorn.access").disabled is False


ALLOWED_LOG_ARGUMENTS = {"unreadable", "interrupted", "resumed"}


def _is_safe_log_argument(node: ast.AST) -> bool:
    """Only exception TYPE names, plain counters and counts[...] may be formatted into a log line."""
    if isinstance(node, ast.Attribute) and node.attr == "__name__" and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "type":
        return True                                                   # type(exc).__name__
    if isinstance(node, ast.Name) and node.id in ALLOWED_LOG_ARGUMENTS:
        return True
    return isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "counts"


def test_no_log_call_in_the_app_formats_anything_but_types_and_counts():
    """Tokens, passwords, phone numbers, ID numbers, names and PDF text can never reach a log through a log call."""
    checked = 0
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        if path.name == "logging_setup.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("log", "logger") and node.func.attr in ("debug", "info", "warning", "error", "exception", "critical")):
                checked += 1
                first = node.args[0]
                assert isinstance(first, ast.Constant) and isinstance(first.value, str), f"{path.name}:{node.lineno} log message must be a plain string"
                assert all(_is_safe_log_argument(a) for a in node.args[1:]), f"{path.name}:{node.lineno} logs something other than a type or count"
                assert not node.keywords or all(k.arg == "exc_info" for k in node.keywords)
    assert checked >= 12


def test_the_app_never_prints_and_never_logs_with_f_strings_outside_scripts():
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        if "scripts" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            assert not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print"), f"print() in {path.name}:{node.lineno}"
