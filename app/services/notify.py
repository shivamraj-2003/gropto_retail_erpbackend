"""One entry point for outbound messages. With REDIS_URL set (and
CELERY_ENABLED not false) messages are queued to the Celery `notifications`
worker — the API request returns immediately and failed sends retry with
backoff. Without it they're sent inline, exactly as before.

Returns (accepted, error): `accepted` means queued or delivered; an inline
provider failure comes back as (False, error).
"""

import asyncio
import logging

from app.core.config import settings
from app.services import email as email_service
from app.services import sms as sms_service
from app.services import whatsapp as whatsapp_service

logger = logging.getLogger("gropto.notify")


def queue_enabled() -> bool:
    return bool(settings.celery_enabled and settings.redis_url)


async def _enqueue(task_name: str, *args) -> tuple[bool, str | None]:
    from app.worker import tasks

    task = getattr(tasks, task_name)
    try:
        # .delay() does blocking network I/O to the broker — keep it off the event loop.
        await asyncio.to_thread(task.delay, *args)
        return True, None
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not queue %s, sending inline: %s", task_name, exc)
        return False, str(exc)


async def send_email(to: str, subject: str, html: str) -> tuple[bool, str | None]:
    if queue_enabled():
        ok, _ = await _enqueue("send_email_task", to, subject, html)
        if ok:
            return True, None
    return await email_service.send_email(to, subject, html)


async def send_otp_sms(to: str, code: str) -> tuple[bool, str | None]:
    if queue_enabled():
        ok, _ = await _enqueue("send_otp_sms_task", to, code)
        if ok:
            return True, None
    ok, _id, error = await sms_service.send_otp_sms(to, code)
    return ok, error


async def send_whatsapp_template(to: str, template: str, language: str, params: list[str]) -> tuple[bool, str | None]:
    if queue_enabled():
        ok, _ = await _enqueue("send_whatsapp_template_task", to, template, language, params)
        if ok:
            return True, None
    ok, _id, error = await whatsapp_service.send_template_message(to, template, language, params)
    return ok, error
