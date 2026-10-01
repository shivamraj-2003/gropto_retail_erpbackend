import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models_phase4 import CustomerServiceTicket, RfmCohortSnapshot
from app.schemas.schemas_phase4 import RfmCohortOut, TicketCreate, TicketOut

router = APIRouter(prefix="/crm-advanced", tags=["crm-advanced"])


@router.get("/tickets", response_model=list[TicketOut])
async def list_tickets(
    customer_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[CustomerServiceTicket]:
    stmt = select(CustomerServiceTicket).order_by(CustomerServiceTicket.created_at.desc())
    if customer_id:
        stmt = stmt.where(CustomerServiceTicket.customer_id == customer_id)
    if status_filter:
        stmt = stmt.where(CustomerServiceTicket.status == status_filter)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/tickets", response_model=TicketOut, status_code=201)
async def create_ticket(
    payload: TicketCreate,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> CustomerServiceTicket:
    ticket = CustomerServiceTicket(**payload.model_dump())
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


@router.get("/rfm-cohorts", response_model=list[RfmCohortOut])
async def list_rfm_cohorts(
    segment: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[RfmCohortSnapshot]:
    stmt = select(RfmCohortSnapshot)
    if segment:
        stmt = stmt.where(RfmCohortSnapshot.segment == segment)
    result = await db.execute(stmt)
    return list(result.scalars().all())
