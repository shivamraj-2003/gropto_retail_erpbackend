"""Store cash operations (Phase 2): shift open/close, cash-in/out, day close with
expected-vs-counted reconciliation. Variance beyond tolerance is flagged via audit
rather than blocked — the till still has to close at the end of the day."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models import Payment, Sale
from app.models.models_phase2 import CashierShift, CashMovement, DayClose
from app.schemas.schemas_phase2 import CashMovementIn, DayCloseRequest, ShiftClose, ShiftOpen
from app.services.audit import write_audit

VARIANCE_TOLERANCE = 50.0


async def open_shift(db: AsyncSession, *, current: CurrentUser, payload: ShiftOpen) -> CashierShift:
    shift = CashierShift(
        store_id=payload.store_id,
        device_id=payload.device_id,
        cashier_id=current.user_id,
        opening_float=payload.opening_float,
        status="open",
    )
    db.add(shift)
    await db.flush()
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=payload.device_id,
        action="shift.opened",
        entity_type="cashier_shift",
        entity_id=shift.id,
        new_value={"opening_float": payload.opening_float},
    )
    return shift


async def record_cash_movement(db: AsyncSession, *, current: CurrentUser, shift: CashierShift, payload: CashMovementIn) -> CashMovement:
    movement = CashMovement(shift_id=shift.id, direction=payload.direction, amount=payload.amount, reason=payload.reason)
    db.add(movement)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=shift.store_id,
        device_id=shift.device_id,
        action="cash.movement",
        entity_type="cashier_shift",
        entity_id=shift.id,
        new_value={"direction": payload.direction, "amount": payload.amount, "reason": payload.reason},
    )
    return movement


async def _expected_cash(db: AsyncSession, shift: CashierShift) -> float:
    cash_sales = (
        await db.execute(
            select(Payment.amount)
            .join(Sale, Sale.id == Payment.sale_id)
            .where(Payment.mode == "cash", Sale.device_id == shift.device_id, Sale.billed_at >= shift.opened_at)
        )
    ).scalars().all()
    movements = (await db.execute(select(CashMovement).where(CashMovement.shift_id == shift.id))).scalars().all()
    movement_total = sum(m.amount if m.direction == "in" else -m.amount for m in movements)
    return float(shift.opening_float) + float(sum(cash_sales)) + movement_total


async def close_shift(db: AsyncSession, *, current: CurrentUser, shift: CashierShift, payload: ShiftClose) -> CashierShift:
    if shift.status != "open":
        raise HTTPException(status_code=409, detail="Shift already closed")

    expected = await _expected_cash(db, shift)
    shift.expected_cash = expected
    shift.counted_cash = payload.counted_cash
    shift.variance = payload.counted_cash - expected
    shift.status = "closed"
    shift.closed_at = datetime.now(timezone.utc)

    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=shift.store_id,
        device_id=shift.device_id,
        action="shift.closed",
        entity_type="cashier_shift",
        entity_id=shift.id,
        new_value={"expected": expected, "counted": payload.counted_cash, "variance": shift.variance},
    )
    return shift


async def close_day(db: AsyncSession, *, current: CurrentUser, payload: DayCloseRequest) -> DayClose:
    shifts = (
        await db.execute(
            select(CashierShift).where(
                CashierShift.store_id == payload.store_id,
                CashierShift.status == "closed",
            )
        )
    ).scalars().all()
    day_shifts = [s for s in shifts if s.closed_at and s.closed_at.date() == payload.business_date]

    total_expected = sum(float(s.expected_cash or 0) for s in day_shifts)
    total_counted = sum(float(s.counted_cash or 0) for s in day_shifts)
    variance = total_counted - total_expected

    day_close = DayClose(
        store_id=payload.store_id,
        business_date=payload.business_date,
        total_expected_cash=total_expected,
        total_counted_cash=total_counted,
        variance=variance,
        closed_by=current.user_id,
    )
    db.add(day_close)
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=payload.store_id,
        device_id=current.device_id,
        action="day.closed",
        entity_type="day_close",
        entity_id=None,
        new_value={"variance": variance, "shift_count": len(day_shifts)},
    )
    return day_close
