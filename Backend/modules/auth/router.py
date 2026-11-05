import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from deps import Audit, Config, CurrentUser, Now, Principal, Session, auth_rate_limit, get_session, require, unauthorized
from errors import Problem
from security import hash_password, hash_refresh_token, make_access_token, new_refresh_token, verify_password

from .models import User
from .repository import EmailTaken, LoginFailureRepository, RefreshTokenRepository, UnknownOrganization, UserRepository
from .schemas import LoginRequest, Me, RegisterRequest, StaffCreate, TokenResponse

REFRESH_COOKIE = "refresh_token"
COOKIE_PATH = "/v1/auth"        # the browser sends the refresh token to auth endpoints only, never to the rest of the API


def get_users(session: Annotated[AsyncSession, Depends(get_session)]) -> UserRepository:
    return UserRepository(session)


def get_tokens(session: Annotated[AsyncSession, Depends(get_session)]) -> RefreshTokenRepository:
    return RefreshTokenRepository(session)


Users = Annotated[UserRepository, Depends(get_users)]
Tokens = Annotated[RefreshTokenRepository, Depends(get_tokens)]
RateLimited = Depends(auth_rate_limit)

router = APIRouter(prefix="/auth", tags=["auth"])


async def issue(
    user: User, family_id: uuid.UUID, tokens: RefreshTokenRepository, response: Response, now: datetime, settings: Settings
) -> TokenResponse:
    """A new access token in the body, a new refresh token in an HttpOnly cookie. The caller owns the transaction."""
    refresh = new_refresh_token()
    await tokens.add(user.id, family_id, hash_refresh_token(refresh), now + timedelta(days=settings.refresh_token_days))
    response.set_cookie(
        REFRESH_COOKIE, refresh,
        max_age=settings.refresh_token_days * 86400,
        path=COOKIE_PATH,
        httponly=True,                  # JavaScript (and so any XSS) cannot read it
        secure=settings.cookie_secure,  # HTTPS only, outside plain-http local dev
        samesite="strict",              # never sent on a request started by another site: blocks CSRF on /refresh
    )
    access = make_access_token(
        user.id, user.role, user.organization_id, now, settings.access_token_minutes, settings.jwt_secret
    )
    return TokenResponse(access_token=access, expires_in=settings.access_token_minutes * 60)


@router.post("/register", response_model=Me, status_code=201, dependencies=[RateLimited])
async def register(payload: RegisterRequest, session: Session, users: Users, audit: Audit, now: Now):
    # argon2 is slow ON PURPOSE (~50-100 ms of CPU). In `async def` that would freeze the event loop for every other
    # request, so it runs in a worker thread.
    password_hash = await asyncio.to_thread(hash_password, payload.password)
    try:
        async with session.begin():
            user = await users.add({"email": payload.email, "password_hash": password_hash, "role": "donor"})
            await audit.add(now, "user.registered", "user", user.id, actor_id=user.id)
    except EmailTaken:
        # ponytail: this 409 tells an attacker the email has an account (enumeration). Accepted for now: signup UX.
        # Upgrade path: always 202 + "check your email", once email verification exists (3.6).
        raise Problem(409, "email-taken", "Email already registered") from None
    return Me.model_validate(user)


@router.post("/login", response_model=TokenResponse, dependencies=[RateLimited])
async def login(payload: LoginRequest, request: Request, response: Response, session: Session, users: Users,
                tokens: Tokens, audit: Audit, now: Now, settings: Config):
    # B10: lockout counts per (account, IP). Per account alone, anyone who knew a bank's email could keep it locked
    # out with 5 wrong passwords every 15 minutes. Now the attacker's IP is locked, staff on their own network are not.
    # ponytail: an attacker with many IPs gets max_failed_logins guesses per IP (the per-IP rate limit still applies);
    # upgrade = MFA for staff accounts. Behind a proxy, client.host is the proxy unless uvicorn trusts its
    # X-Forwarded-For (--proxy-headers --forwarded-allow-ips): without that, this is per account again.
    ip = request.client.host if request.client else "unknown"
    failures = LoginFailureRepository(session)
    async with session.begin():
        user = await users.get_by_email_for_update(payload.email)
        # always run argon2, even for an unknown email: equal timing, so response time does not reveal accounts
        ok = await asyncio.to_thread(verify_password, user.password_hash if user else None, payload.password)
        locked_until = await failures.locked_until(user.id, ip) if user is not None else None
        locked = locked_until is not None and locked_until > now
        if user is not None and ok and not locked and user.is_active:
            await failures.clear(user.id, ip)
            await audit.add(now, "user.logged_in", "user", user.id, actor_id=user.id)
            return await issue(user, uuid.uuid4(), tokens, response, now, settings)
        if user is not None and not ok and not locked:
            if await failures.add_failure(user.id, ip, now) >= settings.max_failed_logins:
                await failures.lock(user.id, ip, now + timedelta(minutes=settings.lockout_minutes))
                await audit.add(now, "user.locked", "user", user.id)        # ids only: no IP in the audit log
            else:
                await audit.add(now, "user.login_failed", "user", user.id)
    # Commit FIRST (the failure count must survive), then answer. One message for every failure reason:
    # unknown email, wrong password, locked, deactivated. Telling them apart would help an attacker.
    raise unauthorized("Invalid email or password.")


# Not rate-limited: a refresh token is 256 random bits, so guessing is hopeless. Limiting it only hurts real users
# (every page load refreshes, and a hospital network or mobile carrier puts hundreds of people behind one IP).
@router.post("/refresh", response_model=TokenResponse)
async def refresh(request: Request, response: Response, session: Session, users: Users, tokens: Tokens,
                  audit: Audit, now: Now, settings: Config):
    plain = request.cookies.get(REFRESH_COOKIE)
    if not plain:
        raise unauthorized("No refresh token.")
    async with session.begin():
        row = await tokens.get_for_update(hash_refresh_token(plain))
        if row is not None and row.revoked_at is None and row.expires_at > now:
            if row.used_at is not None:
                # REUSE: this token was already rotated. Either the client or a thief holds a stale copy, and we
                # cannot tell which, so the whole login (family) dies. The real user just logs in again.
                # ponytail: two tabs refreshing at the same instant also land here. Upgrade path: a few-second grace.
                await tokens.revoke_family(row.family_id, now)
                await audit.add(now, "auth.refresh_reuse", "user", row.user_id)
            else:
                user = await users.get(row.user_id)
                if user is not None and user.is_active:
                    row.used_at = now
                    return await issue(user, row.family_id, tokens, response, now, settings)
                await tokens.revoke_family(row.family_id, now)
    raise unauthorized("The refresh token is invalid, expired or revoked. Log in again.")


@router.post("/logout", status_code=204)
async def logout(request: Request, session: Session, tokens: Tokens, now: Now, settings: Config):
    """Kills this login's whole refresh family. Idempotent: logging out twice, or with no cookie, is still a 204.

    The access token is not revoked: it dies on its own within access_token_minutes (see deps.Principal)."""
    plain = request.cookies.get(REFRESH_COOKIE)
    if plain:
        async with session.begin():
            row = await tokens.get_for_update(hash_refresh_token(plain))
            if row is not None:
                await tokens.revoke_family(row.family_id, now)
    response = Response(status_code=204)
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH, httponly=True, secure=settings.cookie_secure, samesite="strict")
    return response


@router.get("/me", response_model=Me)
async def me(principal: CurrentUser, users: Users):
    user = await users.get(principal.id)
    if user is None or not user.is_active:
        raise unauthorized("This account no longer exists or is deactivated.")
    return Me.model_validate(user)


@router.post("/users", response_model=Me, status_code=201)
async def create_staff(payload: StaffCreate, session: Session, users: Users, audit: Audit, now: Now,
                       admin: Principal = require("admin")):
    """Staff accounts are created by an admin, never self-service: a hospital login can see donor records.

    ponytail: the role is not matched to the organization's kind (a 'hospital' user in an NGO is accepted). Checking
    it needs a call into the organizations module; add it with the facility-verification flow."""
    password_hash = await asyncio.to_thread(hash_password, payload.password)
    try:
        async with session.begin():
            user = await users.add({
                "email": payload.email, "password_hash": password_hash,
                "role": payload.role, "organization_id": payload.organization_id,
            })
            await audit.add(now, "user.created", "user", user.id, {"role": payload.role}, actor_id=admin.id)
    except EmailTaken:
        raise Problem(409, "email-taken", "Email already registered") from None
    except UnknownOrganization:
        raise Problem(422, "unknown-organization", "Unknown organization",
                      "organization_id does not match any organization.") from None
    return Me.model_validate(user)
