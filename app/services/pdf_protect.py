"""Password-protecting a payslip PDF for sending: AES-256, in memory only.

- user password = the employee's id_last4 (uppercase, case-sensitive); owner password = random, never stored or returned.
- The protected copy exists ONLY as bytes in memory (handed to the WhatsApp upload, then dropped). Nothing is written to disk.
- It is VERIFIED before it may be used: it must be AES-256, must refuse to open with no password, a wrong password and a
  wrong-case password, and must open with the right one. If any check fails nothing is sent (ProtectError): an unprotected
  payslip is never sent as a fallback.
Messages are plain language and never contain the password.
"""
import io
import secrets
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions  # noqa: F401  (kept for readers: default permissions are used)


class ProtectError(Exception):
    """The protected copy could not be made or failed its safety check. Safe to show to HR."""


def protect_pdf(source: Path, password: str | None) -> bytes:
    if not password or not password.strip():
        raise ProtectError("This employee has no ID number, so the PDF password cannot be set.")
    try:
        reader = PdfReader(str(source), strict=False)
        if reader.is_encrypted:
            raise ProtectError("The stored PDF is already password-protected.")
        pages = len(reader.pages)
        writer = PdfWriter(clone_from=reader)
        writer.encrypt(user_password=password, owner_password=secrets.token_urlsafe(32), algorithm="AES-256")
        out = io.BytesIO()
        writer.write(out)
        data = out.getvalue()
    except ProtectError:
        raise
    except Exception:
        raise ProtectError("The PDF could not be password-protected.") from None
    verify_protected(data, password, pages)
    return data


def _opens_with(data: bytes, password: str) -> bool:
    try:
        reader = PdfReader(io.BytesIO(data))
        if int(reader.decrypt(password)) == 0:
            return False
        len(reader.pages)
        return True
    except Exception:
        return False


def verify_protected(data: bytes, password: str, expected_pages: int) -> None:
    """Raises ProtectError unless the copy is strongly protected and opens ONLY with the right password."""
    try:
        reader = PdfReader(io.BytesIO(data))
        encrypt = reader.trailer["/Encrypt"] if reader.is_encrypted else None
        strong = encrypt is not None and encrypt.get("/V") == 5          # V5 = AES-256
        without = PdfReader(io.BytesIO(data))
        can_read_without = True
        try:
            len(without.pages)
        except Exception:
            can_read_without = False
    except Exception:
        raise ProtectError("The protected PDF failed its safety check, so nothing was sent.") from None
    wrong = {"", password + "x", password[:-1], password.swapcase()} - {password}
    if not strong or can_read_without or any(_opens_with(data, w) for w in wrong):
        raise ProtectError("The protected PDF failed its safety check, so nothing was sent.")
    try:
        right = PdfReader(io.BytesIO(data))
        ok = int(right.decrypt(password)) != 0 and len(right.pages) == expected_pages
    except Exception:
        ok = False
    if not ok:
        raise ProtectError("The protected PDF failed its safety check, so nothing was sent.")
