import asyncio
import ssl

import httpx
import pytest

from http_client_config import (
    CLIENT_LIMITS,
    PANEL_TIMEOUT,
    PUBLIC_TIMEOUT,
    PanelConfig,
    normalize_panel_base,
)
from http_client_errors import (
    HttpClientNotStartedError,
    UnsafePanelUrlError,
    UpstreamAuthenticationError,
    UpstreamResponseTooLargeError,
    UpstreamTlsError,
)
from http_clients import HttpClientManager


class RecordingFactory:
    def __init__(self, handler=None):
        self.handler = handler or (lambda request: httpx.Response(200, text="ok"))
        self.calls = []
        self.clients = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler),
            timeout=kwargs["timeout"],
            limits=kwargs["limits"],
            follow_redirects=kwargs["follow_redirects"],
        )
        self.clients.append(client)
        return client


def test_manager_creates_clients_only_during_lifespan_and_closes_once():
    requests = 0

    def handler(request):
        nonlocal requests
        requests += 1
        return httpx.Response(200, text="ok")

    factory = RecordingFactory(handler)
    manager = HttpClientManager(
        env_getter=lambda key, default="": default,
        client_factory=factory,
    )
    assert manager.active_client_count == 0
    assert factory.clients == []

    async def exercise():
        await manager.start()
        assert manager.active_client_count == 1
        first = await manager.request_public("GET", "https://example.test/1", max_bytes=10)
        second = await manager.request_public("GET", "https://example.test/2", max_bytes=10)
        assert first.text == second.text == "ok"
        await manager.close()
        await manager.close()
        with pytest.raises(HttpClientNotStartedError):
            await manager.request_public("GET", "https://example.test/3", max_bytes=10)

    asyncio.run(exercise())

    assert requests == 2
    assert len(factory.clients) == 1
    assert factory.clients[0].is_closed


def test_twenty_concurrent_requests_reuse_one_client_without_mixing_responses():
    async def handler(request):
        await asyncio.sleep(0)
        return httpx.Response(200, text=request.url.path)

    factory = RecordingFactory(handler)
    manager = HttpClientManager(
        env_getter=lambda key, default="": default,
        client_factory=factory,
    )

    async def exercise():
        await manager.start()
        responses = await asyncio.gather(
            *(
                manager.request_public(
                    "GET", f"https://example.test/{index}", max_bytes=100
                )
                for index in range(20)
            )
        )
        await manager.close()
        return [response.text for response in responses]

    assert asyncio.run(exercise()) == [f"/{index}" for index in range(20)]
    assert len(factory.clients) == 1


def test_clients_receive_explicit_timeouts_limits_tls_and_redirect_policy():
    values = {
        "XUI_API_URL": "https://panel.example.test/root/",
        "XUI_API_TOKEN": "token",
        "XUI_TLS_VERIFY": "false",
    }
    factory = RecordingFactory()
    manager = HttpClientManager(
        env_getter=lambda key, default="": values.get(key, default),
        client_factory=factory,
    )

    async def exercise():
        await manager.start()
        await manager.close()

    asyncio.run(exercise())

    assert len(factory.calls) == 2
    assert factory.calls[0] == {
        "verify": True,
        "timeout": PUBLIC_TIMEOUT,
        "limits": CLIENT_LIMITS,
        "follow_redirects": False,
    }
    assert factory.calls[1] == {
        "verify": False,
        "timeout": PANEL_TIMEOUT,
        "limits": CLIENT_LIMITS,
        "follow_redirects": False,
    }


@pytest.mark.parametrize("with_length", [True, False])
def test_streaming_response_limit_rejects_declared_and_actual_oversize(with_length):
    headers = {"Content-Length": "5"} if with_length else {}
    factory = RecordingFactory(
        lambda request: httpx.Response(200, content=b"12345", headers=headers)
    )
    manager = HttpClientManager(
        env_getter=lambda key, default="": default,
        client_factory=factory,
    )

    async def exercise():
        await manager.start()
        with pytest.raises(UpstreamResponseTooLargeError):
            await manager.request_public("GET", "https://example.test", max_bytes=4)
        await manager.close()

    asyncio.run(exercise())
    assert factory.clients[0].is_closed


def test_partial_startup_failure_closes_public_client():
    values = {
        "XUI_API_URL": "file:///etc/passwd",
        "XUI_API_TOKEN": "token",
    }
    factory = RecordingFactory()
    manager = HttpClientManager(
        env_getter=lambda key, default="": values.get(key, default),
        client_factory=factory,
    )

    with pytest.raises(UnsafePanelUrlError):
        asyncio.run(manager.start())

    assert len(factory.clients) == 1
    assert factory.clients[0].is_closed
    assert manager.active_client_count == 0


def test_invalid_panel_credentials_fail_once_with_safe_error():
    login_posts = 0

    def handler(request):
        nonlocal login_posts
        if request.method == "POST":
            login_posts += 1
            return httpx.Response(401, text="invalid")
        return httpx.Response(200, text="login")

    values = {
        "XUI_API_URL": "https://panel.example.test",
        "XUI_USERNAME": "admin",
        "XUI_PASSWORD": "wrong-password",
    }
    manager = HttpClientManager(
        env_getter=lambda key, default="": values.get(key, default),
        transport=httpx.MockTransport(handler),
    )

    async def exercise():
        await manager.start()
        async with manager.panel_api() as api:
            with pytest.raises(UpstreamAuthenticationError, match="panel authentication failed"):
                await api.login()
        await manager.close()

    asyncio.run(exercise())
    assert login_posts == 1


def test_tls_failure_is_safe_and_does_not_log_upstream_url(caplog):
    secret_url = "https://internal-panel.example/secret/path?token=abc123"

    def handler(request):
        try:
            raise ssl.SSLError(f"certificate failure for {secret_url}")
        except ssl.SSLError as cause:
            raise httpx.ConnectError("TLS handshake failed", request=request) from cause

    manager = HttpClientManager(
        env_getter=lambda key, default="": default,
        transport=httpx.MockTransport(handler),
    )

    async def exercise():
        await manager.start()
        with pytest.raises(UpstreamTlsError, match="upstream TLS verification failed"):
            await manager.request_public("GET", secret_url, max_bytes=100)
        await manager.close()

    asyncio.run(exercise())
    assert secret_url not in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("HTTPS://Panel.Example.TEST:443/root/", "https://panel.example.test/root"),
        ("http://[2001:db8::1]:80/", "http://[2001:db8::1]"),
        ("https://panel.example.test:8443/a/b", "https://panel.example.test:8443/a/b"),
    ],
)
def test_panel_base_normalization(value, expected):
    assert normalize_panel_base(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/passwd",
        "https://user:pass@panel.example.test",
        "https://panel.example.test/?token=secret",
        "https://panel.example.test/#fragment",
        "https://panel.example.test:invalid",
    ],
)
def test_panel_base_rejects_unsafe_or_ambiguous_urls(value):
    with pytest.raises(UnsafePanelUrlError):
        PanelConfig(value, api_token="token").normalized()
