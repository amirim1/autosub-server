import os
import copy
import json
from pathlib import Path
from logger import logger
from runtime_paths import get_autosub_root, get_release_dir, get_shared_dir


VERSION = "3.0.3"

AUTOSUB_ROOT = get_autosub_root()
APP_DIR = get_release_dir()
SHARED_DIR = get_shared_dir()
CONFIG_PATH = Path(os.environ.get("AUTOSUB_CONFIG", str(SHARED_DIR / "config.json")))
DB_PATH = Path(os.environ.get("AUTOSUB_DB", str(SHARED_DIR / "data.db")))
ENV_PATHS = [
    Path(os.environ.get("AUTOSUB_ENV", str(SHARED_DIR / ".env"))),
    Path(".env"),
]
LOG_PATH = Path(os.environ.get("AUTOSUB_LOG", str(SHARED_DIR / "autosub.log")))
BACKUP_DIR = Path(
    os.environ.get("AUTOSUB_BACKUP_DIR", str(SHARED_DIR / "backups"))
)

DEFAULT_CONFIG = {
    "dashboard_enabled": True,
    "probe_url": "http://www.gstatic.com/generate_204",
    "probe_interval": "60s",
    "group_rules": {
        "clients": ["stable"],
        "admins": ["stable", "all"],
    },
    "client_group_overrides": {},
    "node_catalog": [],
    "autoselects": [
        {
            "id": "stable",
            "name": "Основные авто",
            "strategy": "leastPing",
            "selected_node_ids": [],
            "enabled": True,
        },
        {
            "id": "all",
            "name": "Все ноды авто",
            "strategy": "leastPing",
            "selected_node_ids": ["*"],
            "enabled": True,
        },
    ],
}


class LegacyConfigError(ValueError):
    """Raised when an existing legacy config cannot be imported safely."""


def _require_string_list(value, field):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise LegacyConfigError(f"legacy config field {field} must be a list of strings")


def validate_legacy_config(data):
    """Validate only the legacy fields that are persisted into SQLite."""
    if not isinstance(data, dict):
        raise LegacyConfigError("legacy config must contain a JSON object")

    for field in ("probe_url", "probe_interval"):
        if field in data and not isinstance(data[field], str):
            raise LegacyConfigError(f"legacy config field {field} must be a string")

    group_rules = data.get("group_rules", {})
    if not isinstance(group_rules, dict):
        raise LegacyConfigError("legacy config field group_rules must be an object")
    for key, items in group_rules.items():
        if not isinstance(key, str):
            raise LegacyConfigError("legacy config field group_rules has a non-string key")
        _require_string_list(items, f"group_rules.{key}")

    overrides = data.get("client_group_overrides", {})
    if not isinstance(overrides, dict):
        raise LegacyConfigError("legacy config field client_group_overrides must be an object")
    for key, items in overrides.items():
        if not isinstance(key, str):
            raise LegacyConfigError(
                "legacy config field client_group_overrides has a non-string key"
            )
        if isinstance(items, str):
            continue
        _require_string_list(items, f"client_group_overrides.{key}")

    node_catalog = data.get("node_catalog", [])
    if not isinstance(node_catalog, list):
        raise LegacyConfigError("legacy config field node_catalog must be a list")
    seen_fingerprints = set()
    for index, node in enumerate(node_catalog):
        if not isinstance(node, dict):
            raise LegacyConfigError(f"legacy node {index} must be an object")
        fingerprint = node.get("id") or node.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise LegacyConfigError(f"legacy node {index} is missing an id")
        if fingerprint in seen_fingerprints:
            raise LegacyConfigError("legacy config contains duplicate node ids")
        seen_fingerprints.add(fingerprint)
        for field in (
            "canonical_id",
            "canonicalId",
            "name",
            "protocol",
            "address",
            "network",
            "security",
            "tag",
        ):
            if field in node and not isinstance(node[field], str):
                raise LegacyConfigError(f"legacy node {index} field {field} must be a string")
        if "port" in node and not isinstance(node["port"], (str, int)):
            raise LegacyConfigError(f"legacy node {index} field port must be a string or integer")

    autoselects = data.get("autoselects", [])
    if not isinstance(autoselects, list):
        raise LegacyConfigError("legacy config field autoselects must be a list")
    seen_ids = set()
    for index, auto in enumerate(autoselects):
        if not isinstance(auto, dict):
            raise LegacyConfigError(f"legacy autoselect {index} must be an object")
        autoselect_id = auto.get("id")
        if not isinstance(autoselect_id, str) or not autoselect_id.strip():
            raise LegacyConfigError(f"legacy autoselect {index} is missing an id")
        if autoselect_id in seen_ids:
            raise LegacyConfigError("legacy config contains duplicate autoselect ids")
        seen_ids.add(autoselect_id)
        if "name" in auto and not isinstance(auto["name"], str):
            raise LegacyConfigError(f"legacy autoselect {index} field name must be a string")
        if "strategy" in auto and auto["strategy"] not in {"leastPing", "leastLoad"}:
            raise LegacyConfigError(f"legacy autoselect {index} has an invalid strategy")
        for field in ("selected_node_ids", "tag_filter"):
            if field in auto:
                _require_string_list(auto[field], f"autoselects[{index}].{field}")
        if "enabled" in auto and not isinstance(auto["enabled"], bool):
            raise LegacyConfigError(f"legacy autoselect {index} field enabled must be boolean")

    return data


def ensure_app_dir():
    SHARED_DIR.mkdir(parents=True, exist_ok=True)


def load_dotenv():
    env = {}
    for path in ENV_PATHS:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            env[key.strip()] = value
    env.update(os.environ)
    return env


ENV = load_dotenv()


def env_get(key, default=""):
    return ENV.get(key, default)


def load_config():
    ensure_app_dir()
    if not CONFIG_PATH.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("legacy config read failed")
        raise LegacyConfigError("legacy config could not be parsed") from exc
    validate_legacy_config(data)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(data)
    validate_legacy_config(cfg)
    return cfg
