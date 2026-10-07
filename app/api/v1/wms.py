import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access, require_warehouse_access
from app.core.database import get_db
from app.models.models import Product
from app.models.models_phase2 import Warehouse
from app.models.models_phase2 import Transfer
from app.models.models_phase4 import (
    InventoryBatch,
    PutawayTask,
    StoreIndent,
    StoreIndentItem,
    WarehouseZoneLocation,
    WmsPickListTask,
)
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import TransferCreate, TransferItemIn
from app.schemas.schemas_phase4 import (
    InventoryBatchOut,
    PickConfirm,
    PickListCreate,
    PickListDispatch,
    PutawayTaskConfirm,
    PutawayTaskCreate,
    PutawayTaskOut,
    StoreIndentCreate,
    StoreIndentOut,
    WarehouseZoneLocationOut,
    WmsPickListTaskOut,
)
from app.services.audit import write_audit
from app.services.inventory import adjust_warehouse_balance, get_warehouse_balance
from app.services.transfers import dispatch_transfer

router = APIRouter(prefix="/wms", tags=["wms"])


@router.get("/batches", response_model=list[InventoryBatchOut])
async def list_batches(
    store_id: uuid.UUID | None = None,
    warehouse_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.batch.view")),
) -> list[InventoryBatch]:
    stmt = select(InventoryBatch).order_by(InventoryBatch.expiry_date.asc())
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(InventoryBatch.store_id == store_id)
    if warehouse_id:
        require_warehouse_access(warehouse_id, current)
        stmt = stmt.where(InventoryBatch.warehouse_id == warehouse_id)
    elif not store_id and current.access is not None and current.access.warehouse_ids and not current.is_super_admin:
        stmt = stmt.where(InventoryBatch.warehouse_id.in_(current.access.warehouse_ids))
    if product_id:
        stmt = stmt.where(InventoryBatch.product_id == product_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/locations", response_model=list[WarehouseZoneLocationOut])
async def list_locations(
    warehouse_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.location.view")),
) -> list[WarehouseZoneLocation]:
    require_warehouse_access(warehouse_id, current)
    result = await db.execute(select(WarehouseZoneLocation).where(WarehouseZoneLocation.warehouse_id == warehouse_id))
    return list(result.scalars().all())


@router.post("/putaway", response_model=PutawayTaskOut, status_code=201)
async def create_putaway_task(
    payload: PutawayTaskCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.putaway.create")),
) -> PutawayTask:
    task = PutawayTask(**payload.model_dump())
    db.add(task)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="putaway.created",
        entity_type="putaway_task",
        entity_id=task.id,
        new_value={"grn_id": str(payload.grn_id), "product_id": str(payload.product_id)},
    )
    await db.commit()
    await db.refresh(task)
    return task


@router.get("/putaway", response_model=list[PutawayTaskOut])
async def list_putaway_tasks(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("wms.putaway.view")),
) -> list[PutawayTask]:
    stmt = select(PutawayTask)
    if status_filter:
        stmt = stmt.where(PutawayTask.status == status_filter)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/putaway/{task_id}/confirm", response_model=PutawayTaskOut)
async def confirm_putaway_task(
    task_id: uuid.UUID,
    payload: PutawayTaskConfirm,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.putaway.update")),
) -> PutawayTask:
    """Point 5 audit fix: there was previously no way to ever complete a
    putaway task — this confirms the location and actually moves the batch
    there, so zone/rack/bin becomes real placement instead of a disconnected
    reference table."""
    task = await db.get(PutawayTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Putaway task not found")
    if task.status != "pending":
        raise HTTPException(status_code=409, detail=f"Putaway task is {task.status}, not pending")

    location = await db.get(WarehouseZoneLocation, payload.confirmed_location_id)
    if location is None or not location.is_active:
        raise HTTPException(status_code=400, detail="Confirmed location is not a valid active location")
    require_warehouse_access(location.warehouse_id, current)

    batches = (
        await db.execute(
            select(InventoryBatch).where(
                InventoryBatch.product_id == task.product_id,
                InventoryBatch.location_id.is_(None),
            )
        )
    ).scalars().all()
    for batch in batches:
        batch.location_id = location.id

    task.confirmed_location_id = location.id
    task.status = "completed"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="putaway.confirmed",
        entity_type="putaway_task",
        entity_id=task.id,
        new_value={"confirmed_location_id": str(location.id), "batches_placed": len(batches)},
    )
    await db.commit()
    await db.refresh(task)
    return task


@router.get("/indents", response_model=Page[StoreIndentOut])
async def list_indents(
    store_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.indent.view")),
) -> Page[StoreIndentOut]:
    stmt = select(StoreIndent)
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(StoreIndent.store_id == store_id)
    elif not current.sees_all_stores() and not current.permissions & {"wms.pick.create", "wms.indent.transfer"}:
        # Store-side users see their own stores' indents; warehouse-side
        # fulfilment roles keep seeing the full queue they work from.
        stmt = stmt.where(StoreIndent.store_id.in_(current.store_ids))
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(StoreIndent.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.post("/indents", response_model=StoreIndentOut, status_code=201)
async def create_indent(
    payload: StoreIndentCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.indent.create")),
) -> StoreIndent:
    """Point 5 audit fix: an indent used to be a bare header row with no way
    to say what or how much was being requested — now carries real line
    items, same pattern as PurchaseRequisition."""
    require_store_access(payload.store_id, current)
    if not payload.items:
        raise HTTPException(status_code=400, detail="An indent needs at least one line item")
    indent = StoreIndent(
        store_id=payload.store_id,
        warehouse_id=payload.warehouse_id,
        requested_by=current.user_id,
        priority=payload.priority,
        reason=payload.reason,
        status="submitted",
    )
    db.add(indent)
    await db.flush()
    for item in payload.items:
        db.add(StoreIndentItem(indent_id=indent.id, product_id=item.product_id, quantity=item.quantity))
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="indent.submitted",
        entity_type="store_indent",
        entity_id=indent.id,
        new_value={"line_count": len(payload.items), "priority": payload.priority},
    )
    await db.commit()
    await db.refresh(indent)
    return indent


@router.post("/indents/{indent_id}/convert-to-transfer")
async def convert_indent_to_transfer(
    indent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.indent.transfer")),
) -> dict:
    """Point 5 audit fix: 'converted_to_transfer' was a documented status
    value nothing ever actually set — this is the real conversion, building a
    warehouse-to-store Transfer (dispatch_transfer) from the indent's lines."""
    indent = await db.get(StoreIndent, indent_id)
    if indent is None:
        raise HTTPException(status_code=404, detail="Indent not found")
    if indent.status != "submitted":
        raise HTTPException(status_code=409, detail=f"Indent is {indent.status}, not submitted")
    require_store_access(indent.store_id, current)

    transfer_payload = TransferCreate(
        source_type="warehouse",
        source_id=indent.warehouse_id,
        dest_type="store",
        dest_id=indent.store_id,
        items=[TransferItemIn(product_id=i.product_id, dispatched_qty=float(i.quantity)) for i in indent.items],
    )
    transfer = await dispatch_transfer(db, current=current, payload=transfer_payload)
    indent.status = "converted_to_transfer"
    indent.transfer_id = transfer.id
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=indent.store_id,
        device_id=current.device_id,
        action="indent.converted_to_transfer",
        entity_type="store_indent",
        entity_id=indent.id,
        new_value={"transfer_id": str(transfer.id)},
    )
    await db.commit()
    return {"transfer_id": str(transfer.id), "status": indent.status}


async def _fefo_suggestion(db: AsyncSession, warehouse_id: uuid.UUID, product_id: uuid.UUID) -> InventoryBatch | None:
    """FEFO primary, FIFO fallback (Point 5 audit fix): the batch expiring
    soonest wins; among batches with no expiry date at all (nulls last),
    the oldest-received one wins instead of being left effectively
    unordered."""
    stmt = (
        select(InventoryBatch)
        .where(InventoryBatch.warehouse_id == warehouse_id, InventoryBatch.product_id == product_id, InventoryBatch.quantity > 0)
        .order_by(InventoryBatch.expiry_date.asc().nulls_last(), InventoryBatch.created_at.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


# A pick depletes batches immediately but the warehouse balance only at
# dispatch, so "picked but not yet dispatched" sits in the balance and not in
# the batches. Open pick tasks reserve what they still have to pick.
_PICKED_NOT_DISPATCHED = ("picking", "picked", "packed")
_STILL_TO_PICK = ("pending", "picking")


async def _stock_position(db: AsyncSession, warehouse_id: uuid.UUID, product_id: uuid.UUID) -> dict:
    """Unpicked stock physically in the warehouse, and how much of it open
    pick lists already claim. The two stock records (warehouse_balances and
    inventory_batches) are both kept by GRN, but transfers only touch the
    balance and some older data only has batches — so on-hand is the larger
    of the two views rather than trusting either one alone."""
    batch_qty = float(
        (
            await db.execute(
                select(func.coalesce(func.sum(InventoryBatch.quantity), 0)).where(
                    InventoryBatch.warehouse_id == warehouse_id,
                    InventoryBatch.product_id == product_id,
                    InventoryBatch.quantity > 0,
                )
            )
        ).scalar_one()
    )
    balance = await get_warehouse_balance(db, product_id=product_id, warehouse_id=warehouse_id)
    picked_open = float(
        (
            await db.execute(
                select(func.coalesce(func.sum(WmsPickListTask.picked_qty), 0)).where(
                    WmsPickListTask.warehouse_id == warehouse_id,
                    WmsPickListTask.product_id == product_id,
                    WmsPickListTask.status.in_(_PICKED_NOT_DISPATCHED),
                )
            )
        ).scalar_one()
    )
    reserved = float(
        (
            await db.execute(
                select(func.coalesce(func.sum(WmsPickListTask.requested_qty - WmsPickListTask.picked_qty), 0)).where(
                    WmsPickListTask.warehouse_id == warehouse_id,
                    WmsPickListTask.product_id == product_id,
                    WmsPickListTask.status.in_(_STILL_TO_PICK),
                )
            )
        ).scalar_one()
    )
    on_hand = max(float(balance) - picked_open, batch_qty, 0.0)
    return {"on_hand": on_hand, "reserved": reserved, "free": max(on_hand - reserved, 0.0)}


async def _names(db: AsyncSession, warehouse_id: uuid.UUID, product_id: uuid.UUID) -> tuple[str, str]:
    product = await db.get(Product, product_id)
    warehouse = await db.get(Warehouse, warehouse_id)
    return (product.name if product else str(product_id)), (warehouse.code if warehouse else str(warehouse_id))


def _fmt(qty: float) -> str:
    return f"{qty:g}"


@router.post("/pick-lists", response_model=list[WmsPickListTaskOut], status_code=201)
async def create_pick_list(
    payload: PickListCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.pick.create")),
) -> list[dict]:
    """Blueprint §5: "pick list, scan-based picking" + FEFO. Each requested
    line gets a real FEFO batch suggestion (soonest-expiring stock first) and
    a best-effort bin suggestion (the schema has no product→bin mapping yet,
    so this is the warehouse's first active location, not a per-batch one)."""
    for line in payload.lines:
        require_warehouse_access(line.warehouse_id, current)
    # Stock check up front: a pick list for stock that isn't there only fails
    # later, at the picker's scanner. Lines for the same product in one
    # request add up.
    requested: dict[tuple[uuid.UUID, uuid.UUID], float] = {}
    for line in payload.lines:
        k = (line.warehouse_id, line.product_id)
        requested[k] = requested.get(k, 0.0) + float(line.requested_qty)
    for (warehouse_id, product_id), qty in requested.items():
        pos = await _stock_position(db, warehouse_id, product_id)
        if qty > pos["free"]:
            name, wh = await _names(db, warehouse_id, product_id)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Not enough {name} in {wh}: requested {_fmt(qty)}, free to pick {_fmt(pos['free'])} "
                    f"({_fmt(pos['on_hand'])} on hand, {_fmt(pos['reserved'])} already on open pick lists). "
                    "Receive stock (GRN or transfer) into this warehouse first."
                ),
            )
    created: list[dict] = []
    for line in payload.lines:
        location = (
            await db.execute(
                select(WarehouseZoneLocation)
                .where(WarehouseZoneLocation.warehouse_id == line.warehouse_id, WarehouseZoneLocation.is_active.is_(True))
                .limit(1)
            )
        ).scalars().first()
        fefo_batch = await _fefo_suggestion(db, line.warehouse_id, line.product_id)
        task = WmsPickListTask(
            transfer_order_id=line.transfer_order_id,
            warehouse_id=line.warehouse_id,
            product_id=line.product_id,
            zone_location_id=location.id if location else None,
            requested_qty=line.requested_qty,
            status="pending",
        )
        db.add(task)
        await db.flush()
        created.append(
            {
                **{c.name: getattr(task, c.name) for c in task.__table__.columns},
                "fefo_batch_number": fefo_batch.batch_number if fefo_batch else None,
                "fefo_expiry_date": fefo_batch.expiry_date if fefo_batch else None,
            }
        )
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="pick_list.created",
        entity_type="wms_pick_list_task",
        entity_id=None,
        new_value={"line_count": len(payload.lines)},
    )
    await db.commit()
    return created


@router.get("/pick-lists", response_model=list[WmsPickListTaskOut])
async def list_pick_lists(
    warehouse_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.pick.view")),
) -> list[dict]:
    stmt = select(WmsPickListTask)
    if warehouse_id:
        require_warehouse_access(warehouse_id, current)
        stmt = stmt.where(WmsPickListTask.warehouse_id == warehouse_id)
    elif current.access is not None and current.access.warehouse_ids and not current.is_super_admin:
        stmt = stmt.where(WmsPickListTask.warehouse_id.in_(current.access.warehouse_ids))
    if status_filter:
        stmt = stmt.where(WmsPickListTask.status == status_filter)
    tasks = list((await db.execute(stmt.order_by(WmsPickListTask.created_at.desc()))).scalars().all())
    positions: dict[tuple[uuid.UUID, uuid.UUID], dict] = {}
    out: list[dict] = []
    for t in tasks:
        k = (t.warehouse_id, t.product_id)
        if t.status in _STILL_TO_PICK and k not in positions:
            positions[k] = await _stock_position(db, *k)
        row = {c.name: getattr(t, c.name) for c in t.__table__.columns}
        row["available_qty"] = positions[k]["on_hand"] if k in positions else None
        out.append(row)
    return out


@router.post("/pick-lists/{task_id}/pick", response_model=WmsPickListTaskOut)
async def confirm_pick(
    task_id: uuid.UUID,
    payload: PickConfirm,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.pick.update")),
) -> WmsPickListTask:
    """Scan-based picking confirmation. Depletes the FEFO/FIFO batch by the
    picked quantity — real batch-level stock movement, not just a status
    flip. Point 5 audit fix: when the caller supplies scanned_barcode/
    scanned_location_id, both are now verified against the task before the
    pick is accepted — previously picked_qty was the only input, so any
    quantity against any task_id was accepted with zero integrity check."""
    task = await db.get(WmsPickListTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Pick list task not found")
    require_warehouse_access(task.warehouse_id, current)
    if payload.picked_qty <= 0 or payload.picked_qty > task.requested_qty:
        raise HTTPException(status_code=400, detail="picked_qty must be between 0 and requested_qty")

    if payload.scanned_barcode is not None:
        product = await db.get(Product, task.product_id)
        if product is None or product.barcode != payload.scanned_barcode:
            raise HTTPException(status_code=409, detail="Scanned barcode does not match this pick task's product")

    if payload.scanned_location_id is not None:
        if task.zone_location_id is None or payload.scanned_location_id != task.zone_location_id:
            raise HTTPException(status_code=409, detail="Scanned location does not match this pick task's assigned location")

    # Only the increase over what was already picked moves stock — re-confirming
    # a partial pick used to deplete the batches a second time.
    already = float(task.picked_qty or 0)
    delta = float(payload.picked_qty) - already
    if delta < 0:
        raise HTTPException(status_code=400, detail=f"Already picked {_fmt(already)}; picked quantity can't go down")
    if delta > 0:
        pos = await _stock_position(db, task.warehouse_id, task.product_id)
        if delta > pos["on_hand"]:
            name, wh = await _names(db, task.warehouse_id, task.product_id)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Only {_fmt(pos['on_hand'])} units of {name} in stock at {wh} — can't pick {_fmt(delta)}. "
                    "Receive stock into this warehouse, or pick what's there and cancel the rest."
                ),
            )
        # FEFO: soonest-expiring batches first. Stock that arrived without
        # batch records (transfers) has nothing to deplete here — it's covered
        # by the balance, which dispatch decrements.
        remaining = delta
        batches = (
            await db.execute(
                select(InventoryBatch)
                .where(InventoryBatch.warehouse_id == task.warehouse_id, InventoryBatch.product_id == task.product_id, InventoryBatch.quantity > 0)
                .order_by(InventoryBatch.expiry_date.asc().nulls_last(), InventoryBatch.created_at.asc())
            )
        ).scalars().all()
        for batch in batches:
            if remaining <= 0:
                break
            take = min(float(batch.quantity), remaining)
            batch.quantity = float(batch.quantity) - take
            remaining -= take

    task.picked_qty = payload.picked_qty
    task.picker_id = current.user_id
    task.status = "picked" if payload.picked_qty >= task.requested_qty else "picking"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="pick_list.picked",
        entity_type="wms_pick_list_task",
        entity_id=task.id,
        new_value={"picked_qty": payload.picked_qty, "scan_verified": payload.scanned_barcode is not None},
    )
    await db.commit()
    await db.refresh(task)
    return task


@router.post("/pick-lists/{task_id}/pack", response_model=WmsPickListTaskOut)
async def pack_pick_list(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.pack.update")),
) -> WmsPickListTask:
    task = await db.get(WmsPickListTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Pick list task not found")
    require_warehouse_access(task.warehouse_id, current)
    if task.status != "picked":
        raise HTTPException(status_code=400, detail="Task must be fully picked before it can be packed")
    task.status = "packed"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="pick_list.packed",
        entity_type="wms_pick_list_task",
        entity_id=task.id,
    )
    await db.commit()
    await db.refresh(task)
    return task


@router.post("/pick-lists/dispatch")
async def dispatch_pick_lists(
    payload: PickListDispatch,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("wms.dispatch.transfer")),
) -> dict:
    """Closes the Warehouse-to-Store loop (blueprint §16): every packed pick-
    list task becomes one real Transfer document via the existing
    dispatch_transfer() service — no separate, disconnected WMS-only dispatch
    record."""
    tasks = (
        await db.execute(select(WmsPickListTask).where(WmsPickListTask.id.in_(payload.pick_list_ids)))
    ).scalars().all()
    if not tasks:
        raise HTTPException(status_code=404, detail="No matching pick list tasks")
    if any(t.status != "packed" for t in tasks):
        raise HTTPException(status_code=400, detail="All pick list tasks must be packed before dispatch")
    warehouse_ids = {t.warehouse_id for t in tasks}
    if len(warehouse_ids) > 1:
        raise HTTPException(status_code=400, detail="All pick list tasks in one dispatch must share the same warehouse")
    require_warehouse_access(next(iter(warehouse_ids)), current)

    # Picks were validated against batches too, so stock that existed only as
    # batches (balance never updated) is real. Bring the balance up to what
    # was picked — audited — so dispatch doesn't refuse stock already in the box.
    warehouse_id = next(iter(warehouse_ids))
    needed: dict[uuid.UUID, float] = {}
    for t in tasks:
        needed[t.product_id] = needed.get(t.product_id, 0.0) + float(t.picked_qty)
    for product_id, qty in needed.items():
        balance = await get_warehouse_balance(db, product_id=product_id, warehouse_id=warehouse_id)
        if balance < qty:
            await adjust_warehouse_balance(db, product_id=product_id, warehouse_id=warehouse_id, delta=qty - float(balance))
            await write_audit(
                db,
                user_id=current.user_id,
                role_code=current.role_code,
                store_id=None,
                device_id=current.device_id,
                action="warehouse_balance.reconciled_from_batches",
                entity_type="warehouse_balance",
                entity_id=product_id,
                old_value={"warehouse_id": str(warehouse_id), "quantity": float(balance)},
                new_value={"warehouse_id": str(warehouse_id), "quantity": qty},
                reason="Picked stock existed as batches but the warehouse balance lagged behind",
            )

    transfer_payload = TransferCreate(
        source_type="warehouse",
        source_id=warehouse_id,
        dest_type=payload.dest_type,
        dest_id=payload.dest_id,
        items=[TransferItemIn(product_id=t.product_id, dispatched_qty=float(t.picked_qty)) for t in tasks],
    )
    transfer = await dispatch_transfer(db, current=current, payload=transfer_payload)
    for t in tasks:
        t.status = "dispatched"
        t.transfer_order_id = transfer.id
    await db.commit()
    return {"transfer_id": str(transfer.id), "dispatched_tasks": len(tasks)}
