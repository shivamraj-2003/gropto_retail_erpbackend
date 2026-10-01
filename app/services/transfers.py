"""Warehouse-to-store, store-to-store and store-to-warehouse transfers (Phase 2).
Two-sided document: dispatch decrements the source, receive increments the
destination — correctness never depended on an in_transit bucket (the source
is already gone and the destination isn't credited until receipt). Point 7
audit fix: a store destination's InventoryBalance.in_transit is now also
bumped at dispatch and cleared at receipt, purely so "how much is inbound to
this store" is queryable — it never feeds available-stock math. Store legs
use the Phase 1 inventory_balances table; warehouse legs use warehouse_balances
(see services/inventory.py's adjust_warehouse_balance) — both sides actually
move a balance, not just record the transfer document.
"""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models_phase2 import Transfer, TransferItem
from app.schemas.schemas_phase2 import TransferCreate, TransferReceive
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit
from app.services.inventory import adjust_in_transit, adjust_warehouse_balance, apply_movement, get_balance, get_warehouse_balance

DISCREPANCY_APPROVAL_THRESHOLD = 20  # absolute units variance on any one line; above this needs review before closing


async def dispatch_transfer(db: AsyncSession, *, current: CurrentUser, payload: TransferCreate) -> Transfer:
    for item in payload.items:
        if payload.source_type == "store":
            balance = await get_balance(db, product_id=item.product_id, store_id=payload.source_id)
        elif payload.source_type == "warehouse":
            balance = await get_warehouse_balance(db, product_id=item.product_id, warehouse_id=payload.source_id)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown source_type {payload.source_type}")
        if balance < item.dispatched_qty:
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient stock for product {item.product_id}: have {balance}, dispatching {item.dispatched_qty}",
            )

    transfer = Transfer(
        source_type=payload.source_type,
        source_id=payload.source_id,
        dest_type=payload.dest_type,
        dest_id=payload.dest_id,
        status="dispatched",
        dispatched_by=current.user_id,
    )
    db.add(transfer)
    await db.flush()

    for item in payload.items:
        db.add(TransferItem(transfer_id=transfer.id, product_id=item.product_id, dispatched_qty=item.dispatched_qty))
        if payload.source_type == "store":
            await apply_movement(
                db,
                product_id=item.product_id,
                store_id=payload.source_id,
                delta=-item.dispatched_qty,
                reason_code="transfer_out",
                source_type="transfer_dispatch",
                source_id=transfer.id,
                created_by=current.user_id,
                device_id=current.device_id,
            )
        else:
            await adjust_warehouse_balance(
                db, product_id=item.product_id, warehouse_id=payload.source_id, delta=-item.dispatched_qty
            )
        if payload.dest_type == "store":
            await adjust_in_transit(db, product_id=item.product_id, store_id=payload.dest_id, delta=item.dispatched_qty)

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.source_id if payload.source_type == "store" else None,
        device_id=current.device_id,
        action="transfer.dispatched",
        entity_type="transfer",
        entity_id=transfer.id,
        new_value={"dest_type": payload.dest_type, "dest_id": str(payload.dest_id)},
    )
    return transfer


async def receive_transfer(db: AsyncSession, *, current: CurrentUser, transfer: Transfer, payload: TransferReceive) -> Transfer:
    if transfer.status != "dispatched":
        raise HTTPException(status_code=409, detail="Transfer is not awaiting receipt")

    discrepancy = False
    items_by_id = {item.id: item for item in transfer.items}
    for received in payload.items:
        item = items_by_id.get(received.transfer_item_id)
        if item is None:
            raise HTTPException(status_code=400, detail="Unknown transfer item")
        item.received_qty = received.received_qty
        if received.received_qty != item.dispatched_qty:
            discrepancy = True
        if transfer.dest_type == "store":
            await apply_movement(
                db,
                product_id=item.product_id,
                store_id=transfer.dest_id,
                delta=received.received_qty,
                reason_code="transfer_in",
                source_type="transfer_receipt",
                source_id=item.id,
                created_by=current.user_id,
                device_id=current.device_id,
            )
        else:
            await adjust_warehouse_balance(
                db, product_id=item.product_id, warehouse_id=transfer.dest_id, delta=received.received_qty
            )
        if transfer.dest_type == "store":
            # Clears the full dispatched amount, not just what was actually
            # received — the entire dispatched quantity has left the pipeline
            # either way; any shortfall is the discrepancy captured below.
            await adjust_in_transit(db, product_id=item.product_id, store_id=transfer.dest_id, delta=-float(item.dispatched_qty))

    transfer.status = "discrepancy" if discrepancy else "received"
    transfer.received_by = current.user_id
    transfer.received_at = datetime.now(timezone.utc)

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=transfer.dest_id if transfer.dest_type == "store" else None,
        device_id=current.device_id,
        action="transfer.received",
        entity_type="transfer",
        entity_id=transfer.id,
        new_value={"status": transfer.status},
    )
    return transfer


async def resolve_discrepancy(db: AsyncSession, *, current: CurrentUser, transfer: Transfer, note: str | None) -> Transfer | dict:
    """Point 5 audit fix: a transfer left at status="discrepancy" previously
    had no further resolution path at all — this closes it out, routing
    through the approval engine when any single line's variance is large
    (same escalation pattern Returns/Stock Count already use), direct-closing
    otherwise."""
    if transfer.status != "discrepancy":
        raise HTTPException(status_code=409, detail=f"Transfer is {transfer.status}, not in discrepancy")

    max_variance = max(
        (abs(float(i.dispatched_qty) - float(i.received_qty or 0)) for i in transfer.items), default=0.0
    )
    if max_variance > DISCREPANCY_APPROVAL_THRESHOLD and current.role_code != "super_admin":
        request = await submit_or_apply(
            db,
            current=current,
            request_type="transfer_discrepancy_resolution",
            entity_type="transfer",
            entity_id=transfer.id,
            old_value=None,
            new_value={"transfer_id": str(transfer.id), "note": note},
            reason=note,
            store_id=transfer.dest_id if transfer.dest_type == "store" else None,
        )
        return {"approval_request_id": request.id, "status": request.status}

    transfer.status = "resolved"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=transfer.dest_id if transfer.dest_type == "store" else None,
        device_id=current.device_id,
        action="transfer.discrepancy_resolved",
        entity_type="transfer",
        entity_id=transfer.id,
        reason=note,
    )
    return transfer
