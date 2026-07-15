# codex-dev 角度槽位入库最小支持 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户已要求在当前共享脏工作区直接继续；不创建工作树、不派生代理、不提交 Git。

**Goal:** 让 `codex-dev` 在保持 identity/style master 行为不变的前提下支持 `ExecutionRequest(step="angle_inventory")`，先完成无网络的离线实现与验收，再等待真实现场批准。

**Architecture:** `run_controller` 继续只提交统一 `ExecutionRequest`；`CodexDevExecutor` 在适配器内部增加 `angle_inventory` 分支。该分支从 manifest 读取单品白底图和既有产品身份档案，加载 canonical Angle Inventory Skill 与 required reference，通过已有 canvas-agent Codex transport 取得结构化结果，严格校验后原子写入 manifest 声明的角度产物目录。

**Tech Stack:** Python 3 标准库、`unittest`、现有 canvas-agent HTTP/SSE；不新增第三方依赖。

## Global Constraints

- 批次固定为 `single`，只按 A、B、C、D 单品槽位识别；忽略 required reference 末尾误植的套装字段，但不修改原规则文件。
- 不修改 Infinite Canvas fork、`scripts/`、`schemas/` 或 `manifests/`。
- 不生成主图/详情图变量配置、最终提示词、图片或 QC。
- `codex-dev` 仍不是默认执行器；demo、openai-image、identity、style master 行为保持不变。
- token、完整提示词、图片正文、Codex 原始错误和完整产品资料不得进入事件日志。
- 阶段 A 不启动真实 Codex、不访问网络、不读取真实批次图片、不写真实角度产物、不改变真实批次状态。
- 不提交、不推送、不创建 PR。

---

### Task 1: 建立角度入库离线契约测试

**Files:**
- Modify: `tests/test_codex_dev_executor.py`

**Interfaces:**
- Consumes: `CodexDevExecutor.execute(ExecutionRequest(step="angle_inventory"))`、现有 `FakeTransport`。
- Produces: 合法角度表 fixture、12 图临时输入和 angle success/error 行为测试。

- [x] **Step 1: 运行修改前全仓基线**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Expected: 当前 108 项测试全部通过；若基线失败，停止实现并排查现有问题。

- [x] **Step 2: 增加合法角度结果 helper**

在 `tests/test_codex_dev_executor.py` 增加一个根据 asset id 列表构造模型返回的 helper：

```python
def valid_angle_inventory(asset_ids: tuple[str, ...]) -> dict[str, object]:
    return {
        "artifact_type": "angle_inventory",
        "angle_slots": [
            {
                "angle_slot": "B",
                "source_asset_id": asset_id,
                "camera_angle": "45°斜侧视",
                "decision_basis": "可见壶口、侧前轮廓和壶身立体关系。",
                "naturally_visible_content": ["壶身立体感", "壶口边缘"],
                "must_not_force_content": ["完整顶部俯视", "背面不可见结构"],
                "suitable_page_tasks": ["整体识别", "立体感展示"],
                "unsuitable_page_tasks": ["完整壶口俯视"],
                "main_image_suitability": "适合：立体关系清楚。",
                "detail_image_suitability": "适合：可说明侧前结构。",
                "risk_notes": "无明显风险",
                "recommended_task_binding": "生活场景主图",
                "admission_result": "合格，可进入对应槽位",
                "merged_reference_note": "无",
                "usable_for": ["主图", "详情图"],
                "notes": "",
            }
            for asset_id in asset_ids
        ],
        "missing_angle_slots": ["A", "C", "D"],
        "retake_recommendations": [],
        "notes": "",
    }
```

- [x] **Step 3: 增加 angle fixture**

fixture 必须：

```python
for index in range(1, 13):
    (white_bg_dir / f"image{index:02d}.jpg").write_bytes(b"offline-jpeg")

(angle_skill_dir / "SKILL.md").write_text(
    "ANGLE_SKILL_MARKER: 只做单品角度槽位入库", encoding="utf-8"
)
(angle_reference_dir / "角度槽位入库表生成与识别提示词.txt").write_text(
    "ANGLE_REFERENCE_MARKER: A/B/C/D；末尾套装字段不适用于本批次", encoding="utf-8"
)
```

复制 context manifest，补充 `batch_type="single"`、`user_declared_set_product=False`、`artifacts.angle_inventory` 和既有身份档案路径，返回 12 个稳定文件名和角度输出目录。

- [x] **Step 4: 写成功路径失败测试**

测试执行 `angle_inventory` 后应得到 `angle_inventory.json`；断言：

```python
self.assertEqual("p1", artifact["product_id"])
self.assertEqual("angle_inventory", artifact["artifact_type"])
self.assertEqual(12, len(artifact["image_assets"]))
self.assertEqual(12, len(artifact["angle_slots"]))
self.assertEqual(
    {item["asset_id"] for item in artifact["image_assets"]},
    {item["source_asset_id"] for item in artifact["angle_slots"]},
)
self.assertEqual("角度槽位入库表已生成", result.detail)
```

提示断言必须覆盖 Skill marker、reference marker、身份档案、`只处理 angle_inventory`、`single`、A/B/C/D 和“忽略套装误植字段”；附件必须正好是 12 张 fixture JPG，且不得包含风格参考图。

- [x] **Step 5: 写输入和范围拒绝失败测试**

增加独立测试：

- 缺少白底图时传输前拒绝。
- 缺少产品身份档案时传输前拒绝。
- `batch_type="set"` 或 `user_declared_set_product=True` 时传输前拒绝。
- 返回含 `set_layouts`、`style_master`、`variable_configs`、`final_prompt`、`images` 或 `qc_results` 时写入前拒绝。
- 已有 `angle_inventory.json` 不覆盖。
- 输出路径不在 `workspace.artifacts_root` 内时传输前拒绝。

- [x] **Step 6: 写结构拒绝失败测试**

从合法返回分别制造：遗漏一个 `source_asset_id`、重复一个 id、未知 id、非法槽位 `E`、非法 `admission_result`、不以允许词开头的主图/详情图适用性、缺少逐图必需字段。每种情况均断言统一“返回格式异常”、不泄露原始正文且没有正式文件。

- [x] **Step 7: 更新非支持步骤测试**

把原测试请求从 `angle_inventory` 改为 `main_vc`，并断言错误明确列出 `identity、style_master、angle_inventory`；验证在传输或文件访问前拒绝。

- [x] **Step 8: 运行目标测试确认 RED**

Run:

```powershell
python -m unittest tests.test_codex_dev_executor.CodexDevExecutorTest -v
```

Expected: 新 angle 测试因适配器当前不支持该步骤而失败；既有 identity/style master 测试继续通过。

---

### Task 2: 实现 codex-dev 的 angle_inventory 分支

**Files:**
- Modify: `canvas-bridge/codex_dev_executor.py`
- Test: `tests/test_codex_dev_executor.py`

**Interfaces:**
- Consumes: `inputs.white_bg_images`、`artifacts.product_identity_archive`、`artifacts.angle_inventory`、`workspace.artifacts_root`。
- Produces: `ExecutionResult(detail="角度槽位入库表已生成", outputs=(angle_inventory.json,), provider="codex-dev", metadata={"thread_id": ...})`。

- [x] **Step 1: 增加 angle 常量和分派**

```python
SUPPORTED_STEPS = frozenset({"identity", "style_master", "angle_inventory"})
ANGLE_SLOT_VALUES = frozenset({"A", "B", "C", "D", "不适合归入现有槽位"})
ANGLE_ADMISSION_VALUES = frozenset({
    "合格，可进入对应槽位",
    "勉强可用，但建议重拍",
    "不适合入库，需重拍",
})
ANGLE_SUITABILITY_PREFIXES = ("适合", "勉强适合", "不适合")
```

`_execute()` 先拒绝非支持步骤，再按 `identity`、`style_master`、`angle_inventory` 显式分派；不修改 `run_controller` 或 registry。

- [x] **Step 2: 加载 Angle Skill 和单品输入**

新增 `_load_angle_inventory_rules()`，读取：

```text
.agents/skills/angle-inventory/SKILL.md
.agents/skills/angle-inventory/references/角度槽位入库表生成与识别提示词.txt
```

新增 `_validate_single_product_batch()`；任何 set 声明都在传输前拒绝。角度分支复用 manifest 白底图读取结果，但用角度专属的缺失/读取错误消息；读取既有产品身份档案，不读取 style master。

- [x] **Step 3: 建立稳定 image_assets**

按附件顺序建立：

```python
image_assets = [
    {"asset_id": f"img_{index:03d}", "file_path": filename, "notes": ""}
    for index, filename in enumerate(source_inputs, start=1)
]
```

该列表由适配器固定，不接受模型覆盖。

- [x] **Step 4: 约束输出路径和提示词**

新增 `_angle_inventory_output_path()`，固定文件名 `angle_inventory.json` 并验证位于 `artifacts_root` 内。新增 `_build_angle_inventory_prompt()`，完整嵌入 Skill、required reference 和身份档案，附 `asset_id -> 文件名` 对照，明确：

- 只处理单品 `angle_inventory`。
- 逐张按实际角度判断，不为凑槽位强行归类。
- 末尾套装字段与本批次冲突，必须忽略。
- 只返回一个 JSON，不返回 Markdown 代码块之外的说明。
- 不得输出风格、变量配置、最终提示词、图片或 QC。

- [x] **Step 5: 严格解析和校验**

新增 `_parse_angle_inventory(text, product_id, image_assets)`：

1. 接受裸 JSON 或单个 JSON fence。
2. 校验 `artifact_type=angle_inventory`。
3. 拒绝套装编排和其他越界顶层字段。
4. 校验 `angle_slots` 是数组且长度等于 `image_assets`。
5. 校验 source id 集合与真实 asset id 集合完全相等。
6. 校验每项必需字段、槽位、入库结论及适用性前缀。
7. 校验 `missing_angle_slots` 只含 A/B/C/D 且不重复。
8. 标准化 `retake_recommendations` 和 `notes`。
9. 强制写入适配器生成的 `product_id`、`artifact_type`、`image_assets`。

- [x] **Step 6: 原子写入和统一结果**

新增 `_write_angle_inventory()`，沿用现有临时文件加 `os.link` 的不覆盖写入策略。成功返回：

```python
ExecutionResult(
    detail="角度槽位入库表已生成",
    outputs=(output_path,),
    provider=self.name,
    metadata={"thread_id": turn.thread_id},
)
```

- [x] **Step 7: 运行目标测试确认 GREEN**

Run:

```powershell
python -m unittest tests.test_codex_dev_executor.CodexDevExecutorTest -v
```

Expected: angle、identity 和 style master 测试全部通过，FakeTransport 是唯一传输。

- [x] **Step 8: 运行执行器回归**

Run:

```powershell
python -m unittest tests.test_codex_dev_executor tests.test_executor_contract tests.test_openai_image_executor tests.test_run_controller -v
```

Expected: codex-dev、demo、openai-image 和控制器全部通过。

---

### Task 3: 文档和阶段 A 离线总验收

**Files:**
- Modify: `canvas-bridge/README.md`
- Modify: `docs/CANVAS_PROJECT_STATE.md`

**Interfaces:**
- Consumes: 已通过的 `angle_inventory` 离线行为。
- Produces: 可恢复的状态记录；不得写“真实角度表已生成”。

- [x] **Step 1: 更新 README**

记录 `codex-dev` 当前支持 identity、style master、angle inventory；angle 分支只读白底图和产品身份档案、固定单品 A/B/C/D、严格一图一条、只写 `angle_inventory.json`，仍不是默认执行器。

- [x] **Step 2: 更新状态账本**

在 `docs/CANVAS_PROJECT_STATE.md` 记录：规则冲突的用户确认口径、阶段 A 代码与离线测试结果、真实执行尚未授权、批次仍为 `needs_angle_inventory`。

- [x] **Step 3: 阶段 A 总验收**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m compileall -q canvas-bridge tests
python canvas-bridge/spike_canvas_push.py --help
git diff --check
```

Expected: 全仓测试零失败；编译和 CLI 返回 0；`git diff --check` 无错误。

- [x] **Step 4: 边界核查**

Run read-only checks proving：

- 依赖声明文件没有变化。
- fork、scripts、schemas、manifest 和真实外部工作区没有被阶段 A 修改。
- 没有真实 codex-dev 服务和持久化 `CODEX_DEV_ALLOW_REAL_EXECUTION`。
- outputs/renders、outputs/repaired 和正式 angle_inventory 仍不存在或为空。
- diff 中没有 token、API key 或产品图片正文。

- [x] **Step 5: 报告并停止**

向用户报告阶段 A 结果，并单独申请阶段 B 权限：调用真实 Codex、消耗配额、读取 12 张白底图、写入正式角度表、追加事件日志并更新画布投影。没有明确批准不得进入 Task 4。

---

### Task 4: 阶段 B 真实现场验收（仅获批后执行）

**Files:**
- Write only declared artifact: `D:\onedrive\OneDrive\Desktop\杯类\shuiping_20260712\artifacts\angle_inventory\angle_inventory.json`
- Append journal: `manifests/shuiping_20260712.events.jsonl`
- Update after verified success: `canvas-bridge/README.md`, `docs/CANVAS_PROJECT_STATE.md`

**Interfaces:**
- Consumes: 阶段 A 已验收的 `codex-dev`、12 张白底图和产品身份档案。
- Produces: 正式角度槽位入库表、统一事件记录和画布成功投影。

- [ ] **Step 1: 现场前检查**

确认 canvas-agent 和原工作画布已连接、正式角度表不存在、无重复真实执行服务、批次路由为 `needs_angle_inventory`，输出目录仍为 0 张图片。

- [ ] **Step 2: 临时启动真实服务**

只在本次隐藏子进程中设置 `CODEX_DEV_ALLOW_REAL_EXECUTION=1`，以 `--executor codex-dev` 启动现有 `--serve`；不持久化开关或 token。

- [ ] **Step 3: 经画布三段门禁执行**

在 `wfrun:shuiping_20260712:batch` 写入 `run: angle_inventory`，等待本次 `step_succeeded` 或脱敏 `step_failed`，不重复提交。

- [ ] **Step 4: 关闭临时服务并校验产物**

停止本次 PID，运行：

```powershell
python scripts/validate_artifact_schema.py --schema schemas/angle_inventory.schema.json --file "D:\onedrive\OneDrive\Desktop\杯类\shuiping_20260712\artifacts\angle_inventory\angle_inventory.json"
```

同时核对 12 个实际文件名全部且仅出现一次、槽位和值域合法、缺失槽位和重拍建议一致、无套装/风格/变量/最终提示词/图片/QC 越界字段。

- [ ] **Step 5: 核对事件、画布和下一阶段**

事件日志只记录通用开始/结果；画布 angle 阶段和产物节点为 success；运行台进入 `needs_main_variable_config` 但不自动执行；outputs/renders 和 repaired 仍为 0。

- [ ] **Step 6: 更新最终现场事实并重复总验收**

只在真实成功后更新 README 与状态账本，并重复全仓测试、compileall、CLI、schema 和 `git diff --check`。不提交 Git。
