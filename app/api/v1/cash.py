import uuid

from fastapi import APIRouter, Depends, HTTPException
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Device, User
from app.models.models_phase2 import CashierShift, DayClose
from app.schemas.schemas_phase2 import CashMovementIn, DayCloseRequest, ShiftClose, ShiftOpen, ShiftReopen
from app.services import cash as cash_service

router = APIRouter(prefix="/cash", tags=["cash"])


@router.get("/shifts/current")
async def current_shift(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current: CurrentUser = Depends(require_permission("pos.shift.view")),
) -> dict | None:
    """The device needs to know its own open shift (if any) to record cash
    movements/close without the cashier having to remember a shift id."""
    result = await db.execute(
        select(CashierShift).where(CashierShift.device_id == device_id, CashierShift.status == "open")
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        return None
    return {
        "shift_id": str(shift.id),
        "opening_float": float(shift.opening_float),
        "opened_at": shift.opened_at.isoformat(),
        "opening_denominations": shift.opening_denominations,
        # opening_cash, cash_sales, cash_in, cash_out, expected_cash — worked
        # out live so the screen never asks the cashier to add anything up.
        **await cash_service.cash_summary(db, shift),
    }


@router.get("/shifts")
async def recent_shifts(
    store_id: uuid.UUID,
    limit: int = 15,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.view")),
) -> list[dict]:
    """The store's latest shifts, newest first, so a closed one can be found (and, in an emergency, reopened)."""
    require_store_access(store_id, current)
    rows = (
        await db.execute(
            select(CashierShift, Device.code, User.full_name)
            .join(Device, Device.id == CashierShift.device_id)
            .join(User, User.id == CashierShift.cashier_id)
            .where(CashierShift.store_id == store_id)
            .order_by(CashierShift.opened_at.desc())
            .limit(max(1, min(limit, 50)))
        )
    ).all()
    closed_days = {
        d for (d,) in (await db.execute(select(DayClose.business_date).where(DayClose.store_id == store_id))).all()
    }
    can_reopen = current.has_permission("pos.shift.override")
    return [
        {
            "shift_id": str(s.id), "till": code, "cashier": name, "status": s.status,
            "opened_at": s.opened_at.isoformat() if s.opened_at else None, "closed_at": s.closed_at.isoformat() if s.closed_at else None,
            "opening_float": float(s.opening_float), "expected_cash": float(s.expected_cash) if s.expected_cash is not None else None,
            "counted_cash": float(s.counted_cash) if s.counted_cash is not None else None, "variance": float(s.variance) if s.variance is not None else None,
            "day_closed": bool(s.closed_at and s.closed_at.date() in closed_days),
            "can_reopen": bool(can_reopen and s.status == "closed" and not (s.closed_at and s.closed_at.date() in closed_days)),
        }
        for s, code, name in rows
    ]


@router.post("/shifts/{shift_id}/reopen")
async def reopen_shift(
    shift_id: uuid.UUID,
    payload: ShiftReopen,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.override")),
) -> dict:
    """Emergency only. See services/cash.py reopen_shift."""
    shift = await db.get(CashierShift, shift_id)
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")
    require_store_access(shift.store_id, current)
    shift = await cash_service.reopen_shift(db, current=current, shift=shift, reason=payload.reason.strip())
    await db.commit()
    return {"shift_id": str(shift.id), "status": shift.status}


@router.get("/payment-match")
async def payment_match(
    store_id: uuid.UUID,
    business_date: date | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.view")),
) -> dict:
    """One day, one store: what the bills say came in by each way of paying, set against what can actually be checked.

    cash       counter cash from the bills, against the cash counted when the shifts closed
    card / upi what the bills say, against what the Paytm machine confirmed for those same bills (a card or UPI payment typed in
               by hand, with no machine behind it, shows as "not confirmed")
    online     orders delivered that day: paid in advance, and cash on delivery (cash the riders must hand in)
    Nothing here changes any record; it only shows where the two sides agree and where they do not."""
    require_store_access(store_id, current)
    day = business_date or date.today()
    params = {"store_id": str(store_id), "day": day}

    counter = (
        await db.execute(
            text(
                """
                select p.mode,
                       coalesce(sum(p.amount), 0) as recorded,
                       count(distinct s.id) as bills,
                       coalesce(sum(case when pp.id is not null then p.amount else 0 end), 0) as confirmed,
                       count(distinct case when pp.id is null then s.id end) as unconfirmed_bills
                from payments p
                join sales s on s.id = p.sale_id
                left join paytm_pos_payments pp
                       on pp.merchant_transaction_id = p.reference and pp.status = 'success' and pp.store_id = s.store_id
                where s.store_id = :store_id and s.status = 'completed'
                  and timezone('Asia/Kolkata', s.billed_at)::date = :day
                group by p.mode order by p.mode
                """
            ),
            params,
        )
    ).all()

    shifts = (
        await db.execute(
            text(
                """
                select coalesce(sum(expected_cash), 0) as expected, coalesce(sum(counted_cash), 0) as counted, count(*) as n
                from cashier_shifts
                where store_id = :store_id and status = 'closed' and timezone('Asia/Kolkata', closed_at)::date = :day
                """
            ),
            params,
        )
    ).one()
    open_shifts = await db.scalar(
        text("select count(*) from cashier_shifts where store_id = :store_id and status = 'open'"), {"store_id": str(store_id)}
    )

    online = (
        await db.execute(
            text(
                """
                select payment_mode, coalesce(sum(grand_total), 0) as amount, count(*) as orders
                from orders
                where allocated_store_id = :store_id and status = 'delivered'
                  and timezone('Asia/Kolkata', delivered_at)::date = :day
                group by payment_mode
                """
            ),
            params,
        )
    ).all()

    rows = []
    for r in counter:
        recorded, confirmed = float(r.recorded), float(r.confirmed)
        row = {"mode": r.mode, "recorded": recorded, "bills": int(r.bills), "confirmed": None, "difference": None, "note": None}
        if r.mode == "cash":
            # cash is matched to the count in the drawer, once the shifts have closed
            if int(shifts.n) > 0:
                row["confirmed"] = float(shifts.counted)
                row["difference"] = round(float(shifts.counted) - float(shifts.expected), 2)
                row["note"] = f"Counted {float(shifts.counted):,.2f} against {float(shifts.expected):,.2f} expected (opening float and cash in/out included) from {int(shifts.n)} closed shift(s)"
            else:
                row["note"] = "No shift has closed yet today, so there is no count to match"
        elif r.mode in ("card", "upi"):
            row["confirmed"] = confirmed
            row["difference"] = round(recorded - confirmed, 2)
            row["note"] = (
                "Every bill is confirmed by the Paytm machine" if int(r.unconfirmed_bills) == 0
                else f"{int(r.unconfirmed_bills)} bill(s) were entered by hand with no Paytm confirmation: check them against the bank / UPI app"
            )
        else:
            row["note"] = "Used inside the app (wallet or gift voucher): nothing outside to match"
        rows.append(row)

    return {
        "store_id": str(store_id),
        "business_date": day.isoformat(),
        "open_shifts": int(open_shifts or 0),
        "counter": rows,
        "online": [{"payment_mode": o.payment_mode, "amount": float(o.amount), "orders": int(o.orders)} for o in online],
        "online_note": "Cash on delivery is cash the riders still have to hand in. Paid-in-advance orders have no payment gateway feed to match against.",
    }


@router.post("/shifts/open")
async def open_shift(
    payload: ShiftOpen,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.create")),
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
    current: CurrentUser = Depends(require_permission("pos.cash_movement.create")),
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
    current: CurrentUser = Depends(require_permission("pos.shift.close")),
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
    current: CurrentUser = Depends(require_permission("store.day_close.close")),
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
