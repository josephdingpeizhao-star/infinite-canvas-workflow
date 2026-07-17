# Canvas Bridge (Spike)

把主仓库的工作流图模板与批次 manifest 投影到 infinite-canvas 画布的桥接层原型。

## 边界（硬性约束）

- 仓库文件（manifest、schema、规则、报告）是唯一事实来源；画布只是投影目标。
- 本目录代码只读取仓库文件，除非用户显式要求，否则不写入仓库任何文件。
- 不修改 infinite-canvas 源码；只通过 canvas-agent 的本地 HTTP 协议（`/api/tools`）通信。
- 纯 Python 标准库，与 `scripts/` 保持一致，不引入第三方依赖。

## 模块

- `projector.py`：纯函数投影。图模板 + 批次 manifest →`canvas_apply_ops` 操作列表（分层布局、`wf:<product_id>:` 前缀 id、先删后建幂等）；`node_runtime_view` 从 `route_batch()` 结果推导节点状态（产物在=success、下一步=▶、阻塞=error+原因、门禁镜像完整性报告）；`runtime_update_ops` 生成增量更新。
- `state_reader.py`：镜像 `detect_current_state.inspect_batch()`，支持任意路径的批次 manifest；附完整性报告定位。
- `layout_store.py`：布局持久化（阶段2）。按图节点 id 记录位置/尺寸/视口，默认存 `manifests/<product_id>.canvas_layout.json`（可 Git diff），schema 见 `schemas/canvas_layout.schema.json`。布局是纯 UI 状态，不影响执行依赖。
- `batch_editor.py`：受控编辑（阶段3）。画布上的 `wfedit:<pid>:batch` 配置节点回读后过三段门禁（白名单解析→字段校验→改后 manifest 干跑 `route_batch()`），全部通过才原子写回；白名单仅 `requested_outputs`/`notes`，拓扑只读。
- `run_controller.py`：执行接入（阶段4）。画布新增 `wfrun:<pid>:batch` 运行台（写 `run: next` / `run: <步骤>` / `retry: <已完成步骤>`）与 `wflog:<pid>:events` 日志投影；命令过三段门禁（动词白名单解析→按真实 `route_batch()` 判定可运行/可重试→统一执行器接口执行），事件追加写 `<pid>.events.jsonl`（执行历史的事实来源）。
- `executor_contract.py` / `executor_registry.py`：可替换执行器边界。上层只认识 `ExecutionRequest` / `ExecutionResult` 和执行器名称，不认识供应商 API、模型或 SDK。
- `executor_factory.py`：组合入口，显式注册具体适配器；新增供应商不修改画布命令和门禁。
- `demo_executor.py`：现有演示工作区的兼容适配器。
- `openai_image_executor.py`：GPT Image 2 适配器（默认模型 `gpt-image-2`），纯标准库 HTTP；无参考图走 `/v1/images/generations`，有参考图走 `/v1/images/edits`。HTTP 传输可注入，自动测试不访问真实网络。
- `render_task_assembler.py`：从 `final_prompt_index.json` 按原顺序组装供应商无关的图片任务。整批先核对提示词、唯一白底参考图和输出边界；主图映射 `1024x1024`，详情图暂映射 `1024x1536`；已有同名 PNG 自动跳过，便于安全续跑。
- `image_production_executor.py`：生产图片组合执行器 `image-production`，只接受 `integrity` 与 `renders`。前者运行 prompts-only 确定性门禁，后者在双开关通过后复用既有 `openai-image` 逐张执行；任一张失败即停止，已成功图片保留，错误不回显密钥或提示词正文。
- `codex_dev_executor.py`：可选开发适配器 `codex-dev`。当前接受 `identity`、`style_master`、`angle_inventory`、`main_vc`、`detail_vc`、`final_prompts` 与 `qc`，通过 canvas-agent 现有 Codex 新线程 + HTTP/SSE 能力取得结构化结果；其他步骤在任何传输或文件访问前拒绝。前三步保持原有身份、风格和单品 A/B/C/D 角度边界。变量配置与最终提示词只读取已验收结构化上游档案：主图固定 6 套、1:1、2 套手持；详情固定 8 套、模块01至模块08、3:4、1 套手持，模块05只允许标注高度约 25 厘米且不得手持；`detail_vc` 在同一专用 thread 内按两项一段返回四段，U+FFFD 或可确认的截断 JSON 前缀可整段重发，整次执行最多恢复 2 次；只有第 1 段的配置与公共约束已通过业务门禁、但顶层 `notes` 不是字符串时，最多允许 1 次同线程完整格式纠正，不在本地搬移字段或复用失败正文；四段全部通过后才在内存重组并运行原完整校验。最终提示词用两个独立 thread 分别编译 6+8 套，全部通过后才一次性写入 14 份 JSON/Markdown 和索引，不生成 ComfyUI 作业、QC 或图片。最终提示词编译指令逐编号写明手持与绑定字面契约（与校验器一致）。所有下游步骤只允许合格 A/B/C，拒绝缺失 D、被拒源图、容量/其他尺寸/重量/具体材质/耐热/认证/品牌型号等未确认事实，并由本地适配器固定产品编号、正式路径、编号和哈希。该模块同时封装开发模型选择（当前经现场诊断验证为 `gpt-5.5`）、Codex 附件、同线程分批（单批附件载荷上限 20 MiB）、完整 JSON 请求体上限（默认 28 MiB）、SSE、真实 turn status、专用 thread 结果回读、空回复拒绝、返回校验和脱敏错误，不向运行台暴露 Codex 细节。
- `codex_dev_qc.py`：`codex-dev / qc` 的纯标准库业务模块。它在首个传输前一次性核对 manifest 路径边界、14 张 PNG 名称与 1:1/3:4 比例、14 份最终提示词绑定、3 张正式手持声明、白底参考图格式、QC Skill + 运行规则 + 三份完整参考正文、`qc_report.schema.json` 合同以及 20/28 MiB 请求上限；随后固定为 7 个两图批次加 1 个不带附件的全批总结，全部在同一 thread 内完成。只有 U+FFFD 或明确 JSON 截断可同线程重发，全局最多 2 次；合法 JSON 业务错误不重试。八批全部通过后才以排他原子方式只写 `qc_report.json`，永不覆盖既有报告，也不改动同目录完整性报告；`adds_new_generation_direction` 由本地固定为 `false`。
- `ic_client.py`：canvas-agent HTTP 客户端。从 `~/.infinite-canvas/canvas-agent.json` 读取 url/token。
- `make_demo_workspace.py`：演示用外部工作区脚手架（默认 `D:/dev/canvas-demo-workspace`，带安全标记，绝不写仓库）。
- `spike_canvas_push.py`：驱动脚本，见 `--help`。`--serve` 正常仍一次提交完整初始投影；若网页端对整批投影超时，则保持原操作顺序按小批次回退，避免运行台停在只完成部分节点的旧状态。

## 前置条件

1. canvas-agent 在本机运行（默认 `http://127.0.0.1:17371`）。
2. infinite-canvas web 在本机运行（默认 `http://localhost:3000`）。
3. 浏览器已打开某个画布页，并带 `?agentUrl=...&agentToken=...` 参数完成自动连接。

## 典型用法

```powershell
python canvas-bridge/spike_canvas_push.py --health
python canvas-bridge/spike_canvas_push.py --push-live <批次manifest> [--layout-path P] [--restore-viewport]
python canvas-bridge/spike_canvas_push.py --watch <批次manifest> --interval 2 [--layout-path P]
python canvas-bridge/spike_canvas_push.py --apply-edits <批次manifest> [--layout-path P]
python canvas-bridge/spike_canvas_push.py --serve <批次manifest> [--layout-path P] [--interval 2] [--executor demo]
python canvas-bridge/spike_canvas_push.py --layout-save <批次manifest> [--layout-path P]
python canvas-bridge/spike_canvas_push.py --status-demo
python canvas-bridge/spike_canvas_push.py --image-url http://127.0.0.1:8801/spike.svg
python canvas-bridge/spike_canvas_push.py --clear-mine

# 演示工作区（逐阶段点亮）
python canvas-bridge/make_demo_workspace.py --init
python canvas-bridge/make_demo_workspace.py --add-inputs
python canvas-bridge/make_demo_workspace.py --advance identity   # ...直到 qc
python canvas-bridge/make_demo_workspace.py --reset
```

## 可替换执行器边界

- 业务路由负责判断“现在能不能运行”；适配器只负责“如何执行”，不得绕过门禁。
- `--serve` 的默认执行器仍是 `demo`。`codex-dev` 已注册为可选开发适配器，支持 `identity`、`style_master`、`angle_inventory`、`main_vc`、`detail_vc`、只产出提示词的 `final_prompts` 与只产出结构化报告的 `qc`；`image-production` 已注册为生产图片组合入口，内部复用 `openai-image`，不替换也不改变现有三个适配器的行为。
- `codex-dev` 复用 canvas-agent 的 `/agent/codex/threads/new`、`/agent/codex/turn`、`/events` 与 thread 读取接口，Python 不启动另一套 Codex 会话。新线程请求可在适配器内部选择模型；上层业务仍只认识 `codex-dev` 名称和统一契约。SSE 只作为完成通知，`failed` / `interrupted` 会先收敛为脱敏失败，只有 `completed` 才从本次专用 thread 重新读取最终文本，避免把共享事件流中其他线程的消息当成本次结果。identity、style master 和 angle inventory 的既有行为不变；main/detail 变量配置只在对应正式上游存在后运行，最终提示词只在两类变量配置均存在后运行，QC 只在 14 张正式渲染图与全部上游正式产物完整时运行。七类输出都必须位于 manifest 声明的 `artifacts_root` 范围内；越界产物、异常格式、Unicode 损坏字符和不受支持事实均在写入前拒绝，已有档案不会被覆盖。最终提示词整包与 QC 报告都使用同目录临时文件和排他落盘；任一批失败不留下正式半成品。
- `codex-dev` 的自动测试使用假传输和临时工作区，不启动真实 Codex、不访问网络、不读取真实批次图片。2026-07-15 完成本次详情配置校验器的类级修复后，全仓 180 项测试通过；既有四段覆盖、同线程续传、传输恢复与包装纠正上限、业务错误不重试、完整八项手持数量及汇总校验、失败零正式文件等行为保持不变。canvas-agent 继续使用连续 UTF-8 解码，避免中文字符跨数据块时被替换为 U+FFFD；fork 侧既有 3 项测试与 TypeScript 编译结果不受本次主仓库修改影响。
- 下游未确认事实门禁现在把数值精确等于用户确认高度、单位为厘米或 `cm`（不区分大小写）的复述默认视为合法，并由 detail 分段、detail 整包、main 整包和 `final_prompts` 批次共享；同一子句或字段路径含竞争维度、区间/连字、负号、单位扩展或相邻乘号尺寸组时仍拒绝，其他既有单位及非确认高度的厘米值也仍拒绝。材质/认证扫描只额外保护结构完整的“不把/不将……写死/固定/标注/设定/锁定/指定为/成……”否定指令，且保护范围只覆盖“为/成”后的目标列表；结构中夹带或另起的正向事实仍拒绝，既有保护词行为不变，也不会因“不锈钢”中的“不”或一般“不是……”而放行。一次业务门禁会先收集本次输入内全部未确认参数与商品事实，再用不超过 200 字符的“类别 + 净化字段路径 + 计数”统一报错；未知键名改用稳定占位符，不回显原值、数值上下文或提示词正文。段号、结构、模块、角度、比例和手持等其他校验仍保持原来的逐项立即失败。
- 真实执行默认关闭；只有获得用户明确批准后，才可在该次服务进程中临时设置 `CODEX_DEV_ALLOW_REAL_EXECUTION=1`，该开关不写配置、不持久化。既有 identity/style/angle/main/detail 真实验收历史、`ExecutionRequest / ExecutionResult`、三段门禁、默认 `demo`、`openai-image`、其他 `codex-dev` 阶段和产物格式均未改变。首次 `final_prompts` 于 2026-07-15 18:10:24 至 18:12:37 安全失败；离线修复并重新取得明确批准后，第二次执行已于 20:05:38 至 20:09:24 成功，正式目录现有 14 份 JSON、14 份 Markdown 和两份索引。后续 integrity、renders 与 QC 均已按独立闸门完成，任何重做或返修仍须另行批准。
- canvas-agent token 只从本机配置读取并放在鉴权请求头中；`codex-dev` 只接受 `http://127.0.0.1`、`http://localhost` 或 `http://[::1]` 回环地址，并显式禁用系统代理。事件日志只记录通用成功说明或彻底切断原始异常链的脱敏错误，不记录 token、完整提示词、Codex 原始错误正文或产品图片内容。
- GPT Image 2 密钥只从服务端环境变量 `OPENAI_API_KEY` 读取；可选 `OPENAI_IMAGE_MODEL` 和 `OPENAI_BASE_URL` 只属于该适配器。`OPENAI_BASE_URL` 可填写已带 `/v1` 的 API 根路径，也可填写裸域名（自动补为 `/v1`）；生产 HTTP 传输使用固定且不含敏感信息的 `Codex-Canvas-Bridge/1.0` 客户端标识，并保留调用方显式传入的标识。密钥不得写入 manifest、画布节点、事件日志或仓库文件。
- 可选 `OPENAI_IMAGE_TIMEOUT_SECONDS` 只接受 `30` 至 `1800` 的整数；未设置时保持默认 `180` 秒，内部显式传入的等待值优先。该值表示连接或响应连续无新数据时的等待上限，不是整次任务的总时长；非法值在联网前拒绝。闸门执行可在获批的临时进程中设为 `900`，不得持久化。
- `shuiping_20260712` 的 14 张正式图片已按用户批准完成；真实 QC 后名称与字节数复核不变。任何追加、覆盖或重新生成仍必须重新批准真实 API 成本。
- 2026-07-17 已在提交 `634c58f` 完成 `codex-dev / qc` 离线 TDD：新增 22 项 QC 专项测试，全仓 260 项测试通过；真实 manifest 的只读计划预检确认 14 张图、7 个两图批次与 `main_02/main_05/detail_02` 三张手持图，最大估算附件负载低于 20 MiB。随后在用户明确批准“执行，代写”后，从原画布一次且仅一次提交 `run: qc`：事件于 12:06:06 开始、12:32:16 成功，耗时 1569.9 秒，无重试或第二条画布命令。唯一 `qc_report.json` 为 56,371 bytes，SHA-256 为 `54ADB10B8D573E266EC24E65FC45A2E62DD50F05AF7547FD5B79BC06F5D6ED0D`；14 张图、175 条检查、19 个 issues 和 19 个 repair_targets 均通过结构与 `jsonschema` 校验。事件现为 72 行，路由为 `ready`；临时服务与执行开关已清除，ComfyUI 和 repaired 未执行。
- 未来接入其他图片服务时，实现相同的 `execute(ExecutionRequest) -> ExecutionResult` 契约并在 `executor_factory.py` 注册即可，上层画布逻辑不变。

## 生产图片执行链（已实现，真实 QC 已完成）

1. `image-production / integrity` 调用既有校验脚本的 `--prompts-only` 模式，只检查 6+8 数量与顺序、既有 Schema、来源文件与逐项解析指纹、手持数量、1:1/3:4 字面、高度约 25 厘米语义和 UTF-8/Unicode 完整性。它不读取 ComfyUI 作业清单，并在 JSON/Markdown 报告中逐项记录跳过旧内容启发式扫描与旧编译器字面扫描的原因；默认 ComfyUI 模式未改变。
2. 门禁通过后，`image-production / renders` 从索引读取 14 项，逐项绑定 manifest 白底图目录中唯一同名参考图，并把 `final_prompt` 原文与 `negative_prompt` 原文用固定分隔符组合；不改写正向正文。
3. 真实传输前必须同时满足 `RENDER_ALLOW_REAL_EXECUTION=1` 与非空 `OPENAI_API_KEY`。可选 `RENDER_MAX_IMAGES` 只执行前 N 个尚缺图片；已有 `<config_id>.png` 自动跳过。第三张失败时前两张保留，下一次从缺口继续，不覆盖已有图片。连接、正常响应读取或错误响应读取超时均统一为不含密钥、提示词和原始响应的中文失败；超时永不自动重试，也不留下半成品。
4. 主图请求固定 `1024x1024`，详情图请求继续使用既有 `1024x1536` 映射。供应端实际可能返回 3:4 或 2:3；本批最终统一要求精确 3:4。供应端原图已是 3:4 时保持不变；其他竖版比例先备份供应端原图，再通过扩展不足方向的柔和虚化背景统一为 3:4，原商品与文字区域不裁剪、不缩放、不拉伸。当前 `detail_02` 与 `detail_05` 从 `1024x1536` 左右各扩展 64 像素为 `1152x1536`，供应端原图保存在外部工作区 `artifacts/audit/render_originals`；其余详情图保持供应端原文件。
5. `shuiping_20260712` 已完成 prompts-only 门禁、模型探测、全部真实出图和真实 QC。六张主图均为有效 `1254x1254` PNG；八张详情图均为有效且精确 3:4 的 PNG。正式 renders 恰好 14 个文件，QC 后字节数不变；事件账本 72 行，唯一 `qc_report.json` 已生成，真实路由为 `ready`。ComfyUI 作业与 repaired 均为 0；返修决策见 `docs/CANVAS_PROJECT_STATE.md` §8。

## 状态

阶段 1（只读实时投影）、阶段 2（布局持久化）、阶段 3（受控编辑，`--apply-edits`）、阶段 4（执行接入，`--serve` 运行台）均已跑通并有测试与现场验证。`codex-dev` 的 identity、style master、angle inventory、`main_vc`、`detail_vc`、`final_prompts` 和 `qc` 均已完成 `shuiping_20260712` 真实验收。正式目录包含 14 份最终提示词 JSON、14 份同名 Markdown 和两份索引；生产图片链已完成 prompts-only 门禁、14 项任务组装、裸 API 基址兼容、固定客户端标识、可配置无数据等待上限与真实断点续跑，当前全仓 260 项测试通过。正式 renders 恰好 14 张：六张正方形主图、八张精确 3:4 详情图；真实 QC 报告按序覆盖 14 张与 175 条检查，事件 72 行，路由 `ready`。ComfyUI 作业与 repaired 均为 0；19 个 issues 与 19 个 repair_targets 的后续处置待用户另行决定。真实 QC 与用户人工终审均已完成，19 条问题已按用户裁定处置，批次正式关账，路由保持 `ready`；QC 报告作为审计记录原样保留，任何图片追加、覆盖或重生成仍须另行批准。
