"""python -m app.scripts.production_check: every check, with the outside world replaced by fakes."""
import secrets
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.scripts import production_check as pc
from app.scripts.check_live_config import FAIL, INFO, PASS, SKIP, WARN
from tests.test_production import GOOD_DB, build

GB = 1024 ** 3
SECRET, KEY = secrets.token_urlsafe(48), Fernet.generate_key().decode()


def good_env(tmp_path: Path, **changes) -> dict:
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    env = {"APP_ENV": "production", "SESSION_SECRET": SECRET, "ID_ENCRYPTION_KEY": KEY, "DATABASE_URL": GOOD_DB, "WHATSAPP_MODE": "live",
           "WHATSAPP_ALLOWED_RECIPIENTS": "", "PRIVATE_STORAGE_DIR": str(storage), "APP_PORT": "8000"}
    env.update(changes)
    return env


def good_probes(**changes) -> pc.Probes:
    values = dict(
        settings=lambda: build(session_secret=SECRET, id_encryption_key=KEY),
        db=lambda url: pc.DbResult(True, revision="head1"),
        head_revision=lambda: "head1",
        disk=lambda path: (100 * GB, 60 * GB),
        file_info=lambda path: pc.FileInfo(0o600 if path.name == ".env" else 0o700, "payslip"),
        http_get=lambda url: pc.HttpResult(404, {}) if url.endswith("/docs") else None,
        is_posix=True, euid=1001,
    )
    values.update(changes)
    return pc.Probes(**values)


def run(tmp_path, env_changes=None, url=None, **probe_changes):
    env = good_env(tmp_path, **(env_changes or {}))
    return pc.run_checks(env, good_probes(**probe_changes), tmp_path / ".env", url)


def statuses(report, text):
    return [c.status for c in report.checks if text.lower() in c.title.lower()]


def test_a_good_server_passes_with_no_failures(tmp_path):
    report = run(tmp_path)
    assert report.failed == [] and report.warned == []
    text = pc.render(report)
    assert "RESULT: PASS" in text and "FAIL" not in text


@pytest.mark.parametrize("changes, title", [
    ({"APP_ENV": "development"}, "APP_ENV is"),
    ({"APP_ENV": ""}, "APP_ENV is"),
    ({"SESSION_SECRET": "change-me"}, "SESSION_SECRET is not good enough"),
    ({"SESSION_SECRET": "a" * 40}, "SESSION_SECRET is not good enough"),
    ({"ID_ENCRYPTION_KEY": "dGVzdC1rZXktZm9yLXRlc3RzLW9ubHktMzItYnl0ZXM="}, "ID_ENCRYPTION_KEY is not good enough"),
    ({"ID_ENCRYPTION_KEY": ""}, "ID_ENCRYPTION_KEY is not good enough"),
    ({"DATABASE_URL": "mysql+pymysql://u:YOUR_DB_PASSWORD@h/db"}, "DATABASE_URL is not good enough"),
    ({"DATABASE_URL": "sqlite://"}, "DATABASE_URL is not good enough"),
    ({"SESSION_COOKIE_SECURE": "false"}, "NOT marked Secure"),
    ({"SESSION_COOKIE_SECURE": "0"}, "NOT marked Secure"),
    ({"WEB_CONCURRENCY": "4"}, "WEB_CONCURRENCY"),
    ({"WEB_CONCURRENCY": "auto"}, "WEB_CONCURRENCY"),
    ({"WHATSAPP_MODE": "sandbox"}, "WHATSAPP_MODE is not"),
])
def test_each_unsafe_setting_is_a_failure_with_a_fix(tmp_path, changes, title):
    report = run(tmp_path, changes)
    failing = [c for c in report.failed if title.lower() in c.title.lower()]
    assert failing and failing[0].fix, [c.title for c in report.failed]


def test_secure_cookies_and_a_single_worker_pass_when_unset_or_one(tmp_path):
    for changes in ({}, {"SESSION_COOKIE_SECURE": "true"}, {"WEB_CONCURRENCY": "1"}):
        report = run(tmp_path, changes)
        assert statuses(report, "Session cookies") == [PASS] and statuses(report, "one app process") == [PASS]


def test_a_short_but_unpredictable_session_secret_only_warns(tmp_path):
    report = run(tmp_path, {"SESSION_SECRET": "aB3dE5gH7jK9"})
    assert statuses(report, "shorter than 32") == [WARN]


def test_when_the_app_would_refuse_to_start_the_reasons_are_shown_in_plain_words(tmp_path):
    def broken():
        return build(session_secret="change-me" * 6, app_host="0.0.0.0")

    report = run(tmp_path, settings=broken)
    (check,) = [c for c in report.failed if "REFUSE TO START" in c.title]
    assert any("SESSION_SECRET" in d for d in check.detail) and any("APP_HOST" in d for d in check.detail)


def test_a_settings_crash_of_any_other_kind_still_gives_a_clean_failure(tmp_path):
    def crash():
        raise RuntimeError("secret detail " + SECRET)

    text = pc.render(run(tmp_path, settings=crash))
    assert "RuntimeError" in text and SECRET not in text and "secret detail" not in text


# ---- file permissions ----

@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_an_env_file_other_users_can_read_fails(tmp_path, mode):
    report = run(tmp_path, file_info=lambda p: pc.FileInfo(mode if p.name == ".env" else 0o700, "payslip"))
    (check,) = [c for c in report.failed if ".env file can be read" in c.title]
    assert "chmod 600" in check.fix


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_a_private_env_file_passes(tmp_path, mode):
    report = run(tmp_path, file_info=lambda p: pc.FileInfo(mode if p.name == ".env" else 0o700, "payslip"))
    assert statuses(report, ".env file is private") == [PASS]


def test_permissions_are_skipped_on_windows_and_a_missing_env_file_is_only_information(tmp_path):
    assert statuses(run(tmp_path, is_posix=False), "Permissions of the .env") == [SKIP]
    report = run(tmp_path, file_info=lambda p: None if p.name == ".env" else pc.FileInfo(0o700, "payslip"))
    assert statuses(report, "no .env file") == [INFO]


def test_running_the_check_as_root_is_a_warning(tmp_path):
    assert statuses(run(tmp_path, euid=0), "as root") == [WARN]


# ---- WhatsApp mode ----

def test_mock_mode_in_production_is_a_loud_warning(tmp_path):
    (check,) = [c for c in run(tmp_path, {"WHATSAPP_MODE": "mock"}).warned if "MOCK" in c.title]
    assert "NO REAL MESSAGES" in check.title and check.fix


def test_blank_mode_means_mock(tmp_path):
    assert any("MOCK" in c.title for c in run(tmp_path, {"WHATSAPP_MODE": ""}).warned)


def test_live_mode_with_the_test_allowlist_still_on_is_a_loud_warning_and_never_prints_the_numbers(tmp_path):
    report = run(tmp_path, {"WHATSAPP_ALLOWED_RECIPIENTS": "+26771234567, +26772345678"})
    (check,) = [c for c in report.warned if "ALLOWED_RECIPIENTS" in c.title]
    assert "STILL SET (2 numbers)" in check.title and "FAIL" in check.title and "clear WHATSAPP_ALLOWED_RECIPIENTS" in check.fix
    text = pc.render(report)
    assert "71234567" not in text and "72345678" not in text


def test_live_mode_with_the_allowlist_cleared_passes(tmp_path):
    report = run(tmp_path)
    assert statuses(report, "LIVE mode is on") == [PASS] and not any("MOCK" in c.title for c in report.warned)


# ---- database ----

@pytest.mark.parametrize("code, words", [(1045, "user or password"), (1049, "does not exist"), (2003, "not running"), (1044, "no rights"), (None, "error type OperationalError")])
def test_a_database_that_cannot_be_reached_fails_in_plain_words(tmp_path, code, words):
    report = run(tmp_path, db=lambda url: pc.DbResult(False, error_code=code, error_type="OperationalError"))
    (check,) = [c for c in report.failed if "Cannot connect" in c.title]
    assert any(words in d for d in check.detail)


def test_the_database_url_is_never_printed_even_when_the_connection_fails(tmp_path):
    seen = []
    report = run(tmp_path, db=lambda url: (seen.append(url), pc.DbResult(False, error_code=1045))[1])
    assert seen == [GOOD_DB] and "Zx81qLmN4pRt7VbC" not in pc.render(report)


def test_tables_not_created_or_out_of_date_fail(tmp_path):
    assert statuses(run(tmp_path, db=lambda u: pc.DbResult(True, revision=None)), "tables have not been created") == [FAIL]
    assert statuses(run(tmp_path, db=lambda u: pc.DbResult(True, revision="old")), "not up to date") == [FAIL]
    assert statuses(run(tmp_path), "structure is up to date") == [PASS]


def test_the_database_is_not_probed_when_its_url_is_bad(tmp_path):
    called = []
    report = run(tmp_path, {"DATABASE_URL": "sqlite://"}, db=lambda u: called.append(u))
    assert called == [] and statuses(report, "Database connection") == [SKIP]


# ---- storage and disk ----

def test_a_missing_storage_folder_fails(tmp_path):
    report = run(tmp_path, {"PRIVATE_STORAGE_DIR": str(tmp_path / "nope")})
    assert statuses(report, "does not exist") == [FAIL]


def test_storage_inside_the_public_static_folder_fails(tmp_path):
    static = pc.PROJECT_ROOT / "app" / "static"
    assert statuses(run(tmp_path, {"PRIVATE_STORAGE_DIR": str(static)}), "inside the public static") == [FAIL]
    assert statuses(run(tmp_path, {"PRIVATE_STORAGE_DIR": str(static / "sub")}), "inside the public static") == [FAIL]


def test_storage_that_other_users_can_open_fails(tmp_path):
    report = run(tmp_path, file_info=lambda p: pc.FileInfo(0o600 if p.name == ".env" else 0o755, "payslip"))
    assert statuses(report, "storage folder can be opened by other users") == [FAIL]
    assert statuses(run(tmp_path), "storage folder is private") == [PASS]


def test_a_storage_folder_that_cannot_be_written_fails(tmp_path, monkeypatch):
    def deny(*a, **k):
        raise PermissionError

    monkeypatch.setattr(pc.tempfile, "NamedTemporaryFile", deny)
    assert statuses(run(tmp_path), "not writable") == [FAIL]


@pytest.mark.parametrize("free_gb, expected", [(0.2, FAIL), (3, WARN), (6, PASS), (60, PASS)])
def test_free_disk_space_thresholds(tmp_path, free_gb, expected):
    report = run(tmp_path, disk=lambda p: (100 * GB, int(free_gb * GB)))
    assert [c.status for c in report.checks if "disk space" in c.title.lower()] == [expected]


# ---- /docs ----

def test_docs_are_checked_in_the_code_and_in_the_running_app(tmp_path):
    report = run(tmp_path)
    assert statuses(report, "/docs, /redoc and /openapi.json do not exist") == [PASS] and statuses(report, "running app answers 404") == [PASS]


def test_a_running_app_that_serves_docs_fails(tmp_path):
    report = run(tmp_path, http_get=lambda url: pc.HttpResult(200, {}))
    assert statuses(report, "answers 200 for /docs") == [FAIL]


def test_an_app_that_is_not_running_is_only_information(tmp_path):
    assert statuses(run(tmp_path, http_get=lambda url: None), "not running on this machine") == [INFO]


def test_the_live_probe_uses_the_configured_port(tmp_path):
    seen = []
    run(tmp_path, {"APP_PORT": "9123"}, http_get=lambda url: (seen.append(url), None)[1])
    assert seen == ["http://127.0.0.1:9123/docs"]


# ---- the public address (--url) ----

def fake_site(health=200, hsts=True, docs=404, redirect=True):
    def get(url):
        if url.startswith("http://127.0.0.1"):
            return None
        if url.startswith("http://"):
            return pc.HttpResult(308, {}) if redirect else pc.HttpResult(200, {})
        if url.endswith("/health"):
            return pc.HttpResult(health, {"strict-transport-security": "max-age=1"} if hsts else {})
        return pc.HttpResult(docs, {})
    return get


def test_the_public_address_checks_pass_for_a_good_site(tmp_path):
    report = run(tmp_path, url="https://payslips.example.com", http_get=fake_site())
    assert report.failed == [] and statuses(report, "redirected to https") == [PASS]


@pytest.mark.parametrize("kwargs, title", [({"hsts": False}, "Strict-Transport-Security"), ({"health": 502}, "502 for /health"), ({"docs": 200}, "/docs answers 200")])
def test_public_address_problems_fail(tmp_path, kwargs, title):
    report = run(tmp_path, url="https://payslips.example.com", http_get=fake_site(**kwargs))
    assert any(title in c.title for c in report.failed)


def test_nothing_answering_at_the_public_address_explains_where_to_look(tmp_path):
    report = run(tmp_path, url="https://payslips.example.com", http_get=lambda u: None if "example" in u else None)
    (check,) = [c for c in report.failed if "Nothing answered" in c.title]
    assert "DNS" in " ".join(check.detail) and "security group" in check.fix


def test_a_public_address_without_https_is_refused(tmp_path):
    assert statuses(run(tmp_path, url="http://payslips.example.com"), "must start with https") == [FAIL]


# ---- output, secrets and exit code ----

def test_no_secret_or_key_ever_appears_in_the_output_even_when_everything_is_wrong(tmp_path):
    env = good_env(tmp_path, APP_ENV="development", WHATSAPP_ALLOWED_RECIPIENTS="+26771234567")
    text = pc.render(pc.run_checks(env, good_probes(db=lambda u: pc.DbResult(False, error_code=1045)), tmp_path / ".env"))
    for secret in (SECRET, KEY, "Zx81qLmN4pRt7VbC", "+26771234567", "71234567"):
        assert secret not in text


def test_fixes_are_listed_once_each_under_what_to_do_next(tmp_path):
    text = pc.render(run(tmp_path, {"APP_ENV": "development", "WHATSAPP_MODE": "mock"}))
    assert "RESULT: FAIL (1 problem" in text and "What to do next:" in text and "Set APP_ENV=production" in text


def test_main_exit_codes(tmp_path, capsys):
    ok = pc.main([], env=good_env(tmp_path), probes=good_probes(), env_file=tmp_path / ".env")
    assert ok == 0 and "RESULT: PASS" in capsys.readouterr().out
    bad = pc.main([], env=good_env(tmp_path, APP_ENV="development"), probes=good_probes(), env_file=tmp_path / ".env")
    assert bad == 1 and "RESULT: FAIL" in capsys.readouterr().out


def test_main_refuses_to_use_real_probes_while_tests_run(tmp_path):
    with pytest.raises(RuntimeError):
        pc.main([], env=good_env(tmp_path))


def test_load_env_reads_the_file_then_real_environment_variables(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("APP_ENV=development  # comment\nWHATSAPP_MODE=mock\nSOMETHING_ELSE=1\n", encoding="utf-8")
    monkeypatch.setenv("WHATSAPP_MODE", "live")
    env = pc.load_env(f)
    assert env["APP_ENV"] == "development" and env["WHATSAPP_MODE"] == "live" and "SOMETHING_ELSE" not in env


def test_the_real_probes_work_against_a_real_sqlite_file_and_report_failures_without_the_url(tmp_path):
    ok = pc._db_probe(f"sqlite:///{tmp_path / 'a.db'}")
    assert ok.ok and ok.revision is None
    bad = pc._db_probe("mysql+pymysql://u:Sup3rSecretPw@127.0.0.1:1/none")
    assert bad.ok is False and "Sup3rSecretPw" not in repr(bad)
    info = pc._file_info(tmp_path)
    assert info is not None and pc._file_info(tmp_path / "missing") is None
    total, free = pc._disk(tmp_path)
    assert total > free > 0
