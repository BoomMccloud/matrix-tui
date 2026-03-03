#!/bin/sh
# BeforeTool hook — blocks bare git push (must use --force for CI fix flow).
# Exit code 2 = Gemini CLI blocks the tool execution.
# Reads JSON from stdin with tool invocation details.
INPUT=$(cat)

# Check if this is a shell command containing "git push" without "--force"
COMMAND=$(echo "$INPUT" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1)
if echo "$COMMAND" | grep -q 'git push' && ! echo "$COMMAND" | grep -q '\-\-force'; then
  echo '{"error": "git push without --force is blocked. Use create-pr workflow or --force for CI fixes."}'
  exit 2
fi

# Block "git add -A" and "git add ." to prevent scope creep
if echo "$COMMAND" | grep -q 'git add -A\|git add \.\|git add --all'; then
  echo '{"error": "git add -A / git add . is blocked. Stage specific files by name: git add <file1> <file2> ..."}'
  exit 2
fi

echo '{}'
