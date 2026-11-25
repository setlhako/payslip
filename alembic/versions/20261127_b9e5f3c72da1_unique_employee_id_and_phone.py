"""UNIQUE indexes on employees.id_number_hash and employees.phone

STEP 2 of 2. It REFUSES to run (with a plain explanation, changing nothing) while:
  - some employee has a stored full ID but no hash yet  -> run  python -m app.scripts.backfill_id_hash
  - two employees share a phone number or an ID hash    -> fix them (the backfill command lists them: names and ids only)
Then run `python -m alembic upgrade head` again. NULLs stay allowed and may repeat (no phone / no full ID on file).

Revision ID: b9e5f3c72da1
Revises: a8d4e2b61c90
Create Date: 2026-11-27
"""
from typing import Sequence, Union

from alembic import op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = 'b9e5f3c72da1'
down_revision: Union[str, Sequence[str], None] = 'a8d4e2b61c90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _count(sql: str) -> int:
    return int(op.get_bind().execute(sa.text(sql)).scalar() or 0)


def upgrade() -> None:
    unhashed = _count("SELECT COUNT(*) FROM employees WHERE id_number_encrypted IS NOT NULL AND id_number_hash IS NULL")
    dup_phones = _count("SELECT COUNT(*) FROM (SELECT phone FROM employees WHERE phone IS NOT NULL "
                        "GROUP BY phone HAVING COUNT(*) > 1) AS d")
    dup_ids = _count("SELECT COUNT(*) FROM (SELECT id_number_hash FROM employees WHERE id_number_hash IS NOT NULL "
                     "GROUP BY id_number_hash HAVING COUNT(*) > 1) AS d")
    problems = []
    if unhashed:
        problems.append(f"{unhashed} employee(s) have a stored ID number that has not been hashed yet. "
                        "Run:  python -m app.scripts.backfill_id_hash")
    if dup_phones:
        problems.append(f"{dup_phones} phone number(s) are used by more than one employee.")
    if dup_ids:
        problems.append(f"{dup_ids} ID number(s) are used by more than one employee.")
    if problems:
        raise CommandError(
            "Cannot make phone and ID numbers unique yet (nothing was changed):\n  - " + "\n  - ".join(problems)
            + "\nThe backfill command lists the employees involved (names and ids only). Fix them in the app (edit the "
              "phone, or use 'Remove the saved ID number'), then run:  python -m alembic upgrade head"
        )
    op.drop_index("ix_employees_phone", table_name="employees")
    op.drop_index("ix_employees_id_number_hash", table_name="employees")
    op.create_index("uq_employees_phone", "employees", ["phone"], unique=True)
    op.create_index("uq_employees_id_number_hash", "employees", ["id_number_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_employees_id_number_hash", table_name="employees")
    op.drop_index("uq_employees_phone", table_name="employees")
    op.create_index("ix_employees_id_number_hash", "employees", ["id_number_hash"], unique=False)
    op.create_index("ix_employees_phone", "employees", ["phone"], unique=False)
