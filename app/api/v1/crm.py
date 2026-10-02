import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase3 import Campaign
from app.models.models_phase4 import ConsentHistory, SavedAudience
from app.schemas.schemas import Page
from app.schemas.schemas_phase3 import CampaignCreate, CampaignOut, CampaignSendResult, ConsentUpdate
from app.schemas.schemas_phase4 import (
    AudiencePreviewIn,
    CampaignAnalyticsOut,
    ConsentHistoryOut,
    Customer360Full,
    SavedAudienceCreate,
    SavedAudienceOut,
)
from app.services import crm as crm_service
from app.services.audit import write_audit
from app.services.channels import get_supported_channels

router = APIRouter(prefix="/crm", tags=["crm"])


@router.get("/campaigns", response_model=Page[CampaignOut])
async def list_campaigns(
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.campaign.view")),
) -> Page[CampaignOut]:
    stmt = select(Campaign)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(Campaign.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.get("/customers/{customer_id}/360", response_model=Customer360Full)
async def customer_360(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.customer.view")),
) -> dict:
    return await crm_service.customer_360(db, customer_id=customer_id)


@router.get("/segments/rfm")
async def rfm_segments(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.analytics.view")),
) -> list[dict]:
    return await crm_service.rfm_segments(db)


@router.put("/customers/{customer_id}/consent")
async def update_consent(
    customer_id: uuid.UUID,
    payload: ConsentUpdate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("crm.consent.update")),
) -> dict:
    await crm_service.update_consent(
        db,
        customer_id=customer_id,
        whatsapp=payload.whatsapp_opt_in,
        sms=payload.sms_opt_in,
        email=payload.email_opt_in,
        user_id=current.user_id,
        source=payload.source,
    )
    await db.commit()
    return {"status": "ok"}


@router.get("/customers/{customer_id}/consent-history", response_model=list[ConsentHistoryOut])
async def consent_history(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.customer.view")),
) -> list[ConsentHistory]:
    result = await db.execute(
        select(ConsentHistory).where(ConsentHistory.customer_id == customer_id).order_by(ConsentHistory.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("/campaigns", status_code=201)
async def create_campaign(
    payload: CampaignCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("crm.campaign.create")),
) -> dict:
    """Audience is either a saved audience (payload.saved_audience_id) or an
    inline segment_query — {"segment": "<rfm band>"} or {"criteria": {...}}
    matching AudienceCriteria. Consent is enforced in crm_service.send_campaign,
    not here — a customer who opts out after the campaign is drafted still
    gets skipped at send time, never messaged."""
    if payload.saved_audience_id:
        audience = await db.get(SavedAudience, payload.saved_audience_id)
        if audience is None:
            raise HTTPException(status_code=404, detail="Saved audience not found")
    campaign = Campaign(
        name=payload.name,
        channel=payload.channel,
        segment_query=payload.segment_query,
        saved_audience_id=payload.saved_audience_id,
        template_name=payload.template_name,
        created_by=current.user_id,
    )
    db.add(campaign)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="campaign.created",
        entity_type="campaign",
        entity_id=campaign.id,
        new_value={"name": payload.name, "channel": payload.channel, "template_name": payload.template_name},
    )
    await db.commit()
    await db.refresh(campaign)
    return {"campaign_id": str(campaign.id)}


@router.post("/campaigns/{campaign_id}/send", response_model=CampaignSendResult)
async def send_campaign(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("crm.campaign.approve")),
) -> dict:
    return await crm_service.send_campaign(db, campaign_id=campaign_id, user_id=current.user_id)


@router.get("/campaigns/{campaign_id}/analytics", response_model=CampaignAnalyticsOut)
async def campaign_analytics(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.campaign.view")),
) -> dict:
    return await crm_service.campaign_analytics(db, campaign_id=campaign_id)


@router.get("/customers")
async def list_customers(
    search: str | None = None,
    store_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("crm.customer.view")),
) -> Page:
    """Search/list customers by phone or name. When store_id is supplied,
    scoped to customers who have at least one sale at that store (and the
    caller must actually have access to that store) — Point 11 audit fix:
    previously this endpoint had no store-scoping path at all, so any
    crm.view holder could browse every customer company-wide with no filter
    available."""
    from sqlalchemy import text as _text

    from app.models.models import Customer

    base_stmt = select(Customer)
    if store_id is not None:
        require_store_access(store_id, current)
        store_customer_ids = (
            await db.execute(
                _text("select distinct customer_id from sales where store_id = :sid and customer_id is not null"),
                {"sid": str(store_id)},
            )
        ).scalars().all()
        base_stmt = base_stmt.where(Customer.id.in_(store_customer_ids))
    if search:
        base_stmt = base_stmt.where(Customer.phone.ilike(f"%{search}%") | Customer.name.ilike(f"%{search}%"))
    total = (await db.execute(select(func.count()).select_from(base_stmt.subquery()))).scalar_one()
    rows = (await db.execute(base_stmt.order_by(Customer.created_at.desc()).limit(min(limit, 200)).offset(offset))).scalars().all()
    return Page(
        items=[{"id": str(c.id), "phone": c.phone, "name": c.name, "email": c.email, "created_at": c.created_at.isoformat()} for c in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/analytics/clv")
async def clv_dashboard(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.analytics.view")),
) -> dict:
    from app.models.models_phase4 import ClvSnapshot
    from app.services.clv import compute_clv_snapshots, get_clv_summary

    count = (await db.execute(select(func.count()).select_from(ClvSnapshot))).scalar_one()
    if count == 0:
        await compute_clv_snapshots(db)
    return await get_clv_summary(db)


@router.post("/analytics/clv/refresh")
async def refresh_clv(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.analytics.update")),
) -> dict:
    from app.services.clv import compute_clv_snapshots

    snapshots = await compute_clv_snapshots(db)
    return {"recomputed": len(snapshots)}


@router.get("/analytics/retention")
async def retention_dashboard(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.analytics.view")),
) -> dict:
    from app.services.clv import get_retention_metrics

    return await get_retention_metrics(db)


@router.get("/analytics/churn")
async def churn_analysis(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.analytics.view")),
) -> dict:
    from app.models.models_phase4 import RfmCohortSnapshot

    rows = (await db.execute(select(RfmCohortSnapshot).where(RfmCohortSnapshot.churn_risk_flag == True))).scalars().all()  # noqa: E712
    return {
        "total_at_risk": len(rows),
        "customers": [
            {
                "customer_id": str(r.customer_id),
                "segment": r.segment,
                "recency_score": r.recency_score,
                "frequency_score": r.frequency_score,
                "monetary_score": r.monetary_score,
            }
            for r in rows[:50]
        ],
    }


@router.get("/channels")
async def list_channels(
    _current: CurrentUser = Depends(require_permission("crm.audience.view")),
) -> list[dict]:
    return get_supported_channels()


@router.post("/audiences/preview")
async def preview_audience(
    payload: AudiencePreviewIn,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.audience.create")),
) -> dict:
    customer_ids = await crm_service.build_audience(db, payload.criteria.model_dump(exclude_none=True, mode="json"))
    return {"estimated_size": len(customer_ids), "sample_customer_ids": [str(c) for c in customer_ids[:20]]}


@router.post("/audiences", response_model=SavedAudienceOut, status_code=201)
async def create_audience(
    payload: SavedAudienceCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("crm.audience.create")),
) -> SavedAudience:
    audience = await crm_service.save_audience(
        db,
        name=payload.name,
        criteria=payload.criteria.model_dump(exclude_none=True, mode="json"),
        user_id=current.user_id,
    )
    await db.commit()
    await db.refresh(audience)
    return audience


@router.get("/audiences", response_model=list[SavedAudienceOut])
async def list_audiences(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.audience.view")),
) -> list[SavedAudience]:
    result = await db.execute(select(SavedAudience).order_by(SavedAudience.created_at.desc()))
    return list(result.scalars().all())
