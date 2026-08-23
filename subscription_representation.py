"""Subscription representation selection and local landing page rendering."""

import base64
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import quote, unquote_plus

from fastapi.templating import Jinja2Templates

from client_profiles import detect_client_profile
from config import VERSION, env_get
from landing_catalog import build_landing_view
from logging_utils import get_request_id


KNOWN_SUBSCRIPTION_CLIENTS = (
    "v2ray",
    "happ",
    "nekobox",
    "sing-box",
    "clash",
    "shadowrocket",
    "stash",
    "surge",
    "foxray",
    "streisand",
    "passwall",
    "openwrt",
)


class SubscriptionRepresentation(Enum):
    JSON = "json"
    HTML = "html"


_WIRE_FORMAT_ALIASES = {
    "xray": "xray",
    "json": "xray",
    "singbox": "singbox",
    "sing-box": "singbox",
    "sb": "singbox",
    "clash": "clash",
    "clash-meta": "clash",
    "clash.meta": "clash",
    "mihomo": "clash",
    "links": "links",
    "base64": "links",
}

_JSON_FORMAT_VALUES = frozenset({"json"}) | set(_WIRE_FORMAT_ALIASES)


def resolve_wire_format(*, is_json_route, format_values=(), user_agent=""):
    """Resolve the requested wire format: xray | singbox | clash | links."""
    for value in format_values or ():
        key = str(value).strip().lower()
        resolved = _WIRE_FORMAT_ALIASES.get(key)
        if resolved is not None:
            return resolved
    return detect_client_profile(user_agent=user_agent).wire_format


class UnsupportedSubscriptionFormat(ValueError):
    pass


@dataclass(frozen=True)
class AcceptedMediaType:
    media_type: str
    quality: float
    position: int


templates_dir = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

subscription_css_path = Path(__file__).resolve().parent / "static" / "subscription.css"


def parse_accept_header(value):
    accepted = []
    for position, item in enumerate(str(value or "").split(",")):
        parts = [part.strip() for part in item.split(";")]
        media_type = parts[0].lower()
        if not media_type:
            continue
        quality = 1.0
        valid = True
        for parameter in parts[1:]:
            name, separator, raw_value = parameter.partition("=")
            if separator and name.strip().lower() == "q":
                try:
                    quality = float(raw_value.strip())
                except ValueError:
                    valid = False
                if not 0 <= quality <= 1:
                    valid = False
                break
        if valid and quality > 0:
            accepted.append(AcceptedMediaType(media_type, quality, position))
    return tuple(accepted)


def select_subscription_representation(
    *,
    is_json_route,
    format_values=(),
    accept="",
    user_agent="",
):
    if is_json_route:
        return SubscriptionRepresentation.JSON

    explicit = tuple(format_values or ())
    if explicit:
        if len(explicit) != 1:
            raise UnsupportedSubscriptionFormat("format must be specified once")
        normalized = str(explicit[0]).strip().lower()
        if normalized in _JSON_FORMAT_VALUES:
            return SubscriptionRepresentation.JSON
        try:
            return SubscriptionRepresentation(normalized)
        except ValueError as exc:
            raise UnsupportedSubscriptionFormat(
                "supported subscription formats are json and html"
            ) from exc

    normalized_agent = str(user_agent or "").lower()
    is_subscription_client = any(
        client in normalized_agent for client in KNOWN_SUBSCRIPTION_CLIENTS
    )
    if "mozilla/" in normalized_agent and not is_subscription_client:
        return SubscriptionRepresentation.HTML

    choices = []
    for item in parse_accept_header(accept):
        if item.media_type == "text/html":
            representation = SubscriptionRepresentation.HTML
        elif item.media_type in ("application/json", "text/plain"):
            representation = SubscriptionRepresentation.JSON
        else:
            continue
        choices.append((-item.quality, item.position, representation))
    if choices:
        return min(choices)[2]

    if is_subscription_client:
        return SubscriptionRepresentation.JSON
    return SubscriptionRepresentation.JSON


def strip_format_query(raw_query):
    stripped_names = {"format", "client"}
    retained = []
    for component in str(raw_query or "").split("&"):
        encoded_name = component.partition("=")[0]
        if unquote_plus(encoded_name) in stripped_names:
            continue
        retained.append(component)
    return "&".join(retained)


def build_public_subscription_url(request, encoded_sub_id):
    """Absolute /json/ URL used inside client deep links.

    AUTOSUB_PUBLIC_URL wins (reliable behind reverse proxies); otherwise the
    request base URL is used as a best-effort fallback.
    """
    json_path = f"/json/{encoded_sub_id}"
    public_base = str(env_get("AUTOSUB_PUBLIC_URL", "")).strip().rstrip("/")
    if public_base:
        return public_base + json_path
    return str(request.base_url).rstrip("/") + json_path


def render_subscription_page(request, sub_id, *, error=False, status_code=200):
    encoded_sub_id = quote(str(sub_id), safe="")
    subscribe_url = build_public_subscription_url(request, encoded_sub_id)
    subscribe_url_b64 = base64.b64encode(subscribe_url.encode("utf-8")).decode("ascii")
    context = {
        "app_version": VERSION,
        "error": error,
        "html_url": f"/sub/{encoded_sub_id}?format=html",
        "json_url": f"/json/{encoded_sub_id}",
        "subscribe_url": subscribe_url,
        "subscribe_url_b64": subscribe_url_b64,
        "platform_panels": build_landing_view(subscribe_url_b64),
        "request_id": get_request_id() if error else "",
    }
    return templates.TemplateResponse(
        request=request,
        name="subscription.html",
        context=context,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )
