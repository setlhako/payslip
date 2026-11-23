"""The id_type / id_last4 migration must keep every existing employee and every stored last-4 value."""
import logging

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import PROJECT_ROOT, get_settings

OLD, NEW = "32ae9ef285f6", "2419d27d8159"
INSERT = text(
    "INSERT INTO employees (first_name, surname, name_key, phone, omang_last4, employee_code, active, created_at, updated_at) "
    "VALUES (:f, :s, :k, :p, :o, :c, 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
)


def test_migration_keeps_existing_rows_and_values(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    saved_handlers, saved_level = logging.root.handlers[:], logging.root.level  # alembic reconfigures logging
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    engine = create_engine(url)
    try:
        command.upgrade(cfg, OLD)                                   # the schema as it was before this change
        with engine.begin() as c:
            c.execute(INSERT, dict(f="Thabo", s="Molefe", k="molefe thabo", p="+26771234567", o="4321", c="E1"))
            c.execute(INSERT, dict(f="Neo", s="Dube", k="dube neo", p=None, o="0042", c=None))       # leading zeros
            c.execute(INSERT, dict(f="Kea", s="Nophone", k="kea nophone", p=None, o=None, c=None))   # no ID on file

        command.upgrade(cfg, NEW)
        cols = {col["name"] for col in inspect(engine).get_columns("employees")}
        assert {"id_type", "id_last4"} <= cols and "omang_last4" not in cols
        with engine.connect() as c:
            rows = c.execute(text("SELECT first_name, phone, employee_code, id_type, id_last4 FROM employees ORDER BY id")).all()
        assert rows == [
            ("Thabo", "+26771234567", "E1", "omang", "4321"),     # existing values become id_type='omang'
            ("Neo", None, None, "omang", "0042"),                 # leading zeros survive
            ("Kea", None, None, None, None),                      # no ID stays "no ID"
        ]

        # a passport added afterwards: going back keeps the person but cannot keep a passport in the old column
        with engine.begin() as c:
            c.execute(text("UPDATE employees SET id_type='passport', id_last4='AB12' WHERE first_name='Kea'"))
        command.downgrade(cfg, OLD)
        with engine.connect() as c:
            back = c.execute(text("SELECT first_name, omang_last4 FROM employees ORDER BY id")).all()
        assert back == [("Thabo", "4321"), ("Neo", "0042"), ("Kea", None)]

        command.upgrade(cfg, "head")                                # and forward again
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM employees")).scalar() == 3
    finally:
        engine.dispose()
        logging.root.handlers[:] = saved_handlers
        logging.root.setLevel(saved_level)
        monkeypatch.undo()
        get_settings.cache_clear()


# ---- id_number_encrypted ---------------------------------------------------------------------------------------

BEFORE_ENC, WITH_ENC = "2419d27d8159", "9b3f6c1d4e27"
INSERT_NEW = text(
    "INSERT INTO employees (first_name, surname, name_key, phone, id_type, id_last4, employee_code, active, created_at, updated_at) "
    "VALUES (:f, :s, :k, :p, :t, :l, :c, 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
)


def test_encrypted_id_column_keeps_every_existing_row_and_starts_empty(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migration2.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    saved_handlers, saved_level = logging.root.handlers[:], logging.root.level
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    engine = create_engine(url)
    try:
        command.upgrade(cfg, BEFORE_ENC)
        with engine.begin() as c:
            c.execute(INSERT_NEW, dict(f="Thabo", s="Molefe", k="molefe thabo", p="+26771234567", t="omang", l="4321", c="E1"))
            c.execute(INSERT_NEW, dict(f="Chipo", s="Fakename", k="chipo fakename", p=None, t="passport", l="AB12", c=None))
            c.execute(INSERT_NEW, dict(f="Kea", s="Nophone", k="kea nophone", p=None, t=None, l=None, c=None))

        command.upgrade(cfg, WITH_ENC)
        assert "id_number_encrypted" in {col["name"] for col in inspect(engine).get_columns("employees")}
        select_all = text("SELECT first_name, phone, employee_code, id_type, id_last4, id_number_encrypted FROM employees ORDER BY id")
        with engine.connect() as c:
            assert c.execute(select_all).all() == [
                ("Thabo", "+26771234567", "E1", "omang", "4321", None),     # everything kept; full ID not known yet
                ("Chipo", None, None, "passport", "AB12", None),
                ("Kea", None, None, None, None, None),
            ]

        with engine.begin() as c:
            c.execute(text("UPDATE employees SET id_number_encrypted = 'gAAAA-fake' WHERE first_name = 'Thabo'"))
        command.downgrade(cfg, BEFORE_ENC)                                   # going back drops only the encrypted column
        cols = {col["name"] for col in inspect(engine).get_columns("employees")}
        assert "id_number_encrypted" not in cols and {"id_type", "id_last4"} <= cols
        with engine.connect() as c:
            assert c.execute(text("SELECT first_name, id_type, id_last4 FROM employees ORDER BY id")).all() == [
                ("Thabo", "omang", "4321"), ("Chipo", "passport", "AB12"), ("Kea", None, None)]
    finally:
        engine.dispose()
        logging.root.handlers[:] = saved_handlers
        logging.root.setLevel(saved_level)
        monkeypatch.undo()
        get_settings.cache_clear()
