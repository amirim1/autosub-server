#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/autosub-server

if [ -f "$APP_DIR/autosub_server.py" ]; then
  bash "$SRC_DIR/update.sh"
else
  bash "$SRC_DIR/install.sh"
fi

bash "$APP_DIR/setup_nginx.sh"

echo
echo "AutoSub service:"
systemctl --no-pager --full status autosub-server || true

echo
echo "Health:"
curl -fsS http://127.0.0.1:25500/health || true

echo
echo "Next:"
echo "1. Check /opt/autosub-server/.env"
echo "2. Set 3x-ui JSON reverse proxy URI: https://sub.your-domain.com:2097/json/"
echo "3. Open dashboard through SSH tunnel: http://127.0.0.1:25500/admin"
