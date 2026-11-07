"""users, refresh tokens, donor ownership and consent

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15

Identity. Design and trade-offs: docs/adr/013-authentication.md.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE users (
            id               uuid        PRIMARY KEY,
            email            text        NOT NULL CHECK (length(email) <= 254 AND position('@' in email) > 1),
            password_hash    text        NOT NULL,
            role             text        NOT NULL CHECK (role IN ('donor', 'hospital', 'blood_bank', 'ngo', 'admin')),
            organization_id  uuid        REFERENCES organizations (id) ON DELETE RESTRICT,
            is_active        boolean     NOT NULL DEFAULT true,
            failed_logins    smallint    NOT NULL DEFAULT 0,
            locked_until     timestamptz,
            created_at       timestamptz NOT NULL DEFAULT now(),
            -- staff belong to exactly one organization; donors and admins to none. The database refuses anything else.
            CONSTRAINT users_staff_have_an_organization
                CHECK ((role IN ('hospital', 'blood_bank', 'ngo')) = (organization_id IS NOT NULL))
        )""")
    op.execute("CREATE UNIQUE INDEX users_email_lower_uq ON users (lower(email))")
    # Opaque refresh tokens, stored as a SHA-256 hash: a leaked table cannot be replayed.
    # family_id groups one login's rotations, so reuse of an old token revokes the whole chain.
    op.execute("""
        CREATE TABLE refresh_tokens (
            id          uuid        PRIMARY KEY,
            user_id     uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            family_id   uuid        NOT NULL,
            token_hash  text        NOT NULL UNIQUE,
            expires_at  timestamptz NOT NULL,
            used_at     timestamptz,
            revoked_at  timestamptz,
            created_at  timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX refresh_tokens_family_idx ON refresh_tokens (family_id)")   # revoke-family is a lookup by family
    # Expand step: both nullable. Walk-in donors registered at a camp have no account (user_id NULL).
    # Rows from before authentication have no recorded consent (consent_at NULL); the API now requires it for new rows.
    op.execute("ALTER TABLE donors ADD COLUMN user_id uuid UNIQUE REFERENCES users (id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE donors ADD COLUMN consent_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE donors DROP COLUMN consent_at")
    op.execute("ALTER TABLE donors DROP COLUMN user_id")
    op.execute("DROP TABLE refresh_tokens")
    op.execute("DROP TABLE users")
