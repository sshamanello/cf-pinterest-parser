#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=${PROJECT_DIR:-/home/nick/cf-pinterest-parser}
SERVICE_NAME=${SERVICE_NAME:-cf-pinterest-scheduler.service}
USER_NAME=${USER_NAME:-$USER}

sudo apt-get update
sudo apt-get install -y adb python3-venv

mkdir -p "$PROJECT_DIR/logs"
python3 -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

sudo cp "$PROJECT_DIR/deploy/99-android-xiaomi.rules" /etc/udev/rules.d/99-android-xiaomi.rules
sudo udevadm control --reload-rules
sudo udevadm trigger

sudo cp "$PROJECT_DIR/deploy/cf-pinterest-scheduler.service" /etc/systemd/system/$SERVICE_NAME
sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE_NAME
sudo systemctl status $SERVICE_NAME --no-pager
