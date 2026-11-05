from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import RefreshToken, User


class EmailTaken(Exception):
    pass


class UnknownOrganization(Exception):
    pass


class UserRepository:
    """All SQL for users lives here. It never commits: the caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, data: dict) -> User:
        user = User(**data)
        self.session.add(user)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            if "users_email_lower_uq" in str(exc.orig):
                raise EmailTaken() from exc
            if "users_organization_id_fkey" in str(exc.orig):
                raise UnknownOrganization() from exc
            raise
        return user

    async def get(self, user_id: UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email_for_update(self, email: str) -> User | None:
        """FOR UPDATE: two wrong passwords at the same moment must count as two failures, not one (lost update)."""
        stmt = select(User).where(func.lower(User.email) == email.lower()).with_for_update()
        return await self.session.scalar(stmt)


class LoginFailureRepository:
    """Failed logins per (account, client IP), migration 0018 (B10). Callers hold the user row lock (FOR UPDATE), so
    two parallel wrong passwords for one account are serialised and both count."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def locked_until(self, user_id: UUID, ip: str) -> datetime | None:
        return await self.session.scalar(
            text("SELECT locked_until FROM login_failures WHERE user_id = :u AND ip = :ip"), {"u": user_id, "ip": ip})

    async def add_failure(self, user_id: UUID, ip: str, now: datetime) -> int:
        """Count one more failure from this IP. Returns the new count."""
        return await self.session.scalar(text("""
            INSERT INTO login_failures (user_id, ip, failures, last_failed_at) VALUES (:u, :ip, 1, :now)
            ON CONFLICT (user_id, ip) DO UPDATE SET failures = login_failures.failures + 1, last_failed_at = :now
            RETURNING failures"""), {"u": user_id, "ip": ip, "now": now})

    async def lock(self, user_id: UUID, ip: str, until: datetime) -> None:
        await self.session.execute(
            text("UPDATE login_failures SET failures = 0, locked_until = :until WHERE user_id = :u AND ip = :ip"),
            {"u": user_id, "ip": ip, "until": until})

    async def clear(self, user_id: UUID, ip: str) -> None:
        """A successful login from this IP forgets its failures (and the IP: data minimisation)."""
        await self.session.execute(
            text("DELETE FROM login_failures WHERE user_id = :u AND ip = :ip"), {"u": user_id, "ip": ip})


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, user_id: UUID, family_id: UUID, token_hash: str, expires_at: datetime) -> None:
        self.session.add(RefreshToken(user_id=user_id, family_id=family_id, token_hash=token_hash, expires_at=expires_at))
        await self.session.flush()

    async def get_for_update(self, token_hash: str) -> RefreshToken | None:
        """FOR UPDATE: two refreshes with one token are serialised, so exactly one can rotate it."""
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        return await self.session.scalar(stmt)

    async def revoke_family(self, family_id: UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
