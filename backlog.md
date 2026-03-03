# Backlog

## Graceful shutdown on SIGTERM

When `systemctl restart` sends SIGTERM, the bot exits without cleaning up. Containers are left running and state.json may be stale. `deploy.sh` now works around this by force-stopping all `sandbox-*` containers, but the bot should handle SIGTERM properly:

- Register a signal handler in `__main__.py`
- Cancel all workers, destroy all containers
- Save final state
- Then exit

This would also let us remove the `rm -f state.json` hack from deploy.sh.

## Gemini scope creep on sandbox.py regex

Issue #28 asked for tests only, but Gemini also modified `_strip_ansi` regex in `sandbox.py`. The manifest check should catch this going forward, but worth monitoring whether the "declare scope" step + validation actually prevents it in practice.

## IPC path confusion

Gemini repeatedly writes IPC files (pr-url.txt, acceptance-criteria.md) inside the cloned repo instead of `/workspace/.ipc/`. We've added warnings to GEMINI.md and both command templates. If this keeps happening, consider:

- Symlinking `/workspace/<repo>/.ipc` → `/workspace/.ipc` after clone
- Or having validate_work check both locations and copy if found in wrong place
