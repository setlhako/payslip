"""Phone number normalisation to E.164 (Botswana +267 by default)."""
import re
from dataclasses import dataclass, field

_STRIP = re.compile(r"[\s\-(). ]")  # spaces, dashes, brackets, dots, non-breaking space
_DIGITS = re.compile(r"[0-9]+")


@dataclass
class PhoneResult:
    value: str | None = None           # e.g. "+26771234567"; None when blank or invalid
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def normalise_phone(raw: str | None) -> PhoneResult:
    s = _STRIP.sub("", raw or "")
    if not s:
        return PhoneResult()  # blank: the caller decides whether that matters
    if s.startswith("00"):
        s = "+" + s[2:]
    has_plus = s.startswith("+")
    digits = s[1:] if has_plus else s
    if not _DIGITS.fullmatch(digits):
        return PhoneResult(error="Phone number may only contain digits (with an optional + at the start).")

    if not has_plus:
        if len(digits) == 8:
            digits = "267" + digits                      # 71234567
        elif len(digits) == 11 and digits.startswith("267"):
            pass                                          # 26771234567
        else:
            return PhoneResult(
                error="Not recognised. Botswana numbers have 8 digits (e.g. 71234567); "
                      "numbers from other countries must start with + and the country code."
            )

    warnings: list[str] = []
    if digits.startswith("267"):
        national = digits[3:]
        if len(national) != 8:
            return PhoneResult(error="Botswana numbers have 8 digits after +267.")
        if not national.startswith("7"):
            warnings.append(
                "This does not look like a Botswana mobile number (mobiles are 8 digits starting with 7). "
                "WhatsApp needs a mobile number."
            )
    elif not (8 <= len(digits) <= 15) or digits.startswith("0"):
        return PhoneResult(error="Not a valid international number (country code + number, 8 to 15 digits).")
    return PhoneResult(value="+" + digits, warnings=warnings)


def mask_phone(phone: str | None) -> str:
    """Show only the last 3 digits, e.g. '+267 ***** 567'."""
    return f"***{phone[-3:]}" if phone else "-"

