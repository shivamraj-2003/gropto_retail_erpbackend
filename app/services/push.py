"""Push notifications via Firebase Cloud Messaging (legacy HTTP server-key
API) — Point 11 audit fix. Previously a hardcoded stub with no real client.
Same is_configured() gate as every other provider in this codebase. Note:
this app has no device-token registry for customers yet, so `to_token` is
passed in by the caller (campaign send currently has no token source — see
services/crm.py for how this is surfaced as "no device token on file" rather
than silently skipped)."""

import httpx

from app.core.config import settings


def is_configured() -> bool:
    return bool(settings.fcm_server_key)


async def send_push(to_token: str, title: str, body: str) -> tuple[bool, str | None, str | None]:
    """Returns (ok, provider_message_id, error)."""
    url = "https://fcm.googleapis.com/fcm/send"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                json={"to": to_token, "notification": {"title": title, "body": body}},
                headers={"Authorization": f"key={settings.fcm_server_key}", "Content-Type": "application/json"},
            )
        data = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
        if resp.status_code >= 400 or data.get("failure", 0) > 0:
            error = (data.get("results") or [{}])[0].get("error", resp.text[:300])
            return False, None, error
        return True, data.get("multicast_id") and str(data["multicast_id"]), None
    except httpx.HTTPError as exc:
        return False, None, str(exc)
