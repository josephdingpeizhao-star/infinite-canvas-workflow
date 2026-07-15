# Codex Dev Model Compatibility Implementation Plan

> **For agentic workers:** Execute inline in the current session. Do not dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `codex-dev` create canvas-agent Codex threads with the verified `gpt-5.5` model and treat failed/interrupted app-server turns as failures instead of empty successful replies.

**Architecture:** The provider-specific model choice remains inside `codex-dev`. The main workflow continues to send only `ExecutionRequest(step="identity")`. canvas-agent gains a generic optional model field at its existing thread boundary and preserves the app-server turn status in `agent_done`; it does not acquire workflow semantics.

**Tech Stack:** Python standard library, TypeScript, Node built-in test runner, existing `tsx` development runtime.

## Global Constraints

- Do not change the global Codex model or authentication.
- Do not add dependencies.
- Do not change `run_controller`, command parsing, three gates, event format, manifest, schemas, scripts, or product artifacts.
- Do not call the real product batch during offline implementation.
- Do not commit, push, or create a PR.
- Preserve the existing dirty worktrees, including `D:\dev\infinite-canvas\web\bun.lock`.

---

### Task 1: canvas-agent optional model and terminal status

**Files:**
- Create: `D:\dev\infinite-canvas\canvas-agent\src\agents.test.ts`
- Modify: `D:\dev\infinite-canvas\canvas-agent\src\agents.ts`
- Modify: `D:\dev\infinite-canvas\canvas-agent\src\http-server.ts`
- Modify: `D:\dev\infinite-canvas\canvas-agent\package.json`

**Interfaces:**
- Consumes: HTTP JSON `{ "model": "gpt-5.5" }` at `POST /agent/codex/threads/new`.
- Produces: `thread/start` parameters with optional `model`; `agent_done` with app-server `status`; failed/interrupted turns become rejected turns.

- [x] **Step 1: Write failing Node tests**

Test `codexThreadStartParams("C:/workspace", "gpt-5.5")` includes the model while an omitted model remains omitted. Test `codexTurnFailure` returns no failure for `completed`, and a safe failure for `failed` and `interrupted` even when `turn.error` is absent.

- [x] **Step 2: Run tests to verify RED**

Run: `npm test`

Expected: FAIL because the exported helpers and test script do not exist yet.

- [x] **Step 3: Implement the minimal generic boundary**

Add `model?: string` to the internal Codex run options, pass it to `thread/start`, accept a trimmed optional model in `/agent/codex/threads/new`, and derive terminal failure from the app-server `Turn.status`. Include status in `agent_done` without adding workflow-specific logic.

- [x] **Step 4: Run Node tests and TypeScript build**

Run: `npm test`

Expected: PASS.

Run: `npm run build`

Expected: exit 0.

### Task 2: codex-dev selects the verified compatible model

**Files:**
- Modify: `canvas-bridge/codex_dev_executor.py`
- Modify: `tests/test_codex_dev_executor.py`

**Interfaces:**
- Consumes: `CanvasAgentCodexTransport(model="gpt-5.5")`.
- Produces: `/agent/codex/threads/new` request body containing only the optional development model; failed/interrupted `agent_done` becomes sanitized `CanvasAgentTransportError("thread")`.

- [x] **Step 1: Write failing Python tests**

Assert the new-thread request body is `{ "model": "gpt-5.5" }`. Add an SSE fixture with `agent_done.status = "failed"` and assert the transport returns the unified `thread` error without reading a false assistant result.

- [x] **Step 2: Run focused tests to verify RED**

Run: `python -m unittest tests.test_codex_dev_executor.CanvasAgentCodexTransportTest`

Expected: FAIL because the current request body is empty and status is ignored.

- [x] **Step 3: Implement the minimal adapter change**

Store the model inside `CanvasAgentCodexTransport`, send it only when creating the dedicated thread, and reject non-completed terminal status before thread result parsing.

- [x] **Step 4: Run focused and full tests**

Run: `python -m unittest tests.test_codex_dev_executor.CanvasAgentCodexTransportTest`

Expected: PASS.

Run: `python -m unittest discover -s tests`

Expected: all tests pass.

### Task 3: Fork ledger and project handoff

**Files:**
- Modify: `D:\dev\infinite-canvas\FORK_NOTES.md`
- Modify: `D:\dev\infinite-canvas\CHANGELOG.md`
- Modify: `D:\dev\infinite-canvas\docs\content\docs\progress\pending-test.mdx`
- Modify: `canvas-bridge/README.md`
- Modify: `docs/CANVAS_PROJECT_STATE.md`

- [x] **Step 1: Register every fork anchor and the new test file**

Document that the fork only gained generic optional model/status transport behavior and no workflow semantics.

- [x] **Step 2: Update user-facing handoff records**

Record the confirmed root cause, offline fix, unchanged batch state, and the remaining live acceptance gate.

- [x] **Step 3: Final verification**

Run Node tests/build,  full Python tests, Python compile check, CLI help, `git diff --check` in both repositories, secret scan, and process/state checks. Confirm no real Codex service or product artifact was created by offline implementation.
