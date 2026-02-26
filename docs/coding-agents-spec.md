# In-Container Coding Agents

## Current State

The orchestrator (Claude Sonnet) delegates coding tasks to **Gemini CLI** running inside the sandbox container via the `code` tool. Gemini has 1M token context and can read entire repos in a single invocation.

## Architecture

```
Matrix Room → Bot → Orchestrator (Sonnet)
                        │
                     code(task)
                        │
                   Gemini CLI
                   (inside sandbox container)
                   streams output → Matrix room
```

## The `code` Tool

**When to use:** any non-trivial coding task — writing features, fixing bugs, refactoring, reviewing code, explaining a codebase.

**When NOT to use:** simple shell operations (use `run_command` instead).

Gemini output streams back to the Matrix room in ~800-char chunks as it works. Final output is also returned to the orchestrator for follow-up reasoning.

## GitHub Integration

Containers have `gh` CLI installed and `GITHUB_TOKEN` injected as an env var. Gemini can:
- Clone repos: `git clone https://$GITHUB_TOKEN@github.com/user/repo`
- Push branches and create PRs: `gh pr create`
- Merge PRs: `gh pr merge`

This enables a fully closed loop: Gemini edits code → pushes → opens PR → merges → orchestrator calls `self_update` to redeploy.

## Workspace Context Files

Every sandbox has persistent context files that Gemini reads automatically:

```
/workspace/
├── GEMINI.md       # Auto-loaded by Gemini CLI — imports status.md, rules for agents
├── status.md       # Append-only task log (AfterAgent hook writes to this)
└── <repo files>    # Cloned repository
```

**AfterAgent hook** — after every Gemini session, appends a timestamped entry to `status.md` automatically.

**Notification hook** — when Gemini fires a `Notification` event (e.g. `ToolPermission`), a hook writes the event JSON to `/workspace/.ipc/notification.json`. The bot polls for this file and sends a formatted Matrix notification within ~1s. See `notification_hook.md`.

## Gemini Settings

Hooks are configured in `/workspace/.gemini/settings.json` on container creation:

```json
{
  "hooks": {
    "AfterAgent": [...],
    "Notification": [...]
  }
}
```

## Next Step — Multi-Agent Routing

The original spec planned multiple agents with the orchestrator routing between them. This is now being implemented:

- **Gemini CLI** — planning, analysis, code review (1M context for whole-repo reasoning)
- **Qwen Code** — implementation, bug fixes, refactoring

The `code` tool splits into `plan` (Gemini), `implement` (Qwen), and `review` (Gemini). See `multi_agent_routing.md` for the full spec. After routing works, tmux persistent sessions enable context persistence and bidirectional communication — see `tmux_gemini_sessions.md`.
