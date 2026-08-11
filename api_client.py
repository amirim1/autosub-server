import asyncio
import copy
import json
import re
import time
import urllib.parse

from config import env_get
from http_client_errors import (
    HttpClientNotStartedError,
    UpstreamAuthenticationError,
    UpstreamResponseError,
    UpstreamServerError,
)
from http_client_config import MAX_HTML_BYTES, MAX_JSON_BYTES, MAX_SUBSCRIPTION_BYTES


def join_url(base, path):
    return base.rstrip("/") + "/" + path.lstrip("/")


async def fetch_original_subscription(sub_id, query="", client_manager=None):
    xui_url = env_get("XUI_SUB_URL", env_get("XUI_URL", ""))
    if not xui_url:
        raise RuntimeError("XUI_SUB_URL is not configured in .env")
    path = f"/json/{urllib.parse.quote(sub_id, safe='')}"
    url = join_url(xui_url, path)
    if query:
        url += "?" + query
        
    if client_manager is None:
        raise HttpClientNotStartedError("managed HTTP client is required")
    resp = await client_manager.request_public(
        "GET",
        url,
        max_bytes=MAX_SUBSCRIPTION_BYTES,
        headers={"User-Agent": "AutoSub/1.0"},
    )
    if resp.status_code >= 500:
        raise UpstreamServerError("upstream subscription is temporarily unavailable")
    if not 200 <= resp.status_code < 300:
        raise UpstreamResponseError("upstream subscription returned an error")
    ctype = resp.headers.get("Content-Type", "application/octet-stream")
    return resp.text, ctype, resp.headers


def normalize_subscription(text):
    data = json.loads(text)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("profiles", "configs", "items", "data", "obj"):
            if isinstance(data.get(key), list):
                return data[key]
    raise ValueError("original subscription is not a JSON profile list")


def unwrap_api_obj(data):
    if isinstance(data, dict):
        if "obj" in data:
            return data["obj"]
        if "data" in data:
            return data["data"]
        if "result" in data:
            return data["result"]
    return data


def extract_client_groups(client):
    keys = ("group", "groups", "groupId", "groupIds", "clientGroup", "clientGroups", "group_name")
    groups = []
    for key in keys:
        groups.extend(_group_values(client.get(key)))
    
    # Filter out "0" if it was extracted as a string from an unassigned/default integer ID
    return [g for g in list(dict.fromkeys(groups)) if g != "0"]


def _group_values(value):
    result = []
    if value is None:
        return result
    if isinstance(value, str):
        return [x.strip() for x in re.split(r"[,;|]", value) if x.strip()]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                result.extend(_group_values(item.get("name") or item.get("title") or item.get("id")))
            else:
                result.extend(_group_values(item))
    if isinstance(value, dict):
        result.extend(_group_values(value.get("name") or value.get("title") or value.get("id")))
    return result


class XuiApi:
    def __init__(self, config, client, requester):
        self.base = config.base_url
        self.username = config.username
        self.password = config.password
        self.api_token = config.api_token
        self.client = client
        self._request = requester
        self.csrf_token = ""
        self._auth_generation = 0
        self._inbounds_cache = None
        self._inbounds_cache_time = 0
        self._inbounds_ttl = 60
        self._clients_cache = None
        self._clients_cache_time = 0
        self._clients_ttl = 30
        self._lock = asyncio.Lock()

    @property
    def cookie_header(self):
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.client.cookies.jar)

    def enabled(self):
        return bool(self.base and (self.api_token or (self.username and self.password)))

    async def login(self):
        if not self.enabled():
            raise RuntimeError("XUI_API_TOKEN or XUI_USERNAME/XUI_PASSWORD are not configured")
        if self.api_token:
            return

        async with self._lock:
            if self._auth_generation and self.cookie_header:
                return
            await self._login_locked()

    async def _login_locked(self):
        resp = await self._request(
            self.client,
            "GET",
            self.base,
            max_bytes=MAX_HTML_BYTES,
            headers={"User-Agent": "AutoSub/1.0"},
        )
        if not 200 <= resp.status_code < 300:
            raise UpstreamAuthenticationError("panel login page was rejected")
        token_match = re.search(
            r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
            resp.text,
        )
        self.csrf_token = token_match.group(1) if token_match else ""
        headers = {
            "User-Agent": "AutoSub/1.0",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base.rstrip("/") + "/",
        }
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        resp = await self._request(
            self.client,
            "POST",
            join_url(self.base, "/login"),
            max_bytes=MAX_JSON_BYTES,
            headers=headers,
            data={"username": self.username, "password": self.password},
        )
        if not 200 <= resp.status_code < 300 or not self.cookie_header:
            raise UpstreamAuthenticationError("panel authentication failed")
        self._auth_generation += 1

    async def _refresh_after_auth_failure(self, observed_generation):
        async with self._lock:
            if self._auth_generation != observed_generation and self.cookie_header:
                return
            self.client.cookies.clear()
            self.csrf_token = ""
            self._inbounds_cache = None
            self._clients_cache = None
            await self._login_locked()

    def _get_request_headers(self):
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base.rstrip("/") + "/",
            "User-Agent": "AutoSub/1.0",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        return headers

    async def get_json(self, path, retry=True):
        if not self.cookie_header and not self.api_token:
            await self.login()
        url = join_url(self.base, path)
        headers = self._get_request_headers()
        observed_generation = self._auth_generation
        resp = await self._request(
            self.client,
            "GET",
            url,
            max_bytes=MAX_JSON_BYTES,
            headers=headers,
        )
        if resp.status_code in (401, 403):
            if retry and not self.api_token:
                await self._refresh_after_auth_failure(observed_generation)
                return await self.get_json(path, retry=False)
            raise UpstreamAuthenticationError("panel authentication failed")
        if not 200 <= resp.status_code < 300:
            raise UpstreamResponseError("panel API returned an error")
        if "html" in resp.headers.get("content-type", "").lower():
            raise UpstreamResponseError("panel API returned an unexpected response")
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise UpstreamResponseError("panel API returned malformed JSON") from exc
        return unwrap_api_obj(data)

    async def inbounds(self):
        now = time.time()
        if self._inbounds_cache is not None and (now - self._inbounds_cache_time) < self._inbounds_ttl:
            return self._inbounds_cache
        data = await self.get_json("/panel/api/inbounds/list")
        result = data if isinstance(data, list) else []
        self._inbounds_cache = result
        self._inbounds_cache_time = now
        return result

    async def clients_list(self):
        now = time.time()
        if self._clients_cache is not None and (now - self._clients_cache_time) < self._clients_ttl:
            return self._clients_cache
        try:
            data = await self.get_json("/panel/api/clients/list")
        except Exception:
            data = []
        if not isinstance(data, list):
            data = []
        self._clients_cache = data
        self._clients_cache_time = now
        return data

    async def group_map(self):
        paths = [
            "/panel/api/clients/groups",
            "/panel/api/groups/list",
            "/panel/api/groups",
        ]
        for path in paths:
            try:
                data = await self.get_json(path)
            except Exception:
                continue
            if isinstance(data, dict):
                for key in ("groups", "items", "list", "data", "obj"):
                    if isinstance(data.get(key), list):
                        data = data[key]
                        break
            if not isinstance(data, list):
                continue
            result = {}
            for item in data:
                if not isinstance(item, dict):
                    continue
                gid = item.get("id", item.get("groupId", item.get("group_id")))
                name = item.get("name") or item.get("title") or item.get("remark")
                if gid is not None and name:
                    result[str(gid)] = str(name)
            if result:
                return result
        return {}

    async def clients_from_inbounds(self):
        clients = []
        inbounds = await self.inbounds()
        for inbound in inbounds:
            inbound_id = inbound.get("id")
            inbound_remark = inbound.get("remark") or inbound.get("tag") or f"inbound-{inbound_id}"
            settings = inbound.get("settings")
            parsed = {}
            if isinstance(settings, str) and settings.strip():
                try:
                    parsed = json.loads(settings)
                except Exception:
                    parsed = {}
            elif isinstance(settings, dict):
                parsed = settings
            raw_clients = parsed.get("clients") if isinstance(parsed, dict) else []
            if not isinstance(raw_clients, list):
                raw_clients = []
            for client in raw_clients:
                if not isinstance(client, dict):
                    continue
                item = copy.deepcopy(client)
                item["_inbound_id"] = inbound_id
                item["_inbound_remark"] = inbound_remark
                clients.append(item)
        return clients

    async def find_client_by_sub_id(self, sub_id):
        if not self.enabled():
            return None
        group_map = await self.group_map()

        clients = await self.clients_list()
        for client in clients:
            candidates = [
                client.get("subId"),
                client.get("sub_id"),
                client.get("subscriptionId"),
                client.get("subscription_id"),
            ]
            if sub_id in [str(x) for x in candidates if x is not None]:
                raw_groups = extract_client_groups(client)
                email = client.get("email") or ""
                if not email:
                    inbound_ids = client.get("inboundIds") or client.get("inbound_ids") or []
                    for iid in inbound_ids:
                        ib = await self._find_inbound_by_id(iid)
                        if ib:
                            clients_in = self._parse_inbound_clients(ib)
                            for c in clients_in:
                                if c.get("email") and c.get("subId") == sub_id:
                                    email = c.get("email", "")
                                    break
                        if email:
                            break
                return {
                    "email": email,
                    "sub_id": sub_id,
                    "groups": self._expand_group_names(raw_groups, group_map),
                    "raw": client,
                }

        ib_clients = await self.clients_from_inbounds()
        for client in ib_clients:
            candidates = [
                client.get("subId"),
                client.get("sub_id"),
                client.get("subscriptionId"),
                client.get("subscription_id"),
                client.get("id"),
            ]
            if sub_id in [str(x) for x in candidates if x is not None]:
                raw_groups = extract_client_groups(client)
                return {
                    "email": client.get("email") or client.get("name") or "",
                    "sub_id": sub_id,
                    "groups": self._expand_group_names(raw_groups, group_map),
                    "raw": client,
                }
        return None

    async def _find_inbound_by_id(self, inbound_id):
        inbounds = await self.inbounds()
        for ib in inbounds:
            if ib.get("id") == inbound_id:
                return ib
        return None

    def _parse_inbound_clients(self, inbound):
        settings = inbound.get("settings")
        parsed = {}
        if isinstance(settings, str) and settings.strip():
            try:
                parsed = json.loads(settings)
            except Exception:
                parsed = {}
        elif isinstance(settings, dict):
            parsed = settings
        raw = parsed.get("clients") if isinstance(parsed, dict) else []
        return raw if isinstance(raw, list) else []

    def _expand_group_names(self, groups, group_map):
        result = []
        for group in groups or []:
            text = str(group)
            result.append(text)
            mapped = group_map.get(text)
            if mapped:
                result.append(mapped)
        return list(dict.fromkeys(result))
