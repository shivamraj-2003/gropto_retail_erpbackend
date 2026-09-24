"""CRM & customer intelligence (Phase 3): customer 360, RFM segmentation, consent,
campaign audience — read from data already captured by Phase 1 sales + loyalty."""

import uuid

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models_phase3 import CustomerConsent


async def customer_360(db: AsyncSession, *, customer_id: uuid.UUID) -> dict:
    row = (
        await db.execute(
            text(
                """
                select
                    count(*) as total_orders,
                    coalesce(sum(grand_total), 0) as total_spend,
                    coalesce(avg(grand_total), 0) as aov,
                    max(billed_at) as last_purchase,
                    min(billed_at) as first_purchase
                from sales where customer_id = :cid and status = 'completed'
                """
            ),
            {"cid": str(customer_id)},
        )
    ).one()
    loyalty_balance = (
        await db.execute(text("select coalesce(sum(delta_points), 0) from loyalty_ledger where customer_id = :cid"), {"cid": str(customer_id)})
    ).scalar_one()
    return {
        "total_orders": int(row.total_orders),
        "total_spend": float(row.total_spend),
        "aov": float(row.aov),
        "first_purchase": row.first_purchase.isoformat() if row.first_purchase else None,
        "last_purchase": row.last_purchase.isoformat() if row.last_purchase else None,
        "loyalty_balance": float(loyalty_balance),
    }


async def rfm_segments(db: AsyncSession) -> list[dict]:
    """Simple RFM banding computed directly from sales — no separate materialized
    table needed at this data volume."""
    rows = (
        await db.execute(
            text(
                """
                with agg as (
                    select customer_id,
                        extract(day from now() - max(billed_at)) as recency_days,
                        count(*) as frequency,
                        sum(grand_total) as monetary
                    from sales
                    where status = 'completed' and customer_id is not null
                    group by customer_id
                )
                select customer_id, recency_days, frequency, monetary,
                    case
                        when recency_days <= 30 and frequency >= 5 then 'champion'
                        when recency_days <= 30 then 'recent'
                        when recency_days > 90 and frequency >= 3 then 'at_risk'
                        when recency_days > 180 then 'churned'
                        else 'regular'
                    end as segment
                from agg
                """
            )
        )
    ).all()
    return [
        {
            "customer_id": str(r.customer_id),
            "recency_days": int(r.recency_days),
            "frequency": int(r.frequency),
            "monetary": float(r.monetary),
            "segment": r.segment,
        }
        for r in rows
    ]


async def update_consent(db: AsyncSession, *, customer_id: uuid.UUID, whatsapp: bool | None, sms: bool | None, email: bool | None) -> None:
    values = {"customer_id": customer_id}
    updates = {}
    if whatsapp is not None:
        values["whatsapp_opt_in"] = whatsapp
        updates["whatsapp_opt_in"] = whatsapp
    if sms is not None:
        values["sms_opt_in"] = sms
        updates["sms_opt_in"] = sms
    if email is not None:
        values["email_opt_in"] = email
        updates["email_opt_in"] = email

    stmt = pg_insert(CustomerConsent).values(**values)
    if updates:
        stmt = stmt.on_conflict_do_update(index_elements=[CustomerConsent.customer_id], set_=updates)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[CustomerConsent.customer_id])
    await db.execute(stmt)
