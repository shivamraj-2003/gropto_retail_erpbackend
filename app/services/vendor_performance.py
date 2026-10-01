"""Vendor performance snapshot computation (Point 6 audit fix). This table
previously had real columns and a docstring claiming a nightly refresh job,
but nothing anywhere ever wrote a row to it — confirmed via repo-wide search
for its constructor. Computed here from real PO/GRN data and run daily by
the scheduler (see services/scheduler.py)."""

from datetime import date

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models_phase2 import VendorPerformanceSnapshot

LOOKBACK_DAYS = 90


async def compute_vendor_performance(db: AsyncSession) -> int:
    rows = (
        await db.execute(
            text(
                """
                with current_period as (
                    select po.vendor_id,
                           sum(poi.quantity) as ordered_qty,
                           sum(least(poi.received_qty, poi.quantity)) as received_qty,
                           sum(poi.quantity * poi.unit_cost) / nullif(sum(poi.quantity), 0) as avg_unit_cost
                    from purchase_orders po
                    join purchase_order_items poi on poi.purchase_order_id = po.id
                    where po.created_at >= now() - make_interval(days => :lookback)
                    group by po.vendor_id
                ),
                prior_period as (
                    select po.vendor_id,
                           sum(poi.quantity * poi.unit_cost) / nullif(sum(poi.quantity), 0) as avg_unit_cost
                    from purchase_orders po
                    join purchase_order_items poi on poi.purchase_order_id = po.id
                    where po.created_at >= now() - make_interval(days => :lookback * 2)
                      and po.created_at < now() - make_interval(days => :lookback)
                    group by po.vendor_id
                ),
                grn_stats as (
                    select po.vendor_id,
                           sum(gi.received_qty) as grn_qty,
                           sum(gi.received_qty) filter (where gi.qc_status in ('rejected', 'damaged')) as rejected_qty,
                           avg(extract(epoch from (g.created_at - po.created_at)) / 86400.0) as avg_lead_time_days
                    from grn g
                    join purchase_orders po on po.id = g.purchase_order_id
                    join grn_items gi on gi.grn_id = g.id
                    where g.created_at >= now() - make_interval(days => :lookback)
                    group by po.vendor_id
                )
                select cp.vendor_id,
                       coalesce(cp.received_qty / nullif(cp.ordered_qty, 0) * 100, 0) as fill_rate,
                       coalesce(gs.avg_lead_time_days, 7) as avg_lead_time_days,
                       coalesce(gs.rejected_qty / nullif(gs.grn_qty, 0) * 100, 0) as rejection_rate,
                       case when pp.avg_unit_cost is not null and pp.avg_unit_cost != 0
                            then (cp.avg_unit_cost - pp.avg_unit_cost) / pp.avg_unit_cost * 100
                            else 0 end as price_variance_pct
                from current_period cp
                left join prior_period pp on pp.vendor_id = cp.vendor_id
                left join grn_stats gs on gs.vendor_id = cp.vendor_id
                """
            ),
            {"lookback": LOOKBACK_DAYS},
        )
    ).all()

    today = date.today()
    count = 0
    for r in rows:
        fill_rate = float(r.fill_rate)
        rejection_rate = float(r.rejection_rate)
        avg_lead_time_days = float(r.avg_lead_time_days)
        price_variance_pct = float(r.price_variance_pct)
        # Simple weighted service score: fill rate and low rejection matter
        # most, lead time and price stability contribute less — configurable
        # by changing these weights, not hardcoded as an opaque black box.
        lead_time_score = max(0.0, 100.0 - avg_lead_time_days * 5.0)
        price_stability_score = max(0.0, 100.0 - abs(price_variance_pct) * 2.0)
        service_score = round(
            fill_rate * 0.4 + (100.0 - rejection_rate) * 0.3 + lead_time_score * 0.2 + price_stability_score * 0.1, 2
        )

        stmt = (
            pg_insert(VendorPerformanceSnapshot)
            .values(
                vendor_id=r.vendor_id,
                snapshot_date=today,
                fill_rate=round(fill_rate, 2),
                avg_lead_time_days=round(avg_lead_time_days, 2),
                rejection_rate=round(rejection_rate, 2),
                price_variance_pct=round(price_variance_pct, 2),
                service_score=service_score,
            )
            .on_conflict_do_update(
                index_elements=["vendor_id", "snapshot_date"],
                set_={
                    "fill_rate": round(fill_rate, 2),
                    "avg_lead_time_days": round(avg_lead_time_days, 2),
                    "rejection_rate": round(rejection_rate, 2),
                    "price_variance_pct": round(price_variance_pct, 2),
                    "service_score": service_score,
                },
            )
        )
        await db.execute(stmt)
        count += 1
    return count
