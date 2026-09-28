from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import CurrentUser, require_permission
from app.core.config import settings
from app.schemas.schemas import (
    PaymentConfigOut,
    RazorpayOrderOut,
    RazorpayOrderRequest,
    RazorpayVerifyRequest,
)
from app.services import payments as payments_service

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("/config", response_model=PaymentConfigOut)
async def payment_config(_current: CurrentUser = Depends(require_permission("sale.create"))):
    if not payments_service.is_configured():
        return PaymentConfigOut(enabled=False)
    return PaymentConfigOut(enabled=True, key_id=settings.razorpay_key_id)


@router.post("/razorpay/order", response_model=RazorpayOrderOut)
async def create_razorpay_order(
    body: RazorpayOrderRequest, current: CurrentUser = Depends(require_permission("sale.create"))
):
    if not payments_service.is_configured():
        raise HTTPException(status_code=409, detail="Payment gateway is not configured")
    order = payments_service.create_order(
        body.amount, body.receipt, notes={"cashier_id": str(current.id)}
    )
    return RazorpayOrderOut(
        order_id=order["id"], amount=order["amount"], currency=order["currency"], key_id=settings.razorpay_key_id
    )


@router.post("/razorpay/verify")
async def verify_razorpay_payment(
    body: RazorpayVerifyRequest, _current: CurrentUser = Depends(require_permission("sale.create"))
):
    if not payments_service.is_configured():
        raise HTTPException(status_code=409, detail="Payment gateway is not configured")
    ok = payments_service.verify_payment_signature(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Signature verification failed")
    return {"verified": True, "payment_id": body.razorpay_payment_id}
