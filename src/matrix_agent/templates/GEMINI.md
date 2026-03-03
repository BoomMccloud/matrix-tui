# Workspace Context

This file is your instruction set. It is loaded automatically on every invocation.

## Prior work (auto-imported)

@status.md

## Rules

1. Before starting any task, read status.md (imported above) to understand what has already been done.
2. When working inside a cloned repo, read AGENTS.md in the repo root for conventions and architecture.
3. After completing each task, append one line to /workspace/status.md:
   [YYYY-MM-DD HH:MM] <what was done>
4. After cloning a new repo, run `/init` inside the repo directory to generate
   a project-specific GEMINI.md with codebase context.

## IPC Directory — CRITICAL

All validation files MUST be written to `/workspace/.ipc/`, NOT inside the repo directory.
This directory is a host-mounted volume. The host reads these files to validate your work.

- `/workspace/.ipc/changed-files.txt` — file manifest (one file per line)
- `/workspace/.ipc/acceptance-criteria.md` — acceptance criteria
- `/workspace/.ipc/pr-url.txt` — PR URL after creating the pull request
- `/workspace/.ipc/clarification.txt` — questions if issue is unclear

If you write these files anywhere else (e.g., inside the cloned repo), validation WILL fail.

## What NOT to do
- Do NOT modify AGENTS.md, CLAUDE.md, .gitignore, or files under scripts/, .github/, .claude/
- Do NOT edit old status.md entries — append only
- One line per task in status.md, no multi-line entries
