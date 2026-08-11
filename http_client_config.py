from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from http_client_errors import UnsafePanelUrlError


CONNECT_TIMEOUT = 5.0
PANEL_READ_TIMEOUT = 20.0
PUBLIC_READ_TIMEOUT = 30.0
WRITE_TIMEOUT = 10.0
POOL_TIMEOUT = 5.0
MAX_CONNECTIONS = 20
MAX_KEEPALIVE_CONNECTIONS = 10
KEEPALIVE_EXPIRY = 30.0
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_SUBSCRIPTION_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 1024 * 1024
MAX_PANEL_CLIENTS = 16

PUBLIC_TIMEOUT = httpx.Timeout(
    connect=CONNECT_TIMEOUT,
    read=PUBLIC_READ_TIMEOUT,
    write=WRITE_TIMEOUT,
    pool=POOL_TIMEOUT,
)
PANEL_TIMEOUT = httpx.Timeout(
    connect=CONNECT_TIMEOUT,
    read=PANEL_READ_TIMEOUT,
    write=WRITE_TIMEOUT,
    pool=POOL_TIMEOUT,
)
CLIENT_LIMITS = httpx.Limits(
    max_connections=MAX_CONNECTIONS,
    max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
    keepalive_expiry=KEEPALIVE_EXPIRY,
)


def tls_verify_enabled(value):
    return str(value or "true").lower() not in ("0", "false", "no", "off")


def normalize_panel_base(value):
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafePanelUrlError("panel URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafePanelUrlError("panel URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise UnsafePanelUrlError("panel URL must not contain query or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafePanelUrlError("panel URL contains an invalid port") from exc
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    authority = host if port in (None, default_port) else f"{host}:{port}"
    path = "/" + parsed.path.strip("/") if parsed.path.strip("/") else ""
    return urlunsplit((parsed.scheme.lower(), authority, path, "", ""))


@dataclass(frozen=True)
class PanelConfig:
    base_url: str
    username: str = ""
    password: str = ""
    api_token: str = ""
    verify: bool = True

    @property
    def enabled(self):
        return bool(self.base_url and (self.api_token or (self.username and self.password)))

    def normalized(self):
        if not self.enabled:
            return self
        return PanelConfig(
            normalize_panel_base(self.base_url),
            self.username,
            self.password,
            self.api_token,
            self.verify,
        )
