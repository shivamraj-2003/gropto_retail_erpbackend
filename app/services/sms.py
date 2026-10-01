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
