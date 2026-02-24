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

**UserInputRequired hook** — if Gemini needs human input, writes a sentinel file to `/workspace/.ipc/needs_input.json`. The bot polls for this file and sends a Matrix notification within ~1s.

## Gemini Settings

Hooks are configured in `/workspace/.gemini/settings.json` on container creation:

```json
{
  "hooks": {
    "AfterAgent": [...],
    "UserInputRequired": [...]
  }
}
```

## Future

The original spec planned three agents (Qwen Code, Aider, Gemini CLI) with the orchestrator routing between them based on task type. This was simplified to Gemini-only since it handles all task types adequately with its 1M context. The routing logic can be added back if Gemini proves insufficient for specific scenarios (e.g. Aider's git workflow integration).
