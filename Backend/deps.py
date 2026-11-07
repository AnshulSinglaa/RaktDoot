"""Shared FastAPI dependencies. Every module imports from here, never from main.py (that would be a circular import)."""
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, AsyncIterator
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from audit import AuditRepository
from config import Settings
from errors import Problem
from security import read_access_token

ROLES = ("donor", "hospital", "blood_bank", "ngo", "admin")     # mirrors the CHECK in migration 0007


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:     # one session per request
        yield session                                           # closed after the request


def get_audit_repo(session: Annotated[AsyncSession, Depends(get_session)]) -> AuditRepository:
    return AuditRepository(session)


def get_now() -> datetime:
    return datetime.now(timezone.utc)


Config = Annotated[Settings, Depends(get_app_settings)]
Session = Annotated[AsyncSession, Depends(get_session)]
Audit = Annotated[AuditRepository, Depends(get_audit_repo)]
Now = Annotated[datetime, Depends(get_now)]


# ---------- who is calling ----------
@dataclass(frozen=True)
class Principal:
    """The caller, as the access token says. No database read per request: that is the point of a signed token.

    Trade-off: a deactivated user keeps access until the token expires (access_token_minutes). Upgrade path:
    a Redis deny-list of user ids checked here, when an incident shows 15 minutes is too long."""
    id: UUID
    role: str
    organization_id: UUID | None
    expires_at: datetime                # the token's exp: a long-lived SSE stream ends here (B11)


def unauthorized(detail: str = "Log in and send the access token as 'Authorization: Bearer <token>'.") -> Problem:
    return Problem(401, "unauthorized", "Unauthorized", detail, headers={"WWW-Authenticate": "Bearer"})


def get_current_user(request: Request, settings: Config) -> Principal:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized()
    claims = read_access_token(token, settings.jwt_secret)
    if claims is None or claims["role"] not in ROLES:
        raise unauthorized("The access token is invalid or expired.")      # never say which: it helps attackers
    org = claims.get("org")
    return Principal(id=UUID(claims["sub"]), role=claims["role"], organization_id=UUID(org) if org else None,
                     expires_at=datetime.fromtimestamp(claims["exp"], timezone.utc))


CurrentUser = Annotated[Principal, Depends(get_current_user)]


def require(*roles: str):
    """Role check (RBAC). Usage: `principal: Principal = require("admin")`.

    It only answers "may this KIND of user call this endpoint". Whether they may touch THIS row (object-level, BOLA)
    is checked inside the route, where the row is known."""
    def check(principal: CurrentUser) -> Principal:
        if principal.role not in roles:
            raise Problem(403, "forbidden", "Forbidden", "Your role cannot do this.")
        return principal

    return Depends(check)


def auth_rate_limit(request: Request, settings: Config) -> None:
    """At most N login/register calls per client IP per minute: slows password guessing and signup spam.

    Trade-off: in-process memory, fixed 60 s window. Two app instances = two counters, and a restart forgets.
    Upgrade path: Redis INCR + EXPIRE once there are 2+ instances, or an edge rate limiter."""
    _limit(request, "auth", settings.auth_rate_limit_per_minute)


def public_rate_limit(request: Request, settings: Config) -> None:
    """Anonymous search endpoints: a token bucket per client IP (QA 2r). Burst `public_rate_burst`, refilled at
    `public_rate_limit_per_minute`. Generous for a hospital ward behind one NAT IP, useless for scraping the map, and
    no 2x burst at a window rollover (a fixed window allows `limit` at 0:59 and `limit` again at 1:00).

    Trade-off: in-process memory, per worker: 4 uvicorn workers = 4 buckets per IP, a restart refills them.
    Upgrade path: one Redis bucket per IP (a Lua script or GCRA) when there are several instances, or the edge (5.8)."""
    rate = settings.public_rate_limit_per_minute / 60                   # tokens per second
    burst = settings.public_rate_burst
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    buckets = request.app.state.rate_buckets
    tokens, last = buckets.get(ip, (burst, now))
    tokens = min(burst, tokens + (now - last) * rate)
    if tokens < 1:
        buckets[ip] = (tokens, now)
        raise Problem(
            429, "too-many-requests", "Too many requests", "Too many searches from this network. Wait and try again.",
            headers={"Retry-After": str(math.ceil((1 - tokens) / rate))},
        )
    buckets[ip] = (tokens - 1, now)
    if len(buckets) > 10_000:                   # bounded memory: a bucket idle long enough to be full again is forgotten
        for key in [k for k, (t, at) in buckets.items() if t + (now - at) * rate >= burst]:
            del buckets[key]


def _limit(request: Request, bucket: str, per_minute: int) -> None:
    ip = request.client.host if request.client else "unknown"
    window = int(time.monotonic() // 60)
    hits = request.app.state.rate_hits
    key = (bucket, ip, window)
    hits[key] = hits.get(key, 0) + 1
    for old in [k for k in hits if k[-1] < window]:     # forget past windows, so memory stays bounded
        del hits[old]
    if hits[key] > per_minute:
        raise Problem(
            429, "too-many-requests", "Too many requests", "Too many attempts. Wait a minute and try again.",
            headers={"Retry-After": "60"},
        )
