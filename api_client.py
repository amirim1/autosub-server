import asyncio
import copy
import json
import re
import time
import urllib.parse
import httpx

from config import env_get


def join_url(base, path):
    return base.rstrip("/") + "/" + path.lstrip("/")


_sub_cache = {}
_sub_cache_ttl = 30  # seconds
_sub_cache_lock = asyncio.Lock()


async def fetch_original_subscription(sub_id, query=""):
    cache_key = f"{sub_id}:{query}"
    now = time.time()
    
    async with _sub_cache_lock:
        if cache_key in _sub_cache:
            data, ctype, headers, expiry = _sub_cache[cache_key]
            if expiry >= now:
                return data, ctype, headers
            else:
                _sub_cache.pop(cache_key, None)

    xui_url = env_get("XUI_SUB_URL", env_get("XUI_URL", ""))
    if not xui_url:
        raise RuntimeError("XUI_SUB_URL is not configured in .env")
    path = f"/json/{urllib.parse.quote(sub_id, safe='')}"
    url = join_url(xui_url, path)
    if query:
        url += "?" + query
        
    verify = env_get("XUI_TLS_VERIFY", "true").lower() not in ("0", "false", "no", "off")
    async with httpx.AsyncClient(verify=verify, timeout=25.0) as client:
        resp = await client.get(url, headers={"User-Agent": "AutoSub/1.0"})
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        
        async with _sub_cache_lock:
            # cleanup expired while we're at it
            expired = [k for k, v in _sub_cache.items() if v[3] < time.time()]
            for k in expired:
                _sub_cache.pop(k, None)
            if len(_sub_cache) > 1000:
                _sub_cache.clear()
            _sub_cache[cache_key] = (resp.text, ctype, resp.headers, time.time() + _sub_cache_ttl)
            
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
    def __init__(self):
        self.base = env_get("XUI_API_URL", env_get("XUI_URL", ""))
        self.username = env_get("XUI_USERNAME")
        self.password = env_get("XUI_PASSWORD")
        self.api_token = env_get("XUI_API_TOKEN")
        self.cookie_header = ""
        self.csrf_token = ""
        self._inbounds_cache = None
        self._inbounds_cache_time = 0
        self._inbounds_ttl = 60
        self._clients_cache = None
        self._clients_cache_time = 0
        self._clients_ttl = 30
        self._lock = asyncio.Lock()
        
        verify = env_get("XUI_TLS_VERIFY", "true").lower() not in ("0", "false", "no", "off")
        self.client = httpx.AsyncClient(verify=verify, timeout=20.0)

    def enabled(self):
        return bool(self.base and (self.api_token or (self.username and self.password)))

    async def login(self):
        if not self.enabled():
            raise RuntimeError("XUI_API_TOKEN or XUI_USERNAME/XUI_PASSWORD are not configured")
        if self.api_token:
            return

        async with self._lock:
            if self.cookie_header:
                return # Already logged in by another concurrent task
                
            resp = await self.client.get(self.base, headers={"User-Agent": "AutoSub/1.0"})
            resp.raise_for_status()
            
            token_match = re.search(
                r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', resp.text
            )
            if token_match:
                self.csrf_token = token_match.group(1)

            cookies = resp.cookies
            self.cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
            
            url = join_url(self.base, "/login")
            login_headers = {
                "User-Agent": "AutoSub/1.0",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.base.rstrip("/") + "/",
            }
            if self.cookie_header:
                login_headers["Cookie"] = self.cookie_header
            if self.csrf_token:
                login_headers["X-CSRF-Token"] = self.csrf_token
                
            resp = await self.client.post(url, data={"username": self.username, "password": self.password}, headers=login_headers)
            resp.raise_for_status()
            
            for k, v in resp.cookies.items():
                cookies.set(k, v)
                
            self.cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
            if not self.cookie_header:
                raise RuntimeError("3x-ui login did not return session cookie")

    async def refresh(self):
        async with self._lock:
            self.cookie_header = ""
            self.csrf_token = ""
            self._inbounds_cache = None
            self._clients_cache = None
        await self.login()

    def _get_request_headers(self):
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base.rstrip("/") + "/",
            "User-Agent": "AutoSub/1.0",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        return headers

    async def get_json(self, path, retry=True):
        if not self.cookie_header and not self.api_token:
            await self.login()
        url = join_url(self.base, path)
        headers = self._get_request_headers()
        try:
            resp = await self.client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return unwrap_api_obj(data)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403 and retry and self.api_token:
                raise
            if exc.response.status_code in (401, 403) and retry and not self.api_token:
                await self.refresh()
                return await self.get_json(path, retry=False)
            raise

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


# --- Global singleton ---

_global_api = None


def get_xui_api():
    """Return a shared XuiApi instance. Reuses login session and caches across requests."""
    global _global_api
    if _global_api is None:
        _global_api = XuiApi()
    return _global_api
