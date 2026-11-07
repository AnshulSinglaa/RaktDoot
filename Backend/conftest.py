import asyncio
import os
import time
import uuid
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from config import Settings
from db import to_async_url
from main import create_app
from security import hash_password

ALEMBIC_INI = Path(__file__).parent / "alembic.ini"


def alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", to_async_url(url).replace("%", "%%"))   # preset: env.py must not use the dev URL
    return cfg


async def run_sql(url: str, sql: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _fetch(url: str, sql: str, *args):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def audit_rows(test_url: str, entity_id: str | None = None):
    """Read audit_events with a separate connection, so the code under test does not grade itself.

    Without an entity_id, logins are left out: every `client` logs in, and that event is not what the test is about."""
    if entity_id is None:
        return asyncio.run(_fetch(test_url, "SELECT * FROM audit_events WHERE event <> 'user.logged_in' ORDER BY at, id"))
    return asyncio.run(
        _fetch(test_url, "SELECT * FROM audit_events WHERE entity_id = $1::uuid ORDER BY at, id", entity_id)
    )


def pytest_collection_modifyitems(items):
    for item in items:
        if "test_url" in item.fixturenames:          # touches the database, directly or through another fixture
            item.add_marker(pytest.mark.integration)


def configured_test_url() -> str | None:
    try:
        return Settings().test_database_url
    except Exception:       # no .env and no env vars (a clean clone, or CI): fall through to a container
        return None


def wait_for_postgres(url: str, seconds: int = 30) -> None:
    """Postgres logs "ready" once for its temporary init server, so poll for a real connection instead."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            asyncio.run(run_sql(url, "SELECT 1"))
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="session")
def test_url():
    url, container = configured_test_url(), None
    if not url:
        try:
            from testcontainers.core.container import DockerContainer  # lazy: unit tests never need Docker

            builder = (
                DockerContainer("postgis/postgis:16-3.4")       # same image as docker-compose.yml
                .with_env("POSTGRES_PASSWORD", "test")
                .with_env("POSTGRES_DB", "lifelink_test")
                .with_exposed_ports(5432)
            )
            builder.start()
            container = builder
            host, port = container.get_container_host_ip(), container.get_exposed_port(5432)
            url = f"postgresql://postgres:test@{host}:{port}/lifelink_test"
            wait_for_postgres(url)
        except Exception as exc:
            if container is not None:
                container.stop()
            if os.getenv("CI"):
                raise                                  # in CI a missing database is a FAILURE, never a skip
            pytest.skip(f"no test database: {exc!r}")
    try:
        # Safety rail: this fixture DROPS the schema. It must never run against a real database.
        assert url.rsplit("/", 1)[-1].endswith("_test"), "refusing to run: not a *_test database"
        asyncio.run(run_sql(url, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        command.upgrade(alembic_config(url), "head")                               # builds the schema the way prod will
        yield url
    finally:
        if container is not None:
            container.stop()


TEST_SECRET = "test-secret-" + "x" * 32       # tests never depend on the developer's .env secret
TEST_PASSWORD = "correct horse battery staple"
_TEST_HASH = hash_password(TEST_PASSWORD)    # argon2 is slow on purpose: hash once per session, not once per user


def make_user(test_url: str, role: str, organization_id: str | None = None, email: str | None = None) -> tuple[str, str]:
    """Insert a user straight into the database (the way an operator bootstraps the first admin). Returns (id, email).

    Staff roles get a fresh organization of the matching kind unless one is given."""
    if role in ("hospital", "blood_bank", "ngo") and organization_id is None:
        organization_id = str(uuid.uuid4())
        asyncio.run(run_sql(
            test_url,
            f"INSERT INTO organizations (id, kind, name, city) VALUES ('{organization_id}', '{role}', 'Test {role}', 'Bhopal')",
        ))
    user_id, email = str(uuid.uuid4()), email or f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    asyncio.run(_fetch(
        test_url,
        "INSERT INTO users (id, email, password_hash, role, organization_id) VALUES ($1::uuid, $2, $3, $4, $5::uuid)",
        user_id, email, _TEST_HASH, role, organization_id,
    ))
    return user_id, email


def as_role(client: TestClient, test_url: str, role: str, organization_id: str | None = None) -> dict:
    """Create a user with this role, log in through the real endpoint, return the Authorization header."""
    _, email = make_user(test_url, role, organization_id)
    r = client.post("/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


APP_TEST_PASSWORD = "app-test-password"


def app_url(test_url: str) -> str:
    """The same database, but as the least-privilege <db>_app role (migration 0008), like production."""
    owner = make_url(test_url)
    return owner.set(username=f"{owner.database}_app", password=APP_TEST_PASSWORD).render_as_string(hide_password=False)


@pytest.fixture
def settings(test_url) -> Settings:
    # The TestClient talks plain http://testserver, and a client never sends a Secure cookie over http. Say so here,
    # not in a developer's .env: CI has no .env, and the refresh tests failed there while passing locally.
    return Settings(database_url=app_url(test_url), jwt_secret=TEST_SECRET, cookie_secure=False)


@pytest.fixture
def fresh_db(test_url) -> None:
    db = make_url(test_url).database
    asyncio.run(run_sql(
        test_url,
        "TRUNCATE outbox, donor_responses, allocations, emergency_requests, refresh_tokens, login_failures, blood_units, "
        "donors, users, "
        "organizations, idempotency_keys, audit_events; "
        # every test: test_migrations drops and recreates the role, which forgets its password
        f"ALTER ROLE {db}_app PASSWORD '{APP_TEST_PASSWORD}'",
    ))


@pytest.fixture
def app(settings, fresh_db):
    return create_app(settings)


@pytest.fixture
def client(app, test_url):
    """Logged in as an ADMIN: most tests are about the API's behaviour, not about who may call it.
    Who may call what is tested in test_auth.py with `anon` and `as_role`."""
    with TestClient(app) as c:          # "with" runs startup and shutdown
        c.headers.update(as_role(c, test_url, "admin"))
        yield c


@pytest.fixture
def anon(app):
    with TestClient(app) as c:
        yield c
