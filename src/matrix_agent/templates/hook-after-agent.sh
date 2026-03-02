#!/bin/sh
# AfterAgent hook — writes result to IPC, appends timestamp to status.md.
# Reads JSON from stdin (Gemini hook protocol), writes JSON to stdout.
# Uses tee to preserve raw JSON (echo can mangle control chars).
cat > /workspace/.ipc/event-result.json 2>> /workspace/.ipc/hook-errors.log
timestamp=$(date '+%Y-%m-%d %H:%M')
echo "[$timestamp] Gemini session completed" >> /workspace/status.md 2>> /workspace/.ipc/hook-errors.log
echo '{"continue": true}'
