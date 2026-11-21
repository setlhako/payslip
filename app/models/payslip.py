from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import MatchMethod, MatchStatus, PayslipStatus, PdfCheck
from app.models.mixins import TimestampMixin


class Payslip(TimestampMixin, Base):
    """One uploaded PDF inside a batch, and what happened to it."""

    __tablename__ = "payslips"
    __table_args__ = (
        Index("ix_payslips_batch_employee", "batch_id", "employee_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False)
    # Empty until matched. NOT unique with batch_id: two files for one employee must be allowed
    # so the preview screen can flag it.
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))

    # Files (names only, inside the private storage folder; the stored name is random, not the original)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str | None] = mapped_column(String(255))
    encrypted_filename: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(Integer)
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    files_deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Matching
    match_method: Mapped[str] = mapped_column(String(20), default=MatchMethod.none.value, nullable=False)
    match_status: Mapped[str] = mapped_column(String(20), default=MatchStatus.unmatched.value, nullable=False)
    confirmed: Mapped[bool] = mapped_column(default=False, nullable=False)  # HR has approved this match
    pdf_check: Mapped[str] = mapped_column(String(20), default=PdfCheck.not_checked.value, nullable=False)

    # Sending
    status: Mapped[str] = mapped_column(String(20), default=PayslipStatus.pending.value, nullable=False, index=True)
    whatsapp_media_id: Mapped[str | None] = mapped_column(String(100))
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(200), index=True)  # webhooks look payslips up by this
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)

    batch: Mapped["Batch"] = relationship(back_populates="payslips")
    employee: Mapped["Employee | None"] = relationship()


from app.models.batch import Batch  # noqa: E402,F401
from app.models.employee import Employee  # noqa: E402,F401
