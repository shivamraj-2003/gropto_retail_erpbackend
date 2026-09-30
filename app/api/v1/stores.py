import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_admin_or_super, require_permission
from app.core.database import get_db
from app.models.models import Role, Store
from app.models.models_phase2 import Warehouse
from app.schemas.schemas_phase2 import (
    RoleOut,
    StoreCreateIn,
    StoreCreateResult,
    StoreOut,
    StoreUpdateIn,
    StoreUpdateResult,
    WarehouseOut,
)
from app.services.approvals import submit_or_apply

router = APIRouter(tags=["stores"])


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[Role]:
    """Lets the frontend read discount limits (and any other role metadata) from
    the single source of truth instead of a hand-maintained duplicate constant —
    closes the gap noted in frontend/shared/roleLimits.ts."""
    result = await db.execute(select(Role).order_by(Role.name))
    return list(result.scalars().all())


@router.get("/stores", response_model=list[StoreOut])
async def list_stores(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[Store]:
    """Every store, active or not filtered here — needed for cross-store pickers
    like the transfer destination selector. No store-scoping check: seeing the
    store list (name/code) isn't sensitive the way its transactions are."""
    result = await db.execute(select(Store).where(Store.is_active.is_(True)).order_by(Store.name))
    return list(result.scalars().all())


@router.post("/stores", response_model=StoreCreateResult, status_code=201)
async def create_store(
    payload: StoreCreateIn,
    current: CurrentUser = Depends(require_admin_or_super),
    db: AsyncSession = Depends(get_db),
) -> StoreCreateResult:
    """Onboards a new franchise location. Super Admin's call applies
    immediately; an Admin's call queues for Super Admin approval — same
    engine every other sensitive change in this app goes through."""
    existing = await db.execute(select(Store).where(Store.code == payload.code))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"Store code '{payload.code}' is already in use")

    new_store_id = uuid.uuid4()
    new_value = {
        "store_id": str(new_store_id),
        "code": payload.code,
        "name": payload.name,
        "city": payload.city,
        "cluster": payload.cluster,
    }
    request = await submit_or_apply(
        db,
        current=current,
        request_type="store_create",
        entity_type="store",
        entity_id=new_store_id,
        old_value=None,
        new_value=new_value,
        reason=f"New store: {payload.name} ({payload.code})",
        store_id=None,
    )
    await db.commit()

    if request.status == "approved":
        return StoreCreateResult(status="created", store_id=new_store_id)
    return StoreCreateResult(status="pending_approval", request_id=request.id)


@router.put("/stores/{store_id}", response_model=StoreUpdateResult)
@router.patch("/stores/{store_id}", response_model=StoreUpdateResult)
async def update_store(
    store_id: uuid.UUID,
    payload: StoreUpdateIn,
    current: CurrentUser = Depends(require_admin_or_super),
    db: AsyncSession = Depends(get_db),
) -> StoreUpdateResult:
    if payload.name is None and payload.city is None and payload.cluster is None:
        raise HTTPException(status_code=400, detail="Provide at least one field to change")

    store = await db.get(Store, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")

    old_value = {"name": store.name, "city": store.city, "cluster": store.cluster}
    new_value: dict = {}
    if payload.name is not None:
        new_value["name"] = payload.name
    if payload.city is not None:
        new_value["city"] = payload.city
    if payload.cluster is not None:
        new_value["cluster"] = payload.cluster

    request = await submit_or_apply(
        db,
        current=current,
        request_type="store_update",
        entity_type="store",
        entity_id=store_id,
        old_value=old_value,
        new_value=new_value,
        reason=f"Updated store {store.code}",
        store_id=store_id,
    )
    await db.commit()

    if request.status == "approved":
        return StoreUpdateResult(status="updated")
    return StoreUpdateResult(status="pending_approval", request_id=request.id)


@router.get("/warehouses", response_model=list[WarehouseOut])
async def list_warehouses(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[Warehouse]:
    result = await db.execute(select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.name))
    return list(result.scalars().all())
