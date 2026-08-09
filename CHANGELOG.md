# Changelog

All notable changes to AutoSub Server will be documented in this file.

## [v2.1.0] - 2026-07-31

2026-08-09: Добавлен ограниченный process-local LRU/TTL-кэш готовых публичных
подписок: SHA-256 ключи, per-key single-flight, stale-if-error для временных
upstream-сбоев, поколенческая инвалидация после admin-изменений и безопасное
завершение через FastAPI lifespan. Публичные маршруты и форматы ответов сохранены.

2026-08-09: Added a bounded process-local LRU/TTL cache for fully built public
subscriptions, with SHA-256 keys, per-key single-flight, stale-if-error for transient
upstream failures, generation invalidation after admin mutations, and lifespan-safe
shutdown. Public routes and response formats remain unchanged.

2026-08-09: Перенести upstream HTTP-клиенты под управление FastAPI lifespan:
переиспользовать bounded connection pools, изолировать 3x-ui sessions, добавить
явные phase timeouts и response-size limits, безопасную ротацию credentials и
гарантированное закрытие при shutdown (завершено).

2026-08-09: Move upstream HTTP clients under FastAPI lifespan management: reuse
bounded connection pools, isolate 3x-ui sessions, enforce explicit phase timeouts
and response-size limits, rotate credentials safely, and close resources reliably
during shutdown (completed).

2026-08-04: Сделать SQLite-миграции транзакционными, добавить integrity checks и консистентный backup перед upgrade - ошибки больше не повышают `schema_version`, startup завершается с rollback (завершено).

2026-08-04: Заменить process-local CSRF store на reusable HMAC-SHA256 tokens и перенести backup root в `/opt/autosub-server/shared/backups/` - админ-формы совместимы с restart/workers без server-side token state (завершено).

2026-08-04: Добавить request ID, редактирование чувствительных логов и security headers - ошибки сопоставимы без утечки subscription ID, email и upstream secrets (завершено).

2026-08-01: Исправить ложную поддержку шифрования Happ в `autosub_server.py`, `builder.py` и админке - удалены недокументированные заголовки и поля payload, обычная JSON-подписка больше не заявляется как зашифрованная (завершено).

### Fixed
- **Probe Interval Persistence**: `probe_interval` from SQLite and the dashboard now propagates into generated `burstObservatory.pingConfig.interval` instead of being hardcoded.
- **Balancer Strategy Handling**: Autoselect profiles now honor the stored `strategy` value, support both `leastPing` and `leastLoad`, and safely fall back to `leastPing` with a warning for unknown values.
- **Empty JSON Subscription Handling**: Valid upstream `[]` subscriptions now return cleanly with HTTP 200 and original payload semantics instead of failing on `profiles[0]`.
- **Trusted Proxy Rate Limiting**: Client IP detection now trusts forwarded headers only from explicitly configured reverse proxies via `AUTOSUB_TRUSTED_PROXIES`, blocking spoofed `X-Real-IP` and `X-Forwarded-For` values from direct clients.
- **Graceful HTTP Client Shutdown**: The shared `httpx.AsyncClient` used by `XuiApi` now closes during application shutdown, and repeated close calls remain safe.
- **Pytest Discovery**: Added native `pytest.ini` discovery so `pytest -q` works without `PYTHONPATH=.`

### Added
- **Admin Strategy Selection**: The dashboard can now create and edit autoselect profiles with explicit `leastPing` or `leastLoad` strategy selection.
- **Regression Coverage**: Added tests for balancer parameters, empty subscriptions, trusted proxy parsing, lifecycle cleanup, Basic Auth, CSRF flows, and rate limiting behavior.

---

## [v2.0.0] - 2026-07-31

### Added
- **Мажорный релиз: AutoSub Server 2.0 / Major Release: AutoSub Server 2.0**:
  - [RU] Объединены все последние функции, включая новый интерфейс Glassmorphism UI, интеллектуальную стратегию балансировщика `leastLoad`, расширенную маршрутизацию RU Bypass для обхода VPN, выборочную защиту настроек `Hide-Settings` и совместимость со старыми ссылками `/sub/` в стабильную мажорную версию.
  - [EN] Consolidated all recent features including the new Glassmorphism UI, intelligent `leastLoad` balancer strategy, expanded RU Bypass routing, selective `Hide-Settings`, and legacy `/sub/` compatibility into a new stable major version.
- **Улучшенные скрипты установки / Improved Installation Scripts**:
  - [RU] Скрипты `install.sh` и `update.sh` стали более чистыми благодаря отключению подробных логов зависимостей pip и добавлению красивых эмодзи/цветов для наглядного отображения прогресса.
  - [EN] Made `install.sh` and `update.sh` outputs cleaner by silencing verbose dependency logs and adding aesthetic emojis/colors to indicate progress clearly.
- **Подробная документация / Detailed Documentation**:
  - [RU] Полностью обновлен `README.md` с подробным пошаговым руководством по установке и ключевыми возможностями для новых пользователей.
  - [EN] Fully updated `README.md` with an extensive step-by-step setup guide and highlighted key features for new users.

---

## [v1.3.3] - 2026-07-31

### Added
- **Admin Dashboard Glassmorphism UI Overhaul**: Full modern dark glassmorphism redesign of the Admin Dashboard with top metrics cards (Total Nodes, Active Balancers, Manual Clients).
- **Floating Toast Notifications & Sticky Action Bar**: Added animated top-right toast notifications for all dashboard save/edit actions and a sticky save bar pinned at the bottom of the viewport.
- **Dedicated All Clients & Server Nodes Views**: Added dedicated interactive cards displaying all synced 3x-ui clients with assigned groups, inbounds, and JSON subscription links, plus a complete server nodes catalog table.
- **Legacy Security Rules**: Introduced group-based Hide-Settings and experimental Happ payload controls; the unsupported Happ payload behavior was removed in v2.1.0.
- **Extended Russian Routing Bypass**: Expanded the `direct` routing rules to include a comprehensive, full reference list of major Russian services and domains (Yandex, VK, Mail.ru, banks, e-commerce, etc.) to optimize connectivity speeds and bypass VPN routing for local resources.
- **Advanced Load Balancing & Optimization**: Upgraded balancer strategy to `leastLoad` for intelligent traffic distribution across grouped latency baselines, and optimized `burstObservatory` check frequency to reduce mobile battery drain while maintaining fast failover.

---

## [v1.3.0] - 2026-07-31

### Added
- **Customizable Autoselect Balancers**: Full CRUD management of autoselect profiles in the Admin Dashboard. Users can rename existing autoselect profiles (e.g. `🚀 Основные авто` -> `🚀 Мой Авто VPN`), create new custom balancers (e.g. `🇩🇪 Германия Авто`), and delete custom profiles.
- **Selective Config Metadata (`Hide-Settings`)**: Added group/client rules for emitting the `hide-settings` subscription header.
- **Experimental Happ Encryption Config**: Added the legacy per-group setting later removed from runtime and the dashboard in v2.1.0 because Happ does not support the advertised payload header.

---

## [v1.2.0] - 2026-07-31

### Added
- **Legacy `/sub/` Route Support**: Seamless support for legacy `/sub/{sub_id}` subscription URLs in `autosub_server.py` and Nginx templates. Clients can now keep their existing `/sub/` URLs without any changes in their apps.
- **Smart Browser Proxying**: Intelligent request inspection (`Accept: text/html` and User-Agent detection). When accessed via a web browser, `/sub/{sub_id}` proxies and displays 3x-ui's native HTML subscription landing page with user traffic stats and client links; when accessed by a VPN client (Happ, v2rayNG, NekoBox, Sing-box, etc.), it returns the enriched AutoSub JSON with auto-selecting profiles.

### Fixed
- **"Socket Closed" / Connection Drop Error**: Fixed connection truncation and "Socket closed" errors during repeated subscription refreshes in Happ/v2rayNG by stripping upstream transport headers (`Content-Length`, `Transfer-Encoding`, `Connection`, `Keep-Alive`).
- **Nginx Multi-Port Examples**: Updated Nginx configuration examples to demonstrate binding both legacy `/sub/` and `/json/` paths on custom ports (e.g., 2096 and 2097).

---

## [v1.1.2] - 2026-07-31

### Fixed
- **Happ Proxy / v2rayNG Compatibility**: Fixed TCP Ping and Latency check failure (infinite spinner) for JSON subscriptions.
- **Outbound Normalization**: Automatic conversion of flat `settings` into canonical Xray Core `vnext` (VLESS/VMess) and `servers` (Trojan) structures.
- **Xray Core Schema**: Added standard `inbounds` and `routing` sections to each profile object for full `libXray` test compatibility.
- **Legacy Cleanup**: Removed obsolete 3x-ui flat root fields (`address`, `add`, `port`).

### Added
- **Outbound Validation Logging**: Added automatic WARNING log detection when receiving invalid outbounds from upstream panels (`missing vnext / servers`).
- **Structure Documentation**: Created `docs/structure.md` detailing repository layout, branching model (`dev`/`main`), and release workflow.

---

## [v1.1.1] - 2026-07-27
- Minor fixes for template rendering, APP_DIR paths, and async test runner execution.

## [v1.1.0] - 2026-07-27
- Added release versioning support, release channel selection, and update script improvements.

## [v1.0.0] - 2026-07-27
- Initial release of AutoSub Server.
