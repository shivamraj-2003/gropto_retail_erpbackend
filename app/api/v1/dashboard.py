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
