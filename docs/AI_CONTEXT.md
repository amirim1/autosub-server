# AI Project Context

## Project Purpose

Локальный FastAPI-прокси для подписок 3x-ui, добавляющий профили автоматического выбора нод и админ-панель.

## Technology Stack

Python 3.10+, FastAPI/Uvicorn, httpx, aiosqlite, Jinja2, pytest/pytest-asyncio, SQLite.

## Architecture Overview

HTTP и lifecycle находятся в `autosub_server.py`; bounded rate limiting и trusted
proxy resolution — в `rate_limiter.py`; бизнес-логика подписок — в `builder.py`;
3x-ui integration — в `api_client.py`; persistence — в `storage.py`; dashboard —
в `dashboard.py`, `templates/`, `static/`; конфигурация — в `config.py`;
fingerprinting — в `fingerprint.py`.

Поток запроса подписки: `/json/{sub_id}` или `/sub/{sub_id}` → upstream 3x-ui → нормализация и autoselect в `builder.py` → группы/правила из SQLite → JSON. Для legacy `/sub/` браузер получает HTML 3x-ui, VPN-клиент — JSON.

Маршруты админки: `GET /admin`, `/admin/preview`, `/admin/api-test`, `/admin/debug`; mutating `POST /admin/save`, `/admin/discover`, `/admin/set-client-group`, `/admin/delete-client-group`, `/admin/add-autoselect`, `/admin/delete-autoselect`. Они защищены Basic Auth при заданном `AUTOSUB_ADMIN_PASSWORD` и CSRF для POST.

## Directory Structure

- `*.py` — runtime modules
- `tests/` — pytest tests
- `templates/`, `static/` — dashboard assets
- `docs/` — project documentation
- `.env.example`, `config.example.json` — configuration examples; secrets must remain outside Git
- `install.sh`, `update.sh`, `autosub-server.service` — Linux deployment
- `nginx-example.conf`, `setup_nginx.sh`, `finish_setup.sh` — reverse proxy and service setup
- `CHANGELOG.md` — release history

## Development Commands

- Run server locally: `python autosub_server.py`
- Run tests: `python -m pytest -q`
- Syntax check: `python -m compileall -q *.py`
- Install dependencies: `python -m pip install --require-hashes -r requirements-dev.txt`

Linux-only deployment checks: `bash -n install.sh update.sh setup_nginx.sh finish_setup.sh`, `nginx -t`, `systemctl status autosub-server --no-pager`.

## Testing Commands

Основные проверки — `python -m pytest -q`, Ruff, Pyright basic, coverage,
pip-audit, Bandit и ShellCheck. Тесты находятся в `tests/test_*.py` и дополнительно
покрывают lifecycle-managed HTTP clients и конкурентный subscription cache.

## Deployment

Production использует Linux/systemd и reverse proxy Nginx. Перед deployment проверяй `.env`, trusted proxies, TLS и `nginx -t`; Windows-аудит не заменяет Linux smoke test.

`AUTOSUB_APP_DIR`, `AUTOSUB_DB`, `AUTOSUB_CONFIG` и `AUTOSUB_LOG` управляют путями runtime. По умолчанию service работает из `/opt/autosub-server`, слушает `127.0.0.1:25500`, а Nginx проксирует `/json/` и `/sub/`.

## Important Rules

Минимальный diff, отсутствие лишней функциональности, повторное использование кода, отсутствие commit/push без запроса. Не ослаблять CSRF, Basic Auth, rate limiting, TLS и forwarded-header validation.

## Common Mistakes

- Не путать `XUI_SUB_URL` с `XUI_API_URL`.
- Не считать `hide-settings` шифрованием.
- Не доверять forwarded headers от публичных клиентов.
- Не изменять SQLite schema без миграции и теста.
- Не считать passing pytest доказательством работы systemd/nginx.
- Не смешивать `XUI_SUB_URL` и `XUI_API_URL`.
- Не менять legacy `config.json` migration behavior без regression test.

## Security Notes

Секреты только в окружении; `.env` не коммитить. Админ-панель должна оставаться защищённой. Сохраняй TLS verification и trusted proxy allowlist.

SQLite-таблицы: `meta`, `client_groups`, `node_catalog`, `group_rules`, `autoselects`, `client_group_overrides`. `hide-settings` — метаданные, а не шифрование.
