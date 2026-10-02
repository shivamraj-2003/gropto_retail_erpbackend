"""Shared key-value layer, rate limiting, login lockout and OTP throttling —
exercised on the in-memory backend plus the fail-open wrapper. No database,
no Redis."""

import pytest
from fastapi import HTTPException

from app.core import kv as kv_module
from app.core.kv import MemoryKV, _FailOpen
from app.services import otp, rate_limit


@pytest.fixture(autouse=True)
def fresh_memory_kv(monkeypatch):
    store = MemoryKV()
    monkeypatch.setattr(kv_module, "_kv", store)
    return store


async def test_memory_kv_incr_set_nx_ttl():
    store = MemoryKV()
    assert await store.incr("a", 60) == 1
    assert await store.incr("a", 60) == 2
    assert await store.set("lock", "x", ttl_seconds=60, nx=True)
    assert not await store.set("lock", "y", ttl_seconds=60, nx=True)
    assert 0 < await store.ttl("lock") <= 60
    await store.delete("lock")
    assert await store.ttl("lock") == -2


async def test_fail_open_uses_memory_when_redis_errors():
    class Broken:
        name = "redis"

        async def incr(self, *_a, **_k):
            raise ConnectionError("down")

    wrapped = _FailOpen(Broken(), MemoryKV())
    assert await wrapped.incr("k", 60) == 1


async def test_login_lockout_after_threshold_and_reset_on_success():
    for _ in range(rate_limit.LOGIN_LOCKOUT_THRESHOLD):
        assert await rate_limit.is_login_locked("a@b.c", "dev1") is None
        await rate_limit.record_login_failure("a@b.c", "dev1")
    assert await rate_limit.is_login_locked("A@B.C", "dev1")  # identifier is case-insensitive
    assert await rate_limit.is_login_locked("a@b.c", "dev2") is None
    await rate_limit.record_login_success("a@b.c", "dev1")
    assert await rate_limit.is_login_locked("a@b.c", "dev1") is None


async def test_request_rate_limit():
    for _ in range(rate_limit.REQUEST_LIMIT_PER_WINDOW):
        assert not await rate_limit.is_request_rate_limited("10.0.0.1")
    assert await rate_limit.is_request_rate_limited("10.0.0.1")
    assert not await rate_limit.is_request_rate_limited("10.0.0.2")


async def test_otp_resend_cooldown_and_hourly_cap(monkeypatch):
    await otp.throttle("user@x.com", "password_reset")
    with pytest.raises(HTTPException) as exc:
        await otp.throttle("USER@x.com", "password_reset")
    assert exc.value.status_code == 429 and "wait" in exc.value.detail

    monkeypatch.setattr(otp.settings, "otp_resend_cooldown_seconds", 0)
    monkeypatch.setattr(otp.settings, "otp_max_sends_per_hour", 2)
    await otp.throttle("other@x.com", "password_reset")
    await otp.throttle("other@x.com", "password_reset")
    with pytest.raises(HTTPException) as exc:
        await otp.throttle("other@x.com", "password_reset")
    assert "hour" in exc.value.detail
