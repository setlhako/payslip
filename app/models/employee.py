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
    omang_last4: Mapped[str | None] = mapped_column(String(4))   # ONLY the last 4 digits - the full Omang is never stored
    employee_code: Mapped[str | None] = mapped_column(String(50), unique=True)  # optional; NULLs don't clash
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.surname}"
