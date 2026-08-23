import copy
import json
import time

from api_client import fetch_original_subscription, normalize_subscription
from config import DEFAULT_DIRECT_DOMAINS, env_get
from fingerprint import (
    canonical_node_id,
    extract_proxy_outbound,
    legacy_profile_node_id,
    node_name,
    node_summary,
    profile_node_id,
    unique_tag,
)
from generators import (
    WIRE_FORMATS,
    _block_outbound,
    _direct_outbound,
    build_clash_document,
    build_links_document,
    build_singbox_document,
    build_xray_profile,
    dumps_clash,
    encode_links_payload,
)
from logger import log
from logging_utils import fingerprint_secret, mask_email


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


def build_autoselect_profile(
    template_profile,
    selected_profiles,
    autoselect,
    probe_url,
    probe_interval,
    direct_domains=None,
    sticky_domains=None,
):
    """Backward-compatible wrapper around the Xray generator. Public API."""
    if direct_domains is None:
        direct_domains = DEFAULT_DIRECT_DOMAINS
    return build_xray_profile(
        template_profile,
        selected_profiles,
        autoselect,
        probe_url,
        probe_interval,
        direct_domains=direct_domains,
        sticky_domains=sticky_domains,
    )


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


def _normalize_outbound_settings(outbound):
    """Rebuild canonical settings (vnext/servers) from flat 3x-ui fields. Public API."""
    if not isinstance(outbound, dict):
        return outbound
    protocol = str(outbound.get("protocol", "")).lower()
    settings = outbound.get("settings")
    if not isinstance(settings, dict):
        settings = {}

    tag = outbound.get("tag") or "unknown"

    if protocol in ("vless", "vmess"):
        if "vnext" not in settings:
            addr = settings.get("address") or settings.get("add") or ""
            port = settings.get("port") or 443
            vuid = settings.get("id") or settings.get("uuid") or ""
            flow = settings.get("flow") or ""
            encryption = settings.get("encryption") or "none"
            level = settings.get("level", 8)
            if addr and vuid:
                user_obj = {
                    "id": str(vuid),
                    "encryption": encryption,
                    "level": level,
                    "security": settings.get("security", "auto"),
                }
                if flow and protocol == "vless":
                    user_obj["flow"] = flow
                if protocol == "vmess":
                    user_obj["alterId"] = settings.get("alterId", 0)

                outbound["settings"] = {
                    "vnext": [
                        {
                            "address": str(addr),
                            "port": int(port) if str(port).isdigit() else port,
                            "users": [user_obj],
                        }
                    ]
                }
                log(f"WARNING: Invalid {protocol.upper()} outbound detected '{tag}' ({addr}:{port}): missing vnext, auto-fixing applied")

    elif protocol == "trojan":
        if "servers" not in settings:
            addr = settings.get("address") or settings.get("add") or ""
            port = settings.get("port") or 443
            password = settings.get("password") or settings.get("id") or ""
            level = settings.get("level", 8)
            if addr and password:
                outbound["settings"] = {
                    "servers": [
                        {
                            "address": str(addr),
                            "port": int(port) if str(port).isdigit() else port,
                            "password": str(password),
                            "level": level,
                        }
                    ]
                }
                log(f"WARNING: Invalid TROJAN outbound detected '{tag}' ({addr}:{port}): missing servers, auto-fixing applied")

    final_settings = outbound.get("settings", {})
    if protocol in ("vless", "vmess") and "vnext" not in final_settings:
        log(f"WARNING: Outbound '{tag}' protocol '{protocol}' is missing 'vnext' section!")
    elif protocol == "trojan" and "servers" not in final_settings:
        log(f"WARNING: Outbound '{tag}' protocol 'trojan' is missing 'servers' section!")

    return outbound


def _sub_headers(resp_headers):
    h = {}
    keys = (
        "Subscription-Userinfo", "Profile-Title", "Content-Disposition",
        "Profile-Update-Interval", "Profile-Web-Page-Url",
        "Announce", "Routing", "Routing-Enable"
    )
    for key in keys:
        val = resp_headers.get(key)
        if val is not None:
            h[key] = val
    return h


async def build_for_subscription(sub_id, storage, query="", http_manager=None, out_format="xray"):
    format_key = str(out_format or "xray").strip().lower()
    if format_key not in WIRE_FORMATS:
        format_key = "xray"

    sub_ref = fingerprint_secret(sub_id)
    cfg_probe_url, cfg_probe_interval = await storage.get_probe_config()

    try:
        original_text, content_type, resp_headers = await fetch_original_subscription(
            sub_id, query=query, client_manager=http_manager
        )
    except Exception as exc:
        log(f"sub_id_hash={sub_ref}: failed to fetch upstream subscription")
        raise RuntimeError("Failed to fetch upstream subscription") from exc

    try:
        profiles = normalize_subscription(original_text)
    except Exception as exc:
        log(f"sub_id_hash={sub_ref}: failed to parse upstream subscription")
        raise RuntimeError("Failed to parse upstream subscription") from exc

    if not profiles:
        log(f"sub_id_hash={sub_ref}: upstream subscription is an empty profile list, passthrough original")
        return original_text, content_type, _sub_headers(resp_headers)

    sub_title_env = env_get("SUB_TITLE", "")
    sub_userinfo_env = env_get("SUB_USERINFO", "")

    sub_headers = _sub_headers(resp_headers)
    client = await resolve_client(sub_id, storage, http_manager=http_manager)

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
        log(f"sub_id_hash={sub_ref}: client not found, passthrough original")
        return original_text, content_type, sub_headers

    groups = client.get("groups") or []
    log(
        f"sub_id_hash={sub_ref}: resolved client email={mask_email(client.get('email'))} "
        f"groups={groups} source={client.get('source')}"
    )

    if not groups:
        log(f"sub_id_hash={sub_ref}: client has no groups, passthrough original")
        return original_text, content_type, sub_headers

    group_rules = await storage.get_group_rules()
    allowed_ids = allowed_autoselect_ids(groups, group_rules)
    log(f"sub_id_hash={sub_ref}: groups={groups} rules_keys={list(group_rules.keys())} allowed_ids={allowed_ids}")
    if not allowed_ids:
        log(f"sub_id_hash={sub_ref}: allowed_ids empty, passthrough")
        return original_text, content_type, sub_headers

    autoselects = await storage.get_autoselects()
    log(f"sub_id_hash={sub_ref}: autoselects in db: {[(a['id'], a['selected_node_ids']) for a in autoselects]}")
    by_id = {a.get("id"): a for a in autoselects if a.get("enabled", True)}
    direct_domains = await storage.get_direct_domains()
    try:
        sticky_domains = await storage.get_sticky_domains()
    except Exception:
        sticky_domains = []
    if not isinstance(sticky_domains, list):
        sticky_domains = []

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

    pool_specs = []
    for auto_id in allowed_ids:
        auto = by_id.get(auto_id)
        if not auto:
            log(f"sub_id_hash={sub_ref}: autoselect '{auto_id}' not found or disabled in by_id (ids={list(by_id.keys())})")
            continue
        sel = auto.get("selected_node_ids") or []
        tag_filter = auto.get("tag_filter") or []
        matched = match_profiles(remaining_enriched, sel, tag_filter)
        log(f"sub_id_hash={sub_ref}: autoselect '{auto_id}' selected={sel} tag_filter={tag_filter} matched={len(matched)} profiles")
        if not matched:
            tags_in_pool = set(str(p.get("_tag", "")) for p in remaining_enriched)
            log(f"sub_id_hash={sub_ref}: pool tags={tags_in_pool}")
            continue
        pool_specs.append((auto, matched))

    if format_key == "xray":
        auto_profiles = []
        for auto, matched in pool_specs:
            generated = build_xray_profile(
                template,
                matched,
                auto,
                cfg_probe_url,
                cfg_probe_interval,
                direct_domains,
                sticky_domains,
            )
            if generated:
                auto_profiles.append(generated)
            else:
                log(f"sub_id_hash={sub_ref}: autoselect '{auto.get('id')}' build returned None")

        if not auto_profiles:
            log(f"sub_id_hash={sub_ref}: no autoselects matched (allowed_ids={allowed_ids} autoselects={len(autoselects)})")
            return original_text, content_type, sub_headers

        output = dummy_nodes + auto_profiles + remaining_profiles
    else:
        nodes = []
        used_tags = set()
        tag_by_profile = {}
        for i, e in enumerate(remaining_enriched):
            outbound = e.get("_outbound")
            if not outbound:
                continue
            tag = unique_tag(node_name(e, i), used_tags)
            outbound_copy = copy.deepcopy(outbound)
            outbound_copy["tag"] = tag
            outbound_copy = _normalize_outbound_settings(outbound_copy)
            outbound_copy["tag"] = tag
            nodes.append((tag, outbound_copy))
            tag_by_profile[id(e)] = tag

        groups = []
        for auto, matched in pool_specs:
            member_tags = [tag_by_profile[id(p)] for p in matched if id(p) in tag_by_profile]
            if member_tags:
                groups.append((auto, member_tags))

        if not groups:
            log(f"sub_id_hash={sub_ref}: no convertible groups for {format_key}, passthrough")
            return original_text, content_type, sub_headers

        generator_kwargs = {
            "probe_url": cfg_probe_url,
            "probe_interval": cfg_probe_interval,
            "direct_domains": direct_domains,
            "sticky_domains": sticky_domains,
        }
        if format_key == "singbox":
            document = build_singbox_document(nodes, groups, **generator_kwargs)
            payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
            generated_type = "application/json; charset=utf-8"
        elif format_key == "links":
            links = build_links_document(nodes)
            if not links:
                log(f"sub_id_hash={sub_ref}: no convertible nodes for links, passthrough")
                return original_text, content_type, sub_headers
            payload = encode_links_payload(links)
            generated_type = "text/plain; charset=utf-8"
        else:
            document = build_clash_document(nodes, groups, **generator_kwargs)
            payload = dumps_clash(document)
            generated_type = "text/yaml; charset=utf-8"

        email = mask_email(client.get("email"))
        log(f"sub_id_hash={sub_ref}: generated {len(groups)} autoselect groups ({format_key}) for {email}")
        return payload, generated_type, sub_headers

    # Clean internal fields and inject address/port/inbounds/outbounds for v2rayNG/Happ ping
    def _clean(p):
        if not isinstance(p, dict):
            return p
        cleaned = {k: v for k, v in p.items() if not str(k).startswith("_")}

        # Remove 3x-ui legacy flat subscription root fields
        for key in ("address", "add", "port"):
            cleaned.pop(key, None)

        name_val = cleaned.get("remarks") or cleaned.get("name") or cleaned.get("ps") or cleaned.get("tag") or ""
        if name_val:
            cleaned["remarks"] = name_val
            cleaned["name"] = name_val
            cleaned["ps"] = name_val
            cleaned["tag"] = name_val

        # If p is a single outbound profile (has protocol at top level)
        if "protocol" in cleaned and "outbounds" not in cleaned:
            outbound_single = {
                "tag": "proxy",
                "protocol": cleaned["protocol"],
                "settings": cleaned.get("settings", {}),
            }
            if "streamSettings" in cleaned:
                outbound_single["streamSettings"] = cleaned["streamSettings"]

            outbound_single = _normalize_outbound_settings(outbound_single)

            cleaned["outbounds"] = [outbound_single, _direct_outbound(), _block_outbound()]
            for key in ("protocol", "settings", "streamSettings"):
                cleaned.pop(key, None)

        elif "outbounds" in cleaned and isinstance(cleaned["outbounds"], list):
            cleaned["outbounds"] = [_normalize_outbound_settings(ob) for ob in cleaned["outbounds"]]

        if "inbounds" not in cleaned:
            cleaned["inbounds"] = [
                {
                    "listen": "127.0.0.1",
                    "port": 10808,
                    "protocol": "socks",
                    "settings": {
                        "auth": "noauth",
                        "udp": True,
                        "userLevel": 8,
                    },
                    "sniffing": {
                        "destOverride": ["http", "tls", "quic"],
                        "enabled": True,
                    },
                    "tag": "socks",
                }
            ]

        if "routing" not in cleaned:
            cleaned["routing"] = {
                "domainStrategy": "IPIfNonMatch",
                "rules": [
                    {
                        "network": "tcp,udp",
                        "outboundTag": "proxy",
                        "type": "field",
                    }
                ],
            }

        return cleaned

    cleaned_output = [_clean(p) for p in output]

    email = mask_email(client.get("email"))
    log(f"sub_id_hash={sub_ref}: generated {len(auto_profiles)} autoselects for {email}")
    return json.dumps(cleaned_output, ensure_ascii=False, separators=(",", ":")), "application/json; charset=utf-8", sub_headers


async def resolve_client(sub_id, storage, http_manager=None):
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

    if http_manager is not None:
        try:
            async with http_manager.panel_api() as api:
                client = await api.find_client_by_sub_id(sub_id) if api else None
                if client:
                    email = client.get("email") or ""
                    if email in overrides:
                        merged = list(dict.fromkeys(client.get("groups", []) + overrides[email]))
                        client["groups"] = merged
                        client["source"] = "api+override"
                    else:
                        client["source"] = "api"
                    return client
        except Exception:
            log(f"sub_id_hash={fingerprint_secret(sub_id)}: API fallback failed")

    if sub_id in overrides:
        return {"email": sub_id, "sub_id": sub_id, "groups": overrides[sub_id], "source": "override"}

    return None


async def discover_nodes_from_sub_id(sub_id, http_manager=None):
    text, _, _ = await fetch_original_subscription(
        sub_id, client_manager=http_manager
    )
    profiles = normalize_subscription(text)
    result = []
    for i, profile in enumerate(profiles):
        summary = node_summary(profile, i)
        summary["tag"] = node_name(profile, i)
        result.append(summary)
    return result


async def resolve_security_flags(sub_id, storage, client=None, http_manager=None):
    if not client:
        client = await resolve_client(sub_id, storage, http_manager=http_manager)
    try:
        sec_rules = await storage.get_security_rules()
        if not isinstance(sec_rules, dict):
            sec_rules = {}
    except Exception:
        sec_rules = {}

    hide_groups = sec_rules.get("hide_settings_groups", [])
    if not isinstance(hide_groups, list):
        hide_groups = []
    groups = (client or {}).get("groups") or []
    c_email = (client or {}).get("email") or ""

    def _matches(rule_list):
        if "*" in rule_list:
            return True
        for g in groups:
            if g in rule_list or g.lower() in [x.lower() for x in rule_list]:
                return True
        for ident in (sub_id, c_email):
            if ident and (ident in rule_list or ident.lower() in [x.lower() for x in rule_list]):
                return True
        return False

    return {"hide_settings": _matches(hide_groups)}
