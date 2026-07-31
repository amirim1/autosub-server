#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${1:-sub.your-domain.com}"
PUBLIC_PORT="${2:-2097}"
AUTOSUB_UPSTREAM="${3:-http://127.0.0.1:25500}"
CERT_PATH="${CERT_PATH:-/etc/letsencrypt/live/$DOMAIN/fullchain.pem}"
KEY_PATH="${KEY_PATH:-/etc/letsencrypt/live/$DOMAIN/privkey.pem}"
SITE_NAME=autosub-json
AVAILABLE="/etc/nginx/sites-available/$SITE_NAME"
ENABLED="/etc/nginx/sites-enabled/$SITE_NAME"

if [ ! -f "$CERT_PATH" ]; then
  echo "Certificate not found: $CERT_PATH" >&2
  exit 1
fi

if [ ! -f "$KEY_PATH" ]; then
  echo "Private key not found: $KEY_PATH" >&2
  exit 1
fi

cat > "$AVAILABLE" <<EOF
server {
    listen $PUBLIC_PORT ssl http2;
    server_name $DOMAIN;

    ssl_certificate $CERT_PATH;
    ssl_certificate_key $KEY_PATH;

    location /json/ {
        proxy_pass $AUTOSUB_UPSTREAM/json/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /sub/ {
        proxy_pass $AUTOSUB_UPSTREAM/sub/;
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

ln -s "$AVAILABLE" "$ENABLED" 2>/dev/null || true
nginx -t
systemctl reload nginx

echo "Nginx AutoSub site enabled:"
echo "https://$DOMAIN:$PUBLIC_PORT/json/ID"
echo
echo "Set this in 3x-ui JSON reverse proxy URI:"
echo "https://$DOMAIN:$PUBLIC_PORT/json/"
