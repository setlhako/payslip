from sqlalchemy import Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin


class Employee(TimestampMixin, Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    surname: Mapped[str] = mapped_column(String(100), nullable=False)

    # Normalised name used for matching: lowercase, no accents, words sorted alphabetically,
    # e.g. "Thabo Molefe" -> "molefe thabo". Filled in by the app, indexed for fast lookup.
    # NOT unique: two employees can share a name (those files go to "needs review").
    name_key: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    # E.164, e.g. +26771234567. UNIQUE across ALL employees, active or not (index `uq_employees_phone` below): a deactivated
    # employee keeps their number until someone edits it. NULL (no phone) can repeat.
    phone: Mapped[str | None] = mapped_column(String(16))
    # id_type is "omang" or "passport"; id_last4 = the last 4 characters of the number, uppercase, in plain text: it is
    # the PDF password. id_type and id_last4 are NULL when no ID is on file.
    # id_number_encrypted = the FULL number, encrypted (Fernet, key ID_ENCRYPTION_KEY, see services/id_crypto.py).
    # NULL for people whose full number has not been imported yet. Never read it except through the reveal endpoint.
    id_type: Mapped[str | None] = mapped_column(String(10))
    id_last4: Mapped[str | None] = mapped_column(String(4))
    id_number_encrypted: Mapped[str | None] = mapped_column(String(255))
    # Blind index of the full number (HMAC-SHA256 of the normalised number, key derived from ID_ENCRYPTION_KEY: see
    # services/id_crypto.py). UNIQUE, so one ID cannot belong to two employees. NULL for legacy rows that only have a last 4
    # (and until `python -m app.scripts.backfill_id_hash` has run). Never shown, logged or exported.
    id_number_hash: Mapped[str | None] = mapped_column(String(64))
    employee_code: Mapped[str | None] = mapped_column(String(50), unique=True)  # optional; NULLs don't clash
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("uq_employees_phone", "phone", unique=True),
        Index("uq_employees_id_number_hash", "id_number_hash", unique=True),
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.surname}"

    @property
    def has_full_id(self) -> bool:
        return bool(self.id_number_encrypted)

    @property
    def id_type_label(self) -> str:
        return {"omang": "Omang", "passport": "Passport"}.get(self.id_type or "", "")
