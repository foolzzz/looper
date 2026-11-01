# RFC：热升级 v0（计划内重启不中断运行，只升级改了的组件）

- **状态：** 已批准（2026-10-30）。第 9 节的四个问题都按推荐答案执行；先做 S0，S1 在 S0 上线之后开工。
- **需求：** 维护者问：能不能在任务继续跑的情况下重启？把系统拆成几个组件，每次只升级改了的组件。请设计一个热升级方案。
- **相关：** [Agent SDK 运行器](agent-sdk-runner-v0.zh-CN.md)（运行器协议 0.1、generation、`session/load`）、[定时任务](scheduled-tasks-v0.zh-CN.md)（错过的触发怎么补）、[issue #909](https://github.com/foolzzz/looper/issues/909)（同一个库上跑两个 `looper serve` 不安全）、[开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 1 节第 8 条（合入后部署）和第 5.2 节、`docs/architecture/agent-system.md` 的「How an agent is stopped」和「Message Bus」两节。
- **核对基线：** `main` `6a3cbb94e`（v2.14.26），本文的代码行号已逐项核对。变更频率仍统计 2026-10-15（改名之后）到 2026-10-30 的 246 个发布 tag（v2.8.0 到 v2.14.25）。本文只读了代码和 git 历史，没有启动服务，也没有读运行中服务的数据库。内存数字来自本机 CPython 3.11 的一次性测量，启动耗时来自一次性库上升级检查的日志。
- **引用约定：** 路径相对仓库根，写成「文件:行」。
- **术语：**
  - **控制面**：`looper serve` 进程本身，包括 HTTP API 与控制台、总线、Manager/TaskRouter/Planner/WorkerPool 的逻辑、调度器和渠道。
  - **运行**：一次 Worker、Reviewer 或咨询的 agent 进程，可以是 CLI 子进程，也可以是运行器里的一个会话。
  - **看护进程（keeper）**：本文新增的小进程。每个运行一个，替控制面持有 agent 进程和它的 stdio，控制面重启时它不退出。
  - **排空（drain）**：计划内重启之前不再开始新的运行或回合，等已经开始的结束。
  - **接管（adopt）**：新控制面接上旧控制面留下、仍由看护进程持有的运行，从记下的日志偏移接着消费。
  - **升级计划**：部署脚本比较组件指纹后得出的四种结果之一：`NOOP`、`WEB_ONLY`、`CONTROL_PLANE`、`FULL`（第 7.2 节）。

## 1. 背景与目标

### 1.1 背景

`looper serve` 是一个 Python 进程（`looper/agents/serve.py:3-12`）。HTTP API 和控制台、总线、Manager、每个任务的 Planner、Worker/Reviewer 池、调度器、Lark 与 Telegram 渠道都在里面。agent 的运行时是它的子进程：Claude Code、Codex、OMP、Grok 的 CLI，Pi 和 DSH 的 Node 运行器，以及由它们再起的 MCP 桥。数据都在 PostgreSQL 里。

重启会打断所有正在跑的回合：Worker 池停下时结束全部运行（`looper/agents/tiers/worker.py:470-491`），TaskRouter 停掉所有 Planner（`looper/agents/tiers/task.py:174-184`）。计划还在进行的任务会在下次启动时续跑，但被打断的步骤要在新会话里从头再跑一遍（`looper/agents/tiers/planner.py:2515-2550`；Worker 每一步都是新会话，`looper/agents/tiers/worker.py:103`），已经花掉的钱白花了。所以目前的部署做法是等到没有进行中的任务再重启，最多等 1 小时。可常驻服务很少空闲（例如每分钟触发一次的定时任务、要跑很久的评审类任务），一次发布常常要等几个小时。

发布本身又很频繁。2026-10-15 到 10-30 的 16 天里打了 246 个 tag，相邻两次发布的间隔中位数是 0.48 小时（约 29 分钟），P25 是 0.24 小时，P75 是 0.93 小时。245 个间隔里有 186 个不到 1 小时，最多的一天发了 46 次。「等空闲再重启」基本追不上发布的速度，发布只能排着队等。

### 1.2 目标

- **G1 计划内重启不浪费已经开始的运行。** `CONTROL_PLANE` 计划下，进行中的 Worker/Reviewer 运行不中断、不重跑（S1）。`FULL` 计划下，在排空期限内结束的运行一个都不浪费（S0）。
- **G2 部署的等待有上界。** `CONTROL_PLANE` 最多等「控制面回合排空期限（默认 3 分钟）+ 重启空窗」，`FULL` 最多等运行排空期限（默认 20 分钟）。不再等「没有进行中的任务」。
- **G3 只升级改了的组件。** 没有代码改动的发布不重启。只改了控制台的发布也不重启。看护进程的代码改了，不打断任何运行，只有新运行用上新代码。
- **G4 不加新依赖。** 仍然只依赖 PostgreSQL，不用 docker，只在单机上跑。

验收用例见第 7.9 节。

### 1.3 不做

- 不保证控制面崩溃时运行不中断。S1 只管计划内重启，崩溃后的接管放到 S2。
- 机器重启、看护进程自己崩溃时，运行照今天的方式中断后重做。
- Manager、Planner 正在进行的模型回合不跨重启保留。排空会等它们结束，超时就中断后重做，和今天一样。
- 不把 API/控制台、渠道、调度器拆成单独的进程。它们随控制面一起重启，空窗只有几秒（理由见第 3 节结论 1）。
- 不做多实例高可用和蓝绿并行（方案 C 不推荐，见第 5.3 节）。也不修 #909 来让同一个库上的多个 `looper serve` 变安全，而是明确只允许一个（第 7.7 节、待决问题 3）。
- 不做进程内热重载（`importlib.reload`，第 5.4 节）。
- 不改各家 agent CLI。跨表结构变更的降级仍不支持，和今天一样。

## 2. 现状：重启会丢什么

### 2.1 停机和启动的顺序

**停机**（收到 SIGTERM，`looper/agents/serve.py:741-759`）依次停：咨询、关卡提醒、等待检查、关卡转发、调度器、HTTP 服务、Manager 群、`AgentSupervisor.stop_all`（按注册的倒序：先停 Worker 池，结束所有运行；再停 TaskRouter，所有 Planner 一起停，各自结束常驻 Claude，`looper/agents/supervisor.py:209-234`）、运行器、渠道、总线、数据库连接池。整个停机最多 15 秒（`SHUTDOWN_DEADLINE_SEC`，留在 launchd 发 SIGKILL 的 20 秒之内，`looper/agents/serve.py:414-416`），超时就直接退出，剩下的交给下次启动清理。

**启动**（`looper/agents/serve.py:518-723`）的顺序是：建连接池 → 表结构有变更就先备份 → `ensure_schema` → 结束上一个服务留下的 agent 进程（`proc_tree.sweep`，539-545）→ 决定哪些任务续跑、哪些标为停止（548）→ 把仍是 `running` 的运行记录记成 `interrupted`（549-552）→ 清理 worktree 和只读副本 → 重新排队上次的咨询（684）→ 启动各 agent 并续跑任务（688-690）→ 启动 HTTP 服务（717）→ 启动调度器，它的第一轮会补上停机期间错过的触发（719）。HTTP 服务在续跑之后才开始监听，这一点对第 7.8 节有用。在一次性的空库上、没开渠道时，从第一行日志到 `agent system running` 约 0.6–1 秒（升级检查留下的日志），这还不算 Python 的导入时间；真实环境还要加上渠道检查和任务续跑，空窗多长需要实测。

### 2.2 只在内存里的状态

| 状态 | 在哪 | 重启时 | 依据 |
|---|---|---|---|
| 任务与 Planner 的映射 | `TaskRouter._tasks`、`_planners` | 停机时全部停掉，启动时按库里的计划重建 | `looper/agents/tiers/task.py:149-150`、`174-184`、`248-268` |
| Planner 正在进行的回合 | 常驻 Claude 进程，加上 `_turn_lock` | 被结束。决策回合之前结果已经存库（状态 `deciding`），续跑时按存下的结果再决策一次 | `looper/agents/tiers/planner.py:861`、`2286-2291`、`2554-2558` |
| Worker/Reviewer 的运行 | `WorkerPool._active`、`_inputs`、`_slots`、`_reminders` | 停机时全部结束。续跑时同一次尝试换一个新 `worker_id`、在新会话里重跑，在评审中的步骤换一个新 Reviewer | `looper/agents/tiers/worker.py:428-455`、`470-491`；`looper/agents/tiers/planner.py:1819-1829`、`2541-2550` |
| Manager 正在进行的回合 | 消息循环里还没确认送达的那条消息 | 消息还没标为已送达，启动后重放，Manager 再答一遍；会话按 `agent_sessions` 续接 | `looper/agents/bus.py:256-263`；`looper/agents/tiers/manager.py:1010-1027` |
| 总线队列 | `MessageBus._queues`（JSONL 日志做持久化） | Manager、TaskRouter、渠道的未送达消息会重放；发给 Planner 和 Worker 的直接丢掉 | `looper/agents/bus.py:20-22`、`182-209` |
| 平台工具的令牌 | `PlannerOps._grants`、`RoomOps._grants`（只存哈希） | 全部失效；带旧令牌的 MCP 配置文件会留在 `.local/mcp/` 下 | `looper/agents/ops/planner.py:201-216`；`looper/agents/ops/room.py:181-196`；开发计划第 5.2 节 |
| 运行器会话 | `RunnerSupervisor` 的 generation 和租约 | 运行器进程被停掉。Manager/Planner 靠 `session/load` 续接，Worker 会话重做 | `looper/agents/serve.py:752-753`；`looper/agents/runtimes/runner/supervisor.py:165-360` |
| SSE 订阅 | `Broadcaster._subs` | 连接断开。不过事件流是按游标从数据库读的，重连后会补齐 | `looper/agents/broadcast.py:19-27`；`looper/agents/v1_domains/activity.py:92-127` |
| Telegram 长轮询 | python-telegram-bot | 启动时 `drop_pending_updates=True`，停机期间发来的消息会丢 | `looper/agents/channels/telegram.py:181`；`looper/agents/serve.py:652-653` |
| Lark 连接 | `lark-cli event consume` 子进程或长连接 | 会重连。断线期间的事件平台会不会补发，需要验证 | `looper/agents/channels/lark_transport.py:101-105`；`looper/agents/channels/lark_stream.py:17` |
| 调度器 | 删除屏障 `_project_firings`；正在跑的前置检查 | 屏障只在本进程里有效。正在检查前置条件的定时运行，启动时记成 `gate_failed`，算一次失败 | `looper/agents/scheduler.py:106-125`、`376-380` |
| 咨询 | `ConsultService` | 正在跑的改回排队，再跑一遍 | `looper/agents/consult.py:215-226` |

### 2.3 子进程：怎么结束，和服务怎么通信

| 进程 | 谁启动、怎么启动 | 和服务的通道 | 正常停机时 | 服务被杀掉时 |
|---|---|---|---|---|
| Worker/Reviewer 的 CLI | `ProcessRun.launch` → `proc_tree.spawn`，进程在自己的 session 里，stdin/stdout/stderr 都是管道（`looper/agents/tiers/worker.py:365-378`） | stdin/stdout 上跑 stream-json（`looper/agents/runtimes/claude_code.py:244-246`）。Codex `app-server`、OMP rpc、Grok ACP 也走 stdio | 结束整棵进程树：先 SIGTERM，等 3 秒，再 SIGKILL（`looper/agents/proc_tree.py:48`） | 因为在自己的 session 里，不会跟着死（`looper/agents/proc_tree.py:21-27`），但管道读端已经关了，下次启动时 `sweep` 会按 pid 和启动时间把它结束（`looper/agents/proc_tree.py:369-445`） |
| Manager/Planner 的常驻进程 | `claude_cli.py:897`、`runtimes/per_turn.py:261`、`runtimes/grok_acp.py:500`，都经过 `proc_tree.spawn` | 同上 | 随 Planner 和 Manager 群一起结束 | 同上 |
| MCP 桥 `looper mcp-server` | 由 agent CLI 启动 | 对 CLI 走 MCP stdio；对服务用 HTTP 加 Bearer 令牌，访问 `LOOPER_AGENT_URL`（就是控制台端口），超时 120 秒（`looper/agents/mcp_server.py:1-17`、`37`） | 随 CLI 一起结束 | 请求失败，调用方得到一个错误工具结果 |
| Pi 扩展 | 加载在 Pi 里 | 直接用 HTTP 调 agent API（运行器 RFC 第 2.1 节） | 同上 | 同上 |
| Pi/DSH 运行器 | `RunnerSupervisor._start` → `proc_tree.spawn`（`looper/agents/runtimes/runner/supervisor.py:328`） | stdio 上的 JSON-RPC，协议 0.1（`looper/agents/runtimes/runner/bridge.py:1`、`protocol.py:14-25`）；反向的 `tools/call` 和 `budget/authorize` 要服务来回答 | 停机时停掉（`looper/agents/serve.py:752-753`） | 同 CLI |
| 咨询、定时任务的前置检查、等待检查 | `consult.py:470`、`schedule_gate.py:109`、`wait_service.py:370`，都经过 `proc_tree.spawn` | stdout | 结束 | 同 CLI |

**结论：** 今天没有哪个子进程能真正活过服务。stdio 管道握在服务手里；反向调用要用服务内存里的令牌；Codex `app-server`、运行器这类有状态协议，请求表也在服务内存里。就算有进程躲过了 SIGKILL，下次启动也会在续跑任务之前把它结束。

### 2.4 启动时续跑什么、停掉什么

- 计划还在进行的任务续跑，`pending` 的任务重新开始，其余进行中的任务标为 `stopped`（`looper/agents/serve.py:390-411`；`looper/db/plans.py:163-176`；`looper/db/tasks.py:459-487`）。
- 续跑时的规则（`looper/agents/tiers/planner.py:2515-2563`）：`dispatched` 的步骤同一次尝试重跑，新的 `worker_id` 在 `planner.py:1827-1829` 生成；`completed`（在评审中）的步骤换一个新 Reviewer；`deciding` 的步骤按存下的结果再决策一次。
- 仍是 `running` 的运行记录一律记成 `interrupted`（`looper/db/llm_records.py:131-137`）。

### 2.5 一次计划内重启的代价

- **白花的：** 每个进行中的 Worker/Reviewer 运行花的钱和时间（在新会话里从头跑，worktree 里留着上次跑了一半的改动）、被打断的 Planner 决策回合、被打断的 Manager 回合（会再答一遍）、正在跑的咨询，还有正在做前置检查的定时运行（记成一次 `gate_failed`）。
- **丢掉的：** 空窗期间发到 Telegram 的消息；Lark 的情况需要验证。
- **要冷启动的：** Manager 和每个在跑任务的 Planner，都要重新起进程、续接会话。
- **不受影响的：** 计划、步骤、日志、会话、聊天和活动记录（都在 PostgreSQL 里）；SSE 按游标续读；操作者令牌和 cookie。
- **缺的数据：** 每次重启打断了多少运行、花了多少钱，目前没有记录，第 8.2 节给了一条只读查询来补这个基线。

## 3. 哪些部分改得最勤

下表统计的是每两个相邻发布 tag 之间（共 245 个间隔）碰到某个区域的比例，也就是「有多少次部署要用到这个区域的新代码」：

| 区域 | 包含 | 占发布的比例 |
|---|---|---|
| 控制台前端 | `web/` | 38.4% |
| HTTP API 与平台工具 | `http_server.py`、`api_v1.py`、`v1_domains/`、`ops/` | 36.3% |
| Manager/Planner/Worker 逻辑 | `tiers/`（其中 `manager.py` 14.3%、`planner.py` 8.6%、`worker.py` 4.9%） | 22.0% |
| 数据访问 | `looper/db/*.py` | 17.6% |
| 运行时适配器 | `runtimes/`（不含 `runner/`） | 16.3% |
| 运行器 | `runners/`（TypeScript，10.6%）或 `runtimes/runner/`（Python，9.0%） | 16.3% |
| 服务骨架 | `serve.py`、`supervisor.py`、`bus.py`、`message.py` | 10.2% |
| 表结构 | `looper/db/schema.sql`（v2.14.0 收紧升级边界之后的 25 次发布里只有 1 次） | 9.8% |
| 渠道 | `channels/` | 9.4% |
| 进程机制 | `claude_cli.py` 7.8%；`proc_tree.py` 4.9% | — |
| 调度器 | `scheduler.py`、`schedules.py`、`schedule_*.py`、`cron.py` | 6.1% |
| Worker 看得到的契约 | `system_prompt.py`、`ops/room.py`、`ops/vault.py`、`room.py` | 2.4% |
| MCP 桥、Pi 扩展 | `mcp_server.py`、`runtimes/pi_extension/` | 各 0.8% |

按第 7.2 节的规则把这 245 个间隔折成升级计划：13.1%（32 次）没有改运行时代码（只改了文档、测试或版本号），是 `NOOP`；7.3%（18 次）只改了控制台，是 `WEB_ONLY`；2.4%（6 次）改了 Worker 看得到的契约，是 `FULL`；剩下 77.1%（189 次）要重启控制面。按提交数看，同一时期有 1570 个非合并提交、1044 个合并；改动最多的是测试（56.7% 的提交）和文档（44.4%）。

**结论：**

1. 改得最勤的是控制台、API、各 tier 和运行时适配器，这些全是逻辑，应该留在可以重启的控制面里。把它们拆成单独的进程得不到什么：控制面几秒就能重启完；反而每一次内存调用都要变成跨进程调用。
2. 要跨重启活下来的是 agent 进程和它的 stdio。今天握着它们的代码是改得最少的（`proc_tree.py` 4.9%），只握进程、不解析内容的话，改动只会更少。
3. 运行时适配器（16.3%）和 `claude_cli.py`（7.8%）改得多，所以要留在控制面。看护进程只管字节，不解析事件流。
4. Worker 看得到的契约很少改（2.4%）。一旦改了，旧运行拿到的契约和新代码不一致，就走 `FULL`。
5. 约 20% 的发布根本不用重启。

## 4. 组件边界需要什么

### 4.1 协议

- **控制面 ↔ 看护进程（新增，要带版本）：** 写 stdin、订阅 stdout/stderr（带偏移，支持从任意偏移补读）、发信号、结束进程树、围栏（第 7.5 节）。
- **MCP 桥、Pi 扩展 ↔ agent API（已有，但会变成跨版本契约）：** 旧运行里的桥会去调新的控制面。工具列表是桥启动时拿到的，所以工具改名、删除或参数收紧都会打断旧运行。
- **运行器 ↔ RunnerBridge（已有，协议 0.1）：** 反向请求和会话的状态在 RunnerBridge 的内存里。要跨重启，必须加重新挂接和补发（S3）。

### 4.2 有两个进程时，任务归谁

- 方案 A、B 里始终只有一个控制面。用数据库咨询锁强制这一点（第 7.7 节）。看护进程不拥有任务，只拥有一个进程；它同一时刻只接受一个控制者，纪元（epoch）最高的那个说了算，靠这个做围栏。
- 方案 C 里新旧两个控制面同时在跑。每个任务都要一份所有权租约；Manager、渠道、调度器这些单例也要租约（第 5.3 节）。

### 4.3 表结构迁移和还在跑的旧进程

- 方案 A、B 里只有控制面访问数据库。旧控制面先退出，新的才启动并迁移，在 `_SCHEMA_LOCK` 下执行（`looper/db/connection.py:64-66`、`170-206`），版本超过自己支持的就拒绝启动（`looper/db/connection.py:92-106`）。看护进程不连数据库，所以迁移的做法和今天完全一样。
- 方案 C 里新旧代码要在同一个库上并存几个小时。迁移只能做加法，旧代码还得扛得住新加的约束。

### 4.4 跨版本契约：新控制面从旧运行那里继承了什么

- 运行清单：哪个步骤、什么角色、哪条运行记录、什么时候开始、用的什么运行时。
- Worker/Reviewer 的报告契约：旧运行是按旧契约写报告的。
- Worker 席位的工具列表和令牌。
- 每种运行时的 stdin 协议状态：Claude Code 的 stream-json 基本没有状态；Codex `app-server`、OMP rpc、Grok ACP 有请求 id 和 thread 状态。

### 4.5 进程还在跑，磁盘上的代码被换掉了

控制面里有不少写在函数里的 import（比如 `looper/agents/serve.py:108`、`127`、`149-152`）。代码被 `git pull` 或 `pip install` 换掉之后，还在跑的旧进程可能导入到新模块。今天这个窗口很短；排空会把它拉长到几分钟，看护进程更是要活好几个小时。所以要有规则约束（第 7.2 节的部署顺序，第 7.5 节的「看护进程只用启动时导入的代码」）。

## 5. 候选方案

### 5.1 方案 A：在回合边界排空，然后续跑（不拆分）

在现有的单进程上加一个有类型的排空状态。部署前先排空：不再开始新的运行和 Manager 回合，暂停定时触发，等已经开始的结束；排空完成或到期限后重启，再用现有的续跑逻辑接着跑。

- **复杂度：** 低。暂停点都已经有了：Worker 池的 `_take_slot` 本来就会等位（`looper/agents/tiers/worker.py:723-755`），Manager 有 `busy()`（`looper/agents/tiers/manager.py:1005-1008`），Planner 有 `_turn_lock`（`looper/agents/tiers/planner.py:861`）。
- **仍会被打断的：** 到期限还没跑完的运行。排空期间不开始新运行，所有任务最多停顿一个期限那么久。
- **故障半径：** 和今天一样。
- **表结构迁移：** 不变。
- **「只升级改了的组件」：** 不拆进程。只靠组件指纹让 `NOOP` 和 `WEB_ONLY` 的发布跳过重启（约 20%），其余发布都要排空后整体重启。
- **工作量：** 3 个 PR，2–3 天（按 agent 开发估算）。

### 5.2 方案 B：看护进程持有运行，控制面可以随时重启

把「持有 agent 进程和它的 stdio」从控制面里拿出来，放进一个改得很少、协议带版本的看护层；其余逻辑都留在控制面。计划内重启时，控制面在一个干净的边界上和运行脱离（detach），把消费到的偏移记下来后退出；新控制面启动后接管这些运行：先把日志从头重放一遍、只重建内存状态（`[0, 偏移)` 这一段不产生任何副作用），之后接着消费实时输出。

看护层放在哪里有两种做法：

| | B1：一个常驻宿主进程 | B2：每个运行一个看护进程（推荐） |
|---|---|---|
| 进程数 | 1 个 | 每个运行 1 个。本机实测：空的 CPython 3.11 解释器约 18 MB RSS，加载 asyncio 和 `proc_tree` 后约 24 MB，启动约 70 ms（macOS 的 RSS 含共享页，独占的内存更少） |
| 宿主代码升级 | 要排空，或者像运行器那样做 generation 交接 | 新运行用新的看护进程，旧运行的看护进程继续跑到运行结束，一个都不打断 |
| 崩溃影响 | 所有运行 | 一个运行 |
| 协议 | 要多路复用，还要维护一份运行登记表 | 每个运行一条连接；登记表就是目录 |
| 运行器 | 宿主可以直接持有 | 同一个看护进程也能持有一个运行器进程（它不关心子进程是什么），S3 只需要给运行器协议加上重新挂接 |

- **复杂度：** 中。新增看护进程、一份本地协议和接管逻辑（第 7.5、7.6 节）。
- **仍会被打断的：** Manager/Planner 正在进行的回合（等最多 3 分钟，超时就中断后重做）；暂时还不能托管的运行时（Codex `app-server`、OMP rpc、Grok ACP）的运行，以及运行器会话（到 S3 之前，按排空等待）；`FULL` 计划（约 2.4%）；看护进程自己崩溃的那一个运行；控制面崩溃时的运行（到 S2 之前）。
- **故障半径：** 控制面崩溃不再必然杀掉运行（S2 之后能接管）。看护进程崩溃只影响一个运行。接管出 bug 时退回到「结束后重做」，也就是今天的行为。
- **表结构迁移：** 和今天一样。看护进程不连数据库，旧控制面退出之后新的才迁移；被接管的运行由新代码按新表结构继续写记录。
- **「只升级改了的组件」：** 部署脚本比较组件指纹（第 7.2 节）。约 77% 的发布只重启控制面，运行不受影响；看护进程的代码改了不需要动作；契约改了走 `FULL`。
- **工作量：** S1 共 5 个 PR，1.5–2 周；S2 再加 2 个 PR；S3 再加 3–4 个 PR。

### 5.3 方案 C：蓝绿部署，加任务所有权租约

新版本起一个新的控制面，接新的工作；旧的继续把自己手上的任务跑完再退出。

- **前提：** 先把 #909 的多进程安全做完，而且远不止 #909。总线要跨进程（PostgreSQL 表加 LISTEN/NOTIFY），因为 `MessageBus` 是进程内的（`looper/agents/bus.py:162-172`）。每个任务要有一份所有权租约（`tasks.owner_instance`，加上续约和过期）。单例也要租约：Manager 的常驻会话只能有一个进程续接，Telegram 的 `getUpdates` 只能有一个轮询者，Lark 的长连接也一样，调度器、关卡转发、等待服务同理。删除屏障要改用咨询锁。控制台只有一个端口：发给旧实例所拥有任务的操作要转发过去，旧运行的 `LOOPER_AGENT_URL` 也必须还连得上旧实例，所以要么加一个前置代理，要么每个实例单开一个 agent 端口。`Broadcaster`、`_grants`、`_planners` 这些内存表都要放进数据库，或者改成按所有者路由。
- **复杂度：** 高。几乎每一张内存表都要动。
- **仍会被打断的：** 任务不会被打断。但 Manager 回合和渠道交接时会有一段空窗；旧任务继续跑旧代码，可能要好几个小时，这期间修好的 bug 到不了正在跑的任务。
- **故障半径：** 最大。租约一旦脑裂，就会有两个 Planner 在同一个 worktree 里派 Worker，可能弄坏数据；跨进程总线出 bug 会丢消息。
- **表结构迁移：** 新旧代码要并存好几个小时，只能先扩后缩（expand/contract）。旧实例一旦崩溃，就会因为版本保护（`looper/db/connection.py:92-106`）再也起不来。
- **「只升级改了的组件」：** 做不到。每次发布都是一个完整的新实例。
- **工作量：** 12 个以上 PR，4–6 周，以后还要一直守着「两个版本并存」的纪律。

### 5.4 不考虑的做法：进程内热重载

`importlib.reload` 不会更新已经存在的对象、闭包和正在跑的协程。Looper 的状态都在长期存活的对象和协程里（Planner、`_execute` 的闭包），重载之后新旧代码会混在一起，出了问题也说不清是哪一边。不考虑。

### 5.5 对比

| | A 排空后续跑 | B 看护进程 + 控制面 | C 蓝绿 + 租约 |
|---|---|---|---|
| 复杂度 | 低 | 中 | 高 |
| 计划内重启时进行中的运行 | 等它跑完（最多 20 分钟），超时就中断 | 不中断（Claude Code 运行从 S1 起，其余运行时逐个跟进） | 不中断，跑旧代码 |
| 部署等待 | ≤ 运行排空期限 | ≤ 回合排空期限 + 几秒空窗 | 几乎为零（旧实例在后台慢慢排空） |
| 故障半径 | 同今天 | 比今天小 | 比今天大（可能脑裂） |
| 表结构迁移 | 不变 | 不变 | 先扩后缩，跨几个小时 |
| 只升级改了的组件 | 只能跳过 `NOOP` 和 `WEB_ONLY` | 按组件指纹决定 | 不支持 |
| 工作量 | 3 个 PR | S1 共 5 个 PR | 12 个以上 PR |

## 6. 推荐方案与分阶段路径

**推荐 B2，先做 A。** A 本身就有价值，而且以后一直是 B 的兜底路径：协议不兼容、契约变了、运行时还不能托管的时候，都退回 A。B2 只把改得最少的「握进程」这件事拆出去；所有常改的逻辑都留在一个可以随时重启的控制面里。它直接回答了维护者的要求：拆成组件、只升级改了的那个。C 的代价和风险都远大于收益，不做。

| 阶段 | 内容 | 单独交付能带来什么 | 依赖 | PR |
|---|---|---|---|---|
| **S0** | 组件指纹和升级计划；排空状态和暂停点；`looper service upgrade` / `restart --drain`；不再丢 Telegram 消息 | 约 20% 的发布不用重启；其余发布最多等 20 分钟，排空期限内结束的运行一个不浪费，不再等「没有任务」；重启不会再制造 `gate_failed`；空窗期间的消息不丢 | — | 3 |
| **S1** | 看护进程（每个运行一个）；Claude Code 的 Worker/Reviewer 运行可以被托管；计划内脱离和接管；agent API 断线重试；令牌持久化；单控制面锁和纪元围栏 | 约 77% 的发布只重启控制面，Claude Code 的运行不中断、不重跑 | S0 | 5 |
| **S2** | 控制面崩溃后的接管：持久化消费偏移，副作用全部做成幂等 | 控制面崩溃不再杀掉运行 | S1 | 2 |
| **S3** | 看护进程持有运行器进程；运行器协议 0.2 支持重新挂接、按序号补发更新、按 `call_id` 去重反向请求；Codex `app-server`、OMP rpc、Grok ACP 逐个变成可托管 | 其余运行时的运行也不中断 | S1 | 3–4 |
| S4（可选） | 空闲的 Manager/Planner 常驻进程也交给看护进程，跨重启保持温热 | 少一次冷启动 | S1 | 2 |

先做 S0，因为它不改架构、几天就能上线，马上解决「部署等几个小时」的问题，还为 S1 准备好了部署脚本和排空机制。S1 先只托管 Claude Code，因为它是默认的运行时，stream-json 协议也基本没有状态。别的运行时按实际被 Worker 使用的比例排顺序（第 8.2 节的查询可以给出这个比例）。

## 7. 关键实现点（S0 与 S1）

### 7.1 组件和指纹

在 `looper/components.toml` 里声明各组件（用 glob），CI 检查每个被跟踪的文件至少属于一个组件，或者明确列在忽略里（一个文件可以同时属于几个组件）。新加的顶层目录必须先归类，否则检查不过。

| 组件 | 路径 | 改了以后 |
|---|---|---|
| `web` | 计划统一比较构建输入的源指纹：`web/src/**`、`web/index.html`、`web/public/**`、`web/package*.json`、Vite/TypeScript/Tailwind/PostCSS 配置；构建将它写入 `web/dist/.source-fingerprint`，wheel 同样带在 `looper/web/dashboard/.source-fingerprint`；构建产物哈希只供校验 | 不用重启：静态文件每次请求都从磁盘读（`looper/agents/http_server.py:55-66`、`1309-1360`） |
| `control_plane` | `looper/**`（包含 `keeper` 的重叠文件），不含只供 CLI 用的文件和 `looper/__init__.py`；另含 `[project]` 的运行时依赖声明和分发的 `uv.lock`，忽略 `pyproject.toml` 的工具配置 | 重启控制面 |
| `keeper` | `looper/agents/keeper/**`、`looper/agents/proc_tree.py`、`looper/child_env.py` | 不需要动作，新运行自然用上新代码（这几个文件同时也算在 `control_plane` 里） |
| `runners` | `runners/**` | S3 之前算在 `control_plane` 里 |
| 忽略 | `docs/**`、`tests/**`、`evals/**`、`*.md`、`looper/__init__.py`（只有版本号） | — |

- **指纹** 是组件里每个文件的「路径 + 内容的 sha256」排序后再取一次 sha256。目标版本的指纹可以直接从 git 树（`git cat-file --batch`，不用检出）或者 wheel（zip）算出来，不需要先安装。
- **接管契约指纹**（单独一组，任何一项变了都走 `FULL`）：`run_manifest_version`；Worker 和 Reviewer 报告契约原文的 sha256（`looper/agents/system_prompt.py` 的 `WORK_CONTRACT`、`REVIEW_CONTRACT`）；Worker/Reviewer 席位工具列表 JSON 的 sha256；看护进程支持的协议版本集合。
- **运行中的指纹：** 控制面启动时，按自己加载的安装目录计算并固定 `components` 中除 `web` 外的指纹和 `adoption`，与 `version`、`loaded_at` 一起保留到退出；磁盘文件被替换不改变这份加载快照。`POST /api/v1/system/components`（`system.components`，要操作者令牌）每次从 `server._static_dir` 读取记录的源指纹到 `components.web`，重新计算当前构建产物哈希到 `verification.web`。HTTP 200 正文为 `{status: "ok", result: {version, loaded_at, components: {web, control_plane, keeper, runners}, adoption: {…}, verification: {web}}}`。后续升级计划比较源指纹，`WEB_ONLY` 安装后用构建产物哈希确认实际提供的文件。旧构建没有源指纹记录时按未知来源处理，首次升级重建，不能误报 `NOOP`。

### 7.2 升级计划和部署脚本

新加 `looper service upgrade [--to <git ref | wheel>] [--dry-run] [--turns-timeout 180] [--drain-timeout 1200] [--no-force]`；`CONTROL_PLANE` 用 `--turns-timeout` 排空回合，`FULL` 用 `--drain-timeout` 排空运行。`looper service restart --drain` 是它在 `FULL` 计划下省去安装的那一段。需要重启时，顺序必须是 **先算计划 → 排空 → 停止旧控制面并完成脱离 → 确认旧进程退出 → 安装 → 启动**。排空超时也遵守这个顺序，旧进程仍在运行时不得就地安装，避免延迟导入混用新旧代码（第 4.5 节）：

```text
target   = manifest_of(source)              # git 树或 wheel，不安装
running  = (POST /api/v1/system/components {}).result  # 加载快照 + 本次从磁盘读取的 web 指纹
keepers  = scan(.local/runs/*/keeper.json)  # 还活着的看护进程及其协议版本
plan     = decide(target, running, keepers, keeper_mode)
print(plan, reasons)                        # --dry-run 到此为止
NOOP          → 安装（只是换文件），不重启
WEB_ONLY      → 安装；checkout 先构建到临时目录再整体换名替换 web/dist；校验 index.html
CONTROL_PLANE → 排空（scope=turns）→ service stop（托管运行完成脱离，确认旧进程退出）→ 安装 → service start → 等服务起来 → 校验
FULL          → 排空（scope=runs）→ service stop（完成停机，确认旧进程退出）→ 安装 → service start → 等服务起来 → 校验
校验：重新 POST /api/v1/system/components {}，components.web 等于 target.components.web，
      verification.web 等于安装后构建产物的哈希；其余 components 和 adoption 等于 target 对应值；
      CONTROL_PLANE 的接管数 = 重启前托管的运行数；打印部署报告（第 8.2 节）
```

```text
decide(target, running, keepers, keeper_mode):
  current = running.components        # web 是已提供构建的源指纹，非产物哈希
  changed = {c | target[c] != current[c]}
  if changed == ∅:                                         return NOOP
  if changed ⊆ {web}:                                      return WEB_ONLY
  if keeper_mode == off:                                   return FULL
  if ∃ k ∈ keepers: k.protocol ∉ target.keeper_protocols:  return FULL
  if target.adoption != running.adoption:                  return FULL
  return CONTROL_PLANE        # keeper 组件变了不影响计划
```

在 checkout 里，「安装」是 `git merge --ff-only`；装 wheel 时是 `pip install`。`NOOP` 和 `WEB_ONLY` 之后，`GET /api/v1/whoami` 的 `result.looper_version` 仍然报旧控制面加载的版本号（`looper/agents/api_v1.py:75-79`），部署报告分别列出控制面加载指纹、当前 web 指纹和安装的包版本。连续两次只升级控制台时，每次都用新请求读取的 web 指纹制定计划和校验，不把启动时的值当成当前静态资源版本。

checkout 的 `NOOP` 和 `WEB_ONLY` 不同步依赖；需要同步时使用 `uv sync --extra test --inexact`，保留部署的测试依赖和环境中其他已安装的包。构建控制台时传入当前解释器的 `PYTHON`，发布前统一目录为 0755、文件为 0644。有未跟踪的指纹输入时，在排空前拒绝并列出文件，Git 忽略的文件除外。wheel 的 `NOOP` 和 `WEB_ONLY` 均安装整个包而不同步依赖，控制台先暂存再原子替换。新进程启动失败时，从当前安装再启动一次；命令仍报告失败，并用 `serving` 表明服务是否已响应。

### 7.3 S0：排空

新模块 `looper/agents/drain.py` 定义一个有类型的状态，全进程只有一个实例，由 serve 创建，交给 WorkerPool、ManagerFleet、Scheduler、ConsultService 使用：

```python
class DrainPhase(StrEnum): SERVING = "serving"; DRAINING = "draining"; DRAINED = "drained"
class DrainScope(StrEnum): TURNS = "turns"; RUNS = "runs"
@dataclass(frozen=True)
class DrainStatus: phase: DrainPhase; scope: DrainScope | None; since: datetime | None; deadline: datetime | None
```

暂停点：

| 位置 | 排空时 | 重启之后 | 为什么这样就不浪费 |
|---|---|---|---|
| `WorkerPool._take_slot`（`worker.py:723-755`） | 新运行一律等位，相当于上限是 0 | 步骤在库里仍是 `dispatched`，续跑时照常重派（`planner.py:2541-2550`） | 等位发生在 `_execute` 之前（`worker.py:791-809`），运行根本没开始 |
| Manager 的 `_handle` 入口（`looper/agents/tiers/manager.py:1030-1043`），在任何用户看得到的动作和取 `_turn_lock` 之前 | 已取出的消息由当前 handler 持有，等待 SERVING 许可，等待期间不得返回或推进 `subscribe`；后续消息仍排队 | 取消排空就继续处理；停机时取消等待并退出消息循环（`manager.py:1013-1028`），不再取下一条，未确认的消息启动后重放（`bus.py:182-209`），只答一次 | `bus.subscribe` 在 yield 之后才标为已送达（`looper/agents/bus.py:256-263`）；等待时不确认、不开始回合 |
| 调度器 `tick` 里的触发（`scheduler.py:419-443`） | 不触发新的定时运行；已经在做的前置检查让它跑完 | 第一轮按各定时任务的 catch-up 规则补触发（`scheduler.py:1-24`） | 不会再出现 `gate_failed`（`scheduler.py:376-380`） |
| `ConsultService` 排队中的咨询 | 不开始 | 照常（`consult.py:215-226`） | — |
| Planner 回合、`TASK_START` | **不拦** | — | Planner 要先把 Worker 的结果存进库，才能交给决策回合；如果拦住，结果会一直留在发给 Planner 的总线队列里，而重启时这个队列会被丢掉（`bus.py:20-22`），结果就丢了。规划和决策回合本身有上限（每回合 1200 秒，`serve.py:289-292`），它们派出的运行会在 `_take_slot` 等位 |

**判定排空完成（DRAINED）：** 所有 Manager 的 `busy()` 都是 false；每个 Planner 的 `_turn_lock` 都空闲，而且 `bus.pending_count(planner_id) == 0`；没有正在做前置检查的定时运行；没有正在进行的等待检查；没有正在跑的咨询；没有正在启动的运行。scope 是 `runs` 时，还要求 WorkerPool 里没有正在跑的运行；scope 是 `turns` 时，只要求没有不可托管的运行（S1）。Claude 自己发起的 Manager 回合不算在 `busy()` 里（`manager.py:1005-1008`），排空不等它们，重启时照今天的方式处理。

**任务进度的回复路由：** 与 web 回复一样，重放依赖消息携带的 `reply_route`，不依赖渠道实例的 correlation 缓存。Manager 创建 Telegram/Lark 任务时把原聊天和 Lark 原消息 ID 存为 `tasks.origin_reply_route`；TaskRouter 的任务报告和 Manager 的进度消息沿用该路由及完整的渠道地址。缓存命中时仍按原方式发送，缓存缺失时 Telegram 发往原聊天，Lark 回复原消息；任务报告处理后由总线确认，后续启动不再重放。`STATUS_UPDATE` 本身仍是瞬时消息；本保证覆盖 drain 持有的 `TASK_ACCEPTED`、`GOAL_STATUS` 在重启后的重放，不保证平台已收到但总线尚未确认时崩溃的严格 exactly-once。升级前未保存原消息路由的任务保留旧行为，无法补出历史 Lark 消息 ID。

**期限：** `turns` 默认 180 秒，`runs` 默认 1200 秒。到期后部署照常往下走，剩下的运行像今天一样被中断，报告里逐个列出来；`--no-force` 时放弃这次部署，回到 SERVING。

**接口（要操作者令牌，放在 v1 的 system 域里，loopercli 自动生成对应命令）：** 域操作统一使用 POST（现有 `system.status` 见 `looper/agents/v1_domains/status.py:29-30`）：

| 操作 | 请求 | 结果 |
|---|---|---|
| `system.drain` | `POST /api/v1/system/drain {scope, timeout_seconds}` | 进入排空并立即返回当前状态，不等待排空完成 |
| `system.drain_cancel` | `POST /api/v1/system/drain_cancel {}` | 取消排空，返回 SERVING 状态 |
| `system.drain_status` | `POST /api/v1/system/drain_status {}` | 返回当前排空状态 |

以上操作者调用都执行一次并返回 HTTP 200，正文为 `{status: "ok", result: {phase, scope, since, deadline, waiting: {runs: [{worker_id, task_id, role, since, keepable}], manager_turns, planner_turns, gates, waits, consults}}}`；202 只表示 `needs_confirmation`，不表示排空尚未完成（`looper/agents/api_v1.py:65-71`）。取消排空也只用 POST。`POST /api/v1/system/status` 的 `result` 里也带上 `drain`，控制台据此显示一条横幅：「正在升级：新的回合和运行会在重启后开始」。排空状态只存在内存里：排空期间服务如果崩溃，下次启动就是 SERVING。

### 7.4 S0：重启空窗

- **Telegram：** 计划内重启不再丢掉积压的更新（`drop_pending_updates=False`），按 `update_id` 去重；超过渠道配置 `backlog_max_age_seconds`（默认 600 秒）的积压消息只记录、不交给 Manager。PTB 在入队之后推进 offset、下次轮询确认；入队前先提交 runtime root 的 `.local/telegram/<channel_id>/<bot_id>.sqlite3`，`updates.update_id` 为持久化去重键，重启重放 pending 更新。验证细节见[渠道重启投递](../channel-restart-delivery.md)。
- **Lark：** 长连接替身平台不保留离线事件，重连只收到新事件；`lark-cli` 替身可显式重放输入事件，但不能证明真实平台保证。真实平台是否补发仍未验证，v0 不改 transport，重启空窗消息按可能丢失处理，见[渠道重启投递](../channel-restart-delivery.md)。
- **控制台：** 空窗期间请求会失败，靠显示出来的横幅和重试解决，不另外做排队。

### 7.5 S1：看护进程

**放在哪：** `looper/agents/keeper/`。只允许导入标准库、`looper.agents.proc_tree` 和 `looper.child_env`，而且启动时一次全部导入。CI 做两项检查：用 `python -X importtime -m looper.agents.keeper --check` 确认导入的 looper 模块在白名单里；用 AST 检查这几个模块里没有写在函数里的 import。

**运行目录** `<runtime root>/.local/runs/<run_id>/`（权限 0700，`run_id` 就是 `worker_id`）：

| 文件 | 谁写 | 内容 |
|---|---|---|
| `manifest.json` | 控制面在启动看护进程之前写 | `{manifest_version: 1, run_id, role, task_id, step_id, record_id, runtime_type, started_at, work_dir, is_verify, task_role, timeout_s, orphan_timeout_s, adoption: {...}}`，不含任何秘密 |
| `keeper.json` | 看护进程写，每次原子替换 | `{format: 1, protocol_versions: ["1.0"], keeper_version, keeper_pid, keeper_started, child_pid, child_started, state: starting\|running\|exited, exit_code, signal, ended_at, end_reason: exited\|ended\|orphaned, stdout_truncated, stdout_truncated_at}` |
| `stdout.log`、`stderr.log`、`stdin.log` | 看护进程写 | 三个方向的原始字节，只追加。stdout 设磁盘上限（默认 512 MiB，超过后只停止追加日志，继续向当前订阅者推送实时输出，不结束运行）；`keeper.json` 记录截断标记和偏移，截断的运行不能接管 |
| `grant.json` | 控制面写 | 这个运行的席位令牌授权：哈希、任务、席位、room、vault 配置（第 7.8 节） |
| `detach.json` | 控制面在脱离时写 | `{epoch, consumed: {stdout, stderr}, stdin_offset, at}` |
| `keeper.sock` | 看护进程 | Unix 套接字，权限 0600 |

**进程模型：** 控制面直接用 `asyncio.create_subprocess_exec(..., start_new_session=True)` 启动 `sys.executable -m looper.agents.keeper --dir <run dir> -- <argv…>`，不经过 `proc_tree.spawn`，从启动起就不为 keeper 写以控制面为所有者的 `proc_tree` 记录。`proc_tree.spawn` 会把调用方登记为所有者（`looper/agents/proc_tree.py:335-346`），而新控制面先执行 `sweep`、后执行 `end_unfinished_records`（`looper/agents/serve.py:539-551`），到接管扫描时再豁免已经来不及。看护进程自己的 stdin/stdout/stderr 都接 `/dev/null`，诊断写进 `keeper.log`。子进程的环境变量就是看护进程的环境去掉 `LOOPER_KEEPER_*`，这样 provider 的 key 不会落盘；cwd 由参数指定。看护进程用 `proc_tree` 的方式启动子进程（子进程在自己的 session 里），并以 keeper 自己的 pid 和启动时间作为「服务」写一份子进程记录，这样控制面启动时的 `sweep` 会因为 keeper 还活着而不去碰子进程（`looper/agents/proc_tree.py:369-410`）。子进程退出、日志落盘之后，看护进程写 `state=exited` 然后退出，运行目录留到控制面释放。**孤儿保护：** 连续 `orphan_timeout_s`（默认 30 分钟）都没有控制者连上，看护进程就结束子进程（`end_reason=orphaned`），防止控制面再也回不来时一直在花钱。

**协议 1.0：** JSON-RPC 2.0，每行一个 JSON，单帧不超过 4 MiB（和运行器协议的 `MAX_FRAME_BYTES` 一致），数据用 base64 分块，每块不超过 64 KiB。

| 方向 | 方法 | 参数 → 结果 | 说明 |
|---|---|---|---|
| 控制面 → 看护 | `initialize` | `{protocol_versions, controller: {looper_version, epoch}}` → `{protocol_version, keeper: {version, pid, started}, child: {pid, started, state, exit_code}, offsets: {stdout, stderr, stdin}}` | 双方取共同的最高版本；没有共同版本就返回 `VERSION_MISMATCH`。纪元低于已经见过的，返回 `STALE_CONTROLLER`；纪元更高的会顶替当前控制者，旧连接收到 `REPLACED` 后被关闭 |
| | `stream/subscribe` | `{stream: stdout\|stderr, from_offset}` → `{}` | 先从日志补读，再推实时数据；stdout 已截断时拒绝新订阅 |
| | `stdin/write` | `{data, expect_offset}` → `{offset}` | 只有 `stdin.log` 的长度等于 `expect_offset` 时才写，否则返回 `OFFSET_MISMATCH` 和实际长度。日志追加并排队后即确认偏移，由独立写任务向子进程 drain；这样慢读不会延迟确认，重连后也不会重复写 |
| | `stdin/close` | → `{}` | 幂等 |
| | `child/signal` | `{signal: TERM\|INT\|KILL}` → `{}` | 发给整个进程组 |
| | `child/end` | `{grace_seconds}` → `{exit_code}` | 按 `proc_tree` 的方式结束整棵树并回收 |
| 看护 → 控制面 | `stream/data` | `{stream, offset, data}` | 通知 |
| | `stream/eof` | `{stream, offset}` | 通知 |
| | `child/exited` | `{exit_code, signal, at}` | 通知 |

**版本协商的规则：** 次版本只能做加法。控制面至少要支持「当前主版本」和「上一个主版本」；只有在没有任何还活着的看护进程讲某个主版本时，才能把它删掉（第 7.2 节的 `decide` 会检查，不满足就走 `FULL`）。看护进程是旧控制面在安装新代码之前启动的，所以它讲的版本一定是旧控制面支持的。

### 7.6 S1：控制面怎么接入、怎么接管

- **开关：** 运维文件里加 `[agent_host] keeper = "off" | "on"`，S1 默认 `off`，和运行器的 opt-in 一样，验证通过后再改默认值。运行时适配器加一个类属性 `keepable: ClassVar[bool] = False`，S1 只把 Claude Code 设成 True。
- **`KeptRun`（实现 `RunHandle`，`looper/agents/runtimes/channel.py:30-49`）：** `launch()` 写好 `manifest.json`，启动看护进程，调 `initialize`，从 0 开始订阅 stdout 和 stderr，然后交出一个模仿 `asyncio.subprocess.Process` 的对象：`pid` 是子进程的 pid；`stdin` 适配成 `write/drain/close/is_closing`，底下调 `stdin/write`；`stdout/stderr` 是由 `stream/data` 喂数据的 `asyncio.StreamReader`；`wait()` 和 `returncode` 跟着 `child/exited` 走。`runtime.run_channel(process, …)` 和各适配器的 `parse` 都不用改。`end()` 调 `child/end`。`ProcessRun`（`worker.py:341-398`）在开关打开、运行时可托管时换成 `KeptRun`。
- **把 `_execute` 拆开**（`worker.py:853-1300`）：拆成 `prepare(envelope) → RunContext` 和 `consume(ctx, handle, replay_until=None)`。`RunContext` 里可以序列化的字段就写进 `manifest.json`。
- **脱离（只在计划内重启时）：** `WorkerPool.stop(detach=True)` 对每个托管的运行：停止往 reader 里喂数据；等到 reader 的缓冲区空了、消费者正停在等下一行上，此时「喂进去的字节数」就等于「已经处理完的字节数」；写 `detach.json`；关闭套接字。运行记录保持 `running`；`finally` 里撤销令牌、标记离线这些动作都跳过（`worker.py:827-835`）。不可托管的运行照今天的方式处理。
- **启动时：** 在 `end_unfinished_records` 之前扫描 `.local/runs/*/`。看护进程还活着（或者已经 `exited` 但日志还在）、并且有 `detach.json`、纪元等于上一个控制面的纪元的，算作候选；`end_unfinished_records(keep=候选的 record_id)` 不碰它们。没有干净脱离的运行在 S1 里直接结束（S2 再处理）。
- **接管：** `Planner._resume` 对 `dispatched` 的步骤，先调 `worker_pool.adopt(step.worker_id)`，失败才走 `_dispatch_step(same_attempt=True)`；对 `completed`（在评审中）的步骤，对 `reviewer_id` 做同样的事。`adopt` 的步骤：只有当库里这个步骤仍然引用这个 `worker_id` 时才接管；按清单重建 `WorkerProcess`；在派新运行之前先重新占回这个运行的位子；重新登记输入通道和席位令牌；启动 `consume(ctx, KeptRun.attach(dir), replay_until=consumed)`。
- **重放：** 把日志的 `[0, consumed)` 送进同一条解析管线，带上 `replaying=True`。这时运行记录器只更新内存里的计数（用量、模型、上下文、压缩、花费上限），不写库；WorkerPool 不写活动、不发提醒、不回引导确认、不执行花费上限的停止；`stdin.log` 用来重建 `_Reminders.uuids`。重放完了再接实时输出：如果这时已经超过花费上限，立刻停；超时按清单里的 `started_at` 重新计算。运行记录器加一个 `ClaudeRunRecorder.resume(record_id)`：不插新行，`_seq` 从库里的最大值接着往下编（`looper/db/schema.sql:1160-1166` 的主键是 `(record_id, seq)`）。
- **结果只交一次：** 在控制面停着的时候跑完的运行，重放到 result 事件后照常发 `WORK_RESULT`。如果旧控制面已经交过结果，而且 Planner 已经存进库，这个步骤就不会再是 `dispatched`，也就不会被接管，所以不会重复。
- **收尾：** Planner 存好结果、步骤离开 `dispatched` 之后，WorkerPool 删除运行目录。`carry_on_tasks` 之后仍没人接管的候选，结束运行，记录记成 `interrupted`。启动时也清扫那些步骤已经不在 `dispatched` 或评审中的运行目录。

### 7.7 S1：所有权和围栏

- **只允许一个控制面：** serve 建好连接池之后，用一条专用连接取会话级的 `pg_try_advisory_lock`，一直持有到进程退出。取不到就抛 `StartupCheckFailed`，写清楚是谁占着锁（从 `pg_locks` 和 `pg_stat_activity` 查；`application_name` 设成 `looper serve <pid>@<host>`）。专用连接通过断开通知和有期限的健康检查监测失锁；连接断开或无法确认仍持锁时，旧控制面立即停止接收工作和派工，取消进行中的控制面操作并退出，沿用有期限的停机兜底。失锁是本实例的终态：不得重连后继续工作或重新抢锁，普通数据库连接池即使自动重连也不得恢复读写和派工；数据库重启导致锁释放时同样适用。连接一断锁自然释放，退出不会留下死锁；keeper 的纪元围栏不能代替控制面失锁退出。按关键词只读检索，没找到在同一个库上起两个服务的测试，实现时要再确认一遍；调度器里那段 `SKIP LOCKED` 的说明（`scheduler.py:11-13`）要相应改掉。这是一处行为变更，见待决问题 3。
- **纪元：** 拿到锁之后，在数据库里把 `control_plane_epoch` 加 1，然后在 `initialize` 时交给看护进程。看护进程拒绝比自己见过的更低的纪元；遇到更高的就顶替。这样一来，一个卡在停机途中的旧控制面，再也写不进任何运行的 stdin。
- **运行和步骤一一对应：** 运行归 `worker_id` 所在的那个步骤。只有当库里这个步骤还是 `dispatched` 或在评审中、`worker_id`/`reviewer_id` 也对得上时，才接管。

### 7.8 S1：跨重启的契约

- **agent API 断线重试：** `looper/agents/mcp_server.py` 的 `AgentClient` 遇到连接被拒（请求肯定没送到）时，按 0.5 秒起、翻倍、最多 5 秒的间隔重试，直到 `LOOPER_AGENT_RECONNECT_SECONDS`（默认 120 秒）用完；收到带 `Retry-After` 的 503 也重试；只读操作（按操作定义的 `risk=read` 判定，包括 POST）遇到任何连接错误都可以重试；其余情况和今天一样（发出去之后超时的请求不重试，`LooperTimedOut`）。Pi 扩展做同样的修改。HTTP 服务在续跑和接管之后才开始监听（`serve.py:688-717`），所以重试成功时，被接管运行的令牌已经重新生效了。
- **令牌：** 托管运行的席位授权在签发时写进 `grant.json`（只存哈希），接管时 `RoomOps` 从这里加载。Planner 的令牌跟着 Planner 常驻进程走，那个进程本来就会重启，不需要持久化。
- **契约变了就走 `FULL`：** 用的是第 7.1 节的接管契约指纹。
- **Codex `app-server`、OMP rpc、Grok ACP** 在 S1 里不可托管：`turns` 排空会等它们，最多等到运行排空期限，报告里单独列出来。S3 里每种运行时各自补上「用重放重建请求 id 和 thread 状态」并通过测试之后，再把 `keepable` 改成 True。
- **systemd：** 生成的服务单元要加 `KillMode=process`，否则 `systemctl --user restart` 会把整个 cgroup 里的看护进程一起杀掉。这样一来，agent 的清理全靠 serve 自己的停机流程和下次启动时的 `sweep`，和 macOS 上一致。launchd 在 `bootout` 时会不会结束其他 session 里的进程，需要实机验证（第 7.9 节用例 12）。

### 7.9 验收用例

S0（用全新的数据库、一次性的 runtime root 和非 8767 的端口跑 `looper serve`；用替身 CLI 控制运行时长）：

1. 一个 Worker 在跑时开始 `runs` 排空：第二个派出的步骤在等位；排空期间发给 Manager 的消息不开始回合；那个 Worker 跑完之后状态变成 DRAINED；重启之后，等位的步骤开始跑，Manager 那条消息只答一次；`llm_records` 里没有 `interrupted`。
2. 运行比期限长：到期后像今天一样被中断，报告里列出这个运行；带 `--no-force` 时放弃部署，回到 SERVING。
3. 操作者调用 `POST /api/v1/system/drain_cancel {}` 取消排空，返回 200 和 SERVING 状态：等位的运行和 Manager 消息马上继续。
4. 排空期间到点的定时任务：重启之后按 catch-up 规则触发，不出现 `gate_failed`。
5. 升级计划：只改文档 → `NOOP`，进程 pid 不变；连续两次只改控制台 → 每次都是 `WEB_ONLY`，pid 和控制面加载指纹不变，控制台拿到新的 `index.html`，组件接口的 web 指纹每次都等于本次目标，再对同一目标规划得到 `NOOP`；改了 `planner.py` → `FULL`（S0 里没有看护进程），包括排空超时分支，确认旧控制面退出后才开始安装，安装完才启动新控制面。
6. 空窗期间发到 Telegram 的消息（用替身 Bot API）：重启后只投递一次；超过积压上限的只记录。

S1：

7. Claude Code Worker 跑到一半时做 `CONTROL_PLANE` 重启：keeper 没有以旧控制面为所有者的进程记录，新控制面的启动 `sweep` 不结束 keeper 或其子进程；运行不中断，`worker_id` 不变，只有一条运行记录，最后是 `completed`，花费只算一次，步骤没有被重派，活动记录是连着的；重启之后 Planner 发给它的引导能送到。真实 Claude CLI 跑一次，替身 CLI 覆盖其余分支。
8. 运行在控制面停着的时候跑完：启动后结果只交一次。
9. 空窗期间发出的 MCP 调用：重试后成功；超过重试窗口的返回错误结果。
10. 用旧纪元连接被拒；纪元更高的控制面顶替旧的；控制面一直不回来时，孤儿保护在期限后结束运行。
11. 契约变了（改一下 `WORK_CONTRACT`）→ 计划是 `FULL`。
12. `looper service restart` 在 launchd（macOS）和 systemd（Linux）上都不会杀掉看护进程。
13. 杀掉一个看护进程：只有那一个运行中断后重做，别的运行不受影响。
14. 反向用例：同一个库上再起一个 `looper serve`，它拒绝启动，并说明谁占着锁。
15. 断开第一个控制面的持锁连接，再启动第二个实例：第一个停止接收工作、停止派工并退出，第二个取得锁后启动；第一个的普通连接池恢复连接也不能继续工作。分别覆盖直接断开专用连接和数据库重启导致失锁两种情况。

### 7.10 任务卡

| 卡 | 阶段 | 内容 | 验收 |
|---|---|---|---|
| C1 | S0 | `components.toml` 和指纹计算（git 树、wheel、安装目录三种来源）；控制面加载快照与按请求读取的 web 指纹；`POST /api/v1/system/components`；CI 的归类检查；`decide` 函数；`looper service upgrade --dry-run` | 用例 5；`decide` 的每个分支各一行参数化测试 |
| C2 | S0 | `DrainState`、四个暂停点、判定完成、期限；POST 操作 `system.drain`、`system.drain_cancel`、`system.drain_status`；`system/status` 里的 `drain`；控制台横幅 | 用例 1–4 |
| C3 | S0 | `looper service upgrade` / `restart --drain` 的执行和部署报告；排空、停止并完成脱离、确认退出后安装再启动；`web/dist` 原子替换；Telegram 不再丢积压 | 用例 1、5、6；部署报告的字段 |
| C4 | S1 | `looper/agents/keeper/`：运行目录、进程模型、协议 1.0、纪元、孤儿保护、日志上限；导入白名单的 CI 检查 | 用例 10、13；控制面被杀掉之后子进程继续跑，从任意偏移补读 |
| C5 | S1 | `KeptRun`；`_execute` 拆成 prepare 和 consume；计划内脱离；`keepable` 开关；systemd 加 `KillMode=process` | 用例 7（替身 CLI）、12 |
| C6 | S1 | 启动时扫描候选；`end_unfinished_records(keep=…)`；`adopt`；重放重建；`ClaudeRunRecorder.resume`；释放运行目录 | 用例 7、8 |
| C7 | S1 | 单控制面锁、失锁退出和纪元；`grant.json`；`mcp_server` 和 Pi 扩展的断线重试；接管契约指纹接入 `decide` | 用例 9、11、14、15 |
| C8 | S1 | 真实链路验证：真实 Claude CLI 跑一次用例 7，在 macOS 和 Linux 上各跑一次用例 12，收集第 8.2 节的指标 | 截图和部署报告附在 PR 上 |

## 8. 风险与监控项

### 8.1 风险

| 风险 | 后果 | 缓解 | 剩下的风险 |
|---|---|---|---|
| 新控制面消费旧运行，两边版本不一致 | 报告被判成违反契约、工具调用失败 | 接管契约指纹不一致就走 `FULL`；清单带版本号 | 指纹没覆盖到的隐含约定，靠用例 7 和真实运行去发现 |
| 进程还在跑，磁盘上的代码被换掉 | 旧进程导入到新模块，新旧代码混在一起 | 排空后先停止、完成脱离并确认退出，再安装启动；看护进程只用启动时导入的代码，CI 检查 | 版本隔离安装目录仍是待办；存活的看护进程依赖导入约束 |
| 看护进程占资源 | 每个运行多一个约 24 MB 的进程（实测，含共享页） | 运行的并发上限不变；`system/status` 显示看护进程的数量 | Claude CLI 进程本身占多少内存需要实测；预计远大于看护进程（推测） |
| 服务管理器把看护进程一起杀了 | 运行中断，退回到今天的样子 | systemd 加 `KillMode=process`；launchd 实机验证 | macOS 上的行为需要验证 |
| 重放的结果和原来不一样 | 内存里的计数有偏差 | 重放只重建计数，不产生副作用；结果按步骤状态只交一次 | 新代码重新解释旧事件，计数可能略有出入（只影响观测，不影响结果） |
| 空窗期间平台工具调用失败 | 模型看到错误结果 | 连接被拒时重试，最长 120 秒；HTTP 在接管完成之后才开始监听 | 空窗比 120 秒长时（比如表结构变更要先做 `pg_dump`）会失败，需要实测备份的耗时 |
| 单控制面锁获取失败或失锁 | 第二个实例起不来；旧实例若继续工作会脑裂 | 报错里写清楚是谁占着；专用连接断开（含数据库重启）后旧控制面停止接活并退出，连接池不得恢复工作 | 不提供强制夺锁；退出后的恢复依赖服务管理器重新启动 |
| 孤儿运行一直在花钱 | 浪费 | 孤儿保护默认 30 分钟；接管时立刻检查花费上限 | 空窗那几秒里花费上限暂时不生效 |
| 日志占满磁盘 | 磁盘写满 | stdout 上限 512 MB；释放和清扫运行目录 | — |
| 排空迟迟完不成 | 部署变慢 | 两个期限；报告里列出在等谁 | 期限到了照样会中断一部分运行 |
| 安全 | key 和输出泄漏 | 看护进程的环境和今天子进程的环境一样；目录 0700、套接字 0600，只有运行服务的用户能写 stdin；日志的敏感程度和 `llm_record_events` 一样 | 有 shell 的 agent 能读运行目录，这和今天能读 runtime root 是同一个已知缺口（开发计划第 5.2 节） |

### 8.2 监控与告警

Looper 是本机服务，没有独立的指标系统。可观测性落在三个地方：结构化日志、`POST /api/v1/system/status` 的字段，以及控制台概览和 Manager 通知。

- **部署报告**（`looper service upgrade` 打印出来，同时写一行结构化日志）：`plan`、`reasons`、`drain_seconds`、`gap_seconds`（从 SIGTERM 到 HTTP 重新可用）、`kept_before`、`adopted`、`interrupted{reason=deadline|full|not_keepable|keeper_lost|protocol}`、被中断运行的估计花费（取运行记录里已经流出来的用量）。
- **`system/status`：** `drain.phase` 和在等谁；`keepers.alive`、`keepers.orphaned`、`keepers.log_bytes`；`components` 的指纹。
- **日志行：** `upgrade plan=… reasons=…`；`drain phase=… waiting=…`；`adopt run=… offset=… replay_events=… replay_ms=…`；`keeper orphaned run=…`；MCP 桥打到 stderr 的 `agent api retry attempts=… waited=…`。
- **告警**（作为概览里的问题项，P1 同时通知 Manager）：
  - P1：`CONTROL_PLANE` 重启之后 `adopted < kept_before`，有托管的运行丢了。
  - P1：第二个控制面被拒绝启动，说明有人在同一个库上又起了一个服务。
  - P2：排空到了期限，中断了运行（列出是哪些）。
  - P2：有看护进程超过 10 分钟没有控制者。
  - P3：`gap_seconds > 30`；一次重启里 MCP 重试用完的次数大于 0。
- **基线**（S0 动工之前，由维护者在运行中的库上跑一次只读查询）：最近 14 天里 `status = 'interrupted'` 的运行记录，按服务启动的时间分桶，统计条数、角色、运行时和已经流出来的用量。由此得出每次重启的浪费，以及 Worker 实际用了哪些运行时（决定 S3 的顺序）。
- **演练**（用一次性的 runtime root、端口和数据库）：控制面运行中被 `kill -9`（S1：没有干净脱离的运行在启动时被结束，确认不会失控）；`kill -9` 一个看护进程（只影响一个运行）；契约变更（走 `FULL`）；在 launchd 和 systemd 上各做一次重启。

## 9. 待维护者决策的问题

1. **排空的语义和期限。** 推荐：按第 7.3 节做。排空期间新运行等位，新的 Manager 回合留到重启后再开始，定时任务暂停触发（重启后按 catch-up 补），Planner 回合照常进行；运行排空期限 20 分钟，回合排空期限 3 分钟，到期像今天一样中断。这意味着部署期间各任务最多停顿 20 分钟（S1 之后，大多数发布只停顿 3 分钟以内）。
2. **看护层放在哪：每个运行一个看护进程，还是一个常驻宿主。** 推荐每个运行一个（B2）：看护代码升级不打断任何运行，一次崩溃只影响一个运行，协议也最简单。代价是每个运行多一个约 24 MB 的 Python 进程。运行器（S3）由同一种看护进程持有。
3. **要不要把「一个数据库只跑一个 `looper serve`」做成启动检查。** 推荐要。这是 B 依赖的前提，而且第二个实例会被明确拒绝，而不是悄悄地不安全。#909 里剩下的两条多实例问题，以「拒绝第二个实例」为由关闭；方案 C 不做。
4. **部署顺序和安装布局。** 推荐：S0 起部署脚本先从 git 树或 wheel 算出计划；需要重启时，排空完成或到期后先停止旧控制面，S1 还要完成托管运行的脱离，确认旧进程退出后才 `git merge --ff-only` 或 `pip install`，最后启动新控制面。S1 继续就地安装，并约束看护进程只用启动时导入的代码。按版本分开的不可变安装目录（`releases/<version>` 加一个 `current` 链接）先记成待办，等到看护进程跨好几个版本长期存活成为常态再做。

维护者于 2026-10-31 确认本阶段的五项决定：只有在回合排空期间收到 SIGTERM 才脱离托管运行；`CONTROL_PLANE` 升级对不能托管的运行等待到 `--drain-timeout`；失去控制面锁时，控制面结束自己直接启动的 agent 进程，保留托管运行；单控制面锁在 keeper 开关默认开启的同一次变更中改为无条件启用；维护者的服务经过约一周、至少 20 次 `CONTROL_PLANE` 部署且未出现 P1（接管数少于保留数）后，再翻转默认值。
