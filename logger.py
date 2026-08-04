import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os

from logging_utils import RequestContextRedactionFilter

# Define log path directly to avoid circular dependency with config.py
DEFAULT_APP_DIR = Path("/opt/autosub-server")
if not DEFAULT_APP_DIR.exists():
    DEFAULT_APP_DIR = Path(__file__).parent.resolve()

APP_DIR = Path(os.environ.get("AUTOSUB_APP_DIR", str(DEFAULT_APP_DIR)))
LOG_PATH = Path(os.environ.get("AUTOSUB_LOG", str(APP_DIR / "autosub.log")))

LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3


def setup_logger():
    # Ensure directory exists
    APP_DIR.mkdir(parents=True, exist_ok=True)

    _logger = logging.getLogger("autosub")
    _logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s [request_id=%(request_id)s]: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    if not any(
        isinstance(item, RequestContextRedactionFilter) for item in _logger.filters
    ):
        _logger.addFilter(RequestContextRedactionFilter())

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
