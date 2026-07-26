import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os

# Define log path directly to avoid circular dependency with config.py
APP_DIR = Path(os.environ.get("AUTOSUB_APP_DIR", "/opt/autosub-server"))
LOG_PATH = Path(os.environ.get("AUTOSUB_LOG", str(APP_DIR / "autosub.log")))

LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3


def setup_logger():
    # Ensure directory exists
    APP_DIR.mkdir(parents=True, exist_ok=True)

    _logger = logging.getLogger("autosub")
    _logger.setLevel(logging.INFO)

    formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    # File handler with rotation
    try:
        file_handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        _logger.addHandler(file_handler)
    except Exception as e:
        print(f"Failed to setup file logging: {e}")

    # Stream handler
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    _logger.addHandler(stream_handler)

    return _logger


logger = setup_logger()


def log(msg):
    """Convenience alias for logger.info(). Used by builder.py and others."""
    logger.info(msg)
