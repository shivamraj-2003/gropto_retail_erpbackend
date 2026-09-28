"""Email via Resend — a single API key, no SMTP server to run. Left
unconfigured in dev, in which case forgot-password falls back to flagging
the request for an Admin/Super Admin to resolve directly instead of
pretending to send anything."""

import httpx

from app.core.config import settings


def is_configured() -> bool:
    return bool(settings.resend_api_key)


async def send_email(to: str, subject: str, html: str) -> tuple[bool, str | None]:
    """Returns (ok, error)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                json={"from": settings.resend_from_email, "to": [to], "subject": subject, "html": html},
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            )
        if resp.status_code >= 400:
            return False, resp.text[:300]
        return True, None
    except httpx.HTTPError as exc:
        return False, str(exc)
