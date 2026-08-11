import uuid

from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import MutableHeaders

from logger import logger
from logging_utils import (
    get_request_id,
    reset_request_id,
    set_request_id,
)


# The legacy dashboard still uses inline event handlers and styles. Remove these
# allowances when that markup is migrated to static JavaScript and CSS.
ADMIN_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)
NON_HTML_CSP = "default-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
PUBLIC_HTML_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def json_error(message: str, status_code: int, detail: str | None = None) -> JSONResponse:
    content = {"error": message, "request_id": get_request_id()}
    if detail:
        content["detail"] = detail
    return JSONResponse(status_code=status_code, content=content)


def plain_error(message: str, status_code: int) -> PlainTextResponse:
    return PlainTextResponse(
        f"{message}. Request ID: {get_request_id()}", status_code=status_code
    )


def _apply_security_headers(path: str, status_code: int, headers: MutableHeaders) -> None:
    for name, value in SECURITY_HEADERS.items():
        headers.setdefault(name, value)

    content_type = headers.get("content-type", "").lower()
    is_html = "text/html" in content_type
    is_admin = path == "/admin" or path.startswith("/admin/")
    if is_admin:
        policy = ADMIN_CSP
    elif is_html:
        policy = PUBLIC_HTML_CSP
    else:
        policy = NON_HTML_CSP
    headers.setdefault("Content-Security-Policy", policy)

    if is_admin or (path.startswith("/sub/") and is_html) or status_code >= 400:
        headers["Cache-Control"] = "no-store"


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        token = set_request_id(request_id)
        response_started = False

        async def send_with_context(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                _apply_security_headers(
                    scope.get("path", ""), message["status"], headers
                )
                logger.info(
                    "HTTP request completed method=%s status=%s",
                    scope.get("method", "unknown"),
                    message["status"],
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        except Exception as exc:
            if response_started:
                raise
            logger.exception(
                "Unhandled HTTP request failure method=%s error_type=%s",
                scope.get("method", "unknown"),
                type(exc).__name__,
            )
            if scope.get("path", "").startswith("/admin"):
                response = plain_error("Operation failed", 500)
            else:
                response = json_error("Internal server error", 500)
            await response(scope, receive, send_with_context)
        finally:
            reset_request_id(token)
