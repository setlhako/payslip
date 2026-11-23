"""Cross-checking the matched employee against the text inside the PDF."""
from types import SimpleNamespace as E

from app.models.enums import PdfCheck
from app.services.pdf_check import check_text, extract_text, text_word_set
from tests.pdfs import locked_pdf, named_pdf, pdf_with, scanned_pdf

THABO = E(id=1, first_name="Thabo", surname="Molefe")
NEO = E(id=2, first_name="Neo", surname="Dube")
ALL = [THABO, NEO, E(id=3, first_name="Ana", surname="Silva")]


def words_of(tmp_path, data: bytes):
    path = tmp_path / "x.pdf"
    path.write_bytes(data)
    return text_word_set(extract_text(path))


def test_matched_name_found_in_the_text_is_ok(tmp_path):
    assert check_text(words_of(tmp_path, named_pdf("Thabo", "Molefe")), THABO, ALL).result == PdfCheck.ok.value


def test_name_order_and_case_in_the_text_do_not_matter(tmp_path):
    words = words_of(tmp_path, pdf_with("Employee: MOLEFE, thabo", "Pay month: November 2026"))
    assert check_text(words, THABO, ALL).result == PdfCheck.ok.value


def test_another_employees_name_in_the_text_is_a_possible_mismatch(tmp_path):
    words = words_of(tmp_path, named_pdf("Neo", "Dube"))
    r = check_text(words, THABO, ALL)
    assert r.result == PdfCheck.possible_mismatch.value and r.other_employee_id == NEO.id


def test_both_names_present_counts_as_ok(tmp_path):
    words = words_of(tmp_path, pdf_with("Thabo Molefe", "Approved by Neo Dube", "Pay month: November 2026"))
    assert check_text(words, THABO, ALL).result == PdfCheck.ok.value


def test_first_name_alone_is_not_enough(tmp_path):
    words = words_of(tmp_path, pdf_with("Employee: Thabo", "Pay month: November 2026", "Gross pay 100"))
    assert check_text(words, THABO, ALL).result == PdfCheck.name_not_found.value


def test_text_without_any_employee_name_is_information_only(tmp_path):
    words = words_of(tmp_path, pdf_with("Employee: Someone Else", "Pay month: November 2026", "Gross pay 100"))
    assert check_text(words, THABO, ALL).result == PdfCheck.name_not_found.value


def test_a_scanned_pdf_with_no_text_is_no_text_not_an_error(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(scanned_pdf("Thabo Molefe"))
    words = text_word_set(extract_text(path))
    assert check_text(words, THABO, ALL).result == PdfCheck.no_text.value


def test_unreadable_files_give_none_instead_of_raising(tmp_path):
    for data in (b"", b"%PDF-1.4 garbage", b"not a pdf at all", locked_pdf("Thabo Molefe")):
        path = tmp_path / "bad.pdf"
        path.write_bytes(data)
        assert extract_text(path) is None
    assert extract_text(tmp_path / "missing.pdf") is None


def test_the_matched_employee_is_never_reported_as_the_other_one(tmp_path):
    words = words_of(tmp_path, named_pdf("Neo", "Dube"))
    assert check_text(words, NEO, ALL).result == PdfCheck.ok.value
