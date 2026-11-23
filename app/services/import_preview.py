"""Short-lived, in-memory store for import previews.

The preview holds the cleaned rows. Full ID numbers are in them only ENCRYPTED (plus the last 4 characters). It lives only in this server process's
memory, never on disk and never in the database. It is tied to the logged-in user, expires after
IMPORT_PREVIEW_MINUTES, and is removed on confirm, cancel or logout.
Note: this assumes ONE server process (the plan: a single uvicorn on EC2). With several workers a preview
would have to move to a shared store.
"""
import secrets
import threading
import time
from dataclasses import dataclass

from app.config import get_settings
from app.services.import_parser import ParseResult

MAX_ENTRIES = 50


def _now() -> float:
    return time.monotonic()  # separate function so tests can fast-forward time


@dataclass
class _Entry:
    user_id: int
    expires_at: float
    result: ParseResult


class PreviewStore:
    def __init__(self) -> None:
        self._items: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _purge(self) -> None:
        t = _now()
        for token in [k for k, v in self._items.items() if v.expires_at <= t]:
            del self._items[token]

    def purge_expired(self) -> int:
        """Drop expired previews now (housekeeping). Returns how many were removed."""
        with self._lock:
            before = len(self._items)
            self._purge()
            return before - len(self._items)

    def put(self, user_id: int, result: ParseResult) -> str:
        with self._lock:
            self._purge()
            for token in [k for k, v in self._items.items() if v.user_id == user_id]:
                del self._items[token]  # one preview per user: a new upload replaces the old one
            while len(self._items) >= MAX_ENTRIES:
                del self._items[next(iter(self._items))]
            token = secrets.token_urlsafe(32)
            ttl = get_settings().import_preview_minutes * 60
            self._items[token] = _Entry(user_id, _now() + ttl, result)
            return token

    def get(self, token: str | None, user_id: int) -> ParseResult | None:
        if not token:
            return None
        with self._lock:
            self._purge()
            entry = self._items.get(token)
            return entry.result if entry and entry.user_id == user_id else None

    def discard(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._items.pop(token, None)

    def discard_for_user(self, user_id: int) -> None:
        with self._lock:
            for token in [k for k, v in self._items.items() if v.user_id == user_id]:
                del self._items[token]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


store = PreviewStore()
