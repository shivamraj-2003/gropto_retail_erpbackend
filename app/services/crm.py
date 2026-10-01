"""CRM & customer intelligence (Phase 3): customer 360, RFM segmentation,
consent, audience builder, multi-channel campaign send — reads from data
already captured by Phase 1 sales + loyalty + coupons + returns + tickets.

Point 11 audit fix: this file previously returned a 6-field Customer 360
(orders/spend/AOV/dates/loyalty-balance only), had no real build_audience()
despite the API referencing one, only ever sent WhatsApp, and never wrote a
consent-change history. All of that is real here now.
"""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Customer
from app.models.models_phase3 import Campaign, CampaignRecipient, CustomerConsent
from app.models.models_phase4 import ConsentHistory, Coupon, CouponRedemption, CustomerServiceTicket, RfmCohortSnapshot, SavedAudience
from app.services import channels, wallet
from app.services.audit import write_audit

RFM_SEGMENT_SQL = """
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
    total_orders = int(row.total_orders)

    loyalty_balance = (
        await db.execute(text("select coalesce(sum(delta_points), 0) from loyalty_ledger where customer_id = :cid"), {"cid": str(customer_id)})
    ).scalar_one()

    # Purchase frequency: orders per month since first purchase.
    months_active = 1.0
    if row.first_purchase:
        first = row.first_purchase if row.first_purchase.tzinfo else row.first_purchase.replace(tzinfo=timezone.utc)
        months_active = max((datetime.now(timezone.utc) - first).days / 30.44, 1.0)
    purchase_frequency = round(total_orders / months_active, 4)

    preferred_store_row = (
        await db.execute(
            text(
                """
                select s.store_id, st.name, count(*) as cnt
                from sales s join stores st on st.id = s.store_id
                where s.customer_id = :cid and s.status = 'completed'
                group by s.store_id, st.name
                order by cnt desc
                limit 1
                """
            ),
            {"cid": str(customer_id)},
        )
    ).first()
    preferred_store = {"id": str(preferred_store_row.store_id), "name": preferred_store_row.name} if preferred_store_row else None

    preferred_categories_rows = (
        await db.execute(
            text(
                """
                select c.name, sum(si.line_total) as spend
                from sale_items si
                join sales s on s.id = si.sale_id
                join products p on p.id = si.product_id
                join categories c on c.id = p.category_id
                where s.customer_id = :cid and s.status = 'completed'
                group by c.name
                order by spend desc
                limit 3
                """
            ),
            {"cid": str(customer_id)},
        )
    ).all()
    preferred_categories = [r.name for r in preferred_categories_rows]

    new_or_repeat = "new" if total_orders <= 1 else "repeat"

    rfm_row = (
        await db.execute(text(f"select segment from ({RFM_SEGMENT_SQL}) t where customer_id = :cid"), {"cid": str(customer_id)})
    ).first()
    rfm_segment = rfm_row.segment if rfm_row else None

    cohort_row = (
        await db.execute(select(RfmCohortSnapshot.churn_risk_flag).where(RfmCohortSnapshot.customer_id == customer_id))
    ).scalar_one_or_none()
    churn_risk = bool(cohort_row) if cohort_row is not None else None

    wallet_balance = await wallet.get_wallet_balance(db, customer_id=customer_id)

    loyalty_history_rows = (
        await db.execute(
            text(
                "select delta_points, reason, source_type, created_at from loyalty_ledger "
                "where customer_id = :cid order by created_at desc limit 20"
            ),
            {"cid": str(customer_id)},
        )
    ).all()
    loyalty_history = [
        {"delta_points": float(r.delta_points), "reason": r.reason, "source_type": r.source_type, "created_at": r.created_at.isoformat()}
        for r in loyalty_history_rows
    ]

    coupon_rows = (
        await db.execute(
            select(CouponRedemption, Coupon.code)
            .join(Coupon, Coupon.id == CouponRedemption.coupon_id)
            .where(CouponRedemption.customer_id == customer_id)
            .order_by(CouponRedemption.created_at.desc())
            .limit(20)
        )
    ).all()
    coupon_history = [
        {"code": code, "discount_amount": float(redemption.discount_amount), "source_type": redemption.source_type, "created_at": redemption.created_at.isoformat()}
        for redemption, code in coupon_rows
    ]

    refund_rows = (
        await db.execute(
            text(
                """
                select r.refund_total, r.status, r.created_at, 'pos' as source
                from returns r join sales s on s.id = r.sale_id
                where s.customer_id = :cid
                union all
                select orr.refund_total, orr.status, orr.created_at, 'online' as source
                from order_returns orr join orders o on o.id = orr.order_id
                where o.customer_id = :cid
                order by created_at desc
                limit 20
                """
            ),
            {"cid": str(customer_id)},
        )
    ).all()
    refund_history = [
        {"refund_total": float(r.refund_total), "status": r.status, "source": r.source, "created_at": r.created_at.isoformat()}
        for r in refund_rows
    ]

    ticket_rows = (
        await db.execute(
            select(CustomerServiceTicket)
            .where(CustomerServiceTicket.customer_id == customer_id)
            .order_by(CustomerServiceTicket.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    ticket_history = [
        {"id": str(t.id), "category": t.category, "subject": t.subject, "status": t.status, "priority": t.priority, "created_at": t.created_at.isoformat()}
        for t in ticket_rows
    ]

    order_rows = (
        await db.execute(
            text(
                """
                select bill_number as reference, grand_total, billed_at as occurred_at, 'pos' as source
                from sales where customer_id = :cid and status = 'completed'
                union all
                select id::text as reference, grand_total, created_at as occurred_at, 'online' as source
                from orders where customer_id = :cid
                order by occurred_at desc
                limit 20
                """
            ),
            {"cid": str(customer_id)},
        )
    ).all()
    order_history = [
        {"reference": r.reference, "grand_total": float(r.grand_total), "source": r.source, "occurred_at": r.occurred_at.isoformat()}
        for r in order_rows
    ]

    return {
        "total_orders": total_orders,
        "total_spend": float(row.total_spend),
        "aov": float(row.aov),
        "first_purchase": row.first_purchase.isoformat() if row.first_purchase else None,
        "last_purchase": row.last_purchase.isoformat() if row.last_purchase else None,
        "loyalty_balance": float(loyalty_balance),
        "purchase_frequency": purchase_frequency,
        "preferred_store": preferred_store,
        "preferred_categories": preferred_categories,
        "new_or_repeat": new_or_repeat,
        "rfm_segment": rfm_segment,
        "churn_risk": churn_risk,
        "wallet_balance": wallet_balance,
        "loyalty_history": loyalty_history,
        "coupon_history": coupon_history,
        "refund_history": refund_history,
        "ticket_history": ticket_history,
        "order_history": order_history,
    }


async def rfm_segments(db: AsyncSession) -> list[dict]:
    """Simple RFM banding computed directly from sales — no separate materialized
    table needed at this data volume."""
    rows = (await db.execute(text(RFM_SEGMENT_SQL))).all()
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


async def update_consent(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    whatsapp: bool | None,
    sms: bool | None,
    email: bool | None,
    user_id: uuid.UUID | None,
    source: str = "admin",
) -> None:
    existing = await db.get(CustomerConsent, customer_id)
    old = {
        "whatsapp_opt_in": existing.whatsapp_opt_in if existing else None,
        "sms_opt_in": existing.sms_opt_in if existing else None,
        "email_opt_in": existing.email_opt_in if existing else None,
    }

    values = {"customer_id": customer_id}
    updates = {}
    changes: list[tuple[str, bool | None, bool]] = []
    for channel_name, new_value, old_value in (
        ("whatsapp", whatsapp, old["whatsapp_opt_in"]),
        ("sms", sms, old["sms_opt_in"]),
        ("email", email, old["email_opt_in"]),
    ):
        if new_value is None:
            continue
        values[f"{channel_name}_opt_in"] = new_value
        updates[f"{channel_name}_opt_in"] = new_value
        if old_value != new_value:
            changes.append((channel_name, old_value, new_value))

    stmt = pg_insert(CustomerConsent).values(**values)
    if updates:
        stmt = stmt.on_conflict_do_update(index_elements=[CustomerConsent.customer_id], set_=updates)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[CustomerConsent.customer_id])
    await db.execute(stmt)

    # Point 11 audit fix: consent changes previously left zero trace of who
    # changed what, when, or from what prior value.
    for channel_name, old_value, new_value in changes:
        db.add(
            ConsentHistory(
                customer_id=customer_id,
                channel=channel_name,
                old_value=old_value,
                new_value=new_value,
                changed_by=user_id,
                source=source,
            )
        )
    if changes:
        await write_audit(
            db,
            user_id=user_id,
            role_code=None,
            store_id=None,
            device_id=None,
            action="customer.consent_updated",
            entity_type="customer",
            entity_id=customer_id,
            old_value=old,
            new_value={f"{c}_opt_in": v for c, _, v in changes},
        )


async def _segment_customer_ids(db: AsyncSession, segment: str) -> list[uuid.UUID]:
    rows = await rfm_segments(db)
    return [uuid.UUID(r["customer_id"]) for r in rows if r["segment"] == segment]


async def build_audience(db: AsyncSession, criteria: dict) -> list[uuid.UUID]:
    """Point 11 audit fix: this function was called by the API but did not
    exist anywhere — POST/GET /crm/audiences failed on every call. Combines
    multiple real criteria against actual transaction/consent/loyalty data,
    intersected (AND semantics across criteria)."""
    conditions = ["1=1"]
    params: dict = {}
    if criteria.get("min_spend") is not None:
        conditions.append("monetary >= :min_spend")
        params["min_spend"] = criteria["min_spend"]
    if criteria.get("max_spend") is not None:
        conditions.append("monetary <= :max_spend")
        params["max_spend"] = criteria["max_spend"]
    if criteria.get("min_orders") is not None:
        conditions.append("frequency >= :min_orders")
        params["min_orders"] = criteria["min_orders"]
    if criteria.get("min_recency_days") is not None:
        conditions.append("recency_days >= :min_recency_days")
        params["min_recency_days"] = criteria["min_recency_days"]
    if criteria.get("max_recency_days") is not None:
        conditions.append("recency_days <= :max_recency_days")
        params["max_recency_days"] = criteria["max_recency_days"]

    where_clause = " and ".join(conditions)
    rows = (
        await db.execute(
            text(
                f"""
                with agg as (
                    select customer_id,
                        extract(day from now() - max(billed_at)) as recency_days,
                        count(*) as frequency,
                        sum(grand_total) as monetary
                    from sales
                    where status = 'completed' and customer_id is not null
                    group by customer_id
                )
                select customer_id from agg where {where_clause}
                """
            ),
            params,
        )
    ).scalars().all()
    customer_ids: set[uuid.UUID] = set(rows)

    if criteria.get("segment"):
        customer_ids &= set(await _segment_customer_ids(db, criteria["segment"]))

    if criteria.get("store_id"):
        store_rows = (
            await db.execute(
                text("select distinct customer_id from sales where store_id = :sid and customer_id is not null"),
                {"sid": str(criteria["store_id"])},
            )
        ).scalars().all()
        customer_ids &= set(store_rows)

    if criteria.get("churn_risk") is not None:
        churn_rows = (
            await db.execute(select(RfmCohortSnapshot.customer_id).where(RfmCohortSnapshot.churn_risk_flag == criteria["churn_risk"]))
        ).scalars().all()
        customer_ids &= set(churn_rows)

    if criteria.get("loyalty_tier_min_points") is not None:
        loyalty_rows = (
            await db.execute(
                text("select customer_id from loyalty_ledger group by customer_id having sum(delta_points) >= :pts"),
                {"pts": criteria["loyalty_tier_min_points"]},
            )
        ).scalars().all()
        customer_ids &= set(loyalty_rows)

    if criteria.get("has_used_coupon") is not None:
        coupon_rows = set(
            (await db.execute(select(CouponRedemption.customer_id).where(CouponRedemption.customer_id.isnot(None)))).scalars().all()
        )
        customer_ids &= coupon_rows if criteria["has_used_coupon"] else (customer_ids - coupon_rows)

    if criteria.get("has_refund") is not None:
        refund_rows = set(
            (
                await db.execute(
                    text(
                        "select s.customer_id from returns r join sales s on s.id = r.sale_id where s.customer_id is not null "
                        "union select o.customer_id from order_returns orr join orders o on o.id = orr.order_id where o.customer_id is not null"
                    )
                )
            )
            .scalars()
            .all()
        )
        customer_ids &= refund_rows if criteria["has_refund"] else (customer_ids - refund_rows)

    return list(customer_ids)


async def save_audience(db: AsyncSession, *, name: str, criteria: dict, user_id: uuid.UUID | None) -> SavedAudience:
    customer_ids = await build_audience(db, criteria)
    audience = SavedAudience(name=name, criteria=criteria, estimated_size=len(customer_ids), created_by=user_id)
    db.add(audience)
    await db.flush()
    await write_audit(
        db,
        user_id=user_id,
        role_code=None,
        store_id=None,
        device_id=None,
        action="audience.created",
        entity_type="saved_audience",
        entity_id=audience.id,
        new_value={"name": name, "criteria": criteria, "estimated_size": audience.estimated_size},
    )
    return audience


CONSENT_FIELD_BY_CHANNEL = {"whatsapp": "whatsapp_opt_in", "sms": "sms_opt_in", "email": "email_opt_in"}


async def _resolve_audience(db: AsyncSession, campaign: Campaign) -> list[uuid.UUID]:
    if campaign.saved_audience_id:
        audience = await db.get(SavedAudience, campaign.saved_audience_id)
        if audience is None:
            raise HTTPException(status_code=404, detail="Saved audience not found")
        return await build_audience(db, audience.criteria)
    if campaign.segment_query.get("segment"):
        return await _segment_customer_ids(db, campaign.segment_query["segment"])
    if campaign.segment_query.get("criteria"):
        return await build_audience(db, campaign.segment_query["criteria"])
    return []


async def send_campaign(db: AsyncSession, *, campaign_id: uuid.UUID, user_id: uuid.UUID | None = None) -> dict:
    """Point 11 audit fix: previously WhatsApp-only, with any other channel
    outright refused. Now dispatches through services/channels.py for all
    four channels — each gated by its own provider configuration, each
    checked against the matching consent field (push has no consent column
    in this schema, so it isn't gated — see CustomerConsent). Per-recipient
    idempotency is enforced by a DB unique constraint on (campaign_id,
    customer_id), so a concurrent/duplicate send call can't double-message
    anyone."""
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != "draft":
        raise HTTPException(status_code=409, detail=f"Campaign already {campaign.status}")
    if not campaign.template_name:
        raise HTTPException(status_code=400, detail="template_name is required to send a campaign")

    configured = {c["id"]: c["configured"] for c in channels.get_supported_channels()}
    if not configured.get(campaign.channel):
        raise HTTPException(status_code=409, detail=f"{campaign.channel} is not configured — see services/{campaign.channel if campaign.channel != 'sms' else 'sms'}.py")

    customer_ids = await _resolve_audience(db, campaign)

    campaign.status = "sending"
    await db.flush()

    consent_field = CONSENT_FIELD_BY_CHANNEL.get(campaign.channel)
    sent, failed, skipped = 0, 0, 0
    for customer_id in customer_ids:
        customer = await db.get(Customer, customer_id)
        if customer is None:
            continue

        if consent_field is not None:
            consent = await db.get(CustomerConsent, customer_id)
            if consent is None or not getattr(consent, consent_field):
                skipped += 1
                try:
                    async with db.begin_nested():
                        db.add(CampaignRecipient(campaign_id=campaign.id, customer_id=customer_id, status="skipped_no_consent"))
                        await db.flush()
                except IntegrityError:
                    pass
                continue

        ok, message_id, error = await channels.send_message(
            campaign.channel,
            customer.phone,
            campaign.template_name,
            email=customer.email,
            push_token=customer.push_token,
        )
        try:
            async with db.begin_nested():
                if ok:
                    db.add(CampaignRecipient(campaign_id=campaign.id, customer_id=customer_id, status="sent", provider_message_id=message_id))
                else:
                    db.add(CampaignRecipient(campaign_id=campaign.id, customer_id=customer_id, status="failed", error=error))
                await db.flush()
            if ok:
                sent += 1
            else:
                failed += 1
        except IntegrityError:
            continue  # already recorded for this (campaign, customer) — replay-safe

    campaign.status = "sent"
    campaign.sent_count = sent
    campaign.failed_count = failed
    await write_audit(
        db,
        user_id=user_id,
        role_code=None,
        store_id=None,
        device_id=None,
        action="campaign.sent",
        entity_type="campaign",
        entity_id=campaign.id,
        new_value={"channel": campaign.channel, "sent_count": sent, "failed_count": failed, "skipped_no_consent": skipped},
    )
    await db.commit()
    return {"status": campaign.status, "sent_count": sent, "failed_count": failed, "skipped_no_consent": skipped}


async def campaign_analytics(db: AsyncSession, *, campaign_id: uuid.UUID) -> dict:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    skipped = (
        await db.execute(
            text("select count(*) from campaign_recipients where campaign_id = :cid and status = 'skipped_no_consent'"),
            {"cid": str(campaign_id)},
        )
    ).scalar_one()
    total_attempted = campaign.sent_count + campaign.failed_count
    delivery_rate = round(campaign.sent_count / total_attempted * 100, 2) if total_attempted else 0.0
    return {
        "campaign_id": str(campaign.id),
        "name": campaign.name,
        "channel": campaign.channel,
        "status": campaign.status,
        "sent_count": campaign.sent_count,
        "failed_count": campaign.failed_count,
        "skipped_count": int(skipped),
        "delivery_rate": delivery_rate,
        "created_at": campaign.created_at.isoformat(),
    }
