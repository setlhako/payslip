import io
import zipfile

import pytest
from openpyxl import Workbook

from app.models import Employee
from app.services.id_crypto import decrypt_id
from app.services.import_parser import ImportFileError, parse_upload
from app.services.import_planner import ERROR, NEW, UNCHANGED, UPDATE, apply_import, plan_import
from app.services.import_template import build_template
from tests.conftest import make_employee

HEADER = "first_name,surname,phone,omang,employee_code\n"


def csv_bytes(*lines, header=HEADER, encoding="utf-8"):
    return (header + "\n".join(lines) + "\n").encode(encoding)


def parse_csv(*lines, **kw):
    return parse_upload("people.csv", csv_bytes(*lines, **kw), 2000)


def xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def plan_of(db, *lines):
    return plan_import(db, parse_csv(*lines).rows)


# ---- reading files ---------------------------------------------------------------------------------------------

def test_csv_basic_row():
    row = parse_csv("Thabo,Molefe,71234567,123456789,E1").rows[0]
    assert (row.first_name, row.surname, row.phone, row.id_last4, row.employee_code) == (
        "Thabo", "Molefe", "+26771234567", "6789", "E1")
    assert row.row_number == 2 and not row.errors


def test_headers_are_case_and_spacing_tolerant():
    data = csv_bytes("Thabo,Molefe,71234567,1234,E1", header="First Name, SURNAME ,Phone,OMANG,Employee-Code\n")
    row = parse_upload("x.csv", data, 2000).rows[0]
    assert row.first_name == "Thabo" and row.employee_code == "E1"


def test_missing_required_column_is_a_file_error():
    with pytest.raises(ImportFileError, match="first_name"):
        parse_upload("x.csv", b"surname,phone\nMolefe,71234567\n", 2000)


def test_missing_optional_columns_give_notices_and_unknown_columns_are_ignored():
    res = parse_upload("x.csv", b"first_name,surname,national id\nThabo,Molefe,123456789\n", 2000)
    text = " ".join(res.notices)
    assert "phone" in text and "id_number" in text and "national id" in text
    assert res.rows[0].id_last4 is None  # the unknown column was not read as an Omang


def test_csv_cp1252_fallback_and_semicolon_delimiter():
    data = "first_name;surname;phone;omang;employee_code\nZoë;Müller;71234567;1234;\n".encode("cp1252")
    row = parse_upload("x.csv", data, 2000).rows[0]
    assert row.first_name == "Zoë" and row.surname == "Müller"


def test_csv_with_utf8_bom():
    data = b"\xef\xbb\xbf" + csv_bytes("Zoë,Smith,71234567,1234,")
    assert parse_upload("x.csv", data, 2000).rows[0].first_name == "Zoë"


def test_blank_lines_skipped_but_row_numbers_stay_true_to_the_file():
    res = parse_csv("A,One,71234567,1234,", ",,,,", "B,Two,71234568,1234,")
    assert [r.row_number for r in res.rows] == [2, 4]


def test_xlsx_numbers_are_read_as_text_values():
    data = xlsx_bytes([["first_name", "surname", "phone", "omang", "employee_code"],
                       ["Thabo", "Molefe", 71234567, 123, None],
                       ["Neo", "Dube", 71234568.0, 987654321, 55]])
    a, b = parse_upload("x.xlsx", data, 2000).rows
    assert a.phone == "+26771234567" and a.id_last4 == "0123" and a.warnings  # leading zero restored + warned
    assert b.phone == "+26771234568" and b.id_last4 == "4321" and b.employee_code == "55"


def test_xlsx_only_first_sheet_is_read():
    wb = Workbook()
    wb.active.append(["first_name", "surname"])
    wb.active.append(["Thabo", "Molefe"])
    wb.create_sheet("Other").append(["Somebody", "Else"])
    buf = io.BytesIO()
    wb.save(buf)
    assert len(parse_upload("x.xlsx", buf.getvalue(), 2000).rows) == 1


def test_xlsm_and_other_formats_rejected():
    for name in ("x.xlsm", "x.xls", "x.xlsb", "x.txt", "x"):
        with pytest.raises(ImportFileError):
            parse_upload(name, xlsx_bytes([["first_name", "surname"], ["A", "B"]]), 2000)


def test_xlsx_with_macros_rejected_even_if_renamed():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/vbaProject.bin", b"macro")
    with pytest.raises(ImportFileError, match="macros"):
        parse_upload("x.xlsx", buf.getvalue(), 2000)


def test_fake_xlsx_and_empty_and_oversized_files_rejected():
    with pytest.raises(ImportFileError):
        parse_upload("x.xlsx", b"this is not a zip", 2000)
    with pytest.raises(ImportFileError):
        parse_upload("x.csv", b"", 2000)
    with pytest.raises(ImportFileError, match="too large"):
        parse_upload("x.csv", b"a" * 960_000, 2000)
    with pytest.raises(ImportFileError):
        parse_upload("x.csv", b"first_name,surname\n\x00\x00\x00", 2000)


def test_row_limit_enforced():
    lines = [f"Name{chr(65 + i % 26)}{chr(97 + i // 26)},Surname,,," for i in range(6)]
    with pytest.raises(ImportFileError, match="Too many rows"):
        parse_upload("x.csv", csv_bytes(*lines), 5)
    assert len(parse_upload("x.csv", csv_bytes(*lines), 6).rows) == 6


def test_row_validation_messages():
    rows = parse_csv(",Molefe,71234567,1234,", "Thabo,,71234567,1234,", "Thabo,Molefe,12,1234,",
                     "Thabo,Molefe,71234567,12#4,", "123,456,71234567,1234,").rows
    assert all(r.errors for r in rows)
    assert "First name" in rows[0].errors[0] and "Surname" in rows[1].errors[0]
    assert any("Phone" in e for e in rows[2].errors) and any("ID" in e for e in rows[3].errors)
    assert "letters" in rows[4].errors[0]


def test_template_example_rows_are_rejected_if_left_in():
    rows = parse_upload("t.xlsx", build_template(), 2000).rows
    assert len(rows) == 2 and all(any("example row" in e for e in r.errors) for r in rows)


def test_template_has_headers_dropdown_and_text_formatting():
    from openpyxl import load_workbook

    ws = load_workbook(io.BytesIO(build_template())).worksheets[0]
    assert [c.value for c in ws[1]] == ["first_name", "surname", "phone", "id_number", "id_type", "employee_code"]
    assert [c.value for c in ws[2]][4] == "omang" and [c.value for c in ws[3]][4] == "passport"
    for letter in "CDEF":
        assert ws.column_dimensions[letter].number_format == "@"
        assert ws[f"{letter}2"].number_format == "@" and ws[f"{letter}3"].number_format == "@"
    assert any("omang,passport" in dv.formula1 for dv in ws.data_validations.dataValidation)


# ---- planning (upsert rules) -----------------------------------------------------------------------------------

def test_new_employee(db):
    (p,) = plan_of(db, "Thabo,Molefe,71234567,1234,")
    assert p.action == NEW and not p.errors


def test_upsert_by_employee_code_updates_phone_and_name(db):
    e = make_employee(db, "Thabo", "Molefe", "+26771111111", "1234", "E1")
    (p,) = plan_of(db, "Thabo,Molefe-Smith,72222222,1234,E1")
    assert p.action == UPDATE and p.employee_id == e.id
    assert any("Surname changes" in c for c in p.changes) and any("Phone changes" in c for c in p.changes)


def test_upsert_by_code_is_case_insensitive(db):
    make_employee(db, "Thabo", "Molefe", "+26771111111", "1234", "ab-1", id_number="123451234")
    (p,) = plan_of(db, "Thabo,Molefe,71111111,123451234,AB-1")
    assert p.action == UNCHANGED


def test_upsert_by_name_when_no_code_ignores_order_case_accents(db):
    e = make_employee(db, "Zoë", "Molefe", "+26771111111", "1234")
    (p,) = plan_of(db, "MOLEFE,zoe,72222222,,")
    assert p.action == UPDATE and p.employee_id == e.id and "Phone changes" in p.changes[0]


def test_unchanged_when_nothing_differs(db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", id_number="123456789")
    (p,) = plan_of(db, "Thabo,Molefe,71234567,123456789,")
    assert p.action == UNCHANGED and not p.changes


def test_same_last_4_but_no_stored_full_id_is_an_update_that_fills_it_in(db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", "6789")        # only the last 4 were saved before
    (p,) = plan_of(db, "Thabo,Molefe,71234567,123456789,")
    assert p.action == UPDATE and p.changes == ["The full ID number will be stored (it was not imported before)."]
    apply_import(db, [p])
    db.commit()
    assert decrypt_id(e.id_number_encrypted) == "123456789" and e.id_last4 == "6789"


def test_a_row_without_an_id_never_erases_the_stored_full_id(db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", id_number="123456789")
    before = e.id_number_encrypted
    apply_import(db, plan_of(db, "Thabo,Molefe,72222222,,"))
    db.commit()
    assert e.id_number_encrypted == before and e.id_last4 == "6789"


def test_importing_a_new_full_id_replaces_the_stored_one(db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", id_number="123456789")
    (p,) = plan_of(db, "Thabo,Molefe,71234567,555556789,")
    assert p.action == UPDATE and p.changes == ["The full ID number will change."]
    apply_import(db, [p])
    db.commit()
    assert decrypt_id(e.id_number_encrypted) == "555556789" and e.id_last4 == "6789"


def test_blank_phone_and_omang_never_erase_saved_values(db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", "6789")
    planned = plan_of(db, "Thabo,Molefe,,,")
    assert planned[0].action == UNCHANGED
    apply_import(db, planned)
    db.commit()
    db.refresh(e)
    assert e.phone == "+26771234567" and e.id_last4 == "6789"


def test_name_matching_two_existing_employees_is_an_error(db):
    make_employee(db, "Thabo", "Molefe", code="E1")
    make_employee(db, "Thabo", "Molefe", code="E2")
    (p,) = plan_of(db, "Thabo,Molefe,71234567,1234,")
    assert p.action == ERROR and "employee_code" in p.errors[0]


def test_same_name_twice_in_file_without_codes_marks_both_rows_as_errors(db):
    a, b = plan_of(db, "Hope,Duplicate,71234567,1234,", "hope,DUPLICATE,71234568,1234,")
    assert a.action == b.action == ERROR
    assert "row 3" in a.errors[0] and "row 2" in b.errors[0] and "employee_code" in a.errors[0]


def test_same_name_in_file_is_fine_when_codes_tell_them_apart(db):
    a, b = plan_of(db, "Hope,Same,71234567,1234,C1", "Hope,Same,71234568,1234,C2")
    assert a.action == b.action == NEW


def test_duplicate_employee_code_in_file_errors_both(db):
    a, b = plan_of(db, "A,One,71234567,1234,X1", "B,Two,71234568,1234,x1")
    assert a.action == b.action == ERROR and "unique" in a.errors[0]


def test_two_rows_updating_the_same_employee_error(db):
    make_employee(db, "Thabo", "Molefe", code="E1")
    a, b = plan_of(db, "Thabo,Molefe,71234567,1234,E1", "Thabo,Molefe,71234568,1234,")
    assert a.action == b.action == ERROR


def test_new_code_with_existing_same_name_creates_new_but_warns(db):
    make_employee(db, "Thabo", "Molefe", "+26771111111", "1234")
    (p,) = plan_of(db, "Thabo,Molefe,72222222,1234,E9")
    assert p.action == NEW and any("already exists" in w for w in p.warnings)


def test_warnings_for_missing_phone_omang_and_shared_phone(db):
    make_employee(db, "Other", "Person", "+26771234567", "1111")
    a, b = plan_of(db, "New,Person,71234567,,", "No,Phone,,,")
    assert a.action == NEW and any("Same phone number as existing employee" in w for w in a.warnings)
    assert any("No ID number" in w for w in a.warnings) and any("No phone" in w for w in b.warnings)


def test_inactive_match_is_updated_but_warned_and_not_reactivated(db):
    e = make_employee(db, "Thabo", "Molefe", "+26771111111", "1234", active=False)
    planned = plan_of(db, "Thabo,Molefe,72222222,1234,")
    assert planned[0].action == UPDATE and any("inactive" in w for w in planned[0].warnings)
    apply_import(db, planned)
    db.commit()
    db.refresh(e)
    assert e.phone == "+26772222222" and e.active is False


def test_rows_with_errors_are_skipped_and_valid_rows_saved(db):
    planned = plan_of(db, "Good,Person,71234567,1234,", "Bad,Person,12,1234,", "Also,Good,71234568,12#4,")
    counts = apply_import(db, planned)
    db.commit()
    assert counts["new"] == 1 and counts["errors"] == 2
    assert [e.full_name for e in db.query(Employee)] == ["Good Person"]


def test_applied_employee_has_normalised_name_key_and_last4_only(db):
    apply_import(db, plan_of(db, "Zoë,O'Brien,71234567,987654321,"))
    db.commit()
    e = db.query(Employee).one()
    assert e.name_key == "obrien zoe" and e.id_last4 == "4321" and e.id_type == "omang" and e.active is True


# ---- passports, id_type and the old "omang" header ---------------------------------------------------------------

NEW_HEADER = "first_name,surname,phone,id_number,id_type,employee_code\n"


def parse_new(*lines):
    return parse_upload("x.csv", csv_bytes(*lines, header=NEW_HEADER), 2000)


def test_passport_row_keeps_last_four_characters_uppercase():
    row = parse_new("Chipo,Fakename,71000003,ab1234567,passport,").rows[0]
    assert row.id_type == "passport" and row.id_last4 == "4567" and not row.errors and not row.warnings
    row = parse_new("Chipo,Fakename,71000003,bp12ab9z,PASSPORT,").rows[0]
    assert row.id_last4 == "AB9Z"            # upper-cased
    assert row.id_last4 == row.id_last4.upper()


def test_blank_id_type_is_guessed_with_a_warning():
    omang, passport = parse_new("A,One,71000001,999900011,,", "B,Two,71000002,zz99887766,,").rows
    assert (omang.id_type, omang.id_last4) == ("omang", "0011")
    assert (passport.id_type, passport.id_last4) == ("passport", "7766")
    assert any("treated as an Omang" in w for w in omang.warnings)
    assert any("treated as a passport" in w for w in passport.warnings)
    assert not omang.errors and not passport.errors


def test_explicit_type_gives_no_guess_warning():
    row = parse_new("A,One,71000001,999900011,omang,").rows[0]
    assert not row.warnings


def test_passport_too_short_and_omang_with_letters_are_errors():
    short, letters, bad_type = parse_new(
        "A,One,71000001,X1,passport,", "B,Two,71000002,12AB34567,omang,", "C,Three,71000003,12345678,visa,").rows
    assert any("5 to 15" in e for e in short.errors)
    assert any("digits only" in e for e in letters.errors)
    assert any("'omang' or 'passport'" in e for e in bad_type.errors)


def test_short_omang_is_still_padded_with_a_warning():
    row = parse_new("A,One,71000001,42,omang,").rows[0]
    assert row.id_last4 == "0042" and any("padded" in w for w in row.warnings)


def test_spaces_and_dashes_are_ignored_in_the_id_number():
    assert parse_new("A,One,71000001,123-456 789,omang,").rows[0].id_last4 == "6789"
    assert parse_new("A,One,71000001,ab-12 34 cd,passport,").rows[0].id_last4 == "34CD"


def test_old_omang_header_still_works_as_id_number():
    row = parse_upload("x.csv", csv_bytes("A,One,71000001,123456789,", header="first_name,surname,phone,omang,employee_code\n"), 2000).rows[0]
    assert row.id_last4 == "6789" and row.id_type == "omang"


def test_type_without_number_is_warned_and_ignored():
    row = parse_new("A,One,71000001,,passport,").rows[0]
    assert row.id_last4 is None and row.id_type is None and any("left unchanged" in w for w in row.warnings)


def test_planner_reports_id_changes_without_revealing_digits(db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", "4321", "E1")
    (p,) = plan_import(db, parse_new("Thabo,Molefe,71234567,pa99887766,passport,E1").rows)
    assert p.action == UPDATE
    assert any("last 4 characters" in c and "will change" in c for c in p.changes)
    assert "ID type changes from omang to passport." in p.changes
    assert "7766" not in " ".join(p.changes + p.warnings)


def test_applying_a_passport_sets_type_and_uppercase_last4(db):
    apply_import(db, plan_import(db, parse_new("Chipo,Fakename,71000003,ab1234567,passport,").rows))
    db.commit()
    e = db.query(Employee).one()
    assert (e.id_type, e.id_last4) == ("passport", "4567")
