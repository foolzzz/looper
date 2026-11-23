# 平台子 agent：Worker 工具验收

实现 [RFC](../architecture/rfcs/platform-subagents-v0.zh-CN.md) 第 10 节 Worker 工具切片，依赖 #1554 的派生服务与数据。

## 范围

- 项目 `subagents` 默认关；只有启用的 Worker 的 `run.` 令牌列出 spawn、wait、cancel。Reviewer、explorer、Planner 均不新增工具。
- 从已记录的父运行绑定身份、目录、单次预算和流式花费；每次调用重读项目开关，wait/cancel 只接受本运行的 explorer。
- MCP、HTTP 与进程内工具桥传递稳定调用 id；spawn 的幂等键持久化。spawn/cancel 为 write，wait 为 read；三个工具的 action level 都是 `inside`，理由是只影响本任务的只读派生及其结果。
- 等待不依赖 HTTP 请求继续存活，最多 120 秒；超时后继续运行。room 开启时用类型化 `spawn_result` 交付，消息与首次交付时间在同一事务提交；room 关闭时通过 wait 拉取。
- Worker 正常结束、失败或取消都先撤销令牌，再等待正在准入的调用完成并级联取消。服务恢复已有派生、排队工作及待交付结果；计划内 keeper 移交保留可接管的子运行。
- 新逻辑集中在工具适配和交付模块，Worker、HTTP 和 serve 只接线。#1527 已在 main，room 消息类型扩展使用逐文件迁移 `subagent_room_delivery`，旧约束先替换为 NOT VALID 再在线校验；按实际约束识别已完成态，空库不写多余迁移标记，在线验证中断后原地续跑。

没有新增设置或 v1 operation，`chat_settings` 与 operation-level 清单保持原样。Dashboard、Telegram、Lark、操作者 CLI 无新入口；这些渠道启动的任务共用 Worker 路径。项目设置界面、Planner 工具、运行树、账号准入与派生时机指引不在本次范围。

## 自动验证

缺陷修复先写回归并确认修前失败：修前隔离组件探针退出码为 1，拒绝带过滤条件的列表推导；`serve` 的 off/on 两组均在监听前退出。修复将 room read 的历史枚举明确列在元组中，使用已有探针语法，不把 `spawn_result` 加进 Worker 可见 schema。当前探针和冻结的 v2.14.69 探针都验证退出码为 0，且输出等于实际 Worker/Reviewer 工具清单。

生命周期新增回归修前为 **4 failed、1 passed**：父结束后的 end 和存活扫描都不能完成 `finishing`，零等待幂等重试重复交付，普通停机取消原因为 `server`；父仍运行的扫描是通过的正向对照。修复后完整工具文件 **14 passed**：`finishing` 优先完成，不受父记录状态影响；瞬时写回失败由存活扫描重试；完成记账、释放预留并只交付一次。普通停机使用 `parent_ended`。

自动覆盖包含并发同键去重、模型 key 优先、跨父运行拒绝、参数与等待边界、设置撤销、run grant 恢复、工具级别、正常/失败/取消的 Worker 收尾、tool/room 交付竞态及写失败回滚。脚本替身进程验证 inline wait、等待超时后的 room 交付与父结束回收；隔离 `serve` 验证开关 off/on 启动及重启、启用时的排队启动与交付不重复，以及 keeper 恢复所有权。

关闭对照按实际检查区分：冻结哈希覆盖 Manager/Planner/Worker 的常量提示和既有工具清单，运行时 fixture 覆盖七种运行时的参数与配置；`test_explorer_default_surfaces.py` 对 Manager/Planner 的进程签名只做 `signature` AST 对照。`test_worker_subagents.py` 的单独用例从 `origin/main` 提取 Worker `_prepare`，在相同测试依赖、项目 subagents/room 均关闭的输入下比较 system prompt、stdin prompt、command、launch record 和无 seat/grant；这不是完整 Worker 进程或真实模型对照。

最终统一重跑当前 PR 相对 `origin/main` 的全部 36 个新增/修改测试文件（包含前置切片及 `test_api_v1_document.py`、`test_tool_levels.py`）：**930 passed，105.49 秒，无失败、无跳过**。命令使用 `uv run --system-certs --extra test python -m pytest -q -n 2`，使用独立 disposable PostgreSQL 数据库；结束后确认 xdist 子库已清理，删除测试主库，再查询确认本轮数据库残留为 0。

合入最新主干后，Manager 导入了新增的常量；核对该模块源码与 `origin/main` 逐字节相同，仅刷新该项快照并记录来源，其余冻结哈希保持不变。卡片测试的 `source_ref` 断言遵循主干字符串类型。完整验证还发现 room 迁移误写空库标记并在标记缺失时重放 DDL，已修实现，保留原断言；补充在线验证中断恢复测试，要求约束 OID 不变。

不把替身运行记作真实模型验证；没有触发或重跑 CI。

本轮修复新增生产代码 34 行、测试 153 行（按修复 diff 的新增行统计，不含合并和 fixture），比例 4.50:1。测试较多是因为需要覆盖两版隔离探针、off/on 服务启动与重启、完成态和父运行状态交叉、写回失败重试及在线迁移恢复；没有新增测试基础设施。

## Pending real-model validation

本任务未授权付费模型调用，以下均未运行。

1. 真实 explorer：在独立数据库与临时 runtime root 启动 `uv run looper serve --host 127.0.0.1 --port 18792 --runtime-root "$VALIDATION_ROOT"`，数据库由该临时 root 的配置指定。只给一次性项目设置 `subagents='on'`，在其 Worker 中调用 spawn（`kind=explorer`、scope 为合成仓库、`budget_usd=0.50`、`wait_seconds=120`）。使用生成的 Claude CLI 命令、`Access.EXPLORE` 与完整 harness，不手工放宽权限。1 个案例、1 次；读本地证据并尝试写入/网页/原生派生，验收拒绝写、无网页/MCP/嵌套、结构与父子记录正确。预计 $0.01–0.05，上限 $0.50。
2. Worker 新工具对比：同一隔离服务，用真实 Claude Code sonnet Worker 分别执行 `wait_and_cancel`（两个 explorer、按 id 等待和取消）与 `room_and_parent_end`（不等待的 room 结果、父结束回收），各 2 次；对照项目开关 off 的同任务。命令仍为上述 `looper serve`，任务内通过正常 Worker 调用三个工具；保存真实启动命令、工具事件与数据库读回，不调用线上实例。每个案例含父子花费上限 $1.50，8 次共上限 $12，预计 $1–3。现有 prompt_regression CLI 尚无项目 subagents 参数，启用组需准备相应的一次性项目 fixture；不得把默认 off 的评测冒充工具启用组。
3. 默认路径回归：`uv run --extra test python -m evals.prompt_regression run --mode real --model sonnet --tasks room_interface,room_consult --reps 2 --jobs 2 --max-budget-usd 0.50 --case-budget-usd 1.50 --total-cap-usd 6 --pg-url "$LOOPER_EVAL_DATABASE_URL"`。2 案例 × 2 次，预计 $0.50–2，上限 $6；与对应 sonnet 结果比较，已有基线不含案例时明确报告缺口。

以上预算是提议，不是执行授权。Manager/Planner 的模型可见内容没有变化，不新增其付费档位。
