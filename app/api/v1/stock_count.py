import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import StockCount
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import StockCountCreate, StockCountOut, StockCountSubmitIn
from app.services.stock_count import finalize_count, initiate_count, submit_count_lines

router = APIRouter(prefix="/stock-counts", tags=["stock-counts"])


@router.get("", response_model=Page[StockCountOut])
async def list_stock_counts(
    store_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.stock_count.view")),
) -> Page[StockCountOut]:
    stmt = select(StockCount)
    if not current.sees_all_stores():
        stmt = stmt.where(StockCount.store_id.in_(current.store_ids))
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(StockCount.store_id == store_id)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(StockCount.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.get("/{count_id}", response_model=StockCountOut)
async def get_stock_count(
    count_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.stock_count.view")),
) -> StockCount:
    count = await db.get(StockCount, count_id)
    if count is None:
        raise HTTPException(status_code=404, detail="Stock count not found")
    require_store_access(count.store_id, current)
    return count


@router.post("", response_model=StockCountOut, status_code=201)
async def create_stock_count(
    payload: StockCountCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.stock_count.create")),
) -> StockCount:
    require_store_access(payload.store_id, current)
    count = await initiate_count(db, current=current, payload=payload)
    await db.commit()
    await db.refresh(count)
    return count


@router.patch("/{count_id}/lines", response_model=StockCountOut)
async def submit_stock_count_lines(
    count_id: uuid.UUID,
    payload: StockCountSubmitIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("inventory.stock_count.update")),
) -> StockCount:
    count = await db.get(StockCount, count_id)
    if count is None:
        raise HTTPException(status_code=404, detail="Stock count not found")
    require_store_access(count.store_id, current)
    count = await submit_count_lines(db, current=current, count=count, payload=payload)
    await db.commit()
    await db.refresh(count)
    return count


@router.post("/{count_id}/finalize")
async def finalize_stock_count(
    count_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    # Maker-checker: the finalizing call requires inventory.adjust (write
    # authority), distinct from inventory.view used to count — a plain
    # counter cannot also be the one who applies the adjustment.
    current: CurrentUser = Depends(require_permission("inventory.stock_count.close")),
) -> dict:
    count = await db.get(StockCount, count_id)
    if count is None:
        raise HTTPException(status_code=404, detail="Stock count not found")
    require_store_access(count.store_id, current)
    result = await finalize_count(db, current=current, count=count)
    await db.commit()
    if isinstance(result, dict):
        return {"approval_request_id": str(result["approval_request_id"]), "status": result["status"]}
    return {"status": result.status, "completed_at": result.completed_at.isoformat() if result.completed_at else None}
