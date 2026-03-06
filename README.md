<h1 align="center">Matrix Agent</h1>

<p align="center">
  <em>A self-hosted agentic coding assistant accessible via Matrix chat.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/Podman-Ready-0E5C92?logo=podman&logoColor=white" alt="Podman">
  <img src="https://img.shields.io/badge/Matrix-Supported-black?logo=matrix" alt="Matrix">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
</p>

---

**Matrix Agent** brings an autonomous coding assistant directly into your Matrix
chat rooms. Each room receives an isolated Podman container where the agent can
write code, execute commands, take browser screenshots, and submit GitHub pull
requests.

## Features

- **Isolated workspaces** — every Matrix room gets its own dedicated Podman
  sandbox container
- **Full coding environment** — Python 3, Node.js 20, git, gh CLI, and
  Playwright pre-installed
- **Multi-agent routing** — Haiku orchestrator delegates `plan`/`review` to
  Gemini CLI (1M context) and `implement` to Qwen Code
- **Hook-driven IPC** — Gemini hooks and a Qwen wrapper write progress/result
  events to `/workspace/.ipc/`, streamed to Matrix in real time
- **Multi-LLM orchestration** — powered by LiteLLM; use Claude, Gemini, or any
  OpenRouter model
- **Streaming output** — coding agent progress streams into the chat as it works
- **GitHub integration** — agent can clone repos, push branches, and open PRs
- **Self-updating** — agent can redeploy itself via the `self_update` tool
- **Unencrypted rooms only** — E2EE is not supported

## Architecture

```
Matrix Client (Element)
       |
Matrix Homeserver (self-hosted Synapse)
       |
Matrix Bot (python-nio + LiteLLM)
       |
  Haiku (orchestrator) ──> Tools: run_command, write_file, read_file,
       |                          take_screenshot, plan, implement,
       |                          review, run_tests, self_update
       |
  ┌────┴────┐
  │         │
Gemini    Qwen Code
(plan/    (implement,
 review)   auto-accept)
       |
  IPC hooks → event-result.json / event-progress.json → Matrix
       |
  Podman sandbox container (one per room)
       |
  GitHub (gh CLI for PRs)
```

## Using Matrix Agent with your own repository

Matrix Agent can autonomously work on GitHub issues in **any repository** — not just its own. When an issue is labeled `agent-task`, the bot clones the repo into a fresh sandbox container, implements the fix, and opens a pull request.

### How it works

1. Open an issue in your target repository
2. Add the `agent-task` label
3. The bot picks it up via webhook, posts "Working on this issue..."
4. Gemini CLI runs inside the sandbox: plans, implements, tests, and commits
5. The bot opens a PR and posts the URL as a comment
6. If CI fails on the PR, reopen the issue — the bot will automatically fix it

### Setting up a target repository

**Step 1 — Add the `agent-task` label** to your repo (create it if it doesn't exist):

```bash
gh label create agent-task --repo owner/your-repo --color aaaaaa --description "Agent will pick up this task"
```

**Step 2 — Configure the webhook** on your target repo:

- Go to **Settings → Webhooks → Add webhook**
- Payload URL: `http://<VPS_IP>:<GITHUB_WEBHOOK_PORT>/webhook/github`
- Content type: `application/json`
- Secret: the value of `GITHUB_WEBHOOK_SECRET` in your `.env`
- Events: select **Issues** and **Issue comments**

**Step 3 — Give the bot access** to your repo:

Ensure your `GITHUB_TOKEN` in `.env` has `contents: write` and `pull-requests: write` permissions on the target repo. A fine-grained PAT scoped to the specific repo works well.

**Step 4 — Set `GITHUB_REPO`** in `.env`:

```env
GITHUB_REPO=owner/your-repo
```

This tells the bot which repo to watch for crash recovery (re-enqueuing open `agent-task` issues on restart).

### Writing good issues

The bot works best with issues that are:

- **Self-contained** — the change should be achievable within the existing codebase without external dependencies
- **Specific** — include file names, method names, or error messages where relevant
- **Testable** — include acceptance criteria the bot can verify by running tests

**Good example:**

> **Add input validation to the user registration endpoint**
>
> The `/api/register` endpoint in `src/routes/auth.py` does not validate that `email` is a valid email format or that `password` is at least 8 characters. Add validation and return a 400 with a descriptive error message if either check fails.
>
> Acceptance criteria:
> - POST /api/register with invalid email returns 400
> - POST /api/register with password < 8 chars returns 400
> - Existing tests pass, new tests added for both cases

**Tips:**

- The bot runs `pytest` and `ruff` automatically — issues that ask for tests alongside code changes work better
- Mention specific files to avoid scope creep
- If the issue is unclear, the bot will post a clarification question as a comment rather than guessing
- For web UI changes, ask the bot to commit a `screenshot.png` to the branch — it will serve the page locally and capture it, and the screenshot will appear in the PR description

### The CI feedback loop

If CI fails on the bot's PR, reopen the issue — the bot detects the reopen, reads the CI failure comment, and launches a targeted fix session. This loop repeats until CI is green.

To enable this, add the CI feedback workflow to your repo (`.github/workflows/ci-feedback.yml`):

```yaml
name: CI Failure Feedback
on:
  workflow_run:
    workflows: [CI]
    types: [completed]

jobs:
  feedback:
    if: ${{ github.event.workflow_run.conclusion == 'failure' }}
    runs-on: ubuntu-latest
    steps:
      - name: Post failure comment and reopen issue
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          PR_NUMBER=$(gh pr list --repo ${{ github.repository }} \
            --head ${{ github.event.workflow_run.head_branch }} \
            --json number -q '.[0].number')
          if [ -z "$PR_NUMBER" ]; then exit 0; fi
          ISSUE_NUMBER=$(gh pr view $PR_NUMBER --repo ${{ github.repository }} \
            --json body -q '.body' | grep -oP '(?<=Closes #)\d+' | head -1)
          if [ -z "$ISSUE_NUMBER" ]; then exit 0; fi
          gh issue comment $ISSUE_NUMBER --repo ${{ github.repository }} \
            --body "⚠️ CI failed on PR #$PR_NUMBER. Please fix the failing tests."
          gh issue reopen $ISSUE_NUMBER --repo ${{ github.repository }}
```

### Safety guardrails

The bot will never modify:

- `pyproject.toml`, `uv.lock`, `package-lock.json`, `Cargo.lock`, `go.sum`
- `.gitignore`, `CLAUDE.md`, `AGENTS.md`, `Containerfile`, `Makefile`
- Anything under `.gemini/`, `.claude/`, `.github/`, `scripts/`

If Gemini attempts to touch these, the host automatically reverts the changes before pushing.

---

## Deployment

### Prerequisites

- VPS with **4 vCPU / 8 GB RAM** recommended (2-3 concurrent rooms)
- Ubuntu/Debian with root access
- Podman installed
- [uv](https://github.com/astral-sh/uv) installed
- LLM API key (OpenRouter, Gemini, etc.)
- Gemini API key (for the in-sandbox coding agent)
- GitHub fine-grained PAT with `contents: write` + `pull-requests: write`
  (optional)

### Step 1 — Clone and configure

```bash
git clone https://github.com/BoomMccloud/matrix-tui.git
cd matrix-tui
cp .env.example .env
nano .env
```

Fill in `.env` — at minimum:

| Variable                | Description                                                                             |
| ----------------------- | --------------------------------------------------------------------------------------- |
| `VPS_IP`                | Your VPS public IP — homeserver URL, bot user, and admin user are all derived from this |
| `MATRIX_PASSWORD`       | Bot account password (you choose)                                                       |
| `MATRIX_ADMIN_PASSWORD` | Your human account password (you choose)                                                |
| `LLM_API_KEY`           | API key for the orchestrator LLM                                                        |
| `GEMINI_API_KEY`        | API key for Gemini (plan/review agent)                                                  |
| `DASHSCOPE_API_KEY`     | API key for Qwen Code (implement agent)                                                 |

If not self-hosting, override the derived values explicitly:

```env
MATRIX_HOMESERVER = https://matrix.org
MATRIX_USER = @mybot:matrix.org
MATRIX_ADMIN_USER = @me:matrix.org
```

### Step 2 — Set up the local Matrix homeserver (Synapse)

This runs a local Synapse instance in a Podman container, creates both accounts,
and configures systemd:

```bash
bash scripts/setup-synapse.sh
```

This script:

1. Creates `/opt/synapse/data` and generates `homeserver.yaml`
2. Patches the config for plain HTTP on port 8008, no federation
3. Installs and starts a `synapse` systemd service
4. Registers the bot account and your admin account
5. Adds a `Requires=synapse.service` dependency to `matrix-agent.service`

### Step 3 — Build the sandbox image and install the bot service

```bash
bash scripts/install-service.sh
```

This script:

1. Validates `.env` has required API keys (`MATRIX_PASSWORD`, `LLM_API_KEY`, `GEMINI_API_KEY`)
2. Checks that `podman` and `uv` are installed
3. Builds the sandbox image (`matrix-agent-sandbox:latest`)
4. Creates and enables the `matrix-agent` systemd service (auto-detects Synapse dependency)
5. Starts the bot

```bash
# Check it's running
journalctl -u matrix-agent -f
```

### Step 4 — Connect a Matrix client

1. Open [Element](https://app.element.io/) (or any Matrix client)
2. Choose **Sign in** and set the homeserver to `http://<VPS_IP>:8008`
3. Log in as your admin account (`@yourname:<VPS_IP>`)
4. Create a room (unencrypted), invite `@matrixbot:<VPS_IP>`
5. The bot joins and replies — send it a task

### Redeploying

To update bot code and rebuild the sandbox image:

```bash
# Manually on the VPS
bash scripts/deploy.sh

# Or ask the bot in chat
"redeploy yourself"
```

The bot will run `git pull`, rebuild the sandbox image, send you the result,
then restart itself.

## Configuration reference

| Variable                  | Default                                | Description                                |
| ------------------------- | -------------------------------------- | ------------------------------------------ |
| `VPS_IP`                  |                                        | VPS public IP, used by setup-synapse.sh    |
| `MATRIX_HOMESERVER`       | `https://matrix.org`                   | Matrix homeserver URL                      |
| `MATRIX_USER`             |                                        | Bot Matrix user ID                         |
| `MATRIX_PASSWORD`         |                                        | Bot password                               |
| `MATRIX_ADMIN_USER`       |                                        | Human admin Matrix user ID                 |
| `MATRIX_ADMIN_PASSWORD`   |                                        | Human admin password                       |
| `LLM_API_KEY`             |                                        | Orchestrator LLM API key                   |
| `LLM_MODEL`               | `openrouter/anthropic/claude-sonnet-4` | LiteLLM model string                       |
| `GEMINI_API_KEY`          |                                        | Gemini API key for plan/review agent       |
| `DASHSCOPE_API_KEY`       |                                        | DashScope API key for Qwen implement agent |
| `GITHUB_TOKEN`            |                                        | Fine-grained PAT for GitHub PR submissions |
| `PODMAN_PATH`             | `podman`                               | Path to podman binary                      |
| `SANDBOX_IMAGE`           | `matrix-agent-sandbox:latest`          | Sandbox image name                         |
| `COMMAND_TIMEOUT_SECONDS` | `120`                                  | Max time per shell command                 |
| `CODING_TIMEOUT_SECONDS`  | `2400`                                 | Max time per Gemini CLI invocation         |
| `MAX_AGENT_TURNS`         | `25`                                   | Max LLM tool-call rounds per message       |
| `IPC_BASE_DIR`            | `/tmp/sandbox-ipc`                     | Host directory for sandbox IPC files       |

## Agent tools

| Tool              | Runs on  | CLI     | Description                                     |
| ----------------- | -------- | ------- | ----------------------------------------------- |
| `run_command`     | Sandbox  |         | Execute shell commands                          |
| `write_file`      | Sandbox  |         | Write files into the container                  |
| `read_file`       | Sandbox  |         | Read files from the container                   |
| `plan`            | Sandbox  | Gemini  | Plan, analyze, or explain (1M token context)    |
| `implement`       | Sandbox  | Qwen    | Write or modify code (auto-accept mode)         |
| `review`          | Sandbox  | Gemini  | Review code changes for bugs and issues         |
| `run_tests`       | Sandbox  |         | Run ruff lint + pytest                          |
| `take_screenshot` | Sandbox  |         | Screenshot a URL via Playwright                 |
| `self_update`     | VPS host |         | git pull + rebuild image + restart service      |

## IPC events

Sandbox containers communicate back to the host via JSON files written to
`/workspace/.ipc/` (bind-mounted to the host). The bot polls these files and
forwards them to Matrix.

| File                   | Written by           | Purpose                                |
| ---------------------- | -------------------- | -------------------------------------- |
| `event-result.json`    | AfterAgent hook / Qwen wrapper | Agent session completed       |
| `event-progress.json`  | AfterTool hook       | Tool completed (Gemini only)           |
| `notification.json`    | Notification hook    | Gemini needs attention                 |
| `hook-errors.log`      | All hooks/wrapper    | Stderr from hook failures              |

## Diagnostics

Check IPC logs and hook setup for all running containers:

```bash
# Check a specific container
IPC_BASE_DIR=/tmp/sandbox-ipc bash scripts/check-ipc-logs.sh sandbox-myroom

# Auto-discover all sandbox-* containers
IPC_BASE_DIR=/tmp/sandbox-ipc bash scripts/check-ipc-logs.sh
```

The script verifies: IPC directory exists, hook scripts are executable, Gemini
settings.json has all hook events registered, qwen wrapper exists, and
`hook-errors.log` is empty.

## Room lifecycle

1. **Invite** — bot joins, sends greeting
2. **First message** — sandbox container created for the room
3. **Tasks** — agent runs tool loop, streams Gemini output to chat
4. **Cleanup** — container destroyed when all users leave or bot is kicked

## Troubleshooting

**Bot not responding after restart** The invite may have fired before the bot
was ready. Leave and re-invite, or send a new message to an existing room.

**Sync timeouts with matrix.org** matrix.org blocks long-poll connections from
some VPS IPs. Use the local Synapse setup instead (`setup-synapse.sh`).

**Container creation fails** Check that the sandbox image was built:
`podman images | grep matrix-agent-sandbox`

**Command timeout errors** Increase `COMMAND_TIMEOUT_SECONDS` in `.env` for slow
operations like `npm install`.

## Development

```bash
# Run the bot locally
uv run python -m matrix_agent

# Run unit tests
uv run pytest tests/ -v

# Run integration tests (needs podman + API keys)
uv run --env-file .env pytest tests/test_integration.py -v -s

# Lint
uv run ruff check src tests

# Rebuild sandbox image
podman build -t matrix-agent-sandbox:latest -f Containerfile .

# Check IPC logs on a running container
IPC_BASE_DIR=/tmp/sandbox-ipc bash scripts/check-ipc-logs.sh
```

## Documentation

- [MVP Spec](docs/mvp_spec.md)
- [Programming Loop Spec](docs/programming-loop-spec.md)
- [Memory Spec](docs/memory-spec.md)
