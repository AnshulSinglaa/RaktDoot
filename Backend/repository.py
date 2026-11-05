from datetime import datetime
from uuid import UUID

from sqlalchemy import Text, bindparam, literal, select, text, tuple_
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Donor


class DuplicateEmail(Exception):
    pass


class DuplicateProfile(Exception):
    """This account already has a donor profile (donors.user_id is UNIQUE)."""


class DuplicatePhone(Exception):
    """Another VERIFIED donor already has this phone number (donors_phone_uq is partial since 0012)."""


def _raise_domain_error(exc: IntegrityError):
    for constraint, error in (("donors_email_lower_uq", DuplicateEmail), ("donors_user_id_key", DuplicateProfile),
                              ("donors_phone_uq", DuplicatePhone)):
        if constraint in str(exc.orig):
            raise error() from exc
    raise exc


class DonorRepository:
    """All SQL for donors lives here. It never commits: the caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, data: dict) -> Donor:
        donor = Donor(**data)
        self.session.add(donor)
        await self.flush()
        return donor

    async def flush(self) -> None:
        """Send pending INSERT/UPDATEs now, so a constraint failure surfaces here as a domain error, not at COMMIT."""
        try:
            await self.session.flush()
        except IntegrityError as exc:
            _raise_domain_error(exc)

    async def get(self, donor_id: UUID, fresh: bool = False) -> Donor | None:
        """fresh=True re-reads the row: needed after a raw UPDATE in the same session (the identity map is stale)."""
        return await self.session.get(Donor, donor_id, populate_existing=fresh)

    async def verify_phone(self, donor_id: UUID, phone: str, now: datetime) -> bool:
        """Verify ONLY the number the staff member saw, in ONE statement. A read-then-write would verify whatever number
        is on file at commit time: a concurrent PATCH could swap in a number nobody saw. Here the WHERE is re-checked
        on the row as it is after any lock wait. False = no row matched (missing donor, no phone, or another number).
        Another VERIFIED donor with this number -> DuplicatePhone (partial unique index donors_phone_uq, 0012)."""
        try:
            row = await self.session.execute(text(
                "UPDATE donors SET phone_verified_at = coalesce(phone_verified_at, :now) "
                "WHERE id = :id AND phone = :phone RETURNING id"), {"now": now, "id": donor_id, "phone": phone})
        except IntegrityError as exc:
            _raise_domain_error(exc)
        return row.first() is not None

    async def record_donation(self, donor_id: UUID, donated_at: datetime, phone: str | None, now: datetime) -> bool:
        """One statement: keep the LATEST donation date (GREATEST ignores NULL) and verify the phone only if the number
        presented matches the one on file (same rule as verify_phone). False = no such donor."""
        try:
            row = await self.session.execute(text(
                "UPDATE donors SET last_donated_at = GREATEST(last_donated_at, :at), updated_at = :now, "
                "phone_verified_at = CASE WHEN phone = CAST(:phone AS text) THEN coalesce(phone_verified_at, :now) "
                "ELSE phone_verified_at END "
                "WHERE id = :id RETURNING id"), {"at": donated_at, "now": now, "phone": phone, "id": donor_id})
        except IntegrityError as exc:
            _raise_domain_error(exc)
        return row.first() is not None

    async def get_by_user(self, user_id: UUID) -> Donor | None:
        return await self.session.scalar(select(Donor).where(Donor.user_id == user_id))

    async def list(
        self,
        blood_group: str | None = None,
        is_available: bool | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 20,
    ) -> list[Donor]:
        """Keyset page: rows strictly after the bookmark `after`, in (created_at, id) order.

        Fetches limit + 1 rows: the caller keeps `limit` and uses the extra one to know "there is a next page"
        without a slow COUNT(*)."""
        stmt = select(Donor).order_by(Donor.created_at, Donor.id).limit(limit + 1)
        if blood_group is not None:
            stmt = stmt.where(Donor.blood_group == blood_group)
        if is_available is not None:
            stmt = stmt.where(Donor.is_available == is_available)
        if after is not None:
            # literal(..., column type): plain python values would be sent as timestamp WITHOUT time zone
            ts, donor_id = after
            stmt = stmt.where(
                tuple_(Donor.created_at, Donor.id)
                > tuple_(literal(ts, Donor.created_at.type), literal(donor_id, Donor.id.type))
            )
        return list((await self.session.scalars(stmt)).all())


class IdempotencyRepository:
    """Saved results for Idempotency-Key. Like every repository here it never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(self, key: str, request_hash: str) -> bool:
        """True: this request now owns the key. False: a finished request already used it.

        If another transaction is mid-flight with the same key, Postgres makes this INSERT wait until that
        transaction commits (then we get False) or rolls back (then we get True). The database does the locking."""
        result = await self.session.execute(
            text(
                "INSERT INTO idempotency_keys (idempotency_key, request_hash) VALUES (:key, :hash) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING idempotency_key"
            ),
            {"key": key, "hash": request_hash},
        )
        return result.first() is not None

    async def get(self, key: str):
        result = await self.session.execute(
            text(
                "SELECT request_hash, response_status, response_body "
                "FROM idempotency_keys WHERE idempotency_key = :key"
            ),
            {"key": key},
        )
        return result.first()

    async def finish(self, key: str, status: int, body: str) -> None:
        await self.session.execute(
            text(
                "UPDATE idempotency_keys SET response_status = :status, response_body = :body "
                "WHERE idempotency_key = :key"
            ),
            {"key": key, "status": status, "body": body},
        )


async def nearby_eligible_donors(
    session: AsyncSession, latitude: float, longitude: float, radius_m: float, groups: list[str],
    cutoffs: tuple[datetime, datetime], limit: int,
) -> list:
    """Available, eligible, compatible donors within radius_m, nearest first (GiST index on donors.location).

    Returns (id, name, blood_group, distance_m) only: the caller turns these into a minimal, privacy-safe view."""
    rows = await session.execute(
        text("""
            SELECT d.id, d.name, d.blood_group, ST_Distance(d.location, here.point) AS distance_m
            FROM donors d,
                 (SELECT CAST(ST_SetSRID(ST_MakePoint(:lng, :lat), 4326) AS geography) AS point) AS here
            WHERE d.is_available
              AND d.blood_group = ANY(:groups)
              AND (d.last_donated_at IS NULL OR d.last_donated_at <=
                       CASE WHEN d.sex = 'male' THEN CAST(:male_cutoff AS timestamptz)
                            ELSE CAST(:other_cutoff AS timestamptz) END)
              AND d.phone_verified_at IS NOT NULL          -- only donors we can actually reach
              AND d.consent_at IS NOT NULL                 -- DPDP: no consent on record, no processing (B8)
              AND ST_DWithin(d.location, here.point, :radius)
            ORDER BY distance_m, d.id
            LIMIT :limit""").bindparams(bindparam("groups", type_=ARRAY(Text))),
        {"lat": latitude, "lng": longitude, "radius": radius_m, "groups": groups,
         "male_cutoff": cutoffs[0], "other_cutoff": cutoffs[1], "limit": limit},
    )
    return list(rows)


async def donors_by_ids(session: AsyncSession, ids: list[UUID]) -> dict[UUID, tuple[str, str]]:
    """id -> (name, blood_group). For showing a hospital who answered, never more than that."""
    rows = await session.execute(select(Donor.id, Donor.name, Donor.blood_group).where(Donor.id.in_(ids)))
    return {r.id: (r.name, r.blood_group) for r in rows}


DISTANCE_BANDS = ((2_000, "under 2 km"), (5_000, "2-5 km"), (10_000, "5-10 km"), (25_000, "10-25 km"), (50_000, "25-50 km"))


async def eligible_donor_counts_by_band(
    session: AsyncSession, latitude: float, longitude: float, groups: list[str], cutoffs: tuple[datetime, datetime],
) -> dict[str, int]:
    """How many available, eligible, compatible donors live in each distance band (up to 50 km). Counts only:
    no id, no name, no coordinate ever leaves this function."""
    bands = " ".join(f"WHEN dist < {m} THEN '{label}'" for m, label in DISTANCE_BANDS)
    rows = await session.execute(
        text(f"""
            SELECT CASE {bands} END AS band, count(*) AS donors
            FROM (
                SELECT ST_Distance(d.location, here.point) AS dist
                FROM donors d,
                     (SELECT CAST(ST_SetSRID(ST_MakePoint(:lng, :lat), 4326) AS geography) AS point) AS here
                WHERE d.is_available
                  AND d.blood_group = ANY(:groups)
                  AND (d.last_donated_at IS NULL OR d.last_donated_at <=
                       CASE WHEN d.sex = 'male' THEN CAST(:male_cutoff AS timestamptz)
                            ELSE CAST(:other_cutoff AS timestamptz) END)
                  AND d.phone_verified_at IS NOT NULL          -- only donors we can actually reach
                  AND d.consent_at IS NOT NULL                 -- DPDP: no consent on record, not even counted (B8)
                  AND ST_DWithin(d.location, here.point, :radius)
            ) nearby
            GROUP BY band""").bindparams(bindparam("groups", type_=ARRAY(Text))),
        {"lat": latitude, "lng": longitude, "groups": groups, "male_cutoff": cutoffs[0], "other_cutoff": cutoffs[1],
         "radius": DISTANCE_BANDS[-1][0]},
    )
    return {r.band: r.donors for r in rows}
