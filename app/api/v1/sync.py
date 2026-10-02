from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission
from app.core.database import get_db
from app.schemas.schemas import SyncPushBatch, SyncPushResponse
from app.services.sync import process_sale

router = APIRouter(prefix="/sync", tags=["sync"])


@router.get("/health")
async def health() -> dict:
    """Lightweight connectivity probe the device checks before a push attempt,
    so an offline device costs one small request rather than a long timeout."""
    return {"ok": True}


@router.post("/push", response_model=SyncPushResponse)
async def push(
    batch: SyncPushBatch,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("pos.sale.create")),
) -> SyncPushResponse:
    """Applies each item in creation order, returning a per-item verdict so one bad
    row never blocks the good rows behind it. Always returns 200 — a network error or
    timeout mid-flight means nothing was decided, so the client keeps the whole batch
    pending and retries, which is safe precisely because the server deduplicates."""
    results = [await process_sale(db, sale) for sale in batch.sales]
    await db.commit()
    revision = (await db.execute(text("select last_value from global_revision_seq"))).scalar_one()
    return SyncPushResponse(results=results, server_revision=int(revision))
