"""Notification tasks. Each wraps the existing async provider client and
retries with backoff on failure; a final failure is logged with the provider's
error. Tasks never receive OTP plaintext in logs (args aren't logged)."""

import asyncio
import logging

from app.services import email as email_service
from app.services import sms as sms_service
from app.services import whatsapp as whatsapp_service
from app.worker.celery_app import celery_app

logger = logging.getLogger("gropto.tasks")

_RETRY = dict(autoretry_for=(RuntimeError,), retry_backoff=10, retry_backoff_max=300, max_retries=4)


def _check(result: tuple, channel: str) -> None:
    ok, error = result[0], result[-1]
    if not ok:
        logger.warning("%s send failed: %s", channel, error)
        raise RuntimeError(f"{channel} send failed: {error}")


@celery_app.task(name="notify.email", **_RETRY)
def send_email_task(to: str, subject: str, html: str) -> None:
    _check(asyncio.run(email_service.send_email(to, subject, html)), "email")


@celery_app.task(name="notify.sms", **_RETRY)
def send_sms_task(to: str, body: str) -> None:
    _check(asyncio.run(sms_service.send_sms(to, body)), "sms")


@celery_app.task(name="notify.otp_sms", **_RETRY)
def send_otp_sms_task(to: str, code: str) -> None:
    _check(asyncio.run(sms_service.send_otp_sms(to, code)), "otp-sms")


@celery_app.task(name="notify.whatsapp_template", **_RETRY)
def send_whatsapp_template_task(to: str, template: str, language: str, params: list[str]) -> None:
    _check(asyncio.run(whatsapp_service.send_template_message(to, template, language, params)), "whatsapp")
