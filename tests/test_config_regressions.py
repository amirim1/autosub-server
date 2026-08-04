import asyncio
import json

import pytest

import config
from storage import Storage


def test_missing_empty_and_malformed_config_use_defaults(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "ensure_app_dir", lambda: None)

    missing = config.load_config()
    assert missing == config.DEFAULT_CONFIG
    assert missing is not config.DEFAULT_CONFIG

    path.write_text("", encoding="utf-8")
    assert config.load_config() == config.DEFAULT_CONFIG

    path.write_text("{not json", encoding="utf-8")
    assert config.load_config() == config.DEFAULT_CONFIG


def test_partial_config_preserves_defaults_but_wrong_field_types_are_accepted(
    tmp_path, monkeypatch
):
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
    wrong_types = config.load_config()
    assert wrong_types["autoselects"] == "wrong"
    assert wrong_types["group_rules"] == []


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
                "client_group_overrides": {"sub-1": ["clients"]},
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
        assert await store.get_client_group_overrides() == {"sub-1": ["clients"]}
        assert [row["fingerprint"] for row in await store.get_node_catalog()] == [
            "node-1"
        ]
        await store.close()

    asyncio.run(exercise())


@pytest.mark.xfail(
    strict=True,
    reason="Known issue: malformed config imports defaults and permanently sets config_migrated",
)
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
            except (json.JSONDecodeError, ValueError):
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
