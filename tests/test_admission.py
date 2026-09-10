import asyncio
import os

import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError

from app import admission
from app.auth import Principal


class Redis:
    result = [1, 0]
    released = []

    async def eval(self, *args):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def pipeline(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def zrem(self, key, lease):
        self.released.append((key, lease))

    async def execute(self):
        return [1, 1]


def test_employee_and_service_budgets_are_separate():
    employee = Principal("actor", "tenant", frozenset())
    service = Principal("actor", "tenant", frozenset(), source="service")
    assert set(admission.budget(employee)[0]).isdisjoint(admission.budget(service)[0])


@pytest.mark.parametrize("result,status", [([0, 12], 429), (ConnectionError("unavailable"), 503)])
def test_budget_rejection_fails_closed(monkeypatch, result, status):
    fake = Redis()
    fake.result = result
    monkeypatch.setattr(admission, "redis", fake)

    async def run():
        with pytest.raises(HTTPException) as error:
            async with admission.admit(Principal("actor", "tenant", frozenset())):
                pytest.fail("rejected request executed")
        assert error.value.status_code == status
        assert int(error.value.headers["Retry-After"]) > 0

    asyncio.run(run())


def test_deadline_releases_both_leases(monkeypatch):
    fake = Redis()
    fake.released = []
    monkeypatch.setattr(admission, "redis", fake)
    monkeypatch.setattr(admission.settings, "request_timeout_seconds", 0.01)

    async def run():
        with pytest.raises(HTTPException) as error:
            async with admission.admit(Principal("actor", "tenant", frozenset())):
                await asyncio.sleep(1)
        assert error.value.status_code == 504
        assert len(fake.released) == 2

    asyncio.run(run())


@pytest.mark.parametrize("shared", [False, True])
def test_cancelled_thread_keeps_lease_until_resources_are_released(monkeypatch, shared):
    import threading

    started, finish = threading.Event(), threading.Event()
    fake = Redis()
    fake.released = []
    monkeypatch.setattr(admission, "redis", fake)

    def blocking():
        started.set()
        assert finish.wait(2)

    async def request():
        async with admission.admit(Principal("actor", "tenant", frozenset())):
            if shared:
                from app.cache import singleflight
                await singleflight("cancel-thread-test", lambda: admission.run_sync(blocking))
            else:
                await admission.run_sync(blocking)

    async def run():
        task = asyncio.create_task(request())
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            assert not fake.released
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(fake.released) == 2

    asyncio.run(run())


def test_lease_renewal_is_stopped_after_request(monkeypatch):
    fake = Redis()
    stopped = asyncio.Event()
    monkeypatch.setattr(admission, "redis", fake)

    async def renew(*args):
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    monkeypatch.setattr(admission, "_renew", renew)

    async def run():
        async with admission.admit(Principal("actor", "tenant", frozenset())):
            await asyncio.sleep(0)
        assert stopped.is_set()

    asyncio.run(run())


@pytest.mark.skipif(not os.getenv("TEST_REDIS_URL"), reason="requires isolated TEST_REDIS_URL")
def test_real_redis_atomic_concurrency_rate_and_expired_lease():
    from uuid import uuid4
    from redis.asyncio import Redis as RealRedis

    async def run():
        client = RealRedis.from_url(os.environ["TEST_REDIS_URL"])
        prefix = "test:{admission}:" + uuid4().hex
        keys = [prefix + suffix for suffix in (":global", ":tenant", ":actor")]
        try:
            outcomes = await asyncio.gather(*[
                client.eval(admission.ACQUIRE, 3, *keys, str(i), 2, 2, 10, 30) for i in range(10)
            ])
            assert sum(item[0] for item in outcomes) == 2
            assert await client.zcard(keys[0]) == 2
            members = await client.zrange(keys[0], 0, -1)
            assert await client.eval(admission.RENEW, 2, *keys[:2], members[0], 120) == 1
            now = (await client.time())[0]
            assert await client.zscore(keys[0], members[0]) >= now + 119
            assert await client.eval(admission.RENEW, 2, *keys[:2], "missing", 120) == 0
            await client.delete(*keys)
            assert (await client.eval(admission.ACQUIRE, 3, *keys, "first", 2, 2, 1, 30))[0] == 1
            for key in keys[:2]:
                await client.zrem(key, "first")
            assert (await client.eval(admission.ACQUIRE, 3, *keys, "second", 2, 2, 1, 30))[0] == 0
            await client.delete(*keys)
            for key in keys[:2]:
                await client.zadd(key, {"crashed": 1})
            assert (await client.eval(admission.ACQUIRE, 3, *keys, "new", 1, 1, 10, 30))[0] == 1
        finally:
            await client.delete(*keys)
            await client.aclose()

    asyncio.run(run())
