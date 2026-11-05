from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from deps import Audit, CurrentUser, Now, Principal, Session, get_session, require
from errors import Problem
from modules.inventory import service as inventory
from pagination import decode_cursor, encode_cursor

from .repository import OrganizationRepository
from .schemas import (
    BankSettings,
    OrganizationCreate,
    OrganizationKind,
    OrganizationPage,
    OrganizationRead,
    OrganizationUpdate,
    StockTake,
    StockTakeResult,
)


def get_org_repo(session: Annotated[AsyncSession, Depends(get_session)]) -> OrganizationRepository:
    return OrganizationRepository(session)


OrgRepo = Annotated[OrganizationRepository, Depends(get_org_repo)]

router = APIRouter(prefix="/organizations", tags=["organizations"])

# ponytail: no DELETE (an organization with stock is blocked by the FK, and deactivation belongs with the
# verification flow in Stage 2) and no Idempotency-Key (no natural unique key, so a client retry duplicates a row;
# add the key when creation becomes self-service). Both recorded in docs/adr/011-module-layout.md.


@router.post("", response_model=OrganizationRead, status_code=201)
async def create_organization(
    payload: OrganizationCreate, response: Response, session: Session, repo: OrgRepo, audit: Audit, now: Now,
    admin: Principal = require("admin"),        # onboarding a facility is a verification step: admins only
):
    async with session.begin():                 # insert + audit event: ONE transaction
        org = await repo.add({"kind": payload.kind.value, **payload.model_dump(exclude={"kind"})})
        await audit.add(now, "organization.created", "organization", org.id, actor_id=admin.id)
    response.headers["Location"] = f"/v1/organizations/{org.id}"
    return OrganizationRead.model_validate(org)


@router.get("", response_model=OrganizationPage)
async def list_organizations(
    repo: OrgRepo,
    kind: OrganizationKind | None = None,
    city: str | None = Query(default=None, min_length=1, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
):
    after = decode_cursor(cursor) if cursor else None
    rows = await repo.list_page(kind.value if kind else None, city, after, limit)
    page, has_more = rows[:limit], len(rows) > limit
    return OrganizationPage(
        items=[OrganizationRead.model_validate(o) for o in page],
        next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if has_more else None,
    )


@router.get("/{org_id}", response_model=OrganizationRead)
async def get_organization(org_id: UUID, repo: OrgRepo):
    org = await repo.get(org_id)
    if org is None:
        raise Problem(404, "organization-not-found", "Organization not found")
    return OrganizationRead.model_validate(org)


ADMIN_ONLY_FIELDS = {"name", "latitude", "longitude", "phone", "has_blood_centre"}


@router.patch("/{org_id}", response_model=OrganizationRead)
async def update_organization(
    org_id: UUID, changes: OrganizationUpdate, session: Session, repo: OrgRepo, audit: Audit, now: Now,
    principal: CurrentUser,
):
    # object-level: an admin, or staff of THIS organization. Organizations are public, so a 403 leaks nothing here.
    if principal.role != "admin" and principal.organization_id != org_id:
        raise Problem(403, "forbidden", "Forbidden", "You can only edit your own organization.")
    changed = changes.model_dump(exclude_unset=True)
    # B3: the location decides where donors are searched and alerted, the name is what families see: staff moving
    # their own hospital's pin would point donor alerts at any spot. Admin-only, like the phone.
    if principal.role != "admin" and ADMIN_ONLY_FIELDS & changed.keys():
        raise Problem(403, "forbidden", "Forbidden",
                      "Only an admin changes a facility's name, location, phone or blood-centre flag.")
    async with session.begin():
        org = await repo.get(org_id)
        if org is None:
            raise Problem(404, "organization-not-found", "Organization not found")
        if changed.get("has_blood_centre") and org.kind != "hospital":
            raise Problem(422, "not-a-hospital", "Not a hospital", "Only a hospital can have its own blood centre.")
        for field, value in changed.items():
            setattr(org, field, value)
        await audit.add(
            now, "organization.updated", "organization", org_id, {"fields": sorted(changed)}, actor_id=principal.id
        )
    return OrganizationRead.model_validate(org)


def _own_bank(org, principal: Principal, admin_too: bool) -> None:
    """Object-level check for bank-only actions: staff of THIS blood bank (and, where allowed, an admin)."""
    if org is None:
        raise Problem(404, "organization-not-found", "Organization not found")
    mine = principal.role == "blood_bank" and principal.organization_id == org.id
    if not (mine or (admin_too and principal.role == "admin")):
        raise Problem(403, "forbidden", "Forbidden", "Only this blood bank's own staff can do that.")
    if org.kind != "blood_bank":
        raise Problem(422, "not-a-blood-bank", "Not a blood bank", "This only applies to blood banks.")


@router.post("/{org_id}/stock-take", response_model=StockTakeResult)
async def stock_take(org_id: UUID, body: StockTake, session: Session, repo: OrgRepo, audit: Audit, now: Now,
                     principal: Principal = require("blood_bank")):
    """The bank counted its shelf (ADR 023): every listed group/component must be counted; a lower count takes the
    surplus units off the list (earliest expiry first), a higher one is refused (register the new bags instead).
    The ONLY way stock_confirmed_at moves: registering a unit does not set it (one new unit says nothing about the
    other 40). Not an admin power: a count is a physical fact only the people at the shelf can state."""
    async with session.begin():
        org = await repo.get(org_id)
        _own_bank(org, principal, admin_too=False)
        counts = {(c.blood_group.value, c.component.value): c.units for c in body.counts}
        try:
            removed = await inventory.stock_take(session, org_id, counts, now)
        except inventory.StockTakeRefused as refused:
            pairs = ", ".join(refused.pairs)
            if refused.kind == "stock-take-incomplete":
                raise Problem(422, refused.kind, "Stock-take incomplete",
                              f"Count every group on your list, including zeros. Missing: {pairs}.") from None
            raise Problem(422, refused.kind, "Count higher than the list",
                          f"Add the new bags with their label expiry first. Higher than listed: {pairs}.") from None
        org.stock_confirmed_at = now                # organizations owns this column (ADR 011): set here, not in inventory
        # counts only: no unit ids, no names
        await audit.add(now, "organization.stock_take", "organization", org_id, {"removed": removed},
                        actor_id=principal.id)
        lines = await inventory.summary(session, org_id, now)
    return StockTakeResult(summary=lines, stock_confirmed_at=now)


@router.patch("/{org_id}/bank-settings", response_model=OrganizationRead)
async def bank_settings(org_id: UUID, body: BankSettings, session: Session, repo: OrgRepo, audit: Audit, now: Now,
                        principal: Principal = require("blood_bank", "admin")):
    """auto_confirm_holds: the bank trusts its stock list enough that new holds start CONFIRMED (ADR 021)."""
    async with session.begin():
        org = await repo.get(org_id)
        _own_bank(org, principal, admin_too=True)
        org.auto_confirm_holds = body.auto_confirm_holds
        await audit.add(now, "organization.bank_settings_changed", "organization", org_id,
                        {"auto_confirm_holds": body.auto_confirm_holds}, actor_id=principal.id)
    return OrganizationRead.model_validate(org)
