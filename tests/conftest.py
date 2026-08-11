import sqlite3

import pytest


@pytest.fixture
def legacy_db_factory():
    def create(path, version, *, shape_version=None, wal=False):
        shape = int(version) if shape_version is None else shape_version
        connection = sqlite3.connect(path)
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )
        if shape == 0:
            connection.commit()
            connection.close()
            return path

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
        if shape >= 2:
            node_columns.insert(1, "canonical_id TEXT NOT NULL DEFAULT ''")
        if shape >= 3:
            node_columns.insert(-1, "tag TEXT NOT NULL DEFAULT ''")
        auto_columns = [
            "id TEXT PRIMARY KEY",
            "name TEXT NOT NULL",
            "strategy TEXT NOT NULL DEFAULT 'leastPing'",
            "selected_node_ids TEXT NOT NULL DEFAULT '[]'",
            "enabled INTEGER NOT NULL DEFAULT 1",
        ]
        if shape >= 3:
            auto_columns.insert(-1, "tag_filter TEXT NOT NULL DEFAULT '[]'")
        connection.execute(f"CREATE TABLE node_catalog ({', '.join(node_columns)})")
        connection.execute(f"CREATE TABLE autoselects ({', '.join(auto_columns)})")
        connection.execute(
            "CREATE TABLE client_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "sub_id TEXT NOT NULL, email TEXT NOT NULL DEFAULT '', "
            "groups TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO node_catalog (fingerprint, name) VALUES ('legacy-node', 'Legacy')"
        )
        connection.execute(
            "INSERT INTO autoselects (id, name) VALUES ('legacy-auto', 'Legacy Auto')"
        )
        connection.execute(
            "INSERT INTO client_groups (sub_id, email, groups) "
            "VALUES ('legacy-sub', 'legacy@example.test', 'legacy')"
        )
        connection.commit()
        connection.close()
        return path

    return create
