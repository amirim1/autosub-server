"""Shared balancer-pool analysis used by all subscription format generators."""

import os
import re

from config import DEFAULT_MIN_PROBE_INTERVAL_SECONDS, MIN_PROBE_INTERVAL_ENV


_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s|m|h)?$", re.IGNORECASE)

_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    None: 1.0,
}

# Regional indicator symbols A..Z (flag emoji encoding).
_RI_A = 0x1F1E6
_RI_Z = 0x1F1FF

_ISO_3166_CODES = frozenset("""
AC AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())

_ISO_CODE_RE = re.compile(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])")

COUNTRY_TOKENS = {
    "AE": ("uae", "emirat"),
    "AM": ("armenia", "армени"),
    "AT": ("austria", "австри"),
    "AU": ("australia", "австрал"),
    "BE": ("belgium", "бельг"),
    "BG": ("bulgaria", "болгар"),
    "BR": ("brazil", "бразил"),
    "CA": ("canada", "канад"),
    "CH": ("switzerland", "swiss", "швейцар"),
    "CN": ("china", "кита"),
    "CY": ("cyprus", "кипр"),
    "CZ": ("czech", "чехи"),
    "DE": ("germany", "deutschland", "герман"),
    "DK": ("denmark", "дани"),
    "EE": ("estonia", "эстон"),
    "ES": ("spain", "espana", "испан"),
    "FI": ("finland", "финлянд"),
    "FR": ("france", "франц"),
    "GB": ("uk", "britain", "england", "london", "британ", "англ"),
    "GE": ("georgia", "грузи"),
    "HK": ("hongkong", "гонконг", "хонконг"),
    "HU": ("hungary", "венгр"),
    "IE": ("ireland", "ирланд"),
    "IL": ("israel", "израил"),
    "IN": ("india", "инди"),
    "IT": ("italy", "итал"),
    "JP": ("japan", "japan", "япон"),
    "KZ": ("kazakhstan", "казахстан"),
    "LT": ("lithuania", "литв"),
    "LU": ("luxembourg", "люксембург"),
    "LV": ("latvia", "латв"),
    "MD": ("moldova", "молдов"),
    "NL": ("netherlands", "holland", "нидерланд", "голланд"),
    "NO": ("norway", "норвег"),
    "PL": ("poland", "поль"),
    "PT": ("portugal", "португал"),
    "RO": ("romania", "румын"),
    "RS": ("serbia", "серб"),
    "SE": ("sweden", "швец"),
    "SG": ("singapore", "сингапур"),
    "SK": ("slovakia", "словак"),
    "TR": ("turkey", "turkiye", "турц"),
    "UA": ("ukraine", "украин"),
    "US": ("usa", "united states", "america", "сша", "америк"),
    "VN": ("vietnam", "вьетнам"),
}


def parse_interval_seconds(value):
    """Parse an Xray-style duration string into whole seconds. Returns None on failure."""
    if isinstance(value, (int, float)) and value > 0:
        return max(1, int(value))
    match = _INTERVAL_RE.match(str(value or "").strip())
    if not match:
        return None
    number, unit = match.groups()
    try:
        seconds = float(number) * _UNIT_SECONDS[unit.lower() if unit else None]
    except ValueError:
        return None
    return max(1, int(round(seconds)))


def min_probe_interval_seconds():
    """Configured floor for health-check intervals (anti-flapping guard)."""
    raw = os.environ.get(MIN_PROBE_INTERVAL_ENV, "").strip()
    configured = parse_interval_seconds(raw) if raw else None
    if configured is None:
        return DEFAULT_MIN_PROBE_INTERVAL_SECONDS
    return max(1, configured)


def clamp_probe_interval(value):
    """Return the probe interval in whole seconds, never below the configured floor."""
    parsed = parse_interval_seconds(value)
    if parsed is None:
        parsed = DEFAULT_MIN_PROBE_INTERVAL_SECONDS
    return max(min_probe_interval_seconds(), parsed)


def clamp_probe_interval_string(value):
    """Like clamp_probe_interval but preserves the original duration notation."""
    parsed = parse_interval_seconds(value)
    floor = min_probe_interval_seconds()
    if parsed is not None and parsed >= floor:
        return str(value).strip()
    return interval_string(floor)


def interval_string(seconds):
    return f"{int(seconds)}s"


def _flag_country_codes(text):
    codes = []
    for index in range(len(text) - 1):
        pair = text[index:index + 2]
        if len(pair) == 2 and all(_RI_A <= ord(ch) <= _RI_Z for ch in pair):
            code = "".join(chr(ord(ch) - _RI_A + ord("A")) for ch in pair)
            if code not in codes:
                codes.append(code)
    return codes


def detect_country(name):
    """Best-effort country detection from a node display name/tag."""
    text = str(name or "")
    flags = _flag_country_codes(text)
    if flags:
        return flags[0]
    lowered = text.lower()
    for code, tokens in COUNTRY_TOKENS.items():
        for token in tokens:
            if len(token) <= 3:
                pattern = rf"(?<![a-zа-яё0-9]){re.escape(token)}(?![a-zа-яё0-9])"
                if re.search(pattern, lowered):
                    return code
            elif token in lowered:
                return code
    for match in _ISO_CODE_RE.finditer(text):
        code = match.group(1)
        if code in _ISO_3166_CODES:
            return code
    return ""


def group_by_country(tags):
    """Group tags by detected country preserving input order.

    Returns an ordered dict {country_code_or_empty: [tags]}.
    """
    groups = {}
    for tag in tags:
        cc = detect_country(tag)
        groups.setdefault(cc, []).append(tag)
    return groups


def dominant_country_group(groups):
    """Pick the largest country group (first on tie). Returns its key or ''."""
    best_key = ""
    best_size = -1
    for key, members in groups.items():
        if key and len(members) > best_size:
            best_key = key
            best_size = len(members)
    return best_key


def dns_server_ips(servers):
    """Extract literal IP entries from a DNS server list (DoH/DoT URLs are skipped)."""
    ips = []
    for entry in servers or []:
        text = str(entry or "").strip()
        if not text:
            continue
        if "://" in text:
            continue
        if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", text) or ":" in text:
            if text not in ips:
                ips.append(text)
    return ips
