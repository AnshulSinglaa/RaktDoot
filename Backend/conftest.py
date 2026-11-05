import asyncio
import os
import time
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from config import Settings
from db import to_async_url
from main import create_app

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
            from testcontainers.core.container import DockerContainer     # lazy: unit tests never need Docker

            builder = (
                DockerContainer("postgres:16")
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


@pytest.fixture
def settings(test_url) -> Settings:
    return Settings(database_url=test_url)


@pytest.fixture
def fresh_db(test_url) -> None:
    asyncio.run(run_sql(test_url, "TRUNCATE blood_units, donors, organizations, idempotency_keys, audit_events"))


@pytest.fixture
def app(settings, fresh_db):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:          # "with" runs startup and shutdown
        yield c
