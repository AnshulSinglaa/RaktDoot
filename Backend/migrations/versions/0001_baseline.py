"""baseline

Revision ID: 0001
Revises: 
Create Date: 2026-10-07 20:02:35.166887

Same schema as the retired sql/001_init.sql. One statement per op.execute:
asyncpg's prepared statements reject multi-statement strings.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE organizations (
            id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            kind        text        NOT NULL CHECK (kind IN ('hospital', 'blood_bank', 'ngo')),
            name        text        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            city        text        NOT NULL CHECK (length(city) BETWEEN 1 AND 100),
            created_at  timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("""
        CREATE TABLE donors (
            id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            name             text        NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
            email            text        NOT NULL CHECK (length(email) <= 254 AND position('@' in email) > 1),
            age              smallint    NOT NULL CHECK (age BETWEEN 18 AND 65),
            blood_group      text        NOT NULL CHECK (blood_group IN ('A+','A-','B+','B-','AB+','AB-','O+','O-')),
            is_available     boolean     NOT NULL DEFAULT true,
            last_donated_at  timestamptz,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now()
        )""")
    # The database's own guarantee: no two donors share an email, whatever the letter case.
    op.execute("CREATE UNIQUE INDEX donors_email_lower_uq ON donors (lower(email))")
    op.execute("""
        CREATE TABLE blood_units (
            id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            blood_bank_id  uuid        NOT NULL REFERENCES organizations (id) ON DELETE RESTRICT,
            blood_group    text        NOT NULL CHECK (blood_group IN ('A+','A-','B+','B-','AB+','AB-','O+','O-')),
            component      text        NOT NULL DEFAULT 'red_cells' CHECK (component IN ('red_cells', 'plasma', 'platelets')),
            collected_at   timestamptz NOT NULL,
            expires_at     timestamptz NOT NULL,
            status         text        NOT NULL DEFAULT 'AVAILABLE' CHECK (status IN ('AVAILABLE', 'RESERVED', 'ISSUED', 'EXPIRED')),
            CONSTRAINT blood_units_expiry_after_collection CHECK (expires_at > collected_at)
        )""")
    # Built for the matching question: "available units of group X at bank Y, soonest-to-expire first".
    op.execute("""
        CREATE INDEX blood_units_available_idx
            ON blood_units (blood_bank_id, blood_group, component, expires_at)
            WHERE status = 'AVAILABLE'""")


def downgrade() -> None:
    op.execute("DROP TABLE blood_units")      # children first: blood_units points at organizations
    op.execute("DROP TABLE donors")
    op.execute("DROP TABLE organizations")
