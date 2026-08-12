import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import autosub_server
from logging_utils import get_request_id


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    with TestClient(autosub_server.app) as test_client:
        yield test_client


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
        ("/sub/sub-1", "application/json", "Mozilla/5.0 Happ/3.0", "json"),
        ("/sub/sub-1", None, "Mozilla/5.0", "html"),
        ("/sub/sub-1", None, "UnknownClient/1.0", "json"),
    ],
)
def test_current_html_json_negotiation(
    client, monkeypatch, path, accept, user_agent, expected_kind
):
    build = AsyncMock(return_value=('[{"kind":"json"}]', "application/json", {}))
    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
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
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "Подписка AutoSub готова" in response.text
        assert "upstream" not in response.text
        build.assert_awaited_once()
    else:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == [{"kind": "json"}]
        build.assert_awaited_once()


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
    assert response.json() == {
        "error": "Internal server error",
        "request_id": response.headers["x-request-id"],
    }
    assert "upstream" not in response.text
    assert "traceback" not in response.text.lower()


def test_html_failure_returns_local_error(client, monkeypatch):
    monkeypatch.setattr(
        autosub_server,
        "build_for_subscription",
        AsyncMock(side_effect=RuntimeError("private upstream failure")),
    )

    response = client.get(
        "/sub/fallback", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"}
    )

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("text/html")
    assert "Подписка временно недоступна" in response.text
    assert response.headers["x-request-id"] in response.text
    assert "private upstream failure" not in response.text


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


def test_concurrent_http_waiters_share_build_but_keep_request_ids(client, monkeypatch):
    calls = 0
    build_request_ids = []

    async def build(*args, **kwargs):
        nonlocal calls
        calls += 1
        build_request_ids.append(get_request_id())
        await asyncio.sleep(0.05)
        return '[{"name":"cached"}]', "application/json", {}

    monkeypatch.setattr(autosub_server, "build_for_subscription", build)
    monkeypatch.setattr(
        autosub_server, "resolve_security_flags", AsyncMock(return_value={})
    )

    def fetch(index):
        return client.get(
            "/json/shared?client=happ",
            headers={"X-Request-ID": f"untrusted-{index}"},
        )

    with ThreadPoolExecutor(max_workers=20) as executor:
        responses = list(executor.map(fetch, range(20)))

    request_ids = {response.headers["x-request-id"] for response in responses}
    assert calls == 1
    assert build_request_ids == ["-"]
    assert len(request_ids) == 20
    assert all(response.json() == [{"name": "cached"}] for response in responses)
    assert all("untrusted-" not in response.text for response in responses)
    assert all(
        request_id not in response.text
        for request_id in request_ids
        for response in responses
    )
