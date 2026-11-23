"""Tidying that must happen INSIDE the running app (the daily purge script runs in a separate process and cannot reach these):
expired import previews and one-time passwords in memory, and old login-attempt rows in the database."""
import asyncio
import logging

from app.services import login_limiter
from app.services.import_preview import store as import_preview_store
from app.services.one_time import temp_passwords

log = logging.getLogger(__name__)
INTERVAL_SECONDS = 300


def run_once(session_factory) -> dict[str, int]:
    previews = import_preview_store.purge_expired()
    secrets_gone = temp_passwords.purge_expired()
    with session_factory() as db:
        attempts = login_limiter.purge_old(db)
    return {"import_previews": previews, "one_time_secrets": secrets_gone, "login_attempts": attempts}


async def loop(session_factory) -> None:
    """Runs forever (until the app stops); a failure is logged by type and the loop carries on."""
    while True:
        await asyncio.sleep(INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(run_once, session_factory)
        except Exception as exc:
            log.error("Housekeeping failed: %s", type(exc).__name__)
