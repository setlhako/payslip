"""One ID number and one phone number can belong to only one employee: blind index, form, importer, database, migration."""
import base64
import hashlib
import hmac
import logging
import re

import pytest
from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, get_settings
from app.models import Employee
from app.scripts.backfill_id_hash import backfill
from app.scripts.reencrypt_ids import reencrypt_all
from app.services import employees as employee_service
from app.services.id_crypto import IdCipher, decrypt_id, encrypt_id, get_cipher, id_blind_index, normalise_id
from app.services.import_parser import parse_upload
from app.services.import_planner import ERROR, NEW, UNCHANGED, UPDATE, plan_import
from tests.conftest import csrf_from, make_employee

OMANG = "123456789"
PASSPORT = "BP1234567"
HEADER = "first_name,surname,phone,id_number,id_type,employee_code\n"


def plan(db, *lines):
    return plan_import(db, parse_upload("p.csv", (HEADER + "\n".join(lines) + "\n").encode(), 2000).rows)


def form(client, path, **fields):
    token = csrf_from(client.get("/").text)
    return client.post(path, data={"csrf_token": token, **fields}, follow_redirects=False)


# ---- the blind index ---------------------------------------------------------------------------------------------

def test_blind_index_is_hmac_of_the_normalised_id_with_an_hkdf_derived_key():
    key = get_settings().id_encryption_key.get_secret_value()
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"id-blind-index").derive(base64.urlsafe_b64decode(key))
    expected = hmac.new(derived, b"AB1234567", hashlib.sha256).hexdigest()
    assert id_blind_index("ab 12-34567") == expected and len(expected) == 64
    assert id_blind_index("AB1234567") == id_blind_index(" ab-1234567 ")           # case, spaces and dashes do not matter
    assert id_blind_index("123456789") != id_blind_index("123456788")


def test_blind_index_ignores_the_id_type_so_omang_and_passport_strings_collide():
    assert normalise_id("123456789") == "123456789"                               # the type is not an input at all
    assert id_blind_index(OMANG) == id_blind_index(OMANG)


def test_blind_index_is_not_the_plain_hash_and_changes_with_the_key():
    assert id_blind_index(OMANG) != hashlib.sha256(OMANG.encode()).hexdigest()
    other = Fernet.generate_key().decode()
    assert IdCipher(other).blind_index(OMANG) != id_blind_index(OMANG)
    assert get_cipher().blind_index(OMANG) == id_blind_index(OMANG)


def test_rotating_the_key_changes_hashes_and_reencrypt_rehashes(db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", id_number=OMANG)
    old_hash = e.id_number_hash
    new = Fernet.generate_key().decode()
    cipher = IdCipher(new, [get_settings().id_encryption_key.get_secret_value()])
    done, failed = reencrypt_all(db, cipher)
    db.refresh(e)
    assert (done, failed) == (1, []) and e.id_number_hash == cipher.blind_index(OMANG) != old_hash
    assert cipher.decrypt(e.id_number_encrypted) == OMANG


# ---- the database is the last line of defence --------------------------------------------------------------------

def test_database_refuses_the_same_phone_or_id_hash_twice(db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", id_number=OMANG)
    for first, kwargs in (("Phone", dict(phone="+26771234567")), ("Hash", dict(id_number_hash=id_blind_index(OMANG)))):
        db.add(Employee(first_name=first, surname="Clash", name_key=f"clash {first.lower()}", active=True, **kwargs))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_many_employees_may_have_no_phone_and_no_full_id(db):
    for i in range(3):
        make_employee(db, f"No{i}", "Data")
    make_employee(db, "Only", "Last4", id_last4="1234")
    make_employee(db, "Only2", "Last4", id_last4="1234")                           # a last 4 may repeat: it is not the ID
    assert db.query(Employee).count() == 5


# ---- the add/edit form -------------------------------------------------------------------------------------------

def test_form_refuses_a_phone_that_belongs_to_someone_else_and_names_them(logged_in, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567")
    r = form(logged_in, "/employees/new", first_name="Neo", surname="Dube", phone="71234567")
    assert r.status_code == 422 and "This phone number already belongs to Thabo Molefe." in r.text
    assert db.query(Employee).count() == 1


def test_form_refuses_an_id_that_belongs_to_someone_else_and_never_shows_it(logged_in, db, caplog):
    caplog.set_level(logging.DEBUG)
    make_employee(db, "Thabo", "Molefe", "+26771234567", id_number=OMANG)
    r = form(logged_in, "/employees/new", first_name="Neo", surname="Dube", id_type="omang", id_number="123 456-789")
    assert r.status_code == 422 and "This ID number already belongs to Thabo Molefe." in r.text
    assert OMANG not in r.text and OMANG[-4:] not in r.text and id_blind_index(OMANG) not in r.text
    assert OMANG not in caplog.text and id_blind_index(OMANG) not in caplog.text
    assert db.query(Employee).count() == 1


def test_the_same_characters_as_omang_and_as_passport_are_the_same_id(logged_in, db):
    make_employee(db, "Thabo", "Molefe", id_number=OMANG)                          # an Omang
    r = form(logged_in, "/employees/new", first_name="Neo", surname="Dube", id_type="passport", id_number=OMANG)
    assert r.status_code == 422 and "already belongs to Thabo Molefe" in r.text


def test_passport_case_and_spaces_do_not_hide_a_duplicate(logged_in, db):
    make_employee(db, "Chipo", "Fake", id_number=PASSPORT, id_type="passport")
    r = form(logged_in, "/employees/new", first_name="Neo", surname="Dube", id_type="passport", id_number="bp 1234-567")
    assert r.status_code == 422 and "already belongs to Chipo Fake" in r.text


def test_editing_a_person_without_changing_their_own_phone_or_id_is_fine(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", id_number=OMANG)
    r = form(logged_in, f"/employees/{e.id}/edit", first_name="Thabo", surname="Molefe-Smith", phone="71234567",
             id_type="omang", id_number=OMANG)
    assert r.status_code == 303
    db.refresh(e)
    assert e.surname == "Molefe-Smith" and e.id_number_hash == id_blind_index(OMANG)


def test_form_saves_the_hash_and_changing_the_id_changes_it(logged_in, db):
    form(logged_in, "/employees/new", first_name="Neo", surname="Dube", id_type="omang", id_number=OMANG)
    e = db.scalars(select(Employee)).one()
    assert e.id_number_hash == id_blind_index(OMANG)
    form(logged_in, f"/employees/{e.id}/edit", first_name="Neo", surname="Dube", id_type="omang", id_number="987654321")
    db.refresh(e)
    assert e.id_number_hash == id_blind_index("987654321")
    form(logged_in, f"/employees/{e.id}/edit", first_name="Neo", surname="Dube")     # blank = keep
    db.refresh(e)
    assert e.id_number_hash == id_blind_index("987654321") and decrypt_id(e.id_number_encrypted) == "987654321"


def test_inactive_employees_still_hold_their_phone_and_id_until_edited(logged_in, db):
    old = make_employee(db, "Old", "Timer", "+26771234567", id_number=OMANG, active=False)
    r = form(logged_in, "/employees/new", first_name="Neo", surname="Dube", phone="71234567")
    assert r.status_code == 422 and "already belongs to Old Timer" in r.text and "inactive" in r.text
    # editing the inactive employee frees the phone (blank phone) and the ID (remove)
    assert form(logged_in, f"/employees/{old.id}/edit", first_name="Old", surname="Timer", phone="", remove_id="1").status_code == 303
    db.refresh(old)
    assert old.phone is None and old.id_number_hash is None and old.id_number_encrypted is None and old.id_last4 is None
    assert form(logged_in, "/employees/new", first_name="Neo", surname="Dube", phone="71234567",
                id_type="omang", id_number=OMANG).status_code == 303


def test_remove_id_and_a_new_id_together_is_an_error(logged_in, db):
    e = make_employee(db, "Thabo", "Molefe", id_number=OMANG)
    r = form(logged_in, f"/employees/{e.id}/edit", first_name="Thabo", surname="Molefe", id_type="omang",
             id_number="987654321", remove_id="1")
    assert r.status_code == 422 and "not both" in r.text


def test_race_the_database_constraint_wins_on_the_form(logged_in, db, monkeypatch):
    """Both checks pass (we pretend nobody owns the number), but the database has the last word: nothing is saved."""
    make_employee(db, "Thabo", "Molefe", "+26771234567", id_number=OMANG)
    real = employee_service.phone_owner
    calls = {"n": 0}

    def blind_first_time(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr(employee_service, "phone_owner", blind_first_time)
    r = form(logged_in, "/employees/new", first_name="Neo", surname="Dube", phone="71234567")
    assert r.status_code == 422 and "already belongs to Thabo Molefe" in r.text     # message names the person who got there first
    assert db.query(Employee).count() == 1


def test_race_with_nothing_to_name_still_gives_a_plain_message(logged_in, db, monkeypatch):
    make_employee(db, "Thabo", "Molefe", "+26771234567")
    monkeypatch.setattr(employee_service, "phone_owner", lambda *a, **k: None)
    r = form(logged_in, "/employees/new", first_name="Neo", surname="Dube", phone="71234567")
    assert r.status_code == 422 and "already belongs to another employee" in r.text and db.query(Employee).count() == 1


# ---- the importer ------------------------------------------------------------------------------------------------

def test_import_flags_a_phone_or_id_that_belongs_to_a_different_existing_employee(db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", id_number=OMANG)
    a, b, c = plan(db,
                   "Neo,Dube,71234567,,",                  # Thabo's phone
                   "Kea,Moeng,71111111,123456789,omang,",  # Thabo's ID
                   "Pat,Fine,72222222,555555555,omang,")
    assert a.action == ERROR and "This phone number already belongs to Thabo Molefe." in " ".join(a.errors)
    assert b.action == ERROR and "This ID number already belongs to Thabo Molefe." in " ".join(b.errors)
    assert c.action == NEW and not c.errors
    assert OMANG not in " ".join(a.errors + b.errors)


def test_import_same_person_again_is_fine(db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", id_number=OMANG)
    (p,) = plan(db, "Thabo,Molefe,71234567,123456789,omang,")
    assert p.action == UNCHANGED and not p.errors
    (q,) = plan(db, "Thabo,Molefe,71234567,987654321,omang,")                         # same person, new ID: an update
    assert q.action == UPDATE and not q.errors


def test_import_same_person_matched_by_code_may_keep_their_values(db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", id_number=OMANG, code="E1")
    (p,) = plan(db, "T,M,71234567,123456789,omang,E1")
    assert p.action != ERROR and not p.errors


def test_import_duplicates_inside_the_file_are_errors_on_every_involved_row(db):
    a, b, c, d = plan(db,
                      "Neo,Dube,71234567,111111111,omang,",
                      "Kea,Moeng,71234567,222222222,omang,",           # same phone as row 2
                      "Pat,Lee,73333333,333333333,omang,",
                      "Sam,Ray,74444444,333333333,passport,")          # same characters as row 4, other type
    assert a.action == b.action == ERROR and c.action == d.action == ERROR
    assert "row 3" in a.errors[0] and "row 2" in b.errors[0]
    assert any("row 5" in e for e in c.errors) and any("row 4" in e for e in d.errors)


def test_import_row_without_the_clash_is_not_dragged_into_it(db):
    a, b = plan(db, "Neo,Dube,71234567,,", "Kea,Moeng,72222222,,")
    assert a.action == b.action == NEW


def test_import_hash_is_stored_and_matches_the_form(logged_in, db):
    csv = HEADER + f"Thabo,Molefe,71234567,{OMANG},omang,\n"
    token = csrf_from(logged_in.get("/employees/import").text)
    logged_in.post("/employees/import", files={"file": ("p.csv", csv.encode())}, data={"csrf_token": token})
    logged_in.post("/employees/import/confirm", data={"csrf_token": csrf_from(logged_in.get("/").text)})
    assert db.scalars(select(Employee)).one().id_number_hash == id_blind_index(OMANG)


def test_import_preview_shows_the_clash_without_any_id_value(logged_in, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", id_number=OMANG)
    csv = HEADER + f"Neo,Dube,71111111,{OMANG},omang,\n"
    token = csrf_from(logged_in.get("/employees/import").text)
    logged_in.post("/employees/import", files={"file": ("p.csv", csv.encode())}, data={"csrf_token": token})
    page = logged_in.get("/employees/import/preview").text
    assert "already belongs to Thabo Molefe" in page and OMANG not in page and OMANG[-4:] not in page


def test_import_race_the_database_wins_and_nothing_is_saved(logged_in, db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    csv = HEADER + f"Neo,Dube,71234567,{OMANG},omang,\nKea,Moeng,72222222,,,\n"
    token = csrf_from(logged_in.get("/employees/import").text)
    logged_in.post("/employees/import", files={"file": ("p.csv", csv.encode())}, data={"csrf_token": token})

    from app.routers import employees as router
    from app.services.import_parser import parse_upload as parse

    stale = plan_import(db, parse("p.csv", csv.encode(), 2000).rows)               # planned BEFORE the competitor appears
    make_employee(db, "Thabo", "Molefe", "+26771234567")                           # somebody else saves this phone meanwhile
    monkeypatch.setattr(router, "plan_import", lambda *a, **k: stale)
    r = logged_in.post("/employees/import/confirm", data={"csrf_token": csrf_from(logged_in.get("/").text)}, follow_redirects=True)
    assert "Nothing was changed" in r.text
    assert db.query(Employee).count() == 1                                          # only the competitor: all or nothing
    assert OMANG not in caplog.text and OMANG not in r.text


# ---- migrations and the backfill command -------------------------------------------------------------------------

STEP0, STEP1, STEP2 = "f7c3a91d2e58", "a8d4e2b61c90", "b9e5f3c72da1"
INSERT = text(
    "INSERT INTO employees (first_name, surname, name_key, phone, id_type, id_last4, id_number_encrypted, active, created_at, updated_at) "
    "VALUES (:f, :s, :k, :p, :t, :l, :e, 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
)


@pytest.fixture
def migrating(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    saved_handlers, saved_level = logging.root.handlers[:], logging.root.level
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    engine = create_engine(url)
    yield cfg, engine
    engine.dispose()
    logging.root.handlers[:] = saved_handlers
    logging.root.setLevel(saved_level)
    monkeypatch.undo()
    get_settings.cache_clear()


def add(engine, first, surname, phone=None, number=None):
    with engine.begin() as c:
        c.execute(INSERT, dict(f=first, s=surname, k=f"{surname} {first}".lower(), p=phone,
                               t="omang" if number else None, l=number[-4:] if number else None,
                               e=encrypt_id(number) if number else None))


def index_names(engine):
    return {i["name"]: bool(i["unique"]) for i in inspect(engine).get_indexes("employees")}


def test_step_one_never_fails_on_messy_existing_data(migrating):
    cfg, engine = migrating
    command.upgrade(cfg, STEP0)
    add(engine, "A", "One", "+26771234567", OMANG)
    add(engine, "B", "Two", "+26771234567", OMANG)                                  # duplicates everywhere
    command.upgrade(cfg, STEP1)
    cols = {c["name"] for c in inspect(engine).get_columns("employees")}
    assert "id_number_hash" in cols and index_names(engine)["ix_employees_phone"] is False
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM employees")).scalar() == 2


def test_step_two_refuses_until_backfilled_and_deduplicated_then_makes_the_indexes_unique(migrating):
    cfg, engine = migrating
    command.upgrade(cfg, STEP1)
    add(engine, "A", "One", "+26771234567", OMANG)
    add(engine, "B", "Two", "+26771234567", "987654321")
    add(engine, "C", "Three", None, OMANG)                                          # same ID as A
    add(engine, "D", "Four", None, None)

    with pytest.raises(CommandError) as refused:                                    # 1. hashes missing
        command.upgrade(cfg, "head")
    assert "backfill_id_hash" in str(refused.value) and OMANG not in str(refused.value)

    with Session(engine) as db:
        report = backfill(db, get_cipher())
    assert report.hashed_now == 3 and not report.clean
    assert [sorted(n for _, n in g) for g in report.duplicate_ids] == [["A One", "C Three"]]
    assert [sorted(n for _, n in g) for g in report.duplicate_phones] == [["A One", "B Two"]]
    with Session(engine) as db:                                                     # idempotent
        assert backfill(db, get_cipher()).hashed_now == 0

    with pytest.raises(CommandError) as refused:                                    # 2. duplicates remain
        command.upgrade(cfg, "head")
    assert "more than one employee" in str(refused.value)
    assert index_names(engine)["ix_employees_phone"] is False                       # nothing was changed

    with engine.begin() as c:                                                       # HR fixes the data
        c.execute(text("UPDATE employees SET phone = '+26772222222' WHERE first_name = 'B'"))
        c.execute(text("UPDATE employees SET id_number_encrypted = NULL, id_number_hash = NULL, id_type = NULL, id_last4 = NULL "
                       "WHERE first_name = 'C'"))
    with Session(engine) as db:
        assert backfill(db, get_cipher()).clean

    command.upgrade(cfg, "head")
    names = index_names(engine)
    assert names["uq_employees_phone"] is True and names["uq_employees_id_number_hash"] is True
    assert "ix_employees_phone" not in names
    with pytest.raises(IntegrityError):
        add(engine, "E", "Five", "+26771234567")
    with engine.begin() as c:                                                       # NULLs may still repeat
        add(engine, "F", "Six")
        add(engine, "G", "Seven")

    command.downgrade(cfg, STEP1)                                                   # and back
    assert index_names(engine)["ix_employees_phone"] is False


def test_backfill_reports_names_and_ids_but_never_values(migrating, capsys):
    cfg, engine = migrating
    command.upgrade(cfg, STEP1)
    add(engine, "Anna", "One", "+26771234567", OMANG)
    add(engine, "Bea", "Two", "+26771234567", OMANG)
    from app.scripts import backfill_id_hash
    import app.database

    original = backfill_id_hash.SessionLocal
    backfill_id_hash.SessionLocal = lambda: Session(engine)
    try:
        code = backfill_id_hash.main()
    finally:
        backfill_id_hash.SessionLocal = original
    out = capsys.readouterr().out
    assert code == 1 and "Anna One" in out and "Bea Two" in out and "employee #1" in out
    assert OMANG not in out and "71234567" not in out and id_blind_index(OMANG) not in out
    assert "NOT applied yet" in out


def test_model_has_the_unique_indexes_so_a_fresh_install_enforces_them():
    indexes = {i.name: i.unique for i in Employee.__table__.indexes}
    assert indexes["uq_employees_phone"] and indexes["uq_employees_id_number_hash"]
