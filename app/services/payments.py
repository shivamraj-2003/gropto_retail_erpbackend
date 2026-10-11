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


def create_upi_qr(amount_rupees: float, receipt: str, notes: dict | None = None, valid_minutes: int = 10) -> dict:
    """A one-time UPI QR for an exact amount, shown on the customer-facing display. The customer scans it with any
    UPI app; there is no widget on the till. Needs "QR Codes" switched on for the Razorpay account."""
    import time

    return _client().qrcode.create(
        {
            "type": "upi_qr",
            "name": "Gropto",
            "usage": "single_use",
            "fixed_amount": True,
            "payment_amount": int(round(amount_rupees * 100)),
            "description": receipt[:100],
            "close_by": int(time.time()) + max(valid_minutes, 3) * 60,
            "notes": {**(notes or {}), "receipt": receipt},
        }
    )


def upi_qr_status(qr_id: str, expected_rupees: float) -> dict:
    """Asks Razorpay (never trusts the till) whether this QR has been paid, for the full amount."""
    client = _client()
    qr = client.qrcode.fetch(qr_id)
    paid_paise = int(qr.get("payments_amount_received") or 0)
    expected = int(round(expected_rupees * 100))
    if int(qr.get("payments_count_received") or 0) >= 1 and paid_paise >= expected:
        payments = client.qrcode.fetch_all_payments(qr_id).get("items", [])
        captured = next((p for p in payments if p.get("status") in ("captured", "authorized")), None)
        if captured:
            return {"status": "paid", "payment_id": captured["id"]}
    if qr.get("status") == "closed":
        return {"status": "expired", "payment_id": None}
    return {"status": "waiting", "payment_id": None}


def close_upi_qr(qr_id: str) -> None:
    try:
        _client().qrcode.close(qr_id)
    except Exception:  # already closed or expired — nothing to undo
        pass
