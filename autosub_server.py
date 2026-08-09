#!/usr/bin/env python3
import base64
import ipaddress
import os
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

from config import APP_DIR, CONFIG_PATH, DB_PATH, ensure_app_dir, env_get, load_config, VERSION
from logger import logger
from logging_utils import fingerprint_secret
from http_security import RequestContextMiddleware, json_error, plain_error
from csrf import create_csrf_manager
from storage import Storage
from api_client import fetch_original_sub_html
from http_clients import HttpClientManager
from builder import build_for_subscription, discover_nodes_from_sub_id, resolve_security_flags
from dashboard import (
    render_admin,
    render_api_test,
    render_debug,
    render_preview,
    save_admin_form,
)

storage = Storage(DB_PATH)

security = HTTPBasic(auto_error=False)


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


def _csrf_error(request: Request, token: object | None) -> Response | None:
    if request.app.state.csrf_manager.verify(token, scope="admin"):
        return None
    logger.warning("CSRF validation failed")
    return plain_error("CSRF validation failed", 403)

@asynccontextmanager
async def lifespan(app: FastAPI):
    admin_host = str(env_get("AUTOSUB_HOST", "127.0.0.1") or "")
    admin_password = str(env_get("AUTOSUB_ADMIN_PASSWORD", "") or "")
    validate_admin_security_config(admin_host, admin_password)
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
    app.state.http_clients = http_clients
    try:
        await http_clients.start()
        if CONFIG_PATH.exists():
            cfg = load_config()
            migrated = await storage.migrate_from_config(cfg)
            if migrated:
                logger.info("config.json migrated to SQLite")
        else:
            logger.warning(f"config.json not found at {CONFIG_PATH}, starting fresh")
        logger.info(f"AutoSub Server v{VERSION} started")
        logger.info(f"DB: {DB_PATH}")
        if admin_password.strip():
            logger.info("Admin dashboard: Basic Auth enabled")
        else:
            logger.warning("Admin dashboard: passwordless access enabled on loopback bind")
        yield
    finally:
        try:
            await http_clients.close()
        finally:
            await storage.close()
        logger.info("AutoSub Server stopped")


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)

# Mount static files securely
static_dir = APP_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/admin", status_code=307)


@app.get("/health")
async def health():
    return PlainTextResponse(f"AutoSub Server v{VERSION} OK")


# Rate limiting store (in-memory)
_ip_requests = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 30  # max requests per window
_last_ip_cleanup = 0

DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,::1/128"


def _parse_trusted_proxies(value: str):
    networks = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning(f"Ignoring invalid trusted proxy address/network: {item}")
    return networks


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        if peer:
            logger.warning(f"Ignoring invalid request peer address: {peer}")
        return "unknown"

    trusted = _parse_trusted_proxies(
        env_get("AUTOSUB_TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES)
    )
    if not any(peer_ip in network for network in trusted):
        return str(peer_ip)

    forwarded = []
    for item in request.headers.get("X-Forwarded-For", "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            forwarded.append(ipaddress.ip_address(item))
        except ValueError:
            logger.warning(f"Ignoring invalid X-Forwarded-For address: {item}")

    for candidate in reversed(forwarded):
        if not any(candidate in network for network in trusted):
            return str(candidate)

    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        try:
            return str(ipaddress.ip_address(real_ip))
        except ValueError:
            logger.warning(f"Ignoring invalid X-Real-IP address: {real_ip}")

    return str(peer_ip)


def _check_rate_limit(ip: str) -> bool:
    if not ip:
        return True
    now = time.time()
    
    global _last_ip_cleanup
    if now - _last_ip_cleanup > 300:  # Cleanup every 5 mins
        _last_ip_cleanup = now
        stale_ips = [k for k, times in _ip_requests.items() if not times or now - times[-1] > RATE_LIMIT_WINDOW]
        for k in stale_ips:
            _ip_requests.pop(k, None)

    # Remove timestamps older than window
    _ip_requests[ip] = [t for t in _ip_requests[ip] if now - t < RATE_LIMIT_WINDOW]
    if len(_ip_requests[ip]) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    _ip_requests[ip].append(now)
    return True


@app.get("/json/{sub_id}")
@app.get("/sub/{sub_id}")
async def handle_json_route(sub_id: str, request: Request):
    client_ip = _client_ip(request)

    if not _check_rate_limit(client_ip):
        logger.warning(
            "Rate limit exceeded for IP: %s sub_id_hash=%s",
            client_ip,
            fingerprint_secret(sub_id),
        )
        return json_error(
            "Too Many Requests",
            429,
            detail="Rate limit exceeded. Please try again later.",
        )

    # Detect if request is coming from a web browser (e.g., Chrome/Firefox opening /sub/ link)
    accept_header = request.headers.get("accept", "").lower()
    user_agent = request.headers.get("user-agent", "").lower()
    is_browser = "text/html" in accept_header or (
        "mozilla/" in user_agent
        and not any(client in user_agent for client in ["v2ray", "happ", "nekobox", "sing-box", "clash", "shadowrocket", "stash", "surge", "foxray", "streisand", "passwall", "openwrt"])
    )

    if is_browser and request.url.path.startswith("/sub/"):
        try:
            html_content, ctype, status_code = await fetch_original_sub_html(
                sub_id,
                request.headers,
                client_manager=request.app.state.http_clients,
            )
            return HTMLResponse(content=html_content, status_code=status_code)
        except Exception as err:
            logger.warning(
                "Failed to proxy upstream HTML sub_id_hash=%s error_type=%s",
                fingerprint_secret(sub_id),
                type(err).__name__,
            )

    try:
        query = request.url.query
        output, ctype, sub_headers = await build_for_subscription(
            sub_id,
            storage,
            query=query,
            http_manager=request.app.state.http_clients,
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
            
        media_type = ctype if "json" in ctype else "application/json; charset=utf-8"
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


@app.get("/admin", dependencies=[Depends(verify_admin)])
async def admin_page(request: Request):
    csrf_token = request.app.state.csrf_manager.generate()
    return await render_admin(
        request,
        storage,
        csrf_token=csrf_token,
        client_manager=request.app.state.http_clients,
    )


@app.get("/admin/preview", dependencies=[Depends(verify_admin)])
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


@app.get("/admin/api-test", dependencies=[Depends(verify_admin)])
async def admin_api_test(request: Request):
    content = await render_api_test(request.app.state.http_clients)
    return Response(content=content, media_type="application/json; charset=utf-8")


@app.get("/admin/debug", dependencies=[Depends(verify_admin)])
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


@app.post("/admin/save", dependencies=[Depends(verify_admin)])
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
        return RedirectResponse(url="/admin?msg=Настройки+успешно+сохранены", status_code=303)
    except Exception:
        logger.exception("Admin settings save failed")
        return plain_error("Settings save failed", 500)


@app.post("/admin/discover", dependencies=[Depends(verify_admin)])
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
        return RedirectResponse(url=f"/admin?msg=Каталог+успешно+обновлен:+{len(nodes)}+нод", status_code=303)
    except Exception:
        logger.exception("Admin node discovery failed")
        return plain_error("Node discovery failed", 500)


@app.post("/admin/set-client-group", dependencies=[Depends(verify_admin)])
async def admin_set_client_group(request: Request, csrf: str = Form("", alias="_csrf"), sub_id: str = Form(""), email: str = Form(""), groups: str = Form("")):
    csrf_error = _csrf_error(request, csrf)
    if csrf_error:
        return csrf_error
    sub_id = sub_id.strip()
    if sub_id:
        await storage.set_client_groups(sub_id, email.strip(), groups.strip())
        return RedirectResponse(url="/admin?msg=Группа+клиента+успешно+обновлена", status_code=303)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-client-group", dependencies=[Depends(verify_admin)])
async def admin_delete_client_group(request: Request, csrf: str = Form("", alias="_csrf"), sub_id: str = Form("")):
    csrf_error = _csrf_error(request, csrf)
    if csrf_error:
        return csrf_error
    sub_id = sub_id.strip()
    if sub_id:
        await storage.delete_client_groups(sub_id)
        return RedirectResponse(url="/admin?msg=Назначение+клиента+удалено", status_code=303)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/add-autoselect", dependencies=[Depends(verify_admin)])
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
            return RedirectResponse(url=f"/admin?msg=Балансировщик+{name}+успешно+создан", status_code=303)
        except Exception:
            logger.exception("Admin autoselect creation failed")
            return plain_error("Autoselect creation failed", 500)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-autoselect", dependencies=[Depends(verify_admin)])
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
