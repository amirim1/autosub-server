import hashlib
import logging
import re
from contextvars import ContextVar, Token
from urllib.parse import urlsplit


_request_id: ContextVar[str] = ContextVar("autosub_request_id", default="-")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])([\w.+-]+)@([\w.-]+\.[A-Za-z]{2,})(?![\w.-])",
    re.UNICODE,
)
_AUTH_RE = re.compile(
    r"(?i)\bauthorization\s*:\s*(?:basic|bearer)\s+[^\s,;]+"
)
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|api[_-]?token|token)\b(\s*[:=]\s*)([^\s,;&]+)"
)


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str) -> Token[str]:
    return _request_id.set(value)


def reset_request_id(token: Token[str]) -> None:
    _request_id.reset(token)


def _safe_text(value: object | None) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def fingerprint_secret(value: object | None) -> str:
    """Return a stable, irreversible short reference for a sensitive value."""
    text = _safe_text(value)
    if not text:
        return "<empty>"
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest[:12]}"


def mask_email(value: object | None) -> str:
    """Mask a valid email while retaining only a small operator hint."""
    text = _safe_text(value).strip()
    if not text or text.count("@") != 1:
        return "<redacted>"
    local, domain = text.rsplit("@", 1)
    if not local or not domain or "." not in domain or any(ch.isspace() for ch in text):
        return "<redacted>"
    masked_local = "*" if len(local) == 1 else f"{local[0]}***"
    return f"{masked_local}@{domain}"


def sanitize_url(value: object | None) -> str:
    """Reduce an HTTP(S) URL to its scheme and hostname."""
    text = _safe_text(value).strip()
    try:
        parsed = urlsplit(text)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return "<redacted>"
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = f"[{hostname}]"
        return f"{parsed.scheme.lower()}://{hostname}"
    except Exception:
        return "<redacted>"


def sanitize_log_message(value: object | None) -> str:
    """Redact project-specific credentials and identifiers from log text."""
    text = _safe_text(value)
    if not text:
        return ""
    text = _AUTH_RE.sub("Authorization: <redacted>", text)
    text = _URL_RE.sub(lambda match: sanitize_url(match.group(0)), text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return _EMAIL_RE.sub(
        lambda match: mask_email(f"{match.group(1)}@{match.group(2)}"), text
    )


class RequestContextRedactionFilter(logging.Filter):
    """Attach request context and sanitize messages before any handler sees them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = "Unprintable log message"
        record.msg = sanitize_log_message(message)
        record.args = ()
        record.request_id = get_request_id()

        if record.exc_info and isinstance(record.exc_info, tuple):
            exc_type, exc_value, traceback = record.exc_info
            safe_value = RuntimeError(
                f"{getattr(exc_type, '__name__', type(exc_value).__name__)}: details redacted"
            )
            record.exc_info = (RuntimeError, safe_value, traceback)
            record.exc_text = None
        return True
