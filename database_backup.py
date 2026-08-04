import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from database_errors import DatabaseBackupError


def _value(row):
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


async def _rows(connection, sql):
    async with connection.execute(sql) as cursor:
        return await cursor.fetchall()


async def _verify_backup(connection, expected_version):
    quick_rows = await _rows(connection, "PRAGMA quick_check")
    if [_value(row) for row in quick_rows] != ["ok"]:
        raise DatabaseBackupError("backup quick_check failed")
    if await _rows(connection, "PRAGMA foreign_key_check"):
        raise DatabaseBackupError("backup foreign_key_check failed")
    rows = await _rows(
        connection, "SELECT value FROM meta WHERE key = 'schema_version'"
    )
    if len(rows) != 1 or str(_value(rows[0])) != str(expected_version):
        raise DatabaseBackupError("backup schema version does not match source")


async def create_sqlite_backup(connection, backup_dir, source_version, target_version):
    root = Path(backup_dir)
    path = None
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not root.is_dir():
            raise OSError("backup root is not a directory")
        if os.name != "nt":
            root.chmod(0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        for _ in range(10):
            name = (
                f"data-v{source_version}-before-v{target_version}-{stamp}-"
                f"{secrets.token_hex(4)}.db"
            )
            path = root / name
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            os.close(descriptor)
            break
        else:
            raise OSError("could not reserve a unique backup name")
        destination = await aiosqlite.connect(path)
        try:
            await connection.backup(destination)
        finally:
            await destination.close()
        if os.name != "nt":
            path.chmod(0o600)
        verify = await aiosqlite.connect(path)
        try:
            await _verify_backup(verify, source_version)
        finally:
            await verify.close()
        return path
    except Exception as exc:
        if path is not None and path.exists():
            path.unlink(missing_ok=True)
        if isinstance(exc, DatabaseBackupError):
            raise
        raise DatabaseBackupError("could not create a consistent database backup") from exc
