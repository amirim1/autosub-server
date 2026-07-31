import asyncio
from unittest.mock import AsyncMock

import api_client


def test_xui_api_aclose_is_idempotent():
    api = api_client.XuiApi()

    asyncio.run(api.aclose())
    asyncio.run(api.aclose())

    assert api.client.is_closed


def test_close_xui_api_does_not_create_and_clears_singleton(monkeypatch):
    monkeypatch.setattr(api_client, "_global_api", None)
    asyncio.run(api_client.close_xui_api())
    assert api_client._global_api is None

    fake_api = AsyncMock()
    monkeypatch.setattr(api_client, "_global_api", fake_api)
    asyncio.run(api_client.close_xui_api())

    fake_api.aclose.assert_awaited_once()
    assert api_client._global_api is None
