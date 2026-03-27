# Matrix-Symphony: KISS Implementation Spec

This document defines a phased, trigger-based architectural upgrade for the **Matrix Agent**. We prioritize **Objective Truth** and **Physical Persistence** (P0) over complex orchestration (P1+).

---

## Phase 0: The "Minimum Viable Orchestrator" (DO THIS NOW)
**Goal:** Make the workspace folder survive the container exit and enforce safety boundaries on the host.

### 1. Workspace Persistence (The "Don't Delete" Rule)
Stop treating the `/workspace` as ephemeral. 
- **Change:** Refactor `SandboxManager` to map `task_id` to a stable host directory: `/var/lib/matrix-agent/workspaces/{task_id}`.
- **Logic:** 
  - If directory exists: `cd repo && git pull`.
  - If directory missing: `mkdir` and `git clone`.
- **Benefit:** Solving 80% of context/speed issues by keeping `node_modules`, `.git`, and build artifacts alive.

### 2. Host-Side Validation (The "Objective Truth" Rule)
The agent is a "hallucination engine"; the host is the "fact-checker."
- **Forbidden File Check:** Hard-coded list in `sandbox.py` (e.g., `pyproject.toml`, `.github/`). If the agent touches these, the host **automatically reverts** the specific files before the PR is pushed.
- **Local Test Runner:** The host detects the project type (e.g., `pytest`, `npm test`) and runs it. If it fails, the agent is forced into a "Fix Loop" without human intervention.
- **Visual Verification (P0):** If UI-related files (CSS, HTML, React, etc.) are modified, the host **must** capture a screenshot using `screenshot.js`. PR creation is blocked if the screenshot fails or shows a broken layout (if automated detection is possible).

### 3. Simple Idempotency
- **Change:** If a GitHub webhook for an existing `task_id` arrives, the `TaskRunner` simply restarts the Gemini session in the existing workspace. No complex "Hibernation" logic is needed yet.

---

## Phase 1: Trigger-Based Upgrades (DO THESE LATER)

Only implement these features when the specific **Scaling Stressor** is met.

### Trigger A: "Reasoning Leakage"
*Condition: You have >3 repos with fundamentally different stacks (e.g., Go vs. React) and a single global prompt starts making incorrect assumptions.*
- **Upgrade:** **In-Repo `WORKFLOW.md`**.
- **Action:** Allow the agent to read `.gemini/WORKFLOW.md` from the target repo to override the global system prompt and verification rules.

### Trigger B: "Code Entropy / High Drift"
*Condition: Multiple humans or agents are modifying the same files on the same branch, leading to constant merge conflicts.*
- **Upgrade:** **The Synchronous Wake-up Protocol**.
- **Action:** Implement mandatory `git fetch` + `git rebase` + "Conflict Resolution" prompt injection on every task resume.

### Trigger C: "High Concurrency (>20 Tasks)"
*Condition: The VPS starts lagging due to the overhead of starting/stopping many Podman containers.*
- **Upgrade:** **Formal State Machine & Hibernation**.
- **Action:** Implement a robust state reconciler (replaces `reconcile_loop`) that manages container lifecycle more granularly (Pause/Resume vs. Stop/Start).

---

## Revised P0 State Machine
1. **DISCOVERED:** Webhook/Message received.
2. **SYNC:** `git clone` or `git pull` into `/workspaces/{task_id}`.
3. **RUN:** Execute `gemini -p "Task Description" -y`.
4. **VERIFY:** 
   - Host runs `check_forbidden()`. Auto-revert violations.
   - Host runs `detect_and_test()`. Return logs to agent if failed.
5. **LAND:** `git push` and `gh pr create`.

---

## Implementation Roadmap

1. **Step 1 (Immediate):** Update `SandboxManager.create` to use a persistent host path based on `task_id`.
2. **Step 2 (Immediate):** Move the `validate_work` logic from a "recommendation" to a **hard gate** in `TaskRunner._process_github`.
3. **Step 3 (Immediate):** Ensure `gh pr create` is only called if `validate_work` passes.
