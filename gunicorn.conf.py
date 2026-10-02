"""Gunicorn settings for the production API container.

Multiple workers are safe now that cross-process state lives in Redis
(rate limits, lockouts, OTP throttles, RBAC cache version, scheduler locks).
Each worker opens its own DB pools — keep
    WEB_CONCURRENCY x (5+5 main + 2+1 reporting)
under the database pooler's connection cap (Supabase session pooler on this
project: 15, i.e. ONE worker per container unless you switch DATABASE_URL to
the transaction pooler on port 6543 or raise the cap).
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
worker_class = "uvicorn.workers.UvicornWorker"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
timeout = 60
graceful_timeout = 30
keepalive = 5
# Recycle workers periodically to bound any slow memory growth.
max_requests = 2000
max_requests_jitter = 200
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
# Trust X-Forwarded-For from the reverse proxy in front (Caddy/Nginx/LB).
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "*")
