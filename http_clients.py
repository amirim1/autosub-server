import asyncio
import hashlib
import ssl
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from http_client_config import (
    CLIENT_LIMITS,
    MAX_PANEL_CLIENTS,
    PANEL_TIMEOUT,
    PUBLIC_TIMEOUT,
    PanelConfig,
    tls_verify_enabled,
)
from http_client_errors import (
    HttpClientNotStartedError,
    UpstreamConnectionError,
    UpstreamResponseTooLargeError,
    UpstreamTimeoutError,
    UpstreamTlsError,
)
from logger import logger


@dataclass
class _PanelEntry:
    key: str
    api: object
    client: httpx.AsyncClient
    active: int = 0
    retired: bool = False


class HttpClientManager:
    def __init__(
        self,
        *,
        env_getter,
        client_factory=httpx.AsyncClient,
        transport=None,
        max_panel_clients=MAX_PANEL_CLIENTS,
    ):
        self._env_get = env_getter
        self._client_factory = client_factory
        self._transport = transport
        self._max_panel_clients = max_panel_clients
        self._public_client = None
        self._panels = OrderedDict()
        self._default_panel_key = None
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False

    def _new_client(self, *, verify, timeout):
        kwargs = {
            "verify": verify,
            "timeout": timeout,
            "limits": CLIENT_LIMITS,
            "follow_redirects": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return self._client_factory(**kwargs)

    def current_panel_config(self):
        return PanelConfig(
            str(self._env_get("XUI_API_URL", self._env_get("XUI_URL", "")) or ""),
            str(self._env_get("XUI_USERNAME", "") or ""),
            str(self._env_get("XUI_PASSWORD", "") or ""),
            str(self._env_get("XUI_API_TOKEN", "") or ""),
            tls_verify_enabled(self._env_get("XUI_TLS_VERIFY", "true")),
        )

    async def start(self):
        async with self._lock:
            if self._started:
                return
            if self._closed:
                raise HttpClientNotStartedError("HTTP client manager is closed")
            try:
                self._public_client = self._new_client(verify=True, timeout=PUBLIC_TIMEOUT)
                self._started = True
                config = self.current_panel_config()
                if config.enabled:
                    entry = await self._create_panel_entry(config.normalized())
                    self._default_panel_key = entry.key
            except Exception:
                clients = self._take_all_clients()
                self._started = False
                self._closed = True
                for client in clients:
                    await client.aclose()
                raise

    def _panel_keys(self, config):
        credential = hashlib.sha256(
            f"{config.username}\0{config.password}\0{config.api_token}".encode()
        ).hexdigest()
        key = hashlib.sha256(
            f"{config.base_url}\0{config.username}\0{config.verify}\0{credential}".encode()
        ).hexdigest()
        return key

    async def _create_panel_entry(self, config):
        from api_client import XuiApi

        key = self._panel_keys(config)
        entry = self._panels.get(key)
        if entry is not None:
            return entry
        if not config.verify:
            logger.warning("TLS verification is disabled for one panel session")
        client = self._new_client(verify=config.verify, timeout=PANEL_TIMEOUT)
        api = XuiApi(config, client, self.request_with_client)
        entry = _PanelEntry(key, api, client)
        self._panels[key] = entry
        return entry

    def _take_all_clients(self):
        clients = [entry.client for entry in self._panels.values()]
        if self._public_client is not None:
            clients.append(self._public_client)
        self._panels.clear()
        self._public_client = None
        self._default_panel_key = None
        return clients

    async def close(self):
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            clients = self._take_all_clients()
        for client in clients:
            if not client.is_closed:
                await client.aclose()

    async def _retire_default(self, new_key):
        closing = []
        if self._default_panel_key and self._default_panel_key != new_key:
            old = self._panels.get(self._default_panel_key)
            if old is not None:
                old.retired = True
                if old.active == 0:
                    self._panels.pop(old.key, None)
                    closing.append(old.client)
        self._default_panel_key = new_key
        return closing

    def _evict_inactive(self, protected_key=None):
        closing = []
        while len(self._panels) > self._max_panel_clients:
            victim = next(
                (
                    entry
                    for entry in self._panels.values()
                    if entry.active == 0 and entry.key != protected_key
                ),
                None,
            )
            if victim is None:
                break
            self._panels.pop(victim.key, None)
            closing.append(victim.client)
        return closing

    @asynccontextmanager
    async def panel_api(self, config=None):
        is_default = config is None
        config = self.current_panel_config() if is_default else config
        if not config.enabled:
            if is_default:
                async with self._lock:
                    self._require_started()
                    closing = await self._retire_default(None)
                for client in closing:
                    await client.aclose()
            yield None
            return
        normalized = config.normalized()
        async with self._lock:
            self._require_started()
            entry = await self._create_panel_entry(normalized)
            closing = await self._retire_default(entry.key) if is_default else []
            entry.active += 1
            self._panels.move_to_end(entry.key)
            closing.extend(self._evict_inactive(entry.key))
        for client in closing:
            await client.aclose()
        try:
            yield entry.api
        finally:
            async with self._lock:
                entry.active -= 1
                closing = []
                if entry.retired and entry.active == 0:
                    self._panels.pop(entry.key, None)
                    closing.append(entry.client)
                closing.extend(self._evict_inactive())
            for client in closing:
                await client.aclose()

    def _require_started(self):
        if not self._started or self._closed:
            raise HttpClientNotStartedError("HTTP client manager is not running")

    async def request_public(self, method, url, *, max_bytes, headers=None, data=None):
        self._require_started()
        return await self.request_with_client(
            self._public_client,
            method,
            url,
            max_bytes=max_bytes,
            headers=headers,
            data=data,
        )

    async def request_with_client(
        self, client, method, url, *, max_bytes, headers=None, data=None
    ):
        self._require_started()
        try:
            async with client.stream(method, url, headers=headers, data=data) as response:
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise UpstreamResponseTooLargeError("upstream response is too large")
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise UpstreamResponseTooLargeError("upstream response is too large")
                    chunks.append(chunk)
                decoded_headers = [
                    (name, value)
                    for name, value in response.headers.multi_items()
                    if name.lower()
                    not in {"content-encoding", "content-length", "transfer-encoding"}
                ]
                return httpx.Response(
                    response.status_code,
                    headers=decoded_headers,
                    content=b"".join(chunks),
                    request=response.request,
                )
        except UpstreamResponseTooLargeError:
            logger.warning("Upstream response rejected because it exceeded the size limit")
            raise
        except httpx.TimeoutException as exc:
            logger.warning("Upstream request timed out error_type=%s", type(exc).__name__)
            raise UpstreamTimeoutError("upstream request timed out") from exc
        except httpx.ConnectError as exc:
            if isinstance(exc.__cause__, ssl.SSLError):
                logger.warning("Upstream TLS verification failed")
                raise UpstreamTlsError("upstream TLS verification failed") from exc
            logger.warning("Upstream connection failed")
            raise UpstreamConnectionError("upstream connection failed") from exc
        except httpx.TransportError as exc:
            logger.warning("Upstream transport failed error_type=%s", type(exc).__name__)
            raise UpstreamConnectionError("upstream transport failed") from exc

    @property
    def active_client_count(self):
        return (1 if self._public_client is not None else 0) + len(self._panels)

    @property
    def panel_client_count(self):
        return len(self._panels)
