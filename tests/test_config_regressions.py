import asyncio
import json

import pytest

import config
from database_errors import DatabaseIntegrityError
from storage import Storage


def test_missing_config_uses_defaults_but_malformed_config_fails(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "ensure_app_dir", lambda: None)

    missing = config.load_config()
    assert missing == config.DEFAULT_CONFIG
    assert missing is not config.DEFAULT_CONFIG

    path.write_text("", encoding="utf-8")
    with pytest.raises(config.LegacyConfigError):
        config.load_config()

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(config.LegacyConfigError):
        config.load_config()

    path.write_bytes(b'{"probe_url":"\xff"}')
    with pytest.raises(config.LegacyConfigError):
        config.load_config()

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(config.LegacyConfigError):
        config.load_config()


def test_partial_config_preserves_defaults_but_wrong_field_types_fail(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "ensure_app_dir", lambda: None)
    path.write_text(json.dumps({"probe_interval": "10s"}), encoding="utf-8")

    partial = config.load_config()

    assert partial["probe_interval"] == "10s"
    assert partial["group_rules"] == config.DEFAULT_CONFIG["group_rules"]
    assert partial["autoselects"] == config.DEFAULT_CONFIG["autoselects"]

    path.write_text(
        json.dumps({"autoselects": "wrong", "group_rules": []}), encoding="utf-8"
    )
    with pytest.raises(config.LegacyConfigError):
        config.load_config()


def test_valid_legacy_config_imports_once_and_preserves_values(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "ensure_app_dir", lambda: None)
    path.write_text(
        json.dumps(
            {
                "probe_interval": "30s",
                "autoselects": [
                    {
                        "id": "legacy",
                        "name": "Legacy",
                        "strategy": "leastLoad",
                        "selected_node_ids": ["node-1"],
                        "tag_filter": ["DE"],
                    }
                ],
                "group_rules": {"clients": ["legacy"]},
                "client_group_overrides": {
                    "sub-1": ["clients"],
                    "sub-legacy-string": "clients,admins",
                },
                "node_catalog": [{"id": "node-1", "name": "Node 1"}],
            }
        ),
        encoding="utf-8",
    )

    async def exercise():
        store = Storage(tmp_path / "import.db")
        await store.connect()
        assert await store.migrate_from_config(config.load_config()) is True
        assert await store.migrate_from_config({"autoselects": []}) is False
        autos = await store.get_autoselects()
        assert [(item["id"], item["strategy"]) for item in autos] == [
            ("legacy", "leastLoad")
        ]
        assert await store.get_group_rules() == {"clients": ["legacy"]}
        assert await store.get_client_group_overrides() == {
            "sub-1": ["clients"],
            "sub-legacy-string": ["clients", "admins"],
        }
        assert [row["fingerprint"] for row in await store.get_node_catalog()] == [
            "node-1"
        ]
        await store.close()

    asyncio.run(exercise())


def test_fixed_config_is_imported_after_previous_malformed_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "ensure_app_dir", lambda: None)
    path.write_text("{broken", encoding="utf-8")

    async def exercise():
        store = Storage(tmp_path / "fixed-after-broken.db")
        await store.connect()
        try:
            broken_config = None
            try:
                broken_config = config.load_config()
            except config.LegacyConfigError:
                pass
            if broken_config is not None:
                assert await store.migrate_from_config(broken_config) is True

            path.write_text(
                json.dumps(
                    {
                        "autoselects": [
                            {
                                "id": "corrected",
                                "name": "Corrected",
                                "selected_node_ids": ["*"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            assert await store.migrate_from_config(config.load_config()) is True
            assert any(
                item["id"] == "corrected" for item in await store.get_autoselects()
            )
        finally:
            await store.close()

    asyncio.run(exercise())


def test_legacy_import_rolls_back_without_marker_and_retries(tmp_path, monkeypatch):
    cfg = {
        "autoselects": [
            {
                "id": "retry",
                "name": "Retry",
                "selected_node_ids": ["*"],
            }
        ],
        "group_rules": {"retry-group": ["retry"]},
        "client_group_overrides": {},
        "node_catalog": [],
    }

    async def exercise():
        store = Storage(tmp_path / "rollback-retry.db")
        await store.connect()
        original_verify = store._verify_config_import

        async def fail_verify(_cfg):
            raise DatabaseIntegrityError("simulated verification failure")

        monkeypatch.setattr(store, "_verify_config_import", fail_verify)
        with pytest.raises(DatabaseIntegrityError):
            await store.migrate_from_config(cfg)
        assert await store.get_meta("config_migrated", "0") == "0"
        assert not any(item["id"] == "retry" for item in await store.get_autoselects())
        assert "retry-group" not in await store.get_group_rules()

        monkeypatch.setattr(store, "_verify_config_import", original_verify)
        assert await store.migrate_from_config(cfg) is True
        assert await store.get_meta("config_migrated") == "1"
        assert await store.migrate_from_config(cfg) is False
        assert [
            item["id"] for item in await store.get_autoselects() if item["id"] == "retry"
        ] == ["retry"]
        await store.close()

    asyncio.run(exercise())


def test_concurrent_legacy_import_is_single_commit(tmp_path):
    cfg = {
        "autoselects": [
            {
                "id": "single-flight-import",
                "name": "Single Import",
                "selected_node_ids": ["*"],
            }
        ],
        "group_rules": {},
        "client_group_overrides": {},
        "node_catalog": [],
    }

    async def exercise():
        store = Storage(tmp_path / "concurrent-import.db")
        await store.connect()
        try:
            outcomes = await asyncio.gather(
                store.migrate_from_config(cfg),
                store.migrate_from_config(cfg),
            )
            assert sorted(outcomes) == [False, True]
            assert [
                item["id"]
                for item in await store.get_autoselects()
                if item["id"] == "single-flight-import"
            ] == ["single-flight-import"]
        finally:
            await store.close()

    asyncio.run(exercise())
