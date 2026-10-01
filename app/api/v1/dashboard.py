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
    region_breakdown = (
        await db.execute(
            text(
                """
                select coalesce(r.name, 'Unassigned') as region_name,
                       coalesce(sum(s.grand_total), 0) revenue,
                       count(distinct st.id) as store_count
                from stores st
                left join regions r on r.id = st.region_id
                left join sales s on s.store_id = st.id and s.billed_at::date = current_date and s.status = 'completed'
                group by r.name order by revenue desc
                """
            )
        )
    ).all()
    return {
        "sales_today": float(row.sales_today),
        "bills_today": int(row.bills_today),
        "store_breakdown": [{"store": r.name, "revenue": float(r.revenue)} for r in store_breakdown],
        "region_breakdown": [
            {"region": r.region_name, "revenue": float(r.revenue), "store_count": int(r.store_count)}
            for r in region_breakdown
        ],
    }


@router.get("/ceo-command-center")
async def ceo_command_center(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    """The 9-block CEO home screen (blueprint §3/§22) in one call: Sales,
    Margin, Stores, Inventory, Warehouse, Customers, Finance, Online, Control
    Tower. Every figure is computed here, not narrated — Warehouse/Customers/
    Online were previously absent from any dashboard endpoint entirely."""

    sales = (
        await db.execute(
            text(
                """
                select
                    coalesce(sum(grand_total) filter (where billed_at::date = current_date), 0) as today,
                    coalesce(sum(grand_total) filter (where date_trunc('month', billed_at) = date_trunc('month', now())), 0) as mtd,
                    coalesce(sum(grand_total) filter (where date_trunc('year', billed_at) = date_trunc('year', now())), 0) as ytd,
                    coalesce(count(*) filter (where billed_at::date = current_date), 0) as bills_today,
                    coalesce(sum(grand_total) filter (
                        where billed_at >= current_date - interval '7 days'), 0) as last_7d,
                    coalesce(sum(grand_total) filter (
                        where billed_at >= current_date - interval '14 days'
                          and billed_at < current_date - interval '7 days'), 0) as prior_7d
                from sales where status = 'completed'
                """
            )
        )
    ).one()
    units_today = (
        await db.execute(
            text(
                """
                select coalesce(sum(si.quantity), 0) from sale_items si
                join sales s on s.id = si.sale_id
                where s.billed_at::date = current_date and s.status = 'completed'
                """
            )
        )
    ).scalar_one()
    online_today = (
        await db.execute(
            text("select coalesce(sum(grand_total), 0) from orders where created_at::date = current_date and status != 'cancelled'")
        )
    ).scalar_one()
    same_store_growth = float(sales.last_7d) - float(sales.prior_7d)
    same_store_growth_pct = (same_store_growth / float(sales.prior_7d) * 100) if sales.prior_7d else None

    margin = (
        await db.execute(
            text(
                """
                select
                    coalesce(sum(s.grand_total), 0) as revenue_mtd,
                    coalesce(sum(si.taxable_value), 0) as taxable_mtd,
                    coalesce((select sum(amount) from expenses where status = 'approved'
                        and date_trunc('month', created_at) = date_trunc('month', now())), 0) as expenses_mtd,
                    coalesce(sum(s.discount_total), 0) as discounts_mtd
                from sales s
                left join sale_items si on si.sale_id = s.id
                where s.status = 'completed' and date_trunc('month', s.billed_at) = date_trunc('month', now())
                """
            )
        )
    ).one()
    gross_margin_value = float(margin.revenue_mtd) - float(margin.expenses_mtd)
    gross_margin_pct = (gross_margin_value / float(margin.revenue_mtd) * 100) if margin.revenue_mtd else None

    store_rank = (
        await db.execute(
            text(
                """
                select st.name, coalesce(sum(s.grand_total), 0) revenue, count(s.id) bills
                from stores st
                left join sales s on s.store_id = st.id and date_trunc('month', s.billed_at) = date_trunc('month', now()) and s.status = 'completed'
                where st.is_active = true
                group by st.name order by revenue desc
                """
            )
        )
    ).all()

    inventory = (
        await db.execute(
            text(
                """
                select
                    coalesce(sum(ib.quantity * p.purchase_price), 0) as inventory_value,
                    count(*) filter (where ib.quantity <= 0) as oos_count,
                    count(*) filter (where ib.quantity > 0) as in_stock_count
                from inventory_balances ib join products p on p.id = ib.product_id
                """
            )
        )
    ).one()
    near_expiry_value = (
        await db.execute(
            text(
                """
                select coalesce(sum(quantity * purchase_cost), 0) from inventory_batches
                where expiry_date is not null and expiry_date between current_date and current_date + interval '15 days'
                  and quantity > 0
                """
            )
        )
    ).scalar_one()

    warehouse = (
        await db.execute(
            text(
                """
                select
                    count(*) filter (where g.status = 'draft' or g.status = 'qc_hold') as pending_grns,
                    (select count(*) from transfers where status = 'dispatched') as pending_transfers,
                    (select coalesce(avg(case when gi.received_qty = gi.expected_qty then 100.0 else
                        greatest(0, 100 - abs(gi.received_qty - gi.expected_qty) / nullif(gi.expected_qty, 0) * 100) end), 100)
                     from grn_items gi) as pick_accuracy_pct
                from grn g
                """
            )
        )
    ).one()

    customers = (
        await db.execute(
            text(
                """
                select
                    count(distinct customer_id) filter (where billed_at::date = current_date) as active_today,
                    count(distinct customer_id) filter (where billed_at >= current_date - interval '30 days') as active_30d,
                    coalesce(sum(loyalty_points_earned) filter (where date_trunc('month', billed_at) = date_trunc('month', now())), 0) as loyalty_points_mtd
                from sales where status = 'completed' and customer_id is not null
                """
            )
        )
    ).one()
    new_vs_repeat = (
        await db.execute(
            text(
                """
                with first_sale as (
                    select customer_id, min(billed_at) as first_at from sales
                    where status = 'completed' and customer_id is not null group by customer_id
                )
                select
                    count(*) filter (where first_at::date = current_date) as new_today,
                    count(*) filter (where first_at::date < current_date) as repeat_base
                from first_sale
                """
            )
        )
    ).one()

    finance = (
        await db.execute(
            text(
                """
                select
                    coalesce((select sum(cash_deposited) from bank_deposits where business_date = current_date), 0) as cash_deposited_today,
                    coalesce((select sum(amount_due) from payables where status != 'paid'), 0) as payables_outstanding,
                    coalesce((select sum(amount_due) from customer_receivables where status != 'paid'), 0) as receivables_outstanding,
                    coalesce((select sum(amount) from expenses where status = 'approved'
                        and date_trunc('month', created_at) = date_trunc('month', now())), 0) as expenses_mtd
                """
            )
        )
    ).one()

    online = (
        await db.execute(
            text(
                """
                select
                    count(*) as orders_mtd,
                    count(*) filter (where status = 'cancelled') as cancellations_mtd,
                    count(*) filter (where status = 'delivered') as delivered_mtd,
                    coalesce(avg(extract(epoch from (delivered_at - created_at)) / 60) filter (where delivered_at is not null), null) as avg_delivery_minutes
                from orders where date_trunc('month', created_at) = date_trunc('month', now())
                """
            )
        )
    ).one()

    control_tower = (
        await db.execute(
            text(
                """
                select
                    (select count(*) from ceo_alerts where status = 'active') as active_alerts,
                    (select count(*) from fraud_alerts where status = 'open') as open_fraud_alerts,
                    (select count(*) from approval_requests where status = 'pending') as pending_approvals,
                    (select count(*) from inventory_balances where quantity < 0) as negative_stock_lines
                """
            )
        )
    ).one()

    return {
        "sales": {
            "today": float(sales.today),
            "mtd": float(sales.mtd),
            "ytd": float(sales.ytd),
            "bills_today": int(sales.bills_today),
            "units_today": float(units_today),
            "offline_today": float(sales.today),
            "online_today": float(online_today),
            "same_store_growth_value": same_store_growth,
            "same_store_growth_pct": round(same_store_growth_pct, 1) if same_store_growth_pct is not None else None,
        },
        "margin": {
            "gross_margin_value_mtd": gross_margin_value,
            "gross_margin_pct_mtd": round(gross_margin_pct, 1) if gross_margin_pct is not None else None,
            "discount_leakage_mtd": float(margin.discounts_mtd),
        },
        "stores": {
            "ranking": [{"store": r.name, "revenue_mtd": float(r.revenue), "bills_mtd": int(r.bills)} for r in store_rank],
            "top": store_rank[0].name if store_rank else None,
            "bottom": store_rank[-1].name if store_rank else None,
        },
        "inventory": {
            "value": float(inventory.inventory_value),
            "out_of_stock_lines": int(inventory.oos_count),
            "in_stock_lines": int(inventory.in_stock_count),
            "near_expiry_value_15d": float(near_expiry_value),
        },
        "warehouse": {
            "pending_grns": int(warehouse.pending_grns),
            "pending_transfers": int(warehouse.pending_transfers),
            "pick_accuracy_pct": round(float(warehouse.pick_accuracy_pct), 1),
        },
        "customers": {
            "active_today": int(customers.active_today),
            "active_30d": int(customers.active_30d),
            "loyalty_points_issued_mtd": float(customers.loyalty_points_mtd),
            "new_customers_today": int(new_vs_repeat.new_today),
            "repeat_customer_base": int(new_vs_repeat.repeat_base),
        },
        "finance": {
            "cash_deposited_today": float(finance.cash_deposited_today),
            "payables_outstanding": float(finance.payables_outstanding),
            "receivables_outstanding": float(finance.receivables_outstanding),
            "expenses_mtd": float(finance.expenses_mtd),
        },
        "online": {
            "orders_mtd": int(online.orders_mtd),
            "cancellations_mtd": int(online.cancellations_mtd),
            "cancellation_rate_pct": round(online.cancellations_mtd / online.orders_mtd * 100, 1) if online.orders_mtd else None,
            "delivered_mtd": int(online.delivered_mtd),
            "avg_delivery_minutes": round(float(online.avg_delivery_minutes), 1) if online.avg_delivery_minutes is not None else None,
        },
        "control_tower": {
            "active_ceo_alerts": int(control_tower.active_alerts),
            "open_fraud_alerts": int(control_tower.open_fraud_alerts),
            "pending_approvals": int(control_tower.pending_approvals),
            "negative_stock_lines": int(control_tower.negative_stock_lines),
        },
    }
