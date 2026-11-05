from datetime import datetime
from uuid import UUID

from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Organization


class OrganizationRepository:
    """All SQL for organizations lives here. It never commits: the caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, data: dict) -> Organization:
        org = Organization(**data)
        self.session.add(org)
        await self.session.flush()
        return org

    async def get(self, org_id: UUID) -> Organization | None:
        return await self.session.get(Organization, org_id)

    async def list_page(
        self, kind: str | None = None, city: str | None = None, after: tuple[datetime, UUID] | None = None, limit: int = 20
    ) -> list[Organization]:
        # limit + 1: the caller keeps `limit` rows and uses the extra one to know "there is a next page"
        # ponytail: no index on (created_at, id). Hundreds of rows, not millions; ADR 009 says no index without a measured query.
        stmt = select(Organization).order_by(Organization.created_at, Organization.id).limit(limit + 1)
        if kind is not None:
            stmt = stmt.where(Organization.kind == kind)
        if city is not None:            # case-insensitive: users type "bhopal", the row says "Bhopal"
            stmt = stmt.where(func.lower(Organization.city) == city.strip().lower())
        if after is not None:
            ts, org_id = after
            stmt = stmt.where(
                tuple_(Organization.created_at, Organization.id)
                > tuple_(literal(ts, Organization.created_at.type), literal(org_id, Organization.id.type))
            )
        return list((await self.session.scalars(stmt)).all())
