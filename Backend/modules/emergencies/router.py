import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from audit import AuditRepository
from config import Settings
from deps import Audit, Config, CurrentUser, Now, Principal, Session, get_session, require
from domain import BloodGroup, compatible_groups, eligibility_cutoffs
from errors import Problem
from events import publish
from modules.inventory import service as inventory
from modules.organizations import service as organizations
from outbox import OutboxRepository
from repository import DonorRepository, IdempotencyRepository, donors_by_ids, nearby_eligible_donors
from security import short_code

from .models import EmergencyRequest
from .repository import EmergencyRepository, ResponseRepository
from .schemas import (
    AllocationRead,
    BankHold,
    CloseRequest,
    DonorMatch,
    DonorReply,
    EmergencyCreate,
    EmergencyRead,
    HoldRead,
    HoldReject,
    InboxItem,
    NotifyResult,
    ResponseRead,
)
from .service import HoldWindows, allocate_remaining, recompute
from .state import can_move


def get_repo(session: Annotated[AsyncSession, Depends(get_session)]) -> EmergencyRepository:
    return EmergencyRepository(session)


def get_idem(session: Annotated[AsyncSession, Depends(get_session)]) -> IdempotencyRepository:
    return IdempotencyRepository(session)


Repo = Annotated[EmergencyRepository, Depends(get_repo)]
Idem = Annotated[IdempotencyRepository, Depends(get_idem)]
router = APIRouter(prefix="/emergencies", tags=["emergencies"])


async def present(repo: EmergencyRepository, req: EmergencyRequest, now: datetime,
                  cap_reached: bool = False) -> EmergencyRead:
    allocations = await repo.live_allocations(req.id)
    holds = await repo.holds(req.id, now)
    banks = await organizations.facilities(repo.session, list({h.blood_bank_id for h in holds}))
    return EmergencyRead(
        id=req.id, code=req.code, hospital_ref=req.hospital_ref, hospital_id=req.hospital_id,
        blood_group=req.blood_group, component=req.component,
        units_needed=req.units_needed, units_allocated=len(allocations),
        units_collected=sum(a.collected_at is not None for a in allocations), urgency=req.urgency, status=req.status,
        created_at=req.created_at, allocations=[AllocationRead.model_validate(a) for a in allocations],
        holds=[HoldRead(blood_bank_id=h.blood_bank_id, bank_name=banks[h.blood_bank_id].name,
                        bank_phone=banks[h.blood_bank_id].phone, bank_is_demo=banks[h.blood_bank_id].is_demo,
                        units=h.units, status=h.status, expires_at=h.expires_at)
               for h in holds],
        cap_reached=cap_reached,
    )


async def owned(repo: EmergencyRepository, request_id: UUID, principal: Principal, lock: bool = False) -> EmergencyRequest:
    """Object-level check (BOLA): staff of the requesting hospital, or an admin. Anyone else gets the same 404."""
    req = await repo.get(request_id, lock=lock)
    if req is None or (principal.role != "admin" and req.hospital_id != principal.organization_id):
        raise Problem(404, "emergency-not-found", "Emergency request not found")
    return req


@router.post("", response_model=EmergencyRead, status_code=201)
async def create_emergency(
    payload: EmergencyCreate, response: Response, session: Session, repo: Repo, idem: Idem, audit: Audit,
    now: Now, settings: Config,
    staff: Principal = require("hospital"),
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,   # B9: <=255 in the DB
):
    """Post an emergency and, in the SAME transaction, reserve the nearest compatible units.

    Idempotency-Key matters more here than anywhere: a hospital retrying after a timeout must not reserve
    a second set of units that another patient needs (ADR 007)."""
    key = f"{staff.id}:{idempotency_key}" if idempotency_key else None
    request_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    async with session.begin():
        if key and not await idem.claim(key, request_hash):
            saved = await idem.get(key)
            if saved.request_hash != request_hash:
                raise Problem(422, "idempotency-key-reuse", "Idempotency key reused",
                              "This key was already used with a different request body.")
            body = json.loads(saved.response_body)
            return Response(saved.response_body, saved.response_status, media_type="application/json",
                            headers={"Location": f"/v1/emergencies/{body['id']}", "Idempotent-Replayed": "true"})
        hospital_at = await organizations.location_of(session, staff.organization_id)
        if hospital_at is None:
            raise Problem(422, "hospital-has-no-location", "Hospital has no location",
                          "Ask an admin to set your hospital's latitude and longitude first.")
        row = {"hospital_id": staff.organization_id, "created_by": staff.id, "blood_group": payload.blood_group.value,
               "component": payload.component, "units_needed": payload.units_needed, "urgency": payload.urgency,
               "hospital_ref": payload.hospital_ref, "status": "OPEN", "created_at": now, "updated_at": now}
        for attempt in (1, 2):                  # a code collision (1 in ~1e12 per pair) retries once with a new code
            try:
                async with session.begin_nested():          # SAVEPOINT: a unique violation must not abort the claim
                    req = await repo.add({**row, "id": uuid.uuid4(), "code": short_code()})
                break
            except IntegrityError as exc:
                if attempt == 2 or "emergency_requests_code_key" not in str(exc.orig):
                    raise
        reserved, capped = await allocate_remaining(session, repo, req, hospital_at,
                                                    settings.allocation_radius_km * 1000, now,
                                                    HoldWindows.from_settings(settings))
        # counts only: no patient detail ever goes into the audit log
        await audit.add(now, "emergency.created", "emergency", req.id,
                        {"units_needed": req.units_needed, "units_reserved": reserved, "capped": capped},
                        actor_id=staff.id)
        await publish(session, req.id, "created")
        result = await present(repo, req, now, cap_reached=capped)
        if key:
            await idem.finish(key, 201, result.model_dump_json())
    response.headers["Location"] = f"/v1/emergencies/{req.id}"
    return result


@router.get("", response_model=list[EmergencyRead])
async def list_emergencies(repo: Repo, now: Now, principal: Principal = require("hospital", "admin")):
    rows = await repo.recent(None if principal.role == "admin" else principal.organization_id)
    # ponytail: one allocations query per request (N+1). Fine at 50 rows; one JOIN when a profile shows it matters.
    return [await present(repo, r, now) for r in rows]


@router.get("/donor-inbox", response_model=list[InboxItem])
async def donor_inbox(session: Session, principal: Principal = require("donor")):
    """The emergencies this donor was asked to help with, newest first. Declared BEFORE /{request_id}:
    routes match in order, and "donor-inbox" is not a UUID."""
    me = await DonorRepository(session).get_by_user(principal.id)
    if me is None:
        return []
    return await inbox_items(session, await ResponseRepository(session).inbox(me.id))


async def inbox_items(session: AsyncSession, rows: list) -> list[InboxItem]:
    names = await organizations.names_of(session, list({r.hospital_id for r in rows}))
    return [InboxItem(request_id=r.request_id, hospital_name=names[r.hospital_id][0], hospital_city=names[r.hospital_id][1],
                      blood_group=r.blood_group, component=r.component, urgency=r.urgency,
                      request_status=r.request_status, my_status=r.my_status, notified_at=r.notified_at)
            for r in rows]


@router.get("/holds", response_model=list[BankHold])
async def my_bank_holds(session: Session, repo: Repo, now: Now, staff: Principal = require("blood_bank")):
    """The calling blood bank's PENDING, CONFIRMED and OVERDUE holds on live requests. Declared BEFORE /{request_id}."""
    rows = await repo.holds_of_bank(staff.organization_id, now)
    hospitals = await organizations.facilities(session, list({r.hospital_id for r in rows}))
    return [bank_hold(r.request_id, r.code, r.hospital_ref, hospitals[r.hospital_id], r.blood_group, r.component, r.units,
                      r.urgency, r.status, r.expires_at) for r in rows]


def bank_hold(request_id, code, hospital_ref, hospital, blood_group, component, units, urgency, status,
              expires_at) -> BankHold:
    """The one place a bank row is built (GET /holds and the confirm / reject / release answers): same shape both ways."""
    return BankHold(request_id=request_id, code=code, hospital_ref=hospital_ref, hospital_name=hospital.name,
                    hospital_phone=hospital.phone, hospital_is_demo=hospital.is_demo, blood_group=blood_group,
                    component=component, units=units, urgency=urgency, status=status, expires_at=expires_at)


async def bank_action(repo: EmergencyRepository, request_id: UUID, staff: Principal) -> EmergencyRequest:
    """Lock the request; 404 unless the calling bank holds (or held) units for it: another bank learns nothing."""
    req = await repo.get(request_id, lock=True)
    if req is None or not await repo.bank_holds(request_id, staff.organization_id):
        raise Problem(404, "hold-not-found", "Hold not found", "Your blood bank has no hold on this request.")
    return req


async def bank_view(repo: EmergencyRepository, req: EmergencyRequest, bank_id: UUID, now: datetime) -> BankHold:
    """What confirm / reject / release return to a bank: ITS part of the request only, never other banks' holds or
    unit ids (same shape as GET /holds). Its live hold if it has one, else its latest released one."""
    mine = [h for h in await repo.holds(req.id, now) if h.blood_bank_id == bank_id]
    live = [h for h in mine if h.status in LIVE_HOLD]
    h = (live or mine)[-1]
    hospital = (await organizations.facilities(repo.session, [req.hospital_id]))[req.hospital_id]
    return bank_hold(req.id, req.code, req.hospital_ref, hospital, req.blood_group, req.component, h.units, req.urgency,
                     h.status, h.expires_at)


LIVE_HOLD = ("PENDING", "CONFIRMED", "OVERDUE", "ISSUED")
LIVE_REQUEST = ("OPEN", "ALLOCATED")
NOT_PENDING = ("hold-not-pending", "Hold not pending")


def unit_expired() -> Problem:
    """B4: a held unit passed its label expiry. Never confirmed or issued: the bank rejects (not_in_stock discards it)
    or releases the hold, and the hospital asks for blood again."""
    return Problem(409, "unit-expired", "Held unit expired",
                   "A unit in this hold is past its expiry date and cannot be issued. Reject the hold and discard it.")


@router.post("/{request_id}/holds/confirm", response_model=BankHold)
async def confirm_hold(request_id: UUID, session: Session, repo: Repo, audit: Audit, now: Now, settings: Config,
                       staff: Principal = require("blood_bank")):
    """The bank checked its shelf: the held units are real and set aside. Starts the pickup window.
    Bank staff only, never an admin: only the people at the shelf can say the blood is there (ADR 021)."""
    async with session.begin():
        req = await bank_action(repo, request_id, staff)
        live = [h for h in await repo.bank_holds(request_id, staff.organization_id) if h.released_at is None]
        if live and all(h.confirmed_at is not None for h in live):
            return await bank_view(repo, req, staff.organization_id, now)    # repeat click: changes nothing
        if await inventory.any_expired(session, [h.blood_unit_id for h in live if h.confirmed_at is None], now):
            raise unit_expired()
        n = 0
        if req.status in LIVE_REQUEST:
            n = await repo.confirm(req.id, staff.organization_id, now, now + HoldWindows.from_settings(settings).pickup)
        if n == 0:
            raise Problem(409, *NOT_PENDING, "This hold has expired or was released; it cannot be confirmed.")
        await audit.add(now, "hold.confirmed", "emergency", req.id,
                        {"blood_bank_id": str(staff.organization_id), "units": n}, actor_id=staff.id)
        await publish(session, req.id, "hold_confirmed")
        return await bank_view(repo, req, staff.organization_id, now)


@router.post("/{request_id}/holds/reject", response_model=BankHold)
async def reject_hold(request_id: UUID, body: HoldReject, session: Session, repo: Repo, audit: Audit, now: Now,
                      staff: Principal = require("blood_bank")):
    """The bank cannot supply. not_in_stock: the units do not exist (DISCARDED, so nobody is promised them again);
    cannot_supply: they exist but cannot go out (back to AVAILABLE). Either way this bank is not asked again for
    this request, and the request reopens if it no longer has enough held."""
    async with session.begin():
        req = await bank_action(repo, request_id, staff)
        units = await repo.reject(req.id, staff.organization_id, now) if req.status in LIVE_REQUEST else []
        if not units:
            raise Problem(409, *NOT_PENDING, "Only a pending hold can be rejected.")
        to = "DISCARDED" if body.reason == "not_in_stock" else "AVAILABLE"
        await inventory.set_status(session, units, "RESERVED", to)
        await recompute(repo, req, now)
        await audit.add(now, "hold.rejected", "emergency", req.id,
                        {"blood_bank_id": str(staff.organization_id), "units": len(units), "reason": body.reason},
                        actor_id=staff.id)
        await publish(session, req.id, "hold_rejected")
        return await bank_view(repo, req, staff.organization_id, now)


@router.post("/{request_id}/holds/release", response_model=BankHold)
async def release_hold(request_id: UUID, session: Session, repo: Repo, audit: Audit, now: Now,
                       staff: Principal = require("blood_bank")):
    """The bank takes back an OVERDUE hold (confirmed, pickup deadline passed, the hospital never said "received" or
    "cancel"). Nothing releases a confirmed hold automatically: the units may already be in a patient, and only the
    bank knows whether they are still on its shelf (ADR 021). Units back to AVAILABLE, the request reopens if short.
    A repeat click returns the released hold again (idempotent)."""
    async with session.begin():
        req = await bank_action(repo, request_id, staff)
        units = await repo.release_overdue(req.id, staff.organization_id, now) if req.status in LIVE_REQUEST else []
        if not units:
            view = await bank_view(repo, req, staff.organization_id, now)
            if view.status == "RELEASED":
                return view
            raise Problem(409, "hold-not-overdue", "Hold not overdue",
                          "Only a confirmed hold past its pickup deadline, on an open request, can be released.")
        await inventory.set_status(session, units, "RESERVED", "AVAILABLE")
        await recompute(repo, req, now)
        await audit.add(now, "hold.released", "emergency", req.id,
                        {"blood_bank_id": str(staff.organization_id), "units": len(units)}, actor_id=staff.id)
        await publish(session, req.id, "hold_released")
        return await bank_view(repo, req, staff.organization_id, now)


async def collect(session: AsyncSession, repo: EmergencyRepository, audit: AuditRepository, req: EmergencyRequest,
                  bank_id: UUID, now: datetime, principal: Principal) -> int:
    """The units of this bank's live hold physically left the bank: ISSUED for good, whatever state the hold was in
    (PENDING, CONFIRMED, OVERDUE). Nothing releases a collected hold afterwards (ADR 021 revision 2). Returns units
    collected now; 0 on a repeat (and nothing is audited twice). Refuses (409) if a unit is past its label expiry."""
    uncollected = [h.blood_unit_id for h in await repo.bank_holds(req.id, bank_id)
                   if h.released_at is None and h.collected_at is None]
    if await inventory.any_expired(session, uncollected, now):
        raise unit_expired()
    units = await repo.mark_collected(req.id, bank_id, now)
    if units:
        await inventory.set_status(session, units, "RESERVED", "ISSUED")
        await audit.add(now, "hold.collected", "emergency", req.id,
                        {"blood_bank_id": str(bank_id), "units": len(units), "by": principal.role}, actor_id=principal.id)
        req.updated_at = now
    return len(units)


NOT_LIVE = ("hold-not-live", "Hold already released",
            "This hold was already released and its units went back to the bank's stock. Call the bank.")


@router.post("/{request_id}/holds/{bank_id}/collected", response_model=EmergencyRead)
async def hold_collected(request_id: UUID, bank_id: UUID, session: Session, repo: Repo, audit: Audit, now: Now,
                         staff: Principal = require("hospital")):
    """The ward attests it collected this bank's units. Hospital staff only, never an admin: only the people who
    received the blood can say so. A repeat click returns the request again (idempotent)."""
    async with session.begin():
        req = await owned(repo, request_id, staff, lock=True)
        if req.status not in LIVE_REQUEST:
            raise Problem(409, "invalid-transition", "Invalid transition", f"A {req.status} request is closed.")
        rows = await repo.bank_holds(req.id, bank_id)
        if not rows:
            raise Problem(404, "hold-not-found", "Hold not found", "That blood bank has no hold on this request.")
        if await collect(session, repo, audit, req, bank_id, now, staff):
            await publish(session, req.id, "hold_collected")
        elif not any(r.collected_at for r in rows):
            raise Problem(409, *NOT_LIVE)
        return await present(repo, req, now)


@router.post("/{request_id}/holds/issued", response_model=BankHold)
async def hold_issued(request_id: UUID, session: Session, repo: Repo, audit: Audit, now: Now,
                      staff: Principal = require("blood_bank")):
    """The bank handed its held units over (e.g. issued at the counter without the app): same effect as the hospital's
    "collected". The bank's own staff only. A repeat click returns the issued hold again (idempotent)."""
    async with session.begin():
        req = await bank_action(repo, request_id, staff)
        n = await collect(session, repo, audit, req, staff.organization_id, now, staff) if req.status in LIVE_REQUEST else 0
        view = await bank_view(repo, req, staff.organization_id, now)
        if n:
            await publish(session, req.id, "hold_collected")
        elif view.status != "ISSUED":
            raise Problem(409, *NOT_LIVE)
        return view


@router.get("/{request_id}", response_model=EmergencyRead)
async def get_emergency(request_id: UUID, repo: Repo, now: Now, principal: CurrentUser):
    return await present(repo, await owned(repo, request_id, principal), now)


@router.post("/{request_id}/allocate", response_model=EmergencyRead)
async def allocate_again(request_id: UUID, session: Session, repo: Repo, audit: Audit, now: Now, settings: Config,
                         principal: Principal = require("hospital", "admin")):
    """Retry for the missing units, e.g. after a blood bank registered new stock."""
    async with session.begin():
        req = await owned(repo, request_id, principal, lock=True)
        if req.status != "OPEN":
            raise Problem(409, "invalid-transition", "Invalid transition", f"A {req.status} request cannot allocate.")
        hospital_at = await organizations.location_of(session, req.hospital_id)
        reserved, capped = await allocate_remaining(session, repo, req, hospital_at,
                                                    settings.allocation_radius_km * 1000, now,
                                                    HoldWindows.from_settings(settings))
        await audit.add(now, "emergency.allocated", "emergency", req.id, {"units_reserved": reserved, "capped": capped},
                        actor_id=principal.id)
        await publish(session, req.id, "allocated")
        return await present(repo, req, now, cap_reached=capped)


async def finish(request_id: UUID, target: str, body: CloseRequest, session: AsyncSession, repo: EmergencyRepository,
                 audit: AuditRepository, now: datetime, principal: Principal) -> EmergencyRequest:
    """Closing always answers "which banks did you collect from?" (ADR 021 revision 2).
    FULFILLED: the listed banks' holds become collected (ISSUED); at least one unit must have been collected; every
    other live hold is released ('cancelled', units AVAILABLE). Works on an OPEN request too (partly fulfilled).
    CANCELLED: refused once anything was collected (that blood is in a patient); else every hold goes back."""
    async with session.begin():
        req = await owned(repo, request_id, principal, lock=True)
        if not can_move(req.status, target):
            raise Problem(409, "invalid-transition", "Invalid transition",
                          f"A {req.status} request cannot become {target}.")
        live = await repo.live_allocations(req.id)
        if target == "FULFILLED":
            if set(body.collected_from) - {a.blood_bank_id for a in live}:
                raise Problem(422, "unknown-bank", "Unknown blood bank",
                              "Every bank in collected_from must have a live hold on this request.")
            for bank_id in body.collected_from:
                await collect(session, repo, audit, req, bank_id, now, principal)
            collected = sum(a.collected_at is not None for a in await repo.live_allocations(req.id))
            if not collected:
                raise Problem(409, "nothing-collected", "Nothing was collected",
                              "Nothing was collected: cancel the request instead.")
            released = await repo.release_all(req.id, now, "cancelled")
            details = {"units_needed": req.units_needed, "units_collected": collected, "units_released": len(released)}
        else:
            if body.collected_from or any(a.collected_at is not None for a in live):
                raise Problem(409, "units-collected", "Units were collected",
                              "Some units were collected: close the request as received.")
            released = await repo.release_all(req.id, now, "cancelled")
            details = {"units": len(released)}
        await inventory.set_status(session, released, "RESERVED", "AVAILABLE")
        req.status, req.updated_at = target, now
        req.hospital_ref = None         # B13: the bed/IP number is only needed while blood is moving (minimisation)
        await audit.add(now, f"emergency.{target.lower()}", "emergency", req.id, details, actor_id=principal.id)
        await publish(session, req.id, target.lower())
    return req


@router.post("/{request_id}/fulfil", response_model=EmergencyRead)
async def fulfil(request_id: UUID, body: CloseRequest, session: Session, repo: Repo, audit: Audit, now: Now,
                 principal: Principal = require("hospital", "admin")):
    """Blood received: name the banks collected from now; whatever was not collected is released."""
    if principal.role == "admin" and body.collected_from:      # the ward attests receipt (ADR 021 rev 2), not an admin
        raise Problem(403, "forbidden", "Forbidden", "Only the hospital's own staff can say units were received.")
    return await present(repo, await finish(request_id, "FULFILLED", body, session, repo, audit, now, principal), now)


@router.post("/{request_id}/cancel", response_model=EmergencyRead)
async def cancel(request_id: UUID, body: CloseRequest, session: Session, repo: Repo, audit: Audit, now: Now,
                 principal: Principal = require("hospital", "admin")):
    """Nothing was collected: every hold goes back to its bank. `collected_from` must be empty."""
    return await present(repo, await finish(request_id, "CANCELLED", body, session, repo, audit, now, principal), now)


def distance_band(metres: float) -> str:
    """Bands, not exact distances: 2.31 km and a direction would let someone triangulate a donor's home."""
    for limit_km, band in ((2, "under 2 km"), (5, "2-5 km"), (10, "5-10 km"), (25, "10-25 km")):
        if metres < limit_km * 1000:
            return band
    return "25+ km"


async def donors_for(session: AsyncSession, repo: EmergencyRepository, request_id: UUID, principal: Principal,
                     now: datetime, settings: Settings, radius_km: float, limit: int) -> tuple[EmergencyRequest, list]:
    """The one donor lookup behind donor-matches and notify-donors: a live request of the caller's hospital, then
    compatible, eligible, available donors near that hospital, nearest first."""
    req = await owned(repo, request_id, principal)
    if req.status not in LIVE_REQUEST:
        raise Problem(409, "invalid-transition", "Request is closed", f"A {req.status} request does not need donors.")
    latitude, longitude = await organizations.location_of(session, req.hospital_id)
    groups = [g.value for g in compatible_groups(BloodGroup(req.blood_group), req.component)]
    rows = await nearby_eligible_donors(
        session, latitude, longitude, radius_km * 1000, groups,
        cutoffs=eligibility_cutoffs(now, settings.donation_interval_male_days, settings.donation_interval_female_days),
        limit=limit,
    )
    return req, rows


@router.get("/{request_id}/donor-matches", response_model=list[DonorMatch])
async def donor_matches(
    request_id: UUID, session: Session, repo: Repo, audit: Audit, now: Now, settings: Config,
    principal: Principal = require("hospital", "admin"),
    radius_km: float = Query(default=10, gt=0, le=50),
    limit: int = Query(default=20, ge=1, le=50),
):
    """When banks cannot cover a request: compatible, eligible, available donors near the hospital, nearest first.

    Need-to-know (threat model #2): only for a live request of YOUR hospital, capped radius and count, minimal fields,
    and every lookup is audited (who looked at donor data, and how many rows they got)."""
    async with session.begin():
        req, rows = await donors_for(session, repo, request_id, principal, now, settings, radius_km, limit)
        await audit.add(now, "emergency.donors_viewed", "emergency", req.id,
                        {"donors_returned": len(rows), "radius_km": radius_km}, actor_id=principal.id)
    return [DonorMatch(donor_id=r.id, first_name=r.name.split()[0], blood_group=r.blood_group,
                       distance_band=distance_band(r.distance_m)) for r in rows]


@router.post("/{request_id}/notify-donors", response_model=NotifyResult, status_code=202)
async def notify_donors(
    request_id: UUID, session: Session, repo: Repo, audit: Audit, now: Now, settings: Config,
    principal: Principal = require("hospital", "admin"),
    radius_km: float = Query(default=10, gt=0, le=50),
    limit: int = Query(default=20, ge=1, le=50),
):
    """Ask the nearest eligible donors for help. 202: the messages are QUEUED in the outbox in this transaction and
    delivered by the worker. If this transaction rolls back, no donor is ever contacted for it."""
    async with session.begin():
        req, rows = await donors_for(session, repo, request_id, principal, now, settings, radius_km, limit)
        outbox = OutboxRepository(session)
        for r in rows:
            await outbox.add("donor.notify", {"request_id": str(req.id), "donor_id": str(r.id)}, now)
        await audit.add(now, "emergency.donors_notified", "emergency", req.id,
                        {"donors_queued": len(rows), "radius_km": radius_km}, actor_id=principal.id)
    return NotifyResult(queued=len(rows))


@router.post("/{request_id}/responses", response_model=InboxItem)
async def reply(request_id: UUID, body: DonorReply, session: Session, audit: Audit, now: Now,
                principal: Principal = require("donor")):
    """A notified donor says yes or no. Only donors who were actually asked can answer (no row = 404)."""
    async with session.begin():
        me = await DonorRepository(session).get_by_user(principal.id)
        req = await EmergencyRepository(session).get(request_id)
        if me is None or req is None:
            raise Problem(404, "emergency-not-found", "Emergency request not found")
        if req.status not in ("OPEN", "ALLOCATED"):
            raise Problem(409, "invalid-transition", "Request is closed", "This request no longer needs donors.")
        status = "ACCEPTED" if body.accept else "DECLINED"
        if not await ResponseRepository(session).respond(request_id, me.id, status, now):
            raise Problem(404, "emergency-not-found", "Emergency request not found")     # never asked: same 404
        await audit.add(now, f"donor.{status.lower()}", "emergency", request_id, actor_id=principal.id)
        await publish(session, request_id, "response")
        (item,) = await inbox_items(session, await ResponseRepository(session).inbox(me.id, request_id=request_id))
    return item


async def responses_view(session: AsyncSession, request_id: UUID) -> list[ResponseRead]:
    rows = await ResponseRepository(session).for_request(request_id)
    donors = await donors_by_ids(session, [r.donor_id for r in rows])
    return [ResponseRead(donor_id=r.donor_id, first_name=donors[r.donor_id][0].split()[0],
                         blood_group=donors[r.donor_id][1], status=r.status, responded_at=r.responded_at)
            for r in rows]


@router.get("/{request_id}/responses", response_model=list[ResponseRead])
async def responses(request_id: UUID, session: Session, repo: Repo, principal: Principal = require("hospital", "admin")):
    """Who the hospital asked and what they said: first name and group only (minimisation, as in donor-matches)."""
    req = await owned(repo, request_id, principal)
    return await responses_view(session, req.id)


TERMINAL = ("FULFILLED", "CANCELLED")


@router.get("/{request_id}/events")
async def live_events(request_id: UUID, request: Request, principal: Principal = require("hospital", "admin")):
    """Server-Sent Events: the request's full state now, then again after every committed change (docs/adr/018).

    No Session dependency on purpose: it would hold a pooled DB connection for the whole life of the stream, and
    10 open dashboards would exhaust a pool of 10. Each snapshot borrows a connection for milliseconds instead."""
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as s:                           # auth check, then the connection goes back
        await owned(EmergencyRepository(s), request_id, principal)
    hub, heartbeat = request.app.state.events, request.app.state.settings.sse_heartbeat_seconds

    async def snapshot() -> tuple[str, bool]:
        async with sessionmaker() as s:
            repo = EmergencyRepository(s)
            req = await repo.get(request_id)
            body = {"request": (await present(repo, req, datetime.now(timezone.utc))).model_dump(mode="json"),
                    "responses": [r.model_dump(mode="json") for r in await responses_view(s, request_id)]}
        return f"event: snapshot\ndata: {json.dumps(body)}\n\n", req.status in TERMINAL

    async def stream():
        with hub.subscribe(str(request_id)) as queue:         # subscribe BEFORE the first snapshot: no missed change
            frame, done = await snapshot()
            yield frame
            while not done:                                   # a closed request has nothing more to say
                # B11: authorised once, at connect, so the stream must not outlive the token: at its exp the stream
                # ends and the client reconnects with a fresh token (a deactivated user cannot get one).
                left = (principal.expires_at - datetime.now(timezone.utc)).total_seconds()
                if left <= 0:
                    break
                try:
                    await asyncio.wait_for(queue.get(), min(heartbeat, left))
                except TimeoutError:
                    yield ": heartbeat\n\n"                 # keeps proxies from closing an idle connection
                    continue
                frame, done = await snapshot()
                yield frame

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
