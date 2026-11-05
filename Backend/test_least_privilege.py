"""Threat model #3: what can code running inside the API do to the database? Only row work, never history or schema."""
import asyncio

import asyncpg
import pytest

from conftest import _fetch, app_url, run_sql


def as_app(test_url: str, sql: str):
    return asyncio.run(run_sql(app_url(test_url), sql))


@pytest.mark.parametrize("sql", [
    "DELETE FROM audit_events",
    "UPDATE audit_events SET event = 'x'",
    "TRUNCATE audit_events",
    "ALTER TABLE audit_events DISABLE TRIGGER audit_events_immutable",
    "DROP TABLE donors",
])
def test_the_app_role_cannot_erase_history_or_change_the_schema(test_url, fresh_db, sql):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        as_app(test_url, sql)


def test_the_app_role_is_not_a_superuser(test_url, fresh_db):
    (row,) = asyncio.run(_fetch(app_url(test_url), "SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user"))
    assert not row["rolsuper"] and not row["rolcreaterole"]


def test_every_table_is_granted_to_the_app(test_url, fresh_db):
    """Catches the classic mistake: a new migration creates a table and forgets the GRANT (the app then 500s in prod)."""
    rows = asyncio.run(_fetch(test_url, """
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version'
          AND NOT has_table_privilege(current_database() || '_app', c.oid, 'SELECT, INSERT')"""))
    assert [r["relname"] for r in rows] == []
