"""emergency_requests: a short request code and the hospital's own reference (2c, ADR 023 context)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-20

code: 8 characters the ward writes on the paper requisition, so bank and hospital can tell one request from another
without any name. New rows get a Crockford base32 code from the app (security.short_code); existing rows are
backfilled here from md5 (hex, upper case: a different alphabet, still 8 characters and unique). UNIQUE is the
guarantee; the app retries once on a collision.
hospital_ref: the ward's IP or bed number, optional, at most 30 characters. Never a name (the API pattern refuses
anything but letters, digits, space and / # -), never audited, never public.
Downgrade drops both columns (lossy on purpose: codes are re-issued on the next upgrade, references are gone).
Grants: table-level since 0008, nothing new to grant.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0017'
down_revision: Union[str, Sequence[str], None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE emergency_requests ADD COLUMN code text, "
               "ADD COLUMN hospital_ref text CHECK (length(hospital_ref) <= 30)")
    op.execute("UPDATE emergency_requests SET code = upper(substr(md5(random()::text || id::text), 1, 8))")
    op.execute("ALTER TABLE emergency_requests ALTER COLUMN code SET NOT NULL, "
               "ADD CONSTRAINT emergency_requests_code_key UNIQUE (code)")


def downgrade() -> None:
    op.execute("ALTER TABLE emergency_requests DROP COLUMN hospital_ref, DROP COLUMN code")
