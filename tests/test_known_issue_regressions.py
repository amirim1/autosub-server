import asyncio
import json
import logging
from html.parser import HTMLParser
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


class _FlashMessageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.message = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "div" and attributes.get("id") == "serverFlashMessage":
            self.message = attributes.get("data-message")


@pytest.mark.parametrize(
    "payload",
    [
        '<img src=x onerror=alert(1)>',
        '<script>alert(1)</script>',
        '"><svg onload=alert(1)>',
        '&amp;lt;img onerror=alert(1)&amp;gt;',
        "Обычный русский текст",
    ],
)
def test_admin_flash_message_does_not_use_inner_html(payload):
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
    assert "textElement.textContent = String(message)" in show_toast
    for unsafe_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert unsafe_sink not in show_toast

    rendered = dashboard.templates.get_template("admin.html").render(message=payload)
    parser = _FlashMessageParser()
    parser.feed(rendered)
    assert parser.message == payload


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


@pytest.mark.parametrize(
    "detail",
    [
        "https://internal-panel.example/secret-path",
        "password=supersecret",
        "token=abc123",
        r"C:\private\path",
    ],
)
def test_admin_preview_hides_exception_details(client, monkeypatch, caplog, detail):
    monkeypatch.setattr(
        autosub_server, "render_preview", AsyncMock(side_effect=RuntimeError(detail))
    )
    caplog.set_level(logging.ERROR, logger="autosub")

    response = client.get("/admin/preview?sub_id=test")

    assert response.status_code == 500
    assert response.text == "Preview generation failed"
    assert detail not in response.text
    assert "Admin preview generation failed" in caplog.text
    assert "Traceback" in caplog.text


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


def test_admin_api_test_does_not_return_internal_url(monkeypatch, caplog):
    internal_url = "https://panel.example.test/secret-base-path"
    secret = f"request failed for {internal_url}?password=supersecret&token=abc123"
    api = AsyncMock()
    api.base = internal_url
    api.csrf_token = ""
    api.cookie_header = ""
    api.login.side_effect = RuntimeError(secret)
    monkeypatch.setattr(dashboard, "get_xui_api", lambda: api)
    caplog.set_level(logging.ERROR, logger="autosub")

    payload = json.loads(asyncio.run(dashboard.render_api_test()))

    assert payload == {"ok": False, "error": "Connection failed"}
    assert internal_url not in json.dumps(payload)
    assert "supersecret" not in json.dumps(payload)
    assert "abc123" not in json.dumps(payload)
    assert "Admin API connection test failed" in caplog.text


def test_admin_api_test_success_does_not_return_internal_url(monkeypatch):
    internal_url = "https://panel.example.test/secret-base-path"
    api = AsyncMock()
    api.base = internal_url
    api.csrf_token = "csrf"
    api.cookie_header = "session=true"
    api.inbounds.return_value = []
    api.group_map.return_value = {}
    monkeypatch.setattr(dashboard, "get_xui_api", lambda: api)

    payload = json.loads(asyncio.run(dashboard.render_api_test()))

    assert payload["ok"] is True
    assert payload["message"] == "Connection successful"
    assert internal_url not in json.dumps(payload)


def test_admin_action_errors_are_generic(client, monkeypatch, caplog):
    secret = "https://internal-panel.example/secret?password=supersecret&token=abc123"
    error = RuntimeError(secret)
    caplog.set_level(logging.ERROR, logger="autosub")

    monkeypatch.setattr(autosub_server, "save_admin_form", AsyncMock(side_effect=error))
    token = autosub_server._generate_csrf_token()
    save = client.post("/admin/save", data={"_csrf": token})

    monkeypatch.setattr(
        autosub_server, "discover_nodes_from_sub_id", AsyncMock(side_effect=error)
    )
    token = autosub_server._generate_csrf_token()
    discover = client.post(
        "/admin/discover", data={"_csrf": token, "sub_id": "test"}
    )

    autosub_server.storage.add_autoselect.side_effect = error
    token = autosub_server._generate_csrf_token()
    add = client.post(
        "/admin/add-autoselect",
        data={"_csrf": token, "autoselect_id": "test", "name": "Test"},
    )

    autosub_server.storage.delete_autoselect.side_effect = error
    token = autosub_server._generate_csrf_token()
    delete = client.post(
        "/admin/delete-autoselect",
        data={"_csrf": token, "autoselect_id": "test"},
    )

    expected = [
        (save, "Settings save failed"),
        (discover, "Node discovery failed"),
        (add, "Autoselect creation failed"),
        (delete, "Autoselect deletion failed"),
    ]
    for response, message in expected:
        assert response.status_code == 500
        assert response.text == message
        assert secret not in response.text

    assert "Admin settings save failed" in caplog.text
    assert "Admin node discovery failed" in caplog.text
    assert "Admin autoselect creation failed" in caplog.text
    assert "Admin autoselect deletion failed" in caplog.text
