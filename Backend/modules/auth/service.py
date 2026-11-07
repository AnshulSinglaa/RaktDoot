"""What other modules may ask the auth module. The ONLY import path into this module (ADR 011)."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import User


async def orgs_with_active_staff(session: AsyncSession, role: str) -> list[UUID]:
    """Organizations that have at least one ACTIVE `role` account: the ones that can answer."""
    rows = await session.scalars(select(User.organization_id).distinct()
                                 .where(User.role == role, User.is_active, User.organization_id.is_not(None)))
    return list(rows)
