import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from database_backup import create_sqlite_backup
from database_errors import DatabaseIntegrityError, DatabaseMigrationError
from database_schema import (
    SCHEMA_VERSION,
    check_integrity,
    list_tables,
    read_schema_version,
    validate_legacy_source,
    validate_schema,
)


COMMON_SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS group_rules (group_name TEXT NOT NULL, autoselect_id TEXT NOT NULL, PRIMARY KEY (group_name, autoselect_id))",
    "CREATE TABLE IF NOT EXISTS client_group_overrides (key TEXT PRIMARY KEY, groups TEXT NOT NULL DEFAULT '')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_groups_sub_id ON client_groups(sub_id)",
)


async def _ensure_common_schema(connection):
    for statement in COMMON_SCHEMA_SQL:
        await connection.execute(statement)


async def _to_v1(connection):
    statements = (
        "CREATE TABLE client_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id TEXT NOT NULL, email TEXT NOT NULL DEFAULT '', groups TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')))",
        "CREATE TABLE node_catalog (fingerprint TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', protocol TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', port TEXT NOT NULL DEFAULT '', network TEXT NOT NULL DEFAULT '', security TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL DEFAULT (datetime('now')))",
        "CREATE TABLE autoselects (id TEXT PRIMARY KEY, name TEXT NOT NULL, strategy TEXT NOT NULL DEFAULT 'leastPing', selected_node_ids TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1)",
    )
    for statement in statements:
        await connection.execute(statement)
    await _ensure_common_schema(connection)


async def _to_v2(connection):
    await _ensure_common_schema(connection)
    await connection.execute(
        "ALTER TABLE node_catalog ADD COLUMN canonical_id TEXT NOT NULL DEFAULT ''"
    )


async def _to_v3(connection):
    await _ensure_common_schema(connection)
    await connection.execute("ALTER TABLE node_catalog ADD COLUMN tag TEXT NOT NULL DEFAULT ''")
    await connection.execute(
        "ALTER TABLE autoselects ADD COLUMN tag_filter TEXT NOT NULL DEFAULT '[]'"
    )


async def _to_v4(connection):
    await _ensure_common_schema(connection)
    await connection.execute("CREATE INDEX idx_client_groups_email ON client_groups(email)")
    await connection.execute("CREATE INDEX idx_node_catalog_name ON node_catalog(name)")


@dataclass(frozen=True)
class MigrationStep:
    target: int
    apply: Callable


MIGRATIONS = {
    1: MigrationStep(1, _to_v1),
    2: MigrationStep(2, _to_v2),
    3: MigrationStep(3, _to_v3),
    4: MigrationStep(4, _to_v4),
}


async def initialize_fresh_database(connection, *, fault_hook=None):
    try:
        await connection.execute("BEGIN IMMEDIATE")
        if await list_tables(connection):
            await connection.commit()
            return False
        await connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await connection.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '0')"
        )
        for target in range(1, SCHEMA_VERSION + 1):
            step = MIGRATIONS[target]
            if fault_hook:
                fault_hook("before_apply", target)
            await step.apply(connection)
            await validate_schema(connection, target)
            await connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(target),)
            )
        await check_integrity(connection)
        await connection.commit()
        return True
    except Exception as exc:
        await connection.rollback()
        if isinstance(exc, DatabaseMigrationError):
            raise
        raise DatabaseMigrationError("fresh database initialization failed") from exc
    except BaseException:
        await connection.rollback()
        raise


async def run_migrations(connection, source_version, *, migrations=None, fault_hook=None):
    steps: Mapping[int, MigrationStep] = MIGRATIONS if migrations is None else migrations
    try:
        await connection.execute("BEGIN IMMEDIATE")
        locked_version = await read_schema_version(connection)
        if locked_version == SCHEMA_VERSION:
            await validate_schema(connection)
            await connection.commit()
            return False
        if locked_version != source_version:
            raise DatabaseMigrationError("database schema version changed unexpectedly")
        for target in range(locked_version + 1, SCHEMA_VERSION + 1):
            step = steps.get(target)
            if step is None or step.target != target:
                raise DatabaseMigrationError("required database migration step is missing")
            await validate_legacy_source(connection, target - 1)
            if fault_hook:
                fault_hook("before_apply", target)
            await step.apply(connection)
            if fault_hook:
                fault_hook("before_postcondition", target)
            await validate_schema(connection, target)
            if fault_hook:
                fault_hook("before_version_update", target)
            await connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(target),)
            )
            if fault_hook:
                fault_hook("after_version_update", target)
        await check_integrity(connection)
        await connection.commit()
        return True
    except Exception as exc:
        await connection.rollback()
        if isinstance(exc, DatabaseMigrationError):
            raise
        raise DatabaseMigrationError("database migration failed") from exc
    except BaseException:
        await connection.rollback()
        raise


async def prepare_database(
    connection,
    backup_dir,
    *,
    migrations=None,
    fault_hook=None,
    version_observer=None,
):
    try:
        tables = await list_tables(connection)
        if not tables:
            initialized = await initialize_fresh_database(
                connection, fault_hook=fault_hook
            )
            if initialized:
                return None

        await check_integrity(connection)
        source_version = await read_schema_version(connection)
        if version_observer:
            version_observer(source_version)
        if source_version == SCHEMA_VERSION:
            await validate_schema(connection)
            return None

        await validate_legacy_source(connection, source_version)
        backup_path = await create_sqlite_backup(
            connection, backup_dir, source_version, SCHEMA_VERSION
        )
        await run_migrations(
            connection,
            source_version,
            migrations=migrations,
            fault_hook=fault_hook,
        )
        await validate_schema(connection)
        return backup_path
    except DatabaseMigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise DatabaseIntegrityError("SQLite database operation failed") from exc
