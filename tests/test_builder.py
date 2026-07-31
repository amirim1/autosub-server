import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from builder import build_autoselect_profile, build_for_subscription, match_profiles


def _selected_profile():
    return {
        "remarks": "Node 1",
        "_outbound": {
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "node.example.com",
                        "port": 443,
                        "users": [{"id": "uuid-1"}],
                    }
                ]
            },
        },
    }


@pytest.mark.parametrize(
    ("configured", "expected", "has_settings"),
    [
        ("leastPing", "leastPing", False),
        ("leastLoad", "leastLoad", True),
        ("unsupported", "leastPing", False),
    ],
)
def test_build_autoselect_uses_probe_interval_and_strategy(configured, expected, has_settings):
    result = build_autoselect_profile(
        {"remarks": "Template"},
        [_selected_profile()],
        {"name": "Auto", "strategy": configured},
        "https://probe.example/generate_204",
        "10m",
    )

    assert result["burstObservatory"]["pingConfig"]["interval"] == "10m"
    strategy = result["routing"]["balancers"][0]["strategy"]
    assert strategy["type"] == expected
    assert ("settings" in strategy) is has_settings


def test_empty_subscription_is_passed_through():
    storage_mock = AsyncMock()
    storage_mock.get_probe_config.return_value = ("https://probe.example/", "1m")

    with patch(
        "builder.fetch_original_subscription",
        new=AsyncMock(return_value=("[]", "application/json", {"Profile-Title": "Empty"})),
    ):
        output, content_type, headers = asyncio.run(
            build_for_subscription("empty-sub", storage_mock)
        )

    assert output == "[]"
    assert content_type == "application/json"
    assert headers["Profile-Title"] == "Empty"
    storage_mock.get_group_rules.assert_not_awaited()

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
            assert "hideSettings" not in node
            assert "hide_settings" not in node
            assert node.get("remarks") != ""
            assert node.get("name") != ""
            assert node.get("ps") != ""
            first_outbound = node["outbounds"][0]
            settings = first_outbound.get("settings", {})
            assert "vnext" in settings
            vnext = settings["vnext"][0]
            assert vnext["address"] == "pl01.amirim.space"
            assert vnext["port"] == 45336
