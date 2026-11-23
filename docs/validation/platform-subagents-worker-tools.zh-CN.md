# 平台子 agent：Worker 工具验收

实现 [RFC](../architecture/rfcs/platform-subagents-v0.zh-CN.md) 第 10 节 Worker 工具切片，依赖 #1554 的派生服务与数据。

## 范围

- 项目 `subagents` 默认关；只有启用的 Worker 的 `run.` 令牌列出 spawn、wait、cancel。Reviewer、explorer、Planner 均不新增工具。
- 从已记录的父运行绑定身份、目录、单次预算和流式花费；每次调用重读项目开关，wait/cancel 只接受本运行的 explorer。
- MCP、HTTP 与进程内工具桥传递稳定调用 id；spawn 的幂等键持久化。spawn/cancel 为 write，wait 为 read；三个工具的 action level 都是 `inside`，理由是只影响本任务的只读派生及其结果。
- 等待不依赖 HTTP 请求继续存活，最多 120 秒；超时后继续运行。room 开启时用类型化 `spawn_result` 交付，消息与首次交付时间在同一事务提交；room 关闭时通过 wait 拉取。
- Worker 正常结束、失败或取消都先撤销令牌，再等待正在准入的调用完成并级联取消。服务恢复已有派生、排队工作及待交付结果；计划内 keeper 移交保留可接管的子运行。
- 新逻辑集中在工具适配和交付模块，Worker、HTTP 和 serve 只接线。#1527 已在 main，room 消息类型扩展使用逐文件迁移 `subagent_room_delivery`，旧约束先替换为 NOT VALID 再在线校验。

没有新增设置或 v1 operation，`chat_settings` 与 operation-level 清单保持原样。Dashboard、Telegram、Lark、操作者 CLI 无新入口；这些渠道启动的任务共用 Worker 路径。项目设置界面、Planner 工具、运行树、账号准入与派生时机指引不在本次范围。

## 自动验证

测试先于实现编写；初版代码完成后统一执行本次新增/修改测试，以及 `test_api_v1_document.py` 和 `test_tool_levels.py`，一次 pytest、`-n 2`，使用独立 disposable PostgreSQL 数据库，结束后删除。

通过的覆盖：并发同键去重、模型 key 优先、跨父运行拒绝、参数与等待边界、设置撤销、run grant 恢复、工具级别、正常/失败/取消的 Worker 收尾、tool/room 交付竞态及写失败回滚。真实替身进程验证了 inline wait、等待超时后的 room 交付与父结束回收。

关闭对照包含 Manager/Planner/Worker 的提示与工具列表、七种运行时的既有启动参数与配置、Manager/Planner 的进程签名构成，以及与最新 `origin/main` 同输入执行 Worker `_prepare` 后的提示、stdin、命令和 launch 记录。

唯一一次测试结果：**523 passed、1 failed，47.16 秒，无跳过**。失败是新增 `serve` 用例的准备检查：复用的旧 helper 只接受旧本地端口和固定密码，与本轮独立测试库不匹配，服务尚未启动。已改为明确检查本任务的独立端口或 CI 端口，不把口令写死；遵守一次测试限制，修正后未重跑，`serve` 排队和重启交付仍未验证。独立数据库及 xdist 子库已删除。

随后静态检查发现，既有 Worker keeper 扫描会清理 explorer 的目录，并把其未结束记录关掉。已按持久化 explorer 子记录把对应目录与记录交给 SpawnService 恢复，关闭路径仍传相同的 Worker 参数；补充的两项所有权回归未运行。该启动接线需要后续执行测试确认，不能视为通过。

不把替身运行记作真实模型验证；没有触发或重跑 CI。

本切片新增行数：生产代码 542 行、测试 791 行（不含 fixture），比例 1.46:1。

## Pending real-model validation

本任务未授权付费模型调用，以下均未运行。

1. 真实 explorer：在独立数据库与临时 runtime root 启动 `uv run looper serve --host 127.0.0.1 --port 18792 --runtime-root "$VALIDATION_ROOT"`，数据库由该临时 root 的配置指定。只给一次性项目设置 `subagents='on'`，在其 Worker 中调用 spawn（`kind=explorer`、scope 为合成仓库、`budget_usd=0.50`、`wait_seconds=120`）。使用生成的 Claude CLI 命令、`Access.EXPLORE` 与完整 harness，不手工放宽权限。1 个案例、1 次；读本地证据并尝试写入/网页/原生派生，验收拒绝写、无网页/MCP/嵌套、结构与父子记录正确。预计 $0.01–0.05，上限 $0.50。
2. Worker 新工具对比：同一隔离服务，用真实 Claude Code sonnet Worker 分别执行 `wait_and_cancel`（两个 explorer、按 id 等待和取消）与 `room_and_parent_end`（不等待的 room 结果、父结束回收），各 2 次；对照项目开关 off 的同任务。命令仍为上述 `looper serve`，任务内通过正常 Worker 调用三个工具；保存真实启动命令、工具事件与数据库读回，不调用线上实例。每个案例含父子花费上限 $1.50，8 次共上限 $12，预计 $1–3。现有 prompt_regression CLI 尚无项目 subagents 参数，启用组需准备相应的一次性项目 fixture；不得把默认 off 的评测冒充工具启用组。
3. 默认路径回归：`uv run --extra test python -m evals.prompt_regression run --mode real --model sonnet --tasks room_interface,room_consult --reps 2 --jobs 2 --max-budget-usd 0.50 --case-budget-usd 1.50 --total-cap-usd 6 --pg-url "$LOOPER_EVAL_DATABASE_URL"`。2 案例 × 2 次，预计 $0.50–2，上限 $6；与对应 sonnet 结果比较，已有基线不含案例时明确报告缺口。

以上预算是提议，不是执行授权。Manager/Planner 的模型可见内容没有变化，不新增其付费档位。
