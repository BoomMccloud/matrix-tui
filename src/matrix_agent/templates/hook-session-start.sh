#!/bin/sh
# SessionStart hook — detect project type, install deps, run baseline tests.
# Writes results to /workspace/.baseline-tests.txt for context.
# Reads JSON from stdin (Gemini hook protocol).
cat > /dev/null  # consume stdin

BASELINE="/workspace/.baseline-tests.txt"

# Find the git repo (cloned into /workspace or a subdirectory)
REPO=$(find /workspace -maxdepth 2 -name .git -type d 2>/dev/null | head -1 | sed 's|/.git||')
if [ -z "$REPO" ]; then
  echo '{}'
  exit 0
fi

cd "$REPO" || exit 0

echo "=== Baseline test results (before any changes) ===" > "$BASELINE"
echo "Repo: $REPO" >> "$BASELINE"
echo "Date: $(date)" >> "$BASELINE"

# Detect project type and install deps + run tests
if [ -f "pyproject.toml" ]; then
  echo "Project type: Python (pyproject.toml)" >> "$BASELINE"
  uv sync --extra dev >> "$BASELINE" 2>&1 || pip install -e ".[dev]" >> "$BASELINE" 2>&1
  echo "--- Lint ---" >> "$BASELINE"
  uv run ruff check . >> "$BASELINE" 2>&1 || true
  echo "--- Tests ---" >> "$BASELINE"
  uv run pytest tests/ -v >> "$BASELINE" 2>&1 || true
elif [ -f "package.json" ]; then
  echo "Project type: Node.js (package.json)" >> "$BASELINE"
  npm install >> "$BASELINE" 2>&1 || true
  echo "--- Lint ---" >> "$BASELINE"
  npm run lint >> "$BASELINE" 2>&1 || true
  echo "--- Tests ---" >> "$BASELINE"
  npm test >> "$BASELINE" 2>&1 || true
elif [ -f "Cargo.toml" ]; then
  echo "Project type: Rust (Cargo.toml)" >> "$BASELINE"
  echo "--- Build ---" >> "$BASELINE"
  cargo build >> "$BASELINE" 2>&1 || true
  echo "--- Tests ---" >> "$BASELINE"
  cargo test >> "$BASELINE" 2>&1 || true
elif [ -f "go.mod" ]; then
  echo "Project type: Go (go.mod)" >> "$BASELINE"
  echo "--- Tests ---" >> "$BASELINE"
  go test ./... >> "$BASELINE" 2>&1 || true
fi

# Also read CI config if present
if [ -f ".github/workflows/ci.yml" ]; then
  echo "--- CI config ---" >> "$BASELINE"
  cat .github/workflows/ci.yml >> "$BASELINE"
fi

echo '{}'
