import asyncio
import json
import sqlite3
from pathlib import Path

import config
from release_manager import ReleaseLayout, migrate_legacy_persistent
from storage import Storage


class MemoryLinks:
    def __init__(self):
        self.targets = {}

    def is_link(self, path):
        return Path(path) in self.targets

    def read(self, path):
        return self.targets[Path(path)]

    def create(self, target, path):
        self.targets[Path(path)] = Path(target)

    def replace(self, source, destination):
        self.targets[Path(destination)] = self.targets.pop(Path(source))

    def unlink(self, path):
        del self.targets[Path(path)]


def test_fresh_install_tree_initializes_shared_database(tmp_path):
    root = tmp_path / "autosub-server"
    links = MemoryLinks()
    layout = ReleaseLayout(root, links=links)
    release = layout.prepare_release(Path(), Path("runtime-manifest.txt"), "fresh-a")
    (release / "venv").mkdir()
    (layout.shared / ".env").write_text("AUTOSUB_SECRET_KEY=test\n", encoding="utf-8")
    (layout.shared / "config.json").write_text("{}\n", encoding="utf-8")
    layout.atomic_switch("fresh-a")

    async def initialize():
        store = Storage(layout.shared / "data.db", backup_dir=layout.shared / "backups")
        await store.connect()
        try:
            assert await store.get_meta("schema_version") == "5"
        finally:
            await store.close()

    asyncio.run(initialize())

    assert layout.current_release() == "fresh-a"
    assert {path.name for path in root.iterdir()} >= {"releases", "shared"}
    assert {path.name for path in layout.shared.iterdir()} >= {
        ".env",
        "config.json",
        "data.db",
        "backups",
    }


def test_flat_layout_migration_imports_config_once_after_database_copy(tmp_path, monkeypatch):
    root = tmp_path / "autosub-server"
    root.mkdir()
    (root / ".env").write_text("AUTOSUB_SECRET_KEY=preserved\n", encoding="utf-8")
    legacy_config = {
        "autoselects": [
            {
                "id": "legacy-smoke",
                "name": "Legacy Smoke",
                "selected_node_ids": ["*"],
            }
        ],
        "group_rules": {"smoke": ["legacy-smoke"]},
        "client_group_overrides": {"sub-smoke": ["smoke"]},
        "node_catalog": [],
    }
    (root / "config.json").write_text(json.dumps(legacy_config), encoding="utf-8")

    async def create_flat_database():
        store = Storage(root / "data.db", backup_dir=root / "backups")
        await store.connect()
        await store.close()

    asyncio.run(create_flat_database())
    backup = migrate_legacy_persistent(root, "legacy-smoke")

    shared_config = root / "shared/config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", shared_config)
    monkeypatch.setattr(config, "ensure_app_dir", lambda: None)

    async def import_and_restart():
        store = Storage(root / "shared/data.db", backup_dir=root / "shared/backups")
        await store.connect()
        try:
            assert await store.migrate_from_config(config.load_config()) is True
            assert await store.migrate_from_config(config.load_config()) is False
            assert [
                item["id"] for item in await store.get_autoselects() if item["id"] == "legacy-smoke"
            ] == ["legacy-smoke"]
            assert await store.get_group_rules() == {"smoke": ["legacy-smoke"]}
            assert await store.get_client_group_overrides() == {"sub-smoke": ["smoke"]}
            assert await store.get_meta("config_migrated") == "1"
        finally:
            await store.close()

    asyncio.run(import_and_restart())

    assert backup is not None and backup.is_file()
    connection = sqlite3.connect(root / "shared/data.db")
    try:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        connection.close()
    assert (root / "shared/.env").read_text(encoding="utf-8") == ("AUTOSUB_SECRET_KEY=preserved\n")
    assert shared_config.is_file()
