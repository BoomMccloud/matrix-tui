#!/bin/sh
# BeforeTool hook — blocks git push (host handles pushing after validation).
# Exit code 2 = Gemini CLI blocks the tool execution.
# Reads JSON from stdin with tool invocation details.
INPUT=$(cat)

# --- Shared denylists (single source of truth) ---
DENIED_NAMES=".gitignore AGENTS.md CLAUDE.md pyproject.toml uv.lock package-lock.json Cargo.lock go.sum Containerfile Makefile conftest.py __init__.py"
DENIED_DIRS=".gemini/ .claude/ .github/ scripts/ src/matrix_agent/templates/"

# Helper: check if a path is forbidden. Sets BLOCKED_REASON on match.
check_path() {
  _path="$1"
  _base=$(basename "$_path")
  for F in $DENIED_NAMES; do
    if [ "$_base" = "$F" ]; then
      BLOCKED_REASON="$F is a protected config file"
      return 0
    fi
  done
  for D in $DENIED_DIRS; do
    case "$_path" in *"$D"*)
      BLOCKED_REASON="path contains protected directory $D"
      return 0
      ;;
    esac
  done
  return 1
}

# --- Extract fields ---
COMMAND=$(echo "$INPUT" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1)
TOOL_NAME=$(echo "$INPUT" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"tool_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')

# --- Guard: block ALL git push (host handles pushing after validation) ---
if echo "$COMMAND" | grep -q 'git push'; then
  echo '{"error": "git push is blocked. The host handles pushing after validation."}'
  exit 2
fi

# --- Guard: git add -A / git add . ---
if echo "$COMMAND" | grep -q 'git add -A\|git add \.\|git add --all'; then
  echo '{"error": "git add -A / git add . is blocked. Stage specific files by name: git add <file1> <file2> ..."}'
  exit 2
fi

# --- Guard: git add of forbidden files ---
if echo "$COMMAND" | grep -q 'git add'; then
  for F in $DENIED_NAMES; do
    if echo "$COMMAND" | grep -q "[/ ]${F}"; then
      echo "{\"error\": \"Staging $F is blocked: protected config file.\"}"
      exit 2
    fi
  done
fi

# --- Guard: block writes to forbidden files via write_file/replace tools ---
if [ "$TOOL_NAME" = "write_file" ] || [ "$TOOL_NAME" = "replace" ]; then
  FILE_PATH=$(echo "$INPUT" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"file_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')
  if check_path "$FILE_PATH"; then
    echo "{\"error\": \"Writing to $FILE_PATH is blocked: $BLOCKED_REASON.\"}"
    exit 2
  fi
fi

# --- Guard: block shell commands that write to forbidden files (best-effort) ---
if [ -n "$COMMAND" ]; then
  for F in $DENIED_NAMES; do
    if echo "$COMMAND" | grep -qE "(>|cp |mv |tee |sed -i|cat .*>).*[/ ]?${F}([\"' ]|$)"; then
      echo "{\"error\": \"Shell write to $F is blocked: protected config file.\"}"
      exit 2
    fi
  done
  for D in $DENIED_DIRS; do
    if echo "$COMMAND" | grep -qE "(>|cp |mv |tee |sed -i|cat .*>).*${D}"; then
      echo "{\"error\": \"Shell write to path containing $D is blocked: protected directory.\"}"
      exit 2
    fi
  done
fi

# --- Guard: block IPC files written to wrong location ---
if [ -n "$COMMAND" ]; then
  for IPC_FILE in pr-url.txt acceptance-criteria.md changed-files.txt; do
    if echo "$COMMAND" | grep -q "$IPC_FILE"; then
      if ! echo "$COMMAND" | grep -q "/workspace/.ipc/$IPC_FILE"; then
        echo "{\"error\": \"IPC file $IPC_FILE must be written to /workspace/.ipc/$IPC_FILE, not elsewhere.\"}"
        exit 2
      fi
    fi
  done
fi

echo '{}'
