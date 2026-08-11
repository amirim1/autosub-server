import os
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parent


def _detected_root() -> Path:
    configured = os.environ.get("AUTOSUB_ROOT")
    if configured:
        return Path(configured)
    if SOURCE_DIR.parent.name == "releases":
        return SOURCE_DIR.parent.parent
    return SOURCE_DIR


def get_autosub_root() -> Path:
    return _detected_root()


def get_release_dir() -> Path:
    return Path(os.environ.get("AUTOSUB_APP_DIR", str(SOURCE_DIR)))


def get_shared_dir() -> Path:
    configured = os.environ.get("AUTOSUB_SHARED_DIR")
    if configured:
        return Path(configured)
    root = get_autosub_root()
    if os.environ.get("AUTOSUB_ROOT") or SOURCE_DIR.parent.name == "releases":
        return root / "shared"
    return get_release_dir()
