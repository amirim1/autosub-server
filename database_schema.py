from database_errors import DatabaseIntegrityError, UnsupportedSchemaVersionError


SCHEMA_VERSION = 4
APPLICATION_TABLES = {
    "meta",
    "client_groups",
    "node_catalog",
    "group_rules",
    "autoselects",
    "client_group_overrides",
}
BASE_CORE_COLUMNS = {
    "client_groups": {"id", "sub_id", "email", "groups", "created_at", "updated_at"},
    "node_catalog": {
        "fingerprint", "name", "protocol", "address", "port", "network", "security", "first_seen"
    },
    "autoselects": {"id", "name", "strategy", "selected_node_ids", "enabled"},
}


async def _rows(connection, sql, parameters=()):
    async with connection.execute(sql, parameters) as cursor:
        return await cursor.fetchall()


def _value(row):
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


async def list_tables(connection):
    rows = await _rows(
        connection,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
    )
    return {str(_value(row)) for row in rows}


async def _columns(connection, table):
    rows = await _rows(connection, f"PRAGMA table_info({table})")
    return {row["name"]: row for row in rows}


async def _require_tables(connection, required=APPLICATION_TABLES):
    if set(required) - await list_tables(connection):
        raise DatabaseIntegrityError("database schema is missing required tables")


async def _require_columns(connection, table, required, forbidden=()):
    columns = await _columns(connection, table)
    names = set(columns)
    if set(required) - names or set(forbidden) & names:
        raise DatabaseIntegrityError(f"database table {table} has an inconsistent schema")
    return columns


async def _require_added_column(connection, table, column, expected_default):
    columns = await _require_columns(connection, table, {column})
    info = columns[column]
    if int(info["notnull"]) != 1 or info["dflt_value"] != expected_default:
        raise DatabaseIntegrityError(f"database table {table} has an inconsistent schema")


async def _require_index(connection, table, name, columns, *, unique=False):
    rows = await _rows(connection, f"PRAGMA index_list({table})")
    indexes = {row["name"]: row for row in rows}
    info = indexes.get(name)
    if info is None or bool(info["unique"]) is not unique:
        raise DatabaseIntegrityError(f"database table {table} has an inconsistent index")
    actual = [row["name"] for row in await _rows(connection, f"PRAGMA index_info({name})")]
    if actual != list(columns):
        raise DatabaseIntegrityError(f"database table {table} has an inconsistent index")


async def check_integrity(connection):
    quick_rows = await _rows(connection, "PRAGMA quick_check")
    if [_value(row) for row in quick_rows] != ["ok"]:
        raise DatabaseIntegrityError("SQLite quick_check failed")
    if await _rows(connection, "PRAGMA foreign_key_check"):
        raise DatabaseIntegrityError("SQLite foreign_key_check failed")


async def read_schema_version(connection):
    if "meta" not in await list_tables(connection):
        raise UnsupportedSchemaVersionError("database schema metadata is missing")
    rows = await _rows(
        connection, "SELECT value FROM meta WHERE key = 'schema_version'"
    )
    if len(rows) != 1:
        raise UnsupportedSchemaVersionError("database schema version is missing")
    raw = str(_value(rows[0]))
    try:
        version = int(raw)
    except ValueError as exc:
        raise UnsupportedSchemaVersionError("database schema version is malformed") from exc
    if version < 0 or str(version) != raw:
        raise UnsupportedSchemaVersionError("database schema version is malformed")
    if version > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError("database schema version is newer than supported")
    return version


async def validate_legacy_source(connection, version):
    tables = await list_tables(connection)
    if version == 0:
        if tables != {"meta"}:
            raise DatabaseIntegrityError("version 0 database has unexpected application tables")
        return
    if not {"meta", *BASE_CORE_COLUMNS} <= tables:
        raise DatabaseIntegrityError("legacy database is missing required tables")
    for table, required in BASE_CORE_COLUMNS.items():
        forbidden = set()
        if table == "node_catalog":
            forbidden = {"canonical_id", "tag"}
            if version >= 2:
                required = required | {"canonical_id"}
                forbidden.remove("canonical_id")
            if version >= 3:
                required = required | {"tag"}
                forbidden.remove("tag")
        elif table == "autoselects":
            forbidden = {"tag_filter"}
            if version >= 3:
                required = required | {"tag_filter"}
                forbidden.clear()
        await _require_columns(connection, table, required, forbidden)


async def validate_schema(connection, version=SCHEMA_VERSION):
    await _require_tables(connection)
    for table, required in BASE_CORE_COLUMNS.items():
        await _require_columns(connection, table, required)
    await _require_columns(connection, "group_rules", {"group_name", "autoselect_id"})
    await _require_columns(connection, "client_group_overrides", {"key", "groups"})
    if version >= 2:
        await _require_added_column(connection, "node_catalog", "canonical_id", "''")
        await _require_index(
            connection, "client_groups", "uq_client_groups_sub_id", ["sub_id"], unique=True
        )
    if version >= 3:
        await _require_added_column(connection, "node_catalog", "tag", "''")
        await _require_added_column(connection, "autoselects", "tag_filter", "'[]'")
    if version >= 4:
        await _require_index(connection, "client_groups", "idx_client_groups_email", ["email"])
        await _require_index(connection, "node_catalog", "idx_node_catalog_name", ["name"])
