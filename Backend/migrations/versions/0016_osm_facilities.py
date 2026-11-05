"""organizations: OpenStreetMap provenance and ownership (ADR 022)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-20

osm_id ('node/123', 'way/456', 'relation/7') is the idempotency key of `manage.py import-osm`: partial UNIQUE, because
manual and synthetic rows have none. data_source says where a row came from; ownership says who runs the facility,
and ownership_inferred marks a 'government' read from the name, not from an operator:type tag.
Backfill: seed-demo's synthetic facilities end in " (demo)"; everything else stays 'manual'.

Downgrade drops the LABEL columns and their CHECKs but KEEPS osm_id and its partial unique index: osm_id is the
import's match key, so without it the re-import after the next upgrade inserts every facility a second time (QA 2r).
Keeping it is sound: nullable, no default, 0015's code never reads or writes it. Upgrade therefore uses IF NOT EXISTS
for osm_id and its index. Lossy on purpose for the labels only: data_source, ownership and source_updated_at are
re-derived by running import-osm again after the next upgrade (same rows, same ids; ownership_inferred too).
Grants: table-level since 0008, so the app role can read and write the new columns without a new GRANT.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0016'
down_revision: Union[str, Sequence[str], None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE organizations
            ADD COLUMN IF NOT EXISTS osm_id text CHECK (osm_id ~ '^(node|way|relation)/[0-9]+$'),
            ADD COLUMN data_source text NOT NULL DEFAULT 'manual' CHECK (data_source IN ('osm', 'synthetic', 'manual')),
            ADD COLUMN source_updated_at timestamptz,
            ADD COLUMN ownership text NOT NULL DEFAULT 'unknown'
                CHECK (ownership IN ('government', 'private', 'charitable', 'unknown')),
            ADD COLUMN ownership_inferred boolean NOT NULL DEFAULT false,
            ADD CONSTRAINT organizations_inferred_is_known CHECK (NOT ownership_inferred OR ownership <> 'unknown'),
            ADD CONSTRAINT organizations_osm_has_id CHECK (osm_id IS NOT NULL OR data_source <> 'osm')""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS organizations_osm_id_uq ON organizations (osm_id) WHERE osm_id IS NOT NULL")
    op.execute("UPDATE organizations SET data_source = 'synthetic' WHERE name LIKE '% (demo)'")


def downgrade() -> None:
    op.execute("""
        ALTER TABLE organizations
            DROP CONSTRAINT organizations_osm_has_id,
            DROP CONSTRAINT organizations_inferred_is_known,
            DROP COLUMN ownership_inferred,
            DROP COLUMN ownership,
            DROP COLUMN source_updated_at,
            DROP COLUMN data_source""")
    # osm_id and organizations_osm_id_uq stay: see the docstring
