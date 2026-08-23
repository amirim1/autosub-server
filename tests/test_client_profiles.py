import pytest

from client_profiles import (
    GENERIC_PROFILE,
    UnknownClientError,
    detect_client_profile,
    resolve_client_profile,
)
from generators import WIRE_FORMATS


@pytest.mark.parametrize(
    ("user_agent", "expected_id", "expected_wire"),
    [
        ("Happ/1.14.0 (iOS 17.5.1; iPhone15,3)", "happ", "singbox"),
        ("Happ/1.10 (Android 14)", "happ", "singbox"),
        ("Incy/2.1.0 (iOS 17.0)", "incy", "singbox"),
        ("V2RayTun/3.0.5 (Android 13)", "v2raytun", "singbox"),
        ("v2raytun/2.9 com.v2raytun.android", "v2raytun", "singbox"),
        ("sing-box/1.11.0 (android)", "singbox", "singbox"),
        ("ClashMetaForAndroid/2.11.3.Meta", "clash", "clash"),
        ("stash-mihomo/1.18 iOS", "clash", "clash"),
        ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/126 Safari/537.36", "generic", "xray"),
        ("curl/8.4.0", "generic", "xray"),
        ("v2rayNG/1.8.31", "generic", "xray"),
        ("", "generic", "xray"),
    ],
)
def test_detect_client_profile_by_user_agent(user_agent, expected_id, expected_wire):
    profile = detect_client_profile(user_agent)
    assert profile.id == expected_id
    assert profile.wire_format == expected_wire


def test_generic_profile_constant():
    assert GENERIC_PROFILE.wire_format == "xray"
    assert GENERIC_PROFILE.ua_tokens == ()


@pytest.mark.parametrize("client_value", ["happ", "INCY", " V2RayTun ", "singbox", "clash", "generic"])
def test_explicit_client_overrides_user_agent(client_value):
    profile = resolve_client_profile(
        client_values=[client_value],
        user_agent="Mozilla/5.0 (Windows NT 10.0)",
    )
    assert profile.id == client_value.strip().lower()


def test_unknown_client_raises():
    with pytest.raises(UnknownClientError):
        resolve_client_profile(client_values=["hysteria-app"], user_agent="")


def test_repeated_client_values_raise():
    with pytest.raises(UnknownClientError):
        resolve_client_profile(client_values=["happ", "incy"], user_agent="")


def test_empty_client_list_falls_back_to_user_agent():
    profile = resolve_client_profile(client_values=[], user_agent="Happ/1.0")
    assert profile.id == "happ"


def test_every_profile_wire_format_is_supported():
    profiles = [GENERIC_PROFILE]
    from client_profiles import _PROFILES

    profiles.extend(_PROFILES.values())
    for profile in profiles:
        assert profile.wire_format in WIRE_FORMATS
