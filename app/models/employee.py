from sqlalchemy import Boolean, String
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

    phone: Mapped[str | None] = mapped_column(String(16))        # E.164, e.g. +26771234567
    # id_type is "omang" or "passport"; id_last4 = the last 4 characters of the number, uppercase, in plain text: it is
    # the PDF password. id_type and id_last4 are NULL when no ID is on file.
    # id_number_encrypted = the FULL number, encrypted (Fernet, key ID_ENCRYPTION_KEY, see services/id_crypto.py).
    # NULL for people whose full number has not been imported yet. Never read it except through the reveal endpoint.
    id_type: Mapped[str | None] = mapped_column(String(10))
    id_last4: Mapped[str | None] = mapped_column(String(4))
    id_number_encrypted: Mapped[str | None] = mapped_column(String(255))
    employee_code: Mapped[str | None] = mapped_column(String(50), unique=True)  # optional; NULLs don't clash
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.surname}"

    @property
    def has_full_id(self) -> bool:
        return bool(self.id_number_encrypted)

    @property
    def id_type_label(self) -> str:
        return {"omang": "Omang", "passport": "Passport"}.get(self.id_type or "", "")
