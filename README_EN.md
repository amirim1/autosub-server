# AutoSub Server

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/amirim1/autosub-server/actions/workflows/ci.yml/badge.svg)](https://github.com/amirim1/autosub-server/actions/workflows/ci.yml)
[![Latest Release](https://img.shields.io/github/v/release/amirim1/autosub-server)](https://github.com/amirim1/autosub-server/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**AutoSub Server** adds automatic server-selection profiles to JSON subscriptions
produced by 3x-ui. Users keep a normal subscription URL while compatible VPN clients
receive configurable `leastPing` or `leastLoad` balancers allowed for their group.

Use AutoSub to:

- prepend one or more automatic-selection profiles to existing nodes;
- choose nodes per profile and assign different profiles to client groups;
- discover current nodes and clients from 3x-ui;
- manage the configuration through a local web dashboard;
- preserve existing `/sub/<subId>` URLs and VPN-client compatibility.

Example:

```text
Original:  Germany, Netherlands, Finland
AutoSub:   🚀 Primary auto, ⚡ All nodes auto,
           Germany, Netherlands, Finland
```

AutoSub does **not** replace 3x-ui, Xray, Nginx, or a VPN client. It does not create
inbounds or convert Base64 subscriptions. Its input and output are the JSON
subscription already produced by 3x-ui.

[Русская версия](README.md)

## How it works

```text
VPN client or browser
        │  /json/SUB_ID or /sub/SUB_ID
        ▼
Nginx: TLS and public /json/ + /sub/
        │  http://127.0.0.1:25500
        ▼
AutoSub Server
        ├─ fetches the original 3x-ui JSON subscription
        ├─ resolves the client and its groups
        ├─ prepends the allowed autoselect profiles
        └─ returns JSON with the original subscription metadata
```

- `/json/<subId>` always returns JSON.
- `/sub/<subId>` detects the caller. A regular Mozilla browser or WebView receives
  a safe local AutoSub landing page even when its `Accept` header prefers JSON;
  known VPN clients receive JSON.
- `?format=json` and `?format=html` explicitly select a `/sub/` representation.
  Explicit format wins over browser detection. `/json/` cannot become HTML.
- Untrusted 3x-ui HTML is never executed under the AutoSub origin. The local landing
  page has no external scripts, analytics, or panel resources.

## Main features

- `leastPing` and `leastLoad` profiles with selected nodes or `*`;
- 3x-ui group rules and per-client overrides;
- node discovery and panel API diagnostics;
- bounded LRU/TTL cache, per-key single-flight, and stale-if-error;
- separate subscription and authenticated panel HTTP pools;
- upstream response limits, explicit timeouts, and safe GET retries;
- local Basic Auth dashboard with CSRF and rate limiting;
- dashboard-managed Xray domain rules for traffic that must bypass the balancer;
- validated trusted-proxy client-IP resolution;
- transactional SQLite migrations and pre-update backups;
- atomic release directories, readiness checks, and automatic code rollback;
- hash-locked dependencies and Python 3.10/3.12/3.14 CI.

## Requirements and supported topology

- Linux with systemd (Debian/Ubuntu-like deployment);
- root access for the installer and updater;
- Python 3.10 or newer;
- `git`, `curl`, `flock`, `systemctl`, and at least 512 MiB free space;
- a working 3x-ui JSON subscription;
- Nginx and a TLS certificate for public access.

AutoSub binds to `127.0.0.1:25500` by default. Expose only `/json/` and `/sub/`
through Nginx. Reach `/admin` through an SSH tunnel.

## Installation

| Channel | Intended use | Installed source |
|---|---|---|
| `main` | production | latest published GitHub Release |
| `dev` | test server | exact current `dev` branch |
| `vX.Y.Z` | pinned deployment | exact tag, with no branch fallback |

Do not switch a production host between `main` and `dev` for testing.

### Stable (`main`)

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/main/install.sh | bash
```

### Development (`dev`)

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/dev/install.sh \
  | AUTOSUB_VERSION=dev bash
```

### Pinned release

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/main/install.sh \
  | AUTOSUB_VERSION=v3.1.0 bash
```

The installer resolves `latest` through GitHub Releases, fetches the exact ref,
creates `/opt/autosub-server/{releases,shared}`, installs a per-release venv, and
installs the systemd unit. A fresh install generates random admin and CSRF secrets.

## Initial configuration

Edit the persistent environment:

```bash
nano /opt/autosub-server/shared/.env
```

Review at least:

```dotenv
AUTOSUB_HOST=127.0.0.1
AUTOSUB_PORT=25500
AUTOSUB_TRUSTED_PROXIES=127.0.0.1/32,::1/128

XUI_SUB_URL=https://sub.example.com:2096
XUI_API_URL=https://panel.example.com:54321/secret-path
XUI_API_TOKEN=replace-with-3x-ui-api-token
XUI_TLS_VERIFY=true
```

`XUI_SUB_URL` is the JSON subscription origin. `XUI_API_URL` is the panel/API
origin and often uses a different port or secret path. `XUI_URL` is only a legacy
fallback. An API token is preferred over `XUI_USERNAME`/`XUI_PASSWORD`.

```bash
systemctl restart autosub-server
curl -fsS http://127.0.0.1:25500/health/ready
```

Configure Nginx after obtaining a TLS certificate:

```bash
bash /opt/autosub-server/current/setup_nginx.sh sub.example.com 2097
```

Then set the 3x-ui **JSON reverse proxy URI** to:

```text
https://sub.example.com:2097/json/
```

Open `/sub/REAL_SUB_ID` in a browser to verify the local landing page, and request
`/json/REAL_SUB_ID` to verify the generated subscription. See
[`nginx-example.conf`](nginx-example.conf) for the reverse-proxy layout.

## Dashboard

Create a local tunnel:

```bash
ssh -L 25500:127.0.0.1:25500 root@SERVER_IP
```

Open `http://127.0.0.1:25500/admin` and use the credentials from `shared/.env`.
The dashboard can test the panel API, discover nodes, configure profiles, assign
group rules, and preview a generated subscription.

The **Sites routed directly, without the balancer** field controls the Xray domain
rule inserted into every generated autoselect profile. One `domain:`, `full:`,
`keyword:`, `regexp:`, or `geosite:` rule is accepted per line; blank lines and lines
starting with `#` are ignored. A new or upgraded installation starts with AutoSub's
existing Russian-site list. Saving an empty field removes only this domain rule;
private-IP direct routing and the built-in blocking rules remain active.

## Updating

### Stable (`main` → latest Release)

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/main/update.sh | bash
```

On an installed v3 layout:

```bash
/opt/autosub-server/update.sh
```

### Development (`dev`)

```bash
curl -fsSL https://raw.githubusercontent.com/amirim1/autosub-server/dev/update.sh \
  | AUTOSUB_VERSION=dev bash
```

or:

```bash
AUTOSUB_VERSION=dev /opt/autosub-server/update.sh
```

### Pinned release

```bash
AUTOSUB_VERSION=v3.1.0 /opt/autosub-server/update.sh
```

The updater takes a lock, checks disk/Python requirements, backs up SQLite,
installs hash-locked dependencies, atomically switches `current`, and waits for
`/health/ready`. Failed activation automatically rolls code back. Persistent files
under `shared/` are preserved.

```text
/opt/autosub-server/
├── current -> releases/<release-id>
├── releases/
├── update.sh
└── shared/
    ├── .env
    ├── config.json
    ├── data.db
    ├── autosub.log
    └── backups/
```

A database backup is for manual corruption recovery. A normal code rollback does
not roll SQLite back after a new service has accepted traffic. Verified copies are
stored under `/opt/autosub-server/shared/backups/`.

## Routes

| Method and path | Purpose |
|---|---|
| `GET /json/{sub_id}` | Xray JSON subscription, always |
| `GET /json/{sub_id}?format=singbox` | sing-box client config |
| `GET /json/{sub_id}?format=clash` | Clash.Meta subscription YAML |
| `GET /sub/{sub_id}` | browser landing or JSON by caller type |
| `GET /sub/{sub_id}?format=json` | explicit JSON |
| `GET /sub/{sub_id}?format=html` | explicit local HTML |
| `GET /health` | compatible liveness endpoint |
| `GET /health/live` | explicit liveness endpoint |
| `GET /health/ready` | in-process dependency readiness |

Responses include `X-Request-ID`; rate limits return `429` and `Retry-After`.
See [`docs/API.md`](docs/API.md) for the detailed contract.

### Balancing and geo-sensitive routing

Generated balancer groups avoid session splitting and multi-country IP hopping:

- `sticky_domains` (admin panel) are routed through one fixed node, keeping banks,
  streaming platforms and IP-bound APIs on a stable egress; DNS follows the same path.
- Per-autoselect **country scope** restricts balancing to nodes of a single detected
  country (flag emoji or name tokens), so reconnects never change egress country.
- Health-check intervals are clamped to a 60s floor (`AUTOSUB_MIN_PROBE_INTERVAL`)
  to prevent mid-session node flapping.

## Security notes

- Never expose port `25500` or `/admin` directly to the Internet.
- Never commit `.env`, `data.db`, logs, or real subscription IDs.
- Keep `XUI_TLS_VERIFY=true` unless a deliberate local self-signed upstream requires
  otherwise.
- Trust only the actual immediate Nginx peer in `AUTOSUB_TRUSTED_PROXIES`; world-wide
  networks are rejected at startup.
- Keep `/opt/autosub-server`, the unit, and updater root-owned. The service unit
  isolates the process and permits writes only under `shared/`.
- Add a coarse edge rate limit when deploying behind a public CDN.

See [`SECURITY.md`](SECURITY.md) for private vulnerability reporting.

## Development

```bash
git clone https://github.com/amirim1/autosub-server.git
cd autosub-server
git switch dev
python3 -m venv .venv-development
source .venv-development/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest --cov=. --cov-branch --cov-fail-under=70
ruff check .
pyright
```

`requirements*.in` are dependency inputs; `requirements*.txt` are generated lock
files with hashes. See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for lock updates
and quality gates. CI runs Python 3.10/3.12/current-stable 3.14 tests, coverage, Ruff, Pyright,
pip-audit, Bandit, ShellCheck, and CodeQL.

## Troubleshooting

```bash
systemctl status autosub-server --no-pager
journalctl -u autosub-server -n 200 --no-pager
tail -n 200 /opt/autosub-server/shared/autosub.log
curl -v http://127.0.0.1:25500/health/ready
nginx -t
```

- Subscription `502`: verify `XUI_SUB_URL`; the upstream may have timed out,
  returned non-JSON, or exceeded the response limit.
- Panel API failure: verify `XUI_API_URL`, API token, panel port, and secret path.
- Browser shows JSON: use `/sub/`, not `/json/`, and remove `?format=json`.
- Missing balancers: enable the profiles, select current node IDs, and allow the
  profile IDs for the user's group.
- `429`: wait for `Retry-After` and verify the trusted-proxy configuration.

## Further documentation

- [Architecture](docs/ARCHITECTURE.md)
- [API](docs/API.md)
- [Database and migrations](docs/DATABASE.md)
- [Development](docs/DEVELOPMENT.md)
- [Repository structure and Git workflow](docs/structure.md)
- [Changelog](CHANGELOG.md)
- [GitHub Releases](https://github.com/amirim1/autosub-server/releases)

License: [MIT](LICENSE).
