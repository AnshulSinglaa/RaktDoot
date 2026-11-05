import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from db import make_engine, make_sessionmaker
from deps import (  # noqa: F401
    Audit,
    Config,
    CurrentUser,
    Now,
    Principal,
    Session,
    get_app_settings,
    get_audit_repo,
    get_now,
    get_session,
    require,
)
from domain import BloodGroup, compatible_donor_groups, donation_interval_days, is_eligible
from errors import Problem, install_error_handlers
from events import EventHub
from models import Donor
from modules.auth.router import router as auth_router
from modules.emergencies.router import router as emergencies_router
from modules.inventory.router import router as inventory_router
from modules.organizations.router import router as organizations_router
from modules.public.router import router as public_router
from pagination import decode_cursor, encode_cursor
from repository import DonorRepository, DuplicateEmail, DuplicatePhone, DuplicateProfile, IdempotencyRepository
from schemas import DonationCreate, DonorCreate, DonorPage, DonorRead, DonorUpdate, PhoneVerification


# ---------- donor-specific dependencies (shared ones live in deps.py) ----------
def get_repo(session: Annotated[AsyncSession, Depends(get_session)]) -> DonorRepository:
    return DonorRepository(session)


def get_idem_repo(session: Annotated[AsyncSession, Depends(get_session)]) -> IdempotencyRepository:
    return IdempotencyRepository(session)


Repo = Annotated[DonorRepository, Depends(get_repo)]
IdemRepo = Annotated[IdempotencyRepository, Depends(get_idem_repo)]


def present(donor: Donor, now: datetime, settings: Settings) -> DonorRead:
    interval = donation_interval_days(
        donor.sex, settings.donation_interval_male_days, settings.donation_interval_female_days
    )
    return DonorRead(
        id=donor.id,
        name=donor.name,
        blood_group=donor.blood_group,
        is_available=donor.is_available,
        eligible_now=donor.is_available and is_eligible(donor.last_donated_at, now, interval),
        phone_verified=donor.phone_verified_at is not None,
    )


def phone_taken() -> Problem:      # a new instance each time: a shared exception object would carry tracebacks
    return Problem(409, "phone-taken", "Phone already registered", "Another verified donor uses this number.")


# ponytail: an unverified duplicate of a verified number is left in place (never matched, can never be verified with
# that number). Purging squatted numbers is plan item 3.


# ---------- the app and its routes ----------
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url, settings.pool_size, settings.max_overflow, settings.sql_echo)
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.events = EventHub()
        await app.state.events.start(settings.database_url)    # one LISTEN connection, outside the pool
        print("[startup] database pool created", flush=True)
        yield
        await app.state.events.stop()
        await engine.dispose()                  # close every pooled connection cleanly
        print("[shutdown] database pool closed", flush=True)

    app = FastAPI(title="Rakt Doot", lifespan=lifespan)
    app.state.settings = settings
    app.state.rate_hits = {}                    # per app, so tests and instances never share counters (see deps.py)
    app.state.rate_buckets = {}                 # public token buckets, same reason
    install_error_handlers(app)
    # an explicit allow-list, never "*": with credentials, "*" would let any site call the API as the user
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=["Location"],
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"     # never guess a content type (JSON run as HTML)
        response.headers["X-Frame-Options"] = "DENY"               # no clickjacking via iframes
        response.headers["Referrer-Policy"] = "no-referrer"        # URLs with ids never leak to other sites
        response.headers["Cache-Control"] = "no-store"             # responses carry health data: no shared caches
        if settings.cookie_secure:                                  # only meaningful once served over HTTPS
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
    v1 = APIRouter(prefix="/v1")                # a promise: the shape of /v1 responses will not break under clients

    # infrastructure endpoints stay unversioned: load balancers and monitors are not API clients
    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(session: Session):
        try:
            await session.execute(text("SELECT 1"))
        except Exception:
            raise Problem(503, "database-unavailable", "Database unavailable") from None
        return {"status": "ready"}

    @app.get("/info")
    def info(settings: Config):
        return {"app": settings.app_name, "environment": settings.environment}

    @v1.post("/donors", response_model=DonorRead, status_code=201)
    async def register(
        payload: DonorCreate,
        response: Response,
        session: Session,
        repo: Repo,
        idem: IdemRepo,
        audit: Audit,
        now: Now,
        settings: Config,
        principal: Principal = require("donor", "ngo", "blood_bank", "admin"),
        idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,   # B9: <=255 in the DB
    ):
        """A donor creates their OWN profile. NGO / blood-bank staff and admins register walk-in donors at a camp
        (no account, user_id NULL); `consent: true` then records the donor's signed consent form."""
        row = {
            "name": payload.name,
            "email": payload.email,
            "age": payload.age,
            "blood_group": payload.blood_group.value,
            "last_donated_at": payload.last_donated_at,
            "city": payload.city,
            "sex": payload.sex,
            "phone": payload.phone,
            # staff registering a walk-in donor see the person: their phone counts as verified. Self-signup does not.
            "phone_verified_at": now if payload.phone and principal.role != "donor" else None,
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "consent_at": now,
            "user_id": principal.id if principal.role == "donor" else None,
        }
        # keys are per caller: without the prefix, user B replaying user A's key would receive A's saved response
        if idempotency_key is not None:
            idempotency_key = f"{principal.id}:{idempotency_key}"
        # fingerprint of the NORMALISED body: same key + different body is a client bug, not a retry
        request_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        try:
            async with session.begin():                  # claim the key, insert, audit, save the answer: ONE transaction
                if idempotency_key is not None and not await idem.claim(idempotency_key, request_hash):
                    saved = await idem.get(idempotency_key)      # ponytail: a purge job could delete it between the two calls
                    if saved.request_hash != request_hash:
                        raise Problem(
                            422, "idempotency-key-reuse", "Idempotency key reused",
                            "This key was already used with a different request body.",
                        )
                    return Response(                              # a retry: replay the first answer, do nothing new
                        content=saved.response_body,
                        status_code=saved.response_status,
                        media_type="application/json",
                        headers={
                            "Location": f"/v1/donors/{json.loads(saved.response_body)['id']}",
                            "Idempotent-Replayed": "true",
                        },
                    )
                donor = await repo.add(row)
                await audit.add(now, "donor.registered", "donor", donor.id, actor_id=principal.id)   # ids only
                result = present(donor, now, settings)
                if idempotency_key is not None:
                    await idem.finish(idempotency_key, 201, result.model_dump_json())
        except DuplicateEmail:       # a failed attempt rolls the key AND the audit event back: errors leave no trace
            raise Problem(
                409, "duplicate-email", "Email already registered", "A donor with this email already exists."
            ) from None
        except DuplicateProfile:
            raise Problem(409, "donor-profile-exists", "Donor profile exists", "This account already has one.") from None
        except DuplicatePhone:      # only staff registrations insert a VERIFIED phone, so only they can hit this
            raise phone_taken() from None
        response.headers["Location"] = f"/v1/donors/{donor.id}"
        return result

    @v1.get("/donors", response_model=DonorPage)
    async def list_donors(
        repo: Repo,
        now: Now,
        settings: Config,
        # Need-to-know (threat model #2): the full donor register is admin-only. Hospitals find donors through
        # matching, scoped to one emergency and one radius, never by browsing the whole country.
        _: Principal = require("admin"),
        blood_group: BloodGroup | None = None,
        is_available: bool | None = None,
        limit: int = Query(default=20, ge=1, le=100),      # the cap protects the database from limit=10000000
        cursor: str | None = None,
    ):
        after = decode_cursor(cursor) if cursor else None
        rows = await repo.list(blood_group.value if blood_group else None, is_available, after, limit)
        page, has_more = rows[:limit], len(rows) > limit
        return DonorPage(
            items=[present(d, now, settings) for d in page],
            next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if has_more else None,
        )

    # Declared before /donors/{donor_id}: routes match in order and "me" is not a UUID.
    @v1.get("/donors/me", response_model=DonorRead)
    async def my_donor_profile(repo: Repo, now: Now, settings: Config, principal: CurrentUser):
        donor = await repo.get_by_user(principal.id)
        if donor is None:
            raise Problem(404, "donor-not-found", "Donor not found", "This account has no donor profile yet.")
        return present(donor, now, settings)

    @v1.get("/donors/{donor_id}", response_model=DonorRead)
    async def get_donor(donor_id: UUID, repo: Repo, now: Now, settings: Config, principal: CurrentUser):
        donor = await repo.get(donor_id)
        # Object-level check (BOLA): the donor themself or an admin (B2). Staff see donors only through the minimal,
        # audited views of an emergency (first name, group, distance band), never the full record by id.
        # Anyone else gets the same 404 as a missing row: a 403 would confirm that the id exists.
        if donor is None or (principal.role != "admin" and donor.user_id != principal.id):
            raise Problem(404, "donor-not-found", "Donor not found")
        return present(donor, now, settings)

    @v1.patch("/donors/{donor_id}", response_model=DonorRead)
    async def update_donor(
        donor_id: UUID, changes: DonorUpdate, session: Session, repo: Repo, audit: Audit, now: Now, settings: Config,
        principal: CurrentUser,
    ):
        changed = changes.model_dump(exclude_unset=True)
        async with session.begin():
            donor = await repo.get(donor_id)
            # only the owner or an admin may edit; anyone else learns nothing (404, not 403)
            if donor is None or (principal.role != "admin" and donor.user_id != principal.id):
                raise Problem(404, "donor-not-found", "Donor not found")
            for field, value in changed.items():
                setattr(donor, field, value)
            if "phone" in changed:
                # a new number must be seen again before we alert it. Unverified rows are outside donors_phone_uq, so a
                # PATCH never 409s: no "this number is registered" oracle for strangers.
                donor.phone_verified_at = None
            donor.updated_at = now
            await repo.flush()
            # field NAMES only, never the new values
            await audit.add(
                now, "donor.updated", "donor", donor_id, {"fields": sorted(changed)}, actor_id=principal.id
            )
        return present(donor, now, settings)

    @v1.post("/donors/{donor_id}/verify-phone", response_model=DonorRead)
    async def verify_phone(
        donor_id: UUID, body: PhoneVerification, session: Session, repo: Repo, audit: Audit, now: Now, settings: Config,
        staff: Principal = require("blood_bank", "ngo", "admin"),
    ):
        """Staff confirm the donor's number IN PERSON (they called it, or the donor is standing there). Until then the
        donor is never matched or alerted. ponytail: SMS OTP is out of scope (ADR 020): in-person checks are slower
        but harder to fake; add OTP when self-signups must become matchable without a visit."""
        async with session.begin():
            try:
                verified = await repo.verify_phone(donor_id, body.phone, now)      # one conditional UPDATE
            except DuplicatePhone:
                raise phone_taken() from None
            if not verified:            # only now read the row, to say WHY (never echo the stored number)
                donor = await repo.get(donor_id)
                if donor is None:
                    raise Problem(404, "donor-not-found", "Donor not found")
                if donor.phone is None:
                    raise Problem(422, "no-phone", "No phone on file", "Add the donor's phone number first.")
                raise Problem(409, "phone-mismatch", "Phone does not match",
                              "The number presented is not the one on file. Ask the donor to update it first.")
            donor = await repo.get(donor_id, fresh=True)
            await audit.add(now, "donor.phone_verified", "donor", donor_id, actor_id=staff.id)
        return present(donor, now, settings)

    @v1.post("/donors/{donor_id}/donations", response_model=DonorRead, status_code=201)
    async def record_donation(
        donor_id: UUID, body: DonationCreate, session: Session, repo: Repo, audit: Audit, now: Now, settings: Config,
        staff: Principal = require("blood_bank", "admin"),
    ):
        """A blood bank records that this donor just gave blood. The ONLY way last_donated_at changes after signup."""
        donated_at = body.donated_at or now
        if donated_at > now:
            raise Problem(422, "donation-in-future", "Donation in the future", "donated_at cannot be in the future.")
        async with session.begin():
            # one UPDATE: keep the latest date (a late paper record must not make a donor look eligible sooner), and
            # verify the phone only if the number the donor showed matches the one on file
            try:
                found = await repo.record_donation(donor_id, donated_at, body.phone, now)
            except DuplicatePhone:
                raise phone_taken() from None
            if not found:
                raise Problem(404, "donor-not-found", "Donor not found")
            donor = await repo.get(donor_id, fresh=True)
            await audit.add(now, "donor.donation_recorded", "donor", donor_id,
                            {"organization_id": str(staff.organization_id) if staff.organization_id else None},
                            actor_id=staff.id)
        return present(donor, now, settings)

    @v1.get("/blood-groups/{recipient}/compatible-donors", response_model=list[BloodGroup])
    def compatible(recipient: BloodGroup):
        return compatible_donor_groups(recipient)

    v1.include_router(auth_router)
    v1.include_router(organizations_router)     # feature modules mount under /v1
    v1.include_router(inventory_router)
    v1.include_router(emergencies_router)
    v1.include_router(public_router)
    app.include_router(v1)                      # after the routes exist
    return app
