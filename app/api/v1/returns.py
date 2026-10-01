import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import Return
from app.schemas.schemas import Page
from app.schemas.schemas_phase2 import ReturnCreate, ReturnLinkExchangeIn, ReturnOut
from app.services.returns import create_return, link_exchange

router = APIRouter(prefix="/returns", tags=["returns"])


@router.get("", response_model=Page[ReturnOut])
async def list_returns(
    store_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("sale.void")),
) -> Page[ReturnOut]:
    stmt = select(Return)
    if not current.sees_all_stores():
        stmt = stmt.where(Return.store_id.in_(current.store_ids))
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(Return.store_id == store_id)
    capped_limit = min(limit, 200)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(stmt.order_by(Return.created_at.desc()).limit(capped_limit).offset(offset))
    return Page(items=list(result.scalars().all()), total=total, limit=capped_limit, offset=offset)


@router.post("", response_model=ReturnOut, status_code=201)
async def submit_return(
    payload: ReturnCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("sale.void")),
) -> Return:
    require_store_access(payload.store_id, current)
    ret = await create_return(db, current=current, payload=payload)
    await db.commit()
    await db.refresh(ret)
    return ret


@router.post("/{return_id}/link-exchange", response_model=ReturnOut)
async def link_return_exchange(
    return_id: uuid.UUID,
    payload: ReturnLinkExchangeIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("sale.void")),
) -> Return:
    ret = await db.get(Return, return_id)
    if ret is None:
        raise HTTPException(status_code=404, detail="Return not found")
    require_store_access(ret.store_id, current)
    ret = await link_exchange(db, current=current, ret=ret, exchange_sale_id=payload.exchange_sale_id)
    await db.commit()
    await db.refresh(ret)
    return ret
