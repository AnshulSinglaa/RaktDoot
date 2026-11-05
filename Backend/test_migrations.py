import asyncio

import asyncpg
from alembic import command
from alembic.script import ScriptDirectory

from conftest import alembic_config


async def fetch(url: str, sql: str):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql)
    finally:
        await conn.close()


def donor_columns(url: str) -> set[str]:
    rows = asyncio.run(fetch(url, "SELECT column_name FROM information_schema.columns WHERE table_name = 'donors'"))
    return {r["column_name"] for r in rows}


def test_db_is_at_the_newest_revision(test_url):
    cfg = alembic_config(test_url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert [r["version_num"] for r in asyncio.run(fetch(test_url, "SELECT version_num FROM alembic_version"))] == [head]


def test_every_migration_can_go_down_and_back_up(test_url):
    cfg = alembic_config(test_url)
    command.downgrade(cfg, "0001")                  # back to before city: undoes 0004, 0003 and 0002
    assert "city" not in donor_columns(test_url)
    command.upgrade(cfg, "head")
    assert "city" in donor_columns(test_url)
    command.downgrade(cfg, "base")                  # undo everything: proves 0001's downgrade too
    assert donor_columns(test_url) == set()
    command.upgrade(cfg, "head")                    # leave the DB at head for every other test
