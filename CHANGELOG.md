# Changelog

All notable changes to AutoSub Server will be documented in this file.

## [v1.3.3] - 2026-07-31

### Fixed
- **Happ Content-Type Fix**: Restored `application/json; charset=utf-8` response header for Happ subscription requests while maintaining `Happ-Encrypt` and `Hide-Settings` security flags, resolving the "unknown content type" error in Happ.

---

## [v1.3.2] - 2026-07-31

### Added
- **Happ Payload Base64 Subscription Encoding**: Encodes subscription payload into Base64 for Happ requests when `happ_encrypt` or `hide_settings` is enabled. Forces Happ to lock node config cards in Happ UI.
- **Save Confirmation Banner & UI Clarity**: Added a green save notification banner directly above the save button in the Admin Dashboard and simplified Security Rules placeholders (`*` for all clients).

---

## [v1.3.1] - 2026-07-31

### Fixed
- **In-App Config Locking (`Hide-Settings`)**: Injected `hideSettings: true` and `hide_settings: true` properties directly into JSON profile objects as well as HTTP response headers (`Hide-Settings`, `X-Hide-Settings`), forcing Happ and v2rayNG to completely lock and disable viewing/editing node configurations in the client UI.

---

## [v1.3.0] - 2026-07-31

### Added
- **Customizable Autoselect Balancers**: Full CRUD management of autoselect profiles in the Admin Dashboard. Users can rename existing autoselect profiles (e.g. `🚀 Основные авто` -> `🚀 Мой Авто VPN`), create new custom balancers (e.g. `🇩🇪 Германия Авто`), and delete custom profiles.
- **Selective Config Protection (`Hide-Settings`)**: Group/client configurable setting for `Hide-Settings: true` headers. Completely locks configuration details (IP, ports, keys) in Happ/v2rayNG UI for selected client groups while keeping them accessible for admin/testing groups.
- **Happ Subscription Encryption Config**: Added database schema and dashboard configuration for per-group Happ subscription encryption rules.

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
