import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import autosub_server
from logger import logger
from logging_utils import (
    fingerprint_secret,
    get_request_id,
    mask_email,
    sanitize_log_message,
    sanitize_url,
)


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    fake_storage.get_node_catalog.return_value = []
    fake_storage.get_client_group_overrides.return_value = {}
    fake_storage.get_all_client_groups.return_value = []
    fake_storage.get_autoselects.return_value = []
    fake_storage.get_group_rules.return_value = {}
    fake_storage.get_security_rules.return_value = {}
    fake_storage.get_probe_config.return_value = ("", "60s")
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "close_xui_api", AsyncMock())
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    monkeypatch.setattr(autosub_server, "_check_rate_limit", lambda ip: True)
    monkeypatch.setattr(autosub_server, "_client_ip", lambda request: "192.0.2.10")
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(return_value=("[]", "application/json", {})),
    )
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )
    autosub_server._ip_requests.clear()
    with TestClient(autosub_server.app) as test_client:
        yield test_client
    autosub_server._ip_requests.clear()


def test_request_ids_are_server_generated_unique_and_valid(client):
    supplied = "client-controlled-request-id"
    first = client.get("/health", headers={"X-Request-ID": supplied})
    second = client.get("/health")

    first_id = first.headers["x-request-id"]
    second_id = second.headers["x-request-id"]
    assert first_id != supplied
    assert first_id != second_id
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        first_id,
    )


def test_parallel_requests_keep_distinct_request_contexts(client, caplog):
    caplog.set_level(logging.INFO, logger="autosub")

    with ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(lambda _: client.get("/health"), range(12)))

    response_ids = {response.headers["x-request-id"] for response in responses}
    completion_ids = {
        record.request_id
        for record in caplog.records
        if record.getMessage().startswith("HTTP request completed")
    }
    assert len(response_ids) == len(responses)
    assert response_ids <= completion_ids


def test_error_body_header_and_log_share_request_id(client, monkeypatch, caplog):
    secret = "https://panel.example/secret-path?token=abc123"
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(side_effect=RuntimeError(secret)),
    )
    caplog.set_level(logging.ERROR, logger="autosub")

    response = client.get("/json/sub_very_secret_123456")
    request_id = response.headers["x-request-id"]

    assert response.json() == {
        "error": "Internal server error",
        "request_id": request_id,
    }
    assert any(record.request_id == request_id for record in caplog.records)
    assert secret not in caplog.text


def test_request_context_is_cleared_outside_http_request(client, caplog):
    client.get("/health")
    assert get_request_id() == "-"

    caplog.set_level(logging.INFO, logger="autosub")
    logger.info("Startup-style event")
    assert caplog.records[-1].request_id == "-"


def test_lifespan_startup_and_shutdown_logs_use_placeholder(
    monkeypatch, tmp_path, caplog
):
    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "close_xui_api", AsyncMock())
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    caplog.set_level(logging.INFO, logger="autosub")

    async def exercise():
        async with autosub_server.lifespan(autosub_server.app):
            pass

    asyncio.run(exercise())

    lifecycle_records = [
        record
        for record in caplog.records
        if "AutoSub Server" in record.getMessage()
    ]
    assert lifecycle_records
    assert all(record.request_id == "-" for record in lifecycle_records)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("alice@example.com", "a***@example.com"),
        ("a@example.com", "*@example.com"),
        ("üser@example.org", "ü***@example.org"),
        ("invalid", "<redacted>"),
        ("", "<redacted>"),
        (None, "<redacted>"),
    ],
)
def test_mask_email(value, expected):
    assert mask_email(value) == expected


def test_redaction_helpers_are_safe_and_deterministic():
    sub_id = "sub_very_secret_123456"
    assert fingerprint_secret(sub_id) == fingerprint_secret(sub_id)
    assert sub_id not in fingerprint_secret(sub_id)
    assert fingerprint_secret(None) == "<empty>"
    assert sanitize_url("https://panel.example/secret-path?token=abc123") == (
        "https://panel.example"
    )
    assert sanitize_url(None) == "<redacted>"


def test_log_message_and_exception_redaction(caplog):
    secrets = [
        "sub_very_secret_123456",
        "alice.private@example.com",
        "SuperSecret123",
        "abc123xyz",
        "dXNlcjpwYXNz",
        "/secret-path?token=abc123",
        "csrf-secret-value",
    ]
    message = (
        f"sub_id_hash={fingerprint_secret(secrets[0])} "
        "email=alice.private@example.com password=SuperSecret123 "
        "token=abc123xyz Authorization: Basic dXNlcjpwYXNz "
        "AUTOSUB_SECRET_KEY=csrf-secret-value "
        "url=https://panel.example/secret-path?token=abc123"
    )
    caplog.set_level(logging.ERROR, logger="autosub")

    try:
        raise RuntimeError(message)
    except RuntimeError:
        logger.exception(message)

    for secret in secrets:
        assert secret not in caplog.text
    assert "Traceback" in caplog.text
    assert "https://panel.example" in caplog.text
    assert sanitize_log_message(None) == ""


def test_uvicorn_access_log_and_server_header_are_disabled(monkeypatch):
    run = Mock()
    monkeypatch.setattr(autosub_server.uvicorn, "run", run)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)

    autosub_server.main()

    assert run.call_args.kwargs["access_log"] is False
    assert run.call_args.kwargs["server_header"] is False


BASE_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def _assert_base_security_headers(response):
    for name, value in BASE_HEADERS.items():
        assert response.headers[name] == value
    assert "strict-transport-security" not in response.headers
    assert response.headers["x-request-id"]


def test_security_headers_cover_success_admin_401_404_and_500(
    client, monkeypatch
):
    responses = [client.get("/health"), client.get("/json/test"), client.get("/missing")]
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(side_effect=RuntimeError("failure")),
    )
    responses.append(client.get("/json/failure"))
    for response in responses:
        _assert_base_security_headers(response)
        assert "content-security-policy" in response.headers

    monkeypatch.setattr(
        autosub_server,
        "env_get",
        lambda key, default="": "configured"
        if key == "AUTOSUB_ADMIN_PASSWORD"
        else default,
    )
    unauthorized = client.get("/admin")
    _assert_base_security_headers(unauthorized)
    assert unauthorized.status_code == 401
    assert unauthorized.headers["cache-control"] == "no-store"


def test_admin_csp_cache_control_static_js_and_upstream_html(client, monkeypatch):
    admin = client.get("/admin")
    _assert_base_security_headers(admin)
    assert admin.headers["cache-control"] == "no-store"
    policy = admin.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline'" in policy
    assert "style-src 'self' 'unsafe-inline'" in policy
    assert "object-src 'none'" in policy
    assert '<script src="/static/dashboard.js"></script>' in admin.text

    javascript = (
        Path(__file__).parents[1] / "static" / "dashboard.js"
    ).read_text(encoding="utf-8")
    assert "function showToast" in javascript

    monkeypatch.setattr(
        autosub_server,
        "fetch_original_sub_html",
        AsyncMock(return_value=("<html>upstream</html>", "text/html", 200)),
    )
    upstream = client.get(
        "/sub/test", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"}
    )
    _assert_base_security_headers(upstream)
    assert "content-security-policy" not in upstream.headers
