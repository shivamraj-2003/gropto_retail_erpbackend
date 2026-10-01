import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models_phase2 import CashierShift
from app.schemas.schemas_phase2 import CashMovementIn, DayCloseRequest, ShiftClose, ShiftOpen
from app.services import cash as cash_service

router = APIRouter(prefix="/cash", tags=["cash"])


@router.get("/shifts/current")
async def current_shift(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("sale.create")),
) -> dict | None:
    """The device needs to know its own open shift (if any) to record cash
    movements/close without the cashier having to remember a shift id."""
    result = await db.execute(
        select(CashierShift).where(CashierShift.device_id == device_id, CashierShift.status == "open")
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        return None
    return {"shift_id": str(shift.id), "opening_float": float(shift.opening_float), "opened_at": shift.opened_at.isoformat()}


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
    result = await cash_service.record_cash_movement(db, current=current, shift=shift, payload=payload)
    await db.commit()
    if isinstance(result, dict):
        return {"approval_request_id": str(result["approval_request_id"]), "status": result["status"]}
    return {"movement_id": str(result.id), "status": "applied"}


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
    return {
        "expected_cash": float(shift.expected_cash),
        "counted_cash": float(shift.counted_cash),
        "variance": float(shift.variance),
        "denomination_breakdown": shift.denomination_breakdown,
        "tender_breakdown": shift.tender_breakdown,
    }


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
        "tender_breakdown": result.tender_breakdown,
    }
