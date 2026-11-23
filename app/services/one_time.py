"""Hand a secret to ONE page view and forget it. Used to show a temporary password exactly once.

The secret lives only in this process's memory (never the database, a file, the session cookie or a log), is tied to the
admin who created it, can be read once, and expires after a few minutes. Single-process assumption, like the import preview.
"""
import secrets
import threading
import time

TTL_SECONDS = 300
MAX_ENTRIES = 50


def _now() -> float:
    return time.monotonic()      # a function so tests can fast-forward


class OneTimeStore:
    def __init__(self) -> None:
        self._items: dict[str, tuple[int, object, float]] = {}
        self._lock = threading.Lock()

    def _purge(self) -> None:
        t = _now()
        for token in [k for k, (_, _, exp) in self._items.items() if exp <= t]:
            del self._items[token]

    def put(self, actor_id: int, value: object) -> str:
        with self._lock:
            self._purge()
            while len(self._items) >= MAX_ENTRIES:
                del self._items[next(iter(self._items))]
            token = secrets.token_urlsafe(24)
            self._items[token] = (actor_id, value, _now() + TTL_SECONDS)
            return token

    def pop(self, token: str | None, actor_id: int) -> object | None:
        """The value, ONCE, and only for the admin it was made for. Anything else returns None."""
        if not token:
            return None
        with self._lock:
            self._purge()
            entry = self._items.get(token)
            if entry is None or entry[0] != actor_id:
                return None
            del self._items[token]
            return entry[1]

    def purge_expired(self) -> int:
        with self._lock:
            before = len(self._items)
            self._purge()
            return before - len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


temp_passwords = OneTimeStore()
