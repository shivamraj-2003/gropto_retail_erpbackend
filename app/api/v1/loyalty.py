import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import DiscountRule, LoyaltyConfig, LoyaltyTier
from app.schemas.schemas_phase2 import (
    DiscountRuleChange,
    DiscountRuleCreate,
    DiscountRuleOut,
    LoyaltyConfigChange,
    LoyaltyConfigOut,
    LoyaltyTierIn,
    LoyaltyTierOut,
)
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit

router = APIRouter(tags=["loyalty-discounts"])


async def get_or_create_loyalty_config(db: AsyncSession) -> LoyaltyConfig:
    config = await db.get(LoyaltyConfig, True)
    if config is None:
        config = LoyaltyConfig(
            id=True,
            earn_rate=0.01,
            redeem_value=1.0,
            min_balance_to_redeem=100.0,
            max_redeem_share=0.5,
        )
        db.add(config)
        await db.commit()
        await db.refresh(config)
    return config


@router.get("/loyalty/config", response_model=LoyaltyConfigOut)
async def get_loyalty_config(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> LoyaltyConfig:
    return await get_or_create_loyalty_config(db)


@router.post("/loyalty/config-change")
async def request_loyalty_config_change(
    payload: LoyaltyConfigChange,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> dict:
    """Directly financial — always routed through the approval engine (Super
    Admin applies in the same motion; anyone else queues a request)."""
    config = await get_or_create_loyalty_config(db)
    old_value = {
        "earn_rate": float(config.earn_rate),
        "redeem_value": float(config.redeem_value),
        "min_balance_to_redeem": float(config.min_balance_to_redeem),
        "max_redeem_share": float(config.max_redeem_share),
        "points_expiry_days": config.points_expiry_days,
    }
    new_value = {k: v for k, v in payload.model_dump(exclude={"reason"}).items() if v is not None}
    if not new_value:
        raise HTTPException(status_code=400, detail="No fields to change")

    request = await submit_or_apply(
        db,
        current=current,
        request_type="loyalty_rule_change",
        entity_type="loyalty_config",
        entity_id=None,
        old_value=old_value,
        new_value=new_value,
        reason=payload.reason,
        store_id=None,
    )
    await db.commit()
    return {"approval_request_id": str(request.id), "status": request.status}


@router.get("/loyalty/tiers", response_model=list[LoyaltyTierOut])
async def list_loyalty_tiers(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[LoyaltyTier]:
    result = await db.execute(select(LoyaltyTier).order_by(LoyaltyTier.min_lifetime_points))
    return list(result.scalars().all())


@router.post("/loyalty/tiers", response_model=LoyaltyTierOut, status_code=201)
async def create_loyalty_tier(
    payload: LoyaltyTierIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> LoyaltyTier:
    """Directly financial (changes future earn rates) — same approval posture
    as loyalty config itself, so this always writes an audit row; only Super
    Admin's write applies without review."""
    tier = LoyaltyTier(**payload.model_dump())
    db.add(tier)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="loyalty_tier.created",
        entity_type="loyalty_tier",
        entity_id=tier.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(tier)
    return tier


@router.get("/loyalty/customers/{customer_id}/tier")
async def get_customer_tier(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> dict:
    """Tier is always recomputed from real ledger history (lifetime points
    earned = sum of positive deltas) rather than read from a cached field."""
    lifetime_points = (
        await db.execute(
            text("select coalesce(sum(delta_points), 0) from loyalty_ledger where customer_id = :customer_id and delta_points > 0"),
            {"customer_id": str(customer_id)},
        )
    ).scalar_one()
    tiers = list(
        (await db.execute(select(LoyaltyTier).order_by(LoyaltyTier.min_lifetime_points.desc()))).scalars().all()
    )
    current_tier = next((t for t in tiers if float(lifetime_points) >= float(t.min_lifetime_points)), None)
    next_tier = next((t for t in reversed(tiers) if float(t.min_lifetime_points) > float(lifetime_points)), None)
    return {
        "customer_id": str(customer_id),
        "lifetime_points_earned": float(lifetime_points),
        "current_tier": {"name": current_tier.name, "earn_rate_multiplier": float(current_tier.earn_rate_multiplier)} if current_tier else None,
        "next_tier": {"name": next_tier.name, "points_needed": float(next_tier.min_lifetime_points) - float(lifetime_points)} if next_tier else None,
    }


@router.get("/discounts/rules", response_model=list[DiscountRuleOut])
async def list_discount_rules(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[DiscountRule]:
    result = await db.execute(select(DiscountRule).order_by(DiscountRule.name))
    return list(result.scalars().all())


@router.post("/discounts/rules", response_model=DiscountRuleOut, status_code=201)
async def create_discount_rule(
    payload: DiscountRuleCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> DiscountRule:
    rule = DiscountRule(**payload.model_dump())
    db.add(rule)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="discount_rule.created",
        entity_type="discount_rule",
        entity_id=rule.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(rule)
    return rule


@router.post("/discounts/rules/{rule_id}/change")
async def request_discount_rule_change(
    rule_id: uuid.UUID,
    payload: DiscountRuleChange,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> dict:
    rule = await db.get(DiscountRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Discount rule not found")

    old_value = {"percent": rule.percent, "flat_amount": rule.flat_amount, "active": rule.active}
    new_value = {k: v for k, v in payload.model_dump(exclude={"reason"}).items() if v is not None}
    if not new_value:
        raise HTTPException(status_code=400, detail="No fields to change")

    request = await submit_or_apply(
        db,
        current=current,
        request_type="high_discount",
        entity_type="discount_rule",
        entity_id=rule_id,
        old_value=old_value,
        new_value=new_value,
        reason=payload.reason,
        store_id=None,
    )
    await db.commit()
    return {"approval_request_id": str(request.id), "status": request.status}
