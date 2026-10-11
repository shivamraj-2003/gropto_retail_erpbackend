from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

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
async def payment_config(_current: CurrentUser = Depends(require_permission("pos.payment.view"))):
    if not payments_service.is_configured():
        return PaymentConfigOut(enabled=False)
    return PaymentConfigOut(enabled=True, key_id=settings.razorpay_key_id)


@router.post("/razorpay/order", response_model=RazorpayOrderOut)
async def create_razorpay_order(
    body: RazorpayOrderRequest, current: CurrentUser = Depends(require_permission("pos.payment.create"))
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
    body: RazorpayVerifyRequest, _current: CurrentUser = Depends(require_permission("pos.payment.create"))
):
    if not payments_service.is_configured():
        raise HTTPException(status_code=409, detail="Payment gateway is not configured")
    ok = payments_service.verify_payment_signature(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Signature verification failed")
    return {"verified": True, "payment_id": body.razorpay_payment_id}


class UpiQrRequest(BaseModel):
    amount: float = Field(gt=0)
    receipt: str = Field(min_length=1, max_length=100)


class UpiQrOut(BaseModel):
    qr_id: str
    image_url: str
    amount: float


class UpiQrStatusOut(BaseModel):
    status: str  # waiting | paid | expired
    payment_id: str | None = None


@router.post("/razorpay/qr", response_model=UpiQrOut)
async def create_upi_qr(body: UpiQrRequest, current: CurrentUser = Depends(require_permission("pos.payment.create"))):
    """A QR for this exact amount, shown on the customer display."""
    if not payments_service.is_configured():
        raise HTTPException(status_code=409, detail="Payment gateway is not configured")
    try:
        qr = payments_service.create_upi_qr(body.amount, body.receipt, notes={"cashier_id": str(current.id)})
    except Exception as exc:  # the gateway rejects it when QR Codes is not enabled on the account
        raise HTTPException(status_code=502, detail=f"Could not create the payment QR: {exc}") from exc
    return UpiQrOut(qr_id=qr["id"], image_url=qr["image_url"], amount=body.amount)


@router.get("/razorpay/qr/{qr_id}", response_model=UpiQrStatusOut)
async def upi_qr_status(
    qr_id: str, amount: float, _current: CurrentUser = Depends(require_permission("pos.payment.create"))
):
    if not payments_service.is_configured():
        raise HTTPException(status_code=409, detail="Payment gateway is not configured")
    try:
        return UpiQrStatusOut(**payments_service.upi_qr_status(qr_id, amount))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not check the payment: {exc}") from exc


@router.post("/razorpay/qr/{qr_id}/close")
async def close_upi_qr(qr_id: str, _current: CurrentUser = Depends(require_permission("pos.payment.create"))):
    if payments_service.is_configured():
        payments_service.close_upi_qr(qr_id)
    return {"closed": True}
