from builder import match_profiles

def test_match_profiles():
    profiles = [
        {"_node_id": "1", "_tag": "US 1"},
        {"_node_id": "2", "_tag": "US 2"},
        {"_node_id": "3", "_tag": "DE 1"},
    ]
    # Test tag filter
    matched = match_profiles(profiles, ["*"], ["us 1"])
    assert len(matched) == 1
    assert matched[0]["_node_id"] == "1"
    
    # Test ID matching
    matched2 = match_profiles(profiles, ["2"])
    assert len(matched2) == 1
    assert matched2[0]["_node_id"] == "2"
    
    # Test all
    matched3 = match_profiles(profiles, ["*"])
    assert len(matched3) == 3


def test_clean_enrichment():
    import json
    from unittest.mock import AsyncMock, patch
    from builder import build_for_subscription

    storage_mock = AsyncMock()
    storage_mock.get_probe_config.return_value = ("http://cp.cloudflare.com/", "10m")
    storage_mock.get_client_groups.return_value = ["vip"]
    storage_mock.get_client_email.return_value = "test@example.com"
    storage_mock.get_group_rules.return_value = {"vip": ["auto"]}
    storage_mock.get_autoselects.return_value = [
        {"id": "auto", "name": "Auto", "enabled": True, "selected_node_ids": ["*"]}
    ]

    raw_sub = json.dumps([
        {
            "tag": "PL-Node",
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "pl01.amirim.space",
                        "port": 45336,
                        "users": [{"id": "uuid-1"}]
                    }
                ]
            }
        }
    ])

    with patch("builder.fetch_original_subscription", new=AsyncMock(return_value=(raw_sub, "application/json", {}))):
        import asyncio
        output_text, _, _ = asyncio.run(build_for_subscription("sub123", storage_mock))
        nodes = json.loads(output_text)
        
        # We expect autoselect node + regular node
        assert len(nodes) == 2
        for node in nodes:
            assert "inbounds" in node and len(node["inbounds"]) > 0
            assert "outbounds" in node and len(node["outbounds"]) > 0
            assert node.get("remarks") != ""
            assert node.get("name") != ""
            assert node.get("ps") != ""
            first_outbound = node["outbounds"][0]
            settings = first_outbound.get("settings", {})
            assert "vnext" in settings
            vnext = settings["vnext"][0]
            assert vnext["address"] == "pl01.amirim.space"
            assert vnext["port"] == 45336

