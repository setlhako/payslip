"""Logging: structured lines to stdout (journald picks them up) plus an optional rotating file, levels from settings.

Secrets and personal data must never reach a log. The code already logs only counts, ids and exception TYPES; this module adds a
SAFETY NET: every line (message AND traceback) passes through `redact()`, which masks long digit runs (phone numbers, ID numbers),
access tokens, Bearer values, signatures, encrypted ID values and things like `password=...`. PDF contents are never passed to a logger.
Uvicorn's access log is switched off in production: its lines contain the full URL with the query string (search words, one-time links).
"""
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

_PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [hidden]"),
    (re.compile(r"\bEAA[A-Za-z0-9]{8,}\b"), "[hidden]"),                                   # Meta access tokens
    (re.compile(r"\bsha(?:1|256)=[0-9a-fA-F]{16,}"), "sha256=[hidden]"),                    # webhook signatures
    (re.compile(r"\bgAAAA[A-Za-z0-9_\-=]{20,}"), "[hidden]"),                              # Fernet tokens (encrypted ID numbers)
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b(\s*[=:]\s*)[^\s,;&]+"), r"\1\2[hidden]"),
    (re.compile(r"\+?\d[\d \-]{5,}\d"), "***"),                                            # phone / ID numbers (7+ digits, spaces or dashes inside)
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "[hidden]"),                                  # any other long token-like string
]
MARK = "_payslip_handler"


def redact(text: str | None) -> str:
    out = text or ""
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


class RedactingFormatter(logging.Formatter):
    """Plain text lines. Only the MESSAGE and the traceback are redacted (the time and level are ours and safe: redacting the
    whole line would turn the date into ***)."""

    def format(self, record: logging.LogRecord) -> str:
        safe = logging.makeLogRecord(record.__dict__.copy())
        safe.msg, safe.args = redact(record.getMessage()), None
        safe.exc_info, safe.exc_text, safe.stack_info = None, None, None
        line = super().format(safe)
        if record.exc_info:
            line += chr(10) + redact(self.formatException(record.exc_info))
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message (and exception text). Message and exception are redacted."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname, "logger": record.name, "message": redact(record.getMessage()),
        }
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=True)


def _remove_ours(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, MARK, False):
            root.removeHandler(handler)
            handler.close()


def configure_logging(settings, stream=None) -> None:
    """Idempotent. Only touches handlers this function added earlier (so test log capture is left alone)."""
    root = logging.getLogger()
    _remove_ours(root)
    fmt = settings.log_format if settings.log_format != "auto" else ("json" if settings.app_env == "production" else "text")
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stdout)]
    if settings.log_dir:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        path = settings.log_dir / "payslip-sender.log"
        handlers.append(RotatingFileHandler(path, maxBytes=settings.log_file_max_mb * 1024 * 1024, backupCount=settings.log_file_backups, encoding="utf-8"))
        try:
            os.chmod(path, 0o600)                                    # readable by the app user only (ignored on Windows)
        except OSError:
            pass
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, MARK, True)
        root.addHandler(handler)
    root.setLevel(settings.log_level)
    for noisy in ("httpx", "httpcore", "watchfiles", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)           # (httpx would log request URLs)
    access = logging.getLogger("uvicorn.access")
    access.disabled = settings.app_env == "production"
