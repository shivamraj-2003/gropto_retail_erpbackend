"""Procurement expansion (Phase 2): requisition -> PO (approval-gated) -> GRN
receiving with variance capture. Three-way match against the vendor invoice and
rate comparison across quotations are intentionally left thin here — this wires
the document flow and the stock effect, which is the structural piece other work
builds on."""


import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.api.deps import CurrentUser
from app.models.models import Product
from app.models.models_phase2 import Grn, GrnItem, PurchaseOrder, PurchaseOrderItem, PurchaseRequisition, PurchaseRequisitionItem
from app.models.models_phase4 import InventoryBatch, VendorDebitCreditNote
from app.schemas.schemas_phase2 import GrnCreate, PurchaseOrderCreate, RequisitionCreate
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.inventory import adjust_warehouse_balance, apply_movement


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


async def create_purchase_order(db: AsyncSession, *, current: CurrentUser, payload: PurchaseOrderCreate) -> PurchaseOrder:
    total = sum(item.quantity * item.unit_cost for item in payload.items)
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
        db.add(PurchaseOrderItem(purchase_order_id=po.id, product_id=item.product_id, quantity=item.quantity, unit_cost=item.unit_cost))
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
    if payload.purchase_order_id:
        po = await db.get(PurchaseOrder, payload.purchase_order_id)
        if po is None or po.status not in ("approved", "received"):
            raise HTTPException(status_code=409, detail="Purchase order not approved yet")

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
    po_item_costs: dict = {}
    po_vendor_id = None
    if payload.purchase_order_id:
        po_for_vendor = await db.get(PurchaseOrder, payload.purchase_order_id)
        po_vendor_id = po_for_vendor.vendor_id if po_for_vendor else None
    if payload.warehouse_id and payload.purchase_order_id:
        po_items = (
            await db.execute(select(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == payload.purchase_order_id))
        ).scalars().all()
        po_item_costs = {pi.product_id: float(pi.unit_cost) for pi in po_items}

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

    if payload.purchase_order_id:
        po = await db.get(PurchaseOrder, payload.purchase_order_id)
        po.status = "received"

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
