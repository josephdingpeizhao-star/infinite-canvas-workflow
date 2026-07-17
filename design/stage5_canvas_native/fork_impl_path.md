# 画布原生节点卡片实现路径（供 5B/5C 决策）

> **“独立轻量操作面板”方案已由用户于 2026-07-17 否决。现行承载方式：Infinite Canvas 画布原生节点卡片。**

本文件只给出未来实现路径，不修改 fork、不接后台、不执行任何真实流程。5A-R 的六幕 HTML 是可丢弃设计稿，不能直接上线。

## 1. 最小节点类型方案

建议 fork 只新增 **1个枚举节点类型**：

```text
CanvasNodeType.WorkflowCard = "workflow_card"
```

九种视觉卡片不应变成九个枚举类型。统一由 `WorkflowCard` 承载，再用 metadata 中的 `kind` 区分：

```text
wizard | batch | stage | output_declaration | confirmation |
run_progress | qc_issue | final_review | delivery
```

现有 Image 节点继续承载 K07 图片结果；Group 节点可继续承担视觉分组。这样既避免普通 Text 节点自带“生图”按钮，也不借用 Config 节点仅有目标连接点的特殊规则。

## 2. fork 新组件文件建议

全部作为新增文件，符合 fork“新增文件 + 登记锚点”纪律：

| 新文件 | 职责 |
|---|---|
| `web/src/components/canvas/canvas-workflow-card.tsx` | 统一入口，根据 `metadata.workflowCard.kind` 选择卡片呈现；只处理展示和本地交互 |
| `web/src/components/canvas/canvas-workflow-card-sections.tsx` | 共用标题、状态、字段、按钮、折叠详情、进度条和错误摘要 |
| `web/src/components/canvas/canvas-workflow-card-model.ts` | 纯函数校验和规范化 metadata；未知 kind 或不完整字段安全降级 |

若当前 web 测试工具可直接复用，再新增纯函数测试文件；不得为了单一组件先引入新依赖。

## 3. 预计 fork 锚点

预计 **11个锚点组、涉及7个既有文件**。实际实施时必须逐条登记到 `FORK_NOTES.md`，不能只登记一个笼统条目。

| 预计锚点 | 既有位置 | 最小改动 |
|---|---|---|
| A1 | `web/src/types/canvas.ts` · `CanvasNodeType` | 增加 `WorkflowCard` 枚举值 |
| A2 | `web/src/types/canvas.ts` · `CanvasNodeMetadata` | 增加版本化 `workflowCard` metadata 类型 |
| A3 | `web/src/constant/canvas.ts` · `NODE_DEFAULT_SIZE/NODE_SPECS` | 登记默认尺寸与空 metadata；保持 exhaustive Record 完整 |
| A4 | `web/src/components/canvas/canvas-node.tsx` · `nodeContentRenderers` | 增加 WorkflowCard → 新组件映射；沿用 Config 富UI注入的先例 |
| A5 | `web/src/components/canvas/canvas-node.tsx` · 连接点显示 | bridge-owned 卡片按 metadata 隐藏手工连接点，避免用户改写仓库拓扑 |
| A6 | `web/src/pages/canvas/project.tsx` · `renderNodeContent` | 只对 WorkflowCard 注入新组件；Config 现有路径保持不变 |
| A7 | `web/src/pages/canvas/project.tsx` · 节点点击/配置面板分支 | 点击 WorkflowCard 不打开图片/生成配置面板 |
| A8 | `web/src/pages/canvas/project.tsx` · 上下文菜单调用 | 根据 metadata 传入“不可复制/不可删除”的 bridge-owned 权限 |
| A9 | `web/src/components/canvas/canvas-context-menu.tsx` | 对锁定的 WorkflowCard 隐藏复制和删除；普通节点行为不变 |
| A10 | `canvas-agent/src/schemas.ts` · `nodeTypeSchema` | 允许 `workflow_card` 经现有 `canvas_apply_ops` 进入网页 |
| A11 | `canvas-agent/src/types.ts` · `CanvasNodeType` | 同步类型联合，保证 agent 快照不把新类型降级 |

### 明确不建议新增的锚点

- 不在画布工具栏增加“工作流卡片”按钮：这些卡片只能由 bridge 按仓库事实投影，不能由用户随手创建。
- 不把工作流动作塞进 Config 的模型、尺寸、生成按钮逻辑。
- 不修改普通 Image/Text/Video/Audio/Group 的既有行为。
- 不修改通用连线方向算法；WorkflowCard 默认具备左右语义，但 5B 阶段由 bridge 锁定手工改线。

## 4. canvas-bridge metadata 扩展

建议新增版本化结构，不把整份 manifest、QC报告或事件正文复制进画布：

```json
{
  "status": "idle",
  "workflowRef": {
    "graph_id": "kettle_ecommerce_image_v1",
    "node_id": "stage_product_identity",
    "product_id": "demo_thermos_20260717"
  },
  "workflowCard": {
    "version": 1,
    "cardId": "K03",
    "kind": "stage",
    "eyebrow": "第 2 步，共 9 步",
    "title": "产品身份档案",
    "summary": "只整理已确认事实和图片中可见的信息。",
    "stateLabel": "下一步",
    "progress": { "current": 1, "total": 9 },
    "facts": [
      { "label": "费用", "value": "无" },
      { "label": "阻塞", "value": "无" }
    ],
    "actions": [
      {
        "id": "start-next",
        "label": "开始下一步",
        "contractRef": "C23",
        "command": "run: next",
        "tone": "primary",
        "enabled": true
      }
    ],
    "locks": {
      "connections": true,
      "duplicate": true,
      "delete": true
    }
  }
}
```

### 数据边界

- `projector.py` 只投影 route、workflow graph 和安全摘要；不产生业务事实。
- `batch_editor.py` 继续只写 `requested_outputs`/`notes`，K04 只是界面换装。
- `run_controller.py` 继续只接受 `run:`/`retry:`，K03/K05/K06 按钮不新增命令词汇。
- K08 只投影 issue id、严重程度、人话说明和关联资产；机器 QC 原报告仍只读。
- 不在 metadata 放密钥、完整提示词、模型原始响应、未脱敏错误或可恢复的敏感片段。
- 大列表采用“一条问题一个节点”或分页摘要，不把19条问题塞进单个超大 metadata。

## 5. 主仓库预计触点（5B/5C，非本任务）

| 位置 | 未来职责 |
|---|---|
| `canvas-bridge/projector.py` | 把现有 `wf:` 阶段/门禁节点投影为 `workflow_card` 并提供安全 metadata |
| `canvas-bridge/batch_editor.py` | 生成 K04 metadata，写入通道仍为原三段门禁 |
| `canvas-bridge/run_controller.py` | 生成 K03/K05/K06动作描述，命令解析与路由判断不变 |
| `canvas-bridge/state_reader.py` | 继续提供 route；不为UI另造第二套状态判断 |
| 新增独立 projector helper | 将 route/事件/QC转换成卡片 view model，保持纯函数、可测试 |

## 6. 上游同步影响

- 风险最高的冲突点是 `canvas-node.tsx` 与 `pages/canvas/project.tsx`，因为它们是上游活跃的核心画布文件。
- 一个枚举项会触发两个 exhaustive Record（默认规格、内容渲染器）；这是有价值的编译期提醒，不应改成松散字符串绕过。
- agent 的 schema/type 两个锚点必须与 web 同步，否则新节点会被拒绝或降级为 Text。
- 不加工具栏入口、不改变 Config 和生成链，可把同步影响控制在11个明确锚点组。
- 每次同步上游后，至少复核：枚举、默认规格、渲染映射、锁定连接点、点击行为、上下文菜单、agent schema/type，以及主仓库 bridge 投影测试。

## 7. 5B 建议范围

5B 只在 demo 沙盒实现三种卡，验证“读、受控写、受保护执行”三个核心通道：

1. **K03 阶段卡**：先替换现有 `wf:` 阶段/门禁 Text 节点，验证状态、连线、布局和缩放。
2. **K04 产出声明卡**：接现有 `requested_outputs`/`notes` 白名单，验证卡片内编辑仍走原三段门禁。
3. **K05 费用确认卡**：先接 demo 执行器，只验证显著确认与 `run: next` 门禁，不产生真实费用。

5B 不做 K01 建批（依赖 NC-01）、K08/K09 终审关账（依赖 NC-03）、下载（依赖 NC-04）或任何真实图片调用。三卡 demo 通过后，5C 再扩展向导、运行进度、QC问题与交付态。
