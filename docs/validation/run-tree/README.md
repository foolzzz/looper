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

关闭路径使用父分支 execution 快照与工具列表指纹对照；项目 API/export 省略 NULL 字段，旧配置导入新项目保持 NULL。成本条目以实际 explorer 运行数为条件，零运行份额与无份额的 HTML 相同；Agents 无 explorer 时 HTML 相同。Manager 工具列表与 schema 直接比较完整指纹，不剔除新增字段；关闭时 compact view 与父分支序列化字节一致。Manager 项目工具拒绝五项子 agent 设置，且不写入数据库。

撤回 Manager 设置入口前，回归测试复现 7 failed、18 passed；撤回后，7 个定向后端测试文件共 455 passed（`-n 2`，包括 `test_api_v1_document.py` 与 `test_tool_levels.py`），无跳过项。使用独立 PostgreSQL，根数据库与 xdist 数据库已删除并验证不存在。Manager 的 `TOOLS`、`_project_action`、`_confirming_project` 源码与父分支逐字节一致，完整工具指纹未更新。此前前端 5 个测试文件共 43 passed，`npm run build` 通过，保留既有的大包体积提示；此次未改前端，未重跑浏览器。新增生产代码 505 行、测试 495 行（不含 fixture），比例 0.98:1。未运行完整测试集，未触发或重跑 CI。

## 入口与验证边界

首次启用走 Dashboard 项目设置表单和项目管理 API。Manager 项目工具不新增参数或确认处理；从聊天修改子 agent 设置留给后续 PR，并在该 PR 做 Manager 对话对比。本次未调用真实模型，也无待执行的 Manager 对话评测。

Dashboard 与生成 CLI 使用同一操作 schema；Lark/Telegram 不增加子 agent 设置入口，Manager 工具保持原样。`runs.tree` 是 operator-only 读取。`ops/tasks/get` 仅在有 explorer 运行时增加 `cost_by_role`，保留原 `cost_usd`。基线未公开操作者单节点取消操作，本次没有取消按钮。
