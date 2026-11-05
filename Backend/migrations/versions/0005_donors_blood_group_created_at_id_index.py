"""donors blood group created_at id index

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-10

Backs the filtered keyset list: WHERE blood_group = :g ORDER BY created_at, id.
Equality column first, then the sort columns, so Postgres jumps straight to the group
and reads rows already in order (no walk-and-discard). Evidence: docs/adr/009-indexing-policy.md.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ponytail: plain CREATE INDEX blocks writes while it builds. Fine at this size.
    # Big table: CREATE INDEX CONCURRENTLY inside op.get_context().autocommit_block().
    # Deliberately NOT indexed: is_available (low selectivity, flips often, breaks HOT updates).
    op.execute("CREATE INDEX donors_blood_group_created_at_id_idx ON donors (blood_group, created_at, id)")


def downgrade() -> None:
    op.execute("DROP INDEX donors_blood_group_created_at_id_idx")
