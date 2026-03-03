#!/usr/bin/env python3
"""V1 — Validate core Podman workflow via subprocess.

Run: python3 scripts/validate_podman.py

Tests:
  1. podman pull python:3.12-slim
  2. podman run -d (detached container)
  3. podman exec echo hello (stdout capture)
  4. podman exec python3 -c 'print(1+1)' (command execution)
  5. Port-mapped HTTP server reachable from host
  6. podman stop + podman rm (cleanup)
"""

import asyncio
import subprocess
import sys
import urllib.request

IMAGE = "docker.io/library/python:3.12-slim"
CONTAINER_NAME = "validate-podman-test"
HOST_PORT = 18080
CONTAINER_PORT = 8080


def run(cmd: list[str], *, check: bool = True, capture: bool = True) -> str:
    """Run a command and return stdout."""
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
        timeout=120,
    )
    if capture:
        return result.stdout.strip()
    return ""


def cleanup():
    """Best-effort cleanup."""
    subprocess.run(
        ["podman", "stop", CONTAINER_NAME],
        capture_output=True, check=False, timeout=30,
    )
    subprocess.run(
        ["podman", "rm", "-f", CONTAINER_NAME],
        capture_output=True, check=False, timeout=30,
    )


def main():
    # Clean up any leftover container from a previous run
    cleanup()

    passed = 0
    total = 6

    # 1. Pull image
    print("\n[1/6] Pulling image...")
    try:
        run(["podman", "pull", IMAGE])
        print("  ✓ Image pulled")
        passed += 1
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        sys.exit(1)

    # 2. Start detached container
    print("\n[2/6] Starting detached container...")
    try:
        cid = run([
            "podman", "run", "-d",
            "--name", CONTAINER_NAME,
            "-p", f"{HOST_PORT}:{CONTAINER_PORT}",
            IMAGE,
            "sleep", "infinity",
        ])
        print(f"  ✓ Container started: {cid[:12]}")
        passed += 1
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        cleanup()
        sys.exit(1)

    try:
        # 3. exec echo
        print("\n[3/6] Exec: echo hello...")
        out = run(["podman", "exec", CONTAINER_NAME, "echo", "hello"])
        assert out == "hello", f"Expected 'hello', got '{out}'"
        print(f"  ✓ Output: {out}")
        passed += 1

        # 4. exec python
        print("\n[4/6] Exec: python3 -c 'print(1+1)'...")
        out = run(["podman", "exec", CONTAINER_NAME, "python3", "-c", "print(1+1)"])
        assert out == "2", f"Expected '2', got '{out}'"
        print(f"  ✓ Output: {out}")
        passed += 1

        # 5. Port-mapped HTTP server
        print("\n[5/6] Starting HTTP server and testing port mapping...")
        # Start http server in background inside container
        run([
            "podman", "exec", "-d", CONTAINER_NAME,
            "python3", "-m", "http.server", str(CONTAINER_PORT),
        ])
        # Give it a moment to start
        import time
        time.sleep(2)

        url = f"http://localhost:{HOST_PORT}/"
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            body = resp.read().decode()
            assert resp.status == 200, f"Expected 200, got {resp.status}"
            print(f"  ✓ HTTP server reachable at {url} (status {resp.status})")
            passed += 1
        except Exception as e:
            print(f"  ✗ HTTP server not reachable: {e}")

        # 6. Cleanup
        print("\n[6/6] Stopping and removing container...")
        run(["podman", "stop", CONTAINER_NAME])
        run(["podman", "rm", CONTAINER_NAME])
        print("  ✓ Container cleaned up")
        passed += 1

    except Exception as e:
        print(f"\n  ✗ Unexpected error: {e}")
        cleanup()

    print(f"\n{'='*40}")
    print(f"Results: {passed}/{total} passed")
    if passed == total:
        print("✓ All Podman validations passed!")
    else:
        print("✗ Some validations failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
