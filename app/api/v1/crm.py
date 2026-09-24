import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models_phase3 import Campaign
from app.schemas.schemas_phase3 import CampaignCreate, ConsentUpdate
from app.services import crm as crm_service

router = APIRouter(prefix="/crm", tags=["crm"])


@router.get("/customers/{customer_id}/360")
async def customer_360(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    return await crm_service.customer_360(db, customer_id=customer_id)


@router.get("/segments/rfm")
async def rfm_segments(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> list[dict]:
    return await crm_service.rfm_segments(db)


@router.put("/customers/{customer_id}/consent")
async def update_consent(
    customer_id: uuid.UUID,
    payload: ConsentUpdate,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    await crm_service.update_consent(
        db,
        customer_id=customer_id,
        whatsapp=payload.whatsapp_opt_in,
        sms=payload.sms_opt_in,
        email=payload.email_opt_in,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/campaigns", status_code=201)
async def create_campaign(
    payload: CampaignCreate,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    """Audience is defined via segment_query (consumed by the messaging integration
    layer, e.g. WhatsApp/SMS/email provider) — consent is enforced by crm_service
    joining against customer_consent before any send, not at campaign creation."""
    campaign = Campaign(name=payload.name, channel=payload.channel, segment_query=payload.segment_query)
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return {"campaign_id": str(campaign.id)}
