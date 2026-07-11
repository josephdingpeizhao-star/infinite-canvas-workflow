# Canvas Bridge (Spike)

把主仓库的工作流图模板与批次 manifest 投影到 infinite-canvas 画布的桥接层原型。

## 边界（硬性约束）

- 仓库文件（manifest、schema、规则、报告）是唯一事实来源；画布只是投影目标。
- 本目录代码只读取仓库文件，除非用户显式要求，否则不写入仓库任何文件。
- 不修改 infinite-canvas 源码；只通过 canvas-agent 的本地 HTTP 协议（`/api/tools`）通信。
- 纯 Python 标准库，与 `scripts/` 保持一致，不引入第三方依赖。

## 模块

- `projector.py`：纯函数投影。`manifests/workflow_graph.template.json` + 批次 manifest → `canvas_apply_ops` 操作列表（分层布局，节点 id 以 `wf:<product_id>:` 前缀命名，重推自动先删后建，幂等）。
- `ic_client.py`：canvas-agent HTTP 客户端。从 `~/.infinite-canvas/canvas-agent.json` 读取 url/token。
- `spike_canvas_push.py`：Spike 驱动脚本，见 `--help`。

## 前置条件

1. canvas-agent 在本机运行（默认 `http://127.0.0.1:17371`）。
2. infinite-canvas web 在本机运行（默认 `http://localhost:3000`）。
3. 浏览器已打开某个画布页，并带 `?agentUrl=...&agentToken=...` 参数完成自动连接。

## 典型用法

```powershell
python canvas-bridge/spike_canvas_push.py --health
python canvas-bridge/spike_canvas_push.py --push-batch tests/fixtures/external_workspace_batch_manifest.fixture.json
python canvas-bridge/spike_canvas_push.py --stress 300
python canvas-bridge/spike_canvas_push.py --status-demo
python canvas-bridge/spike_canvas_push.py --image-url http://127.0.0.1:8801/spike.svg
python canvas-bridge/spike_canvas_push.py --get-state --save-layout <路径>
python canvas-bridge/spike_canvas_push.py --clear-mine
```

## 状态

阶段 0 技术 Spike 原型。阶段 1（只读画布）验收前不应作为常规工具使用。
