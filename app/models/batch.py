from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import BatchStatus, ProcessingState, SendState
from app.models.mixins import TimestampMixin


class Batch(TimestampMixin, Base):
    """One pay run, e.g. "November 2026", holding many payslips."""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    pay_month_label: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=BatchStatus.draft.value, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    send_state: Mapped[str] = mapped_column(
        String(20), default=SendState.idle.value, server_default=SendState.idle.value, nullable=False)
    send_mode: Mapped[str | None] = mapped_column(String(10))           # "mock" or "live", as it was when Send was clicked
    halted_reason: Mapped[str | None] = mapped_column(String(255))      # shown as a banner (e.g. access token rejected)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime)            # when HR marked it ready
    files_deleted_at: Mapped[datetime | None] = mapped_column(DateTime)    # when the stored PDFs were purged

    # Background matching progress (the batch page shows a progress bar while this is "processing")
    processing_state: Mapped[str] = mapped_column(
        String(20), default=ProcessingState.done.value, server_default=ProcessingState.done.value, nullable=False)
    processing_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    processing_done: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    processing_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)

    payslips: Mapped[list["Payslip"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


from app.models.payslip import Payslip  # noqa: E402  (avoids a circular import)
