import asyncio

import asyncpg

from config import get_settings


async def main() -> None:
    conn = await asyncpg.connect(get_settings().database_url)
    try:
        tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        print("tables:", [t["tablename"] for t in tables])
        print("donors rows:", await conn.fetchval("SELECT count(*) FROM donors"))
    finally:
        await conn.close()


asyncio.run(main())