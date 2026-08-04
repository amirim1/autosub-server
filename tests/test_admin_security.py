import asyncio
import base64
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

import autosub_server
import dashboard


ADMIN_ROUTES = [
    ("GET", "/admin"),
    ("GET", "/admin/preview?sub_id=test"),
    ("GET", "/admin/api-test"),
    ("GET", "/admin/debug?sub_id=test"),
    ("POST", "/admin/save"),
    ("POST", "/admin/discover"),
    ("POST", "/admin/set-client-group"),
    ("POST", "/admin/delete-client-group"),
    ("POST", "/admin/add-autoselect"),
    ("POST", "/admin/delete-autoselect"),
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "close_xui_api", AsyncMock())
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "render_api_test", AsyncMock(return_value="{}"))
    autosub_server._csrf_tokens.clear()
    with TestClient(autosub_server.app) as test_client:
        yield test_client
    autosub_server._csrf_tokens.clear()


def _basic(username, password):
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


def _admin_env(username="admin", password="correct", host="127.0.0.1"):
    values = {
        "AUTOSUB_ADMIN_USERNAME": username,
        "AUTOSUB_ADMIN_PASSWORD": password,
        "AUTOSUB_HOST": host,
    }
    return lambda key, default="": values.get(key, default)


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_every_admin_route_requires_credentials(client, monkeypatch, method, path):
    monkeypatch.setattr(autosub_server, "env_get", _admin_env())

    response = client.request(method, path, headers=_basic("admin", "wrong"))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="AutoSub Admin"'


@pytest.mark.parametrize(
    ("username", "password", "expected_status"),
    [
        ("operator", "correct", 200),
        ("wrong", "correct", 401),
        ("operator", "wrong", 401),
        ("wrong", "wrong", 401),
        (None, None, 401),
    ],
)
def test_admin_authentication_matrix(
    client, monkeypatch, username, password, expected_status
):
    monkeypatch.setattr(
        autosub_server, "env_get", _admin_env(username="operator")
    )
    headers = _basic(username, password) if username is not None else {}

    response = client.get("/admin/api-test", headers=headers)

    assert response.status_code == expected_status
    if expected_status == 401:
        assert response.headers["www-authenticate"] == 'Basic realm="AutoSub Admin"'


def test_admin_credentials_use_constant_time_comparison_for_both_fields(monkeypatch):
    calls = []
    compare_digest = autosub_server.secrets.compare_digest

    def record_compare(left, right):
        calls.append((left, right))
        return compare_digest(left, right)

    monkeypatch.setattr(autosub_server, "env_get", _admin_env(username="operator"))
    monkeypatch.setattr(autosub_server.secrets, "compare_digest", record_compare)

    credentials = HTTPBasicCredentials(username="operator", password="correct")
    assert autosub_server.verify_admin(credentials) is True
    assert calls == [(b"operator", b"operator"), (b"correct", b"correct")]


def test_admin_credentials_support_unicode_without_type_errors(monkeypatch):
    monkeypatch.setattr(
        autosub_server,
        "env_get",
        _admin_env(username="администратор", password="секретный пароль"),
    )
    credentials = HTTPBasicCredentials(
        username="администратор", password="секретный пароль"
    )

    assert autosub_server.verify_admin(credentials) is True


def test_nonempty_admin_password_is_compared_without_stripping(client, monkeypatch):
    monkeypatch.setattr(
        autosub_server,
        "env_get",
        _admin_env(password="  correct password  "),
    )

    exact = client.get(
        "/admin/api-test", headers=_basic("admin", "  correct password  ")
    )
    stripped = client.get(
        "/admin/api-test", headers=_basic("admin", "correct password")
    )

    assert exact.status_code == 200
    assert stripped.status_code == 401


@pytest.mark.parametrize(
    ("host", "password", "raises"),
    [
        ("127.0.0.1", "", False),
        ("::1", "", False),
        ("localhost", "", False),
        ("LOCALHOST", "   ", False),
        ("0.0.0.0", "", True),
        ("::", "", True),
        ("192.168.1.10", "", True),
        ("10.0.0.10", "", True),
        ("172.16.0.10", "", True),
        ("admin.internal.example", "", True),
        ("0.0.0.0", "   ", True),
        ("0.0.0.0", "configured", False),
    ],
)
def test_empty_admin_password_startup_matrix(host, password, raises):
    if raises:
        with pytest.raises(
            autosub_server.AdminSecurityConfigError,
            match="allowed only with a loopback",
        ):
            autosub_server.validate_admin_security_config(host, password)
    else:
        autosub_server.validate_admin_security_config(host, password)


def test_lifespan_rejects_unsafe_admin_before_storage_connect(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(
        autosub_server, "env_get", _admin_env(password="", host="0.0.0.0")
    )

    async def exercise():
        async with autosub_server.lifespan(autosub_server.app):
            pass

    with pytest.raises(autosub_server.AdminSecurityConfigError):
        asyncio.run(exercise())
    fake_storage.connect.assert_not_awaited()


def test_admin_route_inventory_matches_auth_regression_matrix():
    expected = {(method, path.split("?", 1)[0]) for method, path in ADMIN_ROUTES}
    actual = {
        (method, route.path)
        for route in autosub_server.app.routes
        if route.path.startswith("/admin")
        for method in getattr(route, "methods", set())
    }

    assert actual == expected


def test_admin_debug_error_is_generic_json(client, monkeypatch, caplog):
    secret = r"C:\private\path?password=supersecret&token=abc123"
    monkeypatch.setattr(
        autosub_server, "render_debug", AsyncMock(side_effect=RuntimeError(secret))
    )
    caplog.set_level(logging.ERROR, logger="autosub")

    response = client.get("/admin/debug?sub_id=test")

    assert response.status_code == 500
    assert response.json() == {
        "error": "Debug generation failed",
        "request_id": response.headers["x-request-id"],
    }
    assert secret not in response.text
    assert "Admin debug generation failed" in caplog.text


def test_admin_client_list_error_is_generic(monkeypatch, caplog):
    secret = "https://internal-panel.example/secret?token=abc123"

    def fail_api_lookup():
        raise RuntimeError(secret)

    monkeypatch.setattr(dashboard, "get_xui_api", fail_api_lookup)
    caplog.set_level(logging.ERROR, logger="autosub")

    clients, error = asyncio.run(dashboard.api_clients_safe())

    assert clients == []
    assert error == "API connection failed"
    assert secret not in error
    assert "Admin client list loading failed" in caplog.text
