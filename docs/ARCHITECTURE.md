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
- `subscription_representation.py` — deterministic `/sub/` representation selection
  and rendering of the local safe subscription page.
- `storage.py` — async SQLite connection, schema creation, migrations and CRUD.
- `dashboard.py` — admin view rendering, form parsing and persistence orchestration.
- `config.py` — environment loading, application paths, defaults, and strict legacy
  config parsing/validation before one-time SQLite import.
- `fingerprint.py` — stable node IDs, canonical fingerprints and unique tags.
- `logger.py` — application log setup.
- `templates/`, `static/` — dashboard UI.

## Data Flow

1. Client calls `/json/{sub_id}` or `/sub/{sub_id}`.
2. Server determines client IP using the validated trusted-proxy allowlist and
   applies the public rate policy before cache or upstream work.
3. `/json/` always selects generated JSON. Legacy `/sub/` applies explicit
   `format=json|html`, weighted `Accept`, client compatibility and safe fallback rules.
4. `builder.build_for_subscription()` fetches the upstream JSON subscription through
   `api_client.py`; the built result remains protected by the existing cache.
5. For HTML, only readiness is taken from the built result; AutoSub renders its own
   autoescaped template and never returns the upstream body as HTML.
6. Builder resolves client groups, group rules and autoselect definitions through `storage.py`.
7. Profiles are normalized, generated profiles are prepended, and subscription headers are returned.

## Module Responsibilities

`autosub_server.py` owns HTTP contracts and security boundaries; do not move business logic there casually. `builder.py` owns subscription semantics and Xray profile compatibility. `api_client.py` owns upstream authentication and TLS. `storage.py` owns persistence and migration compatibility. `dashboard.py` owns admin presentation and form orchestration. `config.py` owns environment/path defaults, not business rules.

## Security Boundaries

- Admin routes use optional HTTP Basic Auth controlled by `AUTOSUB_ADMIN_PASSWORD`.
- Mutating admin forms require reusable, expiring HMAC-SHA256 CSRF tokens.
- Forwarded client IP headers are accepted only from an immediate peer in
  `AUTOSUB_TRUSTED_PROXIES`; XFF chains are evaluated right-to-left.
- A bounded process-local limiter separates public, admin-auth and expensive-admin
  policies. It is not shared between Uvicorn workers and requires no Redis.
- Public HTML is local-only, carries a strict self-only CSP and `no-store`, and never
  receives upstream markup, redirects, credentials or panel URLs.
- Upstream TLS verification is enabled by default through `XUI_TLS_VERIFY`.
- Tokens, passwords and `.env` contents must never be committed or logged.
- Nginx should expose `/json/` and `/sub/`; the local admin port should normally be reached through an SSH tunnel.

## Deployment Architecture

Production uses one root-managed systemd/Uvicorn process on `127.0.0.1:25500`.
There is deliberately no `autosub` Unix user. Nginx terminates TLS and proxies only
`/json/` and `/sub/` to the local service.

`/opt/autosub-server` contains `releases/<id>/`, relative symlink `current`, persistent
`shared/`, and root-owned `update.sh`. Every complete release has immutable-ish runtime
files plus its own `venv`; `.env`, `config.json`, `data.db` (including WAL/SHM),
`autosub.log`, and `backups/` are shared. `runtime_paths.py` separates release assets
from persistence while preserving local repository defaults.

`release_manager.py` validates IDs and manifest paths, creates marker-protected staging,
uses a temporary symlink plus `os.replace` for activation, creates SQLite backups via
the backup API, polls lifecycle readiness, rolls code back, and bounds successful
release retention. The production systemd runner is not traffic-isolated, so it never
automatically restores the DB after a start attempt; a verified backup is retained for
operator recovery. Only an explicitly isolated stopped pre-traffic runner may invoke
automatic restore. `runtime-manifest.txt` is the shared install/update allowlist and
excludes tests, docs, `.codex`, secrets, and data.
An atomic root-only `.update-state.json` records previous/candidate/phase/backup so a
subsequent updater restores the recorded previous release after a switch interruption;
unknown or mismatched state fails closed.

Readiness is false before startup completes and is reset before downstream resources
close during shutdown. Startup opens/migrates SQLite, initializes HTTP/cache/rate-limit/
proxy/CSRF resources, strictly imports legacy config when needed, and only then makes
`/health/ready` return 200. Readiness never calls 3x-ui.
