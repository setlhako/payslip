"""Delete stored payslip PDFs that are no longer needed. Run: python -m app.scripts.purge_old_batches

Two rules (both keep the batch and its list of files in the database for your records, marked "files deleted"):
1. A batch that FINISHED SENDING more than SENT_RETENTION_DAYS ago (default 7) loses its PDFs, unless it still has failed or
   uncertain payslips (HR may need to retry those, which needs the PDF), in which case rule 2 applies.
2. Any batch created more than BATCH_RETENTION_DAYS ago (default 30) loses its PDFs (drafts nobody finished, and the
   unresolved ones from rule 1). Batches that are being sent right now are never touched.
Also removes folders in private storage that belong to no batch (left behind by a crash) and old login-attempt rows.
Only counts are printed. Safe to run as often as you like: the production timer runs it daily.
"""
import shutil
import sys
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import Batch, Payslip
from app.models.enums import BatchStatus, PayslipStatus
from app.models.mixins import utcnow
from app.services import audit, batch_storage, login_limiter

UNRESOLVED = (PayslipStatus.failed.value, PayslipStatus.uncertain.value)


def _due_batches(db: Session, days: int, sent_days: int) -> list[Batch]:
    now = utcnow()
    old = list(db.scalars(select(Batch).where(
        Batch.created_at < now - timedelta(days=days), Batch.files_deleted_at.is_(None), Batch.status != BatchStatus.sending.value)))
    finished = list(db.scalars(select(Batch).where(
        Batch.status == BatchStatus.completed.value, Batch.files_deleted_at.is_(None), Batch.completed_at.is_not(None),
        Batch.completed_at < now - timedelta(days=sent_days))))
    due = {b.id: b for b in old}
    for batch in finished:
        has_unresolved = db.scalar(select(Payslip.id).where(Payslip.batch_id == batch.id, Payslip.status.in_(UNRESOLVED)).limit(1))
        if not has_unresolved:
            due[batch.id] = batch
    return sorted(due.values(), key=lambda b: b.id)


def purge(db: Session, days: int | None = None, sent_days: int | None = None, orphan_grace_seconds: int = 3600) -> dict[str, int]:
    """orphan_grace_seconds: a folder with no batch is only removed once it is this old (an upload in progress is not an orphan)."""
    settings = get_settings()
    days = settings.batch_retention_days if days is None else days
    sent_days = settings.sent_retention_days if sent_days is None else sent_days
    now = utcnow()
    purged_batches = purged_files = 0
    for batch in _due_batches(db, days, sent_days):
        payslips = db.scalars(select(Payslip).where(Payslip.batch_id == batch.id, Payslip.stored_filename.is_not(None))).all()
        batch_storage.delete_batch_files(batch.id)
        for p in payslips:
            p.stored_filename, p.files_deleted_at = None, now
        batch.files_deleted_at = now
        audit.record(db, "batch_files_purged", entity_type="batch", entity_id=batch.id, commit=False, details=f"files={len(payslips)}")
        purged_batches += 1
        purged_files += len(payslips)
    db.commit()

    orphans = 0
    root = batch_storage.storage_root()
    if root.is_dir():
        known = {str(i) for i in db.scalars(select(Batch.id))}
        for child in root.iterdir():
            if child.is_dir() and child.name not in known and time.time() - child.stat().st_mtime >= orphan_grace_seconds:
                shutil.rmtree(child, ignore_errors=True)
                orphans += 1
    return {"batches": purged_batches, "files": purged_files, "orphan_folders": orphans}


def main() -> int:
    settings = get_settings()
    with SessionLocal() as db:
        counts = purge(db)
        attempts = login_limiter.purge_old(db)
    print(f"Purged {counts['files']} stored file(s) from {counts['batches']} batch(es) "
          f"(sent more than {settings.sent_retention_days} days ago, or created more than {settings.batch_retention_days} days ago); "
          f"removed {counts['orphan_folders']} orphan folder(s) and {attempts} old login-attempt row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
