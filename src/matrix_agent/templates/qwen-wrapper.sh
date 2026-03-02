#!/bin/sh
# Wrapper for qwen CLI — writes event-result.json on completion.
# Usage: .qwen-wrapper.sh "prompt text"
output=$(timeout ${QWEN_TIMEOUT:-1800} qwen -y -p "$1" 2>&1) || true
rc=$?
timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
cat > /workspace/.ipc/event-result.json <<IPCEOF
{"cli": "qwen", "exit_code": $rc, "timestamp": "$timestamp"}
IPCEOF
if [ $rc -ne 0 ]; then
  echo "wrapper error: qwen exited $rc" >> /workspace/.ipc/hook-errors.log
fi
echo "$output"
exit $rc
