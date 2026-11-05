"""idempotency keys

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-10

One row per Idempotency-Key. The row is claimed, the donor is inserted, and the saved
response is written in ONE transaction, so either all of it exists or none of it does.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ponytail: the key namespace is global because there is no auth yet. Stage 2 makes the key unique per
    # (user_id, key); until then a guessed key could replay someone else's saved response.
    # ponytail: response_body is a JSON string in a text column (jsonb buys querying we do not need).
    # ponytail: rows are never purged yet. The body holds a donor's name, so retention (~24h) is a job in 3.5.
    op.execute("""
        CREATE TABLE idempotency_keys (
            idempotency_key  text        PRIMARY KEY CHECK (length(idempotency_key) BETWEEN 1 AND 255),
            request_hash     text        NOT NULL,
            response_status  smallint,
            response_body    text,
            created_at       timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX idempotency_keys_created_at_idx ON idempotency_keys (created_at)")   # for the future purge job


def downgrade() -> None:
    op.execute("DROP TABLE idempotency_keys")
