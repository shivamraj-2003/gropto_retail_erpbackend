"""WhatsApp Business Cloud API (Meta) — the only channel campaigns can
actually send through today. SMS/Push/Email stay UI-only until a provider is
picked for each (see CRM campaign send: it refuses non-whatsapp channels
outright rather than pretending to send).

Marketing messages to a customer outside the 24-hour service window MUST use
a pre-approved message template (Meta's rule, not this app's) — free-form
text only works as a reply within an open conversation. So a campaign send
always goes through /messages with type=template; the template itself is
created and approved in Meta Business Manager ahead of time, this just
invokes it by name.
"""

import httpx

from app.core.config import settings


def is_configured() -> bool:
    return bool(settings.whatsapp_phone_number_id and settings.whatsapp_access_token)


async def send_template_message(
    to_phone: str, template_name: str, language_code: str = "en_US", body_params: list[str] | None = None
) -> tuple[bool, str | None, str | None]:
    """Returns (ok, provider_message_id, error)."""
    url = f"https://graph.facebook.com/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
    components = []
    if body_params:
        components.append({"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]})
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {"name": template_name, "language": {"code": language_code}, "components": components},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url, json=payload, headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"}
            )
        data = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
        if resp.status_code >= 400:
            error = data.get("error", {}).get("message", resp.text[:300])
            return False, None, error
        message_id = (data.get("messages") or [{}])[0].get("id")
        return True, message_id, None
    except httpx.HTTPError as exc:
        return False, None, str(exc)
