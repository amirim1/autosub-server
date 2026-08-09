from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import autosub_server


def _request(peer, headers=None):
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/json/test",
            "raw_path": b"/json/test",
            "query_string": b"",
            "root_path": "",
            "headers": raw_headers,
            "client": (peer, 12345),
            "server": ("testserver", 80),
        }
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "render_api_test", AsyncMock(return_value="{}"))
    autosub_server._ip_requests.clear()
    with TestClient(autosub_server.app) as test_client:
        yield test_client
    autosub_server._ip_requests.clear()


def test_csrf_uses_no_process_local_token_store():
    assert not hasattr(autosub_server, "_csrf_tokens")
    assert not hasattr(autosub_server, "_CSRF_TOKEN_MAX")
    assert not hasattr(autosub_server, "_validate_csrf_token")


def test_one_csrf_token_is_rendered_into_multiple_admin_forms():
    template = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(
        encoding="utf-8"
    )

    shared_field = 'name="_csrf" value="{{ csrf_token }}"'
    assert template.count(shared_field) >= 6


def test_random_csrf_token_is_rejected(client, monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    monkeypatch.setattr(autosub_server, "save_admin_form", save)

    response = client.post(
        "/admin/save", data={"_csrf": "not-a-generated-token"}, follow_redirects=False
    )

    assert response.status_code == 403
    save.assert_not_awaited()


@pytest.mark.parametrize(
    ("peer", "trusted", "headers", "expected"),
    [
        ("198.51.100.4", "127.0.0.1/32", {}, "198.51.100.4"),
        ("2001:db8::10", "::1/128", {}, "2001:db8::10"),
        ("127.0.0.1", "127.0.0.1/32", {"X-Real-IP": "203.0.113.7"}, "203.0.113.7"),
        ("203.0.113.4", "127.0.0.1/32", {"X-Forwarded-For": "192.0.2.9"}, "203.0.113.4"),
        ("127.0.0.1", "127.0.0.1/32,10.0.0.0/8", {"X-Forwarded-For": "192.0.2.1, 10.0.0.5"}, "192.0.2.1"),
        ("127.0.0.1", "127.0.0.1/32", {"X-Forwarded-For": ""}, "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1/32", {"X-Forwarded-For": "bad", "X-Real-IP": "bad"}, "127.0.0.1"),
    ],
)
def test_client_ip_regression_matrix(monkeypatch, peer, trusted, headers, expected):
    monkeypatch.setattr(
        autosub_server,
        "env_get",
        lambda key, default="": trusted if key == "AUTOSUB_TRUSTED_PROXIES" else default,
    )

    assert autosub_server._client_ip(_request(peer, headers)) == expected


def test_rate_limiter_uses_address_selected_from_trusted_proxy_chain(monkeypatch):
    autosub_server._ip_requests.clear()
    monkeypatch.setattr(
        autosub_server,
        "env_get",
        lambda key, default="": "127.0.0.1/32,10.0.0.0/8"
        if key == "AUTOSUB_TRUSTED_PROXIES"
        else default,
    )
    request = _request(
        "127.0.0.1", {"X-Forwarded-For": "198.51.100.20, 10.0.0.4"}
    )

    selected_ip = autosub_server._client_ip(request)
    assert autosub_server._check_rate_limit(selected_ip) is True

    assert selected_ip == "198.51.100.20"
    assert "198.51.100.20" in autosub_server._ip_requests


def test_rate_limit_boundaries_independent_ips_and_cleanup(monkeypatch):
    autosub_server._ip_requests.clear()
    clock = {"now": 1000.0}
    monkeypatch.setattr(autosub_server.time, "time", lambda: clock["now"])
    monkeypatch.setattr(autosub_server, "RATE_LIMIT_MAX_REQUESTS", 2)
    monkeypatch.setattr(autosub_server, "_last_ip_cleanup", 0)

    assert autosub_server._check_rate_limit("192.0.2.1") is True
    assert autosub_server._check_rate_limit("192.0.2.1") is True
    assert autosub_server._check_rate_limit("192.0.2.1") is False
    assert autosub_server._check_rate_limit("192.0.2.2") is True

    clock["now"] += autosub_server.RATE_LIMIT_WINDOW + 1
    assert autosub_server._check_rate_limit("192.0.2.1") is True
    assert autosub_server._ip_requests["192.0.2.1"] == [clock["now"]]


def test_rate_limit_scope_and_missing_retry_after(client, monkeypatch):
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    monkeypatch.setattr(autosub_server, "RATE_LIMIT_MAX_REQUESTS", 0)
    monkeypatch.setattr(autosub_server, "_client_ip", lambda request: "198.51.100.8")
    monkeypatch.setattr(autosub_server, "render_api_test", AsyncMock(return_value="{}"))

    limited = client.get("/json/limited")
    health = client.get("/health")
    admin = client.get("/admin/api-test")

    assert limited.status_code == 429
    assert "retry-after" not in limited.headers
    assert health.status_code == 200
    assert admin.status_code == 200
