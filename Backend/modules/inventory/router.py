from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from deps import Audit, Now, Principal, Session, get_session, require
from domain import BloodGroup
from errors import Problem
from pagination import decode_cursor, encode_cursor

from . import service
from .repository import BloodUnitRepository, UnknownOrganization
from .schemas import (
    BloodUnitCreate,
    BloodUnitPage,
    BloodUnitRead,
    Component,
    StockLine,
    UnitStatus,
)
from .shelf_life import shelf_life_error


def get_unit_repo(session: Annotated[AsyncSession, Depends(get_session)]) -> BloodUnitRepository:
    return BloodUnitRepository(session)


UnitRepo = Annotated[BloodUnitRepository, Depends(get_unit_repo)]

router = APIRouter(prefix="/blood-units", tags=["inventory"])

# ponytail: no PATCH or DELETE. A unit's status moves AVAILABLE -> RESERVED -> ISSUED / EXPIRED through the allocation
# rules of Stage 3 (locks, expiry job); a generic PATCH would let anyone bypass rules that do not exist yet.
# ponytail: any organization may hold stock. Restricting it to kind = blood_bank needs a cross-module call, which we add
# with the first rule that needs it (3.3 / 3.4). Both recorded in docs/adr/012-inventory-minimal-scope.md.


@router.post("", response_model=BloodUnitRead, status_code=201)
async def register_unit(
    payload: BloodUnitCreate, response: Response, session: Session, repo: UnitRepo, audit: Audit, now: Now,
    principal: Principal = require("blood_bank", "admin"),
):
    # object-level (BOLA): blood-bank staff add stock to THEIR bank only. Checked before validation, so a stranger
    # learns nothing about the payload rules.
    if principal.role != "admin" and principal.organization_id != payload.blood_bank_id:
        raise Problem(403, "forbidden", "Forbidden", "You can only add stock to your own blood bank.")
    reason = shelf_life_error(payload.component.value, payload.collected_at, payload.expires_at, now)
    if reason:
        raise Problem(422, "invalid-shelf-life", "Invalid shelf life", reason)
    try:
        async with session.begin():             # insert + audit event: ONE transaction
            unit = await repo.add(
                {
                    "blood_bank_id": payload.blood_bank_id,
                    "blood_group": payload.blood_group.value,
                    "component": payload.component.value,
                    "collected_at": payload.collected_at,
                    "expires_at": payload.expires_at,
                }
            )
            await audit.add(now, "blood_unit.registered", "blood_unit", unit.id, actor_id=principal.id)
    except UnknownOrganization:
        raise Problem(
            422, "unknown-organization", "Unknown organization", "blood_bank_id does not match any organization."
        ) from None
    response.headers["Location"] = f"/v1/blood-units/{unit.id}"
    return BloodUnitRead.model_validate(unit)


def own_bank_only(principal: Principal, blood_bank_id: UUID | None) -> UUID | None:
    """B5: unit lists show live hold activity per bank (RESERVED, ISSUED), so they are staff-only: an admin sees any
    bank, a blood bank's staff only their own (no filter = their own). Families use /v1/public/nearest-banks."""
    if principal.role == "admin":
        return blood_bank_id
    if blood_bank_id not in (None, principal.organization_id):
        raise Problem(403, "forbidden", "Forbidden", "You can only list your own blood bank's units.")
    return principal.organization_id


@router.get("", response_model=BloodUnitPage)
async def list_units(
    repo: UnitRepo,
    principal: Principal = require("blood_bank", "admin"),
    blood_bank_id: UUID | None = None,
    blood_group: BloodGroup | None = None,
    component: Component | None = None,
    status: UnitStatus | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
):
    after = decode_cursor(cursor) if cursor else None
    rows = await repo.list_page(
        own_bank_only(principal, blood_bank_id),
        blood_group.value if blood_group else None,
        component.value if component else None,
        status.value if status else None,
        after,
        limit,
    )
    page, has_more = rows[:limit], len(rows) > limit
    return BloodUnitPage(
        items=[BloodUnitRead.model_validate(u) for u in page],
        next_cursor=encode_cursor(page[-1].expires_at, page[-1].id) if has_more else None,
    )


# Declared before /{unit_id}: routes match in order, and "summary" is not a UUID.
@router.get("/summary", response_model=list[StockLine])
async def stock_summary(blood_bank_id: UUID, session: Session, now: Now,
                        principal: Principal = require("blood_bank", "admin")):
    """Usable units per group and component at ONE bank: the stock-take form is pre-filled from this. That bank's
    staff or an admin (reading is fine for an admin; counting is not, see POST /organizations/{id}/stock-take)."""
    if principal.role != "admin" and principal.organization_id != blood_bank_id:
        raise Problem(403, "forbidden", "Forbidden", "You can only read your own blood bank's stock summary.")
    return await service.summary(session, blood_bank_id, now)


@router.get("/{unit_id}", response_model=BloodUnitRead)
async def get_unit(unit_id: UUID, repo: UnitRepo, principal: Principal = require("blood_bank", "admin")):
    unit = await repo.get(unit_id)
    # another bank's unit: the same 404 as a missing one (object-level, no "this id exists" oracle)
    if unit is None or (principal.role != "admin" and unit.blood_bank_id != principal.organization_id):
        raise Problem(404, "blood-unit-not-found", "Blood unit not found")
    return BloodUnitRead.model_validate(unit)
