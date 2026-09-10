"""Shared, fail-closed query budgets. Redis coordinates; PostgreSQL owns knowledge."""
import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings

redis = Redis.from_url(settings.redis_url, socket_timeout=settings.dependency_timeout_seconds,
                       socket_connect_timeout=settings.dependency_timeout_seconds)

# Use Redis time and a single atomic operation so replicas share the same budget.
ACQUIRE = """
local now = tonumber(redis.call('TIME')[1])
local window = math.floor(now / 60)
local count = 0
if tonumber(redis.call('HGET', KEYS[3], 'window')) == window then
  count = tonumber(redis.call('HGET', KEYS[3], 'count') or '0')
end
for i = 1, 2 do redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now) end
if count >= tonumber(ARGV[4]) then return {0, 60 - now % 60} end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) or
   redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then return {0, 1} end
for i = 1, 2 do
  redis.call('ZADD', KEYS[i], now + tonumber(ARGV[5]), ARGV[1])
  redis.call('EXPIRE', KEYS[i], tonumber(ARGV[5]) + 1)
end
redis.call('HSET', KEYS[3], 'window', window, 'count', count + 1)
redis.call('EXPIRE', KEYS[3], 61)
return {1, 0}
"""
RENEW = """
local now = tonumber(redis.call('TIME')[1])
for i = 1, 2 do
  if not redis.call('ZSCORE', KEYS[i], ARGV[1]) then return 0 end
end
for i = 1, 2 do
  redis.call('ZADD', KEYS[i], now + tonumber(ARGV[2]), ARGV[1])
  redis.call('EXPIRE', KEYS[i], tonumber(ARGV[2]) + 1)
end
return 1
"""


async def _renew(keys, lease, lifetime, owner):
    try:
        while True:
            await asyncio.sleep(min(15, lifetime / 3))
            if not await redis.eval(RENEW, 2, *keys[:2], lease, lifetime):
                owner.cancel()
                return
    except RedisError:
        owner.cancel()


def budget(principal):
    service = principal.source == "service"
    kind = "service" if service else "employee"
    tenant = sha256(principal.tenant_id.encode()).hexdigest()
    actor = sha256(f"{principal.tenant_id}\0{principal.subject}".encode()).hexdigest()
    prefix = "knowledge:{admission}:" + kind
    keys = [prefix, f"{prefix}:tenant:{tenant}", f"{prefix}:actor:{actor}"]
    limits = (settings.service_concurrency, settings.tenant_service_concurrency,
              settings.service_requests_per_minute) if service else (
                  settings.employee_concurrency, settings.tenant_employee_concurrency,
                  settings.employee_requests_per_minute)
    return keys, limits


async def run_sync(function, *args):
    """Do not release admission while a non-cancellable thread still owns resources.

    Synchronous clients must have bounded connect/socket/statement timeouts.
    Cancellation waits for their cleanup, which can extend the response deadline.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Retrieve failures without replacing cancellation.
        raise


@asynccontextmanager
async def admit(principal):
    keys, limits = budget(principal)
    lease = uuid4().hex
    lifetime = int(settings.request_timeout_seconds) + 30
    try:
        accepted, retry = await redis.eval(
            ACQUIRE, len(keys), *keys, lease, *limits, lifetime)
    except RedisError as exc:
        raise HTTPException(503, "Query admission unavailable", headers={"Retry-After": "5"}) from exc
    if not accepted:
        raise HTTPException(429, "Query budget exceeded", headers={"Retry-After": str(retry)})
    renewal = asyncio.create_task(_renew(keys, lease, lifetime, asyncio.current_task()))
    try:
        async with asyncio.timeout(settings.request_timeout_seconds):
            yield
    except TimeoutError as exc:
        raise HTTPException(504, "Query deadline exceeded") from exc
    finally:
        renewal.cancel()
        try:
            await renewal
        except asyncio.CancelledError:
            pass
        # Expiring leases also recover capacity after a process crash.
        try:
            async with asyncio.timeout(settings.dependency_timeout_seconds):
                async with redis.pipeline(transaction=True) as pipe:
                    for key in keys[:2]:
                        pipe.zrem(key, lease)
                    await pipe.execute()
        except (RedisError, TimeoutError):
            pass
