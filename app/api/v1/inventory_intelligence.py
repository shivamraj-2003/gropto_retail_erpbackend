import uuid
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
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
                    select product_id, sum(revenue) as total_revenue,
                           avg(units) as mean_units, stddev_pop(units) as stddev_units,
                           coalesce((select p2.purchase_price from products p2 where p2.id = weekly.product_id), 0) as purchase_price
                    from weekly group by product_id
                ),
                ranked as (
                    select *, sum(total_revenue) over (order by total_revenue desc) / nullif(sum(total_revenue) over (), 0) as cum_share
                    from per_product
                )
                select product_id, total_revenue, mean_units, stddev_units, cum_share
                from ranked order by total_revenue desc
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
        days_of_inventory_row = (
            await db.execute(
                text(
                    """
                    select coalesce(ib.quantity, 0) as qty
                    from inventory_balances ib where ib.store_id = :store_id and ib.product_id = :product_id
                    """
                ),
                {"store_id": str(store_id), "product_id": str(r.product_id)},
            )
        ).first()
        current_qty = float(days_of_inventory_row.qty) if days_of_inventory_row else 0
        daily_velocity = float(r.mean_units) / 7 if r.mean_units else 0
        days_of_inventory = (current_qty / daily_velocity) if daily_velocity > 0 else 0
        metric = AbcXyzMetric(
            product_id=r.product_id,
            store_id=store_id,
            abc_class=abc_class,
            xyz_class=xyz_class,
            stock_turns=round(float(r.total_revenue) / max(current_qty * 1, 1), 2),
            days_of_inventory=round(days_of_inventory, 1),
        )
        db.add(metric)
        metrics.append(metric)
    await db.commit()
    for m in metrics:
        await db.refresh(m)
    return metrics


@router.get("/dead-slow-stock")
async def dead_slow_stock(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[dict]:
    """Products sitting in stock with zero or near-zero sales in the last 60
    days — the "dead/slow-moving stock alert" blueprint §7 calls for, absent
    until now."""
    require_store_access(store_id, current)
    rows = (
        await db.execute(
            text(
                """
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
                  and coalesce(sold.units_60d, 0) <= 1
                order by ib.quantity * p.purchase_price desc
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()
    return [
        {
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
