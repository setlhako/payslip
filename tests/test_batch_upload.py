"""Upload validation, limits and ZIP safety (service level), and where files end up on disk."""
import io
import re
import zipfile

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import Batch, Payslip
from app.services import batch_ingest, batch_storage
from app.services.batch_ingest import UploadRejected, ingest_pdfs, ingest_zip, safe_display_name
from tests.pdfs import locked_pdf, named_pdf, pdf_with, upload_obj

UUID_PDF = re.compile(r"^[0-9a-f]{32}\.pdf$")


@pytest.fixture
def batch(db):
    b = Batch(pay_month_label="November 2026")
    db.add(b)
    db.commit()
    return b


def rows(db, batch):
    return list(db.scalars(select(Payslip).where(Payslip.batch_id == batch.id).order_by(Payslip.id)))


def files_on_disk(batch):
    d = batch_storage.batch_dir(batch.id)
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def make_zip(entries: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> object:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return upload_obj("payslips.zip", buf.getvalue())


# ---- one PDF -----------------------------------------------------------------------------------------------------

def test_a_valid_pdf_is_stored_under_a_random_uuid_name_in_the_batch_folder(db, batch):
    result = ingest_pdfs(db, batch, [upload_obj("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))])
    (p,) = rows(db, batch)
    assert (result.accepted, result.invalid) == (1, 0)
    assert p.original_filename == "Thabo_Molefe.pdf"                       # the real name lives in the database only
    assert UUID_PDF.match(p.stored_filename) and p.stored_filename != p.original_filename
    assert files_on_disk(batch) == [p.stored_filename]                     # exactly that one file, nothing else
    assert p.file_size > 0 and len(p.file_sha256) == 64 and p.page_count == 1
    assert batch_storage.batch_dir(batch.id).name == str(batch.id)         # per-batch subfolder
    assert p.match_status == "pending"


def test_no_file_is_ever_stored_under_its_original_name(db, batch):
    names = ["Thabo_Molefe.pdf", "molefe_thabo.pdf", "../../evil.pdf", "C:\\Windows\\x.pdf", "Neo Dube (2).pdf"]
    ingest_pdfs(db, batch, [upload_obj(n, named_pdf("A", "B")) for n in names])
    on_disk = files_on_disk(batch)
    assert len(on_disk) == 5 and all(UUID_PDF.match(n) for n in on_disk)
    stored = {p.stored_filename for p in rows(db, batch)}
    assert stored == set(on_disk)
    for p in rows(db, batch):
        assert Path_name(p.original_filename) not in on_disk


def Path_name(name: str) -> str:
    return re.split(r"[\\/]+", name)[-1]


def test_nothing_outside_the_private_folder_is_written_and_it_is_not_public(db, batch, tmp_path):
    ingest_pdfs(db, batch, [upload_obj("a.pdf", named_pdf("A", "B"))])
    assert batch_storage.storage_is_private()
    root = get_settings().storage_path.resolve()
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written and all(root in p.resolve().parents for p in written)


def test_stored_path_refuses_anything_that_is_not_a_uuid_name(batch):
    assert batch_storage.stored_path(batch.id, "../../etc/passwd") is None
    assert batch_storage.stored_path(batch.id, "thabo_molefe.pdf") is None
    assert batch_storage.stored_path(batch.id, None) is None
    assert batch_storage.stored_path(batch.id, "0" * 32 + ".pdf") is not None


# ---- bad PDFs are flagged (and never stored) ---------------------------------------------------------------------

@pytest.mark.parametrize("name,data,reason", [
    ("empty.pdf", b"", "empty"),
    ("fake.pdf", b"This is a text file", "not a real PDF"),
    ("html.pdf", b"<html>%PDF-</html>", "not a real PDF"),
    ("corrupt.pdf", b"%PDF-1.4\n1 0 obj << /Broken", "damaged"),
    ("truncated.pdf", named_pdf("A", "B")[:300], "damaged"),
    ("locked.pdf", locked_pdf("secret"), "password-protected"),
])
def test_bad_pdfs_are_flagged_with_a_plain_reason_and_not_stored(db, batch, name, data, reason):
    result = ingest_pdfs(db, batch, [upload_obj(name, data), upload_obj("good.pdf", named_pdf("A", "B"))])
    bad, good = rows(db, batch)
    assert (result.accepted, result.invalid) == (1, 1)
    assert bad.match_status == "invalid" and reason in bad.invalid_reason and bad.stored_filename is None
    assert good.match_status == "pending" and files_on_disk(batch) == [good.stored_filename]


def test_non_pdf_extensions_are_flagged_without_reading_them(db, batch):
    ingest_pdfs(db, batch, [upload_obj("notes.docx", b"x"), upload_obj("noext", b"x"), upload_obj("pack.zip", b"x")])
    docx, noext, archive = rows(db, batch)
    assert "Not a PDF" in docx.invalid_reason and ".docx" in docx.invalid_reason
    assert noext.invalid_reason == "Not a PDF file."
    assert "ZIP" in archive.invalid_reason and files_on_disk(batch) == []


def test_a_pdf_over_the_size_limit_is_flagged_not_stored(db, batch, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 0)
    ingest_pdfs(db, batch, [upload_obj("big.pdf", named_pdf("A", "B"))])
    (p,) = rows(db, batch)
    assert p.match_status == "invalid" and "larger than 0 MB" in p.invalid_reason and files_on_disk(batch) == []


def test_nothing_chosen_is_refused(db, batch):
    with pytest.raises(UploadRejected):
        ingest_pdfs(db, batch, [])
    with pytest.raises(UploadRejected):
        ingest_pdfs(db, batch, [upload_obj("", b"")])
    with pytest.raises(UploadRejected):
        ingest_zip(db, batch, None)


# ---- batch-wide limits reject the whole upload and clean up ----------------------------------------------------

def test_too_many_files_rejects_the_upload_and_writes_nothing(db, batch, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_files_per_batch", 2)
    with pytest.raises(UploadRejected, match="Too many files"):
        ingest_pdfs(db, batch, [upload_obj(f"{i}.pdf", named_pdf("A", "B")) for i in range(3)])
    db.rollback()
    assert rows(db, batch) == [] and files_on_disk(batch) == []


def test_the_file_limit_counts_files_already_in_the_batch(db, batch, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_files_per_batch", 3)
    ingest_pdfs(db, batch, [upload_obj(f"{i}.pdf", named_pdf("A", "B")) for i in range(2)])
    with pytest.raises(UploadRejected):
        ingest_pdfs(db, batch, [upload_obj(f"x{i}.pdf", named_pdf("A", "B")) for i in range(2)])
    assert len(rows(db, batch)) == 2 and len(files_on_disk(batch)) == 2       # the first upload is untouched


def test_total_size_limit_rejects_and_removes_what_this_upload_wrote(db, batch, monkeypatch):
    first = named_pdf("A", "B")
    ingest_pdfs(db, batch, [upload_obj("first.pdf", first)])
    before = files_on_disk(batch)
    monkeypatch.setattr(get_settings(), "max_batch_total_mb", 0)
    with pytest.raises(UploadRejected, match="larger than"):
        ingest_pdfs(db, batch, [upload_obj("second.pdf", named_pdf("C", "D"))])
    assert files_on_disk(batch) == before


# ---- ZIP files -------------------------------------------------------------------------------------------------

def test_zip_of_pdfs_is_unpacked_flattened_and_stored_by_uuid(db, batch):
    z = make_zip({"payslips/Thabo_Molefe.pdf": named_pdf("Thabo", "Molefe"), "deep/er/neo_dube.pdf": named_pdf("Neo", "Dube")})
    result = ingest_zip(db, batch, z)
    assert result.accepted == 2
    assert sorted(p.original_filename for p in rows(db, batch)) == ["Thabo_Molefe.pdf", "neo_dube.pdf"]   # folders removed
    assert all(UUID_PDF.match(n) for n in files_on_disk(batch)) and len(files_on_disk(batch)) == 2


def test_zip_ignores_folders_hidden_files_and_macosx(db, batch):
    z = make_zip({
        "payslips/": b"", "__MACOSX/payslips/._thabo_molefe.pdf": b"junk", ".DS_Store": b"x", "payslips/.hidden.pdf": b"x",
        "Thumbs.db": b"x", "payslips/thabo_molefe.pdf": named_pdf("Thabo", "Molefe"),
    })
    result = ingest_zip(db, batch, z)
    assert (result.accepted, result.invalid, result.ignored) == (1, 0, 5)
    assert [p.original_filename for p in rows(db, batch)] == ["thabo_molefe.pdf"]


@pytest.mark.parametrize("evil", [
    "../evil.pdf", "../../evil.pdf", "a/../../evil.pdf", "/etc/evil.pdf", "\\evil.pdf", "C:\\Windows\\evil.pdf",
    "C:/evil.pdf", "a\\..\\..\\evil.pdf", "ok/../../x/evil.pdf",
])
def test_zip_path_traversal_is_rejected_and_nothing_is_written(db, batch, tmp_path, evil):
    z = make_zip({"good.pdf": named_pdf("A", "B"), evil: named_pdf("E", "Vil")})
    with pytest.raises(UploadRejected, match="unsafe"):
        ingest_zip(db, batch, z)
    db.rollback()
    assert files_on_disk(batch) == [] and not any(p.name.startswith("evil") for p in tmp_path.rglob("*"))


@pytest.mark.parametrize("inner", ["inner.zip", "folder/archive.rar", "x.7z", "stuff.tar.gz", "a.tgz"])
def test_nested_archives_are_rejected(db, batch, inner):
    with pytest.raises(UploadRejected, match="Nested archives"):
        ingest_zip(db, batch, make_zip({"good.pdf": named_pdf("A", "B"), inner: b"PK\x03\x04"}))
    db.rollback()
    assert files_on_disk(batch) == []


def test_pdf_pretending_to_be_a_zip_inside_a_zip_is_just_an_invalid_file(db, batch):
    result = ingest_zip(db, batch, make_zip({"sneaky.pdf": b"PK\x03\x04 not a pdf"}))
    assert (result.accepted, result.invalid) == (0, 1) and files_on_disk(batch) == []


def test_zip_bomb_by_declared_size_is_rejected(db, batch, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_unzipped_mb", 1)
    bomb = make_zip({"a.pdf": b"%PDF-" + b"\0" * (3 * 1024 * 1024)})        # 3 MB of zeros: tiny when zipped
    with pytest.raises(UploadRejected, match="far too much"):
        ingest_zip(db, batch, bomb)
    assert files_on_disk(batch) == []


def test_zip_bomb_by_expansion_ratio_is_rejected(db, batch):
    bomb = make_zip({"a.pdf": b"%PDF-" + b"\0" * (8 * 1024 * 1024)})        # 8 MB of zeros: thousands of times its zip size
    with pytest.raises(UploadRejected, match="zip bomb"):
        ingest_zip(db, batch, bomb)
    assert files_on_disk(batch) == []


def test_many_small_entries_adding_up_are_caught_by_the_total_guard(db, batch, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_unzipped_mb", 1)
    entries = {f"f{i}.pdf": b"%PDF-" + b"\0" * 400_000 for i in range(4)}       # each fine alone, 1.6 MB together
    with pytest.raises(UploadRejected, match="far too much"):
        ingest_zip(db, batch, make_zip(entries, zipfile.ZIP_STORED))


def test_too_many_zip_entries_are_refused(db, batch, monkeypatch):
    monkeypatch.setattr(batch_ingest, "MAX_ZIP_ENTRIES", 3)
    with pytest.raises(UploadRejected, match="too many entries"):
        ingest_zip(db, batch, make_zip({f"{i}.pdf": named_pdf("A", "B") for i in range(4)}))


def test_zip_file_count_limit_applies(db, batch, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_files_per_batch", 2)
    with pytest.raises(UploadRejected, match="Too many files"):
        ingest_zip(db, batch, make_zip({f"{i}.pdf": named_pdf("A", "B") for i in range(3)}))
    assert files_on_disk(batch) == []


def test_password_protected_zip_is_rejected(db, batch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.pdf", b"data")
    raw = bytearray(buf.getvalue())
    raw[raw.index(b"PK") + 8] |= 0x01          # set the "encrypted" flag in the central directory (python won't write it)
    with pytest.raises(UploadRejected, match="password-protected"):
        ingest_zip(db, batch, upload_obj("p.zip", bytes(raw)))


@pytest.mark.parametrize("data", [b"", b"not a zip at all", b"PK\x03\x04 broken"])
def test_a_file_that_is_not_a_zip_is_refused(db, batch, data):
    with pytest.raises(UploadRejected, match="not a valid ZIP"):
        ingest_zip(db, batch, upload_obj("x.zip", data))


def test_an_empty_zip_or_one_with_only_junk_is_refused(db, batch):
    with pytest.raises(UploadRejected, match="no files"):
        ingest_zip(db, batch, make_zip({"folder/": b"", ".DS_Store": b"x"}))


def test_non_pdf_files_inside_a_zip_are_flagged_and_bad_pdfs_too(db, batch):
    ingest_zip(db, batch, make_zip({"a.pdf": named_pdf("A", "B"), "notes.docx": b"x", "fake.pdf": b"hello", "locked.pdf": locked_pdf("s")}))
    by = {p.original_filename: p for p in rows(db, batch)}
    assert by["a.pdf"].match_status == "pending"
    assert all(by[n].match_status == "invalid" for n in ("notes.docx", "fake.pdf", "locked.pdf"))
    assert len(files_on_disk(batch)) == 1


# ---- names -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("a/b/c.pdf", "c.pdf"), ("a\\b\\c.pdf", "c.pdf"), ("../x.pdf", "x.pdf"), ("  spaced name.pdf ", "spaced name.pdf"),
    ("tab\there.pdf", "tabhere.pdf"), ("", "unnamed"), (None, "unnamed"), ("/", "unnamed"),
])
def test_safe_display_name(raw, expected):
    assert safe_display_name(raw) == expected


def test_very_long_names_are_shortened_but_keep_the_extension():
    name = safe_display_name("x" * 400 + ".pdf")
    assert len(name) == 255 and name.endswith(".pdf")


def test_duplicate_file_names_are_both_kept_so_they_can_be_flagged(db, batch):
    ingest_pdfs(db, batch, [upload_obj("same.pdf", named_pdf("A", "B")), upload_obj("same.pdf", named_pdf("A", "B"))])
    assert [p.original_filename for p in rows(db, batch)] == ["same.pdf", "same.pdf"]
    assert len(set(files_on_disk(batch))) == 2


def test_private_storage_is_not_inside_the_public_static_folder():
    assert batch_storage.storage_is_private()
