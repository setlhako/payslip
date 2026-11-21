"""Reads settings from the .env file (and environment variables).

Every setting lives here so the rest of the code never reads .env directly.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "production"] = "development"
    session_secret: SecretStr  # signs the login cookie; create with: python -m app.scripts.generate_secret
    database_url: SecretStr  # contains the DB password, so it is a SecretStr

    # WhatsApp
    whatsapp_mode: Literal["mock", "live"] = "mock"
    whatsapp_send_kind: Literal["template", "document"] = "template"
    whatsapp_template_name: str = "payslip_"
    whatsapp_template_language: str = "en"
    graph_api_version: str = "v23.0"
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")

    # Files
    private_storage_dir: Path = Path("private_storage")
    max_upload_mb: int = 10
    retention_hours: int = 24

    # Login protection
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
