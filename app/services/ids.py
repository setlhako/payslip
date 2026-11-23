"""ID number handling for Omang and passports.

HR keeps the FULL number, but only ever ENCRYPTED in the database (services/id_crypto.py). The last 4 characters are
also kept in plain text (`id_last4`), because they are the password that opens the employee's payslip PDF.

Rule for this module: the raw number may appear in memory only. It is never put in an error message, warning,
log line, preview, audit entry, session or file.
"""
import re
from dataclasses import dataclass, field

OMANG, PASSPORT = "omang", "passport"
ID_TYPES = (OMANG, PASSPORT)
ID_TYPE_LABELS = {OMANG: "Omang", PASSPORT: "Passport"}

OMANG_LENGTH = 9
PASSPORT_MIN, PASSPORT_MAX = 5, 15          # outside this range = error
PASSPORT_USUAL_MIN, PASSPORT_USUAL_MAX = 6, 9  # outside this range = warning
FULL_MASK = "•" * 9                    # same length for everyone, so the real length is not revealed


@dataclass
class IdResult:
    id_type: str | None = None   # "omang" / "passport"; None when there is no ID
    number: str | None = None    # the full number, cleaned (spaces/dashes removed, passports upper-cased). IN MEMORY ONLY.
    last4: str | None = None     # the last 4 characters of `number`, uppercase
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def parse_id_type(raw: str | None) -> tuple[str | None, str | None]:
    """Returns (id_type, error). Blank gives (None, None)."""
    value = (raw or "").strip().lower()
    if not value:
        return None, None
    if value in ID_TYPES:
        return value, None
    return None, "ID type must be 'omang' or 'passport'."


def parse_id(raw_number: str | None, raw_type: str | None = None) -> IdResult:
    """Validate an ID number and work out its last 4 characters.

    - id_type given: the number must fit that type.
    - id_type blank: guess (all digits = omang, contains letters = passport) and WARN so HR confirms.
    - Spaces and dashes are ignored.
    - Omang: digits only. Warns when it does not have 9 digits (Excel drops leading zeros). Shorter than 4 digits is
      left-padded with zeros so there are always 4 characters for the password.
    - Passport: letters and digits, 5 to 15 characters (error outside that), warns outside 6 to 9. Stored upper-case.
    """
    id_type, type_error = parse_id_type(raw_type)
    if type_error:
        return IdResult(error=type_error)

    s = re.sub(r"[\s\-]+", "", raw_number or "")
    if not s:
        result = IdResult()
        if id_type:
            result.warnings.append("An ID type was given but there is no ID number, so the ID was left unchanged.")
        return result
    if not re.fullmatch(r"[A-Za-z0-9]+", s):
        return IdResult(error="ID number may only contain letters and digits.")  # value deliberately not repeated

    warnings: list[str] = []
    if id_type is None:
        if s.isdigit():
            id_type = OMANG
            warnings.append("ID type was blank, so it was treated as an Omang (digits only). Please confirm.")
        else:
            id_type = PASSPORT
            warnings.append("ID type was blank, so it was treated as a passport (it contains letters). Please confirm.")

    if id_type == OMANG:
        if not s.isdigit():
            return IdResult(error="An Omang must contain digits only. If this is a passport, set the ID type to passport.")
        if len(s) < 4:
            warnings.append(
                f"Omang had only {len(s)} digit(s), so it was padded with leading zeros to 4 digits "
                "(Excel drops leading zeros). Please check it is right."
            )
            s = s.zfill(4)
        elif len(s) != OMANG_LENGTH:
            warnings.append(
                f"An Omang normally has {OMANG_LENGTH} digits but this one has {len(s)} "
                "(Excel drops leading zeros). Please check it is the complete number."
            )
        return IdResult(OMANG, s, s[-4:], warnings)

    if not PASSPORT_MIN <= len(s) <= PASSPORT_MAX:
        return IdResult(error=f"A passport number needs {PASSPORT_MIN} to {PASSPORT_MAX} letters or digits.")
    if not PASSPORT_USUAL_MIN <= len(s) <= PASSPORT_USUAL_MAX:
        warnings.append(
            f"Passport numbers usually have {PASSPORT_USUAL_MIN} to {PASSPORT_USUAL_MAX} characters but this one has "
            f"{len(s)}. Please check it is right."
        )
    s = s.upper()
    return IdResult(PASSPORT, s, s[-4:], warnings)
