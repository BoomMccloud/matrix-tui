#!/bin/bash
# deploy.sh — pull latest code, rebuild sandbox image, restart the bot service.
# For human use. The self_update agent tool runs the same steps inline.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="matrix-agent-sandbox:latest"
SERVICE="matrix-agent"

cd "$REPO_DIR"

# Ensure host dependencies are installed
echo "==> checking host dependencies"
for cmd in podman gh; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found on host. Install it first." >&2
        exit 1
    fi
done

# Ensure gh is authenticated if GITHUB_TOKEN is set
if [ -n "$GITHUB_TOKEN" ] || grep -q 'GITHUB_TOKEN' .env 2>/dev/null; then
    if ! gh auth status &>/dev/null; then
        echo "==> authenticating gh CLI with GITHUB_TOKEN"
        # Source .env to get the token if not already exported
        if [ -z "$GITHUB_TOKEN" ] && [ -f .env ]; then
            GITHUB_TOKEN=$(grep '^GITHUB_TOKEN' .env | cut -d= -f2- | xargs)
        fi
        if [ -n "$GITHUB_TOKEN" ]; then
            echo "$GITHUB_TOKEN" | gh auth login --with-token
        else
            echo "WARNING: GITHUB_TOKEN not set, gh CLI not authenticated" >&2
        fi
    fi
fi

echo "==> git pull"
git pull

echo "==> rebuilding sandbox image"
podman build -t "$IMAGE" -f Containerfile .

echo "==> restarting $SERVICE"
systemctl restart "$SERVICE"

echo "==> done"
