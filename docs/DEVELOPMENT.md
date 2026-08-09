# AutoSub Server Development

## Installation

Use Python 3.10+ and install development dependencies with:

```bash
python -m pip install --require-hashes -r requirements-dev.txt
```

For production, use the Linux installer rather than manually copying runtime files:

```bash
bash install.sh
```

## Local Run

Configure `.env` from `.env.example`, then run:

```bash
python autosub_server.py
```

The default local bind is `127.0.0.1:25500`. Health check:

```bash
curl http://127.0.0.1:25500/health
curl http://127.0.0.1:25500/health/ready
```

## Testing and Static Checks

```bash
python -m pytest -q
python -m pytest tests/test_rate_limiter.py tests/test_rate_limit_routes.py -q
python -m pytest tests/test_subscription_representation.py -q
python -m compileall -q *.py
```

Run Ruff, Pyright, pytest with coverage, pip-audit and Bandit as configured project
gates. On Linux, validate deployment scripts with:

```bash
bash -n install.sh update.sh setup_nginx.sh finish_setup.sh
```

## Development Workflow

1. Check `git status` and preserve unrelated changes.
2. Read the relevant module, tests and documentation before editing.
3. Make the smallest coherent change.
4. Add or update a regression test for behavior changes.
5. Run the relevant tests and report exact results.
6. Do not commit or push unless explicitly requested.

When changing `/sub/`, preserve `/json/` as an unconditional JSON endpoint and add
tests for explicit format priority, weighted `Accept`, unknown/VPN clients, local
template autoescape, CSP, cache behavior, and malicious upstream HTML.

## Deployment Commands

- `bash install.sh` — install `releases/current/shared`, a per-release venv, and service.
- `bash update.sh` — stage/validate a release, back up SQLite, switch, check, rollback.
- `bash setup_nginx.sh <domain> <port> [upstream]` — create the Nginx reverse proxy.
- `bash finish_setup.sh` — install/update, configure Nginx and show health/service status.
- `nginx -t` — validate Nginx configuration.
- `systemctl status autosub-server --no-pager` — inspect service state.

## Troubleshooting

- Service: `systemctl status autosub-server`; logs: `journalctl -u autosub-server -f`.
- Health: `curl http://127.0.0.1:25500/health`.
- Readiness: `curl http://127.0.0.1:25500/health/ready`.
- Configuration: `/opt/autosub-server/shared/.env`; active code: `current/`.
- The root-managed service has no dedicated AutoSub Unix identity.
- Deployment tests use temporary roots and fake runners; never point them at `/opt`.
- Public proxy: `curl -k https://<domain>:<port>/json/<sub_id>`.
- Confirm `XUI_SUB_URL` and `XUI_API_URL` are distinct and valid.
- Confirm `AUTOSUB_ADMIN_PASSWORD` and `AUTOSUB_TRUSTED_PROXIES` are set appropriately.
- Keep `AUTOSUB_TRUSTED_PROXIES` limited to actual reverse-proxy IPs/CIDRs. Empty
  disables forwarded-header trust; invalid and world-wide networks fail startup.
- Do not treat a passing pytest run as proof that systemd, Nginx, certificates or an external 3x-ui instance work.
