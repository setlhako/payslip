import re

from sqlalchemy import select

from app.models import AuditLog, Employee
from tests.conftest import csrf_from, make_employee


def names(html: str) -> list[str]:
    return re.findall(r'class="person-name">([^<]+)<', html)


def token(client) -> str:
    """Any page has a CSRF token (the Log out button in the sidebar), so use the dashboard."""
    return csrf_from(client.get("/").text)


def form_post(client, path, **fields):
    return client.post(path, data={"csrf_token": token(client), **fields}, follow_redirects=False)


def audit(db, prefix="employee"):
    return [(a.action, a.entity_id, a.details) for a in db.scalars(select(AuditLog).order_by(AuditLog.id))
            if a.action.startswith(prefix)]


# ---- list: content, hidden IDs, structure ----------------------------------------------------------------------

def test_list_shows_columns_and_hides_ids_by_default(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", "4321", "E1", id_number="987654321")
    html = logged_in.get("/employees").text
    assert "Thabo Molefe" in html and ">E1<" in html and "+26771234567" in html and "Omang" in html
    assert "Password (last 4)" in html and "ID type" in html and "ID number" in html
    assert "987654321" not in html                                    # the full ID is NOT in the page
    assert f'data-id-slot="{e.id}">{"•" * 9}<' in html           # shown as nine dots by default
    assert 'data-secret="4321"' in html                              # the last 4 are kept for the Show IDs button...
    assert html.replace('data-secret="4321"', "").count("4321") == 0  # ...but never printed in the page text
    assert "&bull;&bull;&bull;&bull;" in html                        # what is shown by default
    assert "data-toggle-ids" in html and "Show IDs" in html and "data-reveal-form" in html


def test_people_with_only_the_last_4_show_it_masked_with_a_note(logged_in, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", "4321", "E1")        # saved before full IDs existed
    make_employee(db, "Neo", "Dube")                                          # no ID at all
    html = logged_in.get("/employees").text
    assert "&bull;&bull;&bull;&bull;4321" in html and "full ID not imported yet" in html
    assert html.count("full ID not imported yet") == 1 and "data-id-slot" not in html
    assert "Not set" in html
    assert re.search(r'<script src="/static/app\.js\?v=[0-9a-f]{10}" defer></script>', html)


def test_list_shows_id_type_for_passports_and_not_set(logged_in, db):
    make_employee(db, "Chipo", "Fakename", None, "4567", None, id_type="passport")
    make_employee(db, "Neo", "Dube")
    html = logged_in.get("/employees").text
    assert "Passport" in html and "Not set" in html


def test_table_is_semantic_and_labelled(logged_in, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", "4321")
    html = logged_in.get("/employees").text
    assert "<caption" in html and html.count('scope="col"') >= 6
    assert 'aria-label="Actions for Thabo Molefe"' in html
    assert 'class="skip-link"' in html and '<main id="main"' in html and 'lang="en"' in html
    assert 'aria-sort="ascending"' in html        # name column is the default sort


def test_row_menu_has_edit_and_a_protected_deactivate_form(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe")
    html = logged_in.get("/employees").text
    assert f'href="/employees/{e.id}/edit"' in html
    assert f'action="/employees/{e.id}/deactivate"' in html and html.count('name="csrf_token"') == 5   # logout + the idle dialog's logout + reveal form + bulk form + this row


# ---- list: search, filters, sort, paging -----------------------------------------------------------------------

def test_search_by_name_phone_code_and_accents(logged_in, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", None, "E100")
    make_employee(db, "Zoë", "Dube", "+26772345678", None, "E200")
    make_employee(db, "Neo", "Kgosi")

    def found(q):
        return sorted(names(logged_in.get("/employees", params={"q": q, "status": "all"}).text))

    assert found("molefe") == ["Thabo Molefe"] and found("thabo molefe") == ["Thabo Molefe"]
    assert found("7234") == ["Zoë Dube"] and found("72 345") == ["Zoë Dube"]
    assert found("e100") == ["Thabo Molefe"] and found("zoe") == ["Zoë Dube"]
    assert found("nobody") == [] and found("%") == [] and found("_") == []


def test_status_chips_show_counts_and_filter(logged_in, db):
    make_employee(db, "Act", "Ive")
    make_employee(db, "Gone", "Away", active=False)
    make_employee(db, "Also", "Active")
    lst = lambda s: names(logged_in.get("/employees", params={"status": s}).text)
    assert lst("active") == ["Also Active", "Act Ive"] and lst("inactive") == ["Gone Away"] and len(lst("all")) == 3
    html = logged_in.get("/employees").text
    assert re.search(r'aria-current="true">Active <span class="count">2<', html)
    assert re.search(r'>Inactive <span class="count">1<', html) and re.search(r'>All <span class="count">3<', html)
    assert logged_in.get("/employees", params={"status": "bogus"}).status_code == 200


def test_search_counts_in_chips_follow_the_query(logged_in, db):
    make_employee(db, "Thabo", "Molefe")
    make_employee(db, "Neo", "Molefe", active=False)
    make_employee(db, "Zed", "Other")
    html = logged_in.get("/employees", params={"q": "molefe"}).text
    assert re.search(r'Active <span class="count">1<', html) and re.search(r'All <span class="count">2<', html)


def test_clear_search_button_only_when_searching(logged_in, db):
    make_employee(db, "Thabo", "Molefe")
    assert 'aria-label="Clear search"' not in logged_in.get("/employees").text
    html = logged_in.get("/employees", params={"q": "tha"}).text
    assert 'aria-label="Clear search"' in html and 'href="/employees"' in html


def test_sorting_by_name_and_status(logged_in, db):
    make_employee(db, "Bea", "Alpha", active=False)
    make_employee(db, "Cy", "Beta")
    make_employee(db, "Dee", "Gamma")
    get = lambda **p: names(logged_in.get("/employees", params={"status": "all", **p}).text)
    assert get() == ["Bea Alpha", "Cy Beta", "Dee Gamma"]
    assert get(sort="name", dir="desc") == ["Dee Gamma", "Cy Beta", "Bea Alpha"]
    assert get(sort="status") == ["Cy Beta", "Dee Gamma", "Bea Alpha"]            # active first
    assert get(sort="status", dir="desc") == ["Bea Alpha", "Cy Beta", "Dee Gamma"]  # inactive first
    assert get(sort="bogus", dir="sideways") == get()
    html = logged_in.get("/employees", params={"sort": "name", "dir": "desc"}).text
    assert re.search(r'<th scope="col" aria-sort="none"><a class="sort" href="/employees\?sort=status">Status', html)


def test_sort_header_links_flip_direction(logged_in, db):
    make_employee(db, "Bea", "Alpha")
    asc = logged_in.get("/employees").text
    assert 'href="/employees?dir=desc"' in asc            # clicking Name when ascending asks for descending
    desc = logged_in.get("/employees", params={"dir": "desc"}).text
    assert 'aria-sort="descending"' in desc and re.search(r'<a class="sort" href="/employees">Name', desc)


def test_pagination_counts_and_links(logged_in, db):
    for i in range(30):
        make_employee(db, f"Name{chr(97 + i % 26)}{chr(97 + i // 26)}", "Zed")
    p1 = logged_in.get("/employees").text
    assert "Showing 1&ndash;25 of 30" in p1 and len(names(p1)) == 25
    assert 'aria-label="Next page"' in p1 and 'aria-label="Previous page"' not in p1
    p2 = logged_in.get("/employees", params={"page": 2}).text
    assert "Showing 26&ndash;30 of 30" in p2 and len(names(p2)) == 5
    assert 'aria-label="Previous page"' in p2 and 'aria-current="page"' in p2
    assert logged_in.get("/employees", params={"page": "abc"}).status_code == 200
    assert "Showing 26&ndash;30 of 30" in logged_in.get("/employees", params={"page": 99}).text


def test_per_page_selector(logged_in, db):
    for i in range(30):
        make_employee(db, f"Name{chr(97 + i % 26)}{chr(97 + i // 26)}", "Zed")
    ten = logged_in.get("/employees", params={"per_page": 10}).text
    assert len(names(ten)) == 10 and "Showing 1&ndash;10 of 30" in ten and "Page </span>3<" in ten
    assert '<option value="10" selected>' in ten and "data-autosubmit" in ten
    assert len(names(logged_in.get("/employees", params={"per_page": 100}).text)) == 30
    assert len(names(logged_in.get("/employees", params={"per_page": 7}).text)) == 25    # not allowed -> default


def test_empty_states(logged_in, db):
    html = logged_in.get("/employees").text
    assert "No employees yet" in html and 'href="/employees/new"' in html and 'href="/employees/import"' in html
    make_employee(db, "Thabo", "Molefe")
    html = logged_in.get("/employees", params={"q": "zzz"}).text
    assert "No employees match" in html and "Show all employees" in html
    assert "No employees match" in logged_in.get("/employees", params={"status": "inactive"}).text


# ---- add / edit -------------------------------------------------------------------------------------------------

def test_add_employee_with_an_omang(logged_in, db):
    r = form_post(logged_in, "/employees/new", first_name=" Thabo ", surname="Molefe", phone="71 234 567",
                  id_type="omang", id_number="123456789", employee_code="E1")
    assert r.status_code == 303
    e = db.query(Employee).one()
    assert (e.first_name, e.phone, e.id_type, e.id_last4, e.employee_code, e.name_key, e.active) == (
        "Thabo", "+26771234567", "omang", "6789", "E1", "molefe thabo", True)
    assert audit(db) == [("employee_created", e.id, None)]
    assert "Employee added." in logged_in.get("/employees").text


def test_add_employee_with_a_passport_is_uppercased(logged_in, db):
    form_post(logged_in, "/employees/new", first_name="Chipo", surname="Fakename", id_type="passport", id_number="bp12ab9z")
    e = db.query(Employee).one()
    assert (e.id_type, e.id_last4) == ("passport", "AB9Z")


def test_add_without_an_id_is_fine(logged_in, db):
    assert form_post(logged_in, "/employees/new", first_name="Neo", surname="Dube").status_code == 303
    e = db.query(Employee).one()
    assert e.id_type is None and e.id_last4 is None


def test_add_validation_errors_are_plain_and_attached_to_fields(logged_in, db):
    r = form_post(logged_in, "/employees/new", first_name="", surname="Molefe", phone="12", id_type="omang", id_number="12AB")
    assert r.status_code == 422
    assert "First name is required." in r.text and "Not recognised" in r.text and "digits only" in r.text
    assert 'aria-invalid="true"' in r.text and "Please fix the 3 highlighted fields" in r.text
    assert db.query(Employee).count() == 0


def test_id_rules_in_the_form(logged_in, db):
    cases = [
        (dict(id_type="", id_number="123456789"), "Choose the ID type"),
        (dict(id_type="passport", id_number="AB12"), "5 to 15 letters or digits"),
        (dict(id_type="omang", id_number="12AB34567"), "digits only"),
        (dict(id_type="passport", id_number=""), "Enter the ID number"),
        (dict(id_type="visa", id_number="123456789"), "ID type must be"),
    ]
    for fields, expected in cases:
        r = form_post(logged_in, "/employees/new", first_name="A", surname="One", **fields)
        assert r.status_code == 422 and expected in r.text, fields
    assert db.query(Employee).count() == 0


def test_short_omang_asks_for_confirmation_then_pads(logged_in, db):
    fields = dict(first_name="Dina", surname="Demo", id_type="omang", id_number="123")
    r = form_post(logged_in, "/employees/new", **fields)
    assert r.status_code == 200 and "padded" in r.text and db.query(Employee).count() == 0
    ack = re.search(r'name="ack" value="([^"]+)"', r.text).group(1)
    assert form_post(logged_in, "/employees/new", ack=ack, **fields).status_code == 303
    assert db.query(Employee).one().id_last4 == "0123"


def test_full_id_typed_in_the_form_is_never_echoed_back(logged_in):
    r = form_post(logged_in, "/employees/new", first_name="", surname="X", id_type="omang", id_number="987654321")
    assert "987654321" not in r.text and "4321" not in r.text
    r = form_post(logged_in, "/employees/new", first_name="", surname="X", id_type="passport", id_number="ZX99887766")
    assert "ZX99887766" not in r.text and "7766" not in r.text


def test_duplicate_code_is_an_error(logged_in, db):
    make_employee(db, "Other", "Person", code="E1")
    r = form_post(logged_in, "/employees/new", first_name="New", surname="Person", employee_code="e1")
    assert r.status_code == 422 and "already used by Other Person" in r.text


def test_duplicate_name_warns_then_save_anyway(logged_in, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567")
    fields = dict(first_name="MOLEFE", surname="thabo", phone="71234568")   # a phone number can no longer be shared
    r = form_post(logged_in, "/employees/new", **fields)
    assert r.status_code == 200 and "Another employee has the same name" in r.text
    assert "Save anyway" in r.text and db.query(Employee).count() == 1
    ack = re.search(r'name="ack" value="([^"]+)"', r.text).group(1)
    assert form_post(logged_in, "/employees/new", ack=ack, **fields).status_code == 303 and db.query(Employee).count() == 2


def test_save_anyway_does_not_cover_warnings_that_were_not_shown(logged_in, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567")
    r = form_post(logged_in, "/employees/new", ack="forged", first_name="Thabo", surname="Molefe")
    assert r.status_code == 200 and db.query(Employee).count() == 1


def test_opening_the_edit_form_is_audited_and_shows_the_id_hidden(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", "4321", "E1", id_number="987654321")
    html = logged_in.get(f"/employees/{e.id}/edit").text
    assert audit(db) == [("employee_edit_opened", e.id, None)]            # employee id only
    assert "987654321" not in html and "4321" not in html                 # neither the full ID nor even the last 4
    assert f'data-id-slot="{e.id}"' in html and f'name="employee_id" value="{e.id}"' in html
    assert 'id="id_number" name="id_number" value=""' in html             # the number box is never pre-filled
    assert "Saved: Omang" in html and "Leave empty to keep the saved ID" in html
    assert "last 4 characters" in html and "password that opens this person" in html
    assert '<option value="omang" selected>' in html and "data-toggle-ids" in html
    logged_in.get(f"/employees/{e.id}/edit")
    assert len(audit(db)) == 2                                            # every open is recorded


def test_edit_form_has_an_id_type_dropdown_and_explanation_for_new_people(logged_in, db):
    html = logged_in.get("/employees/new").text
    assert '<select class="select" id="id_type"' in html and '<option value="passport"' in html
    assert "become the password that opens this person" in html and "Saved:" not in html
    assert audit(db) == []                                                # opening the ADD form is not an edit


def test_edit_employee_and_keep_the_id_when_the_box_is_empty(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", "4321", "E1")
    r = form_post(logged_in, f"/employees/{e.id}/edit", first_name="Thabo", surname="Molefe-Smith", phone="72345678",
                  id_type="omang", id_number="", employee_code="E1")
    assert r.status_code == 303
    db.refresh(e)
    assert e.surname == "Molefe-Smith" and e.name_key == "molefe smith thabo" and e.phone == "+26772345678"
    assert (e.id_type, e.id_last4) == ("omang", "4321")
    assert ("employee_edited", e.id, None) in audit(db)


def test_edit_can_switch_to_a_passport_with_a_new_number(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe", None, "4321")
    form_post(logged_in, f"/employees/{e.id}/edit", first_name="Thabo", surname="Molefe", id_type="passport", id_number="ab12cd34")
    db.refresh(e)
    assert (e.id_type, e.id_last4) == ("passport", "CD34")


def test_edit_cannot_change_the_type_without_the_number(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe", None, "4321")
    r = form_post(logged_in, f"/employees/{e.id}/edit", first_name="Thabo", surname="Molefe", id_type="passport", id_number="")
    assert r.status_code == 422 and "enter the ID number again" in r.text
    db.refresh(e)
    assert (e.id_type, e.id_last4) == ("omang", "4321")


def test_editing_own_code_or_name_does_not_trigger_duplicate_warnings(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", "4321", "E1")
    r = form_post(logged_in, f"/employees/{e.id}/edit", first_name="Thabo", surname="Molefe", phone="71234567", employee_code="E1")
    assert r.status_code == 303


def test_deactivate_and_reactivate_are_soft_and_audited(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe")
    assert form_post(logged_in, f"/employees/{e.id}/deactivate").status_code == 303
    db.refresh(e)
    assert e.active is False and db.query(Employee).count() == 1
    assert form_post(logged_in, f"/employees/{e.id}/activate").status_code == 303
    db.refresh(e)
    assert e.active is True
    assert [a[:2] for a in audit(db)] == [("employee_deactivated", e.id), ("employee_activated", e.id)]


def test_actions_need_csrf_token(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe")
    assert logged_in.post(f"/employees/{e.id}/deactivate").status_code == 403
    assert logged_in.post("/employees/new", data={"first_name": "A", "surname": "B"}).status_code == 403
    assert logged_in.post(f"/employees/{e.id}/edit", data={"first_name": "A", "surname": "B"}).status_code == 403
    db.refresh(e)
    assert e.active and db.query(Employee).count() == 1


def test_there_is_no_way_to_hard_delete(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe")
    t = token(logged_in)
    for method, path in (("DELETE", f"/employees/{e.id}"), ("POST", f"/employees/{e.id}/delete"), ("POST", f"/employees/{e.id}")):
        r = logged_in.request(method, path, data={"csrf_token": t}, headers={"X-CSRF-Token": t})
        assert r.status_code in (404, 405), (method, path)
    assert db.query(Employee).count() == 1


def test_unknown_employee_is_404_and_not_audited(logged_in, db):
    assert logged_in.get("/employees/999/edit").status_code == 404
    assert audit(db) == []
