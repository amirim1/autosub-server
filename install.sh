#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
if [ -n "$SCRIPT_SOURCE" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" 2>/dev/null && pwd)"
fi
APP_DIR="${AUTOSUB_ROOT:-/opt/autosub-server}"
TMP_DIR=""

fail() {
  echo "AutoSub installation failed: $*" >&2
  exit 1
}

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    case "$TMP_DIR" in
      "$APP_DIR"/releases/.source-*) rm -rf -- "$TMP_DIR" ;;
      *) echo "Refusing to remove unexpected temporary path" >&2 ;;
    esac
  fi
}
trap cleanup EXIT

if [ "${AUTOSUB_SKIP_ROOT_CHECK:-0}" != "1" ] && [ "$(id -u)" -ne 0 ]; then
  fail "run this installer as root"
fi
if [ "$(uname -s)" != "Linux" ]; then
  fail "Linux is required"
fi

if [ -n "$SCRIPT_DIR" ] \
  && [ -f "$SCRIPT_DIR/update.sh" ] \
  && [ -f "$SCRIPT_DIR/autosub_server.py" ] \
  && [ -f "$SCRIPT_DIR/release_manager.py" ] \
  && [ -f "$SCRIPT_DIR/runtime-manifest.txt" ]; then
  AUTOSUB_SOURCE_DIR="$SCRIPT_DIR" bash "$SCRIPT_DIR/update.sh"
  exit $?
fi

for command in git curl sed head install mktemp; do
  command -v "$command" >/dev/null 2>&1 || {
    fail "required command not found: $command"
  }
done

TARGET_VER="${AUTOSUB_VERSION:-latest}"
if [ "$TARGET_VER" = "latest" ]; then
  if ! TARGET_VER="$(
    curl --fail --location --silent --show-error \
      https://api.github.com/repos/amirim1/autosub-server/releases/latest \
      | sed -nE 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' \
      | head -n 1
  )"; then
    echo "Could not resolve the latest AutoSub release tag" >&2
    exit 1
  fi
  if [ -z "$TARGET_VER" ]; then
    echo "Latest AutoSub release metadata has no tag" >&2
    exit 1
  fi
fi
if [[ ! "$TARGET_VER" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "Unsafe AutoSub version/ref" >&2
  exit 1
fi

install -d -m 700 "$APP_DIR" "$APP_DIR/releases"
TMP_DIR="$(mktemp -d "$APP_DIR/releases/.source-XXXXXXXX")"
: >"$TMP_DIR/.autosub-source"
git clone --quiet --depth 1 --branch "$TARGET_VER" \
  https://github.com/amirim1/autosub-server.git "$TMP_DIR/checkout" \
  || {
    echo "Could not fetch the exact requested AutoSub ref" >&2
    exit 1
  }

AUTOSUB_SOURCE_DIR="$TMP_DIR/checkout" AUTOSUB_VERSION="$TARGET_VER" \
  bash "$TMP_DIR/checkout/update.sh"
