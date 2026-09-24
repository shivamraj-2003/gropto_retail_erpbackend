import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import LoyaltyConfig, LoyaltyLedger


class DuplicateLedgerEntry(Exception):
    pass


async def apply_ledger_entry(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    delta_points: float,
    reason: str,
    source_type: str,
    source_id: uuid.UUID,
) -> LoyaltyLedger:
    entry = LoyaltyLedger(
        customer_id=customer_id,
        delta_points=delta_points,
        reason=reason,
        source_type=source_type,
        source_id=source_id,
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateLedgerEntry() from exc
    return entry


async def get_balance(db: AsyncSession, *, customer_id: uuid.UUID) -> float:
    result = await db.execute(
        select(LoyaltyLedger.delta_points).where(LoyaltyLedger.customer_id == customer_id)
    )
    return float(sum(result.scalars().all()))


async def get_config(db: AsyncSession) -> LoyaltyConfig:
    return await db.get(LoyaltyConfig, True)
