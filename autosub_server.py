#!/usr/bin/env python3
import base64
import ipaddress
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, Depends, Form, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

from config import (
    BACKUP_DIR,
    CONFIG_PATH,
    DB_PATH,
    VERSION,
    ensure_app_dir,
    env_get,
    load_config,
)
from logger import logger
from logging_utils import fingerprint_secret
from http_security import RequestContextMiddleware, json_error, plain_error
from csrf import create_csrf_manager
from storage import Storage
from http_clients import HttpClientManager
from subscription_cache import SubscriptionCache
from rate_limiter import ClientIpResolver, RateLimiter, RateLimitPolicy
from subscription_representation import (
    SubscriptionRepresentation,
    UnsupportedSubscriptionFormat,
    render_subscription_page,
    resolve_wire_format,
    select_subscription_representation,
    strip_format_query,
    subscription_css_path,
)
from client_profiles import UnknownClientError, resolve_client_profile
from builder import build_for_subscription, discover_nodes_from_sub_id, resolve_security_flags
from dashboard import (
    render_admin,
    render_api_test,
    render_debug,
    render_preview,
    save_admin_form,
)

storage = Storage(DB_PATH, backup_dir=BACKUP_DIR)

security = HTTPBasic(auto_error=False)

DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,::1/128"
PUBLIC_RATE_LIMIT = RateLimitPolicy("public", limit=60, window=60.0)
ADMIN_RATE_LIMIT = RateLimitPolicy("admin-auth", limit=20, window=60.0)
EXPENSIVE_ADMIN_RATE_LIMIT = RateLimitPolicy(
    "admin-expensive", limit=10, window=60.0
)


class AdminSecurityConfigError(RuntimeError):
    """Raised when the admin dashboard would start with unsafe authentication."""


def _is_loopback_bind_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_admin_security_config(host: str, password: str) -> None:
    """Allow passwordless admin access only on an explicit loopback bind."""
    if str(password or "").strip():
        return
    if _is_loopback_bind_host(host):
        return
    raise AdminSecurityConfigError(
        "Empty AUTOSUB_ADMIN_PASSWORD is allowed only with a loopback AUTOSUB_HOST"
    )


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    configured_username = str(env_get("AUTOSUB_ADMIN_USERNAME", "admin") or "")
    configured_password = str(env_get("AUTOSUB_ADMIN_PASSWORD", "") or "")
    if not configured_password.strip():
        return True
    if credentials:
        username_matches = secrets.compare_digest(
            str(credentials.username).encode("utf-8"),
            configured_username.encode("utf-8"),
        )
        password_matches = secrets.compare_digest(
            str(credentials.password).encode("utf-8"),
            configured_password.encode("utf-8"),
        )
        if username_matches and password_matches:
            return True
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": 'Basic realm="AutoSub Admin"'},
    )


class RateLimitExceeded(RuntimeError):
    def __init__(self, policy, retry_after):
        super().__init__("rate limit exceeded")
        self.policy = policy
        self.retry_after = retry_after


def _client_ip(request: Request) -> str:
    cached = getattr(request.state, "rate_limit_client_ip", None)
    if cached is not None:
        return cached
    scope_app = request.scope.get("app")
    app_state = getattr(scope_app, "state", None)
    resolver = getattr(app_state, "client_ip_resolver", None)
    if resolver is None:
        resolver = ClientIpResolver(
            env_get("AUTOSUB_TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES)
        )
    peer = request.client.host if request.client else ""
    resolved = resolver.resolve(
        peer,
        request.headers.get("X-Forwarded-For", ""),
        request.headers.get("X-Real-IP", ""),
    )
    if resolved.malformed_peer:
        logger.warning("Malformed direct client address")
    elif resolved.malformed_forwarded:
        logger.warning("Malformed forwarded client address")
    request.state.rate_limit_client_ip = resolved.ip
    return resolved.ip


async def _enforce_rate_limit(request, policy):
    client_ip = _client_ip(request)
    decision = await request.app.state.rate_limiter.check(client_ip, policy)
    if decision.allowed:
        return client_ip
    logger.warning(
        "Rate limit exceeded policy=%s client_hash=%s",
        policy.name,
        fingerprint_secret(client_ip),
    )
    raise RateLimitExceeded(policy.name, decision.retry_after)


async def enforce_admin_access(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    await _enforce_rate_limit(request, ADMIN_RATE_LIMIT)
    return verify_admin(credentials)


async def enforce_expensive_admin_access(
    request: Request,
    _authenticated: bool = Depends(enforce_admin_access),
):
    await _enforce_rate_limit(request, EXPENSIVE_ADMIN_RATE_LIMIT)
    return _authenticated


def _csrf_error(request: Request, token: object | None) -> Response | None:
    if request.app.state.csrf_manager.verify(token, scope="admin"):
        return None
    logger.warning("CSRF validation failed")
    return plain_error("CSRF validation failed", 403)


def _subscription_cache_variant():
    keys = (
        "XUI_SUB_URL",
        "XUI_API_URL",
        "XUI_URL",
        "XUI_USERNAME",
        "XUI_PASSWORD",
        "XUI_API_TOKEN",
        "XUI_TLS_VERIFY",
        "SUB_TITLE",
        "SUB_USERINFO",
    )
    return "\0".join(str(env_get(key, "") or "") for key in keys)


async def _invalidate_subscription_cache(request):
    await request.app.state.subscription_cache.invalidate()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ready = False
    admin_host = str(env_get("AUTOSUB_HOST", "127.0.0.1") or "")
    admin_password = str(env_get("AUTOSUB_ADMIN_PASSWORD", "") or "")
    validate_admin_security_config(admin_host, admin_password)
    client_ip_resolver = ClientIpResolver(
        env_get("AUTOSUB_TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES)
    )
    csrf_manager, generated_csrf_secret = create_csrf_manager(
        admin_host,
        env_get("AUTOSUB_SECRET_KEY", ""),
        is_loopback=_is_loopback_bind_host,
    )
    app.state.csrf_manager = csrf_manager
    if generated_csrf_secret:
        logger.warning(
            "AUTOSUB_SECRET_KEY is not configured; using a temporary process secret on loopback bind"
        )
    ensure_app_dir()
    await storage.connect()
    http_clients = HttpClientManager(env_getter=env_get)
    subscription_cache = SubscriptionCache()
    rate_limiter = RateLimiter()
    app.state.http_clients = http_clients
    app.state.subscription_cache = subscription_cache
    app.state.client_ip_resolver = client_ip_resolver
    app.state.rate_limiter = rate_limiter
    try:
        await http_clients.start()
        if CONFIG_PATH.exists():
            cfg = load_config()
            migrated = await storage.migrate_from_config(cfg)
            if migrated:
                logger.info("config.json migrated to SQLite")
        else:
            logger.warning(f"config.json not found at {CONFIG_PATH}, starting fresh")
        app.state.ready = True
        logger.info(f"AutoSub Server v{VERSION} started")
        logger.info(f"DB: {DB_PATH}")
        if admin_password.strip():
            logger.info("Admin dashboard: Basic Auth enabled")
        else:
            logger.warning("Admin dashboard: passwordless access enabled on loopback bind")
        yield
    finally:
        app.state.ready = False
        try:
            await subscription_cache.close()
        finally:
            try:
                await http_clients.close()
            finally:
                await storage.close()
        logger.info("AutoSub Server stopped")


app = FastAPI(lifespan=lifespan)
app.state.ready = False
app.add_middleware(RequestContextMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_error(request: Request, error: RateLimitExceeded):
    if request.url.path == "/admin" or request.url.path.startswith("/admin/"):
        response = plain_error("Too Many Requests", 429)
    else:
        response = json_error(
            "Too Many Requests",
            429,
            detail="Rate limit exceeded. Please try again later.",
        )
    response.headers["Retry-After"] = str(error.retry_after)
    response.headers["Cache-Control"] = "no-store"
    return response

# Mount static files securely
static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/admin", status_code=307)


@app.get("/health")
async def health():
    return PlainTextResponse(f"AutoSub Server v{VERSION} OK")


@app.get("/health/live")
async def liveness():
    return JSONResponse({"status": "alive"})


@app.get("/health/ready")
async def readiness(request: Request):
    if request.app.state.ready:
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


@app.get("/sub/_assets/subscription.css", include_in_schema=False)
async def subscription_stylesheet():
    return FileResponse(
        subscription_css_path,
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _get_cached_subscription(request, sub_id, query, wire_format="xray", client_id="generic"):
    logger.info(
        "Subscription request sub_id_hash=%s client=%s wire_format=%s",
        fingerprint_secret(sub_id),
        client_id,
        wire_format,
    )
    subscription_cache = request.app.state.subscription_cache
    cache_key = await subscription_cache.make_key(
        sub_id,
        query,
        variant=f"{_subscription_cache_variant()}:{wire_format}",
    )

    async def build_subscription():
        kwargs = {
            "query": query,
            "http_manager": request.app.state.http_clients,
        }
        if wire_format != "xray":
            kwargs["out_format"] = wire_format
        return await build_for_subscription(sub_id, storage, **kwargs)

    return await subscription_cache.get_or_build(cache_key, build_subscription)


@app.get("/json/{sub_id}")
@app.get("/sub/{sub_id}")
async def handle_json_route(sub_id: str, request: Request):
    await _enforce_rate_limit(request, PUBLIC_RATE_LIMIT)

    is_json_route = request.url.path.startswith("/json/")
    try:
        representation = select_subscription_representation(
            is_json_route=is_json_route,
            format_values=request.query_params.getlist("format"),
            accept=request.headers.get("accept", ""),
            user_agent=request.headers.get("user-agent", ""),
        )
    except UnsupportedSubscriptionFormat:
        return json_error("Unsupported subscription format", 400)

    try:
        client_profile = resolve_client_profile(
            client_values=request.query_params.getlist("client"),
            user_agent=request.headers.get("user-agent", ""),
        )
    except UnknownClientError:
        return json_error("Unsupported client", 400)

    wire_format = resolve_wire_format(
        is_json_route=is_json_route,
        format_values=request.query_params.getlist("format"),
        user_agent=request.headers.get("user-agent", ""),
    )

    query = request.url.query
    if not is_json_route:
        query = strip_format_query(query)

    if representation is SubscriptionRepresentation.HTML:
        try:
            await _get_cached_subscription(request, sub_id, query)
        except Exception:
            logger.exception(
                "Subscription landing validation failed sub_id_hash=%s",
                fingerprint_secret(sub_id),
            )
            return render_subscription_page(request, sub_id, error=True, status_code=502)
        return render_subscription_page(request, sub_id)

    try:
        output, ctype, sub_headers = await _get_cached_subscription(
            request, sub_id, query, wire_format, client_profile.id
        )
        
        SKIP_HEADERS = {
            "content-length",
            "content-encoding",
            "transfer-encoding",
            "connection",
            "keep-alive",
            "server",
            "date",
            "content-type",
        }
        headers = {"Cache-Control": "no-store"}
        for key, val in sub_headers.items():
            if not val or key.lower() in SKIP_HEADERS:
                continue
            header_val = str(val)
            if key.lower() == "profile-title" and not header_val.startswith("base64:"):
                try:
                    header_val.encode("ascii")
                except UnicodeEncodeError:
                    header_val = f"base64:{base64.b64encode(header_val.encode('utf-8')).decode('ascii')}"
            headers[key] = header_val
            
        if ctype.startswith(("application/json", "text/yaml", "text/plain")):
            media_type = ctype
        else:
            media_type = "application/json; charset=utf-8"
        sec_flags = await resolve_security_flags(
            sub_id, storage, http_manager=request.app.state.http_clients
        )
        for key in list(headers):
            if key.lower() == "hide-settings":
                headers.pop(key)
        if sec_flags.get("hide_settings"):
            headers["hide-settings"] = "1"

        return Response(content=output.encode("utf-8"), media_type=media_type, headers=headers)
    except Exception:
        logger.exception(
            "Subscription generation failed sub_id_hash=%s",
            fingerprint_secret(sub_id),
        )
        return json_error("Internal server error", 500)


@app.get("/admin", dependencies=[Depends(enforce_admin_access)])
async def admin_page(request: Request):
    csrf_token = request.app.state.csrf_manager.generate()
    return await render_admin(
        request,
        storage,
        csrf_token=csrf_token,
        client_manager=request.app.state.http_clients,
    )


@app.get("/admin/preview", dependencies=[Depends(enforce_expensive_admin_access)])
async def admin_preview(request: Request, sub_id: str = ""):
    sub_id = sub_id.strip()
    if not sub_id:
        return RedirectResponse(url="/admin", status_code=303)
    try:
        return await render_preview(
            request, storage, sub_id, request.app.state.http_clients
        )
    except Exception:
        logger.exception("Admin preview generation failed")
        return plain_error("Preview generation failed", 500)


@app.get("/admin/api-test", dependencies=[Depends(enforce_expensive_admin_access)])
async def admin_api_test(request: Request):
    content = await render_api_test(request.app.state.http_clients)
    return Response(content=content, media_type="application/json; charset=utf-8")


@app.get("/admin/debug", dependencies=[Depends(enforce_admin_access)])
async def admin_debug(request: Request, sub_id: str = ""):
    sub_id = sub_id.strip()
    if not sub_id:
        return json_error("sub_id is required", 400)
    try:
        content = await render_debug(storage, sub_id, request.app.state.http_clients)
        return Response(content=content, media_type="application/json; charset=utf-8")
    except Exception:
        logger.exception("Admin debug generation failed")
        return json_error("Debug generation failed", 500)


@app.post("/admin/save", dependencies=[Depends(enforce_admin_access)])
async def admin_save(request: Request):
    form = await request.form()
    form_data = dict(form)
    
    # We must properly extract multiple values if they are present, 
    # but the current architecture of dashboard.py uses get() extensively on dicts.
    # FastAPI's form() returns a FormData object which has getlist().
    # Let's convert it to a standard dict with list values for multiples, or just pass it since the old read_form did exactly that.
    
    parsed = {}
    for key, values in form.multi_items():
        if key in parsed:
            if isinstance(parsed[key], list):
                parsed[key].append(values)
            else:
                parsed[key] = [parsed[key], values]
        else:
            parsed[key] = values
            
    csrf = parsed.get("_csrf", "")
    if isinstance(csrf, list):
        csrf = csrf[0]
        
    csrf_error = _csrf_error(request, csrf)
    if csrf_error:
        return csrf_error
    try:
        await save_admin_form(storage, parsed)
        await _invalidate_subscription_cache(request)
        return RedirectResponse(url="/admin?msg=Настройки+успешно+сохранены", status_code=303)
    except ValueError:
        logger.warning("Admin settings validation failed")
        return plain_error("Invalid settings", 400)
    except Exception:
        logger.exception("Admin settings save failed")
        return plain_error("Settings save failed", 500)


@app.post("/admin/discover", dependencies=[Depends(enforce_expensive_admin_access)])
async def admin_discover(request: Request, sub_id: str = Form(""), csrf: str = Form("", alias="_csrf")):
    csrf_error = _csrf_error(request, csrf)
    if csrf_error:
        return csrf_error
    sub_id = sub_id.strip()
    if not sub_id:
        return RedirectResponse(url="/admin", status_code=303)
    try:
        nodes = await discover_nodes_from_sub_id(
            sub_id, request.app.state.http_clients
        )
        await storage.set_node_catalog(nodes)
        await _invalidate_subscription_cache(request)
        return RedirectResponse(url=f"/admin?msg=Каталог+успешно+обновлен:+{len(nodes)}+нод", status_code=303)
    except Exception:
        logger.exception("Admin node discovery failed")
        return plain_error("Node discovery failed", 500)


@app.post("/admin/set-client-group", dependencies=[Depends(enforce_admin_access)])
async def admin_set_client_group(request: Request, csrf: str = Form("", alias="_csrf"), sub_id: str = Form(""), email: str = Form(""), groups: str = Form("")):
    csrf_error = _csrf_error(request, csrf)
    if csrf_error:
        return csrf_error
    sub_id = sub_id.strip()
    if sub_id:
        await storage.set_client_groups(sub_id, email.strip(), groups.strip())
        await _invalidate_subscription_cache(request)
        return RedirectResponse(url="/admin?msg=Группа+клиента+успешно+обновлена", status_code=303)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-client-group", dependencies=[Depends(enforce_admin_access)])
async def admin_delete_client_group(request: Request, csrf: str = Form("", alias="_csrf"), sub_id: str = Form("")):
    csrf_error = _csrf_error(request, csrf)
    if csrf_error:
        return csrf_error
    sub_id = sub_id.strip()
    if sub_id:
        await storage.delete_client_groups(sub_id)
        await _invalidate_subscription_cache(request)
        return RedirectResponse(url="/admin?msg=Назначение+клиента+удалено", status_code=303)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/add-autoselect", dependencies=[Depends(enforce_admin_access)])
async def admin_add_autoselect(
    request: Request,
    csrf: str = Form("", alias="_csrf"),
    autoselect_id: str = Form(""),
    name: str = Form(""),
    strategy: str = Form("leastPing"),
):
    csrf_error = _csrf_error(request, csrf)
    if csrf_error:
        return csrf_error
    autoselect_id = autoselect_id.strip()
    name = name.strip()
    if strategy not in ("leastPing", "leastLoad"):
        strategy = "leastPing"
    if autoselect_id and name:
        try:
            await storage.add_autoselect(autoselect_id, name, strategy=strategy)
            await _invalidate_subscription_cache(request)
            return RedirectResponse(
                url="/admin?msg=Балансировщик+успешно+создан", status_code=303
            )
        except Exception:
            logger.exception("Admin autoselect creation failed")
            return plain_error("Autoselect creation failed", 500)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-autoselect", dependencies=[Depends(enforce_admin_access)])
async def admin_delete_autoselect(
    request: Request,
    csrf: str = Form("", alias="_csrf"),
    autoselect_id: str = Form(""),
):
    csrf_error = _csrf_error(request, csrf)
    if csrf_error:
        return csrf_error
    autoselect_id = autoselect_id.strip()
    if autoselect_id:
        try:
            await storage.delete_autoselect(autoselect_id)
            await _invalidate_subscription_cache(request)
            return RedirectResponse(url="/admin?msg=Балансировщик+удален", status_code=303)
        except Exception:
            logger.exception("Admin autoselect deletion failed")
            return plain_error("Autoselect deletion failed", 500)
    return RedirectResponse(url="/admin", status_code=303)


def main():
    host = env_get("AUTOSUB_HOST", "127.0.0.1")
    port = int(env_get("AUTOSUB_PORT", "25500"))
    uvicorn.run(
        "autosub_server:app",
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        server_header=False,
    )

if __name__ == "__main__":
    main()
