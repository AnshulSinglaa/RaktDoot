"""donors created_at id index

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10

Backs keyset pagination: ORDER BY created_at, id with a row-comparison cursor.
Justified by EXPLAIN ANALYZE numbers (see docs/adr/005-rest-conventions.md).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Trade-off: a plain CREATE INDEX blocks writes while it builds. Fine on a small table.
    # Big table: CREATE INDEX CONCURRENTLY inside op.get_context().autocommit_block().
    op.execute("CREATE INDEX donors_created_at_id_idx ON donors (created_at, id)")


def downgrade() -> None:
    op.execute("DROP INDEX donors_created_at_id_idx")
