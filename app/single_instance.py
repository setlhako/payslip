"""Exactly ONE app process may run in production.

Why: the send engine (worker threads, delayed mock statuses, the import preview store, one-time passwords) lives in this
process's memory, and startup recovery marks payslips that were "sending" as "uncertain". A second process would run its own
recovery against payslips the first one is busy sending, and would not share any of that memory. So the app takes an exclusive
lock on a file in its private storage folder at start-up and refuses to start if another process holds it. The operating
system releases the lock when the process ends (even if it crashes), so a restart is always safe.
"""
import os
import sys
from pathlib import Path


class AlreadyRunning(RuntimeError):
    """Another process of the app is already running."""


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(
                "Another payslip-sender process is already running. Run exactly ONE app process (one uvicorn worker): "
                "see the README, 'Why exactly one process'."
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))                  # for humans: which process holds it
        handle.flush()
        self._file = handle
        return self

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def acquire_instance_lock(path: Path) -> InstanceLock:
    return InstanceLock(path).acquire()
