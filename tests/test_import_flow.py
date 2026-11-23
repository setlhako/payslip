"""The 3-step import flow (Upload -> Review -> Done) and the 'full ID numbers are only ever stored encrypted' guarantees."""
import logging
import re
import tempfile

from sqlalchemy import String, Text, select, text

from app.database import Base
from app.models import AuditLog, Employee
from app.services import import_preview
from app.services.id_crypto import decrypt_id
from app.services.import_preview import store
from tests.conftest import csrf_from, make_employee

FULL_OMANG = "987654321"
FULL_PASSPORT = "BP12a5678"
CSV = (
    "first_name,surname,phone,id_number,id_type,employee_code\n"
    f"Thabo,Molefe,71234567,{FULL_OMANG},omang,E1\n"
    f"Neo,Dube,26772345678,{FULL_PASSPORT},passport,\n"
    "Bad,Phone,12,123456783,omang,\n"
)


def upload(client, data=CSV, name="people.csv"):
    token = csrf_from(client.get("/employees/import").text)
    raw = data.encode() if isinstance(data, str) else data
    return client.post("/employees/import", files={"file": (name, raw, "application/octet-stream")},
                       data={"csrf_token": token}, follow_redirects=False)


def post(client, path):
    return client.post(path, data={"csrf_token": csrf_from(client.get("/").text)}, follow_redirects=False)


# ---- the flow --------------------------------------------------------------------------------------------------

def test_upload_page_is_step_one(logged_in):
    html = logged_in.get("/employees/import").text
    assert re.search(r'<li class="is-current" aria-current="step">\s*<span class="step-marker">1</span>\s*<span class="step-label">Upload', html)
    assert "Download import template" in html and "id_type" in html and "never saved on the server" in html


def test_upload_shows_preview_and_saves_nothing_yet(logged_in, db):
    r = upload(logged_in)
    assert r.status_code == 303 and r.headers["location"] == "/employees/import/preview"
    page = logged_in.get("/employees/import/preview").text
    assert "Nothing has been saved yet" in page and "Thabo Molefe" in page and "Neo Dube" in page
    assert re.search(r'<li class="is-current" aria-current="step">\s*<span class="step-marker">2</span>\s*<span class="step-label">Review', page)
    assert "badge-new" in page and "badge-error" in page and "Row" in page
    assert db.query(Employee).count() == 0


def test_summary_bar_counts_and_problem_rows_come_first(logged_in):
    upload(logged_in)
    page = logged_in.get("/employees/import/preview").text
    counts = re.findall(r'<div class="summary-count [^"]+">(\d+)</div><div class="summary-label">([^<]+)<', page)
    assert counts == [("2", "New"), ("0", "Updated"), ("0", "Unchanged"), ("0", "With warnings"), ("1", "Errors (skipped)")]
    # the file lists the bad row LAST, the preview lists it FIRST
    assert page.index("Bad Phone") < page.index("Thabo Molefe") < page.index("Neo Dube")
    assert "1 row will be skipped" in page


def test_blank_id_type_shows_a_warning_badge_and_message(logged_in):
    upload(logged_in, "first_name,surname,phone,id_number,id_type\nAnna,Test,71000001,999900011,\nBen,Pass,71000002,zz99887766,\n")
    page = logged_in.get("/employees/import/preview").text
    assert page.count("badge-warning") >= 2
    assert "treated as an Omang" in page and "treated as a passport" in page
    assert "0011" not in page and "7766" not in page                      # no ID characters in the preview


def test_confirm_saves_valid_rows_only_audits_counts_and_shows_the_done_step(logged_in, db):
    upload(logged_in)
    r = post(logged_in, "/employees/import/confirm")
    assert r.status_code == 303 and r.headers["location"] == "/employees/import/done"
    assert sorted(e.full_name for e in db.query(Employee)) == ["Neo Dube", "Thabo Molefe"]    # bad row skipped
    thabo = db.scalars(select(Employee).where(Employee.first_name == "Thabo")).one()
    neo = db.scalars(select(Employee).where(Employee.first_name == "Neo")).one()
    assert (thabo.id_type, thabo.id_last4, thabo.phone, thabo.employee_code) == ("omang", "4321", "+26771234567", "E1")
    assert (neo.id_type, neo.id_last4) == ("passport", "5678")                                 # upper-cased last 4
    assert decrypt_id(thabo.id_number_encrypted) == FULL_OMANG                                 # the full ID is kept, encrypted
    assert decrypt_id(neo.id_number_encrypted) == FULL_PASSPORT.upper()
    assert FULL_OMANG not in thabo.id_number_encrypted and FULL_PASSPORT.upper() not in neo.id_number_encrypted

    log = db.scalars(select(AuditLog).where(AuditLog.action == "employees_import_confirmed")).one()
    assert log.details == "new=2 updated=0 unchanged=0 skipped_errors=1 with_warnings=0"

    done = logged_in.get("/employees/import/done").text
    assert "2 employees saved" in done and "2 new, 0 updated and 0 unchanged" in done and "1 row was skipped" in done
    assert done.count("is-done") == 3 and "Import another file" in done and 'href="/employees"' in done
    assert logged_in.get("/employees/import/done", follow_redirects=False).status_code == 303   # shown once


def test_preview_can_only_be_confirmed_once(logged_in, db):
    upload(logged_in)
    assert post(logged_in, "/employees/import/confirm").headers["location"] == "/employees/import/done"
    again = post(logged_in, "/employees/import/confirm")
    assert again.headers["location"] == "/employees/import" and db.query(Employee).count() == 2
    assert logged_in.get("/employees/import/preview", follow_redirects=False).status_code == 303


def test_cancel_clears_the_preview(logged_in, db):
    upload(logged_in)
    assert post(logged_in, "/employees/import/cancel").status_code == 303
    assert db.query(Employee).count() == 0 and store._items == {}
    assert logged_in.get("/employees/import/preview", follow_redirects=False).status_code == 303
    assert "Import cancelled" in logged_in.get("/employees").text


def test_preview_expires(logged_in, monkeypatch):
    upload(logged_in)
    assert logged_in.get("/employees/import/preview", follow_redirects=False).status_code == 200
    later = import_preview._now() + 16 * 60
    monkeypatch.setattr(import_preview, "_now", lambda: later)
    r = logged_in.get("/employees/import/preview", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/employees/import"
    assert "expired" in logged_in.get("/employees/import").text


def test_preview_is_tied_to_the_user_and_cleared_on_logout(logged_in, admin):
    upload(logged_in)
    (token,) = store._items.keys()
    assert store.get(token, admin.id) is not None and store.get(token, admin.id + 1) is None
    post(logged_in, "/logout")
    assert store._items == {}


def test_new_upload_replaces_the_previous_preview(logged_in):
    upload(logged_in)
    upload(logged_in, "first_name,surname\nOnly,One\n")
    assert len(store._items) == 1 and "Only One" in logged_in.get("/employees/import/preview").text


def test_updated_and_unchanged_rows_are_labelled(logged_in, db):
    make_employee(db, "Thabo", "Molefe", "+26771111111", code="E1", id_number=FULL_OMANG)
    make_employee(db, "Neo", "Dube", "+26772345678", id_number=FULL_PASSPORT, id_type="passport")
    upload(logged_in)
    page = logged_in.get("/employees/import/preview").text
    assert "badge-update" in page and "Phone changes from +26771111111 to +26771234567" in page
    assert "badge-unchanged" in page


def test_reimporting_fills_in_the_full_id_for_people_who_only_had_the_last_4(logged_in, db):
    old = make_employee(db, "Thabo", "Molefe", "+26771234567", "4321", "E1")        # saved before full IDs existed
    assert old.id_number_encrypted is None
    upload(logged_in)
    page = logged_in.get("/employees/import/preview").text
    assert "The full ID number will be stored" in page and "badge-update" in page
    assert FULL_OMANG not in page and "4321" not in page
    post(logged_in, "/employees/import/confirm")
    db.refresh(old)
    assert decrypt_id(old.id_number_encrypted) == FULL_OMANG and old.id_last4 == "4321"
    # importing the same file again changes nothing for him
    upload(logged_in)
    assert "The full ID number will" not in logged_in.get("/employees/import/preview").text.split("Neo Dube")[0].split("Thabo Molefe")[1]


def test_a_different_full_id_with_the_same_last_4_is_reported_as_a_change(logged_in, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", code="E1", id_number="111114321")
    upload(logged_in)
    assert "The full ID number will change" in logged_in.get("/employees/import/preview").text


def test_show_filters_on_the_preview(logged_in):
    upload(logged_in)
    problems = logged_in.get("/employees/import/preview", params={"show": "problems"}).text
    assert "Bad Phone" in problems and "Thabo Molefe" not in problems
    changes = logged_in.get("/employees/import/preview", params={"show": "changes"}).text
    assert "Thabo Molefe" in changes and "Bad Phone" not in changes


def test_nothing_to_save_disables_the_button(logged_in):
    upload(logged_in, "first_name,surname,phone\nBad,Phone,12\n")
    page = logged_in.get("/employees/import/preview").text
    assert "Nothing to save" in page and "disabled" in page and "/employees/import/confirm" not in page


def test_import_failure_rolls_everything_back(logged_in, db, monkeypatch):
    upload(logged_in)

    def boom(*a, **k):
        raise RuntimeError("simulated failure just before commit")

    monkeypatch.setattr("app.routers.employees.audit.record", boom)
    r = post(logged_in, "/employees/import/confirm")
    assert r.status_code == 303 and r.headers["location"] == "/employees/import"
    assert db.query(Employee).count() == 0
    assert "nothing was changed" in logged_in.get("/employees/import").text


def test_upload_errors_are_shown_on_the_upload_page(logged_in):
    r = upload(logged_in, b"not,the,right\ncolumns,at,all\n")
    assert r.status_code == 400 and "Missing required column" in r.text
    r = upload(logged_in, b"fake", "evil.xlsm")
    assert r.status_code == 400 and "Macro" in r.text


def test_import_requires_csrf_token(logged_in):
    r = logged_in.post("/employees/import", files={"file": ("a.csv", b"first_name,surname\nA,B\n")})
    assert r.status_code == 403
    assert logged_in.post("/employees/import/confirm").status_code == 403
    assert logged_in.post("/employees/import/cancel").status_code == 403


def test_template_download(logged_in):
    r = logged_in.get("/employees/import/template")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and r.content.startswith(b"PK")


# ---- uploads never touch the disk / big uploads are refused -----------------------------------------------------

def test_upload_is_never_written_to_a_temp_file(logged_in, monkeypatch):
    def boom(self):
        raise AssertionError("the upload was spilled to a temp file on disk")

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", boom)
    probe = tempfile.SpooledTemporaryFile(max_size=5)
    try:
        probe.write(b"x" * 20)
        raise AssertionError("test is not effective: rollover patch did not trigger")
    except AssertionError as exc:
        assert "spilled" in str(exc)
    # a file just under the size limit must still stay in memory
    # (the csv module caps one field at ~131k characters, so spread the bulk over several ignored columns)
    junk = [f"junk{i}" for i in range(9)]
    big = ("first_name,surname," + ",".join(junk) + "\nTina,Turner," + ",".join("j" * 100_000 for _ in junk) + "\n").encode()
    assert 900_000 < len(big) < 950_000
    assert upload(logged_in, big).status_code == 303


def test_oversized_uploads_are_refused(logged_in):
    assert upload(logged_in, b"a" * 1_200_000).status_code == 413
    r = upload(logged_in, b"first_name,surname\n" + b"a" * 960_000)
    assert r.status_code == 400 and "too large" in r.text


# ---- the full ID number is never stored or logged ---------------------------------------------------------------

def test_full_id_numbers_never_reach_database_logs_memory_or_pages(logged_in, db, caplog):
    caplog.set_level(logging.DEBUG)
    upload(logged_in)
    preview_html = logged_in.get("/employees/import/preview").text
    in_memory = repr(store._items)
    post(logged_in, "/employees/import/confirm")
    done_html = logged_in.get("/employees/import/done").text
    list_html = logged_in.get("/employees", params={"status": "all"}).text

    secrets = (FULL_OMANG, FULL_PASSPORT, FULL_PASSPORT.upper(), "98765", "BP12", "bp12")
    # 1. every text column of every table in the database (the encrypted column is checked on its own below:
    #    short strings like "BP12" can appear by pure chance inside random base64 text)
    leaked = []
    for table in Base.metadata.sorted_tables:
        for col in [c.name for c in table.columns if isinstance(c.type, (String, Text)) and c.name != "id_number_encrypted"]:
            for (value,) in db.execute(text(f'SELECT "{col}" FROM "{table.name}"')):
                if value and any(s in str(value) for s in secrets):
                    leaked.append((table.name, col))
    assert leaked == []
    # 2. logs, the preview held in memory, and every page shown
    for where, blob in (("logs", caplog.text), ("memory", in_memory), ("preview", preview_html),
                        ("done page", done_html), ("list", list_html.replace('data-secret="4321"', "").replace('data-secret="5678"', ""))):
        assert not any(s in blob for s in secrets), where
    # 3. the parsed rows held for the preview carry only the last 4 characters and the ENCRYPTED full number
    for entry in store._items.values():
        for row in entry.result.rows:
            assert not any(s in str(v) for v in vars(row).values() for s in secrets)
    assert "4321" in in_memory and "5678" in in_memory
    assert db.scalars(select(Employee.id_last4).where(Employee.first_name == "Neo")).one() == "5678"
    # 4. in the database the full number exists ONLY as encrypted text
    stored = db.scalars(select(Employee.id_number_encrypted).where(Employee.id_number_encrypted.is_not(None))).all()
    assert len(stored) == 2 and not any(FULL_OMANG in v or FULL_PASSPORT.upper() in v for v in stored)
    assert sorted(decrypt_id(v) for v in stored) == sorted([FULL_OMANG, FULL_PASSPORT.upper()])


def test_session_cookie_does_not_carry_ids_or_names(logged_in):
    upload(logged_in)
    cookie = logged_in.cookies.get("payslip_session")
    assert FULL_OMANG not in cookie and "Molefe" not in cookie and FULL_PASSPORT not in cookie
