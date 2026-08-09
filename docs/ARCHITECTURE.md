# AutoSub Server Architecture

## Overview

AutoSub Server — локальный FastAPI-прокси для JSON-подписок 3x-ui. Он получает исходную подписку, добавляет разрешённые профили Autoselect/LeastPing или LeastLoad и возвращает результат клиенту. Оригинальные ноды сохраняются после сгенерированных профилей.

## Components

- `autosub_server.py` — FastAPI application, lifecycle, routes, auth, CSRF, rate limiting and response handling.
- `builder.py` — subscription parsing, profile normalization, node matching, autoselect generation and security flags.
- `api_client.py` — HTTP access to subscriptions and 3x-ui API, authentication and TLS verification.
- `subscription_cache.py` — bounded cache of fully built public subscriptions with
  per-key single-flight and stale-if-error.
- `rate_limiter.py` — bounded process-local sliding-window limiter and validated
  trusted-proxy client-IP resolution.
- `storage.py` — async SQLite connection, schema creation, migrations and CRUD.
- `dashboard.py` — admin view rendering, form parsing and persistence orchestration.
- `config.py` — environment loading, application paths and defaults.
- `fingerprint.py` — stable node IDs, canonical fingerprints and unique tags.
- `logger.py` — application log setup.
- `templates/`, `static/` — dashboard UI.

## Data Flow

1. Client calls `/json/{sub_id}` or `/sub/{sub_id}`.
2. Server determines client IP using the validated trusted-proxy allowlist and
   applies the public rate policy before cache or upstream work.
3. Legacy `/sub/` requests from browsers may fetch and return upstream 3x-ui HTML; subscription clients receive generated JSON.
4. `builder.build_for_subscription()` fetches the upstream subscription through `api_client.py`.
5. Builder resolves client groups, group rules and autoselect definitions through `storage.py`.
6. Profiles are normalized, generated profiles are prepended, and subscription headers are returned.

## Module Responsibilities

`autosub_server.py` owns HTTP contracts and security boundaries; do not move business logic there casually. `builder.py` owns subscription semantics and Xray profile compatibility. `api_client.py` owns upstream authentication and TLS. `storage.py` owns persistence and migration compatibility. `dashboard.py` owns admin presentation and form orchestration. `config.py` owns environment/path defaults, not business rules.

## Security Boundaries

- Admin routes use optional HTTP Basic Auth controlled by `AUTOSUB_ADMIN_PASSWORD`.
- Mutating admin forms require reusable, expiring HMAC-SHA256 CSRF tokens.
- Forwarded client IP headers are accepted only from an immediate peer in
  `AUTOSUB_TRUSTED_PROXIES`; XFF chains are evaluated right-to-left.
- A bounded process-local limiter separates public, admin-auth and expensive-admin
  policies. It is not shared between Uvicorn workers and requires no Redis.
- Upstream TLS verification is enabled by default through `XUI_TLS_VERIFY`.
- Tokens, passwords and `.env` contents must never be committed or logged.
- Nginx should expose `/json/` and `/sub/`; the local admin port should normally be reached through an SSH tunnel.

## Deployment Architecture

The systemd service runs `/opt/autosub-server/autosub_server.py` from a virtualenv on `127.0.0.1:25500`. Nginx terminates TLS on the public subscription port and proxies `/json/` and `/sub/` to the local service. `install.sh` installs a release, `update.sh` creates backups and updates runtime files, `setup_nginx.sh` writes the Nginx site, and `finish_setup.sh` orchestrates setup and health checks.
