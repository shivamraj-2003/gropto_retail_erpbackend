"""Celery for outbound notifications (email, SMS, WhatsApp), with Redis
(REDIS_URL, e.g. Upstash rediss://) as broker and result backend.

Run a worker:   celery -A app.worker.celery_app worker --loglevel=info --pool=solo   (Windows)
                celery -A app.worker.celery_app worker --loglevel=info --concurrency=4  (Linux/Docker)

Scheduled jobs stay on APScheduler inside the API (with Redis locks so only
one worker runs each), so no `celery beat` is needed.
"""

import ssl

from celery import Celery

from app.core.config import settings

celery_app = Celery("gropto", broker=settings.redis_url or None, backend=settings.redis_url or None, include=["app.worker.tasks"])

_tls = {"ssl_cert_reqs": ssl.CERT_REQUIRED} if settings.redis_url.startswith("rediss://") else None

celery_app.conf.update(
    broker_use_ssl=_tls,
    redis_backend_use_ssl=_tls,
    broker_connection_retry_on_startup=True,
    task_default_queue="notifications",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    result_expires=3600,
    task_ignore_result=True,
    # Upstash closes idle connections; keep the broker transport resilient.
    broker_transport_options={"visibility_timeout": 3600, "socket_keepalive": True, "health_check_interval": 30},
    timezone="Asia/Kolkata",
)
