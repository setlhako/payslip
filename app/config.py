"""Reads settings from the .env file (and environment variables).

Every setting lives here so the rest of the code never reads .env directly.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

import base64
import re

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,   # a startup error must never print a (mistyped) key, secret or database password
    )

    app_env: Literal["development", "production"] = "development"
    # Where the server listens. In production it must stay on the machine itself: Caddy (HTTPS) is the only way in.
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    # Logging: stdout/journald always; a rotating file too when LOG_DIR is set. LOG_FORMAT auto = json in production, text otherwise.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["auto", "json", "text"] = "auto"
    log_dir: Path | None = None
    log_file_max_mb: int = Field(default=10, ge=1, le=200)
    log_file_backups: int = Field(default=5, ge=1, le=50)
    session_secret: SecretStr  # signs the login cookie; create with: python -m app.scripts.generate_secret
    database_url: SecretStr  # contains the DB password, so it is a SecretStr

    # Full Omang/passport numbers are stored ENCRYPTED with this key (Fernet). Required: the app will not start without it.
    # Create it with: python -m app.scripts.generate_encryption_key   (written into .env, never printed)
    id_encryption_key: SecretStr
    # Old key(s) kept only so existing data can still be read while rotating (comma-separated). See README "Rotating the key".
    id_encryption_key_previous: SecretStr = SecretStr("")
    reveal_max_per_minute: int = 10      # how many times per minute one admin may press "Show IDs"

    # WhatsApp
    whatsapp_mode: Literal["mock", "live"] = "mock"
    whatsapp_send_kind: Literal["template", "document"] = "template"
    whatsapp_template_name: str = "payslip_"
    whatsapp_template_language: str = "en"
    # ONLY for the preview on the send screen: the wording of your approved template, with {{1}} for the employee's name and
    # {{2}} for the pay month. WhatsApp always sends the approved template itself; this text is never sent.
    whatsapp_template_preview_text: str = (
        "Hello {{1}}, your payslip for {{2}} is attached. To open it, use the last 4 characters of your Omang or passport number as the password."
    )
    graph_api_version: str = "v23.0"
    whatsapp_phone_number_id: str = ""
    # The WhatsApp Business ACCOUNT id (not the phone number id). Sending does not need it; only the preflight command
    # (python -m app.scripts.check_live_config) uses it, to look up your message templates.
    whatsapp_business_account_id: str = ""
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    # Safety switch: when set (comma-separated E.164 numbers) live mode sends ONLY to these numbers.
    whatsapp_allowed_recipients: str = ""
    whatsapp_base_url: str = "https://graph.facebook.com"
    whatsapp_timeout_seconds: int = Field(default=30, ge=5, le=120)

    # Sending engine
    send_concurrency: int = Field(default=5, ge=1, le=20)     # payslips sent at the same time
    send_interval_ms: int = Field(default=200, ge=0, le=10_000)  # minimum gap between WhatsApp requests (all workers together)
    send_max_retries: int = Field(default=3, ge=0, le=6)      # automatic retries for clear transient failures only

    # Files
    private_storage_dir: Path = Path("private_storage")
    max_upload_mb: int = 10              # largest single payslip PDF
    # Stored PDFs are deleted this many days after a batch finished sending (python -m app.scripts.purge_old_batches, run daily).
    # A batch that still has failed or uncertain payslips keeps its PDFs for BATCH_RETENTION_DAYS instead, so HR can retry them.
    sent_retention_days: int = Field(default=7, ge=0, le=365)

    # Payslip batches
    max_files_per_batch: int = 300
    max_batch_total_mb: int = 200        # all stored PDFs of one batch together
    max_unzipped_mb: int = 250           # zip-bomb guard: everything a ZIP expands to, counted as it is unpacked
    batch_retention_days: int = 30       # python -m app.scripts.purge_old_batches deletes stored files older than this

    # Login protection
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15

    # Employee import
    import_max_rows: int = 2000
    import_preview_minutes: int = 15   # an unconfirmed import preview is discarded after this long

    # Sessions
    session_idle_minutes: int = 30       # logged out after this long with no activity
    session_absolute_hours: int = 8      # logged out after this long no matter what
    session_cookie_secure: bool | None = None  # None = automatic: on in production, off in development

    @field_validator("session_secret")
    @classmethod
    def _secret_is_strong(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError(
                "SESSION_SECRET is missing or too short (needs 32+ characters). "
                "Run: python -m app.scripts.generate_secret"
            )
        return v

    @field_validator("whatsapp_allowed_recipients")
    @classmethod
    def _allowed_recipients_are_numbers(cls, v: str) -> str:
        for i, item in enumerate(p.strip() for p in v.split(",") if p.strip()):
            if not re.fullmatch(r"\+[1-9]\d{7,14}", item):
                raise ValueError(f"WHATSAPP_ALLOWED_RECIPIENTS entry {i + 1} is not a phone number like +26771234567.")  # value not echoed
        return v

    @model_validator(mode="after")
    def _live_mode_needs_its_settings(self):
        """Live mode refuses to start if anything it needs is missing (or still the .env.example placeholder)."""
        if self.whatsapp_mode != "live":
            return self
        values = {
            "WHATSAPP_ACCESS_TOKEN": self.whatsapp_access_token.get_secret_value(),
            "WHATSAPP_PHONE_NUMBER_ID": self.whatsapp_phone_number_id,
            "WHATSAPP_APP_SECRET": self.whatsapp_app_secret.get_secret_value(),
            "WHATSAPP_VERIFY_TOKEN": self.whatsapp_verify_token.get_secret_value(),
            "GRAPH_API_VERSION": self.graph_api_version,
        }
        missing = [name for name, v in values.items() if not v.strip() or v.strip().lower().startswith("your-")]
        if not re.fullmatch(r"v\d+\.\d+", self.graph_api_version.strip()) and "GRAPH_API_VERSION" not in missing:
            missing.append("GRAPH_API_VERSION (looks like v23.0)")
        if missing:
            raise ValueError("WHATSAPP_MODE=live needs these settings in .env: " + ", ".join(missing))
        return self

    @property
    def allowed_recipients(self) -> set[str]:
        return {p.strip() for p in self.whatsapp_allowed_recipients.split(",") if p.strip()}

    @model_validator(mode="after")
    def _production_rules(self):
        """In production the app REFUSES TO START unless it is safely configured. Messages name settings, never values."""
        if self.app_env != "production":
            return self
        problems: list[str] = []
        for problem in (session_secret_problem(self.session_secret.get_secret_value()),
                        encryption_key_problem(self.id_encryption_key.get_secret_value()),
                        database_url_problem(self.database_url.get_secret_value())):
            if problem:
                problems.append(problem)
        if self.session_cookie_secure is False:
            problems.append("SESSION_COOKIE_SECURE=false is not allowed in production (cookies must be Secure)")
        if self.app_host not in ("127.0.0.1", "localhost", "::1"):
            problems.append("APP_HOST must be 127.0.0.1 in production: only Caddy (HTTPS) may reach the app")
        if problems:
            raise ValueError("APP_ENV=production refuses to start: " + "; ".join(problems))
        return self

    @field_validator("id_encryption_key")
    @classmethod
    def _id_key_is_valid(cls, v: SecretStr) -> SecretStr:
        from app.services.id_crypto import key_is_valid  # imported here: id_crypto itself imports this module

        if not key_is_valid(v.get_secret_value()):
            raise ValueError(
                "ID_ENCRYPTION_KEY is missing or not a valid key. Run: python -m app.scripts.generate_encryption_key"
            )
        return v

    @field_validator("id_encryption_key_previous")
    @classmethod
    def _previous_keys_are_valid(cls, v: SecretStr) -> SecretStr:
        from app.services.id_crypto import split_keys, key_is_valid

        if not all(key_is_valid(k) for k in split_keys(v.get_secret_value())):
            raise ValueError("ID_ENCRYPTION_KEY_PREVIOUS contains a key that is not valid.")  # key itself never shown
        return v

    @property
    def storage_path(self) -> Path:
        """Absolute path of the private storage folder."""
        p = self.private_storage_dir
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def secure_cookies(self) -> bool:
        """Should the session cookie carry the Secure flag (HTTPS only)?"""
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.app_env == "production"


def _is_placeholder(value: str) -> bool:
    """Empty, or one of the example values in .env.example."""
    v = (value or "").strip().lower()
    return (not v or v.startswith("your-") or v in {"change-me", "changeme", "change_me", "secret", "password"}
            or "change-me" in v or "changeme" in v or "your_db_password" in v)


def session_secret_problem(secret: str) -> str | None:
    """Plain-language problem with a SESSION_SECRET value, or None when it is fine. Shared with production_check."""
    if _is_placeholder(secret) or len(set(secret)) < 12:
        return "SESSION_SECRET is missing, a placeholder, or too predictable (run: python -m app.scripts.generate_secret)"
    return None


def encryption_key_problem(key: str) -> str | None:
    """Plain-language problem with an ID_ENCRYPTION_KEY value, or None. A real key is random bytes, not readable text."""
    try:
        from app.services.id_crypto import key_is_valid

        raw = base64.urlsafe_b64decode(key.encode())
        strong = key_is_valid(key) and len(set(raw)) >= 16
    except Exception:
        strong = False
    if _is_placeholder(key) or not strong:
        return "ID_ENCRYPTION_KEY is missing, a placeholder, or not a strong random key (run: python -m app.scripts.generate_encryption_key)"
    return None


def database_url_problem(url: str) -> str | None:
    if _is_placeholder(url):
        return "DATABASE_URL is missing or still a placeholder"
    if not url.lower().startswith(("mysql", "mariadb")):
        return "DATABASE_URL must point at MySQL/MariaDB in production"
    return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
