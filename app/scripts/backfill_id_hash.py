"""Fill in `employees.id_number_hash` for employees saved before duplicate-ID checks existed, and report duplicates.

Run (project root):  python -m app.scripts.backfill_id_hash      On the server:  deploy/app-run.sh app.scripts.backfill_id_hash

What it does, in plain words:
  1. For every employee with a stored full ID number it decrypts the number IN MEMORY, computes its blind index
     (HMAC-SHA256, see services/id_crypto.py) and saves only that. Nothing is printed or logged about the number.
  2. It lists employees who share an ID number or a phone number: NAMES AND EMPLOYEE IDS ONLY, never the values.
  3. It does NOT make anything unique. That is the next migration (`python -m alembic upgrade head`), which refuses
     to run until there are no duplicates. Fix duplicates in the app (edit the phone, or tick "Remove the saved ID
     number" on the edit page), run this command again, then run the migration.
Safe to run any number of times. Exit code 0 = clean, 1 = something needs your attention.
"""
import sys
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Employee
from app.services import audit
from app.services.id_crypto import IdCipher, IdCryptoError, get_cipher


@dataclass
class BackfillReport:
    hashed_now: int = 0                                   # rows whose hash was filled in or corrected
    already_ok: int = 0
    unreadable: list[tuple[int, str]] = field(default_factory=list)       # (employee id, name): could not be decrypted
    duplicate_ids: list[list[tuple[int, str]]] = field(default_factory=list)      # groups of (employee id, name)
    duplicate_phones: list[list[tuple[int, str]]] = field(default_factory=list)
    unique_index_refused: bool = False

    @property
    def clean(self) -> bool:
        return not (self.unreadable or self.duplicate_ids or self.duplicate_phones or self.unique_index_refused)


def backfill(db: Session, cipher: IdCipher) -> BackfillReport:
    """Compute hashes (decrypting in memory), then look for duplicates. Commits once."""
    report = BackfillReport()
    employees = list(db.scalars(select(Employee).order_by(Employee.id)))
    for e in employees:
        if not e.id_number_encrypted:
            continue
        try:
            wanted = cipher.blind_index(cipher.decrypt(e.id_number_encrypted))
        except IdCryptoError:
            report.unreadable.append((e.id, e.full_name))
            continue
        if e.id_number_hash == wanted:
            report.already_ok += 1
        else:
            e.id_number_hash = wanted
            report.hashed_now += 1
    try:
        audit.record(db, "id_hashes_backfilled", details=f"hashed={report.hashed_now} unreadable={len(report.unreadable)}",
                     commit=False)
        db.commit()
    except IntegrityError:
        # a UNIQUE index already exists and two rows want the same hash: the database refused, nothing was saved
        db.rollback()
        report.unique_index_refused = True

    by_hash: dict[str, list[Employee]] = defaultdict(list)
    by_phone: dict[str, list[Employee]] = defaultdict(list)
    for e in employees:
        if report.unique_index_refused:
            break
        if e.id_number_hash:
            by_hash[e.id_number_hash].append(e)
    for e in employees:
        if e.phone:
            by_phone[e.phone].append(e)
    report.duplicate_ids = [[(e.id, e.full_name) for e in g] for g in by_hash.values() if len(g) > 1]
    report.duplicate_phones = [[(e.id, e.full_name) for e in g] for g in by_phone.values() if len(g) > 1]
    return report


def _print_groups(title: str, groups: list[list[tuple[int, str]]]) -> None:
    print(f"\n{title}: {len(groups)} group(s)")
    for group in groups:
        print("  - " + "; ".join(f"employee #{i} ({name})" for i, name in group))


def main() -> int:
    with SessionLocal() as db:
        report = backfill(db, get_cipher())
    print(f"Hashed {report.hashed_now} employee(s); {report.already_ok} already had the right hash.")
    if report.unreadable:
        print(f"\n{len(report.unreadable)} stored ID(s) could not be decrypted (wrong or missing ID_ENCRYPTION_KEY?):")
        for i, name in report.unreadable:
            print(f"  - employee #{i} ({name})")
        print("  Put the old key in ID_ENCRYPTION_KEY_PREVIOUS, or import those employees again to refill their IDs.")
    if report.unique_index_refused:
        print("\nThe database refused to save the hashes because two employees have the same ID number.")
        print("Nothing was changed. Find them with the unique-index error in the app, fix them, and run this again.")
    if report.duplicate_ids:
        _print_groups("Employees sharing one ID number", report.duplicate_ids)
        print("  Fix: edit one of them and enter the right ID number, or tick 'Remove the saved ID number'.")
    if report.duplicate_phones:
        _print_groups("Employees sharing one phone number", report.duplicate_phones)
        print("  Fix: edit one of them and enter the right phone number (or clear it).")
    if report.clean:
        print("\nNo duplicates found. Now run:  python -m alembic upgrade head")
        return 0
    print("\nThe unique indexes are NOT applied yet. When everything above is fixed, run this command again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
