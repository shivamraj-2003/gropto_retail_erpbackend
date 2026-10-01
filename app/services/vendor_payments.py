"""Vendor payments against a Payable — partial payments, running balance, and
an approval threshold for large payouts (Point 6 audit fix: no payment model,
no partial-payment support, and no approval workflow existed at all before)."""

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.models.models_phase2 import Payable, VendorPayment
from app.schemas.schemas_phase2 import VendorPaymentIn
from app.services.approvals import submit_or_apply
from app.services.audit import write_audit

PAYMENT_APPROVAL_THRESHOLD = 20000.0


async def record_payment(db: AsyncSession, *, current: CurrentUser, payable: Payable, payload: VendorPaymentIn) -> VendorPayment | dict:
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Payment amount must be positive")
    if payload.amount > float(payable.amount_due) + 0.01:
        raise HTTPException(status_code=409, detail=f"Payment {payload.amount} exceeds the outstanding balance {float(payable.amount_due)}")

    if payload.amount > PAYMENT_APPROVAL_THRESHOLD and current.role_code != "super_admin":
        request = await submit_or_apply(
            db,
            current=current,
            request_type="vendor_payment_approval",
            entity_type="payable",
            entity_id=payable.id,
            old_value=None,
            new_value={"payable_id": str(payable.id), "amount": payload.amount, "reference": payload.reference},
            reason=payload.reference,
            store_id=None,
        )
        return {"approval_request_id": request.id, "status": request.status}

    payment = VendorPayment(
        payable_id=payable.id, amount=payload.amount, reference=payload.reference, requested_by=current.user_id, status="applied"
    )
    db.add(payment)
    payable.amount_due = float(payable.amount_due) - payload.amount
    payable.status = "paid" if payable.amount_due <= 0.01 else "partially_paid"
    await write_audit(
        db,
        user_id=current.user_id,
        role_code=current.role_code,
        store_id=None,
        device_id=current.device_id,
        action="vendor_payment.applied",
        entity_type="payable",
        entity_id=payable.id,
        new_value={"amount": payload.amount, "remaining_due": float(payable.amount_due)},
    )
    return payment
