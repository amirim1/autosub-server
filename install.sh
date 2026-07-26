#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/autosub-server
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE:-$0}")" 2>/dev/null && pwd || echo "")"

if [ -z "$SRC_DIR" ] || [ ! -f "$SRC_DIR/autosub_server.py" ]; then
  echo "Fetching AutoSub Server source files from GitHub..."
  TMP_DIR="$(mktemp -d)"
  if command -v git &>/dev/null; then
    git clone --depth 1 https://github.com/amirim1/autosub-server.git "$TMP_DIR"
  else
    curl -fsSL "https://github.com/amirim1/autosub-server/archive/refs/heads/main.tar.gz?t=$(date +%s)" | tar -xz -C "$TMP_DIR" --strip-components=1
  fi
  SRC_DIR="$TMP_DIR"
fi

mkdir -p "$APP_DIR"

install_file() {
  local src="$1"
  local dst="$2"
  if [ "$(readlink -f "$src")" != "$(readlink -f "$dst" 2>/dev/null || true)" ]; then
    cp "$src" "$dst"
  fi
}

install_file "$SRC_DIR/autosub_server.py" "$APP_DIR/autosub_server.py"
install_file "$SRC_DIR/config.py" "$APP_DIR/config.py"
install_file "$SRC_DIR/storage.py" "$APP_DIR/storage.py"
install_file "$SRC_DIR/fingerprint.py" "$APP_DIR/fingerprint.py"
install_file "$SRC_DIR/api_client.py" "$APP_DIR/api_client.py"
install_file "$SRC_DIR/builder.py" "$APP_DIR/builder.py"
install_file "$SRC_DIR/dashboard.py" "$APP_DIR/dashboard.py"
install_file "$SRC_DIR/logger.py" "$APP_DIR/logger.py"
install_file "$SRC_DIR/nginx-example.conf" "$APP_DIR/nginx-example.conf"
install_file "$SRC_DIR/README.md" "$APP_DIR/README.md"
install_file "$SRC_DIR/setup_nginx.sh" "$APP_DIR/setup_nginx.sh"
install_file "$SRC_DIR/finish_setup.sh" "$APP_DIR/finish_setup.sh"
install_file "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"

# Install python dependencies
echo "Setting up Python virtual environment..."
if [ ! -d "$APP_DIR/venv" ]; then
  if ! python3 -c "import venv" &>/dev/null; then
    echo "Installing python3-venv package..."
    apt-get update -y && apt-get install -y python3-venv || true
  fi
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Copy static assets
mkdir -p "$APP_DIR/static"
if [ -d "$SRC_DIR/static" ]; then
  cp -r "$SRC_DIR/static/"* "$APP_DIR/static/" 2>/dev/null || true
fi

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$SRC_DIR/.env.example" "$APP_DIR/.env"
fi

if [ ! -f "$APP_DIR/config.json" ]; then
  cp "$SRC_DIR/config.example.json" "$APP_DIR/config.json"
fi

python3 - <<'PY'
import json
from pathlib import Path
p = Path("/opt/autosub-server/config.json")
data = json.loads(p.read_text(encoding="utf-8"))
changed = False
for item in data.get("autoselects", []):
    if item.get("id") == "all" and not item.get("selected_node_ids"):
        item["selected_node_ids"] = ["*"]
        changed = True
if changed:
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
PY

cp "$SRC_DIR/autosub-server.service" /etc/systemd/system/autosub-server.service
chmod 600 "$APP_DIR/.env"
chmod +x "$APP_DIR/setup_nginx.sh"
chmod +x "$APP_DIR/finish_setup.sh"
grep -q '^XUI_SUB_URL=' "$APP_DIR/.env" || echo 'XUI_SUB_URL=https://YOUR_DOMAIN:2096' >> "$APP_DIR/.env"
grep -q '^XUI_API_URL=' "$APP_DIR/.env" || echo 'XUI_API_URL=https://YOUR_DOMAIN:PANEL_PORT' >> "$APP_DIR/.env"
grep -q '^XUI_API_TOKEN=' "$APP_DIR/.env" || echo 'XUI_API_TOKEN=' >> "$APP_DIR/.env"
grep -q '^AUTOSUB_ADMIN_PASSWORD=' "$APP_DIR/.env" || echo 'AUTOSUB_ADMIN_PASSWORD=' >> "$APP_DIR/.env"
systemctl daemon-reload
systemctl enable autosub-server
systemctl restart autosub-server

echo "Installed AutoSub."
echo "Edit secrets: nano $APP_DIR/.env"
echo "Restart: systemctl restart autosub-server"
echo "Dashboard tunnel: ssh -L 25500:127.0.0.1:25500 root@SERVER"
echo "Dashboard URL: http://127.0.0.1:25500/admin"
