"""Where uploaded payslip PDFs live: PRIVATE_STORAGE_DIR/batches/<batch id>/<random uuid>.pdf

- Outside app/static and never served by any route (a test checks both).
- A file is stored under a random UUID, NEVER its original name; the original name is kept in the database only.
- Stored names are validated before use, so a bad database value can never point outside the batch folder.
"""
import re
import shutil
import uuid
from pathlib import Path

from app.config import PROJECT_ROOT, get_settings

_STORED_NAME = re.compile(r"^[0-9a-f]{32}\.pdf$")
_PART_NAME = re.compile(r"^[0-9a-f]{32}\.part$")


def storage_root() -> Path:
    return get_settings().storage_path / "batches"


def batch_dir(batch_id: int) -> Path:
    return storage_root() / str(int(batch_id))


def ensure_batch_dir(batch_id: int) -> Path:
    path = batch_dir(batch_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_stored_name() -> str:
    return f"{uuid.uuid4().hex}.pdf"


def part_path(batch_id: int) -> Path:
    """A temporary name for a file that is still being written."""
    return ensure_batch_dir(batch_id) / f"{uuid.uuid4().hex}.part"


def stored_path(batch_id: int, stored_filename: str | None) -> Path | None:
    if not stored_filename or not _STORED_NAME.match(stored_filename):
        return None
    return batch_dir(batch_id) / stored_filename


def delete_stored(batch_id: int, stored_filename: str | None) -> None:
    path = stored_path(batch_id, stored_filename)
    if path is not None:
        path.unlink(missing_ok=True)


def delete_batch_files(batch_id: int) -> None:
    shutil.rmtree(batch_dir(batch_id), ignore_errors=True)


def storage_is_private() -> bool:
    """True when the storage folder is not inside the app's public static folder."""
    static = (PROJECT_ROOT / "app" / "static").resolve()
    root = get_settings().storage_path.resolve()
    return static != root and static not in root.parents and root not in static.parents
