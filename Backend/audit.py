"""Audit trail in PostgreSQL. Design and trade-offs: docs/adr/010-audit-log-in-postgres.md."""
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class AuditRepository:
    """Insert-only. Like every repository here it never commits: the caller's transaction decides.

    Because the event is written inside the SAME transaction as the change, either both exist or neither does:
    no lost events, no phantom events. The price: if the audit insert fails, the request fails (fail closed).
    Privacy: ids and field NAMES only, never values (no names, emails or blood groups)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self, at: datetime, event: str, entity: str, entity_id: UUID, details: dict | None = None,
        actor_id: UUID | None = None,           # who did it. None = the system, or an anonymous request
    ) -> None:
        await self.session.execute(
            text(
                "INSERT INTO audit_events (at, event, entity, entity_id, actor_id, details) "
                "VALUES (:at, :event, :entity, :entity_id, :actor_id, CAST(:details AS jsonb))"
            ),
            {
                "at": at,                           # injected clock, timezone-aware (UTC)
                "event": event,                     # e.g. donor.registered
                "entity": entity,
                "entity_id": entity_id,
                "actor_id": actor_id,
                "details": json.dumps(details or {}),
            },
        )
