# Canvas Bridge (Spike)

把主仓库的工作流图模板与批次 manifest 投影到 infinite-canvas 画布的桥接层原型。

## 边界（硬性约束）

- 仓库文件（manifest、schema、规则、报告）是唯一事实来源；画布只是投影目标。
- 默认投影只读。只有用户在画布上明确触发既有受控动作时，才允许相应白名单写入：阶段 3 配置编辑、阶段 4 运行命令、M2-a 的“建批”，以及 M2-b 在真实费用确认后的规范目标声明。M2-a 只在全部门禁通过后创建一个新的仓库 manifest 与外部批次工作区；M2-b 只允许经既有 `batch_editor` 门禁把空的 `requested_outputs` 一次性声明为 `main, detail, final_prompts, qc_reports`，既有文件与不同的非空意图一律不覆盖。
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
- `workflow_demo_executor.py`：M1-b 独立的零成本演示执行器。只接受 `renders`，每轮在带 `.canvas_demo` 标记的工作区建立独立目录，原子写出 6 张 `720x720` 主图与 8 张 `720x960` 详情 PNG；每次建目录、临时写入和正式替换前都复核安全标记与解析后路径边界。取消发生在开始前时零目录，运行中断时保留已完成 PNG 且不留临时文件。
- `workflow_demo_projection.py`：把已经落盘的演示 PNG 逐张投影成普通图片节点并连回工作流机器，节点 id 固定使用 `wfdemo-output:<machine>:<run>:<index>`；只替换同一轮同一张，旧轮结果保留并向右避让。画布中的 data URI 只用于本地 demo 展示；M2-b 正式图片使用下面独立的 17373 哈希通道。
- `workflow_demo_service.py`：M1-b 常驻桥接服务。只消费 `workflow` 节点中处于 `queued` 的命令，不投影九工序、运行台或日志；命令复用动词白名单、批次路由与注册执行器三段门禁，按文件落盘顺序流式上桌，并用 demo 工作区内事件账本与单实例锁防止重复执行。服务离线、陈旧或非法命令都转成人话状态。
- `batch_intake_controller.py`：M2-a 信息卡门禁。只认 `batch-info` 节点的 `build: batch` 动作，按顺序核对请求编号与时间、七字段、信息卡和工作流的唯一连线、以及直接连给同一工作流的原始素材；派生图、缺少原始文件声明或连线不明确时都以人话拒绝。
- `batch_creator.py`：M2-a 建批事务。根据商品品类和本机日期生成中文批次号（如 `餐具_20260718`），用户不填写路径；先调用既有建批脚本做零写入预检，再在安全标记保护的临时目录中写入原图与回执，最终逐文件重算哈希，仓库 manifest 最后发布。同名批次、重复请求、越界路径和竞态写入都拒绝覆盖。
- `workflow_batch_intake_service.py`：M2-a 常驻服务与原图上传通道。控制消息仍从画布读取，原图字节单独经 `127.0.0.1:17372` 上传；必须复用现有 canvas-agent 令牌，只接受本机画布来源，按文件类型、文件头、大小和 SHA-256 校验。服务日志不记录令牌或图片内容，任何完整性不一致都进入不可重试的硬停止状态。单实例仍由不删除载体的一字节系统锁保证；持有者说明只有在取得系统锁后才覆盖，异常退出后下一实例可重新持锁并刷新说明。
- `workflow_production_controller.py`：M2-b 真实模式选择与门禁衔接。已登记信息卡和至少一张批次素材必须同时连到同一台机器；确认费用后才通过既有编辑门禁声明四项规范输出。每一步仍只由 `run_controller` 的命令解析、真实路由判定和统一执行三段门禁放行；本模块不另建生产续跑分支。
- `workflow_production_service.py`：M2-b 后台编排。顺序复用现役 `codex-dev` 六工序、`image-production / integrity` 和 `image-production / renders`，机器只显示人话进度，不投影九工序、运行台或日志。任一步失败即停且不自动重试；部分图片只能用既有 `retry: renders` 重新进三段门禁，14 张完成后停在 QC 前。
- `workflow_production_projection.py` / `workflow_production_render_observer.py`：正式 PNG 落盘后逐张上桌并连回机器；固定 `wfprod-output:<batch>:<config>` 节点保留旧成果并避让。主图只收正方形，详情只收精确 3:4；2:3 返回先原样保存到审计目录后停机，M2-b 不引入 Pillow、不自动扩边。
- `workflow_production_http_server.py`：固定 `127.0.0.1:17373` 的只读费用/正式 PNG 端点和风格补登上传入口。复用 canvas-agent 令牌，仅接受本机画布来源；正式图片返回 SHA-256，浏览器再次核验后转存 localforage，节点不依赖服务长期在线，也不使用 data URI。`GET /workbench-health` 只返回四名工人的状态与最后状态时间，健康为 200，关键工人停止或画布重连中为 503，不返回令牌、路径或异常原文。
- `workflow_style_reference_intake.py`：M2-b “画布补登 A”通道。用户把磁盘风格图直接连到已登记信息卡；全部浏览器声明、节点凭证、字节数、类型和 SHA-256 一致后，才把原字节写入批准的 `inputs/style_refs` 并新建独立回执。白底原图、资产清单和建批回执不改写；不一致时整次硬停止且不自动重试。
- `canvas_workbench_service.py`：日常“画布工作台”承载入口。在同一进程中运行 M1 demo、M2-a 建批、M2-b 真实制作和风格补登，并统一管理 17372/17373 两个回环监听。建批、真实制作、风格补登是关键工人，任一意外停止都会让整机停止并非零退出；demo 仍按既有隔离策略处理。脱敏状态只追加到受标记保护的 `canvas_workbench.events.jsonl`。M1 的 0 元演示行为保持不变；旧 demo 单服务入口继续保留作对照。
- `openai_image_executor.py`：GPT Image 2 适配器（默认模型 `gpt-image-2`），纯标准库 HTTP；无参考图走 `/v1/images/generations`，有参考图走 `/v1/images/edits`。HTTP 传输可注入，自动测试不访问真实网络。
- `render_task_assembler.py`：从 `final_prompt_index.json` 按原顺序组装供应商无关的图片任务。整批先核对提示词、唯一白底参考图和输出边界；主图映射 `1024x1024`，详情图暂映射 `1024x1536`；已有同名 PNG 自动跳过，便于安全续跑。
- `image_production_executor.py`：生产图片组合执行器 `image-production`，只接受 `integrity` 与 `renders`。前者运行 prompts-only 确定性门禁，后者在双开关通过后复用既有 `openai-image` 逐张执行；任一张失败即停止，已成功图片保留，错误不回显密钥或提示词正文。
- `codex_dev_executor.py`：可选开发适配器 `codex-dev`。当前接受 `identity`、`style_master`、`angle_inventory`、`main_vc`、`detail_vc`、`final_prompts` 与 `qc`，通过 canvas-agent 现有 Codex 新线程 + HTTP/SSE 能力取得结构化结果；其他步骤在任何传输或文件访问前拒绝。前三步保持原有身份、风格和单品 A/B/C/D 角度边界。变量配置与最终提示词只读取已验收结构化上游档案：主图固定 6 套、1:1、2 套手持；详情固定 8 套、模块01至模块08、3:4、1 套手持，模块05只允许标注当批已确认高度且不得手持；`detail_vc` 在同一专用 thread 内按两项一段返回四段，U+FFFD 或可确认的截断 JSON 前缀可整段重发，整次执行最多恢复 2 次；只有第 1 段的配置与公共约束已通过业务门禁、但顶层 `notes` 不是字符串时，最多允许 1 次同线程完整格式纠正，不在本地搬移字段或复用失败正文；四段全部通过后才在内存重组并运行原完整校验。最终提示词用两个独立 thread 分别编译 6+8 套，全部通过后才一次性写入 14 份 JSON/Markdown 和索引，不生成 ComfyUI 作业、QC 或图片。最终提示词编译指令逐编号写明手持与绑定字面契约（与校验器一致）。所有下游步骤只允许合格 A/B/C，拒绝缺失 D、被拒源图、容量/其他尺寸/重量/具体材质/耐热/认证/品牌型号等未确认事实，并由本地适配器固定产品编号、正式路径、编号和哈希。该模块同时封装开发模型选择（当前经现场诊断验证为 `gpt-5.5`）、Codex 附件、同线程分批（单批附件载荷上限 20 MiB）、完整 JSON 请求体上限（默认 28 MiB）、SSE、真实 turn status、专用 thread 结果回读、空回复拒绝、返回校验和脱敏错误，不向运行台暴露 Codex 细节。
- 新建批次以 manifest 顶层 `user_confirmed_facts` 为唯一权威用户确认入口，字段固定为 `product_type`、`height_cm`、`handheld_main=2`、`handheld_detail=1` 及三个布尔开关；商品品类只要求非空，不再锁死为水壶。旧 manifest 没有该对象时继续精确解析 `notes`；对象一旦存在就不回退，缺字段、多字段或类型错误均在执行前脱敏拒绝。允许清水为 `false` 时只允许空置；禁止倾倒与加热为 `true` 时拒绝正向动作描述，为 `false` 时不额外注入禁令但也不等于授权；D 缺失且“不补拍”为 `false` 时在传输前阻断，为 `true` 时仍只使用 A/B/C，不启用 D。
- `codex_dev_qc.py`：`codex-dev / qc` 的纯标准库业务模块。它在首个传输前一次性核对 manifest 路径边界、14 张 PNG 名称与 1:1/3:4 比例、14 份最终提示词绑定、3 张正式手持声明、白底参考图格式、QC Skill + 运行规则 + 三份完整参考正文、`qc_report.schema.json` 合同以及 20/28 MiB 请求上限；随后固定为 7 个两图批次加 1 个不带附件的全批总结，全部在同一 thread 内完成。只有 U+FFFD 或明确 JSON 截断可同线程重发，全局最多 2 次；合法 JSON 业务错误不重试。八批全部通过后才以排他原子方式只写 `qc_report.json`，永不覆盖既有报告，也不改动同目录完整性报告；`adds_new_generation_direction` 由本地固定为 `false`。
- `ic_client.py`：canvas-agent HTTP 客户端。从 `~/.infinite-canvas/canvas-agent.json` 读取 url/token。
- `make_demo_workspace.py`：演示用外部工作区脚手架（默认 `D:/dev/canvas-demo-workspace`，带安全标记，绝不写仓库）；M1-b 只新增 `--prepare-workflow-demo` 分支，补齐 demo 路由所需的最小档案且永不覆盖既有文件。
- `spike_canvas_push.py`：驱动脚本，见 `--help`。`--clear-projection <manifest>` 只删除指定批次当前活跃且在册的投影节点，并保护其他批次、未知同前缀节点和用户自建节点；`--serve` 正常仍一次提交完整初始投影，若网页端对整批投影超时，则保持原操作顺序按小批次回退，避免运行台停在只完成部分节点的旧状态。M1-b 另增 `--serve-workflow-demo` 与只供人工验收使用的 `--clear-workflow-demo <machine-id>`；后者只删除精确 `wfdemo-output:` 前缀结果，未接入启动器。

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
python canvas-bridge/spike_canvas_push.py --clear-projection <批次manifest>
python canvas-bridge/spike_canvas_push.py --clear-mine
python canvas-bridge/spike_canvas_push.py --serve-workflow-demo D:\dev\canvas-demo-workspace\manifests\batch_manifest.json --interval 2
python canvas-bridge/spike_canvas_push.py --serve-canvas-workbench D:\dev\canvas-demo-workspace\manifests\batch_manifest.json --interval 2
python canvas-bridge/spike_canvas_push.py --clear-workflow-demo <隔离画布机器id>  # 仅人工验收清理

# M2-a 隔离验收专用；测试根必须预先带 .canvas_intake_test_root 安全标记
python canvas-bridge/spike_canvas_push.py --serve-canvas-workbench D:\dev\canvas-demo-workspace\manifests\batch_manifest.json --batch-intake-test-root <隔离测试工作区> --interval 2

# 演示工作区（逐阶段点亮）
python canvas-bridge/make_demo_workspace.py --init
python canvas-bridge/make_demo_workspace.py --add-inputs
python canvas-bridge/make_demo_workspace.py --advance identity   # ...直到 qc
python canvas-bridge/make_demo_workspace.py --prepare-workflow-demo
python canvas-bridge/make_demo_workspace.py --reset

# 新批次只读预检；去掉 --dry-run 才会创建 manifest 与目录
python scripts/build_batch_manifest.py --product-id <批次编号> --product-type <商品品类> --height-cm <已确认高度> --handheld-main 2 --handheld-detail 1 --allow-clear-water true --forbid-pouring-and-heating true --missing-d-no-retake true --dry-run
```

## M2-a 信息卡与建批门禁

用户在画布上的信息卡直接填写商品品类、高度和三个行为开关；主图手持 2 张、详情手持 1 张继续只读显示。传给仓库的 `user_confirmed_facts` 仍是原有七字段，字段名、类型和 2+1 固定值一字不改。批次号、manifest 文件名、工作区目录与上传路由都按 UTF-8 往返，例如：

- 批次号：`餐具_20260718`
- 仓库事实入口：`manifests/餐具_20260718.batch_manifest.json`
- 外部工作区：`<既有批准父目录>/餐具_20260718/`

“建批”依次经过三道门：

1. **信息门**：七字段必须完整合法；信息卡只连一台工作流机器，且命令必须是本次新鲜的 `build: batch`。
2. **原图门**：至少一张原始素材直接连给同一台机器；文件名、类型、大小、来源声明和路径边界全部合法。浏览器先对原始 `File` 算 SHA-256，全部原图完成本地哈希预检后才允许发出第一份上传。
3. **落盘门**：既有建批脚本先做零写入预检；上传暂存、外部工作区和最终发布路径逐级校验，最终文件重新计算 SHA-256 后，仓库 manifest 才作为最后一步出现。

如果浏览器存储原图、上传暂存文件或最终工作区文件任一 SHA-256 不一致，流程立即停止并显示“原图一致性未通过”；该请求不能重试，也不会退回有损图片、放宽无损标准或留下一个可用批次。同名批次与同一请求的重复提交同样拒绝，既有 manifest 和工作区不覆盖。

## 本机上传、安全边界与回执

- 上传监听器固定绑定 `127.0.0.1:17372`，产品入口不提供改端口参数；仅自动测试可用系统分配的临时端口。
- 鉴权必须复用 `%USERPROFILE%\.infinite-canvas\canvas-agent.json` 中现有 token，并使用恒定时间比较；只接受 `http://localhost:3000` 或 `http://127.0.0.1:3000` 的本机画布来源。
- 请求日志被关闭；服务事件只记安全状态，不记录 token、图片字节、图片正文或原始异常内容。原图只落在受标记保护的暂存区与目标工作区，不嵌入画布命令或事件日志。
- 单文件最多 64 MiB，单批最多 512 MiB、最多 100 张；PNG、JPEG、WebP、GIF、BMP 还要同时通过文件类型与文件头核对。
- 建批成功后，画布回执只显示批次号、图片张数和七字段，并提示“批次已登记，真图制作将在下一里程碑开通”；不显示本机路径或哈希。外部工作区的 `manifests/batch_intake_receipt.json` 保存每张图的浏览器声明哈希、上传文件哈希和最终目标哈希，供审计核对。
- 服务状态与防重账本位于 `%USERPROFILE%\.infinite-canvas\batch-intake`，带专用安全标记。隔离验收的 `--batch-intake-test-root` 只接受已经存在且带精确 `.canvas_intake_test_root` 标记的独立目录；它不能指向真实批次父目录。
- 工作台工人状态账本为同目录的 `canvas_workbench.events.jsonl`，只追加工人名、状态和记录时间。`.batch_intake_service.lock` 是永久保留的一字节锁载体，不以文件创建时间判断实例是否存活；`.batch_intake_service.owner.json` 只是当前持有者说明。启动器遇到已有实例时会安全拒绝新工作台，第三服务窗口显示“建批服务已在运行”后退出，既有实例继续工作，不能删除锁载体强行启动。
- 收尾时必须把 `17372` 纳入残留检查。先运行 `Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 17372 -State Listen -ErrorAction SilentlyContinue`；若仍有监听，只能在核对完整命令行为本轮 `--serve-canvas-workbench` 后精确停止，不能按进程名批量结束。

M2-a 全程只做本机登记与逐字节原图保全，不访问外网、不调用模型、不产生费用，也不设置任何真实执行开关。2026-07-19 阶段 D 已完成：主仓 360/360 项通过；fork 36 项测试（272 个断言）、TypeScript 和生产构建通过；隔离批次 `验收餐具_20260719` 的两张浏览器原图、上传文件和最终文件 SHA-256 逐项一致，中文 manifest 文件名、工作区目录和路由读取往返无损；重复建批未上传、未覆盖。M1 演示分别在工作台入口和旧 `--serve-workflow-demo` 入口各完成 6+8 张，两边对应 14 个文件哈希全部一致。测试批次、隔离画布配置和临时服务均已清理，本段随本轮两仓独立提交收账，不 push；真实第二批次仍由用户本人操作。

## M2-b 真实费用、门禁续跑与正式图片通道

1. 无信息卡的工作流机器继续进入 M1 的 0 元演示；只要连有信息卡，就不得因信息卡未完成、连线不完整或多卡歧义退回演示。已登记信息卡与至少一张批次素材同时满足时，页面只读读取 17373 费用估算，显示剩余张数、约计美元金额和约计时长。取消只关闭费用卡，零命令、零 manifest 变更、零费用。
2. 用户确认费用后，机器只写 `run: next`；已有正式图片的续跑只写 `retry: renders`。后台首先经既有 `batch_editor` 把空目标一次性声明为 `main, detail, final_prompts, qc_reports`，随后每一步都依次经过 `run_controller.parse_run_content()`、`run_controller.resolve_command()`、`run_controller.execute_step()`。完整性与渲染没有门禁外放行分支；闸门①只开上游执行开关，六道工序完成后在完整性执行器之前正常暂停，不记录完整性开始或失败；路由到 `needs_qc_reports` 且已有 14 张后立即停机，QC 属 M2-c。
3. 正式 PNG 先落盘，再由 17373 只读端点交给浏览器。服务端文件 SHA、响应 SHA 和浏览器 Blob SHA 三者一致后才写入 localforage，并把 `storageKey` 回写节点；刷新或停止工作台后仍可显示。任一不一致硬停止且不自动重试。
4. 风格补登只接受直接连到已登记信息卡的磁盘图片。所有图片先完成浏览器预检再发第一份 POST；服务端在全部文件通过后才发布正式文件和新的 `style_reference_intake_receipt.<request>.json`。既有白底原图、资产清单与建批回执只读。
5. 阶段 C/D 只允许假执行器、临时工作区、回环 HTTP 和离线测试；不得设置真实执行开关、读取或索要密钥、调用模型或产生费用。阶段 E 的每个闸门仍须单独批准。

风格补登按钮会先只读访问 `http://127.0.0.1:17373/workbench-health`。服务未启动、风格工人已停止、画布正在重连、服务健康但 8 秒未确认分别显示四种人话提示；前三种不生成请求编号、不启动确认计时，四种情况都不自动重试。恢复后必须由用户亲手重新点击。若第三服务窗口消失，表示工作台已经停机，重新双击 `启动画布.bat` 即可；不要在旧页面上反复点击。

### 阶段 E 每闸门预检与收尾（每次都完整执行）

- 闸门①上游六工序、闸门②完整性加首张、闸门③剩余十三张在开始前都必须逐项复核：两仓为已提交干净状态；全量测试、TypeScript 与构建通过；首批账本仍为 72 行且指纹不变；第二批次事件行数和已有产物先登记；进程、用户、机器三个作用域的 `CODEX_DEV_ALLOW_REAL_EXECUTION`、`RENDER_ALLOW_REAL_EXECUTION`、`OPENAI_API_KEY`、`OPENAI_BASE_URL` 均为空；不存在并行 `--watch`、旧 `--serve`、另一个画布工作台或其他真实执行进程；`127.0.0.1:17373` 在本闸门工作台启动前必须**无监听**；启动后只读访问 `/workbench-health` 必须为 200，且建批、真实制作、风格补登三名关键工人均为 `running`；本闸门只准备一次画布命令。闸门①进程只临时打开上游执行开关，图片执行开关保持关闭；后台仍先让 `run_controller` 判定下一步，但在完整性第三段执行门禁之前正常暂停。这项保护只能阻止越过闸门，不能放行任何步骤。
- 闸门执行中只接受用户画布发出的唯一命令；任何失败立即停，不现场改码、不自动重试、不追加第二条命令。闸门②固定 `RENDER_MAX_IMAGES=1`；闸门③只在另获批准后处理剩余缺口。
- 每闸门结束都要停止临时服务，清空本次进程内开关和凭据，再核对三个作用域均为空；同时检查 `127.0.0.1:17372` 与 `127.0.0.1:17373` 均无监听。17373 检查命令：`Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 17373 -State Listen -ErrorAction SilentlyContinue`。若仍有监听，只能先查看 owner 的完整命令行，确认是本闸门工作台后精确停止；不得按 Python/bun 进程名批量结束。

## 可替换执行器边界

- 业务路由负责判断“现在能不能运行”；适配器只负责“如何执行”，不得绕过门禁。
- M1-b 的 `workflow-demo` 只在 `build_executor("workflow-demo", ...)` 的独立分支注册；原 `build_registry()` 的既有四个适配器与所有真实批次调用保持原样。它只允许标记后的 demo 工作区，不读取生产图片、不开真实执行开关，也不发起外部请求。
- `--serve` 的默认执行器仍是 `demo`。`codex-dev` 已注册为可选开发适配器，支持 `identity`、`style_master`、`angle_inventory`、`main_vc`、`detail_vc`、只产出提示词的 `final_prompts` 与只产出结构化报告的 `qc`；`image-production` 已注册为生产图片组合入口，内部复用 `openai-image`，不替换也不改变现有三个适配器的行为。
- `codex-dev` 复用 canvas-agent 的 `/agent/codex/threads/new`、`/agent/codex/turn`、`/events` 与 thread 读取接口，Python 不启动另一套 Codex 会话。新线程请求可在适配器内部选择模型；上层业务仍只认识 `codex-dev` 名称和统一契约。SSE 只作为完成通知，`failed` / `interrupted` 会先收敛为脱敏失败，只有 `completed` 才从本次专用 thread 重新读取最终文本，避免把共享事件流中其他线程的消息当成本次结果。identity、style master 和 angle inventory 的既有行为不变；main/detail 变量配置只在对应正式上游存在后运行，最终提示词只在两类变量配置均存在后运行，QC 只在 14 张正式渲染图与全部上游正式产物完整时运行。七类输出都必须位于 manifest 声明的 `artifacts_root` 范围内；越界产物、异常格式、Unicode 损坏字符和不受支持事实均在写入前拒绝，已有档案不会被覆盖。最终提示词整包与 QC 报告都使用同目录临时文件和排他落盘；任一批失败不留下正式半成品。
- `codex-dev` 的自动测试使用假传输和临时工作区，不启动真实 Codex、不访问网络、不读取真实批次图片。2026-07-15 完成本次详情配置校验器的类级修复后，全仓 180 项测试通过；既有四段覆盖、同线程续传、传输恢复与包装纠正上限、业务错误不重试、完整八项手持数量及汇总校验、失败零正式文件等行为保持不变。canvas-agent 继续使用连续 UTF-8 解码，避免中文字符跨数据块时被替换为 U+FFFD；fork 侧既有 3 项测试与 TypeScript 编译结果不受本次主仓库修改影响。
- 下游未确认事实门禁现在把数值精确等于用户确认高度、单位为厘米或 `cm`（不区分大小写）的复述默认视为合法，并由 detail 分段、detail 整包、main 整包和 `final_prompts` 批次共享；同一子句或字段路径含竞争维度、区间/连字、负号、单位扩展或相邻乘号尺寸组时仍拒绝，其他既有单位及非确认高度的厘米值也仍拒绝。材质/认证扫描只额外保护结构完整的“不把/不将……写死/固定/标注/设定/锁定/指定为/成……”否定指令，且保护范围只覆盖“为/成”后的目标列表；结构中夹带或另起的正向事实仍拒绝，既有保护词行为不变，也不会因“不锈钢”中的“不”或一般“不是……”而放行。一次业务门禁会先收集本次输入内全部未确认参数与商品事实，再用不超过 200 字符的“类别 + 净化字段路径 + 计数”统一报错；未知键名改用稳定占位符，不回显原值、数值上下文或提示词正文。段号、结构、模块、角度、比例和手持等其他校验仍保持原来的逐项立即失败。
- 真实执行默认关闭；只有获得用户明确批准后，才可在该次服务进程中临时设置 `CODEX_DEV_ALLOW_REAL_EXECUTION=1`，该开关不写配置、不持久化。既有 identity/style/angle/main/detail 真实验收历史、`ExecutionRequest / ExecutionResult`、三段门禁、默认 `demo`、`openai-image`、其他 `codex-dev` 阶段和产物格式均未改变。首次 `final_prompts` 于 2026-07-15 18:10:24 至 18:12:37 安全失败；离线修复并重新取得明确批准后，第二次执行已于 20:05:38 至 20:09:24 成功，正式目录现有 14 份 JSON、14 份 Markdown 和两份索引。后续 integrity、renders 与 QC 均已按独立闸门完成，任何重做或返修仍须另行批准。
- 2026-07-17 完成品类泛化与结构化用户确认的纯离线 TDD。历史 `shuiping_20260712` 的旧 notes 路径与四类提示词 UTF-8 字节指纹保持不变；新批次建批脚本强制收齐七项确认后才允许创建，并继续以仓库 manifest 为事实入口。全程未运行真实 Codex/模型、未启动画布命令、未创建第二批次，也未修改 QC、执行调度、三段门禁或 Schema。
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

阶段 1（只读实时投影）、阶段 2（布局持久化）、阶段 3（受控编辑，`--apply-edits`）、阶段 4（执行接入，`--serve` 运行台）均已跑通并有测试与现场验证。M1-a、M1-b 已完成，用户于 2026-07-18 亲手走完 M1-c 全剧本并确认“全部顺利，没有卡点”，M1 正式闭环。

M2-a 已按用户批准完成信息卡画布直填、中文批次命名、受控建批、原图保真通道、工作台承载切换和阶段 D 隔离验收。M2-b 阶段 C/D 的纯离线实现已完成：真实费用卡、三段门禁后台编排、逐张正式图片上桌并转存浏览器、信息卡风格补登 A、失败停机与既有路由续跑均有自动测试；主仓 393 项、fork 50 项（311 个断言）、TypeScript 与生产构建通过。没有触碰真实用户画布、真实模型、密钥或费用；阶段 E 尚未开始，必须逐闸门另获批准。首批 `shuiping_20260712` 仍保持关账冻结，事件 72 行、路由 `ready`，任何图片追加、覆盖或重生成仍须另行批准。
