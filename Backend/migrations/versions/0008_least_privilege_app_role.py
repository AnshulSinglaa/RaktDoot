"""least-privilege role for the running app

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-15

Threat model #3: the app connected as the database OWNER (a superuser in dev), so code running inside the API could
DELETE audit rows, DISABLE the immutability trigger or DROP tables. From here on:
  - the owner role runs migrations only (LIFELINK_MIGRATIONS_DATABASE_URL);
  - the app connects as <database>_app (e.g. lifelink_app), which can only read and write rows, and may only
    INSERT/SELECT audit_events: no UPDATE, no DELETE, no ALTER.
The role name follows the database, so lifelink (dev) and lifelink_test (tests) on one server never share a role or
a password. The password is set by the operator, never in a migration: ALTER ROLE lifelink_app PASSWORD '...'.
RULE for every future migration that creates a table: GRANT the app what it needs in the same migration
(test_least_privilege checks this). Design: docs/adr/014-least-privilege-db-role.md.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DO $$
        DECLARE app text := current_database() || '_app';
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = app) THEN
                EXECUTE format('CREATE ROLE %I LOGIN', app);
            END IF;
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), app);
            EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', app);
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON users, refresh_tokens, donors, organizations, '
                           'blood_units, idempotency_keys TO %I', app);
            EXECUTE format('GRANT SELECT, INSERT ON audit_events TO %I', app);     -- append-only, for real now
            EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO %I', app);
        END $$""")


def downgrade() -> None:
    op.execute("""
        DO $$
        DECLARE app text := current_database() || '_app';
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = app) THEN
                EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', app);
                EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', app);
                EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', app);
                EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', current_database(), app);
                EXECUTE format('DROP ROLE %I', app);
            END IF;
        END $$""")
