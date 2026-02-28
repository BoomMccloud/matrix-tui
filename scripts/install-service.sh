#!/bin/bash
# install-service.sh — build sandbox image and install matrix-agent systemd service.
# Run as root from the repo directory on the VPS, after setup-synapse.sh.
# Reads .env for API keys (validates they are set).

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
IMAGE="matrix-agent-sandbox:latest"
SERVICE_FILE="/etc/systemd/system/matrix-agent.service"

# ------------------------------------------------------------------ #
# Read .env
# ------------------------------------------------------------------ #
get_env() {
    awk -F' *= *' "/^$1/{print \$2}" "$ENV_FILE" | tr -d '[:space:]"'"'"
}

echo "==> Checking .env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env not found. Run: cp .env.example .env && nano .env"
    exit 1
fi

# Validate required keys
missing=()
for key in MATRIX_PASSWORD LLM_API_KEY GEMINI_API_KEY; do
    val=$(get_env "$key")
    if [[ -z "$val" ]]; then
        missing+=("$key")
    fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: Missing required keys in .env: ${missing[*]}"
    exit 1
fi

# Optional keys — warn if missing
for key in DASHSCOPE_API_KEY GITHUB_TOKEN; do
    val=$(get_env "$key")
    if [[ -z "$val" ]]; then
        echo "  WARN: $key not set (optional but recommended)"
    fi
done

echo "  .env OK"

# ------------------------------------------------------------------ #
# Check prerequisites
# ------------------------------------------------------------------ #
echo "==> Checking prerequisites"

if ! command -v podman &>/dev/null; then
    echo "ERROR: podman not found. Install it first."
    exit 1
fi

if ! command -v uv &>/dev/null; then
    echo "ERROR: uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "  podman: $(podman --version)"
echo "  uv: $(uv --version)"

# ------------------------------------------------------------------ #
# Build sandbox image
# ------------------------------------------------------------------ #
echo "==> Building sandbox image: $IMAGE"
cd "$REPO_DIR"
podman build -t "$IMAGE" -f Containerfile .
echo "  Image built OK"

# ------------------------------------------------------------------ #
# Install systemd service
# ------------------------------------------------------------------ #
echo "==> Installing $SERVICE_FILE"

# Detect uv path
UV_PATH=$(command -v uv)

# Check if synapse service exists for dependency
AFTER="network.target"
REQUIRES=""
if systemctl list-unit-files synapse.service &>/dev/null; then
    AFTER="network.target synapse.service"
    REQUIRES="Requires=synapse.service"
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Matrix Agent Bot
After=$AFTER
$REQUIRES

[Service]
Type=simple
Restart=on-failure
RestartSec=5s
WorkingDirectory=$REPO_DIR
ExecStart=$UV_PATH run python -m matrix_agent

[Install]
WantedBy=multi-user.target
EOF

echo "  Service file written"

# ------------------------------------------------------------------ #
# Enable and start
# ------------------------------------------------------------------ #
echo "==> Enabling and starting matrix-agent"
systemctl daemon-reload
systemctl enable matrix-agent
systemctl restart matrix-agent

echo ""
echo "==> matrix-agent is running!"
echo ""
echo "Check status:  systemctl status matrix-agent"
echo "View logs:     journalctl -u matrix-agent -f"
echo "Redeploy:      bash scripts/deploy.sh"
