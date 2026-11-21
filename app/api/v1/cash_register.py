"""The Cash Register: one day of a store as a single timeline. Every bill (with its GST and how it was paid), every cash in and out,
every shift opening and closing, returns and cancelled bills, with the cash drawer's running balance beside them."""

import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Customer, Device, Payment, Sale, SaleItem, User
from app.models.models_phase2 import CashierShift, CashMovement, Return

router = APIRouter(prefix="/cash", tags=["cash"])
IST = ZoneInfo("Asia/Kolkata")
CASH_MODES = ("cash",)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=IST)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _r2(n: float) -> float:
    return round(float(n or 0), 2)


@router.get("/register")
async def cash_register(
    store_id: uuid.UUID,
    business_date: date | None = None,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.view")),
) -> dict:
    """The day's timeline for a store (the business date is the Indian date), with the drawer's running cash balance and a summary."""
    require_store_access(store_id, current)
    day = business_date or datetime.now(IST).date()
    start, end = _day_bounds(day)

    users = {uid: name for uid, name in (await db.execute(select(User.id, User.full_name))).all()}
    tills = {d.id: d.code for d in (await db.execute(select(Device.id, Device.code).where(Device.store_id == store_id))).all()}

    events: list[dict] = []

    # ---- shifts opened and closed today
    shifts = (await db.execute(select(CashierShift).where(CashierShift.store_id == store_id))).scalars().all()
    shift_by_id = {s.id: s for s in shifts}
    for s in shifts:
        if s.opened_at and start <= _aware(s.opened_at) < end:
            events.append({
                "at": _aware(s.opened_at), "type": "shift_open", "ref": "Shift opened", "till": tills.get(s.device_id, ""), "who": users.get(s.cashier_id, ""),
                "detail": "Opening cash in the drawer", "cash": _r2(s.opening_float),
            })
        if s.closed_at and start <= _aware(s.closed_at) < end:
            events.append({
                "at": _aware(s.closed_at), "type": "shift_close", "ref": "Shift closed", "till": tills.get(s.device_id, ""), "who": users.get(s.cashier_id, ""),
                "detail": f"Counted ₹{_r2(s.counted_cash):.2f} against ₹{_r2(s.expected_cash):.2f} expected", "cash": 0.0, "info": True,
                "variance": _r2(s.variance) if s.variance is not None else None,
            })

    # ---- cash in / out
    movements = (
        await db.execute(
            select(CashMovement).join(CashierShift, CashierShift.id == CashMovement.shift_id)
            .where(CashierShift.store_id == store_id, CashMovement.created_at >= start, CashMovement.created_at < end)
        )
    ).scalars().all()
    for m in movements:
        sh = shift_by_id.get(m.shift_id)
        sign = 1 if m.direction == "in" else -1
        events.append({
            "at": _aware(m.created_at), "type": "cash_in" if sign > 0 else "cash_out", "ref": "Cash in" if sign > 0 else "Cash out",
            "till": tills.get(sh.device_id, "") if sh else "", "who": users.get(sh.cashier_id, "") if sh else "", "detail": m.reason, "cash": sign * _r2(m.amount),
        })

    # ---- bills (and cancelled bills)
    sales = (
        await db.execute(
            select(Sale).where(Sale.store_id == store_id, Sale.billed_at >= start, Sale.billed_at < end, Sale.status.in_(("completed", "voided"))).order_by(Sale.billed_at)
        )
    ).scalars().all()
    ids = [s.id for s in sales]
    pay_rows: dict[uuid.UUID, list[Payment]] = {}
    if ids:
        for p in (await db.execute(select(Payment).where(Payment.sale_id.in_(ids)))).scalars().all():
            pay_rows.setdefault(p.sale_id, []).append(p)
    tax_rows: dict[uuid.UUID, tuple[float, float, float]] = {}
    if ids:
        for sid, taxable, cgst, sgst, igst in (
            await db.execute(
                select(SaleItem.sale_id, func.sum(SaleItem.taxable_value), func.sum(SaleItem.cgst_amount), func.sum(SaleItem.sgst_amount), func.sum(SaleItem.igst_amount))
                .where(SaleItem.sale_id.in_(ids)).group_by(SaleItem.sale_id)
            )
        ).all():
            tax_rows[sid] = (_r2(taxable), _r2(cgst) + _r2(sgst), _r2(igst))
    customers = {}
    cust_ids = [s.customer_id for s in sales if s.customer_id]
    if cust_ids:
        customers = {c.id: c for c in (await db.execute(select(Customer).where(Customer.id.in_(cust_ids)))).scalars().all()}
    for s in sales:
        pays = pay_rows.get(s.id, [])
        by_mode: dict[str, float] = {}
        for p in pays:
            by_mode[p.mode] = _r2(by_mode.get(p.mode, 0) + float(p.amount))
        taxable, gst, igst = tax_rows.get(s.id, (0.0, 0.0, 0.0))
        c = customers.get(s.customer_id)
        voided = s.status == "voided"
        events.append({
            "at": _aware(s.billed_at), "type": "bill_void" if voided else "bill", "ref": s.bill_number, "sale_id": str(s.id), "till": tills.get(s.device_id, ""),
            "who": users.get(s.cashier_id, ""), "detail": (c.name or c.phone) if c else "Walk-in", "customer_phone": c.phone if c else None,
            "taxable": taxable, "gst": _r2(gst + igst), "discount": _r2(s.discount_total), "total": _r2(s.grand_total), "modes": by_mode,
            "cash": 0.0 if voided else by_mode.get("cash", 0.0), "info": voided,
            "cash_received": _r2(s.cash_received) if s.cash_received is not None else None, "change_given": _r2(s.change_given) if s.change_given is not None else None,
        })

    # ---- refunds on returns (the refund's payment mode is not recorded, so these do not move the drawer figure)
    for r in (
        await db.execute(select(Return).where(Return.store_id == store_id, Return.created_at >= start, Return.created_at < end, Return.status == "completed"))
    ).scalars().all():
        events.append({
            "at": _aware(r.created_at), "type": "return", "ref": "Return" + (" (no bill)" if r.no_bill else ""), "till": "", "who": "", "detail": r.reason or "",
            "total": -_r2(r.refund_total), "cash": 0.0, "info": True,
        })

    events.sort(key=lambda e: e["at"])
    balance = 0.0
    for e in events:
        balance = round(balance + e.get("cash", 0.0), 2)
        e["balance"] = balance
        e["at"] = e["at"].isoformat()

    bills = [e for e in events if e["type"] == "bill"]
    mode_totals: dict[str, float] = {}
    for b in bills:
        for m, v in b["modes"].items():
            mode_totals[m] = _r2(mode_totals.get(m, 0) + v)
    floats = sum(e["cash"] for e in events if e["type"] == "shift_open")
    cash_in = sum(e["cash"] for e in events if e["type"] == "cash_in")
    cash_out = -sum(e["cash"] for e in events if e["type"] == "cash_out")
    return {
        "business_date": day.isoformat(),
        "summary": {
            "bills": len(bills), "cancelled_bills": len([e for e in events if e["type"] == "bill_void"]),
            "sales_total": _r2(sum(b["total"] for b in bills)), "taxable": _r2(sum(b["taxable"] for b in bills)), "gst": _r2(sum(b["gst"] for b in bills)),
            "discount": _r2(sum(b["discount"] for b in bills)), "by_mode": mode_totals,
            "opening_cash": _r2(floats), "cash_in": _r2(cash_in), "cash_out": _r2(cash_out),
            "returns": _r2(-sum(e["total"] for e in events if e["type"] == "return")),
            "expected_in_drawer": _r2(floats + mode_totals.get("cash", 0.0) + cash_in - cash_out),
        },
        "events": events,
    }


@router.get("/register/bill/{sale_id}")
async def register_bill(
    sale_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.view")),
) -> dict:
    """One bill line by line, with the GST on each line and how it was paid."""
    sale = await db.get(Sale, sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail="Bill not found")
    require_store_access(sale.store_id, current)
    items = (await db.execute(select(SaleItem).where(SaleItem.sale_id == sale_id))).scalars().all()
    payments = (await db.execute(select(Payment).where(Payment.sale_id == sale_id))).scalars().all()
    return {
        "bill_number": sale.bill_number, "status": sale.status, "grand_total": _r2(sale.grand_total), "discount": _r2(sale.discount_total),
        "cash_received": _r2(sale.cash_received) if sale.cash_received is not None else None, "change_given": _r2(sale.change_given) if sale.change_given is not None else None,
        "items": [
            {
                "name": i.product_name_snapshot, "hsn": i.hsn_code_snapshot, "qty": float(i.quantity), "price": _r2(i.unit_price), "discount": _r2(i.line_discount),
                "taxable": _r2(i.taxable_value), "gst_rate": float(i.tax_rate_snapshot), "gst": _r2(float(i.cgst_amount) + float(i.sgst_amount) + float(i.igst_amount)), "total": _r2(i.line_total),
            }
            for i in items
        ],
        "payments": [{"mode": p.mode, "amount": _r2(p.amount), "reference": p.reference} for p in payments],
    }
