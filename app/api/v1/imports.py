import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access, require_warehouse_access
from app.core.database import get_db
from app.models.models import ImportBatch
from app.services import imports as import_service

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post("/products/stage")
async def stage_products(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.product.import")),
) -> dict:
    content = await file.read()
    try:
        batch = await import_service.stage_products_file(db, file_bytes=content, filename=file.filename, uploaded_by=current.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await import_service.preview_batch(db, batch.id)


@router.get("/{batch_id}/preview")
async def preview(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("import.batch.view")),
) -> dict:
    return await import_service.preview_batch(db, batch_id)


@router.post("/products/{batch_id}/commit")
async def commit_products(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.product.import")),
) -> dict:
    try:
        return await import_service.commit_products_batch(db, batch_id, current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/opening-stock/stage")
async def stage_opening_stock(
    file: UploadFile,
    store_id: uuid.UUID | None = None,
    warehouse_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.opening_stock.import")),
) -> dict:
    """Opening stock for one store OR one warehouse (exactly one of the two)."""
    if (store_id is None) == (warehouse_id is None):
        raise HTTPException(status_code=400, detail="Pick either a store or a warehouse to load stock into")
    if store_id is not None:
        require_store_access(store_id, current)
    else:
        require_warehouse_access(warehouse_id, current)
    content = await file.read()
    try:
        batch = await import_service.stage_opening_stock_file(
            db, file_bytes=content, filename=file.filename, store_id=store_id, warehouse_id=warehouse_id, uploaded_by=current.user_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await import_service.preview_batch(db, batch.id)


@router.post("/opening-stock/{batch_id}/commit")
async def commit_opening_stock(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("import.opening_stock.import")),
) -> dict:
    try:
        return await import_service.commit_opening_stock_batch(db, batch_id, current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{batch_id}/file")
async def download_original_file(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("import.batch.view")),
) -> StreamingResponse:
    """Every batch keeps its uploaded file (§10) — this is how you get it back."""
    batch = await db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return StreamingResponse(
        io.BytesIO(batch.file_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{batch.file_name}"'},
    )


@router.get("/{batch_id}/result-report")
async def download_result_report(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("import.batch.view")),
) -> StreamingResponse:
    """A downloadable result report showing exactly what was applied, skipped and
    rejected, with row numbers (§10)."""
    batch = await import_service.get_batch_with_rows(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    wb = Workbook()
    ws = wb.active
    ws.title = "Import Result"
    ws.append(["Row", "Action", "Errors", "Parsed Values"])
    for row in batch.rows:
        ws.append([row.line_number, row.computed_action, "; ".join(row.validation_messages), str(row.parsed_values)])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="import_result_{batch_id}.xlsx"'},
    )
