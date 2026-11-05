from datetime import datetime
from uuid import UUID

from sqlalchemy import literal, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import BloodUnit


class UnknownOrganization(Exception):
    pass


class BloodUnitRepository:
    """All SQL for blood units lives here. It never commits: the caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, data: dict) -> BloodUnit:
        unit = BloodUnit(**data)
        self.session.add(unit)
        try:
            await self.session.flush()          # send the INSERT now so a constraint failure shows up here
        except IntegrityError as exc:
            if "blood_units_blood_bank_id_fkey" in str(exc.orig):
                raise UnknownOrganization(str(data["blood_bank_id"])) from exc
            raise
        return unit

    async def get(self, unit_id: UUID) -> BloodUnit | None:
        return await self.session.get(BloodUnit, unit_id)

    async def list_page(
        self,
        blood_bank_id: UUID | None = None,
        blood_group: str | None = None,
        component: str | None = None,
        status: str | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 20,
    ) -> list[BloodUnit]:
        """Soonest-to-expire first (FEFO: first expired, first out), keyset-paged on (expires_at, id).

        ponytail: no new index. With all four filters the partial index from migration 0001 already serves this order;
        unfiltered it is a scan of a small table. Index only for a measured query (ADR 009)."""
        stmt = select(BloodUnit).order_by(BloodUnit.expires_at, BloodUnit.id).limit(limit + 1)
        if blood_bank_id is not None:
            stmt = stmt.where(BloodUnit.blood_bank_id == blood_bank_id)
        if blood_group is not None:
            stmt = stmt.where(BloodUnit.blood_group == blood_group)
        if component is not None:
            stmt = stmt.where(BloodUnit.component == component)
        if status is not None:
            stmt = stmt.where(BloodUnit.status == status)
        if after is not None:
            ts, unit_id = after
            stmt = stmt.where(
                tuple_(BloodUnit.expires_at, BloodUnit.id)
                > tuple_(literal(ts, BloodUnit.expires_at.type), literal(unit_id, BloodUnit.id.type))
            )
        return list((await self.session.scalars(stmt)).all())
