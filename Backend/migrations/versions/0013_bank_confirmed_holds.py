"""bank-confirmed holds with expiry, DISCARDED units, facility phone, stock freshness, auto-confirm, own blood centre

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-19

A hold is the existing allocations row (ADR 021). Its state is derived from columns:
  PENDING   released_at IS NULL AND confirmed_at IS NULL   (expires_at = allocated_at + hold_confirm_minutes)
  CONFIRMED released_at IS NULL AND confirmed_at IS NOT NULL (expires_at = confirmed_at + hold_pickup_hours)
  released  released_at set, release_reason in (rejected, expired, cancelled)
Backfill: live allocations from before 0013 were reserved without any confirmation step, so they become CONFIRMED with
a fixed 6 h pickup window from the moment of the upgrade; released ones (only cancel released before) become 'cancelled'.

Downgrade drops every new column and FAILS on purpose if any blood unit is DISCARDED (the old CHECK has no such status):
a discarded unit is a real fact (the bank said it is not on the shelf); silently turning it back into stock would lie.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE allocations ADD COLUMN confirmed_at timestamptz, ADD COLUMN expires_at timestamptz, "
               "ADD COLUMN release_reason text CHECK (release_reason IN ('rejected', 'expired', 'cancelled'))")
    op.execute("UPDATE allocations SET confirmed_at = allocated_at, expires_at = now() + interval '6 hours' "
               "WHERE released_at IS NULL")
    op.execute("UPDATE allocations SET expires_at = allocated_at, release_reason = 'cancelled' WHERE released_at IS NOT NULL")
    op.execute("ALTER TABLE allocations ALTER COLUMN expires_at SET NOT NULL")
    # the expiry job's only query: live holds past their deadline
    op.execute("CREATE INDEX allocations_live_expiry_idx ON allocations (expires_at) WHERE released_at IS NULL")
    op.execute("ALTER TABLE blood_units DROP CONSTRAINT blood_units_status_check")
    op.execute("ALTER TABLE blood_units ADD CONSTRAINT blood_units_status_check "
               "CHECK (status IN ('AVAILABLE', 'RESERVED', 'ISSUED', 'EXPIRED', 'DISCARDED'))")
    op.execute(r"ALTER TABLE organizations ADD COLUMN phone text CHECK (phone ~ '^\+[0-9]{8,15}$'), "
               "ADD COLUMN stock_confirmed_at timestamptz, "
               "ADD COLUMN auto_confirm_holds boolean NOT NULL DEFAULT false, "
               "ADD COLUMN has_blood_centre boolean NOT NULL DEFAULT false, "
               "ADD CONSTRAINT organizations_blood_centre_only_hospitals CHECK (NOT has_blood_centre OR kind = 'hospital')")


def downgrade() -> None:
    op.execute("ALTER TABLE organizations DROP CONSTRAINT organizations_blood_centre_only_hospitals, "
               "DROP COLUMN has_blood_centre, DROP COLUMN auto_confirm_holds, DROP COLUMN stock_confirmed_at, DROP COLUMN phone")
    op.execute("ALTER TABLE blood_units DROP CONSTRAINT blood_units_status_check")
    op.execute("ALTER TABLE blood_units ADD CONSTRAINT blood_units_status_check "
               "CHECK (status IN ('AVAILABLE', 'RESERVED', 'ISSUED', 'EXPIRED'))")      # fails if DISCARDED units exist
    op.execute("DROP INDEX allocations_live_expiry_idx")
    op.execute("ALTER TABLE allocations DROP COLUMN release_reason, DROP COLUMN expires_at, DROP COLUMN confirmed_at")
