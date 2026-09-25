import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import Return
from app.schemas.schemas_phase2 import ReturnCreate, ReturnOut
from app.services.returns import create_return

router = APIRouter(prefix="/returns", tags=["returns"])


@router.get("", response_model=list[ReturnOut])
async def list_returns(
    store_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("sale.void")),
) -> list[Return]:
    stmt = select(Return)
    if current.role_code != "super_admin":
        stmt = stmt.where(Return.store_id.in_(current.store_ids))
    if store_id:
        require_store_access(store_id, current)
        stmt = stmt.where(Return.store_id == store_id)
    result = await db.execute(stmt.order_by(Return.created_at.desc()).limit(200))
    return list(result.scalars().all())


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
