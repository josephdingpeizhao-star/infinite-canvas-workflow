# codex-dev 风格母版最小支持设计

## 目标

让真实批次 `shuiping_20260712` 在已有产品身份档案之后，通过原画布运行台、三段门禁和可替换执行器边界生成一份正式《风格母版》。本轮只增加 `style_master` 能力，不生成图片，不进入角度入库或其他后续步骤。

## 用户可感知行为

- `D:\onedrive\OneDrive\Desktop\shuiping\风格参考图.png` 被复制到 manifest 已声明的 `inputs\style_refs` 目录；原文件保留不动。
- 画布“风格参考图”节点显示 1 个输入，运行台允许执行 `style_master`。
- 执行成功后生成 `artifacts\style_master\style_master.json`，画布“风格母版提取”和“风格母版”节点显示成功，事件日志追加开始与成功记录。
- 产品身份档案、白底图、画布命令、门禁和后续工作流保持不变。

## 输入边界

- 临时目录共有 13 个文件，其中 12 张 JPG 是当前批次原白底图；只有明确命名的 `风格参考图.png` 作为风格输入。
- 导入采用保留源文件的复制方式，不移动、不删除临时目录内容。
- 若目标存在同名同内容文件，导入视为已完成；若同名但内容不同，停止并报错，绝不覆盖。
- 适配器只从 manifest 的 `inputs.style_reference_images` 读取受支持图片，不扫描临时目录。

## 执行器边界

```text
画布运行台
  -> 原三段门禁与真实路由
  -> ExecutionRequest(step="style_master")
  -> ExecutorRegistry
  -> codex-dev
  -> canvas-agent 现有 Codex HTTP/SSE 线程
  -> ExecutionResult
  -> 事件日志与画布状态投影
```

- `codex-dev` 从仅支持 `identity` 扩展为仅支持 `identity` 和 `style_master`；其他步骤继续安全拒绝。
- Codex 专属线程、附件、请求、SSE、返回解析和脱敏错误仍全部留在适配器内部。
- `run_controller`、画布命令解析、三段门禁、事件日志不增加 Codex 专属分支。
- 不修改 Infinite Canvas fork，不新增第三方依赖，不修改 manifest、schema 或 scripts。

## 风格母版任务

- 完整加载 `.agents/skills/style-master-extractor/SKILL.md` 和 required reference `references/反向提取风格母版提示词.txt`。
- 同时读取已经生成的产品身份档案作为上位约束，防止风格规则覆盖产品结构、比例、颜色、材质、图案、配件关系或真实尺寸。
- Codex 只返回 JSON；适配器强制设置 `product_id`、`artifact_type=style_master` 和实际 `source_references`。
- `style_master` 对象必须覆盖 required reference 的主要栏目，包括整体视觉定位、版式、背景、色调、光线、主体呈现、道具、文字、留白、情绪、可复用规则、风格保真增强、禁止事项和最终精简版。
- 返回中若出现产品身份档案、角度表、变量配置、最终提示词、图片或 QC 等越界产物，写入前拒绝。
- 结果通过既有 `schemas/style_master.schema.json` 校验；本轮不改变 schema。

## 写入与失败处理

- 输出固定为 manifest 声明的 `artifacts.style_master` 下的 `style_master.json`，并验证路径仍在 `workspace.artifacts_root` 内。
- 使用临时文件加原子落盘；已有正式风格母版绝不覆盖。
- 缺少参考图、缺少产品身份档案、规则文件缺失、canvas-agent 连接或线程失败、空回复、非法 JSON、越界字段或结构缺失，统一收敛为脱敏的 `ExecutorExecutionError`。
- 事件日志不记录 token、完整提示词、图片正文、Codex 原始错误正文或产品隐私数据。

## 测试与验收

1. 先写失败测试，确认 `style_master` 当前被拒绝。
2. 使用假传输验证规则加载、参考图附件、身份档案约束、JSON 校验、原子写入和统一错误；离线测试不启动真实 Codex、不访问网络。
3. 回归验证 `identity`、`demo`、`openai-image` 和原控制器行为不变。
4. 全仓测试、Python 编译检查、CLI 帮助、schema 校验和 `git diff --check` 全部通过。
5. 离线验证通过后，临时启用真实执行开关，通过画布运行台执行一次 `style_master`；执行结束立即关闭临时服务和开关。
6. 最终核对风格母版文件、事件账本、当前路由和画布投影；确认没有生成图片，也没有修改 fork、scripts、schemas 或 manifest。

## 不包含

- 角度入库、主图变量配置、详情图变量配置、最终提示词、图片生成和 QC。
- 风格母版的画布业务卡片排版优化。
- Codex 作为默认执行器或生产永久依赖。
- demo、openai-image 或现有 identity 行为的重设计。
