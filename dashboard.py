import html
import json
import re
import urllib.parse

from api_client import get_xui_api, extract_client_groups, fetch_original_subscription, normalize_subscription
from builder import (
    allowed_autoselect_ids,
    enrich_profiles,
    resolve_client,
    discover_nodes_from_sub_id,
)
from fingerprint import node_name, profile_node_id, node_summary


# --- Helpers ---

def esc(value):
    return html.escape(str(value or ""))


def page_head():
    return """<link rel="stylesheet" href="/static/dashboard.css">
<script src="/static/dashboard.js"></script>"""


async def api_clients_safe(limit=500):
    """Fetch clients from 3x-ui API. Returns (clients_list, error_string)."""
    try:
        api = get_xui_api()
        if not api.enabled():
            return [], "XUI credentials are not configured"
        
        db_clients = []
        try:
            db_clients = await api.clients_list()
        except Exception:
            pass
            
        group_map = {}
        try:
            group_map = await api.group_map()
        except Exception:
            pass
            
        client_groups_cache = {}
        for c in db_clients:
            c_sub_id = c.get("subId") or c.get("sub_id") or c.get("subscriptionId") or c.get("subscription_id")
            c_email = c.get("email")
            raw_grps = extract_client_groups(c)
            expanded_grps = api._expand_group_names(raw_grps, group_map)
            if c_sub_id:
                client_groups_cache[str(c_sub_id)] = expanded_grps
            if c_email:
                client_groups_cache[str(c_email)] = expanded_grps

        clients = []
        for client in await api.clients_from_inbounds():
            sub_id = client.get("subId") or client.get("sub_id") or client.get("subscriptionId") or client.get("id") or ""
            email = client.get("email") or ""
            
            grps = client_groups_cache.get(str(sub_id)) or client_groups_cache.get(email) or extract_client_groups(client)
            
            clients.append({
                "email": email,
                "sub_id": sub_id,
                "groups": grps,
                "inbound": client.get("_inbound_remark") or "",
            })
        return clients[:limit], ""
    except Exception as exc:
        return [], str(exc)


# --- Shared preview/debug logic ---

async def _resolve_preview_data(storage, sub_id):
    """
    Common logic for render_preview and render_debug.
    Returns dict with: original profiles, enriched, client, groups, allowed_ids, autoselect results.
    """
    original_text, _, _ = await fetch_original_subscription(sub_id)
    profiles = normalize_subscription(original_text)
    client = await resolve_client(sub_id, storage)
    groups = (client or {}).get("groups") or []
    group_rules = await storage.get_group_rules()
    allowed = allowed_autoselect_ids(groups, group_rules)
    autoselects = await storage.get_autoselects()
    by_id = {a.get("id"): a for a in autoselects if a.get("enabled", True)}
    enriched = enrich_profiles(profiles)

    autos_result = []
    for auto_id in allowed:
        auto = by_id.get(auto_id)
        if not auto:
            autos_result.append({"id": auto_id, "exists": False, "name": None, "matched": 0, "selected": [], "tag_filter": []})
            continue
        selected = list(auto.get("selected_node_ids") or [])
        tag_filter = auto.get("tag_filter") or []
        pool = enriched
        if tag_filter:
            tf = set(t.strip().lower() for t in tag_filter if t.strip())
            if tf and "*" not in tf:
                pool = [p for p in pool if str(p.get("_tag", "")).lower() in tf]
        selected_set = set(selected)
        if "*" in selected_set:
            matched = len(pool)
        else:
            matched = sum(
                1 for p in pool
                if p.get("_node_id") in selected_set or p.get("_canonical_id") in selected_set or p.get("_legacy_id") in selected_set
            )
        autos_result.append({
            "id": auto_id,
            "exists": True,
            "name": auto.get("name"),
            "matched": matched,
            "selected": selected,
            "tag_filter": tag_filter,
        })

    return {
        "profiles": profiles,
        "enriched": enriched,
        "client": client,
        "groups": groups,
        "group_rules": group_rules,
        "allowed": allowed,
        "autos": autos_result,
    }


# --- Renderers ---

async def render_admin(storage, message="", csrf_token=""):
    clients, clients_error = await api_clients_safe()

    catalog = await storage.get_node_catalog()
    node_rows = "".join(
        f"<tr><td><b>{esc(n.get('name'))}</b><br><span class='muted'>{esc(n.get('fingerprint'))}</span></td>"
        f"<td>{esc(n.get('protocol'))}</td><td>{esc(n.get('address'))}:{esc(n.get('port'))}</td>"
        f"<td>{esc(n.get('network'))}</td><td>{esc(n.get('security'))}</td>"
        f"<td>{esc(n.get('tag'))}</td></tr>"
        for n in catalog
    ) or "<tr><td colspan='6' class='muted'>Каталог пустой. Сделай discovery по подписке, где есть все нужные ноды.</td></tr>"

    overrides = await storage.get_client_group_overrides()
    local_groups_raw = await storage.get_all_client_groups()
    local_groups_map = {r.get('sub_id'): r.get('groups', []) for r in local_groups_raw if r.get('sub_id')}

    grouped_clients = {}
    if clients:
        for c in clients:
            key = c.get('sub_id') or c.get('email')
            if not key:
                continue
            if key not in grouped_clients:
                grouped_clients[key] = {
                    'email': c.get('email') or '',
                    'sub_id': c.get('sub_id') or '',
                    'groups': list(c.get('groups', [])),
                    'inbounds': [c.get('inbound')] if c.get('inbound') else []
                }
            else:
                if c.get('inbound') and c.get('inbound') not in grouped_clients[key]['inbounds']:
                    grouped_clients[key]['inbounds'].append(c.get('inbound'))
                for g in c.get('groups', []):
                    if g not in grouped_clients[key]['groups']:
                        grouped_clients[key]['groups'].append(g)

    # Merge overrides and local groups so the table matches what 'Preview' sees
    for key, c in grouped_clients.items():
        extra_groups = []
        if c['email'] in overrides: extra_groups.extend(overrides[c['email']])
        if c['sub_id'] in overrides: extra_groups.extend(overrides[c['sub_id']])
        if c['sub_id'] in local_groups_map: extra_groups.extend(local_groups_map[c['sub_id']])
        
        for g in extra_groups:
            if g not in c['groups']:
                c['groups'].append(g)

    client_rows = "".join(
        f"<tr><td>{esc(c.get('email'))}</td><td>{esc(c.get('sub_id'))}</td>"
        f"<td>{''.join(f'<span class=\"pill\">{esc(g)}</span>' for g in c.get('groups', [])) or '<span class=\"muted\">нет</span>'}</td>"
        f"<td>{' '.join(f'<span class=\"pill\" style=\"background:transparent;border:1px solid #4a5568;color:#a0aec0;\">{esc(ib)}</span>' for ib in c.get('inbounds', [])) or '<span class=\"muted\">нет</span>'}</td></tr>"
        for c in grouped_clients.values()
    ) if grouped_clients else f"<tr><td colspan='4' class='muted'>{esc(clients_error or 'Клиенты не найдены')}</td></tr>"

    local_groups = await storage.get_all_client_groups()
    local_client_rows = "".join(
        f"<tr><td>{esc(r.get('sub_id'))}</td><td>{esc(r.get('email'))}</td>"
        f"<td>{''.join(f'<span class=\"pill\">{esc(g)}</span>' for g in r.get('groups', [])) or '<span class=\"muted\">нет</span>'}</td>"
        f"<td><form method='post' action='/admin/delete-client-group' style='display:inline'>"
        f"<input type='hidden' name='_csrf' value='{esc(csrf_token)}'>"
        f"<input type='hidden' name='sub_id' value='{esc(r.get('sub_id'))}'>"
        f"<button class='mini danger'>удалить</button></form></td></tr>"
        for r in local_groups
    ) or "<tr><td colspan='4' class='muted'>Локальные группы не заданы</td></tr>"

    autoselects = await storage.get_autoselects()
    group_rules = await storage.get_group_rules()
    overrides = await storage.get_client_group_overrides()

    rules_text = "\n".join(f"{group}={','.join(ids)}" for group, ids in group_rules.items())
    overrides_text = "\n".join(
        f"{key}={','.join(groups)}" for key, groups in overrides.items()
    )

    probe_url, probe_interval = await storage.get_probe_config()

    autos_html = []
    for auto in autoselects:
        auto_id_esc = esc(auto.get("id"))
        tag_filter_names = set(t.lower() for t in (auto.get("tag_filter") or []))
        checks = "".join(
            f"<label class='check'><input class='tagcheck' data-auto='{auto_id_esc}' type='checkbox' name='tag_{auto_id_esc}' value='{esc(n.get('tag') or n.get('name') or '')}' {'checked' if (n.get('tag') or n.get('name') or '').strip().lower() in tag_filter_names else ''}>"
            f"<span><b>{esc(n.get('name'))}</b><br><span class='muted'>{esc(n.get('protocol'))} {esc(n.get('address'))}:{esc(n.get('port'))}</span></span></label>"
            for n in catalog
        ) or "<p class='muted'>Сначала сделай discovery нод.</p>"
        tag_filter_count = len(tag_filter_names)
        autos_html.append(
            f"<div class='card'><h3>{esc(auto.get('name'))}</h3>"
            f"<p class='muted'>ID: {auto_id_esc} · стратегия: наименьшая задержка · выбрано: {tag_filter_count if tag_filter_count else 'все'}</p>"
            f"<div class='toolbar'><label class='check'><input type='checkbox' name='mode_{auto_id_esc}' value='*' {'checked' if not tag_filter_count else ''}> Все ноды пользователя</label>"
            f"<button class='mini' type='button' onclick=\"setTagChecks('{auto_id_esc}',true)\">Выбрать все из каталога</button>"
            f"<button class='mini' type='button' onclick=\"setTagChecks('{auto_id_esc}',false)\">Снять все</button></div>"
            f"<div class='checks'>{checks}</div></div>"
        )

    msg = f"<div class='card ok'>{esc(message)}</div>" if message else ""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AutoSub v3 Dashboard</title>{page_head()}</head><body><div class="shell">
<div class="top"><div><h1>AutoSub v3 Dashboard</h1><p>Локальная панель управления JSON-подписками и автовыборами.</p></div><a class="btn secondary" href="/health">health</a></div>
{msg}
<div class="grid">
<div class="card"><h2>Discovery нод</h2><form method="post" action="/admin/discover">
<input type="hidden" name="_csrf" value="{esc(csrf_token)}">
<label>ID подписки, где видны нужные ноды</label><input type="text" name="sub_id" placeholder="idподписки">
<p class="muted">AutoSub возьмет оригинальный JSON с локального 3x-ui и построит каталог нод для выбора.</p>
<button type="submit">Обновить каталог</button></form></div>
<div class="card"><h2>Preview клиента</h2><form method="get" action="/admin/preview">
<label>ID подписки клиента</label><input type="text" name="sub_id" placeholder="idподписки">
<p class="muted">Покажет email, группы и какие автовыборы будут добавлены.</p>
<button type="submit">Показать preview</button></form></div>
</div>
<form method="post" action="/admin/save">
<input type="hidden" name="_csrf" value="{esc(csrf_token)}">
<div class="grid"><div class="card"><h2>Права групп</h2>
<label>Формат: group=autoselect_id,autoselect_id</label><textarea name="group_rules">{esc(rules_text)}</textarea>
<p class="muted">Пример: clients=stable и admins=stable,all.</p></div>
<div class="card"><h2>Ручные группы клиента (override)</h2>
<label>Формат: email или subId = группы</label><textarea name="client_group_overrides">{esc(overrides_text)}</textarea>
<p class="muted">Это запасной вариант, если API 3x-ui не отдает группы.</p></div></div>
<div class="card"><h2>Probe настройки</h2>
<div class="grid">
<div><label>Probe URL</label><input type="text" name="probe_url" value="{esc(probe_url)}"></div>
<div><label>Probe interval</label><input type="text" name="probe_interval" value="{esc(probe_interval)}"></div>
</div></div>
<div class="card"><h2>Автовыборы</h2><p class="muted">Порядок выдачи равен порядку ниже. Оригинальные ноды всегда остаются после них.</p></div>
{''.join(autos_html)}
<button type="submit">Сохранить настройки</button></form>

<div class="card"><h2>Локальные группы клиентов</h2>
<p class="muted">Группы, назначенные вручную через эту панель, имеют приоритет над API 3x-ui.</p>
<input type="text" id="searchLocalClients" onkeyup="filterTable('searchLocalClients', 'tableLocalClients')" placeholder="Поиск по sub_id, email или группам...">
<form method="post" action="/admin/set-client-group" class="toolbar">
<input type="hidden" name="_csrf" value="{esc(csrf_token)}">
<input type="text" name="sub_id" placeholder="sub_id" style="width:200px">
<input type="text" name="email" placeholder="email (необяз.)" style="width:200px">
<input type="text" name="groups" placeholder="группы через запятую" style="width:250px">
<button type="submit">Назначить группы</button>
</form>
<table id="tableLocalClients"><thead><tr><th>Sub ID</th><th>Email</th><th>Группы</th><th></th></tr></thead><tbody>{local_client_rows}</tbody></table></div>

<div class="card"><h2>Каталог нод</h2>
<input type="text" id="searchNodes" onkeyup="filterTable('searchNodes', 'tableNodes')" placeholder="Поиск по ноде, протоколу, адресу...">
<table id="tableNodes"><thead><tr><th>Нода</th><th>Протокол</th><th>Адрес</th><th>Сеть</th><th>Security</th><th>Тэг</th></tr></thead><tbody>{node_rows}</tbody></table></div>
<div class="card"><h2>Клиенты из 3x-ui</h2>
<input type="text" id="searchClients" onkeyup="filterTable('searchClients', 'tableClients')" placeholder="Поиск по email, sub_id или inbound...">
<table id="tableClients"><thead><tr><th>Email</th><th>Sub ID</th><th>Группы</th><th>Inbound</th></tr></thead><tbody>{client_rows}</tbody></table></div>
</div></body></html>"""


async def render_preview(storage, sub_id):
    data = await _resolve_preview_data(storage, sub_id)
    client = data["client"]
    groups = data["groups"]
    enriched = data["enriched"]

    rows = []
    for auto in data["autos"]:
        if auto.get("exists") and auto["matched"]:
            rows.append(f"<tr><td>{esc(auto.get('name'))}</td><td>{auto['matched']}</td></tr>")
    rows_html = "".join(rows) or "<tr><td colspan='2' class='muted'>Автовыборы не будут добавлены</td></tr>"

    node_rows = "".join(
        f"<tr><td>{esc(node_name(p, i))}</td><td>{esc(profile_node_id(p))}</td><td>{esc(p.get('_canonical_id') or '')}</td><td>{esc(p.get('_tag') or '')[:16]}...</td></tr>"
        for i, p in enumerate(enriched)
    )
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AutoSub Preview</title>{page_head()}</head><body><div class="shell"><p><a href="/admin">назад</a></p>
<div class="card"><h1>Preview</h1><p>ID: <b>{esc(sub_id)}</b></p>
<p>Email: <b>{esc((client or {}).get('email') or 'не найден')}</b></p>
<p>Группы: {''.join(f'<span class="pill">{esc(g)}</span>' for g in groups) or '<span class="muted">нет</span>'}</p>
<p>Источник: <b>{esc((client or {}).get('source', 'неизвестно'))}</b></p></div>
<div class="card"><h2>Будет добавлено</h2><table><thead><tr><th>Автовыбор</th><th>Нод</th></tr></thead><tbody>{rows_html}</tbody></table></div>
<div class="card"><h2>Оригинальные ноды клиента</h2><table><thead><tr><th>Нода</th><th>ID (profile)</th><th>ID (canonical)</th><th>Тэг (originNodeGuid)</th></tr></thead><tbody>{node_rows}</tbody></table></div>
</div></body></html>"""


async def render_api_test():
    try:
        api = get_xui_api()
        await api.login()
        inbounds = await api.inbounds()
        groups = await api.group_map()
        payload = {
            "ok": True,
            "api_url": api.base,
            "csrf": bool(api.csrf_token),
            "cookie": bool(api.cookie_header),
            "inbounds": len(inbounds),
            "groups": groups,
        }
    except Exception as exc:
        payload = {
            "ok": False,
            "api_url": env_get("XUI_API_URL", env_get("XUI_URL", "")),
            "error": str(exc),
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def render_debug(storage, sub_id):
    data = await _resolve_preview_data(storage, sub_id)
    client = data["client"]
    profiles = data["profiles"]
    enriched = data["enriched"]

    payload = {
        "sub_id": sub_id,
        "client": {
            "email": (client or {}).get("email"),
            "groups": data["groups"],
            "source": (client or {}).get("source"),
        } if client else None,
        "rules": data["group_rules"],
        "allowed_ids": data["allowed"],
        "profiles_count": len(profiles),
        "autos": data["autos"],
        "profile_ids": [
            {"name": node_name(profile, i), "id": profile_node_id(profile), "canonical_id": p.get("_canonical_id"), "legacy_id": p.get("_legacy_id"), "tag": p.get("_tag", "")}
            for i, (profile, p) in enumerate(zip(profiles, enriched))
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --- Form parsing ---

def parse_rules_text(text):
    rules = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        group, values = line.split("=", 1)
        ids = [x.strip() for x in values.split(",") if x.strip()]
        if group.strip():
            rules[group.strip()] = ids
    return rules


def parse_overrides_text(text):
    overrides = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, values = line.split("=", 1)
        groups = [x.strip() for x in re.split(r"[,;|]", values) if x.strip()]
        if key.strip():
            overrides[key.strip()] = groups
    return overrides


async def save_admin_form(storage, data):
    """Save admin form data to SQLite only (no more config.json dual-write)."""
    group_rules = parse_rules_text(data.get("group_rules", ""))
    await storage.set_group_rules(group_rules)

    overrides = parse_overrides_text(data.get("client_group_overrides", ""))
    await storage.set_client_group_overrides(overrides)

    probe_url = data.get("probe_url", "http://www.gstatic.com/generate_204")
    probe_interval = data.get("probe_interval", "60s")
    await storage.set_probe_config(probe_url, probe_interval)

    autoselects = await storage.get_autoselects()
    for auto in autoselects:
        aid = auto.get("id")
        mode = data.get(f"mode_{aid}", "")
        if isinstance(mode, str) and mode == "*":
            await storage.update_autoselect(aid, ["*"], [])
        else:
            tag_key = f"tag_{aid}"
            tag_raw = data.get(tag_key, [])
            if isinstance(tag_raw, str):
                tag_raw = [tag_raw]
            tag_filter = [t.strip() for t in tag_raw if t.strip()]
            await storage.update_autoselect(aid, ["*"], tag_filter)


# Need env_get for render_api_test fallback
from config import env_get
