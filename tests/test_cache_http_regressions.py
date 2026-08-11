import asyncio
import json

import httpx
import pytest

import api_client
from http_client_errors import (
    UpstreamConnectionError,
    UpstreamResponseError,
    UpstreamTimeoutError,
)
from http_clients import HttpClientManager
from subscription_cache import SubscriptionCache


class TrackingClientFactory:
    def __init__(self, handler):
        self.transport = httpx.MockTransport(handler)
        self.clients = []

    def __call__(self, *args, **kwargs):
        client = httpx.AsyncClient(
            transport=self.transport,
            timeout=kwargs["timeout"],
            limits=kwargs["limits"],
            follow_redirects=kwargs["follow_redirects"],
        )
        self.clients.append(client)
        return client


async def run_managed(factory, action):
    manager = HttpClientManager(
        env_getter=lambda key, default="": default,
        client_factory=factory,
    )
    await manager.start()
    try:
        return await action(manager)
    finally:
        await manager.close()


@pytest.fixture(autouse=True)
def reset_http_state(monkeypatch):
    def env_get(key, default=""):
        if key == "XUI_SUB_URL":
            return "https://subscriptions.example.test"
        return default

    monkeypatch.setattr(api_client, "env_get", env_get)
    yield


def test_successful_json_fetch_reuses_and_closes_managed_client():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[{"name": "node"}], headers={"Content-Type": "application/json"})

    factory = TrackingClientFactory(handler)

    async def exercise(manager):
        first = await api_client.fetch_original_subscription(
            "sub/with slash", "client=happ", client_manager=manager
        )
        await api_client.fetch_original_subscription("other", client_manager=manager)
        return first

    text, content_type, _ = asyncio.run(run_managed(factory, exercise))

    assert json.loads(text) == [{"name": "node"}]
    assert content_type == "application/json"
    assert requests[0].url.raw_path.split(b"?", 1)[0] == b"/json/sub%2Fwith%20slash"
    assert requests[0].url.query == b"client=happ"
    assert len(factory.clients) == 1
    assert factory.clients[0].is_closed


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_subscription_http_errors_are_safe_and_not_cached(status):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="upstream error")

    async def exercise(manager):
        for _ in range(2):
            with pytest.raises(UpstreamResponseError):
                await api_client.fetch_original_subscription("error-sub", client_manager=manager)

    asyncio.run(run_managed(TrackingClientFactory(handler), exercise))
    assert calls == 2


@pytest.mark.parametrize(
    ("exception_factory", "expected"),
    [
        (lambda request: httpx.ConnectTimeout("connect timeout", request=request), UpstreamTimeoutError),
        (lambda request: httpx.ReadTimeout("read timeout", request=request), UpstreamTimeoutError),
        (lambda request: httpx.WriteTimeout("write timeout", request=request), UpstreamTimeoutError),
        (lambda request: httpx.PoolTimeout("pool timeout", request=request), UpstreamTimeoutError),
        (lambda request: httpx.TransportError("transport failed", request=request), UpstreamConnectionError),
    ],
)
def test_transport_failures_are_mapped(exception_factory, expected):
    def handler(request):
        raise exception_factory(request)

    async def exercise(manager):
        with pytest.raises(expected):
            await api_client.fetch_original_subscription("transport-sub", client_manager=manager)

    asyncio.run(run_managed(TrackingClientFactory(handler), exercise))


def test_redirect_is_not_followed():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example.test/next"})

    async def exercise(manager):
        with pytest.raises(UpstreamResponseError):
            await api_client.fetch_original_subscription("redirect-sub", client_manager=manager)

    asyncio.run(run_managed(TrackingClientFactory(handler), exercise))
    assert len(requests) == 1


@pytest.mark.parametrize("body", ["", "not-json", "{}"])
def test_empty_or_invalid_subscription_body_fails_normalization(body):
    factory = TrackingClientFactory(
        lambda request: httpx.Response(200, text=body, headers={"Content-Type": "application/json"})
    )

    async def exercise(manager):
        return await api_client.fetch_original_subscription("invalid-body", client_manager=manager)

    text, _, _ = asyncio.run(run_managed(factory, exercise))
    with pytest.raises((json.JSONDecodeError, ValueError)):
        api_client.normalize_subscription(text)


def test_concurrent_cache_miss_is_single_flight():
    calls = 0

    async def build():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return "[]", "application/json", {}

    async def exercise():
        cache = SubscriptionCache()
        key = await cache.make_key("stampede")
        await asyncio.gather(
            *(cache.get_or_build(key, build) for _ in range(50))
        )
        await cache.close()

    asyncio.run(exercise())
    assert calls == 1
