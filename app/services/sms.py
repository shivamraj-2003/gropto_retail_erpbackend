"""SMS via Twilio (Point 11 audit fix). Previously a hardcoded stub that
always returned "not configured" with no real client behind it at all. Same
is_configured()-gate pattern as every other provider in this codebase
(Razorpay, WhatsApp, Resend, GSP e-invoice) — left unconfigured in dev, in
which case campaign sends report a real per-recipient failure instead of
fabricating a delivery."""

import httpx

from app.core.config import settings


def is_configured() -> bool:
    return bool(settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number)


async def send_sms(to_phone: str, body: str) -> tuple[bool, str | None, str | None]:
    """Returns (ok, provider_message_id, error)."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                data={"To": to_phone, "From": settings.twilio_from_number, "Body": body},
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            )
        data = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
        if resp.status_code >= 400:
            return False, None, data.get("message", resp.text[:300])
        return True, data.get("sid"), None
    except httpx.HTTPError as exc:
        return False, None, str(exc)


# ---------------------------------------------------------------------------
# MSG91 (India) — single auth key; OTPs go through a DLT-approved template.
# ---------------------------------------------------------------------------


def msg91_configured() -> bool:
    return bool(settings.msg91_auth_key and settings.msg91_otp_template_id)


def otp_sms_configured() -> bool:
    provider = settings.sms_provider.lower()
    if provider == "msg91":
        return msg91_configured()
    if provider == "twilio":
        return is_configured()
    return msg91_configured() or is_configured()


def _msisdn(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"91{digits}" if len(digits) == 10 else digits


async def send_msg91_otp(to_phone: str, code: str) -> tuple[bool, str | None, str | None]:
    """MSG91 v5 OTP API: MSG91 renders the approved template with the code we
    generated (we keep our own hash + expiry), so verification stays here."""
    params = {
        "template_id": settings.msg91_otp_template_id,
        "mobile": _msisdn(to_phone),
        "otp": code,
        "otp_expiry": str(settings.otp_expiry_minutes),
    }
    if settings.msg91_sender_id:
        params["sender"] = settings.msg91_sender_id
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://control.msg91.com/api/v5/otp",
                params=params,
                headers={"authkey": settings.msg91_auth_key, "accept": "application/json"},
            )
        data = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
        if resp.status_code >= 400 or data.get("type") == "error":
            return False, None, str(data.get("message") or resp.text[:300])
        return True, data.get("request_id"), None
    except httpx.HTTPError as exc:
        return False, None, str(exc)


async def send_otp_sms(to_phone: str, code: str) -> tuple[bool, str | None, str | None]:
    provider = settings.sms_provider.lower()
    if provider == "msg91" or (provider == "auto" and msg91_configured()):
        return await send_msg91_otp(to_phone, code)
    return await send_sms(
        to_phone, f"{code} is your Gropto ERP verification code. It expires in {settings.otp_expiry_minutes} minutes."
    )
