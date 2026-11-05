"""login failures counted per (account, client IP)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-24

B10: the lockout counter lived on users (failed_logins, locked_until), so ANYONE who knew a bank's or hospital's login
email could lock it out forever, 5 wrong passwords every 15 minutes. Now the counter is per (account, IP): the
attacker's IP is locked, the real staff on their own network still log in.
users.failed_logins and users.locked_until are no longer read or written. They stay for now (expand step, so a
rollback to the previous app version still works) and are dropped in a later contract migration.
ip: the client address as the API sees it, needed to tell attackers from staff. Personal data (DPDP): a row is
deleted on the next successful login from that IP. ponytail: no purge job for rows that never see a success; add one
(DELETE WHERE last_failed_at < now() - interval '30 days') when the table is measurably large.
Downgrade drops the table (lossy on purpose: lockouts in progress are forgotten).
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0018'
down_revision: Union[str, Sequence[str], None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE login_failures (
            user_id         uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            ip              text        NOT NULL CHECK (length(ip) BETWEEN 1 AND 64),
            failures        smallint    NOT NULL DEFAULT 0,
            locked_until    timestamptz,
            last_failed_at  timestamptz NOT NULL,
            PRIMARY KEY (user_id, ip)
        )""")
    op.execute("""
        DO $$
        BEGIN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON login_failures TO %I', current_database() || '_app');
        END $$""")


def downgrade() -> None:
    op.execute("DROP TABLE login_failures")
