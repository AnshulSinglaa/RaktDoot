"""transactional outbox and donor responses

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-15

The outbox: work to do AFTER a transaction commits (notify a donor) is written as a ROW in the SAME transaction.
A worker drains it with FOR UPDATE SKIP LOCKED. "Save the request, then call the SMS API" can lose the message
(crash between the two) or send it for a rolled-back request; the outbox can do neither (docs/adr/017-outbox.md).
donor_responses doubles as the delivery log: UNIQUE (request_id, donor_id) makes "notify this donor" idempotent.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE outbox (
            id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            topic         text        NOT NULL CHECK (length(topic) BETWEEN 1 AND 100),
            payload       jsonb       NOT NULL,
            created_at    timestamptz NOT NULL,
            available_at  timestamptz NOT NULL,          -- retry backoff: not before this time
            attempts      smallint    NOT NULL DEFAULT 0,
            last_error    text,
            processed_at  timestamptz,
            dead_at       timestamptz                    -- gave up: a human looks at it (dead-letter)
        )""")
    # the worker's only question: "what is due?" A partial index stays small: done rows drop out of it.
    op.execute("CREATE INDEX outbox_due_idx ON outbox (available_at, id) WHERE processed_at IS NULL AND dead_at IS NULL")
    op.execute("""
        CREATE TABLE donor_responses (
            request_id    uuid        NOT NULL REFERENCES emergency_requests (id) ON DELETE RESTRICT,
            donor_id      uuid        NOT NULL REFERENCES donors (id) ON DELETE RESTRICT,
            status        text        NOT NULL CHECK (status IN ('NOTIFIED', 'ACCEPTED', 'DECLINED')),
            notified_at   timestamptz NOT NULL,
            responded_at  timestamptz,
            PRIMARY KEY (request_id, donor_id)           -- one donor is notified about one request at most once
        )""")
    op.execute("CREATE INDEX donor_responses_donor_idx ON donor_responses (donor_id, notified_at DESC)")
    op.execute("""
        DO $$
        BEGIN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON outbox, donor_responses TO %I', current_database() || '_app');
            EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO %I', current_database() || '_app');
        END $$""")


def downgrade() -> None:
    op.execute("DROP TABLE donor_responses")
    op.execute("DROP TABLE outbox")
