"""Batches: access, create + upload, and what the matching decides."""
import io
import re
import zipfile

import pytest

from app.config import get_settings
from app.models import Batch, Employee, Payslip
from app.services import batch_storage
from tests.batch_helpers import (  # noqa: F401  (people is a fixture)
    batch_id_of, by_name, create, good_batch, page, payslips, people, post,
)
from tests.conftest import make_employee
from tests.pdfs import locked_pdf, named_pdf, scanned_pdf


# ---- access ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/batches", "/batches/new", "/batches/1", "/batches/1/confirm"])
def test_pages_need_a_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("path", [
    "/batches/new", "/batches/1/upload", "/batches/1/ready", "/batches/1/reopen", "/batches/1/delete", "/batches/1/rematch",
    "/batches/1/payslips/1/match", "/batches/1/payslips/1/confirm", "/batches/1/payslips/1/remove",
])
def test_every_batch_post_needs_a_csrf_token(logged_in, path):
    assert logged_in.post(path).status_code == 403
    assert logged_in.post(path, data={"csrf_token": "wrong"}).status_code == 403


def test_unknown_batch_or_a_file_of_another_batch_is_a_404(logged_in, people):
    assert logged_in.get("/batches/999").status_code == 404
    first, second = good_batch(logged_in), good_batch(logged_in)
    other_file = re.findall(r'id="file-(\d+)"', page(logged_in, second))[0]
    assert post(logged_in, f"/batches/{first}/payslips/{other_file}/remove").status_code == 404


def test_batches_nav_item_is_active_and_pages_use_the_same_layout(logged_in):
    for path in ("/batches", "/batches/new"):
        html = logged_in.get(path).text
        assert re.search(r'href="/batches" aria-current="page"', html) and 'class="mark"' in html and "page-head" in html


# ---- list and create form ----------------------------------------------------------------------------------------

def test_empty_list_and_new_form(logged_in):
    html = logged_in.get("/batches").text
    assert "No batches yet" in html and 'href="/batches/new"' in html
    form = logged_in.get("/batches/new").text
    assert 'enctype="multipart/form-data"' in form and 'name="files"' in form and "multiple" in form
    assert 'name="zip_file"' in form and 'name="pay_month_label"' in form and 'name="mode"' in form
    assert "300 files" in form and "10 MB per PDF" in form and "200 MB" in form


@pytest.mark.parametrize("label", ["November 2026", "Nov 2026", "nov 2026", "December 2026 (bonus)", "2026 January", "Sept. 2027"])
def test_good_pay_month_labels(logged_in, people, label):
    assert create(logged_in, [("a.pdf", named_pdf("A", "B"))], label=label).status_code == 303


@pytest.mark.parametrize("label,message", [
    ("", "Enter the pay month"), ("2026", "Include the month and the year"), ("November", "Include the month and the year"),
    ("Smarch 2026", "Include the month and the year"), ("<script>Nov 2026</script>", "3 to 60 letters"), ("N" * 80 + " 2026", "3 to 60"),
])
def test_bad_pay_month_labels_are_explained_and_nothing_is_created(logged_in, db, label, message):
    r = create(logged_in, [("a.pdf", named_pdf("A", "B"))], label=label)
    assert r.status_code == 422 and message in r.text and 'aria-invalid="true"' in r.text
    assert db.query(Batch).count() == 0


def test_creating_a_batch_uploads_matches_and_lists_it(logged_in, db, people):
    bid = good_batch(logged_in)
    listing = logged_in.get("/batches").text
    assert "Batch created. 3 files added." in listing
    assert "November 2026" in listing and f'href="/batches/{bid}"' in listing and "Draft" in listing
    assert re.findall(r'<td class="num[^"]*">(\d+)</td>', listing) == ["3", "3", "0", "0", "0"]   # files, matched, review, unmatched, invalid


def test_nothing_is_created_when_the_upload_is_rejected(logged_in, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_files_per_batch", 1)
    r = create(logged_in, [("a.pdf", named_pdf("A", "B")), ("b.pdf", named_pdf("C", "D"))])
    assert r.status_code == 400 and "Too many files" in r.text and "November 2026" in r.text      # the label is kept
    assert db.query(Batch).count() == 0 and db.query(Payslip).count() == 0
    root = batch_storage.storage_root()
    assert not (root.exists() and any(root.iterdir()))


def test_create_with_a_zip(logged_in, db, people):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payslips/Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))
        zf.writestr("__MACOSX/payslips/._x.pdf", b"junk")
    bid = batch_id_of(create(logged_in, mode="zip", zip_bytes=buf.getvalue()))
    assert list(by_name(db, bid)) == ["Thabo_Molefe.pdf"] and by_name(db, bid)["Thabo_Molefe.pdf"].match_status == "exact"


def test_a_bad_zip_is_explained_on_the_form(logged_in, db):
    r = create(logged_in, mode="zip", zip_bytes=b"this is not a zip")
    assert r.status_code == 400 and "not a valid ZIP" in r.text and db.query(Batch).count() == 0


def test_missing_files_are_explained(logged_in, db):
    assert "Choose a ZIP file" in create(logged_in, mode="zip").text
    assert "Choose at least one PDF" in create(logged_in, mode="pdfs").text
    assert "Choose how you are uploading" in create(logged_in, [("a.pdf", b"x")], mode="other").text


# ---- matching results --------------------------------------------------------------------------------------------

def test_exact_matches_are_automatic_and_confirmed(logged_in, db, people):
    bid = good_batch(logged_in)
    rows = by_name(db, bid)
    assert [p.match_status for p in rows.values()] == ["exact"] * 3
    assert rows["dube_neo.pdf"].employee_id == people["neo"].id and rows["dube_neo.pdf"].match_method == "filename_exact"
    assert all(p.confirmed and p.pdf_check == "ok" for p in rows.values())
    assert db.get(Batch, bid).processing_state == "done"


def test_the_review_page_shows_files_matches_badges_and_masked_phones(logged_in, db, people):
    html = page(logged_in, good_batch(logged_in))
    for text in ("Thabo_Molefe.pdf", "Thabo Molefe", "Neo Dube", "Exact", "***567", "***678", "***789", "Ready"):
        assert text in html, text
    for full in ("+26771234567", "71234567", "26771234567"):
        assert full not in html                                         # only the last 3 digits are ever shown
    assert "Active employees with no payslip in this batch (1)" in html and "Kea Quiet" in html
    assert 'class="summary-bar summary-6"' in html


def test_send_button_is_disabled_until_the_batch_is_ready_then_enabled(logged_in, people):
    bid = good_batch(logged_in)
    for path in (f"/batches/{bid}", f"/batches/{bid}/confirm"):
        html = logged_in.get(path).text
        assert re.search(r'<button type="button" class="btn btn-secondary" disabled[^>]*>.*?Send payslips</button>', html, re.S)
        assert f'href="/batches/{bid}/send"' not in html
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    for path in (f"/batches/{bid}", f"/batches/{bid}/confirm"):
        html = logged_in.get(path).text
        assert f'<a class="btn btn-primary" href="/batches/{bid}/send">' in html and "Send payslips</a>" in html


def test_typo_gives_needs_review_with_suggestions_and_a_score(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefee.pdf", named_pdf("Thabo", "Molefe"))]))
    p = payslips(db, bid)[0]
    assert (p.match_status, p.employee_id, p.confirmed) == ("needs_review", None, False)
    html = page(logged_in, bid)
    assert "Needs review" in html and "Use Thabo Molefe" in html and re.search(r"\(9\d% similar\)", html)


def test_unknown_person_is_unmatched(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("quentin_zzyzx.pdf", named_pdf("Quentin", "Zzyzx"))]))
    assert payslips(db, bid)[0].match_status == "unmatched"
    html = page(logged_in, bid)
    assert "Unmatched" in html and "No employee found" in html and "Not matched to an employee yet" in html


def test_same_name_shared_by_two_employees_needs_review_with_both_offered(logged_in, db, people):
    make_employee(db, "Thabo", "Molefe", "+26779999999", "2222", "E9")
    bid = batch_id_of(create(logged_in, [("thabo_molefe.pdf", named_pdf("Thabo", "Molefe"))]))
    p = payslips(db, bid)[0]
    assert p.match_status == "needs_review" and p.employee_id is None
    assert page(logged_in, bid).count("Use Thabo Molefe") == 2


def test_inactive_employees_are_never_matched_automatically(logged_in, db):
    make_employee(db, "Gone", "Away", "+26771111111", "1234", active=False)
    bid = batch_id_of(create(logged_in, [("gone_away.pdf", named_pdf("Gone", "Away"))]))
    assert payslips(db, bid)[0].match_status == "unmatched"


def test_file_name_matching_ignores_order_case_digits_and_accents(logged_in, db):
    make_employee(db, "Zoë", "O'Brien", "+26771111111", "1234")
    bid = batch_id_of(create(logged_in, [("OBRIEN-zoe 2026-11.pdf", named_pdf("Zoë", "O'Brien"))]))
    assert payslips(db, bid)[0].match_status == "exact"


def test_possible_mismatch_when_the_pdf_names_another_employee(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefe.pdf", named_pdf("Neo", "Dube"))]))
    p = payslips(db, bid)[0]
    assert (p.match_status, p.employee_id, p.confirmed, p.pdf_check) == ("exact", people["thabo"].id, False, "possible_mismatch")
    assert p.text_names_employee_id == people["neo"].id
    html = page(logged_in, bid)
    assert "Possible mismatch" in html and "looks like a different employee (Neo Dube)" in html and "Confirm match" in html


def test_a_scan_with_no_text_is_an_information_note_not_an_error(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefe.pdf", scanned_pdf("Thabo Molefe"))]))
    p = payslips(db, bid)[0]
    assert (p.match_status, p.pdf_check, p.confirmed) == ("exact", "no_text", True)
    html = page(logged_in, bid)
    assert "Could not verify against PDF content" in html and "Ready" in html and "row-error" not in html


def test_invalid_files_are_listed_with_a_reason_and_not_stored(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [
        ("fake.pdf", b"just text"), ("locked.pdf", locked_pdf("x")), ("empty.pdf", b""), ("good.pdf", named_pdf("A", "B")),
    ]))
    html = page(logged_in, bid)
    assert html.count("Invalid file</span>") >= 3 and "not a real PDF" in html and "password-protected" in html and "empty" in html
    assert sorted(p.stored_filename is None for p in payslips(db, bid)) == [False, True, True, True]


def test_problem_rows_are_listed_first(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [
        ("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe")), ("quentin_zzyzx.pdf", named_pdf("Q", "Z")), ("fake.pdf", b"nope"),
    ]))
    html = page(logged_in, bid)
    ready_at = html.index("Thabo_Molefe.pdf")
    assert html.index("fake.pdf") < ready_at and html.index("quentin_zzyzx.pdf") < ready_at
    assert "?show=problems" in html and "Need attention" in html


def test_needs_attention_filter(logged_in, people):
    bid = batch_id_of(create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe")), ("quentin_zzyzx.pdf", named_pdf("Q", "Z"))]))
    only = page(logged_in, bid, show="problems")
    assert "quentin_zzyzx.pdf" in only and "Thabo_Molefe.pdf" not in only


def test_the_batches_list_counts_by_state(logged_in, db, people):
    batch_id_of(create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe")), ("thabo_molefee.pdf", named_pdf("Thabo", "Molefe")),
                                   ("quentin_zzyzx.pdf", named_pdf("Q", "Z")), ("fake.pdf", b"x")], label="October 2026"))
    html = logged_in.get("/batches").text
    assert re.findall(r'<td class="num[^"]*">(\d+)</td>', html) == ["4", "1", "1", "1", "1"]


def test_employee_records_are_not_changed_by_matching(logged_in, db, people):
    before = [(e.id, e.phone, e.id_last4, e.id_number_encrypted, e.active) for e in db.query(Employee).order_by(Employee.id)]
    good_batch(logged_in)
    db.expire_all()
    assert before == [(e.id, e.phone, e.id_last4, e.id_number_encrypted, e.active) for e in db.query(Employee).order_by(Employee.id)]
