"""Vendor invoice intake: real three-way match (PO vs GRN vs Invoice) and the
Payable it produces (Point 6 audit fix — neither existed as real logic
before: three-way match hardcoded po_amount=grn_amount=invoice_amount so it
always "matched", and Payable was never created by any code path at all)."""

import uuid
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import Product, Vendor
from app.models.models_phase2 import Grn, GrnItem, Payable, PurchaseOrder, PurchaseOrderItem, VendorInvoice
from app.models.models_phase4 import VendorInvoiceMatch
from app.schemas.schemas_phase2 import VendorInvoiceCreate
from app.services.audit import write_audit

MATCH_TOLERANCE_PCT = 0.01  # 1% of the reference amount
MATCH_TOLERANCE_FLOOR = 1.0  # never less than ₹1 — avoids false mismatches on tiny rounding


async def _grn_accepted_amount(db: AsyncSession, grn: Grn, po_item_costs: dict) -> float:
    total = 0.0
    for item in grn.items:
        if item.qc_status != "accepted":
            continue
        unit_cost = po_item_costs.get(item.product_id)
        if unit_cost is None:
            product = await db.get(Product, item.product_id)
            unit_cost = float(product.purchase_price) if product else 0.0
        total += float(item.received_qty) * unit_cost
    return total


async def create_vendor_invoice(db: AsyncSession, *, current: CurrentUser, payload: VendorInvoiceCreate) -> dict:
    existing = (
        await db.execute(
            select(VendorInvoice).where(VendorInvoice.vendor_id == payload.vendor_id, VendorInvoice.invoice_number == payload.invoice_number)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Invoice {payload.invoice_number} already recorded for this vendor")

    po: PurchaseOrder | None = None
    grn: Grn | None = None
    po_item_costs: dict = {}
    po_amount: float | None = None
    grn_amount: float | None = None

    if payload.purchase_order_id:
        po = await db.get(PurchaseOrder, payload.purchase_order_id)
        if po is None:
            raise HTTPException(status_code=404, detail="Purchase order not found")
        po_amount = float(po.total_amount)
        po_item_costs = {i.product_id: float(i.unit_cost) for i in po.items}

    if payload.grn_id:
        grn = await db.get(Grn, payload.grn_id)
        if grn is None:
            raise HTTPException(status_code=404, detail="GRN not found")
        grn_amount = await _grn_accepted_amount(db, grn, po_item_costs)

    # Real matching: the GRN (what was actually accepted) is the authoritative
    # reference when available — an invoice should be paid for what arrived
    # and passed QC, not merely what was ordered. Falls back to the PO total
    # when there's no GRN to compare against yet.
    reference_amount = grn_amount if grn_amount is not None else po_amount
    match_status = "recorded"
    variance = 0.0
    if reference_amount is not None:
        variance = round(payload.invoice_amount - reference_amount, 2)
        tolerance = max(MATCH_TOLERANCE_FLOOR, reference_amount * MATCH_TOLERANCE_PCT)
        match_status = "matched" if abs(variance) <= tolerance else "discrepancy_flagged"

    invoice = VendorInvoice(
        vendor_id=payload.vendor_id,
        purchase_order_id=payload.purchase_order_id,
        grn_id=payload.grn_id,
        invoice_number=payload.invoice_number,
        invoice_date=payload.invoice_date,
        invoice_amount=payload.invoice_amount,
        status=match_status,
        created_by=current.user_id,
    )
    db.add(invoice)
    await db.flush()

    match_record = None
    if po is not None and grn is not None:
        match_record = VendorInvoiceMatch(
            po_id=po.id,
            grn_id=grn.id,
            vendor_invoice_no=payload.invoice_number,
            po_amount=po_amount or 0.0,
            grn_amount=grn_amount or 0.0,
            invoice_amount=payload.invoice_amount,
            variance_amount=abs(variance),
            status=match_status,
        )
        db.add(match_record)

    payable = None
    if match_status != "discrepancy_flagged":
        # Point 6 audit fix: this is the entire missing Payable creation
        # path — a real payable, keyed to the vendor's own credit terms.
        vendor = await db.get(Vendor, payload.vendor_id)
        credit_days = vendor.credit_days if vendor else 30
        payable = Payable(
            vendor_id=payload.vendor_id,
            vendor_invoice_id=invoice.id,
            original_amount=payload.invoice_amount,
            amount_due=payload.invoice_amount,
            due_date=payload.invoice_date + timedelta(days=credit_days),
            status="outstanding",
        )
        db.add(payable)
        await db.flush()

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="vendor_invoice.recorded",
        entity_type="vendor_invoice",
        entity_id=invoice.id,
        new_value={
            "invoice_amount": payload.invoice_amount,
            "po_amount": po_amount,
            "grn_amount": grn_amount,
            "variance": variance,
            "match_status": match_status,
        },
    )
    return {"invoice": invoice, "po_amount": po_amount, "grn_amount": grn_amount, "variance_amount": variance, "match_status": match_status, "payable": payable}


async def force_match_invoice(db: AsyncSession, *, current: CurrentUser, invoice: VendorInvoice) -> Payable:
    """Overrides a discrepancy-flagged invoice and creates its payable anyway
    — a deliberate human decision (e.g. the vendor's invoice is correct and
    the GRN/PO data was the one that was wrong), gated at the API layer to
    Purchase Head/Finance Head/Super Admin only."""
    if invoice.status != "discrepancy_flagged":
        raise HTTPException(status_code=409, detail=f"Invoice is {invoice.status}, not flagged for discrepancy")
    existing_payable = (
        await db.execute(select(Payable).where(Payable.vendor_invoice_id == invoice.id))
    ).scalar_one_or_none()
    if existing_payable is not None:
        raise HTTPException(status_code=409, detail="Payable already exists for this invoice")

    vendor = await db.get(Vendor, invoice.vendor_id)
    credit_days = vendor.credit_days if vendor else 30
    payable = Payable(
        vendor_id=invoice.vendor_id,
        vendor_invoice_id=invoice.id,
        original_amount=invoice.invoice_amount,
        amount_due=invoice.invoice_amount,
        due_date=invoice.invoice_date + timedelta(days=credit_days),
        status="outstanding",
    )
    db.add(payable)
    invoice.status = "matched"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="vendor_invoice.match_overridden",
        entity_type="vendor_invoice",
        entity_id=invoice.id,
    )
    return payable
