"""Subscription payload generators for Xray, sing-box, Clash.Meta and share-link formats."""

import base64
import copy
import json
from urllib.parse import quote, urlencode

import yaml

from balancer import (
    clamp_probe_interval,
    clamp_probe_interval_string,
    dns_server_ips,
    dominant_country_group,
    group_by_country,
    interval_string,
)
from config import normalize_direct_domains, SUPPORTED_AUTOSELECT_STRATEGIES
from fingerprint import node_name, unique_tag
from logger import log


WIRE_FORMATS = ("xray", "singbox", "clash", "links")

URLTEST_TOLERANCE_MS = 50
UNSUPPORTED_DOMAIN_PREFIXES = ("regexp:", "geosite:")


def _deepcopy_strategy(strategy_type):
    strategy = {"type": strategy_type}
    if strategy_type == "leastLoad":
        strategy["settings"] = {
            "maxRTT": "2500ms",
            "expected": 1,
            "baselines": ["250ms", "700ms", "1500ms"],
            "tolerance": 0.2,
        }
    return strategy


def _resolve_strategy(autoselect):
    strategy_type = autoselect.get("strategy", "leastPing")
    if strategy_type not in SUPPORTED_AUTOSELECT_STRATEGIES:
        log(f"WARNING: Unsupported autoselect strategy '{strategy_type}', falling back to leastPing")
        strategy_type = "leastPing"
    return strategy_type


def build_xray_profile(
    template_profile,
    selected_profiles,
    autoselect,
    probe_url,
    probe_interval,
    direct_domains=None,
    sticky_domains=None,
):
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
    country_scope = bool(autoselect.get("country_scope"))
    result = copy.deepcopy(template_profile)
    for key in ("remarks", "remark", "ps", "name"):
        result[key] = name

    dns_servers = ["77.88.8.8", "1.1.1.1", "8.8.8.8"]
    result["dns"] = {
        "servers": dns_servers,
        "queryStrategy": "UseIP",
    }
    result["inbounds"] = [
        {
            "tag": "socks",
            "port": 10808,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": True},
            "sniffing": {
                "enabled": True,
                "routeOnly": False,
                "destOverride": ["http", "tls", "quic"],
            },
        },
        {
            "tag": "http",
            "port": 10809,
            "listen": "127.0.0.1",
            "protocol": "http",
            "settings": {"allowTransparent": False},
            "sniffing": {
                "enabled": True,
                "routeOnly": False,
                "destOverride": ["http", "tls", "quic"],
            },
        },
    ]
    result["outbounds"] = selected_outbounds + [_direct_outbound(), _block_outbound()]
    result["burstObservatory"] = {
        "pingConfig": {
            "timeout": "2s",
            "interval": clamp_probe_interval_string(probe_interval),
            "sampling": 2,
            "destination": probe_url,
            "connectivity": "",
        },
        "subjectSelector": tags[:],
    }

    if direct_domains is None:
        direct_domains = []
    direct_domains = normalize_direct_domains(direct_domains)

    routing_rules = [
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
        {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
        {"type": "field", "port": "443", "network": "udp", "outboundTag": "block"},
    ]

    sticky_target = None
    sticky_list = normalize_direct_domains(sticky_domains) if sticky_domains else []
    if sticky_list and tags:
        sticky_target = tags[0]
        routing_rules.append({
            "type": "field",
            "domain": sticky_list,
            "outboundTag": sticky_target,
        })

    if direct_domains:
        routing_rules.append({"type": "field", "domain": direct_domains, "outboundTag": "direct"})

    strategy_type = _resolve_strategy(autoselect)
    balancers = []
    catch_all_tag = name
    if country_scope:
        groups = group_by_country(tags)
        if len(groups) == 1:
            only_members = next(iter(groups.values()))
            balancers.append(_xray_balancer(name, only_members, strategy_type))
        else:
            for cc, members in groups.items():
                suffix = cc if cc else "misc"
                balancers.append(_xray_balancer(f"{name}-{suffix}", members, strategy_type))
            dominant = dominant_country_group(groups)
            if dominant:
                catch_all_tag = f"{name}-{dominant}"
            else:
                catch_all_tag = f"{name}-misc"
    else:
        balancers.append(_xray_balancer(name, tags, strategy_type))

    dns_coupling_target = sticky_target or (catch_all_tag if country_scope else None)
    resolver_ips = dns_server_ips(dns_servers)
    if dns_coupling_target and resolver_ips:
        coupling_rule = {"type": "field", "ip": resolver_ips}
        if country_scope and not sticky_target:
            coupling_rule["balancerTag"] = dns_coupling_target
        else:
            coupling_rule["outboundTag"] = dns_coupling_target
        routing_rules.append(coupling_rule)

    routing_rules.append({"type": "field", "network": "tcp,udp", "balancerTag": catch_all_tag})

    result["routing"] = {
        "domainMatcher": "hybrid",
        "domainStrategy": "IPIfNonMatch",
        "rules": routing_rules,
        "balancers": balancers,
    }
    return result


def _xray_balancer(tag, members, strategy_type):
    return {
        "tag": tag,
        "selector": members[:],
        "fallbackTag": members[0] if members else "",
        "strategy": _deepcopy_strategy(strategy_type),
    }


def _direct_outbound():
    return {"protocol": "freedom", "settings": {"domainStrategy": "UseIP"}, "tag": "direct"}


def _block_outbound():
    return {"protocol": "blackhole", "settings": {"response": {"type": "http"}}, "tag": "block"}


# --- Shared conversion helpers ---

def _stream_parts(outbound):
    stream = outbound.get("streamSettings", {})
    if isinstance(stream, str):
        try:
            stream = json.loads(stream)
        except Exception:
            stream = {}
    if not isinstance(stream, dict):
        stream = {}
    network = str(stream.get("network") or "tcp").lower()
    security = str(stream.get("security") or "none").lower()
    return network, security, stream


def _tls_sub_settings(security, stream):
    if security == "reality":
        return stream.get("realitySettings") or {}
    if security == "xtls":
        return stream.get("xtlsSettings") or {}
    if security == "tls":
        return stream.get("tlsSettings") or {}
    return {}


def _first_vnext(settings):
    vnext = (settings.get("vnext") or [{}])[0]
    users = (vnext.get("users") or [{}])[0]
    return vnext, users


def _first_server(settings):
    return (settings.get("servers") or [{}])[0]


# --- sing-box generation ---

def to_singbox_outbound(outbound, tag):
    if not isinstance(outbound, dict):
        return None
    protocol = str(outbound.get("protocol", "")).lower()
    settings = outbound.get("settings") or {}
    network, security, stream = _stream_parts(outbound)

    outbound_sb = None
    if protocol in ("vless", "vmess"):
        vnext, user = _first_vnext(settings)
        address = vnext.get("address")
        port = vnext.get("port")
        uuid_value = user.get("id")
        if not (address and port and uuid_value):
            return None
        outbound_sb = {
            "type": protocol,
            "tag": tag,
            "server": str(address),
            "server_port": int(port),
            "uuid": str(uuid_value),
        }
        if protocol == "vmess":
            outbound_sb["security"] = str(user.get("security") or "auto")
            outbound_sb["alter_id"] = int(user.get("alterId") or 0)
        elif user.get("flow"):
            outbound_sb["flow"] = str(user["flow"])
    elif protocol == "trojan":
        server = _first_server(settings)
        address = server.get("address")
        port = server.get("port")
        password = server.get("password")
        if not (address and port and password):
            return None
        outbound_sb = {
            "type": "trojan",
            "tag": tag,
            "server": str(address),
            "server_port": int(port),
            "password": str(password),
        }
    elif protocol == "shadowsocks":
        server = _first_server(settings)
        address = server.get("address")
        port = server.get("port")
        password = server.get("password")
        if not (address and port and password):
            return None
        outbound_sb = {
            "type": "shadowsocks",
            "tag": tag,
            "server": str(address),
            "server_port": int(port),
            "method": str(server.get("method") or "aes-128-gcm"),
            "password": str(password),
        }
    else:
        return None

    tls_block = _singbox_tls(security, stream)
    if tls_block:
        outbound_sb["tls"] = tls_block
    transport = _singbox_transport(network, stream)
    if transport:
        outbound_sb["transport"] = transport
    return outbound_sb


def _singbox_tls(security, stream):
    if security not in ("tls", "reality", "xtls"):
        return None
    sub = _tls_sub_settings(security, stream)
    tls_block = {"enabled": True, "server_name": str(sub.get("serverName") or "")}
    if sub.get("allowInsecure") or sub.get("insecure"):
        tls_block["insecure"] = True
    if security == "reality":
        fingerprint = sub.get("fingerprint") or "chrome"
        tls_block["utls"] = {"enabled": True, "fingerprint": str(fingerprint)}
        tls_block["reality"] = {
            "enabled": True,
            "public_key": str(sub.get("publicKey") or ""),
            "short_id": str(sub.get("shortId") or ""),
        }
    elif sub.get("fingerprint"):
        tls_block["utls"] = {"enabled": True, "fingerprint": str(sub["fingerprint"])}
    return tls_block


def _singbox_transport(network, stream):
    if network == "ws":
        ws = stream.get("wsSettings") or {}
        transport = {"type": "ws", "path": str(ws.get("path") or "/")}
        host = (ws.get("headers") or {}).get("Host")
        if host:
            transport["headers"] = {"Host": str(host)}
        return transport
    if network == "grpc":
        grpc = stream.get("grpcSettings") or {}
        transport = {"type": "grpc", "service_name": str(grpc.get("serviceName") or "")}
        if grpc.get("multiMode"):
            transport["multi_mode"] = True
        return transport
    if network == "http":
        http = stream.get("httpSettings") or {}
        transport = {"type": "http", "path": str(http.get("path") or "/")}
        hosts = http.get("host") or []
        if hosts:
            transport["host"] = [str(h) for h in hosts]
        return transport
    if network == "httpupgrade":
        upgrade = stream.get("httpupgradeSettings") or {}
        transport = {"type": "httpupgrade", "path": str(upgrade.get("path") or "/")}
        if upgrade.get("host"):
            transport["host"] = str(upgrade["host"])
        return transport
    return None


def _translate_domain_pattern(pattern):
    """Translate an Xray domain pattern into a generic suffix/keyword entry."""
    text = str(pattern or "").strip()
    if not text or text.startswith(UNSUPPORTED_DOMAIN_PREFIXES):
        return None
    if text.startswith("domain:"):
        value = text[len("domain:"):]
        return ("domain_suffix", value) if value else None
    if text.startswith("full:"):
        value = text[len("full:"):]
        return ("domain_suffix", value) if value else None
    if text.startswith("keyword:"):
        value = text[len("keyword:"):]
        return ("domain_keyword", value) if value else None
    return ("domain_keyword", text)


def _group_domain_values(domains):
    suffixes = []
    keywords = []
    for pattern in domains or []:
        translated = _translate_domain_pattern(pattern)
        if translated is None:
            continue
        kind, value = translated
        (suffixes if kind == "domain_suffix" else keywords).append(value)
    return suffixes, keywords


def build_singbox_document(nodes, groups, *, probe_url, probe_interval, direct_domains=None, sticky_domains=None):
    """Assemble a complete sing-box client config.

    nodes: [(tag, xray_outbound)] — all real nodes, globally unique tags.
    groups: [(autoselect, [member_tags])] — autoselect pools referencing node tags.
    """
    interval_seconds = clamp_probe_interval(probe_interval)
    outbounds = []
    present_tags = []
    seen_tags = set()
    for tag, outbound in nodes:
        if tag in seen_tags:
            continue
        converted = to_singbox_outbound(outbound, tag)
        if converted is None:
            log(f"WARNING: Node '{tag}' is not convertible to sing-box, skipped")
            continue
        seen_tags.add(tag)
        present_tags.append(tag)
        outbounds.append(converted)

    group_defs = []
    used_group_tags = set(seen_tags)

    def _unique_group_tag(base):
        tag = base
        n = 2
        while tag in used_group_tags:
            tag = f"{base} ({n})"
            n += 1
        used_group_tags.add(tag)
        return tag

    for autoselect, member_tags in groups or []:
        members = [t for t in member_tags if t in seen_tags]
        if not members:
            continue
        name = _unique_group_tag(autoselect.get("name") or "Auto")
        selector_members = []
        default_outbound = members[0]
        if autoselect.get("country_scope"):
            grouped = group_by_country(members)
            dominant = dominant_country_group(grouped)
            for cc, cc_members in grouped.items():
                subgroup_tag = _unique_group_tag(f"{name} · {cc}" if cc else f"{name} · misc")
                group_defs.append({
                    "type": "urltest",
                    "tag": subgroup_tag,
                    "outbounds": cc_members,
                    "url": probe_url,
                    "interval": interval_string(interval_seconds),
                    "tolerance": URLTEST_TOLERANCE_MS,
                })
                selector_members.append(subgroup_tag)
                if dominant and cc == dominant:
                    default_outbound = subgroup_tag
        else:
            auto_tag = _unique_group_tag(f"{name} · Auto")
            group_defs.append({
                "type": "urltest",
                "tag": auto_tag,
                "outbounds": members,
                "url": probe_url,
                "interval": interval_string(interval_seconds),
                "tolerance": URLTEST_TOLERANCE_MS,
            })
            selector_members.append(auto_tag)
            default_outbound = auto_tag
        selector_members.extend(members)
        group_defs.append({
            "type": "selector",
            "tag": name,
            "outbounds": selector_members,
            "default": default_outbound,
        })

    sticky_list = normalize_direct_domains(sticky_domains) if sticky_domains else []
    suffixes, keywords = _group_domain_values(direct_domains or [])
    sticky_suffixes, sticky_keywords = _group_domain_values(sticky_list)

    pinned = present_tags[0] if present_tags else None
    final_target = next(
        (g["tag"] for g in group_defs if g.get("type") == "selector"),
        present_tags[0] if present_tags else None,
    )

    route_rules = [{"ip_is_private": True, "outbound": "direct"}]
    if sticky_list and pinned:
        sticky_rule = {"outbound": pinned}
        if sticky_suffixes:
            sticky_rule["domain_suffix"] = sticky_suffixes
        if sticky_keywords:
            sticky_rule["domain_keyword"] = sticky_keywords
        route_rules.append(sticky_rule)
    direct_rule = {"outbound": "direct"}
    if suffixes:
        direct_rule["domain_suffix"] = suffixes
    if keywords:
        direct_rule["domain_keyword"] = keywords
    if len(direct_rule) > 1:
        route_rules.append(direct_rule)

    dns_detour = pinned or final_target
    remote_dns = {
        "tag": "remote",
        "address": "https://8.8.8.8/dns-query",
    }
    if dns_detour:
        remote_dns["detour"] = dns_detour
    document = {
        "log": {"level": "warning"},
        "dns": {
            "servers": [
                remote_dns,
                {"tag": "local", "address": "local"},
            ],
            "final": "remote",
        },
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": 2080,
            }
        ],
        "outbounds": outbounds + group_defs + [
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {
            "rules": route_rules,
            "final": final_target or "direct",
        },
    }
    return document


# --- Clash.Meta generation ---

def to_clash_proxy(outbound, tag):
    if not isinstance(outbound, dict):
        return None
    protocol = str(outbound.get("protocol", "")).lower()
    settings = outbound.get("settings") or {}
    network, security, stream = _stream_parts(outbound)
    sub = _tls_sub_settings(security, stream)

    proxy = None
    if protocol in ("vless", "vmess"):
        vnext, user = _first_vnext(settings)
        address = vnext.get("address")
        port = vnext.get("port")
        uuid_value = user.get("id")
        if not (address and port and uuid_value):
            return None
        proxy = {
            "name": tag,
            "type": protocol,
            "server": str(address),
            "port": int(port),
            "uuid": str(uuid_value),
            "udp": True,
        }
        if protocol == "vmess":
            proxy["alterId"] = int(user.get("alterId") or 0)
            proxy["cipher"] = str(user.get("security") or "auto")
        elif user.get("flow"):
            proxy["flow"] = str(user["flow"])
        if security in ("tls", "reality", "xtls"):
            proxy["tls"] = True
            if sub.get("serverName"):
                proxy["servername"] = str(sub["serverName"])
            if sub.get("fingerprint"):
                proxy["client-fingerprint"] = str(sub["fingerprint"])
            if security == "reality":
                proxy["reality-opts"] = {
                    "public-key": str(sub.get("publicKey") or ""),
                    "short-id": str(sub.get("shortId") or ""),
                }
    elif protocol == "trojan":
        server = _first_server(settings)
        address = server.get("address")
        port = server.get("port")
        password = server.get("password")
        if not (address and port and password):
            return None
        proxy = {
            "name": tag,
            "type": "trojan",
            "server": str(address),
            "port": int(port),
            "password": str(password),
            "udp": True,
        }
        if sub.get("serverName"):
            proxy["sni"] = str(sub["serverName"])
    elif protocol == "shadowsocks":
        server = _first_server(settings)
        address = server.get("address")
        port = server.get("port")
        password = server.get("password")
        if not (address and port and password):
            return None
        proxy = {
            "name": tag,
            "type": "ss",
            "server": str(address),
            "port": int(port),
            "cipher": str(server.get("method") or "aes-128-gcm"),
            "password": str(password),
            "udp": True,
        }
    else:
        return None

    if network == "ws":
        ws = stream.get("wsSettings") or {}
        ws_opts = {"path": str(ws.get("path") or "/")}
        host = (ws.get("headers") or {}).get("Host")
        if host:
            ws_opts["headers"] = {"Host": str(host)}
        proxy["ws-opts"] = ws_opts
        proxy["network"] = "ws"
    elif network == "grpc":
        grpc = stream.get("grpcSettings") or {}
        if grpc.get("serviceName"):
            proxy["grpc-service-name"] = str(grpc["serviceName"])
        proxy["network"] = "grpc"
    elif network == "http":
        http = stream.get("httpSettings") or {}
        proxy["network"] = "h2"
        if http.get("path"):
            proxy["h2-opts"] = {"path": str(http["path"]), "host": [str(h) for h in (http.get("host") or [])]}
    return proxy


def build_clash_document(nodes, groups, *, probe_url, probe_interval, direct_domains=None, sticky_domains=None):
    interval_seconds = clamp_probe_interval(probe_interval)
    proxies = []
    seen_tags = set()
    for tag, outbound in nodes:
        if tag in seen_tags:
            continue
        converted = to_clash_proxy(outbound, tag)
        if converted is None:
            log(f"WARNING: Node '{tag}' is not convertible to Clash, skipped")
            continue
        seen_tags.add(tag)
        proxies.append(converted)

    group_defs = []
    used_group_names = set(seen_tags)

    def _unique_group_name(base):
        name = base
        n = 2
        while name in used_group_names:
            name = f"{base} ({n})"
            n += 1
        used_group_names.add(name)
        return name

    for autoselect, member_tags in groups or []:
        members = [t for t in member_tags if t in seen_tags]
        if not members:
            continue
        name = _unique_group_name(autoselect.get("name") or "Auto")
        selector_members = []
        if autoselect.get("country_scope"):
            grouped = group_by_country(members)
            for cc, cc_members in grouped.items():
                subgroup_tag = _unique_group_name(f"{name} · {cc}" if cc else f"{name} · misc")
                group_defs.append({
                    "name": subgroup_tag,
                    "type": "url-test",
                    "proxies": cc_members,
                    "url": probe_url,
                    "interval": interval_seconds,
                    "tolerance": URLTEST_TOLERANCE_MS,
                    "lazy": True,
                })
                selector_members.append(subgroup_tag)
        else:
            auto_tag = _unique_group_name(f"{name} · Auto")
            group_defs.append({
                "name": auto_tag,
                "type": "url-test",
                "proxies": members,
                "url": probe_url,
                "interval": interval_seconds,
                "tolerance": URLTEST_TOLERANCE_MS,
                "lazy": True,
            })
            selector_members.append(auto_tag)
        selector_members.extend(members)
        group_defs.append({"name": name, "type": "select", "proxies": selector_members})

    sticky_list = normalize_direct_domains(sticky_domains) if sticky_domains else []
    suffixes, keywords = _group_domain_values(direct_domains or [])
    sticky_suffixes, sticky_keywords = _group_domain_values(sticky_list)

    pinned = proxies[0]["name"] if proxies else None
    final_target = next(
        (g["name"] for g in group_defs if g.get("type") == "select"),
        pinned,
    )

    rules = [
        "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
        "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
        "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
        "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
        "IP-CIDR,::1/128,DIRECT,no-resolve",
        "IP-CIDR,fc00::/7,DIRECT,no-resolve",
    ]
    if sticky_list and pinned:
        for value in sticky_suffixes:
            rules.append(f"DOMAIN-SUFFIX,{value},{pinned}")
        for value in sticky_keywords:
            rules.append(f"DOMAIN-KEYWORD,{value},{pinned}")
    for value in suffixes:
        rules.append(f"DOMAIN-SUFFIX,{value},DIRECT")
    for value in keywords:
        rules.append(f"DOMAIN-KEYWORD,{value},DIRECT")
    rules.append(f"MATCH,{final_target or 'DIRECT'}")

    document = {
        "mixed-port": 7890,
        "mode": "rule",
        "log-level": "warning",
        "proxies": proxies,
        "proxy-groups": group_defs,
        "rules": rules,
    }
    return document


def dumps_clash(document):
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False, default_flow_style=False)


# --- Share-link (base64) generation ---


def _transport_link_params(network, stream):
    params = {}
    if network == "ws":
        ws = stream.get("wsSettings") or {}
        params["type"] = "ws"
        params["path"] = str(ws.get("path") or "/")
        host = (ws.get("headers") or {}).get("Host")
        if host:
            params["host"] = str(host)
    elif network == "grpc":
        grpc = stream.get("grpcSettings") or {}
        params["type"] = "grpc"
        params["serviceName"] = str(grpc.get("serviceName") or "")
    elif network == "httpupgrade":
        upgrade = stream.get("httpupgradeSettings") or {}
        params["type"] = "httpupgrade"
        params["path"] = str(upgrade.get("path") or "/")
        if upgrade.get("host"):
            params["host"] = str(upgrade["host"])
    else:
        params["type"] = "tcp"
    return params


def _tls_link_params(security, stream):
    params = {}
    if security not in ("tls", "reality", "xtls"):
        return params
    sub = _tls_sub_settings(security, stream)
    params["security"] = "tls" if security == "xtls" else security
    if sub.get("serverName"):
        params["sni"] = str(sub["serverName"])
    fingerprint = sub.get("fingerprint") or ("chrome" if security == "reality" else None)
    if fingerprint:
        params["fp"] = str(fingerprint)
    if sub.get("alpn"):
        alpn = sub["alpn"]
        params["alpn"] = ",".join(str(a) for a in alpn) if isinstance(alpn, list) else str(alpn)
    if security == "reality":
        params["pbk"] = str(sub.get("publicKey") or "")
        params["sid"] = str(sub.get("shortId") or "")
    if sub.get("allowInsecure") or sub.get("insecure"):
        params["allowInsecure"] = "1"
    return params


def to_share_link(outbound, tag):
    """Convert an Xray outbound into a standard share-link URI, or None."""
    if not isinstance(outbound, dict):
        return None
    protocol = str(outbound.get("protocol", "")).lower()
    settings = outbound.get("settings") or {}
    network, security, stream = _stream_parts(outbound)

    fragment = quote(str(tag or ""), safe="")

    if protocol == "vmess":
        vnext, user = _first_vnext(settings)
        address = vnext.get("address")
        port = vnext.get("port")
        uuid_value = user.get("id")
        if not (address and port and uuid_value):
            return None
        ws = stream.get("wsSettings") or {}
        payload = {
            "v": "2",
            "ps": str(tag or ""),
            "add": str(address),
            "port": str(port),
            "id": str(uuid_value),
            "aid": str(user.get("alterId") or 0),
            "scy": str(user.get("security") or "auto"),
            "net": network,
            "type": "none",
            "host": str((ws.get("headers") or {}).get("Host") or ""),
            "path": str(ws.get("path") or "") if network == "ws" else "",
            "tls": "tls" if security in ("tls", "reality", "xtls") else "",
            "sni": str(_tls_sub_settings(security, stream).get("serverName") or ""),
        }
        encoded = base64.b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return f"vmess://{encoded}"

    if protocol == "vless":
        vnext, user = _first_vnext(settings)
        address = vnext.get("address")
        port = vnext.get("port")
        uuid_value = user.get("id")
        if not (address and port and uuid_value):
            return None
        params = {"encryption": "none"}
        if user.get("flow"):
            params["flow"] = str(user["flow"])
        params.update(_transport_link_params(network, stream))
        params.update(_tls_link_params(security, stream))
        query = urlencode(params, quote_via=quote, safe="")
        return f"vless://{uuid_value}@{address}:{port}?{query}#{fragment}"

    if protocol == "trojan":
        server = _first_server(settings)
        address = server.get("address")
        port = server.get("port")
        password = server.get("password")
        if not (address and port and password):
            return None
        params = _transport_link_params(network, stream)
        tls_params = _tls_link_params("tls", stream) if security in ("tls", "reality", "xtls") else {}
        if security not in ("tls", "reality", "xtls"):
            tls_params["security"] = "none"
        params.update(tls_params)
        query = urlencode(params, quote_via=quote, safe="")
        return (
            f"trojan://{quote(str(password), safe='')}@{address}:{port}?{query}#{fragment}"
        )

    if protocol == "shadowsocks":
        server = _first_server(settings)
        address = server.get("address")
        port = server.get("port")
        password = server.get("password")
        method = server.get("method")
        if not (address and port and password and method):
            return None
        userinfo = base64.b64encode(f"{method}:{password}".encode("utf-8")).decode("ascii")
        plugin_part = ""
        return f"ss://{userinfo}@{address}:{port}{plugin_part}#{fragment}"

    return None


def build_links_document(nodes):
    """Convert [(tag, outbound)] into a list of share-link URIs."""
    links = []
    seen_tags = set()
    for tag, outbound in nodes:
        if tag in seen_tags:
            continue
        link = to_share_link(outbound, tag)
        if link is None:
            log(f"WARNING: Node '{tag}' is not convertible to a share link, skipped")
            continue
        seen_tags.add(tag)
        links.append(link)
    return links


def encode_links_payload(links):
    joined = "\n".join(links)
    return base64.b64encode(joined.encode("utf-8")).decode("ascii")
