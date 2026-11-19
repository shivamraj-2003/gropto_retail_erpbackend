import uuid
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_permission, require_store_access
from app.core.database import get_db
from app.models.models import Payment, Sale
from app.models.models_paytm import PaytmPosPayment
from app.models.models_phase2 import CashierShift, DayClose
from app.services.audit import write_audit

router = APIRouter(prefix="/sales", tags=["sales"])

# Only these three are plain "money taken at the counter". Wallet, gift voucher and loyalty move balances elsewhere,
# so they are never switched to or from here.
SWITCHABLE = ("cash", "card", "upi")


class PaymentModeChange(BaseModel):
    from_mode: str
    to_mode: str
    amount: float = Field(gt=0)  # picks the payment line when a bill has more than one
    reason: str = Field(min_length=5, max_length=300)


@router.post("/{sale_id}/change-payment-mode")
async def change_payment_mode(
    sale_id: uuid.UUID,
    payload: PaymentModeChange,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_permission("pos.shift.override")),
) -> dict:
    """Fix how a bill was paid (a bill rung up as cash that was really UPI). A manager's action: a reason is required and
    the change is written to the audit log. Refused when the money was confirmed by the Paytm machine, when the till's shift
    has closed or the store's day is closed (the count has been signed off), and for wallet, gift voucher or loyalty payments."""
    sale = await db.get(Sale, sale_id)
    if sale is None or sale.status != "completed":
        raise HTTPException(status_code=404, detail="Bill not found")
    require_store_access(sale.store_id, current)
    if payload.from_mode not in SWITCHABLE or payload.to_mode not in SWITCHABLE:
        raise HTTPException(status_code=422, detail="Only cash, card and UPI can be changed into each other")
    if payload.from_mode == payload.to_mode:
        raise HTTPException(status_code=422, detail="That is already the payment mode")

    payments = (await db.execute(select(Payment).where(Payment.sale_id == sale_id))).scalars().all()
    line = next((p for p in payments if p.mode == payload.from_mode and abs(float(p.amount) - payload.amount) < 0.01), None)
    if line is None:
        raise HTTPException(status_code=404, detail=f"This bill has no {payload.from_mode} payment of {payload.amount:g}")

    confirmed = await db.scalar(
        select(PaytmPosPayment.id).where(PaytmPosPayment.merchant_transaction_id == (line.reference or ""), PaytmPosPayment.status == "success").limit(1)
    )
    if confirmed is not None:
        raise HTTPException(status_code=409, detail="The Paytm machine confirmed this payment, so its mode cannot be changed. Refund it on the machine instead")

    billed = sale.billed_at if sale.billed_at.tzinfo else sale.billed_at.replace(tzinfo=timezone.utc)
    closed_shift = await db.scalar(
        select(CashierShift.id).where(
            CashierShift.device_id == sale.device_id, CashierShift.status == "closed",
            CashierShift.opened_at <= billed, CashierShift.closed_at >= billed,
        ).limit(1)
    )
    if closed_shift is not None:
        raise HTTPException(status_code=409, detail="That till's shift is already closed and counted. Reopen the shift first (emergency) if this must change")
    day_closed = await db.scalar(select(DayClose.id).where(DayClose.store_id == sale.store_id, DayClose.business_date == billed.date()).limit(1))
    if day_closed is not None:
        raise HTTPException(status_code=409, detail="The store's day is closed, so this bill can no longer be changed")

    line.mode = payload.to_mode
    # a bill with no cash left no longer has cash received / change to give back
    if not any(p.mode == "cash" for p in payments):
        sale.cash_received = None
        sale.change_given = None
    await write_audit(
        db, user_id=current.user_id, role_code=current.role_code, store_id=sale.store_id, device_id=current.device_id,
        action="sale.payment_mode_changed", entity_type="sale", entity_id=sale.id,
        old_value={"mode": payload.from_mode, "amount": payload.amount}, new_value={"mode": payload.to_mode, "amount": payload.amount},
        reason=payload.reason,
    )
    await db.commit()
    return {"sale_id": str(sale.id), "mode": payload.to_mode}
