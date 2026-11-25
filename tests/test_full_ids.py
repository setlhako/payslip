"""Full ID numbers: stored ONLY encrypted, hidden by default, revealed through ONE audited, rate-limited endpoint."""
import io
import logging
import re

import pytest
from cryptography.fernet import Fernet
from openpyxl import load_workbook
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings, get_settings
from app.models import AuditLog, Employee
from app.scripts import generate_encryption_key
from app.scripts.reencrypt_ids import reencrypt_all
from app.services import id_reveal
from app.services.id_crypto import IdCipher, IdCryptoError, decrypt_id, encrypt_id, key_is_valid
from tests.conftest import csrf_from, make_employee
from tests.test_auth import _api_routes

OMANG = "987654321"
PASSPORT = "AB12CD34"            # stored upper-case; password CD34
ALL_IDS = (OMANG, PASSPORT, "ab12cd34")


def csrf(client):
    return csrf_from(client.get("/").text)


def reveal(client, **fields):
    return client.post("/employees/reveal-ids", data={"csrf_token": csrf(client), **fields})


def audit_rows(db, action="ids_revealed"):
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


# ---- encryption --------------------------------------------------------------------------------------------------

def test_encryption_round_trip():
    token = encrypt_id(OMANG)
    assert token != OMANG and OMANG not in token and "98765" not in token
    assert decrypt_id(token) == OMANG
    assert encrypt_id(OMANG) != token                      # a fresh random IV each time: equal IDs don't look equal
    assert decrypt_id(encrypt_id(PASSPORT)) == PASSPORT


def test_wrong_key_fails_safely_with_a_message_that_leaks_nothing():
    key_a, key_b = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = IdCipher(key_a).encrypt(OMANG)
    with pytest.raises(IdCryptoError) as caught:
        IdCipher(key_b).decrypt(token)
    message = str(caught.value)
    assert OMANG not in message and key_a not in message and key_b not in message and token not in message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("bad", ["", "not-a-token", "gAAAAAB" + "x" * 80])
def test_garbage_or_tampered_values_fail_safely(bad):
    with pytest.raises(IdCryptoError):
        decrypt_id(bad)
    tampered = encrypt_id(OMANG)[:-4] + "AAAA"
    with pytest.raises(IdCryptoError):
        decrypt_id(tampered)                               # authenticated: a changed value is rejected, not misread


@pytest.mark.parametrize("bad_key", ["", "short", "x" * 44, " "])
def test_missing_or_invalid_key_is_refused(bad_key):
    assert not key_is_valid(bad_key)
    with pytest.raises(IdCryptoError):
        IdCipher(bad_key)


def test_the_app_will_not_start_without_a_valid_key(monkeypatch):
    base = dict(_env_file=None, session_secret="s" * 40, database_url="sqlite://")
    monkeypatch.delenv("ID_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError) as missing:
        Settings(**base)
    assert "ID_ENCRYPTION_KEY" in str(missing.value) or "id_encryption_key" in str(missing.value)
    with pytest.raises(ValidationError) as invalid:
        Settings(**base, id_encryption_key="not-a-key-" + "q" * 30)
    assert "q" * 30 not in str(invalid.value)              # the bad value is not echoed
    with pytest.raises(ValidationError):
        Settings(**base, id_encryption_key=Fernet.generate_key().decode(), id_encryption_key_previous="garbage")


def test_settings_never_print_the_key():
    assert get_settings().id_encryption_key.get_secret_value() not in repr(get_settings())


def test_key_rotation_old_key_decrypts_new_key_encrypts():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = IdCipher(old).encrypt(OMANG)
    with pytest.raises(IdCryptoError):
        IdCipher(new).decrypt(token)                       # new key alone cannot read old data...
    both = IdCipher(new, [old])
    assert both.decrypt(token) == OMANG                    # ...but with the old key kept as "previous" it can
    rotated = both.rotate(token)
    assert IdCipher(new).decrypt(rotated) == OMANG         # after re-encrypting, the new key alone is enough
    with pytest.raises(IdCryptoError):
        IdCipher(old).decrypt(rotated)


def test_reencrypt_all_moves_every_stored_id_to_the_new_key(db):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    a = make_employee(db, "Thabo", "Molefe", id_last4="6789")
    b = make_employee(db, "Neo", "Dube", id_last4="CD34", id_type="passport")
    make_employee(db, "Kea", "Nophone")                    # no full ID: skipped
    a.id_number_encrypted, b.id_number_encrypted = IdCipher(old).encrypt(OMANG), IdCipher(old).encrypt(PASSPORT)
    db.commit()
    done, failed = reencrypt_all(db, IdCipher(new, [old]))
    assert (done, failed) == (2, [])
    db.refresh(a), db.refresh(b)
    assert IdCipher(new).decrypt(a.id_number_encrypted) == OMANG and IdCipher(new).decrypt(b.id_number_encrypted) == PASSPORT
    (entry,) = audit_rows(db, "id_keys_rotated")
    assert entry.details == "reencrypted=2 failed=0"


def test_reencrypt_all_reports_values_no_key_can_read_and_keeps_them(db):
    e = make_employee(db, "Thabo", "Molefe", id_last4="6789")
    e.id_number_encrypted = IdCipher(Fernet.generate_key().decode()).encrypt(OMANG)   # made with a key we no longer have
    db.commit()
    before = e.id_number_encrypted
    done, failed = reencrypt_all(db, IdCipher(Fernet.generate_key().decode()))
    assert (done, failed) == (0, [e.id]) and e.id_number_encrypted == before


# ---- the key helper script ---------------------------------------------------------------------------------------

@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("APP_ENV=development\nID_ENCRYPTION_KEY=\nID_ENCRYPTION_KEY_PREVIOUS=\nOTHER=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(generate_encryption_key, "PROJECT_ROOT", tmp_path)
    return path


def env_value(path, name):
    return re.search(rf"^{name}=(.*)$", path.read_text(encoding="utf-8"), re.M).group(1)


def test_generate_key_writes_a_valid_key_into_env_without_printing_it(env_file, capsys):
    assert generate_encryption_key.main([]) == 0
    key = env_value(env_file, "ID_ENCRYPTION_KEY")
    assert key_is_valid(key) and "OTHER=keep-me" in env_file.read_text(encoding="utf-8")
    out = capsys.readouterr()
    assert key not in out.out + out.err


def test_generate_key_never_replaces_an_existing_key(env_file, capsys):
    generate_encryption_key.main([])
    key = env_value(env_file, "ID_ENCRYPTION_KEY")
    assert generate_encryption_key.main([]) == 0 and env_value(env_file, "ID_ENCRYPTION_KEY") == key
    assert "already set" in capsys.readouterr().out


def test_rotate_keeps_the_old_key_as_previous_and_never_prints_keys(env_file, capsys):
    generate_encryption_key.main([])
    first = env_value(env_file, "ID_ENCRYPTION_KEY")
    capsys.readouterr()
    assert generate_encryption_key.main(["--rotate"]) == 0
    second = env_value(env_file, "ID_ENCRYPTION_KEY")
    assert second != first and key_is_valid(second) and env_value(env_file, "ID_ENCRYPTION_KEY_PREVIOUS") == first
    assert generate_encryption_key.main(["--rotate"]) == 0
    assert env_value(env_file, "ID_ENCRYPTION_KEY_PREVIOUS").split(",") == [second, first]   # newest old key first
    out = capsys.readouterr()
    assert not any(k in out.out + out.err for k in (first, second, env_value(env_file, "ID_ENCRYPTION_KEY")))


def test_rotate_without_a_key_is_refused(env_file):
    assert generate_encryption_key.main(["--rotate"]) == 1


# ---- storing: import and forms -----------------------------------------------------------------------------------

def test_import_stores_an_encrypted_full_id_and_derives_last_4(logged_in, db):
    csv = ("first_name,surname,phone,id_number,id_type,employee_code\n"
           "Thabo,Molefe,71234567,123 456-789,omang,E1\n"
           "Chipo,Fakename,71234568,ab12cd34,passport,E2\n")
    token = csrf(logged_in)
    logged_in.post("/employees/import", files={"file": ("p.csv", csv.encode())}, data={"csrf_token": token})
    logged_in.post("/employees/import/confirm", data={"csrf_token": csrf(logged_in)})
    thabo = db.scalars(select(Employee).where(Employee.first_name == "Thabo")).one()
    chipo = db.scalars(select(Employee).where(Employee.first_name == "Chipo")).one()
    assert (thabo.id_type, thabo.id_last4, decrypt_id(thabo.id_number_encrypted)) == ("omang", "6789", "123456789")
    assert (chipo.id_type, chipo.id_last4, decrypt_id(chipo.id_number_encrypted)) == ("passport", "CD34", PASSPORT)
    assert "123456789" not in thabo.id_number_encrypted and PASSPORT not in chipo.id_number_encrypted


def test_add_form_stores_encrypted_and_edit_keeps_or_replaces_it(logged_in, db):
    def post(path, **fields):
        return logged_in.post(path, data={"csrf_token": csrf(logged_in), **fields}, follow_redirects=False)

    assert post("/employees/new", first_name="Thabo", surname="Molefe", id_type="omang", id_number=OMANG).status_code == 303
    e = db.scalars(select(Employee)).one()
    stored = e.id_number_encrypted
    assert decrypt_id(stored) == OMANG and e.id_last4 == "4321"

    # saving without touching the ID keeps the stored value exactly
    assert post(f"/employees/{e.id}/edit", first_name="Thabo", surname="Molefe-Smith", id_type="omang", id_number="").status_code == 303
    db.refresh(e)
    assert e.id_number_encrypted == stored and e.surname == "Molefe-Smith" and e.id_last4 == "4321"

    # typing a new number replaces it, and the password (last 4) follows
    assert post(f"/employees/{e.id}/edit", first_name="Thabo", surname="Molefe-Smith", id_type="omang", id_number="111222333").status_code == 303
    db.refresh(e)
    assert decrypt_id(e.id_number_encrypted) == "111222333" and e.id_last4 == "2333"

    # changing the type needs the number typed again, and nothing changes when that is refused
    r = post(f"/employees/{e.id}/edit", first_name="Thabo", surname="Molefe-Smith", id_type="passport", id_number="")
    assert r.status_code == 422
    db.refresh(e)
    assert decrypt_id(e.id_number_encrypted) == "111222333" and e.id_type == "omang"


def test_a_number_that_is_not_9_digits_is_saved_after_a_warning_and_a_bad_passport_is_refused(logged_in, db):
    def post(**fields):
        return logged_in.post("/employees/new", data={"csrf_token": csrf(logged_in), "first_name": "A", "surname": "One", **fields},
                              follow_redirects=False)

    r = post(id_type="omang", id_number="12345678")
    assert r.status_code == 200 and "9 digits" in r.text and db.query(Employee).count() == 0   # warned, not yet saved
    ack = re.search(r'name="ack" value="([^"]+)"', r.text).group(1)
    assert post(id_type="omang", id_number="12345678", ack=ack).status_code == 303 and db.query(Employee).count() == 1
    assert post(id_type="passport", id_number="AB12").status_code == 422                        # 4 characters: too short


# ---- hidden by default -------------------------------------------------------------------------------------------

def make_people(db, n=3):
    people = [make_employee(db, f"Person{chr(65 + i)}", "Test", f"+2677100000{i}", id_number=f"90000000{i}") for i in range(n)]
    people.append(make_employee(db, "Pass", "Port", id_number=PASSPORT, id_type="passport"))
    return people


def test_no_page_contains_a_full_id_by_default(logged_in, db):
    people = make_people(db)
    pages = [logged_in.get("/employees", params={"status": "all"}).text, logged_in.get("/").text,
             logged_in.get("/employees", params={"q": "Port"}).text, logged_in.get("/employees/import").text]
    pages += [logged_in.get(f"/employees/{p.id}/edit").text for p in people]
    secrets = [f"90000000{i}" for i in range(3)] + [PASSPORT, PASSPORT.lower()]
    for html in pages:
        assert not any(s in html for s in secrets)
    assert logged_in.get("/employees").text.count("data-id-slot=") == 4
    assert all(p.id_number_encrypted not in pages[0] for p in people)          # not even the encrypted form is sent


def test_list_page_is_not_cached(logged_in, db):
    make_people(db)
    assert logged_in.get("/employees").headers["cache-control"] == "no-store"


# ---- the reveal endpoint -----------------------------------------------------------------------------------------

def test_reveal_requires_login(client, db):
    e = make_people(db)[0]
    r = client.post("/employees/reveal-ids", data={"employee_id": e.id}, follow_redirects=False)
    assert r.status_code in (303, 403) and "9000" not in r.text
    r = client.post("/employees/reveal-ids", data={"employee_id": e.id, "csrf_token": "x"}, follow_redirects=False)
    assert "900000000" not in r.text


def test_reveal_requires_a_csrf_token(logged_in, db):
    e = make_people(db)[0]
    for data, headers in (({"employee_id": e.id}, {}), ({"employee_id": e.id, "csrf_token": "wrong"}, {})):
        r = logged_in.post("/employees/reveal-ids", data=data, headers=headers)
        assert r.status_code == 403 and "900000000" not in r.text
    assert audit_rows(db) == []
    # the header form of the token is accepted too
    r = logged_in.post("/employees/reveal-ids", data={"employee_id": e.id}, headers={"X-CSRF-Token": csrf(logged_in)})
    assert r.status_code == 200 and r.json() == {"ids": {str(e.id): "900000000"}}


def test_reveal_returns_only_the_ids_on_the_requested_page_and_is_never_cached(logged_in, db):
    people = [make_employee(db, "Person", f"N{chr(97 + i)}{chr(97 + i)}", id_number=f"80000{i:04d}") for i in range(12)]
    first = reveal(logged_in, per_page="10", page="1", sort="name")
    second = reveal(logged_in, per_page="10", page="2", sort="name")
    page_1 = logged_in.get("/employees", params={"per_page": 10, "page": 1}).text
    page_2 = logged_in.get("/employees", params={"per_page": 10, "page": 2}).text
    for response in (first, second):
        assert response.status_code == 200 and response.headers["cache-control"].startswith("no-store")
    shown_1 = set(map(int, first.json()["ids"]))
    shown_2 = set(map(int, second.json()["ids"]))
    assert len(shown_1) == 10 and len(shown_2) == 2 and not shown_1 & shown_2
    assert shown_1 == set(map(int, re.findall(r'data-id-slot="(\d+)"', page_1)))      # exactly the rows drawn on that page
    assert shown_2 == set(map(int, re.findall(r'data-id-slot="(\d+)"', page_2)))
    assert first.json()["ids"][str(people[0].id)] == "800000000"


def test_reveal_follows_the_same_search_and_filter_as_the_page(logged_in, db):
    people = make_people(db)
    make_employee(db, "Gone", "Away", active=False, id_number="777777777")
    ids = reveal(logged_in, q="Port").json()["ids"]
    assert ids == {str(people[3].id): PASSPORT}
    inactive = reveal(logged_in, status="inactive").json()["ids"]
    assert list(inactive.values()) == ["777777777"]
    assert reveal(logged_in, q="zzz-nobody").json() == {"ids": {}}


def test_reveal_one_employee_for_the_edit_page(logged_in, db):
    people = make_people(db)
    r = reveal(logged_in, employee_id=str(people[1].id))
    assert r.json() == {"ids": {str(people[1].id): "900000001"}}                   # only that one, not the whole list
    assert reveal(logged_in, employee_id="99999").status_code == 404


def test_people_without_a_full_id_are_left_out(logged_in, db):
    old = make_employee(db, "Old", "Record", id_last4="4321")
    new = make_employee(db, "New", "Record", id_number=OMANG)
    ids = reveal(logged_in, q="Record").json()["ids"]
    assert ids == {str(new.id): OMANG} and str(old.id) not in ids


def test_unreadable_stored_ids_are_skipped_and_logged_without_values(logged_in, db, caplog):
    caplog.set_level(logging.DEBUG)
    good = make_employee(db, "Good", "One", id_number=OMANG)
    bad = make_employee(db, "Bad", "Key", id_last4="1111")
    bad.id_number_encrypted = IdCipher(Fernet.generate_key().decode()).encrypt("555555555")
    db.commit()
    r = reveal(logged_in)
    assert r.status_code == 200 and r.json()["ids"] == {str(good.id): OMANG} and "555555555" not in r.text
    assert "could not be decrypted" in caplog.text and "555555555" not in caplog.text


# ---- audit and rate limit ----------------------------------------------------------------------------------------

def test_one_audit_entry_per_reveal_with_a_count_and_no_id_values(logged_in, db, admin):
    people = make_people(db)
    reveal(logged_in, status="all")
    (entry,) = audit_rows(db)
    assert entry.user_id == admin.id and entry.details == f"username '{admin.username}', employees=4"
    assert entry.entity_type is None and entry.ip_address
    dump = " ".join(str(v) for v in (entry.action, entry.details, entry.entity_type, entry.entity_id, entry.ip_address))
    assert not any(s in dump for s in [*ALL_IDS, "90000000", "6789", "CD34"])

    reveal(logged_in, employee_id=str(people[0].id))
    second = audit_rows(db)[1]
    assert second.entity_type == "employee" and second.entity_id == people[0].id and second.details.endswith("employees=1")
    assert len(audit_rows(db)) == 2                                                  # one per request


def test_nothing_is_audited_when_nothing_was_revealed(logged_in, db):
    make_employee(db, "Old", "Record", id_last4="4321")
    assert reveal(logged_in).json() == {"ids": {}} and audit_rows(db) == []


def test_reveal_is_rate_limited_per_admin(logged_in, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "reveal_max_per_minute", 3)
    e = make_people(db)[0]
    assert [reveal(logged_in, employee_id=str(e.id)).status_code for _ in range(3)] == [200, 200, 200]
    blocked = reveal(logged_in, employee_id=str(e.id))
    assert blocked.status_code == 429 and "900000000" not in blocked.text and "ids" not in blocked.json()
    assert blocked.headers["cache-control"].startswith("no-store")
    assert len(audit_rows(db)) == 3                                                  # a refused request reveals and records nothing


def test_rate_limit_only_counts_the_last_minute(db, admin):
    from datetime import timedelta

    from app.models.mixins import utcnow

    for _ in range(10):
        db.add(AuditLog(user_id=admin.id, action=id_reveal.ACTION, created_at=utcnow() - timedelta(minutes=2)))
    db.commit()
    assert id_reveal.is_rate_limited(db, admin) is False
    for _ in range(10):
        db.add(AuditLog(user_id=admin.id, action=id_reveal.ACTION))
    db.commit()
    assert id_reveal.is_rate_limited(db, admin) is True


# ---- no other way out --------------------------------------------------------------------------------------------

def test_the_reveal_route_is_the_only_one_that_can_return_full_ids(app):
    paths = sorted(r.path for r in _api_routes(app.routes))
    assert len(paths) >= 15
    assert [p for p in paths if "reveal" in p] == ["/employees/reveal-ids"]
    # the only file/JSON endpoints: the audit export, the employee list export (names, phones, status: see test_ui_upgrade.py), the batch
    # send report and progress (none holds ID numbers: see test_sending.py, test_audit_system.py)
    assert [p for p in paths if re.search(r"export|download|csv|xlsx|json|api", p, re.I)] == [
        "/audit/export.csv", "/batches/{batch_id:int}/report.csv", "/batches/{batch_id:int}/status.json", "/employees/export.csv"]
    # the only file a user can download is the empty import template
    assert [p for p in paths if p.endswith("/template")] == ["/employees/import/template"]


def test_the_downloadable_template_contains_no_real_data(logged_in, db):
    make_people(db)
    data = logged_in.get("/employees/import/template").content
    wb = load_workbook(io.BytesIO(data))
    text = " ".join(str(c.value) for ws in wb for row in ws.iter_rows() for c in row if c.value)
    assert not any(s in text for s in [*ALL_IDS, "90000000", "PersonA"])


def test_get_requests_cannot_reveal_anything(logged_in, db):
    e = make_people(db)[0]
    assert logged_in.get("/employees/reveal-ids", params={"employee_id": e.id}).status_code in (404, 405)
    for fmt in ("csv", "json", "xlsx"):
        r = logged_in.get("/employees", params={"format": fmt, "status": "all"})
        assert "900000000" not in r.text and r.headers["content-type"].startswith("text/html")


# ---- logs --------------------------------------------------------------------------------------------------------

def test_logs_never_contain_full_ids_keys_or_tokens(logged_in, db, caplog):
    caplog.set_level(logging.DEBUG)
    csv = "first_name,surname,phone,id_number,id_type\nThabo,Molefe,71234567,123456789,omang\nBad,Pass,71234568,AB1,passport\n"
    logged_in.post("/employees/import", files={"file": ("p.csv", csv.encode())}, data={"csrf_token": csrf(logged_in)})
    logged_in.get("/employees/import/preview")
    logged_in.post("/employees/import/confirm", data={"csrf_token": csrf(logged_in)})
    logged_in.post("/employees/new", data={"csrf_token": csrf(logged_in), "first_name": "Chipo", "surname": "Fake",
                                           "id_type": "passport", "id_number": PASSPORT})
    logged_in.post("/employees/new", data={"csrf_token": csrf(logged_in), "first_name": "X", "surname": "Y",
                                           "id_type": "omang", "id_number": "12AB34567"})   # an error path
    thabo = db.scalars(select(Employee).where(Employee.first_name == "Thabo")).one()
    reveal(logged_in, status="all")
    reveal(logged_in, employee_id=str(thabo.id))
    tokens = [e.id_number_encrypted for e in db.scalars(select(Employee)) if e.id_number_encrypted]
    key = get_settings().id_encryption_key.get_secret_value()
    assert tokens
    for forbidden in ("123456789", PASSPORT, PASSPORT.lower(), "12AB34567", key, *tokens):
        assert forbidden not in caplog.text, forbidden[:6]


def test_error_responses_never_echo_the_id(logged_in):
    r = logged_in.post("/employees/new", data={"csrf_token": csrf(logged_in), "first_name": "X", "surname": "Y",
                                               "id_type": "omang", "id_number": "12AB34567"})
    assert r.status_code == 422 and "12AB34567" not in r.text
