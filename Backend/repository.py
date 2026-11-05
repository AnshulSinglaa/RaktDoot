from datetime import datetime
from uuid import UUID

from sqlalchemy import literal, select, text, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Donor


class DuplicateEmail(Exception):
    pass


class DonorRepository:
    """All SQL for donors lives here. It never commits: the caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, data: dict) -> Donor:
        donor = Donor(**data)
        self.session.add(donor)
        try:
            await self.session.flush()          # send the INSERT now so a constraint failure shows up here
        except IntegrityError as exc:
            if "donors_email_lower_uq" in str(exc.orig):
                raise DuplicateEmail(data["email"]) from exc
            raise
        return donor

    async def get(self, donor_id: UUID) -> Donor | None:
        return await self.session.get(Donor, donor_id)

    async def list(
        self,
        blood_group: str | None = None,
        is_available: bool | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 20,
    ) -> list[Donor]:
        """Keyset page: rows strictly after the bookmark `after`, in (created_at, id) order.

        Fetches limit + 1 rows: the caller keeps `limit` and uses the extra one to know "there is a next page"
        without a slow COUNT(*)."""
        stmt = select(Donor).order_by(Donor.created_at, Donor.id).limit(limit + 1)
        if blood_group is not None:
            stmt = stmt.where(Donor.blood_group == blood_group)
        if is_available is not None:
            stmt = stmt.where(Donor.is_available == is_available)
        if after is not None:
            # literal(..., column type): plain python values would be sent as timestamp WITHOUT time zone
            ts, donor_id = after
            stmt = stmt.where(
                tuple_(Donor.created_at, Donor.id)
                > tuple_(literal(ts, Donor.created_at.type), literal(donor_id, Donor.id.type))
            )
        return list((await self.session.scalars(stmt)).all())


class IdempotencyRepository:
    """Saved results for Idempotency-Key. Like every repository here it never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(self, key: str, request_hash: str) -> bool:
        """True: this request now owns the key. False: a finished request already used it.

        If another transaction is mid-flight with the same key, Postgres makes this INSERT wait until that
        transaction commits (then we get False) or rolls back (then we get True). The database does the locking."""
        result = await self.session.execute(
            text(
                "INSERT INTO idempotency_keys (idempotency_key, request_hash) VALUES (:key, :hash) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING idempotency_key"
            ),
            {"key": key, "hash": request_hash},
        )
        return result.first() is not None

    async def get(self, key: str):
        result = await self.session.execute(
            text(
                "SELECT request_hash, response_status, response_body "
                "FROM idempotency_keys WHERE idempotency_key = :key"
            ),
            {"key": key},
        )
        return result.first()

    async def finish(self, key: str, status: int, body: str) -> None:
        await self.session.execute(
            text(
                "UPDATE idempotency_keys SET response_status = :status, response_body = :body "
                "WHERE idempotency_key = :key"
            ),
            {"key": key, "status": status, "body": body},
        )
