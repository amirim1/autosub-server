import copy
import json
import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from logger import logger
from database_errors import DatabaseIntegrityError
from migrations import MIGRATIONS, SCHEMA_VERSION, prepare_database


SUPPORTED_AUTOSELECT_STRATEGIES = {"leastPing", "leastLoad"}


def normalize_autoselect_strategy(strategy):
    return strategy if strategy in SUPPORTED_AUTOSELECT_STRATEGIES else "leastPing"


def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


class Storage:
    def __init__(
        self,
        db_path,
        *,
        backup_dir=None,
        migrations=None,
        migration_fault_hook=None,
    ):
        self.db_path = str(db_path)
        db_file = Path(db_path)
        self.backup_dir = Path(backup_dir) if backup_dir else db_file.parent / "shared" / "backups"
        self.migrations = MIGRATIONS if migrations is None else migrations
        self.migration_fault_hook = migration_fault_hook
        self.last_backup_path = None
        self._schema_version_before = "unknown"
        self._lock = asyncio.Lock()
        self.conn = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = dict_factory
        try:
            await self.conn.execute("PRAGMA journal_mode=WAL")
            await self.conn.execute("PRAGMA foreign_keys=ON")
            await self.conn.execute("PRAGMA busy_timeout=5000")
            await self._init_schema()
        except sqlite3.DatabaseError as exc:
            logger.exception(
                "Database startup migration failed current_version=%s target_version=%s",
                self._schema_version_before,
                SCHEMA_VERSION,
            )
            await self.conn.close()
            self.conn = None
            raise DatabaseIntegrityError("SQLite database operation failed") from exc
        except Exception:
            logger.exception(
                "Database startup migration failed current_version=%s target_version=%s",
                self._schema_version_before,
                SCHEMA_VERSION,
            )
            await self.conn.close()
            self.conn = None
            raise

    async def _init_schema(self):
        async with self._lock:
            self.last_backup_path = await prepare_database(
                self.conn,
                self.backup_dir,
                migrations=self.migrations,
                fault_hook=self.migration_fault_hook,
                version_observer=lambda version: setattr(
                    self, "_schema_version_before", str(version)
                ),
            )

    async def close(self):
        if self.conn:
            await self.conn.close()

    # --- Meta ---

    async def get_meta(self, key, default=None):
        async with self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row else default

    async def set_meta(self, key, value):
        async with self._lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )
            await self.conn.commit()

    # --- Migration from config.json ---

    async def migrate_from_config(self, cfg):
        migrated = await self.get_meta("config_migrated", "0")
        if migrated == "1":
            return False

        async with self._lock:
            try:
                await self.conn.execute("BEGIN TRANSACTION")
                for auto in cfg.get("autoselects", []):
                    await self.conn.execute(
                        "INSERT OR REPLACE INTO autoselects (id, name, strategy, selected_node_ids, tag_filter, enabled) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            auto.get("id", ""),
                            auto.get("name", ""),
                            auto.get("strategy", "leastPing"),
                            json.dumps(auto.get("selected_node_ids", []), ensure_ascii=False),
                            json.dumps(auto.get("tag_filter", []), ensure_ascii=False),
                            1 if auto.get("enabled", True) else 0,
                        ),
                    )
    
                for group_name, autoselect_ids in (cfg.get("group_rules") or {}).items():
                    for as_id in autoselect_ids:
                        await self.conn.execute(
                            "INSERT OR IGNORE INTO group_rules (group_name, autoselect_id) VALUES (?, ?)",
                            (str(group_name), str(as_id)),
                        )
    
                for node in cfg.get("node_catalog", []):
                    fp = node.get("id") or node.get("fingerprint") or ""
                    if not fp:
                        continue
                    cid = node.get("canonical_id") or node.get("canonicalId") or ""
                    await self.conn.execute(
                        "INSERT OR REPLACE INTO node_catalog (fingerprint, canonical_id, name, protocol, address, port, network, security, tag) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            fp,
                            cid,
                            node.get("name", ""),
                            node.get("protocol", ""),
                            node.get("address", ""),
                            str(node.get("port", "") or ""),
                            node.get("network", ""),
                            node.get("security", ""),
                            node.get("tag", ""),
                        ),
                    )
    
                for key, groups in (cfg.get("client_group_overrides") or {}).items():
                    if isinstance(groups, list):
                        groups = ",".join(groups)
                    await self.conn.execute(
                        "INSERT OR REPLACE INTO client_group_overrides (key, groups) VALUES (?, ?)",
                        (str(key), str(groups)),
                    )
    
                await self.conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    ("config_migrated", "1"),
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        return True

    # --- Client Groups ---

    async def get_client_groups(self, sub_id):
        async with self.conn.execute("SELECT groups FROM client_groups WHERE sub_id = ?", (sub_id,)) as cursor:
            row = await cursor.fetchone()
        if row and row["groups"]:
            return [g.strip() for g in row["groups"].split(",") if g.strip()]
        return []

    async def get_client_info(self, sub_id):
        async with self.conn.execute("SELECT sub_id, email, groups FROM client_groups WHERE sub_id = ?", (sub_id,)) as cursor:
            row = await cursor.fetchone()
        return row

    async def get_client_email(self, sub_id):
        row = await self.get_client_info(sub_id)
        return row["email"] if row else ""

    async def set_client_groups(self, sub_id, email, groups_str):
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO client_groups (sub_id, email, groups, updated_at) VALUES (?, ?, ?, ?)",
                (sub_id, email, groups_str, now),
            )
            await self.conn.commit()

    async def delete_client_groups(self, sub_id):
        async with self._lock:
            await self.conn.execute("DELETE FROM client_groups WHERE sub_id = ?", (sub_id,))
            await self.conn.commit()

    async def get_all_client_groups(self):
        async with self.conn.execute("SELECT sub_id, email, groups FROM client_groups ORDER BY sub_id") as cursor:
            rows = await cursor.fetchall()
        return rows

    # --- Node Catalog ---

    async def get_node_catalog(self):
        async with self.conn.execute("SELECT fingerprint, canonical_id, name, protocol, address, port, network, security, tag FROM node_catalog ORDER BY name") as cursor:
            rows = await cursor.fetchall()
        return rows

    async def get_canonical_id_map(self):
        async with self.conn.execute("SELECT fingerprint, canonical_id FROM node_catalog WHERE canonical_id != ''") as cursor:
            rows = await cursor.fetchall()
        return {r["fingerprint"]: r["canonical_id"] for r in rows}

    async def set_node_catalog(self, nodes):
        async with self._lock:
            try:
                await self.conn.execute("BEGIN TRANSACTION")
                await self.conn.execute("DELETE FROM node_catalog")
                for node in nodes:
                    fp = node.get("fingerprint") or node.get("id") or ""
                    cid = node.get("canonical_id") or ""
                    await self.conn.execute(
                        "INSERT INTO node_catalog (fingerprint, canonical_id, name, protocol, address, port, network, security, tag) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            fp,
                            cid,
                            node.get("name", ""),
                            node.get("protocol", ""),
                            node.get("address", ""),
                            str(node.get("port", "") or ""),
                            node.get("network", ""),
                            node.get("security", ""),
                            node.get("tag", ""),
                        ),
                    )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    # --- Autoselects ---

    async def get_autoselects(self):
        async with self.conn.execute("SELECT id, name, strategy, selected_node_ids, tag_filter, enabled FROM autoselects ORDER BY rowid") as cursor:
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            sel = row["selected_node_ids"]
            try:
                selected = json.loads(sel) if isinstance(sel, str) else (sel or [])
            except Exception:
                selected = []
            tf = row["tag_filter"]
            try:
                tag_filter = json.loads(tf) if isinstance(tf, str) else (tf or [])
            except Exception:
                tag_filter = []
            result.append({
                "id": row["id"],
                "name": row["name"],
                "strategy": row["strategy"],
                "selected_node_ids": selected,
                "tag_filter": tag_filter,
                "enabled": bool(row["enabled"]),
            })
        return result

    async def add_autoselect(self, autoselect_id, name, strategy="leastPing", selected_node_ids=None, tag_filter=None, enabled=1):
        if selected_node_ids is None:
            selected_node_ids = ["*"]
        if tag_filter is None:
            tag_filter = []
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO autoselects (id, name, strategy, selected_node_ids, tag_filter, enabled) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    autoselect_id,
                    name,
                    normalize_autoselect_strategy(strategy),
                    json.dumps(selected_node_ids, ensure_ascii=False),
                    json.dumps(tag_filter, ensure_ascii=False),
                    1 if enabled else 0,
                ),
            )
            await self.conn.commit()

    async def delete_autoselect(self, autoselect_id):
        async with self._lock:
            await self.conn.execute("DELETE FROM autoselects WHERE id = ?", (autoselect_id,))
            await self.conn.execute("DELETE FROM group_rules WHERE autoselect_id = ?", (autoselect_id,))
            await self.conn.commit()

    async def update_autoselect(self, autoselect_id, selected_node_ids=None, tag_filter=None, name=None, enabled=None, strategy=None):
        async with self._lock:
            updates = []
            params = []
            if name is not None:
                updates.append("name = ?")
                params.append(name)
            if selected_node_ids is not None:
                updates.append("selected_node_ids = ?")
                params.append(json.dumps(selected_node_ids, ensure_ascii=False))
            if tag_filter is not None:
                updates.append("tag_filter = ?")
                params.append(json.dumps(tag_filter, ensure_ascii=False))
            if enabled is not None:
                updates.append("enabled = ?")
                params.append(1 if enabled else 0)
            if strategy is not None:
                updates.append("strategy = ?")
                params.append(normalize_autoselect_strategy(strategy))
            if updates:
                params.append(autoselect_id)
                query = f"UPDATE autoselects SET {', '.join(updates)} WHERE id = ?"
                await self.conn.execute(query, tuple(params))
                await self.conn.commit()

    # --- Security Rules (Hide Settings & Encryption) ---

    async def get_security_rules(self):
        raw = await self.get_meta("security_rules", "{}")
        try:
            val = json.loads(raw)
            if isinstance(val, dict):
                return val
        except Exception:
            pass
        return {"hide_settings_groups": ["*"]}

    async def set_security_rules(self, rules_dict):
        await self.set_meta("security_rules", json.dumps(rules_dict, ensure_ascii=False))

    # --- Group Rules ---

    async def get_group_rules(self):
        async with self.conn.execute("SELECT group_name, autoselect_id FROM group_rules ORDER BY group_name") as cursor:
            rows = await cursor.fetchall()
        rules = {}
        for row in rows:
            g = row["group_name"]
            if g not in rules:
                rules[g] = []
            rules[g].append(row["autoselect_id"])
        return rules

    async def set_group_rules(self, rules_dict):
        async with self._lock:
            await self.conn.execute("DELETE FROM group_rules")
            for group_name, autoselect_ids in rules_dict.items():
                for as_id in autoselect_ids:
                    await self.conn.execute(
                        "INSERT INTO group_rules (group_name, autoselect_id) VALUES (?, ?)",
                        (str(group_name), str(as_id)),
                    )
            await self.conn.commit()

    # --- Client Group Overrides ---

    async def get_client_group_overrides(self):
        async with self.conn.execute("SELECT key, groups FROM client_group_overrides") as cursor:
            rows = await cursor.fetchall()
        result = {}
        for row in rows:
            parts = [g.strip() for g in row["groups"].split(",") if g.strip()]
            if parts:
                result[row["key"]] = parts
        return result

    async def set_client_group_overrides(self, overrides_dict):
        async with self._lock:
            await self.conn.execute("DELETE FROM client_group_overrides")
            for key, groups in overrides_dict.items():
                if isinstance(groups, list):
                    groups = ",".join(groups)
                await self.conn.execute(
                    "INSERT INTO client_group_overrides (key, groups) VALUES (?, ?)",
                    (str(key), str(groups)),
                )
            await self.conn.commit()

    # --- Probe Config ---

    async def get_probe_config(self):
        probe_url = await self.get_meta("probe_url", "http://cp.cloudflare.com/generate_204")
        probe_interval = await self.get_meta("probe_interval", "60s")
        return probe_url, probe_interval

    async def set_probe_config(self, probe_url, probe_interval):
        async with self._lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("probe_url", probe_url),
            )
            await self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("probe_interval", probe_interval),
            )
            await self.conn.commit()
