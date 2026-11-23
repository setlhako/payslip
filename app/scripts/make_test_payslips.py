"""Make FAKE payslip PDFs to try the Batches feature. Run: python -m app.scripts.make_test_payslips [--out FOLDER]

Built from the names of the employees currently in your database (names only: no phone numbers or IDs are read).
Writes, inside test_payslips/ (git-ignored):
    pdfs/                  the individual test PDFs
    test_payslips.zip      the same files in one ZIP (inside a folder, plus junk entries that must be ignored)
    WHAT_TO_EXPECT.txt     which file tests which case, and what the review screen should say
All content is invented ("FAKE TEST DATA"). Needs fpdf2 (a dev dependency: pip install -r requirements-dev.txt).
"""
import argparse
import io
import re
import sys
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT
from app.database import SessionLocal
from app.models import Employee
from app.services.names import name_key

PASSWORD = "test1234"


def _latin(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


def text_pdf(lines: list[str]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "PAYSLIP (FAKE TEST DATA)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=12)
    for line in lines:
        pdf.cell(0, 8, _latin(line), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def image_only_pdf(label: str) -> bytes:
    """A PDF whose only content is a picture of text: there is no text layer to read (like a scan)."""
    from fpdf import FPDF
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (700, 220), "white")
    ImageDraw.Draw(image).text((20, 20), f"PAYSLIP (FAKE)\n{_latin(label)}", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    pdf = FPDF()
    pdf.add_page()
    pdf.image(buffer, x=10, y=10, w=180)
    return bytes(pdf.output())


def protected_pdf(lines: list[str]) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(text_pdf(lines))))
    writer.encrypt(PASSWORD)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _slug(text: str) -> str:
    plain = re.sub(r"[^A-Za-z0-9]+", "_", _latin(text)).strip("_")
    return plain or "x"


def _body(first: str, surname: str) -> list[str]:
    return [f"Employee: {first} {surname}", "Pay month: November 2026", "Gross pay: P 12,345.00", "Net pay: P 10,100.00"]


def build(db: Session, out: Path) -> list[tuple[str, str]]:
    """Write the files. Returns [(file name, what it tests)]."""
    employees = list(db.scalars(select(Employee).where(Employee.active.is_(True)).order_by(Employee.id)))
    if len(employees) < 2:
        raise SystemExit("Add at least 2 active employees first (Employees > Import), then run this again.")

    pdf_dir = out / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    for old in pdf_dir.glob("*.pdf"):
        old.unlink()
    made: list[tuple[str, str]] = []
    blobs: dict[str, bytes] = {}
    pointer = 0
    by_key: dict[str, list[Employee]] = {}
    for emp in employees:
        by_key.setdefault(name_key(emp.first_name, emp.surname), []).append(emp)
    # People who share a name can never be matched automatically, so the "exact" style cases use people with a unique name.
    unique = [emp for emp in employees if len(by_key[name_key(emp.first_name, emp.surname)]) == 1]
    pool = unique if len(unique) >= 2 else employees

    def take() -> Employee:
        nonlocal pointer
        e = pool[pointer % len(pool)]
        pointer += 1
        return e

    def add(name: str, data: bytes, what: str) -> None:
        blobs[name] = data
        made.append((name, what))

    for _ in range(3):
        e = take()
        add(f"{_slug(e.first_name)}_{_slug(e.surname)}.pdf", text_pdf(_body(e.first_name, e.surname)),
            "EXACT match (first_surname). Should be auto-matched.")
    for _ in range(2):
        e = take()
        add(f"{_slug(e.surname)}_{_slug(e.first_name)}.pdf", text_pdf(_body(e.first_name, e.surname)),
            "EXACT match with the name order swapped (surname_first).")
    e = take()
    typo = e.surname[:-1] if len(e.surname) > 3 else e.surname + e.surname[-1]
    add(f"{_slug(e.first_name)}_{_slug(typo)}.pdf", text_pdf(_body(e.first_name, e.surname)),
        "TYPO in the surname. Should be NEEDS REVIEW with a suggestion and a score.")
    e = take()
    add(f"{_slug(e.first_name)}_Mpho_{_slug(e.surname)}.pdf", text_pdf(_body(e.first_name, e.surname)),
        "MIDDLE NAME in the file name. Should be NEEDS REVIEW.")
    add("Quentin_Zzyzx.pdf", text_pdf(_body("Quentin", "Zzyzx")), "UNKNOWN person. Should be UNMATCHED.")

    shared = next((v for v in by_key.values() if len(v) > 1), None)
    if shared is None:
        skipped_shared = True
    else:
        skipped_shared = False
        add(f"{_slug(shared[0].first_name)}_{_slug(shared[0].surname)}.pdf", text_pdf(_body(shared[0].first_name, shared[0].surname)),
            "SAME NAME shared by 2+ employees. Should be NEEDS REVIEW (you choose who).")

    a, b = take(), take()
    add(f"{_slug(a.first_name)}_{_slug(a.surname)}_2.pdf", text_pdf(_body(b.first_name, b.surname)),
        "FILE NAME says one person but the PDF TEXT names another. Should be POSSIBLE MISMATCH.")
    # (the _2 / _3 / _4 numbers keep file names unique; digits are ignored when matching, so each is still an EXACT name match)
    a = take()
    add(f"{_slug(a.first_name)}_{_slug(a.surname)}_3.pdf", image_only_pdf(f"{a.first_name} {a.surname}"),
        "IMAGE-ONLY PDF (no text). Matches by name only; shows 'could not verify against PDF content'.")
    a = take()
    add(f"{_slug(a.first_name)}_{_slug(a.surname)}_4.pdf", protected_pdf(_body(a.first_name, a.surname)),
        f"PASSWORD-PROTECTED PDF (password {PASSWORD}). Should be INVALID FILE.")
    add("Not_Really_A_Pdf.pdf", b"This is just a text file with a .pdf name.\n", "NOT A PDF renamed to .pdf. Should be INVALID FILE.")

    for name, data in blobs.items():
        (pdf_dir / name).write_bytes(data)

    zip_path = out / "test_payslips.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in blobs.items():
            zf.writestr(f"payslips/{name}", data)                 # inside a folder: it is flattened on upload
        zf.writestr("__MACOSX/payslips/._junk.pdf", b"mac junk")   # must be ignored
        zf.writestr("payslips/.DS_Store", b"junk")                 # hidden file: ignored
    notes = ["Files made for the Batches feature (all FAKE).", ""]
    notes += [f"{name}\n    {what}" for name, what in made]
    if skipped_shared:
        notes += ["", "No two of your employees share a name, so the 'same name' case was skipped. Add two people with the same",
                  "name (and different employee codes) and run this again to try it."]
    notes += ["", f"Active employees in your list: {len(employees)}. Employees with no file here will appear under",
              "'Active employees with no payslip in this batch'. With fewer than ~11 employees some people get two files,",
              "which also shows 'Another file in this batch is matched to the same employee'.",
              "The ZIP holds the same PDFs inside a folder, plus junk entries (__MACOSX, .DS_Store) that are ignored."]
    (out / "WHAT_TO_EXPECT.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make fake test payslip PDFs from your current employees' names.")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "test_payslips"), help="output folder (default: test_payslips)")
    args = parser.parse_args(argv)
    try:
        import fpdf  # noqa: F401
    except ImportError:
        print("fpdf2 is missing. Run:  pip install -r requirements-dev.txt")
        return 1
    out = Path(args.out)
    with SessionLocal() as db:
        made = build(db, out)
    print(f"Made {len(made)} fake PDFs in {out / 'pdfs'} and the ZIP {out / 'test_payslips.zip'}.")
    for name, what in made:
        print(f"  {name}\n      {what}")
    print(f"The same list is in {out / 'WHAT_TO_EXPECT.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
