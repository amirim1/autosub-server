#!/usr/bin/env python3
import base64
import os
import secrets
import time
import traceback
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

from config import APP_DIR, CONFIG_PATH, DB_PATH, ensure_app_dir, env_get, load_config, VERSION
from logger import logger
from storage import Storage
from api_client import fetch_original_sub_html
from builder import build_for_subscription, discover_nodes_from_sub_id, resolve_security_flags
from dashboard import (
    render_admin,
    render_api_test,
    render_debug,
    render_preview,
    save_admin_form,
)

storage = Storage(DB_PATH)

# CSRF token store (token: expiry_time)
_csrf_tokens = {}
_CSRF_TOKEN_MAX = 500
_CSRF_TOKEN_TTL = 3600  # 1 hour

def _generate_csrf_token():
    now = time.time()
    # Cleanup expired
    expired = [k for k, v in _csrf_tokens.items() if v < now]
    for k in expired:
        _csrf_tokens.pop(k, None)
        
    token = secrets.token_hex(24)
    if len(_csrf_tokens) >= _CSRF_TOKEN_MAX:
        # If still too large, remove oldest
        if _csrf_tokens:
            oldest = min(_csrf_tokens.items(), key=lambda x: x[1])[0]
            _csrf_tokens.pop(oldest, None)
            
    _csrf_tokens[token] = now + _CSRF_TOKEN_TTL
    return token

def _validate_csrf_token(token: str):
    now = time.time()
    if token in _csrf_tokens:
        expiry = _csrf_tokens.pop(token)
        if expiry >= now:
            return True
    return False

security = HTTPBasic(auto_error=False)

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    password = env_get("AUTOSUB_ADMIN_PASSWORD", "")
    if not password:
        return True
    if credentials:
        if secrets.compare_digest(credentials.password, password):
            return True
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": 'Basic realm="AutoSub Admin"'},
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_app_dir()
    await storage.connect()
    if CONFIG_PATH.exists():
        cfg = load_config()
        migrated = await storage.migrate_from_config(cfg)
        if migrated:
            logger.info("config.json migrated to SQLite")
    else:
        logger.warning(f"config.json not found at {CONFIG_PATH}, starting fresh")
    logger.info(f"AutoSub Server v{VERSION} started")
    logger.info(f"DB: {DB_PATH}")
    admin_password = env_get("AUTOSUB_ADMIN_PASSWORD", "")
    if admin_password:
        logger.info("Admin dashboard: Basic Auth enabled")
    else:
        logger.warning("Admin dashboard: NO PASSWORD SET — anyone with port access can modify settings")
        
    yield
    
    await storage.close()
    logger.info("AutoSub Server stopped")


app = FastAPI(lifespan=lifespan)

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
    # Extract client IP (handle Nginx reverse proxy headers)
    client_ip = (
        request.headers.get("X-Real-IP")
        or request.headers.get("X-Forwarded-For")
        or (request.client.host if request.client else "")
    )
    if client_ip:
        client_ip = client_ip.split(",")[0].strip()

    if not _check_rate_limit(client_ip):
        logger.warning(f"Rate limit exceeded for IP: {client_ip} on sub_id: {sub_id}")
        return JSONResponse(
            status_code=429,
            content={"error": "Too Many Requests", "detail": "Rate limit exceeded. Please try again later."},
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
            html_content, ctype, status_code = await fetch_original_sub_html(sub_id, request.headers)
            return HTMLResponse(content=html_content, status_code=status_code)
        except Exception as err:
            logger.warning(f"Failed to proxy HTML landing page from 3x-ui for sub_id {sub_id}: {err}")

    try:
        query = request.url.query
        output, ctype, sub_headers = await build_for_subscription(sub_id, storage, query=query)
        
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
        sec_flags = await resolve_security_flags(sub_id, storage)
        if sec_flags.get("hide_settings"):
            headers["Hide-Settings"] = "true"
            headers["hide-settings"] = "true"
            headers["Hide-User-Info"] = "true"
            headers["hide-user-info"] = "true"

        return Response(content=output.encode("utf-8"), media_type=media_type, headers=headers)
    except Exception as e:
        logger.error(f"Error in JSON route: {e}\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": "Internal server error"})


@app.get("/admin", dependencies=[Depends(verify_admin)])
async def admin_page(request: Request):
    csrf_token = _generate_csrf_token()
    return await render_admin(request, storage, csrf_token=csrf_token)


@app.get("/admin/preview", dependencies=[Depends(verify_admin)])
async def admin_preview(request: Request, sub_id: str = ""):
    sub_id = sub_id.strip()
    if not sub_id:
        return RedirectResponse(url="/admin", status_code=303)
    try:
        return await render_preview(request, storage, sub_id)
    except Exception as exc:
        return PlainTextResponse(f"preview failed: {exc}", status_code=500)


@app.get("/admin/api-test", dependencies=[Depends(verify_admin)])
async def admin_api_test():
    content = await render_api_test()
    return Response(content=content, media_type="application/json; charset=utf-8")


@app.get("/admin/debug", dependencies=[Depends(verify_admin)])
async def admin_debug(sub_id: str = ""):
    sub_id = sub_id.strip()
    if not sub_id:
        return JSONResponse(status_code=400, content={"error": "sub_id is required"})
    content = await render_debug(storage, sub_id)
    return Response(content=content, media_type="application/json; charset=utf-8")


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
        
    if not _validate_csrf_token(csrf):
        return PlainTextResponse("CSRF token invalid or expired. Please reload the page.", status_code=403)
    try:
        await save_admin_form(storage, parsed)
        csrf_token = _generate_csrf_token()
        return await render_admin(request, storage, "Настройки сохранены", csrf_token=csrf_token)
    except Exception as exc:
        return PlainTextResponse(f"save failed: {exc}", status_code=500)


@app.post("/admin/discover", dependencies=[Depends(verify_admin)])
async def admin_discover(request: Request, sub_id: str = Form(""), csrf: str = Form("", alias="_csrf")):
    if not _validate_csrf_token(csrf):
        return PlainTextResponse("CSRF token invalid or expired. Please reload the page.", status_code=403)
    sub_id = sub_id.strip()
    if not sub_id:
        return RedirectResponse(url="/admin", status_code=303)
    try:
        nodes = await discover_nodes_from_sub_id(sub_id)
        await storage.set_node_catalog(nodes)
        csrf_token = _generate_csrf_token()
        return await render_admin(request, storage, f"Каталог обновлен: {len(nodes)} нод", csrf_token=csrf_token)
    except Exception as exc:
        return PlainTextResponse(f"discovery failed: {exc}", status_code=500)


@app.post("/admin/set-client-group", dependencies=[Depends(verify_admin)])
async def admin_set_client_group(csrf: str = Form("", alias="_csrf"), sub_id: str = Form(""), email: str = Form(""), groups: str = Form("")):
    if not _validate_csrf_token(csrf):
        return PlainTextResponse("CSRF token invalid or expired. Please reload the page.", status_code=403)
    sub_id = sub_id.strip()
    if sub_id:
        await storage.set_client_groups(sub_id, email.strip(), groups.strip())
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-client-group", dependencies=[Depends(verify_admin)])
async def admin_delete_client_group(csrf: str = Form("", alias="_csrf"), sub_id: str = Form("")):
    if not _validate_csrf_token(csrf):
        return PlainTextResponse("CSRF token invalid or expired. Please reload the page.", status_code=403)
    sub_id = sub_id.strip()
    if sub_id:
        await storage.delete_client_groups(sub_id)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/add-autoselect", dependencies=[Depends(verify_admin)])
async def admin_add_autoselect(
    csrf: str = Form("", alias="_csrf"),
    autoselect_id: str = Form(""),
    name: str = Form(""),
):
    if not _validate_csrf_token(csrf):
        return PlainTextResponse("CSRF token invalid or expired. Please reload the page.", status_code=403)
    autoselect_id = autoselect_id.strip()
    name = name.strip()
    if autoselect_id and name:
        try:
            await storage.add_autoselect(autoselect_id, name)
        except Exception as exc:
            logger.error(f"Failed to add autoselect profile: {exc}")
            return PlainTextResponse(f"add autoselect failed: {exc}", status_code=500)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-autoselect", dependencies=[Depends(verify_admin)])
async def admin_delete_autoselect(
    csrf: str = Form("", alias="_csrf"),
    autoselect_id: str = Form(""),
):
    if not _validate_csrf_token(csrf):
        return PlainTextResponse("CSRF token invalid or expired. Please reload the page.", status_code=403)
    autoselect_id = autoselect_id.strip()
    if autoselect_id:
        try:
            await storage.delete_autoselect(autoselect_id)
        except Exception as exc:
            logger.error(f"Failed to delete autoselect profile: {exc}")
            return PlainTextResponse(f"delete autoselect failed: {exc}", status_code=500)
    return RedirectResponse(url="/admin", status_code=303)


def main():
    host = env_get("AUTOSUB_HOST", "127.0.0.1")
    port = int(env_get("AUTOSUB_PORT", "25500"))
    uvicorn.run("autosub_server:app", host=host, port=port, log_level="info")

if __name__ == "__main__":
    main()
