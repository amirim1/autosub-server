# Database Migration

Use this skill for SQLite schema or persistence changes.

1. Read `storage.py`, current schema version logic, migration code and related tests.
2. Identify affected tables, application-enforced relationships and legacy `config.json` behavior.
3. Design an additive, idempotent forward migration with a safe default for existing rows.
4. Preserve existing client groups, node catalog, rules and autoselect definitions.
5. Add migration and rollback/data-integrity tests using a temporary SQLite database.
6. Run `python -m pytest -q` and `python -m compileall -q *.py`.

Never run a migration against the production `data.db` during development. Do not drop tables, rewrite history or silently discard data.
