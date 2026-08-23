import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

import migrations as migrations_module
from database_errors import (
    DatabaseBackupError,
    DatabaseIntegrityError,
    DatabaseMigrationError,
)
from storage import Storage


def _version(path):
    connection = sqlite3.connect(path)
    value = connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    connection.close()
    return value


def test_wal_backup_is_consistent_old_version_and_contains_committed_data(
    tmp_path, legacy_db_factory
):
    db_path = legacy_db_factory(tmp_path / "wal-source.db", 1, wal=True)
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        "INSERT INTO node_catalog (fingerprint, name) VALUES ('wal-node', 'WAL')"
    )
    writer.commit()
    backup_root = tmp_path / "nested" / "shared" / "backups"

    async def exercise():
        store = Storage(db_path, backup_dir=backup_root)
        await store.connect()
        backup_path = store.last_backup_path
        await store.close()
        return backup_path

    backup_path = asyncio.run(exercise())
    writer.close()

    assert backup_path is not None
    assert backup_path.parent == backup_root
    assert backup_path.name.startswith("data-v1-before-v5-")
    backup = sqlite3.connect(backup_path)
    assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
    assert backup.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0] == "1"
    assert backup.execute(
        "SELECT fingerprint FROM node_catalog ORDER BY fingerprint"
    ).fetchall() == [("legacy-node",), ("wal-node",)]
    backup.close()
    if os.name != "nt":
        assert backup_root.stat().st_mode & 0o777 == 0o700
        assert backup_path.stat().st_mode & 0o777 == 0o600


def test_fresh_and_current_database_do_not_create_backups(tmp_path):
    db_path = tmp_path / "current.db"
    backup_root = tmp_path / "backups"

    async def exercise():
        fresh = Storage(db_path, backup_dir=backup_root)
        await fresh.connect()
        assert fresh.last_backup_path is None
        await fresh.close()
        current = Storage(db_path, backup_dir=backup_root)
        await current.connect()
        assert current.last_backup_path is None
        await current.close()

    asyncio.run(exercise())
    assert not backup_root.exists()


def test_backup_failure_blocks_migration(tmp_path, legacy_db_factory):
    db_path = legacy_db_factory(tmp_path / "backup-failure.db", 1)
    backup_root = tmp_path / "not-a-directory"
    backup_root.write_text("occupied", encoding="utf-8")

    async def exercise():
        store = Storage(db_path, backup_dir=backup_root)
        with pytest.raises(DatabaseBackupError):
            await store.connect()

    asyncio.run(exercise())
    assert _version(db_path) == "1"
    assert backup_root.read_text(encoding="utf-8") == "occupied"


def test_pre_migration_quick_check_failure_blocks_backup_and_upgrade(
    tmp_path, legacy_db_factory, monkeypatch
):
    db_path = legacy_db_factory(tmp_path / "quick-check.db", 1)
    backup_root = tmp_path / "backups"

    async def corrupt(_connection):
        raise DatabaseIntegrityError("SQLite quick_check failed")

    monkeypatch.setattr(migrations_module, "check_integrity", corrupt)

    async def exercise():
        with pytest.raises(DatabaseIntegrityError, match="quick_check"):
            await Storage(db_path, backup_dir=backup_root).connect()

    asyncio.run(exercise())
    assert _version(db_path) == "1"
    assert not backup_root.exists()


def test_post_migration_integrity_failure_rolls_back(
    tmp_path, legacy_db_factory, monkeypatch
):
    db_path = legacy_db_factory(tmp_path / "foreign-key-failure.db", 1)
    calls = 0

    async def fail_second_check(_connection):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DatabaseIntegrityError("SQLite foreign_key_check failed")

    monkeypatch.setattr(migrations_module, "check_integrity", fail_second_check)

    async def exercise():
        with pytest.raises(DatabaseIntegrityError, match="foreign_key_check"):
            await Storage(db_path).connect()

    asyncio.run(exercise())
    assert calls == 2
    assert _version(db_path) == "1"
    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(node_catalog)")
        }
    assert "canonical_id" not in columns


def test_malformed_database_fails_with_integrity_error(tmp_path):
    db_path = tmp_path / "malformed.db"
    db_path.write_bytes(b"not a sqlite database")

    async def exercise():
        with pytest.raises(DatabaseIntegrityError):
            await Storage(db_path).connect()

    asyncio.run(exercise())


def test_retries_create_unique_backups_without_overwriting(
    tmp_path, legacy_db_factory
):
    db_path = legacy_db_factory(tmp_path / "retry-backup.db", 1)
    backup_root = tmp_path / "backups"

    def fail_after_alter(stage, target):
        if (stage, target) == ("before_postcondition", 2):
            raise OSError("simulated permission denied")

    async def exercise():
        for _ in range(2):
            with pytest.raises(DatabaseMigrationError):
                await Storage(
                    db_path,
                    backup_dir=backup_root,
                    migration_fault_hook=fail_after_alter,
                ).connect()

    asyncio.run(exercise())
    backups = list(backup_root.glob("data-v1-before-v5-*.db"))
    assert len(backups) == 2
    assert len({path.name for path in backups}) == 2
    assert all(_version(path) == "1" for path in backups)


def test_install_and_update_ship_all_database_runtime_modules():
    root = Path(__file__).parents[1]
    modules = ("database_errors.py", "database_backup.py", "database_schema.py", "migrations.py")
    manifest = (root / "runtime-manifest.txt").read_text(encoding="utf-8").splitlines()
    assert all(module in manifest for module in modules)
    installer = (root / "install.sh").read_text(encoding="utf-8")
    updater = (root / "update.sh").read_text(encoding="utf-8")
    assert 'bash "$TMP_DIR/checkout/update.sh"' in installer
    assert "runtime-manifest.txt" in updater
