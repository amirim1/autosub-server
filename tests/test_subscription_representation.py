from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import api_client
import autosub_server
from http_client_errors import UpstreamResponseTooLargeError, UpstreamTimeoutError
from http_clients import HttpClientManager
from rate_limiter import RateLimitPolicy
from subscription_representation import (
    SubscriptionRepresentation,
    UnsupportedSubscriptionFormat,
    parse_accept_header,
    select_subscription_representation,
    templates,
)


MALICIOUS_HTML = """<html>
<script>window.pwned=true</script>
<img src=x onerror="window.pwned=true">
<svg onload="window.pwned=true"></svg>
<iframe src="https://evil.example"></iframe>
<meta http-equiv="refresh" content="0;url=https://evil.example">
<form action="https://evil.example"></form>
</html>"""


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    build = AsyncMock(
        return_value=(
            '[{"name":"safe"}]',
            "application/json; charset=utf-8",
            {"Content-Disposition": 'attachment; filename="subscription.json"'},
        )
    )
    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )
    with TestClient(autosub_server.app) as test_client:
        yield test_client, build


@pytest.mark.parametrize(
    ("accept", "user_agent", "expected"),
    [
        ("application/json", "Mozilla/5.0", SubscriptionRepresentation.JSON),
        ("text/plain", "Mozilla/5.0", SubscriptionRepresentation.JSON),
        ("text/html", "Unknown/1.0", SubscriptionRepresentation.HTML),
        ("*/*", "Unknown/1.0", SubscriptionRepresentation.JSON),
        ("", "Unknown/1.0", SubscriptionRepresentation.JSON),
        ("*/*", "Mozilla/5.0", SubscriptionRepresentation.HTML),
        ("", "Mozilla/5.0", SubscriptionRepresentation.HTML),
        ("*/*", "Happ/3.0", SubscriptionRepresentation.JSON),
        (
            "text/html, application/json;q=0.9",
            "Unknown/1.0",
            SubscriptionRepresentation.HTML,
        ),
        (
            "text/html;q=0.2, application/json;q=0.8",
            "Mozilla/5.0",
            SubscriptionRepresentation.JSON,
        ),
        (
            "application/json;q=0, text/html;q=0.5",
            "Happ/3.0",
            SubscriptionRepresentation.HTML,
        ),
    ],
)
def test_accept_and_user_agent_selection(accept, user_agent, expected):
    assert (
        select_subscription_representation(
            is_json_route=False,
            accept=accept,
            user_agent=user_agent,
        )
        is expected
    )


def test_accept_parser_handles_weights_and_ignores_malformed_quality():
    accepted = parse_accept_header(
        "text/html;q=0.7, application/json; q=1, text/plain;q=bad, */*;q=0"
    )
    assert [(item.media_type, item.quality) for item in accepted] == [
        ("text/html", 0.7),
        ("application/json", 1.0),
    ]


@pytest.mark.parametrize("value", ["raw", "javascript", "<script>", "", "JSON,HTML"])
def test_unsupported_explicit_format_is_rejected(value):
    with pytest.raises(UnsupportedSubscriptionFormat):
        select_subscription_representation(
            is_json_route=False,
            format_values=(value,),
        )


def test_explicit_format_overrides_accept_and_json_route_is_fixed():
    assert select_subscription_representation(
        is_json_route=False,
        format_values=("json",),
        accept="text/html",
    ) is SubscriptionRepresentation.JSON
    assert select_subscription_representation(
        is_json_route=False,
        format_values=("html",),
        accept="application/json",
    ) is SubscriptionRepresentation.HTML
    assert select_subscription_representation(
        is_json_route=True,
        format_values=("html",),
        accept="text/html",
    ) is SubscriptionRepresentation.JSON


def test_explicit_formats_strip_control_parameter_and_share_cache(client):
    test_client, build = client

    html = test_client.get(
        "/sub/shared?format=html&client=happ", headers={"Accept": "application/json"}
    )
    json_response = test_client.get(
        "/sub/shared?format=json&client=happ", headers={"Accept": "text/html"}
    )

    assert html.status_code == 200
    assert html.headers["content-type"].startswith("text/html; charset=utf-8")
    assert "content-disposition" not in html.headers
    assert json_response.status_code == 200
    assert json_response.json() == [{"name": "safe"}]
    assert json_response.headers["content-disposition"] == (
        'attachment; filename="subscription.json"'
    )
    build.assert_awaited_once()
    assert build.await_args.kwargs["query"] == "client=happ"


def test_json_route_never_becomes_html_and_preserves_query(client):
    test_client, build = client
    response = test_client.get(
        "/json/fixed?format=html&client=happ",
        headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert build.await_args.kwargs["query"] == "format=html&client=happ"


def test_invalid_route_format_returns_safe_400_before_build(client):
    test_client, build = client
    response = test_client.get(
        "/sub/id?format=%3Cscript%3E", headers={"Accept": "text/html"}
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Unsupported subscription format"
    assert "<script>" not in response.text
    build.assert_not_awaited()


def test_local_html_has_strict_headers_and_no_unsafe_template_sinks(client):
    test_client, _ = client
    response = test_client.get("/sub/id?format=html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html; charset=utf-8")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["permissions-policy"]
    assert response.headers["x-request-id"]
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "style-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "https://" not in response.text
    assert 'href="/sub/_assets/subscription.css"' in response.text

    stylesheet = test_client.get("/sub/_assets/subscription.css")
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert stylesheet.headers["cache-control"] == "public, max-age=86400"
    assert "subscription-card" in stylesheet.text

    template = (
        Path(__file__).parents[1] / "templates" / "subscription.html"
    ).read_text(encoding="utf-8")
    assert templates.env.autoescape("subscription.html")
    assert "|safe" not in template
    assert "innerHTML" not in template
    assert "<script" not in template


def test_malicious_upstream_derived_body_is_never_rendered(client, monkeypatch):
    test_client, _ = client
    build = AsyncMock(return_value=(MALICIOUS_HTML, "text/html", {}))
    monkeypatch.setattr(autosub_server, "build_for_subscription", build)

    response = test_client.get("/sub/malicious?format=html")

    assert response.status_code == 200
    for marker in ("<script", "onerror", "onload", "<iframe", "http-equiv", "<form"):
        assert marker not in response.text.lower()
    assert "Подписка AutoSub готова" in response.text


@pytest.mark.parametrize(
    "error",
    [
        UpstreamTimeoutError("safe timeout"),
        UpstreamResponseTooLargeError("safe size limit"),
    ],
)
def test_upstream_failures_use_local_error_page(client, monkeypatch, error):
    test_client, _ = client
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(side_effect=error),
    )

    response = test_client.get("/sub/error?format=html")

    assert response.status_code == 502
    assert response.headers["x-request-id"] in response.text
    assert str(error) not in response.text


def test_mock_transport_html_and_redirect_become_local_errors(monkeypatch, tmp_path):
    responses = [
        httpx.Response(200, text=MALICIOUS_HTML, headers={"Content-Type": "text/html"}),
        httpx.Response(302, headers={"Location": "https://evil.example/redirect"}),
        httpx.Response(500, text=MALICIOUS_HTML, headers={"Content-Type": "text/html"}),
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return responses[len(requests) - 1]

    transport = httpx.MockTransport(handler)

    def manager_factory(*, env_getter):
        return HttpClientManager(env_getter=env_getter, transport=transport)

    async def validate_subscription(sub_id, storage, query="", http_manager=None):
        body, content_type, headers = await api_client.fetch_original_subscription(
            sub_id, query, client_manager=http_manager
        )
        api_client.normalize_subscription(body)
        return body, content_type, headers

    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    monkeypatch.setattr(autosub_server, "HttpClientManager", manager_factory)
    monkeypatch.setattr(autosub_server, "build_for_subscription", validate_subscription)
    monkeypatch.setattr(
        api_client,
        "env_get",
        lambda key, default="": "https://upstream.example"
        if key == "XUI_SUB_URL"
        else default,
    )

    with TestClient(autosub_server.app) as test_client:
        malicious = test_client.get("/sub/one?format=html")
        redirected = test_client.get("/sub/two?format=html")
        server_error = test_client.get("/sub/three?format=html")

    for response in (malicious, redirected, server_error):
        assert response.status_code == 502
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["x-request-id"] in response.text
        assert "evil.example" not in response.text
        assert "window.pwned" not in response.text
        assert "location" not in response.headers
    assert len(requests) == 3


def test_all_sub_formats_share_public_rate_limit(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        autosub_server,
        "PUBLIC_RATE_LIMIT",
        RateLimitPolicy("representation-rate", 3, 60),
    )

    assert test_client.get("/sub/id", headers={"Accept": "application/json"}).status_code == 200
    assert test_client.get("/sub/id?format=html").status_code == 200
    assert test_client.get("/sub/id?format=raw").status_code == 400
    rejected = test_client.get("/sub/id?format=json")
    assert rejected.status_code == 429
    assert int(rejected.headers["retry-after"]) > 0


def test_runtime_assets_are_present_and_packaged():
    root = Path(__file__).parents[1]
    runtime_assets = (
        "subscription_representation.py",
        "templates/subscription.html",
        "static/subscription.css",
    )
    for relative in runtime_assets:
        assert (root / relative).is_file()

    manifest = (root / "runtime-manifest.txt").read_text(encoding="utf-8").splitlines()
    assert all(relative in manifest for relative in runtime_assets)
    installer = (root / "install.sh").read_text(encoding="utf-8")
    updater = (root / "update.sh").read_text(encoding="utf-8")
    assert 'bash "$TMP_DIR/checkout/update.sh"' in installer
    assert "runtime-manifest.txt" in updater

    production_source = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("autosub_server.py", "api_client.py")
    )
    assert "fetch_original_sub_html" not in production_source
    assert "HTMLResponse" not in production_source
