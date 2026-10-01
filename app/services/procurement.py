"""Procurement expansion (Phase 2): requisition -> PO (approval-gated) -> GRN
receiving with variance capture. Three-way match against the vendor invoice and
rate comparison across quotations are intentionally left thin here — this wires
the document flow and the stock effect, which is the structural piece other work
builds on."""


import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import Product
from app.models.models_phase2 import Grn, GrnItem, PurchaseOrder, PurchaseOrderItem, PurchaseRequisition, PurchaseRequisitionItem
from app.models.models_phase4 import InventoryBatch, VendorDebitCreditNote
from app.schemas.schemas_phase2 import GrnCreate, PurchaseOrderCreate, RequisitionCreate
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.inventory import adjust_damaged, adjust_warehouse_balance, apply_movement


def _generate_grn_number() -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"GRN-{today}-{secrets.randbelow(10**6):06d}"


async def create_requisition(db: AsyncSession, *, current: CurrentUser, payload: RequisitionCreate) -> PurchaseRequisition:
    req = PurchaseRequisition(store_id=payload.store_id, requested_by=current.user_id, status="submitted")
    db.add(req)
    await db.flush()
    for item in payload.items:
        db.add(PurchaseRequisitionItem(requisition_id=req.id, product_id=item.product_id, quantity=item.quantity))
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="requisition.submitted",
        entity_type="purchase_requisition",
        entity_id=req.id,
        new_value={"item_count": len(payload.items)},
    )
    return req


def _po_line_total(quantity: float, unit_cost: float, discount_amount: float, tax_rate: float) -> float:
    """Point 6 audit fix: PurchaseOrderItem previously had no discount or tax
    field at all — total_amount was a flat quantity*unit_cost sum that
    understated true vendor liability."""
    net = quantity * unit_cost - discount_amount
    return net + net * tax_rate / 100


async def create_purchase_order(db: AsyncSession, *, current: CurrentUser, payload: PurchaseOrderCreate) -> PurchaseOrder:
    total = sum(_po_line_total(item.quantity, item.unit_cost, item.discount_amount, item.tax_rate) for item in payload.items)
    po = PurchaseOrder(
        vendor_id=payload.vendor_id,
        store_id=payload.store_id,
        requisition_id=payload.requisition_id,
        total_amount=total,
        created_by=current.user_id,
        status="pending_approval",
    )
    db.add(po)
    await db.flush()
    for item in payload.items:
        db.add(
            PurchaseOrderItem(
                purchase_order_id=po.id,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_cost=item.unit_cost,
                discount_amount=item.discount_amount,
                tax_rate=item.tax_rate,
            )
        )
    await db.flush()

    # Multi-level PO approval through the existing approval engine — a Super Admin
    # creator approves in the same motion, per the generic engine's rule.
    await submit_or_apply(
        db,
        current=current,
        request_type="purchase_order_approval",
        entity_type="purchase_order",
        entity_id=po.id,
        old_value=None,
        new_value={"total_amount": float(total), "vendor_id": str(payload.vendor_id)},
        reason=None,
        store_id=payload.store_id,
    )
    return po


async def receive_grn(db: AsyncSession, *, current: CurrentUser, payload: GrnCreate) -> Grn:
    po = None
    po_items_by_product: dict = {}
    if payload.purchase_order_id:
        po = await db.get(PurchaseOrder, payload.purchase_order_id)
        if po is None or po.status not in ("approved", "partially_received"):
            raise HTTPException(status_code=409, detail="Purchase order not approved yet")
        po_items_by_product = {i.product_id: i for i in po.items}

        # Point 6 audit fix: previously zero validation existed that a GRN's
        # items/quantities had anything to do with the PO it claimed to
        # receive against — any product, any quantity, was accepted.
        for item in payload.items:
            po_item = po_items_by_product.get(item.product_id)
            if po_item is None:
                raise HTTPException(status_code=400, detail=f"Product {item.product_id} is not on purchase order {po.id}")
            cumulative = float(po_item.received_qty) + item.received_qty
            if cumulative > float(po_item.quantity) and not item.allow_over_receipt:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Receiving {item.received_qty} would bring total received to {cumulative}, "
                        f"exceeding the ordered {float(po_item.quantity)} for product {item.product_id}. "
                        f"Set allow_over_receipt to confirm this is intentional."
                    ),
                )

    grn = Grn(
        grn_number=_generate_grn_number(),
        purchase_order_id=payload.purchase_order_id,
        store_id=payload.store_id,
        warehouse_id=payload.warehouse_id,
        received_by=current.user_id,
        status="accepted",
    )
    db.add(grn)
    await db.flush()

    # Unit cost for a warehouse receipt's batch record: the PO line's agreed
    # price when this GRN is against a PO, else the product's own last
    # purchase price for an ad-hoc/no-PO warehouse receipt — GrnItem itself
    # carries no cost field (cost lives on the PO line, per the existing
    # store-side flow), so this mirrors that same source of truth rather than
    # inventing a new one.
    po_item_costs: dict = {pid: float(pi.unit_cost) for pid, pi in po_items_by_product.items()}
    po_vendor_id = po.vendor_id if po else None

    now = datetime.now(timezone.utc)
    for item in payload.items:
        grn_item = GrnItem(
            grn_id=grn.id,
            product_id=item.product_id,
            expected_qty=item.expected_qty,
            received_qty=item.received_qty,
            batch_number=item.batch_number,
            mfg_date=item.mfg_date,
            expiry_date=item.expiry_date,
            qc_status=item.qc_status,
            # Point 5 audit fix: QC used to have no distinct reviewer/timestamp
            # at all — this is still the same call as receiving (no separate
            # QC step exists yet), but at least records who/when attested it.
            qc_by=current.user_id,
            qc_at=now,
        )
        db.add(grn_item)
        await db.flush()

        # Point 6 audit fix: cumulative received quantity against the PO
        # line — the field that made over/under-receiving uncheckable.
        po_item = po_items_by_product.get(item.product_id)
        if po_item is not None:
            po_item.received_qty = float(po_item.received_qty) + item.received_qty

        # Point 5 audit fix: a rejected/damaged line previously left no trace
        # beyond the qc_status tag — nothing ever notified the vendor side.
        # Auto-generates a debit note (money owed back) for the lost value.
        if item.qc_status in ("rejected", "damaged") and po_vendor_id is not None:
            unit_cost = po_item_costs.get(item.product_id)
            if unit_cost is None:
                product = await db.get(Product, item.product_id)
                unit_cost = float(product.purchase_price) if product else 0.0
            db.add(
                VendorDebitCreditNote(
                    vendor_id=po_vendor_id,
                    grn_id=grn.id,
                    grn_item_id=grn_item.id,
                    note_type="debit_note",
                    amount=round(unit_cost * item.received_qty, 2),
                    reason=f"GRN {grn.grn_number}: {item.received_qty} units {item.qc_status} on receipt",
                    status="issued",
                )
            )

        # Point 7 audit fix: a damaged line received against a store (not a
        # warehouse) previously left no trace in inventory_balances at all —
        # now visible as real damaged stock, distinct from available.
        if item.qc_status == "damaged" and payload.store_id:
            await adjust_damaged(db, product_id=item.product_id, store_id=payload.store_id, delta=item.received_qty)

        if item.qc_status != "accepted":
            continue

        if payload.store_id:
            await apply_movement(
                db,
                product_id=item.product_id,
                store_id=payload.store_id,
                delta=item.received_qty,
                reason_code="grn_receipt",
                source_type="grn_item",
                source_id=grn.id,
                created_by=current.user_id,
                device_id=current.device_id,
            )
        elif payload.warehouse_id:
            # Point 2 audit fix: this branch previously didn't exist at all —
            # a warehouse-bound GRN silently moved no stock anywhere, and
            # inventory_batches (what WMS's FEFO picking reads from) had no
            # write path in the entire codebase. Both are now real.
            unit_cost = po_item_costs.get(item.product_id)
            if unit_cost is None:
                product = await db.get(Product, item.product_id)
                unit_cost = float(product.purchase_price) if product else 0.0
            db.add(
                InventoryBatch(
                    product_id=item.product_id,
                    warehouse_id=payload.warehouse_id,
                    batch_number=item.batch_number or f"GRN-{str(grn.id)[:8].upper()}",
                    mfg_date=item.mfg_date,
                    expiry_date=item.expiry_date,
                    quantity=item.received_qty,
                    purchase_cost=unit_cost,
                )
            )
            await adjust_warehouse_balance(
                db, product_id=item.product_id, warehouse_id=payload.warehouse_id, delta=item.received_qty
            )

    if po is not None:
        # Point 6 audit fix: this used to unconditionally set status =
        # "received" on the very first GRN against a PO, even a tiny partial
        # receipt. Now reflects the real cumulative state across every GRN
        # ever raised against this PO.
        fully_received = all(float(pi.received_qty) >= float(pi.quantity) for pi in po.items)
        any_received = any(float(pi.received_qty) > 0 for pi in po.items)
        po.status = "received" if fully_received else "partially_received" if any_received else po.status

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="grn.received",
        entity_type="grn",
        entity_id=grn.id,
        new_value={"item_count": len(payload.items)},
    )
    return grn


async def cancel_purchase_order(db: AsyncSession, *, current: CurrentUser, po: PurchaseOrder, reason: str | None) -> PurchaseOrder:
    """Point 6 audit fix: there was previously no PO cancel/edit endpoint at
    all — once created, a PO could never be formally cancelled, only ignored."""
    if po.status in ("received", "cancelled", "closed"):
        raise HTTPException(status_code=409, detail=f"Cannot cancel a PO that is {po.status}")
    if any(float(pi.received_qty) > 0 for pi in po.items):
        raise HTTPException(status_code=409, detail="Cannot cancel a PO that already has receipts against it")
    po.status = "cancelled"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=po.store_id,
        device_id=current.device_id,
        action="purchase_order.cancelled",
        entity_type="purchase_order",
        entity_id=po.id,
        reason=reason,
    )
    return po
