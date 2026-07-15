# codex-dev 角度槽位入库最小支持设计

## 目标

让真实单品批次 `shuiping_20260712` 在产品身份档案和风格母版均已完成后，通过原画布运行台、三段门禁和可替换执行器边界生成一份正式《角度槽位入库表》。本轮只增加 `angle_inventory` 能力，不生成主图变量配置、详情图变量配置、最终提示词、图片或 QC 产物。

## 用户可感知行为

- 画布运行台在真实路由允许时可执行 `angle_inventory`。
- 执行成功后生成 `D:\\onedrive\\OneDrive\\Desktop\\杯类\\shuiping_20260712\\artifacts\\angle_inventory\\angle_inventory.json`。
- 画布“角度槽位入库”和“角度槽位入库表”节点显示成功，事件日志追加开始与成功记录。
- 产品身份档案、风格母版、12 张白底图、画布命令、三段门禁和既有执行器行为保持不变。
- 成功后批次只进入 `needs_main_variable_config`，不自动执行下一步。

## 已确认的规则解释

`.agents/skills/angle-inventory/references/角度槽位入库表生成与识别提示词.txt` 的主体规则明确要求单品白底图按 A、B、C、D 四个固定角度槽位归类，但文件末尾误插入了“是否套装合影白底图、套装编排槽位、件数核对”等套装字段。用户已于 2026-07-13 明确确认以下口径：

- 本批次是 `single`，只执行单品角度识别。
- 保留 A、B、C、D 槽位及单品逐图字段。
- 忽略末尾与单品职责冲突的套装字段。
- 不修改原 Skill、required reference 或 schema；在适配器提示与返回校验中固定单品边界。

## 选定方案

在现有可选开发适配器 `codex-dev` 内增加第三个受限步骤 `angle_inventory`，复用已经现场验证的 canvas-agent Codex 线程、图片附件分批、HTTP/SSE、空回复拒绝、错误脱敏和原子写入能力。

没有选择新建 `codex-angle-dev`，因为它会增加注册和运行选择成本；没有把适配器重构成通用步骤插件系统，因为本轮只需一个明确步骤，全面重构会扩大对已验收 identity/style master 的回归风险。

## 调用链与隔离边界

```text
画布运行台
  -> 原三段门禁与真实 route_batch
  -> ExecutionRequest(step="angle_inventory")
  -> ExecutorRegistry
  -> codex-dev
  -> canvas-agent 现有 Codex HTTP/SSE 线程
  -> 结构与范围校验
  -> angle_inventory.json
  -> ExecutionResult
  -> 事件日志与画布状态投影
```

- `run_controller`、命令解析、三段门禁和事件日志不增加 Codex 专属分支。
- `codex-dev` 仍不是默认执行器；`demo` 和 `openai-image` 不变。
- Codex 专属线程、模型、附件、请求、SSE、返回解析和错误分类继续只留在适配器内部。
- 不修改 Infinite Canvas fork，不新增第三方依赖，不修改 `scripts/`、`schemas/` 或 `manifests/`。

## 输入边界

- 只从 manifest 的 `inputs.white_bg_images` 读取受支持图片，按稳定文件名顺序加载当前 12 张 JPG。
- 读取既有 `product_identity_archive.json`，只用于核对产品身份和防止虚构；不得用它改变单张白底图的实际角度。
- 不读取风格母版作为角度判断依据，避免风格要求反向改变真实机位。
- 不扫描 manifest 之外的文件夹，不读取套装合影目录或风格参考图。
- 若 `batch_type` 不是 `single`，或 `user_declared_set_product=true`，在传输前拒绝。
- 缺少白底图、产品身份档案或规则文件时，在传输前拒绝。

## 输出结构

正式产物继续遵守现有 `schemas/angle_inventory.schema.json`，顶层至少包含：

- `product_id`
- `artifact_type=angle_inventory`
- `image_assets`
- `angle_slots`
- `missing_angle_slots`
- `retake_recommendations`
- `notes`

`image_assets` 由适配器根据真实附件固定生成，每张图包含稳定的 `asset_id`、实际文件名 `file_path` 和备注。

`angle_slots` 必须让每张源图恰好出现一次。每项包含：

- `angle_slot`：`A`、`B`、`C`、`D` 或 `不适合归入现有槽位`
- `source_asset_id`
- `camera_angle`
- `decision_basis`
- `naturally_visible_content`
- `must_not_force_content`
- `suitable_page_tasks`
- `unsuitable_page_tasks`
- `main_image_suitability`
- `detail_image_suitability`
- `risk_notes`
- `recommended_task_binding`
- `admission_result`
- `merged_reference_note`
- `usable_for`
- `notes`

`admission_result` 只允许：`合格，可进入对应槽位`、`勉强可用，但建议重拍`、`不适合入库，需重拍`。`main_image_suitability` 和 `detail_image_suitability` 必须分别以 `适合`、`勉强适合` 或 `不适合` 表达，并附简要理由。

`missing_angle_slots` 只列 A、B、C、D 中没有合格或可用来源的槽位；`retake_recommendations` 只处理角度、清晰度、遮挡和产品完整性，不涉及风格、道具或审美设计。

## 返回校验

- Codex 只返回一个 JSON 对象；适配器强制覆盖 `product_id`、`artifact_type` 和 `image_assets`，不信任模型填写的文件路径。
- `angle_slots` 数量必须等于源图片数量，且每个 `source_asset_id` 与实际 `image_assets` 一一对应，不得遗漏、重复或引用未知文件。
- 槽位、入库结论和主图/详情图适用性必须在固定词表内。
- 每项的业务字段必须齐全且非空；无明显风险时明确写 `无明显风险`，未提供多角度合并参考图时明确写 `无`。
- 返回若包含套装编排、风格母版、产品身份档案、变量配置、最终提示词、图片或 QC 等越界产物，在写入前拒绝。
- 不为了凑齐槽位强行归类；不从无尺寸参照的白底图虚构尺寸、容量、材质、认证、配件或不可见结构。

## 写入与失败处理

- 输出固定为 manifest 声明的 `artifacts.angle_inventory` 下的 `angle_inventory.json`。
- 输出路径必须仍位于 `workspace.artifacts_root` 内。
- 采用临时文件加原子落盘；已有正式角度表绝不覆盖。
- canvas-agent 缺失、连接失败、线程失败、空回复、非法 JSON、缺图、重复图、非法槽位、越界字段或结构缺失，统一收敛为脱敏的 `ExecutorExecutionError`。
- 事件日志不得记录 token、完整提示词、图片正文、Codex 原始错误正文或完整产品资料。
- 任何失败都不写正式产物、不改变真实批次路由，也不启动后续步骤。

## 测试与阶段验收

### 阶段 A：离线实现

1. 修改前重新运行全仓基线测试。
2. 先写失败测试，确认 `angle_inventory` 当前被安全拒绝。
3. 使用假传输验证：完整规则加载、12 图附件、身份档案约束、单品分流、合法输出、全部图片一一对应、缺图/重复图、非法槽位、套装字段、越界产物、已有文件和路径越界拒绝。
4. 回归验证 identity、style master、demo、openai-image 和原控制器行为不变。
5. 全仓测试、Python 编译、CLI 帮助和 `git diff --check` 全部通过。
6. 确认没有真实网络调用，没有真实产物或批次状态变化，没有新增依赖，也没有修改 fork、scripts、schemas 或 manifest。

### 阶段 B：真实现场验收

阶段 A 完成并向用户报告后，另行申请明确批准。获批后才可：

1. 临时启用真实执行开关并启动 `codex-dev --serve`。
2. 从真实画布运行台经三段门禁执行 `run: angle_inventory`。
3. 读取 12 张白底图并生成正式 `angle_inventory.json`。
4. 用现有 schema 校验产物，核对每张图恰好出现一次、缺失槽位与重拍建议一致。
5. 更新事件账本和画布投影，确认下一阶段为主图变量配置但不自动执行。
6. 关闭临时服务和真实执行开关；确认 outputs/renders 与 outputs/repaired 仍为 0。

## 不包含

- 修改角度 Skill、required reference 或 schema 中的历史冲突。
- 套装角度与编排入库。
- 风格设计、道具设计或文案设计。
- 主图变量配置、详情图变量配置、最终提示词、图片生成和 QC。
- 将 Codex 设为默认执行器或生产永久依赖。
- 重构既有执行器体系或修改 Infinite Canvas fork。
