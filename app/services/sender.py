"""The sending engine: queue payslips, send them in the background, never send the same one twice by accident.

Lifecycle of a payslip:  pending -> queued -> sending -> sent -> delivered -> read     (or failed / uncertain)
  * "sending" is SAVED (committed) BEFORE any network call. If the app dies mid-send, the row is "uncertain" after a restart.
  * "uncertain" = the request may have been accepted (timeout, unreadable answer, restart). It is NEVER retried automatically,
    because retrying could deliver the payslip twice. HR decides (Retry selected uncertain rows, with a warning).
  * Automatic retries with backoff happen ONLY for clear transient failures: HTTP 429, 5xx, cannot connect (nothing was sent).
    Waits follow Meta's advice: 4^attempt seconds (1, 4, 16) or the Retry-After header. If the message call keeps failing with 5xx
    after the retries, the row becomes "uncertain" (a 5xx cannot prove the message was not accepted).
  * Only these moves queue a row: Send (pending), Resume (pending), Retry failed (failed), Retry uncertain (uncertain, HR confirmed).
    A sent / delivered / read / sending / queued row can never be queued again.
  * An access-token / permission error HALTS the whole batch at once (queued rows go back to pending, banner shown).
The worker claims rows with an atomic UPDATE (queued -> sending), so two workers never take the same row.
Logs hold counts, ids and exception TYPES only: never names, phone numbers, tokens or file contents.
"""
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Batch, Employee, Payslip
from app.models.enums import BatchStatus, PayslipStatus, SendState
from app.models.mixins import utcnow
from app.services import audit, batch_storage
from app.services.pdf_protect import ProtectError, protect_pdf
from app.services.readiness import build_context, problems
from app.services.send_status import apply_status, apply_status_event, maybe_complete
from app.services.whatsapp import (
    AMBIGUOUS, AUTH, TRANSIENT, WhatsAppClient, WhatsAppError, build_client, payslip_filename,
)
from app.services.whatsapp_errors import (
    LOCAL_FILE_MISSING, LOCAL_NO_PASSWORD, LOCAL_NO_PHONE, LOCAL_NOT_READY, LOCAL_PROTECT, LOCAL_RESTART, LOCAL_TIMEOUT,
    LOCAL_UNKNOWN, clean_error_text, explain,
)

log = logging.getLogger(__name__)
BACKOFF_CAP = 60


class SendError(Exception):
    """Something HR asked for is not allowed right now. Plain language."""


@dataclass
class SendConfig:
    concurrency: int = 5
    interval: float = 0.2
    max_retries: int = 3
    send_kind: str = "template"
    template_name: str = "payslip_"
    language: str = "en"

    @classmethod
    def from_settings(cls, s) -> "SendConfig":
        return cls(s.send_concurrency, s.send_interval_ms / 1000, s.send_max_retries, s.whatsapp_send_kind,
                   s.whatsapp_template_name, s.whatsapp_template_language)


class RateLimiter:
    """Gentle rate limiting: at least `interval` seconds between the START of any two WhatsApp requests (all workers)."""

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.interval, self._clock, self._sleep, self._next, self._lock = interval, clock, sleep, 0.0, threading.Lock()

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            self._sleep(start - now)


# ---- queueing (HR actions). They change the session and write the audit entry; the CALLER commits. -----------------

def _rows(db: Session, batch: Batch, *statuses: str) -> list[Payslip]:
    return list(db.scalars(select(Payslip).where(Payslip.batch_id == batch.id, Payslip.status.in_(statuses)).order_by(Payslip.id)))


def send_blockers(db: Session, batch: Batch) -> int:
    """How many files still have a problem (no phone, no ID, inactive, not matched ...). Must be 0 to send."""
    rows = list(db.scalars(select(Payslip).where(Payslip.batch_id == batch.id)))
    ids = {p.employee_id for p in rows if p.employee_id} | {p.text_names_employee_id for p in rows if p.text_names_employee_id}
    employees = {e.id: e for e in db.scalars(select(Employee).where(Employee.id.in_(ids)))} if ids else {}
    ctx = build_context(rows, employees)
    return sum(1 for p in rows if problems(p, ctx))


def start_send(db: Session, batch: Batch, confirm_label: str, *, mode: str, user_id: int, ip: str | None) -> int:
    if batch.status != BatchStatus.ready.value:
        raise SendError("Only a batch that is marked ready can be sent.")
    if (confirm_label or "").strip() != batch.pay_month_label:
        raise SendError("The pay month you typed does not match this batch, so nothing was sent.")
    bad = send_blockers(db, batch)
    if bad:
        raise SendError(f"{bad} file{'s' if bad != 1 else ''} have problems now. Reopen the batch and fix them first.")
    rows = _rows(db, batch, PayslipStatus.pending.value)
    if not rows:
        raise SendError("There is nothing to send.")
    for p in rows:
        p.status = PayslipStatus.queued.value
    batch.status, batch.send_state, batch.send_mode = BatchStatus.sending.value, SendState.running.value, mode
    batch.send_started_at, batch.halted_reason, batch.stopped_at, batch.completed_at = utcnow(), None, None, None
    audit.record(db, "send_started", user_id=user_id, entity_type="batch", entity_id=batch.id, ip=ip, commit=False,
                 details=f"files={len(rows)} mode={mode}")
    return len(rows)


def stop_send(db: Session, batch: Batch, *, user_id: int, ip: str | None) -> int:
    """Stop: queued rows go back to pending. Rows already being sent finish normally."""
    if batch.status != BatchStatus.sending.value:
        raise SendError("This batch is not being sent.")
    queued = _rows(db, batch, PayslipStatus.queued.value)
    for p in queued:
        p.status = PayslipStatus.pending.value
    batch.send_state, batch.stopped_at = SendState.stopped.value, utcnow()
    audit.record(db, "batch_stopped", user_id=user_id, entity_type="batch", entity_id=batch.id, ip=ip, commit=False,
                 details=f"queued_cancelled={len(queued)}")
    maybe_complete(db, batch)
    return len(queued)


def resume_send(db: Session, batch: Batch, *, user_id: int, ip: str | None) -> int:
    if batch.status != BatchStatus.sending.value or batch.send_state not in (SendState.stopped.value, SendState.halted.value):
        raise SendError("This batch is not stopped.")
    rows = _rows(db, batch, PayslipStatus.pending.value)
    if not rows:
        raise SendError("Nothing is left to send.")
    for p in rows:
        p.status = PayslipStatus.queued.value
    batch.send_state, batch.halted_reason, batch.stopped_at = SendState.running.value, None, None
    audit.record(db, "send_resumed", user_id=user_id, entity_type="batch", entity_id=batch.id, ip=ip, commit=False,
                 details=f"files={len(rows)}")
    return len(rows)


def _requeue(db: Session, batch: Batch, rows: list[Payslip], kind: str, *, user_id: int, ip: str | None) -> int:
    if batch.status not in (BatchStatus.sending.value, BatchStatus.completed.value):
        raise SendError("This batch has not been sent yet.")
    if not rows:
        raise SendError("There is nothing to retry.")
    for p in rows:
        p.status, p.error, p.error_code = PayslipStatus.queued.value, None, None
    batch.status, batch.send_state, batch.completed_at = BatchStatus.sending.value, SendState.running.value, None
    batch.halted_reason, batch.stopped_at = None, None
    audit.record(db, "retry_clicked", user_id=user_id, entity_type="batch", entity_id=batch.id, ip=ip, commit=False,
                 details=f"kind={kind} count={len(rows)}")
    return len(rows)


def retry_failed(db: Session, batch: Batch, *, user_id: int, ip: str | None, only_id: int | None = None) -> int:
    rows = [p for p in _rows(db, batch, PayslipStatus.failed.value) if only_id is None or p.id == only_id]
    return _requeue(db, batch, rows, "failed", user_id=user_id, ip=ip)


def retry_uncertain(db: Session, batch: Batch, ids: list[int], understood: bool, *, user_id: int, ip: str | None) -> int:
    if not understood:
        raise SendError("Please tick the box to confirm you understand the payslip may be delivered twice.")
    wanted = set(ids)
    rows = [p for p in _rows(db, batch, PayslipStatus.uncertain.value) if p.id in wanted]
    if not rows:
        raise SendError("Select at least one uncertain payslip.")
    return _requeue(db, batch, rows, "uncertain", user_id=user_id, ip=ip)


# ---- the worker --------------------------------------------------------------------------------------------------

class _Local(Exception):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(code)
        self.code, self.text = code, message


def claim_next(session_factory, batch_id: int, limit: int) -> list[int]:
    """Atomically take up to `limit` queued rows (queued -> sending) and COMMIT before any network call."""
    with session_factory() as db:
        batch = db.get(Batch, batch_id)
        if batch is None or batch.send_state != SendState.running.value or limit < 1:
            return []
        ids = db.scalars(select(Payslip.id).where(Payslip.batch_id == batch_id, Payslip.status == PayslipStatus.queued.value)
                         .order_by(Payslip.id).limit(limit)).all()
        claimed = []
        for pid in ids:
            result = db.execute(update(Payslip).where(Payslip.id == pid, Payslip.status == PayslipStatus.queued.value).values(
                status=PayslipStatus.sending.value, attempts=Payslip.attempts + 1, error=None, error_code=None))
            if result.rowcount == 1:
                claimed.append(pid)
        db.commit()
        return claimed


def _with_retries(call, cfg: SendConfig, limiter: RateLimiter, sleep) -> str:
    attempt = 0
    while True:
        limiter.wait()
        try:
            return call()
        except WhatsAppError as exc:
            if exc.kind != TRANSIENT or attempt >= cfg.max_retries:
                raise
            sleep(exc.retry_after if exc.retry_after is not None else min(BACKOFF_CAP, 4 ** attempt))
            attempt += 1


def _halt(db: Session, batch: Batch, code: str | None) -> None:
    batch.send_state, batch.stopped_at = SendState.halted.value, utcnow()
    batch.halted_reason = explain(code or "190")[:255]
    db.execute(update(Payslip).where(Payslip.batch_id == batch.id, Payslip.status == PayslipStatus.queued.value)
               .values(status=PayslipStatus.pending.value))
    audit.record(db, "send_token_error", entity_type="batch", entity_id=batch.id, commit=False, details=f"code={code}")


def _fail(p: Payslip, code: str, text: str | None) -> None:
    apply_status(p, "failed", code=code, text=text)


def _uncertain(p: Payslip, code: str, text: str | None) -> None:
    p.status, p.error_code, p.error = PayslipStatus.uncertain.value, code, clean_error_text(text)


def send_one(payslip_id: int, session_factory, client: WhatsAppClient, cfg: SendConfig, limiter: RateLimiter,
             sleep: Callable[[float], None] = time.sleep) -> None:
    """Deliver ONE claimed payslip (status 'sending'). Always ends in sent / failed / uncertain (or a halted batch)."""
    with session_factory() as db:
        p = db.get(Payslip, payslip_id)
        if p is None or p.status != PayslipStatus.sending.value:
            return
        batch_id = p.batch_id
        batch = db.get(Batch, batch_id)
        message_call_started = False
        try:
            employee = db.get(Employee, p.employee_id) if p.employee_id else None
            if employee is None or not employee.active:
                raise _Local(LOCAL_NOT_READY)
            if not employee.phone:
                raise _Local(LOCAL_NO_PHONE)
            if not employee.id_last4:
                raise _Local(LOCAL_NO_PASSWORD)
            path = batch_storage.stored_path(batch.id, p.stored_filename)
            if path is None or not path.exists():
                raise _Local(LOCAL_FILE_MISSING)
            client.check_recipient(employee.phone)                    # the allowed-recipients safety switch, before any upload
            try:
                pdf = protect_pdf(path, employee.id_last4)            # AES-256, verified, in memory only
            except ProtectError as exc:
                raise _Local(LOCAL_NO_PASSWORD if not employee.id_last4 else LOCAL_PROTECT, str(exc)) from None
            filename = payslip_filename(batch.pay_month_label)
            media_id = _with_retries(lambda: client.upload_media(pdf, filename), cfg, limiter, sleep)
            del pdf                                                    # the protected copy is gone from memory now
            p.whatsapp_media_id = media_id
            db.commit()
            message_call_started = True
            if cfg.send_kind == "document":
                call = lambda: client.send_document(employee.phone, media_id, filename, batch.pay_month_label)  # noqa: E731
            else:
                call = lambda: client.send_template(  # noqa: E731
                    employee.phone, media_id, filename, cfg.template_name, cfg.language, employee.full_name, batch.pay_month_label)
            message_id = _with_retries(call, cfg, limiter, sleep)
            p.whatsapp_message_id = message_id
            apply_status(p, "sent")                                   # accepted by WhatsApp; webhook/mock add delivered, read
            db.commit()
        except _Local as exc:
            db.rollback()
            p = db.get(Payslip, payslip_id)
            _fail(p, exc.code, exc.text or explain(exc.code))
            db.commit()
        except WhatsAppError as exc:
            db.rollback()
            p = db.get(Payslip, payslip_id)
            batch = db.get(Batch, batch_id)
            if exc.kind == AUTH:
                _fail(p, exc.code or "190", exc.message)
                _halt(db, batch, exc.code)
            elif exc.kind == AMBIGUOUS or (exc.kind == TRANSIENT and message_call_started and (exc.http_status or 0) >= 500):
                _uncertain(p, exc.code or LOCAL_TIMEOUT, exc.message)
            else:
                _fail(p, exc.code or LOCAL_UNKNOWN, exc.message)
            db.commit()
        except Exception as exc:       # a bug or odd failure: never leave the row in "sending"
            log.error("Sending a payslip failed unexpectedly: %s", type(exc).__name__)
            db.rollback()
            p = db.get(Payslip, payslip_id)
            if message_call_started:
                _uncertain(p, LOCAL_UNKNOWN, "unexpected error after the request may have been sent")
            else:
                _fail(p, LOCAL_UNKNOWN, "unexpected error before anything was sent")
            db.commit()
        finally:
            batch = db.get(Batch, batch_id)
            if batch is not None:
                maybe_complete(db, batch)
                db.commit()


def process_queue(batch_id: int, session_factory, client: WhatsAppClient, cfg: SendConfig, *,
                  sleep: Callable[[float], None] = time.sleep, limiter: RateLimiter | None = None) -> None:
    """Send every queued payslip of the batch (until none are left, or Stop / a halt). With concurrency 1 it runs inline."""
    limiter = limiter or RateLimiter(cfg.interval, sleep=sleep)
    if cfg.concurrency <= 1:
        while True:
            ids = claim_next(session_factory, batch_id, 1)
            if not ids:
                break
            send_one(ids[0], session_factory, client, cfg, limiter, sleep)
    else:
        in_flight: set = set()
        with ThreadPoolExecutor(max_workers=cfg.concurrency, thread_name_prefix="payslip-send") as pool:
            while True:
                room = cfg.concurrency - len(in_flight)
                ids = claim_next(session_factory, batch_id, room) if room > 0 else []
                for pid in ids:
                    in_flight.add(pool.submit(send_one, pid, session_factory, client, cfg, limiter, sleep))
                if not in_flight:
                    if not ids:
                        break
                    continue
                done, in_flight = wait(in_flight, timeout=1.0, return_when=FIRST_COMPLETED)
                for f in done:
                    f.result()
    with session_factory() as db:
        batch = db.get(Batch, batch_id)
        if batch is not None and maybe_complete(db, batch):
            db.commit()


# ---- starting the worker, and recovering after a restart ---------------------------------------------------------

_workers: dict[int, threading.Thread] = {}
_workers_lock = threading.Lock()
_client_cache: dict[str, WhatsAppClient] = {}


def get_client(session_factory) -> WhatsAppClient:
    """The process-wide WhatsApp client for WHATSAPP_MODE (mock statuses go through the same path as the webhook)."""
    settings = get_settings()
    key = settings.whatsapp_mode
    if key not in _client_cache:
        _client_cache[key] = build_client(
            settings, status_sink=lambda mid, status, ts, errors: apply_status_event(session_factory, mid, status, ts, errors))
    return _client_cache[key]


def _has_queued(session_factory, batch_id: int) -> bool:
    with session_factory() as db:
        batch = db.get(Batch, batch_id)
        if batch is None or batch.send_state != SendState.running.value:
            return False
        return bool(db.scalar(select(func.count()).where(Payslip.batch_id == batch_id, Payslip.status == PayslipStatus.queued.value)))


class SendLauncher:
    """Starts (at most one) background worker thread per batch. Replaced by InlineLauncher in tests."""

    def __init__(self, session_factory, client: WhatsAppClient | None = None, cfg: SendConfig | None = None):
        self.session_factory, self._client, self.cfg = session_factory, client, cfg

    def start(self, batch_id: int) -> bool:
        with _workers_lock:
            thread = _workers.get(batch_id)
            if thread is not None and thread.is_alive():
                return False                      # it will pick up newly queued rows itself
            thread = threading.Thread(target=self._run, args=(batch_id,), name=f"send-batch-{batch_id}", daemon=True)
            _workers[batch_id] = thread
            thread.start()
            return True

    def _run(self, batch_id: int) -> None:
        try:
            client = self._client or get_client(self.session_factory)
            cfg = self.cfg or SendConfig.from_settings(get_settings())
            while True:
                process_queue(batch_id, self.session_factory, client, cfg)
                with _workers_lock:
                    if not _has_queued(self.session_factory, batch_id):
                        _workers.pop(batch_id, None)
                        return
        except Exception as exc:
            log.error("The send worker stopped unexpectedly: %s", type(exc).__name__)
            with _workers_lock:
                _workers.pop(batch_id, None)


class InlineLauncher:
    """Runs the worker right away in the calling thread (used by tests, and handy for scripts)."""

    def __init__(self, session_factory, client: WhatsAppClient, cfg: SendConfig, sleep: Callable[[float], None] = lambda s: None):
        self.session_factory, self.client, self.cfg, self.sleep = session_factory, client, cfg, sleep

    def start(self, batch_id: int) -> bool:
        process_queue(batch_id, self.session_factory, self.client, self.cfg, sleep=self.sleep)
        return True


def recover_after_restart(session_factory, launcher) -> dict[str, int]:
    """On app start: anything left in 'sending' becomes 'uncertain'; 'queued' rows of a running batch resume."""
    with session_factory() as db:
        interrupted = db.execute(update(Payslip).where(Payslip.status == PayslipStatus.sending.value).values(
            status=PayslipStatus.uncertain.value, error_code=LOCAL_RESTART, error=explain(LOCAL_RESTART)[:500])).rowcount or 0
        running = list(db.scalars(select(Batch).where(Batch.status == BatchStatus.sending.value, Batch.send_state == SendState.running.value)))
        resumed = 0
        for batch in running:
            queued = db.scalar(select(func.count()).where(Payslip.batch_id == batch.id, Payslip.status == PayslipStatus.queued.value)) or 0
            if queued:
                resumed += 1
            else:
                maybe_complete(db, batch)
        db.commit()
        ids = [b.id for b in running if b.status == BatchStatus.sending.value]
    for batch_id in ids:
        launcher.start(batch_id)
    if interrupted or resumed:
        log.warning("Send recovery: %d interrupted payslip(s) marked uncertain, %d batch(es) resumed.", interrupted, resumed)
    return {"uncertain": interrupted, "resumed": resumed}
