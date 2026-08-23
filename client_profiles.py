"""Client profile registry mapping connecting apps to their default wire format."""

from dataclasses import dataclass


class UnknownClientError(ValueError):
    pass


@dataclass(frozen=True)
class ClientProfile:
    id: str
    display_name: str
    wire_format: str
    ua_tokens: tuple[str, ...]


GENERIC_PROFILE = ClientProfile(
    id="generic",
    display_name="Generic client",
    wire_format="xray",
    ua_tokens=(),
)

# Order matters: the first UA token match wins.
_PROFILES: dict[str, ClientProfile] = {
    "happ": ClientProfile(
        id="happ",
        display_name="Happ",
        wire_format="singbox",
        ua_tokens=("happ",),
    ),
    "incy": ClientProfile(
        id="incy",
        display_name="Incy",
        wire_format="singbox",
        ua_tokens=("incy",),
    ),
    "v2raytun": ClientProfile(
        id="v2raytun",
        display_name="v2RayTun",
        wire_format="singbox",
        ua_tokens=("v2raytun",),
    ),
    "singbox": ClientProfile(
        id="singbox",
        display_name="sing-box",
        wire_format="singbox",
        ua_tokens=("sing-box", "singbox"),
    ),
    "clash": ClientProfile(
        id="clash",
        display_name="Clash / Mihomo",
        wire_format="clash",
        ua_tokens=("clash", "mihomo", "stash"),
    ),
}

_CLIENT_ALIASES: dict[str, ClientProfile] = {
    "generic": GENERIC_PROFILE,
    **_PROFILES,
}


def resolve_client_profile(client_values=(), user_agent=""):
    """Resolve the requesting client.

    Priority: explicit ?client= value(s) over User-Agent detection over generic.
    Raises UnknownClientError for unknown or repeated explicit values.
    """
    values = [str(value).strip().lower() for value in client_values or () if str(value).strip()]
    if len(values) > 1:
        raise UnknownClientError("client must be specified once")
    if values:
        profile = _CLIENT_ALIASES.get(values[0])
        if profile is None:
            raise UnknownClientError(f"unsupported client '{values[0]}'")
        return profile
    return detect_client_profile(user_agent)


def detect_client_profile(user_agent=""):
    lowered = str(user_agent or "").lower()
    for profile in _PROFILES.values():
        if any(token in lowered for token in profile.ua_tokens):
            return profile
    return GENERIC_PROFILE
