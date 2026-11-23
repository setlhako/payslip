"""replace omang_last4 with id_type and id_last4

Employees can now be identified by an Omang OR a passport. Existing values are kept: every row that had an
omang_last4 becomes id_type='omang' with the same four characters in id_last4.

Revision ID: 2419d27d8159
Revises: 32ae9ef285f6
Create Date: 2026-11-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '2419d27d8159'
down_revision: Union[str, Sequence[str], None] = '32ae9ef285f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("id_type", sa.String(length=10), nullable=True))
    op.add_column("employees", sa.Column("id_last4", sa.String(length=4), nullable=True))
    # copy BEFORE dropping the old column, so nothing can be lost
    op.execute(sa.text(
        "UPDATE employees SET id_type = 'omang', id_last4 = omang_last4 WHERE omang_last4 IS NOT NULL"
    ))
    op.drop_column("employees", "omang_last4")


def downgrade() -> None:
    # Passport rows have no place in the old schema, so their ID is dropped (their other data is kept).
    op.add_column("employees", sa.Column("omang_last4", sa.String(length=4), nullable=True))
    op.execute(sa.text("UPDATE employees SET omang_last4 = id_last4 WHERE id_type = 'omang'"))
    op.drop_column("employees", "id_last4")
    op.drop_column("employees", "id_type")
