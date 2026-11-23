"""Reads an uploaded employee file (.xlsx or .csv) ENTIRELY IN MEMORY and turns it into clean rows.

The id_number column contains full Omang or passport numbers, so: the file is never written to disk, and each
number is ENCRYPTED the moment it is read (the parsed rows, and so the preview, only ever hold the encrypted value
and the last 4 characters). Nothing in this module logs row contents.
"""
import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from app.services.id_crypto import encrypt_id
from app.services.ids import parse_id
from app.services.names import clean_display_name, name_words
from app.services.phones import normalise_phone

MAX_FILE_BYTES = 950_000          # stays below the 1 MiB point where uploads would spill to a temp file
MAX_UNPACKED_BYTES = 30_000_000   # zip-bomb guard for .xlsx
REQUIRED_COLUMNS = ("first_name", "surname")
KNOWN_COLUMNS = ("first_name", "surname", "phone", "id_number", "id_type", "employee_code")
# the fake rows in the downloadable template: the importer rejects them if they are left in
EXAMPLE_ROWS = {("example", "person"), ("example", "traveller")}


class ImportFileError(Exception):
    """The whole file is unusable. The message is safe to show to HR."""


@dataclass
class ParsedRow:
    row_number: int                   # as HR sees it in Excel (header = row 1)
    first_name: str = ""
    surname: str = ""
    phone: str | None = None          # normalised E.164, None if blank or invalid
    id_type: str | None = None        # "omang" or "passport"
    id_last4: str | None = None       # the last 4 characters (uppercase): the PDF password
    # the full number, ENCRYPTED already (never plain text here). Hidden from repr() so it can't end up in logs or test output.
    id_encrypted: str | None = field(default=None, repr=False)
    employee_code: str | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    rows: list[ParsedRow]
    notices: list[str] = field(default_factory=list)  # file-level remarks (ignored columns, missing optional columns)


def parse_upload(filename: str, data: bytes, max_rows: int) -> ParseResult:
    if not data:
        raise ImportFileError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ImportFileError("The file is too large (limit about 900 KB). Split it into smaller files.")
    ext = Path(filename or "").suffix.lower()
    if ext == ".xlsx":
        raw = _read_xlsx(data, max_rows)
    elif ext == ".csv":
        raw = _read_csv(data, max_rows)
    elif ext in (".xlsm", ".xltm", ".xlsb", ".xltx"):
        raise ImportFileError("Macro-enabled and other special Excel formats are not accepted. Save as a normal .xlsx or .csv file.")
    elif ext == ".xls":
        raise ImportFileError("Old .xls files are not supported. In Excel use File > Save As > Excel Workbook (.xlsx) or CSV.")
    else:
        raise ImportFileError("Please upload an .xlsx or .csv file.")
    return _build_rows(raw, max_rows)


# ---- reading the file into plain text cells --------------------------------------------------------------------

def _cell_text(value) -> str:
    """Every cell becomes text, so Excel number formatting can't change the value."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def _read_xlsx(data: bytes, max_rows: int) -> list[tuple[int, list[str]]]:
    if not data.startswith(b"PK\x03\x04"):
        raise ImportFileError("That does not look like a real .xlsx file.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportFileError("That does not look like a real .xlsx file.") from None
    names = [n.lower() for n in zf.namelist()]
    if any(n.endswith("vbaproject.bin") or "macrosheets/" in n for n in names):
        raise ImportFileError("This workbook contains macros, so it was rejected. Save a plain .xlsx (no macros) and try again.")
    if "xl/workbook.xml" not in names:
        raise ImportFileError("That does not look like an Excel workbook.")
    if sum(i.file_size for i in zf.infolist()) > MAX_UNPACKED_BYTES:
        raise ImportFileError("The workbook is too big once unpacked.")

    from openpyxl import load_workbook  # imported here so a missing package only breaks imports

    rows: list[tuple[int, list[str]]] = []
    scan_limit = max_rows * 5 + 100  # stops us wading through a million formatted-but-empty rows
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        try:
            ws = wb.worksheets[0]  # first sheet only
            for number, values in enumerate(ws.iter_rows(values_only=True), start=1):
                if number > scan_limit:
                    raise ImportFileError("The sheet has too many rows.")
                rows.append((number, [_cell_text(v) for v in values]))
        finally:
            wb.close()
    except ImportFileError:
        raise
    except Exception:
        raise ImportFileError("The workbook could not be read. Is it damaged or password-protected?") from None
    return rows


def _read_csv(data: bytes, max_rows: int) -> list[tuple[int, list[str]]]:
    if b"\x00" in data[:4096]:
        raise ImportFileError("That does not look like a CSV text file.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportFileError("The text encoding of this CSV could not be read. Save it as 'CSV UTF-8' from Excel.")

    first_line = next((l for l in text.splitlines() if l.strip()), "")
    delimiter = max((",", ";", "\t"), key=first_line.count) if first_line else ","
    try:
        return [
            (number, [c.strip() for c in record])
            for number, record in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), start=1)
            if number <= max_rows * 5 + 100
        ]
    except csv.Error:
        raise ImportFileError("This CSV file could not be read (it may be damaged).") from None


# ---- turning cells into rows -----------------------------------------------------------------------------------

def _header_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


_ALIASES = {_header_key(c): c for c in KNOWN_COLUMNS} | {"omang": "id_number"}  # "omang" = the old header


def _build_rows(raw: list[tuple[int, list[str]]], max_rows: int) -> ParseResult:
    header_at = next((i for i, (_, cells) in enumerate(raw) if any(cells)), None)
    if header_at is None:
        raise ImportFileError("The file has no data.")
    _, header = raw[header_at]

    index: dict[str, int] = {}
    ignored: list[str] = []
    for position, text in enumerate(header):
        if not text:
            continue
        column = _ALIASES.get(_header_key(text))
        if column is None:
            ignored.append(text[:30])
        elif column in index:
            raise ImportFileError(f"The column '{column}' appears more than once in the header row.")
        else:
            index[column] = position
    missing = [c for c in REQUIRED_COLUMNS if c not in index]
    if missing:
        raise ImportFileError(
            "Missing required column(s): " + ", ".join(missing) + ". "
            "The first row must contain: first_name, surname, phone, id_number, id_type (optional), employee_code (optional)."
        )

    notices: list[str] = []
    for optional, label in (("phone", "phone numbers"), ("id_number", "ID numbers")):
        if optional not in index:
            notices.append(f"There is no '{optional}' column, so {label} will not be set or changed.")
    if ignored:
        notices.append("Ignored column(s): " + ", ".join(ignored[:8]) + (" ..." if len(ignored) > 8 else "") + ".")

    def get(cells: list[str], column: str) -> str:
        i = index.get(column)
        return cells[i] if i is not None and i < len(cells) else ""

    rows: list[ParsedRow] = []
    for number, cells in raw[header_at + 1:]:
        if not any(cells):
            continue  # completely blank line
        if len(rows) >= max_rows:
            raise ImportFileError(f"Too many rows: the limit is {max_rows} employees per import.")
        rows.append(_parse_row(number, get, cells))
    if not rows:
        raise ImportFileError("The file has a header row but no employees.")
    return ParseResult(rows=rows, notices=notices)


def _parse_row(number: int, get, cells: list[str]) -> ParsedRow:
    row = ParsedRow(row_number=number)
    row.first_name = clean_display_name(get(cells, "first_name"))
    row.surname = clean_display_name(get(cells, "surname"))
    for label, value in (("First name", row.first_name), ("Surname", row.surname)):
        if not value:
            row.errors.append(f"{label} is missing.")
        elif not name_words(value):
            row.errors.append(f"{label} must contain letters.")
        elif len(value) > 100:
            row.errors.append(f"{label} is too long (maximum 100 characters).")

    if (row.first_name.lower(), row.surname.lower()) in EXAMPLE_ROWS:
        row.errors.append("This is the example row from the template. Delete it, or replace it with a real employee.")

    phone = normalise_phone(get(cells, "phone"))
    if phone.error:
        row.errors.append(f"Phone: {phone.error}")
    row.phone = phone.value
    row.warnings += [f"Phone: {w}" for w in phone.warnings]

    ident = parse_id(get(cells, "id_number"), get(cells, "id_type"))  # the plain number lives only until the next line
    if ident.error:
        row.errors.append(f"ID: {ident.error}")
    row.id_type, row.id_last4 = ident.id_type, ident.last4
    row.id_encrypted = encrypt_id(ident.number) if ident.number else None
    row.warnings += [f"ID: {w}" for w in ident.warnings]

    code = get(cells, "employee_code").strip()
    if code:
        if len(code) > 50:
            row.errors.append("Employee code is too long (maximum 50 characters).")
        elif not code.isprintable():
            row.errors.append("Employee code contains invalid characters.")
        else:
            row.employee_code = code
    return row
