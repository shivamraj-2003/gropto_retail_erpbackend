import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.services import imports as import_service

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post("/products/stage")
async def stage_products(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.commit")),
) -> dict:
    content = await file.read()
    batch = await import_service.stage_products_file(db, file_bytes=content, filename=file.filename, uploaded_by=current.user_id)
    return await import_service.preview_batch(db, batch.id)


@router.get("/{batch_id}/preview")
async def preview(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("import.commit")),
) -> dict:
    return await import_service.preview_batch(db, batch_id)


@router.post("/products/{batch_id}/commit")
async def commit_products(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.commit")),
) -> dict:
    try:
        return await import_service.commit_products_batch(db, batch_id, current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/opening-stock/stage")
async def stage_opening_stock(
    store_id: uuid.UUID,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.commit")),
) -> dict:
    require_store_access(store_id, current)
    content = await file.read()
    batch = await import_service.stage_opening_stock_file(
        db, file_bytes=content, filename=file.filename, store_id=store_id, uploaded_by=current.user_id
    )
    return await import_service.preview_batch(db, batch.id)


@router.post("/opening-stock/{batch_id}/commit")
async def commit_opening_stock(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.commit")),
) -> dict:
    try:
        return await import_service.commit_opening_stock_batch(db, batch_id, current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
