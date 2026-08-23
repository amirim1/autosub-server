"""Server-side catalog backing the public subscription landing page.

All external URLs shown on the landing originate here (or from the
AUTOSUB_LANDING_OVERRIDES environment override) — never from upstream data.
"""

import json
from dataclasses import dataclass

from config import env_get
from logger import logger


OVERRIDES_ENV = "AUTOSUB_LANDING_OVERRIDES"

PLATFORMS = (
    ("android", "Android"),
    ("android-tv", "Android TV"),
    ("ios", "iOS"),
    ("windows", "Windows"),
    ("macos", "macOS"),
)


@dataclass(frozen=True)
class ClientEntry:
    id: str
    name: str
    badge: str
    deep_link_scheme: str
    downloads: dict[str, str]


_DEFAULT_CATALOG = (
    ClientEntry(
        id="happ",
        name="Happ",
        badge="Рекомендуем",
        deep_link_scheme="happ://add/{b64}",
        downloads={
            "android": "https://happ.su/",
            "android-tv": "https://happ.su/",
            "ios": "https://happ.su/",
            "windows": "https://happ.su/",
            "macos": "https://happ.su/",
        },
    ),
    ClientEntry(
        id="v2raytun",
        name="v2RayTun",
        badge="",
        deep_link_scheme="v2raytun://import/{b64}",
        downloads={
            "android": "https://play.google.com/store/apps/details?id=com.v2raytun.android",
            "ios": "https://databridges.tech",
        },
    ),
    ClientEntry(
        id="incy",
        name="Incy",
        badge="",
        deep_link_scheme="incy://import/{b64}",
        downloads={},
    ),
)


def _load_overrides():
    raw = str(env_get(OVERRIDES_ENV, "")).strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("AUTOSUB_LANDING_OVERRIDES is not valid JSON; ignored")
        return {}
    if not isinstance(data, dict):
        logger.warning("AUTOSUB_LANDING_OVERRIDES must be a JSON object; ignored")
        return {}
    result = {}
    for client_id, config in data.items():
        if not isinstance(client_id, str) or not isinstance(config, dict):
            continue
        downloads = config.get("downloads")
        if not isinstance(downloads, dict):
            continue
        clean = {}
        for platform, url in downloads.items():
            if (
                isinstance(platform, str)
                and isinstance(url, str)
                and url.startswith(("https://", "http://"))
            ):
                clean[platform] = url
            else:
                logger.warning(
                    "AUTOSUB_LANDING_OVERRIDES entry rejected (must be http(s) URL) client=%s platform=%s",
                    client_id,
                    platform,
                )
        if clean:
            result[client_id.strip().lower()] = clean
    return result


def get_client_entries():
    overrides = _load_overrides()
    entries = []
    for entry in _DEFAULT_CATALOG:
        downloads = dict(entry.downloads)
        override = overrides.get(entry.id)
        if override:
            downloads.update(override)
        entries.append(
            ClientEntry(
                id=entry.id,
                name=entry.name,
                badge=entry.badge,
                deep_link_scheme=entry.deep_link_scheme,
                downloads=downloads,
            )
        )
    return tuple(entries)


def build_landing_view(subscribe_url_b64):
    """Render the catalog into a per-platform view-model for the landing template.

    A client appears on a platform when a download link exists for it, or on
    every platform when its download coverage is unknown (import-only).
    """
    clients_view = [
        {
            "id": entry.id,
            "name": entry.name,
            "badge": entry.badge,
            "deep_link": entry.deep_link_scheme.format(b64=subscribe_url_b64),
            "downloads": dict(entry.downloads),
        }
        for entry in get_client_entries()
    ]
    return [
        {
            "id": platform_id,
            "label": label,
            "clients": [
                client
                for client in clients_view
                if platform_id in client["downloads"] or not client["downloads"]
            ],
        }
        for platform_id, label in PLATFORMS
    ]
