import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DISPATCH_SLA_MINUTES = 120


async def _evaluate_alerts_safely(db: AsyncSession) -> None:
    """Point 3 audit fix: ceo_command_center used to read ceo_alerts straight
    off the table without ever running the evaluation that populates it — the
    count could be stale/zero despite a real, current risk condition unless
    someone had separately hit GET /control-tower/alerts recently. Runs it
    inline here too (on top of the new 15-min scheduled job) so a fresh page
    load always reflects live conditions. Import is local to avoid a circular
    import between dashboard.py and control_tower.py."""
    from app.api.v1.control_tower import _evaluate_ceo_alerts

    await _evaluate_ceo_alerts(db)


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
    """Company-wide dashboard, real aggregates. Point 2 audit fix: this used
    to run with no store-scope filter at all regardless of caller — fine by
    accident for Super Admin/CEO/COO/Finance Head (who are meant to see
    everything), but it also leaked full company-wide figures to a Regional
    Manager, who the blueprint restricts to their assigned stores. Scoped
    the same way every other list endpoint in this app already is."""
    scoped = not current.sees_all_stores()
    store_filter_sales = "and s.store_id = any(:store_ids)" if scoped else ""
    store_filter_st = "and st.id = any(:store_ids)" if scoped else ""
    params = {"store_ids": [str(s) for s in current.store_ids]} if scoped else {}

    row = (
        await db.execute(
            text(
                f"""
                select
                    coalesce(sum(grand_total) filter (where billed_at::date = current_date), 0) as sales_today,
                    coalesce(count(*) filter (where billed_at::date = current_date), 0) as bills_today
                from sales s where status = 'completed' {store_filter_sales}
                """
            ),
            params,
        )
    ).one()
    store_breakdown = (
        await db.execute(
            text(
                f"""
                select st.name, coalesce(sum(s.grand_total), 0) revenue
                from stores st
                left join sales s on s.store_id = st.id and s.billed_at::date = current_date and s.status = 'completed'
                where 1=1 {store_filter_st}
                group by st.name order by revenue desc
                """
            ),
            params,
        )
    ).all()
    region_breakdown = (
        await db.execute(
            text(
                f"""
                select coalesce(r.name, 'Unassigned') as region_name,
                       coalesce(sum(s.grand_total), 0) revenue,
                       count(distinct st.id) as store_count
                from stores st
                left join regions r on r.id = st.region_id
                left join sales s on s.store_id = st.id and s.billed_at::date = current_date and s.status = 'completed'
                where 1=1 {store_filter_st}
                group by r.name order by revenue desc
                """
            ),
            params,
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
    Online were previously absent from any dashboard endpoint entirely.

    Unfiltered across every store by design — this is the enterprise-wide
    view, not a per-store or per-region one. `report.export` alone isn't
    enough of a gate for that (Regional Manager holds it too, for their own
    scoped reports); explicitly require the enterprise-wide roles here
    (Point 2 audit fix — the same leak `/dashboard/company` had). A Regional
    Manager's equivalent is the now-scoped `GET /dashboard/company`."""
    if not current.sees_all_stores():
        raise HTTPException(status_code=403, detail="Enterprise-wide roles only — see /dashboard/company for a scoped view")

    sales = (
        await db.execute(
            text(
                """
                select
                    coalesce(sum(grand_total) filter (where billed_at::date = current_date), 0) as today,
                    coalesce(sum(grand_total) filter (where date_trunc('month', billed_at) = date_trunc('month', now())), 0) as mtd,
                    coalesce(sum(grand_total) filter (where date_trunc('year', billed_at) = date_trunc('year', now())), 0) as ytd,
                    coalesce(count(*) filter (where billed_at::date = current_date), 0) as bills_today,
                    coalesce(count(*) filter (where date_trunc('month', billed_at) = date_trunc('month', now())), 0) as bills_mtd,
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
                    -- Point 3 audit fix: true COGS-based gross margin — previously
                    -- this was revenue minus operating expenses, not a real
                    -- cost-of-goods figure (no join to product cost existed).
                    coalesce(sum(si.quantity * p.purchase_price), 0) as cogs_mtd,
                    coalesce((select sum(amount) from expenses where status = 'approved'
                        and date_trunc('month', created_at) = date_trunc('month', now())), 0) as expenses_mtd,
                    coalesce(sum(s.discount_total), 0) as discounts_mtd
                from sales s
                left join sale_items si on si.sale_id = s.id
                left join products p on p.id = si.product_id
                where s.status = 'completed' and date_trunc('month', s.billed_at) = date_trunc('month', now())
                """
            )
        )
    ).one()
    gross_margin_value = float(margin.revenue_mtd) - float(margin.cogs_mtd)
    gross_margin_pct = (gross_margin_value / float(margin.revenue_mtd) * 100) if margin.revenue_mtd else None
    net_contribution_mtd = gross_margin_value - float(margin.expenses_mtd)

    category_margin = (
        await db.execute(
            text(
                """
                select coalesce(c.name, 'Uncategorized') as category,
                       coalesce(sum(si.quantity * si.unit_price - si.line_discount), 0) as revenue,
                       coalesce(sum(si.quantity * p.purchase_price), 0) as cogs
                from sale_items si
                join sales s on s.id = si.sale_id
                join products p on p.id = si.product_id
                left join categories c on c.id = p.category_id
                where s.status = 'completed' and date_trunc('month', s.billed_at) = date_trunc('month', now())
                group by c.name
                order by (sum(si.quantity * si.unit_price - si.line_discount) - sum(si.quantity * p.purchase_price)) desc
                """
            )
        )
    ).all()

    store_rank = (
        await db.execute(
            text(
                """
                with footfall_mtd as (
                    select store_id, sum(footfall_count) as footfall
                    from store_footfall
                    where date_trunc('month', business_date) = date_trunc('month', now())
                    group by store_id
                )
                select st.id as store_id,
                       st.name,
                       st.area_sqft,
                       st.target_revenue_monthly,
                       coalesce(sum(s.grand_total), 0) revenue,
                       count(s.id) bills,
                       coalesce(sum(si.quantity * p.purchase_price), 0) as cogs,
                       ff.footfall
                from stores st
                left join sales s on s.store_id = st.id and date_trunc('month', s.billed_at) = date_trunc('month', now()) and s.status = 'completed'
                left join sale_items si on si.sale_id = s.id
                left join products p on p.id = si.product_id
                left join footfall_mtd ff on ff.store_id = st.id
                where st.is_active = true
                group by st.id, st.name, st.area_sqft, st.target_revenue_monthly, ff.footfall
                order by revenue desc
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

    # Point 3 audit fix: Dead Stock and Excess Stock had no company-wide
    # figure anywhere (dead-slow-stock existed only as a per-store endpoint);
    # Ageing had no query at all. All three computed here from the same
    # last-60-day sales velocity already used by inventory_intelligence.py's
    # per-store version, just aggregated company-wide.
    dead_excess = (
        await db.execute(
            text(
                """
                with velocity as (
                    select si.product_id, sum(si.quantity) as units_60d
                    from sale_items si join sales s on s.id = si.sale_id
                    where s.status = 'completed' and s.billed_at >= current_date - interval '60 days'
                    group by si.product_id
                )
                select
                    coalesce(sum(ib.quantity * p.purchase_price) filter (where coalesce(v.units_60d, 0) <= 1), 0) as dead_stock_value,
                    count(*) filter (where coalesce(v.units_60d, 0) <= 1 and ib.quantity > 0) as dead_stock_lines,
                    coalesce(sum(ib.quantity * p.purchase_price) filter (
                        where v.units_60d > 0 and ib.quantity > (v.units_60d / 60.0) * 90), 0) as excess_stock_value,
                    count(*) filter (where v.units_60d > 0 and ib.quantity > (v.units_60d / 60.0) * 90) as excess_stock_lines
                from inventory_balances ib
                join products p on p.id = ib.product_id
                left join velocity v on v.product_id = ib.product_id
                where ib.quantity > 0
                """
            )
        )
    ).one()
    ageing = (
        await db.execute(
            text(
                """
                select
                    coalesce(sum(quantity * purchase_cost) filter (where created_at >= current_date - interval '30 days'), 0) as b0_30,
                    coalesce(sum(quantity * purchase_cost) filter (where created_at < current_date - interval '30 days' and created_at >= current_date - interval '60 days'), 0) as b31_60,
                    coalesce(sum(quantity * purchase_cost) filter (where created_at < current_date - interval '60 days' and created_at >= current_date - interval '90 days'), 0) as b61_90,
                    coalesce(sum(quantity * purchase_cost) filter (where created_at < current_date - interval '90 days'), 0) as b90_plus
                from inventory_batches where quantity > 0
                """
            )
        )
    ).one()

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
    # Point 3 audit fix: Dispatch SLA had no threshold/breach detection
    # anywhere — now real, using dispatched_at/packed_at stamped by
    # dispatch_order()/pick_order() in oms.py.
    dispatch_sla = (
        await db.execute(
            text(
                f"""
                select
                    count(*) filter (where dispatched_at is not null and packed_at is not null) as with_timing,
                    count(*) filter (where dispatched_at is not null and packed_at is not null
                        and dispatched_at - packed_at <= interval '{DISPATCH_SLA_MINUTES} minutes') as within_sla
                from orders
                where date_trunc('month', created_at) = date_trunc('month', now())
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
                    count(*) filter (where first_at::date < current_date) as repeat_base,
                    count(*) as total_customers
                from first_sale
                """
            )
        )
    ).one()
    # Point 3 audit fix: Repeat % had the two raw counts but never divided
    # them; Customer Frequency wasn't computed anywhere at all (purchases per
    # customer over the trailing 90 days, the window RFM/ABC analysis elsewhere
    # in this app already uses for "current" behaviour).
    frequency = (
        await db.execute(
            text(
                """
                select coalesce(avg(cnt), 0) as avg_purchase_frequency_90d
                from (
                    select customer_id, count(*) as cnt from sales
                    where status = 'completed' and customer_id is not null
                      and billed_at >= current_date - interval '90 days'
                    group by customer_id
                ) per_customer
                """
            )
        )
    ).scalar_one()

    finance = (
        await db.execute(
            text(
                """
                select
                    coalesce((select sum(cash_deposited) from bank_deposits where business_date = current_date), 0) as cash_deposited_today,
                    coalesce((select sum(amount_due) from payables where status != 'paid'), 0) as payables_outstanding,
                    coalesce((select sum(amount_due) from customer_receivables where status != 'paid'), 0) as receivables_outstanding,
                    coalesce((select sum(amount) from expenses where status = 'approved'
                        and date_trunc('month', created_at) = date_trunc('month', now())), 0) as expenses_mtd,
                    coalesce((select sum(cgst_amount + sgst_amount) from sale_items si join sales s on s.id = si.sale_id
                        where s.status = 'completed' and date_trunc('month', s.billed_at) = date_trunc('month', now())), 0) as gst_collected_mtd
                """
            )
        )
    ).one()
    # Point 3 audit fix: Cohort Performance had a real table/endpoint but no
    # write path anywhere (see crm_advanced.py's _compute_rfm_cohorts, added
    # this pass) — surfacing a segment-count summary here now that it's a
    # real, populated table instead of structurally-present-but-empty.
    from app.models.models_phase4 import RfmCohortSnapshot
    from sqlalchemy import func as sa_func, select as sa_select

    cohort_rows = (
        await db.execute(sa_select(RfmCohortSnapshot.segment, sa_func.count()).group_by(RfmCohortSnapshot.segment))
    ).all()
    if not cohort_rows:
        from app.api.v1.crm_advanced import _compute_rfm_cohorts

        await _compute_rfm_cohorts(db)
        cohort_rows = (
            await db.execute(sa_select(RfmCohortSnapshot.segment, sa_func.count()).group_by(RfmCohortSnapshot.segment))
        ).all()

    online = (
        await db.execute(
            text(
                """
                select
                    count(*) as orders_mtd,
                    count(*) filter (where status = 'cancelled') as cancellations_mtd,
                    count(*) filter (where status = 'delivered') as delivered_mtd,
                    coalesce(avg(extract(epoch from (delivered_at - created_at)) / 60) filter (where delivered_at is not null), null) as avg_delivery_minutes,
                    coalesce(avg(extract(epoch from (packed_at - created_at)) / 60) filter (where packed_at is not null), null) as avg_pick_pack_minutes,
                    coalesce(avg(extract(epoch from (picking_started_at - created_at)) / 60) filter (where picking_started_at is not null), null) as avg_picking_minutes,
                    coalesce(avg(extract(epoch from (packed_at - picking_started_at)) / 60) filter (where packed_at is not null and picking_started_at is not null), null) as avg_packing_minutes,
                    coalesce(avg(extract(epoch from (dispatched_at - packed_at)) / 60) filter (where dispatched_at is not null and packed_at is not null), null) as avg_dispatch_wait_minutes
                from orders where date_trunc('month', created_at) = date_trunc('month', now())
                """
            )
        )
    ).one()
    # Point 3 audit fix: "Rider Performance" had zero aggregation anywhere —
    # rider_id sat on Order unused beyond the assignment itself.
    rider_performance = (
        await db.execute(
            text(
                """
                select u.full_name as rider,
                       count(*) filter (where o.status = 'delivered') as delivered_mtd,
                       coalesce(avg(extract(epoch from (o.delivered_at - o.created_at)) / 60)
                           filter (where o.delivered_at is not null), null) as avg_delivery_minutes
                from orders o
                join users u on u.id = o.rider_id
                where o.rider_id is not null and date_trunc('month', o.created_at) = date_trunc('month', now())
                group by u.full_name
                order by delivered_mtd desc
                limit 20
                """
            )
        )
    ).all()

    await _evaluate_alerts_safely(db)
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
            "bills_mtd": int(sales.bills_mtd),
            "units_today": float(units_today),
            "offline_today": float(sales.today),
            "online_today": float(online_today),
            "aov_today": round(float(sales.today) / sales.bills_today, 2) if sales.bills_today else None,
            "aov_mtd": round(float(sales.mtd) / sales.bills_mtd, 2) if sales.bills_mtd else None,
            "same_store_growth_value": same_store_growth,
            "same_store_growth_pct": round(same_store_growth_pct, 1) if same_store_growth_pct is not None else None,
        },
        "margin": {
            "gross_margin_value_mtd": gross_margin_value,
            "gross_margin_pct_mtd": round(gross_margin_pct, 1) if gross_margin_pct is not None else None,
            "net_contribution_mtd": net_contribution_mtd,
            "discount_leakage_mtd": float(margin.discounts_mtd),
            "category_margin": [
                {
                    "category": r.category,
                    "revenue_mtd": float(r.revenue),
                    "margin_value_mtd": float(r.revenue) - float(r.cogs),
                    "margin_pct_mtd": round((float(r.revenue) - float(r.cogs)) / float(r.revenue) * 100, 1) if r.revenue else None,
                }
                for r in category_margin
            ],
        },
        "stores": {
            "ranking": [
                {
                    "store_id": str(r.store_id),
                    "store": r.name,
                    "revenue_mtd": float(r.revenue),
                    "bills_mtd": int(r.bills),
                    "margin_value_mtd": float(r.revenue) - float(r.cogs),
                    "margin_pct_mtd": round((float(r.revenue) - float(r.cogs)) / float(r.revenue) * 100, 1) if r.revenue else None,
                    "sales_per_sqft_mtd": round(float(r.revenue) / float(r.area_sqft), 2) if r.area_sqft else None,
                    "target_revenue_monthly": float(r.target_revenue_monthly) if r.target_revenue_monthly else None,
                    "target_achievement_pct": round(float(r.revenue) / float(r.target_revenue_monthly) * 100, 1)
                    if r.target_revenue_monthly else None,
                    "footfall_mtd": int(r.footfall) if r.footfall is not None else None,
                    "conversion_pct": round(int(r.bills) / int(r.footfall) * 100, 1) if r.footfall else None,
                }
                for r in store_rank
            ],
            "top": store_rank[0].name if store_rank else None,
            "bottom": store_rank[-1].name if store_rank else None,
        },
        "inventory": {
            "value": float(inventory.inventory_value),
            "out_of_stock_lines": int(inventory.oos_count),
            "in_stock_lines": int(inventory.in_stock_count),
            "near_expiry_value_15d": float(near_expiry_value),
            "dead_stock_value": float(dead_excess.dead_stock_value),
            "dead_stock_lines": int(dead_excess.dead_stock_lines),
            "excess_stock_value": float(dead_excess.excess_stock_value),
            "excess_stock_lines": int(dead_excess.excess_stock_lines),
            "ageing": {
                "0_30_days": float(ageing.b0_30),
                "31_60_days": float(ageing.b31_60),
                "61_90_days": float(ageing.b61_90),
                "90_plus_days": float(ageing.b90_plus),
            },
        },
        "warehouse": {
            "pending_grns": int(warehouse.pending_grns),
            "pending_transfers": int(warehouse.pending_transfers),
            "pick_accuracy_pct": round(float(warehouse.pick_accuracy_pct), 1),
            "dispatch_sla_minutes_threshold": DISPATCH_SLA_MINUTES,
            "dispatch_sla_pct": round(int(dispatch_sla.within_sla) / int(dispatch_sla.with_timing) * 100, 1)
            if dispatch_sla.with_timing else None,
        },
        "customers": {
            "active_today": int(customers.active_today),
            "active_30d": int(customers.active_30d),
            "loyalty_points_issued_mtd": float(customers.loyalty_points_mtd),
            "new_customers_today": int(new_vs_repeat.new_today),
            "repeat_customer_base": int(new_vs_repeat.repeat_base),
            "repeat_pct": round(int(new_vs_repeat.repeat_base) / int(new_vs_repeat.total_customers) * 100, 1)
            if new_vs_repeat.total_customers else None,
            "avg_purchase_frequency_90d": round(float(frequency), 2),
            "cohort_segments": {row.segment: int(row.count) for row in cohort_rows},
        },
        "finance": {
            "cash_deposited_today": float(finance.cash_deposited_today),
            "payables_outstanding": float(finance.payables_outstanding),
            "receivables_outstanding": float(finance.receivables_outstanding),
            "expenses_mtd": float(finance.expenses_mtd),
            "gst_collected_mtd": float(finance.gst_collected_mtd),
        },
        "online": {
            "orders_mtd": int(online.orders_mtd),
            "cancellations_mtd": int(online.cancellations_mtd),
            "cancellation_rate_pct": round(online.cancellations_mtd / online.orders_mtd * 100, 1) if online.orders_mtd else None,
            "delivered_mtd": int(online.delivered_mtd),
            "fill_rate_pct": round(online.delivered_mtd / online.orders_mtd * 100, 1) if online.orders_mtd else None,
            "avg_delivery_minutes": round(float(online.avg_delivery_minutes), 1) if online.avg_delivery_minutes is not None else None,
            "avg_pick_pack_minutes": round(float(online.avg_pick_pack_minutes), 1) if online.avg_pick_pack_minutes is not None else None,
            "avg_picking_minutes": round(float(online.avg_picking_minutes), 1) if online.avg_picking_minutes is not None else None,
            "avg_packing_minutes": round(float(online.avg_packing_minutes), 1) if online.avg_packing_minutes is not None else None,
            "avg_dispatch_wait_minutes": round(float(online.avg_dispatch_wait_minutes), 1) if online.avg_dispatch_wait_minutes is not None else None,
            "rider_performance": [
                {
                    "rider": r.rider,
                    "delivered_mtd": int(r.delivered_mtd),
                    "avg_delivery_minutes": round(float(r.avg_delivery_minutes), 1) if r.avg_delivery_minutes is not None else None,
                }
                for r in rider_performance
            ],
        },
        "control_tower": {
            "active_ceo_alerts": int(control_tower.active_alerts),
            "open_fraud_alerts": int(control_tower.open_fraud_alerts),
            "pending_approvals": int(control_tower.pending_approvals),
            "negative_stock_lines": int(control_tower.negative_stock_lines),
        },
    }
