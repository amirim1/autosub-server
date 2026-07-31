import copy
import json
import time

from api_client import get_xui_api, fetch_original_subscription, normalize_subscription
from config import env_get
from fingerprint import (
    canonical_node_id,
    extract_proxy_outbound,
    legacy_profile_node_id,
    node_name,
    node_summary,
    profile_node_id,
    unique_tag,
)
from logger import log


def allowed_autoselect_ids(groups, group_rules):
    normalized_rules = {str(k).lower(): v for k, v in group_rules.items()}
    ids = []
    for group in groups or []:
        values = group_rules.get(group, normalized_rules.get(str(group).lower(), []))
        if isinstance(values, str):
            values = [v.strip() for v in values.split(",") if v.strip()]
        for value in values:
            if value not in ids:
                ids.append(value)
    return ids


def build_autoselect_profile(template_profile, selected_profiles, autoselect, probe_url, probe_interval):
    selected_outbounds = []
    tags = []
    used = set()
    for idx, profile in enumerate(selected_profiles):
        outbound = profile.get("_outbound")
        if not outbound:
            continue
        tag = unique_tag(node_name(profile, idx), used)
        outbound["tag"] = tag
        selected_outbounds.append(outbound)
        tags.append(tag)
    if not selected_outbounds:
        return None

    name = autoselect.get("name") or "Авто"
    result = copy.deepcopy(template_profile)
    for key in ("remarks", "remark", "ps", "name"):
        result[key] = name

    result["outbounds"] = selected_outbounds + [_direct_outbound(), _block_outbound()]
    result["observatory"] = {
        "subjectSelector": tags,
        "probeUrl": probe_url,
        "probeInterval": probe_interval,
        "enableConcurrency": True,
    }
    result["routing"] = {
        "domainMatcher": "hybrid",
        "domainStrategy": "IPIfNonMatch",
        "rules": [
            {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
            {"type": "field", "network": "tcp,udp", "balancerTag": name},
        ],
        "balancers": [
            {
                "tag": name,
                "selector": tags[:],
                "fallbackTag": tags[0] if tags else "",
                "strategy": {
                    "type": "leastPing",
                    "settings": {
                        "expected": 1,
                        "maxRTT": "2500ms",
                        "tolerance": 0.2,
                    },
                },
            }
        ],
    }
    return result


def _direct_outbound():
    return {"protocol": "freedom", "settings": {"domainStrategy": "AsIs"}, "tag": "direct"}


def _block_outbound():
    return {"protocol": "blackhole", "settings": {"response": {"type": "http"}}, "tag": "block"}


def _extract_port(profile, outbound):
    port = None
    if outbound:
        settings = outbound.get("settings", {})
        port = settings.get("port")
        if not port:
            for container in (settings.get("vnext") or []):
                port = container.get("port")
                if port:
                    break
        if not port:
            for container in (settings.get("servers") or []):
                port = container.get("port")
                if port:
                    break
    if not port:
        port = profile.get("port")
    return port


def enrich_profiles(profiles):
    """Enrich profiles with extracted outbound, node IDs, and tags. Public API."""
    result = []
    for i, profile in enumerate(profiles):
        p = copy.deepcopy(profile)
        p["_outbound"] = extract_proxy_outbound(profile)
        p["_node_id"] = profile_node_id(profile)
        p["_canonical_id"] = canonical_node_id(profile)
        p["_legacy_id"] = legacy_profile_node_id(profile)
        p["_tag"] = node_name(profile, i)
        result.append(p)
    return result


def match_profiles(profiles, selected_node_ids, tag_filter=None):
    """Match profiles against selected node IDs and optional tag filter. Public API."""
    if tag_filter:
        tf = set(t.strip().lower() for t in tag_filter if t.strip())
        if tf and "*" not in tf:
            profiles = [p for p in profiles if str(p.get("_tag", "")).lower() in tf]

    selected = set(selected_node_ids or [])
    if "*" in selected:
        return profiles[:]

    matched = []
    for p in profiles:
        if (p.get("_node_id") in selected or
            p.get("_canonical_id") in selected or
            p.get("_legacy_id") in selected):
            matched.append(p)
    return matched


def _sub_headers(resp_headers):
    h = {}
    keys = (
        "Subscription-Userinfo", "Profile-Title", "Content-Disposition", 
        "Profile-Update-Interval", "Profile-Web-Page-Url",
        "Announce", "Hide-Settings", "Routing", "Routing-Enable"
    )
    for key in keys:
        val = resp_headers.get(key)
        if val is not None:
            h[key] = val
    return h


async def build_for_subscription(sub_id, storage, query=""):
    cfg_probe_url, cfg_probe_interval = await storage.get_probe_config()

    try:
        original_text, content_type, resp_headers = await fetch_original_subscription(sub_id, query=query)
    except Exception as exc:
        log(f"{sub_id}: failed to fetch upstream subscription: {exc}")
        raise RuntimeError(f"Failed to fetch upstream subscription for {sub_id}: {exc}") from exc

    try:
        profiles = normalize_subscription(original_text)
    except Exception as exc:
        log(f"{sub_id}: failed to parse upstream subscription: {exc}")
        raise RuntimeError(f"Failed to parse upstream subscription for {sub_id}: {exc}") from exc

    sub_title_env = env_get("SUB_TITLE", "")
    sub_userinfo_env = env_get("SUB_USERINFO", "")

    sub_headers = _sub_headers(resp_headers)
    client = await resolve_client(sub_id, storage)

    if sub_title_env:
        sub_headers["Profile-Title"] = sub_title_env
    elif "Profile-Title" not in sub_headers:
        if client and client.get("email"):
            sub_headers["Profile-Title"] = client.get("email")
        else:
            sub_headers["Profile-Title"] = f"AutoSub ({sub_id[:8]})"

    if sub_userinfo_env:
        sub_headers["Subscription-Userinfo"] = sub_userinfo_env

    if not client:
        log(f"{sub_id}: client not found, passthrough original")
        return original_text, content_type, sub_headers

    groups = client.get("groups") or []
    log(f"{sub_id}: resolved client email={client.get('email')} groups={groups} source={client.get('source')}")

    if not groups:
        log(f"{sub_id}: client has no groups, passthrough original")
        return original_text, content_type, sub_headers

    group_rules = await storage.get_group_rules()
    allowed_ids = allowed_autoselect_ids(groups, group_rules)
    log(f"{sub_id}: groups={groups} rules_keys={list(group_rules.keys())} allowed_ids={allowed_ids}")
    if not allowed_ids:
        log(f"{sub_id}: allowed_ids empty, passthrough")
        return original_text, content_type, sub_headers

    autoselects = await storage.get_autoselects()
    log(f"{sub_id}: autoselects in db: {[(a['id'], a['selected_node_ids']) for a in autoselects]}")
    by_id = {a.get("id"): a for a in autoselects if a.get("enabled", True)}

    enriched = enrich_profiles(profiles)
    template = enriched[0] if enriched else profiles[0]

    # Extract announcement/dummy nodes from the top of the list
    dummy_nodes = []
    remaining_profiles = []
    remaining_enriched = []
    
    def _is_dummy(p):
        port = str(p.get("port", ""))
        addr = str(p.get("add", "") or p.get("address", ""))
        
        # Check inside outbound if available (for Xray JSON)
        outbound = p.get("_outbound", {})
        if outbound:
            settings = outbound.get("settings", {})
            if isinstance(settings, dict):
                # Check vnext (vless/vmess)
                for vnext in settings.get("vnext", []):
                    if str(vnext.get("port", "")) in ("0", "1", "80") and str(vnext.get("address", "")) in ("127.0.0.1", "8.8.8.8", "1.1.1.1", ""):
                        return True
                # Check direct address/port
                if str(settings.get("port", "")) in ("0", "1", "80") and str(settings.get("address", "")) in ("127.0.0.1", "8.8.8.8", "1.1.1.1", ""):
                    return True
                    
        return (port in ("0", "1", "80") and addr in ("127.0.0.1", "8.8.8.8", "1.1.1.1", "")) or "announce" in addr.lower()
        
    for p, e in zip(profiles, enriched):
        if _is_dummy(p) or _is_dummy(e):
            dummy_nodes.append(p)
        else:
            remaining_profiles.append(p)
            remaining_enriched.append(e)

    auto_profiles = []
    for auto_id in allowed_ids:
        auto = by_id.get(auto_id)
        if not auto:
            log(f"{sub_id}: autoselect '{auto_id}' not found or disabled in by_id (ids={list(by_id.keys())})")
            continue
        sel = auto.get("selected_node_ids") or []
        tag_filter = auto.get("tag_filter") or []
        matched = match_profiles(remaining_enriched, sel, tag_filter)
        log(f"{sub_id}: autoselect '{auto_id}' selected={sel} tag_filter={tag_filter} matched={len(matched)} profiles")
        if not matched:
            tags_in_pool = set(str(p.get("_tag", "")) for p in remaining_enriched)
            log(f"{sub_id}: pool tags={tags_in_pool}")
            continue
        generated = build_autoselect_profile(template, matched, auto, cfg_probe_url, cfg_probe_interval)
        if generated:
            auto_profiles.append(generated)
        else:
            log(f"{sub_id}: autoselect '{auto_id}' build_autoselect_profile returned None")

    if not auto_profiles:
        log(f"{sub_id}: no autoselects matched (allowed_ids={allowed_ids} autoselects={len(autoselects)})")
        return original_text, content_type, sub_headers

    output = dummy_nodes + auto_profiles + remaining_profiles
    
    # Clean internal fields and inject address/port for v2rayNG/Happ ping
    def _extract_addr_port(obj):
        if not isinstance(obj, dict):
            return None, None
        addr = obj.get("address") or obj.get("add")
        port = obj.get("port")
        if addr and port:
            return addr, port

        settings = obj.get("settings")
        if isinstance(settings, dict):
            addr = settings.get("address") or settings.get("add")
            port = settings.get("port")
            if addr and port:
                return addr, port
            vnext = settings.get("vnext")
            if isinstance(vnext, list) and vnext and isinstance(vnext[0], dict):
                addr = vnext[0].get("address") or vnext[0].get("add")
                port = vnext[0].get("port")
                if addr and port:
                    return addr, port
            servers = settings.get("servers")
            if isinstance(servers, list) and servers and isinstance(servers[0], dict):
                addr = servers[0].get("address") or servers[0].get("add")
                port = servers[0].get("port")
                if addr and port:
                    return addr, port

        outbounds = obj.get("outbounds")
        if isinstance(outbounds, list) and outbounds and isinstance(outbounds[0], dict):
            return _extract_addr_port(outbounds[0])

        return None, None

    def _clean(p):
        if not isinstance(p, dict):
            return p
        cleaned = {k: v for k, v in p.items() if not str(k).startswith("_")}
        addr, port = _extract_addr_port(cleaned)
        if addr and port:
            cleaned["address"] = addr
            cleaned["add"] = addr
            cleaned["port"] = port
            if "settings" in cleaned and isinstance(cleaned["settings"], dict):
                cleaned["settings"]["address"] = addr
                cleaned["settings"]["port"] = port
        return cleaned

    cleaned_output = [_clean(p) for p in output]

    email = client.get("email") or "client"
    log(f"{sub_id}: generated {len(auto_profiles)} autoselects for {email}")
    return json.dumps(cleaned_output, ensure_ascii=False, separators=(",", ":")), "application/json; charset=utf-8", sub_headers


async def resolve_client(sub_id, storage):
    """
    Resolve client with groups. Public API.
    Priority:
    1. Local SQLite client_groups (manually assigned)
    2. Local SQLite client_group_overrides (by sub_id or email)
    3. 3x-ui API fallback
    """
    local_groups = await storage.get_client_groups(sub_id)
    if local_groups:
        client_email = await storage.get_client_email(sub_id)
        return {"email": client_email or "", "sub_id": sub_id, "groups": local_groups, "source": "local"}

    overrides = await storage.get_client_group_overrides()
    for key in (sub_id,):
        if key in overrides:
            return {"email": key, "sub_id": sub_id, "groups": overrides[key], "source": "override"}

    try:
        api = get_xui_api()
        client = await api.find_client_by_sub_id(sub_id)
        if client:
            email = client.get("email") or ""
            if email in overrides:
                merged = list(dict.fromkeys(client.get("groups", []) + overrides[email]))
                client["groups"] = merged
                client["source"] = "api+override"
            else:
                client["source"] = "api"
            return client
    except Exception as exc:
        log(f"{sub_id}: API fallback failed: {exc}")

    if sub_id in overrides:
        return {"email": sub_id, "sub_id": sub_id, "groups": overrides[sub_id], "source": "override"}

    return None


async def discover_nodes_from_sub_id(sub_id):
    text, _, _ = await fetch_original_subscription(sub_id)
    profiles = normalize_subscription(text)
    result = []
    for i, profile in enumerate(profiles):
        summary = node_summary(profile, i)
        summary["tag"] = node_name(profile, i)
        result.append(summary)
    return result
