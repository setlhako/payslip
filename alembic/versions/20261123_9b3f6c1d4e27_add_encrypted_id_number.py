"""add employees.id_number_encrypted

HR now keeps the FULL Omang/passport number, encrypted (see app/services/id_crypto.py). Existing employees keep
all their data (including id_type and id_last4); their id_number_encrypted stays NULL until their file is imported again.

Revision ID: 9b3f6c1d4e27
Revises: 2419d27d8159
Create Date: 2026-11-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '9b3f6c1d4e27'
down_revision: Union[str, Sequence[str], None] = '2419d27d8159'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("id_number_encrypted", sa.String(length=255), nullable=True))


def downgrade() -> None:
    # Only the encrypted full numbers are dropped; id_type, id_last4 and everything else stay.
    op.drop_column("employees", "id_number_encrypted")
