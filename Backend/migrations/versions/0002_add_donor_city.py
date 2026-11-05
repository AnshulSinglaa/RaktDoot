"""add donor city

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10

Expand step of expand/contract: nullable, so code that does not know about city keeps working.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ponytail: tiny table, so a plain CHECK is fine. At 10M rows: ADD CONSTRAINT ... NOT VALID, then VALIDATE CONSTRAINT.
    op.execute("ALTER TABLE donors ADD COLUMN city text CHECK (city IS NULL OR length(city) BETWEEN 1 AND 100)")


def downgrade() -> None:
    op.execute("ALTER TABLE donors DROP COLUMN city")
