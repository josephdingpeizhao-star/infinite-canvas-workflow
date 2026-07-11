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
- `ic_client.py`：canvas-agent HTTP 客户端。从 `~/.infinite-canvas/canvas-agent.json` 读取 url/token。
- `make_demo_workspace.py`：演示用外部工作区脚手架（默认 `D:/dev/canvas-demo-workspace`，带安全标记，绝不写仓库）。
- `spike_canvas_push.py`：驱动脚本，见 `--help`。

## 前置条件

1. canvas-agent 在本机运行（默认 `http://127.0.0.1:17371`）。
2. infinite-canvas web 在本机运行（默认 `http://localhost:3000`）。
3. 浏览器已打开某个画布页，并带 `?agentUrl=...&agentToken=...` 参数完成自动连接。

## 典型用法

```powershell
python canvas-bridge/spike_canvas_push.py --health
python canvas-bridge/spike_canvas_push.py --push-live <批次manifest> [--layout-path P] [--restore-viewport]
python canvas-bridge/spike_canvas_push.py --watch <批次manifest> --interval 2 [--layout-path P]
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

## 状态

阶段 1（只读实时投影）与阶段 2（布局持久化）机制已跑通并有测试与现场验证。阶段 3（受控编辑）之前，画布对业务数据保持零写入。
