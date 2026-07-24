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
- `workflow_production_controller.py`：M2-c 真实模式选择与门禁衔接。已登记信息卡和至少一张批次素材必须同时连到同一台机器；确认费用后才通过既有编辑门禁声明四项规范输出。每一步仍只由 `run_controller` 的命令解析、真实路由判定和统一执行三段门禁放行；14/14 且待质检时选择既有 `run: qc`，`ready` 保持终态，本模块不另建生产续跑分支。
- `workflow_production_service.py`：M2-c 后台编排。顺序复用现役 `codex-dev` 六工序、`image-production / integrity`、`image-production / renders` 与 `codex-dev / qc`，机器只显示人话进度，不投影九工序、运行台或日志。任一步失败即停且不自动重试；主/详情变量配置的受控语义分类可进入原批次失败事件并转成人话。完整性失败只在正式报告确为 fail 且阻塞数为安全整数时记录固定的“N 项阻塞，报告已写入 qc_reports”；浏览器持久化超时只允许精确固定文案受控透出。两类路径都不回显真实路径；报告异常、其他错误及含路径、URL、令牌或密钥的内容仍使用既有笼统文案。部分图片只能用既有 `retry: renders` 重新进三段门禁；路由以 `final_prompt_index.json` 的 `config_id` 清单为目标，合并核对 renders 与 repaired 同名 PNG，全部覆盖后继续 QC。`production_completed` 只表示 14/14 出图完成，并从事件账本现场去重；QC 报告生成后机器停在“质检完成”，正常重复点击不重跑，返修、交付和渲染重跑仍拒绝。
- `workflow_production_projection.py` / `workflow_production_render_observer.py`：正式 PNG 落盘后逐张上桌并连回机器；固定 `wfprod-output:<batch>:<config>` 节点保留旧成果并避让。主图只收正方形，详情精确 3:4 直接放行；精确 2:3 先原样审计，再用左右最外 24px 条带镜像、LANCZOS 拉伸和 radius=18 虚化自动补足背景，原图完整保留在中央并原子替换为精确 3:4。renders 开始前先用同一规则清扫存量详情 PNG；审计同名不同 SHA、主图非正方形和详情其他比例仍停机。Pillow 只在 2:3 分支延迟导入，缺失时回退原审计停机行为。
- `workflow_production_http_server.py`：固定 `127.0.0.1:17373` 的只读费用/正式 PNG 端点和风格补登上传入口。复用 canvas-agent 令牌，仅接受本机画布来源；正式图片返回 SHA-256，并在跨源成功响应中以 `Access-Control-Expose-Headers: x-content-sha256` 明确允许浏览器读取，浏览器再次核验后转存 localforage，节点不依赖服务长期在线，也不使用 data URI。`GET /workbench-health` 只返回四名工人的状态与最后状态时间，健康为 200，关键工人停止或画布重连中为 503，不返回令牌、路径或异常原文。
- `workflow_style_reference_intake.py`：M2-b “画布补登 A”通道。用户把磁盘风格图直接连到已登记信息卡；全部浏览器声明、节点凭证、字节数、类型和 SHA-256 一致后，才把原字节写入批准的 `inputs/style_refs` 并新建独立回执。白底原图、资产清单和建批回执不改写；不一致时整次硬停止且不自动重试。
- `canvas_workbench_service.py`：日常“画布工作台”承载入口。在同一进程中运行 M1 demo、M2-a 建批、M2-b 真实制作和风格补登，并统一管理 17372/17373 两个回环监听。建批、真实制作、风格补登是关键工人，任一意外停止都会让整机停止并非零退出；demo 仍按既有隔离策略处理。脱敏工人状态及固定白名单内的生产失败码只追加到受标记保护的 `canvas_workbench.events.jsonl`。M1 的 0 元演示行为保持不变；旧 demo 单服务入口继续保留作对照。
- `openai_image_executor.py`：GPT Image 2 适配器（默认模型 `gpt-image-2`），纯标准库 HTTP；无参考图走 `/v1/images/generations`，有参考图走 `/v1/images/edits`。HTTP 传输可注入，自动测试不访问真实网络。
- `render_task_assembler.py`：从 `final_prompt_index.json` 按原顺序组装供应商无关的图片任务。整批先核对提示词、唯一白底参考图和输出边界；主图映射 `1024x1024`，详情图暂映射 `1024x1536`；已有同名 PNG 自动跳过，便于安全续跑。
- `image_production_executor.py`：生产图片组合执行器 `image-production`，只接受 `integrity` 与 `renders`。前者运行 prompts-only 确定性门禁，后者在双开关通过后复用既有 `openai-image` 逐张执行；任一张失败即停止，已成功图片保留，错误不回显密钥或提示词正文。
- `qc_repair.py`：只读选择仓库/批次 QC 报告；双副本同时存在时要求逐字节一致。合法报告按最终提示词索引把 repair targets 聚为逐图工单，critical/major 进入增补段，needs_review 只保留人工记录；`return_stage` 不参与路由。每个工单沿用原 final prompt、negative prompt、画布比例和 renders 同源绑定参考图，输出路径只指向 repaired。
- `qc_repair_executor.py`：逐工单创建单任务 `image-production / renders` 计划，继续复用 `openai-image` 和现有比例观察器。单张失败不重试且继续下一张；renders 由执行前后 SHA-256 快照保护，合法同名 repaired 安全跳过，2:3 原件进入 repaired 专属审计目录，同批排他锁阻止并发重复费用。事件只记录 ID、数量、尺寸和哈希，不记录提示词正文、凭据或服务地址。
- `qc_repair_cli.py`：M2-d 服务外 CLI。命令必须先过既有 `parse_run_content()`，再过 `run_controller` 的 CLI 专用 repair 路由门禁，最后从统一执行入口调用返修执行器；不修改画布 service/controller，不触发 QC。真实执行仍须另行批准并由仓库外桌面闸门注入环境参数。
- `codex_dev_executor.py`：可选开发适配器 `codex-dev`。当前接受 `identity`、`style_master`、`angle_inventory`、`main_vc`、`detail_vc`、`final_prompts` 与 `qc`，通过 canvas-agent 现有 Codex 新线程 + HTTP/SSE 能力取得结构化结果；其他步骤在任何传输或文件访问前拒绝。前三步保持原有身份、风格和单品 A/B/C/D 角度边界。变量配置与最终提示词只读取已验收结构化上游档案：主图固定 6 套、1:1、2 套手持；详情固定 8 套、模块01至模块08、3:4、1 套手持，模块05只允许标注当批已确认高度且不得手持；`detail_vc` 在同一专用 thread 内按两项一段返回四段，U+FFFD 或可确认的截断 JSON 前缀可整段重发，整次执行最多恢复 2 次；只有第 1 段的配置与公共约束已通过业务门禁、但顶层 `notes` 不是字符串时，最多允许 1 次同线程完整格式纠正，不在本地搬移字段或复用失败正文；四段全部通过后才在内存重组并运行原完整校验。最终提示词用两个独立 thread 分别编译 6+8 套，全部通过后才一次性写入 14 份 JSON/Markdown 和索引，不生成 ComfyUI 作业、QC 或图片。最终提示词编译指令逐编号写明手持与绑定字面契约（与校验器一致）。所有下游步骤只允许合格 A/B/C，拒绝缺失 D、被拒源图、容量/其他尺寸/重量/具体材质/耐热/认证/品牌型号等未确认事实，并由本地适配器固定产品编号、正式路径、编号和哈希。该模块同时封装开发模型选择（当前经现场诊断验证为 `gpt-5.5`）、Codex 附件、同线程分批（单批附件载荷上限 20 MiB）、完整 JSON 请求体上限（默认 28 MiB）、SSE、真实 turn status、专用 thread 结果回读、空回复拒绝、返回校验和脱敏错误；完成但无非空助手答复会被分类为 `empty_assistant_response`，正常 `agent_done` 与异常回收的备用 `agent_error` 都只携带固定失败类别，主仓两路都能识别为空答复，不写产物、不自动重试，也不向运行台暴露原始异常。
- 新建批次以 manifest 顶层 `user_confirmed_facts` 为唯一权威用户确认入口，字段固定为 `product_type`、`height_cm`、`handheld_main=2`、`handheld_detail=1` 及三个布尔开关；商品品类只要求非空，不再锁死为水壶。下游生产管线和 prompts-only 完整性门禁都按同一纪律读取：旧 manifest 没有该对象时继续精确解析 `notes`；对象一旦存在就不回退，缺字段、多字段或类型错误均在执行前脱敏拒绝。允许清水为 `false` 时只允许空置；禁止倾倒与加热为 `true` 时拒绝正向动作描述，为 `false` 时不额外注入禁令但也不等于授权；D 缺失且“不补拍”为 `false` 时在传输前阻断，为 `true` 时仍只使用 A/B/C，不启用 D。
- `codex_dev_qc.py`：`codex-dev / qc` 的纯标准库业务模块。它在首个传输前一次性核对 manifest 路径边界、14 张 PNG 名称与 1:1/3:4 比例、14 份最终提示词绑定、3 张正式手持声明、白底参考图格式、QC Skill + 运行规则 + 三份完整参考正文、`qc_report.schema.json` 合同以及 20/28 MiB 请求上限；随后固定为 7 个两图批次加 1 个不带附件的全批总结。7 个图片批次各自新开独立 thread，第 8 次汇总再新开无附件 thread，并由提示正文携带前 7 批完整已校验 JSON。只有 U+FFFD 或明确 JSON 截断可在当前组 thread 内重发，全局最多 2 次；跨组 thread id 可不同，组内修复返回错配仍硬停止，合法 JSON 业务错误不重试。八批全部通过后才以排他原子方式只写 `qc_report.json`，永不覆盖既有报告，也不改动同目录完整性报告；`adds_new_generation_direction` 由本地固定为 `false`。
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
- 工作台脱敏事件账本为同目录的 `canvas_workbench.events.jsonl`：工人状态只记录工人名、状态和时间；生产空答复只记录工作流工人、六工序步骤、固定失败码和时间。未知步骤、未知失败码、令牌、路径与异常原文都不允许写入。`.batch_intake_service.lock` 是永久保留的一字节锁载体，不以文件创建时间判断实例是否存活；`.batch_intake_service.owner.json` 只是当前持有者说明。启动器遇到已有实例时会安全拒绝新工作台，第三服务窗口显示“建批服务已在运行”后退出，既有实例继续工作，不能删除锁载体强行启动。
- 收尾时必须把 `17372` 纳入残留检查。先运行 `Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 17372 -State Listen -ErrorAction SilentlyContinue`；若仍有监听，只能在核对完整命令行为本轮 `--serve-canvas-workbench` 后精确停止，不能按进程名批量结束。

M2-a 全程只做本机登记与逐字节原图保全，不访问外网、不调用模型、不产生费用，也不设置任何真实执行开关。2026-07-19 阶段 D 已完成：主仓 360/360 项通过；fork 36 项测试（272 个断言）、TypeScript 和生产构建通过；隔离批次 `验收餐具_20260719` 的两张浏览器原图、上传文件和最终文件 SHA-256 逐项一致，中文 manifest 文件名、工作区目录和路由读取往返无损；重复建批未上传、未覆盖。M1 演示分别在工作台入口和旧 `--serve-workflow-demo` 入口各完成 6+8 张，两边对应 14 个文件哈希全部一致。测试批次、隔离画布配置和临时服务均已清理，本段随本轮两仓独立提交收账，不 push；真实第二批次仍由用户本人操作。

## M2-b 真实费用、门禁续跑与正式图片通道

1. 无信息卡的工作流机器继续进入 M1 的 0 元演示；只要连有信息卡，就不得因信息卡未完成、连线不完整或多卡歧义退回演示。已登记信息卡与至少一张批次素材同时满足时，页面只读读取 17373 费用估算，显示剩余张数、约计美元金额和约计时长。取消只关闭费用卡，零命令、零 manifest 变更、零费用。
2. 用户确认费用后，机器只写 `run: next`；已有正式图片的续跑只写 `retry: renders`。后台首先经既有 `batch_editor` 把空目标一次性声明为 `main, detail, final_prompts, qc_reports`，随后每一步都依次经过 `run_controller.parse_run_content()`、`run_controller.resolve_command()`、`run_controller.execute_step()`。完整性与渲染没有门禁外放行分支；闸门①只开上游执行开关，六道工序完成后在完整性执行器之前正常暂停，不记录完整性开始或失败；路由只在最终提示词索引中的全部 `config_id` 都由 renders 或 repaired 同名 PNG 覆盖时进入 `needs_qc_reports` 并立即停机，QC 属 M2-c。
3. 正式 PNG 先落盘，再由 17373 只读端点交给浏览器。跨源成功响应同时返回允许来源、`x-content-sha256` 和 `Access-Control-Expose-Headers: x-content-sha256`；服务端文件 SHA、响应 SHA 和浏览器 Blob SHA 三者一致后才写入 localforage，并把 `storageKey` 回写节点。刷新或停止工作台后仍可显示；任一不一致硬停止且不自动重试，浏览器持久化超时以固定脱敏文案进入事件账本。
4. 风格补登只接受直接连到已登记信息卡的磁盘图片。所有图片先完成浏览器预检再发第一份 POST；服务端在全部文件通过后才发布正式文件和新的 `style_reference_intake_receipt.<request>.json`。既有白底原图、资产清单与建批回执只读。
5. 阶段 C/D 只允许假执行器、临时工作区、回环 HTTP 和离线测试；阶段 E 的三个真实闸门均已按单次批准执行并于 2026-07-23 收官。后续任何重跑、返修或新增费用仍须重新批准。

2026-07-23，第三批 `杯子_20260722` 已完成 M2-b：6 张主图与 8 张详情图共 14/14 张正式图片全部持久化上桌，事件账本 104 行，`render_auto_padded` 事件 7 条，供应端审计原件 8 份。`detail_01` 保留人工扩边先例，其余符合条件的 2:3 详情图由系统自动无损扩边；`detail_05` 的 936×1248 分辨率差异留待 M2-c QC 与用户终审。

风格补登按钮会先只读访问 `http://127.0.0.1:17373/workbench-health`。服务未启动、风格工人已停止、画布正在重连、服务健康但 8 秒未确认分别显示四种人话提示；前三种不生成请求编号、不启动确认计时，四种情况都不自动重试。恢复后必须由用户亲手重新点击。若第三服务窗口消失，表示工作台已经停机，重新双击 `启动画布.bat` 即可；不要在旧页面上反复点击。

真实费用确认在同一页面内按“机器 + 批次”锁定一次提交：确认成功写出命令后，即使后台失败也不会在原页面再次生成命令。若已取得新的执行批准，用户须重新打开画布，再亲手点击“开始”并重新确认费用；不存在自动重试。空答复时机器显示“本地 Codex 本轮没有返回内容，机器已停下，未自动重试。”，批次事件格式保持不变，工作台脱敏账本只记录 `execution_failure`、步骤与固定码 `empty_assistant_response`。该固定码通过正常完成通知或备用失败通知到达都按同一规则处理；未知失败仍收敛为一般线程失败，原始异常正文不透传。

### 阶段 E 闸门实录与当前运行约束（已完成）

- 闸门①、闸门②、闸门③均按单次批准执行；失败即停、零自动重试和重新打开画布后再次确认的纪律已在第三批生产中验证。
- 日常启动器现默认设置 `CODEX_DEV_ALLOW_REAL_EXECUTION=1`。它只允许上游适配器进入既有门禁，不等于自动开始任务、批准费用或绕过三段门禁。
- 渲染类闸门由仓库外本地启动器按批准承载，该文件不入 Git。未来任何重跑、返修或新增费用仍须重新批准，并继续执行工作台健康检查、端口启停核对、失败即停和收尾归零。

### M2-c 第一段：QC 步接入工作台

- 14/14 正式图片齐全且 QC 报告缺失时，用户在画布机器上点击一次“开始/重新开始”，工作台继续执行既有 `run: qc`；运行中显示“正在逐张质检 14 张成图…”，报告落盘后显示“质检完成，QC 报告已生成。”并停机。
- `production_completed` 保持“14/14 出图完成”语义。服务从事件账本和现场路由判断是否已记账，不依赖进程内存；第三批进入 QC 或在 `ready` 状态重复点击都不会重复追加该事件。
- 现有 QC 继续使用 7 个两图批次和第 8 次汇总。第三批最大估算单批附件为 13,473,190 字节，低于 20 MiB 上限；无需缩图或改变附件策略。7 个图片批次现在各用独立 thread，第 8 次汇总使用独立无附件 thread；组内传输修复仍复用本组 thread，全局恢复上限保持 2 次。
- 第三批首次真实 QC 在完成前 5 组后因旧单线程上下文累积超出 258,400-token 窗口而失败；第 6 组未进入 Codex，报告目录零新增、14 张 renders 无损。线程隔离补丁的纯离线测试为 512/512；用户重启工作台后亲手发起的第二次 QC 已于 16:38:02 成功，成功或失败后停机且不自动重试的纪律保持。QC 角标与已收货框属于 M2-c 第二段。

### M2-d 第一段：QC 驱动的后端单图返修

- 第三批第二次真实 QC 已于 16:38:02 成功：175 项检查为 153 pass / 18 fail / 4 needs_review，18 条 repair targets 聚合为 8 张待返修图；报告明确未新增生成方向。110 行事件账本与 52,403 字节报告已独立入账。
- CLI 形态为 `python canvas-bridge/qc_repair_cli.py --batch-manifest <manifest> --command "run: repair"`。它不接画布；现有 `RUN_VERBS`、`parse_run_content()` 和 `resolve_command()` 原样保留，repair 只使用 `run_controller` 新增的 CLI 专用门禁。
- 每图提示词只由原 final prompt、该图全部可执行 repair goals 和原 negative prompt组成；产品身份、绑定角度与画布比例保持不变。`detail_06` 镜像 needs_review 不进入自动目标，`return_stage` 不参与机器分流。
- 每图通过既有 image-production/openai-image 链生成到 `outputs/repaired/`，参考图与原 renders 同源；renders 不覆盖。单张失败继续但不重试，部分失败清单收尾后停机，不自动复检。2:3 原件审计在 `artifacts/audit/repaired/render_originals/`。
- 离线实现完成后，第三批于 2026-07-24 真实执行 8/8 返修成功；25 行运行事件已独立入账，135 行账本现已冻结。用户终审已采纳全部 8 张返修图，本批定位为流程验证批，`detail_06` needs_review 关闭；这不自动触发复检或交付。

### M2-c 第二段前置：completed 续行与 QC 进度心跳

- completed 机器可重新进入既有费用确认流程，确认后必须发送 `run: next`，由后端按现场路由执行 QC 或幂等完成；paused/部分失败仍使用 `retry: renders`，单页一次提交锁保持不变。
- QC 在 7 个图片批组和第 8 次汇总分别通过解析后发出 8 个进度信号。每次更新保持 `status=running`、`step=qc`、`producedCount=14`，刷新 `updatedAt`，并显示“第 N/8 组完成”。
- 每次 QC 使用独立的短生命周期 daemon 心跳工作线程。QC 主线程只投递；画布更新只尝试一次、使用短超时、不进入 `_apply_with_reconnect`。成功时排空后关闭，失败或未预期异常时取消待发项并在终态前关闭，避免迟到 running 覆盖 completed/failed，也不让工作线程在 QC 结束或服务退出后悬挂。
- 心跳不写 QC 中间产物、不改变报告 schema、检查结论、恢复上限或 12 分钟前端保险丝。离线回归为主仓 542/542、fork 52 项/344 断言；真实显示效果留待下次用户批准的 QC/重跑观察。

### M2-c 第二段：桌面收货与关账

- `GET /workflow-production/{batch}/qc-summary` 只返回逐图 `configId/status/issueCount/topCategories`；有 issues 优先为问题态，无 issues 且任一检查为 needs_review 才是待核对，其余为通过。报告缺失返回 404，前端静默不显示角标。
- 图片下载保留旧的 renders-only 地址，并新增 `/outputs/renders/{config}` 与 `/outputs/repaired/{config}`。两类路径分别受 manifest 白名单和批次工作区边界约束；返修图使用独立节点 ID 与明确 `source=repaired`，复用浏览器 SHA 和字节数持久化合同。
- 返修图入口只做磁盘成品投影，不构造工作流命令、不调用执行器。旧正式图只有在稳定节点 ID、批次、图位和磁盘 SHA 全部吻合时才安全补齐 `source=renders`；失败节点保留脱敏证据且不参与角标或收货。
- `GET/POST /workflow-production/{batch}/acceptance-closeout` 使用同一回环、令牌和浏览器来源保护。POST 必须提交 14 个不同图位的 `configId/source/sha256`；服务逐项核对磁盘实物后追加唯一 `batch_acceptance_closed`。已有关账事件时，制作、QC、返修和返修投影均在改清单、记新事件或调用执行器前拒绝。

## 可替换执行器边界

- 业务路由负责判断“现在能不能运行”；适配器只负责“如何执行”，不得绕过门禁。
- M1-b 的 `workflow-demo` 只在 `build_executor("workflow-demo", ...)` 的独立分支注册；原 `build_registry()` 的既有四个适配器与所有真实批次调用保持原样。它只允许标记后的 demo 工作区，不读取生产图片、不开真实执行开关，也不发起外部请求。
- `--serve` 的默认执行器仍是 `demo`。`codex-dev` 已注册为可选开发适配器，支持 `identity`、`style_master`、`angle_inventory`、`main_vc`、`detail_vc`、只产出提示词的 `final_prompts` 与只产出结构化报告的 `qc`；`image-production` 已注册为生产图片组合入口，内部复用 `openai-image`，不替换也不改变现有三个适配器的行为。
- `codex-dev` 复用 canvas-agent 的 `/agent/codex/threads/new`、`/agent/codex/turn`、`/events` 与 thread 读取接口，Python 不启动另一套 Codex 会话。生产上游适配路径在一处固定 `model=gpt-5.5` 与 `effort=xhigh`，新线程和回合请求均显式携带两项设置，不再继承桌面或全局档位；Canvas Agent 按 Codex 0.139.0 白名单校验档位，并在可恢复的线程异常重建中保留模型和档位。未提供档位的普通画布回合仍省略该字段。上层业务仍只认识 `codex-dev` 名称和统一契约。SSE 只作为完成通知，`failed` / `interrupted` 会先收敛为脱敏失败，只有 `completed` 才从本次专用 thread 重新读取最终文本，避免把共享事件流中其他线程的消息当成本次结果；Canvas Agent 在回合异常回收时另发同一白名单内的备用失败通知，防止完成通知未被消费时丢失空答复类别。identity、style master 和 angle inventory 的既有行为不变；main/detail 变量配置只在对应正式上游存在后运行，最终提示词只在两类变量配置均存在后运行，QC 只在 14 张正式渲染图与全部上游正式产物完整时运行。七类输出都必须位于 manifest 声明的 `artifacts_root` 范围内；越界产物、异常格式、Unicode 损坏字符和不受支持事实均在写入前拒绝，已有档案不会被覆盖。最终提示词整包与 QC 报告都使用同目录临时文件和排他落盘；任一批失败不留下正式半成品。
- 2026-07-21 完成生产图文档位显式固定的离线 TDD：主仓 406 项、fork 69 项/332 断言、Canvas Agent 编译、web TypeScript 与生产构建全部通过。测试覆盖启动参数、回环 HTTP/SSE、空答复单次硬停止与零产物、附件字节/顺序、非法档位拒绝、异常重建保留和普通回合隔离。本次没有运行真实 Codex、没有访问外网、没有设置执行开关或密钥；一次隔离配额验证仍须用户另行批准。
- `codex-dev` 的自动测试使用假传输和临时工作区，不启动真实 Codex、不访问网络、不读取真实批次图片。2026-07-15 完成本次详情配置校验器的类级修复后，全仓 180 项测试通过；既有四段覆盖、同线程续传、传输恢复与包装纠正上限、业务错误不重试、完整八项手持数量及汇总校验、失败零正式文件等行为保持不变。canvas-agent 继续使用连续 UTF-8 解码，避免中文字符跨数据块时被替换为 U+FFFD；fork 侧既有 3 项测试与 TypeScript 编译结果不受本次主仓库修改影响。
- 下游未确认事实门禁现在把数值精确等于用户确认高度、单位为厘米或 `cm`（不区分大小写）的复述默认视为合法，并由 detail 分段、detail 整包、main 整包和 `final_prompts` 批次共享；同一子句或字段路径含竞争维度、区间/连字、负号、单位扩展或相邻乘号尺寸组时仍拒绝，其他既有单位及非确认高度的厘米值也仍拒绝。材质/认证扫描只额外保护结构完整的“不把/不将……写死/固定/标注/设定/锁定/指定为/成……”否定指令，且保护范围只覆盖“为/成”后的目标列表；结构中夹带或另起的正向事实仍拒绝，既有保护词行为不变，也不会因“不锈钢”中的“不”或一般“不是……”而放行。一次业务门禁会先收集本次输入内全部未确认参数与商品事实，再用不超过 200 字符的“类别 + 净化字段路径 + 计数”统一报错；未知键名改用稳定占位符，不回显原值、数值上下文或提示词正文。段号、结构、模块、角度、比例和手持等其他校验仍保持原来的逐项立即失败。
- 2026-07-21 的 `main_vc` 语义门禁修复只增加两类受控放行：背景/道具字段内紧邻非产品道具名的环境材质，以及原规则之外的“未安排/不计划/不执行”等否定谓语。产品本体材质和正向危险动作仍拒绝，原有否定标记与远距离放行句式原样保留。主/详情变量配置提示都要求“唯一合格源图编号 + 原样 A/B/C 槽位字样”，与本地既有硬校验一致。真实失败答复原样夹具及路径、URL、令牌、密钥回退反例均有离线测试；全仓 417 项通过，未调用真实 Codex、网络或图片服务。
- `real_execution_disabled` 兼容分支继续保留，用于非日常入口或启动异常时在调用前安全停止。日常入口已默认设置上游真实执行开关，旧的“裸启动不带开关”操作说明不再作为当前日常流程。
- 2026-07-17 完成品类泛化与结构化用户确认的纯离线 TDD。历史 `shuiping_20260712` 的旧 notes 路径与四类提示词 UTF-8 字节指纹保持不变；新批次建批脚本强制收齐七项确认后才允许创建，并继续以仓库 manifest 为事实入口。全程未运行真实 Codex/模型、未启动画布命令、未创建第二批次，也未修改 QC、执行调度、三段门禁或 Schema。
- canvas-agent token 只从本机配置读取并放在鉴权请求头中；`codex-dev` 只接受 `http://127.0.0.1`、`http://localhost` 或 `http://[::1]` 回环地址，并显式禁用系统代理。事件日志只记录通用成功说明或彻底切断原始异常链的脱敏错误，不记录 token、完整提示词、Codex 原始错误正文或产品图片内容。
- GPT Image 2 密钥只从服务端环境变量 `OPENAI_API_KEY` 读取；可选 `OPENAI_IMAGE_MODEL` 和 `OPENAI_BASE_URL` 只属于该适配器。`OPENAI_BASE_URL` 可填写已带 `/v1` 的 API 根路径，也可填写裸域名（自动补为 `/v1`）；生产 HTTP 传输使用固定且不含敏感信息的 `Codex-Canvas-Bridge/1.0` 客户端标识，并保留调用方显式传入的标识。密钥不得写入 manifest、画布节点、事件日志或仓库文件。
- 可选 `OPENAI_IMAGE_TIMEOUT_SECONDS` 只接受 `30` 至 `1800` 的整数；未设置时保持默认 `180` 秒，内部显式传入的等待值优先。该值表示连接或响应连续无新数据时的等待上限，不是整次任务的总时长；非法值在联网前拒绝。闸门执行可在获批的临时进程中设为 `900`，不得持久化。
- `shuiping_20260712` 的 14 张正式图片已按用户批准完成；真实 QC 后名称与字节数复核不变。任何追加、覆盖或重新生成仍必须重新批准真实 API 成本。
- 2026-07-17 已在提交 `634c58f` 完成 `codex-dev / qc` 离线 TDD：新增 22 项 QC 专项测试，全仓 260 项测试通过；真实 manifest 的只读计划预检确认 14 张图、7 个两图批次与 `main_02/main_05/detail_02` 三张手持图，最大估算附件负载低于 20 MiB。随后在用户明确批准“执行，代写”后，从原画布一次且仅一次提交 `run: qc`：事件于 12:06:06 开始、12:32:16 成功，耗时 1569.9 秒，无重试或第二条画布命令。唯一 `qc_report.json` 为 56,371 bytes，SHA-256 为 `54ADB10B8D573E266EC24E65FC45A2E62DD50F05AF7547FD5B79BC06F5D6ED0D`；14 张图、175 条检查、19 个 issues 和 19 个 repair_targets 均通过结构与 `jsonschema` 校验。事件现为 72 行，路由为 `ready`；临时服务与执行开关已清除，ComfyUI 和 repaired 未执行。
- 2026-07-22 完成 `main_vc` / `detail_vc` 道具材质结构化门禁二轮修复：正式变量配置从本批 `style_master.json` 的 `style_master` 正文提取连续短语白名单，只有道具语境、母版短语命中且无产品指向时才放行；材质词后至少还要有两个文字字符，单独“玻璃”、母版不存在的短语、非道具字段及“杯身为玻璃”“玻璃质感壶身”等产品材质继续拒绝。详情分段只把无产品指向的道具候选延后到整包门禁，最终写入前仍严格裁决。`final_prompts` 同步由执行器把同一正式母版正文传给材质扫描，提示词正文可复用母版道具短语，但产品材质仍拒。主/详情变量配置指令同时要求“辅助参考图调用”的“对应产品”只原样填写本批 `product_id`。真实第三批失败答复以 33,839 字节原始夹具入库，SHA-256 为 `AE05BC6AE743F9358D46005C9A014FF597D006CF3BF7527213F154FA870B907D`；全仓由 421 项增至 435 项并全部通过。
- 同日完成道具语境字段终局收口：在既有母版道具白名单中补全 `构图方式`、`道具密度等级`、`真实感要求`、`风格防退化检查`，并把 MAIN 23 + DETAIL 33 个字段位置全部纳入“道具语境 / 非道具语境”二选一守卫；任何新增、改名、漏归类或双重归类都会直接测试失败。主/详情变量配置指令要求商品材质一律用“材质”统称，不输出陶瓷、玻璃、不锈钢、塑料等具体词；这不放宽响应门禁，四个新字段中的“杯身为玻璃”在分段与整包模式仍全部拒绝。13:59 真实失败第一段以 20,315 字节原始夹具入库，SHA-256 为 `19CA5AB590DC1A661D6F6600272AB628301EB82FB067C37A0C5C47890EF2C404`；新增 8 项测试后全仓由 435 项增至 443 项。该误判家族止损线已达 3/3；此后再发生任何一次真实运行误判，立即转入输出契约级全面重设计，不再追加例外或补丁。
- 同日完成场景否定列举辖域收口：`_term_has_scene_negation` 的既有三路判定原样保留，只单向增加“子句开头的受限否定头 + 完整首个列举成分 + 顿号/或/和/及/与列举链”，并禁止跨越“而/但/却/仍/再”。因此真实句式“不表现饮用、倒水、加热或任何内容物使用”放行，正向动作、转折以及“使用不锈钢、倒水动作”“不锈钢、倒水动作”继续拒绝，且未新增材质或否定谓语词表。主/详情变量配置指令同时要求用“不出现任何禁止的内容物或动作”统称安全边界，或原样复述规则句，不自行列举禁词；响应门禁保持严格。真实第二段以 18,585 字节原始夹具入库，SHA-256 为 `F3DA8D6FBFDE5DF02A8EEEF37EDB74D4B83F6B04EB60E0D5F3069A3805AA4B67`；新增 11 项且既有测试零修改，全仓由 443 项增至 454 项。场景否定误判家族计为 2/3；此后再发生任何一次真实运行误判，立即启动该门禁的输出契约级全面重设计，不再有例外或补丁。
- 同日两族止损线同时触发后完成输出契约级全面重设计，取代前两轮“风格母版短语白名单 + 子句词扫描”的裁决机制。最终提示词 3 字段、主图 23 字段、详情图 33 字段现在按封闭映射归入正向描述、负面清单或非语义控制；`negative_prompt` 与 `禁止事项` 整体豁免材质、认证、参数和场景词，不做反向必含检查。正向材质/资质改为默认放行且只拒紧邻或修饰商品主体的宣称；仅同句出现主体标记不足以拒绝，“产品搭配玻璃器皿”和“突出陶瓷材质”放行，“杯身为玻璃”“陶瓷马克杯”继续拒绝。正向场景门禁、既有四路否定识别和确认高度逻辑保持，系统场景规则完整原文精确豁免。受控工作台错误白名单只新增主/详情最终提示词标签及 `prompts/<数字>/final_prompt|negative_prompt` 路径。33,839B `main_vc` 夹具仍恰拒 6 处 B 类，18,397B 详情批和 15,020B 主图批结合真实上游全链通过；全仓 468 项测试通过。此次纯离线变更未改工人监督、健康接口、账本结构、三段门禁、产物格式或真实执行边界。
- 同日晚完成 `final_prompts` 场景契约补全与列举首项收口：主/详情最终提示词模板复用变量配置的统称安全规则并动态注入 `_variable_scene_rule(requirements)`，逐词禁止清单只写 `negative_prompt`；既有四路场景否定判断不变，第五路只识别“子句开头的受限否定头 + 首个被扫描词 + 立即进入列举”，以 `{慎, 小心, 停, 断, 住, 禁}` 为封闭副词排除集，并在自身列举链内阻断“而/但/却/仍/再”。“不再倒出、饮用”继续由既有第二路放行，“不停倒出、饮用”“不断倒出、饮用”继续拒绝；`无端/无故` 等更远边缘留给 QC 兜底，不再扩集。18,615B r2 夹具 SHA-256 为 `FD56B63FDFE158AADE13A3FDBF1CDB3D2FDACBC70CA0A35A3BD675BDDC5EB82A`，结合正式上游 6/6 通过；新增 8 项且既有测试零修改，全仓 476 项通过。最终止损约文："此后'场景否定误判'家族若再发生任何一次真实运行误判，场景扫描无条件反转为默认放行（仅保留 QC 兜底与负面清单/正向语境分类），不再有任何语法扩展或补丁。"
- 未来接入其他图片服务时，实现相同的 `execute(ExecutionRequest) -> ExecutionResult` 契约并在 `executor_factory.py` 注册即可，上层画布逻辑不变。

## 生产图片执行链（已实现；第三批返修已完成并采纳）

1. `image-production / integrity` 调用既有校验脚本的 `--prompts-only` 模式，只检查 6+8 数量与顺序、既有 Schema、来源文件与逐项解析指纹、手持数量、锚定“画布比例固定为”短语的 1:1/3:4 字面、manifest 已确认高度语义和 UTF-8/Unicode 完整性。比例短语与比例值之间允许零个或多个空白，裸 `1:1` / `3:4` 仍不合格。它不读取 ComfyUI 作业清单，并在 JSON/Markdown 报告中逐项记录跳过旧内容启发式扫描与旧编译器字面扫描的原因；默认 ComfyUI 模式未改变。
2. 门禁通过后，`image-production / renders` 从索引读取 14 项，逐项绑定 manifest 白底图目录中唯一同名参考图，并把 `final_prompt` 原文与 `negative_prompt` 原文用固定分隔符组合；不改写正向正文。
3. 真实传输前必须同时满足 `RENDER_ALLOW_REAL_EXECUTION=1` 与非空 `OPENAI_API_KEY`。可选 `RENDER_MAX_IMAGES` 只执行前 N 个尚缺图片；已有 `<config_id>.png` 自动跳过。第三张失败时前两张保留，下一次从缺口继续，不覆盖已有图片。连接、正常响应读取或错误响应读取超时均统一为不含密钥、提示词和原始响应的中文失败；超时永不自动重试，也不留下半成品。
4. 主图请求固定 `1024x1024`，详情图请求继续使用既有 `1024x1536` 映射，最终统一要求精确 3:4。供应端原图已是 3:4 时保持原文件；精确 2:3 时先保留审计原件，再自动扩展不足方向的镜像虚化背景，1024×1536 固定左右各补 64px 为 1152×1536，原商品与文字区域不裁剪、不缩放、不拉伸；其他异常比例仍停机。首批历史上的 `detail_02` 与 `detail_05` 已按相同参数人工扩边并保留原件；2026-07-23 起该参数由观察器自动执行，扩边事实以 `render_auto_padded` 记录，扩边后 SHA 仍由 `image_persisted` 记录。
5. `shuiping_20260712` 已完成 prompts-only 门禁、模型探测、全部真实出图和真实 QC。六张主图均为有效 `1254x1254` PNG；八张详情图均为有效且精确 3:4 的 PNG。正式 renders 恰好 14 个文件，QC 后字节数不变；事件账本 72 行，唯一 `qc_report.json` 已生成，真实路由为 `ready`。ComfyUI 作业与 repaired 均为 0；返修决策见 `docs/CANVAS_PROJECT_STATE.md` §8。
6. `杯子_20260722` 已完成 14/14 出图、第二次真实 QC 与 8/8 真实返修；唯一 QC 报告和 135 行事件账本已归档。用户已采纳全部 8 张返修图并关闭 `detail_06` needs_review；未自动复检或交付。

## 状态

阶段 1（只读实时投影）、阶段 2（布局持久化）、阶段 3（受控编辑，`--apply-edits`）、阶段 4（执行接入，`--serve` 运行台）均已跑通并有测试与现场验证。M1-a、M1-b 已完成，用户于 2026-07-18 亲手走完 M1-c 全剧本并确认“全部顺利，没有卡点”，M1 正式闭环。

2026-07-24 最新状态：第三批 `杯子_20260722` 已完成 14/14 正式出图、第二次真实 QC 和 8/8 真实返修，事件账本 135 行并冻结。用户终审采纳全部返修图，本批定位为流程验证批，`detail_06` needs_review 关闭。QC 机器现有 7+1 组进度心跳，completed 可经费用确认发送 `run: next`；主仓 542/542、fork 52 项/344 断言及 TypeScript 检查通过。真实心跳和 completed 点击效果留待下次获批运行观察。
