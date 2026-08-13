# AutoSub Server Database

## Storage

The application uses SQLite through `aiosqlite`. `AUTOSUB_DB` overrides the path;
release-layout production uses `/opt/autosub-server/shared/data.db`, while local
repository runs retain the repository-local default.

## Tables

- `meta` — schema/configuration metadata and migration markers.
- `client_groups` — primary group assignment and client email keyed by subscription ID.
- `node_catalog` — discovered nodes, fingerprints, canonical IDs, protocol, address, port, network, security and display tag.
- `group_rules` — maps client group names to autoselect IDs.
- `autoselects` — autoselect definitions, strategy, selected node IDs, tag filters and enabled state.
- `client_group_overrides` — explicit group overrides keyed by a stable key such as email or subscription identifier.

The singleton `direct_domains` setting is stored as a validated JSON list in `meta`.
An absent key means the built-in Russian-site defaults, while a stored empty list is
an intentional override. Because this uses the existing key/value table, it does not
change the schema version and remains compatible with existing databases.

Relationships are application-enforced: `group_rules.autoselect_id` refers to an autoselect definition, while selected node IDs refer to the current node catalog. Deleting an autoselect also removes its group rules.

## Migrations

`Storage.connect()` creates the schema and reads the stored schema version. Existing databases are upgraded with targeted `ALTER TABLE` operations for added node and autoselect fields. Do not edit schema definitions without a forward migration and regression tests.

At startup `autosub_server.py` checks `AUTOSUB_CONFIG`/legacy `config.json`. Existing
input must be a valid UTF-8 JSON object with correctly typed import fields. Partial
configs inherit defaults and unknown legacy fields remain compatible, while malformed
JSON, wrong top-level types, invalid collection shapes, missing record IDs, duplicates,
and invalid strategies stop startup safely.

`Storage.migrate_from_config()` uses `BEGIN IMMEDIATE`, UPSERT/insert operations,
post-write verification, and writes `config_migrated=1` last in the same transaction.
Any parse, validation, DB, verification, cancellation, or commit failure leaves the
marker absent, rolls back partial writes, preserves the original config, and permits
retry at the next startup. A successful second startup sees the marker and skips the
import, preventing duplicates.

## Data Safety Rules

- Back up `data.db` before deployment or migration.
- Use temporary databases in tests; do not use the production database.
- Keep migrations backward-compatible and idempotent.
- Avoid destructive table rewrites and unbounded data deletion.
- Verify commits and close the async connection in lifecycle tests.
- Before code activation, the updater creates a verified `pre-update-*.db` through
  SQLite backup API. Code rollback is automatic. DB restore is automatic only for an
  explicitly isolated, stopped pre-traffic phase; the normal systemd runner permits
  traffic after startup and therefore retains the backup for manual recovery instead.
