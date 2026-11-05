import base64
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audit import AuditRepository
from config import Settings, get_settings
from db import make_engine, make_sessionmaker
from domain import BloodGroup, compatible_donor_groups, is_eligible
from errors import Problem, install_error_handlers
from models import Donor
from repository import DonorRepository, DuplicateEmail, IdempotencyRepository
from schemas import DonorCreate, DonorPage, DonorRead, DonorUpdate


# ---------- dependencies: things a route asks FastAPI to hand it ----------
def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:     # one session per request
        yield session                                           # closed after the request


def get_repo(session: Annotated[AsyncSession, Depends(get_session)]) -> DonorRepository:
    return DonorRepository(session)


def get_idem_repo(session: Annotated[AsyncSession, Depends(get_session)]) -> IdempotencyRepository:
    return IdempotencyRepository(session)


def get_audit_repo(session: Annotated[AsyncSession, Depends(get_session)]) -> AuditRepository:
    return AuditRepository(session)


def get_now() -> datetime:
    return datetime.now(timezone.utc)


Config = Annotated[Settings, Depends(get_app_settings)]
Session = Annotated[AsyncSession, Depends(get_session)]
Repo = Annotated[DonorRepository, Depends(get_repo)]
IdemRepo = Annotated[IdempotencyRepository, Depends(get_idem_repo)]
Audit = Annotated[AuditRepository, Depends(get_audit_repo)]
Now = Annotated[datetime, Depends(get_now)]


def present(donor: Donor, now: datetime, settings: Settings) -> DonorRead:
    eligible = donor.is_available and is_eligible(
        donor.last_donated_at, now, settings.min_donation_interval_days
    )
    return DonorRead(
        id=donor.id,
        name=donor.name,
        blood_group=donor.blood_group,
        is_available=donor.is_available,
        eligible_now=eligible,
    )


# ---------- pagination cursor: an opaque bookmark = (created_at, id) of the last row served ----------
def encode_cursor(donor: Donor) -> str:
    return base64.urlsafe_b64encode(f"{donor.created_at.isoformat()}|{donor.id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        ts, donor_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        when = datetime.fromisoformat(ts)
        if when.tzinfo is None:
            raise ValueError("naive timestamp")
        return when, UUID(donor_id)
    except Exception:       # ponytail: any malformed cursor is the client's fault, never a 500
        raise Problem(
            422, "invalid-cursor", "Invalid cursor",
            "The cursor is malformed. Start again from the first page.",
        ) from None


# ---------- the app and its routes ----------
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url, settings.pool_size, settings.max_overflow, settings.sql_echo)
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        print("[startup] database pool created", flush=True)
        yield
        await engine.dispose()                  # close every pooled connection cleanly
        print("[shutdown] database pool closed", flush=True)

    app = FastAPI(title="LifeLink", lifespan=lifespan)
    app.state.settings = settings
    install_error_handlers(app)
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
        idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
    ):
        row = {
            "name": payload.name,
            "email": payload.email,
            "age": payload.age,
            "blood_group": payload.blood_group.value,
            "last_donated_at": payload.last_donated_at,
            "city": payload.city,
        }
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
                await audit.add(now, "donor.registered", "donor", donor.id)     # same transaction: ids only, no personal data
                result = present(donor, now, settings)
                if idempotency_key is not None:
                    await idem.finish(idempotency_key, 201, result.model_dump_json())
        except DuplicateEmail:       # a failed attempt rolls the key AND the audit event back: errors leave no trace
            raise Problem(
                409, "duplicate-email", "Email already registered", "A donor with this email already exists."
            ) from None
        response.headers["Location"] = f"/v1/donors/{donor.id}"
        return result

    @v1.get("/donors", response_model=DonorPage)
    async def list_donors(
        repo: Repo,
        now: Now,
        settings: Config,
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
            next_cursor=encode_cursor(page[-1]) if has_more else None,
        )

    @v1.get("/donors/{donor_id}", response_model=DonorRead)
    async def get_donor(donor_id: UUID, repo: Repo, now: Now, settings: Config):
        donor = await repo.get(donor_id)
        if donor is None:
            raise Problem(404, "donor-not-found", "Donor not found")
        return present(donor, now, settings)

    @v1.patch("/donors/{donor_id}", response_model=DonorRead)
    async def update_donor(
        donor_id: UUID, changes: DonorUpdate, session: Session, repo: Repo, audit: Audit, now: Now, settings: Config
    ):
        changed = changes.model_dump(exclude_unset=True)
        async with session.begin():
            donor = await repo.get(donor_id)
            if donor is None:
                raise Problem(404, "donor-not-found", "Donor not found")
            for field, value in changed.items():
                setattr(donor, field, value)
            donor.updated_at = now
            # field NAMES only, never the new values
            await audit.add(now, "donor.updated", "donor", donor_id, {"fields": sorted(changed)})
        return present(donor, now, settings)

    @v1.get("/blood-groups/{recipient}/compatible-donors", response_model=list[BloodGroup])
    def compatible(recipient: BloodGroup):
        return compatible_donor_groups(recipient)

    app.include_router(v1)                      # after the routes exist
    return app
