"""Accepting uploaded payslip PDFs (many files, or one ZIP) into a batch.

Safety rules, all enforced here and covered by tests:
- Files are streamed to the private folder in chunks (never fully held in memory) under a random UUID name.
  The original name goes in the database only. Nothing is ever written to a path built from an upload's name.
- ZIP: entry paths are never trusted. Absolute paths, drive letters and ".." are REJECTED (whole upload); folders,
  hidden files and __MACOSX are ignored; everything else is flattened to its base name. Nested archives and
  password-protected ZIPs are rejected. Zip-bomb guard: declared size, actual unpacked bytes (counted as they are
  unpacked, because declared sizes can lie), an expansion-ratio check and an entry-count check.
- Each file must be a real PDF: not empty, starts with %PDF-, opens in pypdf, not password-protected, within the size
  limit. A bad file is NOT stored; its row stays in the batch as "Invalid file" with a plain reason so HR sees it.
- Limits (settings): files per batch, size per PDF, size of the whole batch. Going over a batch-wide limit rejects the
  whole upload and removes everything it wrote (UploadRejected), so a batch is never left half-filled.
No file names, contents or personal data are logged.
"""
import hashlib
import re
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Batch, Payslip
from app.models.enums import MatchMethod, MatchStatus, ProcessingState
from app.models.mixins import utcnow
from app.services import batch_storage

CHUNK = 256 * 1024
MAX_ZIP_ENTRIES = 5000          # a ZIP with more entries than this is refused outright
MAX_EXPANSION_RATIO = 100       # unpacked size / packed size above this (for a big entry) looks like a zip bomb
RATIO_CHECK_MIN_BYTES = 5 * 1024 * 1024
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".z", ".cab", ".iso"}
IGNORED_NAMES = {"thumbs.db", "desktop.ini"}


class UploadRejected(Exception):
    """The whole upload is refused. The message is plain language and safe to show to HR."""


@dataclass
class IngestResult:
    accepted: int = 0       # valid PDFs stored
    invalid: int = 0        # rows added as "Invalid file"
    ignored: int = 0        # ZIP entries skipped silently (folders, hidden files, __MACOSX)

    @property
    def added(self) -> int:
        return self.accepted + self.invalid


def safe_display_name(raw: str | None) -> str:
    """The base name only (any folders removed), without control characters, at most 255 characters."""
    name = re.split(r"[\\/]+", raw or "")[-1]
    name = "".join(c for c in unicodedata.normalize("NFC", name) if c.isprintable()).strip()
    if len(name) > 255:
        suffix = Path(name).suffix[:20]
        name = name[: 255 - len(suffix)] + suffix
    return name or "unnamed"


def _path_is_unsafe(name: str) -> bool:
    if "\x00" in name or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return True
    return ".." in re.split(r"[\\/]+", name)


def _ext(name: str) -> str:
    return PurePosixPath(name.lower()).suffix


class _Ingest:
    def __init__(self, db: Session, batch: Batch):
        self.db, self.batch, self.s = db, batch, get_settings()
        self.max_pdf = self.s.max_upload_mb * 1024 * 1024
        self.max_total = self.s.max_batch_total_mb * 1024 * 1024
        self.max_unzipped = self.s.max_unzipped_mb * 1024 * 1024
        self.existing_count = db.scalar(select(func.count()).where(Payslip.batch_id == batch.id)) or 0
        self.total_bytes = db.scalar(select(func.coalesce(func.sum(Payslip.file_size), 0)).where(Payslip.batch_id == batch.id)) or 0
        self.unpacked_bytes = 0
        self.result = IngestResult()
        self.written: list[Path] = []     # stored files created by THIS upload (removed again if it is rejected)

    # ---- limits ------------------------------------------------------------------------------------------------
    def check_count(self, incoming: int) -> None:
        if self.existing_count + incoming > self.s.max_files_per_batch:
            raise UploadRejected(
                f"Too many files: a batch can hold {self.s.max_files_per_batch} files "
                f"({self.existing_count} already in this batch, {incoming} in this upload)."
            )

    # ---- one file ----------------------------------------------------------------------------------------------
    def _invalid(self, name: str, reason: str) -> None:
        self.db.add(Payslip(
            batch_id=self.batch.id, original_filename=name, match_status=MatchStatus.invalid.value,
            match_method=MatchMethod.none.value, invalid_reason=reason,
        ))
        self.result.invalid += 1

    def add_file(self, raw_name: str, stream, from_zip: bool = False) -> None:
        name = safe_display_name(raw_name)
        ext = _ext(name)
        if ext != ".pdf":
            if ext in ARCHIVE_EXTENSIONS:
                if from_zip:
                    raise UploadRejected("The ZIP contains another ZIP or archive. Nested archives are not accepted: unpack it first.")
                return self._invalid(name, "This is a ZIP or archive file. Use the ZIP option to upload it.")
            return self._invalid(name, f"Not a PDF file (it is a {ext} file)." if ext else "Not a PDF file.")

        part = batch_storage.part_path(self.batch.id)
        size, head, digest, too_big = 0, b"", hashlib.sha256(), False
        try:
            with open(part, "wb") as out:
                while True:
                    chunk = stream.read(min(CHUNK, self.max_pdf + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if from_zip:
                        self.unpacked_bytes += len(chunk)
                        if self.unpacked_bytes > self.max_unzipped:
                            raise UploadRejected("The ZIP unpacks to far too much data and was refused.")
                    if size > self.max_pdf:
                        too_big = True
                        break
                    if not head:
                        head = chunk[:5]
                    digest.update(chunk)
                    out.write(chunk)

            if too_big:
                return self._invalid(name, f"The file is larger than {self.s.max_upload_mb} MB.")
            if size == 0:
                return self._invalid(name, "The file is empty.")
            if not head.startswith(b"%PDF-"):
                return self._invalid(name, "This is not a real PDF (it only has a .pdf name).")
            pages, problem = _inspect_pdf(part)
            if problem:
                return self._invalid(name, problem)
            if self.total_bytes + size > self.max_total:
                raise UploadRejected(f"This batch would be larger than {self.s.max_batch_total_mb} MB in total.")

            stored = batch_storage.new_stored_name()
            final = batch_storage.batch_dir(self.batch.id) / stored
            part.replace(final)
            self.written.append(final)
            self.total_bytes += size
            self.db.add(Payslip(
                batch_id=self.batch.id, original_filename=name, stored_filename=stored, file_size=size,
                file_sha256=digest.hexdigest(), page_count=pages, match_status=MatchStatus.pending.value,
                match_method=MatchMethod.none.value,
            ))
            self.result.accepted += 1
        finally:
            part.unlink(missing_ok=True)

    def cleanup(self) -> None:
        for path in self.written:
            path.unlink(missing_ok=True)
        self.written.clear()


def _inspect_pdf(path: Path) -> tuple[int | None, str | None]:
    """(page count, None) for a usable PDF, else (None, plain-language reason). Error details are never shown."""
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            return None, "The PDF is password-protected. Remove the password and upload it again."
        pages = len(reader.pages)
    except Exception:
        return None, "The PDF is damaged and cannot be opened."
    if pages < 1:
        return None, "The PDF is damaged and cannot be opened."
    return pages, None


def _run(db: Session, batch: Batch, work) -> IngestResult:
    ingest = _Ingest(db, batch)
    try:
        work(ingest)
        db.flush()
    except BaseException:
        ingest.cleanup()
        raise
    return ingest.result


def ingest_pdfs(db: Session, batch: Batch, uploads) -> IngestResult:
    """`uploads`: objects with .filename and a readable binary .file (Starlette UploadFile)."""
    uploads = [u for u in uploads if (getattr(u, "filename", "") or "").strip()]
    if not uploads:
        raise UploadRejected("Choose at least one PDF file.")

    def work(ing: _Ingest) -> None:
        ing.check_count(len(uploads))
        for u in uploads:
            ing.add_file(u.filename, u.file)

    return _run(db, batch, work)


def ingest_zip(db: Session, batch: Batch, upload) -> IngestResult:
    if upload is None or not (getattr(upload, "filename", "") or "").strip():
        raise UploadRejected("Choose a ZIP file.")

    def work(ing: _Ingest) -> None:
        try:
            zf = zipfile.ZipFile(upload.file)
        except zipfile.BadZipFile:
            raise UploadRejected("That is not a valid ZIP file.") from None
        with zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise UploadRejected("The ZIP contains too many entries.")
            declared, wanted = 0, []
            for info in infos:
                if _path_is_unsafe(info.filename):
                    raise UploadRejected("The ZIP contains unsafe file paths and was refused.")
                if info.is_dir():
                    ing.result.ignored += 1
                    continue
                parts = re.split(r"[\\/]+", info.filename)
                base = parts[-1]
                if "__MACOSX" in parts or base.startswith(".") or base.lower() in IGNORED_NAMES:
                    ing.result.ignored += 1
                    continue
                if info.flag_bits & 0x1:
                    raise UploadRejected("The ZIP is password-protected. Remove the password and try again.")
                if _ext(base) in ARCHIVE_EXTENSIONS:
                    raise UploadRejected("The ZIP contains another ZIP or archive. Nested archives are not accepted: unpack it first.")
                declared += info.file_size
                if (info.file_size > RATIO_CHECK_MIN_BYTES
                        and info.file_size > MAX_EXPANSION_RATIO * max(info.compress_size, 1)):
                    raise UploadRejected("The ZIP looks like a zip bomb (it expands far too much) and was refused.")
                wanted.append((info, base))
            if declared > ing.max_unzipped:
                raise UploadRejected("The ZIP unpacks to far too much data and was refused.")
            if not wanted:
                raise UploadRejected("The ZIP has no files in it.")
            ing.check_count(len(wanted))
            for info, base in wanted:
                try:
                    with zf.open(info) as stream:
                        ing.add_file(base, stream, from_zip=True)
                except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError):
                    ing._invalid(safe_display_name(base), "This file inside the ZIP is damaged and could not be unpacked.")

    return _run(db, batch, work)


def start_processing(batch: Batch, pending: int) -> None:
    """Mark the batch as 'matching in progress' (the background job then does the work)."""
    batch.processing_state = ProcessingState.processing.value
    batch.processing_total = pending
    batch.processing_done = 0
    batch.processing_heartbeat_at = utcnow()
