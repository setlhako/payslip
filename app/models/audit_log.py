from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class AuditLog(Base):
    """Who did what, and when. Never put full phone numbers or any Omang digits in `details`."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)       # e.g. "login", "batch_created", "send_clicked"
    entity_type: Mapped[str | None] = mapped_column(String(50))           # e.g. "batch", "employee"
    entity_id: Mapped[int | None] = mapped_column()
    details: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
