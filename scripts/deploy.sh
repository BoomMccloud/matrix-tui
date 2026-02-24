#!/bin/bash
# deploy.sh — pull latest code, rebuild sandbox image, restart the bot service.
# For human use. The self_update agent tool runs the same steps inline.

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

echo "==> done"
