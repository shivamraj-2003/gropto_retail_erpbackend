import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import DiscountRule, LoyaltyConfig
from app.schemas.schemas_phase2 import (
    DiscountRuleChange,
    DiscountRuleCreate,
    DiscountRuleOut,
    LoyaltyConfigChange,
    LoyaltyConfigOut,
)
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit

router = APIRouter(tags=["loyalty-discounts"])


@router.get("/loyalty/config", response_model=LoyaltyConfigOut)
async def get_loyalty_config(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> LoyaltyConfig:
    return await db.get(LoyaltyConfig, True)


@router.post("/loyalty/config-change")
async def request_loyalty_config_change(
    payload: LoyaltyConfigChange,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> dict:
    """Directly financial — always routed through the approval engine (Super
    Admin applies in the same motion; anyone else queues a request)."""
    config = await db.get(LoyaltyConfig, True)
    old_value = {
        "earn_rate": float(config.earn_rate),
        "redeem_value": float(config.redeem_value),
        "min_balance_to_redeem": float(config.min_balance_to_redeem),
        "max_redeem_share": float(config.max_redeem_share),
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
