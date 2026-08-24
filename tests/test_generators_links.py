import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest

from generators import (
    build_links_document,
    encode_links_payload,
    to_share_link,
)


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
                "shortId": "abcd1234",
                "fingerprint": "chrome",
            },
            "wsSettings": {"path": "/wspath", "headers": {"Host": "cdn.example.com"}},
        },
    }


def test_vless_reality_ws_link_params():
    link = to_share_link(_vless_reality_ws_outbound(), "DE Node 1")
    parsed = urlparse(link)
    assert parsed.scheme == "vless"
    assert parsed.hostname == "edge.example.com"
    assert parsed.port == 443
    assert parsed.username == "uuid-1"
    from urllib.parse import unquote
    assert unquote(parsed.fragment) == "DE Node 1"

    query = parse_qs(parsed.query)
    assert query["encryption"] == ["none"]
    assert query["flow"] == ["xtls-rprx-vision"]
    assert query["type"] == ["ws"]
    assert query["path"] == ["/wspath"]
    assert query["host"] == ["cdn.example.com"]
    assert query["security"] == ["reality"]
    assert query["sni"] == ["cdn.example.com"]
    assert query["fp"] == ["chrome"]
    assert query["pbk"] == ["pub-key"]
    assert query["sid"] == ["abcd1234"]


def _vmess_outbound():
    return {
        "protocol": "vmess",
        "settings": {
            "vnext": [
                {
                    "address": "vm.example.com",
                    "port": 8443,
                    "users": [{"id": "uuid-2", "alterId": 7, "security": "auto"}],
                }
            ]
        },
        "streamSettings": {
            "network": "tcp",
            "security": "tls",
            "tlsSettings": {"serverName": "vm.example.com"},
        },
    }


def test_vmess_link_decodes_to_standard_payload():
    link = to_share_link(_vmess_outbound(), "VM Node")
    assert link.startswith("vmess://")
    payload = json.loads(base64.b64decode(link[len("vmess://"):]).decode("utf-8"))
    assert payload["v"] == "2"
    assert payload["ps"] == "VM Node"
    assert payload["add"] == "vm.example.com"
    assert payload["port"] == "8443"
    assert payload["id"] == "uuid-2"
    assert payload["aid"] == "7"
    assert payload["scy"] == "auto"
    assert payload["tls"] == "tls"
    assert payload["sni"] == "vm.example.com"


def _trojan_outbound():
    return {
        "protocol": "trojan",
        "settings": {
            "servers": [{"address": "tr.example.com", "port": 443, "password": "p@ss word"}]
        },
        "streamSettings": {"network": "grpc", "security": "tls",
                           "tlsSettings": {"serverName": "tr.example.com"},
                           "grpcSettings": {"serviceName": "trs"}},
    }


def test_trojan_link_with_grpc():
    link = to_share_link(_trojan_outbound(), "TR")
    parsed = urlparse(link)
    assert parsed.scheme == "trojan"
    assert parsed.hostname == "tr.example.com"
    assert parsed.port == 443
    from urllib.parse import unquote
    assert unquote(parsed.username) == "p@ss word"
    query = parse_qs(parsed.query)
    assert query["type"] == ["grpc"]
    assert query["serviceName"] == ["trs"]
    assert query["sni"] == ["tr.example.com"]
    assert query["security"] == ["tls"]


def _ss_outbound():
    return {
        "protocol": "shadowsocks",
        "settings": {
            "servers": [{"address": "ss.example.com", "port": 8388,
                         "method": "aes-256-gcm", "password": "secret"}]
        },
    }


def test_shadowsocks_link_userinfo_encoding():
    link = to_share_link(_ss_outbound(), "SS")
    parsed = urlparse(link)
    assert parsed.scheme == "ss"
    assert parsed.hostname == "ss.example.com"
    assert parsed.port == 8388
    decoded = base64.b64decode(parsed.username).decode("utf-8")
    assert decoded == "aes-256-gcm:secret"


def test_unsupported_protocols_return_none():
    assert to_share_link({"protocol": "freedom", "settings": {}}, "t") is None
    assert to_share_link({"protocol": "vless", "settings": {}}, "t") is None
    assert to_share_link(None, "t") is None


def test_build_links_document_dedupes_and_skips():
    nodes = [
        ("a", _vless_reality_ws_outbound()),
        ("bad", {"protocol": "freedom", "settings": {}}),
        ("b", _ss_outbound()),
        ("a", _vless_reality_ws_outbound()),
    ]
    links = build_links_document(nodes)
    assert len(links) == 2
    schemes = [urlparse(link).scheme for link in links]
    assert schemes == ["vless", "ss"]


def test_encode_links_payload_roundtrip():
    links = ["vless://u@h:1?x=1#A", "ss://YWVzLTEyODptZQ==@h:2#B"]
    payload = encode_links_payload(links)
    decoded = base64.b64decode(payload).decode("utf-8")
    assert decoded.splitlines() == links


@pytest.mark.parametrize("outbound", [None, {}, {"protocol": "wireguard"}])
def test_malformed_inputs_are_safe(outbound):
    assert to_share_link(outbound, "tag") is None
