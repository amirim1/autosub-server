import pytest
from fingerprint import canonical_node_id, unique_tag, profile_node_id

def test_unique_tag():
    used = set()
    tag1 = unique_tag("My Node", used)
    assert tag1 == "My-Node"
    assert "My-Node" in used
    tag2 = unique_tag("My Node", used)
    assert tag2 == "My-Node-2"

def test_canonical_node_id():
    profile1 = {
        "outbounds": [{
            "tag": "proxy",
            "protocol": "vless",
            "settings": {"address": "1.1.1.1", "port": 443},
            "streamSettings": {
                "network": "ws",
                "security": "tls",
                "tlsSettings": {"serverName": "example.com"},
                "wsSettings": {"path": "/ws"}
            }
        }]
    }
    profile2 = {
        "outbounds": [{
            "tag": "proxy",
            "protocol": "vless",
            "settings": {"address": "2.2.2.2", "port": 8443},
            "streamSettings": {
                "network": "ws",
                "security": "tls",
                "tlsSettings": {"serverName": "example.com"},
                "wsSettings": {"path": "/ws"}
            }
        }]
    }
    # canonical_node_id should ignore address and port, focusing on structural fingerprint
    id1 = canonical_node_id(profile1)
    id2 = canonical_node_id(profile2)
    assert id1 == id2
    
    # profile_node_id includes address/port
    assert profile_node_id(profile1) != profile_node_id(profile2)
