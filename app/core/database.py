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

# Point 19: reports, dashboards and snapshot-refresh jobs get their own small
# pool, so a burst of heavy analytical queries queues behind itself instead of
# taking the connections POS billing and sync need. Sized so both pools
# together (5+5 + 2+1 = 13) stay under the pooler's 15-connection cap.
reporting_engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.reporting_pool_size,
    max_overflow=settings.reporting_max_overflow,
)
ReportingSessionLocal = async_sessionmaker(reporting_engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def get_reporting_db() -> AsyncGenerator[AsyncSession, None]:
    async with ReportingSessionLocal() as session:
        yield session
