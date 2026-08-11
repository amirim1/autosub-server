import base64
import binascii
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable


CSRF_VERSION = "v1"
CSRF_TTL_SECONDS = 3600
CSRF_CLOCK_SKEW_SECONDS = 60
CSRF_MAX_TOKEN_BYTES = 2048
CSRF_MIN_SECRET_CHARS = 32
CSRF_DEFAULT_SCOPE = "admin"
_NONCE_BYTES = 18
_SCOPE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_B64_RE = re.compile(r"[A-Za-z0-9_-]+\Z")


class CsrfSecretConfigError(RuntimeError):
    """Raised when CSRF would use an unsafe configured secret."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or not _B64_RE.fullmatch(value):
        raise ValueError("invalid base64")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            (value + padding).encode("ascii"), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64") from exc


class CsrfTokenManager:
    def __init__(
        self,
        secret: str,
        *,
        ttl_seconds: int = CSRF_TTL_SECONDS,
        clock_skew_seconds: int = CSRF_CLOCK_SKEW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(secret, str) or len(secret) < CSRF_MIN_SECRET_CHARS:
            raise CsrfSecretConfigError("CSRF secret must contain at least 32 characters")
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds
        self._clock_skew_seconds = clock_skew_seconds
        self._clock = clock

    def _signature(self, payload: str) -> bytes:
        return hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest()

    def generate(self, scope: str = CSRF_DEFAULT_SCOPE) -> str:
        if not isinstance(scope, str) or not _SCOPE_RE.fullmatch(scope):
            raise ValueError("invalid CSRF scope")
        timestamp = str(int(self._clock()))
        nonce = _b64encode(secrets.token_bytes(_NONCE_BYTES))
        payload = f"{CSRF_VERSION}.{timestamp}.{nonce}.{scope}"
        return f"{payload}.{_b64encode(self._signature(payload))}"

    def verify(self, token: object | None, scope: str = CSRF_DEFAULT_SCOPE) -> bool:
        try:
            if not isinstance(token, str) or not isinstance(scope, str):
                return False
            if not _SCOPE_RE.fullmatch(scope):
                return False
            if not token or len(token.encode("utf-8")) > CSRF_MAX_TOKEN_BYTES:
                return False

            version, timestamp_text, nonce, token_scope, signature_text = token.split(".")
            if version != CSRF_VERSION or token_scope != scope:
                return False
            timestamp = int(timestamp_text)
            if timestamp < 0 or str(timestamp) != timestamp_text:
                return False

            nonce_bytes = _b64decode(nonce)
            signature = _b64decode(signature_text)
            if len(nonce_bytes) != _NONCE_BYTES or len(signature) != hashlib.sha256().digest_size:
                return False

            payload = f"{version}.{timestamp_text}.{nonce}.{token_scope}"
            expected = self._signature(payload)
            if not hmac.compare_digest(signature, expected):
                return False

            now = int(self._clock())
            if timestamp > now + self._clock_skew_seconds:
                return False
            return now - timestamp <= self._ttl_seconds
        except (AttributeError, UnicodeError, ValueError):
            return False


def create_csrf_manager(
    host: str,
    configured_secret: object | None,
    *,
    is_loopback: Callable[[str], bool],
    clock: Callable[[], float] = time.time,
) -> tuple[CsrfTokenManager, bool]:
    """Return a manager and whether a temporary process secret was generated."""
    secret = "" if configured_secret is None else str(configured_secret)
    if not secret.strip():
        if not is_loopback(host):
            raise CsrfSecretConfigError(
                "AUTOSUB_SECRET_KEY is required for a non-loopback AUTOSUB_HOST"
            )
        secret = secrets.token_urlsafe(48)
        return CsrfTokenManager(secret, clock=clock), True
    if len(secret) < CSRF_MIN_SECRET_CHARS:
        raise CsrfSecretConfigError(
            "AUTOSUB_SECRET_KEY must contain at least 32 characters"
        )
    return CsrfTokenManager(secret, clock=clock), False
