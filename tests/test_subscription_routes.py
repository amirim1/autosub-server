from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import autosub_server


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "close_xui_api", AsyncMock())
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    monkeypatch.setattr(autosub_server, "_check_rate_limit", lambda ip: True)
    monkeypatch.setattr(autosub_server, "_client_ip", lambda request: "192.0.2.10")
    autosub_server._csrf_tokens.clear()
    autosub_server._ip_requests.clear()
    with TestClient(autosub_server.app) as test_client:
        yield test_client
    autosub_server._csrf_tokens.clear()
    autosub_server._ip_requests.clear()


@pytest.mark.parametrize(
    ("path", "accept", "user_agent", "expected_kind"),
    [
        ("/json/sub-1", "application/json", "UnknownClient/1.0", "json"),
        ("/json/sub-1", "text/html", "Mozilla/5.0", "json"),
        ("/sub/sub-1", "application/json", "Happ/3.0", "json"),
        ("/sub/sub-1", "application/json", "UnknownClient/1.0", "json"),
        ("/sub/sub-1", "text/html", "UnknownClient/1.0", "html"),
        ("/sub/sub-1", "text/html", "Happ/3.0", "html"),
        ("/sub/sub-1", "application/json", "Mozilla/5.0", "html"),
        ("/sub/sub-1", None, "Mozilla/5.0", "html"),
        ("/sub/sub-1", None, "UnknownClient/1.0", "json"),
    ],
)
def test_current_html_json_negotiation(
    client, monkeypatch, path, accept, user_agent, expected_kind
):
    build = AsyncMock(return_value=('[{"kind":"json"}]', "application/json", {}))
    html = AsyncMock(return_value=("<html>upstream</html>", "text/html", 202))
    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
    monkeypatch.setattr(autosub_server, "fetch_original_sub_html", html)
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )
    headers = {"User-Agent": user_agent}
    if accept is not None:
        headers["Accept"] = accept
    else:
        headers["Accept"] = ""

    response = client.get(path, headers=headers)

    if expected_kind == "html":
        assert response.status_code == 202
        assert response.headers["content-type"].startswith("text/html")
        assert response.text == "<html>upstream</html>"
        html.assert_awaited_once()
        build.assert_not_awaited()
    else:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == [{"kind": "json"}]
        build.assert_awaited_once()
        html.assert_not_awaited()


def test_query_string_is_forwarded_verbatim(client, monkeypatch):
    build = AsyncMock(return_value=("[]", "application/json", {}))
    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )

    response = client.get("/json/query-sub?b=2&a=1&a=3&blank=")

    assert response.status_code == 200
    assert build.await_args.kwargs["query"] == "b=2&a=1&a=3&blank="


def test_json_route_returns_safe_500_for_upstream_failure(client, monkeypatch):
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(side_effect=RuntimeError("internal upstream URL and traceback")),
    )

    response = client.get("/json/missing")

    assert response.status_code == 500
    assert response.json() == {"error": "Internal server error"}
    assert "upstream" not in response.text
    assert "traceback" not in response.text.lower()


def test_html_failure_falls_back_to_json(client, monkeypatch):
    monkeypatch.setattr(
        autosub_server,
        "fetch_original_sub_html",
        AsyncMock(side_effect=RuntimeError("html unavailable")),
    )
    build = AsyncMock(return_value=("[]", "application/json", {}))
    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )

    response = client.get(
        "/sub/fallback", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == []
    build.assert_awaited_once()


def test_invalid_json_body_is_returned_with_json_content_type(client, monkeypatch):
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(return_value=("not-json", "application/json", {})),
    )
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )

    response = client.get("/json/invalid")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.text == "not-json"
