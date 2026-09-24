import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.schemas.schemas import AiInsightOut
from app.services.ai import get_insight

router = APIRouter(prefix="/ai", tags=["ai"])


@router.get("/insights", response_model=AiInsightOut)
async def insights(
    store_id: uuid.UUID,
    question: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    require_store_access(store_id, current)
    return await get_insight(db, store_id=store_id, question=question)
