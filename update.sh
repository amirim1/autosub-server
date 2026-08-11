#!/usr/bin/env bash
set -euo pipefail
umask 077

APP_DIR="${AUTOSUB_ROOT:-/opt/autosub-server}"
SERVICE_NAME="${AUTOSUB_SERVICE_NAME:-autosub-server}"
SERVICE_UNIT="${AUTOSUB_SERVICE_UNIT:-/etc/systemd/system/autosub-server.service}"
PYTHON="${AUTOSUB_DEPLOY_PYTHON:-python3}"
HEALTH_PORT="${AUTOSUB_PORT:-}"
HEALTH_TIMEOUT="${AUTOSUB_HEALTH_TIMEOUT:-45}"
KEEP_RELEASES="${AUTOSUB_KEEP_RELEASES:-3}"
MIN_FREE_KB="${AUTOSUB_MIN_FREE_KB:-524288}"
SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
if [ -n "$SCRIPT_SOURCE" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" 2>/dev/null && pwd)"
fi
TMP_DIR=""

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    case "$TMP_DIR" in
      "$APP_DIR"/releases/.source-*) rm -rf -- "$TMP_DIR" ;;
      *) echo "Refusing to remove unexpected temporary path" >&2 ;;
    esac
  fi
}
trap cleanup EXIT

fail() {
  echo "AutoSub update failed: $*" >&2
  exit 1
}

if [ "${AUTOSUB_SKIP_ROOT_CHECK:-0}" != "1" ] && [ "$(id -u)" -ne 0 ]; then
  fail "run this updater as root"
fi
if [ "$(uname -s)" != "Linux" ]; then
  fail "Linux is required"
fi

for command in "$PYTHON" git curl flock systemctl df install mktemp grep awk sed head chmod mv dirname sleep; do
  command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  fail "Python 3.10 or newer is required"
fi

install -d -m 700 \
  "$APP_DIR" "$APP_DIR/releases" "$APP_DIR/shared" "$APP_DIR/shared/backups"
exec 9>"$APP_DIR/.update.lock"
if ! flock -n 9; then
  fail "update already in progress"
fi

resolve_source() {
  if [ -n "${AUTOSUB_SOURCE_DIR:-}" ]; then
    SRC_DIR="$AUTOSUB_SOURCE_DIR"
    return
  fi
  if [ -n "$SCRIPT_DIR" ] \
    && [ -f "$SCRIPT_DIR/autosub_server.py" ] \
    && [ -f "$SCRIPT_DIR/release_manager.py" ] \
    && [ -f "$SCRIPT_DIR/runtime-manifest.txt" ]; then
    SRC_DIR="$SCRIPT_DIR"
    return
  fi

  TMP_DIR="$(mktemp -d "$APP_DIR/releases/.source-XXXXXXXX")"
  : >"$TMP_DIR/.autosub-source"
  local requested="${AUTOSUB_VERSION:-latest}"
  if [ "$requested" = "latest" ]; then
    if ! requested="$(
      curl --fail --location --silent --show-error \
        https://api.github.com/repos/amirim1/autosub-server/releases/latest \
        | sed -nE 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' \
        | head -n 1
    )"; then
      fail "could not resolve the latest release tag"
    fi
    [ -n "$requested" ] || fail "latest release metadata has no tag"
  fi
  if [[ ! "$requested" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    fail "unsafe version/ref"
  fi
  echo "Fetching AutoSub source ref: $requested" >&2
  git clone --quiet --depth 1 --branch "$requested" \
    https://github.com/amirim1/autosub-server.git "$TMP_DIR/checkout" \
    || fail "could not fetch the exact requested ref"
  SRC_DIR="$TMP_DIR/checkout"
}

wait_local_health() {
  local path="$1"
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl --fail --silent --show-error --max-time 2 \
      "http://127.0.0.1:${HEALTH_PORT}${path}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

install_service_unit() {
  local source="$1"
  local unit_dir
  unit_dir="$(dirname "$SERVICE_UNIT")"
  local temporary
  temporary="$(mktemp "$unit_dir/.autosub-server.service.XXXXXX")"
  install -m 600 "$source" "$temporary"
  mv -f -- "$temporary" "$SERVICE_UNIT"
  systemctl daemon-reload
}

validate_service_unit() {
  local unit="$1"
  grep -Fqx 'WorkingDirectory=/opt/autosub-server/current' "$unit" \
    || fail "systemd unit has an unexpected WorkingDirectory"
  grep -Fqx 'EnvironmentFile=/opt/autosub-server/shared/.env' "$unit" \
    || fail "systemd unit has an unexpected EnvironmentFile"
  grep -Fqx \
    'ExecStart=/opt/autosub-server/current/venv/bin/python /opt/autosub-server/current/autosub_server.py' \
    "$unit" || fail "systemd unit has an unexpected ExecStart"
  if grep -Eq '^(User|Group)=autosub$' "$unit"; then
    fail "dedicated autosub service identity is not supported"
  fi
}

ensure_shared_defaults() {
  local source="$1"
  if [ ! -f "$APP_DIR/shared/.env" ]; then
    install -m 600 "$source/.env.example" "$APP_DIR/shared/.env"
    "$PYTHON" - "$APP_DIR/shared/.env" <<'PY'
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
csrf_secret = secrets.token_urlsafe(48)
admin_password = secrets.token_urlsafe(24)
updated = [
    f"AUTOSUB_SECRET_KEY={csrf_secret}"
    if line.startswith("AUTOSUB_SECRET_KEY=")
    else f"AUTOSUB_ADMIN_PASSWORD={admin_password}"
    if line.startswith("AUTOSUB_ADMIN_PASSWORD=")
    else line
    for line in lines
]
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
PY
  fi
  if [ ! -f "$APP_DIR/shared/config.json" ]; then
    install -m 600 "$source/config.example.json" "$APP_DIR/shared/config.json"
  fi
  chmod 600 "$APP_DIR/shared/.env" "$APP_DIR/shared/config.json"
}

check_disk_space() {
  if [[ ! "$MIN_FREE_KB" =~ ^[0-9]+$ ]]; then
    fail "AUTOSUB_MIN_FREE_KB must be an integer"
  fi
  local free_kb
  free_kb="$(df -Pk "$APP_DIR" | awk 'NR == 2 {print $4}')"
  if [ -z "$free_kb" ] || [ "$free_kb" -lt "$MIN_FREE_KB" ]; then
    fail "insufficient free disk space (need at least ${MIN_FREE_KB} KiB)"
  fi
}

rollback_to_flat_service() {
  local unit_backup="$1"
  if [ -f "$unit_backup" ]; then
    install_service_unit "$unit_backup" || true
  fi
  systemctl restart "$SERVICE_NAME" || true
}

SRC_DIR=""
check_disk_space
resolve_source
[ -f "$SRC_DIR/release_manager.py" ] || fail "release manager missing from source"
[ -f "$SRC_DIR/runtime-manifest.txt" ] || fail "runtime manifest missing from source"
[ -f "$SRC_DIR/autosub-server.service" ] || fail "systemd unit missing from source"
MANAGER="$SRC_DIR/release_manager.py"
MANIFEST="$SRC_DIR/runtime-manifest.txt"
validate_service_unit "$SRC_DIR/autosub-server.service"

"$PYTHON" "$MANAGER" cleanup-staging "$APP_DIR" --preserve "$SRC_DIR"

PORT_ENV="$APP_DIR/shared/.env"
if [ ! -f "$PORT_ENV" ] && [ -f "$APP_DIR/.env" ]; then
  PORT_ENV="$APP_DIR/.env"
fi
HEALTH_PORT="$(
  "$PYTHON" "$MANAGER" port "$PORT_ENV" --override "$HEALTH_PORT" --default 25500
)"
"$PYTHON" "$MANAGER" recover "$APP_DIR" \
  --service "$SERVICE_NAME" --port "$HEALTH_PORT" --timeout "$HEALTH_TIMEOUT"

TARGET_REQUEST="${AUTOSUB_VERSION:-}"
RELEASE_ID="$(
  "$PYTHON" "$MANAGER" release-id "$SRC_DIR" "$MANIFEST" \
    --requested "$TARGET_REQUEST"
)"
echo "Preparing release: $RELEASE_ID"
"$PYTHON" "$MANAGER" prepare \
  "$APP_DIR" "$SRC_DIR" "$MANIFEST" "$RELEASE_ID" --python "$PYTHON"

LEGACY_LAYOUT=0
if [ ! -L "$APP_DIR/current" ] && [ -f "$APP_DIR/autosub_server.py" ]; then
  LEGACY_LAYOUT=1
fi

if [ "$LEGACY_LAYOUT" -eq 1 ]; then
  echo "Migrating legacy flat installation"
  LEGACY_ID="$("$PYTHON" "$MANAGER" legacy-id "$APP_DIR")"
  "$PYTHON" "$MANAGER" prepare \
    "$APP_DIR" "$APP_DIR" "$MANIFEST" "$LEGACY_ID" \
    --python "$PYTHON" --allow-missing \
    --requirements-lock "$SRC_DIR/requirements.txt"

  UNIT_BACKUP="$APP_DIR/shared/backups/autosub-server.service-pre-layout"
  if [ -f "$SERVICE_UNIT" ] && [ ! -f "$UNIT_BACKUP" ]; then
    install -m 600 "$SERVICE_UNIT" "$UNIT_BACKUP"
  fi
  systemctl stop "$SERVICE_NAME"
  if ! "$PYTHON" "$MANAGER" migrate-legacy "$APP_DIR" "$LEGACY_ID"; then
    rollback_to_flat_service "$UNIT_BACKUP"
    fail "legacy persistent-data migration failed; flat service was restarted"
  fi
  ensure_shared_defaults "$SRC_DIR"
  if ! install_service_unit "$SRC_DIR/autosub-server.service" \
    || ! "$PYTHON" "$MANAGER" switch "$APP_DIR" "$LEGACY_ID" \
    || ! systemctl restart "$SERVICE_NAME" \
    || ! wait_local_health "/health"; then
    rollback_to_flat_service "$UNIT_BACKUP"
    fail "legacy layout activation failed; flat service restart was attempted"
  fi
  echo "Legacy release active: $LEGACY_ID"
else
  ensure_shared_defaults "$SRC_DIR"
fi

PREVIOUS_RELEASE="$(
  "$PYTHON" "$MANAGER" current "$APP_DIR" --optional
)"
install_service_unit "$SRC_DIR/autosub-server.service"

set +e
"$PYTHON" "$MANAGER" activate "$APP_DIR" "$RELEASE_ID" \
  --service "$SERVICE_NAME" --port "$HEALTH_PORT" --timeout "$HEALTH_TIMEOUT"
ACTIVATION_STATUS=$?
set -e
if [ "$ACTIVATION_STATUS" -ne 0 ]; then
  if [ "$ACTIVATION_STATUS" -eq 20 ]; then
    echo "UPDATE_FAILED_ROLLBACK_SUCCEEDED" >&2
  else
    echo "UPDATE_FAILED_ROLLBACK_FAILED" >&2
  fi
  exit "$ACTIVATION_STATUS"
fi

systemctl enable "$SERVICE_NAME"
"$PYTHON" "$MANAGER" retain "$APP_DIR" \
  --keep "$KEEP_RELEASES" --previous "$PREVIOUS_RELEASE"

UPDATER_TEMP="$APP_DIR/.update.sh.new"
install -m 700 "$SRC_DIR/update.sh" "$UPDATER_TEMP"
mv -f -- "$UPDATER_TEMP" "$APP_DIR/update.sh"

echo "AutoSub update succeeded"
echo "Active release: $RELEASE_ID"
echo "Previous release: ${PREVIOUS_RELEASE:-none}"
echo "Readiness: http://127.0.0.1:${HEALTH_PORT}/health/ready"
