"""What other modules may ask the inventory module. The ONLY import path into this module (ADR 011)."""
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, func, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from domain import BloodGroup, stewardship_rank

from .models import BloodUnit
from .schemas import Component, StockLine  # noqa: F401  re-exported: other modules reach them only here (ADR 011)


@dataclass(frozen=True)
class ReservedUnit:
    id: UUID
    blood_bank_id: UUID
    blood_group: str


async def reserve(
    session: AsyncSession,
    bank_ids: list[UUID],          # nearest first: the order IS the preference
    groups: list[str],             # compatible donor groups for the patient
    exact_group: str,              # the patient's group: decides the stewardship order inside a bank
    component: str,
    n: int,
    usable_until: datetime,        # only units that expire AFTER this moment (B4: the caller passes the hold's horizon)
) -> list[ReservedUnit]:
    """Reserve up to n usable units and mark them RESERVED. Must run inside the caller's transaction.

    The correctness core of the whole system (docs/adr/016-allocation.md):
    - FOR UPDATE locks the chosen rows until COMMIT, so no other transaction can reserve them too;
    - SKIP LOCKED makes a concurrent request skip rows someone else is holding and take the NEXT unit, instead of
      waiting for the lock and then finding the unit gone. 20 hospitals racing for 5 units: 5 win, nobody blocks.
    Order: nearest bank, then stewardship inside that bank (exact group, same Rh sign, others, O- last:
    domain.stewardship_rank), then first-expired-first-out (FEFO). Bank position stays FIRST: stewardship never
    sends blood from farther away (a cross-bank preference needs a medical officer's sign-off first)."""
    if n <= 0 or not bank_ids:
        return []
    bank_order = literal(bank_ids, ARRAY(PG_UUID(as_uuid=True)))       # sent as one uuid[] parameter
    patient = BloodGroup(exact_group)
    stewardship = case({g: stewardship_rank(patient, BloodGroup(g), component) for g in groups},
                       value=BloodUnit.blood_group, else_=9)
    stmt = (
        select(BloodUnit.id, BloodUnit.blood_bank_id, BloodUnit.blood_group)
        .where(
            BloodUnit.status == "AVAILABLE",
            BloodUnit.component == component,
            BloodUnit.blood_group.in_(groups),
            BloodUnit.expires_at > usable_until,
            BloodUnit.blood_bank_id.in_(bank_ids),
        )
        .order_by(
            func.array_position(bank_order, BloodUnit.blood_bank_id),
            stewardship,
            BloodUnit.expires_at,
            BloodUnit.id,
        )
        .limit(n)
        .with_for_update(skip_locked=True)
    )
    units = [ReservedUnit(*row) for row in await session.execute(stmt)]
    if units:
        await session.execute(
            update(BloodUnit).where(BloodUnit.id.in_([u.id for u in units])).values(status="RESERVED")
        )
    return units


async def set_status(session: AsyncSession, unit_ids: list[UUID], from_status: str, to_status: str) -> int:
    """RESERVED -> AVAILABLE (released) or RESERVED -> ISSUED (handed over), and AVAILABLE -> ISSUED (a stock-take
    removed it). The from_status guard makes a replay a no-op."""
    if not unit_ids:
        return 0
    result = await session.execute(
        update(BloodUnit)
        .where(BloodUnit.id.in_(unit_ids), BloodUnit.status == from_status)
        .values(status=to_status)
    )
    return result.rowcount


async def any_expired(session: AsyncSession, unit_ids: list[UUID], now: datetime) -> bool:
    """True if any of these units is past its label expiry (B4): such a unit must never be confirmed or issued."""
    if not unit_ids:
        return False
    return await session.scalar(
        select(func.count()).where(BloodUnit.id.in_(unit_ids), BloodUnit.expires_at <= now)) > 0


async def banks_with_stock(session: AsyncSession, groups: list[str], component: str,
                           usable_until: datetime) -> list[UUID]:
    """Every bank holding at least one compatible unit usable past `usable_until` (the public search passes now;
    allocation passes the hold horizon, the same one reserve() uses). The public search passes this as `among=` to
    organizations.nearby, so the stock filter runs BEFORE its LIMIT 20 (R1, 2c): filtering the 20 nearest afterwards
    hid every farther bank with rare blood once 20 nearer banks existed.
    Trade-off: an id list per search; fine for a state (~120 banks), upgrade = a per-bank stock summary table if banks
    reach thousands."""
    rows = await session.scalars(
        select(BloodUnit.blood_bank_id).distinct()
        .where(BloodUnit.status == "AVAILABLE", BloodUnit.component == component, BloodUnit.blood_group.in_(groups),
               BloodUnit.expires_at > usable_until))
    return list(rows)


@dataclass(frozen=True)
class BankStock:
    units: int                      # usable units across every compatible group
    same_group: int                 # of those, units of exactly the patient's group (stewardship: ask for these first)
    groups: list[str]               # which compatible groups are in stock


async def available_by_bank(
    session: AsyncSession, bank_ids: list[UUID], groups: list[str], exact_group: str, component: str, now: datetime
) -> dict[UUID, BankStock]:
    """bank_id -> its usable compatible stock. Usable = AVAILABLE and not expired."""
    if not bank_ids:
        return {}
    rows = await session.execute(
        select(BloodUnit.blood_bank_id, BloodUnit.blood_group, func.count())
        .where(BloodUnit.blood_bank_id.in_(bank_ids), BloodUnit.status == "AVAILABLE", BloodUnit.component == component,
               BloodUnit.blood_group.in_(groups), BloodUnit.expires_at > now)
        .group_by(BloodUnit.blood_bank_id, BloodUnit.blood_group)
    )
    out: dict[UUID, BankStock] = {}
    for bank_id, group, units in rows:
        old = out.get(bank_id, BankStock(0, 0, []))
        out[bank_id] = BankStock(old.units + units, old.same_group + (units if group == exact_group else 0),
                                 sorted([*old.groups, group]))
    return out


# ---------- stock-take (ADR 023) ----------
async def summary(session: AsyncSession, bank_id: UUID, now: datetime) -> list[StockLine]:
    """Usable units per (group, component) at one bank: one GROUP BY, never the paginated unit list (limit 100 would
    undercount a district bank's 200-500 units)."""
    rows = await session.execute(
        select(BloodUnit.blood_group, BloodUnit.component, func.count())
        .where(BloodUnit.blood_bank_id == bank_id, BloodUnit.status == "AVAILABLE", BloodUnit.expires_at > now)
        .group_by(BloodUnit.blood_group, BloodUnit.component)
        .order_by(BloodUnit.blood_group, BloodUnit.component))
    return [StockLine(blood_group=g, component=c, units=n) for g, c, n in rows]


class StockTakeRefused(Exception):
    """`kind` is the problem slug: stock-take-incomplete or stock-take-increase. `pairs` are "B+/red_cells" labels."""

    def __init__(self, kind: str, pairs: list[str]) -> None:
        super().__init__(kind)
        self.kind, self.pairs = kind, pairs


def label(group: str, component: str) -> str:
    return f"{group}/{component}"


async def stock_take(session: AsyncSession, bank_id: UUID, counts: dict[tuple[str, str], int],
                     now: datetime) -> dict[str, int]:
    """Make the list match the shelf: for every (group, component) counted lower than the list, the surplus units go
    AVAILABLE -> ISSUED, earliest expiry first. Returns {"B+/red_cells": units removed}. Must run inside the caller's
    transaction; raises StockTakeRefused (nothing changed) when a listed pair is missing from `counts` or a count is
    higher than the list (new bags are registered with their label expiry, never conjured by a count).

    Locks the bank's usable units FOR UPDATE: reserve() uses SKIP LOCKED, so a hold arriving mid-count skips these
    rows instead of taking a unit whose fate the count is deciding; a hold that locked a unit first makes this wait,
    and the re-checked row (now RESERVED) drops out of the count. RESERVED and ISSUED units are never selected.
    Trade-off: FEFO guess at which bags left; exact once bags carry numbers."""
    rows = (await session.execute(
        select(BloodUnit.id, BloodUnit.blood_group, BloodUnit.component)
        .where(BloodUnit.blood_bank_id == bank_id, BloodUnit.status == "AVAILABLE", BloodUnit.expires_at > now)
        .order_by(BloodUnit.expires_at, BloodUnit.id)
        .with_for_update())).all()
    listed: dict[tuple[str, str], list[UUID]] = {}
    for unit_id, group, component in rows:
        listed.setdefault((group, component), []).append(unit_id)
    missing = sorted(label(*p) for p in listed.keys() - counts.keys())
    if missing:
        raise StockTakeRefused("stock-take-incomplete", missing)
    higher = sorted(label(*p) for p, n in counts.items() if n > len(listed.get(p, [])))
    if higher:
        raise StockTakeRefused("stock-take-increase", higher)
    removed: dict[str, int] = {}
    for pair, ids in sorted(listed.items()):
        surplus = ids[:len(ids) - counts[pair]]          # earliest-expiring first: the FEFO guess
        if surplus:
            removed[label(*pair)] = await set_status(session, surplus, "AVAILABLE", "ISSUED")
    return removed
