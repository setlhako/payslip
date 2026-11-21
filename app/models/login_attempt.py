from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow


class LoginAttempt(Base):
    """Used for login rate limiting; survives server restarts (unlike a counter in memory)."""

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_ip_time", "ip_address", "attempted_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
