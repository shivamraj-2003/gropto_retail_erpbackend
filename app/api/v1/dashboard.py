import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/store/{store_id}")
async def store_dashboard(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    require_store_access(store_id, current)
    row = (
        await db.execute(
            text(
                """
                select
                    coalesce(sum(grand_total) filter (where billed_at::date = current_date), 0) as sales_today,
                    coalesce(sum(grand_total) filter (where date_trunc('month', billed_at) = date_trunc('month', now())), 0) as sales_mtd,
                    coalesce(count(*) filter (where billed_at::date = current_date), 0) as bills_today,
                    coalesce(avg(grand_total) filter (where billed_at::date = current_date), 0) as aov_today
                from sales where store_id = :store_id and status = 'completed'
                """
            ),
            {"store_id": str(store_id)},
        )
    ).one()

    inventory_value = (
        await db.execute(
            text(
                """
                select coalesce(sum(ib.quantity * p.purchase_price), 0)
                from inventory_balances ib join products p on p.id = ib.product_id
                where ib.store_id = :store_id
                """
            ),
            {"store_id": str(store_id)},
        )
    ).scalar_one()

    pending_approvals = (
        await db.execute(
            text("select count(*) from approval_requests where store_id = :store_id and status = 'pending'"),
            {"store_id": str(store_id)},
        )
    ).scalar_one()

    return {
        "sales_today": float(row.sales_today),
        "sales_mtd": float(row.sales_mtd),
        "bills_today": int(row.bills_today),
        "aov_today": float(row.aov_today),
        "inventory_value": float(inventory_value),
        "pending_approvals": int(pending_approvals),
    }


@router.get("/store/{store_id}/trend")
async def store_sales_trend(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    """Last 14 days of revenue + a top-10 products-by-revenue breakdown —
    backs the dashboard charts and answers "where sales are highest/lowest"
    at a glance without an Excel round trip."""
    require_store_access(store_id, current)
    daily = (
        await db.execute(
            text(
                """
                select billed_at::date as day, coalesce(sum(grand_total), 0) as revenue, count(*) as bills
                from sales
                where store_id = :store_id and status = 'completed'
                  and billed_at >= current_date - interval '13 days'
                group by billed_at::date
                order by billed_at::date
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()

    top_products = (
        await db.execute(
            text(
                """
                select si.product_name_snapshot as name,
                       sum(si.quantity) as units,
                       sum(si.line_total) as revenue
                from sale_items si
                join sales s on s.id = si.sale_id
                where s.store_id = :store_id and s.status = 'completed'
                  and s.billed_at >= current_date - interval '13 days'
                group by si.product_name_snapshot
                order by revenue desc
                limit 10
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()

    return {
        "daily": [{"day": str(r.day), "revenue": float(r.revenue), "bills": int(r.bills)} for r in daily],
        "top_products": [{"name": r.name, "units": float(r.units), "revenue": float(r.revenue)} for r in top_products],
    }


@router.get("/company")
async def company_dashboard(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    """Super Admin dashboard driven by real aggregates across every store."""
    row = (
        await db.execute(
            text(
                """
                select
                    coalesce(sum(grand_total) filter (where billed_at::date = current_date), 0) as sales_today,
                    coalesce(count(*) filter (where billed_at::date = current_date), 0) as bills_today
                from sales where status = 'completed'
                """
            )
        )
    ).one()
    store_breakdown = (
        await db.execute(
            text(
                """
                select st.name, coalesce(sum(s.grand_total), 0) revenue
                from stores st
                left join sales s on s.store_id = st.id and s.billed_at::date = current_date and s.status = 'completed'
                group by st.name order by revenue desc
                """
            )
        )
    ).all()
    return {
        "sales_today": float(row.sales_today),
        "bills_today": int(row.bills_today),
        "store_breakdown": [{"store": r.name, "revenue": float(r.revenue)} for r in store_breakdown],
    }
