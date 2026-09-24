from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.schemas.schemas_phase2 import ReturnCreate, ReturnOut
from app.services.returns import create_return
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/returns", tags=["returns"])


@router.post("", response_model=ReturnOut, status_code=201)
async def submit_return(
    payload: ReturnCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("sale.void")),
) -> ReturnOut:
    require_store_access(payload.store_id, current)
    ret = await create_return(db, current=current, payload=payload)
    await db.commit()
    await db.refresh(ret)
    return ret
