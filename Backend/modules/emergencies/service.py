"""Allocation: the one place that coordinates organizations (where are the banks?) and inventory (reserve units)."""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from audit import AuditRepository
from config import get_settings
from domain import BloodGroup, compatible_groups, donation_interval_days, is_eligible
from events import publish
from models import Donor
from modules.auth import service as auth
from modules.inventory import service as inventory
from modules.organizations import service as organizations

from .models import EmergencyRequest
from .repository import EmergencyRepository, ResponseRepository
from .state import status_after_allocation

log = logging.getLogger("raktdoot.emergencies")


@dataclass(frozen=True)
class HoldWindows:
    confirm: timedelta              # PENDING lives this long
    pickup: timedelta               # CONFIRMED lives this long
    cap: int                        # max live held units per hospital

    @classmethod
    def from_settings(cls, settings) -> "HoldWindows":
        return cls(timedelta(minutes=settings.hold_confirm_minutes), timedelta(hours=settings.hold_pickup_hours),
                   settings.max_held_units_per_hospital)


async def allocate_remaining(
    session: AsyncSession, repo: EmergencyRepository, req: EmergencyRequest,
    hospital_at: tuple[float, float], radius_m: float, now: datetime, windows: HoldWindows,
) -> tuple[int, bool]:
    """Hold whatever this request still lacks at the nearest blood banks. Returns (units newly held, cap_reached).
    Runs inside the caller's transaction, with the request row already locked (or just inserted).

    Each new hold is PENDING until its bank confirms (expires after windows.confirm), or CONFIRMED at once if the bank
    auto-confirms (expires after windows.pickup). Only banks that explicitly rejected this request are never asked again
    (excluded_banks); a bank that let its hold expire is asked again (ADR 021 revision 2). Collected units count as
    "have" (they cover the need) but not against the cap (they are not hoarded)."""
    have = len(await repo.live_allocations(req.id))
    missing = req.units_needed - have
    if missing <= 0:
        return 0, False
    await repo.lock_hospital_cap(req.hospital_id)              # one allocation per hospital at a time (anti-hoarding)
    room = max(windows.cap - await repo.held_by_hospital(req.hospital_id), 0)
    wanted = min(missing, room)
    capped = wanted < missing
    groups = [g.value for g in compatible_groups(BloodGroup(req.blood_group), req.component)]
    # B4: a unit is held for at most confirm + pickup (a PENDING hold confirmed at the last minute gets the full pickup
    # window), so only units still in date at that latest pickup moment are promised. Expiring sooner = never held.
    usable_until = now + windows.confirm + windows.pickup
    # B6: every filter runs BEFORE nearby()'s LIMIT, inside its `among`: staffed (no staff = nobody to confirm, D88),
    # holding compatible in-date stock, and not a bank that rejected this request. Filtering the 50 nearest afterwards
    # meant bank #51 with stock was never tried when 50 nearer banks were empty or had said no.
    excluded = await repo.excluded_banks(req.id)
    with_stock = set(await inventory.banks_with_stock(session, groups, req.component, usable_until))
    candidates = [b for b in await auth.orgs_with_active_staff(session, "blood_bank")
                  if b in with_stock and b not in excluded]
    banks = [b.id for b in await organizations.nearby(session, "blood_bank", *hospital_at, radius_m, among=candidates)]
    units = await inventory.reserve(session, banks, groups, req.blood_group, req.component, wanted, usable_until)
    auto = {i for i, f in (await organizations.facilities(session, list({u.blood_bank_id for u in units}))).items()
            if f.auto_confirm_holds}
    await repo.add_allocations([
        {"id": uuid.uuid4(), "request_id": req.id, "blood_unit_id": u.id, "blood_bank_id": u.blood_bank_id,
         "blood_group": u.blood_group, "allocated_at": now,
         "confirmed_at": now if u.blood_bank_id in auto else None,
         "expires_at": now + (windows.pickup if u.blood_bank_id in auto else windows.confirm)}
        for u in units
    ])
    req.status = status_after_allocation(req.units_needed, have + len(units))
    req.updated_at = now
    return len(units), capped


async def recompute(repo: EmergencyRepository, req: EmergencyRequest, now: datetime) -> None:
    """After holds were lost: ALLOCATED -> OPEN when the live holds no longer cover the need.
    ponytail: no automatic re-allocation (the hospital clicks "check blood banks again"); upgrade = call
    allocate_remaining here, once the cap and the rejected-bank rule have been watched in real use."""
    req.status = status_after_allocation(req.units_needed, len(await repo.live_allocations(req.id)))
    req.updated_at = now


async def expire_holds(sessionmaker, now: datetime) -> int:
    """The worker's hold-expiry pass (ADR 021): PENDING holds only. Returns how many holds were released.

    Exactly-once without a queue: each request is locked FOR UPDATE SKIP LOCKED (busy with a user action -> next tick),
    and the release is ONE conditional UPDATE (`WHERE released_at IS NULL AND expires_at <= now`), so a hold released by
    a parallel run, a reject or a cancel no longer matches. CONFIRMED holds are never
    auto-released: they turn OVERDUE. Units go back with a guarded RESERVED -> AVAILABLE."""
    async with sessionmaker() as session:
        ids = await EmergencyRepository(session).requests_with_expired_holds(now)
    released = 0
    for request_id in ids:
        async with sessionmaker() as session, session.begin():
            repo = EmergencyRepository(session)
            req = await repo.lock_live_request(request_id)
            if req is None:
                continue
            rows = await repo.expire(req.id, now)
            if not rows:
                continue
            await inventory.set_status(session, [r.blood_unit_id for r in rows], "RESERVED", "AVAILABLE")
            await recompute(repo, req, now)
            audit = AuditRepository(session)
            for bank_id in sorted({r.blood_bank_id for r in rows}):
                await audit.add(now, "hold.expired", "emergency", req.id,
                                {"blood_bank_id": str(bank_id), "units": sum(r.blood_bank_id == bank_id for r in rows)})
            await publish(session, req.id, "hold_expired")
            released += len(rows)
    return released


async def notify_donor(session: AsyncSession, payload: dict, notifier, now: datetime) -> None:
    """Outbox handler for topic 'donor.notify'. Idempotent: safe to run twice for the same message.

    Order matters: record first, send second, both inside the worker's transaction. If the send fails, the record rolls
    back with it and the retry tries again. If the process dies AFTER sending but before COMMIT, the donor can get a
    second message (at-least-once). Accepted: a duplicate "can you donate?" beats a lost one."""

    request_id, donor_id = uuid.UUID(payload["request_id"]), uuid.UUID(payload["donor_id"])
    req = await session.get(EmergencyRequest, request_id)
    if req is None or req.status not in ("OPEN", "ALLOCATED"):
        return                                         # cancelled or fulfilled meanwhile: do not bother the donor
    # B8: the donor was matchable when the hospital clicked; minutes (or a retry backoff) later they may have opted
    # out, withdrawn consent, changed their phone or donated. Re-check at send time, with the same rules as matching.
    if not still_reachable(await session.get(Donor, donor_id), now):
        log.info("donor.notify skipped for request %s: donor no longer matchable", request_id)   # ids only, no PII
        return
    if await ResponseRepository(session).mark_notified(request_id, donor_id, now):
        await notifier.send(donor_id, request_id)
        await publish(session, request_id, "notified")


def still_reachable(donor: Donor | None, now: datetime) -> bool:
    """The send-time copy of nearby_eligible_donors' WHERE clause (available, verified phone, consent, interval).
    Settings come from get_settings(), the same environment the worker reads at startup."""
    if donor is None:
        return False
    settings = get_settings()
    interval = donation_interval_days(donor.sex, settings.donation_interval_male_days,
                                      settings.donation_interval_female_days)
    return (donor.is_available and donor.phone_verified_at is not None and donor.consent_at is not None
            and is_eligible(donor.last_donated_at, now, interval))


HANDLERS = {"donor.notify": notify_donor}
