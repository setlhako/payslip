"""Decides what an import would do (New / Updated / Unchanged / Error) and applies it.

`plan_import` only READS the database. It is run once for the preview and again at confirm time against the
then-current data, so the saved result is always consistent. `apply_import` changes the session but does NOT
commit: the caller commits once, so the whole import is a single transaction.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Employee
from app.services.employees import apply_values
from app.services.id_crypto import IdCryptoError, decrypt_id
from app.services.import_parser import ParsedRow
from app.services.names import name_key

NEW, UPDATE, UNCHANGED, ERROR = "new", "update", "unchanged", "error"


@dataclass
class PlannedRow:
    row: ParsedRow
    action: str
    employee_id: int | None = None
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def plan_import(db: Session, rows: list[ParsedRow]) -> list[PlannedRow]:
    employees = list(db.scalars(select(Employee)))
    by_id = {e.id: e for e in employees}
    by_code = {e.employee_code.lower(): e for e in employees if e.employee_code}
    by_key: dict[str, list[Employee]] = defaultdict(list)
    by_phone: dict[str, list[Employee]] = defaultdict(list)
    for e in employees:
        by_key[e.name_key].append(e)
        if e.phone:
            by_phone[e.phone].append(e)

    planned: list[PlannedRow] = []
    keys: dict[int, str] = {}
    matched_by_code: set[int] = set()  # row numbers that matched an existing employee through employee_code

    # 1. Find which existing employee (if any) each row refers to.
    for row in rows:
        p = PlannedRow(row=row, action=NEW, errors=list(row.errors), warnings=list(row.warnings))
        planned.append(p)
        if p.errors:
            p.action = ERROR
            continue
        keys[row.row_number] = name_key(row.first_name, row.surname)
        if row.employee_code:
            existing = by_code.get(row.employee_code.lower())
            if existing:
                p.employee_id = existing.id
                p.action = UPDATE
                matched_by_code.add(row.row_number)
        else:
            matches = by_key.get(keys[row.row_number], [])
            if len(matches) > 1:
                p.errors.append(
                    f"{len(matches)} existing employees have this name. Add an employee_code to this row to say which one."
                )
            elif matches:
                p.employee_id = matches[0].id
                p.action = UPDATE

    live = [p for p in planned if not p.errors]

    # 2. Problems between rows of the same file.
    code_rows: dict[str, list[int]] = defaultdict(list)
    name_rows: dict[str, list[int]] = defaultdict(list)
    target_rows: dict[int, list[int]] = defaultdict(list)
    for p in live:
        if p.row.employee_code:
            code_rows[p.row.employee_code.lower()].append(p.row.row_number)
        else:
            name_rows[keys[p.row.row_number]].append(p.row.row_number)
        if p.employee_id:
            target_rows[p.employee_id].append(p.row.row_number)
    for p in live:
        n = p.row.row_number
        if p.row.employee_code and len(code_rows[p.row.employee_code.lower()]) > 1:
            others = [r for r in code_rows[p.row.employee_code.lower()] if r != n]
            p.errors.append(f"Employee code is also used on row {_rows(others)}. Each code must be unique.")
        if not p.row.employee_code and len(name_rows[keys[n]]) > 1:
            others = [r for r in name_rows[keys[n]] if r != n]
            p.errors.append(
                f"The same name also appears on row {_rows(others)} and there is no employee_code. "
                "Add an employee_code to each of these rows so they can be told apart."
            )
        if p.employee_id and len(target_rows[p.employee_id]) > 1:
            others = [r for r in target_rows[p.employee_id] if r != n]
            p.errors.append(f"Row {_rows(others)} would update the same employee as this row.")

    # 3. Work out the changes and the warnings for rows that are still fine.
    in_file_phone: dict[str, list[int]] = defaultdict(list)
    for p in planned:
        if not p.errors and p.row.phone:
            in_file_phone[p.row.phone].append(p.row.row_number)

    for p in planned:
        if p.errors:
            p.action = ERROR
            continue
        row, existing = p.row, by_id.get(p.employee_id) if p.employee_id else None
        if existing is None:
            p.action = NEW
            if row.employee_code and by_key.get(keys[row.row_number]):
                same = by_key[keys[row.row_number]][0]
                p.warnings.append(
                    f"An employee named {same.full_name} already exists (employee code: {same.employee_code or 'none'}). "
                    "A NEW employee will be created. If it is the same person, cancel and fix the code first."
                )
            final_phone, final_id = row.phone, row.id_last4
        else:
            if row.row_number in matched_by_code:
                if row.first_name != existing.first_name:
                    p.changes.append(f"First name changes from '{existing.first_name}' to '{row.first_name}'.")
                if row.surname != existing.surname:
                    p.changes.append(f"Surname changes from '{existing.surname}' to '{row.surname}'.")
            if row.phone and row.phone != existing.phone:
                old = f"from {existing.phone} " if existing.phone else ""
                p.changes.append(f"Phone changes {old}to {row.phone}." if old else f"Phone will be set to {row.phone}.")
            if row.id_last4:
                if not existing.id_last4:
                    p.changes.append(f"ID will be set (type: {row.id_type}).")
                else:
                    id_changes = []
                    if row.id_last4 != existing.id_last4:
                        id_changes.append("ID number (last 4 characters, the PDF password) will change.")
                    if row.id_type != existing.id_type:
                        id_changes.append(f"ID type changes from {existing.id_type} to {row.id_type}.")
                    if not existing.id_number_encrypted:
                        id_changes.append("The full ID number will be stored (it was not imported before).")
                    elif not id_changes and _full_id_differs(existing, row):
                        id_changes.append("The full ID number will change.")
                    p.changes += id_changes
            p.action = UPDATE if p.changes else UNCHANGED
            if not existing.active:
                p.warnings.append("This employee is currently inactive. The import does not reactivate anyone.")
            final_phone = row.phone or existing.phone
            final_id = row.id_last4 or existing.id_last4

        if not final_phone:
            p.warnings.append("No phone number: this employee cannot receive payslips until one is added.")
        if not final_id:
            p.warnings.append("No ID number: this employee's payslip cannot be password-protected until it is added.")
        if row.phone:
            others = [r for r in in_file_phone[row.phone] if r != row.row_number]
            if others:
                p.warnings.append(f"Same phone number as row {_rows(others)} in this file.")
            clash = [e for e in by_phone.get(row.phone, []) if e.id != p.employee_id]
            if clash:
                p.warnings.append(f"Same phone number as existing employee {clash[0].full_name}.")
    return planned


def _full_id_differs(existing: Employee, row: ParsedRow) -> bool:
    """Is the full ID in the file different from the stored one? Both are decrypted in memory only for this comparison."""
    try:
        return decrypt_id(existing.id_number_encrypted) != decrypt_id(row.id_encrypted)
    except IdCryptoError:
        return True   # the stored value is unreadable (e.g. an old key): replacing it is the right thing


def _rows(numbers: list[int]) -> str:
    shown = ", ".join(str(n) for n in numbers[:5])
    return shown + (" ..." if len(numbers) > 5 else "")


def summarize(planned: list[PlannedRow]) -> dict[str, int]:
    return {
        "new": sum(p.action == NEW for p in planned),
        "updated": sum(p.action == UPDATE for p in planned),
        "unchanged": sum(p.action == UNCHANGED for p in planned),
        "errors": sum(p.action == ERROR for p in planned),
        "warnings": sum(bool(p.warnings) and p.action != ERROR for p in planned),
        "total": len(planned),
    }


def apply_import(db: Session, planned: list[PlannedRow]) -> dict[str, int]:
    """Create/update employees for the valid rows. Rows with errors are skipped. The caller commits."""
    for p in planned:
        if p.action == NEW:
            employee = Employee(active=True)
            apply_values(employee, p.row.first_name, p.row.surname, p.row.phone, p.row.id_type, p.row.id_last4,
                         p.row.employee_code, id_number_encrypted=p.row.id_encrypted)
            db.add(employee)
        elif p.action == UPDATE:
            employee = db.get(Employee, p.employee_id)
            by_code = bool(p.row.employee_code)
            apply_values(
                employee,
                p.row.first_name if by_code else employee.first_name,
                p.row.surname if by_code else employee.surname,
                p.row.phone or employee.phone,
                p.row.id_type if p.row.id_last4 else employee.id_type,
                p.row.id_last4 or employee.id_last4,
                employee.employee_code,
                id_number_encrypted=p.row.id_encrypted or employee.id_number_encrypted,
            )
    return summarize(planned)
