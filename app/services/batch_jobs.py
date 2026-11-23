"""Background matching for a batch: read each new PDF's text, match its file name to an employee, cross-check.

Runs after the upload response is sent (FastAPI BackgroundTasks). Progress is written to the batch row, so the batch
page can show a progress bar. If the server stops half-way the batch looks "interrupted" after a while and HR can press
Re-run matching. Only types and counts are logged, never names or text.
"""
import json
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Batch, Employee, Payslip
from app.models.enums import MatchMethod, MatchStatus, PdfCheck, ProcessingState
from app.models.mixins import utcnow
from app.services import batch_storage
from app.services.matching import EXACT, NEEDS_REVIEW, MatchIndex
from app.services.pdf_check import check_text, extract_text, text_word_set

log = logging.getLogger(__name__)
COMMIT_EVERY = 10
STALE_AFTER = timedelta(minutes=2)    # no progress for this long while "processing" = the server stopped


def match_one(p: Payslip, index: MatchIndex, active_by_id: dict, active: list) -> None:
    """Match one pending payslip and run the PDF text cross-check. Changes the object only."""
    result = index.match(p.original_filename)
    p.match_score = result.score
    p.suggestions = json.dumps(result.suggestions) if result.suggestions else None
    p.employee_id, p.confirmed, p.text_names_employee_id = None, False, None
    p.pdf_check = PdfCheck.not_checked.value
    if result.status == EXACT:
        employee = active_by_id[result.employee_id]
        p.employee_id, p.match_status, p.match_method = employee.id, MatchStatus.exact.value, MatchMethod.filename_exact.value
        path = batch_storage.stored_path(p.batch_id, p.stored_filename)
        text = extract_text(path) if path and path.exists() else None
        check = check_text(text_word_set(text), employee, active)
        p.pdf_check, p.text_names_employee_id = check.result, check.other_employee_id
        p.confirmed = check.result != PdfCheck.possible_mismatch.value      # a possible mismatch needs HR's review
    elif result.status == NEEDS_REVIEW:
        p.match_status, p.match_method = MatchStatus.needs_review.value, MatchMethod.filename_fuzzy.value
    else:
        p.match_status, p.match_method = MatchStatus.unmatched.value, MatchMethod.none.value


def process_batch(batch_id: int, session_factory) -> None:
    try:
        with session_factory() as db:
            batch = db.get(Batch, batch_id)
            if batch is None:
                return
            active = list(db.scalars(select(Employee).where(Employee.active.is_(True))))
            active_by_id = {e.id: e for e in active}
            index = MatchIndex(active)
            rows = list(db.scalars(
                select(Payslip).where(Payslip.batch_id == batch_id, Payslip.match_status == MatchStatus.pending.value)
                .order_by(Payslip.id)
            ))
            batch.processing_state, batch.processing_total, batch.processing_done = ProcessingState.processing.value, len(rows), 0
            batch.processing_heartbeat_at = utcnow()
            db.commit()
            for i, p in enumerate(rows, 1):
                try:
                    match_one(p, index, active_by_id, active)
                except Exception as exc:       # one odd file must not stop the others
                    log.error("Matching a payslip failed: %s", type(exc).__name__)
                    p.match_status, p.match_method = MatchStatus.unmatched.value, MatchMethod.none.value
                batch.processing_done = i
                if i % COMMIT_EVERY == 0 or i == len(rows):
                    batch.processing_heartbeat_at = utcnow()
                    db.commit()
            batch.processing_state = ProcessingState.done.value
            batch.processing_heartbeat_at = utcnow()
            db.commit()
    except Exception as exc:
        log.error("Batch matching failed: %s", type(exc).__name__)
        try:
            with session_factory() as db:
                batch = db.get(Batch, batch_id)
                if batch is not None:
                    batch.processing_state = ProcessingState.failed.value
                    db.commit()
        except Exception:
            pass


def heal_if_interrupted(db: Session, batch: Batch) -> bool:
    """A batch stuck in 'processing' with no progress for a while was interrupted (e.g. server restart)."""
    if batch.processing_state != ProcessingState.processing.value:
        return False
    beat = batch.processing_heartbeat_at or batch.updated_at
    if utcnow() - beat > STALE_AFTER:
        batch.processing_state = ProcessingState.failed.value
        db.commit()
        return True
    return False


def reset_interrupted_matching(session_factory) -> int:
    """On app start: no matching job can be running any more, so a batch still marked 'processing' was interrupted.
    It becomes 'failed' at once (HR can press Re-run matching) instead of waiting for the stale-heartbeat check."""
    with session_factory() as db:
        batches = list(db.scalars(select(Batch).where(Batch.processing_state == ProcessingState.processing.value)))
        for batch in batches:
            batch.processing_state = ProcessingState.failed.value
        db.commit()
        return len(batches)


def queue_rematch(db: Session, batch: Batch) -> int:
    """Put every automatic, still-open file back to 'pending' (manual choices are kept). Returns how many."""
    rows = db.scalars(select(Payslip).where(
        Payslip.batch_id == batch.id, Payslip.match_method != MatchMethod.manual.value,
        Payslip.match_status != MatchStatus.invalid.value,
    )).all()
    for p in rows:
        p.match_status = MatchStatus.pending.value
    return len(rows)
