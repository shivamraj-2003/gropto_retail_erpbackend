from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.services.audit import set_current_ip
from app.services.rate_limit import is_request_rate_limited

app = FastAPI(title="Gropto Retail ERP API", version="1.0.0")

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
    if request.url.path not in ("/health", "/api/v1/sync/health"):
        if is_request_rate_limited(client_ip):
            return JSONResponse(status_code=429, content={"detail": "Too many requests"})
    return await call_next(request)


app.include_router(api_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.environment}
