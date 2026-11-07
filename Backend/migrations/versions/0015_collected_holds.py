"""allocations.collected_at: a hold whose units physically left the bank (ADR 021, revision 2)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-20

A collected hold is live (released_at IS NULL) and confirmed (collecting proves the units existed), and its units are
ISSUED. Nothing may release it: release_all, release_overdue, reject and expire all skip it.
Backfill: the live holds of FULFILLED requests are exactly the units handed over before 0015, so they are collected at
the moment the request closed. Released rows are never touched (the risk the brief names).

Downgrade drops the CHECK and the column. Lossy on purpose: a collected hold on a still-open request reads as CONFIRMED
again, but its units stay ISSUED (set_status is guarded RESERVED -> X), so no blood comes back to stock.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE allocations ADD COLUMN collected_at timestamptz")
    op.execute("UPDATE allocations a SET collected_at = e.updated_at, confirmed_at = coalesce(a.confirmed_at, e.updated_at) "
               "FROM emergency_requests e WHERE e.id = a.request_id AND e.status = 'FULFILLED' AND a.released_at IS NULL")
    op.execute("ALTER TABLE allocations ADD CONSTRAINT allocations_collected_is_live "
               "CHECK (collected_at IS NULL OR (released_at IS NULL AND confirmed_at IS NOT NULL))")


def downgrade() -> None:
    op.execute("ALTER TABLE allocations DROP CONSTRAINT allocations_collected_is_live")
    op.execute("ALTER TABLE allocations DROP COLUMN collected_at")
