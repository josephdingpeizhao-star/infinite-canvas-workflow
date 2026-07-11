# 画布化子项目状态总账（Canvas Project State）

> **本文件是画布子项目的唯一权威状态账本。**任何智能体（Codex、Claude 或其他）在触碰画布相关代码前必须先读完本文件；任何改变画布子项目状态的会话，结束前必须更新本文件（见文末"维护协议"）。本文件取代任何工具私有的会话记忆。
>
> 最后更新：2026-07-11 深夜（阶段 4 验收完成后）。

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
| ⑤ | LLM 阶段的执行用 canvas-agent 内置的 Codex 线程直驱 | 同上 |
| ⑥ | 布局文件（canvas_layout）进 Git | 同上 |

## 4. 阶段进度台账

| 阶段 | 内容 | 状态 | 关键提交 | 验收证据 |
|---|---|---|---|---|
| 0 | 协议冒烟（六个尖峰问题） | ✅ | — | `docs/CANVAS_SPIKE_REPORT.md` |
| 1 | 只读实时投影（状态点亮、--watch 增量） | ✅ | f2293a5 之前诸提交 | 测试 + 现场 |
| 2 | 布局持久化（canvas_layout，按图节点 id） | ✅ | f2293a5 | 测试 + 现场 |
| 3 | 受控编辑（wfedit 节点，三段门禁，--apply-edits） | ✅ | 28788bb | 2026-07-11 晚现场四步验收（含 banana 拒绝、210 字符长 notes 回读无截断） |
| 4 | 执行接入（wfrun 运行台 + 事件日志 + --serve 常驻） | ✅ | 6f4e361 | 2026-07-11 深夜现场：9 步全链路画布触发跑通、门禁拒绝、retry；66/66 测试绿 |

qc 路由缺陷修复（门禁报告不再算质检完成）、孤儿修复（重投影删除全图节点 id，20ba7a8）均已入库。

## 5. 代码地图

**主仓库（本仓库）**：

- `canvas-bridge/`——全部桥接逻辑，模块职责见 `canvas-bridge/README.md`（投影 projector、状态读取 state_reader、布局 layout_store、受控编辑 batch_editor、执行接入 run_controller、驱动脚本 spike_canvas_push）。
- `manifests/workflow_graph.template.json`——工作流图模板（唯一图定义，schema 校验 + 与 route_batch 一致性测试）。
- `tests/test_canvas_*.py`、`tests/test_batch_editor.py`、`tests/test_run_controller.py`、`tests/test_workflow_graph_projection.py`——画布子项目测试（含在全仓库 66 个测试内，运行 `python -m unittest discover -s tests`）。

**fork 仓库（独立 Git 仓库，不在本仓库内）**：

- 位置：`D:\dev\infinite-canvas`，分支 `workflow-editor`，当前 @ 01f2c14，上游基线 ebd8ae2（2026-07-09 origin/main）。
- **改动登记册：`FORK_NOTES.md`（fork 仓库根目录）**——列出全部锚点（截至 2026-07-11 共 3 个：①agent 回读只截断 data:URL ②面板连接持久化 localStorage ③工具确认开关持久化）。动 fork 前必读，同步上游后逐条复核。
- `web/bun.lock` 有一处未提交改动（bun install 副产物，无关紧要）。

**演示工作区（可丢弃）**：`D:\dev\canvas-demo-workspace`，由 `canvas-bridge/make_demo_workspace.py` 管理（`--init/--add-inputs/--advance <步骤>/--reset`），带 `.canvas_demo` 安全标记，绝不写仓库。

## 6. 运行时手册

**服务 4 个**：

| 服务 | 端口/形态 | 启动方式 |
|---|---|---|
| canvas-agent | :17371 | `bun run --cwd D:/dev/infinite-canvas/canvas-agent dev` |
| 画布网页 | :3000 | `bun run --cwd D:/dev/infinite-canvas/web dev` |
| 批次运行台 | 常驻 cmd 窗口（标题"批次运行台服务*"） | `python canvas-bridge/spike_canvas_push.py --serve <manifest> --layout-path <layout> --interval 2` |
| 静态图片 | :8801（仅图片演示需要） | `python -m http.server`（临时目录） |

- **日常入口：仓库根目录 `启动画布.bat`**（双击自启 agent+web+serve 三服务，各有防重复守卫，再开 Chrome 到 `/canvas`）。该文件**有意不入 Git**（含本机绝对路径）；迁移机器时需重建。
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
- 阶段 4 仅内置 **demo 执行器**（驱动演示工作区 `--advance`，有安全标记保护）。真实执行器接入点：`canvas-bridge/run_controller.py` 的 `EXECUTORS` 注册位与 `build_executor()`。

## 8. 后续路线图（候选，未排期）

1. **Codex 执行器**：LLM 阶段（identity/style/angle/vc/qc 等 executor=agent 的图节点）经 canvas-agent 的 Codex 线程直驱（决策⑤）。**耗 API 配额，动手前需用户明确批准。**接入点见 §7。
2. **Comfy/脚本执行器**：`stage_render`（executor=comfy，`scripts/submit_comfy_cloud_jobs.py`）与 `gate_final_prompt_integrity`（executor=python）直跑真实脚本。
3. **真实产品批次入库**：用真实水壶批次替换演示工作区跑全链路。
4. fork 上游同步演练（锁 tag、合并后逐条复核 FORK_NOTES.md + 跑 66 测试 + 桥接冒烟）。

## 9. 维护协议（交接纪律）

1. **改动画布子项目状态的会话，结束前必须更新本文件**（进度台账、代码地图、坑清单、路线图相应条目），并与代码同一提交或紧随提交。
2. 动 fork 前读 `FORK_NOTES.md`；新增锚点必须当场登记；能放 canvas-bridge 的逻辑不进 fork。
3. 验收标准：`python -m unittest discover -s tests` 全绿 + 桥接冒烟（`--health`、`--push-live`）+ 涉及交互时的现场验证。
4. 提交风格沿用 git log 现状（`feat:` / `fix:` / `docs:` 前缀，一里程碑一提交）。
5. 不确定的历史结论先查本文件与 `docs/CANVAS_SPIKE_REPORT.md`、`canvas-bridge/README.md`、fork `FORK_NOTES.md`、git log，**不要凭聊天记忆推断，不要重新分析已定决策**。
