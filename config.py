import os
from pathlib import Path
from logger import logger


VERSION = "1.3.0"

DEFAULT_APP_DIR = Path("/opt/autosub-server")
if not DEFAULT_APP_DIR.exists():
    DEFAULT_APP_DIR = Path(__file__).parent.resolve()

APP_DIR = Path(os.environ.get("AUTOSUB_APP_DIR", str(DEFAULT_APP_DIR)))
CONFIG_PATH = Path(os.environ.get("AUTOSUB_CONFIG", str(APP_DIR / "config.json")))
DB_PATH = Path(os.environ.get("AUTOSUB_DB", str(APP_DIR / "data.db")))
ENV_PATHS = [
    Path(os.environ.get("AUTOSUB_ENV", str(APP_DIR / ".env"))),
    Path(".env"),
]
LOG_PATH = Path(os.environ.get("AUTOSUB_LOG", str(APP_DIR / "autosub.log")))

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


def ensure_app_dir():
    APP_DIR.mkdir(parents=True, exist_ok=True)


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
    import copy
    import json
    ensure_app_dir()
    if not CONFIG_PATH.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("config read failed, using defaults")
        data = {}
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(data if isinstance(data, dict) else {})
    cfg["autoselects"] = data.get("autoselects", cfg["autoselects"]) if isinstance(data, dict) else cfg["autoselects"]
    cfg["group_rules"] = data.get("group_rules", cfg["group_rules"]) if isinstance(data, dict) else cfg["group_rules"]
    cfg["client_group_overrides"] = data.get("client_group_overrides", cfg["client_group_overrides"]) if isinstance(data, dict) else cfg["client_group_overrides"]
    cfg["node_catalog"] = data.get("node_catalog", cfg["node_catalog"]) if isinstance(data, dict) else cfg["node_catalog"]
    return cfg
