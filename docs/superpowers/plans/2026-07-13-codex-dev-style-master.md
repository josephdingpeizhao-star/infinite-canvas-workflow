# codex-dev 风格母版最小支持 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本计划因用户要求保留共享脏工作区且禁止未经授权提交，采用当前会话内执行，不创建工作树、不派生代理、不提交 Git。

**Goal:** 让 `codex-dev` 在保持现有 identity 行为不变的前提下支持 `ExecutionRequest(step="style_master")`，并通过真实画布门禁为 `shuiping_20260712` 生成正式风格母版。

**Architecture:** `run_controller` 继续只提交统一 `ExecutionRequest`；`CodexDevExecutor` 在适配器内部按 `identity` / `style_master` 分派。style master 分支从 manifest 读取风格参考图、既有产品身份档案、Skill 与 required reference，通过已有 canvas-agent Codex transport 执行，校验后原子写入 manifest 声明的产物目录。

**Tech Stack:** Python 3 标准库、`unittest`、现有 canvas-agent HTTP/SSE、本地 Infinite Canvas MCP；不新增第三方依赖。

## Global Constraints

- 只把 `D:\onedrive\OneDrive\Desktop\shuiping\风格参考图.png` 导入 manifest 的 `inputs\style_refs`；12 张 JPG 继续只作为白底产品图。
- 不修改 Infinite Canvas fork、scripts、schemas 或 manifest。
- 不生成图片，不进入 angle inventory、变量配置、最终提示词或 QC。
- `codex-dev` 仍不是默认执行器；demo、openai-image、identity 行为不变。
- token、完整提示词、图片正文、Codex 原始错误和产品隐私数据不得进入事件日志。
- 不提交、不推送、不创建 PR。

---

### Task 1: 建立风格母版离线契约测试

**Files:**
- Modify: `tests/test_codex_dev_executor.py`

**Interfaces:**
- Consumes: `CodexDevExecutor.execute(ExecutionRequest(step="style_master"))`、现有 `FakeTransport`。
- Produces: style master 成功、缺失输入、越界返回、已有产物和非支持步骤的可执行验收测试。

- [ ] **Step 1: 运行修改前全仓基线**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Expected: 当前 103 项测试全部通过。

- [ ] **Step 2: 增加合法风格母版样例与 fixture helper**

在 `tests/test_codex_dev_executor.py` 增加：

```python
VALID_STYLE_MASTER = {
    "artifact_type": "style_master",
    "style_master": {
        "visual_positioning": "适合生活方式电商主视觉与品牌首屏。",
        "composition_and_layout": "竖幅，主体偏下居中，左上留出文字区。",
        "background_rules": "暖米色真实空间背景，前中后景清楚。",
        "color_rules": "低饱和米色基底，绿色与粉色作辅助色。",
        "lighting_rules": "左前方柔和自然光，保留真实接触阴影。",
        "subject_presentation_rules": "主体完整展示，视觉权重最高。",
        "prop_rules": "使用克制的花叶与布面环境元素。",
        "typography_rules": "小面积低干扰中文标题，不复制原文案。",
        "negative_space_rules": "左上留白承载标题并形成呼吸感。",
        "visual_mood": "明亮、柔和、生活化，由暖背景和自然光形成。",
        "reusable_rules": ["暖米色多层背景", "左前方柔光", "花叶虚化层次", "克制文字区", "真实接触阴影"],
        "fidelity_enhancements": {
            "style_anchors": ["右后方花叶虚化背景", "左上文字留白", "浅色布面承托"],
            "reusable_prop_clusters": {"must_keep": ["虚化花叶"], "replaceable": ["浅色布面"], "optional": []},
            "background_layers": {"foreground": "少量虚化枝叶", "midground": "浅色布面", "background": "花叶与暖色空间"},
            "prop_density_level": "常规",
            "contents_and_usage_state": "仅记录参考图可见使用状态，不固化具体产品内容物。",
            "text_inheritance": "参考图含小面积中文标题，只继承排版气质。",
            "anti_degradation_rules": ["不得退化为纯白背景", "不得删除前中后景层次"]
        },
        "forbidden_elements": ["不得改变产品身份", "不得改变产品角度", "不得复制品牌或具体文案", "不得退化为白底棚拍", "不得删除主要层次", "不得用硬阴影", "不得堆叠杂乱道具", "不得让文字压住主体"],
        "concise_style_master": "本风格只约束非产品视觉风格，不覆盖产品身份、角度、尺寸比例和单张页面任务。采用暖米色真实空间、多层花叶虚化背景、浅色布面中景、少量枝叶前景和左前方柔和自然光；保留真实接触阴影、左上留白及小面积低干扰中文标题气质，不复制具体文案。道具密度为常规，关键锚点为花叶虚化层、浅色布面承托和暖色空间层次。不得退化为纯白或纯灰背景，不得删除主要前中后景，也不得为适配风格改变商品结构、颜色、材质、图案或配件关系。"
    },
    "missing_information": [],
    "notes": ""
}
```

fixture helper 创建临时 `style-master-extractor/SKILL.md`、required reference、`style_refs/style.png`、既有 `product_identity_archive.json`，并把相应路径加入复制后的 manifest。

- [ ] **Step 3: 写成功路径失败测试**

测试执行 `style_master` 后应得到 `artifacts/style_master/style_master.json`，其 `product_id`、`artifact_type`、`source_references` 由适配器固定；提示词必须包含两个规则 marker、产品身份档案和“只处理 style_master”，附件必须只含 `style.png`。

- [ ] **Step 4: 写安全拒绝失败测试**

增加独立测试：缺少 style reference、缺少产品身份档案、返回含 `final_prompt`、已有 `style_master.json` 均应在写入前拒绝；把原“非 identity”测试改为用 `angle_inventory`，并断言错误显示仅支持 `identity、style_master`。

- [ ] **Step 5: 运行新测试并确认 RED**

Run:

```powershell
python -m unittest tests.test_codex_dev_executor.CodexDevExecutorTest -v
```

Expected: 新 style master 测试因当前适配器“仅支持 identity”而失败；既有 identity 测试继续通过。

---

### Task 2: 实现 codex-dev 的 style_master 分支

**Files:**
- Modify: `canvas-bridge/codex_dev_executor.py`
- Test: `tests/test_codex_dev_executor.py`

**Interfaces:**
- Consumes: `ExecutorContext.manifest` 中的 `inputs.style_reference_images`、`artifacts.product_identity_archive`、`artifacts.style_master`、`workspace.artifacts_root`。
- Produces: `ExecutionResult(detail="风格母版已生成", outputs=(style_master.json,), provider="codex-dev", metadata={"thread_id": ...})`。

- [ ] **Step 1: 添加 style master 常量**

在适配器内部定义 `SUPPORTED_STEPS = {"identity", "style_master"}`、style master 禁止顶层字段集合，以及以下必需键：

```python
REQUIRED_STYLE_MASTER_FIELDS = (
    "visual_positioning",
    "composition_and_layout",
    "background_rules",
    "color_rules",
    "lighting_rules",
    "subject_presentation_rules",
    "prop_rules",
    "typography_rules",
    "negative_space_rules",
    "visual_mood",
    "reusable_rules",
    "fidelity_enhancements",
    "forbidden_elements",
    "concise_style_master",
)
```

- [ ] **Step 2: 按步骤分派但保留统一入口**

把 `_execute()` 调整为先拒绝非支持步骤，再检查临时真实执行开关和 `product_id`；随后分别调用 `_execute_identity(product_id)` 或 `_execute_style_master(product_id)`。`run_controller` 和 registry 不变。

- [ ] **Step 3: 加载规则、参考图与身份约束**

新增 `_load_style_master_rules()`、`_load_style_reference_images()` 和 `_load_product_identity_archive()`：只读取 manifest 声明路径；图片仅接受既有 `SUPPORTED_IMAGE_SUFFIXES`；身份档案必须是 JSON 对象且 `artifact_type=product_identity_archive`；所有读取错误转换成不含原始正文的 `ExecutorExecutionError`。

- [ ] **Step 4: 约束输出路径与提示词**

新增 `_style_master_output_path()`，固定文件名 `style_master.json` 并用 `Path.resolve().is_relative_to(artifacts_root)` 防止越界。新增 `_build_style_master_prompt()`，完整嵌入 Skill、required reference 和身份档案 JSON，明确只返回一个 JSON、不得生成图片或下游产物，并要求 `style_master` 包含 `REQUIRED_STYLE_MASTER_FIELDS`。

- [ ] **Step 5: 校验与原子写入**

新增 `_parse_style_master()`：接受裸 JSON 或单个 JSON fence，验证 `artifact_type=style_master`、无越界字段、`style_master` 是对象、必需键齐全且值非空；适配器覆盖 `product_id`、`artifact_type`、`source_references`，并标准化 `missing_information` 与 `notes`。新增 `_write_style_master()`，采用与 identity 相同的临时文件加 `os.link` 原子落盘策略，已有文件不覆盖。

- [ ] **Step 6: 运行目标测试确认 GREEN**

Run:

```powershell
python -m unittest tests.test_codex_dev_executor.CodexDevExecutorTest -v
```

Expected: style master 与既有 identity 测试全部通过，假传输没有真实网络访问。

- [ ] **Step 7: 运行执行器回归**

Run:

```powershell
python -m unittest tests.test_codex_dev_executor tests.test_executor_contract tests.test_openai_image_executor tests.test_run_controller -v
```

Expected: `codex-dev`、demo、openai-image 和控制器测试全部通过。

---

### Task 3: 文档与离线总验收

**Files:**
- Modify: `canvas-bridge/README.md`
- Modify: `docs/CANVAS_PROJECT_STATE.md`

**Interfaces:**
- Consumes: 已通过的 style master 适配器行为。
- Produces: 可供后续窗口恢复的真实状态记录，不改变业务数据格式。

- [ ] **Step 1: 更新适配器说明**

把 README 中“codex-dev 当前只支持 identity”更新为“当前支持 identity 与 style_master”；记录 style master 会加载对应 Skill/reference、读取既有身份档案作上位约束、只写 `style_master.json`，仍不是默认执行器。

- [ ] **Step 2: 更新状态账本的阶段说明**

在 `docs/CANVAS_PROJECT_STATE.md` 记录本轮离线边界、真实输入位置和验收结果；真实调用完成前不得写“已生成成功”。

- [ ] **Step 3: 完成离线总验收**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m compileall -q canvas-bridge tests
python canvas-bridge/spike_canvas_push.py --help
git diff --check
```

Expected: 全仓测试全部通过；编译和 CLI 返回 0；`git diff --check` 无错误；没有新增依赖或真实网络调用。

---

### Task 4: 安全导入风格参考图并刷新画布

**Files:**
- Copy only: `D:\onedrive\OneDrive\Desktop\shuiping\风格参考图.png` -> `D:\onedrive\OneDrive\Desktop\杯类\shuiping_20260712\inputs\style_refs\风格参考图.png`
- Preserve: `manifests/shuiping_20260712.batch_manifest.json`

**Interfaces:**
- Consumes: 用户明确提供的单张风格参考图。
- Produces: manifest 已声明目录中的一个可路由风格输入。

- [ ] **Step 1: 计算源和目标 SHA-256**

若目标不存在则复制；若已存在且哈希一致则不操作；若哈希不同立即停止，不覆盖。

- [ ] **Step 2: 刷新真实状态和画布投影**

使用既有状态检测和 `spike_canvas_push.py` 投影入口，不修改 manifest。验收画布 `wf:shuiping_20260712:in_style_refs` 显示 `files: 1`、`status: success`，运行台允许 `style_master`。

---

### Task 5: 通过真实画布门禁执行 style_master

**Files:**
- Write only declared artifact: `D:\onedrive\OneDrive\Desktop\杯类\shuiping_20260712\artifacts\style_master\style_master.json`
- Append journal: `manifests/shuiping_20260712.events.jsonl`
- Update docs after verified success: `canvas-bridge/README.md`, `docs/CANVAS_PROJECT_STATE.md`

**Interfaces:**
- Consumes: 已通过离线验收的 `codex-dev`、1 张风格参考图和既有产品身份档案。
- Produces: 正式风格母版、统一事件记录和画布成功投影。

- [ ] **Step 1: 现场前检查**

确认 canvas-agent `/health` 正常、画布已连接、正式风格母版不存在、真实执行服务没有重复实例；任何一项异常先处理，不写运行命令。

- [ ] **Step 2: 临时启动 codex-dev 服务**

仅在该隐藏子进程中设置 `CODEX_DEV_ALLOW_REAL_EXECUTION=1`，以 `--executor codex-dev` 和既有 layout 启动 `spike_canvas_push.py --serve`；不持久化开关或 token。

- [ ] **Step 3: 从画布运行台提交命令**

先 `canvas_get_state`，再把 `wfrun:shuiping_20260712:batch` 最后一行更新为 `run: style_master`。等待事件账本出现本次 `step_succeeded` 或脱敏 `step_failed`，不重复提交同一命令。

- [ ] **Step 4: 成功后立即关闭临时服务**

只停止本次记录的进程 PID，确认真实执行开关未持久化。

- [ ] **Step 5: 校验正式产物**

验证文件可解析、顶层类型为 `style_master`、来源仅为 `风格参考图.png`、主要栏目齐全、没有 identity/angle/variable/final prompt/images/QC 越界字段；运行：

```powershell
python scripts/validate_artifact_schema.py --schema schemas/style_master.schema.json --file "D:\onedrive\OneDrive\Desktop\杯类\shuiping_20260712\artifacts\style_master\style_master.json"
```

Expected: 输出 `PASS` 并返回 0。

- [ ] **Step 6: 核对事件与画布**

事件日志只包含通用开始/成功说明；画布 style master 阶段和产物节点均为 success，运行台进入下一真实阶段；输出目录仍无生成图片。

- [ ] **Step 7: 写入最终现场事实并做最后验证**

只在成功事实确认后更新 README 与状态账本。再次运行全仓测试、compileall、CLI help、schema 校验和 `git diff --check`；检查 fork、scripts、schemas、manifest 均未被修改。
