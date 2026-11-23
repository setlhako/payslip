"""Employee rules shared by the web forms and the importer."""
import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Employee
from app.services.id_crypto import encrypt_id
from app.services.ids import parse_id, parse_id_type
from app.services.names import clean_display_name, name_key, name_words
from app.services.phones import normalise_phone

PER_PAGE_CHOICES = (10, 25, 50, 100)
DEFAULT_PER_PAGE = 25
SORTS = ("name", "status")


def apply_values(
    employee: Employee, first_name: str, surname: str, phone: str | None,
    id_type: str | None, id_last4: str | None, code: str | None, *, id_number_encrypted: str | None,
) -> None:
    """Set the fields on an employee. The only place names are written, so name_key is always kept in step.

    id_number_encrypted has no default on purpose: to KEEP the stored full ID, pass `employee.id_number_encrypted`.
    """
    employee.first_name = first_name
    employee.surname = surname
    employee.name_key = name_key(first_name, surname)
    employee.phone = phone
    employee.id_type = id_type if id_last4 else None
    employee.id_last4 = id_last4
    employee.id_number_encrypted = id_number_encrypted if id_last4 else None
    employee.employee_code = code


# ---- add / edit form -------------------------------------------------------------------------------------------

@dataclass
class FormResult:
    values: dict = field(default_factory=dict)   # cleaned values to save
    shown: dict = field(default_factory=dict)    # what to put back in the form fields
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """Identifies this exact set of warnings, so 'Save anyway' only covers warnings HR has actually seen."""
        return hashlib.sha256("|".join(self.warnings).encode()).hexdigest()[:16]


def validate_form(db: Session, raw: dict[str, str], existing: Employee | None = None) -> FormResult:
    employee_id = existing.id if existing else None
    r = FormResult()
    first = clean_display_name(raw.get("first_name"))
    surname = clean_display_name(raw.get("surname"))
    r.shown = {"first_name": first, "surname": surname, "phone": (raw.get("phone") or "").strip()[:40],
               "id_type": "", "employee_code": (raw.get("employee_code") or "").strip()[:60]}

    for key, label, value in (("first_name", "First name", first), ("surname", "Surname", surname)):
        if not value:
            r.errors[key] = f"{label} is required."
        elif not name_words(value):
            r.errors[key] = f"{label} must contain letters."
        elif len(value) > 100:
            r.errors[key] = f"{label} is too long (maximum 100 characters)."

    phone = normalise_phone(raw.get("phone"))
    if phone.error:
        r.errors["phone"] = phone.error
    else:
        r.shown["phone"] = phone.value or ""
        r.warnings += phone.warnings

    # ---- ID (Omang / passport). The typed number is encrypted for saving and never shown back.
    chosen, type_error = parse_id_type(raw.get("id_type"))
    if type_error:
        r.errors["id_type"] = type_error
    r.shown["id_type"] = chosen or (existing.id_type if existing and existing.id_type else "")
    id_type = id_last4 = id_encrypted = None
    number_given = bool(re.sub(r"[\s\-]+", "", raw.get("id_number") or ""))
    if number_given:
        if not chosen and not type_error:
            r.errors["id_type"] = "Choose the ID type (Omang or passport)."
        elif chosen:
            res = parse_id(raw.get("id_number"), chosen)
            if res.error:
                r.errors["id_number"] = res.error
            else:
                id_type, id_last4, id_encrypted = res.id_type, res.last4, encrypt_id(res.number)
                r.warnings += res.warnings
    elif existing and existing.id_last4:
        if chosen and chosen != existing.id_type:
            r.errors["id_number"] = "To change the ID type, enter the ID number again."
        else:
            id_type, id_last4 = existing.id_type, existing.id_last4   # left blank = keep what is saved
            id_encrypted = existing.id_number_encrypted
    elif chosen:
        r.errors["id_number"] = "Enter the ID number, or set the ID type back to 'Not set'."

    code = (raw.get("employee_code") or "").strip()
    if len(code) > 50:
        r.errors["employee_code"] = "Employee code is too long (maximum 50 characters)."
    elif code and not code.isprintable():
        r.errors["employee_code"] = "Employee code contains invalid characters."
    elif code:
        clash = db.scalars(
            select(Employee).where(func.lower(Employee.employee_code) == code.lower(), Employee.id != (employee_id or 0))
        ).first()
        if clash:
            r.errors["employee_code"] = f"That employee code is already used by {clash.full_name}."

    if "first_name" not in r.errors and "surname" not in r.errors:
        same_name = db.scalars(
            select(Employee).where(Employee.name_key == name_key(first, surname), Employee.id != (employee_id or 0))
        ).all()
        if same_name:
            r.warnings.append(
                f"Another employee has the same name ({same_name[0].full_name}). Payslip files for this name "
                "will always need manual review."
            )
    if phone.value:
        same_phone = db.scalars(
            select(Employee).where(Employee.phone == phone.value, Employee.id != (employee_id or 0))
        ).all()
        if same_phone:
            r.warnings.append(f"The phone number {phone.value} is already used by {same_phone[0].full_name}.")

    r.values = {
        "first_name": first, "surname": surname, "phone": phone.value, "id_type": id_type,
        "id_last4": id_last4, "id_number_encrypted": id_encrypted, "employee_code": code or None,
    }
    return r


# ---- list / search / sort ---------------------------------------------------------------------------------------

@dataclass
class Page:
    items: list
    page: int
    pages: int
    total: int
    per_page: int

    @property
    def first(self) -> int:
        return (self.page - 1) * self.per_page + 1 if self.total else 0

    @property
    def last(self) -> int:
        return min(self.page * self.per_page, self.total)

    def numbers(self) -> list[int | None]:
        """Page numbers to show, with None where numbers are skipped (rendered as an ellipsis)."""
        wanted = sorted({1, self.pages, *range(max(1, self.page - 1), min(self.pages, self.page + 1) + 1)})
        out: list[int | None] = []
        for n in wanted:
            if out and out[-1] is not None and n - out[-1] > 1:
                out.append(None)
            out.append(n)
        return out


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _search(stmt, q: str):
    compact = re.sub(r"[\s\-().]", "", q).lstrip("+")
    if compact.isdigit() and len(compact) >= 3:
        # Looks like a phone number typed with spaces/dashes ("71 234 567"): search it as ONE number, or as a code.
        return stmt.where(or_(Employee.phone.like(_like(compact), escape="\\"),
                              Employee.employee_code.ilike(_like(q), escape="\\")))
    for token in q.split()[:6]:
        options = [
            Employee.first_name.ilike(_like(token), escape="\\"),
            Employee.surname.ilike(_like(token), escape="\\"),
            Employee.employee_code.ilike(_like(token), escape="\\"),
        ]
        plain = " ".join(name_words(token))
        if plain and not re.search(r"\d", token):  # letters only; matches without accents too: "Zoe" finds "Zoë"
            options.append(Employee.name_key.like(_like(plain), escape="\\"))
        digits = re.sub(r"\D", "", token)
        if len(digits) >= 3:
            options.append(Employee.phone.like(_like(digits), escape="\\"))
        stmt = stmt.where(or_(*options))
    return stmt


def _order(sort: str, direction: str):
    name = [func.lower(Employee.surname), func.lower(Employee.first_name), Employee.id]
    if sort == "status":
        # ascending = active people first
        return [Employee.active.desc() if direction == "asc" else Employee.active.asc(), *name]
    return name if direction == "asc" else [c.desc() for c in name]


def search_employees(
    db: Session, q: str, status: str, page: int, per_page: int = DEFAULT_PER_PAGE,
    sort: str = "name", direction: str = "asc",
) -> Page:
    stmt = _search(select(Employee), q)
    if status == "active":
        stmt = stmt.where(Employee.active.is_(True))
    elif status == "inactive":
        stmt = stmt.where(Employee.active.is_(False))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    items = db.scalars(stmt.order_by(*_order(sort, direction)).limit(per_page).offset((page - 1) * per_page)).all()
    return Page(items=list(items), page=page, pages=pages, total=total, per_page=per_page)


def status_counts(db: Session, q: str) -> dict[str, int]:
    """How many employees match the search in each status (for the filter chips)."""
    stmt = _search(select(Employee.active, func.count()), q).group_by(Employee.active)
    by_flag = {bool(flag): n for flag, n in db.execute(stmt)}
    active, inactive = by_flag.get(True, 0), by_flag.get(False, 0)
    return {"active": active, "inactive": inactive, "all": active + inactive}


def overview(db: Session) -> dict[str, int]:
    """Numbers for the dashboard."""
    counts = status_counts(db, "")
    missing_phone = db.scalar(select(func.count()).where(Employee.active.is_(True), Employee.phone.is_(None))) or 0
    missing_id = db.scalar(select(func.count()).where(Employee.active.is_(True), Employee.id_last4.is_(None))) or 0
    return {**counts, "missing_phone": missing_phone, "missing_id": missing_id}
