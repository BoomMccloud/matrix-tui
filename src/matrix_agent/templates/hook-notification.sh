#!/bin/sh
# Notification hook — writes sentinel file to IPC dir for host watcher.
# Gemini sends JSON on stdin with message, notification_type, details fields.
cat > /workspace/.ipc/notification.json
echo '{}'
