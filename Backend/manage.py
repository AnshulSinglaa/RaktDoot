"""Operator commands (never exposed over HTTP).

    python manage.py migrate                 alembic upgrade head as the OWNER, then set the app role's password
    python manage.py create-admin EMAIL      the first admin (password from LIFELINK_ADMIN_PASSWORD, else prompted)
    python manage.py import-osm [--dir D]    real MP hospitals and blood banks from the OSM snapshots (default data/osm)
    python manage.py seed-demo [--donors N]  synthetic stock, donors and demo accounts (default 2000 donors); on the
                                             imported facilities if any, else on synthetic ones
"""
import asyncio
import getpass
import os
import sys
import uuid
from pathlib import Path

import asyncpg
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from config import get_settings
from security import hash_password

HERE = Path(__file__).parent


def plain(url: str) -> str:
    """asyncpg wants postgresql://, not SQLAlchemy's postgresql+asyncpg://."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def execute(url: str, sql: str, *args):
    conn = await asyncpg.connect(plain(url))
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


async def set_app_password(owner_url: str, password: str) -> None:
    conn = await asyncpg.connect(plain(owner_url))
    try:
        role = f"{make_url(owner_url).database}_app"
        # ALTER ROLE takes no bind parameters: let Postgres quote both values safely with format(%I, %L)
        stmt = await conn.fetchval("SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)", role, password)
        await conn.execute(stmt)
    finally:
        await conn.close()


def migrate() -> None:
    settings = get_settings()
    owner = settings.migrations_database_url or settings.database_url
    command.upgrade(Config(str(HERE / "alembic.ini")), "head")       # env.py also picks the owner URL
    if settings.app_db_password:
        asyncio.run(set_app_password(owner, settings.app_db_password))
        print("[migrate] app role password set")
    print("[migrate] database at head")


def create_admin(email: str) -> None:
    password = os.environ.get("LIFELINK_ADMIN_PASSWORD") or getpass.getpass("Admin password (12+ chars): ")
    if len(password) < 12:
        sys.exit("password must be at least 12 characters")
    result = asyncio.run(execute(
        get_settings().database_url,
        "INSERT INTO users (id, email, password_hash, role) VALUES ($1, lower($2), $3, 'admin') "
        "ON CONFLICT DO NOTHING",
        uuid.uuid4(), email, hash_password(password),
    ))
    print("[create-admin] created" if result.endswith("1") else "[create-admin] already exists, unchanged")


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "migrate":
        migrate()
    elif cmd == "create-admin" and len(args) == 1:
        create_admin(args[0])
    elif cmd == "import-osm":
        from modules.organizations.osm import format_report
        from modules.organizations.service import import_osm_dir
        folder = Path(args[args.index("--dir") + 1]) if "--dir" in args else HERE / "data" / "osm"
        print(format_report(asyncio.run(import_osm_dir(get_settings().database_url, folder))))
    elif cmd == "seed-demo":
        from seed_demo import seed
        donors = int(args[args.index("--donors") + 1]) if "--donors" in args else 2000
        asyncio.run(seed(get_settings().database_url, donors))
    else:
        sys.exit(__doc__)
