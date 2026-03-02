# Workspace Context

This file is your instruction set. It is loaded automatically on every invocation.

## Prior work (auto-imported)

@status.md

## Rules

1. Before starting any task, read status.md (imported above) to understand what has already been done.
2. When working inside a cloned repo, read AGENTS.md in the repo root for conventions and architecture.
3. After completing each task, append one line to /workspace/status.md:
   [YYYY-MM-DD HH:MM] <what was done>
4. When you discover a convention, gotcha, or architectural decision worth remembering,
   append it to AGENTS.md in the repo root. Use the format shown in that file.
5. After cloning a new repo, run `/init` inside the repo directory to generate
   a project-specific GEMINI.md with codebase context.

## What NOT to put in status.md
- Do not put decisions or conventions here — those go in AGENTS.md
- Do not edit old entries — append only
- One line per task, no multi-line entries
