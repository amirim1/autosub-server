import asyncio
import sqlite3

import aiosqlite
import pytest

import storage as storage_module
from database_errors import DatabaseMigrationError
from storage import Storage, dict_factory


def _create_legacy_database(path, version):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(version),)
    )
    if version == 0:
        conn.commit()
        conn.close()
        return

    node_columns = [
        "fingerprint TEXT PRIMARY KEY",
        "name TEXT NOT NULL DEFAULT ''",
        "protocol TEXT NOT NULL DEFAULT ''",
        "address TEXT NOT NULL DEFAULT ''",
        "port TEXT NOT NULL DEFAULT ''",
        "network TEXT NOT NULL DEFAULT ''",
        "security TEXT NOT NULL DEFAULT ''",
        "first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    ]
    if version >= 2:
        node_columns.insert(1, "canonical_id TEXT NOT NULL DEFAULT ''")
    if version >= 3:
        node_columns.insert(-1, "tag TEXT NOT NULL DEFAULT ''")
    conn.execute(f"CREATE TABLE node_catalog ({', '.join(node_columns)})")

    auto_columns = [
        "id TEXT PRIMARY KEY",
        "name TEXT NOT NULL",
        "strategy TEXT NOT NULL DEFAULT 'leastPing'",
        "selected_node_ids TEXT NOT NULL DEFAULT '[]'",
        "enabled INTEGER NOT NULL DEFAULT 1",
    ]
    if version >= 3:
        auto_columns.insert(-1, "tag_filter TEXT NOT NULL DEFAULT '[]'")
    conn.execute(f"CREATE TABLE autoselects ({', '.join(auto_columns)})")
    conn.execute(
        "CREATE TABLE client_groups ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id TEXT NOT NULL, "
        "email TEXT NOT NULL DEFAULT '', groups TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO node_catalog (fingerprint, name) VALUES ('legacy-node', 'Legacy')"
    )
    conn.execute(
        "INSERT INTO autoselects (id, name, selected_node_ids) "
        "VALUES ('legacy-auto', 'Legacy Auto', '[\"legacy-node\"]')"
    )
    conn.execute(
        "INSERT INTO client_groups (sub_id, email, groups) "
        "VALUES ('legacy-sub', 'legacy@example.test', 'legacy')"
    )
    conn.commit()
    conn.close()


def test_new_database_pragmas_schema_version_and_reinitialization(tmp_path):
    async def exercise():
        db_path = tmp_path / "new.db"
        store = Storage(db_path)
        await store.connect()
        assert await store.get_meta("schema_version") == "4"
        assert store.last_backup_path is None
        for pragma, expected in [
            ("journal_mode", "wal"),
            ("foreign_keys", 1),
            ("busy_timeout", 5000),
        ]:
            async with store.conn.execute(f"PRAGMA {pragma}") as cursor:
                row = await cursor.fetchone()
            assert next(iter(row.values())) == expected
        await store.close()

        reopened = Storage(db_path)
        await reopened.connect()
        assert await reopened.get_meta("schema_version") == "4"
        assert reopened.last_backup_path is None
        await reopened.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("version", [0, 1, 2, 3])
def test_migration_from_reconstructable_schema_versions_preserves_data(
    tmp_path, version
):
    db_path = tmp_path / f"v{version}.db"
    _create_legacy_database(db_path, version)

    async def exercise():
        store = Storage(db_path)
        await store.connect()
        assert await store.get_meta("schema_version") == "4"
        assert store.last_backup_path.parent == tmp_path / "shared" / "backups"
        async with store.conn.execute("PRAGMA table_info(node_catalog)") as cursor:
            node_columns = {row["name"] for row in await cursor.fetchall()}
        async with store.conn.execute("PRAGMA table_info(autoselects)") as cursor:
            auto_columns = {row["name"] for row in await cursor.fetchall()}
        assert {"canonical_id", "tag"} <= node_columns
        assert "tag_filter" in auto_columns
        async with store.conn.execute("PRAGMA quick_check") as cursor:
            assert next(iter((await cursor.fetchone()).values())) == "ok"
        async with store.conn.execute("PRAGMA foreign_key_check") as cursor:
            assert await cursor.fetchall() == []
        if version > 0:
            assert await store.get_client_groups("legacy-sub") == ["legacy"]
            assert any(
                row["fingerprint"] == "legacy-node"
                for row in await store.get_node_catalog()
            )
        await store.close()

        reopened = Storage(db_path)
        await reopened.connect()
        assert await reopened.get_meta("schema_version") == "4"
        await reopened.close()

    asyncio.run(exercise())


def test_node_catalog_replacement_rolls_back_on_constraint_error(tmp_path):
    async def exercise():
        store = Storage(tmp_path / "rollback.db")
        await store.connect()
        await store.set_node_catalog([{"id": "original", "name": "Original"}])
        with pytest.raises(sqlite3.IntegrityError):
            await store.set_node_catalog(
                [
                    {"id": "duplicate", "name": "First"},
                    {"id": "duplicate", "name": "Second"},
                ]
            )
        rows = await store.get_node_catalog()
        assert [row["fingerprint"] for row in rows] == ["original"]
        await store.close()

    asyncio.run(exercise())


def test_autoselect_delete_removes_rules_but_unknown_rule_is_allowed(tmp_path):
    async def exercise():
        store = Storage(tmp_path / "rules.db")
        await store.connect()
        await store.add_autoselect("known", "Known")
        await store.set_group_rules({"group": ["known", "unknown"]})
        assert await store.get_group_rules() == {"group": ["known", "unknown"]}

        await store.delete_autoselect("known")

        assert await store.get_group_rules() == {"group": ["unknown"]}
        await store.close()

    asyncio.run(exercise())


def test_malformed_autoselect_json_fields_fall_back_to_empty_lists(tmp_path):
    async def exercise():
        store = Storage(tmp_path / "malformed.db")
        await store.connect()
        await store.conn.execute(
            "INSERT INTO autoselects "
            "(id, name, selected_node_ids, tag_filter) VALUES (?, ?, ?, ?)",
            ("bad-json", "Bad JSON", "not-json", "{wrong}"),
        )
        await store.conn.commit()

        item = next(row for row in await store.get_autoselects() if row["id"] == "bad-json")

        assert item["selected_node_ids"] == []
        assert item["tag_filter"] == []
        await store.close()

    asyncio.run(exercise())


def test_read_only_current_database_can_initialize_but_rejects_writes(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "readonly.db"

    async def prepare():
        store = Storage(db_path)
        await store.connect()
        await store.close()

    asyncio.run(prepare())
    real_connect = aiosqlite.connect
    monkeypatch.setattr(
        storage_module.aiosqlite,
        "connect",
        lambda database: real_connect(database, uri=True),
    )

    async def exercise():
        store = Storage(f"file:{db_path.as_posix()}?mode=ro")
        await store.connect()
        try:
            assert await store.get_meta("schema_version") == "4"
            with pytest.raises(sqlite3.OperationalError):
                await store.set_meta("write", "rejected")
        finally:
            await store.close()

    asyncio.run(exercise())


class _FaultyMigrationConnection:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def execute(self, sql, parameters=None):
        if "ADD COLUMN canonical_id" in sql:
            raise sqlite3.OperationalError("simulated disk I/O failure")
        if parameters is None:
            return self.inner.execute(sql)
        return self.inner.execute(sql, parameters)


def test_unexpected_migration_error_does_not_advance_schema_version(tmp_path):
    db_path = tmp_path / "migration-error.db"
    _create_legacy_database(db_path, 1)

    async def exercise():
        inner = await aiosqlite.connect(db_path)
        inner.row_factory = dict_factory
        store = Storage(db_path)
        store.conn = _FaultyMigrationConnection(inner)
        try:
            with pytest.raises(DatabaseMigrationError):
                await store._init_schema()
            assert await store.get_meta("schema_version") == "1"
            async with inner.execute("PRAGMA table_info(node_catalog)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            assert "canonical_id" not in columns
            assert await store.get_client_groups("legacy-sub") == ["legacy"]
        finally:
            await inner.close()

    asyncio.run(exercise())
