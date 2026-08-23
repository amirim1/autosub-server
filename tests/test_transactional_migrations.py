import asyncio
import logging
import sqlite3

import pytest

from database_errors import (
    DatabaseIntegrityError,
    DatabaseMigrationError,
    UnsupportedSchemaVersionError,
)
from migrations import MIGRATIONS, MigrationStep
from storage import Storage


def _database_state(path):
    connection = sqlite3.connect(path)
    version = connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(node_catalog)")
    }
    rows = connection.execute(
        "SELECT fingerprint, name FROM node_catalog ORDER BY fingerprint"
    ).fetchall()
    connection.close()
    return version, columns, rows


@pytest.mark.parametrize(
    ("stage", "target"),
    [
        ("before_apply", 2),
        ("before_postcondition", 2),
        ("before_version_update", 2),
        ("after_version_update", 2),
        ("before_apply", 3),
    ],
)
def test_failure_rolls_back_entire_upgrade_and_retry_succeeds(
    tmp_path, legacy_db_factory, stage, target
):
    db_path = legacy_db_factory(tmp_path / f"rollback-{stage}-{target}.db", 1)

    def fail(current_stage, current_target):
        if (current_stage, current_target) == (stage, target):
            raise sqlite3.OperationalError("simulated disk full")

    async def exercise():
        failing = Storage(db_path, migration_fault_hook=fail)
        with pytest.raises(DatabaseMigrationError):
            await failing.connect()
        assert failing.conn is None
        assert _database_state(db_path) == (
            "1",
            {
                "fingerprint", "name", "protocol", "address", "port", "network",
                "security", "first_seen",
            },
            [("legacy-node", "Legacy")],
        )

        retry = Storage(db_path)
        await retry.connect()
        assert await retry.get_meta("schema_version") == "5"
        await retry.close()

    asyncio.run(exercise())


def test_missing_migration_step_fails_without_version_change(tmp_path, legacy_db_factory):
    db_path = legacy_db_factory(tmp_path / "missing-step.db", 1)
    incomplete = {version: step for version, step in MIGRATIONS.items() if version != 2}

    async def exercise():
        store = Storage(db_path, migrations=incomplete)
        with pytest.raises(DatabaseMigrationError, match="step is missing"):
            await store.connect()

    asyncio.run(exercise())
    assert _database_state(db_path)[0] == "1"


@pytest.mark.parametrize("value", ["6", "-1", "not-a-version", "01"])
def test_unsupported_or_malformed_versions_fail(
    tmp_path, legacy_db_factory, value
):
    db_path = legacy_db_factory(
        tmp_path / f"bad-version-{value}.db", value, shape_version=1
    )

    async def exercise():
        store = Storage(db_path)
        with pytest.raises(UnsupportedSchemaVersionError):
            await store.connect()

    asyncio.run(exercise())
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == value


def test_missing_metadata_and_conflicting_declared_schema_fail(
    tmp_path, legacy_db_factory
):
    missing_meta = tmp_path / "missing-meta.db"
    connection = sqlite3.connect(missing_meta)
    connection.execute("CREATE TABLE unexpected (value TEXT)")
    connection.commit()
    connection.close()
    missing_version = tmp_path / "missing-version.db"
    connection = sqlite3.connect(missing_version)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    partial = legacy_db_factory(tmp_path / "partial.db", 1, shape_version=2)
    stale = legacy_db_factory(tmp_path / "stale-current.db", 4, shape_version=1)

    async def exercise(path, error):
        store = Storage(path)
        with pytest.raises(error):
            await store.connect()

    asyncio.run(exercise(missing_meta, UnsupportedSchemaVersionError))
    asyncio.run(exercise(missing_version, UnsupportedSchemaVersionError))
    asyncio.run(exercise(partial, DatabaseIntegrityError))
    asyncio.run(exercise(stale, DatabaseIntegrityError))
    assert _database_state(partial)[0] == "1"
    assert _database_state(stale)[0] == "4"


def test_current_schema_with_wrong_index_fails_validation(tmp_path):
    db_path = tmp_path / "wrong-index.db"

    async def prepare():
        store = Storage(db_path)
        await store.connect()
        await store.close()

    asyncio.run(prepare())
    connection = sqlite3.connect(db_path)
    connection.execute("DROP INDEX idx_node_catalog_name")
    connection.execute("CREATE INDEX idx_node_catalog_name ON node_catalog(protocol)")
    connection.commit()
    connection.close()

    async def exercise():
        with pytest.raises(DatabaseIntegrityError):
            await Storage(db_path).connect()

    asyncio.run(exercise())


def test_two_concurrent_startups_apply_each_migration_once(
    tmp_path, legacy_db_factory
):
    db_path = legacy_db_factory(tmp_path / "concurrent.db", 1, wal=True)
    calls = {version: 0 for version in range(2, 6)}
    steps = dict(MIGRATIONS)
    for version in range(2, 6):
        original = MIGRATIONS[version]

        async def counted(connection, *, _version=version, _apply=original.apply):
            calls[_version] += 1
            await _apply(connection)

        steps[version] = MigrationStep(version, counted)

    async def exercise():
        first = Storage(db_path, migrations=steps)
        second = Storage(db_path, migrations=steps)
        await asyncio.gather(first.connect(), second.connect())
        await first.close()
        await second.close()

    asyncio.run(exercise())
    assert calls == {2: 1, 3: 1, 4: 1, 5: 1}
    assert _database_state(db_path)[0] == "5"
    assert _database_state(db_path)[2] == [("legacy-node", "Legacy")]


def test_startup_failure_log_has_versions_without_sensitive_details(
    tmp_path, legacy_db_factory, caplog
):
    db_path = legacy_db_factory(tmp_path / "safe-log.db", 1)
    sensitive = "client@example.test subscription-secret"

    def fail(stage, target):
        if (stage, target) == ("before_apply", 2):
            raise RuntimeError(sensitive)

    async def exercise():
        with pytest.raises(DatabaseMigrationError):
            await Storage(db_path, migration_fault_hook=fail).connect()

    caplog.set_level(logging.ERROR, logger="autosub")
    asyncio.run(exercise())
    assert "current_version=1 target_version=5" in caplog.text
    assert sensitive not in caplog.text
    assert all(getattr(record, "request_id", None) == "-" for record in caplog.records)
