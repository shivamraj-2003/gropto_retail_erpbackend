"""Shared key-value store for state that must be consistent across API
processes: rate limits, login lockouts, OTP throttles, scheduler locks and the
RBAC cache version.

Backends, picked from settings at first use:
  * Redis over TCP (REDIS_URL, e.g. Upstash `rediss://…`) — preferred.
  * Upstash REST (UPSTASH_REDIS_REST_URL + _TOKEN) — same commands over HTTPS.
  * In-memory — nothing configured; correct for exactly one API process, which
    is how this app ran before Redis.

Every call fails *open* to the in-memory store if Redis errors, and logs it:
a Redis outage must degrade rate limiting, never take down login or billing.
"""

import logging
import time
from typing import Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger("gropto.kv")


class KV(Protocol):
    name: str

    async def incr(self, key: str, ttl_seconds: int) -> int: ...
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None, nx: bool = False) -> bool: ...
    async def delete(self, key: str) -> None: ...
    async def ttl(self, key: str) -> int: ...
    async def ping(self) -> bool: ...


class MemoryKV:
    name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}

    def _live(self, key: str) -> tuple[str, float | None] | None:
        item = self._data.get(key)
        if item is None:
            return None
        if item[1] is not None and item[1] <= time.monotonic():
            self._data.pop(key, None)
            return None
        return item

    async def incr(self, key: str, ttl_seconds: int) -> int:
        item = self._live(key)
        if item is None:
            self._data[key] = ("1", time.monotonic() + ttl_seconds)
            return 1
        value = int(item[0]) + 1
        self._data[key] = (str(value), item[1])
        return value

    async def get(self, key: str) -> str | None:
        item = self._live(key)
        return item[0] if item else None

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None, nx: bool = False) -> bool:
        if nx and self._live(key) is not None:
            return False
        self._data[key] = (value, time.monotonic() + ttl_seconds if ttl_seconds else None)
        return True

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def ttl(self, key: str) -> int:
        item = self._live(key)
        if item is None:
            return -2
        return -1 if item[1] is None else max(int(item[1] - time.monotonic()), 0)

    async def ping(self) -> bool:
        return True


class RedisKV:
    name = "redis"

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._r = aioredis.from_url(
            url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3, health_check_interval=30
        )

    async def incr(self, key: str, ttl_seconds: int) -> int:
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl_seconds, nx=True)
            value, _ = await pipe.execute()
        return int(value)

    async def get(self, key: str) -> str | None:
        return await self._r.get(key)

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None, nx: bool = False) -> bool:
        return bool(await self._r.set(key, value, ex=ttl_seconds, nx=nx))

    async def delete(self, key: str) -> None:
        await self._r.delete(key)

    async def ttl(self, key: str) -> int:
        return int(await self._r.ttl(key))

    async def ping(self) -> bool:
        return bool(await self._r.ping())


class UpstashRestKV:
    """Upstash's REST API: POST a JSON command array to the base URL, or an
    array of them to /pipeline."""

    name = "upstash-rest"

    def __init__(self, url: str, token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=3
        )

    async def _cmd(self, *args: str | int):
        resp = await self._client.post("", json=[str(a) for a in args])
        resp.raise_for_status()
        return resp.json().get("result")

    async def incr(self, key: str, ttl_seconds: int) -> int:
        resp = await self._client.post("/pipeline", json=[["INCR", key], ["EXPIRE", key, str(ttl_seconds), "NX"]])
        resp.raise_for_status()
        return int(resp.json()[0]["result"])

    async def get(self, key: str) -> str | None:
        return await self._cmd("GET", key)

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None, nx: bool = False) -> bool:
        args: list[str | int] = ["SET", key, value]
        if ttl_seconds:
            args += ["EX", ttl_seconds]
        if nx:
            args.append("NX")
        return (await self._cmd(*args)) == "OK"

    async def delete(self, key: str) -> None:
        await self._cmd("DEL", key)

    async def ttl(self, key: str) -> int:
        return int(await self._cmd("TTL", key))

    async def ping(self) -> bool:
        return (await self._cmd("PING")) == "PONG"


class _FailOpen:
    """Wraps the remote backend: any error falls back to the in-memory store."""

    def __init__(self, primary: KV, fallback: MemoryKV) -> None:
        self.primary, self.fallback = primary, fallback
        self.name = primary.name
        self._last_warn = 0.0

    def _warn(self, exc: Exception) -> None:
        if time.monotonic() - self._last_warn > 60:
            self._last_warn = time.monotonic()
            logger.error("Redis (%s) unavailable, falling back to in-process state: %s", self.primary.name, exc)

    def __getattr__(self, method: str):
        async def call(*args, **kwargs):
            try:
                return await getattr(self.primary, method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — fail open by design
                self._warn(exc)
                return await getattr(self.fallback, method)(*args, **kwargs)

        return call


_kv: KV | None = None


def get_kv() -> KV:
    global _kv
    if _kv is None:
        memory = MemoryKV()
        if settings.redis_url:
            _kv = _FailOpen(RedisKV(settings.redis_url), memory)  # type: ignore[assignment]
        elif settings.upstash_redis_rest_url and settings.upstash_redis_rest_token:
            _kv = _FailOpen(UpstashRestKV(settings.upstash_redis_rest_url, settings.upstash_redis_rest_token), memory)  # type: ignore[assignment]
        else:
            _kv = memory
        logger.info("Key-value backend: %s", _kv.name)
    return _kv


def is_shared() -> bool:
    """True when state is shared across processes (any Redis backend)."""
    return get_kv().name != "memory"


def key(*parts: str) -> str:
    return ":".join((settings.redis_key_prefix, *parts))
