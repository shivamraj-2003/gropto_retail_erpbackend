"""One-time codes (password reset today) delivered by email, SMS or WhatsApp,
with resend throttling on the shared key-value store:

* a cooldown between sends (OTP_RESEND_COOLDOWN_SECONDS), and
* a cap per rolling hour (OTP_MAX_SENDS_PER_HOUR),

both keyed by the identifier the caller typed — applied whether or not the
account exists, so the throttle can't be used to probe for accounts. Only a
hash of the code is stored (password_reset_otps), and issuing a new code
invalidates every older one.
"""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.kv import get_kv, key
from app.core.security import hash_password
from app.models.models import PasswordResetOtp, User
from app.services import email as email_service
from app.services import notify
from app.services import sms as sms_service
from app.services import whatsapp as whatsapp_service

CHANNELS = ("email", "sms", "whatsapp")


def available_channels(user: User) -> list[str]:
    out = []
    if user.email and email_service.is_configured():
        out.append("email")
    if user.phone and sms_service.otp_sms_configured():
        out.append("sms")
    if user.phone and whatsapp_service.is_configured() and settings.whatsapp_otp_template:
        out.append("whatsapp")
    return out


def _mask(channel: str, user: User) -> str:
    if channel == "email" and user.email:
        name, _, domain = user.email.partition("@")
        return f"{name[:2]}***@{domain}"
    phone = user.phone or ""
    return f"******{phone[-4:]}" if len(phone) >= 4 else "your phone"


async def throttle(identifier: str, purpose: str) -> None:
    """Raises 429 when a code was sent too recently or too often."""
    ident = identifier.strip().lower()
    kv = get_kv()
    cooldown_key = key("otp", purpose, "cooldown", ident)
    if settings.otp_resend_cooldown_seconds > 0 and not await kv.set(cooldown_key, "1", ttl_seconds=settings.otp_resend_cooldown_seconds, nx=True):
        wait = max(await kv.ttl(cooldown_key), 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {wait} seconds before requesting another code.",
            headers={"Retry-After": str(wait)},
        )
    sent = await kv.incr(key("otp", purpose, "hourly", ident), 3600)
    if sent > settings.otp_max_sends_per_hour:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many codes requested. Try again in an hour.",
            headers={"Retry-After": "3600"},
        )


async def issue_password_reset_otp(db: AsyncSession, user: User, channel: str = "auto") -> tuple[str, str]:
    """Creates and sends a code. Returns (channel_used, masked_destination).
    Raises 400 if the requested channel isn't usable for this account."""
    channels = available_channels(user)
    if channel == "auto":
        if not channels:
            raise HTTPException(status_code=400, detail="No delivery channel is configured for this account")
        channel = channels[0]
    elif channel not in channels:
        raise HTTPException(status_code=400, detail=f"Cannot send a code by {channel} for this account")

    await db.execute(
        PasswordResetOtp.__table__.update()
        .where(PasswordResetOtp.user_id == user.id, PasswordResetOtp.used.is_(False))
        .values(used=True)
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        PasswordResetOtp(
            user_id=user.id,
            code_hash=hash_password(code),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.otp_expiry_minutes),
        )
    )

    if channel == "email":
        ok, error = await notify.send_email(
            user.email,
            "Your Gropto ERP verification code",
            f"<p>Your verification code is <strong>{code}</strong>. It expires in {settings.otp_expiry_minutes} minutes.</p>"
            "<p>If you didn't request this, you can ignore this email.</p>",
        )
    elif channel == "sms":
        ok, error = await notify.send_otp_sms(user.phone, code)
    else:
        ok, error = await notify.send_whatsapp_template(
            user.phone, settings.whatsapp_otp_template, settings.whatsapp_otp_language, [code]
        )
    if not ok:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not send the code by {channel}: {error}")
    return channel, _mask(channel, user)
