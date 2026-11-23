# Explorer 账号准入与等待借位

默认关闭，沿用 `engine.admission.enabled` 和项目 `subagents`，无新设置或迁移。

真实服务验收同时发现并修复派生服务的前置状态校验：任务运行时会使用 `planning`、`executing` 等阶段，原先只接受字面值 `running` 会拒绝正常 Worker，并误取消 Planner 的排队子运行。预留、启动及扫描现在统一使用 `tasks.ACTIVE_STATUSES`；已结束任务仍被拒绝。这项修复在项目已启用子 agent 时生效，不依赖账号准入开关；其余关闭路径按下述固定 fixture 保持一致。

## 行为与边界

- explorer 先只读检查任务的 `subagent` 并发类别，再在父运行的车道等待账号，最后在任务锁中复查并认领。账号等待不持有任务锁；槽位持续到子进程结束。
- 阻塞的 spawn/wait 只向同账号准入队列中最靠前的一个 explorer 转借父槽位，沿用车道优先级、老化和同车道到达顺序。子运行先结束时直接归还；等待先返回时，子运行保留普通槽位，父运行按原到达顺序重新准入后才返回工具结果。
- 新派生的身份异步生成，等待循环先等当前正在准备的派生都有身份或退出，再登记借位，仍使用原截止时间。零等待、已在运行的 explorer、冻结账号不借位。Planner 通过当前回合寻找槽位，可以等待前一回合派出的 explorer；Planner 回合准入本身另行交付。
- 冻结的 queued 结果增加 `held: quota`、`resets_at`（供应商重置时间，未知为 null）。按调用缓存 Profile 与账号查询，开放或关闭准入时没有新增结果键。
- 活动使用 `subagent_wait`，前缀为 `Explorer <id>:`。取消账号等待会结束对应任务并保留抢到 CAS 的取消原因；条件等待用 `asyncio.timeout`，避免 Python 3.11 的 `wait_for` 在唤醒与取消竞争时吞掉取消。保留运行在新进程登记，收集结束时释放；旧进程分离仅释放本地槽位，不报告探测结束。
- 准入关闭时不增加任务预检查、账号查询、控制器锁或等待栈。固定基线 `0d9e121c1eb124282c33b5e8352164f30d212425` 的 fixture 逐项冻结工具结果、命令参数、系统提示、记录元数据、派生行字段和直接 SQL 查询次数。

## 入口

Dashboard 沿用任务活动、账号等待数及运行树。现有 `runs.tree` 只以运行记录为节点，尚未认领的 queued explorer 没有记录，因此排队状态通过工具结果、活动和账号等待数验证，不声称运行树已有 queued 节点。运行树数据和 CLI 输出未增加字段。Lark/Telegram 无新增文本；冻结通知沿用账号层。

## 验证

定向检查包含 `tests/test_admission*.py tests/test_subagent*.py tests/test_explorer*.py tests/test_api_v1_document.py tests/test_tool_levels.py`，使用 Python 3.11、两个测试进程和一次性 PostgreSQL。另用固定基线的三个原始模块重新执行关闭路径观测，12 项均与录制 fixture 匹配；重启扫描器读回冻结状态后，验证 explorer 保持原车道排队、到期探测并完成。

新增服务测试使用独立 `looper serve`、空闲端口、临时 runtime root 和协议替身：Worker 通过运行令牌调用真实 HTTP 工具，派两个 explorer，cap=1 时一个借位运行、一个排队；429 后等待返回额度挂起，清除冻结后继续。可设置 `LOOPER_ADMISSION_SCREENSHOT_DIR` 保存 1280/390 的运行树和活动截图。未调用真实模型或外部消息账号。

## Pending real-model validation

未执行，需另行授权：在一次性数据库和 runtime root 上启动 `uv run --system-certs --python 3.11 looper serve --host 127.0.0.1 --port 18794 --runtime-root "$VALIDATION_ROOT"`，启用项目 explorer 与账号准入，仅 Worker 使用真实 Claude sonnet Profile，explorer 账号使用替身冻结信号。通过正常任务入口运行一个“派 explorer、阻塞等待、读取 `held: quota`”案例，1 次，检查父运行能理解挂起结果。预计 $0.05–0.15，单次 Worker 上限 $0.50。现有 prompt regression CLI 没有这个启用组案例，不把默认关闭的评测当作覆盖。
