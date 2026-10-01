from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# Supabase's Session Pooler on this project hard-caps concurrent connections
# at 15 (asyncpg raises EMAXCONNSESSION beyond that) — pool_size + max_overflow
# must stay comfortably under that for a SINGLE process, since a dev reload,
# a second local process, or a one-off script sharing the same DATABASE_URL
# all draw from the same 15-connection budget. 10+20=30 (the old config) was
# more than double the actual server-side limit on its own, which is what
# caused random login/query hangs and timeouts under any concurrent load.
engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
