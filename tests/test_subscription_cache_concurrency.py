import asyncio

import pytest

from http_client_errors import UpstreamTimeoutError
from subscription_cache import SubscriptionCache, SubscriptionCacheClosedError


def result(value="payload"):
    return value, "application/json", {}


@pytest.mark.asyncio
async def test_fifty_same_key_misses_share_one_build():
    cache = SubscriptionCache()
    key = await cache.make_key("same")
    gate = asyncio.Event()
    calls = 0

    async def build():
        nonlocal calls
        calls += 1
        await gate.wait()
        return result()

    tasks = [asyncio.create_task(cache.get_or_build(key, build)) for _ in range(50)]
    await asyncio.sleep(0)
    gate.set()
    values = await asyncio.gather(*tasks)
    assert calls == 1
    assert len(values) == 50
    assert (await cache.stats())["coalesced_waiters"] == 49


@pytest.mark.asyncio
async def test_twenty_different_keys_build_in_parallel():
    cache = SubscriptionCache()
    keys = [await cache.make_key(str(index)) for index in range(20)]
    all_started = asyncio.Event()
    active = 0
    peak = 0

    async def build():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 20:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        active -= 1
        return result()

    await asyncio.gather(*(cache.get_or_build(key, build) for key in keys))
    assert peak == 20


@pytest.mark.asyncio
async def test_eviction_does_not_cancel_inflight_build():
    cache = SubscriptionCache(capacity=1)
    active_key = await cache.make_key("active")
    other_key = await cache.make_key("other")
    started = asyncio.Event()
    finish = asyncio.Event()

    async def active_build():
        started.set()
        await finish.wait()
        return result("active")

    active = asyncio.create_task(cache.get_or_build(active_key, active_build))
    await started.wait()

    async def other_build():
        return result("other")

    assert (await cache.get_or_build(other_key, other_build))[0] == "other"
    finish.set()
    assert (await active)[0] == "active"


@pytest.mark.asyncio
async def test_same_key_failure_is_shared_and_next_call_retries():
    cache = SubscriptionCache()
    key = await cache.make_key("same")
    gate = asyncio.Event()
    calls = 0

    async def fail():
        nonlocal calls
        calls += 1
        await gate.wait()
        raise UpstreamTimeoutError("timeout")

    tasks = [asyncio.create_task(cache.get_or_build(key, fail)) for _ in range(20)]
    await asyncio.sleep(0)
    gate.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert calls == 1
    assert all(isinstance(value, UpstreamTimeoutError) for value in outcomes)
    assert (await cache.stats())["inflight"] == 0

    async def recover():
        nonlocal calls
        calls += 1
        return result("recovered")

    assert (await cache.get_or_build(key, recover))[0] == "recovered"
    assert calls == 2


@pytest.mark.asyncio
async def test_cancelling_one_waiter_does_not_cancel_shared_build():
    cache = SubscriptionCache()
    key = await cache.make_key("same")
    gate = asyncio.Event()

    async def build():
        await gate.wait()
        return result()

    cancelled = asyncio.create_task(cache.get_or_build(key, build))
    survivor = asyncio.create_task(cache.get_or_build(key, build))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    gate.set()
    assert (await survivor)[0] == "payload"


@pytest.mark.asyncio
async def test_cancelled_builder_is_cleaned_up_and_can_retry():
    cache = SubscriptionCache()
    key = await cache.make_key("same")
    started = asyncio.Event()

    async def build():
        started.set()
        await asyncio.Event().wait()

    waiter = asyncio.create_task(cache.get_or_build(key, build))
    await started.wait()
    inflight = next(iter(cache._inflight.values()))
    inflight.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (await cache.stats())["inflight"] == 0

    async def recover():
        return result("recovered")

    assert (await cache.get_or_build(key, recover))[0] == "recovered"


@pytest.mark.asyncio
async def test_concurrent_stale_waiters_share_refresh_and_receive_stale():
    now = [0.0]
    cache = SubscriptionCache(fresh_ttl=1, stale_window=10, clock=lambda: now[0])
    key = await cache.make_key("same")

    async def initial():
        return result("stale")

    await cache.get_or_build(key, initial)
    now[0] = 1
    gate = asyncio.Event()
    calls = 0

    async def fail():
        nonlocal calls
        calls += 1
        await gate.wait()
        raise UpstreamTimeoutError("timeout")

    tasks = [asyncio.create_task(cache.get_or_build(key, fail)) for _ in range(20)]
    await asyncio.sleep(0)
    gate.set()
    values = await asyncio.gather(*tasks)
    assert calls == 1
    assert {value[0] for value in values} == {"stale"}


@pytest.mark.asyncio
async def test_concurrent_stale_waiters_share_successful_refresh():
    now = [0.0]
    cache = SubscriptionCache(fresh_ttl=1, stale_window=10, clock=lambda: now[0])
    key = await cache.make_key("same")

    async def initial():
        return result("stale")

    await cache.get_or_build(key, initial)
    now[0] = 1
    gate = asyncio.Event()
    calls = 0

    async def refresh():
        nonlocal calls
        calls += 1
        await gate.wait()
        return result("fresh")

    tasks = [asyncio.create_task(cache.get_or_build(key, refresh)) for _ in range(20)]
    await asyncio.sleep(0)
    gate.set()
    values = await asyncio.gather(*tasks)
    assert calls == 1
    assert {value[0] for value in values} == {"fresh"}
    assert (await cache.get_or_build(key, refresh))[0] == "fresh"
    assert calls == 1


@pytest.mark.asyncio
async def test_close_cancels_builds_clears_state_and_rejects_work():
    cache = SubscriptionCache()
    key = await cache.make_key("same")
    started = asyncio.Event()

    async def build():
        started.set()
        await asyncio.Event().wait()

    waiter = asyncio.create_task(cache.get_or_build(key, build))
    await started.wait()
    await cache.close()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (await cache.stats())["inflight"] == 0
    with pytest.raises(SubscriptionCacheClosedError):
        await cache.make_key("new")
