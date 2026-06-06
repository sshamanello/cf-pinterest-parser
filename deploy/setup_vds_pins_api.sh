#!/usr/bin/env bash
set -euo pipefail

SRC_DIR=${SRC_DIR:-/home/nick/cf-pinterest-parser}
ROOT_DIR=${ROOT_DIR:-/opt/caddy/html/pins/ready}
TOKEN=${TOKEN:-}

if [ -z "$TOKEN" ]; then
  echo "TOKEN is required"
  exit 1
fi

mkdir -p "$ROOT_DIR"
install -m 0644 "$SRC_DIR/deploy/pins_api.py" /opt/caddy/pins_api.py
install -m 0644 "$SRC_DIR/deploy/pins-api.service" /etc/systemd/system/pins-api.service

if ! grep -q '^PINS_API_TOKEN=' /opt/caddy/.env; then
  printf '\nPINS_API_TOKEN=%s\n' "$TOKEN" >> /opt/caddy/.env
else
  sed -i "s/^PINS_API_TOKEN=.*/PINS_API_TOKEN=${TOKEN//\//\\/}/" /opt/caddy/.env
fi

chown -R www-data:www-data "$ROOT_DIR"
systemctl daemon-reload
systemctl enable --now pins-api.service
systemctl restart caddy
systemctl status pins-api.service --no-pager -l
