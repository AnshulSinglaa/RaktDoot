from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Allocation, EmergencyRequest


class EmergencyRepository:
    """All SQL for emergencies and allocations. It never commits: the caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, data: dict) -> EmergencyRequest:
        req = EmergencyRequest(**data)
        self.session.add(req)
        await self.session.flush()
        return req

    async def get(self, request_id: UUID, lock: bool = False) -> EmergencyRequest | None:
        """lock=True: SELECT ... FOR UPDATE. Two clicks on 'cancel' or 'allocate' for the SAME request queue up
        instead of both acting on a stale status."""
        stmt = select(EmergencyRequest).where(EmergencyRequest.id == request_id)
        if lock:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def recent(self, hospital_id: UUID | None, limit: int = 50) -> list[EmergencyRequest]:
        # Trade-off: newest 50, no cursor. A hospital has a handful of live requests; add keyset paging with history views.
        stmt = select(EmergencyRequest).order_by(EmergencyRequest.created_at.desc(), EmergencyRequest.id).limit(limit)
        if hospital_id is not None:
            stmt = stmt.where(EmergencyRequest.hospital_id == hospital_id)
        return list((await self.session.scalars(stmt)).all())

    async def live_allocations(self, request_id: UUID) -> list[Allocation]:
        # populate_existing: hold UPDATEs below are plain SQL, so objects already in the session may be stale
        stmt = (
            select(Allocation)
            .where(Allocation.request_id == request_id, Allocation.released_at.is_(None))
            .order_by(Allocation.allocated_at, Allocation.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.scalars(stmt)).all())

    async def add_allocations(self, rows: list[dict]) -> None:
        self.session.add_all([Allocation(**r) for r in rows])
        await self.session.flush()

    # ---------- holds (ADR 021). Every UPDATE is conditional, and callers hold the request row lock ----------
    async def lock_hospital_cap(self, hospital_id: UUID) -> None:
        """Serialise allocations of ONE hospital, so two parallel requests cannot both see "room for 5" under the cap.
        Trade-off: hashtext collisions between hospitals only make them queue briefly; per-hospital caps (a nullable
        column) are deferred until a hospital needs a different number."""
        await self.session.execute(text("SELECT pg_advisory_xact_lock(hashtext('hospital-cap:' || :h))"),
                                   {"h": str(hospital_id)})

    async def held_by_hospital(self, hospital_id: UUID) -> int:
        """Live, uncollected held units across the hospital's OPEN/ALLOCATED requests. Collected units are in a patient
        (or on the way), not hoarded, so they do not count."""
        return await self.session.scalar(text("""
            SELECT count(*) FROM allocations a JOIN emergency_requests e ON e.id = a.request_id
            WHERE e.hospital_id = :h AND e.status IN ('OPEN', 'ALLOCATED') AND a.released_at IS NULL
              AND a.collected_at IS NULL"""),
            {"h": hospital_id})

    async def excluded_banks(self, request_id: UUID) -> set[UUID]:
        """Banks never asked again for this request: only those that explicitly said no. A bank that let its hold expire
        (night shift, nobody at the screen) is asked again when the hospital clicks "check blood banks again" after a
        call; one that released an OVERDUE hold may be asked again too."""
        rows = await self.session.execute(text(
            "SELECT DISTINCT blood_bank_id FROM allocations WHERE request_id = :r AND release_reason = 'rejected'"),
            {"r": request_id})
        return {r[0] for r in rows}

    async def bank_holds(self, request_id: UUID, bank_id: UUID) -> list:
        return list(await self.session.execute(text(
            "SELECT blood_unit_id, released_at, confirmed_at, expires_at, collected_at FROM allocations "
            "WHERE request_id = :r AND blood_bank_id = :b"),
            {"r": request_id, "b": bank_id}))

    async def confirm(self, request_id: UUID, bank_id: UUID, now: datetime, pickup_until: datetime) -> int:
        """PENDING -> CONFIRMED, only while still pending AND not yet expired (an expired hold is never revived)."""
        result = await self.session.execute(text("""
            UPDATE allocations SET confirmed_at = :now, expires_at = :until
            WHERE request_id = :r AND blood_bank_id = :b
              AND released_at IS NULL AND confirmed_at IS NULL AND expires_at > :now
            RETURNING id"""), {"now": now, "until": pickup_until, "r": request_id, "b": bank_id})
        return len(result.all())

    async def reject(self, request_id: UUID, bank_id: UUID, now: datetime) -> list[UUID]:
        """PENDING -> released('rejected'). Returns the units to put back (or discard)."""
        result = await self.session.execute(text("""
            UPDATE allocations SET released_at = :now, release_reason = 'rejected'
            WHERE request_id = :r AND blood_bank_id = :b AND released_at IS NULL AND confirmed_at IS NULL
            RETURNING blood_unit_id"""), {"now": now, "r": request_id, "b": bank_id})
        return [r[0] for r in result]

    async def expire(self, request_id: UUID, now: datetime) -> list:
        """Release every PENDING hold of this request whose confirm deadline has passed. The WHERE makes it exactly-once:
        a hold already released (by a parallel expiry run, a reject or a cancel) no longer matches. Returns (unit, bank).
        CONFIRMED holds are never auto-released: past the pickup deadline they turn OVERDUE and a human resolves them,
        because the units may already be in a patient (ADR 021). A COLLECTED hold never matches either: collecting sets
        confirmed_at (CHECK allocations_collected_is_live), so `confirmed_at IS NULL` excludes it."""
        result = await self.session.execute(text("""
            UPDATE allocations SET released_at = :now, release_reason = 'expired'
            WHERE request_id = :r AND released_at IS NULL AND confirmed_at IS NULL AND expires_at <= :now
            RETURNING blood_unit_id, blood_bank_id"""), {"now": now, "r": request_id})
        return list(result)

    async def release_overdue(self, request_id: UUID, bank_id: UUID, now: datetime) -> list[UUID]:
        """The bank takes back its CONFIRMED hold once the pickup deadline has passed (OVERDUE -> released 'released')."""
        result = await self.session.execute(text("""
            UPDATE allocations SET released_at = :now, release_reason = 'released'
            WHERE request_id = :r AND blood_bank_id = :b
              AND released_at IS NULL AND confirmed_at IS NOT NULL AND collected_at IS NULL AND expires_at <= :now
            RETURNING blood_unit_id"""), {"now": now, "r": request_id, "b": bank_id})
        return [r[0] for r in result]

    async def release_all(self, request_id: UUID, now: datetime, reason: str = "cancelled") -> list[UUID]:
        """Every live, UNCOLLECTED hold of the request. Collected units left the bank: they never come back."""
        result = await self.session.execute(text("""
            UPDATE allocations SET released_at = :now, release_reason = :reason
            WHERE request_id = :r AND released_at IS NULL AND collected_at IS NULL RETURNING blood_unit_id"""),
            {"now": now, "reason": reason, "r": request_id})
        return [r[0] for r in result]

    async def mark_collected(self, request_id: UUID, bank_id: UUID, now: datetime) -> list[UUID]:
        """Any live hold of this bank (PENDING, CONFIRMED or OVERDUE) -> COLLECTED: the units physically left the bank.
        Collecting proves the units existed, so it confirms too. Returns the units to mark ISSUED; [] on a repeat."""
        result = await self.session.execute(text("""
            UPDATE allocations SET collected_at = :now, confirmed_at = coalesce(confirmed_at, :now)
            WHERE request_id = :r AND blood_bank_id = :b AND released_at IS NULL AND collected_at IS NULL
            RETURNING blood_unit_id"""), {"now": now, "r": request_id, "b": bank_id})
        return [r[0] for r in result]

    async def requests_with_expired_holds(self, now: datetime, limit: int = 100) -> list[UUID]:
        """Requests with a PENDING hold past its deadline, the most overdue first (a backlog drains oldest-first)."""
        rows = await self.session.execute(text("""
            SELECT a.request_id FROM allocations a JOIN emergency_requests e ON e.id = a.request_id
            WHERE a.released_at IS NULL AND a.confirmed_at IS NULL AND a.expires_at <= :now
              AND e.status IN ('OPEN', 'ALLOCATED')
            GROUP BY a.request_id ORDER BY min(a.expires_at) LIMIT :n"""), {"now": now, "n": limit})
        return [r[0] for r in rows]

    async def lock_live_request(self, request_id: UUID) -> EmergencyRequest | None:
        """For the expiry job: lock an OPEN/ALLOCATED request, or skip it if a user action holds it right now
        (SKIP LOCKED: the next tick picks it up). FULFILLED/CANCELLED requests are never returned."""
        stmt = (select(EmergencyRequest)
                .where(EmergencyRequest.id == request_id, EmergencyRequest.status.in_(("OPEN", "ALLOCATED")))
                .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        return await self.session.scalar(stmt)

    async def holds(self, request_id: UUID, now: datetime) -> list:
        """Per bank and state, for the live view: (blood_bank_id, status, units, expires_at).
        ISSUED = collected (units left the bank). OVERDUE = confirmed, pickup deadline passed, not resolved yet."""
        return list(await self.session.execute(text("""
            SELECT a.blood_bank_id,
                   CASE WHEN a.released_at IS NOT NULL THEN upper(a.release_reason)
                        WHEN a.collected_at IS NOT NULL THEN 'ISSUED'
                        WHEN a.confirmed_at IS NULL THEN 'PENDING'
                        WHEN a.expires_at <= :now THEN 'OVERDUE' ELSE 'CONFIRMED' END AS status,
                   count(*) AS units, min(a.expires_at) AS expires_at, min(a.allocated_at) AS first_at
            FROM allocations a WHERE a.request_id = :r
            GROUP BY 1, 2 ORDER BY first_at, 1, 2"""), {"r": request_id, "now": now}))

    async def holds_of_bank(self, bank_id: UUID, now: datetime) -> list:
        """A blood bank's live, uncollected holds on live requests, oldest deadline first. Request facts only, no patient data."""
        return list(await self.session.execute(text("""
            SELECT e.id AS request_id, e.code, e.hospital_ref, e.hospital_id, e.blood_group, e.component, e.urgency,
                   CASE WHEN a.confirmed_at IS NULL THEN 'PENDING'
                        WHEN a.expires_at <= :now THEN 'OVERDUE' ELSE 'CONFIRMED' END AS status,
                   count(*) AS units, min(a.expires_at) AS expires_at
            FROM allocations a JOIN emergency_requests e ON e.id = a.request_id
            WHERE a.blood_bank_id = :b AND a.released_at IS NULL AND a.collected_at IS NULL
              AND e.status IN ('OPEN', 'ALLOCATED')
            GROUP BY e.id, 8 ORDER BY min(a.expires_at), e.id"""), {"b": bank_id, "now": now}))


class ResponseRepository:
    """donor_responses: the delivery log AND the donor's answer. (request_id, donor_id) is the primary key."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def mark_notified(self, request_id: UUID, donor_id: UUID, now: datetime) -> bool:
        """True only the FIRST time: a duplicate outbox message (retry, double click) finds the row and does nothing."""
        result = await self.session.execute(
            text("INSERT INTO donor_responses (request_id, donor_id, status, notified_at) "
                 "VALUES (:r, :d, 'NOTIFIED', :now) ON CONFLICT DO NOTHING RETURNING donor_id"),
            {"r": request_id, "d": donor_id, "now": now},
        )
        return result.first() is not None

    async def respond(self, request_id: UUID, donor_id: UUID, status: str, now: datetime) -> bool:
        result = await self.session.execute(
            text("UPDATE donor_responses SET status = :s, responded_at = :now "
                 "WHERE request_id = :r AND donor_id = :d RETURNING donor_id"),
            {"s": status, "now": now, "r": request_id, "d": donor_id},
        )
        return result.first() is not None

    async def for_request(self, request_id: UUID) -> list:
        return list(await self.session.execute(
            text("SELECT donor_id, status, notified_at, responded_at FROM donor_responses "
                 "WHERE request_id = :r ORDER BY notified_at, donor_id"), {"r": request_id}))

    async def inbox(self, donor_id: UUID, limit: int = 20, request_id: UUID | None = None) -> list:
        """Newest first. request_id: just that one item (a reply to an old request is not in the newest 20)."""
        only = "AND r.request_id = :r" if request_id else ""
        return list(await self.session.execute(text(f"""
            SELECT e.id AS request_id, e.hospital_id, e.blood_group, e.component, e.urgency, e.status AS request_status,
                   r.status AS my_status, r.notified_at
            FROM donor_responses r JOIN emergency_requests e ON e.id = r.request_id
            WHERE r.donor_id = :d {only}
            ORDER BY r.notified_at DESC LIMIT :n"""), {"d": donor_id, "n": limit, "r": request_id}))
