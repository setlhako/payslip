"""Batches: review actions, readiness checks, ready-to-send rules, background progress, audit and privacy."""
import base64
import logging
import re
from datetime import timedelta

import pytest

from app.config import get_settings
from app.models import Batch, Payslip
from app.models.mixins import utcnow
from app.services import batch_storage
from tests.batch_helpers import (  # noqa: F401  (people is a fixture)
    add_more, audit_rows, batch_id_of, by_name, confirm_page, create, good_batch, page, payslips, people, post,
)
from tests.conftest import make_employee
from tests.pdfs import locked_pdf, named_pdf


def actions(db, name):
    return [a for a in audit_rows(db) if a.action == name]


# ---- review actions ----------------------------------------------------------------------------------------------

def test_accept_a_suggestion_in_one_click_records_manual_and_audits_ids_only(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefee.pdf", named_pdf("Thabo", "Molefe"))]))
    p = payslips(db, bid)[0]
    r = post(logged_in, f"/batches/{bid}/payslips/{p.id}/match", employee_id=str(people["thabo"].id))
    assert r.status_code == 303 and r.headers["location"] == f"/batches/{bid}#file-{p.id}"
    p = payslips(db, bid)[0]
    assert (p.employee_id, p.match_method, p.confirmed) == (people["thabo"].id, "manual", True)
    html = page(logged_in, bid)
    assert "Confirmed by HR" in html and "Match saved." in html
    (entry,) = actions(db, "batch_match_changed")
    assert entry.entity_id == p.id and entry.details == f"batch={bid} from_employee=None to_employee={people['thabo'].id}"


def test_change_the_match_with_the_searchable_list(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))]))
    html = page(logged_in, bid)
    assert '<datalist id="employee-options">' in html and f'value="Neo Dube (#{people["neo"].id})"' in html
    assert 'list="employee-options"' in html and "phone ending 678" in html
    p = payslips(db, bid)[0]
    post(logged_in, f"/batches/{bid}/payslips/{p.id}/match", employee_label=f"Neo Dube (#{people['neo'].id})")
    p = payslips(db, bid)[0]
    assert (p.employee_id, p.match_method) == (people["neo"].id, "manual")
    (entry,) = actions(db, "batch_match_changed")
    assert entry.details == f"batch={bid} from_employee={people['thabo'].id} to_employee={people['neo'].id}"


@pytest.mark.parametrize("label", ["", "Neo Dube", "Neo Dube (#abc)", "nobody (#9999)"])
def test_a_label_that_is_not_from_the_list_is_refused_politely(logged_in, db, people, label):
    bid = batch_id_of(create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))]))
    p = payslips(db, bid)[0]
    post(logged_in, f"/batches/{bid}/payslips/{p.id}/match", employee_label=label)
    assert payslips(db, bid)[0].employee_id == people["thabo"].id
    assert re.search(r"Pick an employee from the list|employee was not found", logged_in.get(f"/batches/{bid}").text)


def test_changing_a_match_rechecks_the_pdf_text_for_the_new_employee(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefe.pdf", named_pdf("Neo", "Dube"))]))      # the text names Neo
    p = payslips(db, bid)[0]
    assert p.pdf_check == "possible_mismatch"
    post(logged_in, f"/batches/{bid}/payslips/{p.id}/match", employee_id=str(people["neo"].id))
    p = payslips(db, bid)[0]
    assert (p.pdf_check, p.confirmed, p.match_method) == ("ok", True, "manual")


def test_confirming_a_possible_mismatch_clears_the_review(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefe.pdf", named_pdf("Neo", "Dube"))]))
    p = payslips(db, bid)[0]
    post(logged_in, f"/batches/{bid}/payslips/{p.id}/confirm")
    p = payslips(db, bid)[0]
    assert p.confirmed and p.employee_id == people["thabo"].id and p.match_method == "filename_exact"
    html = page(logged_in, bid)
    assert "Exact" in html and "Possible mismatch</span>" not in html and "The PDF text names a different employee" in html
    assert len(actions(db, "batch_match_confirmed")) == 1


def test_confirm_needs_an_employee_first(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("quentin_zzyzx.pdf", named_pdf("Q", "Z"))]))
    p = payslips(db, bid)[0]
    post(logged_in, f"/batches/{bid}/payslips/{p.id}/confirm")
    assert not payslips(db, bid)[0].confirmed and "Choose an employee for this file first" in logged_in.get(f"/batches/{bid}").text


def test_invalid_files_cannot_be_matched(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("fake.pdf", b"nope")]))
    p = payslips(db, bid)[0]
    post(logged_in, f"/batches/{bid}/payslips/{p.id}/match", employee_id=str(people["thabo"].id))
    assert payslips(db, bid)[0].employee_id is None and "cannot be matched" in logged_in.get(f"/batches/{bid}").text


def test_remove_a_file_deletes_it_from_disk_and_the_batch(logged_in, db, people):
    bid = good_batch(logged_in)
    p = payslips(db, bid)[0]
    pid, stored = p.id, batch_storage.stored_path(bid, p.stored_filename)
    assert stored.exists()
    post(logged_in, f"/batches/{bid}/payslips/{pid}/remove")
    assert not stored.exists() and len(payslips(db, bid)) == 2
    (entry,) = actions(db, "batch_file_removed")
    assert entry.entity_id == pid and entry.details == f"batch={bid}"
    assert "File removed from the batch." in logged_in.get(f"/batches/{bid}").text


def test_remove_an_invalid_file(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("fake.pdf", b"nope"), ("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))]))
    post(logged_in, f"/batches/{bid}/payslips/{by_name(db, bid)['fake.pdf'].id}/remove")
    assert list(by_name(db, bid)) == ["Thabo_Molefe.pdf"]


def test_add_more_files_later_matches_them_too(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))]))
    assert add_more(logged_in, bid, [("Neo_Dube.pdf", named_pdf("Neo", "Dube")), ("bad.pdf", b"x")]).status_code == 303
    rows = by_name(db, bid)
    assert rows["Neo_Dube.pdf"].match_status == "exact" and rows["bad.pdf"].match_status == "invalid" and len(rows) == 3
    assert rows["Thabo_Molefe.pdf"].match_status == "exact"
    assert "1 file added, 1 invalid" in logged_in.get(f"/batches/{bid}").text
    assert [a.details for a in actions(db, "batch_files_uploaded")] == ["accepted=1 invalid=0 ignored=0", "accepted=1 invalid=1 ignored=0"]


def test_adding_files_over_the_limit_changes_nothing(logged_in, db, people, monkeypatch):
    bid = batch_id_of(create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))]))
    monkeypatch.setattr(get_settings(), "max_files_per_batch", 1)
    add_more(logged_in, bid, [("Neo_Dube.pdf", named_pdf("Neo", "Dube"))])
    assert list(by_name(db, bid)) == ["Thabo_Molefe.pdf"] and len(list(batch_storage.batch_dir(bid).iterdir())) == 1
    assert "Too many files" in logged_in.get(f"/batches/{bid}").text


def test_delete_batch_removes_rows_and_every_file(logged_in, db, people):
    bid = good_batch(logged_in)
    folder = batch_storage.batch_dir(bid)
    assert len(list(folder.iterdir())) == 3
    assert post(logged_in, f"/batches/{bid}/delete").headers["location"] == "/batches"
    assert db.query(Batch).count() == 0 and db.query(Payslip).count() == 0 and not folder.exists()
    (entry,) = actions(db, "batch_deleted")
    assert entry.entity_id == bid and entry.details == "files=3"
    assert "Batch deleted, including its files." in logged_in.get("/batches").text


def test_a_batch_being_sent_cannot_be_deleted(logged_in, db, people):
    bid = good_batch(logged_in)
    db.get(Batch, bid).status = "sending"
    db.commit()
    post(logged_in, f"/batches/{bid}/delete")
    assert db.query(Batch).count() == 1 and "cannot be deleted" in logged_in.get(f"/batches/{bid}").text


# ---- readiness: plain-words problems -----------------------------------------------------------------------------

def test_readiness_no_phone_no_id_and_inactive(logged_in, db):
    make_employee(db, "No", "Phone", None, "1234")
    make_employee(db, "No", "Idnumber", "+26771111111", None)
    gone = make_employee(db, "Soon", "Inactive", "+26772222222", "1234")
    bid = batch_id_of(create(logged_in, [(f"{n}.pdf", named_pdf("A", "B")) for n in ("no_phone", "no_idnumber", "soon_inactive")]))
    gone.active = False
    db.commit()
    html = page(logged_in, bid)
    assert "This employee has no phone number." in html
    assert "This employee has no ID number, so the PDF password cannot be set." in html
    assert "This employee is inactive." in html


def test_readiness_is_live_fixing_the_employee_fixes_the_batch(logged_in, db):
    e = make_employee(db, "No", "Phone", None, "1234")
    bid = batch_id_of(create(logged_in, [("no_phone.pdf", named_pdf("No", "Phone"))]))
    assert "no phone number" in page(logged_in, bid)
    e.phone = "+26771234567"
    db.commit()
    html = page(logged_in, bid)
    assert "no phone number" not in html and "Ready" in html


def test_two_files_for_one_employee_are_flagged_on_both(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefe.pdf", named_pdf("Thabo", "Molefe")), ("molefe_thabo.pdf", named_pdf("Thabo", "Molefe"))]))
    assert page(logged_in, bid).count("Another file in this batch is matched to the same employee.") == 2


def test_duplicate_file_names_are_flagged(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe")), ("thabo_molefe.PDF", named_pdf("Thabo", "Molefe"))]))
    assert page(logged_in, bid).count("The same file name was uploaded more than once.") == 2


def test_invalid_file_readiness_problem_is_the_reason(logged_in, db, people):
    assert "This is not a real PDF" in page(logged_in, batch_id_of(create(logged_in, [("fake.pdf", b"nope")])))


# ---- ready to send -----------------------------------------------------------------------------------------------

def test_confirm_page_lists_file_employee_and_masked_phone(logged_in, db, people):
    html = confirm_page(logged_in, good_batch(logged_in))
    assert "Thabo_Molefe.pdf" in html and "Thabo Molefe" in html and "***567" in html and "71234567" not in html
    assert "I have checked the matches" in html and "Mark as ready to send" in html
    assert "1 active employee will not get a payslip from this batch." in html and "Not ready yet" not in html


def test_marking_ready_needs_the_tick_and_then_works_and_is_audited(logged_in, db, people):
    bid = good_batch(logged_in)
    post(logged_in, f"/batches/{bid}/ready")                                    # no tick
    assert db.get(Batch, bid).status == "draft" and "tick" in logged_in.get(f"/batches/{bid}/confirm").text
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    db.expire_all()
    batch = db.get(Batch, bid)
    assert batch.status == "ready" and batch.ready_at is not None
    (entry,) = actions(db, "batch_marked_ready")
    assert entry.entity_id == bid and entry.details == "files=3"
    html = confirm_page(logged_in, bid)
    assert "ready to send" in html and "Reopen" in html


def test_employees_without_a_file_are_allowed(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))]))        # 3 others have no file
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    assert db.get(Batch, bid).status == "ready"


@pytest.mark.parametrize("files,expected", [
    ([("quentin_zzyzx.pdf", None)], "still need attention"),                                           # unmatched
    ([("thabo_molefee.pdf", None)], "still need attention"),                                           # needs review
    ([("fake.pdf", b"nope"), ("Thabo_Molefe.pdf", None)], "1 invalid: remove or replace them"),         # an invalid file
])
def test_cannot_be_ready_while_files_need_attention(logged_in, db, people, files, expected):
    bid = batch_id_of(create(logged_in, [(n, d if d is not None else named_pdf("Thabo", "Molefe")) for n, d in files]))
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    assert db.get(Batch, bid).status == "draft"
    html = confirm_page(logged_in, bid)
    assert "Not ready yet" in html and expected in html and "disabled" in html
    assert not actions(db, "batch_marked_ready")


def test_possible_mismatch_blocks_until_confirmed(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefe.pdf", named_pdf("Neo", "Dube"))]))
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    assert db.get(Batch, bid).status == "draft"
    post(logged_in, f"/batches/{bid}/payslips/{payslips(db, bid)[0].id}/confirm")
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    assert db.get(Batch, bid).status == "ready"


@pytest.mark.parametrize("setup", ["no_phone", "no_id", "inactive", "two_files"])
def test_readiness_problems_block_ready(logged_in, db, setup):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", "6789")
    files = [("thabo_molefe.pdf", named_pdf("Thabo", "Molefe"))]
    if setup == "two_files":
        files.append(("molefe_thabo.pdf", named_pdf("Thabo", "Molefe")))
    bid = batch_id_of(create(logged_in, files))
    if setup == "no_phone":
        e.phone = None
    elif setup == "no_id":
        e.id_last4 = None
    elif setup == "inactive":
        e.active = False
    db.commit()
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    assert db.get(Batch, bid).status == "draft"


def test_two_files_problem_clears_when_one_is_removed(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefe.pdf", named_pdf("Thabo", "Molefe")), ("molefe_thabo.pdf", named_pdf("Thabo", "Molefe"))]))
    post(logged_in, f"/batches/{bid}/payslips/{payslips(db, bid)[1].id}/remove")
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    assert db.get(Batch, bid).status == "ready"


def test_an_empty_batch_cannot_be_ready(logged_in, db, people):
    bid = good_batch(logged_in)
    for p in payslips(db, bid):
        post(logged_in, f"/batches/{bid}/payslips/{p.id}/remove")
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    assert db.get(Batch, bid).status == "draft" and "no files yet" in confirm_page(logged_in, bid)


def test_a_ready_batch_is_locked_until_reopened(logged_in, db, people):
    bid = good_batch(logged_in)
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    p = payslips(db, bid)[0]
    post(logged_in, f"/batches/{bid}/payslips/{p.id}/remove")
    add_more(logged_in, bid, [("x.pdf", named_pdf("A", "B"))])
    post(logged_in, f"/batches/{bid}/payslips/{p.id}/match", employee_id=str(people["neo"].id))
    assert len(payslips(db, bid)) == 3 and payslips(db, bid)[0].employee_id != people["neo"].id
    assert "marked ready" in logged_in.get(f"/batches/{bid}").text
    post(logged_in, f"/batches/{bid}/reopen")
    db.expire_all()
    assert db.get(Batch, bid).status == "draft" and db.get(Batch, bid).ready_at is None
    post(logged_in, f"/batches/{bid}/payslips/{p.id}/remove")
    assert len(payslips(db, bid)) == 2 and len(actions(db, "batch_reopened")) == 1


# ---- background processing ---------------------------------------------------------------------------------------

def test_a_batch_that_is_still_matching_shows_progress_and_refuses_changes(logged_in, db, people):
    bid = good_batch(logged_in)
    batch = db.get(Batch, bid)
    batch.processing_state, batch.processing_total, batch.processing_done, batch.processing_heartbeat_at = "processing", 10, 4, utcnow()
    db.commit()
    html = page(logged_in, bid)
    assert "Checking the files" in html and "4 of 10" in html and '<progress class="bar" max="10" value="4"' in html
    assert '<meta http-equiv="refresh"' in html
    post(logged_in, f"/batches/{bid}/payslips/{payslips(db, bid)[0].id}/remove")
    assert len(payslips(db, bid)) == 3 and "Matching is still running" in logged_in.get(f"/batches/{bid}").text
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    assert db.get(Batch, bid).status == "draft"


def test_the_page_does_not_refresh_once_matching_is_done(logged_in, people):
    assert "http-equiv" not in page(logged_in, good_batch(logged_in))


def test_an_interrupted_batch_is_noticed_and_can_be_matched_again(logged_in, db, people):
    bid = good_batch(logged_in)
    batch = db.get(Batch, bid)
    batch.processing_state, batch.processing_heartbeat_at = "processing", utcnow() - timedelta(minutes=10)
    db.commit()
    html = page(logged_in, bid)
    assert "Matching did not finish" in html and "Re-run matching" in html and "http-equiv" not in html
    db.expire_all()
    assert db.get(Batch, bid).processing_state == "failed"
    post(logged_in, f"/batches/{bid}/rematch")
    db.expire_all()
    assert db.get(Batch, bid).processing_state == "done" and all(p.match_status == "exact" for p in payslips(db, bid))


def test_rematch_keeps_choices_made_by_hand(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefee.pdf", named_pdf("Thabo", "Molefe")), ("quentin_zzyzx.pdf", named_pdf("Q", "Z"))]))
    typo = by_name(db, bid)["thabo_molefee.pdf"]
    post(logged_in, f"/batches/{bid}/payslips/{typo.id}/match", employee_id=str(people["neo"].id))
    post(logged_in, f"/batches/{bid}/rematch")
    rows = by_name(db, bid)
    assert (rows["thabo_molefee.pdf"].employee_id, rows["thabo_molefee.pdf"].match_method) == (people["neo"].id, "manual")
    assert rows["quentin_zzyzx.pdf"].match_status == "unmatched"


# ---- privacy and security ----------------------------------------------------------------------------------------

def test_audit_entries_contain_counts_and_ids_only(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("thabo_molefee.pdf", named_pdf("Thabo", "Molefe")), ("fake.pdf", b"x"), ("Neo_Dube.pdf", named_pdf("Neo", "Dube"))]))
    typo = by_name(db, bid)["thabo_molefee.pdf"]
    post(logged_in, f"/batches/{bid}/payslips/{typo.id}/match", employee_id=str(people["thabo"].id))
    post(logged_in, f"/batches/{bid}/payslips/{by_name(db, bid)['fake.pdf'].id}/remove")
    post(logged_in, f"/batches/{bid}/ready", checked="on")
    post(logged_in, f"/batches/{bid}/reopen")
    post(logged_in, f"/batches/{bid}/delete")
    rows = audit_rows(db)
    assert [a.action for a in rows] == ["batch_created", "batch_files_uploaded", "batch_match_changed", "batch_file_removed",
                                        "batch_marked_ready", "batch_reopened", "batch_deleted"]
    for a in rows:
        blob = f"{a.details} {a.entity_type} {a.ip_address}"
        assert not re.search(r"[A-Za-z_]+\.pdf|Thabo|Molefe|Neo|Dube|7123|2677", blob), a.action
        assert a.user_id is not None


def test_logs_never_contain_names_file_names_or_phone_numbers(logged_in, db, people, caplog):
    caplog.set_level(logging.DEBUG)
    bid = batch_id_of(create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe")), ("thabo_molefee.pdf", named_pdf("Thabo", "Molefe")),
                                         ("fake.pdf", b"nope"), ("Secret_Person.pdf", named_pdf("Secret", "Person"))]))
    page(logged_in, bid)
    confirm_page(logged_in, bid)
    p = by_name(db, bid)["thabo_molefee.pdf"]
    post(logged_in, f"/batches/{bid}/payslips/{p.id}/match", employee_id=str(people["neo"].id))
    post(logged_in, f"/batches/{bid}/rematch")
    add_more(logged_in, bid, [("more_files.pdf", named_pdf("More", "Files")), ("broken.pdf", b"%PDF-1.4 broken")])
    for needle in ("Thabo", "Molefe", "molefe", "Secret", "Person", "fake.pdf", "broken", "71234567", "2677", "Neo", "Dube"):
        assert needle not in caplog.text, needle


def test_flash_messages_and_the_session_cookie_carry_no_personal_data(logged_in, db, people):
    create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe")), ("Secret_Person.pdf", named_pdf("Secret", "Person"))])
    cookie = logged_in.cookies.get("payslip_session")
    payload = base64.b64decode(cookie.split(".")[0] + "==").decode("utf-8", "ignore")
    assert "Batch created. 2 files added" in payload
    for needle in ("Thabo", "Molefe", "Secret", "Person", ".pdf"):
        assert needle not in payload


def test_batch_pages_have_no_inline_scripts_styles_or_handlers(logged_in, db, people):
    bid = good_batch(logged_in)
    for html in (page(logged_in, bid), confirm_page(logged_in, bid), logged_in.get("/batches").text, logged_in.get("/batches/new").text):
        assert "<script>" not in html and ' style="' not in html and not re.search(r"\son[a-z]+=", html) and "<style" not in html


def test_file_names_are_escaped_on_the_page(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("<img src=x onerror=alert(1)>.pdf", named_pdf("A", "B"))]))
    html = page(logged_in, bid)
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_stored_pdfs_are_not_reachable_from_the_website(logged_in, db, people):
    bid = good_batch(logged_in)
    stored = payslips(db, bid)[0].stored_filename
    for path in (f"/static/{stored}", f"/static/../private_storage/batches/{bid}/{stored}", f"/private_storage/batches/{bid}/{stored}",
                 f"/batches/{bid}/{stored}"):
        assert logged_in.get(path).status_code == 404
    assert batch_storage.storage_is_private()


def test_upload_limit_for_the_batch_routes_is_the_batch_total_plus_overhead():
    from app.services.body_limit import DEFAULT_LIMIT, limit_for

    big = limit_for("/batches/new", {}, DEFAULT_LIMIT)
    assert big > 200 * 1024 * 1024 and limit_for("/batches/12/upload", {}, DEFAULT_LIMIT) == big
    assert limit_for("/batches/12/delete", {}, DEFAULT_LIMIT) == DEFAULT_LIMIT and limit_for("/batches/new/x", {}, DEFAULT_LIMIT) == DEFAULT_LIMIT


def test_a_locked_pdf_is_never_stored_even_inside_a_good_batch(logged_in, db, people):
    bid = batch_id_of(create(logged_in, [("locked.pdf", locked_pdf("x")), ("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))]))
    assert len(list(batch_storage.batch_dir(bid).iterdir())) == 1
