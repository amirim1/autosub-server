#!/usr/bin/env bash
set -euo pipefail
umask 077

DOMAIN="${1:-sub.your-domain.com}"
PUBLIC_PORT="${2:-2097}"
AUTOSUB_UPSTREAM="${3:-http://127.0.0.1:25500}"
CERT_PATH="${CERT_PATH:-/etc/letsencrypt/live/$DOMAIN/fullchain.pem}"
KEY_PATH="${KEY_PATH:-/etc/letsencrypt/live/$DOMAIN/privkey.pem}"
SITE_NAME=autosub-json
AVAILABLE="/etc/nginx/sites-available/$SITE_NAME"
ENABLED="/etc/nginx/sites-enabled/$SITE_NAME"
TEMP_CONFIG=""
BACKUP_CONFIG=""
CREATED_ENABLED=0

fail() {
  echo "AutoSub Nginx setup failed: $*" >&2
  exit 1
}

cleanup() {
  [ -z "$TEMP_CONFIG" ] || rm -f -- "$TEMP_CONFIG"
  [ -z "$BACKUP_CONFIG" ] || rm -f -- "$BACKUP_CONFIG"
}
trap cleanup EXIT

[ "$(uname -s)" = "Linux" ] || fail "Linux is required"
[ "$(id -u)" -eq 0 ] || fail "run this script as root"
for command in nginx systemctl mktemp mv cp chmod readlink; do
  command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done

if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] \
  || [[ "$DOMAIN" == *..* ]]; then
  fail "domain contains unsafe characters"
fi
IFS='.' read -r -a DOMAIN_LABELS <<< "$DOMAIN"
for label in "${DOMAIN_LABELS[@]}"; do
  if [[ ! "$label" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]]; then
    fail "domain contains an invalid label"
  fi
done
if [[ ! "$PUBLIC_PORT" =~ ^[0-9]{1,5}$ ]] \
  || [ "$PUBLIC_PORT" -lt 1 ] \
  || [ "$PUBLIC_PORT" -gt 65535 ]; then
  fail "public port must be between 1 and 65535"
fi
if [[ "$AUTOSUB_UPSTREAM" =~ ^http://(127\.0\.0\.1|localhost):([0-9]{1,5})$ ]]; then
  UPSTREAM_PORT="${BASH_REMATCH[2]}"
else
  fail "upstream must be an HTTP loopback URL with an explicit port"
fi
if [ "$UPSTREAM_PORT" -lt 1 ] || [ "$UPSTREAM_PORT" -gt 65535 ]; then
  fail "upstream port must be between 1 and 65535"
fi
for tls_path in "$CERT_PATH" "$KEY_PATH"; do
  if [[ ! "$tls_path" =~ ^/[A-Za-z0-9._/@+-]+$ ]]; then
    fail "TLS paths must be absolute and contain only safe characters"
  fi
done

[ -f "$CERT_PATH" ] || fail "certificate not found: $CERT_PATH"
[ -f "$KEY_PATH" ] || fail "private key not found: $KEY_PATH"

if [ -e "$ENABLED" ] && [ ! -L "$ENABLED" ]; then
  fail "$ENABLED exists and is not a symlink"
fi
if [ -e "$AVAILABLE" ] && [ ! -f "$AVAILABLE" ]; then
  fail "$AVAILABLE exists and is not a regular file"
fi
if [ -L "$ENABLED" ]; then
  ENABLED_TARGET="$(readlink "$ENABLED")"
  if [ "$ENABLED_TARGET" != "$AVAILABLE" ] \
    && [ "$ENABLED_TARGET" != "../sites-available/$SITE_NAME" ]; then
    fail "$ENABLED points to an unexpected target"
  fi
fi

TEMP_CONFIG="$(mktemp "/etc/nginx/sites-available/.${SITE_NAME}.XXXXXXXX")"
cat > "$TEMP_CONFIG" <<EOF
server {
    listen $PUBLIC_PORT ssl http2;
    server_name $DOMAIN;
    server_tokens off;

    ssl_certificate $CERT_PATH;
    ssl_certificate_key $KEY_PATH;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location /json/ {
        proxy_pass $AUTOSUB_UPSTREAM/json/;
        proxy_http_version 1.1;
        proxy_connect_timeout 5s;
        proxy_send_timeout 15s;
        proxy_read_timeout 45s;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /sub/ {
        proxy_pass $AUTOSUB_UPSTREAM/sub/;
        proxy_http_version 1.1;
        proxy_connect_timeout 5s;
        proxy_send_timeout 15s;
        proxy_read_timeout 45s;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location / {
        return 404;
    }
}
EOF
chmod 0644 "$TEMP_CONFIG"

if [ -f "$AVAILABLE" ]; then
  BACKUP_CONFIG="$(mktemp "/etc/nginx/sites-available/.${SITE_NAME}.backup.XXXXXXXX")"
  cp -p -- "$AVAILABLE" "$BACKUP_CONFIG"
fi
mv -f -- "$TEMP_CONFIG" "$AVAILABLE"
TEMP_CONFIG=""

if [ ! -L "$ENABLED" ]; then
  ln -s "$AVAILABLE" "$ENABLED"
  CREATED_ENABLED=1
fi

if ! nginx -t; then
  if [ -n "$BACKUP_CONFIG" ]; then
    mv -f -- "$BACKUP_CONFIG" "$AVAILABLE"
    BACKUP_CONFIG=""
  else
    rm -f -- "$AVAILABLE"
  fi
  if [ "$CREATED_ENABLED" -eq 1 ]; then
    rm -f -- "$ENABLED"
  fi
  fail "nginx rejected the generated configuration; previous state restored"
fi

if [ -n "$BACKUP_CONFIG" ]; then
  rm -f -- "$BACKUP_CONFIG"
  BACKUP_CONFIG=""
fi
systemctl reload nginx || fail "nginx configuration is valid but reload failed"

echo "Nginx AutoSub site enabled:"
echo "https://$DOMAIN:$PUBLIC_PORT/json/ID"
echo "https://$DOMAIN:$PUBLIC_PORT/sub/ID"
echo
echo "Set this in 3x-ui JSON reverse proxy URI:"
echo "https://$DOMAIN:$PUBLIC_PORT/json/"
