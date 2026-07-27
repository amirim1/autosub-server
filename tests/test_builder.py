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
