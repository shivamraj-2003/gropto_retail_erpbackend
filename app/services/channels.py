"""Multi-channel campaign delivery (Point 11 audit fix: SMS/email/push were
hardcoded stubs that always failed, and a working Resend email client
existed elsewhere in the codebase but was never wired in here). All four
channels now go through a real provider client, each gated by its own
is_configured() — unconfigured providers report a clear, honest failure per
recipient instead of fabricating a delivery."""

import logging

from app.services import email as email_service
from app.services import push as push_service
from app.services import sms as sms_service
from app.services import whatsapp

logger = logging.getLogger(__name__)


async def send_message(channel: str, to_phone: str, template_name: str, **kwargs) -> tuple[bool, str | None, str | None]:
    """Unified send interface. Returns (ok, provider_message_id, error)."""
    if channel == "whatsapp":
        return await whatsapp.send_template_message(to_phone, template_name)
    elif channel == "sms":
        return await _send_sms(to_phone, template_name, **kwargs)
    elif channel == "email":
        email = kwargs.get("email")
        if not email:
            return False, None, "Customer has no email address on file"
        return await _send_email(email, template_name, **kwargs)
    elif channel == "push":
        push_token = kwargs.get("push_token")
        if not push_token:
            return False, None, "Customer has no push device token on file"
        return await _send_push(push_token, template_name, **kwargs)
    else:
        return False, None, f"Unknown channel: {channel}"


async def _send_sms(to_phone: str, template_name: str, **kwargs) -> tuple[bool, str | None, str | None]:
    if not sms_service.is_configured():
        logger.warning(f"SMS send attempted to {to_phone} but Twilio is not configured")
        return False, None, "SMS provider not configured (set TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER)"
    body = kwargs.get("body") or template_name
    return await sms_service.send_sms(to_phone, body)


async def _send_email(to_email: str, template_name: str, **kwargs) -> tuple[bool, str | None, str | None]:
    if not email_service.is_configured():
        logger.warning(f"Email send attempted to {to_email} but Resend is not configured")
        return False, None, "Email provider not configured (set RESEND_API_KEY)"
    subject = kwargs.get("subject") or template_name
    html = kwargs.get("html") or f"<p>{template_name}</p>"
    ok, error = await email_service.send_email(to_email, subject, html)
    return ok, None, error


async def _send_push(to_token: str, template_name: str, **kwargs) -> tuple[bool, str | None, str | None]:
    if not push_service.is_configured():
        logger.warning(f"Push send attempted for token {to_token[:8]}… but FCM is not configured")
        return False, None, "Push notification provider not configured (set FCM_SERVER_KEY)"
    title = kwargs.get("title") or "Gropto"
    body = kwargs.get("body") or template_name
    return await push_service.send_push(to_token, title, body)


def get_supported_channels() -> list[dict]:
    return [
        {"id": "whatsapp", "name": "WhatsApp", "configured": whatsapp.is_configured(), "status": "active" if whatsapp.is_configured() else "pending_provider"},
        {"id": "sms", "name": "SMS", "configured": sms_service.is_configured(), "status": "active" if sms_service.is_configured() else "pending_provider"},
        {"id": "email", "name": "Email", "configured": email_service.is_configured(), "status": "active" if email_service.is_configured() else "pending_provider"},
        {"id": "push", "name": "Push Notification", "configured": push_service.is_configured(), "status": "active" if push_service.is_configured() else "pending_provider"},
    ]
