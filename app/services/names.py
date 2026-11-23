"""Name normalisation, shared by employees and (later) payslip file matching.

"Thabo Molefe", "molefe_thabo", "THABO-Molefe.pdf" -> words {"molefe", "thabo"}.
Rules: strip accents, lowercase, drop apostrophes (O'Brien -> obrien), split on anything that is not a
letter (so digits, _ - . and spaces all separate words and digits are dropped).
"""
import re
import unicodedata

_APOSTROPHES = re.compile("['’`´ʼ]")
_NOT_LETTERS = re.compile(r"[^a-z]+")
_SPECIAL = str.maketrans({"ø": "o", "ł": "l", "đ": "d", "æ": "ae", "œ": "oe", "ß": "ss"})


def name_words(text: str | None) -> list[str]:
    s = unicodedata.normalize("NFKD", text or "").lower().translate(_SPECIAL)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _APOSTROPHES.sub("", s)
    return [w for w in _NOT_LETTERS.split(s) if w]


def name_key(first_name: str, surname: str) -> str:
    """Order-independent key: the sorted set of words in first name + surname."""
    return " ".join(sorted(set(name_words(first_name)) | set(name_words(surname))))


def clean_display_name(text: str | None) -> str:
    """Trim and collapse repeated spaces; keeps the person's own capitalisation and accents."""
    return re.sub(r"\s+", " ", (text or "")).strip()
