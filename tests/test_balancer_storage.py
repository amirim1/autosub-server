import asyncio
import sqlite3

import pytest

from config import DEFAULT_DIRECT_DOMAINS
from storage import Storage


def test_sticky_domains_roundtrip_and_defaults(tmp_path):
    async def exercise():
        store = Storage(tmp_path / "sticky.db")
        await store.connect()
        assert store.get_meta is not None
        assert await store.get_sticky_domains() == []

        await store.set_sticky_domains(["domain:netflix.com", "domain:netflix.com", "full:api.bank.ru"])
        assert await store.get_sticky_domains() == ["domain:netflix.com", "full:api.bank.ru"]

        await store.set_meta("sticky_domains", "not-json")
        assert await store.get_sticky_domains() == []
        await store.close()

    asyncio.run(exercise())


def test_direct_domains_defaults_unchanged(tmp_path):
    async def exercise():
        store = Storage(tmp_path / "direct.db")
        await store.connect()
        assert await store.get_direct_domains() == list(DEFAULT_DIRECT_DOMAINS)
        await store.close()

    asyncio.run(exercise())


def test_autoselect_country_scope_roundtrip(tmp_path):
    async def exercise():
        store = Storage(tmp_path / "scope.db")
        await store.connect()

        await store.add_autoselect("auto-1", "Auto One", country_scope=True)
        autos = {a["id"]: a for a in await store.get_autoselects()}
        assert autos["auto-1"]["country_scope"] is True

        await store.update_autoselect("auto-1", country_scope=False)
        autos = {a["id"]: a for a in await store.get_autoselects()}
        assert autos["auto-1"]["country_scope"] is False

        await store.update_autoselect("auto-1", strategy="leastLoad")
        autos = {a["id"]: a for a in await store.get_autoselects()}
        assert autos["auto-1"]["strategy"] == "leastLoad"
        assert autos["auto-1"]["country_scope"] is False

        with pytest.raises(sqlite3.IntegrityError):
            await store.add_autoselect("auto-1", "Duplicate")
        await store.close()

    asyncio.run(exercise())


def test_migration_v4_to_v5_preserves_rows_and_default(tmp_path):
    db_path = tmp_path / "v4.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '4')")
    conn.execute(
        "CREATE TABLE client_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id TEXT NOT NULL, "
        "email TEXT NOT NULL DEFAULT '', groups TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE node_catalog (fingerprint TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
        "protocol TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', port TEXT NOT NULL DEFAULT '', "
        "network TEXT NOT NULL DEFAULT '', security TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE autoselects (id TEXT PRIMARY KEY, name TEXT NOT NULL, strategy TEXT NOT NULL DEFAULT 'leastPing', "
        "selected_node_ids TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute("ALTER TABLE node_catalog ADD COLUMN canonical_id TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE node_catalog ADD COLUMN tag TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE autoselects ADD COLUMN tag_filter TEXT NOT NULL DEFAULT '[]'")
    conn.execute(
        "CREATE UNIQUE INDEX uq_client_groups_sub_id ON client_groups(sub_id)"
    )
    conn.execute("CREATE INDEX idx_client_groups_email ON client_groups(email)")
    conn.execute("CREATE INDEX idx_node_catalog_name ON node_catalog(name)")
    conn.execute(
        "CREATE TABLE group_rules (group_name TEXT NOT NULL, autoselect_id TEXT NOT NULL, PRIMARY KEY (group_name, autoselect_id))"
    )
    conn.execute(
        "CREATE TABLE client_group_overrides (key TEXT PRIMARY KEY, groups TEXT NOT NULL DEFAULT '')"
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('config_migrated', '1')")
    conn.execute(
        "INSERT INTO autoselects (id, name, strategy, selected_node_ids, tag_filter, enabled) "
        "VALUES ('legacy', 'Legacy', 'leastPing', '[\"*\"]', '[]', 1)"
    )
    conn.commit()
    conn.close()

    async def exercise():
        store = Storage(db_path)
        await store.connect()
        assert await store.get_meta("schema_version") == "5"
        autos = await store.get_autoselects()
        assert len(autos) == 1
        assert autos[0]["country_scope"] is False
        assert autos[0]["selected_node_ids"] == ["*"]
        await store.close()

    asyncio.run(exercise())
