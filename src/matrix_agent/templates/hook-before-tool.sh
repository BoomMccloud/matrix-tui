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

echo '{}'
