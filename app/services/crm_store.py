"""Customer analytics for ONE store, worked out live from that store's own sales.

The company-wide dashboards read saved snapshots (taken across every store). When a store is chosen at the top of the
app these functions answer the same questions — who are the best and the at-risk customers, how many come back —
using only the bills of that store, in the same response shapes so the screen needs no changes.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.crm import RFM_SEGMENT_SQL

_ALL = "where status = 'completed' and customer_id is not null"
_ONE = "where status = 'completed' and customer_id is not null and store_id = :sid"


def _store_rfm_sql() -> str:
    sql = RFM_SEGMENT_SQL.replace(_ALL, _ONE)
    assert sql != RFM_SEGMENT_SQL, "RFM query changed — update crm_store"
    return sql


async def rfm_segments(db: AsyncSession, store_id: uuid.UUID) -> list[dict]:
    rows = (await db.execute(text(_store_rfm_sql()), {"sid": str(store_id)})).all()
    return [
        {"customer_id": str(r.customer_id), "recency_days": int(r.recency_days), "frequency": int(r.frequency), "monetary": float(r.monetary), "segment": r.segment}
        for r in rows
    ]


async def clv_summary(db: AsyncSession, store_id: uuid.UUID) -> dict:
    rows = (
        await db.execute(
            text(
                """
                with per_customer as (
                    select customer_id, sum(grand_total) as total_spend, count(*) as order_count, avg(grand_total) as aov,
                           greatest(extract(month from age(now(), min(billed_at))), 1) as months_active
                    from sales where status = 'completed' and customer_id is not null and store_id = :sid
                    group by customer_id
                )
                select *, order_count::float / months_active as freq, ntile(10) over (order by total_spend) as decile
                from per_customer
                """
            ),
            {"sid": str(store_id)},
        )
    ).all()
    segments: dict[str, dict] = {}
    customers = []
    for r in rows:
        seg = "new" if r.order_count <= 1 else "high_value" if r.decile >= 9 else "medium_value" if r.decile >= 5 else "low_value"
        spend = float(r.total_spend)
        predicted = round(float(r.aov) * float(r.freq) * 12, 2)
        s = segments.setdefault(seg, {"count": 0, "total_clv": 0.0})
        s["count"] += 1
        s["total_clv"] += spend
        customers.append({"customer_id": str(r.customer_id), "historic_clv": spend, "predicted_clv": predicted, "segment": seg})
    n = len(customers)
    total = sum(c["historic_clv"] for c in customers)
    return {
        "total_customers": n,
        "avg_historic_clv": round(total / n, 2) if n else 0,
        "avg_predicted_clv": round(sum(c["predicted_clv"] for c in customers) / n, 2) if n else 0,
        "total_historic_clv": round(total, 2),
        "segments": {k: {"count": v["count"], "avg_clv": round(v["total_clv"] / v["count"], 2), "total_clv": round(v["total_clv"], 2)} for k, v in segments.items()},
        "top_customers": sorted(customers, key=lambda c: c["historic_clv"], reverse=True)[:10],
    }


async def retention(db: AsyncSession, store_id: uuid.UUID) -> dict:
    sid = {"sid": str(store_id)}
    rows = (
        await db.execute(
            text(
                """
                with cohorts as (
                    select customer_id, date_trunc('month', min(billed_at)) as cohort_month
                    from sales where status='completed' and customer_id is not null and store_id = :sid
                    group by customer_id
                ),
                activity as (
                    select distinct s.customer_id, c.cohort_month, date_trunc('month', s.billed_at) as activity_month
                    from sales s join cohorts c on s.customer_id = c.customer_id
                    where s.status='completed' and s.store_id = :sid
                )
                select cohort_month, extract(month from age(activity_month, cohort_month))::int as month_number,
                       count(distinct customer_id) as active_customers
                from activity group by cohort_month, month_number order by cohort_month, month_number
                """
            ),
            sid,
        )
    ).all()
    cohorts: dict[str, dict] = {}
    for r in rows:
        key = r.cohort_month.strftime("%Y-%m") if r.cohort_month else "unknown"
        cohorts.setdefault(key, {})[f"M{int(r.month_number)}"] = int(r.active_customers)
    tot = (
        await db.execute(
            text(
                """
                select count(*) as total, count(*) filter (where cnt > 1) as retained
                from (select customer_id, count(*) as cnt from sales
                      where status='completed' and customer_id is not null and store_id = :sid group by customer_id) t
                """
            ),
            sid,
        )
    ).one()
    return {
        "cohort_retention": cohorts,
        "overall_retention_rate": round(int(tot.retained) / max(int(tot.total), 1) * 100, 2),
        "total_customers": int(tot.total),
        "retained_customers": int(tot.retained),
    }


async def churn(db: AsyncSession, store_id: uuid.UUID) -> dict:
    """Customers of this store who used to come regularly and have gone quiet, or who stopped coming altogether."""
    rows = [r for r in await rfm_segments(db, store_id) if r["segment"] in ("at_risk", "churned")]
    rows.sort(key=lambda r: r["monetary"], reverse=True)

    def recency_score(days: int) -> int:
        return 5 if days <= 30 else 4 if days <= 60 else 3 if days <= 90 else 2 if days <= 180 else 1

    return {
        "total_at_risk": len(rows),
        "customers": [
            {
                "customer_id": r["customer_id"], "segment": r["segment"], "recency_score": recency_score(r["recency_days"]),
                "frequency_score": min(5, r["frequency"]), "monetary_score": 5 if r["monetary"] >= 5000 else 4 if r["monetary"] >= 2000 else 3 if r["monetary"] >= 1000 else 2 if r["monetary"] >= 300 else 1,
            }
            for r in rows[:50]
        ],
    }
