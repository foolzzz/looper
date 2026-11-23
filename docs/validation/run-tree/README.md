# 运行树与项目子 agent 设置验收

本次验证只使用一次性数据库、临时 runtime root 和空闲端口；服务由本分支的 `looper serve` 启动。运行时探测使用替身，未调用真实模型、未启动实际 explorer，未连接外部消息渠道。SQL 合成数据用于验证派生服务基线上的读取与界面，不代表 Worker/Planner 的工具已接线。

通过新表单将项目 `subagents` 从 NULL 改为 `on`，保存后经项目列表 API 读回。任务包含 Planner、Worker、Reviewer、advisor、room consult 和两个 explorer；其中一个回答，另一个以 `parent_ended` 取消且摘要未验证。包含订阅等价成本、API 成本及一个未定价运行；Worker 的一次等待由 tool-use/tool-result 事件配对。

已验证：`runs.tree` 返回全部 7 个节点及等待区间；三档宽度无页面横向溢出，390 px 使用时间列表，另外两档显示时间线；Agents 按父运行列出 explorer，Pipeline 只显示计数；空任务显示 `No runs yet`，模拟读取失败显示带原因的 alert；浏览器未捕获页面异常。

| 视口宽度 | 运行树 | 项目设置 |
| --- | --- | --- |
| 1280 | [截图](run-tree-1280.png) | [截图](project-settings-1280.png) |
| 768 | [截图](run-tree-768.png) | [截图](project-settings-768.png) |
| 390 | [截图](run-tree-390.png) | [截图](project-settings-390.png) |

项目设置截图只截新增区块，排除不相关的项目配置；运行树保留任务导航，展示时间线或窄屏列表及树的顶部。未知成本、取消原因和未验证摘要另由定向组件与数据库测试检查。运行树查询只投影展示字段，不加载提示词或工具结果正文。

关闭路径使用父分支 execution 快照与工具列表指纹对照；项目 API/export 省略 NULL 字段，旧配置导入新项目保持 NULL。成本条目以实际 explorer 运行数为条件，零运行份额与无份额的 HTML 相同；Agents 无 explorer 时 HTML 相同。Manager 代码层两轮确认已通过测试。

合入最新父分支后的定向结果：后端 447 passed（`-n 2`，本次新增/修改测试文件，加 `test_api_v1_document.py` 与 `test_tool_levels.py`）；前端 5 个测试文件共 43 passed；`npm run build` 通过，保留既有的大包体积提示。所有数据库测试均连接独立 PostgreSQL，无跳过项。新增生产代码 536 行、测试 513 行（不含 fixture），比例 0.96:1。未运行完整测试集，未触发或重跑 CI。

## 待验证的模型可见变化

基线 `looper_projects` 未提供 advisor 或 subagents 设置参数。为交付首次从对话开启的入口，本次保持旧工具描述和旧属性不变，增加五个 schema 属性；因此不能宣称 Manager schema 在关闭时逐字节不变。指纹测试只剔除这五个已列明的增量，再与父分支比较。Planner/Worker 提示词、契约与工具列表保持原指纹。

未经批准未运行真实模型。建议批准后先用现有 `config_tools,status,task_cost` 对话做回归：

```sh
uv run --extra test python -m evals.prompt_regression dialogs --mode real --model sonnet \
  --reps 2 --dialogs config_tools,status,task_cost --jobs 2 --max-budget-usd 1.0 \
  --case-budget-usd 1.0 --total-cap-usd 6
uv run --extra test python -m evals.prompt_regression dialogs --mode real --model sonnet \
  --manager-model opus --manager-effort xhigh --reps 1 --dialogs config_tools,status,task_cost \
  --jobs 2 --max-budget-usd 3.0 --case-budget-usd 3.0 --total-cap-usd 9
```

另在同样隔离环境补三组项目对话：开启后先询问确认且未确认不写；拒绝确认后保持关闭；设置预算或数量超范围时返回校验错误。每组 Sonnet 两次、Opus xhigh 一次，建议总费用上限分别 6 和 9 USD。现有命令只覆盖旧对话回归，不覆盖这三组尚未加入 harness 的用例；全部建议的合计费用上限 30 USD，并非已发生费用。

Dashboard 与生成 CLI 使用同一操作 schema；Lark/Telegram 无新命令，沿用 Manager 项目工具及其确认流程。`ops/tasks/get` 仅在有 explorer 运行时增加 `cost_by_role`，保留原 `cost_usd`。基线未公开操作者单节点取消操作，本次没有取消按钮。
