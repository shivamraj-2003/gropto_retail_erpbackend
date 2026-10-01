import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import LoyaltyConfig, LoyaltyLedger, LoyaltyTier


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


async def get_earn_multiplier(db: AsyncSession, *, customer_id: uuid.UUID) -> float:
    """Point 9 audit fix: LoyaltyTier.earn_rate_multiplier existed but was
    never consulted at earn time — tier was cosmetic/reporting-only. Mirrors
    the live lifetime-points recompute already used by the tier API."""
    lifetime_points = (
        await db.execute(
            text(
                "select coalesce(sum(delta_points), 0) from loyalty_ledger "
                "where customer_id = :customer_id and delta_points > 0"
            ),
            {"customer_id": str(customer_id)},
        )
    ).scalar_one()
    tiers = list(
        (await db.execute(select(LoyaltyTier).order_by(LoyaltyTier.min_lifetime_points.desc()))).scalars().all()
    )
    current_tier = next((t for t in tiers if float(lifetime_points) >= float(t.min_lifetime_points)), None)
    return float(current_tier.earn_rate_multiplier) if current_tier else 1.0


async def expire_lapsed_points(db: AsyncSession) -> int:
    """Point 9 audit fix: points earned never expired under any code path.
    FIFO-simulates each customer's earn lots against later redemptions so a
    partially-redeemed earn lot only expires the remainder still outstanding,
    and writes a real negative "expire" ledger entry (idempotent per lot via
    the source_type/source_id/reason unique constraint) rather than silently
    adjusting a balance field. Returns the count of expiry entries written."""
    config = await get_config(db)
    expiry_days = config.points_expiry_days if config else None
    if not expiry_days or expiry_days <= 0:
        return 0

    now = datetime.now(timezone.utc)
    customer_ids = (
        await db.execute(select(LoyaltyLedger.customer_id).distinct())
    ).scalars().all()

    written = 0
    for customer_id in customer_ids:
        entries = (
            await db.execute(
                select(LoyaltyLedger)
                .where(LoyaltyLedger.customer_id == customer_id)
                .order_by(LoyaltyLedger.created_at.asc())
            )
        ).scalars().all()

        # FIFO lots: each earn-like (positive-delta) entry opens a lot; every
        # later negative-delta entry (redeem/expire/reversal) consumes the
        # oldest open lots first.
        lots: list[dict] = []
        for entry in entries:
            delta = float(entry.delta_points)
            if delta > 0:
                lots.append({"id": entry.id, "created_at": entry.created_at, "remaining": delta})
            elif delta < 0:
                to_consume = -delta
                for lot in lots:
                    if to_consume <= 0:
                        break
                    take = min(lot["remaining"], to_consume)
                    lot["remaining"] -= take
                    to_consume -= take

        for lot in lots:
            if lot["remaining"] <= 0:
                continue
            created_at = lot["created_at"]
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_days = (now - created_at).days
            if age_days < expiry_days:
                continue
            try:
                await apply_ledger_entry(
                    db,
                    customer_id=customer_id,
                    delta_points=-lot["remaining"],
                    reason="expire",
                    source_type="loyalty_earn_lot",
                    source_id=lot["id"],
                )
                written += 1
            except DuplicateLedgerEntry:
                pass
    return written
