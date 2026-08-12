# Project Memory

## Current baseline

AutoSub Server `3.0.5` is the current production baseline. Production is
`main`; integration work targets `dev`. Stable `main` entrypoints resolve the latest
published release; `AUTOSUB_VERSION=dev` fetches the exact development branch.

Legacy `/sub/{sub_id}` returns JSON to known VPN clients and a safe local AutoSub
landing page to ordinary Mozilla browsers/WebViews even when their `Accept` header
prefers JSON. Explicit `format=json|html` has priority, `/json/` is always JSON, and
upstream 3x-ui HTML is never executed under the AutoSub origin.

The root-managed systemd service has no capabilities, sees system and application
code read-only, and may write only to `/opt/autosub-server/shared`. CI also includes
CodeQL; GitHub Actions versions are maintained through Dependabot.

PR №12 closes the last known strict xfail. Existing legacy `shared/config.json` is
parsed as strict UTF-8 JSON, minimally validated, and imported by
`Storage.migrate_from_config()` using `BEGIN IMMEDIATE`; persisted rows are verified
before the `config_migrated=1` marker is written last in the same transaction. Failures
roll back, preserve the source file, leave the marker absent, and retry on next startup.

Readiness remains false until DB migration, HTTP manager, subscription cache, rate
limiter, proxy resolver, CSRF manager, and legacy import complete. It is reset before
shutdown closes dependencies and never performs a 3x-ui network check. Deployment is
Linux/Python 3.10+, root-managed with `Restart=on-failure`; fresh install generates
CSRF and admin secrets while repeated install preserves `shared`. Runtime manifest and
temporary-root deployment smoke tests cover manifest-only import, fresh initialization,
legacy persistence/config migration, successful update, rollback, and interrupted
recovery. CI runs Python 3.10/3.12/3.14, locked installs, pytest/coverage, Ruff, Pyright,
pip-audit, Bandit, ShellCheck, and `bash -n`.

## Project Overview

AutoSub Server — локальный прокси JSON-подписок для 3x-ui с профилями Autoselect/LeastPing и админ-панелью.

## Architecture

- `autosub_server.py` — FastAPI lifecycle, маршруты `/json/{sub_id}`, `/sub/{sub_id}`, `/admin`, `/health` и security controls.
- `builder.py` — разбор, нормализация и генерация Xray-профилей.
- `api_client.py` — HTTP/API-клиент 3x-ui без собственного response cache.
- `subscription_cache.py` — bounded LRU/TTL-кэш готовых публичных подписок,
  per-key single-flight, stale-if-error и поколенческая инвалидация.
- `rate_limiter.py` — bounded process-local sliding-window limiter и validated
  trusted-proxy client-IP resolution.
- `subscription_representation.py` — deterministic JSON/local-HTML selection и
  безопасный autoescaped subscription template.
- `http_clients.py`, `http_client_config.py`, `http_client_errors.py` — lifespan-managed HTTP pools, panel-session isolation, limits and safe network errors.
- `storage.py` — асинхронная SQLite persistence и миграции.
- `dashboard.py`, `templates/`, `static/` — админ-интерфейс.
- `config.py`, `runtime_paths.py` — release/shared paths, `.env` loading и defaults.
- `release_manager.py`, `runtime-manifest.txt` — atomic deployment и runtime allowlist.
- `fingerprint.py` — стабильные идентификаторы и теги нод.
- `logger.py` — файловое логирование.

## Technology Stack

Python 3.10+, FastAPI, Uvicorn, httpx, aiosqlite, Jinja2, pytest, pytest-asyncio.

## Important Decisions

- Конфигурация и секреты загружаются из `.env`/окружения.
- Для forwarded-заголовков используется явный список trusted proxies.
- API 3x-ui предпочитает Bearer token, login/password — fallback.
- HTTP clients создаются и закрываются через FastAPI lifespan: один stateless upstream pool и bounded изолированные panel sessions.
- Кэш готовых подписок process-local: 256 записей, 30 секунд fresh, 300 секунд
  stale-if-error, максимум 256 KiB payload на запись; Redis не используется.
- Rate limiter process-local: 4096 LRU buckets, idle TTL 20 минут; отдельные
  public `60/min`, admin-auth `20/min` и expensive-admin `10/min` policies.
- Forwarded headers учитываются только от immediate peer из validated
  `AUTOSUB_TRUSTED_PROXIES`; пустое значение отключает trust, unsafe config
  останавливает startup.
- `/json/` всегда возвращает generated JSON; `/sub/` поддерживает explicit
  `format=json|html`, weighted Accept и local-only HTML. Upstream HTML никогда не
  исполняется под origin AutoSub.
- Рабочая ветка — `dev`, production — `main`.
- `XUI_SUB_URL` используется для получения подписки; `XUI_API_URL` — для API панели; `XUI_URL` — fallback.

## Current State

Release `v3.0.5` restores browser landing selection for JSON-preferring
Mozilla/WebView requests, keeps explicit formats and known VPN clients compatible,
hardens systemd/Nginx/CI, and refreshes first-time-user documentation.

Финальный baseline после PR №12 фиксируется полным pytest/coverage и quality gates;
strict xfail для malformed config marker закрыт production-исправлением. Rate limiter
bounded и разделяет public/admin/expensive policies; browser `/sub/` использует только
local HTML.

## Known Problems

- Основные модули крупные: `builder.py`, `storage.py`, `api_client.py`, `autosub_server.py`, `dashboard.py` превышают 300 LOC.
- Ruff, Pyright basic, pytest/coverage, pip-audit, Bandit и ShellCheck закреплены как project quality gates; production-сборка выполняется через systemd/install scripts на Linux.
- Реальные systemd/nginx/deployment smoke tests локально не выполняются на Windows.
- Linux-specific systemd/Nginx smoke tests остаются внешним deployment gate; Python syntax проверяется через `python -m compileall`.

## Active Tasks

Поддерживать актуальность этого файла только для подтверждённых долгоживущих задач.

## Deployment Notes

Production layout: `/opt/autosub-server/current -> releases/<id>`, per-release venv и
persistent `/opt/autosub-server/shared`. Updater root-owned, использует `flock`,
marker-based staging, SQLite backup, atomic symlink, local readiness, code rollback и
retention трёх release. Атомарный `.update-state.json` сохраняет rollback target для
recovery после interruption. Сервис root-managed без Unix user `autosub`.

Systemd activation не изолирована от Nginx/write traffic. Поэтому после попытки start
БД автоматически не восстанавливается; verified pre-update backup сохраняется для
manual fail-closed recovery. Auto-restore разрешён только для доказанной остановленной
pre-traffic фазы.

Установка и обновление: `install.sh` и root `/opt/autosub-server/update.sh`; сервис:
`autosub-server.service`; reverse proxy: `current/setup_nginx.sh`.

Основные deployment-команды: `bash install.sh`, `/opt/autosub-server/update.sh`,
`current/setup_nginx.sh <domain> <port> [upstream]`; readiness —
`curl http://127.0.0.1:25500/health/ready`.

## Persistence Schema

SQLite содержит `meta`, `client_groups`, `node_catalog`, `group_rules`, `autoselects` и `client_group_overrides`. `autosub_server.py` мигрирует legacy `config.json` при startup; изменения схемы требуют migration path и теста.

## Things Not To Change

Не менять без явного запроса: формат subscription JSON, публичные маршруты, trusted-proxy security model, CSRF/admin controls, миграции SQLite и release history.

## Critical Areas

- Subscription generation and Xray profile normalization in `builder.py`.
- 3x-ui subscription/API authentication and TLS in `api_client.py`.
- Admin Basic Auth, CSRF, trusted proxies and rate limiting in `autosub_server.py`.
- SQLite schema, migrations and legacy `config.json` transition in `storage.py`.
- Linux deployment, Nginx TLS proxy and systemd service.

## Development Philosophy

Prefer minimal, reviewable changes; preserve public routes, subscription formats, stored data and backward compatibility; treat authentication, forwarded headers, TLS and migrations as security-sensitive boundaries; verify behavior with tests before claiming completion.

## Memory Rules

Записывай только устойчивые решения, ограничения и подтверждённые факты. Обновляй после архитектурных или deployment-изменений. Не храни секреты, токены, пароли, cookies, персональные данные и временный контекст отдельной задачи.
