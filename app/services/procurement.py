"""Procurement expansion (Phase 2): requisition -> PO (approval-gated) -> GRN
receiving with variance capture. Three-way match against the vendor invoice and
rate comparison across quotations are intentionally left thin here — this wires
the document flow and the stock effect, which is the structural piece other work
builds on."""


from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models_phase2 import Grn, GrnItem, PurchaseOrder, PurchaseOrderItem, PurchaseRequisition, PurchaseRequisitionItem
from app.schemas.schemas_phase2 import GrnCreate, PurchaseOrderCreate, RequisitionCreate
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.inventory import apply_movement


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
        purchase_order_id=payload.purchase_order_id,
        store_id=payload.store_id,
        received_by=current.user_id,
        status="accepted",
    )
    db.add(grn)
    await db.flush()

    for item in payload.items:
        db.add(
            GrnItem(
                grn_id=grn.id,
                product_id=item.product_id,
                expected_qty=item.expected_qty,
                received_qty=item.received_qty,
                batch_number=item.batch_number,
                expiry_date=item.expiry_date,
                qc_status=item.qc_status,
            )
        )
        if item.qc_status == "accepted" and payload.store_id:
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
