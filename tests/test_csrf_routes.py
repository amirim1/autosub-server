import asyncio
import base64
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import autosub_server


SECRET = "route-test-secret-key-with-at-least-32-characters"
ADMIN_POST_ROUTES = [
    ("/admin/save", {}, "save"),
    ("/admin/discover", {"sub_id": "sub"}, "discover"),
    (
        "/admin/set-client-group",
        {"sub_id": "sub", "email": "a@example.test", "groups": "vip"},
        "set_client",
    ),
    ("/admin/delete-client-group", {"sub_id": "sub"}, "delete_client"),
    (
        "/admin/add-autoselect",
        {"autoselect_id": "auto", "name": "Auto"},
        "add_auto",
    ),
    ("/admin/delete-autoselect", {"autoselect_id": "auto"}, "delete_auto"),
]


def _basic(username="admin", password="correct"):
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


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
    save = AsyncMock()
    discover = AsyncMock(return_value=[])

    values = {
        "AUTOSUB_HOST": "127.0.0.1",
        "AUTOSUB_ADMIN_USERNAME": "admin",
        "AUTOSUB_ADMIN_PASSWORD": "correct",
        "AUTOSUB_SECRET_KEY": SECRET,
    }
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": values.get(key, default))
    monkeypatch.setattr(autosub_server, "save_admin_form", save)
    monkeypatch.setattr(autosub_server, "discover_nodes_from_sub_id", discover)

    actions = {
        "save": save,
        "discover": discover,
        "set_client": fake_storage.set_client_groups,
        "delete_client": fake_storage.delete_client_groups,
        "add_auto": fake_storage.add_autoselect,
        "delete_auto": fake_storage.delete_autoselect,
    }
    with TestClient(autosub_server.app) as test_client:
        yield test_client, actions


@pytest.mark.parametrize(("path", "data", "action_name"), ADMIN_POST_ROUTES)
def test_every_admin_post_requires_basic_auth_and_csrf(
    client, path, data, action_name
):
    test_client, actions = client
    token = test_client.app.state.csrf_manager.generate()

    unauthorized = test_client.post(
        path, data={**data, "_csrf": token}, follow_redirects=False
    )
    missing = test_client.post(
        path, data=data, headers=_basic(), follow_redirects=False
    )
    invalid = test_client.post(
        path, data={**data, "_csrf": "bad-token"}, headers=_basic(), follow_redirects=False
    )

    assert unauthorized.status_code == 401
    assert missing.status_code == 403
    assert invalid.status_code == 403
    actions[action_name].assert_not_awaited()


@pytest.mark.parametrize(("path", "data", "action_name"), ADMIN_POST_ROUTES)
def test_every_admin_post_accepts_reused_valid_token(
    client, path, data, action_name
):
    test_client, actions = client
    token = test_client.app.state.csrf_manager.generate()
    payload = {**data, "_csrf": token}

    first = test_client.post(path, data=payload, headers=_basic(), follow_redirects=False)
    second = test_client.post(path, data=payload, headers=_basic(), follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert actions[action_name].await_count == 2


def test_one_page_token_works_across_all_admin_forms(client):
    test_client, actions = client
    token = test_client.app.state.csrf_manager.generate()

    for path, data, _ in ADMIN_POST_ROUTES:
        response = test_client.post(
            path,
            data={**data, "_csrf": token},
            headers=_basic(),
            follow_redirects=False,
        )
        assert response.status_code == 303

    assert all(action.await_count == 1 for action in actions.values())


def test_csrf_failure_does_not_log_or_return_token_or_secret(client, caplog):
    test_client, _ = client
    bad_token = "v1.1000.secret-nonce.admin.secret-signature"
    caplog.set_level(logging.WARNING, logger="autosub")

    response = test_client.post(
        "/admin/save",
        data={"_csrf": bad_token},
        headers=_basic(),
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.text.startswith("CSRF validation failed. Request ID: ")
    combined = response.text + caplog.text
    assert bad_token not in combined
    assert SECRET not in combined
    assert "CSRF validation failed" in caplog.text


def test_lifespan_loopback_fallback_warns_without_disclosing_secret(
    monkeypatch, tmp_path, caplog
):
    fake_storage = AsyncMock()
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": default)
    caplog.set_level(logging.WARNING, logger="autosub")

    async def exercise():
        async with autosub_server.lifespan(autosub_server.app):
            token = autosub_server.app.state.csrf_manager.generate()
            assert autosub_server.app.state.csrf_manager.verify(token)

    asyncio.run(exercise())

    assert "temporary process secret" in caplog.text
    assert "v1." not in caplog.text


@pytest.mark.parametrize(("secret", "expected"), [("", "required"), ("short", "32 characters")])
def test_lifespan_rejects_non_loopback_missing_or_weak_secret(
    monkeypatch, tmp_path, secret, expected
):
    fake_storage = AsyncMock()
    values = {
        "AUTOSUB_HOST": "0.0.0.0",
        "AUTOSUB_ADMIN_PASSWORD": "configured",
        "AUTOSUB_SECRET_KEY": secret,
    }
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": values.get(key, default))

    async def exercise():
        async with autosub_server.lifespan(autosub_server.app):
            pass

    with pytest.raises(RuntimeError, match=expected):
        asyncio.run(exercise())
    fake_storage.connect.assert_not_awaited()


def test_lifespan_accepts_non_loopback_with_strong_secret(monkeypatch, tmp_path):
    fake_storage = AsyncMock()
    values = {
        "AUTOSUB_HOST": "0.0.0.0",
        "AUTOSUB_ADMIN_PASSWORD": "configured",
        "AUTOSUB_SECRET_KEY": SECRET,
    }
    monkeypatch.setattr(autosub_server, "storage", fake_storage)
    monkeypatch.setattr(autosub_server, "CONFIG_PATH", Path(tmp_path / "missing.json"))
    monkeypatch.setattr(autosub_server, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(autosub_server, "env_get", lambda key, default="": values.get(key, default))

    async def exercise():
        async with autosub_server.lifespan(autosub_server.app):
            token = autosub_server.app.state.csrf_manager.generate()
            assert autosub_server.app.state.csrf_manager.verify(token)

    asyncio.run(exercise())
    fake_storage.connect.assert_awaited_once()
