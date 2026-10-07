import uuid
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
def _is_transaction_pooler(url: str) -> bool:
    """Supabase's transaction pooler listens on 6543. It hands a different
    server connection to each transaction, so asyncpg's prepared-statement
    cache must be off (a cached statement would not exist on the next one)."""
    return ":6543/" in url


def _engine_kwargs(pool_size: int, max_overflow: int) -> dict:
    kwargs: dict = {
        "pool_pre_ping": True,
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle,
    }
    if _is_transaction_pooler(settings.database_url):
        kwargs["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        }
    return kwargs


engine = create_async_engine(settings.database_url, **_engine_kwargs(settings.db_pool_size, settings.db_max_overflow))
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Point 19: reports, dashboards and snapshot-refresh jobs get their own small
# pool, so a burst of heavy analytical queries queues behind itself instead of
# taking the connections POS billing and sync need. Sized so both pools
# together (5+5 + 2+1 = 13) stay under the pooler's 15-connection cap.
reporting_engine = create_async_engine(
    settings.database_url, **_engine_kwargs(settings.reporting_pool_size, settings.reporting_max_overflow)
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
