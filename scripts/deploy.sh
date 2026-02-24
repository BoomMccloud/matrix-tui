#!/bin/bash
# deploy.sh — pull latest code, rebuild sandbox image, restart the bot service.
# Run from /home/matrix-tui on the VPS as root.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="matrix-agent-sandbox:latest"
SERVICE="matrix-agent"

cd "$REPO_DIR"

echo "==> git pull"
git pull

echo "==> rebuilding sandbox image"
podman build -t "$IMAGE" -f Containerfile .

echo "==> restarting $SERVICE"
systemctl restart "$SERVICE"

echo "==> waiting for service to come up..."
sleep 3
systemctl status "$SERVICE" --no-pager
