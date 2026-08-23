import base64
import json
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

import autosub_server
import config
from landing_catalog import (
    PLATFORMS,
    build_landing_view,
    get_client_entries,
)


def _entries_by_id():
    return {entry.id: entry for entry in get_client_entries()}


def test_catalog_defaults():
    entries = _entries_by_id()
    assert set(entries) == {"happ", "v2raytun", "incy"}

    happ = entries["happ"]
    assert happ.badge == "Рекомендуем"
    assert dict(happ.downloads) == {platform: "https://happ.su/" for platform, _ in PLATFORMS}

    v2raytun = entries["v2raytun"]
    assert v2raytun.downloads["android"] == (
        "https://play.google.com/store/apps/details?id=com.v2raytun.android"
    )
    assert "windows" not in v2raytun.downloads

    assert entries["incy"].downloads == {}


def test_deep_link_schemes():
    entries = _entries_by_id()
    b64 = base64.b64encode("https://vpn.example/json/abc".encode()).decode()
    assert entries["happ"].deep_link_scheme.format(b64=b64) == f"happ://add/{b64}"
    assert entries["v2raytun"].deep_link_scheme.format(b64=b64) == f"v2raytun://import/{b64}"
    assert entries["incy"].deep_link_scheme.format(b64=b64) == f"incy://import/{b64}"


def test_build_landing_view_covers_platforms_and_import_only_clients():
    b64 = "c3Vic2NyaXB0aW9u"
    panels = build_landing_view(b64)
    assert [panel["id"] for panel in panels] == [platform for platform, _ in PLATFORMS]

    by_platform = {panel["id"]: {c["id"]: c for c in panel["clients"]} for panel in panels}
    for platform, _ in PLATFORMS:
        assert "happ" in by_platform[platform]
        assert by_platform[platform]["happ"]["deep_link"] == f"happ://add/{b64}"

    assert by_platform["android"]["v2raytun"]["downloads"]["android"].startswith("https://")
    assert "windows" not in by_platform["windows"]

    for platform, _ in PLATFORMS:
        incy = by_platform[platform]["incy"]
        assert incy["downloads"] == {}
        assert incy["deep_link"].startswith("incy://import/")


def test_overrides_add_download_links(monkeypatch):
    monkeypatch.setattr(
        config,
        "ENV",
        {
            **config.ENV,
            "AUTOSUB_LANDING_OVERRIDES": json.dumps(
                {"incy": {"downloads": {"android": "https://incy.example/app"}}}
            ),
        },
    )
    panels = build_landing_view("x")
    by_platform = {panel["id"]: {c["id"]: c for c in panel["clients"]} for panel in panels}
    assert by_platform["android"]["incy"]["downloads"]["android"] == "https://incy.example/app"


def test_overrides_reject_non_http_schemes(monkeypatch):
    monkeypatch.setattr(
        config,
        "ENV",
        {
            **config.ENV,
            "AUTOSUB_LANDING_OVERRIDES": json.dumps(
                {"happ": {"downloads": {"android": "javascript:alert(1)"}}}
            ),
        },
    )
    entries = _entries_by_id()
    assert entries["happ"].downloads["android"] == "https://happ.su/"


def test_overrides_invalid_json_is_ignored(monkeypatch):
    monkeypatch.setattr(config, "ENV", {**config.ENV, "AUTOSUB_LANDING_OVERRIDES": "{broken"})
    entries = _entries_by_id()
    assert entries["happ"].downloads["android"] == "https://happ.su/"


def _landing_client(monkeypatch):
    storage_mock = AsyncMock()
    storage_mock.get_probe_config.return_value = ("https://probe.example/", "10m")
    storage_mock.get_client_groups.return_value = ["vip"]
    storage_mock.get_client_email.return_value = "t@example.com"
    storage_mock.get_group_rules.return_value = {"vip": ["auto"]}
    storage_mock.get_autoselects.return_value = [
        {"id": "auto", "name": "Auto", "enabled": True, "selected_node_ids": ["*"]}
    ]
    storage_mock.get_direct_domains.return_value = []
    monkeypatch.setattr(autosub_server, "storage", storage_mock)
    build = AsyncMock(return_value=("[]", "application/json", {}))
    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
    return TestClient(autosub_server.app)


def test_landing_renders_public_url_from_env(monkeypatch):
    monkeypatch.setattr(
        config,
        "ENV",
        {**config.ENV, "AUTOSUB_PUBLIC_URL": "https://vpn.example"},
    )
    test_client = _landing_client(monkeypatch)
    with test_client as client:
        response = client.get("/sub/secret-id?format=html")
    assert response.status_code == 200

    expected_b64 = base64.b64encode(b"https://vpn.example/json/secret-id").decode()
    assert f'happ://add/{expected_b64}' in response.text
    assert "https://vpn.example/json/secret-id" in response.text


def test_landing_falls_back_to_request_base_url(monkeypatch):
    monkeypatch.setattr(
        config, "ENV", {k: v for k, v in config.ENV.items() if k != "AUTOSUB_PUBLIC_URL"}
    )
    test_client = _landing_client(monkeypatch)
    with test_client as client:
        response = client.get("/sub/secret-id?format=html")
    assert response.status_code == 200
    expected_b64 = base64.b64encode(b"http://testserver/json/secret-id").decode()
    assert f"happ://add/{expected_b64}" in response.text


def test_error_page_has_no_catalog_links(monkeypatch):
    async def failing_build(*args, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(autosub_server, "storage", AsyncMock())
    monkeypatch.setattr(autosub_server, "build_for_subscription", failing_build)
    test_client = TestClient(autosub_server.app)
    with test_client as client:
        response = client.get("/sub/broken?format=html")
    assert response.status_code == 502
    assert "Подписка временно недоступна" in response.text
    assert "Как подключиться" not in response.text
    assert "happ://" not in response.text
