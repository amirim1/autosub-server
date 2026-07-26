import copy
import hashlib
import json
import re


def stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def extract_proxy_outbound(profile):
    if not isinstance(profile, dict):
        return None
    outbounds = profile.get("outbounds")
    if isinstance(outbounds, list):
        for outbound in outbounds:
            if not isinstance(outbound, dict):
                continue
            protocol = outbound.get("protocol")
            tag = outbound.get("tag")
            if tag == "proxy" and protocol not in ("freedom", "blackhole", "dns", "block"):
                return copy.deepcopy(outbound)
        for outbound in outbounds:
            if not isinstance(outbound, dict):
                continue
            if outbound.get("protocol") not in ("freedom", "blackhole", "dns", "block"):
                return copy.deepcopy(outbound)
    elif profile.get("protocol") and profile.get("protocol") not in ("freedom", "blackhole", "dns", "block"):
        return copy.deepcopy(profile)
    return None


def node_name(profile, index=0):
    for key in ("remarks", "remark", "ps", "name"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"Node {index + 1}"


def strip_user_fields(value):
    user_keys = {
        "id", "uuid", "password", "email", "level", "alterId",
        "users", "user", "clients", "client", "auth",
    }
    if isinstance(value, dict):
        return {k: strip_user_fields(v) for k, v in value.items() if k not in user_keys}
    if isinstance(value, list):
        return [strip_user_fields(item) for item in value]
    return value


def legacy_profile_node_id(profile):
    """Original fingerprint (v2.x) — hashes everything including user fields."""
    outbound = extract_proxy_outbound(profile)
    if not outbound:
        payload = {
            "name": node_name(profile),
            "profile": profile,
        }
    else:
        settings = outbound.get("settings", {})
        stream = outbound.get("streamSettings", {})
        payload = {
            "name": node_name(profile),
            "protocol": outbound.get("protocol"),
            "address": settings.get("address"),
            "port": settings.get("port"),
            "network": stream.get("network"),
            "security": stream.get("security"),
            "settings": settings,
            "streamSettings": stream,
        }
    digest = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
    return digest[:16]


def profile_node_id(profile):
    """V2.x fingerprint without user fields (but includes address/port)."""
    outbound = extract_proxy_outbound(profile)
    if not outbound:
        payload = {
            "name": node_name(profile),
            "profile": strip_user_fields(profile),
        }
    else:
        settings = strip_user_fields(outbound.get("settings", {}))
        stream = strip_user_fields(outbound.get("streamSettings", {}))
        payload = {
            "name": node_name(profile),
            "protocol": outbound.get("protocol"),
            "address": settings.get("address"),
            "port": settings.get("port"),
            "network": stream.get("network"),
            "security": stream.get("security"),
            "settings": settings,
            "streamSettings": stream,
        }
    digest = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
    return digest[:16]


def _extract_stream_dict(outbound):
    """Safely extract streamSettings as a dict."""
    stream = outbound.get("streamSettings", {})
    if isinstance(stream, str):
        try:
            stream = json.loads(stream)
        except Exception:
            stream = {}
    if not isinstance(stream, dict):
        stream = {}
    return stream


def canonical_node_id(profile):
    """
    V3 canonical fingerprint — hashes structural properties.
    Same physical node will have the same ID even across different clients.
    Does NOT include address/port (which can differ per client inbound).
    Includes SNI (serverName) to distinguish nodes with same protocol+network+security.
    """
    outbound = extract_proxy_outbound(profile)
    if not outbound:
        return None
    stream = _extract_stream_dict(outbound)

    # Extract SNI from various possible locations in streamSettings
    server_name = ""
    for sub_key in ("tlsSettings", "realitySettings", "xtlsSettings"):
        sub = stream.get(sub_key)
        if isinstance(sub, dict):
            sn = sub.get("serverName", "")
            if sn:
                server_name = str(sn).strip().lower()
                break

    # Extract path from wsSettings / httpSettings / grpcSettings
    transport_path = ""
    for sub_key in ("wsSettings", "httpSettings", "grpcSettings", "httpupgradeSettings"):
        sub = stream.get(sub_key)
        if isinstance(sub, dict):
            tp = sub.get("path") or sub.get("serviceName") or ""
            if tp:
                transport_path = str(tp).strip()
                break

    payload = {
        "protocol": outbound.get("protocol"),
        "network": stream.get("network", ""),
        "security": stream.get("security", ""),
        "serverName": server_name,
        "transportPath": transport_path,
    }
    digest = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
    return digest[:16]


def node_summary(profile, index=0):
    outbound = extract_proxy_outbound(profile)
    settings = outbound.get("settings", {}) if outbound else {}
    stream = outbound.get("streamSettings", {}) if outbound else {}
    tag = profile.get("tag") or profile.get("group") or ""
    if not tag and outbound:
        tag = outbound.get("tag", "")
    return {
        "id": profile_node_id(profile),
        "canonical_id": canonical_node_id(profile),
        "legacy_id": legacy_profile_node_id(profile),
        "name": node_name(profile, index),
        "protocol": outbound.get("protocol", "") if outbound else "",
        "address": settings.get("address", ""),
        "port": settings.get("port", ""),
        "network": stream.get("network", ""),
        "security": stream.get("security", ""),
        "tag": tag or "",
    }


def unique_tag(base, used):
    """Generate a unique tag from base name, preserving unicode words, appending -N suffix if collision."""
    clean = re.sub(r"[^\w_.:-]+", "-", base.strip(), flags=re.UNICODE)[:48].strip("-") or "node"
    tag = clean
    n = 2
    while tag in used:
        tag = f"{clean}-{n}"
        n += 1
    used.add(tag)
    return tag
