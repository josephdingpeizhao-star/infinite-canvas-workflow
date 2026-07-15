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
- `codex_dev_executor.py`：可选开发适配器 `codex-dev`。当前接受 `identity`、`style_master`、`angle_inventory`、`main_vc`、`detail_vc` 与 `final_prompts`，通过 canvas-agent 现有 Codex 新线程 + HTTP/SSE 能力取得结构化结果；其他步骤在任何传输或文件访问前拒绝。前三步保持原有身份、风格和单品 A/B/C/D 角度边界。后三步只读取已验收结构化上游档案，不重新读取白底图：主图固定 6 套、1:1、2 套手持；详情固定 8 套、模块01至模块08、3:4、1 套手持，模块05只允许标注高度约 25 厘米且不得手持；`detail_vc` 在同一专用 thread 内按两项一段返回四段，U+FFFD 或可确认的截断 JSON 前缀可整段重发，整次执行最多恢复 2 次；只有第 1 段的配置与公共约束已通过业务门禁、但顶层 `notes` 不是字符串时，最多允许 1 次同线程完整格式纠正，不在本地搬移字段或复用失败正文；四段全部通过后才在内存重组并运行原完整校验。最终提示词用两个独立 thread 分别编译 6+8 套，全部通过后才一次性写入 14 份 JSON/Markdown 和索引，不生成 ComfyUI 作业、QC 或图片。最终提示词编译指令逐编号写明手持与绑定字面契约（与校验器一致）。所有下游步骤只允许合格 A/B/C，拒绝缺失 D、被拒源图、容量/其他尺寸/重量/具体材质/耐热/认证/品牌型号等未确认事实，并由本地适配器固定产品编号、正式路径、编号和哈希。该模块同时封装开发模型选择（当前经现场诊断验证为 `gpt-5.5`）、Codex 附件、同线程分批（单批附件载荷上限 20 MiB）、完整 JSON 请求体上限（默认 28 MiB）、SSE、真实 turn status、专用 thread 结果回读、空回复拒绝、返回校验和脱敏错误，不向运行台暴露 Codex 细节。
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
- `--serve` 的默认执行器仍是 `demo`。`codex-dev` 已注册为可选开发适配器，支持 `identity`、`style_master`、`angle_inventory`、`main_vc`、`detail_vc` 与只产出提示词的 `final_prompts`；`openai-image` 已完成供应商适配和离线测试，但运行台尚未从最终提示词产物组装 `ImageGenerationTask`，因此不能直接用于当前真实批次。
- `codex-dev` 复用 canvas-agent 的 `/agent/codex/threads/new`、`/agent/codex/turn`、`/events` 与 thread 读取接口，Python 不启动另一套 Codex 会话。新线程请求可在适配器内部选择模型；上层业务仍只认识 `codex-dev` 名称和统一契约。SSE 只作为完成通知，`failed` / `interrupted` 会先收敛为脱敏失败，只有 `completed` 才从本次专用 thread 重新读取最终文本，避免把共享事件流中其他线程的消息当成本次结果。identity、style master 和 angle inventory 的既有行为不变；main/detail 变量配置只在对应正式上游存在后运行，最终提示词只在两类变量配置均存在后运行。六类输出都必须位于 manifest 声明的 `artifacts_root` 范围内；越界产物、异常格式、Unicode 损坏字符和不受支持事实均在写入前拒绝，已有档案不会被覆盖。最终提示词整包使用同目录临时文件和排他落盘；任一批失败不留下正式半成品。
- `codex-dev` 的自动测试使用假传输和临时工作区，不启动真实 Codex、不访问网络、不读取真实批次图片。2026-07-15 完成本次详情配置校验器的类级修复后，全仓 180 项测试通过；既有四段覆盖、同线程续传、传输恢复与包装纠正上限、业务错误不重试、完整八项手持数量及汇总校验、失败零正式文件等行为保持不变。canvas-agent 继续使用连续 UTF-8 解码，避免中文字符跨数据块时被替换为 U+FFFD；fork 侧既有 3 项测试与 TypeScript 编译结果不受本次主仓库修改影响。
- 下游未确认事实门禁现在把数值精确等于用户确认高度、单位为厘米或 `cm`（不区分大小写）的复述默认视为合法，并由 detail 分段、detail 整包、main 整包和 `final_prompts` 批次共享；同一子句或字段路径含竞争维度、区间/连字、负号、单位扩展或相邻乘号尺寸组时仍拒绝，其他既有单位及非确认高度的厘米值也仍拒绝。材质/认证扫描只额外保护结构完整的“不把/不将……写死/固定/标注/设定/锁定/指定为/成……”否定指令，且保护范围只覆盖“为/成”后的目标列表；结构中夹带或另起的正向事实仍拒绝，既有保护词行为不变，也不会因“不锈钢”中的“不”或一般“不是……”而放行。一次业务门禁会先收集本次输入内全部未确认参数与商品事实，再用不超过 200 字符的“类别 + 净化字段路径 + 计数”统一报错；未知键名改用稳定占位符，不回显原值、数值上下文或提示词正文。段号、结构、模块、角度、比例和手持等其他校验仍保持原来的逐项立即失败。
- 真实执行默认关闭；只有获得用户明确批准后，才可在该次服务进程中临时设置 `CODEX_DEV_ALLOW_REAL_EXECUTION=1`，该开关不写配置、不持久化。既有 identity/style/angle/main/detail 真实验收历史、`ExecutionRequest / ExecutionResult`、三段门禁、默认 `demo`、`openai-image`、其他 `codex-dev` 阶段和产物格式均未改变。首次 `final_prompts` 真实验收于 2026-07-15 18:10:24 至 18:12:37 因脱敏“codex-dev 收到的主图最终提示词未保留手持状态”安全失败，本轮没有重试、第二条命令或继续失败 thread；正式提示词目录在包含隐藏项和临时项的核对后仍为 0，真实路由保持 `needs_final_prompts`。唯一临时真实服务已关闭，三个作用域的真实执行开关均为空，只读 `--watch` 已恢复；再次真实执行必须先完成离线只读诊断、形成新方案并重新取得用户明确批准。
- canvas-agent token 只从本机配置读取并放在鉴权请求头中；`codex-dev` 只接受 `http://127.0.0.1`、`http://localhost` 或 `http://[::1]` 回环地址，并显式禁用系统代理。事件日志只记录通用成功说明或彻底切断原始异常链的脱敏错误，不记录 token、完整提示词、Codex 原始错误正文或产品图片内容。
- GPT Image 2 密钥只从服务端环境变量 `OPENAI_API_KEY` 读取；可选 `OPENAI_IMAGE_MODEL` 和 `OPENAI_BASE_URL` 只属于该适配器。密钥不得写入 manifest、画布节点、事件日志或仓库文件。
- 当前状态报告仍禁止生成图片；在最终提示词完整性门禁通过并明确批准真实 API 消耗前，不得现场调用。
- 未来接入其他图片服务时，实现相同的 `execute(ExecutionRequest) -> ExecutionResult` 契约并在 `executor_factory.py` 注册即可，上层画布逻辑不变。

## 状态

阶段 1（只读实时投影）、阶段 2（布局持久化）、阶段 3（受控编辑，`--apply-edits`）、阶段 4（执行接入，`--serve` 运行台）均已跑通并有测试与现场验证。2026-07-12 已增加供应商无关的可替换执行器边界，并把 demo 迁移到统一接口；GPT Image 2 适配器仍仅完成离线测试。`codex-dev` 的 identity、style master、angle inventory、`main_vc` 和 `detail_vc` 均已完成 `shuiping_20260712` 真实批次现场验收。正式主图变量配置为 6 项、全部 1:1、恰好 2 项手持；正式详情变量配置为 8 项、全部 3:4、恰好 1 项手持，二者均只绑定合格 A/B/C，且详情模块05只标注已确认的高度约 25 厘米。首次 `final_prompts` 真实验收已安全失败，正式提示词及临时半成品仍为 0，真实路线保持 `needs_final_prompts`；`next_skill=final-prompt-compiler` 且无 blocker，但不得自动重试。画布对业务数据的写入仍仅经由阶段 3/4 的三段门禁白名单通道；执行历史以 `<pid>.events.jsonl` 追加日志为事实来源，ComfyUI 作业、QC 报告、renders、repaired 均为 0，继续禁止渲染与 QC。
