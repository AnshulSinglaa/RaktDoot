"""allocations integrity: release_reason set iff released; 'released' reason; expiry index for PENDING holds only

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-19

ADR 021 (revised 2026-10-19): the worker auto-expires only PENDING holds. A CONFIRMED hold past its pickup deadline is
OVERDUE and stays held until the hospital fulfils/cancels or the bank releases it ('released', a new reason).
- CHECK (released_at IS NULL) = (release_reason IS NULL): a released row always says why, a live one never does.
  0013 backfilled every released row with 'cancelled', so existing rows comply; ADD CONSTRAINT validates them anyway
  and fails loudly if one does not. ponytail: validated under an ACCESS EXCLUSIVE lock, fine at this size; on a big
  table use ADD ... NOT VALID, then VALIDATE CONSTRAINT (weaker lock).
- The expiry index matches the only query that uses it: live AND unconfirmed holds by deadline.

Downgrade FAILS on purpose if any hold was released with 'released' (0013's CHECK has no such reason): rewriting a
bank's explicit release as something else would falsify the audit trail.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE allocations DROP CONSTRAINT allocations_release_reason_check")
    op.execute("ALTER TABLE allocations ADD CONSTRAINT allocations_release_reason_check "
               "CHECK (release_reason IN ('rejected', 'expired', 'cancelled', 'released'))")
    op.execute("ALTER TABLE allocations ADD CONSTRAINT allocations_released_has_reason "
               "CHECK ((released_at IS NULL) = (release_reason IS NULL))")
    op.execute("DROP INDEX allocations_live_expiry_idx")
    op.execute("CREATE INDEX allocations_pending_expiry_idx ON allocations (expires_at) "
               "WHERE released_at IS NULL AND confirmed_at IS NULL")


def downgrade() -> None:
    op.execute("DROP INDEX allocations_pending_expiry_idx")
    op.execute("CREATE INDEX allocations_live_expiry_idx ON allocations (expires_at) WHERE released_at IS NULL")
    op.execute("ALTER TABLE allocations DROP CONSTRAINT allocations_released_has_reason")
    op.execute("ALTER TABLE allocations DROP CONSTRAINT allocations_release_reason_check")
    op.execute("ALTER TABLE allocations ADD CONSTRAINT allocations_release_reason_check "
               "CHECK (release_reason IN ('rejected', 'expired', 'cancelled'))")      # fails if 'released' rows exist
