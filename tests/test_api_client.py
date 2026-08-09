import asyncio

import httpx

from api_client import XuiApi
from http_client_config import PanelConfig
from http_clients import HttpClientManager


def test_xui_api_uses_manager_owned_client():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"obj": []})

    values = {
        "XUI_API_URL": "https://panel.example.test",
        "XUI_API_TOKEN": "test-token",
    }
    manager = HttpClientManager(
        env_getter=lambda key, default="": values.get(key, default),
        transport=httpx.MockTransport(handler),
    )

    async def exercise():
        await manager.start()
        async with manager.panel_api() as api:
            assert isinstance(api, XuiApi)
            assert await api.get_json("/first") == []
            assert await api.get_json("/second") == []
            assert not api.client.is_closed
        await manager.close()
        return api

    api = asyncio.run(exercise())

    assert len(requests) == 2
    assert all(request.headers["Authorization"] == "Bearer test-token" for request in requests)
    assert api.client.is_closed


def test_explicit_panel_config_reports_disabled_without_credentials():
    assert not PanelConfig("https://panel.example.test").enabled
