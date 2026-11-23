"""Reading PDF text and cross-checking it against the matched employee. Nothing here logs file contents or names.

Rules:
- The matched employee's first name AND surname words are all found in the text  -> ok
- not found, but ANOTHER active employee's full name is in the text               -> possible_mismatch (HR must review)
- no extractable text (a scanned image)                                           -> no_text      (information only)
- text exists but no employee's name is in it                                     -> name_not_found (information only)
"""
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from app.models.enums import PdfCheck
from app.services.matching import employee_words
from app.services.names import name_words

MAX_PAGES_READ = 3          # payslips are short: the name is on the first pages
MAX_TEXT_CHARS = 30_000
MIN_WORDS_FOR_TEXT = 3      # fewer letters-only words than this = treat the PDF as having no text


def extract_text(path: Path) -> str | None:
    """Text of the first pages, or None when it cannot be read. Never raises."""
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            return None
        parts: list[str] = []
        for page in reader.pages[:MAX_PAGES_READ]:
            parts.append(page.extract_text() or "")
            if sum(map(len, parts)) > MAX_TEXT_CHARS:
                break
        return "".join(parts)[:MAX_TEXT_CHARS]
    except Exception:  # pypdf can raise many kinds of errors on odd files; the reason is not needed
        return None


def text_word_set(text: str | None) -> frozenset[str]:
    return frozenset(name_words(text or ""))


@dataclass
class TextCheck:
    result: str                       # a PdfCheck value
    other_employee_id: int | None = None


def _name_in(words: frozenset[str], first_name: str, surname: str) -> bool:
    needed = employee_words(first_name, surname)
    return bool(needed) and needed <= words


def check_text(words: frozenset[str], employee, active_employees) -> TextCheck:
    """`employee` is the matched employee; `active_employees` is everyone who could own the PDF."""
    if len(words) < MIN_WORDS_FOR_TEXT:
        return TextCheck(PdfCheck.no_text.value)
    if _name_in(words, employee.first_name, employee.surname):
        return TextCheck(PdfCheck.ok.value)
    for other in active_employees:
        if other.id != employee.id and _name_in(words, other.first_name, other.surname):
            return TextCheck(PdfCheck.possible_mismatch.value, other.id)
    return TextCheck(PdfCheck.name_not_found.value)
