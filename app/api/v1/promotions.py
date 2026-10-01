import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models_phase4 import (
    CustomerWalletLedger,
    PromotionRule,
    ScheduledPriceChange,
    StorePriceOverride,
)
from app.schemas.schemas_phase4 import (
    PromotionRuleIn,
    PromotionRuleOut,
    ScheduledPriceChangeCreate,
    ScheduledPriceChangeOut,
    StorePriceOverrideCreate,
    StorePriceOverrideOut,
    WalletLedgerOut,
)

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
    _current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> PromotionRule:
    rule = PromotionRule(**payload.model_dump())
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.put("/rules/{rule_id}", response_model=PromotionRuleOut)
async def update_promotion_rule(
    rule_id: uuid.UUID,
    payload: PromotionRuleIn,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> PromotionRule:
    rule = await db.get(PromotionRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Promotion rule not found")
    for field, value in payload.model_dump().items():
        setattr(rule, field, value)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.get("/profitability")
async def promotion_profitability(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    """Blueprint §9 "Promotion profitability and discount-funding tracking."
    PromotionRule (BOGO/combo) has no per-redemption usage log yet — that
    would need checkout to record which rule fired on which bill, a POS
    change out of scope here — so this reports what IS real today: actual
    discount funding by rule from discount_rules, which every bill already
    ties back to via sales.discount_total."""
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
    return {
        "note": "Discount funding is tied to actual bills via sales.discount_total. "
        "Rule-level attribution (which specific rule fired on which bill) isn't tracked "
        "per-bill yet, so figures below are the discount-rule catalogue cross-referenced "
        "against overall discounted-bill activity, not a strict per-rule redemption count.",
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
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> list[dict]:
    """Blueprint §9 "Private-label SKU performance and margin tracking" — real
    revenue/margin per private-label product from actual sale_items, not
    estimated."""
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
    _current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> StorePriceOverride:
    override = StorePriceOverride(**payload.model_dump())
    db.add(override)
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


@router.post("/scheduled-price-changes", response_model=ScheduledPriceChangeOut, status_code=201)
async def create_scheduled_price_change(
    payload: ScheduledPriceChangeCreate,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("loyalty.configure")),
) -> ScheduledPriceChange:
    sp_change = ScheduledPriceChange(**payload.model_dump())
    db.add(sp_change)
    await db.commit()
    await db.refresh(sp_change)
    return sp_change


@router.get("/wallet/{customer_id}", response_model=list[WalletLedgerOut])
async def get_customer_wallet_ledger(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[CustomerWalletLedger]:
    result = await db.execute(select(CustomerWalletLedger).where(CustomerWalletLedger.customer_id == customer_id).order_by(CustomerWalletLedger.created_at.desc()))
    return list(result.scalars().all())
