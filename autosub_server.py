#!/usr/bin/env python3
import base64
import os
import secrets
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

from config import APP_DIR, CONFIG_PATH, DB_PATH, ensure_app_dir, env_get, load_config
from logger import logger
from storage import Storage
from builder import build_for_subscription, discover_nodes_from_sub_id
from dashboard import (
    render_admin,
    render_api_test,
    render_debug,
    render_preview,
    save_admin_form,
)

storage = Storage(DB_PATH)

# CSRF token store
_csrf_tokens = set()
_CSRF_TOKEN_MAX = 200

def _generate_csrf_token():
    token = secrets.token_hex(24)
    if len(_csrf_tokens) >= _CSRF_TOKEN_MAX:
        _csrf_tokens.clear()
    _csrf_tokens.add(token)
    return token

def _validate_csrf_token(token: str):
    if token in _csrf_tokens:
        _csrf_tokens.discard(token)
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
    logger.info(f"AutoSub v3 started")
    logger.info(f"DB: {DB_PATH}")
    admin_password = env_get("AUTOSUB_ADMIN_PASSWORD", "")
    if admin_password:
        logger.info("Admin dashboard: Basic Auth enabled")
    else:
        logger.warning("Admin dashboard: NO PASSWORD SET — anyone with port access can modify settings")
        
    yield
    
    await storage.close()
    logger.info("AutoSub v3 stopped")


app = FastAPI(lifespan=lifespan)

# Mount static files securely
static_dir = APP_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/")
@app.get("/health")
async def health():
    return PlainTextResponse("AutoSub v3 OK")


@app.get("/json/{sub_id}")
async def handle_json_route(sub_id: str, request: Request):
    try:
        query = request.url.query
        output, ctype, sub_headers = await build_for_subscription(sub_id, storage, query=query)
        
        headers = {"Cache-Control": "no-store"}
        for key, val in sub_headers.items():
            if not val:
                continue
            header_val = str(val)
            if key.lower() == "profile-title" and not header_val.startswith("base64:"):
                try:
                    header_val.encode("ascii")
                except UnicodeEncodeError:
                    header_val = f"base64:{base64.b64encode(header_val.encode('utf-8')).decode('ascii')}"
            headers[key] = header_val
            
        media_type = ctype if "json" in ctype else "application/json; charset=utf-8"
        return Response(content=output.encode("utf-8"), media_type=media_type, headers=headers)
    except Exception as e:
        logger.error(f"Error in JSON route: {e}\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/admin", dependencies=[Depends(verify_admin)])
async def admin_page():
    csrf_token = _generate_csrf_token()
    html_content = await render_admin(storage, csrf_token=csrf_token)
    return HTMLResponse(content=html_content)


@app.get("/admin/preview", dependencies=[Depends(verify_admin)])
async def admin_preview(sub_id: str = ""):
    sub_id = sub_id.strip()
    if not sub_id:
        return RedirectResponse(url="/admin", status_code=303)
    try:
        html_content = await render_preview(storage, sub_id)
        return HTMLResponse(content=html_content)
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
        html_content = await render_admin(storage, "Настройки сохранены", csrf_token=csrf_token)
        return HTMLResponse(content=html_content)
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
        html_content = await render_admin(storage, f"Каталог обновлен: {len(nodes)} нод", csrf_token=csrf_token)
        return HTMLResponse(content=html_content)
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


def main():
    host = env_get("AUTOSUB_HOST", "127.0.0.1")
    port = int(env_get("AUTOSUB_PORT", "25500"))
    uvicorn.run("autosub_server:app", host=host, port=port, log_level="info")

if __name__ == "__main__":
    main()
