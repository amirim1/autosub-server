import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import autosub_server
from rate_limiter import RateLimitPolicy


def _request(peer=None, headers=None):
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/json/test",
        "raw_path": b"/json/test",
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": (peer, 12345) if peer else None,
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_client_ip_ignores_spoofed_headers_from_untrusted_peer(monkeypatch):
    monkeypatch.setattr(
        autosub_server,
        "env_get",
        lambda key, default="": "127.0.0.1/32" if key == "AUTOSUB_TRUSTED_PROXIES" else default,
    )
    request = _request(
        "203.0.113.10",
        {"X-Real-IP": "198.51.100.1", "X-Forwarded-For": "198.51.100.2"},
    )

    assert autosub_server._client_ip(request) == "203.0.113.10"


def test_client_ip_uses_nearest_untrusted_forwarded_address(monkeypatch):
    monkeypatch.setattr(
        autosub_server,
        "env_get",
        lambda key, default="": "127.0.0.1/32,10.0.0.0/8" if key == "AUTOSUB_TRUSTED_PROXIES" else default,
    )
    request = _request(
        "127.0.0.1",
        {"X-Forwarded-For": "192.0.2.1, garbage, 198.51.100.20, 10.0.0.4"},
    )

    assert autosub_server._client_ip(request) == "198.51.100.20"


def test_client_ip_falls_back_safely_for_malformed_headers(monkeypatch, caplog):
    monkeypatch.setattr(
        autosub_server,
        "env_get",
        lambda key, default="": "127.0.0.1/32" if key == "AUTOSUB_TRUSTED_PROXIES" else default,
    )
    request = _request(
        "127.0.0.1",
        {"X-Forwarded-For": "not-an-ip", "X-Real-IP": "also-not-an-ip"},
    )

    assert autosub_server._client_ip(request) == "127.0.0.1"
    assert autosub_server._client_ip(_request()) == "unknown"
    assert "Malformed forwarded client address" in caplog.text
    assert "not-an-ip" not in caplog.text
    assert "also-not-an-ip" not in caplog.text


def test_lifespan_closes_http_client_and_storage(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    fake_manager = AsyncMock()
    fake_cache = AsyncMock()
    close_order = []
    fake_cache.close.side_effect = lambda: close_order.append("cache")
    fake_manager.close.side_effect = lambda: close_order.append("http")
    fake_storage.close.side_effect = lambda: close_order.append("storage")
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(
        autosub_server,
        "HttpClientManager",
        lambda env_getter: fake_manager,
    )
    monkeypatch.setattr(autosub_server, "SubscriptionCache", lambda: fake_cache)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)

    async def exercise():
        async with autosub_server.lifespan(autosub_server.app):
            pass

    asyncio.run(exercise())

    fake_storage.connect.assert_awaited_once()
    fake_manager.start.assert_awaited_once()
    fake_cache.close.assert_awaited_once()
    fake_manager.close.assert_awaited_once()
    fake_storage.close.assert_awaited_once()
    assert close_order == ["cache", "http", "storage"]
    assert autosub_server.app.state.ready is False


@pytest.fixture
def http_client(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    with TestClient(autosub_server.app) as client:
        yield client, fake_storage


def test_readiness_reflects_completed_lifespan(http_client):
    client, _ = http_client

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_admin_basic_auth(http_client, monkeypatch):
    client, _ = http_client
    monkeypatch.setattr(
        autosub_server,
        "env_get",
        lambda key, default="": "secret" if key == "AUTOSUB_ADMIN_PASSWORD" else default,
    )
    render_api_test = AsyncMock(return_value="{}")
    monkeypatch.setattr(autosub_server, "render_api_test", render_api_test)

    assert client.get("/admin/api-test").status_code == 401
    credentials = base64.b64encode(b"admin:secret").decode("ascii")
    response = client.get(
        "/admin/api-test", headers={"Authorization": f"Basic {credentials}"}
    )
    assert response.status_code == 200


def test_admin_save_requires_reusable_csrf(http_client, monkeypatch):
    client, _ = http_client
    save = AsyncMock()
    monkeypatch.setattr(autosub_server, "save_admin_form", save)

    missing = client.post("/admin/save", data={}, follow_redirects=False)
    assert missing.status_code == 403
    save.assert_not_awaited()

    invalid = client.post(
        "/admin/save", data={"_csrf": "invalid"}, follow_redirects=False
    )
    assert invalid.status_code == 403
    save.assert_not_awaited()

    token = client.app.state.csrf_manager.generate()
    valid = client.post(
        "/admin/save", data={"_csrf": token}, follow_redirects=False
    )
    assert valid.status_code == 303
    save.assert_awaited_once()

    reused = client.post(
        "/admin/save", data={"_csrf": token}, follow_redirects=False
    )
    assert reused.status_code == 303
    assert save.await_count == 2


def test_empty_http_subscription_and_rate_limit(http_client, monkeypatch):
    client, _ = http_client
    build = AsyncMock(return_value=("[]", "application/json", {}))
    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        autosub_server,
        "PUBLIC_RATE_LIMIT",
        RateLimitPolicy("server-test", 1, 60),
    )

    first = client.get("/json/empty")
    second = client.get("/json/empty")

    assert first.status_code == 200
    assert first.json() == []
    assert second.status_code == 429
    assert build.await_count == 1


def test_happ_receives_plain_json_with_only_canonical_hide_header(http_client, monkeypatch):
    client, _ = http_client
    payload = '[{"remarks":"Node"}]'
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(return_value=(payload, "application/json; charset=utf-8", {})),
    )
    monkeypatch.setattr(
        autosub_server,
        "resolve_security_flags",
        AsyncMock(return_value={"hide_settings": True}),
    )

    response = client.get("/json/happ", headers={"User-Agent": "Happ/3.0"})

    assert response.status_code == 200
    assert response.json() == [{"remarks": "Node"}]
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers.get("hide-settings") == "1"
    assert "happ-encrypt" not in response.headers
    assert "x-hide-settings" not in response.headers
    assert "hide-user-info" not in response.headers


def test_legacy_happ_rule_does_not_enable_headers_or_encryption(http_client, monkeypatch):
    client, fake_storage = http_client
    payload = '[{"remarks":"Node"}]'
    fake_storage.get_security_rules.return_value = {
        "hide_settings_groups": [],
        "happ_encrypt_groups": ["*"],
    }
    fake_storage.get_client_groups.return_value = ["legacy"]
    fake_storage.get_client_email.return_value = "legacy@example.com"
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(return_value=(payload, "application/json", {"Hide-Settings": "true"})),
    )

    response = client.get("/json/legacy", headers={"User-Agent": "Happ/3.0"})

    assert response.status_code == 200
    assert response.text == payload
    assert "hide-settings" not in response.headers
    assert "happ-encrypt" not in response.headers
