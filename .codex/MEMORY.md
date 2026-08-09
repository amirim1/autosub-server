# Project Memory

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
- `http_clients.py`, `http_client_config.py`, `http_client_errors.py` — lifespan-managed HTTP pools, panel-session isolation, limits and safe network errors.
- `storage.py` — асинхронная SQLite persistence и миграции.
- `dashboard.py`, `templates/`, `static/` — админ-интерфейс.
- `config.py` — пути приложения, `.env` loading и runtime defaults.
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
- Рабочая ветка — `dev`, production — `main`.
- `XUI_SUB_URL` используется для получения подписки; `XUI_API_URL` — для API панели; `XUI_URL` — fallback.

## Current State

Последняя проверка 2026-08-09: `python -m pytest -q` — 287 passed, 1 strict xfailed
и 1 Starlette/httpx deprecation warning. Cache stampede устранён; единственный
известный xfail — malformed config marker. Rate limiter bounded и разделяет
public/admin/expensive policies.

## Known Problems

- Основные модули крупные: `builder.py`, `storage.py`, `api_client.py`, `autosub_server.py`, `dashboard.py` превышают 300 LOC.
- Ruff, Pyright basic, pytest/coverage, pip-audit, Bandit и ShellCheck закреплены как project quality gates; production-сборка выполняется через systemd/install scripts на Linux.
- Реальные systemd/nginx/deployment smoke tests локально не выполняются на Windows.
- Linux-specific systemd/Nginx smoke tests остаются внешним deployment gate; Python syntax проверяется через `python -m compileall`.

## Active Tasks

Поддерживать актуальность этого файла только для подтверждённых долгоживущих задач.

## Deployment Notes

Установка и обновление: `install.sh` и `update.sh`; сервис: `autosub-server.service`; reverse proxy: `nginx-example.conf` и `setup_nginx.sh`.

Основные deployment-команды: `bash install.sh`, `bash update.sh`, `bash setup_nginx.sh <domain> <port> [upstream]`; проверка Nginx — `nginx -t`, health — `curl http://127.0.0.1:25500/health`.

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
