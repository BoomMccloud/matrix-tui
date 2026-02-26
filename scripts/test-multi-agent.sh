#!/bin/bash
# Test that both coding CLIs are installed, configured, and respond to -p mode.
# Usage: bash scripts/test-multi-agent.sh
#
# Requires:
#   - Rebuilt image: podman build -t matrix-agent-sandbox:latest -f Containerfile .
#   - Environment vars: GEMINI_API_KEY, DASHSCOPE_API_KEY (optional — tests auth config even without keys)
set -euo pipefail

# Load .env if present (same file the bot uses via pydantic-settings)
# Strip comments and normalize "KEY = value" → "KEY=value" for bash
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    eval "$(grep -v '^\s*#' "$SCRIPT_DIR/.env" | grep '=' | sed 's/ *= */=/')"
    set +a
fi

CONTAINER="test-multi-agent"
IMAGE="matrix-agent-sandbox:latest"

cleanup() {
    echo ""
    echo "Cleaning up..."
    podman stop "$CONTAINER" 2>/dev/null || true
    podman rm "$CONTAINER" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== Starting container ==="
ENV_FLAGS=()
if [ -n "${GEMINI_API_KEY:-}" ]; then
    ENV_FLAGS+=("-e" "GEMINI_API_KEY=$GEMINI_API_KEY")
    echo "  GEMINI_API_KEY: set"
else
    echo "  GEMINI_API_KEY: not set (gemini will fail auth)"
fi
if [ -n "${DASHSCOPE_API_KEY:-}" ]; then
    ENV_FLAGS+=("-e" "DASHSCOPE_API_KEY=$DASHSCOPE_API_KEY")
    echo "  DASHSCOPE_API_KEY: set"
else
    echo "  DASHSCOPE_API_KEY: not set (qwen will fail auth)"
fi
podman run -d --name "$CONTAINER" "${ENV_FLAGS[@]}" "$IMAGE" sleep infinity

# --- Verify env vars inside container ---

echo ""
echo "=== Verifying env vars in container ==="
if [ -n "${GEMINI_API_KEY:-}" ]; then
    if podman exec "$CONTAINER" sh -c 'test -n "$GEMINI_API_KEY"' 2>/dev/null; then
        echo "PASS: GEMINI_API_KEY visible in container"
    else
        echo "FAIL: GEMINI_API_KEY was set but not visible in container"
        exit 1
    fi
fi
if [ -n "${DASHSCOPE_API_KEY:-}" ]; then
    if podman exec "$CONTAINER" sh -c 'test -n "$DASHSCOPE_API_KEY"' 2>/dev/null; then
        echo "PASS: DASHSCOPE_API_KEY visible in container"
    else
        echo "FAIL: DASHSCOPE_API_KEY was set but not visible in container"
        exit 1
    fi
fi

# --- Binary checks ---

echo ""
echo "=== Checking Gemini CLI is installed ==="
if podman exec "$CONTAINER" which gemini >/dev/null 2>&1; then
    echo "PASS: gemini found at $(podman exec "$CONTAINER" which gemini)"
else
    echo "FAIL: gemini not found"
    exit 1
fi

echo ""
echo "=== Checking Qwen Code is installed ==="
if podman exec "$CONTAINER" which qwen >/dev/null 2>&1; then
    echo "PASS: qwen found at $(podman exec "$CONTAINER" which qwen)"
else
    echo "FAIL: qwen not found"
    exit 1
fi

# --- Write Qwen settings (mirrors _init_workspace in sandbox.py) ---

echo ""
echo "=== Writing Qwen auth config ==="
podman exec "$CONTAINER" mkdir -p /root/.qwen
podman exec -i "$CONTAINER" sh -c 'cat > /root/.qwen/settings.json' <<'JSON'
{
  "modelProviders": {
    "openai": [
      {
        "id": "qwen3-coder-next",
        "name": "qwen3-coder-next",
        "baseUrl": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "envKey": "DASHSCOPE_API_KEY"
      }
    ]
  },
  "security": { "auth": { "selectedType": "openai" } },
  "model": { "name": "qwen3-coder-next" }
}
JSON
echo "PASS: settings.json written"

# --- Smoke test -p mode ---

echo ""
echo "=== Testing Gemini CLI -p ==="
output=$(podman exec "$CONTAINER" timeout 30 gemini -p "respond with exactly: GEMINI_OK" 2>&1 || true)
echo "$output" | head -5
if echo "$output" | grep -qi "GEMINI_OK"; then
    echo "PASS: gemini produced expected output"
else
    echo "FAIL: gemini did not produce expected output"
    exit 1
fi

echo ""
echo "=== Testing Qwen Code -p ==="
output=$(podman exec "$CONTAINER" timeout 30 qwen -p "respond with exactly: QWEN_OK" 2>&1 || true)
echo "$output" | head -5
if echo "$output" | grep -qi "QWEN_OK"; then
    echo "PASS: qwen produced expected output"
else
    echo "FAIL: qwen did not produce expected output"
    exit 1
fi

echo ""
echo "=== All checks passed ==="
