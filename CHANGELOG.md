# Changelog

All notable changes to AutoSub Server will be documented in this file.

## [v1.1.2] - 2026-07-31

### Fixed
- **Happ Proxy / v2rayNG Compatibility**: Fixed TCP Ping and Latency check failure (infinite spinner) for JSON subscriptions.
- **Outbound Normalization**: Automatic conversion of flat `settings` into canonical Xray Core `vnext` (VLESS/VMess) and `servers` (Trojan) structures.
- **Xray Core Schema**: Added standard `inbounds` and `routing` sections to each profile object for full `libXray` test compatibility.
- **Legacy Cleanup**: Removed obsolete 3x-ui flat root fields (`address`, `add`, `port`).

### Added
- **Legacy `/sub/` Route Support**: Added `@app.get("/sub/{sub_id}")` route to support serving AutoSub JSON directly over legacy `/sub/` subscription links without changing client URLs.
- **Smart Browser Proxying for `/sub/`**: Automatically detects web browser navigation (`Accept: text/html` / User-Agent) on `/sub/` links and serves 3x-ui's native HTML subscription landing page, while serving AutoSub JSON to VPN clients (v2rayNG, Happ, NekoBox, etc.).
- **Outbound Validation Logging**: Added automatic WARNING log detection when receiving invalid outbounds from upstream panels (`missing vnext / servers`).
- **Structure Documentation**: Created `docs/structure.md` detailing repository layout, branching model (`dev`/`main`), and release workflow.

---

## [v1.1.1] - 2026-07-27
- Minor fixes for template rendering, APP_DIR paths, and async test runner execution.

## [v1.1.0] - 2026-07-27
- Added release versioning support, release channel selection, and update script improvements.

## [v1.0.0] - 2026-07-27
- Initial release of AutoSub Server.
