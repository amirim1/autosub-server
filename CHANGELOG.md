# Changelog

All notable changes to AutoSub Server will be documented in this file.

## [v3.2.0] - 2026-08-24

### Fixed
- Landing stylesheet is now versioned by app version (`?v=`), fixing stale-cache
  rendering after updates (tabs/cards appeared unstyled for 24h).
- v2RayTun deep link switched to the documented raw-URL form
  (`v2raytun://import/{url}`); deep-link schemes became overridable per client via
  `AUTOSUB_LANDING_OVERRIDES` (`deep_link_scheme` with `{b64}` or `{url}`
  placeholder) — Happ desktop does not parse `happ://add/{base64}` deep links,
  so the raw-URL variant can be enabled without code changes.

### Added
- Redesigned public landing page (`/sub/` HTML): platform tabs (Android,
  Android TV, iOS, Windows, macOS — CSS-only, no JavaScript), per-client cards
  with verified download links (Happ, v2RayTun) and one-tap deep links
  (`happ://add/…`, `v2raytun://import/…`, `incy://import/…`) that open the
  client with the subscription pre-filled. Advanced mode keeps the raw JSON
  link and copyable subscription URL.
- New `landing_catalog.py`: server-side client/download catalog. All external
  landing URLs originate here or from `AUTOSUB_LANDING_OVERRIDES` (JSON,
  http(s)-only) — never from upstream data. `AUTOSUB_PUBLIC_URL` sets the
  public origin embedded into deep links.
- Client profile registry (`client_profiles.py`): connecting apps are identified
  via `?client=` override or `User-Agent` detection (priority: query → UA → generic)
  and receive their default wire format. Profiles: Happ, Incy and v2RayTun → Xray
  array (each autoselect and node shown as a separate server card with balancers
  embedded); sing-box/Clash as before; generic → xray (unchanged).
- New optional wire format `links`/`base64`: Base64-encoded list of standard
  share-link URIs (vless/vmess/trojan/ss over reality/tls/ws/grpc/tcp/httpupgrade),
  served as `text/plain`. Available only via explicit `?format=`.
- Unknown or repeated `?client=` values return HTTP 400; the parameter is stripped
  from upstream queries on `/sub/`.
- Session-sticky balancer generation: `sticky_domains` (meta) route geo-sensitive
  services through a fixed first node, and per-autoselect `country_scope` (schema v5)
  restricts auto-balancing to a single detected country group.
- New subscription output formats: `singbox` and `clash` via `/json/{sub_id}?format=…`
  or automatic client User-Agent detection on `/sub/`; Xray remains the default and
  is byte-compatible.
- DNS/egress coupling: generated Xray configs route resolver traffic through the
  same sticky/scoped path; sing-box DNS servers use `detour` to the pinned target.

### Changed
- Generated health-check intervals are clamped to a 60s anti-flapping floor,
  overridable with `AUTOSUB_MIN_PROBE_INTERVAL`.
- Strategy validation now shares one whitelist (`config.SUPPORTED_AUTOSELECT_STRATEGIES`)
  across builder, storage and dashboard; new modules `balancer.py` and `generators.py`.
- PyYAML added as a runtime dependency for Clash output serialization.

## [v3.1.0] - 2026-08-14

### Added
- Added a dashboard editor for the Xray domain rules that bypass every generated
  balancer. It accepts one `domain:`, `full:`, `keyword:`, `regexp:`, or `geosite:`
  rule per line, ignores blank lines and leading-comment lines, and deduplicates
  entries while preserving their order.
- Preserved the exact existing 80-rule Russian-site list as the default for fresh and
  upgraded installations that have not saved an override.

### Changed
- Stored the shared direct-domain configuration as a JSON list in SQLite `meta`.
  This needs no schema migration: an absent key selects the built-in defaults and a
  stored empty list is an intentional override.
- Omitting all domain-direct rules now removes only that routing rule. Private-IP
  direct routing, BitTorrent and UDP/443 blocking, and the catch-all balancer remain
  unchanged.
- Saving the dashboard invalidates generated-subscription cache entries immediately.

### Security
- Validate the direct-domain list before any settings are written: only supported
  Xray prefixes are accepted, control characters and empty patterns are rejected,
  and the input is bounded to 512 entries of at most 512 characters each.
- Return a non-reflective HTTP 400 for invalid admin settings instead of persisting a
  partial configuration or exposing the rejected input.

## [v3.0.6] - 2026-08-12

### Fixed
- Resolved runtime templates and static assets relative to the active immutable
  release, preventing a legacy `AUTOSUB_APP_DIR=/opt/autosub-server` and stale
  flat-layout directories from causing `TemplateNotFound` on browser `/sub/` pages.
- Removed the obsolete `AUTOSUB_APP_DIR` assignment from new environment templates.

## [v3.0.5] - 2026-08-12

### Fixed
- Restored the safe local landing page for ordinary Mozilla/WebView requests to
  legacy `/sub/<subId>` links even when the browser sends a JSON-preferring `Accept`
  header; explicit formats and known VPN-client behavior remain compatible.
- Removed the remaining constant `innerHTML` assignment from dashboard JavaScript and
  stopped placing operator-supplied balancer names in redirect query strings.

### Changed
- Redesigned the local subscription landing page and rewrote the Russian and English
  READMEs around first-time installation, operation, and verified `main`/`dev` flows.
- Updated CI actions, added CodeQL and GitHub Actions Dependabot coverage, and added the
  repository's MIT license and security policy.
- Added current stable Python 3.14 to CI and raised the branch coverage gate from 55%
  to 70%, below the measured 72% baseline.
- Hardened the systemd service sandbox while preserving the root-managed deployment
  model and `/opt/autosub-server/shared` as the only writable runtime path.
- Validated Nginx setup inputs, made site replacement rollback-safe, enforced modern
  TLS/timeouts, and removed the previously ignored symlink-creation failure.
- Replaced remaining SQLite `INSERT OR REPLACE` writes with conflict-aware UPSERTs so
  client row identity and creation time survive group updates.

## [v3.0.4] - 2026-08-11

### Fixed
- Made the installed `/opt/autosub-server/update.sh` ignore leftover legacy flat-layout
  entrypoint files unless its directory is a complete release checkout with the release
  manager and runtime manifest.
- Preserved decoded upstream response metadata correctly so gzip-compressed 3x-ui API
  responses are parsed once and client group rules can add configured balancers.

## [v3.0.3] - 2026-08-11

### Fixed
- Added the missing `/health/live` endpoint promised by the operations contract.
- Enforced mode `0700` on the shared runtime directory during every update.

## [v3.0.2] - 2026-08-11

### Fixed
- Made legacy flat-layout migration build its rollback checkpoint with the exact
  release's hash-locked dependencies instead of trying to apply hash enforcement to
  the unpinned legacy `requirements.txt`.

## [v3.0.1] - 2026-08-11

### Fixed
- Restored the documented `curl | bash` install and update entry points by treating
  stdin execution as having no local source directory and fetching the exact requested
  release instead of reusing an unrelated current working directory.

## [v3.0.0] - 2026-08-11

### Security
- Hardened admin authentication by enforcing both username and password, rejecting
  unsafe non-loopback configurations, and rate-limiting authentication attempts.
- Replaced process-local CSRF state with reusable HMAC-SHA256 tokens and added request
  IDs, redacted errors/logs, and consistent security headers.
- Added trusted-proxy-aware public/admin rate limiting with spoof-safe forwarded-header
  parsing and explicit `429`/`Retry-After` responses.
- Replaced execution of upstream HTML on legacy `/sub/` URLs with a local autoescaped
  AutoSub page using a strict CSP and `no-store`.

### Reliability
- Made SQLite migrations transactional, with schema validation, integrity checks, and
  verified backups before upgrades.
- Made legacy `config.json` recovery fail closed and retryable through strict validation,
  transactional import, post-write verification, and a last-written migration marker.
- Moved upstream HTTP clients into FastAPI lifespan with bounded pools, explicit
  timeouts, response-size limits, isolated panel sessions, and shutdown cleanup.
- Added a bounded LRU/TTL subscription cache with hashed keys, per-key single-flight,
  stale-if-error behavior, and generation-based invalidation.

### Deployment
- Introduced the root-managed `releases/current/shared` layout with release-local
  virtual environments and a manifest-defined immutable runtime payload.
- Added a locked, atomic updater with verified SQLite backups, readiness gating, code
  rollback, interrupted-update recovery, bounded retention, and safe migration from the
  legacy flat layout.

### Compatibility / Breaking Changes
- Python 3.10 is now the minimum supported version.
- Production deployment now uses `/opt/autosub-server/{releases,current,shared}`.
- Invalid legacy configuration now stops startup instead of being silently ignored.
- Browser requests to legacy `/sub/` URLs now receive local AutoSub HTML rather than
  the upstream panel page; generated JSON routes and payloads remain compatible.
- The configured admin username is enforced as well as the password.
- Non-loopback deployments require secure admin authentication configuration.
- Production requires a stable `AUTOSUB_SECRET_KEY` for restart-safe CSRF tokens.
- Rate limiting can return HTTP `429` with `Retry-After`.
- Automatic database restore is intentionally not performed after a normal production
  activation failure because the new release may already have accepted writes; the
  verified backup is retained for manual recovery.

---

## [v2.1.0] - 2026-07-31

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
