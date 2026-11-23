"""user roles, temporary-password flag, session version, and the system_state table

Existing users are KEPT and every one of them becomes an admin (before this change every user could do everything).
New users default to the least powerful role (viewer).

Revision ID: f7c3a91d2e58
Revises: e2a6d5b81f39
Create Date: 2026-11-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f7c3a91d2e58'
down_revision: Union[str, Sequence[str], None] = 'e2a6d5b81f39'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name", sa.String(length=150), nullable=False, server_default=""))
    op.add_column("users", sa.Column("role", sa.String(length=20), nullable=False, server_default="viewer"))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("users", sa.Column("password_changed_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))
    op.execute(sa.text("UPDATE users SET role = 'admin'"))      # everyone who exists today keeps full access
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("system_state")
    for column in ("session_version", "password_changed_at", "must_change_password", "role", "full_name"):
        op.drop_column("users", column)
