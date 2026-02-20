#!/usr/bin/env python3
"""V2 — Validate in-container Playwright screenshots.

Run: python3 scripts/validate_screenshot.py

Prerequisites:
  - Sandbox image built: podman build -t matrix-agent-sandbox -f Containerfile .

Tests:
  1. Start sandbox container with port-mapped HTTP server
  2. podman exec: run Playwright screenshot inside container
  3. podman cp: copy PNG to host
  4. Verify PNG exists and is >0 bytes
"""

import os
import subprocess
import sys
import time

SANDBOX_IMAGE = "matrix-agent-sandbox:latest"
CONTAINER_NAME = "validate-screenshot-test"
HOST_PORT = 18081
CONTAINER_PORT = 8080
HOST_OUTPUT = "/tmp/validate_screenshot.png"


def run(cmd: list[str], *, check: bool = True) -> str:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=120,
    )
    if result.stdout.strip():
        print(f"    stdout: {result.stdout.strip()[:200]}")
    if result.stderr.strip():
        print(f"    stderr: {result.stderr.strip()[:200]}")
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result.stdout.strip()


def cleanup():
    subprocess.run(
        ["podman", "stop", CONTAINER_NAME],
        capture_output=True, check=False, timeout=30,
    )
    subprocess.run(
        ["podman", "rm", "-f", CONTAINER_NAME],
        capture_output=True, check=False, timeout=30,
    )
    if os.path.exists(HOST_OUTPUT):
        os.remove(HOST_OUTPUT)


def main():
    cleanup()

    # Check image exists
    print("\n[0] Checking sandbox image exists...")
    result = subprocess.run(
        ["podman", "image", "exists", SANDBOX_IMAGE],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        print(f"  ✗ Image '{SANDBOX_IMAGE}' not found.")
        print(f"  Build it first: podman build -t matrix-agent-sandbox -f Containerfile .")
        sys.exit(1)
    print(f"  ✓ Image '{SANDBOX_IMAGE}' found")

    passed = 0
    total = 4

    # 1. Start container with HTTP server
    print("\n[1/4] Starting sandbox container...")
    try:
        cid = run([
            "podman", "run", "-d",
            "--name", CONTAINER_NAME,
            "--shm-size=256m",
            "-p", f"{HOST_PORT}:{CONTAINER_PORT}",
            SANDBOX_IMAGE,
            "sleep", "infinity",
        ])
        print(f"  ✓ Container started: {cid[:12]}")

        # Start HTTP server inside container
        run([
            "podman", "exec", "-d", CONTAINER_NAME,
            "python3", "-m", "http.server", str(CONTAINER_PORT),
            "--directory", "/workspace",
        ])
        time.sleep(2)

        # Write a simple HTML page for the server to serve
        html = '<html><body style="background:lime"><h1>Hello from sandbox</h1></body></html>'
        run([
            "podman", "exec", CONTAINER_NAME,
            "sh", "-c", f"cat > /workspace/index.html << 'HTMLEOF'\n{html}\nHTMLEOF",
        ])
        passed += 1
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        cleanup()
        sys.exit(1)

    # 2. Take screenshot inside container
    print("\n[2/4] Taking screenshot via Playwright inside container...")
    try:
        run([
            "podman", "exec", CONTAINER_NAME,
            "node", "/usr/local/bin/screenshot.js",
            f"http://localhost:{CONTAINER_PORT}",
            "/tmp/screenshot.png",
        ])
        print("  ✓ Screenshot taken inside container")
        passed += 1
    except Exception as e:
        print(f"  ✗ Screenshot failed: {e}")
        cleanup()
        sys.exit(1)

    # 3. Copy PNG to host
    print("\n[3/4] Copying screenshot to host...")
    try:
        run([
            "podman", "cp",
            f"{CONTAINER_NAME}:/tmp/screenshot.png",
            HOST_OUTPUT,
        ])
        print(f"  ✓ Copied to {HOST_OUTPUT}")
        passed += 1
    except Exception as e:
        print(f"  ✗ Copy failed: {e}")
        cleanup()
        sys.exit(1)

    # 4. Verify PNG
    print("\n[4/4] Verifying PNG file...")
    if os.path.exists(HOST_OUTPUT):
        size = os.path.getsize(HOST_OUTPUT)
        if size > 0:
            print(f"  ✓ PNG exists: {HOST_OUTPUT} ({size} bytes)")
            passed += 1
        else:
            print(f"  ✗ PNG exists but is empty")
    else:
        print(f"  ✗ PNG not found at {HOST_OUTPUT}")

    # Cleanup
    print("\nCleaning up...")
    cleanup()

    print(f"\n{'='*40}")
    print(f"Results: {passed}/{total} passed")
    if passed == total:
        print("✓ All screenshot validations passed!")
    else:
        print("✗ Some validations failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
