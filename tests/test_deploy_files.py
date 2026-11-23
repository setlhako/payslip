"""The deployment files (deploy/, requirements, .env examples) checked as text: they cannot be run on the dev PC, so these tests
pin the safety properties that matter (and that a future edit must not quietly remove)."""
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest
from dotenv import dotenv_values

from app.config import PROJECT_ROOT, Settings
from app.services import body_limit

DEPLOY = PROJECT_ROOT / "deploy"


def text(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def code_lines(name: str) -> list[str]:
    return [l.strip() for l in text(name).splitlines() if l.strip() and not l.strip().startswith("#")]


EXPECTED = ["Caddyfile.template", "app-run.sh", "backup.sh", "env.production.example", "payslip-backup.service", "payslip-backup.timer",
            "payslip-purge.service", "payslip-purge.timer", "payslip-sender.service", "restore.sh", "setup-server.sh", "update.sh"]


def test_every_deploy_file_exists_and_uses_unix_line_endings():
    for name in EXPECTED:
        data = (DEPLOY / name).read_bytes()
        assert data and b"\r" not in data, f"{name} is missing or has Windows line endings (Linux cannot run it)"
    attributes = (PROJECT_ROOT / ".gitattributes").read_text()
    assert "*.sh text eol=lf" in attributes and "deploy/* text eol=lf" in attributes


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
@pytest.mark.parametrize("script", ["setup-server.sh", "update.sh", "backup.sh", "restore.sh", "app-run.sh"])
def test_the_shell_scripts_have_valid_syntax(script):
    result = subprocess.run(["bash", "-n", str(DEPLOY / script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", ["setup-server.sh", "update.sh", "backup.sh", "restore.sh", "app-run.sh"])
def test_scripts_stop_at_the_first_error_and_start_with_a_shebang(script):
    body = text(script)
    assert body.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in body


# ---- the systemd units ----

def test_the_app_service_runs_one_unprivileged_sandboxed_process():
    unit = text("payslip-sender.service")
    for line in ("User=payslip", "Group=payslip", "Restart=on-failure", "NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true",
                 "PrivateTmp=true", "UMask=0077", "WEB_CONCURRENCY=1", "WorkingDirectory=/opt/payslip-sender",
                 "ExecStart=/opt/payslip-sender/.venv/bin/python -m app.server"):
        assert line in unit, line
    assert "root" not in re.findall(r"^User=(.*)$", unit, re.M)
    code = chr(10).join(code_lines("payslip-sender.service"))              # (comments may MENTION --reload)
    assert "--reload" not in code and "--workers" not in code and "-w " not in code
    assert re.search(r"^ReadWritePaths=/var/lib/payslip-sender /var/log/payslip-sender$", unit, re.M)
    assert "Requires=mariadb" not in unit                                      # a database restart must not restart the app mid-send
    assert not re.search(r"(SECRET|PASSWORD|TOKEN|KEY)=\S", unit)               # no secret in the unit file


def test_the_purge_timer_runs_the_purge_command_every_day():
    service, timer = text("payslip-purge.service"), text("payslip-purge.timer")
    assert "-m app.scripts.purge_old_batches" in service and "User=payslip" in service and "Type=oneshot" in service
    assert re.search(r"OnCalendar=\*-\*-\* \d\d:\d\d", timer) and "Persistent=true" in timer and "WantedBy=timers.target" in timer


def test_the_backup_timer_runs_nightly_as_the_app_user():
    service, timer = text("payslip-backup.service"), text("payslip-backup.timer")
    assert "deploy/backup.sh" in service and "User=payslip" in service and "ReadWritePaths=/var/backups/payslip-sender" in service
    assert re.search(r"OnCalendar=\*-\*-\* 0[0-5]:\d\d", timer) and "Persistent=true" in timer


# ---- Caddy ----

def test_the_caddyfile_has_https_hsts_compression_a_size_limit_and_a_clean_access_log():
    caddy = text("Caddyfile.template")
    assert "__DOMAIN__" in caddy and "__EMAIL__" in caddy
    assert "encode zstd gzip" in caddy and "Strict-Transport-Security" in caddy and "reverse_proxy 127.0.0.1:8000" in caddy
    assert "-Server" in caddy
    assert "http://" not in "\n".join(code_lines("Caddyfile.template"))       # no plain-http site: Caddy redirects to HTTPS by itself
    log_block = caddy[caddy.index("log {"):caddy.index("@uploads")]
    assert 'request>uri regexp \\?.* ""' in log_block and "request>headers>Cookie delete" in log_block and "request>headers>Authorization delete" in log_block


def megabytes(label: str) -> int:
    return int(re.search(r"max_size (\d+)MB", label).group(1))


def test_the_caddy_size_limits_fit_the_app_upload_limits():
    caddy = text("Caddyfile.template")
    uploads = megabytes(caddy[caddy.index("handle @uploads"):])
    everything_else = megabytes(caddy[caddy.index("\thandle {"):])
    settings = Settings(_env_file=None, session_secret="s" * 40, database_url="sqlite://", id_encryption_key="dGVzdC1rZXktZm9yLXRlc3RzLW9ubHktMzItYnl0ZXM=")
    app_upload_mb = settings.max_batch_total_mb + body_limit.UPLOAD_OVERHEAD / (1024 * 1024)
    assert uploads >= app_upload_mb                                          # Caddy must not cut off an upload the app allows
    assert uploads <= app_upload_mb + 20                                     # ...but must not be far looser either
    assert everything_else * 1024 * 1024 >= 2 * 1024 * 1024                  # Meta's webhook may send up to 2 MB
    assert everything_else <= 5
    assert "/batches/new" in caddy and "/batches/*/upload" in caddy


def test_the_optional_admin_ip_restriction_is_present_but_switched_off():
    caddy = text("Caddyfile.template")
    block = caddy[caddy.index("@outsiders"):caddy.index("respond @outsiders")]
    assert "not remote_ip" in block and "not path /webhook/whatsapp /health" in block
    for line in (l for l in caddy.splitlines() if "outsiders" in l or "remote_ip" in l or "not path" in l):
        assert line.strip().startswith("#"), f"must be commented out by default: {line!r}"
    assert not any("remote_ip" in l or "outsiders" in l for l in code_lines("Caddyfile.template"))
    assert "Locked yourself out" in caddy


# ---- the server setup script ----

def test_the_firewall_allows_only_ssh_http_and_https():
    setup = text("setup-server.sh")
    allowed = re.findall(r"^ufw allow (\S+)$", setup, re.M)
    assert sorted(allowed) == ["22/tcp", "443/tcp", "80/tcp"]
    assert "ufw default deny incoming" in setup and "ufw --force enable" in setup
    assert setup.index("ufw allow 22/tcp") < setup.index("ufw --force enable")      # SSH is opened BEFORE the firewall is switched on


def test_the_setup_script_installs_what_production_needs_and_secures_it():
    setup = text("setup-server.sh")
    for needle in ("mariadb-server", "unattended-upgrades", "APT::Periodic::Unattended-Upgrade", "age ", "caddy", "ufw", "python3-venv",
                   "useradd --system", "/usr/sbin/nologin", 'chmod 600 "$ENV_FILE"', "CHARACTER SET utf8mb4", "bind-address = 127.0.0.1",
                   "caddy validate", "alembic upgrade head", "generate_secret", "generate_encryption_key", "pip install -r"):
        assert needle in setup, needle
    assert 'ENV_FILE="$APP_DIR/.env"' in setup and "install -m 600" in setup


def test_the_setup_script_never_prints_a_password_or_key():
    """The database password is created and written to files only; nothing echoes it, and it never appears on a command line."""
    lines = [l for l in text("setup-server.sh").splitlines() if not l.strip().startswith("#")]
    for line in lines:
        if re.match(r"\s*(echo|printf|say)\b", line):
            assert not re.search(r"\$\{?(DB_PASS|SESSION_SECRET|ID_ENCRYPTION_KEY|ENV_VALUE|CURRENT_URL)\b", line) or "cnf" in line, line
    assert "ID_ENCRYPTION_KEY" in text("setup-server.sh")                      # (named in the instructions, never its value)
    assert re.search(r"mariadb <<SQL", text("setup-server.sh"))                 # the SQL (with the password) goes in through stdin, not argv
    assert "mysql -p" not in text("setup-server.sh") and "--password" not in text("setup-server.sh")


def test_the_setup_script_is_safe_to_run_again():
    setup = text("setup-server.sh")
    for needle in ("IF NOT EXISTS", "if ! id", "if [ ! -f \"$ENV_FILE\" ]", "Keeping your existing /etc/caddy/Caddyfile", "unset DB_PASS",
                   'if [ ! -x "$APP_DIR/.venv/bin/python" ]', "if [ ! -f \"$CONF_DIR/backup.conf\" ]"):
        assert needle in setup, needle
    assert "rm -rf" not in setup and "DROP DATABASE IF EXISTS `$DB_NAME`" not in setup       # it never deletes the real database
    assert sum(1 for l in setup.splitlines() if l.strip().startswith("#")) >= 40      # every step is commented


# ---- update, backup, restore ----

def test_the_update_script_does_every_step_and_checks_health():
    update = text("update.sh")
    order = ["tar -czf", 'as_app "$APP_DIR/deploy/backup.sh"', "pull --ff-only", "-m pip install -r requirements.txt", "-m alembic upgrade head",
             "systemctl restart payslip-sender", "curl -fsS"]
    positions = [update.index(item) for item in order]
    assert positions == sorted(positions)
    assert "--archive" in update and "sudo -u" in update and "checkout" in update and "--skip-backup" in update and "--force" in update


def test_backups_are_always_encrypted_with_a_key_that_is_not_on_the_server():
    backup = text("backup.sh")
    assert "age -R" in backup and "age1" in backup and "--single-transaction" in backup and "KEEP_DAYS=14" in backup
    assert "| gzip" in backup and re.search(r"gzip -9 \\\n\s*\| age -R", backup)    # dump -> gzip -> age, no plaintext file in between
    assert not re.search(r">\s*\S*\.sql\b", backup) and ".env" not in backup.replace("ID_ENCRYPTION_KEY (it lives in .env)", "")
    assert "--defaults-extra-file" in backup and "-mtime +" in backup and "S3_URI" in backup
    assert "-i " not in " ".join(re.findall(r"age .*", "\n".join(code_lines("backup.sh"))))   # never needs the private key (age -i)
    assert "ID_ENCRYPTION_KEY" in backup and "SOMEWHERE ELSE" in backup


def test_restore_defaults_to_a_scratch_database_and_asks_before_touching_the_live_one():
    restore = text("restore.sh")
    assert "SCRATCH_DB=payslip_restore_test" in restore and "Type RESTORE" in restore and "--live" in restore
    assert restore.index("Type RESTORE") > restore.index("Restoring into the scratch database")
    assert "before-restore" in restore and "shred" in restore and "age -d -i" in restore


# ---- settings files ----

def env_names(path: Path) -> set[str]:
    return set(dotenv_values(path))


def test_every_setting_in_the_env_examples_is_a_real_setting():
    known = {name.upper() for name in Settings.model_fields}
    for path in (PROJECT_ROOT / ".env.example", DEPLOY / "env.production.example"):
        unknown = env_names(path) - known
        assert not unknown, f"{path.name} lists settings the app does not read: {sorted(unknown)}"
    assert "RETENTION_HOURS" not in env_names(PROJECT_ROOT / ".env.example")
    assert {"SENT_RETENTION_DAYS", "LOG_LEVEL", "LOG_FORMAT", "LOG_DIR", "APP_HOST", "APP_PORT"} <= env_names(PROJECT_ROOT / ".env.example") | {"APP_HOST", "APP_PORT"}


def test_the_production_env_template_starts_safe_and_holds_no_secret():
    values = dotenv_values(DEPLOY / "env.production.example")
    assert values["APP_ENV"] == "production" and values["WHATSAPP_MODE"] == "mock" and values["APP_HOST"] == "127.0.0.1"
    for name in ("SESSION_SECRET", "ID_ENCRYPTION_KEY", "DATABASE_URL", "WHATSAPP_ALLOWED_RECIPIENTS"):
        assert not values[name], f"{name} must be filled by the setup script, not shipped in the template"
    assert values["PRIVATE_STORAGE_DIR"].startswith("/var/lib/payslip-sender/") and values["LOG_DIR"].startswith("/var/log/payslip-sender")
    assert values["SENT_RETENTION_DAYS"] == "7"
    assert all(v.startswith("your-") for k, v in values.items() if k in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"))


def test_the_storage_and_log_folders_in_the_units_match_the_env_template():
    values = dotenv_values(DEPLOY / "env.production.example")
    unit = text("payslip-sender.service")
    assert str(PurePosixPath(values["PRIVATE_STORAGE_DIR"]).parent) in unit and values["LOG_DIR"] in unit


def test_the_production_env_template_really_starts_the_app_once_filled_in():
    """Fill the three blanks the way setup-server.sh does and check production accepts the result."""
    import secrets
    from cryptography.fernet import Fernet

    values = {k.lower(): v for k, v in dotenv_values(DEPLOY / "env.production.example").items() if v}
    values.update(session_secret=secrets.token_urlsafe(48), id_encryption_key=Fernet.generate_key().decode(), session_cookie_secure=None,
                  database_url="mysql+pymysql://payslip_app:Zx81qLmN4pRt7VbC@127.0.0.1:3306/payslip_sender?charset=utf8mb4",
                  log_dir=None, private_storage_dir=Path("/tmp/x"), whatsapp_allowed_recipients="")
    assert Settings(_env_file=None, **values).app_env == "production"


# ---- dependencies ----

def requirement_lines(name: str) -> list[str]:
    out = []
    for raw in (PROJECT_ROOT / name).read_text().splitlines():
        line = raw.split("#")[0].strip()
        if line and not line.startswith("-r"):
            out.append(line)
    return out


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt"])
def test_every_dependency_is_pinned_to_an_exact_version(name):
    lines = requirement_lines(name)
    assert len(lines) > 10 or name.endswith("dev.txt")
    for line in lines:
        assert re.match(r"^[A-Za-z0-9_.\-]+(\[[a-z]+\])?==\d+(\.\d+)*(\.post\d+)?(\s*;.*)?$", line), f"not an exact pin: {line}"
    assert not any(op in line.split(";")[0] for line in lines for op in (">=", "<=", "~=", "!=", ">", "<"))


def test_the_vulnerability_checker_is_a_dev_dependency_and_sqlalchemy_stays_below_2_1():
    assert any(l.startswith("pip-audit==") for l in requirement_lines("requirements-dev.txt"))
    assert not any(l.startswith("pip-audit") for l in requirement_lines("requirements.txt"))
    assert any(l.startswith("SQLAlchemy==2.0.") for l in requirement_lines("requirements.txt"))


def test_the_dev_script_offers_test_audit_and_check():
    script = (PROJECT_ROOT / "dev.ps1").read_text(encoding="utf-8")
    for task in ('"test"', '"audit"', '"check"', '"install"'):
        assert task in script
    assert "pip_audit" in script and "production_check" in script and "pytest" in script
    assert "uvicorn" in (PROJECT_ROOT / "run.ps1").read_text() and "--reload" in (PROJECT_ROOT / "run.ps1").read_text()


# ---- the documentation points at things that exist ----

def test_the_helper_runs_only_app_commands_as_the_app_user_from_the_app_folder():
    helper = text("app-run.sh")
    assert 'cd "$APP_DIR"' in helper and "sudo -u payslip -H" in helper and "=~ ^app" in helper and '"$@"' in helper
    assert helper.index('cd "$APP_DIR"') < helper.index("exec sudo")


def test_every_deploy_file_and_script_mentioned_in_the_guides_exists():
    mentioned = set()
    for guide in ("DEPLOY.md", "README.md", "CLAUDE.md"):
        body = (PROJECT_ROOT / guide).read_text(encoding="utf-8")
        mentioned |= set(re.findall(r"deploy/([A-Za-z0-9_.\-]+\.(?:sh|service|timer|template|example))", body))
        mentioned |= {m for m in re.findall(r"(?<![\w/.])(update|backup|restore|setup-server|app-run)\.sh", body) for m in [m + ".sh"]}
    assert mentioned, "the guides should mention the deploy files"
    missing = sorted(name for name in mentioned if not (DEPLOY / name).exists())
    assert missing == [], f"mentioned in the guides but not in deploy/: {missing}"


def test_every_command_the_guides_tell_you_to_run_exists():
    modules = set()
    for guide in ("DEPLOY.md", "README.md"):
        modules |= set(re.findall(r"app\.scripts\.([a-z_]+)", (PROJECT_ROOT / guide).read_text(encoding="utf-8")))
    assert {"production_check", "check_live_config", "create_admin", "purge_old_batches"} <= modules
    for name in modules:
        assert (PROJECT_ROOT / "app" / "scripts" / f"{name}.py").exists(), name


# ---- a private database and a fresh start ----

def test_the_database_port_is_never_opened_anywhere():
    for name in ("setup-server.sh", "Caddyfile.template", "payslip-sender.service", "update.sh", "restore.sh", "backup.sh"):
        for line in code_lines(name):
            if "3306" in line:
                assert "127.0.0.1:3306" in line, f"{name}: port 3306 must only ever appear as 127.0.0.1:3306 ({line})"
    setup = text("setup-server.sh")
    assert "ufw allow 3306" not in setup and "bind-address = 127.0.0.1" in setup
    assert "mariadb-server" in setup and "CREATE USER IF NOT EXISTS" in setup
    guide = (PROJECT_ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "Port 3306 is never opened" in guide and "starts EMPTY" in guide and "NEW secrets" in guide


def test_the_setup_script_makes_new_secrets_on_the_server_and_copies_no_local_data():
    setup = text("setup-server.sh")
    assert "generate_secret" in setup and "generate_encryption_key" in setup
    assert "mysqldump" not in setup and "private_storage" not in setup and "sample_import" not in setup      # nothing is copied in
    assert "ENV_VALUE=" in setup and "/dev/urandom" in setup                                                    # the DB password is generated here
    assert "--exclude=.env" in (PROJECT_ROOT / "DEPLOY.md").read_text(encoding="utf-8")                          # the PC's .env is never uploaded
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "starts empty" in readme and "ID_ENCRYPTION_KEY" in readme and "separately from the database dumps" in readme
