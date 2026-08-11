import logging

import pytest

from http_client_errors import (
    UpstreamAuthenticationError,
    UpstreamConnectionError,
    UpstreamResponseError,
    UpstreamServerError,
    UpstreamTimeoutError,
)
from logging_utils import reset_request_id, set_request_id
from subscription_cache import SubscriptionCache


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def result(value):
    return value, "application/json", {"Subscription-Userinfo": "upload=1"}


@pytest.mark.asyncio
async def test_fresh_stale_and_expired_boundaries_are_exact():
    clock = Clock()
    cache = SubscriptionCache(fresh_ttl=30, stale_window=300, clock=clock)
    key = await cache.make_key("secret-id")
    calls = 0

    async def build():
        nonlocal calls
        calls += 1
        return result(f"value-{calls}")

    assert (await cache.get_or_build(key, build))[0] == "value-1"
    clock.now = 129.999
    assert (await cache.get_or_build(key, build))[0] == "value-1"
    clock.now = 130.0
    assert (await cache.get_or_build(key, build))[0] == "value-2"
    clock.now = 460.0
    assert (await cache.get_or_build(key, build))[0] == "value-3"
    assert calls == 3


@pytest.mark.asyncio
async def test_lru_evicts_only_least_recent_entry():
    cache = SubscriptionCache(capacity=3)
    keys = {name: await cache.make_key(name) for name in "ABCD"}
    calls = {name: 0 for name in keys}

    async def load(name):
        async def build():
            calls[name] += 1
            return result(name)

        return await cache.get_or_build(keys[name], build)

    for name in "ABC":
        await load(name)
    await load("A")
    await load("D")
    assert (await cache.stats())["evictions"] == 1
    await load("A")
    await load("C")
    await load("B")

    assert calls == {"A": 1, "B": 2, "C": 1, "D": 1}
    assert (await cache.stats())["evictions"] == 2


@pytest.mark.parametrize(
    "error_type",
    [UpstreamConnectionError, UpstreamTimeoutError, UpstreamServerError],
)
@pytest.mark.asyncio
async def test_stale_is_used_only_for_transient_failures(caplog, error_type):
    clock = Clock()
    cache = SubscriptionCache(fresh_ttl=1, stale_window=10, clock=clock)
    key = await cache.make_key("secret-id")

    async def initial():
        return result("stale")

    await cache.get_or_build(key, initial)
    clock.now += 2

    async def transient():
        raise RuntimeError("safe") from error_type("temporary upstream failure")

    caplog.set_level(logging.WARNING, logger="autosub")
    token = set_request_id("stale-request")
    try:
        assert (await cache.get_or_build(key, transient))[0] == "stale"
    finally:
        reset_request_id(token)
    warning = next(
        record for record in caplog.records if "Serving stale subscription" in record.message
    )
    assert warning.request_id == "stale-request"
    assert "secret-id" not in caplog.text

    for error in (
        UpstreamResponseError("bad response"),
        UpstreamAuthenticationError("invalid credentials"),
        RuntimeError("malformed local config"),
    ):
        async def permanent(error=error):
            raise error

        with pytest.raises(type(error)):
            await cache.get_or_build(key, permanent)


@pytest.mark.asyncio
async def test_successful_stale_refresh_becomes_fresh():
    clock = Clock()
    cache = SubscriptionCache(fresh_ttl=1, stale_window=10, clock=clock)
    key = await cache.make_key("id")
    calls = 0

    async def build():
        nonlocal calls
        calls += 1
        return result(f"value-{calls}")

    assert (await cache.get_or_build(key, build))[0] == "value-1"
    clock.now += 1
    assert (await cache.get_or_build(key, build))[0] == "value-2"
    assert (await cache.get_or_build(key, build))[0] == "value-2"
    assert calls == 2


@pytest.mark.parametrize("elapsed", [3, 4])
@pytest.mark.asyncio
async def test_expired_entry_does_not_mask_transient_error(elapsed):
    clock = Clock()
    cache = SubscriptionCache(fresh_ttl=1, stale_window=2, clock=clock)
    key = await cache.make_key("id")

    async def initial():
        return result("old")

    await cache.get_or_build(key, initial)
    clock.now += elapsed

    async def failing():
        raise UpstreamTimeoutError("timeout")

    with pytest.raises(UpstreamTimeoutError):
        await cache.get_or_build(key, failing)


@pytest.mark.asyncio
async def test_oversized_payload_is_returned_but_not_cached():
    cache = SubscriptionCache(max_payload_bytes=4)
    key = await cache.make_key("id")
    calls = 0

    async def build():
        nonlocal calls
        calls += 1
        return result("12345")

    await cache.get_or_build(key, build)
    await cache.get_or_build(key, build)
    assert calls == 2
    assert (await cache.stats())["entries"] == 0


@pytest.mark.asyncio
async def test_keys_are_hashed_and_invalidation_changes_generation():
    cache = SubscriptionCache()
    key = await cache.make_key("private-sub", "token=private", variant="password")
    assert "private" not in repr(key)
    await cache.invalidate()
    next_key = await cache.make_key("private-sub", "token=private", variant="password")
    assert next_key.generation == key.generation + 1
    assert (await cache.stats())["generation"] == 1


@pytest.mark.asyncio
async def test_cached_headers_are_materialized_per_response():
    cache = SubscriptionCache()
    key = await cache.make_key("id")

    async def build():
        return result("payload")

    first = await cache.get_or_build(key, build)
    first[2]["X-Request-ID"] = "request-one"
    second = await cache.get_or_build(key, build)
    assert "X-Request-ID" not in second[2]
