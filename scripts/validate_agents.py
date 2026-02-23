#!/usr/bin/env python3
"""
Phase A validation: confirm Gemini CLI installs and runs non-interactively.

Usage:
    GEMINI_API_KEY=your-key python scripts/validate_agents.py

Steps:
    1. Pull/verify sandbox image exists
    2. Start a container with GEMINI_API_KEY injected
    3. Confirm gemini binary is on PATH
    4. Run a simple prompt (no shell escaping — passed as direct argv)
    5. Verify output and exit code
    6. Stop and remove container
"""

import os
import subprocess
import sys


def run(args: list[str], *, input: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        input=input,
        timeout=timeout,
    )
    if result.stderr:
        print(f"  stderr: {result.stderr.strip()[:200]}", flush=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def step(n: int, desc: str) -> None:
    print(f"\nSTEP {n}: {desc}", flush=True)


def fail(msg: str) -> None:
    print(f"  FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def ok(msg: str = "") -> None:
    print(f"  OK{': ' + msg if msg else ''}", flush=True)


def main() -> None:
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    image = os.environ.get("SANDBOX_IMAGE", "matrix-agent-sandbox:latest")
    podman = os.environ.get("PODMAN_PATH", "podman")

    if not gemini_key:
        fail("GEMINI_API_KEY is not set")

    cid = ""

    try:
        # Step 1: verify image exists
        step(1, f"Verify image {image}")
        rc, out, err = run([podman, "image", "exists", image])
        if rc != 0:
            fail(f"Image not found. Build with: podman build -t {image} .")
        ok(image)

        # Step 2: start container with env var
        step(2, "Start container with GEMINI_API_KEY")
        rc, out, err = run([
            podman, "run", "-d",
            "--shm-size=256m",
            "-e", f"GEMINI_API_KEY={gemini_key}",
            image,
            "sleep", "infinity",
        ])
        if rc != 0:
            fail(f"podman run failed: {err}")
        cid = out
        ok(f"container {cid[:12]}")

        # Step 3: confirm gemini binary
        step(3, "Confirm gemini binary on PATH")
        rc, out, err = run([podman, "exec", cid, "which", "gemini"])
        if rc != 0:
            fail("gemini not found — Containerfile may need rebuilding")
        ok(out)

        # Step 4: confirm env var is set
        step(4, "Confirm GEMINI_API_KEY is set in container")
        rc, out, err = run([podman, "exec", cid, "printenv", "GEMINI_API_KEY"])
        if rc != 0 or not out:
            fail("GEMINI_API_KEY not found in container")
        ok(f"key present ({len(out)} chars)")

        # Step 5: run a simple prompt — task passed as direct argv, no shell escaping
        task = "Say exactly: hello from gemini"
        step(5, f"Run: gemini -p {task!r}")
        rc, out, err = run(
            [podman, "exec", cid, "gemini", "-p", task],
            timeout=120,
        )
        if rc != 0:
            fail(f"gemini exited {rc}. stderr: {err}")
        if not out:
            fail("gemini produced no output")
        ok(f"output ({len(out)} chars): {out[:100]}")

        # Step 6: test shell-escaping edge case — task with quotes and special chars
        step(6, "Test task with quotes and special chars (escaping validation)")
        tricky_task = """Write a Python one-liner: print("it's a 'test' with $special & chars")"""
        rc, out, err = run(
            [podman, "exec", cid, "gemini", "-p", tricky_task],
            timeout=120,
        )
        if rc != 0:
            fail(f"gemini failed on quoted task (exit {rc}): {err}")
        ok(f"handled quotes/special chars cleanly. output: {out[:100]}")

        print(f"\n{'='*50}")
        print("All steps passed. Gemini CLI is working non-interactively.")
        print(f"{'='*50}")

    finally:
        if cid:
            step(99, "Cleanup: stop and remove container")
            run([podman, "stop", cid], timeout=15)
            run([podman, "rm", "-f", cid], timeout=15)
            ok("container removed")


if __name__ == "__main__":
    main()
