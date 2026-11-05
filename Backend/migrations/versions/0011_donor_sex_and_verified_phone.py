"""donor sex (eligibility interval) and verified phone

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-17

- Eligibility differs by sex (NBTC guidance as we understand it: 3 months men, 4 months women; confirm with a blood centre).
  NULL sex (rows from before this migration) is treated with the LONGER interval: the safe side.
- A donor is only matched or alerted once their phone is verified IN PERSON (a blood bank at a donation, or staff
  at a camp): an unreachable or fake donor is noise in a crisis.
  phone is unique: one donor profile per phone number.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE donors ADD COLUMN sex text CHECK (sex IN ('male', 'female', 'other'))")
    op.execute("ALTER TABLE donors ADD COLUMN phone text CHECK (phone ~ '^\\+[0-9]{8,15}$')")
    op.execute("ALTER TABLE donors ADD COLUMN phone_verified_at timestamptz")
    op.execute("CREATE UNIQUE INDEX donors_phone_uq ON donors (phone) WHERE phone IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX donors_phone_uq")
    op.execute("ALTER TABLE donors DROP COLUMN phone_verified_at, DROP COLUMN phone, DROP COLUMN sex")
