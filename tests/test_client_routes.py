import base64
import json

import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import autosub_server


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
