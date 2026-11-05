"""a phone number is unique among VERIFIED donors only

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-19

0011 made phone unique across ALL donors. That let anyone squat a real donor's number with an unverified self-signup
(the real owner then got a 409 at the camp), and the 409 told a stranger "this number is registered" (an oracle).
Now only a verified number is exclusive. An unverified duplicate is never matched and can never be verified with that
number (verifying it would hit this index). ADR 020.

Downgrade restores the 0011 index and FAILS if two donors share a number (one verified, others not): resolve those rows
first. That failure is on purpose: silently deleting donors to make an index fit would be data loss.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX donors_phone_uq")
    op.execute("CREATE UNIQUE INDEX donors_phone_uq ON donors (phone) WHERE phone_verified_at IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX donors_phone_uq")
    op.execute("CREATE UNIQUE INDEX donors_phone_uq ON donors (phone) WHERE phone IS NOT NULL")
