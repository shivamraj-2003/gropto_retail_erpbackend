import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models_phase2 import Payable, VendorInvoice, VendorPerformanceSnapshot
from app.models.models_phase4 import (
    VendorDebitCreditNote,
    VendorRfq,
)
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import (
    PayableOut,
    ThreeWayMatchResultOut,
    VendorInvoiceCreate,
    VendorInvoiceOut,
    VendorPaymentIn,
    VendorPerformanceOut,
)
from app.schemas.schemas_phase4 import (
    VendorNoteCreate,
    VendorNoteOut,
    VendorRfqCreate,
    VendorRfqOut,
)
from app.services.vendor_invoices import create_vendor_invoice, force_match_invoice
from app.services.vendor_payments import record_payment

router = APIRouter(prefix="/procurement-advanced", tags=["procurement-advanced"])


@router.get("/demand-forecast")
async def demand_forecast(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("procurement.forecast.view")),
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
        uncapped_suggested_qty = max(0.0, forecast_demand_during_lead_time + float(r.safety_stock) - current_qty)
        # Point 7 audit fix: max_qty was selected in this query but never
        # actually used anywhere — a pure decorative ceiling. Now caps the
        # suggestion so current_qty + suggested never exceeds it.
        max_qty = float(r.max_qty)
        suggested_order_qty = min(uncapped_suggested_qty, max(0.0, max_qty - current_qty)) if max_qty > 0 else uncapped_suggested_qty
        result.append(
            {
                "product_id": str(r.product_id),
                "name": r.name,
                "sku": r.sku,
                "current_qty": current_qty,
                "daily_velocity_28d": round(daily_velocity, 2),
                "lead_time_days": lead_time_days,
                "safety_stock": float(r.safety_stock),
                "max_qty": max_qty,
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
    _current: CurrentUser = Depends(require_permission("procurement.rfq.view")),
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
    _current: CurrentUser = Depends(require_permission("procurement.rfq.create")),
) -> VendorRfq:
    rfq = VendorRfq(**payload.model_dump())
    db.add(rfq)
    await db.commit()
    await db.refresh(rfq)
    return rfq


@router.get("/vendor-invoices", response_model=Page[VendorInvoiceOut])
async def list_vendor_invoices(
    vendor_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("procurement.vendor_invoice.view")),
) -> Page[VendorInvoiceOut]:
    stmt = select(VendorInvoice)
    if vendor_id:
        stmt = stmt.where(VendorInvoice.vendor_id == vendor_id)
    if status_filter:
        stmt = stmt.where(VendorInvoice.status == status_filter)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(VendorInvoice.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.post("/vendor-invoices", response_model=ThreeWayMatchResultOut, status_code=201)
async def record_vendor_invoice(
    payload: VendorInvoiceCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("procurement.vendor_invoice.create")),
) -> dict:
    """Point 6 audit fix: this is the real three-way match — PO vs GRN vs
    invoice amounts are actually looked up and compared (the previous
    /three-way-match endpoint hardcoded po_amount=grn_amount=invoice_amount,
    so it always reported a match regardless of real data). A clean match
    also creates the resulting Payable; a discrepancy blocks it pending
    review via POST /vendor-invoices/{id}/force-match."""
    result = await create_vendor_invoice(db, current=current, payload=payload)
    await db.commit()
    await db.refresh(result["invoice"])
    return {
        "invoice": result["invoice"],
        "po_amount": result["po_amount"],
        "grn_amount": result["grn_amount"],
        "variance_amount": result["variance_amount"],
        "match_status": result["match_status"],
        "payable_id": result["payable"].id if result["payable"] else None,
    }


@router.post("/vendor-invoices/{invoice_id}/force-match", response_model=PayableOut)
async def force_match_vendor_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    # Deliberately a narrower gate than purchase.manage — overriding a real
    # mismatch is a financial-authority decision, not routine data entry.
    current: CurrentUser = Depends(require_permission("procurement.vendor_invoice.override")),
) -> Payable:
    invoice = await db.get(VendorInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Vendor invoice not found")
    payable = await force_match_invoice(db, current=current, invoice=invoice)
    await db.commit()
    await db.refresh(payable)
    return payable


@router.get("/vendor-performance", response_model=list[VendorPerformanceOut])
async def list_vendor_performance(
    vendor_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("vendor.performance.view")),
) -> list[VendorPerformanceSnapshot]:
    stmt = select(VendorPerformanceSnapshot)
    if vendor_id:
        stmt = stmt.where(VendorPerformanceSnapshot.vendor_id == vendor_id)
    if date_from:
        stmt = stmt.where(VendorPerformanceSnapshot.snapshot_date >= date_from)
    if date_to:
        stmt = stmt.where(VendorPerformanceSnapshot.snapshot_date <= date_to)
    result = await db.execute(stmt.order_by(VendorPerformanceSnapshot.snapshot_date.desc()))
    return list(result.scalars().all())


@router.post("/payables/{payable_id}/payments")
async def pay_payable(
    payable_id: uuid.UUID,
    payload: VendorPaymentIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("finance.payable.create")),
) -> dict:
    payable = await db.get(Payable, payable_id)
    if payable is None:
        raise HTTPException(status_code=404, detail="Payable not found")
    result = await record_payment(db, current=current, payable=payable, payload=payload)
    await db.commit()
    if isinstance(result, dict):
        return {"approval_request_id": str(result["approval_request_id"]), "status": result["status"]}
    await db.refresh(result)
    return {
        "payment_id": str(result.id),
        "payable_id": str(result.payable_id),
        "amount": float(result.amount),
        "status": result.status,
    }


@router.get("/vendor-notes", response_model=list[VendorNoteOut])
async def list_vendor_notes(
    vendor_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("vendor.note.view")),
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
    _current: CurrentUser = Depends(require_permission("vendor.note.create")),
) -> VendorDebitCreditNote:
    note = VendorDebitCreditNote(**payload.model_dump())
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note
