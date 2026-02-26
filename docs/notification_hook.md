# Notification Hook — Surfacing Gemini CLI Events to Matrix

## Problem

When Gemini CLI runs inside a sandbox container, it sometimes blocks or needs approval (e.g. before editing files or running destructive commands). From the user's perspective in Matrix, the bot just appears stuck with no explanation.

## Solution

We use Gemini CLI's `Notification` hook to surface these events to the Matrix room in real time. The flow:

```
Gemini CLI (in container)
  → fires Notification event (e.g. ToolPermission)
  → hook script writes JSON to /workspace/.ipc/notification.json
  → volume mount exposes file to host at /tmp/sandbox-ipc/<container>/notification.json

_watch_ipc (bot.py) polls every 1s
  → detects notification.json
  → parses notification_type, message, details
  → sends formatted message to Matrix room
  → deletes the file
```

The user sees something like:

```
⚠️ Gemini [ToolPermission]: Gemini wants to edit src/auth.py
Details: {
  "tool": "edit_file"
}
```

## How It Works

### Container side

`sandbox.py` writes a Gemini hook config at `/workspace/.gemini/settings.json` during container init. The `Notification` hook points to `/workspace/.gemini/hooks/notification.sh`:

```sh
#!/bin/sh
cat > /workspace/.ipc/notification.json
echo '{}'
```

Gemini CLI pipes JSON to the hook's stdin. The hook writes it directly to the IPC directory. The empty `{}` stdout tells Gemini the hook succeeded (Notification hooks are observability-only — they cannot block or grant permissions).

### Host side

`bot.py` starts a `_watch_ipc` task alongside every agent invocation. It polls for `notification.json` in the container's IPC directory, parses the Gemini JSON schema (`notification_type`, `message`, `details`), formats it, and sends it to the Matrix room.

### IPC directory

Each container gets a volume mount: `<ipc_base_dir>/<container-name>` on the host maps to `/workspace/.ipc` in the container. This is set up in `sandbox.py:create()`.

## What This Does NOT Do

- It does not send input back to Gemini. Notifications are one-way.
- It does not pause or interrupt the agent loop. The orchestrator continues independently.
- It does not replace proper task prompting. If Gemini keeps asking for approval, the fix is a better prompt, not a round-trip IPC loop.

## Gemini Hook Events

From the [Gemini CLI hooks reference](https://geminicli.com/docs/hooks/reference/), the `Notification` event fires with:

| Field | Description |
|---|---|
| `notification_type` | Type of notification (e.g. `ToolPermission`) |
| `message` | Human-readable summary |
| `details` | JSON object with event-specific metadata |

## Testing

Run the test script to verify the hook and IPC flow locally:

```bash
bash scripts/test-ipc-hook.sh
```

This creates a container, writes the hook, simulates a Gemini notification, and verifies the file lands on the host and parses correctly.
