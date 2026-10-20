from collections.abc import AsyncGenerator

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

connect_args = {}
if "asyncpg" in settings.database_url:
    # Disable prepared statement caching when using PgBouncer / Supabase pooler
    connect_args["statement_cache_size"] = 0
    connect_args["prepared_statement_cache_size"] = 0

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=connect_args,
    poolclass=pool.NullPool,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
