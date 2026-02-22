# Matrix Agent

A self-hosted agentic coding assistant accessible via Matrix chat. Each room gets an isolated Podman container where the agent can write code, run commands, and take browser screenshots.

## Architecture

```
Element/Matrix Client  <-->  Matrix Homeserver  <-->  Bot (matrix-nio)
                                                        |
                                                  Agent (LLM tool loop)
                                                        |
                                                  Podman Container
                                                   (code sandbox)
                                                        |
                                                  Playwright (screenshots)
```

## How It Works

1. Invite the bot to a Matrix room
2. Send a coding task as a message
3. The bot creates an isolated container for the room
4. The agent writes code, runs commands, and takes screenshots
5. Results are sent back to the room
6. When all users leave, the container is destroyed

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- Podman
- A Matrix account for the bot (e.g. register at https://app.element.io)
- An LLM API key (OpenRouter, Gemini, or MiniMax)

## Setup

```bash
# Clone
git clone https://github.com/BoomMccloud/matrix-tui.git
cd matrix-tui

# Build sandbox image
podman build -t matrix-agent-sandbox -f Containerfile .

# Configure
cp .env.example .env
# Edit .env with your credentials

# Run
uv run python -m matrix_agent
```

## Configuration

All configuration is via environment variables or `.env` file:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MATRIX_HOMESERVER` | No | `https://matrix.org` | Matrix homeserver URL |
| `MATRIX_USER` | Yes | | Bot's Matrix user ID (e.g. `@mybot:matrix.org`) |
| `MATRIX_PASSWORD` | Yes | | Bot's Matrix password |
| `LLM_API_KEY` | Yes | | API key for your LLM provider |
| `LLM_MODEL` | No | `openrouter/anthropic/claude-sonnet-4` | LiteLLM model string |
| `PODMAN_PATH` | No | `podman` | Path to podman binary |
| `SANDBOX_IMAGE` | No | `matrix-agent-sandbox:latest` | Sandbox container image |
| `COMMAND_TIMEOUT_SECONDS` | No | `120` | Max seconds per command |
| `MAX_AGENT_TURNS` | No | `25` | Max LLM tool-call rounds per message |

### LLM Providers

Any provider supported by [LiteLLM](https://docs.litellm.ai/docs/providers) works:

```bash
# OpenRouter
LLM_MODEL=openrouter/anthropic/claude-sonnet-4
LLM_API_KEY=sk-or-...

# Gemini
LLM_MODEL=gemini/gemini-2.5-pro
LLM_API_KEY=AI...

# MiniMax
LLM_MODEL=minimax/MiniMax-M2.5
LLM_API_KEY=...
```

## Agent Tools

The agent has four tools available in each sandbox:

| Tool | Description |
|------|-------------|
| `run_command` | Execute shell commands |
| `write_file` | Write files to the container |
| `read_file` | Read files from the container |
| `take_screenshot` | Screenshot a URL via Playwright |

## Room Lifecycle

- **Invite bot** → bot joins room, sends greeting
- **First message** → sandbox container created
- **Messages** → routed to agent, replies sent back
- **All users leave** → container destroyed, bot leaves
- **Bot kicked** → container destroyed

## Sandbox Container

The sandbox image (`Containerfile`) includes:

- Node.js 20
- Python 3
- Git
- Playwright + Chromium (for screenshots)

## VPS Deployment

Recommended: **4 vCPU / 8GB RAM** for 2-3 concurrent rooms.

```bash
# On VPS (Ubuntu/Debian)
sudo apt update && sudo apt install -y podman
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

git clone https://github.com/BoomMccloud/matrix-tui.git
cd matrix-tui
podman build -t matrix-agent-sandbox -f Containerfile .
cp .env.example .env
nano .env  # add credentials
uv run python -m matrix_agent
```

## Important Notes

- **Create unencrypted rooms** — the bot does not support E2EE yet
- **Context is ephemeral** — conversation history is lost on bot restart
- **One container per room** — each room is fully isolated

## Docs

- [MVP Spec](docs/mvp_spec.md) — architecture and design decisions
- [Programming Loop Spec](docs/programming-loop-spec.md) — planned autonomous coding workflow
