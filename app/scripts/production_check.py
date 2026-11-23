"""Is this server ready for production? Run: python -m app.scripts.production_check   (on the server: sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.production_check)

It changes nothing and sends nothing. It reads the same settings the app reads (the .env file plus real environment variables) and
prints PASS / FAIL / WARN / INFO / SKIP lines, a RESULT line, and plain-language "What to do next" steps. Exit code 0 = no FAIL, 1 = at
least one FAIL. It never prints a secret, key, password, ID number or full phone number: only whether each one is present and strong.

    python -m app.scripts.production_check                          checks this machine
    python -m app.scripts.production_check --url https://payslips.example.com
                                                                    ALSO checks the public address through Caddy (health, /docs, HSTS)

Every outside effect (database, disk, file permissions, web requests) goes through a small "probe" function so tests can replace it.
"""
import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from dotenv import dotenv_values

from app.config import (PROJECT_ROOT, Settings, database_url_problem, encryption_key_problem, session_secret_problem)
from app.scripts.check_live_config import FAIL, INFO, PASS, SKIP, WARN, Report

ENV_FILE = PROJECT_ROOT / ".env"
NAMES = ("APP_ENV", "SESSION_SECRET", "SESSION_COOKIE_SECURE", "ID_ENCRYPTION_KEY", "DATABASE_URL", "WHATSAPP_MODE",
         "WHATSAPP_ALLOWED_RECIPIENTS", "PRIVATE_STORAGE_DIR", "APP_PORT", "WEB_CONCURRENCY")
GB = 1024 ** 3
MIN_FREE_FAIL, MIN_FREE_WARN = 1 * GB, 5 * GB


# ---- the outside world, as replaceable probes -------------------------------------------------------------------------

@dataclass
class FileInfo:
    mode: int            # permission bits, e.g. 0o600
    owner: str


@dataclass
class DbResult:
    ok: bool
    error_code: int | None = None        # the database's own number (1045 = wrong password, 2003 = cannot connect, ...)
    error_type: str = ""
    revision: str | None = None          # the migration the database is at


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]


@dataclass
class Probes:
    settings: Callable[[], object]
    db: Callable[[str], DbResult]
    head_revision: Callable[[], str | None]
    disk: Callable[[Path], tuple[int, int]]                    # (total bytes, free bytes)
    file_info: Callable[[Path], FileInfo | None]
    http_get: Callable[[str], HttpResult | None]               # None = nothing answered
    is_posix: bool
    euid: int | None


def _file_info(path: Path) -> FileInfo | None:
    try:
        st = path.stat()
    except OSError:
        return None
    try:
        import pwd

        owner = pwd.getpwuid(st.st_uid).pw_name
    except Exception:
        owner = str(st.st_uid)
    return FileInfo(st.st_mode & 0o777, owner)


def _db_probe(url: str) -> DbResult:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import DBAPIError

    engine = None
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 5} if url.lower().startswith(("mysql", "mariadb")) else {})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            try:
                revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:
                revision = None                                  # tables not created yet
        return DbResult(True, revision=revision)
    except DBAPIError as exc:
        args = getattr(exc.orig, "args", ())
        return DbResult(False, error_code=args[0] if args and isinstance(args[0], int) else None, error_type=type(exc.orig).__name__)
    except Exception as exc:                                     # bad URL, driver missing, ...
        return DbResult(False, error_type=type(exc).__name__)
    finally:
        if engine is not None:
            engine.dispose()


def _head_revision() -> str | None:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        return ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini"))).get_current_head()
    except Exception:
        return None


def _http_get(url: str) -> HttpResult | None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("production_check needs mocked probes while tests run.")
    try:
        r = httpx.get(url, timeout=5.0, follow_redirects=False)
    except httpx.HTTPError:
        return None
    return HttpResult(r.status_code, {k.lower(): v for k, v in r.headers.items()})


def _disk(path: Path) -> tuple[int, int]:
    usage = shutil.disk_usage(path)
    return usage.total, usage.free


def default_probes(env_file: Path = ENV_FILE) -> Probes:
    return Probes(
        settings=lambda: Settings(_env_file=env_file),
        db=_db_probe, head_revision=_head_revision, disk=_disk, file_info=_file_info, http_get=_http_get,
        is_posix=os.name == "posix", euid=os.geteuid() if hasattr(os, "geteuid") else None,
    )


def load_env(env_file: Path = ENV_FILE) -> dict[str, str]:
    """The raw values the app would read: the .env file, overridden by real environment variables. No validation."""
    from_file = dotenv_values(env_file) if env_file.exists() else {}
    values = {n: (from_file.get(n) or "") for n in NAMES if n in from_file}
    for n in NAMES:
        if n in os.environ:
            values[n] = os.environ[n]
    return values


def storage_dir(env: dict[str, str]) -> Path:
    raw = env.get("PRIVATE_STORAGE_DIR", "").strip() or "private_storage"
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


# ---- the checks -------------------------------------------------------------------------------------------------------

def check_settings_load(probes: Probes, report: Report) -> object | None:
    """The app itself refuses to start when production settings are unsafe: show its reasons in plain words."""
    try:
        settings = probes.settings()
    except Exception as exc:
        text = " ".join(str(exc).split())
        reasons = text.split("refuses to start: ", 1)[1] if "refuses to start: " in text else ""
        lines = [f"- {r.strip()}" for r in reasons.split("; ") if r.strip()] or ["The settings could not be read: " + type(exc).__name__]
        report.add(FAIL, "The app would REFUSE TO START with these settings", *lines,
                   fix="Fix the settings listed above in the .env file (the app user's copy), then run this check again.")
        return None
    report.add(PASS, "The app accepts these settings (it would start)")
    return settings


def check_environment(env: dict[str, str], report: Report) -> None:
    value = env.get("APP_ENV", "").strip().lower()
    if value == "production":
        report.add(PASS, "APP_ENV is production (debug off, /docs off, Secure cookies required)")
    else:
        report.add(FAIL, f"APP_ENV is '{value or 'not set'}', not production",
                   "Development mode has weaker safety rules and must not face the internet.",
                   fix="Set APP_ENV=production in .env.")


def check_workers(env: dict[str, str], report: Report) -> None:
    raw = env.get("WEB_CONCURRENCY", "").strip()
    if raw and (not raw.isdigit() or int(raw) != 1):
        report.add(FAIL, "WEB_CONCURRENCY asks for more than one worker",
                   "The app must run as exactly ONE process (see the README, 'Why exactly one process').",
                   fix="Remove WEB_CONCURRENCY from .env and from the service file, or set it to 1.")
    else:
        report.add(PASS, "Exactly one app process is configured")


def check_cookies(env: dict[str, str], report: Report) -> None:
    raw = env.get("SESSION_COOKIE_SECURE", "").strip().lower()
    if raw in ("false", "0", "no", "off"):
        report.add(FAIL, "Session cookies are NOT marked Secure (SESSION_COOKIE_SECURE is off)",
                   "Browsers could send the login cookie over plain HTTP.", fix="Remove SESSION_COOKIE_SECURE from .env (it is on by default in production) or set it to true.")
    else:
        report.add(PASS, "Session cookies are marked Secure (HTTPS only), HttpOnly and SameSite=Lax")


def check_secrets(env: dict[str, str], report: Report) -> None:
    """Only whether each value is present and strong. The values are never printed."""
    for label, problem in (("SESSION_SECRET", session_secret_problem(env.get("SESSION_SECRET", ""))),
                           ("ID_ENCRYPTION_KEY", encryption_key_problem(env.get("ID_ENCRYPTION_KEY", ""))),
                           ("DATABASE_URL", database_url_problem(env.get("DATABASE_URL", "")))):
        if problem:
            report.add(FAIL, f"{label} is not good enough", problem, fix=problem)
        else:
            report.add(PASS, f"{label} is present and {'strong' if label != 'DATABASE_URL' else 'not a placeholder'}")
    if len(env.get("SESSION_SECRET", "")) < 32 and not session_secret_problem(env.get("SESSION_SECRET", "")):
        report.add(WARN, "SESSION_SECRET is shorter than 32 characters", fix="Run: python -m app.scripts.generate_secret (it writes a long random one).")


def check_env_file(env_file: Path, probes: Probes, report: Report) -> None:
    if not probes.is_posix:
        report.add(SKIP, "Permissions of the .env file: not checked (only possible on Linux)")
        return
    info = probes.file_info(env_file)
    if info is None:
        report.add(INFO, "There is no .env file here: settings come from real environment variables only")
        return
    if info.mode & 0o077:
        report.add(FAIL, f"The .env file can be read by other users (permissions {info.mode:o})",
                   "It holds the database password, the session secret and the encryption key.", fix=f"Run: chmod 600 {env_file}")
    else:
        report.add(PASS, f"The .env file is private (permissions {info.mode:o}, owner {info.owner})")
    if probes.euid == 0:
        report.add(WARN, "You are running this check as root", "The service runs as its own user, which may see different permissions.",
                   fix="Run it as the app user: sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.production_check")


def check_whatsapp(env: dict[str, str], report: Report) -> None:
    mode = env.get("WHATSAPP_MODE", "mock").strip().lower() or "mock"
    allowed = [p for p in env.get("WHATSAPP_ALLOWED_RECIPIENTS", "").split(",") if p.strip()]
    if mode == "mock":
        report.add(WARN, "WHATSAPP_MODE is MOCK: NO REAL MESSAGES WILL BE SENT",
                   "Fine for a first start and for testing. Employees will receive nothing.",
                   fix="When the live preflight passes (python -m app.scripts.check_live_config) and you are ready to test for real, set WHATSAPP_MODE=live and restart.")
    elif mode == "live":
        if allowed:
            report.add(WARN, f"LIVE mode is ON but WHATSAPP_ALLOWED_RECIPIENTS is STILL SET ({len(allowed)} number{'s' if len(allowed) != 1 else ''}): "
                             "ONLY those numbers will receive payslips, everyone else will FAIL",
                       fix="That is right while you test. Once ONE test batch reached your own phone, clear WHATSAPP_ALLOWED_RECIPIENTS in .env and restart, or real sends to employees will fail.")
        else:
            report.add(PASS, "LIVE mode is on and the test allowlist is cleared: payslips go to every employee in a batch",
                       "Make sure you really sent one test batch to yourself first.")
    else:
        report.add(FAIL, "WHATSAPP_MODE is not 'mock' or 'live'", fix="Set WHATSAPP_MODE=mock or WHATSAPP_MODE=live in .env.")


DB_HELP = {
    1045: "the database user or password is wrong",
    1049: "the database name does not exist",
    2002: "the database server is not running or not reachable",
    2003: "the database server is not running or not reachable",
    1044: "the database user has no rights on that database",
}


def check_database(env: dict[str, str], probes: Probes, report: Report) -> None:
    url = env.get("DATABASE_URL", "")
    if database_url_problem(url):
        report.add(SKIP, "Database connection: not checked (DATABASE_URL must be fixed first)")
        return
    result = probes.db(url)
    if not result.ok:
        why = DB_HELP.get(result.error_code or 0, f"error type {result.error_type or 'unknown'}")
        report.add(FAIL, "Cannot connect to the database", f"Reason: {why}.",
                   fix="Check that MariaDB is running (sudo systemctl status mariadb) and that DATABASE_URL has the right user, password and database name.")
        return
    report.add(PASS, "The database answers")
    head = probes.head_revision()
    if result.revision is None:
        report.add(FAIL, "The database tables have not been created yet", fix="Run the migrations: .venv/bin/python -m alembic upgrade head (the update script does this).")
    elif head and result.revision != head:
        report.add(FAIL, "The database is not up to date with the code",
                   fix="Run the migrations: .venv/bin/python -m alembic upgrade head (the update script does this).")
    elif head:
        report.add(PASS, "The database structure is up to date")


def check_storage(env: dict[str, str], probes: Probes, report: Report) -> None:
    path = storage_dir(env)
    static = (PROJECT_ROOT / "app" / "static").resolve()
    try:
        inside = static == path.resolve() or static in path.resolve().parents
    except OSError:
        inside = False
    if inside:
        report.add(FAIL, "The private storage folder is inside the public static folder", "Payslips would be downloadable by anyone.",
                   fix="Set PRIVATE_STORAGE_DIR to a folder outside app/static, for example /var/lib/payslip-sender/storage.")
        return
    if not path.is_dir():
        report.add(FAIL, "The private storage folder does not exist", fix=f"Create it as the app user: mkdir -p {path} && chmod 700 {path} (the setup script does this).")
        return
    try:
        with tempfile.NamedTemporaryFile(dir=path):
            pass
    except OSError:
        report.add(FAIL, "The private storage folder is not writable by this user", fix=f"Give the app user ownership: sudo chown -R payslip:payslip {path}")
        return
    report.add(PASS, "The private storage folder exists and is writable")
    if probes.is_posix:
        info = probes.file_info(path)
        if info and info.mode & 0o077:
            report.add(FAIL, f"The private storage folder can be opened by other users (permissions {info.mode:o})", fix=f"Run: chmod 700 {path}")
        elif info:
            report.add(PASS, f"The private storage folder is private (permissions {info.mode:o})")


def check_disk(env: dict[str, str], probes: Probes, report: Report) -> None:
    path = storage_dir(env)
    target = path if path.exists() else PROJECT_ROOT
    total, free = probes.disk(target)
    free_gb = free / GB
    used_pct = 100 - round(100 * free / total) if total else 0
    summary = f"{free_gb:.1f} GB free ({used_pct}% used)"
    if free < MIN_FREE_FAIL:
        report.add(FAIL, f"Almost no disk space left: {summary}", "Uploads, the database and backups will start failing.",
                   fix="Free space: delete old backups in the backup folder, run python -m app.scripts.purge_old_batches, or enlarge the EBS volume in the AWS console.")
    elif free < MIN_FREE_WARN:
        report.add(WARN, f"Disk space is getting low: {summary}", fix="Plan to enlarge the EBS volume or clear old backups soon.")
    else:
        report.add(PASS, f"Enough disk space: {summary}")


def check_docs_off(settings, env: dict[str, str], probes: Probes, report: Report) -> None:
    from app.main import create_app

    try:
        app = create_app(settings) if settings is not None else None
    except Exception as exc:
        report.add(FAIL, "Could not build the app to inspect it", type(exc).__name__)
        return
    if app is not None:
        if app.docs_url or app.redoc_url or app.openapi_url:
            report.add(FAIL, "/docs, /redoc or /openapi.json is switched ON", "They list every route of the app.", fix="This is a code problem: update to the latest version of the app.")
        else:
            report.add(PASS, "/docs, /redoc and /openapi.json do not exist")
    port = env.get("APP_PORT", "").strip() or "8000"
    answer = probes.http_get(f"http://127.0.0.1:{port}/docs")
    if answer is None:
        report.add(INFO, "The app is not running on this machine right now, so /docs was not tried live")
    elif answer.status == 404:
        report.add(PASS, "The running app answers 404 for /docs")
    else:
        report.add(FAIL, f"The running app answers {answer.status} for /docs", fix="Restart the service so it runs the current code: sudo systemctl restart payslip-sender")


def check_public_url(url: str, probes: Probes, report: Report) -> None:
    base = url.rstrip("/")
    if not base.startswith("https://"):
        report.add(FAIL, "The public address must start with https://", fix="Use --url https://your-domain")
        return
    health = probes.http_get(f"{base}/health")
    if health is None:
        report.add(FAIL, "Nothing answered at the public address", "DNS, the firewall (ports 80 and 443) or Caddy may be wrong.",
                   fix="Check the DNS record points at the server's Elastic IP, the AWS security group allows 80 and 443, and: sudo systemctl status caddy")
        return
    if health.status == 200:
        report.add(PASS, "The public address answers /health through HTTPS")
    else:
        report.add(FAIL, f"The public address answers {health.status} for /health", fix="Check: sudo systemctl status payslip-sender caddy")
    if "strict-transport-security" in health.headers:
        report.add(PASS, "HSTS is sent (browsers will insist on HTTPS)")
    else:
        report.add(FAIL, "No Strict-Transport-Security header on the public address", fix="Use the Caddyfile from the deploy folder, which sets HSTS.")
    docs = probes.http_get(f"{base}/docs")
    if docs is not None and docs.status != 404:
        report.add(FAIL, f"/docs answers {docs.status} on the public address", fix="Update and restart the app.")
    else:
        report.add(PASS, "/docs is not reachable on the public address")
    plain = probes.http_get("http://" + base[len("https://"):] + "/health")
    if plain is not None and plain.status in (301, 302, 307, 308):
        report.add(PASS, "Plain http:// is redirected to https://")


def run_checks(env: dict[str, str], probes: Probes, env_file: Path = ENV_FILE, url: str | None = None) -> Report:
    report = Report()
    check_environment(env, report)
    check_workers(env, report)
    check_cookies(env, report)
    check_secrets(env, report)
    settings = check_settings_load(probes, report)
    check_env_file(env_file, probes, report)
    check_whatsapp(env, report)
    check_database(env, probes, report)
    check_storage(env, probes, report)
    check_disk(env, probes, report)
    check_docs_off(settings, env, probes, report)
    if url:
        check_public_url(url, probes, report)
    return report


def render(report: Report) -> str:
    lines = ["Production readiness check (changes nothing, sends nothing)", "=" * 60]
    for check in report.checks:
        lines.append(f"[{check.status}] {check.title}")
        lines.extend(f"       {d}" for d in check.detail)
    lines += ["", "=" * 60]
    if report.failed:
        lines.append(f"RESULT: FAIL ({len(report.failed)} problem{'s' if len(report.failed) != 1 else ''} to fix before this server goes live)")
    else:
        lines.append("RESULT: PASS" + (f" (with {len(report.warned)} warning{'s' if len(report.warned) != 1 else ''}: read them)" if report.warned else ""))
    steps = [c for c in report.checks if c.status in (FAIL, WARN) and c.fix]
    if steps:
        lines += ["", "What to do next:"]
        seen = set()
        for c in steps:
            if (c.title, c.fix) in seen:
                continue
            seen.add((c.title, c.fix))
            lines += [f" {len(seen)}. [{c.status}] {c.title}", f"      {c.fix}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None, probes: Probes | None = None, env_file: Path = ENV_FILE) -> int:
    parser = argparse.ArgumentParser(description="Checks that this server is configured safely for production. Changes nothing.")
    parser.add_argument("--url", help="also check the public address, for example https://payslips.example.com")
    args = parser.parse_args(argv)
    if probes is None and os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("production_check needs mocked probes while tests run.")
    env = load_env(env_file) if env is None else env
    report = run_checks(env, probes or default_probes(env_file), env_file, args.url)
    print(render(report))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
