import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import Transfer
from app.models.models_phase4 import (
    InventoryBatch,
    PutawayTask,
    StoreIndent,
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
    PutawayTaskCreate,
    PutawayTaskOut,
    StoreIndentCreate,
    StoreIndentOut,
    WarehouseZoneLocationOut,
    WmsPickListTaskOut,
)
from app.services.transfers import dispatch_transfer

router = APIRouter(prefix="/wms", tags=["wms"])


@router.get("/batches", response_model=list[InventoryBatchOut])
async def list_batches(
    store_id: uuid.UUID | None = None,
    warehouse_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[InventoryBatch]:
    stmt = select(InventoryBatch).order_by(InventoryBatch.expiry_date.asc())
    if store_id:
        stmt = stmt.where(InventoryBatch.store_id == store_id)
    if warehouse_id:
        stmt = stmt.where(InventoryBatch.warehouse_id == warehouse_id)
    if product_id:
        stmt = stmt.where(InventoryBatch.product_id == product_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/locations", response_model=list[WarehouseZoneLocationOut])
async def list_locations(
    warehouse_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[WarehouseZoneLocation]:
    result = await db.execute(select(WarehouseZoneLocation).where(WarehouseZoneLocation.warehouse_id == warehouse_id))
    return list(result.scalars().all())


@router.post("/putaway", response_model=PutawayTaskOut, status_code=201)
async def create_putaway_task(
    payload: PutawayTaskCreate,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> PutawayTask:
    task = PutawayTask(**payload.model_dump())
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


@router.get("/indents", response_model=Page[StoreIndentOut])
async def list_indents(
    store_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> Page[StoreIndentOut]:
    stmt = select(StoreIndent)
    if store_id:
        stmt = stmt.where(StoreIndent.store_id == store_id)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(StoreIndent.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.post("/indents", response_model=StoreIndentOut, status_code=201)
async def create_indent(
    payload: StoreIndentCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> StoreIndent:
    require_store_access(payload.store_id, current)
    indent = StoreIndent(**payload.model_dump())
    db.add(indent)
    await db.commit()
    await db.refresh(indent)
    return indent


async def _fefo_suggestion(db: AsyncSession, warehouse_id: uuid.UUID, product_id: uuid.UUID) -> InventoryBatch | None:
    """First-Expiry-First-Out: the batch expiring soonest (nulls last, since a
    batch with no expiry date is never prioritised for depletion)."""
    stmt = (
        select(InventoryBatch)
        .where(InventoryBatch.warehouse_id == warehouse_id, InventoryBatch.product_id == product_id, InventoryBatch.quantity > 0)
        .order_by(InventoryBatch.expiry_date.asc().nulls_last())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


@router.post("/pick-lists", response_model=list[WmsPickListTaskOut], status_code=201)
async def create_pick_list(
    payload: PickListCreate,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> list[dict]:
    """Blueprint §5: "pick list, scan-based picking" + FEFO. Each requested
    line gets a real FEFO batch suggestion (soonest-expiring stock first) and
    a best-effort bin suggestion (the schema has no product→bin mapping yet,
    so this is the warehouse's first active location, not a per-batch one)."""
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
    await db.commit()
    return created


@router.get("/pick-lists", response_model=list[WmsPickListTaskOut])
async def list_pick_lists(
    warehouse_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[WmsPickListTask]:
    stmt = select(WmsPickListTask)
    if warehouse_id:
        stmt = stmt.where(WmsPickListTask.warehouse_id == warehouse_id)
    if status_filter:
        stmt = stmt.where(WmsPickListTask.status == status_filter)
    result = await db.execute(stmt.order_by(WmsPickListTask.created_at.desc()))
    return list(result.scalars().all())


@router.post("/pick-lists/{task_id}/pick", response_model=WmsPickListTaskOut)
async def confirm_pick(
    task_id: uuid.UUID,
    payload: PickConfirm,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> WmsPickListTask:
    """Scan-based picking confirmation. Depletes the FEFO batch by the picked
    quantity — real batch-level stock movement, not just a status flip."""
    task = await db.get(WmsPickListTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Pick list task not found")
    if payload.picked_qty <= 0 or payload.picked_qty > task.requested_qty:
        raise HTTPException(status_code=400, detail="picked_qty must be between 0 and requested_qty")

    remaining = payload.picked_qty
    batches = (
        await db.execute(
            select(InventoryBatch)
            .where(InventoryBatch.warehouse_id == task.warehouse_id, InventoryBatch.product_id == task.product_id, InventoryBatch.quantity > 0)
            .order_by(InventoryBatch.expiry_date.asc().nulls_last())
        )
    ).scalars().all()
    for batch in batches:
        if remaining <= 0:
            break
        take = min(float(batch.quantity), remaining)
        batch.quantity = float(batch.quantity) - take
        remaining -= take
    if remaining > 0:
        raise HTTPException(status_code=409, detail=f"Only {payload.picked_qty - remaining} units available across batches for this product")

    task.picked_qty = payload.picked_qty
    task.picker_id = current.user_id
    task.status = "picked" if payload.picked_qty >= task.requested_qty else "picking"
    await db.commit()
    await db.refresh(task)
    return task


@router.post("/pick-lists/{task_id}/pack", response_model=WmsPickListTaskOut)
async def pack_pick_list(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.adjust")),
) -> WmsPickListTask:
    task = await db.get(WmsPickListTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Pick list task not found")
    if task.status != "picked":
        raise HTTPException(status_code=400, detail="Task must be fully picked before it can be packed")
    task.status = "packed"
    await db.commit()
    await db.refresh(task)
    return task


@router.post("/pick-lists/dispatch")
async def dispatch_pick_lists(
    payload: PickListDispatch,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.adjust")),
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

    transfer_payload = TransferCreate(
        source_type="warehouse",
        source_id=next(iter(warehouse_ids)),
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
