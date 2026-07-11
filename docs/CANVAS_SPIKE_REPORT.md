# Canvas Spike Report (Stage 0)

Date: 2026-07-11. Verified against a live infinite-canvas page in the user's browser, connected through canvas-agent.

## Environment

- infinite-canvas fork working copy: `D:\dev\infinite-canvas`, branch `workflow-editor` (base ebd8ae2, unmodified upstream code).
- Services: canvas-agent `http://127.0.0.1:17371`, web `http://localhost:3000`, bridge static file server `http://127.0.0.1:8801`.
- Toolchain: bun 1.3.14, node v24.15.0, python 3.12, codex-cli 0.142.4.
- Bridge: `canvas-bridge/` (this repository), pure stdlib.

## Six Spike Questions

1. **Can the frontend handle 50/100/300 nodes?** PASS with a caveat. 300 stress nodes + 26 pipeline nodes + 320 connections pushed in 0.93s (5 chunks) and applied by the real page. User-reported interaction at 327 nodes: "还算流畅，有一点点卡顿" (slight jank while zoomed out). Node layer has viewport culling; the SVG connection layer does not appear to be culled — the first optimization target if large canvases are ever needed. Real per-batch workload is ~26-30 nodes, 10x below the tested ceiling.
2. **Can an existing workflow be converted losslessly to nodes/edges?** PASS. The fixture batch projects to 26 nodes + 35 edges via `manifests/workflow_graph.template.json`; stage topological order equals `route_batch()` behavior (enforced by `tests/test_workflow_graph_projection.py`); all ops passed canvas-agent zod validation.
3. **Can canvas layout be stored separately from business data?** PASS. `canvas_get_state` returns id/position/width/height for every node; a 327-node layout snapshot was exported to a standalone JSON file. Live user viewport changes were captured in read-backs (k=0.14→0.157 across reads).
4. **Can workflow execution be driven without the original page structure?** PASS (mechanics). canvas-agent spawned the local codex process and listed threads (`/agent/codex/workspace`, `/agent/codex/threads` both ok). A real Codex turn (consumes quota) is deferred until explicitly approved. Python script stages are CLI-callable by design.
5. **Can async task status map onto nodes?** PASS. All 9 stage/gate nodes cycled idle→loading→success in route order on the live canvas via `update_node` metadata ops.
6. **Can images be shown by reference without copying?** PASS. An image node with `metadata.content = http://127.0.0.1:8801/spike.svg` rendered correctly (user-confirmed). Agent-applied `add_node` never calls `uploadImage`, so referenced media stays out of the browser IndexedDB by construction.

## Protocol findings

- End-to-end chain bridge → agent (`POST /api/tools`) → SSE `tool_call` → page reducer → `POST /canvas/result` works for push, update, delete, viewport, and read-back.
- Idempotent projection works: delete-by-id before add, deterministic `wf:<product_id>:<node_id>` ids.
- **Read-after-write race**: the agent serves `canvas_get_state` from its cached snapshot, which updates only when the page re-posts state. A read issued immediately after a write can return stale data (observed once, resolved after ~3s). Phase-1 bridge must treat op results as the source of completion and delay/retry state reads.
- With `?agentUrl=&agentToken=` URL params, first connection still requires one manual click in this build (`CanvasLocalAgentPanel` is mounted without `autoConnect`); afterwards localStorage enables auto-reconnect.

## Verdict

Stage 0 passes. Proceed to Phase 1 (read-only canvas fed by real repository state) per the migration plan. No infinite-canvas source changes were needed for any of the above.

## Reproduce

```powershell
bun run --cwd D:/dev/infinite-canvas/canvas-agent dev
bun run --cwd D:/dev/infinite-canvas/web dev
# open a canvas project page with ?agentUrl=http://127.0.0.1:17371&agentToken=<token from %USERPROFILE%\.infinite-canvas\canvas-agent.json>, click connect once
python canvas-bridge/spike_canvas_push.py --push-batch tests/fixtures/external_workspace_batch_manifest.fixture.json
python canvas-bridge/spike_canvas_push.py --status-demo
python canvas-bridge/spike_canvas_push.py --stress 300
python canvas-bridge/spike_canvas_push.py --get-state --save-layout <path>
python canvas-bridge/spike_canvas_push.py --clear-mine
```
