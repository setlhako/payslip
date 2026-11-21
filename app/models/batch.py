from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import BatchStatus
from app.models.mixins import TimestampMixin


class Batch(TimestampMixin, Base):
    """One pay run, e.g. "November 2026", holding many payslips."""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    pay_month_label: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=BatchStatus.draft.value, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime)

    payslips: Mapped[list["Payslip"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


from app.models.payslip import Payslip  # noqa: E402  (avoids a circular import)
