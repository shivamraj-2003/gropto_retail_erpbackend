"""Rate limiting and login-attempt lockout on the shared key-value store
(app/core/kv.py): Redis when configured, so the limits hold across every API
process/worker; the in-memory store otherwise (single-process deployments).
"""

from app.core.kv import get_kv, key

LOGIN_LOCKOUT_THRESHOLD = 5
LOGIN_LOCKOUT_WINDOW_SECONDS = 15 * 60
LOGIN_LOCKOUT_DURATION_SECONDS = 15 * 60


def _login_key(identifier: str, device_fingerprint: str) -> str:
    # Keyed by identifier+device so one compromised/misconfigured till can't lock
    # out every cashier at a store, and one user guessing across many devices
    # still gets caught by the identifier component.
    return f"{identifier.lower()}:{device_fingerprint}"


async def is_login_locked(identifier: str, device_fingerprint: str) -> float | None:
    """Returns remaining lockout seconds, or None if not locked."""
    remaining = await get_kv().ttl(key("lock", _login_key(identifier, device_fingerprint)))
    return float(remaining) if remaining > 0 else None


async def record_login_failure(identifier: str, device_fingerprint: str) -> None:
    k = _login_key(identifier, device_fingerprint)
    kv = get_kv()
    failures = await kv.incr(key("fail", k), LOGIN_LOCKOUT_WINDOW_SECONDS)
    if failures >= LOGIN_LOCKOUT_THRESHOLD:
        await kv.set(key("lock", k), "1", ttl_seconds=LOGIN_LOCKOUT_DURATION_SECONDS)
        await kv.delete(key("fail", k))


async def record_login_success(identifier: str, device_fingerprint: str) -> None:
    k = _login_key(identifier, device_fingerprint)
    kv = get_kv()
    await kv.delete(key("fail", k))
    await kv.delete(key("lock", k))


# ---------------------------------------------------------------------------
# General request rate limiting — a fixed-window counter per client IP.
# ---------------------------------------------------------------------------

REQUEST_LIMIT_PER_WINDOW = 300
REQUEST_WINDOW_SECONDS = 60


async def is_request_rate_limited(client_ip: str) -> bool:
    count = await get_kv().incr(key("rl", client_ip), REQUEST_WINDOW_SECONDS)
    return count > REQUEST_LIMIT_PER_WINDOW
