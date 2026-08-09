import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import autosub_server
from rate_limiter import RateLimitPolicy, TrustedProxyConfigError


def _basic(username="admin", password="correct"):
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    values = {
        "AUTOSUB_ADMIN_USERNAME": "admin",
        "AUTOSUB_ADMIN_PASSWORD": "correct",
        "AUTOSUB_HOST": "127.0.0.1",
        "AUTOSUB_TRUSTED_PROXIES": "",
    }
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(
        autosub_server, "env_get", lambda key, default="": values.get(key, default)
    )
    monkeypatch.setattr(autosub_server, "render_admin", AsyncMock(return_value="admin"))
    with TestClient(autosub_server.app) as test_client:
        yield test_client, fake_storage


def test_public_429_has_required_headers_and_skips_builder(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        autosub_server, "PUBLIC_RATE_LIMIT", RateLimitPolicy("public-test", 2, 60)
    )
    build = AsyncMock(return_value=("[]", "application/json", {}))
    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )
    cache = test_client.app.state.subscription_cache
    original_get_or_build = cache.get_or_build
    cache_calls = 0

    async def tracked_get_or_build(*args, **kwargs):
        nonlocal cache_calls
        cache_calls += 1
        return await original_get_or_build(*args, **kwargs)

    monkeypatch.setattr(cache, "get_or_build", tracked_get_or_build)

    first = test_client.get("/json/same")
    second = test_client.get("/json/same")
    rejected = test_client.get("/json/same")

    assert first.status_code == second.status_code == 200
    assert rejected.status_code == 429
    assert rejected.json() == {
        "error": "Too Many Requests",
        "request_id": rejected.headers["x-request-id"],
        "detail": "Rate limit exceeded. Please try again later.",
    }
    assert int(rejected.headers["retry-after"]) > 0
    assert rejected.headers["cache-control"] == "no-store"
    assert rejected.headers["x-content-type-options"] == "nosniff"
    assert build.await_count == 1
    assert cache_calls == 2


def test_public_limit_runs_before_local_html_build(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        autosub_server, "PUBLIC_RATE_LIMIT", RateLimitPolicy("html-test", 1, 60)
    )
    build = AsyncMock(return_value=("[]", "application/json", {}))
    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
    headers = {"Accept": "text/html", "User-Agent": "Mozilla/5.0"}

    assert test_client.get("/sub/id", headers=headers).status_code == 200
    rejected = test_client.get("/sub/id", headers=headers)
    assert rejected.status_code == 429
    build.assert_awaited_once()


def test_admin_auth_attempts_are_limited_before_handler(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        autosub_server, "ADMIN_RATE_LIMIT", RateLimitPolicy("auth-test", 2, 60)
    )
    render = AsyncMock(return_value="{}")
    monkeypatch.setattr(autosub_server, "render_api_test", render)

    first = test_client.get("/admin/api-test", headers=_basic(password="wrong"))
    second = test_client.get("/admin/api-test", headers=_basic(password="wrong"))
    rejected = test_client.get("/admin/api-test", headers=_basic(password="wrong"))

    assert first.status_code == second.status_code == 401
    assert rejected.status_code == 429
    assert rejected.headers["cache-control"] == "no-store"
    assert int(rejected.headers["retry-after"]) > 0
    assert rejected.headers["x-request-id"] in rejected.text
    assert rejected.headers["x-content-type-options"] == "nosniff"
    render.assert_not_awaited()


def test_successful_admin_auth_does_not_reset_attempt_history(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        autosub_server, "ADMIN_RATE_LIMIT", RateLimitPolicy("auth-history", 2, 60)
    )
    monkeypatch.setattr(autosub_server, "render_api_test", AsyncMock(return_value="{}"))

    assert test_client.get(
        "/admin/api-test", headers=_basic(password="wrong")
    ).status_code == 401
    assert test_client.get("/admin/api-test", headers=_basic()).status_code == 200
    assert test_client.get(
        "/admin/api-test", headers=_basic(password="wrong")
    ).status_code == 429


def test_expensive_admin_policy_is_separate_from_regular_admin(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        autosub_server,
        "EXPENSIVE_ADMIN_RATE_LIMIT",
        RateLimitPolicy("expensive-test", 1, 60),
    )
    preview = AsyncMock(return_value="preview")
    monkeypatch.setattr(autosub_server, "render_preview", preview)

    first = test_client.get("/admin/preview?sub_id=id", headers=_basic())
    rejected = test_client.get("/admin/preview?sub_id=id", headers=_basic())
    regular = test_client.get("/admin", headers=_basic())

    assert first.status_code == 200
    assert rejected.status_code == 429
    assert regular.status_code == 200
    preview.assert_awaited_once()


def test_expensive_admin_route_inventory_is_explicit():
    expected = {"/admin/preview", "/admin/api-test", "/admin/discover"}
    actual = {
        route.path
        for route in autosub_server.app.routes
        if route.path.startswith("/admin")
        and any(
            dependency.call is autosub_server.enforce_expensive_admin_access
            for dependency in route.dependant.dependencies
        )
    }
    assert actual == expected


def test_health_is_not_limited(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        autosub_server, "PUBLIC_RATE_LIMIT", RateLimitPolicy("health-test", 1, 60)
    )
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(return_value=("[]", "application/json", {})),
    )
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )
    test_client.get("/json/id")
    assert test_client.get("/json/id").status_code == 429
    assert test_client.get("/health").status_code == 200


def test_one_hundred_http_requests_preserve_cache_and_request_ids(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        autosub_server, "PUBLIC_RATE_LIMIT", RateLimitPolicy("cross-pr", 60, 60)
    )
    calls = 0

    async def build(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "[]", "application/json", {}

    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )
    cache = test_client.app.state.subscription_cache
    original_get_or_build = cache.get_or_build
    cache_calls = 0

    async def tracked_get_or_build(*args, **kwargs):
        nonlocal cache_calls
        cache_calls += 1
        return await original_get_or_build(*args, **kwargs)

    monkeypatch.setattr(cache, "get_or_build", tracked_get_or_build)

    with ThreadPoolExecutor(max_workers=40) as executor:
        responses = list(executor.map(lambda _: test_client.get("/json/shared"), range(100)))

    assert sum(response.status_code == 200 for response in responses) == 60
    assert sum(response.status_code == 429 for response in responses) == 40
    assert calls == 1
    assert cache_calls == 60
    assert len({response.headers["x-request-id"] for response in responses}) == 100


def test_invalid_proxy_config_fails_before_storage_connect(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    values = {
        "AUTOSUB_HOST": "127.0.0.1",
        "AUTOSUB_TRUSTED_PROXIES": "0.0.0.0/0",
    }
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(
        autosub_server, "env_get", lambda key, default="": values.get(key, default)
    )

    async def exercise():
        async with autosub_server.lifespan(autosub_server.app):
            pass

    with pytest.raises(TrustedProxyConfigError):
        asyncio.run(exercise())
    fake_storage.connect.assert_not_awaited()
