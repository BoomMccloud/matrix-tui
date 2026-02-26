#!/bin/bash
# Test the Gemini Notification hook + IPC file flow locally.
# Usage: bash scripts/test-ipc-hook.sh
set -euo pipefail

CONTAINER="test-hook"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IPC_DIR="$SCRIPT_DIR/.test-ipc"
IMAGE="matrix-agent-sandbox:latest"

cleanup() {
    echo "Cleaning up..."
    podman stop "$CONTAINER" 2>/dev/null || true
    podman rm "$CONTAINER" 2>/dev/null || true
    rm -rf "$IPC_DIR"
}
trap cleanup EXIT

# Setup
echo "=== Setting up container ==="
mkdir -p "$IPC_DIR"
podman run -d --name "$CONTAINER" \
    -v "$IPC_DIR:/workspace/.ipc:Z" \
    "$IMAGE" sleep infinity

# Write hook script into container
podman exec "$CONTAINER" mkdir -p /workspace/.gemini/hooks
podman exec -i "$CONTAINER" sh -c 'cat > /workspace/.gemini/hooks/notification.sh' <<'HOOK'
#!/bin/sh
cat > /workspace/.ipc/notification.json
echo '{}'
HOOK
podman exec "$CONTAINER" chmod +x /workspace/.gemini/hooks/notification.sh

# Simulate Gemini sending a notification
echo ""
echo "=== Simulating Gemini notification ==="
podman exec -i "$CONTAINER" /workspace/.gemini/hooks/notification.sh <<'JSON'
{"notification_type": "ToolPermission", "message": "Gemini wants to edit src/auth.py", "details": {"tool": "edit_file"}}
JSON

# Verify host side
echo ""
echo "=== Checking IPC file on host ==="
if [ -f "$IPC_DIR/notification.json" ]; then
    echo "PASS: notification.json found"
    echo "Contents:"
    cat "$IPC_DIR/notification.json"
    echo ""

    # Parse like bot.py would
    echo ""
    echo "=== Parsed output (as bot.py would display) ==="
    python3 -c "
import json
with open('$IPC_DIR/notification.json') as f:
    data = json.load(f)
ntype = data.get('notification_type', 'unknown')
message = data.get('message', '')
details = data.get('details', {})
body = f'⚠️ Gemini [{ntype}]: {message}'
if details:
    body += '\nDetails: ' + json.dumps(details, indent=2)
print(body)
"
else
    echo "FAIL: notification.json not found in $IPC_DIR"
    ls -la "$IPC_DIR"
    exit 1
fi
