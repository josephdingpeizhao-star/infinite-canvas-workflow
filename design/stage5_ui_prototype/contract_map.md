# 界面动作契约清单（阶段 5A）

本清单用于防止“界面看起来能做，但后台实际上没有”这种脱节。

统计口径：49 个唯一界面动作全部登记；30 个由现有事实来源或白名单通道覆盖，12 个只是本地页面跳转/折叠等零写入行为，7 个需要新契约提案。重复出现在多个页面的全局导航只登记一次。

## 图例

- **现有覆盖**：已经有可读取的事实字段或允许的写入/执行通道。
- **本地行为**：只在原型页面内跳转、展开或返回，不触碰任何业务数据。
- **新契约提案**：现有白名单覆盖不了，必须先补后台能力，不能直接上线。

## 对账表

| ID | 屏幕 | 界面动作 | 类型 | 事实或通道 | 结论 |
|---|---|---|---|---|---|
| C01 | 全局 | 返回原型导航页 | 导航 | 本地相对链接 | 本地行为 |
| C02 | 全局/S1/S9 | 打开批次列表 | 读 | `detect_current_state.discover_product_ids()` + `inspect_batch()` | 现有覆盖 |
| C03 | 全局/S1 | 打开批次总览 | 读 | `state_reader.read_batch_route()` 返回整批路由 | 现有覆盖 |
| C04 | 全局 | 打开 QC 结果页 | 读 | `qc_report.json` | 现有覆盖 |
| C05 | 全局 | 打开完成/关账页 | 读 | 路由 `outputs`、`artifacts.qc_reports` | 现有覆盖 |
| C06 | S1 | 进入新建批次向导 | 导航 | 本地相对链接 | 本地行为 |
| C07 | S1 | 继续处理当前批次 | 导航 | 本地相对链接到 S3 | 本地行为 |
| C08 | S2 | 填写产品编号 | 输入 | `build_batch_manifest.py --product-id` | 现有覆盖 |
| C09 | S2 | 填写商品品类 | 输入 | `user_confirmed_facts.product_type` | 现有覆盖 |
| C10 | S2 | 填写已确认高度 | 输入 | `user_confirmed_facts.height_cm`，正整数 | 现有覆盖 |
| C11 | S2 | 确认主图手持 2 张 | 输入 | `user_confirmed_facts.handheld_main=2` | 现有覆盖 |
| C12 | S2 | 确认详情图手持 1 张 | 输入 | `user_confirmed_facts.handheld_detail=1` | 现有覆盖 |
| C13 | S2 | 选择是否允许清水 | 输入 | `user_confirmed_facts.allow_clear_water` | 现有覆盖 |
| C14 | S2 | 选择是否禁止倾倒与加热 | 输入 | `user_confirmed_facts.forbid_pouring_and_heating` | 现有覆盖 |
| C15 | S2 | 选择 D 缺失时是否不补拍 | 输入 | `user_confirmed_facts.missing_d_no_retake` | 现有覆盖 |
| C16 | S2 | 选择白底图资料 | 写 | 现有 manifest 只有路径事实，没有安全的浏览器上传/复制入口 | [新契约提案 NC-01](backend_gaps.md#gap-03) |
| C17 | S2 | 选择风格参考图 | 写 | 现有 manifest 只有路径事实，没有安全的浏览器上传/复制入口 | [新契约提案 NC-01](backend_gaps.md#gap-03) |
| C18 | S2 | 创建批次并建立演示工作区 | 写 | 目前只能经建批脚本创建，未进入 `requested_outputs`/`notes` 白名单 | [新契约提案 NC-01](backend_gaps.md#gap-03) |
| C19 | S1 | 刷新批次列表 | 读 | 重新执行批次发现与 `inspect_batch()` | 现有覆盖 |
| C20 | S3 | 进入产出声明页 | 导航 | 本地相对链接 | 本地行为 |
| C21 | S3 | 刷新进度 | 读 | `current_stage`、`next_skill`、`missing_required_artifacts`、`blocked_reasons` | 现有覆盖 |
| C22 | S3 | 查看现有产物明细 | 读 | `available_artifacts` 与各 `artifacts/outputs` 的 `files`、`file_count` | 现有覆盖 |
| C23 | S3 | 开始系统建议的下一步 | 执行 | `run: next` | 现有覆盖 |
| C24 | S4 | 勾选主图 | 写 | `requested_outputs: main` | 现有覆盖 |
| C25 | S4 | 勾选详情图 | 写 | `requested_outputs: detail` | 现有覆盖 |
| C26 | S4 | 勾选最终提示词 | 写 | `requested_outputs: final_prompts` | 现有覆盖 |
| C27 | S4 | 勾选质检报告 | 写 | `requested_outputs: qc_reports` | 现有覆盖 |
| C28 | S4 | 填写批次备注 | 写 | `notes` | 现有覆盖 |
| C29 | S4 | 保存产出声明与备注 | 写 | `batch_editor` 白名单解析 → 字段校验 → 路由干跑 → 原子写回 | 现有覆盖 |
| C30 | S5 | 取消本次执行 | 导航 | 返回总览；不发送命令 | 本地行为 |
| C31 | S5 | 确认已理解费用风险 | 审批 | 现有命令没有服务端审批记录 | [新契约提案 NC-02](backend_gaps.md#gap-02) |
| C32 | S5 | 确认并开始生成图片 | 执行 | 费用确认后映射 `run: renders` | 现有覆盖 |
| C33 | S6 | 在原型中查看成功状态 | 导航 | 本地相对链接 | 本地行为 |
| C34 | S6 | 在原型中查看失败状态 | 导航 | 本地相对链接 | 本地行为 |
| C35 | S6 成功态 | 查看 QC 结果 | 读 | `qc_report.json` 与 `route.artifacts.qc_reports` | 现有覆盖 |
| C36 | S6 失败态 | 打开失败与求助页 | 导航 | 本地相对链接 | 本地行为 |
| C37 | S7 | 展开某张图片的问题组 | 读 | `issues[].affected_asset`、`severity`、`description`、`repair_targets` | 现有覆盖 |
| C38 | S7 | 对单条问题作人工裁定 | 写 | 当前 QC 报告是只读审计记录，没有人工裁定写入通道 | [新契约提案 NC-03](backend_gaps.md#gap-08) |
| C39 | S7 | 全部接受并正式关账 | 写 | 当前没有终审决策和关账状态的服务端接口 | [新契约提案 NC-03](backend_gaps.md#gap-08) |
| C40 | S7 | 暂不关账并返回总览 | 导航 | 本地相对链接；不写业务数据 | 本地行为 |
| C41 | S5/S8 | 展开技术详情 | 读 | S5 展示固定命令映射；S8 读取事件账本的 `event`、`step`、脱敏 `detail`、`ts` | 现有覆盖 |
| C42 | S8 | 查看联系管理员指引 | 展示 | 只显示联系指引，不自动发消息 | 本地行为 |
| C43 | S8 | 准备受控重试 | 执行 | 先进入 S5 同款确认卡，再映射 `retry: renders` | 现有覆盖 |
| C44 | S9 | 打开交付物保存位置 | 读 | manifest 声明的 `outputs.renders` 与 `artifacts.qc_reports` 路径 | 现有覆盖 |
| C45 | S9 | 下载全部交付物 | 写 | 当前没有安全打包、下载和完整性校验接口 | [新契约提案 NC-04](backend_gaps.md#gap-09) |
| C46 | S6 | 查看运行时间线 | 读 | `<pid>.events.jsonl` 追加式事件账本 | 现有覆盖 |
| C47 | S6 成功态/S9 | 查看生成文件清单 | 读 | `route.outputs.renders.files` | 现有覆盖 |
| C48 | 导航页 | 打开 README/契约/缺口/承载对比说明 | 导航 | 本地相对链接 | 本地行为 |
| C49 | 导航页/S1 | 跳到页面主要内容 | 无障碍导航 | 同页锚点，不读取或写入业务数据 | 本地行为 |

## 四项新契约提案

1. **NC-01 建批与资料接收**：对应 GAP-03；接收七项确认信息与资料选择，完成路径校验、重名保护和演示工作区创建。
2. **NC-02 审批确认**：对应 GAP-02；服务端记录谁、在何时、对哪一步完成了什么级别的确认，真实费用步骤必须二次确认。
3. **NC-03 人工终审与关账**：对应 GAP-08；逐条记录人工裁定，并以一次明确操作完成关账，QC 原报告保持原样。
4. **NC-04 交付打包下载**：对应 GAP-09；只打包 manifest 声明范围内的最终交付物，并返回文件数与校验结果。

## 不得误读

- 页面中的按钮不等于后台已经支持。
- 原型没有执行任何上述写入或命令。
- `requested_outputs`、`notes`、`run:`、`retry:` 之外的写操作，未落入新契约前一律不得固化。
