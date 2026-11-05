"""audit events

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-10

Append-only audit trail, written in the SAME transaction as the change it records (docs/adr/010-audit-log-in-postgres.md).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ponytail: no secondary index yet. Nothing reads this table until the first audit endpoint (Stage 2, needs auth);
    # then add (entity_id, at, id), justified by EXPLAIN. Retention is a purge job (3.5): DELETE rows older than policy.
    op.execute("""
        CREATE TABLE audit_events (
            id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            at         timestamptz NOT NULL,
            event      text        NOT NULL CHECK (length(event) BETWEEN 1 AND 100),
            entity     text        NOT NULL CHECK (length(entity) BETWEEN 1 AND 50),
            entity_id  uuid        NOT NULL,
            actor_id   uuid,
            details    jsonb       NOT NULL DEFAULT '{}'
        )""")
    # actor_id stays NULL until authentication exists (Stage 2).
    # A recorded fact must not change: UPDATE is refused by the database itself. (DELETE stays allowed for the retention purge.)
    op.execute("""
        CREATE FUNCTION audit_events_no_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events rows are immutable';
        END
        $$""")
    op.execute("""
        CREATE TRIGGER audit_events_immutable BEFORE UPDATE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_no_update()""")


def downgrade() -> None:
    op.execute("DROP TABLE audit_events")          # takes the trigger with it
    op.execute("DROP FUNCTION audit_events_no_update()")
