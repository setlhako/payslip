"""sending engine: batch send state and payslip error code

Adds columns only (nullable or with defaults); existing batches and payslips are kept. Existing batches get
send_state='idle'.

Revision ID: e2a6d5b81f39
Revises: c4a7e1d92b50
Create Date: 2026-11-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e2a6d5b81f39'
down_revision: Union[str, Sequence[str], None] = 'c4a7e1d92b50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("batches", sa.Column("send_state", sa.String(length=20), nullable=False, server_default="idle"))
    op.add_column("batches", sa.Column("send_mode", sa.String(length=10), nullable=True))
    op.add_column("batches", sa.Column("halted_reason", sa.String(length=255), nullable=True))
    op.add_column("batches", sa.Column("stopped_at", sa.DateTime(), nullable=True))
    op.add_column("batches", sa.Column("completed_at", sa.DateTime(), nullable=True))
    op.add_column("payslips", sa.Column("error_code", sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column("payslips", "error_code")
    for column in ("completed_at", "stopped_at", "halted_reason", "send_mode", "send_state"):
        op.drop_column("batches", column)
