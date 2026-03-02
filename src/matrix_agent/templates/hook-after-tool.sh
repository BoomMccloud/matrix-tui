#!/bin/sh
# AfterTool hook — writes tool progress to IPC for host watcher.
cat > /workspace/.ipc/event-progress.json 2>> /workspace/.ipc/hook-errors.log
echo '{}'
