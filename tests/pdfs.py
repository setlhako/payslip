"""Tiny fake PDFs for tests (made with the same helpers as the make_test_payslips script)."""
import io

import pytest

pytest.importorskip("fpdf")

from app.scripts.make_test_payslips import image_only_pdf, protected_pdf, text_pdf  # noqa: E402


def pdf_with(*lines: str) -> bytes:
    """A real one-page PDF that contains these lines of text."""
    return text_pdf(list(lines))


def named_pdf(first: str, surname: str) -> bytes:
    return text_pdf([f"Employee: {first} {surname}", "Pay month: November 2026"])


def scanned_pdf(label: str = "Some Person") -> bytes:
    return image_only_pdf(label)


def locked_pdf(*lines: str) -> bytes:
    return protected_pdf(list(lines) or ["secret"])


def upload_obj(name: str, data: bytes):
    """Looks like Starlette's UploadFile for the ingest service."""
    from types import SimpleNamespace

    return SimpleNamespace(filename=name, file=io.BytesIO(data))
