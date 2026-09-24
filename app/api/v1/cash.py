import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import CashierShift
from app.schemas.schemas_phase2 import CashMovementIn, DayCloseRequest, ShiftClose, ShiftOpen
from app.services import cash as cash_service

router = APIRouter(prefix="/cash", tags=["cash"])


@router.post("/shifts/open")
async def open_shift(
    payload: ShiftOpen,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("sale.create")),
) -> dict:
    require_store_access(payload.store_id, current)
    shift = await cash_service.open_shift(db, current=current, payload=payload)
    await db.commit()
    return {"shift_id": str(shift.id)}


@router.post("/shifts/{shift_id}/movement")
async def cash_movement(
    shift_id: uuid.UUID,
    payload: CashMovementIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("sale.create")),
) -> dict:
    shift = await db.get(CashierShift, shift_id)
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")
    require_store_access(shift.store_id, current)
    movement = await cash_service.record_cash_movement(db, current=current, shift=shift, payload=payload)
    await db.commit()
    return {"movement_id": str(movement.id)}


@router.post("/shifts/{shift_id}/close")
async def close_shift(
    shift_id: uuid.UUID,
    payload: ShiftClose,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("sale.create")),
) -> dict:
    shift = await db.get(CashierShift, shift_id)
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")
    require_store_access(shift.store_id, current)
    shift = await cash_service.close_shift(db, current=current, shift=shift, payload=payload)
    await db.commit()
    return {"expected_cash": float(shift.expected_cash), "counted_cash": float(shift.counted_cash), "variance": float(shift.variance)}


@router.post("/day-close")
async def day_close(
    payload: DayCloseRequest,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("report.export")),
) -> dict:
    require_store_access(payload.store_id, current)
    result = await cash_service.close_day(db, current=current, payload=payload)
    await db.commit()
    return {
        "total_expected_cash": float(result.total_expected_cash),
        "total_counted_cash": float(result.total_counted_cash),
        "variance": float(result.variance),
    }
