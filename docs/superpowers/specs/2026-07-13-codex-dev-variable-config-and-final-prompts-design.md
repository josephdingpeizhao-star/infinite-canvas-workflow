# codex-dev 变量配置与最终提示词设计

## 1. 目标

在 `shuiping_20260712` 已完成产品身份档案、风格母版和角度槽位入库的基础上，继续完成：

1. 6 套淘宝天猫单品主图变量配置；
2. 8 套淘宝天猫单品详情图变量配置；
3. 与上述 14 套配置一一对应的最终提示词及索引。

本阶段不生成图片、不准备正式 ComfyUI 作业、不执行 QC，也不补拍或虚构缺失的 D 槽位。

## 2. 已确认业务事实

- 批次类型仍为 `single`。
- 用户确认产品实际品类和用途为家居盛水水壶。
- 用户确认产品高度约 25 厘米。
- 用户确认整批 14 张规划中启用 3 张手持：主图 2 张、详情图 1 张。
- 用户不补拍 D，允许直接使用现有 A、B、C 槽位继续。
- 容量、最大宽度、口径、底径、重量、具体材质、耐热温度、认证、品牌型号仍未确认，不得推断或写入画面承诺。

用户要求不再就场景细节反复提问，因此采用以下执行裁定：允许清水和家居盛水场景；手持只用于握住把手、轻扶壶身或轻微拿起展示真实比例；不安排倾倒、沸腾、炉灶加热、装满热水或其他依赖未确认结构和耐热性能的动作。

## 3. 方案比较与选择

### 方案 A：三个阶段顺序扩展 `codex-dev`（采用）

先扩展 `main_vc`，验收正式主图配置后再扩展/执行 `detail_vc`，最后执行 `final_prompts`。每一步都有独立门禁、事件和产物，失败不会写入半成品，也不会污染下一步。

优点是风险最小、与当前画布运行台和统一执行器边界一致、可逐阶段停止。代价是需要三次独立开发/验收循环。

### 方案 B：一次调用生成 6+8+14 全部内容（不采用）

调用次数少，但单次输出过大，任何局部格式问题都会导致整批失败，也难以定位主图、详情图或最终提示词中的业务问题。

### 方案 C：绕过运行台手工编写配置（不采用）

短期最快，但会绕过三段门禁、事件账本和可替换执行器边界，不符合项目已经确认的工作方式。

## 4. 受控批次声明

先通过原画布 `wfedit:shuiping_20260712:batch` 节点和既有 `--apply-edits` 三段门禁，将：

- `requested_outputs` 设置为 `main, detail, final_prompts`；
- `notes` 写入用户确认的水壶用途、高约 25 厘米、主图 2 张手持、详情图 1 张手持，以及容量/材质等未确认边界。

这些用户事实只覆盖产品身份档案中对应的“无法确认”字段，不覆盖任何已经确认的产品结构、颜色、纹理、把手、花苞、口沿和角度事实。产品身份档案本身不重跑、不覆盖。

受控编辑成功后，真实路由应进入 `needs_main_variable_configs`。

## 5. 主图变量配置阶段

`ExecutionRequest(step="main_vc")` 只接受单品批次，并加载：

- `main-variable-config/SKILL.md`；
- `main-variable-config.runtime_rule_slices.json`；
- 正式产品身份档案；
- 正式风格母版；
- 正式角度槽位入库表；
- manifest 中的用户确认事实。

不重新识别白底图角度，不读取套装输入，不生成最终提示词或图片。

正式输出为 `artifacts/variable_configs/main_variable_configs.json`，必须满足：

- `artifact_type=main_variable_config`；
- `config_count=6`；
- 6 个唯一 `config_id`，固定使用 `main_01` 至 `main_06`；
- 每项 `output_type=main`；
- 每项包含 canonical 主图变量字段；
- 每项只绑定角度表中合格的 A/B/C 源图，禁止 D 和“不适合归入现有槽位”；
- 恰好 2 项启用手持，其余 4 项明确不启用；
- 手持优先绑定 B 或适合的 A，不对 C 做大面积包握；
- 主图画布比例固定 1:1；
- 用户确认高度写为“约 25 厘米”，但不得添加其他尺寸或容量数字；
- 内容物可为空壶或清水，不出现热水、沸腾或倾倒动作；
- 适配器根据 `common_constraints + per_image_overrides` 计算并固定每项 `resolved_variable_config_sha256`，不信任模型自报哈希。

已有正式文件时拒绝覆盖；返回异常、非法槽位、手持数量错误、乱码、套装字段、最终提示词、图片或 QC 字段均在落盘前脱敏拒绝。

## 6. 详情图变量配置阶段

`ExecutionRequest(step="detail_vc")` 在主图配置成功后运行，加载 detail Skill/runtime package、三份上游档案、正式主图配置和 manifest 用户事实。

正式输出为 `artifacts/variable_configs/detail_variable_configs.json`，必须满足：

- `artifact_type=detail_variable_config`；
- `config_count=8`；
- 8 个唯一 `config_id`，固定使用 `detail_01` 至 `detail_08`；
- 每项 `output_type=detail`；
- 模块01至模块08各出现一次且顺序固定；
- 模块01承接主图核心承诺，模块05只标注已确认的“高度约 25 厘米”，不写容量、宽度、口径、重量或材质参数；
- 每项只绑定合格 A/B/C 源图，禁止 D 和不合格源图；
- 恰好 1 项启用手持，其余 7 项不启用；尺寸标注图不得启用手持；
- 详情图画布比例固定 3:4；
- 8 张构图和核心任务有明显差异，但不为差异改变产品角度；
- 只使用当前画面和用户事实能支持的中文营销文案，不写价格、促销、销量、认证、检测、质保、售后或未提供提示；
- 适配器固定引用与哈希，不信任模型返回的路径、产品 id 或哈希。

同样实行不覆盖、原子写入、乱码拦截、越界字段拦截和脱敏错误。

## 7. 最终提示词阶段

`ExecutionRequest(step="final_prompts")` 只在主图和详情图配置均成功后运行。它加载 `final-prompt-compiler` Skill/runtime package、三份上游档案、两份变量配置和用户确认事实，不读取图片、不改变变量配置。

为控制输出大小，使用两个相互独立的专用 Codex thread：一条编译 6 份主图提示词，另一条编译 8 份详情图提示词。两批都通过校验后才一次性写入正式目录；任一批失败都不写正式文件。两个 thread 不共享会话记忆，各自接收完整、相同版本的上游结构化产物和规则。

正式输出仅位于 `artifacts/final_prompts/`：

- `main_01_final_prompt.json/.md` 至 `main_06_final_prompt.json/.md`；
- `detail_01_final_prompt.json/.md` 至 `detail_08_final_prompt.json/.md`；
- `final_prompt_index.json/.md`。

每份 JSON 通过 `final_prompt.schema.json` 所对应的结构校验，并固定：

- 与一个变量配置一一对应；
- 引用正确的上游档案、配置文件、配置序号和已计算哈希；
- `uses_upstream_prompt_files_as_visual_requirements=false`；
- 保留该配置的绑定角度、画布比例、产品身份、颜色、约 25 厘米高度和手持声明；
- 不新增配置中没有的事实、数字、道具、文字或页面任务；
- 不出现 D 槽位、不合格源图、损坏字符、套装字段或其他产品 id；
- 14 份提示词中手持仍恰好为主图 2、详情图 1；
- 不把 Skill/reference 正文当作最终提示词正文。

本阶段不调用 `scripts/compile_final_prompts.py` 的完整 CLI，因为该入口会同时生成正式 ComfyUI 作业和完整性/QC 报告，超出本次范围；也不修改该脚本。`codex-dev` 只生成最终提示词及索引，不生成 `comfyui_job_manifest`、图片或 QC 产物。

## 8. 画布、事件与执行边界

- 上层路由、命令解析、三段门禁和事件格式保持供应商无关，不增加 Codex 专属分支。
- `codex-dev` 仍是可选开发适配器，默认执行器仍是 `demo`。
- 每个真实阶段只启动一个临时 `--serve --executor codex-dev` 进程，真实执行开关只存在于该子进程。
- 每个阶段从原画布运行台提交一次命令，等待本次 `step_succeeded` 或脱敏 `step_failed`，不自动重复提交。
- 任一真实失败均立即停止临时服务；重试需重新评估，不静默消耗配额。
- 事件日志只记录阶段、结果和脱敏说明，不记录 token、完整提示词、图片正文、原始异常或完整产品资料。

## 9. 测试与验收

实现采用 TDD：每个阶段先写会因“不支持该步骤”或缺少校验而失败的测试，确认红灯后再实现最小代码。

离线测试使用假传输和临时工作区，覆盖：

- 输入产物缺失、批次类型错误、正式产物已存在；
- 主图 6 项、详情 8 项、模块覆盖和唯一 id；
- 合法 A/B/C 绑定、D/不合格源图拒绝；
- 主图 2 手持、详情 1 手持及约 25 厘米用户事实；
- 容量、材质、耐热、认证等未确认事实拒绝；
- 哈希由适配器重算；
- 路径越界、套装/图片/QC/未知顶层字段和 Unicode 损坏字符拒绝；
- 最终提示词 14 份一一对应、无部分写入、无 ComfyUI/QC/图片产物；
- identity、style master、angle inventory、demo、openai-image、控制器和路由回归。

现场验收按主图、详情、最终提示词三个阶段依次进行。每阶段成功后核对 schema、数量、文件名、槽位、手持数量、用户事实、事件和画布；最终确认 `renders=0`、`repaired=0`、无 ComfyUI 作业和 QC 产物、临时服务关闭、真实执行开关未持久化。

## 10. 明确不做

- 不补拍 D，不伪造 D；
- 不重跑或覆盖 identity、style master、angle inventory；
- 不修改 Infinite Canvas fork、Skill、runtime reference、schema、scripts 或批次 manifest 的结构；
- 不新增第三方依赖；
- 不生成图片、不调用 ComfyUI、不执行 QC；
- 不提交、不推送、不创建 PR，也不清理现有 Git 工作区。
