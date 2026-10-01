import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase4 import (
    Coupon,
    CustomerWalletLedger,
    PromotionRedemption,
    PromotionRule,
    ScheduledPriceChange,
    StorePriceOverride,
)
from app.schemas.schemas_phase4 import (
    CouponCreate,
    CouponOut,
    PromotionRedemptionOut,
    PromotionRuleIn,
    PromotionRuleOut,
    ScheduledPriceChangeCreate,
    ScheduledPriceChangeOut,
    StorePriceOverrideCreate,
    StorePriceOverrideOut,
    WalletCreditIn,
    WalletLedgerOut,
)
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.wallet import DuplicateWalletEntry, credit_wallet

router = APIRouter(prefix="/promotions", tags=["promotions"])


@router.get("/rules", response_model=list[PromotionRuleOut])
async def list_promotion_rules(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[PromotionRule]:
    """Blueprint §9 "BOGO, combo, category offers, coupons, cart rules" — the
    PromotionRule table existed with zero API endpoints before this."""
    stmt = select(PromotionRule)
    if active_only:
        stmt = stmt.where(PromotionRule.active.is_(True))
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/rules", response_model=PromotionRuleOut, status_code=201)
async def create_promotion_rule(
    payload: PromotionRuleIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> PromotionRule:
    rule = PromotionRule(**payload.model_dump())
    db.add(rule)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="promotion_rule.created",
        entity_type="promotion_rule",
        entity_id=rule.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(rule)
    return rule


@router.put("/rules/{rule_id}", response_model=PromotionRuleOut)
async def update_promotion_rule(
    rule_id: uuid.UUID,
    payload: PromotionRuleIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> PromotionRule:
    rule = await db.get(PromotionRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Promotion rule not found")
    old_value = {"name": rule.name, "promo_type": rule.promo_type, "active": rule.active}
    for field, value in payload.model_dump().items():
        setattr(rule, field, value)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="promotion_rule.updated",
        entity_type="promotion_rule",
        entity_id=rule.id,
        old_value=old_value,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(rule)
    return rule


@router.get("/profitability")
async def promotion_profitability(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    """Blueprint §9 "Promotion profitability and discount-funding tracking."
    Two real, distinct sources: (1) discount_rules vs. sales.discount_total
    (DiscountRule-driven POS bill/line discounts — still not individually
    attributable per rule, same limitation as before); (2) promotion_redemptions
    (BOGO/combo/category PromotionRule firings, now actually logged per-order
    by services/promotion_engine.py — real per-rule attribution for OMS)."""
    if store_id is not None:
        require_store_access(store_id, current)
    store_filter = "and s.store_id = :store_id" if store_id else ""
    params = {"store_id": str(store_id)} if store_id else {}
    rows = (
        await db.execute(
            text(
                f"""
                select dr.id as rule_id, dr.name, dr.scope, dr.percent, dr.flat_amount,
                       count(distinct s.id) as bills_with_discount,
                       coalesce(sum(s.discount_total), 0) as total_discount_funded,
                       coalesce(sum(s.grand_total), 0) as revenue_on_discounted_bills
                from discount_rules dr
                left join sales s on s.discount_total > 0 and s.status = 'completed' {store_filter}
                group by dr.id, dr.name, dr.scope, dr.percent, dr.flat_amount
                order by total_discount_funded desc
                """
            ),
            params,
        )
    ).all()

    promo_store_filter = "and pr.store_id = :store_id" if store_id else ""
    promo_rows = (
        await db.execute(
            text(
                f"""
                select p.id as rule_id, p.name, p.promo_type,
                       count(*) as redemption_count,
                       coalesce(sum(pr.discount_amount), 0) as total_discount_funded
                from promotion_rules p
                join promotion_redemptions pr on pr.promotion_rule_id = p.id {promo_store_filter}
                group by p.id, p.name, p.promo_type
                order by total_discount_funded desc
                """
            ),
            params,
        )
    ).all()

    return {
        "note": "Two sources: discount_rules (bill/line POS discounts, aggregate-only — "
        "still can't be attributed to a single rule per bill) and promotion_redemptions "
        "(BOGO/combo/category PromotionRule firings, now logged per-order with real "
        "per-rule attribution — see 'promotion_rule_redemptions' below).",
        "rules": [
            {
                "rule_id": str(r.rule_id),
                "name": r.name,
                "scope": r.scope,
                "percent": float(r.percent) if r.percent else None,
                "flat_amount": float(r.flat_amount) if r.flat_amount else None,
            }
            for r in rows
        ],
        "promotion_rule_redemptions": [
            {
                "rule_id": str(r.rule_id),
                "name": r.name,
                "promo_type": r.promo_type,
                "redemption_count": int(r.redemption_count),
                "total_discount_funded": float(r.total_discount_funded),
            }
            for r in promo_rows
        ],
        "company_wide_discount_funding_mtd": float(
            (
                await db.execute(
                    text(
                        "select coalesce(sum(discount_total), 0) from sales where status = 'completed' "
                        "and date_trunc('month', billed_at) = date_trunc('month', now())"
                    )
                )
            ).scalar_one()
        ),
    }


@router.get("/private-label-margin")
async def private_label_margin(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> list[dict]:
    """Blueprint §9 "Private-label SKU performance and margin tracking" — real
    revenue/margin per private-label product from actual sale_items, not
    estimated."""
    if store_id is not None:
        require_store_access(store_id, current)
    store_filter = "and s.store_id = :store_id" if store_id else ""
    params = {"store_id": str(store_id)} if store_id else {}
    rows = (
        await db.execute(
            text(
                f"""
                select p.id as product_id, p.name, p.sku, p.brand,
                       sum(si.quantity) as units_sold,
                       sum(si.line_total) as revenue,
                       sum(si.taxable_value) as taxable_revenue,
                       sum(si.quantity * p.purchase_price) as cost,
                       sum(si.line_total) - sum(si.quantity * p.purchase_price) as gross_margin
                from products p
                join sale_items si on si.product_id = p.id
                join sales s on s.id = si.sale_id
                where p.is_private_label = true and s.status = 'completed'
                  and s.billed_at >= current_date - interval '90 days' {store_filter}
                group by p.id, p.name, p.sku, p.brand
                order by gross_margin desc
                """
            ),
            params,
        )
    ).all()
    return [
        {
            "product_id": str(r.product_id),
            "name": r.name,
            "sku": r.sku,
            "brand": r.brand,
            "units_sold_90d": float(r.units_sold),
            "revenue_90d": float(r.revenue),
            "cost_90d": float(r.cost),
            "gross_margin_90d": float(r.gross_margin),
            "gross_margin_pct": round(float(r.gross_margin) / float(r.revenue) * 100, 1) if r.revenue else None,
        }
        for r in rows
    ]


@router.get("/price-overrides", response_model=list[StorePriceOverrideOut])
async def list_price_overrides(
    store_id: uuid.UUID | None = None,
    city: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[StorePriceOverride]:
    stmt = select(StorePriceOverride).where(StorePriceOverride.active.is_(True))
    if store_id:
        stmt = stmt.where(StorePriceOverride.store_id == store_id)
    if city:
        stmt = stmt.where(StorePriceOverride.city == city)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/price-overrides", response_model=StorePriceOverrideOut, status_code=201)
async def create_price_override(
    payload: StorePriceOverrideCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> StorePriceOverride:
    # Point 9 audit fix: a user holding the blanket loyalty.configure
    # permission could previously create a price override for ANY store —
    # store_id now has to be one the caller actually owns.
    if payload.store_id is not None:
        require_store_access(payload.store_id, current)
    override = StorePriceOverride(**payload.model_dump())
    db.add(override)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="store_price_override.created",
        entity_type="store_price_override",
        entity_id=override.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(override)
    return override


@router.get("/scheduled-price-changes", response_model=list[ScheduledPriceChangeOut])
async def list_scheduled_price_changes(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> list[ScheduledPriceChange]:
    result = await db.execute(select(ScheduledPriceChange).order_by(ScheduledPriceChange.effective_at.desc()))
    return list(result.scalars().all())


@router.post("/scheduled-price-changes", status_code=201)
async def create_scheduled_price_change(
    payload: ScheduledPriceChangeCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> dict:
    """Point 9 audit fix: previously inserted directly with no approval gate
    and nothing ever executed it (effective_at was purely decorative). Now
    routed through the approval engine like an immediate price change — Super
    Admin's submission is approved in the same motion, anyone else's queues —
    and a scheduler job (services/scheduler.py) applies it to the Product and
    marks it executed once effective_at actually arrives."""
    sp_change = ScheduledPriceChange(**payload.model_dump(), status="pending_approval")
    db.add(sp_change)
    await db.flush()

    request = await submit_or_apply(
        db,
        current=current,
        request_type="scheduled_price_change",
        entity_type="scheduled_price_change",
        entity_id=sp_change.id,
        old_value=None,
        new_value=payload.model_dump(mode="json"),
        reason=None,
        store_id=None,
    )
    await db.commit()
    return {"approval_request_id": str(request.id), "status": request.status, "scheduled_price_change_id": str(sp_change.id)}


@router.get("/wallet/{customer_id}", response_model=list[WalletLedgerOut])
async def get_customer_wallet_ledger(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[CustomerWalletLedger]:
    result = await db.execute(select(CustomerWalletLedger).where(CustomerWalletLedger.customer_id == customer_id).order_by(CustomerWalletLedger.created_at.desc()))
    return list(result.scalars().all())


@router.post("/wallet/{customer_id}/credit", response_model=WalletLedgerOut, status_code=201)
async def credit_customer_wallet(
    customer_id: uuid.UUID,
    payload: WalletCreditIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> CustomerWalletLedger:
    """Point 9 audit fix: credit_wallet() existed in services/wallet.py but
    was never called from any endpoint — there was no manual top-up/goodwill-
    credit path at all (only the automatic return-refund credit added
    alongside this fix)."""
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Credit amount must be positive")
    try:
        entry = await credit_wallet(
            db,
            customer_id=customer_id,
            amount=payload.amount,
            reference_type="manual_credit",
            source_type="manual",
            source_id=uuid.uuid4(),
        )
    except DuplicateWalletEntry as exc:
        raise HTTPException(status_code=409, detail="Duplicate wallet credit") from exc
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="wallet.manual_credit",
        entity_type="customer_wallet_ledger",
        entity_id=entry.id,
        new_value={"customer_id": str(customer_id), "amount": payload.amount},
        reason=payload.reason,
    )
    await db.commit()
    await db.refresh(entry)
    return entry


@router.get("/redemptions", response_model=list[PromotionRedemptionOut])
async def list_promotion_redemptions(
    promotion_rule_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> list[PromotionRedemption]:
    stmt = select(PromotionRedemption).order_by(PromotionRedemption.created_at.desc()).limit(500)
    if promotion_rule_id:
        stmt = stmt.where(PromotionRedemption.promotion_rule_id == promotion_rule_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/coupons", response_model=list[CouponOut])
async def list_coupons(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[Coupon]:
    result = await db.execute(select(Coupon).order_by(Coupon.created_at.desc()))
    return list(result.scalars().all())


@router.post("/coupons", response_model=CouponOut, status_code=201)
async def create_coupon(
    payload: CouponCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> Coupon:
    existing = (await db.execute(select(Coupon).where(Coupon.code == payload.code))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Coupon code {payload.code} already exists")
    coupon = Coupon(**payload.model_dump())
    db.add(coupon)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="coupon.created",
        entity_type="coupon",
        entity_id=coupon.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(coupon)
    return coupon


@router.put("/coupons/{coupon_id}", response_model=CouponOut)
async def update_coupon(
    coupon_id: uuid.UUID,
    payload: CouponCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> Coupon:
    coupon = await db.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(status_code=404, detail="Coupon not found")
    old_value = {"active": coupon.active, "discount_value": float(coupon.discount_value)}
    for field, value in payload.model_dump().items():
        setattr(coupon, field, value)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="coupon.updated",
        entity_type="coupon",
        entity_id=coupon.id,
        old_value=old_value,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(coupon)
    return coupon
