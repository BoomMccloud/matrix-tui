#!/bin/bash
# Check IPC logs and verify all expected files exist for a sandbox container.
# Usage: bash scripts/check-ipc-logs.sh [container-name]
#
# If no container name given, lists all sandbox containers and checks each.
# Reads from both the host IPC dir and inside the container.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; FAILURES=$((FAILURES + 1)); }
warn() { echo -e "  ${YELLOW}?${NC} $1"; }
header() { echo -e "\n${BOLD}=== $1 ===${NC}"; }

FAILURES=0

# Resolve IPC base dir (macOS /tmp → /private/tmp)
IPC_BASE="${IPC_BASE_DIR:-$(cd "$(dirname "$0")/.." && pwd)/.test-ipc}"
if [ -d "/private/tmp" ] && [[ "$IPC_BASE" == /tmp/* ]]; then
    IPC_BASE="/private${IPC_BASE}"
fi

check_container() {
    local CONTAINER="$1"
    local IPC_DIR="$IPC_BASE/$CONTAINER"

    header "Container: $CONTAINER"

    # --- Host-side IPC dir ---
    echo ""
    echo "  Host IPC dir: $IPC_DIR"
    if [ -d "$IPC_DIR" ]; then
        pass "IPC directory exists"
    else
        fail "IPC directory missing: $IPC_DIR"
        return
    fi

    # Check each expected IPC file
    for f in event-result.json event-progress.json notification.json; do
        if [ -f "$IPC_DIR/$f" ]; then
            pass "$f exists ($(wc -c < "$IPC_DIR/$f" | tr -d ' ') bytes)"
            echo "      $(head -c 200 "$IPC_DIR/$f")"
        else
            warn "$f not present (may have been consumed or not yet written)"
        fi
    done

    # --- Container-side checks ---
    echo ""
    echo "  Container internals:"

    # Hook scripts exist and are executable
    for hook in after-agent.sh after-tool.sh notification.sh; do
        rc=0
        podman exec "$CONTAINER" test -x "/workspace/.gemini/hooks/$hook" 2>/dev/null || rc=$?
        if [ $rc -eq 0 ]; then
            pass "Hook /workspace/.gemini/hooks/$hook is executable"
        else
            fail "Hook /workspace/.gemini/hooks/$hook missing or not executable"
        fi
    done

    # Qwen wrapper
    rc=0
    podman exec "$CONTAINER" test -x "/workspace/.qwen-wrapper.sh" 2>/dev/null || rc=$?
    if [ $rc -eq 0 ]; then
        pass "Qwen wrapper /workspace/.qwen-wrapper.sh is executable"
    else
        fail "Qwen wrapper missing or not executable"
    fi

    # Gemini settings.json has all hook events
    rc=0
    settings=$(podman exec "$CONTAINER" cat /workspace/.gemini/settings.json 2>/dev/null) || rc=$?
    if [ $rc -eq 0 ]; then
        for event in AfterAgent AfterTool Notification; do
            if echo "$settings" | python3 -c "import sys,json; d=json.load(sys.stdin); assert '$event' in d['hooks']" 2>/dev/null; then
                pass "settings.json has $event hook"
            else
                fail "settings.json missing $event hook"
            fi
        done
    else
        fail "Could not read /workspace/.gemini/settings.json"
    fi

    # Hook errors log
    rc=0
    errors=$(podman exec "$CONTAINER" cat /workspace/.ipc/hook-errors.log 2>/dev/null) || rc=$?
    if [ $rc -eq 0 ]; then
        if [ -z "$errors" ]; then
            pass "hook-errors.log is empty (no errors)"
        else
            fail "hook-errors.log has content:"
            echo "$errors" | sed 's/^/      /'
        fi
    else
        warn "hook-errors.log does not exist yet (no hooks have run)"
    fi

    # status.md
    rc=0
    podman exec "$CONTAINER" test -f /workspace/status.md 2>/dev/null || rc=$?
    if [ $rc -eq 0 ]; then
        lines=$(podman exec "$CONTAINER" sh -c 'wc -l < /workspace/status.md' 2>/dev/null | tr -d ' ')
        pass "status.md exists ($lines lines)"
    else
        warn "status.md not found"
    fi

    # GEMINI.md
    rc=0
    podman exec "$CONTAINER" test -f /workspace/GEMINI.md 2>/dev/null || rc=$?
    if [ $rc -eq 0 ]; then
        pass "GEMINI.md exists"
    else
        warn "GEMINI.md not found"
    fi
}

# --- Main ---
if [ $# -gt 0 ]; then
    CONTAINERS=("$@")
else
    header "Discovering sandbox containers"
    mapfile -t CONTAINERS < <(podman ps --format '{{.Names}}' | grep '^sandbox-' || true)
    if [ ${#CONTAINERS[@]} -eq 0 ]; then
        echo "  No running sandbox-* containers found."
        exit 0
    fi
    echo "  Found ${#CONTAINERS[@]} container(s): ${CONTAINERS[*]}"
fi

for c in "${CONTAINERS[@]}"; do
    check_container "$c"
done

# --- Summary ---
header "Summary"
if [ $FAILURES -eq 0 ]; then
    echo -e "  ${GREEN}All checks passed.${NC}"
else
    echo -e "  ${RED}$FAILURES check(s) failed.${NC}"
    exit 1
fi
