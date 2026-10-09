"""GST turnover/applicability monitoring and e-invoice status (Point 10
audit fix) — none of this existed before. Applicability/turnover are
modelled at the Company (GSTIN/PAN-bearing legal entity) level, never per
store. See services/gst.py and services/einvoice.py for why."""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db, get_reporting_db
from app.models.models import Sale, Store
from app.models.models_phase4 import Company, EInvoice
from app.schemas.schemas_phase4 import EInvoiceCancelIn, EInvoiceOut
from app.services import einvoice as einvoice_service
from app.services.gst import check_einvoice_applicability, current_financial_year, store_applicability

router = APIRouter(prefix="/gst", tags=["gst"])


@router.get("/applicability")
async def get_applicability(
    company_id: uuid.UUID,
    financial_year: int | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("gst.applicability.view")),
) -> dict:
    company = await db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    fy = financial_year or (date.today().year if date.today().month >= 4 else date.today().year - 1)
    return await check_einvoice_applicability(db, company=company, financial_year=fy)


@router.get("/store-applicability")
async def get_store_applicability(
    financial_year: int | None = None,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(require_permission("gst.applicability.view")),
) -> list[dict]:
    """The ₹5 crore e-invoice check, one row per store, on that store's own
    sales. Limited to the stores the caller can see."""
    fy = financial_year or current_financial_year()
    return await store_applicability(
        db, store_ids=None if current.sees_all_stores() else list(current.store_ids), financial_year=fy
    )


@router.get("/store-applicability/export")
async def export_store_applicability(
    financial_year: int | None = None,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(require_permission("gst.applicability.view")),
):
    """The per-store ₹5 crore check as an Excel file."""
    import io

    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook

    rows = await get_store_applicability(financial_year, db, current)
    wb = Workbook()
    ws = wb.active
    ws.title = "E-invoice by store"
    ws.append(["Code", "Store", "GSTIN", "Sales this year", "Sales last year", "Limit", "% of limit", "E-invoice", "Note"])
    for r in rows:
        status = "Not yet" if not r["einvoice_required"] else ("On (switched on)" if r["einvoice_flag"] else "On (over limit)")
        ws.append([r["code"], r["name"], r["gstin"] or "", r["turnover"], r["previous_year_turnover"], r["threshold"],
                   round(r["percent_of_threshold"], 1), status, r["note"] or ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="einvoice-by-store.xlsx"'},
    )


@router.get("/einvoices", response_model=list[EInvoiceOut])
async def list_einvoices(
    status_filter: str | None = None,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("gst.einvoice.view")),
) -> list[EInvoice]:
    if store_id is not None:
        require_store_access(store_id, current)
    stmt = select(EInvoice).order_by(EInvoice.created_at.desc()).limit(200)
    if status_filter:
        stmt = stmt.where(EInvoice.status == status_filter)
    if store_id:
        stmt = stmt.join(Sale, Sale.id == EInvoice.sale_id).where(Sale.store_id == store_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/einvoices/{sale_id}/submit", response_model=EInvoiceOut)
async def submit_einvoice(
    sale_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("gst.einvoice.create")),
) -> EInvoice:
    sale = await db.get(Sale, sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail="Sale not found")
    require_store_access(sale.store_id, current)
    store = await db.get(Store, sale.store_id)
    company = await db.get(Company, store.company_id) if store and store.company_id else None
    einvoice = await einvoice_service.submit_einvoice(db, current=current, sale=sale, store=store, company=company)
    await db.commit()
    await db.refresh(einvoice)
    return einvoice


@router.post("/einvoices/{einvoice_id}/cancel", response_model=EInvoiceOut)
async def cancel_einvoice(
    einvoice_id: uuid.UUID,
    payload: EInvoiceCancelIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("gst.einvoice.cancel")),
) -> EInvoice:
    einvoice = await db.get(EInvoice, einvoice_id)
    if einvoice is None:
        raise HTTPException(status_code=404, detail="E-invoice not found")
    result = await einvoice_service.cancel_einvoice(db, current=current, einvoice=einvoice, reason=payload.reason)
    await db.commit()
    await db.refresh(result)
    return result
