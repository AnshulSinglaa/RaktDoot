"""What other modules may ask the organizations module. The ONLY import path into this module (ADR 011)."""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audit import AuditRepository
from db import make_engine, make_sessionmaker

from .models import Organization
from .osm import map_snapshot


@dataclass(frozen=True)
class Nearby:
    id: UUID
    distance_m: float


async def location_of(session: AsyncSession, org_id: UUID) -> tuple[float, float] | None:
    org = await session.get(Organization, org_id)
    if org is None or org.latitude is None:
        return None
    return org.latitude, org.longitude


async def nearby(
    session: AsyncSession, kind: str, latitude: float, longitude: float, radius_m: float, limit: int = 50,
    ownership: str | None = None, among: list[UUID] | None = None, confirmed_since: datetime | None = None,
) -> list[Nearby]:
    """Organizations of `kind` within radius_m, nearest first. ST_DWithin uses the GiST index on `location`.
    `ownership` and `among` (only these ids, e.g. banks with active staff, D88) filter INSIDE the query, before the
    LIMIT: filtering the 20 nearest afterwards would answer "no private bank" whenever 20 government banks are nearer
    (ADR 022). The public search also passes `among` = banks with compatible stock and `confirmed_since` = stock
    counted after this moment (R1, 2c), for the same reason.

    Distance is straight-line over the earth's surface, not road distance (that needs OSRM: see docs/adr/015)."""
    rows = await session.execute(
        text("""
            SELECT id, ST_Distance(location, here.point) AS distance_m
            FROM organizations,
                 (SELECT CAST(ST_SetSRID(ST_MakePoint(:lng, :lat), 4326) AS geography) AS point) AS here
            WHERE kind = :kind AND ST_DWithin(location, here.point, :radius)
              AND (CAST(:ownership AS text) IS NULL OR ownership = :ownership)
              AND (CAST(:among AS uuid[]) IS NULL OR id = ANY(CAST(:among AS uuid[])))
              AND (CAST(:since AS timestamptz) IS NULL OR stock_confirmed_at >= :since)
            ORDER BY distance_m, id
            LIMIT :limit"""),
        {"lat": latitude, "lng": longitude, "kind": kind, "radius": radius_m, "limit": limit, "ownership": ownership,
         "among": among, "since": confirmed_since},
    )
    return [Nearby(r.id, r.distance_m) for r in rows]


@dataclass(frozen=True)
class Facility:
    name: str
    city: str
    phone: str | None
    stock_confirmed_at: datetime | None
    auto_confirm_holds: bool
    latitude: float | None = None
    longitude: float | None = None
    ownership: str = "unknown"
    ownership_inferred: bool = False
    data_source: str = "manual"

    @property
    def is_demo(self) -> bool:
        """osm/synthetic: the name (and phone) may be real, but its stock and every request in this build are not."""
        return self.data_source in ("osm", "synthetic")


async def facilities(session: AsyncSession, org_ids: list[UUID]) -> dict[UUID, Facility]:
    """id -> the public facts about a facility: name, city, phone, stock freshness, auto-confirm, where, who runs it."""
    rows = await session.execute(text(
        "SELECT id, name, city, phone, stock_confirmed_at, auto_confirm_holds, latitude, longitude, ownership, "
        "ownership_inferred, data_source FROM organizations WHERE id = ANY(:ids)"), {"ids": list(org_ids)})
    return {r.id: Facility(*r[1:]) for r in rows}


async def names_of(session: AsyncSession, org_ids: list[UUID]) -> dict[UUID, tuple[str, str]]:
    """id -> (name, city), for showing a donor which hospital is asking."""
    rows = await session.execute(text("SELECT id, name, city FROM organizations WHERE id = ANY(:ids)"),
                                 {"ids": list(org_ids)})
    return {r.id: (r.name, r.city) for r in rows}


# ---------- import-osm (ADR 022) ----------
COLUMNS = ("osm_id", "kind", "name", "city", "latitude", "longitude", "phone", "ownership", "ownership_inferred")
UPSERT = text("""
    INSERT INTO organizations (osm_id, kind, name, city, latitude, longitude, phone, ownership, ownership_inferred,
                               data_source, source_updated_at)
    SELECT r.*, 'osm', CAST(:source_updated_at AS timestamptz)
    FROM unnest(CAST(:osm_id AS text[]), CAST(:kind AS text[]), CAST(:name AS text[]), CAST(:city AS text[]),
                CAST(:latitude AS float8[]), CAST(:longitude AS float8[]), CAST(:phone AS text[]),
                CAST(:ownership AS text[]), CAST(:ownership_inferred AS boolean[])) AS r
    ON CONFLICT (osm_id) WHERE osm_id IS NOT NULL DO UPDATE SET
        name = EXCLUDED.name, city = EXCLUDED.city, latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
        phone = EXCLUDED.phone, ownership = EXCLUDED.ownership, ownership_inferred = EXCLUDED.ownership_inferred,
        source_updated_at = EXCLUDED.source_updated_at, data_source = EXCLUDED.data_source
    WHERE organizations.kind = EXCLUDED.kind
    RETURNING (xmax = 0) AS inserted""")
# Never touched on conflict: id, created_at (holds and audit point at them) and what the BANK owns:
# stock_confirmed_at, auto_confirm_holds, has_blood_centre. The WHERE refuses to turn a bank into a hospital (a
# bank with holds must stay a bank): such a row returns nothing and is counted `kind_conflict`.


async def import_osm(session: AsyncSession, snapshots: list[tuple[str, dict]], now: datetime) -> dict:
    """Upsert Overpass snapshots [(kind, json)] in the caller's ONE transaction, plus ONE audit event (counts only).
    Rows missing from a newer snapshot are never deleted (holds and audit reference them): they are counted."""
    report = {}
    for kind, snapshot in snapshots:
        rows, counts = map_snapshot(snapshot["elements"], kind)
        result = (await session.execute(UPSERT, {
            "source_updated_at": datetime.fromisoformat(snapshot["osm3s"]["timestamp_osm_base"]),
            **{col: [r[col] for r in rows] for col in COLUMNS}})).all()
        inserted = sum(r.inserted for r in result)
        missing = await session.scalar(text(
            "SELECT count(*) FROM organizations WHERE data_source = 'osm' AND kind = :kind "
            "AND NOT (osm_id = ANY(CAST(:ids AS text[])))"), {"kind": kind, "ids": [r["osm_id"] for r in rows]})
        report[kind] = {"read": len(snapshot["elements"]), "kept": len(rows), "inserted": inserted,
                        "updated": len(result) - inserted, "kind_conflict": len(rows) - len(result),
                        "missing_from_snapshot": missing, **dict(sorted(counts.items()))}
    dropped: dict = {}
    for kind_report in report.values():
        for key, n in kind_report.items():
            if key.startswith("dropped:"):
                dropped[key[8:]] = dropped.get(key[8:], 0) + n
    # entity_id: a fresh id per run (an import touches thousands of rows, it is not about one of them)
    await AuditRepository(session).add(now, "organizations.imported", "organization", uuid4(), {
        "source": "osm", "inserted": sum(r["inserted"] for r in report.values()),
        "updated": sum(r["updated"] for r in report.values()), "dropped": dropped})
    return report


SNAPSHOT_FILES = (("blood_bank", "mp_blood_banks.json"), ("hospital", "mp_hospitals.json"))


async def import_osm_dir(database_url: str, directory: Path) -> dict:
    """`manage.py import-osm`: read both snapshot files, import them in ONE transaction as the app role."""
    snapshots = [(kind, json.loads((directory / name).read_text(encoding="utf-8"))) for kind, name in SNAPSHOT_FILES]
    engine = make_engine(database_url, 1, 0)
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            return await import_osm(session, snapshots, datetime.now(timezone.utc))
    finally:
        await engine.dispose()
