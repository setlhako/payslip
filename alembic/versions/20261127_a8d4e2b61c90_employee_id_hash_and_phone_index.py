"""employees.id_number_hash (blind index of the full ID) + plain indexes on it and on phone

STEP 1 of 2 for "an ID number and a phone number can belong to only one employee". This step can never fail on
existing data: it only adds a nullable column and two NON-unique indexes. Every existing employee is kept as is.
`id_number_hash` stays NULL for everybody until `python -m app.scripts.backfill_id_hash` has run.

STEP 2 (revision b9e5f3c72da1) turns the indexes into UNIQUE ones, but only when the data has no duplicates and
every stored full ID has its hash. See that file and the README section "Unique ID and phone numbers".

Revision ID: a8d4e2b61c90
Revises: f7c3a91d2e58
Create Date: 2026-11-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a8d4e2b61c90'
down_revision: Union[str, Sequence[str], None] = 'f7c3a91d2e58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("id_number_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_employees_id_number_hash", "employees", ["id_number_hash"], unique=False)
    op.create_index("ix_employees_phone", "employees", ["phone"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_employees_phone", table_name="employees")
    op.drop_index("ix_employees_id_number_hash", table_name="employees")
    op.drop_column("employees", "id_number_hash")
