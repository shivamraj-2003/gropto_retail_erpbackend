import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Customer
from app.models.models_credit import CreditEntry
from app.models.models_phase2 import CashierShift, CashMovement
from app.services.audit import write_audit

router = APIRouter(prefix="/credit", tags=["credit"])

RECEIVE_MODES = ("cash", "upi", "card", "bank")


class PaymentIn(BaseModel):
    store_id: uuid.UUID
    amount: float = Field(gt=0)
    mode: str = "cash"
    note: str | None = Field(default=None, max_length=200)


class AdjustIn(BaseModel):
    store_id: uuid.UUID
    amount: float  # + the customer owes more, - written off / corrected
    reason: str = Field(min_length=5, max_length=300)


class LimitIn(BaseModel):
    credit_limit: float | None = Field(default=None, ge=0)  # empty = no limit


async def _balance(db: AsyncSession, customer_id: uuid.UUID, store_id: uuid.UUID) -> float:
    total = await db.scalar(select(func.coalesce(func.sum(CreditEntry.amount), 0)).where(CreditEntry.customer_id == customer_id, CreditEntry.store_id == store_id))
    return round(float(total or 0), 2)


@router.get("/customers")
async def customers_on_credit(
    store_id: uuid.UUID,
    q: str = "",
    only_due: bool = True,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.customer.view")),
) -> dict:
    """Who owes this store money (udhaar), most owed first, with how long the oldest unpaid credit has been waiting."""
    require_store_access(store_id, current)
    stmt = (
        select(
            Customer.id, Customer.name, Customer.phone, Customer.credit_limit,
            func.sum(CreditEntry.amount).label("balance"),
            func.max(CreditEntry.created_at).filter(CreditEntry.kind == "payment").label("last_payment"),
            func.min(CreditEntry.created_at).filter(CreditEntry.kind == "sale").label("first_credit"),
        )
        .join(CreditEntry, CreditEntry.customer_id == Customer.id)
        .where(CreditEntry.store_id == store_id)
        .group_by(Customer.id)
    )
    text = q.strip()
    if text:
        stmt = stmt.where(or_(Customer.phone.ilike(f"%{text}%"), Customer.name.ilike(f"%{text}%")))
    if only_due:
        stmt = stmt.having(func.sum(CreditEntry.amount) > 0.004)
    rows = (await db.execute(stmt.order_by(func.sum(CreditEntry.amount).desc()))).all()
    now = datetime.now(timezone.utc)
    items = [
        {
            "customer_id": str(r.id), "name": r.name, "phone": r.phone, "balance": round(float(r.balance), 2),
            "credit_limit": float(r.credit_limit) if r.credit_limit is not None else None,
            "over_limit": r.credit_limit is not None and float(r.balance) > float(r.credit_limit),
            "last_payment_at": r.last_payment.isoformat() if r.last_payment else None,
            "waiting_days": (now - r.first_credit).days if r.first_credit and float(r.balance) > 0 else 0,
        }
        for r in rows
    ]
    return {"items": items, "total_outstanding": round(sum(i["balance"] for i in items if i["balance"] > 0), 2), "customers": len([i for i in items if i["balance"] > 0])}


@router.get("/entries")
async def recent_entries(
    store_id: uuid.UUID,
    kind: str = "",
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.customer.view")),
) -> dict:
    """Every credit given and every payment received at this store, newest first, plus what was collected today."""
    require_store_access(store_id, current)
    stmt = select(CreditEntry, Customer.name, Customer.phone).join(Customer, Customer.id == CreditEntry.customer_id).where(CreditEntry.store_id == store_id)
    if kind in ("sale", "payment", "adjustment"):
        stmt = stmt.where(CreditEntry.kind == kind)
    rows = (await db.execute(stmt.order_by(CreditEntry.created_at.desc()).limit(max(1, min(limit, 500))))).all()
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today = await db.scalar(
        select(func.coalesce(func.sum(-CreditEntry.amount), 0)).where(CreditEntry.store_id == store_id, CreditEntry.kind == "payment", CreditEntry.created_at >= start)
    )
    return {
        "collected_today": round(float(today or 0), 2),
        "items": [
            {"id": str(e.id), "when": e.created_at.isoformat(), "kind": e.kind, "amount": float(e.amount), "mode": e.mode, "note": e.note,
             "customer_id": str(e.customer_id), "name": name, "phone": phone}
            for e, name, phone in rows
        ],
    }


@router.get("/customers/{customer_id}/ledger")
async def ledger(
    customer_id: uuid.UUID,
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.customer.view")),
) -> dict:
    require_store_access(store_id, current)
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    entries = (
        await db.execute(select(CreditEntry).where(CreditEntry.customer_id == customer_id, CreditEntry.store_id == store_id).order_by(CreditEntry.created_at))
    ).scalars().all()
    running, out = 0.0, []
    for e in entries:
        running = round(running + float(e.amount), 2)
        out.append({"id": str(e.id), "when": e.created_at.isoformat(), "kind": e.kind, "amount": float(e.amount), "mode": e.mode, "note": e.note, "balance": running})
    return {
        "customer": {"id": str(customer.id), "name": customer.name, "phone": customer.phone, "credit_limit": float(customer.credit_limit) if customer.credit_limit is not None else None},
        "balance": running, "entries": list(reversed(out)),
    }


@router.post("/customers/{customer_id}/payments")
async def receive_payment(
    customer_id: uuid.UUID,
    payload: PaymentIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.payment.create")),
) -> dict:
    """Money received against a customer's credit. Cash received goes into the drawer: it is added to the till's open shift as
    cash in, so the shift count still agrees. The balance is allowed to go below zero only by the amount actually paid."""
    require_store_access(payload.store_id, current)
    if payload.mode not in RECEIVE_MODES:
        raise HTTPException(status_code=422, detail="Choose cash, UPI, card or bank")
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    before = await _balance(db, customer_id, payload.store_id)
    if before <= 0:
        raise HTTPException(status_code=409, detail="This customer owes nothing here")
    if payload.amount > before + 0.005:
        raise HTTPException(status_code=409, detail=f"The customer owes ₹{before:,.2f}. Receive at most that, or use an adjustment")
    entry = CreditEntry(customer_id=customer_id, store_id=payload.store_id, kind="payment", amount=-payload.amount, mode=payload.mode, note=payload.note, created_by=current.user_id)
    db.add(entry)
    in_shift = None
    if payload.mode == "cash" and current.device_id is not None:
        shift = await db.scalar(select(CashierShift).where(CashierShift.device_id == current.device_id, CashierShift.status == "open"))
        if shift is not None:
            db.add(CashMovement(shift_id=shift.id, direction="in", amount=payload.amount, reason=f"Credit received from {customer.name or customer.phone}"))
            in_shift = True
        else:
            in_shift = False
    await db.flush()
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=payload.store_id, device_id=current.device_id,
        action="credit.payment_received", entity_type="customer", entity_id=customer_id,
        old_value={"balance": before}, new_value={"amount": payload.amount, "mode": payload.mode, "balance": round(before - payload.amount, 2)},
    )
    await db.commit()
    return {"balance": round(before - payload.amount, 2), "cash_in_shift": in_shift}


@router.post("/customers/{customer_id}/adjust")
async def adjust(
    customer_id: uuid.UUID,
    payload: AdjustIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.override")),
) -> dict:
    """A manager corrects or writes off part of a customer's credit. A reason is required and is kept in the audit log."""
    require_store_access(payload.store_id, current)
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    if abs(payload.amount) < 0.005:
        raise HTTPException(status_code=422, detail="The amount cannot be zero")
    before = await _balance(db, customer_id, payload.store_id)
    db.add(CreditEntry(customer_id=customer_id, store_id=payload.store_id, kind="adjustment", amount=payload.amount, note=payload.reason, created_by=current.user_id))
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=payload.store_id, device_id=current.device_id,
        action="credit.adjusted", entity_type="customer", entity_id=customer_id,
        old_value={"balance": before}, new_value={"change": payload.amount, "balance": round(before + payload.amount, 2)}, reason=payload.reason,
    )
    await db.commit()
    return {"balance": round(before + payload.amount, 2)}


@router.put("/customers/{customer_id}/limit")
async def set_limit(
    customer_id: uuid.UUID,
    payload: LimitIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.override")),
) -> dict:
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    old = float(customer.credit_limit) if customer.credit_limit is not None else None
    customer.credit_limit = payload.credit_limit
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=None, device_id=current.device_id,
        action="credit.limit_set", entity_type="customer", entity_id=customer_id, old_value={"credit_limit": old}, new_value={"credit_limit": payload.credit_limit},
    )
    await db.commit()
    return {"credit_limit": payload.credit_limit}
