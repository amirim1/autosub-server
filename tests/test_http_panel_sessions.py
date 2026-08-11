import asyncio

import httpx
import pytest

from http_client_config import PanelConfig
from http_client_errors import UpstreamAuthenticationError, UpstreamResponseError
from http_clients import HttpClientManager


class RecordingFactory:
    def __init__(self, handler=None):
        self.handler = handler or (lambda request: httpx.Response(200, json={"obj": []}))
        self.clients = []

    def __call__(self, **kwargs):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler),
            timeout=kwargs["timeout"],
            limits=kwargs["limits"],
            follow_redirects=kwargs["follow_redirects"],
        )
        self.clients.append(client)
        return client


def test_panel_sessions_send_cookies_only_through_their_own_clients():
    api_cookies = {}

    def handler(request):
        identity = f"{request.url.scheme}-{request.url.port}-{request.url.path.split('/')[1]}"
        if request.method == "POST" and request.url.path.endswith("/login"):
            return httpx.Response(
                200,
                headers={"Set-Cookie": f"session={identity}; Path=/"},
            )
        if request.url.path.endswith("/who"):
            api_cookies[identity] = request.headers.get("cookie", "")
            return httpx.Response(200, json={"obj": []})
        return httpx.Response(200, text="login")

    factory = RecordingFactory(handler)
    manager = HttpClientManager(
        env_getter=lambda key, default="": default,
        client_factory=factory,
    )
    configs = [
        PanelConfig("http://panel.example.test:8080/a", "admin", "first-secret"),
        PanelConfig("http://panel.example.test:8081/b", "admin", "second-secret"),
        PanelConfig("https://panel.example.test:8080/c", "admin", "third-secret"),
    ]

    async def exercise():
        await manager.start()
        apis = []
        for config in configs:
            async with manager.panel_api(config) as api:
                apis.append(api)
                assert await api.get_json("/who") == []
        assert len({id(api.client) for api in apis}) == 3
        keys = list(manager._panels)
        await manager.close()
        return keys

    keys = asyncio.run(exercise())

    assert len(keys) == 3
    assert all("secret" not in key for key in keys)
    assert api_cookies == {
        "http-8080-a": "session=http-8080-a",
        "http-8081-b": "session=http-8081-b",
        "https-8080-c": "session=https-8080-c",
    }
    assert all(client.is_closed for client in factory.clients)


def test_panel_lru_is_bounded_and_evicts_inactive_clients():
    factory = RecordingFactory()
    manager = HttpClientManager(
        env_getter=lambda key, default="": default,
        client_factory=factory,
        max_panel_clients=3,
    )

    async def exercise():
        await manager.start()
        for index in range(6):
            config = PanelConfig(
                f"https://panel-{index}.example.test",
                api_token=f"token-{index}",
            )
            async with manager.panel_api(config):
                pass
            assert manager.panel_client_count <= 3
        assert sum(client.is_closed for client in factory.clients[1:]) == 3
        await manager.close()

    asyncio.run(exercise())
    assert all(client.is_closed for client in factory.clients)


def test_active_panel_is_not_evicted_when_limit_is_temporarily_exceeded():
    factory = RecordingFactory()
    manager = HttpClientManager(
        env_getter=lambda key, default="": default,
        client_factory=factory,
        max_panel_clients=1,
    )
    first = PanelConfig("https://one.example.test", api_token="one")
    second = PanelConfig("https://two.example.test", api_token="two")

    async def exercise():
        await manager.start()
        async with manager.panel_api(first) as first_api:
            async with manager.panel_api(second):
                assert manager.panel_client_count == 2
                assert not first_api.client.is_closed
            assert not first_api.client.is_closed
        assert manager.panel_client_count == 1
        await manager.close()

    asyncio.run(exercise())


def test_default_credentials_rotation_closes_old_client_after_active_request():
    values = {
        "XUI_API_URL": "https://panel.example.test",
        "XUI_USERNAME": "admin",
        "XUI_PASSWORD": "old-password",
    }
    factory = RecordingFactory()
    manager = HttpClientManager(
        env_getter=lambda key, default="": values.get(key, default),
        client_factory=factory,
    )

    async def exercise():
        await manager.start()
        async with manager.panel_api() as old_api:
            values["XUI_PASSWORD"] = "new-password"
            async with manager.panel_api() as new_api:
                assert old_api is not new_api
                assert not old_api.client.is_closed
            assert not old_api.client.is_closed
        assert old_api.client.is_closed
        assert not new_api.client.is_closed
        values["XUI_PASSWORD"] = ""
        async with manager.panel_api() as disabled_api:
            assert disabled_api is None
        assert new_api.client.is_closed
        await manager.close()

    asyncio.run(exercise())


def test_concurrent_requests_share_one_login():
    login_calls = 0

    async def handler(request):
        nonlocal login_calls
        if request.url.path == "/":
            await asyncio.sleep(0)
            return httpx.Response(200, text='<meta name="csrf-token" content="csrf">')
        if request.url.path == "/login":
            login_calls += 1
            await asyncio.sleep(0)
            return httpx.Response(200, headers={"Set-Cookie": "session=valid; Path=/"})
        return httpx.Response(200, json={"obj": []})

    values = {
        "XUI_API_URL": "https://panel.example.test",
        "XUI_USERNAME": "admin",
        "XUI_PASSWORD": "password",
    }
    factory = RecordingFactory(handler)
    manager = HttpClientManager(
        env_getter=lambda key, default="": values.get(key, default),
        client_factory=factory,
    )

    async def exercise():
        await manager.start()
        async with manager.panel_api() as api:
            await asyncio.gather(*(api.get_json(f"/{index}") for index in range(20)))
        await manager.close()

    asyncio.run(exercise())
    assert login_calls == 1


def test_concurrent_auth_failures_trigger_one_relogin():
    login_calls = 0
    failed_requests = 0
    both_failed = asyncio.Event()

    async def handler(request):
        nonlocal login_calls, failed_requests
        if request.url.path == "/":
            return httpx.Response(200, text="login")
        if request.url.path == "/login":
            login_calls += 1
            return httpx.Response(
                200,
                headers={"Set-Cookie": f"session={login_calls}; Path=/"},
            )
        if login_calls == 1 and request.url.path.startswith("/item-"):
            failed_requests += 1
            if failed_requests == 20:
                both_failed.set()
            await both_failed.wait()
            return httpx.Response(401)
        return httpx.Response(200, json={"obj": []})

    values = {
        "XUI_API_URL": "https://panel.example.test",
        "XUI_USERNAME": "admin",
        "XUI_PASSWORD": "password",
    }
    manager = HttpClientManager(
        env_getter=lambda key, default="": values.get(key, default),
        client_factory=RecordingFactory(handler),
    )

    async def exercise():
        await manager.start()
        async with manager.panel_api() as api:
            await api.login()
            await asyncio.gather(*(api.get_json(f"/item-{index}") for index in range(20)))
        await manager.close()

    asyncio.run(exercise())
    assert failed_requests == 20
    assert login_calls == 2


@pytest.mark.parametrize(
    ("response", "path"),
    [
        (httpx.Response(500, text="internal"), "/server-error"),
        (httpx.Response(200, text="not-json"), "/malformed"),
        (httpx.Response(200, text="<html>login</html>", headers={"Content-Type": "text/html"}), "/html"),
    ],
)
def test_panel_response_errors_are_mapped_to_safe_exception(response, path):
    manager = HttpClientManager(
        env_getter=lambda key, default="": default,
        transport=httpx.MockTransport(lambda request: response),
    )
    config = PanelConfig("https://panel.example.test", api_token="secret")

    async def exercise():
        await manager.start()
        async with manager.panel_api(config) as api:
            with pytest.raises(UpstreamResponseError):
                await api.get_json(path)
        await manager.close()

    asyncio.run(exercise())


def test_repeated_auth_failure_relogs_once_without_looping():
    login_calls = 0
    api_calls = 0

    def handler(request):
        nonlocal login_calls, api_calls
        if request.url.path == "/login":
            login_calls += 1
            return httpx.Response(200, headers={"Set-Cookie": "session=value; Path=/"})
        if request.url.path == "/data":
            api_calls += 1
            return httpx.Response(401)
        return httpx.Response(200, text="login")

    values = {
        "XUI_API_URL": "https://panel.example.test",
        "XUI_USERNAME": "admin",
        "XUI_PASSWORD": "password",
    }
    manager = HttpClientManager(
        env_getter=lambda key, default="": values.get(key, default),
        transport=httpx.MockTransport(handler),
    )

    async def exercise():
        await manager.start()
        async with manager.panel_api() as api:
            with pytest.raises(UpstreamAuthenticationError):
                await api.get_json("/data")
        await manager.close()

    asyncio.run(exercise())
    assert login_calls == 2
    assert api_calls == 2
