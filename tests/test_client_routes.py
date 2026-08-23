import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import autosub_server
from builder import build_for_subscription


def _raw_subscription():
    return json.dumps(
        [
            {
                "remarks": "\U0001F1E9\U0001F1EA Berlin",
                "protocol": "vless",
                "settings": {
                    "vnext": [{"address": "de01.example.com", "port": 443, "users": [{"id": "uuid-de"}]}]
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "tls",
                    "tlsSettings": {"serverName": "de01.example.com"},
                },
            },
            {
                "remarks": "\U0001F1FA\U0001F1F8 Miami",
                "protocol": "trojan",
                "settings": {
                    "servers": [{"address": "us01.example.com", "port": 443, "password": "pw"}]
                },
                "streamSettings": {"network": "tcp", "security": "none"},
            },
        ]
    )


def _raw_subscription_flat_settings():
    """3x-ui style profiles whose settings lack canonical vnext/servers sections."""
    return json.dumps(
        [
            {
                "remarks": "RU-GERMANY",
                "protocol": "vless",
                "settings": {"address": "ru-de01.example.com", "port": "443", "id": "uuid-flat-1"},
                "streamSettings": {
                    "network": "ws",
                    "security": "tls",
                    "tlsSettings": {"serverName": "cdn.example.com"},
                    "wsSettings": {"path": "/wspath", "headers": {"Host": "cdn.example.com"}},
                },
            },
            {
                "remarks": "Niderland",
                "protocol": "trojan",
                "settings": {"address": "nl01.example.com", "port": "443", "password": "pw2"},
                "streamSettings": {"network": "tcp", "security": "none"},
            },
        ]
    )


async def _async_build_raw(out_format, raw):
    storage_mock = AsyncMock()
    storage_mock.get_probe_config.return_value = ("https://probe.example/", "10m")
    storage_mock.get_client_groups.return_value = ["vip"]
    storage_mock.get_client_email.return_value = "t@example.com"
    storage_mock.get_group_rules.return_value = {"vip": ["auto"]}
    storage_mock.get_autoselects.return_value = [
        {"id": "auto", "name": "Auto", "enabled": True, "selected_node_ids": ["*"], "strategy": "leastPing"}
    ]
    storage_mock.get_direct_domains.return_value = []
    with patch(
        "builder.fetch_original_subscription",
        new=AsyncMock(return_value=(raw, "application/json", {})),
    ):
        return await build_for_subscription("sub-id", storage_mock, out_format=out_format)


@pytest.fixture
def client(monkeypatch):
    storage_mock = AsyncMock()
    storage_mock.get_probe_config.return_value = ("https://probe.example/", "10m")
    storage_mock.get_client_groups.return_value = ["vip"]
    storage_mock.get_client_email.return_value = "t@example.com"
    storage_mock.get_group_rules.return_value = {"vip": ["auto"]}
    storage_mock.get_autoselects.return_value = [
        {"id": "auto", "name": "Auto", "enabled": True, "selected_node_ids": ["*"], "strategy": "leastPing"}
    ]
    storage_mock.get_direct_domains.return_value = []
    storage_mock.get_sticky_domains.side_effect = RuntimeError("legacy")

    captured = {}

    async def fake_build(sub_id, storage, query="", http_manager=None, out_format="xray"):
        captured["out_format"] = out_format
        captured["query"] = query
        return await _real_build(out_format)

    async def _real_build(out_format):
        with patch(
            "builder.fetch_original_subscription",
            new=AsyncMock(return_value=(_raw_subscription(), "application/json", {})),
        ):
            from builder import build_for_subscription as real

            return await real("sub-id", storage_mock, query="", http_manager=None, out_format=out_format)

    monkeypatch.setattr(autosub_server, "storage", storage_mock)
    monkeypatch.setattr(autosub_server, "build_for_subscription", fake_build)

    with TestClient(autosub_server.app) as test_client:
        yield test_client, captured


def test_json_route_client_happ_gets_singbox(client):
    test_client, captured = client
    response = test_client.get("/json/sub-id", headers={"User-Agent": "Happ/1.14 iOS"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert captured["out_format"] == "singbox"
    doc = response.json()
    assert isinstance(doc, dict)
    assert any(ob.get("type") == "selector" for ob in doc["outbounds"])


def test_json_route_client_v2raytun_gets_singbox(client):
    test_client, captured = client
    response = test_client.get("/json/sub-id", headers={"User-Agent": "V2RayTun/3.0 Android"})
    assert response.status_code == 200
    assert captured["out_format"] == "singbox"


def test_query_client_overrides_user_agent(client):
    test_client, captured = client
    response = test_client.get(
        "/json/sub-id?client=v2raytun",
        headers={"User-Agent": "Happ/1.14 iOS"},
    )
    assert response.status_code == 200
    assert captured["out_format"] == "singbox"
    assert "?client=" not in (captured.get("query") or "")


def test_explicit_links_format_returns_base64(client):
    test_client, captured = client
    response = test_client.get("/json/sub-id?format=links")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    decoded = base64.b64decode(response.text).decode("utf-8")
    lines = decoded.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("vless://")
    assert lines[1].startswith("trojan://")


def test_base64_alias_maps_to_links(client):
    test_client, captured = client
    response = test_client.get("/json/sub-id?format=base64")
    assert response.status_code == 200
    base64.b64decode(response.text)


def test_unknown_client_returns_400(client):
    test_client, _ = client
    response = test_client.get("/json/sub-id?client=hysteria-app")
    assert response.status_code == 400


def test_flat_settings_subscription_converts_to_singbox():
    output_text, content_type, _ = asyncio.run(
        _async_build_raw("singbox", _raw_subscription_flat_settings())
    )
    assert content_type.startswith("application/json")
    doc = json.loads(output_text)
    outbound_types = {ob["type"] for ob in doc["outbounds"]}
    assert {"vless", "trojan"} <= outbound_types
    vless = next(ob for ob in doc["outbounds"] if ob["type"] == "vless")
    assert vless["server"] == "ru-de01.example.com"
    assert vless["server_port"] == 443
    assert vless["tls"]["server_name"] == "cdn.example.com"
    assert vless["transport"]["type"] == "ws"


def test_flat_settings_subscription_converts_to_links():
    payload, content_type = asyncio.run(_async_build_payload_raw("links", _raw_subscription_flat_settings()))
    assert content_type.startswith("text/plain")
    links = base64.b64decode(payload).decode("utf-8").splitlines()
    assert len(links) == 2
    assert links[0].startswith("vless://uuid-flat-1@ru-de01.example.com:443?")
    assert "type=ws" in links[0]
    assert links[1].startswith("trojan://")


async def _async_build_payload_raw(out_format, raw):
    output_text, content_type, _ = await _async_build_raw(out_format, raw)
    return output_text, content_type


def test_repeated_client_values_return_400(client):
    test_client, _ = client
    response = test_client.get("/json/sub-id?client=happ&client=incy")
    assert response.status_code == 400


def test_generic_default_stays_xray_array(client):
    test_client, captured = client
    response = test_client.get("/json/sub-id", headers={"User-Agent": "curl/8.4"})
    assert response.status_code == 200
    assert "out_format" not in captured or captured["out_format"] in ("xray", None)
    payload = json.loads(response.text)
    assert isinstance(payload, list)
