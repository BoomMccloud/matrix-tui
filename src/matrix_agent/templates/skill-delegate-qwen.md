# Delegate to Qwen Code

Use Qwen Code for writing and modifying code. Invoke it via:

```sh
/workspace/.qwen-wrapper.sh "<detailed task description>"
```

## When to use Qwen
- Writing new code or modifying existing files
- Implementing features, fixing bugs, refactoring
- Writing tests

## When NOT to use Qwen
- Reading or analyzing code (you can do this directly)
- Planning or designing approaches (do this yourself)
- Running shell commands (use your shell tool)

## Important
- Pass the FULL plan context in the prompt — Qwen has no memory of previous calls
- Include specific file paths, function names, and acceptance criteria
- Qwen has a timeout (default 30 minutes) — break large tasks into smaller calls
