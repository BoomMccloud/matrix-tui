#!/bin/bash
# deploy.sh — pull latest code, rebuild sandbox image, restart the bot service.
# Used by both humans (run directly) and the self_update agent tool.

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
