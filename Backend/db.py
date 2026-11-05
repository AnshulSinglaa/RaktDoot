from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def to_async_url(url: str) -> str:
    """Standard Postgres URLs say postgresql://. SQLAlchemy needs to be told which driver: asyncpg."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def make_engine(url: str, pool_size: int, max_overflow: int, echo: bool = False) -> AsyncEngine:
    return create_async_engine(
        to_async_url(url),
        pool_size=pool_size,        # connections kept open and ready
        max_overflow=max_overflow,  # extra connections allowed during a burst, closed afterwards
        pool_pre_ping=True,         # test a connection before use, so a restarted DB does not break requests
        echo=echo,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    # expire_on_commit=False: keep attribute values readable after commit (async cannot lazy-reload them)
    return async_sessionmaker(engine, expire_on_commit=False)