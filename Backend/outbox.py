"""Transactional outbox: side effects (notify a donor) are written as rows IN the business transaction.
Design and trade-offs: docs/adr/017-outbox.md. The worker (worker.py) delivers them."""
import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class OutboxRepository:
    """Insert-only from the API side. Never commits: the caller's transaction decides, so a rolled-back request
    leaves no message behind, and a committed one always does."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, topic: str, payload: dict, now: datetime) -> None:
        await self.session.execute(
            text("INSERT INTO outbox (topic, payload, created_at, available_at) "
                 "VALUES (:topic, CAST(:payload AS jsonb), :now, :now)"),
            {"topic": topic, "payload": json.dumps(payload), "now": now},
        )
