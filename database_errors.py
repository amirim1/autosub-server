class DatabaseMigrationError(RuntimeError):
    """A schema migration could not complete safely."""


class UnsupportedSchemaVersionError(DatabaseMigrationError):
    """The database version is malformed or newer than this application."""


class DatabaseIntegrityError(DatabaseMigrationError):
    """The declared schema or SQLite integrity checks are inconsistent."""


class DatabaseBackupError(DatabaseMigrationError):
    """A consistent pre-migration backup could not be created."""
