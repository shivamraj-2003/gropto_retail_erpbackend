"""GST turnover/applicability monitoring and e-invoice status (Point 10
audit fix) — none of this existed before. Applicability/turnover are
modelled at the Company (GSTIN/PAN-bearing legal entity) level, never per
store. See services/gst.py and services/einvoice.py for why."""

import io
import re
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db, get_reporting_db
from app.models.models import Sale, Store
from app.models.models_phase4 import Company, EInvoice
from app.schemas.schemas_phase4 import EInvoiceCancelIn, EInvoiceOut
from app.services import einvoice as einvoice_service
from app.services import gst_returns
from app.services.audit import write_audit
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


# ---------------------------------------------------------------------------
# GSTR-1 and GSTR-3B working figures (see services/gst_returns.py)
# ---------------------------------------------------------------------------

def _last_month() -> str:
    t = date.today()
    return f"{t.year - 1}-12" if t.month == 1 else f"{t.year}-{t.month - 1:02d}"


async def _return_stores(db: AsyncSession, current: CurrentUser, store_id: uuid.UUID | None) -> list[uuid.UUID]:
    if store_id is not None:
        require_store_access(store_id, current)
        return [store_id]
    if current.sees_all_stores():
        return list((await db.execute(select(Store.id))).scalars().all())
    return list(current.store_ids)


def _check_month(month: str | None) -> str:
    month = month or _last_month()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="The month must look like 2026-10")
    return month


async def _store_label(db: AsyncSession, store_id: uuid.UUID | None) -> dict:
    if store_id is None:
        return {"store": "All stores you can see", "gstin": None}
    store = await db.get(Store, store_id)
    return {"store": store.name if store else "", "gstin": store.gstin if store else None}


@router.get("/returns/gstr1")
async def gstr1_view(
    month: str | None = None,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(require_permission("reports.finance.export")),
) -> dict:
    """GSTR-1 working figures for one month (YYYY-MM): B2B, B2CL, B2CS, credit notes, HSN summary and invoice numbers issued."""
    month = _check_month(month)
    data = await gst_returns.gstr1(db, stores=await _return_stores(db, current, store_id), month=month)
    return {**data, **await _store_label(db, store_id)}


@router.get("/returns/gstr3b")
async def gstr3b_view(
    month: str | None = None,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(require_permission("reports.finance.export")),
) -> dict:
    """GSTR-3B working figures for one month: tax on sales, input credit from purchases, and the tax left to pay."""
    month = _check_month(month)
    data = await gst_returns.gstr3b(db, stores=await _return_stores(db, current, store_id), month=month)
    return {**data, **await _store_label(db, store_id)}


def _sheet(wb: Workbook, title: str, header: list[str], rows: list[list], first: bool = False):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append(header)
    for r in rows:
        ws.append(r)
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = max(12, min(40, max(len(str(c.value or "")) for c in col) + 2))
    return ws


def _download(wb: Workbook, name: str) -> StreamingResponse:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/returns/gstr1/export")
async def gstr1_export(
    month: str | None = None,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(require_permission("reports.finance.export")),
) -> StreamingResponse:
    month = _check_month(month)
    d = await gst_returns.gstr1(db, stores=await _return_stores(db, current, store_id), month=month)
    wb = Workbook()
    _sheet(wb, "Summary", ["Item", "Value"], [["Period", month], ["Invoices", d["totals"]["invoices"]], ["Taxable value", d["totals"]["taxable"]], ["IGST", d["totals"]["igst"]], ["CGST", d["totals"]["cgst"]], ["SGST", d["totals"]["sgst"]], ["Credit notes (refunds)", d["notes_total"]], *[["Check", w] for w in d["warnings"]]], first=True)

    def inv_rows(rows):
        return [[r.get("ctin", ""), r["inum"], r["idt"], r["val"], r["pos_name"], i["rt"], i["txval"], i["iamt"], i["camt"], i["samt"], i["csamt"]] for r in rows for i in r["itms"]]

    head = ["GSTIN/UIN", "Invoice no.", "Invoice date", "Invoice value", "Place of supply", "Rate %", "Taxable value", "IGST", "CGST", "SGST", "Cess"]
    _sheet(wb, "B2B", head, inv_rows(d["b2b"]))
    _sheet(wb, "B2CL", head, inv_rows(d["b2cl"]))
    _sheet(wb, "B2CS", ["Supply type", "Place of supply", "Rate %", "Taxable value", "IGST", "CGST", "SGST", "Cess"], [[r["sply_ty"], r["pos_name"], r["rt"], r["txval"], r["iamt"], r["camt"], r["samt"], r["csamt"]] for r in d["b2cs"]])
    note_head = ["GSTIN/UIN", "Credit note no.", "Credit note date", "Against invoice", "Note value", "Rate %", "Taxable value", "IGST", "CGST", "SGST"]
    note_rows = lambda rows: [[r["ctin"], r["nt_num"], r["nt_dt"], r["inum"], r["val"], i["rt"], i["txval"], i["iamt"], i["camt"], i["samt"]] for r in rows for i in r["itms"]]
    _sheet(wb, "CDNR", note_head, note_rows(d["cdnr"]))
    _sheet(wb, "CDNUR", note_head, note_rows(d["cdnur"]))
    _sheet(wb, "HSN", ["HSN", "Rate %", "Quantity", "Taxable value", "IGST", "CGST", "SGST"], [[r["hsn_sc"], r["rt"], r["qty"], r["txval"], r["iamt"], r["camt"], r["samt"]] for r in d["hsn"]])
    _sheet(wb, "Documents", ["Till", "From", "To", "Total issued", "Cancelled", "Net issued"], [[r["till"], r["from"], r["to"], r["total"], r["cancelled"], r["net_issued"]] for r in d["docs"]])
    await write_audit(db, user_id=current.user_id, role_code=current.role_code, store_id=store_id, device_id=current.device_id, action="report.exported", entity_type="report", entity_id=None, new_value={"report": "gstr1", "month": month})
    await db.commit()
    return _download(wb, f"GSTR1-{month}.xlsx")


@router.get("/returns/gstr3b/export")
async def gstr3b_export(
    month: str | None = None,
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_reporting_db),
    current: CurrentUser = Depends(require_permission("reports.finance.export")),
) -> StreamingResponse:
    month = _check_month(month)
    d = await gst_returns.gstr3b(db, stores=await _return_stores(db, current, store_id), month=month)
    wb = Workbook()
    rows = [
        ["3.1(a) Outward taxable supplies", d["outward_taxable"]["txval"], d["outward_taxable"]["igst"], d["outward_taxable"]["cgst"], d["outward_taxable"]["sgst"]],
        ["3.1(c) Nil rated / exempt outward supplies", d["outward_nil_exempt"]["txval"], 0, 0, 0],
        ["3.1(d) Inward supplies liable to reverse charge", d["inward_reverse_charge"]["txval"], d["inward_reverse_charge"]["igst"], d["inward_reverse_charge"]["cgst"], d["inward_reverse_charge"]["sgst"]],
        ["4(A) Input tax credit available (purchases with a tax invoice)", "", d["itc_available"]["igst"], d["itc_available"]["cgst"], d["itc_available"]["sgst"]],
        ["Input tax credit NOT claimed (vendor has no GSTIN)", "", d["itc_not_claimed"]["igst"], d["itc_not_claimed"]["cgst"], d["itc_not_claimed"]["sgst"]],
        ["Input tax credit not eligible (marked on the bill)", "", d["itc_ineligible"]["igst"], d["itc_ineligible"]["cgst"], d["itc_ineligible"]["sgst"]],
        ["5 Inward nil / exempt / no-GST supplies: inter-state", d["inward_nil_exempt"]["inter"], "", "", ""],
        ["5 Inward nil / exempt / no-GST supplies: intra-state", d["inward_nil_exempt"]["intra"], "", "", ""],
        ["Cash purchases without GST (included above)", d["cash_purchases_without_gst"], "", "", ""],
        ["Tax payable before credit", "", d["tax_payable_before_credit"]["igst"], d["tax_payable_before_credit"]["cgst"], d["tax_payable_before_credit"]["sgst"]],
        ["6.1 TAX TO PAY after credit (reverse charge included)", "", d["tax_to_pay"]["igst"], d["tax_to_pay"]["cgst"], d["tax_to_pay"]["sgst"]],
        ["Credit carried forward", "", d["itc_carried_forward"]["igst"], d["itc_carried_forward"]["cgst"], d["itc_carried_forward"]["sgst"]],
        *[["Check", w, "", "", ""] for w in d["warnings"]],
    ]
    _sheet(wb, "GSTR-3B", ["Table", "Taxable value", "IGST", "CGST", "SGST"], rows, first=True)
    _sheet(wb, "3.2 Inter-state to unregistered", ["Place of supply", "Taxable value", "IGST"], [[r["pos"], r["txval"], r["igst"]] for r in d["inter_state_unregistered"]])
    await write_audit(db, user_id=current.user_id, role_code=current.role_code, store_id=store_id, device_id=current.device_id, action="report.exported", entity_type="report", entity_id=None, new_value={"report": "gstr3b", "month": month})
    await db.commit()
    return _download(wb, f"GSTR3B-{month}.xlsx")
