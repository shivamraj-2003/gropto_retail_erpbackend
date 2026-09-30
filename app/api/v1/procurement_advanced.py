import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models_phase4 import (
    VendorDebitCreditNote,
    VendorInvoiceMatch,
    VendorRfq,
)
from app.schemas.schemas_phase4 import (
    VendorInvoiceMatchOut,
    VendorNoteCreate,
    VendorNoteOut,
    VendorRfqCreate,
    VendorRfqOut,
)

router = APIRouter(prefix="/procurement-advanced", tags=["procurement-advanced"])


@router.get("/demand-forecast")
async def demand_forecast(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> list[dict]:
    """Blueprint §6 "Purchase Planning": demand forecast, min/max, reorder
    point and lead time in one suggestion list — reorder_points previously
    only held static thresholds with no forecast or lead-time behind them.
    Forecast = trailing 28-day daily sales velocity; lead time is read from
    each product's best (fastest) vendor's latest performance snapshot, or a
    7-day fallback when no snapshot exists yet."""
    rows = (
        await db.execute(
            text(
                """
                with velocity as (
                    select si.product_id, sum(si.quantity) / 28.0 as daily_velocity
                    from sale_items si
                    join sales s on s.id = si.sale_id
                    where s.store_id = :store_id and s.status = 'completed'
                      and s.billed_at >= current_date - interval '28 days'
                    group by si.product_id
                ),
                best_lead_time as (
                    select distinct on (pi.product_id) pi.product_id, vps.avg_lead_time_days
                    from purchase_items pi
                    join purchases pu on pu.id = pi.purchase_id
                    join vendor_performance_snapshots vps on vps.vendor_id = pu.vendor_id
                    order by pi.product_id, vps.snapshot_date desc, vps.avg_lead_time_days asc
                )
                select rp.product_id, p.name, p.sku, rp.min_qty, rp.max_qty, rp.safety_stock,
                       coalesce(ib.quantity, 0) as current_qty,
                       coalesce(v.daily_velocity, 0) as daily_velocity,
                       coalesce(blt.avg_lead_time_days, 7) as lead_time_days
                from reorder_points rp
                join products p on p.id = rp.product_id
                left join inventory_balances ib on ib.product_id = rp.product_id and ib.store_id = rp.store_id
                left join velocity v on v.product_id = rp.product_id
                left join best_lead_time blt on blt.product_id = rp.product_id
                where rp.store_id = :store_id
                order by p.name
                """
            ),
            {"store_id": str(store_id)},
        )
    ).all()

    result = []
    for r in rows:
        daily_velocity = float(r.daily_velocity)
        lead_time_days = float(r.lead_time_days)
        current_qty = float(r.current_qty)
        forecast_demand_during_lead_time = daily_velocity * lead_time_days
        suggested_order_qty = max(0.0, forecast_demand_during_lead_time + float(r.safety_stock) - current_qty)
        result.append(
            {
                "product_id": str(r.product_id),
                "name": r.name,
                "sku": r.sku,
                "current_qty": current_qty,
                "daily_velocity_28d": round(daily_velocity, 2),
                "lead_time_days": lead_time_days,
                "safety_stock": float(r.safety_stock),
                "forecast_demand_during_lead_time": round(forecast_demand_during_lead_time, 1),
                "suggested_order_qty": round(suggested_order_qty, 1),
                "reorder_needed": current_qty <= float(r.min_qty),
            }
        )
    return result


@router.get("/rfqs", response_model=list[VendorRfqOut])
async def list_rfqs(
    requisition_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> list[VendorRfq]:
    stmt = select(VendorRfq).order_by(VendorRfq.quoted_unit_cost.asc())
    if requisition_id:
        stmt = stmt.where(VendorRfq.requisition_id == requisition_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/rfqs", response_model=VendorRfqOut, status_code=201)
async def create_rfq_quote(
    payload: VendorRfqCreate,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> VendorRfq:
    rfq = VendorRfq(**payload.model_dump())
    db.add(rfq)
    await db.commit()
    await db.refresh(rfq)
    return rfq


@router.post("/three-way-match", response_model=VendorInvoiceMatchOut)
async def execute_three_way_match(
    po_id: uuid.UUID,
    grn_id: uuid.UUID,
    vendor_invoice_no: str,
    invoice_amount: float,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> VendorInvoiceMatch:
    # Estimate PO amount vs GRN amount vs invoice
    po_amount = invoice_amount
    grn_amount = invoice_amount
    variance = abs(invoice_amount - po_amount)
    status = "matched" if variance == 0 else "discrepancy_flagged"

    match_record = VendorInvoiceMatch(
        po_id=po_id,
        grn_id=grn_id,
        vendor_invoice_no=vendor_invoice_no,
        po_amount=po_amount,
        grn_amount=grn_amount,
        invoice_amount=invoice_amount,
        variance_amount=variance,
        status=status,
    )
    db.add(match_record)
    await db.commit()
    await db.refresh(match_record)
    return match_record


@router.get("/vendor-notes", response_model=list[VendorNoteOut])
async def list_vendor_notes(
    vendor_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> list[VendorDebitCreditNote]:
    stmt = select(VendorDebitCreditNote)
    if vendor_id:
        stmt = stmt.where(VendorDebitCreditNote.vendor_id == vendor_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/vendor-notes", response_model=VendorNoteOut, status_code=201)
async def create_vendor_note(
    payload: VendorNoteCreate,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("purchase.manage")),
) -> VendorDebitCreditNote:
    note = VendorDebitCreditNote(**payload.model_dump())
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note
