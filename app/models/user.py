from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin


class User(TimestampMixin, Base):
    """Someone who can log in. `role` decides what they may do (app/security/permissions.py): admin, hr or viewer."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)   # stored lower-case
    full_name: Mapped[str] = mapped_column(String(150), default="", server_default="", nullable=False)
    # The secure default is the least powerful role; the migration made every EXISTING user an admin.
    role: Mapped[str] = mapped_column(String(20), default="viewer", server_default="viewer", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # argon2 hash, never the password
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # New and reset passwords are temporary: the user must choose their own at the next login.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Bumped by "sign out everywhere" and by password changes: sessions made before the bump stop working.
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def display_name(self) -> str:
        return self.full_name or self.username
