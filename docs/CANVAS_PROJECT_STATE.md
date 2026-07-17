# 画布化子项目状态总账（Canvas Project State）

> **本文件是画布子项目的唯一权威状态账本。**任何智能体（Codex、Claude 或其他）在触碰画布相关代码前必须先读完本文件；任何改变画布子项目状态的会话，结束前必须更新本文件（见文末"维护协议"）。本文件取代任何工具私有的会话记忆。
>
> 最后更新：2026-07-17（`shuiping_20260712` 已完成一次且仅一次真实 `run: qc` 验收：事件于 12:06:06 记录 `step_started`、12:32:16 记录 `step_succeeded`，耗时 1569.9 秒；唯一 `qc_report.json` 的 SHA-256 为 `54ADB10B8D573E266EC24E65FC45A2E62DD50F05AF7547FD5B79BC06F5D6ED0D`。报告按序覆盖 14 张图和 175 条固定检查，`jsonschema` 校验通过；共有 19 个 issues（0 critical、0 major、2 minor、17 needs_review）和 19 个 repair_targets。事件现为 72 行，真实路由为 `ready`；14 张正式图字节数与六份上游指纹均未改变，真实执行开关和临时运行服务已清除，画布 QC 阶段、产物、运行台和日志已投影成功。本轮未重试，未执行 ComfyUI 或 repaired；返修路线待用户另行决定）。

## 1. 定位与目标

把本仓库的水壶电商图工作流投影到 [infinite-canvas](https://github.com/likeXFR/infinite-canvas)（本地自部署白板应用）上，并逐步升级为工作流的**可视化控制台**。终局目标（已定决策②）：画布成为工作流定义的主编辑器。

**硬性原则（违反即架构事故）**：

1. **仓库文件是唯一事实来源，画布只是投影。**画布对业务数据的写入只允许经过白名单+门禁通道（阶段 3 的配置字段、阶段 4 的运行命令），其余场景零写入。
2. **fork 低侵入。**infinite-canvas fork 只允许"新增文件 + 登记在册的锚点行"，一切能放主仓库 `canvas-bridge/` 的逻辑禁止写进 fork。细则与锚点清单见 fork 仓库根目录 `FORK_NOTES.md`（位置见 §5）。
3. **`canvas-bridge/` 纯 Python 标准库**，与 `scripts/` 一致，不引第三方依赖。

## 2. 架构（已定路线三）

```
主仓库 canvas-bridge/ (纯标准库 Python)
    │  HTTP /api/tools (token 鉴权)
    ▼
canvas-agent (bun, 端口 17371)  ←── SSE ──→  浏览器画布页 (web, 端口 3000)
```

- 桥接层通过 canvas-agent 的 `canvas_apply_ops` / `canvas_get_state` 等工具操作画布；agent 把操作转发给当前连接的浏览器画布页执行。
- 投影是幂等全量（先删后建，按 `wf:<product_id>:` 前缀）+ 增量更新（`runtime_update_ops` 视图 diff）。
- 画布节点 id 方案：`wf:<pid>:<图节点id>`（工作流图）、`wfedit:<pid>:batch`（配置编辑）、`wfrun:<pid>:batch`（运行台）、`wflog:<pid>:events`（日志投影）。

## 3. 决策日志（全部已定，勿重新讨论）

| # | 决策 | 日期 |
|---|---|---|
| ① | 没有现成前端可复用，选择 infinite-canvas 作画布基座 | 2026-07-11 前 |
| ② | 画布终局 = 工作流定义的主编辑器（不止只读投影） | 同上 |
| ③ | 接受 fork/修改 infinite-canvas（AGPL，本机自用不分发） | 同上 |
| ④ | 暂不做多人协作 | 同上 |
| ⑤ | 执行层采用可替换执行器边界；Codex 只作为开发工具或可选适配器，正式方向为中央后台直接调用模型 API，不把业务逻辑绑定到 Codex、模型名或供应商 | 2026-07-12 更新 |
| ⑥ | 布局文件（canvas_layout）进 Git | 同上 |

## 4. 阶段进度台账

| 阶段 | 内容 | 状态 | 关键提交 | 验收证据 |
|---|---|---|---|---|
| 0 | 协议冒烟（六个尖峰问题） | ✅ | — | `docs/CANVAS_SPIKE_REPORT.md` |
| 1 | 只读实时投影（状态点亮、--watch 增量） | ✅ | f2293a5 之前诸提交 | 测试 + 现场 |
| 2 | 布局持久化（canvas_layout，按图节点 id） | ✅ | f2293a5 | 测试 + 现场 |
| 3 | 受控编辑（wfedit 节点，三段门禁，--apply-edits） | ✅ | 28788bb | 2026-07-11 晚现场四步验收（含 banana 拒绝、210 字符长 notes 回读无截断） |
| 4 | 执行接入（wfrun 运行台 + 事件日志 + --serve 常驻） | ✅ | 6f4e361 | 2026-07-11 深夜现场：9 步全链路画布触发跑通、门禁拒绝、retry；66/66 测试绿 |
| 4b | 生产图片执行链（prompts-only 门禁 + 索引组装 + image-production） | ✅ 真实出图完成，QC 已验收 | 5f98b35、6f9ffcb | 238/238 测试；真实门禁 pass；14 张正式图完整，QC 后字节数复核不变 |
| 4c | `codex-dev / qc`（7 个两图批次 + 全批总结） | ✅ 离线 TDD 与真实 QC 均完成 | 634c58f | 260/260 测试；真实报告 175 条、19 issues、19 repair_targets；事件 72 行，路由 `ready` |

qc 路由缺陷修复（门禁报告不再算质检完成）、孤儿修复（重投影删除全图节点 id，20ba7a8）均已入库。

2026-07-16 按获批方案完成生产图片执行链的阶段 A/B。既有完整性脚本新增 `--prompts-only` 独立确定性分支，按主图 6 项、详情 8 项核对数量/顺序、`final_prompt.schema.json`、上游路径、源文件 SHA-256、逐项解析指纹、2+1 手持数量、1:1/3:4 字面、已确认高度约 25 厘米语义与 Unicode；不读取 ComfyUI 作业清单，并在报告记录跳过旧内容启发式扫描与旧编译器扫描的原因。默认 ComfyUI 模式、原报告行为、路由、Schema、批次 manifest 与正式产物均未改变。新增任务组装器按索引顺序绑定白底图目录中的唯一同名参考图，主图映射 `1024x1024`，详情图暂映射 `1024x1536`，已有 PNG 自动跳过；新增注册执行器 `image-production` 只接受 `integrity/renders`，真实渲染受 `RENDER_ALLOW_REAL_EXECUTION=1` 与 `OPENAI_API_KEY` 双门禁控制，可用 `RENDER_MAX_IMAGES` 限量，失败保留已成功图片并脱敏错误。全仓由 197 项增至 227 项并全部通过；当前真实 14 份提示词只读内存门禁为 pass、0 阻断/0 警告，现场索引只组装出 6+8 个任务。全程未设置真实开关、未读取密钥、未访问网络、未生成图片、未写正式报告或事件，ComfyUI/QC/renders/repaired 继续为 0，39 行事件与六份关键正式产物保持不变。

真实批次试点（不作为新的架构阶段）：`shuiping_20260712` 已于 2026-07-12 建立单品批次，12 张白底 JPG 从桌面源文件夹复制到 manifest 声明的外部工作区，逐文件 SHA-256 一致；`workflow_doctor.py` 已识别下一 Skill 为 `product-identity-archive`，并已向“无限画布 1”首次投影 17 个批次节点。真实 `codex-dev` identity 已做阶段 B 尝试但未成功产出档案，因此仓库业务状态仍停留在 `needs_product_identity_archive`，不得继续后续业务步骤。

同日已从“无限画布 1”精确删除 `demo_live` 的 29 个演示节点，保留 `shuiping_20260712` 的 17 个真实批次节点；删除时未发现演示 `--serve` 常驻进程，因此不会由后台自动重建。演示工作区文件本身仍保留，未作删除。

阶段 B 重新打开“无限画布 1”时，现场发现 `demo_live` 节点已再次出现，并确认存在一个指向 `D:\dev\canvas-demo-workspace` 的 demo `--serve` 常驻进程；它会在画布重连后重新投影演示节点。该进程和演示节点不属于本轮 identity 执行范围，未停止、未删除；当前画布同时包含 demo 与 `shuiping_20260712` 节点，后续若要恢复“仅真实批次”状态需单独处理。

同日开始生产化执行边界改造：新增供应商无关的 `ExecutionRequest` / `ExecutionResult`、显式执行器注册表和组合入口；现有 demo 已迁移到统一协议。新增 `openai-image` 适配器，默认对接官方 `gpt-image-2`，同时支持无参考图的 generations 请求和带参考图的 edits 请求；只使用 Python 标准库，密钥仅从服务端 `OPENAI_API_KEY` 读取。该适配器目前只通过注入假 HTTP 传输完成离线测试，未调用真实 API、未生成真实图片，也尚未从最终提示词产物自动组装批量图片任务。

同日完成 `codex-dev` 第一阶段离线实现：该适配器注册在同一执行器边界之后且不是默认执行器，当前只接受 `identity`。它完整读取仓库 canonical `product-identity-archive` Skill 和 required reference，复用 canvas-agent 现有的 Codex 新线程、HTTP 与 SSE 能力，不在 Python 中启动另一套 Codex 会话；SSE 只作完成通知，最终结果从本次专用 thread 重新读取。传输只接受本机回环地址并显式禁用系统代理；输出必须位于 manifest 声明的 `artifacts_root` 内且不覆盖已有档案。最终返回必须区分已确认事实、可见推断、无法确认和禁止虚构内容，越界产物或异常结构在写入前拒绝。缺少 canvas-agent、连接失败、线程失败和格式异常均收敛为切断原始异常链的脱敏 `ExecutorExecutionError`。真实执行默认关闭，阶段 B 获批后才可在该次进程临时设置 `CODEX_DEV_ALLOW_REAL_EXECUTION=1`，不写配置、不持久化。阶段 A 的自动测试只使用假传输和临时工作区；在阶段 A 截止时尚未调用真实 Codex、读取真实批次图片、写入真实产品档案、改变真实批次状态或更新现场画布。

阶段 B 于同日获得明确批准并从画布运行台经三段门禁触发。现场测得 12 张输入原始共 41,563,687 bytes，Base64 后约 52.85 MiB，超过 canvas-agent 单次 30MB JSON 上限；因此新增同一专用 thread 内的 4+4+4 分批上传（各批约 16.65/17.45/18.75 MiB）及最终统一综合，并补充“本线程 turn 完成但无 assistant 文本时立即失败”的保护。真实 gpt-5.6-sol 第一批 turn 连续三次完成但均无 assistant 消息、工具调用或错误；第一次因旧等待逻辑人工停止，后两次由适配器在 `manifests/shuiping_20260712.events.jsonl` 记录脱敏 `step_failed`。要求分批返回非空 `batch_observation` JSON 后结果仍为空，说明阻塞位于 canvas-agent/Codex 返回兼容链路而非请求体上限或业务输出校验。没有写入 `product_identity_archive.json`，没有生成图片、调用 ComfyUI、修改 manifest 或前进真实批次状态；临时 `CODEX_DEV_ALLOW_REAL_EXECUTION` 进程均已关闭，开关未持久化。现场尝试后又离线补强两层传输门禁：单张附件超过 20 MiB 时在连接前拒绝，完整 JSON 请求体超过默认 28 MiB 时不发送；助手消息数和用户消息数也改为独立跟踪，避免多消息线程误判。全仓测试现为 102 项通过。

2026-07-13 完成只读兼容性排查并获得用户对最小 fork 修改的明确批准。三份失败 rollout 均由 canvas-agent 内置 `@openai/codex 0.139.0` 执行，图片已被正确解码缩放为 2048×1365，首批四图合计仅约 580KB；同版本模型目录不包含实际继承的全局 `gpt-5.6-sol`。单变量验证中，0.139 使用该继承模型的最小纯文本回合失败且无 assistant，而显式改为其支持的 `gpt-5.5` 后正常返回 `OK`，因此根因为客户端版本与模型配置错配，不是图片、SSE、三段门禁或产品提示词。fork 只增加通用的可选模型 thread 参数和真实 `completed` / `failed` / `interrupted` 状态保真；`codex-dev` 在适配器内部选择 `gpt-5.5`，业务层仍无模型或 Codex 专属分支。离线测试为 fork 2 项 Node 测试、TypeScript 构建和主仓库 103 项 Python 测试通过。

同日随后从“无限画布 1”真实运行台再次写入 `run: identity`，原三段门禁放行后由 `codex-dev` 在同一 `gpt-5.5` thread 中完成 4+4+4 三批图片观察和最终综合。四个回合均产生非空 assistant；最终档案原子写入 `D:\onedrive\OneDrive\Desktop\杯类\shuiping_20260712\artifacts\identity\product_identity_archive.json`，共引用 12 个源文件，完整包含已确认事实、可见推断、无法确认和禁止虚构内容，不含 style master、角度表、变量配置、最终提示词、图片或 QC 字段。真实尺寸全部标为无法确认且无虚构数字，既有 `product_identity_archive.schema.json` 校验通过。事件账本新增一组 `step_started` / `step_succeeded`，运行台和 identity 阶段/档案节点投影为成功。批次现自然进入 `needs_style_master`，但因没有风格参考图而阻断；本轮不越界生成风格母版或任何后续产物。临时 `codex-dev --serve` 和真实执行开关已关闭。

2026-07-13 用户随后提供 `D:\onedrive\OneDrive\Desktop\shuiping\风格参考图.png` 并批准扩展 `codex-dev` 的第一种方案。离线阶段已按 TDD 增加 `style_master` 分支：完整加载 `style-master-extractor` Skill 与 required reference，读取既有产品身份档案作为上位约束，只从 manifest 声明的风格参考目录加载图片，只允许写入 `artifacts_root` 内的 `style_master.json`；缺少输入、缺少身份档案、越界返回、异常结构和已有产物均在写入前脱敏拒绝。上层控制器、命令门禁和事件日志没有 Codex 专属分支，demo、openai-image 和 identity 回归通过。此记录写入时尚未复制真实参考图、调用真实 Codex、写入正式风格母版或改变真实批次状态。

同日随后完成 style master 现场验收。只将明确命名的 `风格参考图.png` 复制到 manifest 声明的 `inputs\style_refs`，源/目标 SHA-256 均为 `733855F89F072D45F0D3918CE6378041E6C4F2C475BFBA3540EAC09CA39FD411`；同目录其余 12 张 JPG 没有被误作风格输入。画布运行台经原三段门禁执行 `run: style_master`：首次 Codex 返回了完整风格内容，但把 `forbidden_elements` / `concise_style_master` 错放顶层并提前关闭根对象，严格 JSON 门禁因此记录一次脱敏 `step_failed`，没有落盘。系统化排查后只在适配器提示中增加明确 JSON 层级骨架，未放宽解析器；离线回归通过后再次经同一门禁执行成功，耗时 107.0s。正式产物原子写入 `D:\onedrive\OneDrive\Desktop\杯类\shuiping_20260712\artifacts\style_master\style_master.json`，只引用该 PNG，14 个必需栏目齐全，含 12 条禁止项、8 个风格锚点、8 条可复用规则和 375 字精简母版；既有 `style_master.schema.json` 校验通过，不含产品身份、角度表、变量配置、最终提示词、图片或 QC 越界字段。事件账本和画布阶段/产物节点均投影为成功，批次进入 `needs_angle_inventory`。临时真实执行服务已关闭，执行开关未持久化，renders/repaired 仍均为 0 张。

同日用户确认继续“角度槽位入库”，并明确裁定 canonical required reference 末尾混入的“套装合影、套装编排、件数核对”字段不适用于本 `single` 批次：保留主体规则的 A/B/C/D 单品槽位和逐图字段，不修改原 Skill、reference 或 schema。`codex-dev` 已按 TDD 增加 `angle_inventory` 阶段 A 分支：只读取 manifest 白底图与既有产品身份档案，不读取风格母版改变角度；适配器固定真实 `image_assets`，要求每张源图恰好对应一条记录，并校验合法槽位、入库结论、主图/详情图适用性、缺失槽位和重拍建议。set 批次、缺图、缺身份、重复/未知图片、非法槽位、套装或下游越界字段、路径越界和已有正式产物均在写入前脱敏拒绝。上层控制器、命令门禁、事件日志和注册表没有 Codex 专属分支；阶段 A 只使用 12 张轻量临时图片和假传输，全仓 114 项测试、Python 编译、CLI 帮助与 `git diff --check` 均通过。未启动真实 Codex、未读取真实批次图片、未写入正式角度表、未改变事件账本或画布状态，真实批次仍为 `needs_angle_inventory`。

同日随后完成 angle inventory 阶段 B 真实现场验收。首次正式结果虽然写入成功，但业务复核发现竖拍图按原始横向像素误判，且含损坏字符；该结果没有被覆盖或删除，而是保留为 `artifacts\audit\angle_inventory\angle_inventory.first-run-rejected-20260713-161428.json`。第一次受控重做被新增的损坏字符门禁安全拒绝，没有正式落盘。只读排查确认 12 张图中 11 张 JPEG 的 EXIF Orientation 为 8，附件预处理没有按该显示方向呈现；主仓库适配器随后仅用 Python 标准库读取 EXIF 并把逐图显示旋转说明交给识别线程，同时补强 A/B/D 边界、缺失槽位一致性和损坏字符拒绝，未修改 Infinite Canvas fork、Skill、required reference、schema、scripts 或批次 manifest。全仓基线增至 116 项通过后，用户明确批准第二次真实调用；原画布三段门禁于 16:50:43 唯一提交 `run: angle_inventory`，16:55:06 成功，耗时 262.2s。正式 `angle_inventory.json` 的 SHA-256 为 `D89DCE8F21BF8E5EFA487061CAD94E4470526461065F3EFFE1029E9AA3C33695`，既有 schema 校验通过，12 个实际文件名全部且仅出现一次：A 4 张、B 1 张、C 1 张、6 张“不适合归入现有槽位”，仅缺 D，并给出直立低角度及局部完整性重拍建议。产物不含 Unicode 损坏字符，也不含套装、风格、变量配置、最终提示词、图片或 QC 越界字段。事件账本、角度阶段节点和产物节点均投影为成功；临时 `codex-dev --serve` 已立即关闭，真实执行开关未持久化，renders/repaired 均为 0。由于 manifest 的 `requested_outputs` 为空，真实路由为 `awaiting_requested_outputs`、`next_skill=null`，没有自动执行主图变量配置或任何后续步骤。

同日用户明确要求不补拍 D、直接继续后续流程，并确认商品为家居盛水水壶、高度约 25 厘米、14 张规划中主图 2 张手持与详情图 1 张手持；未确认容量、其他尺寸、重量、具体材质、耐热、认证、品牌和型号。`codex-dev` 已按 TDD 完成下游阶段 A：`main_vc` 固定生成 6 套 1:1 主图配置且恰好 2 套手持；`detail_vc` 固定生成模块01至模块08共 8 套 3:4 配置且恰好 1 套手持，模块05只允许标注“高度约 25 厘米”且不得手持；`final_prompts` 使用两个独立 Codex thread 分别编译 6+8 套，只有两批均通过才原子写入 14 份 JSON/Markdown 和索引。三步只使用正式身份、风格、角度和变量配置档案，只绑定合格 A/B/C，不读取真实图片、不使用 D、不生成 ComfyUI 作业、QC 或图片；编号、产品 id、上游路径和哈希由适配器固定，异常、越界、Unicode 损坏与已有产物均在写入前拒绝。全仓 136 项测试、Python 编译、CLI 帮助和 `git diff --check` 通过。本记录写入时下游真实调用尚未开始，manifest 仍未受控声明 `requested_outputs`，正式主图/详情配置与最终提示词仍不存在，事件和画布仍保持角度验收后的状态。

同日随后经“无限画布 1”的配置编辑节点和三段门禁受控写入 `requested_outputs: main, detail, final_prompts` 及用户确认事实，真实路由进入 `needs_main_variable_configs`。`main_vc` 首次调用于 17:48:46 因适配器未兼容规则原文的 canonical 手持值而安全失败；TDD 修正后于 17:56:14 从原画布唯一重提，17:59:44 成功，耗时 209.7s。正式 `main_variable_configs.json` 的 SHA-256 为 `4AAA599531C04F03B1206B71AA262515FA75D52D6A22CE5025CE4BCC9506BFAB`，既有 schema 校验通过，包含 `main_01` 至 `main_06` 六项、全部 1:1、恰好 `main_02` 与 `main_05` 两项手持、只绑定合格 A/B/C 源图并固定“约 25 厘米”；不含 D、被拒绝源图、未确认参数、图片、ComfyUI 或 QC 内容。路线随后进入 `needs_detail_variable_configs`。

`detail_vc` 的真实现场验收未形成正式产物，当前安全暂停。三次经原画布和三段门禁的调用分别为：18:03:44 开始、18:09:17 因返回含 3 个 Unicode 损坏字符失败；18:20:55 开始、18:31:53 因“避免塑料感”“不生成认证编号”等明确禁止语被校验器误判为正向商品事实而失败；18:44:54 开始、18:51:27 再次因真实 Unicode 损坏字符失败。两类校验兼容差异已用 TDD 修正：允许规则原文的完整模块名，允许“不要 / 不出现 / 不生成 / 避免”等否定语境，同时继续拒绝正向未确认材质或认证宣称；整批画布初始投影超时后的小批次有序回退也已增加测试保护。全仓基线现为 139 项通过，Python 编译、CLI 帮助和 `git diff --check` 通过。最后一次失败后临时 `codex-dev --serve` 已关闭，真实执行开关未持久化；正式 `detail_variable_configs.json` 不存在，最终提示词未执行，renders/repaired 仍均为 0。原工作画布仍为 27 个节点、32 条连接，详情阶段、运行台和日志已精确投影为失败终态。恢复时必须从 `needs_detail_variable_configs` 重新开始，先解决 Codex 长 JSON 偶发损坏问题；不得复用失败线程正文绕过正式运行台落盘，也不得跳过详情配置直接执行最终提示词。

同日随后完成长 JSON 受控恢复的离线实现，尚未进行新的真实调用。`detail_vc` 现在在一个专用 Codex thread 内按 `detail_01～02`、`03～04`、`05～06`、`07～08` 四段返回；每段带固定段号和精确配置编号，全部在内存中重组后仍执行原有八模块完整校验和排他原子写入。只有 U+FFFD 或无法解析的截断 JSON 被视为可恢复传输损坏，系统会在同一 thread 完整重发该段，不做本地字符替换，也不从失败正文拼补；全任务最多恢复 2 次，合法 JSON 中的越界字段、错误模块、非法角度、未确认事实等业务错误立即拒绝且不重试。目标测试先按旧实现红灯，完成后全仓增至 145 项通过，覆盖四段成功、Unicode/截断恢复、两次上限、越界不重试、段号和配置覆盖以及失败不留正式文件。此次仅修改主仓库 `canvas-bridge/`、测试和文档；未修改 fork、scripts、schemas、Skill、runtime reference 或 manifest，未启动真实 Codex、未消耗配额、未写真实产物、事件日志或画布。正式路由仍为 `needs_detail_variable_configs`，下一次真实调用前必须重新检查并获得用户明确批准。

2026-07-14 用户明确批准后完成一次且仅一次新的真实 `detail_vc` 画布提交。现场先重新通过 145 项测试、Python 编译、CLI 帮助、主图 JSON 与 `git diff --check`，再恢复原项目 `hPbkNXg3WA0p2i46VOh3s`（27 节点、32 连接）并只在临时 `codex-dev --serve` 子进程设置真实执行开关。事件账本于 20:16:34 记录 `step_started`，20:19:38 以脱敏“详情图变量配置分段结构异常”记录 `step_failed`；该结果不是 U+FFFD 或 JSON 截断，而是返回段的段号/配置编号结构不符合约定，按规则属于不可用传输修复掩盖的正式结构错误，因此不会以该结构错误为由重发；本轮也没有再次提交画布命令。正式 `detail_variable_configs.json` 未写入，变量目录仍只有已验收主图文件，`final_prompts`、renders、repaired 均为 0。临时服务已在失败终态后立即停止，真实执行开关未持久化；画布运行台与日志已投影本次脱敏失败。真实路由仍为 `needs_detail_variable_configs`，后续若要调整段结构容错或再次调用，必须重新离线设计、测试并单独取得用户批准。

随后已完成此次失败的离线根因修复和受控格式纠正，尚未发起新的真实调用。只读复现确认：canvas-agent 曾对 Codex stdout 的每个 Buffer 单独解码，中文字符跨数据块时会产生 U+FFFD；fork 现改为同一输出流连续 UTF-8 解码，并用中文 JSON 跨字节边界测试锁定，3 项测试与 TypeScript 编译通过。主仓库继续使用同一 thread 四段返回：U+FFFD 或可确认的截断 JSON 前缀仍最多整段重发 2 次，空回复以及完整或前部已有语法错误的 JSON 立即拒绝；只有第 1 段配置与公共约束已通过比例、角度、模块、手持数量、未确认事实和越界字段门禁，但顶层 `notes` 不是字符串时，最多允许 1 次同线程完整格式纠正。纠正前后必须保持业务配置指纹一致，第四段还会结合前三段核对完整 8 项手持数量和汇总；纠正提示不回显失败正文，本地不搬移字段、不替换字符。相同的 25 厘米数值若在当前字段或嵌套字段路径中用于宽度、直径等未确认语义也会拒绝；段号、配置编号、覆盖、比例、角度、模块、手持或商品事实错误仍立即拒绝。四段全部通过后才在内存重组并执行原八模块完整校验与排他原子落盘。全仓增至 155 项测试通过；本轮未改变 `ExecutionRequest / ExecutionResult`、三段门禁、默认 `demo`、`openai-image`、其他 `codex-dev` 阶段或既有产物格式，未启动真实 Codex、未消耗配额，未改真实批次产物、事件日志或画布。正式路由仍为 `needs_detail_variable_configs`，正式详情配置仍不存在；下一次真实 `detail_vc` 必须再次取得用户明确批准，且成功后也不得自动执行 `final_prompts`。

同日用户再次明确批准后，从原项目 `hPbkNXg3WA0p2i46VOh3s` 经三段门禁完成本轮一次且仅一次 `run: detail_vc` 提交。调用前主仓库 155 项测试、Python 编译、CLI 帮助、主图 schema 与 `git diff --check` 全部通过，fork 侧 3 项测试、TypeScript 构建与 `git diff --check` 通过；27 个节点、32 条连接、正式产物指纹、29 条事件基线及真实执行开关均复核无误。事件账本于 22:16:59 记录 `step_started`，22:20:35 以脱敏“详情图变量配置包含未确认参数”记录 `step_failed`。该终态拒绝属于正式业务门禁拒绝，不是 U+FFFD 或 JSON 截断，因此没有因终态业务错误再次重发，也没有第二次画布提交；失败线程的只读证据同时显示，第 1 段在到达该终态前曾按传输完整性门禁完整重发 1 次。正式 `detail_variable_configs.json` 及临时半成品均未写入，变量目录仍只有已验收主图文件；真实路由保持 `needs_detail_variable_configs`，`final_prompts`、renders、repaired 均为 0，上游正式产物指纹未变。临时 `codex-dev --serve` 已停止，真实执行开关在进程、用户和机器范围均未保留；停止后仅依据事件事实账本把详情阶段、运行台和日志三个投影节点同步为脱敏失败终态，未再次调用 Codex。若要分析或调整“未确认参数”拒绝原因，必须先离线只读诊断；任何新的真实 `detail_vc` 调用仍需用户另行明确批准。

同日随后完成“未确认参数”失败的离线只读诊断与窄范围 TDD 修复，尚未发起新的真实调用。脱敏证据表明，返回内容没有正向宣称容量、宽度、直径、重量、准确材质、耐热、认证、品牌或型号；被拒内容只是 `尺寸比例锁定` 中的已确认“约 25 厘米”以及明确禁止补写未确认参数的安全约束，因此属于校验误判。校验器现在只在专用 `尺寸比例锁定` 叶子中接受精确的已确认高度简写，或在同一子句明确出现“尺寸比例锁定”/“高度”语义时接受该高度；裸数值、容量和重量单位、宽度/直径等其他尺寸、区间、负数、伪装单位、数值前后或嵌套路径中的自然宽度表达仍在写入前拒绝，“宽松/宽阔”等视觉描述不受影响。新旧边界先以失败测试复现，再完成最小修正；全仓 161 项测试、Python 编译、CLI 帮助、已验收主图 schema 与 `git diff --check` 均通过，并经独立只读审查确认无重要问题。本轮未修改 fork、scripts、schemas、manifest、执行器默认值或产物格式，未启动真实 Codex、未消耗配额，未写正式详情配置、事件日志或画布；真实路由仍为 `needs_detail_variable_configs`，任何新的 `detail_vc` 真实调用仍需用户另行明确批准，成功后也不得自动执行 `final_prompts`。

2026-07-15 完成画布子项目 P0 存档：主仓库执行器成果提交 `4c4dfe6e243d6f1be17848c66399e40b0c876381`、真实批次三件套提交 `88ebd441dd0cacabe9fee170127680181a127dd0`、历史 plans/specs 提交 `e32cdc81499d029133d1d50066c95ff9a4da3e74`；fork 锚点 #5–#8 提交 `91e40d04b3c45eb51b0f597ee3beae38b9204c50`。账本修正与既有报告存档收录于本记录所在 docs 提交，其最终哈希见 Git 日志与完成汇报。本次只做存档和账本事实修正，未修改代码逻辑，未启动或调用真实 Codex/模型；`启动画布.bat` 继续因包含本机绝对路径而有意不入 Git。

2026-07-15 完成运行卫生修复（用户选择 R3=a、R6=a）：未入 Git 的 `启动画布.bat` 第三服务由 demo `--serve` 改为 `shuiping_20260712` 的 `--watch` 只读投影；它只读取真实批次事实并向画布同步状态，不创建执行器，也不读取或执行 `run:` / `retry:` 命令。为避免冷启动时画布尚未连接导致首次投影退出，主流程现先等待 agent/web、打开 Chrome，再启动固定的只读投影专用分支；分支先给页面 3 秒连接时间，只有 `--watch` 非正常退出时才每 5 秒重试，正常停止不会自动复活。真实工作区建批时的旧 manifest 已改名为 `batch_manifest.initial-snapshot-20260712.json` 并标注为非权威快照，仓库 manifest 继续是唯一事实入口。本次未启动服务、未调用真实 Codex/模型，未修改代码逻辑、业务路由或已有产物状态；该启动修复也不会主动删除浏览器中可能已存在的 demo 节点，如仍需清理必须另行批准。

同日用户在完整预检通过后明确回复“执行”，随后又明确授权代写；从原项目 `hPbkNXg3WA0p2i46VOh3s` 经 canvas-agent 一次且仅一次提交 `run: detail_vc`。事件账本于 14:19:27 记录 `step_started`，14:23:37 以脱敏“codex-dev 收到的详情图变量配置包含未确认参数”记录 `step_failed`，耗时 250 秒（4 分 10 秒）。本轮只有这一条画布命令，失败后未重试、未改写命令，也未执行 `final_prompts`；本次专用 thread 只返回前两段，受控传输恢复与包装格式纠正均为 0 次。离线只读诊断确认第 1 段通过，第 2 段没有传输损坏，首个拒绝点是手持声明按规则填写的已确认 25 厘米尺寸摘要未被高度语义识别；仅在内存中消除该歧义后，下一拒绝点又是“不把具体材质写死为……”这一安全否定句未命中现有否定词表。两处都没有正向添加未确认参数或商品事实，属于校验器误判，不是模型输出业务错误；失败正文未被复用为产物。正式 `detail_variable_configs.json` 及临时半成品均未落盘，真实路由仍为 `needs_detail_variable_configs`，`final_prompts`、renders、repaired 均为 0；临时 `--serve` 已停止，真实执行开关在进程、用户和机器三个作用域均为空。修复尚未实施，下一步只能离线补回归测试并做窄范围 TDD 修复；任何新的真实调用仍须用户另行明确批准。

同日随后完成上述两处误判的纯离线类级 TDD 修复，未发起任何真实 Codex/模型调用。已确认高度的判定由“少数语境白名单”反转为“精确等于用户确认高度且单位为厘米/`cm` 时默认合法”，detail 分段、detail 整包、main 整包与 `final_prompts` 批次共同受益；竞争维度、区间/连字、负号、单位扩展和相邻乘号尺寸组仍拒绝，其他既有单位及非确认高度厘米值没有放宽。材质/认证扫描只新增结构完整的“不把/不将……写死/固定/标注/设定/锁定/指定为/成……”受限保护，且只保护“为/成”后的目标列表；该结构中夹带或另起的正向事实仍拒绝，既有保护词行为不变，“采用不锈钢”“不是塑料”等硬反例仍拒绝。未确认参数与商品事实现在在 `_reject_unsupported_claims()` 内按本次输入一次收集后统一报错，消息只含类别、净化字段路径和计数，最长 200 字符；未知键名使用占位符，不回显原文或数值上下文，段号、结构、模块、角度、比例和手持等其他校验继续立即失败。`_is_confirmed_height_measurement()` 的显式 `if` 分支由 8 个降为 0 个。全仓增至 180 项测试通过；本轮只修改主仓库 downstream 校验、相关测试、README 与本账本，未修改 executor、提示词构建、协议、恢复/纠正次数、指纹、排他落盘、三段门禁、schemas、scripts、Skill、manifest、fork、真实工作区、事件账本或画布。正式路由仍为 `needs_detail_variable_configs`，正式详情配置仍不存在；第 7 次真实 `detail_vc` 验收必须另行取得用户明确批准。

同日用户在阶段 A 全量预检通过并收到阶段 B 报告后明确回复“执行，代写”，授权从原项目 `hPbkNXg3WA0p2i46VOh3s` 代写一次且仅一次 `run: detail_vc`。主仓库 `main @ bd66cb6`、180 项测试、`git diff --check`、fork `workflow-editor @ 91e40d04` 的 3 项测试与 TypeScript 构建、33 行事件基线、四份上游哈希、三个作用域的真实执行开关、27 个节点/32 条连接及运行台失败终态均先复核通过；没有并行 demo、`--watch` 或其他 `--serve` 进程。临时 `codex-dev --serve` 于 17:00:49 启动，事件账本 17:01:40 记录唯一 `step_started`，17:09:53 记录 `step_succeeded`，总耗时 492.6 秒；本次受控恢复 0 次、格式纠正 0 次，没有第二条画布命令。正式 `detail_variable_configs.json` 通过既有 schema，SHA-256 为 `D1844F639F835446BFDCF2217C62AD4F6F09D0B1AEB7F2BD2CE46BCD933B189C`；业务复核确认 `detail_01` 至 `detail_08` 恰好 8 项、顺序覆盖模块01至模块08、全部 3:4、仅 `detail_02` 一项手持且规则调用值符合 canonical 约定、`detail_05` 为唯一尺寸标注图且只标注“高度约 25 厘米”并明确禁止容量/宽度/直径/重量/材质等未确认项、全部只绑定正式角度表中合格的 A/B/C 源图，不含 D、被拒源图、其他产品测量值或 Unicode 损坏字符。四份上游正式产物哈希保持不变，真实路由前进至 `needs_final_prompts`，`final_prompts`、renders、repaired 仍均为 0；临时运行台已立即停止，真实执行开关在进程、用户和机器三个作用域均为空，画布详情阶段、详情产物、运行台和日志均投影为成功。下一步只能等待用户另行批准 `final_prompts`，不得自动执行。

同日用户在阶段 A 全量预检和禁止项审计通过、收到阶段 B 报告后明确批准，随后从原项目一次且仅一次代写 `run: final_prompts`。事件账本以 35 行为基线，于 18:10:24 记录唯一 `step_started`，18:12:37 以脱敏“codex-dev 收到的主图最终提示词未保留手持状态”记录 `step_failed`，耗时 133 秒；本轮没有重试、第二条画布命令或继续失败 thread。正式 `artifacts\final_prompts` 目录在包含隐藏项和临时项的递归核对后仍为 0，`artifacts\comfyui_jobs`、`artifacts\qc_reports`、`outputs\renders`、`outputs\repaired` 也均为 0；identity、style master、angle inventory、main VC、detail VC 五份正式上游哈希与阶段 A 基线一致。真实路由保持 `needs_final_prompts`，`next_skill=final-prompt-compiler`，无 blocker；唯一临时 `codex-dev --serve` 已停止，真实执行开关在进程、用户和机器三个作用域均为空，只读 `--watch` 已恢复，画布最终投影为失败。失败 thread 正文未复用为产物，也未记录到仓库、事件或文档；`reports/current_state.md/.json` 保持不刷新。本次不推测更深根因，不执行渲染或 QC；再次真实执行前必须先完成离线只读诊断、形成新方案并重新取得用户明确批准。

同日随后完成 `final_prompts` 编译指令契约缺失的纯离线 TDD 修复，未发起任何真实 Codex/模型调用。根因确认为构建指令只抽象要求保留手持与绑定状态，却没有给出响应校验器要求的字面短语；响应解析器和校验器保持一行不动。构建指令现从当次正式变量配置逐编号推导手持启用/禁用状态、唯一绑定源图编号与 A/B/C 槽位，并明确写入与校验器一致的肯定短语、完整否定短语、源图编号和槽位字样；启用手持项同时明确禁止用包含肯定子串的完整否定短语蒙混。绑定推导直接复用既有 `_validate_bound_angle()`，零个或多个合格源图命中及非法槽位都会在对应批次调用前以脱敏错误拒绝。全仓由 180 项增至 188 项测试并全部通过，15 个 `canvas-bridge` Python 模块编译通过，CLI 帮助与 `git diff --check` 正常；本轮只修改最终提示词构建函数、新增一个私有绑定助手、相关测试、README 与本账本，未修改 executor、传输、四段协议、排他落盘、三段门禁、schemas、scripts、Skill、manifest、fork、真实工作区、事件账本或画布。五份上游正式产物哈希、37 行事件、空的 `final_prompts`/ComfyUI/QC/renders/repaired 目录和 `needs_final_prompts` 路由保持不变。

同日用户在完整预检通过并审阅详细执行方案后明确回复“执行，代写”，授权从原项目 `hPbkNXg3WA0p2i46VOh3s` 一次且仅一次提交 `run: final_prompts`。事件账本以 37 行为基线，于 20:05:38 记录唯一 `step_started`；主图独立 thread 于 20:05:41 至 20:07:07 完成，详情独立 thread 随后于 20:07:10 至 20:09:24 完成；事件账本于 20:09:24 记录 `step_succeeded`，总耗时 225.9 秒。`artifacts\final_prompts` 排他落盘恰好 30 个文件，无子目录、隐藏项或临时项：14 份 JSON、14 份同名 Markdown 及 JSON/Markdown 两份索引；14 份 JSON 全部通过既有 `final_prompt.schema.json`，JSON 与 Markdown 逐份一致，`final_prompt_index.json` SHA-256 为 `59029077689084B2FFF09934774EAB968BAB4934A239CE67D80A5335D435F45E`。业务复核确认 `main_01` 至 `main_06` 与 `detail_01` 至 `detail_08` 全部保留对应 img 编号、A/B/C 槽位、1:1 或 3:4 比例和已确认高度约 25 厘米；仅 `main_02`、`main_05`、`detail_02` 启用手持，14 份 negative prompt 均非空，不含 D、被拒源图、未确认商品事实或 Unicode 损坏字符。每份提示词引用三份公共正式上游和本图对应变量配置，整批引用集合完整覆盖五份正式上游；变量配置源哈希及逐项解析哈希均一致，五份上游文件哈希保持阶段 A 基线不变。事件账本最终为 39 行，ComfyUI 作业、QC 报告、renders、repaired 均为 0；真实路由进入 `ready`，`next_required_skill=null`，运行台“可运行：无”，最终提示词阶段与产物节点投影为成功。临时 `codex-dev --serve` 已立即停止，真实执行开关在进程、用户和机器三个作用域均为空；本轮未执行 integrity、renders、QC 或任何第二条画布命令。

2026-07-16 完成 `shuiping_20260712` 批次收口后的三项运行卫生收尾。校验器按窄范围 TDD 补齐“长/深/厚”单字紧邻已确认高度数值时的竞争维度拒绝，覆盖数值前、数值后和字段路径三种语境；“高约 25 厘米”“整壶高度约 25 厘米”、手持声明中的“整壶整体约 25 厘米”、尺寸比例锁定/已确认高度路径以及“提梁较长”“深色背景”等安全语境保持放行，宽/区间/尺寸组/其他单位既有拒绝不变，全仓由 188 项增至 192 项并全部通过，15 个 `canvas-bridge` Python 模块编译通过。状态报告以 `python scripts/workflow_doctor.py --skip-startup-cleanup` 刷新，实际 Git 变化全部位于 `reports/`；`current_state` 现与现场一致为 `ready`、下一 Skill 为空、missing=0、blocked=0，`startup_hygiene.status=pass`、`mode=report_only_no_delete`、清理动作与候选计数均为 0，渲染、ComfyUI 与缺少完整性门禁时渲染仍在禁止清单。发现 workflow_doctor.py 启动清理默认启用且含回收站删除能力，本次以 --skip-startup-cleanup 规避；默认值翻转与 AGENTS.md 补记已列为后续待办。未入 Git 的 `启动画布.bat` 仅把 Chrome 地址从项目列表页改为“无限画布 1”项目直达 URL，保持纯 CRLF；冷启动后 agent/web、直达页面与真实批次只读 `--watch` 均恢复，现场连续读取为 27 节点/32 条连接，最终提示词阶段与产物节点为成功，完整性门禁与报告节点保持 idle。六份关键正式产物哈希和 39 行事件账本复核不变，ComfyUI/QC/renders/repaired 仍为空；本次零真实 Codex/模型调用，未设置真实执行开关，未提交 `run:`/`retry:` 命令，未启动 `--serve`，未修改 manifest、schema、script、Skill、fork、正式产物或事件账本。

同日完成上述 `workflow_doctor.py` 启动清理安全待办（代码提交 `ff622b9`）。参数解析已析出为可测试纯函数；无参数与兼容参数 `--skip-startup-cleanup` 均完全跳过清理选择、候选生成和回收站操作，只有显式 `--apply-startup-cleanup` 才会启用清理，两个参数同时出现仍报错。新增 5 项回归测试后全仓由 192 项增至 197 项并全部通过，Python 编译通过；唯一一次端到端验证使用无参数 `python scripts/workflow_doctor.py`，退出码为 0，汇总中 `applied=false`、候选数、移入回收站数和失败数均为 0，刷新后 `startup_hygiene.mode=report_only_no_delete`、清理动作与安全候选计数仍为 0。`AGENTS.md` 已补记四项校验报告与两份 `current_state` 的刷新范围、回收站无确认弹窗能力、默认关闭、显式启用参数和运行前审阅要求，并标明 `Stage Plan` 为旧流程遗留视图。六份关键正式产物、30 个最终提示词文件和 39 行事件账本保持不变，真实执行开关三个作用域均为空；全程未使用 `--apply-startup-cleanup`，零真实 Codex/模型调用，未修改 `detect_current_state.py`、`detect_current_stage.py`、manifest、schema、Skill、fork、真实工作区、正式产物或事件账本。

同日按用户逐道批准执行 ②b 渲染验收。闸门①把 `qc_reports` 受控加入批次声明，并以无密钥、无渲染开关的 `image-production --serve` 执行一次 `run: integrity`；事件由 39 增至 41，prompts-only 完整性报告为 pass、0 阻断/0 警告，未生成图片，提交为 `f35bde0`。闸门②只请求一次 `https://70api.top/v1/models`，HTTP 200，列表仅返回 `gpt-image-2`，据此确认 `OPENAI_BASE_URL=https://70api.top/v1`、`OPENAI_IMAGE_MODEL=gpt-image-2`；模型列表未公开尺寸或宽高比字段，精确 3:4 继续悬决。用户随后单独批准闸门③的一张图成本；现场复核 41 行事件、0 张成图、无并行服务和无持久化渲染变量后，以 `RENDER_MAX_IMAGES=1` 启动临时服务，并从原画布一次且仅一次提交 `run: renders`。事件于 14:46:18 记录 `step_started`，14:46:21 记录脱敏 `step_failed`：`main_01` 的 1024x1024 图生图请求携带一张 3,028,491-byte 白底参考图，按既有适配器调用 `/v1/images/edits`，中转站返回无法解析为标准 JSON 的 HTTP 403；执行器以“成功 0/计划 1（跳过 0）”安全中止，未写 `main_01.png` 或任何临时成图。失败后没有重试或第二条画布命令，临时服务立即停止，运行日志与事件中密钥匹配数为 0，三个作用域无持久化密钥/渲染变量；事件账本现为 43 行，renders/comfyui_jobs/repaired 仍为 0。画布终态为 29 节点、35 连线，路由保持 `needs_generated_images_before_qc`、可运行仍为 `renders`，运行台投影 403 失败。随后以不带鉴权、不带请求体的 `OPTIONS /v1/images/edits` 做单变量诊断：与生产适配器一致的 Python 默认客户端被 Cloudflare 返回 `403 text/plain`（17 bytes），仅增加浏览器式 `User-Agent` 后请求即穿过 Cloudflare，并由后端返回 `404 application/json`（114 bytes，OPTIONS 路由不存在属预期）。结合 `UrllibTransport` 当时没有显式 `User-Agent`、真实失败同为非 JSON 403，已确认本次 403 的直接根因是中转站/WAF 拒绝 Python 默认客户端标识；这不等于证明修正客户端标识后实际图片请求一定成功，后端模型权限、余额与参数仍需下一次经批准的真实请求验证。2026-07-16 用户另行批准离线 TDD 最小兼容性修复：裸 `OPENAI_BASE_URL` 现在只在 URL 路径为空时自动落到 `/v1`，`UrllibTransport` 增加固定且非敏感的 `Codex-Canvas-Bridge/1.0`，并保留调用方显式标识；新测试覆盖裸域名、已有 `/v1`、generations/edits、请求头大小写/唯一性和输入不变性，全仓 232 项测试通过。修复过程未读取密钥、未访问网络、未生成图片、未新增事件；再次执行闸门③仍须重新取得一张图成本批准，闸门④未获授权，也未执行 QC。

同日用户重新批准闸门③，仅允许 `main_01` 一张、`RENDER_MAX_IMAGES=1` 和一次 `run: renders`。现场从 43 行事件、0 张成图、无并行服务及无持久化渲染变量开始，使用裸基址 `https://70api.top` 与 `gpt-image-2` 启动临时服务；事件于 15:58:08 记录唯一 `step_started`，请求保持连接但在 16:08:55 以脱敏“`The read operation timed out`”记录 `step_failed`，总等待 647 秒。执行器报告“成功 0/计划 1（跳过 0）”，没有写出 `main_01.png` 或临时成图；失败后未重试、未提交第二条画布命令，服务与临时环境立即清除，事件现为 45 行，路由仍为 `needs_generated_images_before_qc`，renders/comfyui_jobs/repaired 仍为 0，QC 与闸门④均未执行。用户随后批准纯离线超时兼容修复：新增临时环境参数 `OPENAI_IMAGE_TIMEOUT_SECONDS`，未设置时保持 180 秒，只接受 30 至 1800 的整数，内部显式等待值优先；它表示连接或响应连续无新数据的等待上限，不是整次任务总时长。连接、正常响应读取、错误响应读取及 `URLError` 包装的超时现在统一为不含密钥、提示词或原始响应的中文失败，且永不自动重试。新增测试覆盖默认 180、临时 900、显式值优先、非法值联网前拒绝、三类超时脱敏及无输出/无重试；全仓 238 项测试通过。离线修复未读取密钥、未访问网络、未新增事件或图片；中转站是否已对第二次调用计费仍待用户侧查账，任何第三次真实调用必须重新批准。

同日用户确认忽略第二次调用的中转站后续处理，并批准按 `1+5+8` 三批生成全部 14 张，最多 14 次新请求、任一失败停止且不重试，本轮不执行 QC 或 repaired。成本发生前复核 HEAD `fb0cd3268aa7f501b0f15795c88ef554f28695a6`、238 项测试、45 行事件、0 张成图、0 个运行服务、0 个持久化渲染变量、原画布项目与 `main_01 1024x1024` 首任务均正确。第一批以 `OPENAI_IMAGE_TIMEOUT_SECONDS=900`、`RENDER_MAX_IMAGES=1` 从原画布提交唯一 `run: renders`；事件于 16:57:39 记录 `step_started`，16:59:30 记录 `step_succeeded`，受控结果为“成功 1/计划 1（跳过 0）（111.0s）”。正式文件 `main_01.png` 为有效非零 PNG、1,919,369 bytes，但 IHDR 实际尺寸是 `1254x1254`，不符合批准方案锁定的 `1024x1024`。因此该文件保留作为现场证据，但不计为通过尺寸验收；临时服务立即停止，密钥与渲染变量清除，未提交第二条或第三条画布命令，未生成其余 13 张，未裁剪、缩放、重试、执行 QC、ComfyUI 或 repaired。事件现为 47 行，renders=1、repaired=0、comfyui_jobs=0；技术路由因检测到任一 render 已转为 `needs_qc_reports`，但业务批次仍为 1/14 且未完成。

用户目验后确认 `main_01` 出图正常，接受供应端原生正方形像素尺寸，并批准继续剩余 13 张、参考成本约 0.78 美元；新验收口径为主图保持正方形、详情图保持约 2:3 竖版，不做裁剪、缩放或拉伸，任一失败立即停止且不重试。现场从 HEAD `cab1ef9e383be42db1a160c7993f36d1086a965a`、47 行事件、1 张 render、0 个运行服务和 0 个持久化渲染变量开始，通过一次遮罩输入把密钥只保留在本轮临时内存。第一段设置 `RENDER_MAX_IMAGES=5`，于 17:16:14 从原画布提交唯一 `retry: renders`；执行器跳过 `main_01`，成功写出 `main_02.png`（1,810,414 bytes，`1254x1254`），随后 `main_03` 请求收到无法解析的 HTTP 502。事件于 17:19:50 记录脱敏 `step_failed`：“成功 1/计划 5（跳过 1）”。按批准规则立即停止服务并清除密钥，没有重试 `main_03`，没有启动详情图批次，也未执行 QC、ComfyUI 或 repaired；`main_03` 无正式文件或半成品。终态事件 49 行，renders=2、repaired=0、comfyui_jobs=0，两个运行进程与三个作用域渲染变量均为 0；技术路由仍为 `needs_qc_reports`，业务批次为 2/14 且未完成。

用户随后批准以 `1+3+8` 三批续跑剩余 12 张，最多 12 次新请求、任一图片失败立即停止且不重试，本轮仍不执行 QC、ComfyUI 或 repaired。费用前从 HEAD `5cf9c63b00152ce4aa0772c2ac2be7238c899ef5` 复核 238 项测试、`git diff --check`、49 行事件、两张主图指纹、0 个运行服务、0 个持久化渲染变量、原画布项目和剩余任务顺序均通过；密钥通过遮罩窗口只保留在临时内存。第一批于 17:39:34 提交唯一 `retry: renders`，17:41:15 成功 1/计划 1（跳过 2，100.8 秒），写出 `main_03.png`（1,650,028 bytes，`1254x1254`）。第二批于 17:42:55 提交唯一 `retry: renders`，17:48:26 成功 3/计划 3（跳过 3，331.0 秒），写出 `main_04` 至 `main_06`；六张主图均为有效非零 `1254x1254` PNG，`main_01`、`main_02` 指纹未变。第三批于 17:49:41 提交唯一 `retry: renders`，写出 `detail_01.png`（1,731,927 bytes，`1086x1448`）后现场尺寸验收发现其为精确 3:4，与本轮批准的约 2:3（误差不超过 1%）相差 12.5%，因此于 17:51:30 记录脱敏 `step_failed`：“成功 1/计划 8（跳过 6）”并立即终止服务。`detail_02` 至 `detail_08` 无正式文件或半成品；由于批内执行器会在一张成功后立即进入下一项，终止前下一项是否已到达供应端无法由本地确认，但本地没有接收或写入其结果。临时服务、密钥工具和全部渲染变量已清除，事件终态 55 行，正式 renders=7（六张主图通过，`detail_01` 作为尺寸失败证据保留）、repaired=0、comfyui_jobs=0；技术路由仍为 `needs_qc_reports`，QC 未执行。

2026-07-17 用户明确接受供应端返回的 3:4，并把 `detail_01` 计为通过；随后批准按 `1+6` 续跑 `detail_02` 至 `detail_08`，最多七次新请求、第一张成功后才进入最后六张、任一失败停止且不重试，本轮仍不执行 QC、ComfyUI 或 repaired。费用前从 HEAD `bee4745e2ba7aebd12a073d4d936b23df18c355a` 复核 238 项测试、`git diff --check`、55 行事件、七张既有图片指纹、0 个运行服务、0 个持久化渲染变量、原画布项目和剩余任务顺序均通过；密钥通过遮罩窗口只保留在临时内存。第一批于 00:34:20 提交唯一 `retry: renders`，00:35:46 图片传输成功 1/计划 1（跳过 7，85.9 秒），写出 `detail_02.png`（2,053,793 bytes，`1024x1536`）。现场随即验收发现其为 2:3，与本轮批准的 3:4 相差 11.11%，因此停止第一批服务并于 00:36:39 追加脱敏业务失败记录；未启动 `RENDER_MAX_IMAGES=6` 的第二批，也没有第二条画布命令。`detail_03` 至 `detail_08` 无正式文件或半成品；临时服务、密钥工具和全部渲染变量已清除。终态事件 58 行，正式 renders=8（六张主图与 `detail_01` 通过，`detail_02` 作为尺寸失败证据保留）、repaired=0、comfyui_jobs=0；技术路由仍为 `needs_qc_reports`，QC 未执行。相同详情执行链连续返回 3:4 与 2:3，已证明供应端比例不稳定，任何继续方案必须重新确定是否同时接受两种竖版比例。

同日用户批准以“无损扩展画布”把八张详情图统一为精确 3:4，并批准最多六次新请求完成 `detail_03` 至 `detail_08`。费用前从 HEAD `2de8696bbfb37e38b82509aca225e98b8a084e7d` 复核 238 项测试、`git diff --check`、58 行事件、8 张既有图片指纹、0 个渲染/监听服务、0 个持久化渲染变量和原画布项目均通过。一次性本机 Pillow 工具先在临时副本验证 3:4 原图不动、2:3 扩展后原像素逐点一致、异常图片拒绝、原子失败不覆盖及无临时文件，再把 `detail_02` 的供应端原图备份到外部工作区 `artifacts/audit/render_originals/detail_02.png`，将正式图从 `1024x1536` 左右各扩展 64 像素为 `1152x1536`；六张主图与 `detail_01` 指纹未变。随后从原画布按六个独立临时服务各提交一次 `retry: renders`：`detail_03` 至 `detail_08` 全部成功，跳过数依次为 8 至 13，未发生失败或自动重试。其中 `detail_03`、`detail_04`、`detail_06`、`detail_07`、`detail_08` 供应端原图本身为 `1086x1448` 精确 3:4，保持原文件；`detail_05` 的 `1024x1536` 供应端原图先审计备份，再以相同方式无损扩展为 `1152x1536`。最终正式目录恰好 14 张：六张 `1254x1254` 主图与八张精确 3:4 详情图；扩展后的中央原图区域与审计原图逐像素一致，审计目录恰好保存 `detail_02`、`detail_05` 两张供应端原图。事件由 58 增至 70，repaired=0、comfyui_jobs=0，无临时文件；临时服务、密钥代理、密钥与渲染变量均已清除。真实路由为 `needs_qc_reports`，本轮停在正式 QC 之前。

同日用户批准阶段 B 方案并进入阶段 C/D，范围严格限定为离线 TDD，不运行真实 QC。提交 `634c58f` 新增 `codex_dev_qc.py` 并以最小分发接入 `codex_dev_executor.py`：首个回合前一次性核对 14 张正式 PNG 的精确名称、1:1/3:4 比例、正式提示词与白底图绑定、主/详情变量配置、3 张手持声明、QC Skill/运行规则/三份完整参考正文、`qc_report.schema.json` 合同以及 20 MiB 单批附件和 28 MiB 整体请求上限；运行协议固定为同一 thread 内 7 个两图批次加 1 个无附件全批总结。只有 U+FFFD 或明确 JSON 截断可同线程恢复，整次最多 2 次；合法 JSON 业务错误不重试。所有批次只在内存聚合，全部通过后才排他写入唯一 `qc_report.json`，本地固定 `adds_new_generation_direction=false`，既有完整性报告不覆盖。新增 22 项 QC 测试后全仓 260/260 通过，逐个 `py_compile` 通过，真实 manifest 只读计划预检为 14 张图、7 批、手持 `main_02/main_05/detail_02`。本轮没有启动 `--serve`、没有设置 `CODEX_DEV_ALLOW_REAL_EXECUTION`、没有访问网络或读取密钥、没有调用模型、没有写正式报告/事件/manifest/schema/Skill/fork/外部工作区；事件仍为 70 行，真实路由仍为 `needs_qc_reports`。真实 `run: qc` 验收是独立闸门，待用户另行批准。

同日随后完成真实 QC 独立闸门。阶段 A 从 `HEAD 51a5000` 只读复核 260 项测试、70 行事件、`needs_qc_reports` 路由、14 张成图名称/字节数、两份完整性报告、六份正式上游 SHA-256、14 资产/7 批/3 张手持计划、三作用域执行开关、唯一只读 `--watch` 及画布连接，全部通过；用户于 12:03:03 明确回复“执行，代写”。用户关闭只读投影后，临时 `codex-dev --serve` 只在自身进程设置 `CODEX_DEV_ALLOW_REAL_EXECUTION=1`，并从原项目运行台一次且仅一次代写 `run: qc`。事件于 12:06:06 记录 `step_started`，同一专用 thread 完成 7 个两图批次和第 8 段无附件总结，于 12:32:16 记录 `step_succeeded`，总耗时 1569.9 秒；未发生受控传输恢复、重试或第二条画布命令。正式 `qc_report.json` 为 56,371 bytes，SHA-256 为 `54ADB10B8D573E266EC24E65FC45A2E62DD50F05AF7547FD5B79BC06F5D6ED0D`，无 U+FFFD 或禁止字段，按序覆盖 14 张图；12 个固定项 × 14、3 个手持项和 4 个全批总结项合计恰好 175 条且无重复，`jsonschema` 校验通过。结果为 156 pass、18 needs_review、1 fail，共 19 个 issues（0 critical、0 major、2 minor、17 needs_review）与 19 个逐一引用有效 issue 的 repair_targets；报告固定 `adds_new_generation_direction=false`。两份完整性报告仍为 5,136 / 3,258 bytes，14 张图字节数与六份上游指纹全部不变；事件由 70 增至 72，路由收敛为 `ready`、missing/blocked 均为空。QC 阶段、报告产物、运行台和日志节点已投影成功；临时终端停止后发现唯一 Python `--serve` 子进程仍残留，按本轮完整命令行唯一识别后精确停止，最终 `WATCH_COUNT=0`、`SERVE_COUNT=0`，三个作用域的真实执行开关和 API key 均为空。`workflow_doctor.py` 以默认 `report_only_no_delete` 模式刷新状态与校验报告，无清理候选或删除动作。本轮未修改代码、schema、Skill、批次 manifest 内容、fork、成图、提示词或两份既有完整性报告，未执行 ComfyUI 或 repaired；QC 指出的返修路线待用户另行决定。

## 5. 代码地图

**主仓库（本仓库）**：

- `canvas-bridge/`——全部桥接逻辑，模块职责见 `canvas-bridge/README.md`（投影 projector、状态读取 state_reader、布局 layout_store、受控编辑 batch_editor、执行接入 run_controller、可替换执行器契约/注册表/组合入口、demo、GPT Image 2、生产任务组装与 `image-production` 组合执行器、`codex-dev` identity/style master/angle inventory/main/detail/final-prompts/qc 适配器、QC 专用离线校验与报告装配模块、驱动脚本 spike_canvas_push）。
- `manifests/workflow_graph.template.json`——工作流图模板（唯一图定义，schema 校验 + 与 route_batch 一致性测试）。
- `tests/test_canvas_*.py`、`tests/test_batch_editor.py`、`tests/test_run_controller.py`、`tests/test_workflow_graph_projection.py`、`tests/test_codex_dev_executor.py`、`tests/test_codex_dev_downstream.py`、`tests/test_codex_dev_qc.py`、`tests/test_final_prompt_integrity_prompts_only.py`、`tests/test_render_task_assembler.py`、`tests/test_image_production_executor.py`——画布子项目测试（当前含在全仓库 260 个测试内，运行 `python -m unittest discover -s tests`）。

**fork 仓库（独立 Git 仓库，不在本仓库内）**：

- 位置：`D:\dev\infinite-canvas`，分支 `workflow-editor`，当前 @ 91e40d04，上游基线 ebd8ae2（2026-07-09 origin/main）。
- **改动登记册：`FORK_NOTES.md`（fork 仓库根目录）**——列出全部锚点（截至 2026-07-14 共 8 个，包含 canvas-agent 可选模型、真实 turn status、无新增依赖的测试入口和 Codex stdout 连续 UTF-8 解码）。动 fork 前必读，同步上游后逐条复核。
- 锚点 #5–#8 及其配套 CHANGELOG、测试登记与 `web/bun.lock`（bun install 副产物）已随 `91e40d04` 提交；fork 工作区在本次存档后应保持干净。

**演示工作区（可丢弃）**：`D:\dev\canvas-demo-workspace`，由 `canvas-bridge/make_demo_workspace.py` 管理（`--init/--add-inputs/--advance <步骤>/--reset`），带 `.canvas_demo` 安全标记，绝不写仓库。工作区文件仍保留，但 `demo_live` 的 29 个画布演示节点已于 2026-07-12 清理。

**首个真实批次工作区（不可按演示数据清理）**：`D:\onedrive\OneDrive\Desktop\杯类\shuiping_20260712`；仓库事实入口为 `manifests/shuiping_20260712.batch_manifest.json`。工作区建批时的旧副本现保留为 `manifests/batch_manifest.initial-snapshot-20260712.json`，仅作初始快照，不是事实入口，也不再承担标准 `batch_manifest.json` 工作区识别标记；任何受控清理仍须从仓库权威 manifest 出发。原始白底图仍保留在 `D:\onedrive\OneDrive\Desktop\shuiping`，工作区使用经哈希核验的副本。

## 6. 运行时手册

**日常服务 3 个；按需服务 2 个**：

| 服务 | 端口/形态 | 启动方式 |
|---|---|---|
| canvas-agent | :17371 | `bun run --cwd D:/dev/infinite-canvas/canvas-agent dev` |
| 画布网页 | :3000 | `bun run --cwd D:/dev/infinite-canvas/web dev` |
| 真实批次只读投影 | 常驻 cmd 窗口（标题"真实批次只读投影服务*"） | `启动画布.bat` 专用分支固定运行 `python canvas-bridge/spike_canvas_push.py --watch manifests/shuiping_20260712.batch_manifest.json --layout-path manifests/shuiping_20260712.canvas_layout.json --interval 2` |
| 批次运行台（按批准临时启动） | 临时 cmd 窗口 | `python canvas-bridge/spike_canvas_push.py --serve <approved-manifest> --layout-path <approved-layout> --executor <approved-executor> --interval 2` |
| 静态图片（按需） | :8801（仅图片演示需要） | `python -m http.server`（临时目录） |

- **日常入口：仓库根目录 `启动画布.bat`**。双击后先确认或启动 agent + web；若有新服务则等待约 10 秒，随后打开 Chrome 到 `http://localhost:3000/canvas/hPbkNXg3WA0p2i46VOh3s`（“无限画布 1”项目直达；若更换工作画布，必须同步修改本行与启动器地址），最后按窗口标题防重复启动真实批次只读投影。投影专用分支先等待页面 3 秒；若画布尚未连接或运行中断导致 `--watch` 非正常退出，则每 5 秒重试同一个固定命令，正常停止不重试。其中 `--watch` 对批次事实只读，只向画布同步投影；画布仍会显示运行台节点，但其中的 `run:` / `retry:` 命令不会被读取或执行。真正的 `--serve` 批次运行台不再随日常入口启动，只能在用户再次明确批准后按批准的 manifest、布局和执行器临时启动。该文件**有意不入 Git**（含本机绝对路径）；迁移机器时需重建。
- 连接凭据：`%USERPROFILE%\.infinite-canvas\canvas-agent.json`（url + token）。**token 不随 agent 重启轮换**（文件存在即沿用）。
- 用户（非程序员）的工作画布：Chrome 里"无限画布 1"（id hPbkNXg3WA0p2i46VOh3s，localhost:3000）；其浏览器 localStorage 已存 token，刷新自动重连。

**连接机制要点（2026-07-11 实证）**：

- agent 重启后，已打开的画布页**不会自动重试连接**；页面加载时若 localStorage 有 token 才自动连（`web/src/components/layout/app-top-nav.tsx` 连接逻辑）。
- `?agentUrl=...&agentToken=...` URL 参数**不会自动触发连接**（上游 `autoConnect` prop 无调用方；README 旧说法描述的是 Codex 插件构建），但用户点一次"连接"按钮时 URL 参数里的 token 优先级最高，且参数存在会自动关闭"工具确认"开关。
- agent 的 `/config` 发现端点不回传 token（只有 hasToken），所以首次连接要么 URL 带参，要么手动粘贴。
- **无头替身画布配方**（页面断连且无法触碰用户浏览器时）：`chrome --headless=new --user-data-dir=<临时目录> --remote-debugging-port=9222 "http://localhost:3000/canvas?mode=new&agentUrl=...&agentToken=..."` → 用 bun 写 CDP WebSocket 客户端执行 `Runtime.evaluate` 种 localStorage 三键（`canvas-agent-url` / `canvas-agent-token` / `canvas-agent-confirm-tools=0`）→ `location.reload()` → 自动连接。

**已知坑（每条都踩过）**：

1. 桥接写画布后立即读会拿到 agent 约 3 秒的旧缓存——**读前 sleep 3**；`--serve` 里已用 consumed_content 守卫防止命令被缓存回声重复消费。
2. PowerShell 跑含中文的 Python 前设 `$env:PYTHONUTF8="1"`。
3. `--apply-edits` 成功后自动重投影，**必须带 `--layout-path`**，否则布局回退到默认分层排布。
4. `--get-state` 输出是汇总字段（nodes_total/nodes_mine/viewport），不是节点数组。
5. 杀进程按端口找 owner（`Get-NetTCPConnection`），别用会自匹配的命令行字符串模式。
6. 推送超时且 agent 报待确认 = 画布页"工具确认"开关开着（锚点③使关闭状态持久，但新浏览器档案默认开启）。
7. 若 `--serve` 启动时画布未连接，会打印 waiting_canvas 并重试初始投影，属正常。

## 7. 阶段 4 使用说明（当前功能面）

- 画布上"▶ 批次运行台"节点写一行命令：`run: next`（执行下一步）/ `run: <步骤>` / `retry: <已完成步骤>`；步骤词汇 = `identity, style_master, angle_inventory, main_vc, detail_vc, final_prompts, integrity, renders, qc`。
- 命令过三段门禁：动词白名单解析 → 按真实 `route_batch()` 判定可运行/可重试（含脱梯段逻辑：integrity 门禁通过才放行 renders）→ 注册执行器子进程执行。
- 执行历史事实来源：`<manifest 目录>/<pid>.events.jsonl` 追加式日志；画布"📜 执行日志"节点只是其投影。
- 日常启动器现只运行真实批次 `--watch`，不创建执行器，也不开放任何画布运行命令。阶段 4 的 `--serve` 接口仍可按批准临时使用；若手工启动时省略 `--executor`，CLI 默认仍为 **demo 执行器**（驱动演示工作区 `--advance`，有安全标记保护），因此真实执行前必须显式核准 manifest、布局和执行器。执行层使用 `executor_contract.py` + `executor_registry.py` + `executor_factory.py` 的可替换边界；`codex-dev` 已完成 identity、style master、angle inventory、`main_vc`、`detail_vc`、`final_prompts` 与 `qc` 的 `shuiping_20260712` 真实验收。`image-production` 已完成 prompts-only 门禁、14 项任务组装与真实断点续跑；正式目录现有六张正方形主图和八张精确 3:4 详情图，唯一 `qc_report.json` 已生成，事件 72 行，真实路由为 `ready`。任何 QC 重做、ComfyUI、repaired 或图片追加/覆盖仍必须单独批准；默认 ComfyUI 模式、默认 demo 执行器和现有 `openai-image` 适配器行为不变。

## 8. 后续路线图（候选，未排期）

1. **②b 14 张正式成图与真实 QC 已经完成**：`main_01` 至 `main_06` 六张主图均为有效 `1254x1254` PNG；`detail_01` 至 `detail_08` 八张详情图均为有效且精确 3:4 的 PNG。真实 QC 后事件为 72 行，正式目录仍恰好 14 个文件且字节数不变，唯一 `qc_report.json` 已通过 175 条覆盖与 schema 校验，真实路由为 `ready`。
2. **②b 详情图比例决策已经闭环**：继续保留现有 `1024x1536` 请求映射，不裁剪、不缩放、不拉伸主体。供应端原图已是 3:4 时保持原文件；返回其他竖版比例时先保存供应端原图，再只扩展不足方向的柔和虚化背景。当前实际扩展的是 `detail_02` 与 `detail_05`，两张原图均保存在外部工作区 `artifacts/audit/render_originals`，中央原图区域逐像素一致。
3. **下一闸门是返修路线决策，当前继续冻结**：真实 QC 报告含 2 个 minor、17 个 needs_review 和 19 个 repair_targets，无 critical 或 major。不得提交 `retry: qc`、`run: renders`/`retry: renders`，不得追加或重生成图片，也不得执行 ComfyUI 或 repaired；manifest、schema、Skill、fork、提示词、尺寸映射和正式上游产物均保持不变。是否人工复核、返修哪些图片及采用哪一返回阶段，待用户另行决定并批准。
4. **模型 API 执行器**：为 identity/style/angle/vc 等非生图步骤及未来中央化 QC 增加独立的文本/视觉模型适配器；不得把这些业务步骤写死到 Codex。现有 `codex-dev / qc` 只作为可选开发适配器，不改变中央后台方向。
5. **中央后台**：把当前本机 `--serve` 逐步迁移为公司统一服务，包括任务队列、用户权限、中央存储、密钥管理和实时状态；同事最终只使用浏览器画布。
6. fork 上游同步演练（锁 tag、合并后逐条复核 FORK_NOTES.md + 跑全仓测试 + 桥接冒烟）。
7. ✅ **已完成：`workflow_doctor` 启动清理改为显式启用**：默认运行仅刷新校验报告与 `current_state`，不生成清理候选、不移入回收站；仅显式 `--apply-startup-cleanup` 时启用清理，兼容 `--skip-startup-cleanup`。操作手册已补充删除能力、无确认弹窗与运行前审阅要求。

## 9. 维护协议（交接纪律）

1. **改动画布子项目状态的会话，结束前必须更新本文件**（进度台账、代码地图、坑清单、路线图相应条目），并与代码同一提交或紧随提交。
2. 动 fork 前读 `FORK_NOTES.md`；新增锚点必须当场登记；能放 canvas-bridge 的逻辑不进 fork。
3. 验收标准：`python -m unittest discover -s tests` 全绿 + 桥接冒烟（`--health`、`--push-live`）+ 涉及交互时的现场验证。
4. 提交风格沿用 git log 现状（`feat:` / `fix:` / `docs:` 前缀，一里程碑一提交）。
5. 不确定的历史结论先查本文件与 `docs/CANVAS_SPIKE_REPORT.md`、`canvas-bridge/README.md`、fork `FORK_NOTES.md`、git log，**不要凭聊天记忆推断，不要重新分析已定决策**。
