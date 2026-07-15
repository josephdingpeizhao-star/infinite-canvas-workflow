# codex-dev Variable Config and Final Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the optional `codex-dev` executor to produce six main-image configs, eight detail-image configs, and fourteen final-prompt artifacts for `shuiping_20260712`, then execute the three stages from the original canvas without generating images or QC.

**Architecture:** Keep the provider-neutral controller and executor registry unchanged. Add a focused standard-library-only downstream helper module for user-fact parsing, prompt construction, response validation, hashes, and exclusive writes; `CodexDevExecutor` only orchestrates transport and returns `ExecutionResult`. Main/detail use one Codex thread each; final prompts use two independent threads and commit the complete bundle only after both validate.

**Tech Stack:** Python 3.12 standard library, `unittest`, existing canvas-agent HTTP/SSE transport, existing JSON schemas, Infinite Canvas MCP/bridge.

## Global Constraints

- Repository files remain the source of truth; the canvas is a projection and controlled entry point.
- Do not modify the Infinite Canvas fork, Skill files, runtime references, schemas, scripts, or manifest structure.
- Do not add third-party dependencies.
- Do not generate images, ComfyUI job artifacts, or QC artifacts.
- Do not overwrite identity, style master, angle inventory, or any existing downstream artifact.
- Do not use or fabricate missing D; only qualified A/B/C assets may be bound.
- Preserve user facts exactly: product type `水壶`, height `约 25 厘米`, main handheld count `2`, detail handheld count `1`.
- Capacity, width, diameter, weight, exact material, heat resistance, certification, brand, and model remain unconfirmed.
- No Git commit, push, PR, reset, checkout, clean, or removal of unrelated files.
- Any failed real stage stops immediately; a repeated real call requires fresh user approval.

---

### Task 1: Downstream validation foundation

**Files:**
- Create: `canvas-bridge/codex_dev_downstream.py`
- Create: `tests/test_codex_dev_downstream.py`

**Interfaces:**
- Produces: `UserConfirmedRequirements`, `parse_user_confirmed_requirements()`, `artifact_file_under_root()`, `load_typed_artifact()`, `load_skill_runtime_package()`, `qualified_angle_assets()`, `stable_json_sha256()`, `write_json_exclusive()`, `write_bundle_exclusive()`.
- Consumes later: manifest mappings, repository root, formal identity/style/angle/config artifacts.

- [ ] **Step 1: Write failing tests for the user-confirmed facts**

```python
from codex_dev_downstream import parse_user_confirmed_requirements

NOTES = (
    "用户确认产品类型: 水壶 | 用户确认高度厘米: 25 | "
    "主图手持数量: 2 | 详情图手持数量: 1 | "
    "允许清水场景: 是 | 禁止倾倒与加热: 是 | D槽位不补拍: 是"
)

def test_user_requirements_are_parsed_from_manifest_notes(self):
    req = parse_user_confirmed_requirements({"notes": NOTES})
    self.assertEqual("水壶", req.product_type)
    self.assertEqual(25, req.height_cm)
    self.assertEqual(2, req.handheld_main)
    self.assertEqual(1, req.handheld_detail)
    self.assertTrue(req.allow_clear_water)
    self.assertTrue(req.forbid_pouring_and_heating)
    self.assertTrue(req.missing_d_no_retake)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m unittest tests.test_codex_dev_downstream -v`

Expected: import failure because `codex_dev_downstream` does not exist.

- [ ] **Step 3: Implement the immutable requirements record and strict parser**

```python
@dataclass(frozen=True)
class UserConfirmedRequirements:
    product_type: str
    height_cm: int
    handheld_main: int
    handheld_detail: int
    allow_clear_water: bool
    forbid_pouring_and_heating: bool
    missing_d_no_retake: bool


def parse_user_confirmed_requirements(manifest: Mapping[str, Any]) -> UserConfirmedRequirements:
    notes = str(manifest.get("notes") or "")
    product_type = _required_match(notes, r"用户确认产品类型\s*:\s*([^|]+)").strip()
    height_cm = int(_required_match(notes, r"用户确认高度厘米\s*:\s*(\d+)") )
    handheld_main = int(_required_match(notes, r"主图手持数量\s*:\s*(\d+)") )
    handheld_detail = int(_required_match(notes, r"详情图手持数量\s*:\s*(\d+)") )
    return UserConfirmedRequirements(
        product_type=product_type,
        height_cm=height_cm,
        handheld_main=handheld_main,
        handheld_detail=handheld_detail,
        allow_clear_water=_yes_value(notes, "允许清水场景"),
        forbid_pouring_and_heating=_yes_value(notes, "禁止倾倒与加热"),
        missing_d_no_retake=_yes_value(notes, "D槽位不补拍"),
    )
```

Invalid/missing facts raise `ExecutorExecutionError("codex-dev 缺少有效的用户确认商品信息")` without echoing notes.

- [ ] **Step 4: Add failing tests for path confinement, typed loading, qualified angles, and exclusive writes**

Test that:

```python
self.assertRaises(ExecutorExecutionError, artifact_file_under_root, manifest, "main_variable_configs", "main_variable_configs.json")
self.assertEqual({"img_001", "img_006", "img_007"}, set(qualified_angle_assets(angle_doc)))
self.assertNotIn("img_005", qualified_angle_assets(angle_doc))
self.assertRaises(ExecutorExecutionError, write_json_exclusive, existing_path, {"x": 2}, "主图变量配置")
```

- [ ] **Step 5: Implement minimal reusable helpers**

`artifact_file_under_root()` resolves manifest artifact entries and rejects targets outside `workspace.artifacts_root`; `load_typed_artifact()` checks file existence, JSON object type, `artifact_type`, and matching `product_id`; `qualified_angle_assets()` includes only A/B/C/D records whose admission is not `不适合入库，需重拍`, then removes D when `missing_angle_slots` contains D; `stable_json_sha256()` hashes compact sorted UTF-8 JSON; exclusive writers use same-directory temporary files plus hard links and roll back only files created by the current call.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run: `python -m unittest tests.test_codex_dev_downstream -v`

Expected: all downstream foundation tests pass.

- [ ] **Step 7: Inspect only the task diff; do not commit**

Run: `git diff --check -- canvas-bridge/codex_dev_downstream.py tests/test_codex_dev_downstream.py`

Expected: exit 0.

---

### Task 2: Main variable config support

**Files:**
- Modify: `canvas-bridge/codex_dev_downstream.py`
- Modify: `canvas-bridge/codex_dev_executor.py`
- Modify: `tests/test_codex_dev_downstream.py`
- Modify: `tests/test_codex_dev_executor.py`

**Interfaces:**
- Produces: `build_variable_config_prompt()`, `parse_variable_config_response()` and `CodexDevExecutor._execute_main_variable_config()`.
- Output: `artifacts/variable_configs/main_variable_configs.json`.

- [ ] **Step 1: Add a fixture with the three upstream artifacts, runtime package, and main output directory**

The fixture manifest must include the exact controlled notes string, `requested_outputs=["main", "detail", "final_prompts"]`, and qualified angle records for A/B/C plus rejected img_005 and missing D.

- [ ] **Step 2: Write the main happy-path test before production code**

```python
def test_main_vc_writes_six_configs_with_two_handheld_and_fixed_hashes(self):
    context, output_path = self.make_downstream_fixture(root)
    response = valid_variable_response("main", count=6, handheld=2)
    executor = CodexDevExecutor(context, transport=FakeTransport(response), repository_root=root)
    result = executor.execute(ExecutionRequest(step="main_vc"))
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    self.assertEqual("main_variable_config", artifact["artifact_type"])
    self.assertEqual(6, artifact["config_count"])
    self.assertEqual([f"main_{i:02d}" for i in range(1, 7)], [c["config_id"] for c in artifact["configs"]])
    self.assertEqual(2, handheld_count(artifact))
    self.assertTrue(all(valid_resolved_hash(artifact, c) for c in artifact["configs"]))
    self.assertEqual((output_path,), result.outputs)
```

- [ ] **Step 3: Run the test and verify RED**

Run: `python -m unittest tests.test_codex_dev_executor.CodexDevExecutorTest.test_main_vc_writes_six_configs_with_two_handheld_and_fixed_hashes -v`

Expected: `codex-dev 仅支持 identity、style_master、angle_inventory`.

- [ ] **Step 4: Add exact main field and response validation constants**

`MAIN_REQUIRED_OVERRIDE_FIELDS` must include:

```python
(
    "主图核心承诺", "绑定角度槽位", "角度适配原则", "产品角度依据", "产品颜色依据",
    "辅助参考图调用", "页面任务", "展示重点", "构图方式", "镜头距离", "产品位置",
    "产品占比", "尺寸比例锁定", "输出画布比例", "风格贴合锚点调用", "道具密度等级",
    "背景层次配置", "内容物状态", "道具生成", "手持交互声明",
    "动态手持样式参考图调用", "背景与光线", "文字信息",
)
```

The allowed model top level is only `common_constraints`, `configs`, `handheld_count_summary`, and `notes`. The adapter injects `product_id`, `artifact_type`, `config_count`, `upstream_artifacts`, `output_type`, fixed ids, and hashes.

- [ ] **Step 5: Implement the prompt and parser**

The prompt loads the main Skill and runtime JSON, serializes identity/style/angle and user facts, states no image generation/final prompts/QC, requires exact ids and two handheld declarations, and forbids D/rejected assets. `parse_variable_config_response()` verifies:

```python
expected_ids = [f"main_{i:02d}" for i in range(1, 7)]
expected_ratio = "1:1"
expected_handheld = requirements.handheld_main
```

Every bound-angle string must contain exactly one qualified asset id and an A/B/C slot; every size lock contains `约 25 厘米`; no capacity units or concrete material/heat/certification claims are allowed; U+FFFD and downstream/set fields are rejected.

- [ ] **Step 6: Dispatch `main_vc` in `CodexDevExecutor`**

Add `main_vc` to `SUPPORTED_STEPS`, load structured upstreams and runtime rules, call `_run_transport(prompt, ())`, parse, exclusive-write, and return:

```python
ExecutionResult(
    detail="主图变量配置已生成",
    outputs=(output_path,),
    provider=self.name,
    metadata={"thread_id": turn.thread_id},
)
```

- [ ] **Step 7: Add invalid-response tests and verify RED then GREEN**

Each subtest mutates one property: five/seven configs, duplicate id, D or rejected asset binding, one/three handheld, missing required field, wrong ratio, invented capacity/material, final prompt/image/QC/set field, U+FFFD, existing output, or path outside artifacts root. Each must fail before write with a sanitized message.

Run: `python -m unittest tests.test_codex_dev_executor.CodexDevExecutorTest -v`

Expected: class passes with existing identity/style/angle behavior unchanged.

---

### Task 3: Detail variable config support

**Files:**
- Modify: `canvas-bridge/codex_dev_downstream.py`
- Modify: `canvas-bridge/codex_dev_executor.py`
- Modify: `tests/test_codex_dev_downstream.py`
- Modify: `tests/test_codex_dev_executor.py`

**Interfaces:**
- Produces: detail mode in `build_variable_config_prompt()` / `parse_variable_config_response()` and `CodexDevExecutor._execute_detail_variable_config()`.
- Consumes: formal main variable config in addition to identity/style/angle.
- Output: `artifacts/variable_configs/detail_variable_configs.json`.

- [ ] **Step 1: Write the detail happy-path test**

Assert eight ids `detail_01..detail_08`, modules 01..08 exactly once and in order, one handheld, 3:4 ratio, module05 height-only annotation, all hashes valid, and no output before main config exists.

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m unittest tests.test_codex_dev_executor.CodexDevExecutorTest.test_detail_vc_covers_eight_modules_with_one_handheld -v`

Expected: unsupported step or missing method.

- [ ] **Step 3: Add exact detail fields**

`DETAIL_REQUIRED_OVERRIDE_FIELDS` must include the canonical fields from `detail_required_fields_core`, including `标准模块归属`, `买家疑问`, `信息来源与可用证据`, `平台硬约束检查`, angle/color fields, `尺寸比例锁定`, `输出画布比例`, `尺寸标注信息`, `尺寸标注图规则`, content/scene/text/handheld fields, `真实感要求`, `风格防退化检查`, and `禁止事项`.

- [ ] **Step 4: Implement detail prompt/parser and dispatcher**

Enforce:

```python
expected_ids = [f"detail_{i:02d}" for i in range(1, 9)]
expected_modules = [f"模块{i:02d}" for i in range(1, 9)]
expected_ratio = "3:4"
expected_handheld = requirements.handheld_detail
```

Module05 must include only `高度约 25 厘米` and explicitly prohibit capacity/width/diameter/weight/material numbers; its handheld declaration is disabled. Module01 references a main core promise but does not copy unsupported claims.

- [ ] **Step 5: Add invalid detail tests and verify GREEN**

Reject missing/duplicate modules, wrong order, missing main config, handheld on module05, zero/two handheld, D/rejected angles, unsupported facts, wrong ratio, final/image/QC/set fields, path escape, existing file, and U+FFFD.

Run: `python -m unittest tests.test_codex_dev_downstream tests.test_codex_dev_executor -v`

Expected: all focused tests pass.

---

### Task 4: Final prompt-only support

**Files:**
- Modify: `canvas-bridge/codex_dev_downstream.py`
- Modify: `canvas-bridge/codex_dev_executor.py`
- Modify: `tests/test_codex_dev_downstream.py`
- Modify: `tests/test_codex_dev_executor.py`

**Interfaces:**
- Produces: `build_final_prompt_batch_prompt()`, `parse_final_prompt_batch_response()`, `build_final_prompt_bundle()`, and `CodexDevExecutor._execute_final_prompts()`.
- Outputs: 14 JSON/Markdown prompt pairs plus JSON/Markdown index under `artifacts/final_prompts/`.

- [ ] **Step 1: Make `FakeTransport` accept a result queue**

Existing one-result tests remain valid; final prompt tests provide two results and assert two calls with zero attachments.

- [ ] **Step 2: Write the final prompt happy-path and atomicity tests**

The first response contains `main_01..main_06`; the second contains `detail_01..detail_08`. Assert exact files, prompt count 14, correct variable config path/pointer/hash, correct ratio/angle/height/handheld preservation, and no files under `artifacts/comfyui_jobs` or `artifacts/qc_reports`.

Add a second test where detail response is invalid and assert the final prompt directory remains without formal files.

- [ ] **Step 3: Run tests and verify RED**

Run: `python -m unittest tests.test_codex_dev_executor.CodexDevExecutorTest.test_final_prompts_write_fourteen_prompt_only_artifacts_atomically -v`

Expected: unsupported step.

- [ ] **Step 4: Implement two independent prompt compilation calls**

Each model response shape is exactly:

```json
{
  "prompts": [
    {"config_id": "main_01", "final_prompt": "...", "negative_prompt": "..."}
  ]
}
```

The adapter builds schema fields and variable-config references itself. It validates exact ids, one-to-one count, non-empty prompt text, matching ratio and bound asset, `约 25 厘米`, handheld status, no unsupported dimensions/material/heat/certification, no D/rejected assets, no U+FFFD, no other product id, and no source-rule body copied as final requirements.

- [ ] **Step 5: Implement exclusive bundle commit**

Build all JSON/Markdown bytes in memory first. `write_bundle_exclusive()` preflights every target, creates same-directory temporary files, hard-links all targets, and removes only current-call targets if any link fails. Existing targets cause a refusal before the first formal write.

- [ ] **Step 6: Verify final focused tests GREEN**

Run: `python -m unittest tests.test_codex_dev_downstream tests.test_codex_dev_executor -v`

Expected: all focused tests pass; identity/style/angle tests remain green.

---

### Task 5: Offline stage-A regression and documentation

**Files:**
- Modify: `canvas-bridge/README.md`
- Modify: `docs/CANVAS_PROJECT_STATE.md`
- Existing spec/plan: `docs/superpowers/specs/2026-07-13-codex-dev-variable-config-and-final-prompts-design.md`, `docs/superpowers/plans/2026-07-13-codex-dev-variable-config-and-final-prompts.md`

- [ ] **Step 1: Run the full offline suite**

Run: `python -m unittest discover -s tests -p "test_*.py"`

Expected: exit 0 and all tests pass.

- [ ] **Step 2: Run syntax and CLI checks**

Run: `python -m compileall -q canvas-bridge tests`

Run: `python canvas-bridge/spike_canvas_push.py --help`

Expected: both exit 0.

- [ ] **Step 3: Update stage-A documentation**

Record supported steps, no-image/no-QC boundary, user-fact precedence, exact counts, path/no-overwrite rules, and current offline-only status. Do not claim real success yet.

- [ ] **Step 4: Verify diff hygiene; do not commit**

Run: `git diff --check`

Run: `git status --short --branch`

Expected: no whitespace errors; intentional dirty worktree remains.

---

### Task 6: Controlled batch declaration and real main_vc acceptance

**Files/state:**
- Modify through existing canvas gate: `manifests/shuiping_20260712.batch_manifest.json`
- Append through controller: `manifests/shuiping_20260712.events.jsonl`
- Create formal artifact: external workspace `artifacts/variable_configs/main_variable_configs.json`

- [ ] **Step 1: Preflight**

Re-run full tests; confirm current route `awaiting_requested_outputs`, formal main/detail/final files absent, 17 real canvas nodes, no duplicate service, env flag unset, renders/repaired 0.

- [ ] **Step 2: Update the canvas edit node and apply existing three gates**

Set:

```text
requested_outputs: main, detail, final_prompts
notes: 用户确认产品类型: 水壶 | 用户确认高度厘米: 25 | 主图手持数量: 2 | 详情图手持数量: 1 | 允许清水场景: 是 | 禁止倾倒与加热: 是 | D槽位不补拍: 是
```

Run the existing `--apply-edits` command with the real manifest. Confirm route becomes `needs_main_variable_configs` and the canvas adds the conditional downstream nodes without losing existing positions.

- [ ] **Step 3: Start one temporary real service and submit `run: main_vc` once**

Set `CODEX_DEV_ALLOW_REAL_EXECUTION=1` only in the child process, use `--executor codex-dev`, write exactly one command from the original run node, wait for terminal event, stop service immediately.

- [ ] **Step 4: Validate formal main artifact**

Run existing schema validator against `schemas/main_variable_config.schema.json`, then custom checks for six ids, qualified A/B/C assets, two handheld, 1:1, height 25, no unsupported facts/D/set/final/image/QC fields, hashes, and zero renders/repaired.

On failure: stop; do not submit a second main call without fresh approval.

---

### Task 7: Real detail_vc acceptance

**Files/state:**
- Append event journal.
- Create external `artifacts/variable_configs/detail_variable_configs.json`.

- [ ] **Step 1: Confirm route `needs_detail_variable_configs` and main artifact still valid**

- [ ] **Step 2: Start one temporary service and submit `run: detail_vc` once**

- [ ] **Step 3: Stop service at terminal event and validate**

Validate schema, eight ids, module01..08 coverage, module05 height-only annotation, one handheld not on module05, 3:4 ratio, qualified A/B/C only, hashes, no unsupported facts/D/set/final/image/QC, and zero outputs.

On failure: stop; no automatic retry.

---

### Task 8: Real final_prompts acceptance

**Files/state:**
- Append event journal.
- Create external `artifacts/final_prompts/*` prompt-only artifacts.

- [ ] **Step 1: Confirm route `needs_final_prompts` and both config artifacts validate**

- [ ] **Step 2: Start one temporary service and submit `run: final_prompts` once**

- [ ] **Step 3: Stop service at terminal event and validate the complete bundle**

Run schema validation on all 14 JSON prompts. Confirm exact 14 ids, one-to-one config refs and hashes, ratios, angles, height, main handheld 2/detail handheld 1, no unsupported facts/U+FFFD/other product/D/rejected asset, and valid index count 14.

Confirm `artifacts/comfyui_jobs`, `artifacts/qc_reports`, `outputs/renders`, and `outputs/repaired` remain empty. Route should become `ready` for requested outputs, but no render or QC command is submitted.

On failure: stop; no automatic retry.

---

### Task 9: Final project ledger and verification

**Files:**
- Modify: `canvas-bridge/README.md`
- Modify: `docs/CANVAS_PROJECT_STATE.md`

- [ ] **Step 1: Record actual per-stage events, artifact paths/hashes, route, canvas state, and zero-output boundary**

- [ ] **Step 2: Run fresh full verification**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m compileall -q canvas-bridge tests
python canvas-bridge/spike_canvas_push.py --help
python scripts/validate_artifact_schema.py --schema schemas/main_variable_config.schema.json --file <formal-main>
python scripts/validate_artifact_schema.py --schema schemas/detail_variable_config.schema.json --file <formal-detail>
git diff --check
```

Also validate all 14 final prompt JSON files, route/canvas/event agreement, no live service, no persistent real-execution flag, no ComfyUI/QC/image artifacts, and unchanged fork/scripts/schemas.

- [ ] **Step 3: Report in business language; do not commit**

State what changed, what did not, how to verify, impact, and safe recovery. Do not present commit/PR options because the user explicitly prohibited Git integration.
