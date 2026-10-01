import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Store
from app.models.models_phase4 import AbcXyzMetric, ExpiryForecastSnapshot
from app.schemas.schemas_phase4 import AbcXyzOut, ExpiryForecastOut

router = APIRouter(prefix="/inventory-intelligence", tags=["inventory-intelligence"])


async def _compute_expiry_forecast(db: AsyncSession, store_id: uuid.UUID) -> ExpiryForecastSnapshot:
    """Real value-at-risk from actual batch data, bucketed at the blueprint's
    §7 7/15/30/60/90-day exposure windows. Replaces the previous behaviour of
    seeding fabricated numbers (₹50,050 etc.) whenever no snapshot existed."""
    row = (
        await db.execute(
            text(
                """
                select
                    coalesce(sum(quantity * purchase_cost) filter (where expiry_date <= current_date + interval '7 days'), 0) as e7,
                    coalesce(sum(quantity * purchase_cost) filter (where expiry_date <= current_date + interval '15 days'), 0) as e15,
                    coalesce(sum(quantity * purchase_cost) filter (where expiry_date <= current_date + interval '30 days'), 0) as e30,
                    coalesce(sum(quantity * purchase_cost) filter (where expiry_date <= current_date + interval '60 days'), 0) as e60,
                    coalesce(sum(quantity * purchase_cost) filter (where expiry_date <= current_date + interval '90 days'), 0) as e90
                from inventory_batches
                where store_id = :store_id and expiry_date is not null and quantity > 0
                """
            ),
            {"store_id": str(store_id)},
        )
    ).one()
    snapshot = ExpiryForecastSnapshot(
        store_id=store_id,
        exposure_7d=float(row.e7),
        exposure_15d=float(row.e15),
        exposure_30d=float(row.e30),
        exposure_60d=float(row.e60),
        exposure_90d=float(row.e90),
        total_value_at_risk=float(row.e90),
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


@router.get("/abc-xyz", response_model=list[AbcXyzOut])
async def get_abc_xyz_analysis(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[AbcXyzMetric]:
    require_store_access(store_id, current)
    result = await db.execute(select(AbcXyzMetric).where(AbcXyzMetric.store_id == store_id))
    rows = list(result.scalars().all())
    if not rows:
        # Nothing ever populates this table on its own — compute it now rather
        # than return an empty list forever.
        rows = await _compute_abc_xyz(db, store_id)
    return rows


async def _compute_abc_xyz(db: AsyncSession, store_id: uuid.UUID) -> list[AbcXyzMetric]:
    """ABC by cumulative revenue share (A=top 70%, B=next 20%, C=last 10%) and
    XYZ by demand variability (coefficient of variation of weekly units sold,
    X<0.5 stable, Y<1.0 moderate, Z>=1.0 erratic) over the last 90 days."""
    rows = (
        await db.execute(
            text(
                """
                with weekly as (
                    select si.product_id, date_trunc('week', s.billed_at) as wk, sum(si.quantity) as units, sum(si.line_total) as revenue
                    from sale_items si
                    join sales s on s.id = si.sale_id
                    where s.store_id = :store_id and s.status = 'completed' and s.billed_at >= current_date - interval '90 days'
                    group by si.product_id, date_trunc('week', s.billed_at)
                ),
                per_product as (
                    select product_id, sum(units) as total_units, sum(revenue) as total_revenue,
                           avg(units) as mean_units, stddev_pop(units) as stddev_units
                    from weekly group by product_id
                ),
                ranked as (
                    select *, sum(total_revenue) over (order by total_revenue desc) / nullif(sum(total_revenue) over (), 0) as cum_share
                    from per_product
                )
                -- Point 7 audit fix: this used to re-query inventory_balances
                -- once per product inside a Python loop (N+1) — now a single
                -- joined query. current_qty also feeds a real stock-turns
                -- formula (units sold / average inventory on hand), replacing
                -- the old revenue-over-quantity figure that mixed ₹ and units.
                select r.product_id, r.total_units, r.total_revenue, r.mean_units, r.stddev_units, r.cum_share,
                       coalesce(ib.quantity, 0) as current_qty
                from ranked r
                left join inventory_balances ib on ib.product_id = r.product_id and ib.store_id = :store_id
                order by r.total_revenue desc
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()

    metrics: list[AbcXyzMetric] = []
    for r in rows:
        abc_class = "A" if r.cum_share <= 0.7 else "B" if r.cum_share <= 0.9 else "C"
        cv = float(r.stddev_units or 0) / float(r.mean_units) if r.mean_units else 0
        xyz_class = "X" if cv < 0.5 else "Y" if cv < 1.0 else "Z"
        current_qty = float(r.current_qty)
        daily_velocity = float(r.mean_units) / 7 if r.mean_units else 0
        days_of_inventory = (current_qty / daily_velocity) if daily_velocity > 0 else 0
        # Average inventory proxy: current on-hand vs the demand actually
        # sold over the period — avoids divide-by-zero while staying a real
        # units-per-units ratio (times the stock turned over in 90 days).
        avg_inventory_estimate = max((current_qty + float(r.total_units)) / 2, 1)
        stock_turns = round(float(r.total_units) / avg_inventory_estimate, 2)
        metric = AbcXyzMetric(
            product_id=r.product_id,
            store_id=store_id,
            abc_class=abc_class,
            xyz_class=xyz_class,
            stock_turns=stock_turns,
            days_of_inventory=round(days_of_inventory, 1),
        )
        db.add(metric)
        metrics.append(metric)
    await db.commit()
    for m in metrics:
        await db.refresh(m)
    return metrics


@router.post("/abc-xyz/recalculate", response_model=list[AbcXyzOut])
async def recalculate_abc_xyz(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> list[AbcXyzMetric]:
    """Point 7 audit fix: ABC/XYZ was previously computed once, lazily, the
    first time the table happened to be empty for a store — and then frozen
    forever with no way to refresh it as sales data changed. This forces a
    real recompute on demand; see also the daily scheduled job."""
    require_store_access(store_id, current)
    await db.execute(delete(AbcXyzMetric).where(AbcXyzMetric.store_id == store_id))
    return await _compute_abc_xyz(db, store_id)


async def refresh_all_abc_xyz(db: AsyncSession) -> int:
    """Called by the daily scheduler job — recomputes ABC/XYZ for every
    active store rather than leaving it to whichever store's dashboard
    happens to be opened next."""
    store_ids = (await db.execute(select(Store.id).where(Store.is_active.is_(True)))).scalars().all()
    count = 0
    for store_id in store_ids:
        await db.execute(delete(AbcXyzMetric).where(AbcXyzMetric.store_id == store_id))
        metrics = await _compute_abc_xyz(db, store_id)
        count += len(metrics)
    return count


SLOW_STOCK_UNITS_THRESHOLD = 5  # Point 7 audit fix: "slow" now a distinct tier from "dead" (<=1 unit), not conflated


@router.get("/dead-slow-stock")
async def dead_slow_stock(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[dict]:
    """Products sitting in stock with zero/near-zero sales ("dead", <=1 unit
    in 60 days) or meaningfully below-normal sales ("slow", <=5 units) — two
    distinct tiers per the blueprint, not one conflated threshold."""
    require_store_access(store_id, current)
    rows = (
        await db.execute(
            text(
                f"""
                select p.id as product_id, p.name, p.sku, ib.quantity, p.purchase_price,
                       coalesce(sold.units_60d, 0) as units_sold_60d
                from inventory_balances ib
                join products p on p.id = ib.product_id
                left join (
                    select si.product_id, sum(si.quantity) as units_60d
                    from sale_items si join sales s on s.id = si.sale_id
                    where s.store_id = :store_id and s.status = 'completed' and s.billed_at >= current_date - interval '60 days'
                    group by si.product_id
                ) sold on sold.product_id = p.id
                where ib.store_id = :store_id and ib.quantity > 0
                  and coalesce(sold.units_60d, 0) <= {SLOW_STOCK_UNITS_THRESHOLD}
                order by ib.quantity * p.purchase_price desc
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()
    return [
        {
            "category": "dead" if float(r.units_sold_60d) <= 1 else "slow",
            "product_id": str(r.product_id),
            "name": r.name,
            "sku": r.sku,
            "quantity": float(r.quantity),
            "value_tied_up": float(r.quantity) * float(r.purchase_price),
            "units_sold_60d": float(r.units_sold_60d),
        }
        for r in rows
    ]


@router.get("/expiry-forecast", response_model=ExpiryForecastOut)
async def get_expiry_forecast(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.view")),
) -> ExpiryForecastSnapshot:
    require_store_access(store_id, current)
    stmt = select(ExpiryForecastSnapshot).where(ExpiryForecastSnapshot.store_id == store_id).order_by(ExpiryForecastSnapshot.generated_at.desc())
    snapshot = (await db.execute(stmt)).scalars().first()
    # Recompute rather than serve a stale/absent snapshot — batches change
    # daily and this endpoint has no separate refresh trigger.
    if snapshot is None or snapshot.generated_at.date() < date.today():
        snapshot = await _compute_expiry_forecast(db, store_id)
    return snapshot


@router.get("/sell-through")
async def sell_through(
    store_id: uuid.UUID,
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[dict]:
    """Point 7 audit fix: sell-through didn't exist anywhere in the codebase.
    Formula: units sold in the period / (beginning-of-period inventory +
    units received during the period) — the standard retail definition,
    distinct from days-of-inventory (which looks at current stock vs
    velocity, not what fraction of available supply actually sold)."""
    require_store_access(store_id, current)
    rows = (
        await db.execute(
            text(
                """
                with period_sales as (
                    select si.product_id, sum(si.quantity) as units_sold
                    from sale_items si join sales s on s.id = si.sale_id
                    where s.store_id = :store_id and s.status = 'completed'
                      and s.billed_at >= current_date - make_interval(days => :days)
                    group by si.product_id
                ),
                period_movements as (
                    select im.product_id,
                           sum(im.delta) as net_delta,
                           sum(im.delta) filter (where im.delta > 0) as received
                    from inventory_movements im
                    where im.store_id = :store_id and im.created_at >= current_date - make_interval(days => :days)
                    group by im.product_id
                )
                select p.id as product_id, p.name, p.sku,
                       coalesce(ps.units_sold, 0) as units_sold,
                       coalesce(ib.quantity, 0) - coalesce(pm.net_delta, 0) as beginning_inventory,
                       coalesce(pm.received, 0) as received_during_period
                from products p
                join inventory_balances ib on ib.product_id = p.id and ib.store_id = :store_id
                left join period_sales ps on ps.product_id = p.id
                left join period_movements pm on pm.product_id = p.id
                where coalesce(ps.units_sold, 0) > 0 or ib.quantity > 0
                order by coalesce(ps.units_sold, 0) desc
                """
            ),
            {"store_id": str(store_id), "days": days},
        )
    ).all()
    result = []
    for r in rows:
        supply = max(float(r.beginning_inventory), 0) + max(float(r.received_during_period), 0)
        sell_through_pct = round(float(r.units_sold) / supply * 100, 1) if supply > 0 else None
        result.append(
            {
                "product_id": str(r.product_id),
                "name": r.name,
                "sku": r.sku,
                "units_sold": float(r.units_sold),
                "beginning_inventory": max(float(r.beginning_inventory), 0),
                "received_during_period": max(float(r.received_during_period), 0),
                "sell_through_pct": sell_through_pct,
                "period_days": days,
            }
        )
    return result


@router.get("/shrinkage")
async def shrinkage_analytics(
    store_id: uuid.UUID,
    days: int = 90,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.view")),
) -> dict:
    """Point 7 audit fix: "shrinkage" previously existed only as one allowed
    value of ReasonCodeMaster.category — nothing aggregated adjustments
    tagged with it into an actual report. Sources every inventory_movement
    whose reason_code maps to a reason-code-master row in the shrinkage
    category, valued at each product's purchase price."""
    require_store_access(store_id, current)
    rows = (
        await db.execute(
            text(
                """
                select p.id as product_id, p.name, p.sku,
                       sum(-im.delta) as shrinkage_units, sum(-im.delta * p.purchase_price) as shrinkage_value
                from inventory_movements im
                join products p on p.id = im.product_id
                join reason_codes_master rcm on rcm.code = im.reason_code and rcm.category = 'shrinkage'
                where im.store_id = :store_id and im.delta < 0
                  and im.created_at >= current_date - make_interval(days => :days)
                group by p.id, p.name, p.sku
                order by shrinkage_value desc
                """
            ),
            {"store_id": str(store_id), "days": days},
        )
    ).all()
    lines = [
        {
            "product_id": str(r.product_id),
            "name": r.name,
            "sku": r.sku,
            "shrinkage_units": float(r.shrinkage_units),
            "shrinkage_value": float(r.shrinkage_value),
        }
        for r in rows
    ]
    return {
        "period_days": days,
        "total_shrinkage_value": round(sum(l["shrinkage_value"] for l in lines), 2),
        "total_shrinkage_units": round(sum(l["shrinkage_units"] for l in lines), 2),
        "lines": lines,
    }


@router.get("/transfer-recommendations")
async def transfer_recommendations(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[dict]:
    """Point 7 audit fix: inter-store transfer recommendations previously
    existed only as narrative text inside two CEO alert descriptions ("...or
    initiate an inter-store transfer...") — no code identified a real source
    (excess) or destination (shortage) store, or a quantity. This pairs, per
    SKU, the store with the most days-of-inventory above its own reorder
    point against the store below its reorder point with the least stock,
    using each store's own 28-day velocity — real excess and real shortage,
    not a guess."""
    if not current.sees_all_stores():
        raise HTTPException(status_code=403, detail="Enterprise-wide roles only")
    rows = (
        await db.execute(
            text(
                """
                with velocity as (
                    select si.product_id, s.store_id, sum(si.quantity) / 28.0 as daily_velocity
                    from sale_items si join sales s on s.id = si.sale_id
                    where s.status = 'completed' and s.billed_at >= current_date - interval '28 days'
                    group by si.product_id, s.store_id
                ),
                stock_position as (
                    select ib.product_id, ib.store_id, ib.quantity,
                           coalesce(v.daily_velocity, 0) as daily_velocity,
                           rp.min_qty, rp.max_qty,
                           case when coalesce(v.daily_velocity, 0) > 0 then ib.quantity / v.daily_velocity else null end as days_of_stock
                    from inventory_balances ib
                    left join velocity v on v.product_id = ib.product_id and v.store_id = ib.store_id
                    left join reorder_points rp on rp.product_id = ib.product_id and rp.store_id = ib.store_id
                    where ib.quantity > 0
                ),
                excess as (
                    select * from stock_position
                    where max_qty > 0 and quantity > max_qty and (days_of_stock is null or days_of_stock > 60)
                ),
                shortage as (
                    select * from stock_position
                    where min_qty > 0 and quantity <= min_qty
                )
                select e.product_id, e.store_id as source_store_id, s.store_id as dest_store_id,
                       e.quantity as source_qty, e.max_qty as source_max_qty,
                       s.quantity as dest_qty, s.min_qty as dest_min_qty,
                       least(e.quantity - e.max_qty, greatest(s.min_qty - s.quantity, 1)) as recommended_qty
                from excess e
                join shortage s on s.product_id = e.product_id and s.store_id != e.store_id
                order by recommended_qty desc
                limit 200
                """
            )
        )
    ).all()
    return [
        {
            "product_id": str(r.product_id),
            "source_store_id": str(r.source_store_id),
            "dest_store_id": str(r.dest_store_id),
            "source_qty": float(r.source_qty),
            "dest_qty": float(r.dest_qty),
            "recommended_transfer_qty": float(r.recommended_qty),
        }
        for r in rows
    ]
