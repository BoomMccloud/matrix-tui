# In-Container Coding Agents

## Current State

The orchestrator routes tasks to two coding agents inside the sandbox container:

- **Gemini CLI** (1M context) — planning, analysis, code review via `plan` and `review` tools
- **Qwen Code** — implementation, bug fixes, refactoring via `implement` tool

## Architecture

```
Matrix Room → Bot → Orchestrator (Haiku)
                        │
                   ┌────┴────┐
                plan/review  implement
                   │           │
              Gemini CLI    Qwen Code
              (sandbox)     (sandbox)
                   └────┬────┘
                   streams output → Matrix room
```

## Tools

| Tool | Agent | When to use |
|---|---|---|
| `plan` | Gemini CLI | Planning, analysis, explaining codebases, first-principles thinking |
| `implement` | Qwen Code | Writing code, fixing bugs, refactoring, writing tests |
| `review` | Gemini CLI | Code review after implementation — bugs, security, conventions |

**When NOT to use:** simple shell operations (use `run_command` instead).

Agent output streams back to the Matrix room in ~800-char chunks. Final output is returned to the orchestrator for follow-up reasoning.

## Typical Workflow

1. `plan()` — understand the codebase and design the approach
2. `implement()` — write the code, passing the plan as context
3. `run_tests()` — verify lint and tests pass
4. `review()` — check for bugs, security issues, missed edge cases
5. If review finds issues, `implement()` again with the feedback

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

## Next Step — tmux Persistent Sessions

Each agent currently runs as a one-shot process (`cli -p <task>`). Context is lost between invocations. The next step is running each agent in its own tmux session inside the container for context persistence and bidirectional communication. See `tmux_gemini_sessions.md`.
