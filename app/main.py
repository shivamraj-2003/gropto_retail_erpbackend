from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.api.v1.router import api_router
from app.core.config import settings
from app.services.audit import set_current_ip
from app.services.rate_limit import is_request_rate_limited
from app.services.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Gropto Retail ERP API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Prefer the original client IP forwarded by a TLS-terminating proxy in
    # front of this service (§3 "Docker Compose ... Caddy for automatic
    # TLS") over the proxy's own socket address; falls back to the direct
    # connection for local/dev runs with no proxy in front.
    forwarded = request.headers.get("x-forwarded-for")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    set_current_ip(client_ip)

    # The connectivity probe (§14 "unauthenticated health endpoint") must never
    # itself be rate limited, or a flapping connection's own probing traffic
    # could lock a device out of finding out it's back online.
    if request.url.path not in ("/health", "/health/ready", "/api/v1/sync/health"):
        if await is_request_rate_limited(client_ip):
            return JSONResponse(status_code=429, content={"detail": "Too many requests"})
    return await call_next(request)


app.include_router(api_router)


def _is_pool_exhaustion(exc: BaseException) -> bool:
    """Supabase's Session Pooler on this project hard-caps concurrent
    connections; once exhausted, asyncpg surfaces it as a generic
    InternalServerError (EMAXCONNSESSION) since it isn't a Postgres error
    code asyncpg has a dedicated exception class for — SQLAlchemy may also
    wrap it in OperationalError depending on where in the connect path it
    happens. Either way this is the one DB failure mode that's guaranteed
    transient (a connection freeing up fixes it, no data or logic is
    involved), so it gets its own clear, retryable response instead of
    surfacing as an opaque 500 with a five-screen traceback."""
    text = str(exc)
    return "EMAXCONNSESSION" in text or "max clients reached" in text.lower()


@app.exception_handler(asyncpg.exceptions.InternalServerError)
@app.exception_handler(OperationalError)
async def db_pool_exhausted_handler(request: Request, exc: Exception) -> JSONResponse:
    if not _is_pool_exhaustion(exc):
        raise exc
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "2"},
        content={"detail": "Database is at its connection limit right now — please retry in a moment."},
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.environment}


@app.get("/health/ready")
async def readiness() -> JSONResponse:
    """Readiness probe for a load balancer / orchestrator: 200 only when the
    database answers and (if configured) Redis does. /health stays a cheap
    liveness check that never touches either."""
    from sqlalchemy import text

    from app.core.database import engine
    from app.core.kv import get_kv

    checks: dict[str, str] = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {type(exc).__name__}"
    kv = get_kv()
    try:
        primary = getattr(kv, "primary", kv)
        checks[f"kv:{kv.name}"] = "ok" if await primary.ping() else "error"
    except Exception as exc:  # noqa: BLE001
        checks[f"kv:{kv.name}"] = f"error: {type(exc).__name__}"
    ready = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if ready else 503, content={"ready": ready, "checks": checks})
