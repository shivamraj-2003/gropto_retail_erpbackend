from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.models.models import Role, Store
from app.models.models_phase2 import Warehouse
from app.schemas.schemas_phase2 import RoleOut, StoreOut, WarehouseOut

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


@router.get("/warehouses", response_model=list[WarehouseOut])
async def list_warehouses(
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("inventory.view")),
) -> list[Warehouse]:
    result = await db.execute(select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.name))
    return list(result.scalars().all())
