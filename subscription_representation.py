from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import quote, unquote_plus

from fastapi.templating import Jinja2Templates

from config import APP_DIR, VERSION
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


class UnsupportedSubscriptionFormat(ValueError):
    pass


@dataclass(frozen=True)
class AcceptedMediaType:
    media_type: str
    quality: float
    position: int


templates_dir = APP_DIR / "templates"
if not templates_dir.exists():
    templates_dir = Path(__file__).parent.resolve() / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

subscription_css_path = APP_DIR / "static" / "subscription.css"
if not subscription_css_path.exists():
    subscription_css_path = Path(__file__).parent.resolve() / "static" / "subscription.css"


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
        try:
            return SubscriptionRepresentation(str(explicit[0]).strip().lower())
        except ValueError as exc:
            raise UnsupportedSubscriptionFormat(
                "supported subscription formats are json and html"
            ) from exc

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

    normalized_agent = str(user_agent or "").lower()
    if any(client in normalized_agent for client in KNOWN_SUBSCRIPTION_CLIENTS):
        return SubscriptionRepresentation.JSON
    if "mozilla/" in normalized_agent:
        return SubscriptionRepresentation.HTML
    return SubscriptionRepresentation.JSON


def strip_format_query(raw_query):
    retained = []
    for component in str(raw_query or "").split("&"):
        encoded_name = component.partition("=")[0]
        if unquote_plus(encoded_name) == "format":
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
