#!/bin/bash
# deploy.sh — pull latest code, rebuild sandbox image, restart the bot service.
# For human use. The self_update agent tool runs the same steps inline.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="matrix-agent-sandbox:latest"
SERVICE="matrix-agent"

cd "$REPO_DIR"

# Ensure host dependencies
echo "==> checking host dependencies"
if ! command -v podman &>/dev/null; then
    echo "ERROR: podman not found. Install it first." >&2
    exit 1
fi

if ! command -v gh &>/dev/null; then
    echo "==> installing gh CLI"
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
    sudo apt-get update -qq && sudo apt-get install -y -qq gh
fi

# Authenticate gh if needed
if [ -f .env ]; then
    GH_TOKEN=$(grep '^GITHUB_TOKEN' .env | cut -d= -f2- | xargs)
    if [ -n "$GH_TOKEN" ] && ! gh auth status &>/dev/null; then
        echo "==> authenticating gh CLI"
        echo "$GH_TOKEN" | gh auth login --with-token
    fi
fi

echo "==> git pull"
git pull

echo "==> rebuilding sandbox image"
podman build -t "$IMAGE" -f Containerfile .

echo "==> restarting $SERVICE"
systemctl restart "$SERVICE"

echo "==> done"
