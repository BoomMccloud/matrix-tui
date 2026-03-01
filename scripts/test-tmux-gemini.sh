#!/bin/bash
# Smallest possible test: can we control Gemini CLI via tmux inside a container?
#
# Tests:
#   1. tmux is installed in container
#   2. Start Gemini in a tmux session with --yolo
#   3. Detect the initial `>` prompt via capture-pane
#   4. Send a task via send-keys
#   5. Detect output + `>` prompt when done
#
# Usage: bash scripts/test-tmux-gemini.sh
# Requires: rebuilt image with tmux, GEMINI_API_KEY in .env
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    eval "$(grep -v '^\s*#' "$SCRIPT_DIR/.env" | grep '=' | sed 's/ *= */=/')"
    set +a
fi

CONTAINER="test-tmux-gemini"
IMAGE="matrix-agent-sandbox:latest"
POLL_INTERVAL=1
STARTUP_TIMEOUT=60
TASK_TIMEOUT=120

cleanup() {
    echo ""
    echo "Cleaning up..."
    podman stop "$CONTAINER" 2>/dev/null || true
    podman rm "$CONTAINER" 2>/dev/null || true
}
trap cleanup EXIT

# --- Start container ---
echo "=== Starting container ==="
ENV_FLAGS=()
if [ -n "${GEMINI_API_KEY:-}" ]; then
    ENV_FLAGS+=("-e" "GEMINI_API_KEY=$GEMINI_API_KEY")
    echo "  GEMINI_API_KEY: set"
else
    echo "  GEMINI_API_KEY: not set — this test will fail"
    exit 1
fi
podman run -d --name "$CONTAINER" "${ENV_FLAGS[@]}" "$IMAGE" sleep infinity

# --- Check tmux is installed ---
echo ""
echo "=== Checking tmux is installed ==="
if podman exec "$CONTAINER" which tmux >/dev/null 2>&1; then
    echo "PASS: tmux found"
else
    echo "FAIL: tmux not found — rebuild image with tmux in apt-get"
    exit 1
fi

# --- Start Gemini in tmux session ---
echo ""
echo "=== Starting Gemini CLI in tmux session ==="
podman exec "$CONTAINER" tmux new-session -d -s gemini -x 220 -y 50 \
    "cd /workspace && gemini --yolo"
echo "Started tmux session 'gemini'"

# --- Wait for initial > prompt ---
echo ""
echo "=== Waiting for initial > prompt (timeout: ${STARTUP_TIMEOUT}s) ==="

strip_ansi() {
    perl -pe 's/\e\[[0-9;]*[a-zA-Z]//g; s/\e\][^\x07]*\x07//g; s/\e[()][0-9]//g; s/\x0f//g'
}

wait_for_prompt() {
    local timeout=$1
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        # Capture pane content, strip ANSI, get last non-empty line
        local pane_content
        pane_content=$(podman exec "$CONTAINER" tmux capture-pane -t gemini -p -S - 2>/dev/null || echo "")
        local last_line
        last_line=$(echo "$pane_content" | strip_ansi | grep -v '^\s*$' | tail -1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || echo "")

        if [ "$last_line" = ">" ]; then
            echo "PASS: detected > prompt after ${elapsed}s"
            return 0
        fi

        # Show progress every 10s
        if [ $((elapsed % 10)) -eq 0 ] && [ $elapsed -gt 0 ]; then
            echo "  ...waiting (${elapsed}s) last line: '$last_line'"
        fi

        sleep "$POLL_INTERVAL"
        elapsed=$((elapsed + POLL_INTERVAL))
    done

    echo "FAIL: no > prompt after ${timeout}s"
    echo "Last pane content:"
    podman exec "$CONTAINER" tmux capture-pane -t gemini -p -S - 2>/dev/null | strip_ansi | tail -20
    return 1
}

wait_for_prompt "$STARTUP_TIMEOUT"

# --- Record current pane length (offset tracking) ---
OFFSET=$(podman exec "$CONTAINER" tmux capture-pane -t gemini -p -S - 2>/dev/null | wc -l)
echo "Pane offset: $OFFSET lines"

# --- Send a task ---
TASK="respond with exactly one line: TMUX_TEST_OK"
echo ""
echo "=== Sending task: '$TASK' ==="
podman exec "$CONTAINER" tmux send-keys -t gemini -l "$TASK"
podman exec "$CONTAINER" tmux send-keys -t gemini Enter

# --- Wait for response + > prompt ---
echo ""
echo "=== Waiting for response (timeout: ${TASK_TIMEOUT}s) ==="

wait_for_prompt "$TASK_TIMEOUT"

# --- Check output contains expected response ---
echo ""
echo "=== Checking output ==="
NEW_OUTPUT=$(podman exec "$CONTAINER" tmux capture-pane -t gemini -p -S - 2>/dev/null | strip_ansi | tail -n +$((OFFSET + 1)))
echo "New output since task:"
echo "$NEW_OUTPUT"
echo "---"

if echo "$NEW_OUTPUT" | grep -qi "TMUX_TEST_OK"; then
    echo "PASS: found expected output"
else
    echo "FAIL: expected output not found"
    exit 1
fi

echo ""
echo "=== All tmux tests passed ==="
