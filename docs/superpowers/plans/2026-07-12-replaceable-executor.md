# Replaceable Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a provider-neutral executor contract and registry, preserve the demo executor, and provide an offline-testable GPT Image 2 adapter without making a paid API call.

**Architecture:** `run_controller` and the canvas daemon depend only on provider-neutral request/result types and a registry-backed composition root. Concrete adapters own provider details. GPT Image 2 HTTP calls use only Python standard-library networking and an injectable transport.

**Tech Stack:** Python standard library, `unittest`, existing canvas bridge modules, OpenAI Image API (`gpt-image-2`).

## Global Constraints

- Do not add third-party dependencies.
- Do not modify the Infinite Canvas fork.
- Do not call a live model API or generate a real image in this phase.
- Do not store or log API keys.
- Preserve existing canvas commands, gates, journals, and demo behavior.
- Do not modify files under `scripts/`, `schemas/`, or `manifests/`.

---

### Task 1: Provider-neutral contract and registry

**Files:**
- Create: `canvas-bridge/executor_contract.py`
- Create: `canvas-bridge/executor_registry.py`
- Create: `tests/test_executor_contract.py`

**Interfaces:**
- Produces: `ExecutionRequest`, `ImageGenerationTask`, `ExecutionResult`, `Executor`, `ExecutorContext`, `ExecutorRegistry`.
- Registry factories receive `ExecutorContext` and return an object implementing `execute(request)`.

- [ ] Write tests proving immutable request/result DTOs, duplicate registration rejection, unknown-name rejection, and factory creation.
- [ ] Run `python -m unittest tests.test_executor_contract -v`; verify failures are caused by missing modules.
- [ ] Implement the minimal contract and registry.
- [ ] Re-run the focused tests and confirm they pass.

### Task 2: Adapt the demo executor and canvas daemon

**Files:**
- Create: `canvas-bridge/demo_executor.py`
- Create: `canvas-bridge/executor_factory.py`
- Modify: `canvas-bridge/run_controller.py`
- Modify: `canvas-bridge/spike_canvas_push.py`
- Modify: `tests/test_run_controller.py`

**Interfaces:**
- Consumes: `ExecutionRequest`, `ExecutionResult`, `ExecutorContext`, `ExecutorRegistry`.
- Produces: `build_executor(name, manifest, manifest_path=None)` from the composition root.

- [ ] Update tests first so demo execution uses `execute(ExecutionRequest(...))` and returns `ExecutionResult`.
- [ ] Add a test proving the canvas controller can use any fake executor implementing the protocol without provider-specific knowledge.
- [ ] Run focused tests and confirm the expected API-mismatch failures.
- [ ] Move demo subprocess behavior behind `DemoWorkspaceExecutor.execute` and register it in the composition root.
- [ ] Make the daemon construct a generic request and consume a generic result; preserve event text and gates.
- [ ] Re-run focused tests and confirm legacy behavior remains green.

### Task 3: GPT Image 2 adapter with injected HTTP transport

**Files:**
- Create: `canvas-bridge/openai_image_executor.py`
- Create: `tests/test_openai_image_executor.py`
- Modify: `canvas-bridge/executor_factory.py`

**Interfaces:**
- Consumes: provider-neutral `ImageGenerationTask` inside `ExecutionRequest.payload`.
- Produces: `OpenAIImageExecutor.execute(request) -> ExecutionResult`.
- Configuration: `OPENAI_API_KEY`, optional adapter constructor values for base URL and model; no global business configuration.

- [ ] Write failing tests for missing key, JSON generation request, multipart edit request, Base64 decoding, atomic output, malformed response, and sanitized HTTP errors.
- [ ] Run focused tests and confirm failures are due to the missing adapter.
- [ ] Implement an injectable standard-library transport and the two Image API request paths.
- [ ] Register `openai-image` without changing business gates.
- [ ] Re-run focused tests and confirm no test performs network I/O.

### Task 4: Documentation and verification

**Files:**
- Modify: `canvas-bridge/README.md`
- Modify: `docs/CANVAS_PROJECT_STATE.md`

**Interfaces:**
- Documents the stable executor boundary, environment-only secret handling, GPT Image 2 adapter, and remaining production gaps.

- [ ] Document how another provider is registered without changing workflow logic.
- [ ] Record that this phase establishes infrastructure only and does not authorize rendering.
- [ ] Run `python -m unittest discover -s tests` and confirm zero failures.
- [ ] Run `python -m compileall canvas-bridge tests` and confirm zero syntax errors.
- [ ] Inspect `git diff` to confirm no user-owned manifest/report changes were overwritten and no secret appears in the diff.
