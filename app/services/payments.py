"""Razorpay integration for real UPI/Card payments at the POS.

Kept as a thin, optional layer on top of the existing quick-tender flow: if
no keys are configured (`RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` in .env),
`is_configured()` is False and the frontend falls back to just recording the
tender the way it always has. Nothing about the offline sale-sync path
changes — the gateway only produces a `payment_id` that gets attached as
`PaymentIn.reference` on the same bill payload it already sends.
"""

import hmac
import hashlib

import razorpay

from app.core.config import settings


def is_configured() -> bool:
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret)


def _client() -> razorpay.Client:
    client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    return client


def create_order(amount_rupees: float, receipt: str, notes: dict | None = None) -> dict:
    """Amount is rupees at the call site; Razorpay wants paise (integer)."""
    client = _client()
    return client.order.create(
        {
            "amount": int(round(amount_rupees * 100)),
            "currency": "INR",
            "receipt": receipt,
            "notes": notes or {},
            "payment_capture": 1,
        }
    )


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    body = f"{order_id}|{payment_id}".encode()
    expected = hmac.new(settings.razorpay_key_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    if not settings.razorpay_webhook_secret:
        return False
    expected = hmac.new(settings.razorpay_webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def refund_payment(payment_id: str, amount_rupees: float) -> dict:
    """Point 8 audit fix: OMS had no refund capability at all. Amount is
    rupees at the call site; Razorpay wants paise (integer)."""
    client = _client()
    return client.payment.refund(payment_id, {"amount": int(round(amount_rupees * 100))})
