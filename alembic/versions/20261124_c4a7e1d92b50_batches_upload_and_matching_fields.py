"""batch upload, background matching progress and payslip matching fields

Adds columns only (all nullable or with defaults), so existing batches and payslips are kept as they are.
Existing batches are marked processing_state='done'.

Revision ID: c4a7e1d92b50
Revises: 9b3f6c1d4e27
Create Date: 2026-11-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c4a7e1d92b50'
down_revision: Union[str, Sequence[str], None] = '9b3f6c1d4e27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("batches", sa.Column("ready_at", sa.DateTime(), nullable=True))
    op.add_column("batches", sa.Column("files_deleted_at", sa.DateTime(), nullable=True))
    op.add_column("batches", sa.Column("processing_state", sa.String(length=20), nullable=False, server_default="done"))
    op.add_column("batches", sa.Column("processing_total", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("batches", sa.Column("processing_done", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("batches", sa.Column("processing_heartbeat_at", sa.DateTime(), nullable=True))

    op.add_column("payslips", sa.Column("page_count", sa.Integer(), nullable=True))
    op.add_column("payslips", sa.Column("invalid_reason", sa.String(length=255), nullable=True))
    op.add_column("payslips", sa.Column("match_score", sa.Integer(), nullable=True))
    op.add_column("payslips", sa.Column("suggestions", sa.Text(), nullable=True))
    op.add_column("payslips", sa.Column("text_names_employee_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    for column in ("text_names_employee_id", "suggestions", "match_score", "invalid_reason", "page_count"):
        op.drop_column("payslips", column)
    for column in ("processing_heartbeat_at", "processing_done", "processing_total", "processing_state",
                   "files_deleted_at", "ready_at"):
        op.drop_column("batches", column)
