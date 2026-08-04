import asyncio
import json

import httpx
import pytest

import api_client


REAL_ASYNC_CLIENT = httpx.AsyncClient

class TrackingClientFactory:
    def __init__(self, handler):
        self.transport = httpx.MockTransport(handler)
        self.clients = []

    def __call__(self, *args, **kwargs):
        client = REAL_ASYNC_CLIENT(
            transport=self.transport,
            timeout=kwargs.get("timeout", 5.0),
            follow_redirects=kwargs.get("follow_redirects", False),
        )
        self.clients.append(client)
        return client


@pytest.fixture(autouse=True)
def reset_http_state(monkeypatch):
    api_client._sub_cache.clear()
    api_client._global_api = None

    def env_get(key, default=""):
        values = {
            "XUI_SUB_URL": "https://subscriptions.example.test",
            "XUI_API_URL": "https://panel.example.test",
            "XUI_API_TOKEN": "test-token",
            "XUI_TLS_VERIFY": "true",
        }
        return values.get(key, default)

    monkeypatch.setattr(api_client, "env_get", env_get)
    yield
    api_client._sub_cache.clear()
    api_client._global_api = None


def test_successful_json_fetch_uses_and_closes_one_client(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=[{"name": "node"}],
            headers={"Content-Type": "application/json"},
        )

    factory = TrackingClientFactory(handler)
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)

    text, content_type, _ = asyncio.run(
        api_client.fetch_original_subscription("sub/with slash", "client=happ")
    )

    assert json.loads(text) == [{"name": "node"}]
    assert content_type == "application/json"
    assert requests[0].url.raw_path.split(b"?", 1)[0] == b"/json/sub%2Fwith%20slash"
    assert requests[0].url.query == b"client=happ"
    assert len(factory.clients) == 1
    assert factory.clients[0].is_closed


def test_successful_html_fetch_preserves_status_and_closes_client(monkeypatch):
    factory = TrackingClientFactory(
        lambda request: httpx.Response(
            201, content=b"<html>ok</html>", headers={"Content-Type": "text/html"}
        )
    )
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)

    body, content_type, status = asyncio.run(
        api_client.fetch_original_sub_html("html-sub", {"accept": "text/html"})
    )

    assert body == "<html>ok</html>"
    assert content_type == "text/html"
    assert status == 201
    assert len(factory.clients) == 1
    assert factory.clients[0].is_closed


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_subscription_http_errors_are_raised_and_not_cached(monkeypatch, status):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="upstream error")

    factory = TrackingClientFactory(handler)
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)

    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(api_client.fetch_original_subscription("error-sub"))
        assert exc_info.value.response.status_code == status

    assert calls == 2
    assert "error-sub:" not in api_client._sub_cache


@pytest.mark.parametrize(
    "exception_factory",
    [
        lambda request: httpx.ConnectTimeout("connect timeout", request=request),
        lambda request: httpx.ReadTimeout("read timeout", request=request),
        lambda request: httpx.TransportError("TLS handshake failed", request=request),
    ],
)
def test_transport_failures_propagate_and_client_is_closed(monkeypatch, exception_factory):
    def handler(request):
        raise exception_factory(request)

    factory = TrackingClientFactory(handler)
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)

    with pytest.raises(httpx.TransportError):
        asyncio.run(api_client.fetch_original_subscription("transport-sub"))

    assert len(factory.clients) == 1
    assert factory.clients[0].is_closed


def test_redirect_is_not_followed(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example.test/next"})

    factory = TrackingClientFactory(handler)
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        asyncio.run(api_client.fetch_original_subscription("redirect-sub"))

    assert exc_info.value.response.status_code == 302
    assert len(requests) == 1


@pytest.mark.parametrize("body", ["", "not-json", "{}"])
def test_empty_or_invalid_subscription_body_fails_normalization(monkeypatch, body):
    factory = TrackingClientFactory(
        lambda request: httpx.Response(
            200, text=body, headers={"Content-Type": "application/json"}
        )
    )
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)

    text, _, _ = asyncio.run(api_client.fetch_original_subscription("invalid-body"))

    with pytest.raises((json.JSONDecodeError, ValueError)):
        api_client.normalize_subscription(text)


def test_cache_hit_avoids_second_upstream_request(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])

    factory = TrackingClientFactory(handler)
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)

    async def exercise():
        await api_client.fetch_original_subscription("same", "a=1")
        await api_client.fetch_original_subscription("same", "a=1")

    asyncio.run(exercise())

    assert calls == 1
    assert len(factory.clients) == 1


def test_cache_ttl_expiration_uses_upstream_again(monkeypatch):
    calls = 0
    clock = {"now": 1000.0}

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])

    factory = TrackingClientFactory(handler)
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)
    monkeypatch.setattr(api_client.time, "time", lambda: clock["now"])

    asyncio.run(api_client.fetch_original_subscription("ttl"))
    clock["now"] += api_client._sub_cache_ttl + 1
    asyncio.run(api_client.fetch_original_subscription("ttl"))

    assert calls == 2


def test_cache_keys_distinguish_sub_ids_query_and_parameter_order(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])

    factory = TrackingClientFactory(handler)
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)

    async def exercise():
        await api_client.fetch_original_subscription("sub-a", "a=1&b=2")
        await api_client.fetch_original_subscription("sub-b", "a=1&b=2")
        await api_client.fetch_original_subscription("sub-a", "b=2&a=1")

    asyncio.run(exercise())

    assert calls == 3
    assert len(factory.clients) == 3
    assert set(api_client._sub_cache) == {
        "sub-a:a=1&b=2",
        "sub-b:a=1&b=2",
        "sub-a:b=2&a=1",
    }

def test_cache_over_limit_is_cleared_in_full(monkeypatch):
    expiry = 2000.0
    monkeypatch.setattr(api_client.time, "time", lambda: 1000.0)
    for index in range(1001):
        api_client._sub_cache[f"old-{index}:"] = (
            "[]",
            "application/json",
            httpx.Headers(),
            expiry,
        )
    factory = TrackingClientFactory(lambda request: httpx.Response(200, json=[]))
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)

    asyncio.run(api_client.fetch_original_subscription("new"))

    assert set(api_client._sub_cache) == {"new:"}


@pytest.mark.xfail(
    strict=True,
    reason="Known issue: concurrent misses are not coalesced by per-key single-flight",
)
def test_concurrent_cache_miss_is_single_flight(monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return httpx.Response(200, json=[])

    factory = TrackingClientFactory(handler)
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)

    async def exercise():
        await asyncio.gather(
            api_client.fetch_original_subscription("stampede"),
            api_client.fetch_original_subscription("stampede"),
        )

    asyncio.run(exercise())

    assert calls == 1


def test_xui_api_reuses_one_client_and_closes_it(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"obj": []})

    factory = TrackingClientFactory(handler)
    monkeypatch.setattr(api_client.httpx, "AsyncClient", factory)
    api = api_client.XuiApi()

    async def exercise():
        assert await api.get_json("/first") == []
        assert await api.get_json("/second") == []
        await api.aclose()

    asyncio.run(exercise())

    assert calls == 2
    assert len(factory.clients) == 1
    assert factory.clients[0].is_closed
