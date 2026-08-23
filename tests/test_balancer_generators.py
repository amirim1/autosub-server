import asyncio
import json
from unittest.mock import AsyncMock, patch

import yaml

from balancer import (
    clamp_probe_interval,
    clamp_probe_interval_string,
    detect_country,
    dns_server_ips,
    dominant_country_group,
    group_by_country,
    min_probe_interval_seconds,
    parse_interval_seconds,
)
from builder import build_for_subscription
from generators import (
    build_clash_document,
    build_singbox_document,
    build_xray_profile,
    dumps_clash,
    to_clash_proxy,
    to_singbox_outbound,
)


def _profile(name):
    return {
        "remarks": name,
        "_outbound": {
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "node.example.com",
                        "port": 443,
                        "users": [{"id": f"uuid-{name}"}],
                    }
                ]
            },
        },
    }


# --- balancer helpers ---


def test_parse_interval_seconds_units():
    assert parse_interval_seconds("60s") == 60
    assert parse_interval_seconds("10m") == 600
    assert parse_interval_seconds("1h") == 3600
    assert parse_interval_seconds("500ms") == 1
    assert parse_interval_seconds("garbage") is None
    assert parse_interval_seconds("") is None


def test_min_probe_interval_env_override(monkeypatch):
    monkeypatch.delenv("AUTOSUB_MIN_PROBE_INTERVAL", raising=False)
    assert min_probe_interval_seconds() == 60
    monkeypatch.setenv("AUTOSUB_MIN_PROBE_INTERVAL", "120s")
    assert min_probe_interval_seconds() == 120
    monkeypatch.setenv("AUTOSUB_MIN_PROBE_INTERVAL", "bogus")
    assert min_probe_interval_seconds() == 60


def test_clamp_probe_interval(monkeypatch):
    monkeypatch.delenv("AUTOSUB_MIN_PROBE_INTERVAL", raising=False)
    assert clamp_probe_interval("5s") == 60
    assert clamp_probe_interval("10m") == 600
    assert clamp_probe_interval(None) == 60


def test_clamp_probe_interval_string_preserves_notation(monkeypatch):
    monkeypatch.delenv("AUTOSUB_MIN_PROBE_INTERVAL", raising=False)
    assert clamp_probe_interval_string("10m") == "10m"
    assert clamp_probe_interval_string("5s") == "60s"
    assert clamp_probe_interval_string(None) == "60s"


def test_detect_country_from_flag_emoji_and_tokens():
    assert detect_country("\U0001F1E9\U0001F1EA Germany #1") == "DE"
    assert detect_country("US - New York") == "US"
    assert detect_country("Германия Premium") == "DE"
    assert detect_country("Ukraine #1") == "UA"
    assert detect_country("UK London") == "GB"
    assert detect_country("Mystery Node") == ""
    assert detect_country("Node 1") == ""


def test_group_by_country_preserves_order_and_dominant_group():
    groups = group_by_country(["DE 1", "US 1", "DE 2", "Unknown"])
    assert list(groups.keys()) == ["DE", "US", ""]
    assert groups["DE"] == ["DE 1", "DE 2"]
    assert dominant_country_group(groups) == "DE"


def test_dns_server_ips_skips_doh_urls():
    assert dns_server_ips(["77.88.8.8", "https://dns.example/dns-query", "1.1.1.1"]) == [
        "77.88.8.8",
        "1.1.1.1",
    ]


# --- Xray generation ---


def test_xray_sticky_domains_pin_first_node_and_coupled_dns():
    result = build_xray_profile(
        {"remarks": "Template"},
        [_profile("Node A"), _profile("Node B")],
        {"name": "Auto"},
        "https://probe.example/generate_204",
        "10m",
        direct_domains=["domain:example.ru"],
        sticky_domains=["domain:netflix.com"],
    )

    rules = result["routing"]["rules"]
    sticky_rule = next(rule for rule in rules if rule.get("domain") == ["domain:netflix.com"])
    assert sticky_rule["outboundTag"] == "Node-A"
    direct_index = next(i for i, r in enumerate(rules) if r.get("domain") == ["domain:example.ru"])
    sticky_index = rules.index(sticky_rule)
    assert sticky_index < direct_index

    dns_rule = next(
        rule
        for rule in rules
        if rule.get("ip") == ["77.88.8.8", "1.1.1.1", "8.8.8.8"]
    )
    assert dns_rule["outboundTag"] == "Node-A"


def test_xray_country_scope_routes_catch_all_to_dominant_balancer():
    profiles = [_profile("DE Alpha"), _profile("DE Beta"), _profile("US Gamma")]
    result = build_xray_profile(
        {"remarks": "Template"},
        profiles,
        {"name": "Auto", "country_scope": True},
        "https://probe.example/generate_204",
        "1m",
    )

    balancers = {b["tag"]: b for b in result["routing"]["balancers"]}
    assert set(balancers) == {"Auto-DE", "Auto-US"}
    assert balancers["Auto-DE"]["selector"] == ["DE-Alpha", "DE-Beta"]
    assert balancers["Auto-DE"]["fallbackTag"] == "DE-Alpha"

    catch_all = next(rule for rule in result["routing"]["rules"] if "balancerTag" in rule)
    assert catch_all["balancerTag"] == "Auto-DE"


def test_xray_default_profile_has_no_dns_coupling_rule():
    result = build_xray_profile(
        {"remarks": "Template"},
        [_profile("Node A")],
        {"name": "Auto"},
        "https://probe.example/generate_204",
        "5s",
    )
    rules = result["routing"]["rules"]
    assert not any("77.88.8.8" in (rule.get("ip") or []) for rule in rules)
    assert result["burstObservatory"]["pingConfig"]["interval"] == "60s"


def test_xray_country_scope_dns_coupling_uses_balancer_tag():
    profiles = [_profile("DE Alpha"), _profile("DE Beta"), _profile("US Gamma")]
    result = build_xray_profile(
        {"remarks": "Template"},
        profiles,
        {"name": "Auto", "country_scope": True},
        "https://probe.example/generate_204",
        "1m",
    )
    dns_rule = next(
        rule
        for rule in result["routing"]["rules"]
        if rule.get("ip") == ["77.88.8.8", "1.1.1.1", "8.8.8.8"]
    )
    assert "balancerTag" in dns_rule
    assert dns_rule["balancerTag"] == "Auto-DE"
    balancer_tags = {b["tag"] for b in result["routing"]["balancers"]}
    assert dns_rule["balancerTag"] in balancer_tags


# --- converters ---


def _vless_reality_ws_outbound():
    return {
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "edge.example.com",
                    "port": 443,
                    "users": [{"id": "uuid-1", "flow": "xtls-rprx-vision"}],
                }
            ]
        },
        "streamSettings": {
            "network": "ws",
            "security": "reality",
            "realitySettings": {
                "serverName": "cdn.example.com",
                "publicKey": "pub-key",
                "shortId": "abcd",
                "fingerprint": "chrome",
            },
            "wsSettings": {"path": "/wspath", "headers": {"Host": "cdn.example.com"}},
        },
    }


def test_to_singbox_outbound_maps_vless_reality_ws():
    sb = to_singbox_outbound(_vless_reality_ws_outbound(), "tag-1")
    assert sb["type"] == "vless"
    assert sb["server"] == "edge.example.com"
    assert sb["server_port"] == 443
    assert sb["flow"] == "xtls-rprx-vision"
    assert sb["tls"]["reality"]["public_key"] == "pub-key"
    assert sb["tls"]["utls"]["fingerprint"] == "chrome"
    assert sb["transport"]["type"] == "ws"
    assert sb["transport"]["path"] == "/wspath"
    assert sb["transport"]["headers"]["Host"] == "cdn.example.com"


def test_to_singbox_outbound_rejects_unsupported_protocol():
    assert to_singbox_outbound({"protocol": "freedom", "settings": {}}, "t") is None
    assert to_singbox_outbound({"protocol": "vless", "settings": {}}, "t") is None


def test_to_clash_proxy_maps_reality_and_network_opts():
    proxy = to_clash_proxy(_vless_reality_ws_outbound(), "tag-2")
    assert proxy["type"] == "vless"
    assert proxy["servername"] == "cdn.example.com"
    assert proxy["reality-opts"]["public-key"] == "pub-key"
    assert proxy["network"] == "ws"
    assert proxy["ws-opts"]["headers"]["Host"] == "cdn.example.com"


def test_translate_geosite_and_regexp_are_dropped_cross_format():
    from generators import _group_domain_values

    suffixes, keywords = _group_domain_values(["geosite:netflix", "regexp:^x$", "domain:ok.ru"])
    assert suffixes == ["ok.ru"]
    assert keywords == []


# --- sing-box document ---


def _nodes():
    return [("n1", _vless_reality_ws_outbound()), ("n2", _profile("plain")["_outbound"])]


def test_singbox_document_structure_and_sticky_routing():
    doc = build_singbox_document(
        _nodes(),
        [({"name": "Auto"}, ["n1", "n2"])],
        probe_url="https://probe.example/generate_204",
        probe_interval="30s",
        direct_domains=["domain:example.ru"],
        sticky_domains=["domain:netflix.com"],
    )

    tags = {ob["tag"]: ob for ob in doc["outbounds"]}
    assert "Auto" in tags and "Auto · Auto" in tags
    urltest = tags["Auto · Auto"]
    assert urltest["interval"] == "60s"
    assert urltest["tolerance"] == 50

    remote_dns = next(s for s in doc["dns"]["servers"] if s["tag"] == "remote")
    assert remote_dns["detour"] == "n1"

    sticky = next(r for r in doc["route"]["rules"] if r.get("outbound") == "n1")
    assert sticky["domain_suffix"] == ["netflix.com"]
    assert doc["route"]["final"] == "Auto"


def test_singbox_country_scope_builds_subgroups_with_default():
    doc = build_singbox_document(
        _nodes(),
        [({"name": "Auto", "country_scope": True}, ["n1", "n2"])],
        probe_url="https://probe.example/generate_204",
        probe_interval="1m",
    )
    tags = {ob["tag"] for ob in doc["outbounds"]}
    assert any(tag.startswith("Auto · ") for tag in tags)
    selector = next(ob for ob in doc["outbounds"] if ob.get("tag") == "Auto")
    assert selector["type"] == "selector"


def test_singbox_every_route_rule_has_match_condition():
    doc = build_singbox_document(
        _nodes(),
        [({"name": "Auto"}, ["n1", "n2"])],
        probe_url="https://probe.example/generate_204",
        probe_interval="1m",
        sticky_domains=["domain:netflix.com"],
    )
    for rule in doc["route"]["rules"]:
        conditions = set(rule) - {"outbound"}
        assert conditions, f"rule without match condition: {rule}"


def test_singbox_duplicate_autoselect_names_produce_unique_group_tags():
    doc = build_singbox_document(
        _nodes(),
        [
            ({"name": "Auto"}, ["n1"]),
            ({"name": "Auto"}, ["n2"]),
        ],
        probe_url="https://probe.example/generate_204",
        probe_interval="1m",
    )
    outbound_tags = [ob["tag"] for ob in doc["outbounds"]]
    assert len(outbound_tags) == len(set(outbound_tags))


def test_singbox_unconvertible_group_does_not_dangle_final_target():
    unconvertible = ("bad", {"protocol": "socks", "settings": {"servers": []}})
    doc = build_singbox_document(
        [unconvertible, ("n1", _vless_reality_ws_outbound())],
        [({"name": "Auto"}, ["bad"])],
        probe_url="https://probe.example/generate_204",
        probe_interval="1m",
    )
    known_tags = {ob.get("tag") for ob in doc["outbounds"]}
    assert doc["route"]["final"] in known_tags | {"direct"}
    for rule in doc["route"]["rules"]:
        if rule.get("outbound") not in ("direct", "block"):
            assert rule["outbound"] in known_tags


# --- Clash document ---


def test_clash_document_rules_and_groups():
    raw = yaml.safe_load(
        dumps_clash(
            build_clash_document(
                _nodes(),
                [({"name": "Auto"}, ["n1", "n2"])],
                probe_url="https://probe.example/generate_204",
                probe_interval="2m",
                direct_domains=["domain:example.ru"],
                sticky_domains=["domain:netflix.com"],
            )
        )
    )

    names = [p["name"] for p in raw["proxies"]]
    assert names == ["n1", "n2"]
    group_names = {g["name"]: g for g in raw["proxy-groups"]}
    assert group_names["Auto"]["type"] == "select"
    auto_urltest = group_names["Auto · Auto"]
    assert auto_urltest["interval"] == 120
    assert auto_urltest["tolerance"] == 50
    assert auto_urltest["lazy"] is True

    rules = raw["rules"]
    assert "DOMAIN-SUFFIX,netflix.com,n1" in rules
    assert "DOMAIN-SUFFIX,example.ru,DIRECT" in rules
    assert rules[-1] == "MATCH,Auto"


def test_clash_country_scope_subgroups():
    raw = build_clash_document(
        [("DE-1", _profile("DE 1")["_outbound"]), ("US-1", _profile("US 1")["_outbound"])],
        [({"name": "Auto", "country_scope": True}, ["DE-1", "US-1"])],
        probe_url="https://probe.example/generate_204",
        probe_interval="1m",
    )
    subgroup_names = [g["name"] for g in raw["proxy-groups"] if g["type"] == "url-test"]
    assert any(name.startswith("Auto · DE") for name in subgroup_names)
    assert any(name.startswith("Auto · US") for name in subgroup_names)


def test_clash_duplicate_autoselect_names_produce_unique_groups():
    raw = build_clash_document(
        _nodes(),
        [
            ({"name": "Auto"}, ["n1"]),
            ({"name": "Auto"}, ["n2"]),
        ],
        probe_url="https://probe.example/generate_204",
        probe_interval="1m",
    )
    group_names = [g["name"] for g in raw["proxy-groups"]]
    assert len(group_names) == len(set(group_names))
    match_rule = raw["rules"][-1]
    assert match_rule == "MATCH,Auto"


def test_clash_unconvertible_group_does_not_dangle_match_target():
    raw = build_clash_document(
        [("bad", {"protocol": "socks", "settings": {}}), ("n1", _vless_reality_ws_outbound())],
        [({"name": "Auto"}, ["bad"])],
        probe_url="https://probe.example/generate_204",
        probe_interval="1m",
    )
    proxy_names = {p["name"] for p in raw["proxies"]}
    group_names = {g["name"] for g in raw["proxy-groups"]}
    match_rule = raw["rules"][-1]
    target = match_rule.split(",", 1)[1]
    assert target in proxy_names | group_names | {"DIRECT"}


# --- end-to-end through builder ---


def _storage_mock(sticky=None):
    storage_mock = AsyncMock()
    storage_mock.get_probe_config.return_value = ("https://probe.example/", "10m")
    storage_mock.get_client_groups.return_value = ["vip"]
    storage_mock.get_client_email.return_value = "test@example.com"
    storage_mock.get_group_rules.return_value = {"vip": ["auto"]}
    storage_mock.get_autoselects.return_value = [
        {"id": "auto", "name": "Auto", "enabled": True, "selected_node_ids": ["*"], "strategy": "leastPing"}
    ]
    storage_mock.get_direct_domains.return_value = []
    if sticky is None:
        storage_mock.get_sticky_domains.side_effect = RuntimeError("legacy storage")
    else:
        storage_mock.get_sticky_domains.return_value = sticky
    return storage_mock


def _raw_subscription():
    return json.dumps(
        [
            {
                "remarks": "\U0001F1E9\U0001F1EA Berlin",
                "protocol": "vless",
                "settings": {
                    "vnext": [{"address": "de01.example.com", "port": 443, "users": [{"id": "uuid-de"}]}]
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "tls",
                    "tlsSettings": {"serverName": "de01.example.com"},
                },
            },
            {
                "remarks": "\U0001F1FA\U0001F1F8 Miami",
                "protocol": "trojan",
                "settings": {
                    "servers": [{"address": "us01.example.com", "port": 443, "password": "pw"}]
                },
                "streamSettings": {"network": "tcp", "security": "none"},
            },
        ]
    )


async def _async_build(out_format, sticky=None):
    storage_mock = _storage_mock(sticky=sticky)
    with patch(
        "builder.fetch_original_subscription",
        new=AsyncMock(return_value=(_raw_subscription(), "application/json", {})),
    ):
        return await build_for_subscription("sub123", storage_mock, out_format=out_format)


def test_builder_singbox_output():
    output_text, content_type, _ = asyncio.run(_async_build("singbox"))
    assert content_type == "application/json; charset=utf-8"
    doc = json.loads(output_text)
    outbound_types = {ob["type"] for ob in doc["outbounds"]}
    assert {"vless", "trojan"} <= outbound_types
    selector = next(ob for ob in doc["outbounds"] if ob.get("tag") == "Auto")
    assert selector["type"] == "selector"
    assert doc["route"]["final"] == "Auto"


def test_builder_clash_output():
    output_text, content_type, _ = asyncio.run(_async_build("clash"))
    assert content_type == "text/yaml; charset=utf-8"
    doc = yaml.safe_load(output_text)
    proxy_types = {p["type"] for p in doc["proxies"]}
    assert {"vless", "trojan"} <= proxy_types


def test_builder_sticky_end_to_end_via_storage():
    output_text, _, _ = asyncio.run(_async_build("xray", sticky=["domain:bank.example"]))
    generated = json.loads(output_text)
    auto_profile = next(n for n in generated if n.get("remarks") == "Auto")
    sticky_rules = [
        r for r in auto_profile["routing"]["rules"] if r.get("domain") == ["domain:bank.example"]
    ]
    assert len(sticky_rules) == 1
    assert sticky_rules[0]["outboundTag"].startswith("Berlin")


def test_unknown_out_format_falls_back_to_xray():
    output_text, content_type, _ = asyncio.run(_async_build("hysteria2"))
    assert content_type == "application/json; charset=utf-8"
    assert isinstance(json.loads(output_text), list)
