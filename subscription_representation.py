from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import quote, unquote_plus

from fastapi.templating import Jinja2Templates

from client_profiles import detect_client_profile
from config import VERSION
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


def render_subscription_page(request, sub_id, *, error=False, status_code=200):
    encoded_sub_id = quote(str(sub_id), safe="")
    context = {
        "app_version": VERSION,
        "error": error,
        "html_url": f"/sub/{encoded_sub_id}?format=html",
        "json_url": f"/json/{encoded_sub_id}",
        "request_id": get_request_id() if error else "",
    }
    return templates.TemplateResponse(
        request=request,
        name="subscription.html",
        context=context,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )
