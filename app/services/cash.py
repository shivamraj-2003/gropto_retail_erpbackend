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
from app.services.approvals import approval_threshold, submit_or_apply
from app.services.audit import write_audit

VARIANCE_TOLERANCE = 50.0
# Point 4 audit fix: cash-in/out had no approval gate at all before this —
# any amount, no review. Store managers and above can still move cash
# directly; a plain cashier's movement above this queues for approval.
CASH_MOVEMENT_APPROVAL_THRESHOLD = 2000.0


def _assert_owns_shift(current: CurrentUser, shift: CashierShift) -> None:
    """Point 4 audit fix: previously only store access was checked, so any
    same-store cashier could close or move cash on a colleague's open
    shift. A shift's own cashier, or a store-manager-and-above role, may
    act on it; a peer cashier may not."""
    if shift.cashier_id == current.user_id:
        return
    # Point 14: was a hardcoded role list (enterprise roles + store/regional
    # manager); those roles were migrated onto pos.shift.override.
    if current.has_permission("pos.shift.override"):
        return
    raise HTTPException(status_code=403, detail="You do not own this shift")


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


async def record_cash_movement(
    db: AsyncSession, *, current: CurrentUser, shift: CashierShift, payload: CashMovementIn
) -> CashMovement | dict:
    _assert_owns_shift(current, shift)

    if payload.amount > await approval_threshold(db, "cash_movement_approval", CASH_MOVEMENT_APPROVAL_THRESHOLD) and not current.is_super_admin:
        request = await submit_or_apply(
            db,
            current=current,
            request_type="cash_movement_approval",
            entity_type="cashier_shift",
            entity_id=shift.id,
            old_value=None,
            new_value={"shift_id": str(shift.id), "direction": payload.direction, "amount": payload.amount, "reason": payload.reason},
            reason=payload.reason,
            store_id=shift.store_id,
        )
        return {"approval_request_id": request.id, "status": request.status}

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


async def _tender_breakdown(db: AsyncSession, *, device_id, since: datetime) -> dict:
    """Point 4 audit fix: close used to reconcile cash only. Every tender's
    total for the shift window is captured here so day close can at least
    report non-cash totals alongside cash, instead of silently dropping them."""
    rows = (
        await db.execute(
            select(Payment.mode, Payment.amount)
            .join(Sale, Sale.id == Payment.sale_id)
            .where(Sale.device_id == device_id, Sale.billed_at >= since)
        )
    ).all()
    breakdown: dict[str, float] = {}
    for mode, amount in rows:
        breakdown[mode] = breakdown.get(mode, 0.0) + float(amount)
    return breakdown


async def close_shift(db: AsyncSession, *, current: CurrentUser, shift: CashierShift, payload: ShiftClose) -> CashierShift:
    if shift.status != "open":
        raise HTTPException(status_code=409, detail="Shift already closed")
    _assert_owns_shift(current, shift)

    counted_cash = payload.counted_cash
    if payload.denomination_breakdown is not None:
        denom_total = sum(int(note) * count for note, count in payload.denomination_breakdown.items())
        if abs(denom_total - payload.counted_cash) > 0.01:
            raise HTTPException(
                status_code=400,
                detail=f"Denomination breakdown sums to {denom_total}, which does not match counted_cash {payload.counted_cash}",
            )

    expected = await _expected_cash(db, shift)
    shift.expected_cash = expected
    shift.counted_cash = counted_cash
    shift.variance = counted_cash - expected
    shift.denomination_breakdown = payload.denomination_breakdown
    shift.tender_breakdown = await _tender_breakdown(db, device_id=shift.device_id, since=shift.opened_at)
    # Point 10 audit fix: VARIANCE_TOLERANCE existed but nothing ever read
    # it — no classification or escalation happened regardless of variance
    # size. The shift still always closes (the till has to close at day
    # end), but a variance beyond tolerance now requires a reason and raises
    # a real, visible escalation for Finance instead of only an audit row.
    if abs(shift.variance) <= VARIANCE_TOLERANCE:
        shift.variance_status = "within_tolerance"
    elif shift.variance < 0:
        shift.variance_status = "shortage_flagged"
    else:
        shift.variance_status = "excess_flagged"
    shift.variance_reason = payload.variance_reason
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
        new_value={
            "expected": expected,
            "counted": counted_cash,
            "variance": shift.variance,
            "variance_status": shift.variance_status,
            "variance_reason": shift.variance_reason,
            "tender_breakdown": shift.tender_breakdown,
        },
    )

    if shift.variance_status != "within_tolerance":
        await submit_or_apply(
            db,
            current=current,
            request_type="cash_variance_escalation",
            entity_type="cashier_shift",
            entity_id=shift.id,
            old_value=None,
            new_value={
                "shift_id": str(shift.id),
                "store_id": str(shift.store_id),
                "variance": shift.variance,
                "variance_status": shift.variance_status,
                "reason": shift.variance_reason,
            },
            reason=shift.variance_reason,
            store_id=shift.store_id,
        )
    return shift


async def close_day(db: AsyncSession, *, current: CurrentUser, payload: DayCloseRequest) -> DayClose:
    # Point 16 audit fix: this previously silently excluded any still-open
    # cashier shift from the day's reconciliation instead of blocking the
    # close — a store could "close" for the day while a till remained open,
    # with that till's cash left out of total_expected_cash/
    # total_counted_cash/variance with no warning. Store Close must depend
    # on Cashier Close actually completing first.
    open_shifts = (
        await db.execute(
            select(CashierShift).where(
                CashierShift.store_id == payload.store_id,
                CashierShift.status == "open",
            )
        )
    ).scalars().all()
    still_open_for_date = [s for s in open_shifts if s.opened_at.date() <= payload.business_date]
    if still_open_for_date:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(still_open_for_date)} cashier shift(s) opened on or before {payload.business_date} "
                "are still open for this store — close every till before closing the day."
            ),
        )

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

    tender_breakdown: dict[str, float] = {}
    for s in day_shifts:
        for mode, amount in (s.tender_breakdown or {}).items():
            tender_breakdown[mode] = tender_breakdown.get(mode, 0.0) + float(amount)

    day_close = DayClose(
        store_id=payload.store_id,
        business_date=payload.business_date,
        total_expected_cash=total_expected,
        total_counted_cash=total_counted,
        variance=variance,
        tender_breakdown=tender_breakdown,
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
        new_value={"variance": variance, "shift_count": len(day_shifts), "tender_breakdown": tender_breakdown},
    )
    return day_close
