import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import autosub_server
import builder
import dashboard


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "close_xui_api", AsyncMock())
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    autosub_server._csrf_tokens.clear()
    autosub_server._ip_requests.clear()
    with TestClient(autosub_server.app) as test_client:
        yield test_client
    autosub_server._csrf_tokens.clear()
    autosub_server._ip_requests.clear()


@pytest.mark.xfail(
    strict=True,
    reason="Known issue: admin msg flows through dataset.message into innerHTML",
)
def test_admin_flash_message_does_not_use_inner_html():
    root = Path(__file__).parents[1]
    server_source = (root / "dashboard.py").read_text(encoding="utf-8")
    template = (root / "templates" / "admin.html").read_text(encoding="utf-8")
    javascript = (root / "static" / "dashboard.js").read_text(encoding="utf-8")
    show_toast = javascript.split("function showToast", 1)[1].split(
        "function setTagChecks", 1
    )[0]

    assert 'request.query_params.get("msg", "")' in server_source
    assert 'data-message="{{ message }}"' in template
    assert "msgElement.dataset.message" in javascript
    assert "innerHTML" not in show_toast


def test_public_error_hides_details_but_traceback_is_logged(
    client, monkeypatch, caplog
):
    secret = "https://internal.example.test/secret-path"
    monkeypatch.setattr(autosub_server, "_check_rate_limit", lambda ip: True)
    monkeypatch.setattr(autosub_server, "_client_ip", lambda request: "192.0.2.4")
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(side_effect=RuntimeError(secret)),
    )
    caplog.set_level(logging.ERROR, logger="autosub")

    response = client.get("/json/public-error")

    assert response.status_code == 500
    assert response.json() == {"error": "Internal server error"}
    assert secret not in response.text
    assert "Traceback" not in response.text
    assert secret in caplog.text
    assert "Traceback" in caplog.text


def test_admin_preview_returns_exception_string(client, monkeypatch):
    detail = "preview failed at internal-service.example.test"
    monkeypatch.setattr(
        autosub_server, "render_preview", AsyncMock(side_effect=RuntimeError(detail))
    )

    response = client.get("/admin/preview?sub_id=test")

    assert response.status_code == 500
    assert detail in response.text


@pytest.mark.xfail(
    strict=True,
    reason="Known issue: rate-limit warning logs the complete subscription ID",
)
def test_subscription_logs_do_not_include_full_sub_id(client, monkeypatch, caplog):
    sub_id = "full-sensitive-subscription-id-123456"
    monkeypatch.setattr(autosub_server, "_check_rate_limit", lambda ip: False)
    monkeypatch.setattr(autosub_server, "_client_ip", lambda request: "192.0.2.8")
    caplog.set_level(logging.WARNING, logger="autosub")

    response = client.get(f"/json/{sub_id}")

    assert response.status_code == 429
    assert sub_id not in caplog.text


@pytest.mark.xfail(
    strict=True,
    reason="Known issue: subscription builder logs the complete client email",
)
def test_subscription_logs_do_not_include_client_email(monkeypatch):
    email = "sensitive-client@example.test"
    raw_subscription = json.dumps(
        [
            {
                "tag": "Node",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": "node.example.test",
                            "port": 443,
                            "users": [{"id": "test-uuid"}],
                        }
                    ]
                },
            }
        ]
    )
    storage = AsyncMock()
    storage.get_probe_config.return_value = ("https://probe.example.test", "60s")
    storage.get_client_groups.return_value = ["vip"]
    storage.get_client_email.return_value = email
    storage.get_group_rules.return_value = {"vip": ["auto"]}
    storage.get_autoselects.return_value = [
        {
            "id": "auto",
            "name": "Auto",
            "strategy": "leastPing",
            "selected_node_ids": ["*"],
            "tag_filter": [],
            "enabled": True,
        }
    ]
    monkeypatch.setattr(
        builder,
        "fetch_original_subscription",
        AsyncMock(return_value=(raw_subscription, "application/json", {})),
    )
    messages = []
    monkeypatch.setattr(builder, "log", messages.append)

    asyncio.run(builder.build_for_subscription("sub-id", storage))

    assert email not in "\n".join(messages)


@pytest.mark.xfail(
    strict=True,
    reason="Known issue: admin API test returns the internal panel URL and exception text",
)
def test_admin_api_test_does_not_return_internal_url(monkeypatch):
    internal_url = "https://panel.example.test/secret-base-path"
    api = AsyncMock()
    api.base = internal_url
    api.csrf_token = ""
    api.cookie_header = ""
    api.login.side_effect = RuntimeError(f"request failed for {internal_url}")
    monkeypatch.setattr(dashboard, "get_xui_api", lambda: api)

    payload = asyncio.run(dashboard.render_api_test())

    assert internal_url not in payload
