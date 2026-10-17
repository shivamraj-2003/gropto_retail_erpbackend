"""Warehouse-to-store, store-to-store and store-to-warehouse transfers.

A transfer is a two-sided document with a life of its own:

  draft              saved for later; NOTHING has moved. Can be edited, sent or deleted.
  dispatched         sent: the sender's stock went down; the goods are on the way.
  partially_received some arrived and were counted in; the rest is still expected.
  received           everything arrived exactly as sent.
  discrepancy        closed, but something is short, extra or damaged and needs a decision.
  resolved           the decision was made and its stock effect applied.
  cancelled          called off while still on the way; the goods went back to the sender.

Stock only moves at three moments: when it is SENT (sender down), when goods are COUNTED IN (destination up by the
good units), and when a mismatch is SETTLED (see transfer_math.plan_resolution). A shortage therefore never just
vanishes: it stays on the transfer until someone writes it off or puts it back. Store legs use inventory_balances
(a movement ledger row each time); warehouse legs use warehouse_balances. A store destination's in_transit figure is
informational only ("how much is inbound"); it never feeds available stock.
"""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import Product, Store
from app.models.models_phase2 import Transfer, TransferItem, Warehouse
from app.schemas.schemas_phase2 import TransferCreate, TransferReceive
from app.services.approvals import approval_threshold, submit_or_apply
from app.services.audit import write_audit
from app.services.inventory import (
    adjust_damaged,
    adjust_in_transit,
    adjust_warehouse_balance,
    apply_movement,
    get_balance,
    get_warehouse_balance,
)
from app.services.transfer_math import EPS, LineQty, open_qty, plan_resolution, status_after_delivery

DISCREPANCY_APPROVAL_THRESHOLD = 20  # absolute units variance on any one line; above this needs review before closing

_REASON = {
    "return_to_source": "transfer_return",
    "reduce_sender": "transfer_count_correction",
    "send_back": "transfer_send_back",
}


async def _assert_place_exists(db: AsyncSession, kind: str, place_id) -> str:
    if kind == "store":
        place = await db.get(Store, place_id)
    elif kind == "warehouse":
        place = await db.get(Warehouse, place_id)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown place type {kind}")
    if place is None:
        raise HTTPException(status_code=404, detail=f"That {kind} was not found")
    if not getattr(place, "is_active", True):
        raise HTTPException(status_code=409, detail=f"{place.name} is switched off")
    return place.name


async def _validate(db: AsyncSession, payload: TransferCreate) -> None:
    if not payload.items:
        raise HTTPException(status_code=400, detail="Add at least one product to transfer")
    if payload.source_type == payload.dest_type and payload.source_id == payload.dest_id:
        raise HTTPException(status_code=400, detail="From and To are the same place — pick a different destination")
    await _assert_place_exists(db, payload.source_type, payload.source_id)
    await _assert_place_exists(db, payload.dest_type, payload.dest_id)
    seen: set = set()
    for item in payload.items:
        if item.dispatched_qty <= 0:
            raise HTTPException(status_code=400, detail="Every quantity must be more than 0")
        if item.product_id in seen:
            raise HTTPException(status_code=400, detail="The same product is listed twice — combine it into one line")
        seen.add(item.product_id)
        if await db.get(Product, item.product_id) is None:
            raise HTTPException(status_code=404, detail="A product in this transfer was not found")


async def _check_stock(db: AsyncSession, source_type: str, source_id, items: list[tuple]) -> None:
    """items: (product_id, quantity). The sender must actually have it."""
    for product_id, quantity in items:
        if source_type == "store":
            balance = await get_balance(db, product_id=product_id, store_id=source_id)
        elif source_type == "warehouse":
            balance = await get_warehouse_balance(db, product_id=product_id, warehouse_id=source_id)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown source_type {source_type}")
        if balance < quantity:
            product = await db.get(Product, product_id)
            raise HTTPException(
                status_code=409,
                detail=f"Not enough stock of {product.name} ({product.sku}): {balance:g} available, you are sending {quantity:g}",
            )


async def _take_stock_out(db: AsyncSession, transfer: Transfer, current: CurrentUser) -> None:
    """The moment of sending: the sender's stock goes down, and the destination store sees it as inbound."""
    for item in transfer.items:
        qty = float(item.dispatched_qty)
        if transfer.source_type == "store":
            await apply_movement(
                db,
                product_id=item.product_id,
                store_id=transfer.source_id,
                delta=-qty,
                reason_code="transfer_out",
                source_type="transfer_dispatch",
                source_id=transfer.id,
                created_by=current.user_id,
                device_id=current.device_id,
            )
        else:
            await adjust_warehouse_balance(db, product_id=item.product_id, warehouse_id=transfer.source_id, delta=-qty)
        if transfer.dest_type == "store":
            await adjust_in_transit(db, product_id=item.product_id, store_id=transfer.dest_id, delta=qty)


def _source_store(t: Transfer):
    return t.source_id if t.source_type == "store" else None


async def dispatch_transfer(db: AsyncSession, *, current: CurrentUser, payload: TransferCreate) -> Transfer:
    """Create a transfer. With `payload.draft` it is only saved (no stock moves); otherwise it is sent at once."""
    await _validate(db, payload)
    if not payload.draft:
        await _check_stock(db, payload.source_type, payload.source_id, [(i.product_id, i.dispatched_qty) for i in payload.items])

    transfer = Transfer(
        source_type=payload.source_type,
        source_id=payload.source_id,
        dest_type=payload.dest_type,
        dest_id=payload.dest_id,
        status="draft" if payload.draft else "dispatched",
        dispatched_by=current.user_id,
        note=(payload.note or "").strip() or None,
    )
    db.add(transfer)
    await db.flush()
    for item in payload.items:
        db.add(TransferItem(transfer_id=transfer.id, product_id=item.product_id, dispatched_qty=item.dispatched_qty))
    await db.flush()
    await db.refresh(transfer, attribute_names=["items"])

    if not payload.draft:
        await _take_stock_out(db, transfer, current)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=_source_store(transfer),
        device_id=current.device_id,
        action="transfer.drafted" if payload.draft else "transfer.dispatched",
        entity_type="transfer",
        entity_id=transfer.id,
        new_value={"dest_type": payload.dest_type, "dest_id": str(payload.dest_id)},
    )
    return transfer


async def update_draft(db: AsyncSession, *, current: CurrentUser, transfer: Transfer, payload: TransferCreate) -> Transfer:
    """Change a saved draft: places, products, quantities, note."""
    if transfer.status != "draft":
        raise HTTPException(status_code=409, detail="Only a saved draft can be edited — this one was already sent")
    await _validate(db, payload)
    transfer.source_type, transfer.source_id = payload.source_type, payload.source_id
    transfer.dest_type, transfer.dest_id = payload.dest_type, payload.dest_id
    transfer.note = (payload.note or "").strip() or None
    for old in list(transfer.items):
        transfer.items.remove(old)
    await db.flush()
    for item in payload.items:
        transfer.items.append(TransferItem(product_id=item.product_id, dispatched_qty=item.dispatched_qty))
    await db.flush()
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=_source_store(transfer), device_id=current.device_id,
        action="transfer.draft_edited", entity_type="transfer", entity_id=transfer.id,
    )
    return transfer


async def send_draft(db: AsyncSession, *, current: CurrentUser, transfer: Transfer) -> Transfer:
    """Send a saved draft: check the stock is there now, then take it out of the sender."""
    if transfer.status != "draft":
        raise HTTPException(status_code=409, detail="This transfer was already sent")
    await _assert_place_exists(db, transfer.source_type, transfer.source_id)
    await _assert_place_exists(db, transfer.dest_type, transfer.dest_id)
    await _check_stock(db, transfer.source_type, transfer.source_id, [(i.product_id, float(i.dispatched_qty)) for i in transfer.items])
    transfer.status = "dispatched"
    transfer.dispatched_by = current.user_id
    transfer.dispatched_at = datetime.now(timezone.utc)
    await _take_stock_out(db, transfer, current)
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=_source_store(transfer), device_id=current.device_id,
        action="transfer.dispatched", entity_type="transfer", entity_id=transfer.id,
        new_value={"dest_type": transfer.dest_type, "dest_id": str(transfer.dest_id), "from_draft": True},
    )
    return transfer


async def delete_draft(db: AsyncSession, *, current: CurrentUser, transfer: Transfer) -> None:
    if transfer.status != "draft":
        raise HTTPException(status_code=409, detail="Only a saved draft can be deleted — a sent transfer can be cancelled instead")
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=_source_store(transfer), device_id=current.device_id,
        action="transfer.draft_deleted", entity_type="transfer", entity_id=transfer.id,
    )
    await db.delete(transfer)


def _lines(transfer: Transfer) -> list[LineQty]:
    return [LineQty(float(i.dispatched_qty), float(i.received_qty or 0), float(i.damaged_qty or 0)) for i in transfer.items]


async def receive_transfer(db: AsyncSession, *, current: CurrentUser, transfer: Transfer, payload: TransferReceive) -> Transfer:
    """Count goods in. Can be done in several deliveries; each call adds what arrived THIS time.

    Good units go into the destination's stock; damaged units are recorded but not added to sellable stock. When
    nothing is missing the transfer closes by itself; with `close` the receiver says nothing more is coming, and
    whatever is still missing becomes a shortage to settle."""
    if transfer.status not in ("dispatched", "partially_received"):
        raise HTTPException(status_code=409, detail="Transfer is not awaiting receipt")

    items_by_id = {item.id: item for item in transfer.items}
    seen: set = set()
    arrivals = []
    for r in payload.items:
        item = items_by_id.get(r.transfer_item_id)
        if item is None:
            raise HTTPException(status_code=400, detail="Unknown transfer item")
        if r.transfer_item_id in seen:
            raise HTTPException(status_code=400, detail="The same product is listed twice")
        seen.add(r.transfer_item_id)
        if r.received_qty < 0 or r.damaged_qty < 0:
            raise HTTPException(status_code=400, detail="Quantities cannot be negative")
        if r.received_qty + r.damaged_qty > EPS:
            arrivals.append((item, float(r.received_qty), float(r.damaged_qty)))
    if not arrivals and not payload.close:
        raise HTTPException(status_code=400, detail="Enter what arrived, or close the transfer if nothing more is coming")

    for item, good, damaged in arrivals:
        before = float(item.received_qty or 0) + float(item.damaged_qty or 0)
        item.received_qty = float(item.received_qty or 0) + good
        item.damaged_qty = float(item.damaged_qty or 0) + damaged
        if transfer.dest_type == "store":
            if good > EPS:
                await apply_movement(
                    db,
                    product_id=item.product_id,
                    store_id=transfer.dest_id,
                    delta=good,
                    reason_code="transfer_in",
                    source_type="transfer_receipt",
                    source_id=uuid.uuid4(),  # one row per delivery, so a line can arrive in parts
                    created_by=current.user_id,
                    device_id=current.device_id,
                )
            if damaged > EPS:
                await adjust_damaged(db, product_id=item.product_id, store_id=transfer.dest_id, delta=damaged)
            # the inbound figure drops by what arrived, but never below what was announced for this line
            still_announced = max(float(item.dispatched_qty) - before, 0.0)
            await adjust_in_transit(db, product_id=item.product_id, store_id=transfer.dest_id, delta=-min(good + damaged, still_announced))
        elif good > EPS:
            await adjust_warehouse_balance(db, product_id=item.product_id, warehouse_id=transfer.dest_id, delta=good)

    lines = _lines(transfer)
    status = status_after_delivery(lines, close=payload.close)
    transfer.status = status
    transfer.received_by = current.user_id
    if status in ("received", "discrepancy"):
        transfer.received_at = datetime.now(timezone.utc)
        if transfer.dest_type == "store":
            # closed: what never arrived is no longer inbound
            for item in transfer.items:
                missing = open_qty(LineQty(float(item.dispatched_qty), float(item.received_qty or 0), float(item.damaged_qty or 0)))
                if missing > EPS:
                    await adjust_in_transit(db, product_id=item.product_id, store_id=transfer.dest_id, delta=-missing)

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=transfer.dest_id if transfer.dest_type == "store" else None,
        device_id=current.device_id,
        action="transfer.received",
        entity_type="transfer",
        entity_id=transfer.id,
        new_value={
            "status": status,
            "closed": payload.close,
            "this_delivery": [{"item": str(i.id), "good": g, "damaged": d} for i, g, d in arrivals],
        },
        reason=(payload.note or "").strip() or None,
    )
    return transfer


def _plan(transfer: Transfer, actions: dict[str, str]):
    lines = {str(i.id): LineQty(float(i.dispatched_qty), float(i.received_qty or 0), float(i.damaged_qty or 0)) for i in transfer.items}
    return plan_resolution(lines, actions)


async def _move_at(db: AsyncSession, transfer: Transfer, item: TransferItem, place: str, delta: float, reason: str, *, user_id, device_id) -> None:
    kind, place_id = (transfer.source_type, transfer.source_id) if place == "source" else (transfer.dest_type, transfer.dest_id)
    if kind == "store":
        await apply_movement(
            db,
            product_id=item.product_id,
            store_id=place_id,
            delta=delta,
            reason_code=reason,
            source_type="transfer_resolution",
            source_id=uuid.uuid4(),
            created_by=user_id,
            device_id=device_id,
        )
    else:
        await adjust_warehouse_balance(db, product_id=item.product_id, warehouse_id=place_id, delta=delta)


async def apply_resolution(
    db: AsyncSession, *, transfer: Transfer, actions: dict[str, str], note: str | None, user_id, role_code: str | None, device_id
) -> None:
    """Settle every short / extra / damaged line, with its stock effect, and close the transfer."""
    steps, errors = _plan(transfer, actions)
    if errors:
        raise HTTPException(status_code=409, detail="This transfer changed since it was checked — open it again")
    by_item = {s.item_id: s for s in steps}
    for item in transfer.items:
        step = by_item.get(str(item.id))
        if step is not None:
            if step.place is not None:
                await _move_at(db, transfer, item, step.place, step.delta, _REASON[step.action], user_id=user_id, device_id=device_id)
            item.settled_qty = step.qty
            item.resolution = step.action
        elif float(item.damaged_qty or 0) > EPS:
            item.resolution = "damaged"
    transfer.status = "resolved"
    await write_audit(
        db,
        user_id=user_id,
        role_code=role_code,
        store_id=transfer.dest_id if transfer.dest_type == "store" else None,
        device_id=device_id,
        action="transfer.discrepancy_resolved",
        entity_type="transfer",
        entity_id=transfer.id,
        new_value={"actions": actions},
        reason=note,
    )


async def resolve_discrepancy(
    db: AsyncSession, *, current: CurrentUser, transfer: Transfer, note: str | None, actions: dict[str, str] | None = None
) -> Transfer | dict:
    """Close a transfer that arrived short, extra or damaged, deciding what happens to each line:

      short  -> write_off (lost: no stock moves, the loss stays on the transfer)
                return_to_source (it never left or came back: the sender's stock goes up)
      extra  -> reduce_sender (the sender's figure was wrong: sender's stock goes down)
                send_back (the extra goes back: the receiver's stock goes down)
      damaged units need no choice; they stay recorded as a loss.

    A large variance on any line needs approval first (a Super Admin settles at once)."""
    if transfer.status != "discrepancy":
        raise HTTPException(status_code=409, detail=f"Transfer is {transfer.status}, not in discrepancy")
    actions = actions or {}
    _, errors = _plan(transfer, actions)
    if errors:
        names = {str(i.id): i for i in transfer.items}
        products = {}
        for item_id in errors:
            p = await db.get(Product, names[item_id].product_id)
            products[item_id] = p.name if p else "an item"
        raise HTTPException(status_code=400, detail="Choose what to do about: " + ", ".join(products.values()))

    max_variance = max(
        (max(abs(open_qty(l)), l.damaged) for l in _lines(transfer)),
        default=0.0,
    )
    if not current.is_super_admin and max_variance > await approval_threshold(db, "transfer_discrepancy_resolution", DISCREPANCY_APPROVAL_THRESHOLD):
        request = await submit_or_apply(
            db,
            current=current,
            request_type="transfer_discrepancy_resolution",
            entity_type="transfer",
            entity_id=transfer.id,
            old_value=None,
            new_value={"transfer_id": str(transfer.id), "note": note, "actions": actions},
            reason=note,
            store_id=transfer.dest_id if transfer.dest_type == "store" else None,
        )
        return {"approval_request_id": request.id, "status": request.status}

    await apply_resolution(
        db, transfer=transfer, actions=actions, note=note, user_id=current.user_id, role_code=current.role_code, device_id=current.device_id
    )
    return transfer


async def cancel_transfer(db: AsyncSession, *, current: CurrentUser, transfer: Transfer, reason: str | None) -> Transfer:
    """Called off before anything arrived: the goods go back to the sender and the inbound figure is cleared."""
    if transfer.status != "dispatched":
        raise HTTPException(
            status_code=409,
            detail=f"Only a transfer still on its way can be cancelled (this one is {transfer.status.replace('_', ' ')})",
        )
    for item in transfer.items:
        qty = float(item.dispatched_qty)
        if transfer.source_type == "store":
            await apply_movement(
                db,
                product_id=item.product_id,
                store_id=transfer.source_id,
                delta=qty,
                reason_code="transfer_cancelled",
                source_type="transfer_cancel",
                source_id=item.id,
                created_by=current.user_id,
                device_id=current.device_id,
            )
        else:
            await adjust_warehouse_balance(db, product_id=item.product_id, warehouse_id=transfer.source_id, delta=qty)
        if transfer.dest_type == "store":
            await adjust_in_transit(db, product_id=item.product_id, store_id=transfer.dest_id, delta=-qty)
    transfer.status = "cancelled"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=transfer.source_id if transfer.source_type == "store" else None,
        device_id=current.device_id,
        action="transfer.cancelled",
        entity_type="transfer",
        entity_id=transfer.id,
        reason=reason,
    )
    return transfer
