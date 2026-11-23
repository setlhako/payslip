"""The purge command, the fake-PDF generator and the batches migration."""
import io
import logging
import zipfile
from datetime import timedelta

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text

from app.config import PROJECT_ROOT, get_settings
from app.models import AuditLog, Batch, Employee, Payslip
from app.models.mixins import utcnow
from app.scripts import make_test_payslips
from app.scripts.purge_old_batches import purge
from app.services import batch_ingest, batch_storage
from app.services.batch_ingest import ingest_pdfs
from tests.conftest import make_employee
from tests.pdfs import named_pdf, upload_obj


def make_batch(db, days_old: int, status="draft", files=2) -> Batch:
    b = Batch(pay_month_label="Old 2026", status=status)
    db.add(b)
    db.flush()
    b.created_at = utcnow() - timedelta(days=days_old)
    ingest_pdfs(db, b, [upload_obj(f"f{i}.pdf", named_pdf("A", "B")) for i in range(files)])
    db.commit()
    return b


# ---- purge_old_batches -------------------------------------------------------------------------------------------

def test_purge_deletes_files_past_retention_and_keeps_newer_batches(db):
    old, recent = make_batch(db, 45), make_batch(db, 5)
    counts = purge(db, days=30)
    assert counts == {"batches": 1, "files": 2, "orphan_folders": 0}
    assert not batch_storage.batch_dir(old.id).exists() and len(list(batch_storage.batch_dir(recent.id).iterdir())) == 2
    db.expire_all()
    assert db.get(Batch, old.id).files_deleted_at is not None and db.get(Batch, recent.id).files_deleted_at is None
    kept = db.scalars(select(Payslip).where(Payslip.batch_id == old.id)).all()
    assert len(kept) == 2 and all(p.stored_filename is None and p.files_deleted_at for p in kept)      # the list is kept, the files are gone
    (entry,) = [a for a in db.scalars(select(AuditLog)) if a.action == "batch_files_purged"]
    assert entry.entity_id == old.id and entry.details == "files=2"


def test_purge_uses_the_retention_setting_and_is_safe_to_repeat(db, monkeypatch):
    make_batch(db, 10)
    monkeypatch.setattr(get_settings(), "batch_retention_days", 7)
    assert purge(db)["batches"] == 1
    assert purge(db) == {"batches": 0, "files": 0, "orphan_folders": 0}


def test_purge_leaves_batches_that_are_being_sent_alone(db):
    sending = make_batch(db, 90, status="sending")
    assert purge(db, days=30)["batches"] == 0 and len(list(batch_storage.batch_dir(sending.id).iterdir())) == 2


def test_purge_removes_folders_that_belong_to_no_batch(db):
    orphan = batch_storage.ensure_batch_dir(9999)
    (orphan / "left-behind.pdf").write_bytes(b"x")
    assert purge(db, days=30)["orphan_folders"] == 0 and orphan.exists()                       # too fresh: an upload may still be running
    assert purge(db, days=30, orphan_grace_seconds=0)["orphan_folders"] == 1 and not orphan.exists()


def test_a_purged_batch_can_no_longer_be_changed(logged_in, db):
    old = make_batch(db, 45)
    purge(db, days=30)
    html = logged_in.get(f"/batches/{old.id}").text
    assert "deleted after the retention period" in html and "Add more files" not in html
    r = logged_in.post(f"/batches/{old.id}/ready", data={"csrf_token": _token(logged_in), "checked": "on"}, follow_redirects=False)
    assert r.status_code == 303 and db.get(Batch, old.id).status == "draft"


def _token(client):
    from tests.conftest import csrf_from
    return csrf_from(client.get("/").text)


# ---- make_test_payslips ------------------------------------------------------------------------------------------

def test_the_generator_makes_every_case_from_the_current_employees(db, tmp_path):
    for first, last in [("Thabo", "Molefe"), ("Neo", "Dube"), ("Chipo", "Fakename"), ("Kea", "Quiet"), ("Ana", "Silva"),
                        ("Ben", "Sample"), ("Dina", "Demo"), ("Emma", "Mock"), ("Ana", "Silva")]:
        make_employee(db, first, last, "+26771234567", "1234")
    made = make_test_payslips.build(db, tmp_path / "out")
    whats = " ".join(w for _, w in made)
    for case in ("EXACT", "swapped", "TYPO", "MIDDLE NAME", "UNKNOWN", "SAME NAME", "PDF TEXT names another", "IMAGE-ONLY", "PASSWORD-PROTECTED", "NOT A PDF"):
        assert case in whats, case
    files = sorted(p.name for p in (tmp_path / "out" / "pdfs").iterdir())
    assert files == sorted(n for n, _ in made) and (tmp_path / "out" / "WHAT_TO_EXPECT.txt").exists()
    with zipfile.ZipFile(tmp_path / "out" / "test_payslips.zip") as zf:
        names = zf.namelist()
    assert any(n.startswith("__MACOSX/") for n in names) and any(n.endswith(".DS_Store") for n in names)
    assert sum(n.startswith("payslips/") and n.endswith(".pdf") for n in names) == len(made)


def test_the_generated_files_behave_as_labelled_when_uploaded(logged_in, db, tmp_path):
    from tests.batch_helpers import batch_id_of, by_name, create

    for first, last in [("Thabo", "Molefe"), ("Neo", "Dube"), ("Chipo", "Fakename"), ("Kea", "Quiet"), ("Ana", "Silva"),
                        ("Ben", "Sample"), ("Dina", "Demo"), ("Emma", "Mock"), ("Ana", "Silva"), ("Gift", "Trial"), ("Hope", "Duplicate")]:
        make_employee(db, first, last, "+26771234567", "1234")
    made = dict(make_test_payslips.build(db, tmp_path / "out"))
    files = [(p.name, p.read_bytes()) for p in sorted((tmp_path / "out" / "pdfs").iterdir())]
    rows = by_name(db, batch_id_of(create(logged_in, files)))
    status = {name: rows[name].match_status for name in made}
    for name, what in made.items():
        if what.startswith("EXACT"):
            assert status[name] == "exact", name
        elif what.startswith(("TYPO", "MIDDLE", "SAME NAME")):
            assert status[name] == "needs_review", name
        elif what.startswith("UNKNOWN"):
            assert status[name] == "unmatched", name
        elif what.startswith("PASSWORD") or what.startswith("NOT A PDF"):
            assert status[name] == "invalid", name
        elif what.startswith("FILE NAME says"):
            assert rows[name].pdf_check == "possible_mismatch", name
        elif what.startswith("IMAGE-ONLY"):
            assert rows[name].pdf_check == "no_text", name


def test_the_generator_needs_at_least_two_employees(db, tmp_path):
    import pytest

    make_employee(db, "Only", "One", "+26771234567", "1234")
    with pytest.raises(SystemExit):
        make_test_payslips.build(db, tmp_path / "out")


def test_generated_pdfs_hold_only_fake_data_and_the_output_folder_is_ignored_by_git():
    assert "test_payslips/" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")


# ---- migration ---------------------------------------------------------------------------------------------------

BEFORE, AFTER = "9b3f6c1d4e27", "c4a7e1d92b50"


def test_batches_migration_keeps_existing_batches_and_payslips(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migration3.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    saved_handlers, saved_level = logging.root.handlers[:], logging.root.level
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    engine = create_engine(url)
    try:
        command.upgrade(cfg, BEFORE)
        with engine.begin() as c:
            c.execute(text("INSERT INTO batches (pay_month_label, status, created_at, updated_at) VALUES "
                           "('October 2026', 'draft', '2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
            c.execute(text("INSERT INTO payslips (batch_id, original_filename, match_method, match_status, confirmed, pdf_check, status, "
                           "attempts, created_at, updated_at) VALUES (1, 'old_file.pdf', 'none', 'unmatched', 0, 'not_checked', 'pending', "
                           "0, '2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
        command.upgrade(cfg, AFTER)
        cols = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in ("batches", "payslips")}
        assert {"processing_state", "processing_total", "processing_done", "ready_at", "files_deleted_at"} <= cols["batches"]
        assert {"page_count", "invalid_reason", "match_score", "suggestions", "text_names_employee_id"} <= cols["payslips"]
        with engine.connect() as c:
            assert c.execute(text("SELECT pay_month_label, status, processing_state, processing_total FROM batches")).all() == [("October 2026", "draft", "done", 0)]
            assert c.execute(text("SELECT original_filename, match_status, page_count, invalid_reason FROM payslips")).all() == [("old_file.pdf", "unmatched", None, None)]
        command.downgrade(cfg, BEFORE)
        with engine.connect() as c:
            assert c.execute(text("SELECT original_filename FROM payslips")).all() == [("old_file.pdf",)]
            assert c.execute(text("SELECT pay_month_label FROM batches")).all() == [("October 2026",)]
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()
        logging.root.handlers[:] = saved_handlers
        logging.root.setLevel(saved_level)
        monkeypatch.undo()
        get_settings.cache_clear()


# ---- sending engine migration ------------------------------------------------------------------------------------

SEND_BEFORE, SEND_AFTER = "c4a7e1d92b50", "e2a6d5b81f39"


def test_sending_migration_keeps_existing_batches_and_payslips(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migration4.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    saved_handlers, saved_level = logging.root.handlers[:], logging.root.level
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    engine = create_engine(url)
    try:
        command.upgrade(cfg, SEND_BEFORE)
        with engine.begin() as c:
            c.execute(text("INSERT INTO batches (pay_month_label, status, created_at, updated_at) VALUES "
                           "('October 2026', 'ready', '2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
            c.execute(text("INSERT INTO payslips (batch_id, original_filename, match_method, match_status, confirmed, pdf_check, status, "
                           "attempts, created_at, updated_at) VALUES (1, 'old_file.pdf', 'filename_exact', 'exact', 1, 'ok', 'pending', "
                           "0, '2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
        command.upgrade(cfg, SEND_AFTER)
        cols = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in ("batches", "payslips")}
        assert {"send_state", "send_mode", "halted_reason", "stopped_at", "completed_at"} <= cols["batches"] and "error_code" in cols["payslips"]
        with engine.connect() as c:
            assert c.execute(text("SELECT pay_month_label, status, send_state, send_mode FROM batches")).all() == [("October 2026", "ready", "idle", None)]
            assert c.execute(text("SELECT original_filename, status, match_status, error_code FROM payslips")).all() == [("old_file.pdf", "pending", "exact", None)]
        command.downgrade(cfg, SEND_BEFORE)
        with engine.connect() as c:
            assert c.execute(text("SELECT original_filename, status FROM payslips")).all() == [("old_file.pdf", "pending")]
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()
        logging.root.handlers[:] = saved_handlers
        logging.root.setLevel(saved_level)
        monkeypatch.undo()
        get_settings.cache_clear()
