"""Customer Lifetime Value computation engine.
Historic CLV = total_spend.
Predicted CLV = AOV × purchase_frequency_per_month × predicted_remaining_lifespan.
Segments: high_value (top 20%), medium_value (20-60%), low_value (60-90%), new (<2 orders)."""

import uuid
from sqlalchemy import text, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.models_phase4 import ClvSnapshot

async def compute_clv_snapshots(db: AsyncSession) -> list[ClvSnapshot]:
    # Query: per customer - total_spend, order_count, first_purchase, last_purchase, avg_order_value
    # Calculate purchase_frequency = order_count / max(months_active, 1)
    # Calculate customer_lifespan_months from first_purchase to now
    # Predicted CLV = aov * frequency_per_month * 12 (project 1 year ahead)
    # Segment by percentile: ntile(5) on historic_clv
    # Delete old snapshots, insert new ones
    # Return list of snapshots
    
    await db.execute(delete(ClvSnapshot))
    
    rows = (await db.execute(text("""
        with per_customer as (
            select customer_id,
                sum(grand_total) as total_spend,
                count(*) as order_count,
                avg(grand_total) as aov,
                min(billed_at) as first_purchase,
                max(billed_at) as last_purchase,
                greatest(extract(month from age(now(), min(billed_at))), 1) as months_active
            from sales
            where status = 'completed' and customer_id is not null
            group by customer_id
        ),
        scored as (
            select *,
                order_count::float / months_active as frequency_per_month,
                ntile(10) over (order by total_spend) as spend_decile
            from per_customer
        )
        select * from scored
    """))).all()
    
    snapshots = []
    for r in rows:
        if r.order_count <= 1:
            segment = "new"
        elif r.spend_decile >= 9:
            segment = "high_value"
        elif r.spend_decile >= 5:
            segment = "medium_value"
        else:
            segment = "low_value"
        
        predicted = float(r.aov) * float(r.frequency_per_month) * 12
        snapshot = ClvSnapshot(
            customer_id=r.customer_id,
            historic_clv=float(r.total_spend),
            predicted_clv=round(predicted, 2),
            avg_order_value=float(r.aov),
            purchase_frequency=round(float(r.frequency_per_month), 4),
            customer_lifespan_months=int(r.months_active),
            segment=segment,
        )
        db.add(snapshot)
        snapshots.append(snapshot)
    await db.commit()
    return snapshots

async def get_clv_summary(db: AsyncSession) -> dict:
    """Aggregated CLV dashboard data."""
    from sqlalchemy import select, func
    stmt = select(
        func.count().label("total_customers"),
        func.avg(ClvSnapshot.historic_clv).label("avg_historic_clv"),
        func.avg(ClvSnapshot.predicted_clv).label("avg_predicted_clv"),
        func.sum(ClvSnapshot.historic_clv).label("total_historic_clv"),
    ).select_from(ClvSnapshot)
    row = (await db.execute(stmt)).one()
    
    # Segment breakdown
    seg_stmt = select(
        ClvSnapshot.segment,
        func.count().label("count"),
        func.avg(ClvSnapshot.historic_clv).label("avg_clv"),
        func.sum(ClvSnapshot.historic_clv).label("total_clv"),
    ).group_by(ClvSnapshot.segment)
    seg_rows = (await db.execute(seg_stmt)).all()
    
    # Top 10 customers by CLV
    top_stmt = select(ClvSnapshot).order_by(ClvSnapshot.historic_clv.desc()).limit(10)
    top_rows = (await db.execute(top_stmt)).scalars().all()
    
    return {
        "total_customers": int(row.total_customers) if row.total_customers else 0,
        "avg_historic_clv": round(float(row.avg_historic_clv or 0), 2),
        "avg_predicted_clv": round(float(row.avg_predicted_clv or 0), 2),
        "total_historic_clv": round(float(row.total_historic_clv or 0), 2),
        "segments": {r.segment: {"count": int(r.count), "avg_clv": round(float(r.avg_clv), 2), "total_clv": round(float(r.total_clv), 2)} for r in seg_rows},
        "top_customers": [{"customer_id": str(r.customer_id), "historic_clv": float(r.historic_clv), "predicted_clv": float(r.predicted_clv), "segment": r.segment} for r in top_rows],
    }

async def get_retention_metrics(db: AsyncSession) -> dict:
    """Monthly cohort retention analysis."""
    rows = (await db.execute(text("""
        with cohorts as (
            select customer_id,
                date_trunc('month', min(billed_at)) as cohort_month
            from sales where status='completed' and customer_id is not null
            group by customer_id
        ),
        activity as (
            select distinct s.customer_id,
                c.cohort_month,
                date_trunc('month', s.billed_at) as activity_month
            from sales s join cohorts c on s.customer_id = c.customer_id
            where s.status='completed'
        )
        select cohort_month,
            extract(month from age(activity_month, cohort_month))::int as month_number,
            count(distinct customer_id) as active_customers
        from activity
        group by cohort_month, month_number
        order by cohort_month, month_number
    """))).all()
    
    cohorts = {}
    for r in rows:
        key = r.cohort_month.strftime("%Y-%m") if r.cohort_month else "unknown"
        if key not in cohorts:
            cohorts[key] = {}
        cohorts[key][f"M{int(r.month_number)}"] = int(r.active_customers)
    
    # Overall retention rate (customers who purchased more than once / total)
    retention_row = (await db.execute(text("""
        select count(*) as total,
            count(*) filter (where cnt > 1) as retained
        from (select customer_id, count(*) as cnt from sales
              where status='completed' and customer_id is not null
              group by customer_id) t
    """))).one()
    
    return {
        "cohort_retention": cohorts,
        "overall_retention_rate": round(int(retention_row.retained) / max(int(retention_row.total), 1) * 100, 2),
        "total_customers": int(retention_row.total),
        "retained_customers": int(retention_row.retained),
    }
