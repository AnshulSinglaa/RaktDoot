"""PostGIS locations, emergency requests and allocations

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-15

Geo without an ORM plugin: the app writes plain latitude/longitude columns, and Postgres derives a GENERATED
geography column from them, indexed with GiST for ST_DWithin radius search (docs/adr/015-geo-matching.md).
Double allocation is impossible twice over: FOR UPDATE SKIP LOCKED in the allocation query, and a partial UNIQUE
index on allocations so even buggy code cannot hold one unit in two live allocations (docs/adr/016-allocation.md).
"""
from typing import Sequence, Union

from alembic import op


revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCATION = """
    ADD COLUMN latitude  double precision CHECK (latitude BETWEEN -90 AND 90),
    ADD COLUMN longitude double precision CHECK (longitude BETWEEN -180 AND 180),
    ADD CONSTRAINT {table}_lat_lng_together CHECK ((latitude IS NULL) = (longitude IS NULL)),
    ADD COLUMN location geography(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography) STORED"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    for table in ("organizations", "donors"):
        op.execute(f"ALTER TABLE {table} {LOCATION.format(table=table)}")
        op.execute(f"CREATE INDEX {table}_location_gist ON {table} USING gist (location)")

    op.execute("""
        CREATE TABLE emergency_requests (
            id            uuid        PRIMARY KEY,
            hospital_id   uuid        NOT NULL REFERENCES organizations (id) ON DELETE RESTRICT,
            created_by    uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
            blood_group   text        NOT NULL CHECK (blood_group IN ('A+','A-','B+','B-','AB+','AB-','O+','O-')),
            component     text        NOT NULL CHECK (component IN ('red_cells', 'plasma', 'platelets')),
            units_needed  smallint    NOT NULL CHECK (units_needed BETWEEN 1 AND 20),
            urgency       text        NOT NULL CHECK (urgency IN ('CRITICAL', 'HIGH', 'NORMAL')),
            status        text        NOT NULL DEFAULT 'OPEN'
                                      CHECK (status IN ('OPEN', 'ALLOCATED', 'FULFILLED', 'CANCELLED')),
            created_at    timestamptz NOT NULL,
            updated_at    timestamptz NOT NULL
        )""")
    op.execute("CREATE INDEX emergency_requests_hospital_created_idx ON emergency_requests (hospital_id, created_at DESC)")
    op.execute("""
        CREATE TABLE allocations (
            id             uuid        PRIMARY KEY,
            request_id     uuid        NOT NULL REFERENCES emergency_requests (id) ON DELETE RESTRICT,
            blood_unit_id  uuid        NOT NULL REFERENCES blood_units (id) ON DELETE RESTRICT,
            -- snapshot of the unit at allocation time: reading a request never joins inventory's table (ADR 011)
            blood_bank_id  uuid        NOT NULL REFERENCES organizations (id) ON DELETE RESTRICT,
            blood_group    text        NOT NULL,
            allocated_at   timestamptz NOT NULL,
            released_at    timestamptz
        )""")
    # The backstop: one unit, at most ONE live allocation. A released unit can be allocated again.
    op.execute("CREATE UNIQUE INDEX allocations_one_live_per_unit ON allocations (blood_unit_id) WHERE released_at IS NULL")
    op.execute("CREATE INDEX allocations_request_idx ON allocations (request_id)")

    # ADR 014: every new table is granted in the same migration
    op.execute("""
        DO $$
        BEGIN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON emergency_requests, allocations TO %I', current_database() || '_app');
        END $$""")


def downgrade() -> None:
    op.execute("DROP TABLE allocations")
    op.execute("DROP TABLE emergency_requests")
    for table in ("donors", "organizations"):
        op.execute(f"DROP INDEX {table}_location_gist")
        op.execute(f"ALTER TABLE {table} DROP COLUMN location, DROP CONSTRAINT {table}_lat_lng_together, "
                   "DROP COLUMN longitude, DROP COLUMN latitude")
    # the extension stays: dropping it is a superuser decision, and other databases objects may rely on it
