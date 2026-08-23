import html
import json
import re
import urllib.parse
from pathlib import Path

from api_client import extract_client_groups, fetch_original_subscription, normalize_subscription
from builder import (
    allowed_autoselect_ids,
    enrich_profiles,
    resolve_client,
    discover_nodes_from_sub_id,
)
from fingerprint import node_name, profile_node_id, node_summary
from fastapi.templating import Jinja2Templates
from config import SUPPORTED_AUTOSELECT_STRATEGIES, normalize_direct_domains, VERSION
from logger import logger

templates_dir = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(templates_dir))


# --- Helpers ---

def esc(value):
    return html.escape(str(value or ""))


def page_head():
    return """<link rel="stylesheet" href="/static/dashboard.css">
<script src="/static/dashboard.js"></script>"""


async def api_clients_safe(client_manager=None, limit=500):
    """Fetch clients from 3x-ui API. Returns (clients_list, error_string)."""
    try:
        if client_manager is None:
            return [], "XUI credentials are not configured"
        async with client_manager.panel_api() as api:
            if api is None:
                return [], "XUI credentials are not configured"

            db_clients = []
            try:
                db_clients = await api.clients_list()
            except Exception:
                pass

            group_map = {}
            try:
                group_map = await api.group_map()
            except Exception:
                pass

            client_groups_cache = {}
            for c in db_clients:
                c_sub_id = c.get("subId") or c.get("sub_id") or c.get("subscriptionId") or c.get("subscription_id")
                c_email = c.get("email")
                raw_grps = extract_client_groups(c)
                expanded_grps = api._expand_group_names(raw_grps, group_map)
                if c_sub_id:
                    client_groups_cache[str(c_sub_id)] = expanded_grps
                if c_email:
                    client_groups_cache[str(c_email)] = expanded_grps

            clients = []
            for client in await api.clients_from_inbounds():
                sub_id = client.get("subId") or client.get("sub_id") or client.get("subscriptionId") or client.get("id") or ""
                email = client.get("email") or ""
                grps = client_groups_cache.get(str(sub_id)) or client_groups_cache.get(email) or extract_client_groups(client)
                clients.append({
                    "email": email,
                    "sub_id": sub_id,
                    "groups": grps,
                    "inbound": client.get("_inbound_remark") or "",
                })
            return clients[:limit], ""
    except Exception:
        logger.exception("Admin client list loading failed")
        return [], "API connection failed"


# --- Shared preview/debug logic ---

async def _resolve_preview_data(storage, sub_id, client_manager=None):
    """
    Common logic for render_preview and render_debug.
    Returns dict with: original profiles, enriched, client, groups, allowed_ids, autoselect results.
    """
    original_text, _, _ = await fetch_original_subscription(
        sub_id, client_manager=client_manager
    )
    profiles = normalize_subscription(original_text)
    client = await resolve_client(sub_id, storage, http_manager=client_manager)
    groups = (client or {}).get("groups") or []
    group_rules = await storage.get_group_rules()
    allowed = allowed_autoselect_ids(groups, group_rules)
    autoselects = await storage.get_autoselects()
    by_id = {a.get("id"): a for a in autoselects if a.get("enabled", True)}
    enriched = enrich_profiles(profiles)

    autos_result = []
    for auto_id in allowed:
        auto = by_id.get(auto_id)
        if not auto:
            autos_result.append({"id": auto_id, "exists": False, "name": None, "matched": 0, "selected": [], "tag_filter": []})
            continue
        selected = list(auto.get("selected_node_ids") or [])
        tag_filter = auto.get("tag_filter") or []
        pool = enriched
        if tag_filter:
            tf = set(t.strip().lower() for t in tag_filter if t.strip())
            if tf and "*" not in tf:
                pool = [p for p in pool if str(p.get("_tag", "")).lower() in tf]
        selected_set = set(selected)
        if "*" in selected_set:
            matched = len(pool)
        else:
            matched = sum(
                1 for p in pool
                if p.get("_node_id") in selected_set or p.get("_canonical_id") in selected_set or p.get("_legacy_id") in selected_set
            )
        autos_result.append({
            "id": auto_id,
            "exists": True,
            "name": auto.get("name"),
            "matched": matched,
            "selected": selected,
            "tag_filter": tag_filter,
        })

    return {
        "profiles": profiles,
        "enriched": enriched,
        "client": client,
        "groups": groups,
        "group_rules": group_rules,
        "allowed": allowed,
        "autos": autos_result,
    }


# --- Renderers ---

async def render_admin(request, storage, message="", csrf_token="", client_manager=None):
    clients, clients_error = await api_clients_safe(client_manager)

    catalog = await storage.get_node_catalog()
    overrides = await storage.get_client_group_overrides()
    local_groups_raw = await storage.get_all_client_groups()
    local_groups_map = {r.get('sub_id'): r.get('groups', []) for r in local_groups_raw if r.get('sub_id')}

    grouped_clients = {}
    if clients:
        for c in clients:
            key = c.get('sub_id') or c.get('email')
            if not key:
                continue
            if key not in grouped_clients:
                grouped_clients[key] = {
                    'email': c.get('email') or '',
                    'sub_id': c.get('sub_id') or '',
                    'groups': list(c.get('groups', [])),
                    'inbounds': [c.get('inbound')] if c.get('inbound') else []
                }
            else:
                if c.get('inbound') and c.get('inbound') not in grouped_clients[key]['inbounds']:
                    grouped_clients[key]['inbounds'].append(c.get('inbound'))
                for g in c.get('groups', []):
                    if g not in grouped_clients[key]['groups']:
                        grouped_clients[key]['groups'].append(g)

    # Merge overrides and local groups so the table matches what 'Preview' sees
    for key, c in grouped_clients.items():
        extra_groups = []
        if c['email'] in overrides: extra_groups.extend(overrides[c['email']])
        if c['sub_id'] in overrides: extra_groups.extend(overrides[c['sub_id']])
        if c['sub_id'] in local_groups_map: extra_groups.extend(local_groups_map[c['sub_id']])
        
        for g in extra_groups:
            if g not in c['groups']:
                c['groups'].append(g)

    local_groups = await storage.get_all_client_groups()
    autoselects = await storage.get_autoselects()
    group_rules = await storage.get_group_rules()
    security_rules = await storage.get_security_rules()

    rules_text = "\n".join(f"{group}={','.join(ids)}" for group, ids in group_rules.items())
    overrides_text = "\n".join(
        f"{key}={','.join(groups)}" for key, groups in overrides.items()
    )

    sec_hide_text = ",".join(security_rules.get("hide_settings_groups", ["*"]))

    probe_url, probe_interval = await storage.get_probe_config()
    direct_domains = await storage.get_direct_domains()
    try:
        sticky_domains = await storage.get_sticky_domains()
    except Exception:
        sticky_domains = []
    if not isinstance(sticky_domains, list):
        sticky_domains = []

    for auto in autoselects:
        auto["tag_filter_names"] = set(t.lower() for t in (auto.get("tag_filter") or []))
        auto["tag_filter_count"] = len(auto["tag_filter_names"])
        auto["country_scope"] = bool(auto.get("country_scope"))
        
    for n in catalog:
        n["tag_or_name"] = n.get("tag") or n.get("name") or ""
        n["tag_or_name_lower"] = n["tag_or_name"].strip().lower()

    if not message:
        message = request.query_params.get("msg", "")

    total_nodes = len(catalog)
    balancer_count = len(autoselects)
    client_count = len(grouped_clients) if grouped_clients else 0

    context = {
        "request": request,
        "message": message,
        "csrf_token": csrf_token,
        "probe_url": probe_url,
        "probe_interval": probe_interval,
        "direct_domains_text": "\n".join(direct_domains),
        "sticky_domains_text": "\n".join(sticky_domains),
        "rules_text": rules_text,
        "overrides_text": overrides_text,
        "sec_hide_text": sec_hide_text,
        "catalog": catalog,
        "grouped_clients": list(grouped_clients.values()) if grouped_clients else [],
        "local_groups": local_groups,
        "autos": autoselects,
        "clients_error": clients_error,
        "app_version": VERSION,
        "total_nodes": total_nodes,
        "balancer_count": balancer_count,
        "client_count": client_count,
    }
    return templates.TemplateResponse(request=request, name="admin.html", context=context)


async def render_preview(request, storage, sub_id, client_manager=None):
    data = await _resolve_preview_data(storage, sub_id, client_manager)
    client = data["client"]
    groups = data["groups"]
    enriched = data["enriched"]

    enriched_for_template = []
    for i, p in enumerate(enriched):
        enriched_for_template.append({
            "name": node_name(p, i),
            "profile_id": profile_node_id(p),
            "canonical_id": p.get("_canonical_id") or "",
            "tag": p.get("_tag") or ""
        })

    context = {
        "request": request,
        "sub_id": sub_id,
        "client": client,
        "groups": groups,
        "autos": data["autos"],
        "enriched": enriched_for_template,
    }
    return templates.TemplateResponse(request=request, name="preview.html", context=context)


async def render_api_test(client_manager=None):
    try:
        if client_manager is None:
            raise RuntimeError("HTTP client manager is unavailable")
        async with client_manager.panel_api() as api:
            if api is None:
                raise RuntimeError("XUI credentials are not configured")
            await api.login()
            inbounds = await api.inbounds()
            groups = await api.group_map()
            payload = {
                "ok": True,
                "message": "Connection successful",
                "csrf": bool(api.csrf_token),
                "cookie": bool(api.cookie_header),
                "inbounds": len(inbounds),
                "groups": groups,
            }
    except Exception:
        logger.exception("Admin API connection test failed")
        payload = {
            "ok": False,
            "error": "Connection failed",
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def render_debug(storage, sub_id, client_manager=None):
    data = await _resolve_preview_data(storage, sub_id, client_manager)
    client = data["client"]
    profiles = data["profiles"]
    enriched = data["enriched"]

    payload = {
        "sub_id": sub_id,
        "client": {
            "email": (client or {}).get("email"),
            "groups": data["groups"],
            "source": (client or {}).get("source"),
        } if client else None,
        "rules": data["group_rules"],
        "allowed_ids": data["allowed"],
        "profiles_count": len(profiles),
        "autos": data["autos"],
        "profile_ids": [
            {"name": node_name(profile, i), "id": profile_node_id(profile), "canonical_id": p.get("_canonical_id"), "legacy_id": p.get("_legacy_id"), "tag": p.get("_tag", "")}
            for i, (profile, p) in enumerate(zip(profiles, enriched))
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --- Form parsing ---

def parse_rules_text(text):
    if isinstance(text, list):
        text = text[0] if text else ""
    rules = {}
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        group, values = line.split("=", 1)
        ids = [x.strip() for x in values.split(",") if x.strip()]
        if group.strip():
            rules[group.strip()] = ids
    return rules


def parse_overrides_text(text):
    if isinstance(text, list):
        text = text[0] if text else ""
    overrides = {}
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, values = line.split("=", 1)
        groups = [x.strip() for x in re.split(r"[,;|]", values) if x.strip()]
        if key.strip():
            overrides[key.strip()] = groups
    return overrides


def parse_direct_domains_text(text):
    if isinstance(text, list):
        text = text[0] if text else ""
    values = []
    for raw_line in str(text).splitlines():
        value = raw_line.strip()
        if value and not value.startswith("#"):
            values.append(value)
    return normalize_direct_domains(values)


async def save_admin_form(storage, data):
    """Save admin form data to SQLite only (no more config.json dual-write)."""
    group_rules = parse_rules_text(data.get("group_rules", ""))
    direct_domains = None
    if "direct_domains" in data:
        direct_domains = parse_direct_domains_text(data.get("direct_domains", ""))
    await storage.set_group_rules(group_rules)

    overrides = parse_overrides_text(data.get("client_group_overrides", ""))
    await storage.set_client_group_overrides(overrides)

    hide_groups = data.get("security_hide_groups", "*")
    if isinstance(hide_groups, list):
        hide_groups = hide_groups[0]

    existing_security_rules = await storage.get_security_rules()
    sec_dict = dict(existing_security_rules) if isinstance(existing_security_rules, dict) else {}
    sec_dict["hide_settings_groups"] = [
        group.strip() for group in str(hide_groups).split(",") if group.strip()
    ]
    await storage.set_security_rules(sec_dict)

    probe_url = data.get("probe_url", "http://www.gstatic.com/generate_204")
    probe_interval = data.get("probe_interval", "60s")
    await storage.set_probe_config(probe_url, probe_interval)

    # Keep compatibility with forms opened before this field was introduced.
    if direct_domains is not None:
        await storage.set_direct_domains(direct_domains)

    if "sticky_domains" in data:
        sticky_domains = parse_direct_domains_text(data.get("sticky_domains", ""))
        await storage.set_sticky_domains(sticky_domains)

    autoselects = await storage.get_autoselects()
    for auto in autoselects:
        aid = auto.get("id")
        name = data.get(f"name_{aid}", auto.get("name"))
        if isinstance(name, list):
            name = name[0]
        name = str(name).strip() if name else auto.get("name")
        strategy = data.get(f"strategy_{aid}", auto.get("strategy", "leastPing"))
        if isinstance(strategy, list):
            strategy = strategy[0]
        if strategy not in SUPPORTED_AUTOSELECT_STRATEGIES:
            strategy = "leastPing"

        country_scope = f"country_scope_{aid}" in data

        mode = data.get(f"mode_{aid}", "")
        if isinstance(mode, str) and mode == "*":
            await storage.update_autoselect(aid, selected_node_ids=["*"], tag_filter=[], name=name, strategy=strategy, country_scope=country_scope)
        else:
            tag_key = f"tag_{aid}"
            tag_raw = data.get(tag_key, [])
            if isinstance(tag_raw, str):
                tag_raw = [tag_raw]
            tag_filter = [t.strip() for t in tag_raw if t.strip()]
            await storage.update_autoselect(aid, selected_node_ids=["*"], tag_filter=tag_filter, name=name, strategy=strategy, country_scope=country_scope)
