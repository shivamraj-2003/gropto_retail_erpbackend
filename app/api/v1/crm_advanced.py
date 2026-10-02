import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models_phase4 import CustomerServiceTicket, RfmCohortSnapshot
from app.schemas.schemas_phase4 import RfmCohortOut, TicketCreate, TicketOut, TicketUpdate
from app.services.audit import write_audit

router = APIRouter(prefix="/crm-advanced", tags=["crm-advanced"])


async def _compute_rfm_cohorts(db: AsyncSession) -> list[RfmCohortSnapshot]:
    """Point 3 audit fix: rfm_cohort_snapshots was a real table with a real
    read endpoint, but nothing anywhere ever wrote a row to it — "Cohort
    Performance" was structurally present yet permanently empty. Computes a
    real RFM (recency/frequency/monetary) score per customer from actual
    sales history, quintile-ranked, same on-demand-compute-if-empty pattern
    `_compute_abc_xyz` already uses for inventory intelligence."""
    rows = (
        await db.execute(
            text(
                """
                with per_customer as (
                    select customer_id,
                           extract(day from now() - max(billed_at))::int as recency_days,
                           count(*) as frequency,
                           sum(grand_total) as monetary
                    from sales
                    where status = 'completed' and customer_id is not null
                    group by customer_id
                ),
                scored as (
                    select customer_id, recency_days, frequency, monetary,
                           -- lower recency_days is better, so invert the quintile direction
                           6 - ntile(5) over (order by recency_days) as recency_score,
                           ntile(5) over (order by frequency) as frequency_score,
                           ntile(5) over (order by monetary) as monetary_score
                    from per_customer
                )
                select * from scored
                """
            )
        )
    ).all()

    snapshots: list[RfmCohortSnapshot] = []
    for r in rows:
        if r.recency_score >= 4 and r.frequency_score >= 4 and r.monetary_score >= 4:
            segment = "champions"
        elif r.frequency_score >= 4:
            segment = "loyal"
        elif r.recency_score <= 2 and (r.frequency_score >= 3 or r.monetary_score >= 3):
            segment = "at_risk"
        elif r.recency_score <= 2 and r.frequency_score <= 2:
            segment = "hibernating"
        elif r.frequency <= 1:
            segment = "new_customer"
        else:
            segment = "needs_attention"
        snapshot = RfmCohortSnapshot(
            customer_id=r.customer_id,
            recency_score=int(r.recency_score),
            frequency_score=int(r.frequency_score),
            monetary_score=int(r.monetary_score),
            segment=segment,
            churn_risk_flag=segment in ("at_risk", "hibernating"),
        )
        db.add(snapshot)
        snapshots.append(snapshot)
    await db.commit()
    for s in snapshots:
        await db.refresh(s)
    return snapshots


async def refresh_rfm_cohorts(db: AsyncSession) -> None:
    """Full recompute (not just fill-if-empty) — called by the daily
    scheduled job so segments stay current as purchase history changes,
    rather than being frozen at whatever they were on first read."""
    await db.execute(delete(RfmCohortSnapshot))
    await _compute_rfm_cohorts(db)


@router.get("/tickets", response_model=list[TicketOut])
async def list_tickets(
    customer_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    # Point 11 audit fix: this was gated by inventory.view, an unrelated
    # permission that most store staff hold — any of them could read every
    # customer service ticket company-wide. crm.view is the actual CRM
    # read permission used everywhere else in this feature area.
    _current: CurrentUser = Depends(require_permission("crm.ticket.view")),
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
    current: CurrentUser = Depends(require_permission("crm.ticket.create")),
) -> CustomerServiceTicket:
    ticket = CustomerServiceTicket(**payload.model_dump())
    db.add(ticket)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="ticket.created",
        entity_type="customer_service_ticket",
        entity_id=ticket.id,
        new_value={"category": ticket.category, "subject": ticket.subject, "priority": ticket.priority},
    )
    await db.commit()
    await db.refresh(ticket)
    return ticket


@router.put("/tickets/{ticket_id}", response_model=TicketOut)
async def update_ticket(
    ticket_id: uuid.UUID,
    payload: TicketUpdate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("crm.ticket.update")),
) -> CustomerServiceTicket:
    """Point 11 audit fix: the frontend already called this endpoint
    (assign/resolve a ticket) — it didn't exist on the backend at all, so
    every call 404'd."""
    ticket = await db.get(CustomerServiceTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    old_value = {"status": ticket.status, "assigned_to": str(ticket.assigned_to) if ticket.assigned_to else None}
    if payload.status is not None:
        ticket.status = payload.status
        if payload.status in ("resolved", "closed") and ticket.resolved_at is None:
            ticket.resolved_at = datetime.now(timezone.utc)
    if payload.assigned_to is not None:
        ticket.assigned_to = payload.assigned_to
    if payload.resolution_notes is not None:
        ticket.resolution_notes = payload.resolution_notes
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="ticket.updated",
        entity_type="customer_service_ticket",
        entity_id=ticket.id,
        old_value=old_value,
        new_value={"status": ticket.status, "assigned_to": str(ticket.assigned_to) if ticket.assigned_to else None},
    )
    await db.commit()
    await db.refresh(ticket)
    return ticket


@router.get("/rfm-cohorts", response_model=list[RfmCohortOut])
async def list_rfm_cohorts(
    segment: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("crm.analytics.view")),
) -> list[RfmCohortSnapshot]:
    stmt = select(RfmCohortSnapshot)
    if segment:
        stmt = stmt.where(RfmCohortSnapshot.segment == segment)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    if not rows and segment is None:
        rows = await _compute_rfm_cohorts(db)
    return rows
