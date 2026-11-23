# RFC：平台子 agent 与持久化运行树 v0

- **状态：** 决定已定，只有文档。第 3 节 D1–D9 来自维护者的产品设计（「平台级子 agent 与运行树」一节和第 4 期的「持久化运行树」）；D10–D20 是维护者 2026-11-22 对第 12 节 11 个问题的答复（全部按推荐）。独立评审后按代码修正了只读的执行方式、预算预留、重启和取消（第 4.3、4.5、4.7、4.8 节）。本文合入后按第 10 节的 PR 顺序实现。
- **命名：** 产品设计里这一项叫 M4；它和本仓库开发计划里已经交付的「M4 项目 skill」不是一回事，本文不用这个编号。
- **需求：** 规划者（Planner）和执行者（Worker）通过 Looper 自己的工具派子 agent，而不是用运行时私有的子 agent 工具（Claude Code 的 `Agent` 工具、agent teams 等）。每个子 agent 是运行树上的一个节点：有父节点、有预算、要过准入、能被取消、结果按约定的方式交回、失败有类型，并且在 Dashboard 上看得到。运行树持久化：服务重启或热升级以后，不会重复派出同一个子 agent。
- **相关：** [Advisor RFC](advisor-v0.zh-CN.md)（`agent_spawns`、`spawn` 原语、`run_kind` 和 `parent_record_id`：本文在它们之上扩展）；[Agent room RFC](agent-room-v0.zh-CN.md)（令牌、座位、推送和拉取、consult）；[引擎可靠性 RFC](engine-reliability-v0.zh-CN.md)（第 4 节失败分型、第 5 节全局准入：本文给子 agent 补上它们的规则）；[Agent 系统架构](../agent-system.md)（「A task's agent room」「Consults of the advisor」「Concurrency limits」「Kept runs across a planned restart」）。
- **核对基线：** `main` `b0aa9fff5`（初稿是 `ddc2d00df`，评审后按这一版重新核对）。第 2 节除第 2.2 节的抽样以外都是读代码得出的，每项标了出处。
- **术语：**
  - **节点：** 运行树上的一次运行（`llm_records` 的一行）。
  - **派出者（spawner）：** 发起派生的那次运行：Worker 的一次运行，或规划者的一个回合。
  - **子 agent：** 派出者经 `looper_agent_spawn` 派出的运行。第一期只有一种：`explorer`（只读调研）。
  - **派生（spawn）：** 一次派出，`agent_spawns` 的一行，从排队到结束。它管一个子运行（`child_record_id`）。
  - **子树：** 一个节点和它派出的全部子 agent。第一期只有一层，所以子树就是「父 + 它的子 agent」。

## 1. 背景与目标

现在 Looper 里已经有三种「一个运行引出另一个运行」：

- **advisor 请教：** 规划者向更强的模型请教，走 `agent_spawns` 和 `spawn.run_child`，没有任何工具；
- **办公台的 consult：** 有人问一个已经结束的座位，Looper 恢复那个会话的只读分叉来回答，记在 `room_consults`；
- **运行时自己的子 agent：** Claude Code 的 `Agent` 工具和 agent teams。它们跑在父运行的进程里，Looper 看不到，也控制不了。

第三种是缺口：

- **看不到：** 原生子 agent 的花费并在父运行里，Dashboard 上没有节点，也不知道它们在做什么；
- **管不住：** 没有单独的预算、数量和并发限制，也不能单独取消。它们能改文件，所以同一个目录可能有不止一个写入者，这和「计划是唯一的事实来源」相冲突；
- **规划者用不了：** 规划者的决定要从它当轮的结果里读，所以它没有 agent teams。它只能自己读仓库，把上下文和花费都耗在读代码上。

**目标：**

- 规划者和 Worker 通过 Looper 的 MCP 工具派子 agent；服务端检查种类、Profile、预算、数量和层数；
- 子 agent 是运行树上的节点：花费单独计算、进准入和并发、能取消、失败有类型；
- 派生持久化：有幂等键、交付方式、「完成中」状态和级联取消；重启以后不重复派出；
- 任务页有「运行树」，能看到谁派了谁、为什么、花了多少、在等谁；
- 默认关闭，按项目打开；关闭时提示词、工具列表、命令行和行为逐字节不变。

**不在目标内：**

- 改文件的子 agent。需要改文件的活，由 Worker 请规划者加一个步骤（D3）；
- 多层嵌套（D4）；
- 关掉运行时自己的子 agent。那是另一项（「原生子 agent 收口」，第 4.11 节），要先有本文的数据才能决定；
- advisor 请教换一条路走。规划者照旧通过 [DECISION] 的 `advise` 请教（D12）。

## 2. 现状（读代码得出）

### 2.1 已有的子运行

| 机制 | 怎么跑 | 记在哪 | 出处 |
|---|---|---|---|
| advisor 请教 | `spawn.run_child`：一个新进程，没有任何工具（`Access.ADVISOR`），系统提示整个替换，在系统临时目录下一个私有空目录里跑，跑完删掉；一个截止时间；按记录的花费检查上限，剩下不到 `MIN_RUN_USD`（0.10 USD）就不启动 | `agent_spawns`（`kind` 只有 `advisor`，状态 `drafting → linting → asking → needs_info → answered/blocked/failed/over_budget`，用 `move(expect=…)` 做 CAS），每轮一行 `llm_records`（`run_kind` `advisor`，`parent_record_id` 是规划者的那个回合） | `looper/agents/spawn.py`、`looper/db/agent_spawns.py`、`looper/agents/advisor.py` |
| 一步到位的派生 | `spawn.spawn()`：建一行 `agent_spawns`（直接 `asking`），跑一次 `run_child`，把结果写回。模块注释写明「M4's tools call it」，但现在没有调用方 | 同上 | `spawn.py` |
| 办公台 consult | 恢复一个已结束座位会话的分叉，只读（`Access.CONSULT`：Claude Code 只有 `Read`、`Glob`、`Grep`；Codex 只读沙箱；OMP、Grok、Pi 各自的读工具），占它所在任务里同类座位的并发（`WorkerPool.slot_for`）；`room_consult_budget_usd`（默认 0.50）、每个任务 `room_consults_per_task`（默认 10）、单独的锁和队列。`RUNTIME_READ_ONLY` 里的运行时（Claude Code、Codex、Grok、DSH、Pi）在原目录跑，其他的（例如 OMP）跑在 HEAD 的一个分离检出里。服务停止时把 `running` 改回 `queued`，启动时重新排队；被杀的记为 `state=failed`、`fallback_reason=stopped` | `room_consults`，一个 `consult-<12 hex>` 运行 | `looper/agents/consult.py` |
| 运行记录的树 | `llm_records.parent_record_id` 和 `run_kind`（`RUN_KINDS`）；`runs.for_task` 返回每个运行的 `kind` 和 `parent_record_id` | — | `looper/db/llm_records.py` |
| 界面 | 任务页的 Agents 标签把 advisor 和判别的运行列在发起它的规划者运行下面；流水线把 consult 画成步骤旁的虚线小节点 | — | `web/` |

### 2.2 运行时自己的子 agent

- **Claude Code：** `Agent`（以前叫 `Task`）工具起一个子 agent，跑在同一个进程里。它的请求计入父运行的 `total_cost_usd`，事件带 `parent_tool_use_id`。agent teams 在 Claude Code 的 Profile 上默认打开（规划者除外），队友也跑在进程里（`--teammate-mode in-process`）。Looper 能从事件里认出它们（`system/task_started` 的 `task_type: local_agent`），但不能单独限制或取消（架构文档「Agent teams」「What a run spent」）。
- **线上抽样**（不是代码事实：一个长期运行的实例上，11 月 10 日以后成功的 Claude Code 运行，每种角色随机抽 25 条，只用只读接口 `runs.list`、`runs.events`，只取计数。复核方法：对任意一段时间的运行，统计事件里 `Agent` 工具的调用和 `system/task_started` 的 `local_agent`）：
  - Worker：8 条（32%）用了 `Agent` 工具，共 12 次，`subagent_type` 是 `general-purpose`（11 次）和后台运行（1 次）；
  - 这些子 agent 一共调用了 397 次工具，其中 `Bash` 379 次、`Write` 和 `Edit` 9 次：它们会改文件；
  - 规划者、Reviewer：0 条。
- **其他运行时：** codex-cli 0.169.0 有自己的多 agent 工具；Grok 有子 agent。Looper 同样看不到。

### 2.3 令牌和 MCP 入口

- 规划者有 `planner.` 令牌；在开了办公台的项目里，每个 Worker、Reviewer 运行有自己的 `run.` 令牌，写明任务和座位。两种令牌都经 `looper mcp-server`（`/api/agent/v1/`）调用 Looper 的工具，运行结束时作废，带令牌的 MCP 文件也删掉（`RoomOps.revoke_run`、`library_with_server`）。
- 保留运行（热升级）的桥接在连接被拒或 HTTP 503 时会重试；其他失败只重试 `RETRY_SAFETY` 里标为安全的调用（`looper/agents/ops/room.py`、`ops/vault.py`）。
- 规划者的写工具只在 Looper 发给它的回合里能用（`needs_user_turn`）。

### 2.4 并发、预算、取消、重启

- **并发：** 只按任务算：Worker 默认 16 个、Reviewer 8 个，规划者不限（架构文档「Concurrency limits」）。账号层面的准入在[引擎可靠性 RFC](engine-reliability-v0.zh-CN.md) 第 5 节，还没实现。
- **预算：** 有每次运行的上限（`run_budget_usd`，Claude Code 是 `--max-budget-usd`，按供应商价计价的运行由 Looper 软停）、任务上限（`task_budget_usd`）、advisor 和 consult 各自的上限。没有「子树」的概念：advisor 的花费记在规划者那份里（`task_costs`）。
- **取消：** 杀一个 Worker 会结束它的进程树（`WorkerPool.kill`、`proc_tree.end`），原生子 agent 跟着一起结束；advisor 请教被撤回或过期时有 `withdrawn`、`timeout`。
- **重启：** 启动时，没结束的 advisor 请教记为 `state=failed`、`failure=interrupted`；consult 会重新排队。能作为保留运行跨过计划内重启的，只有开了 keeper 时、运行时可保留（`RuntimeAdapter.keepable`，今天只有 Claude Code）、并且不是 steer 或托管方式的 Worker 和 Reviewer 运行（「Kept runs across a planned restart」）。`spawn.run_child` 不经过 keeper。
- **只读的执行方式**（`ReadOnly`，`looper/agents/runtimes/__init__.py`），规划者的只读运行就靠它：
  - `PERMISSIONS`：运行时拒绝会改东西的工具和命令，并报告每次拒绝。有 Claude Code，以及靠执行守卫的 DSH；
  - `SANDBOX`：运行时的沙箱让写操作失败，不报告。有 Codex、coco，TTADK 启动的 Codex、coco 也照样转交沙箱模式；
  - `TOOLS`：只给读工具，没有 Shell 和写工具，但 MCP 工具可能还在。有 OMP（要有读工具能力，否则退成 `CHECK`）、Grok、Pi，其中 Pi 的规划者参数还带着 `mcp__looper__*`；
  - `CHECK`：做不到，只能事后比较目录。有 Cursor，以及没声明的运行时。

## 3. 已定的前提

| 编号 | 内容 |
|---|---|
| D1 | 入口是三个 MCP 工具：`looper_agent_spawn`、`looper_agent_wait`、`looper_agent_cancel`，沿用办公台的按运行令牌，服务端校验 |
| D2 | 第一期的种类：`explorer`（只读调研，读代码出摘要）和 `advisor`（复用 advisor 请教）。advisor 只有规划者能派，Worker 只能派 explorer |
| D3 | 第一期的子 agent 只读。要改文件的活，由 Worker 请规划者加步骤；计划是唯一的事实来源，同一个目录只有一个写入者 |
| D4 | 嵌套只有一层；限制每次运行和每个任务能派的数量 |
| D5 | 预算从父运行的额度里切出来，由代码强制执行；子 agent 的并发算进任务的并发上限 |
| D6 | 子 agent 照常拿到 Looper 的 harness 层（边界规则、项目背景、任务背景） |
| D7 | 规划者可以在出计划前派 explorer；它在本轮内等（有上限），超时的结果在下一轮送达 |
| D8 | 持久化运行树：派生记录有幂等键、等待策略、「完成中」状态和级联取消，状态转换用 CAS；热升级后重复派发为 0 |
| D9 | 任务页加「运行树」：树和时间线；节点显示创建者、创建原因、模型、状态、耗时、自身和整棵子树的花费、结果摘要 |
| D10 | 子 agent 在任务里用自己的并发类别 `subagent`，默认 4（Q1） |
| D11 | 第一期 Reviewer 不能派子 agent（Q2） |
| D12 | advisor 不走派生工具，规划者照旧用 [DECISION] 的 `advise`（Q3） |
| D13 | explorer 不能上网（Q4） |
| D14 | explorer 读派出者的现场目录；做不到真正只读的运行时改在检出里读（Q5；评审后按代码修正了哪些运行时用检出，第 4.3 节） |
| D15 | 热升级时，开了 keeper 就把 explorer 作为保留运行接管（Q6；评审后限定为可保留的运行时，第 4.7 节） |
| D16 | 规划者没等到的结果：开了办公台就发办公台消息，否则附在下一个回合（Q7） |
| D17 | Worker 结束时还没结束的子 agent 一律取消（Q8） |
| D18 | 默认上限：一个 0.50 USD，一个任务 3.00 USD，一次运行 4 个，一个任务 20 个，同时 4 个（Q9） |
| D19 | explorer 的内置 Profile 是 `claude-code/sonnet-5-5`，换更便宜的模型由评测决定（Q10） |
| D20 | 结果没过结构检查时交回原文，标「未通过结构检查」（Q11） |

## 4. 设计

### 4.1 节点和边

- 子 agent 的运行是 `llm_records` 的一行：`run_kind` `explorer`，`parent_record_id` 是派出者的记录（Worker 的那次运行，或规划者的那个回合），`task_id` 和 `metadata.step_id` 跟父节点一样。
- 每个子 agent 还有一行 `agent_spawns`：它的种类、简报、预算、状态、交付方式、幂等键、结果和失败类型。运行树由这两张表拼出来，不另建一张表。
- 规划者的节点按回合分：一个回合派出的 explorer 挂在这个回合下面。规划者会话里跨回合的子 agent，按派出它的那个回合归属。

### 4.2 工具

只在打开了 `subagents` 的项目里出现（第 6 节）。规划者用 `planner.` 令牌调用，Worker 用 `run.` 令牌调用。Reviewer 第一期没有这些工具（D11）。工具就是 D1 的三个，不另加查询工具：要看自己派过哪些，用不带 `spawn_ids` 的 `looper_agent_wait`。

| 工具 | 参数 | 返回 | 级别 |
|---|---|---|---|
| `looper_agent_spawn` | `kind`（`explorer`）；`brief`（`goal`、`deliverable`、`scope`：路径或模块列表，可选 `questions`，各有长度上限）；可选 `profile`（id 或名字，只能是项目允许的、能跑 explorer 的）；可选 `budget_usd`；可选 `wait_seconds`（0 到 `wait_max_seconds`，默认 0：不等）；可选 `key`（模型自己的去重键） | `spawn`：`id`、`state`、`budget_usd`、`profile`、`delivery`；等到了就带 `result` | write |
| `looper_agent_wait` | 可选 `spawn_ids`（这次运行派的，最多 8 个；不填就是这次运行，规划者是本任务，派过的全部）；可选 `wait_seconds`（0 到 `wait_max_seconds`，默认 0） | 每个的 `state`；结束了的带 `result` 或 `failure`；另附剩余的预算、数量和并发 | read |
| `looper_agent_cancel` | `spawn_id`；可选 `reason` | 取消后的 `spawn` | write |

- **校验顺序**（`SpawnService.admit`）。前一项不通过就拒绝，不建行：
  1. 令牌和身份：只有规划者和 Worker 的令牌能派；explorer 自己没有任何 Looper 工具，所以第二层在结构上就派不出来（D4）；
  2. 种类和角色：`explorer` 规划者和 Worker 都能派，`advisor` 第一期不走这个工具；
  3. 规划者的写工具照旧只在 Looper 发给它的回合里能用；
  4. 简报：类型化、在长度上限内；`scope` 必须是派出者工作目录下的相对路径；
  5. Profile：在项目允许的列表里（`projects.choosable_profiles`），并且它的运行时按第 4.3 节的表能真正只读地跑；需要检出的，派出者的工作目录要在一个 git 仓库里；
  6. 数量：本次运行不超过 `subagents_per_run`，本任务不超过 `subagents_per_task`；
  7. 预算：按第 4.5 节切出来，切出来不到 `MIN_RUN_USD` 就拒绝；
  8. 熔断：这次运行最近连续 3 个子 agent 都失败了，就拒绝（第 4.9 节）。
- **拒绝**是一个类型化的错误：`refused` 加原因码，原因码有 `kind`、`role`、`brief`、`scope`、`profile`、`no_checkout`、`limit_run`、`limit_task`、`budget`、`breaker`、`nesting`、`off`。模型能据此改了再来；每次拒绝记一行活动。
- **幂等：** 桥接给每次工具调用一个调用 id，重试时带同一个；服务端把它（或模型给的 `key`）存成 `idempotency_key`，`(parent_record_id, idempotency_key)` 唯一。同一个键再来一次，返回已有的那一行，不会再派一个。`looper_agent_spawn` 因此可以列进 `RETRY_SAFETY`，热升级时桥接重试它是安全的。

### 4.3 explorer 怎么跑

- **访问级别：** 新的 `Access.EXPLORE`：只有读工具；没有 MCP 服务器（也就没有 Looper 的工具）；没有网页工具（D13）；没有运行时自己的子 agent 工具（Claude Code 的 `Agent`、agent teams，其他运行时的同类工具都关掉），所以 explorer 下面不会再有进程内的子节点；新会话。它不复用规划者的只读参数：那组参数带着网页搜索和 MCP。
- **只读必须真的做到**（D3、D14）：要么运行时自己不能写，要么跑在一个用完即删的检出里。两样都做不到就拒绝（`profile`）。按运行时：

  | 运行时 | 它的只读方式 | explorer 怎么跑 | 在哪读 |
  |---|---|---|---|
  | Claude Code | `PERMISSIONS` | 只放行 `Read`、`Glob`、`Grep`（和 consult 一样：手动权限模式、不弹确认，其余工具一律拒绝）；空的 MCP 配置（`--strict-mcp-config`）；不开 agent teams | 派出者的工作目录 |
  | DSH | `PERMISSIONS` | 执行守卫拒绝写和 Shell；不配 MCP | 派出者的工作目录 |
  | Codex（含 TTADK 启动） | `SANDBOX` | 只读沙箱；不配 MCP；关掉网页搜索 | 派出者的工作目录 |
  | coco（含 TTADK 启动） | `SANDBOX` | 只读沙箱；不配 MCP | 派出者的工作目录 |
  | OMP（有读工具能力的版本） | `TOOLS` | `--tools read,glob,grep`；不配 MCP（MCP 工具在 `--tools` 下仍会保留，所以一个都不给） | 检出 |
  | Grok | `TOOLS` | `--tools read_file,grep,list_dir`；不配 MCP | 检出 |
  | Pi | `TOOLS` | `--tools read,grep,find,ls`（不带 `mcp__looper__*`）；不配 MCP | 检出 |
  | Cursor、没有读工具能力的 OMP、未声明的运行时 | `CHECK` | 拒绝（`profile`） | — |

  - **为什么 `TOOLS` 也要检出：** 这类运行时没有沙箱，只是工具少，只靠工具列表约束；运行时一旦升级，多出一个能写的工具，事后的 `wrote_files` 也只能丢掉结果，改动回滚不了。检出用完就删，派出者的目录不会被碰到。
  - **检出：** 派出者工作目录所在仓库的一个分离检出，放在运行时根目录下 `.local/explorers/<spawn>/`。它包含派出那一刻的已提交内容和未提交的改动：用一个临时 index 生成一棵树，再检出它，不动派出者的 index、stash 和分支（具体做法在 PR1 用临时仓库验证）。不在 git 仓库里的目录派不了这类 explorer（`no_checkout`）。检出在任何结局下都删掉（第 4.8 节）。
  - **启动参数快照测试：** 每个运行时一条测试，把 explorer 的启动参数和上表对齐；Claude Code、Codex 再用真实 CLI 各跑一次，确认写操作被拒（第 11 节）。
- **系统提示：** harness 层（边界规则）、项目背景和知识索引、任务小节（需求，加上 Worker 派出时的步骤）、explorer 角色提示，最后是 `EXPLORER_CONTRACT`（D6）。
- **第一轮输入：** 简报（目标、要交回什么、范围、问题），外加一句：它是谁派的、为什么派。
- **结果契约：** 最后一个 JSON 对象：`summary`（不超过 2,000 字）；`findings`，每条有 `claim` 和 `evidence`（`path`、可选 `lines`）；`open_questions`；`confidence`（`high`、`medium`、`low`）。代码会检查：`evidence.path` 必须在派出者的工作目录下并且存在。读不出 JSON 时，`result.text` 保留裁剪过的原文，`result.verified` 为 `false`，交回的结果写明「未通过结构检查」。
- **角色和 Profile：**
  - 新角色 `explorer`，在 Settings → Roles 里可以选 Profile、超时（默认 300 秒）和最多轮数（默认 40）；
  - 内置 Profile 是 `claude-code/sonnet-5-5`，比派出者常用的模型便宜；
  - 派出时可以从项目允许的列表里另选一个。
- **怎么启动：** 一个子进程，用 `spawn.run_child` 的同一套边界：截止时间、进程树回收、按记录的花费检查上限、拒答、类型化的失败。但工作目录照上表定，不用 `run_child` 的私有临时目录。可保留的运行时（今天只有 Claude Code，非 steer、非托管方式）在开了 keeper 时经保留运行的句柄启动，以便热升级时被接管（第 4.7 节）；其他运行时照常是一个普通子进程。

### 4.4 结果怎么交回（等待策略）

每个派生有一个持久化的交付方式 `delivery`，重启以后照它交：

| `delivery` | 什么时候用 | 怎么交 |
|---|---|---|
| `tool` | `looper_agent_spawn` 或 `looper_agent_wait` 带 `wait_seconds` > 0，并且在时限内结束 | 作为工具调用的返回值 |
| `room` | 派出者没等，或者等超时了；项目开了办公台 | Looper 往办公台写一条 `spawn_result` 消息给派出者的座位（规划者是 `planner`），带摘要（裁剪到 1,500 字）和 `spawn_id`；完整结果用 `looper_agent_wait` 取。推送和拉取照办公台现有规则：Worker 推送，规划者被 [ROOM] 回合唤醒 |
| `turn` | 规划者没开办公台 | 把结果附在规划者下一个回合的消息后面，最多 4,000 字；超出的写明用 `looper_agent_wait` 取 |

- **规划者（D7）：** 允许在 [PLAN] 回合里先派 explorer 再出计划。它在本轮内用 `looper_agent_wait` 等，每次最多 `wait_max_seconds`（默认 120 秒），整个回合照旧受回合超时约束。等不到的结果按上表在下一轮送达。规划者的决定照旧从它当轮的结果里读，不用等子 agent 的回合。
- **Worker：** 可以派了以后接着干，在要用的时候再 `looper_agent_wait`；也可以靠办公台推送拿到结果。Worker 结束时还没结束的子 agent 会被取消（第 4.8 节，D17）。
- `delivered_at` 记录结果第一次交到派出者手里的时间。交付是幂等的：同一个结果不会推两次。

### 4.5 预算

- **怎么切（D5）：** 派出时预留 `budget = min(请求的, 父运行剩余, 子树剩余, 任务剩余)`，其中每一项只在它有定义时参与：
  - 请求的：不填时用项目的 `subagent_budget_usd`，再不填用 explorer 角色的 `run_budget_usd`，默认 0.50；
  - 父运行剩余 = 父运行的单次上限 − 父运行到这一刻的花费 − 它的子 agent 已预留和已花的。父运行没有单次上限，或者它的花费不知道，这一项就不参与（不当作 0）；
  - 子树剩余 = `subagent_task_budget_usd` − 本任务全部子 agent 已预留和已花的；
  - 任务剩余 = `task_budget_usd` − 任务已花 − 本任务子 agent 已预留的。任务没有上限时这一项不参与。

  切出来不到 `MIN_RUN_USD`（0.10）就拒绝（`budget`）。
- **父运行的花费不知道时只有一种口径：** 父运行剩余这一项不参与，只用子树上限和任务上限卡。派生行记下 `parent_spend_known = false`，运行树上可以看到。第 7 节同此。
- **预留不会超卖：** 预留在一个事务里做。先锁住任务那一行（`SELECT … FROM tasks WHERE id = $1 FOR UPDATE`），在锁里重新算三个剩余，满足才插入派生行（预留额就是它的 `budget_usd`），然后提交。同一个任务的两个派生，即使幂等键不同、同时到达，也只能一个接一个地拿到剩余；不同任务之间互不影响。释放（结束、取消）在同一把锁里改行。
- **父运行到这一刻的花费：** 按供应商价计价的运行用 Looper 已有的流式计价（`RunSpend`）。运行时自己报花费的运行（例如订阅登录的 Claude Code），用已经流出的用量按模型价格估算，和 consult 的计价方式一样。两样都没有，就是「不知道」。
- **子树算进父运行的上限：** 父运行自己的花费加上子树的花费超过父运行的上限时，Looper 用已有的软停把父运行停下（和 `cap_stop` 一样）。子 agent 自己的进程上限就是它的预留额（`--max-budget-usd`，或者 Looper 的软停）。
- **记账：** 子 agent 结束时，把它的实际花费记进 `agent_spawns.spent_usd`，同时释放预留。运行被切断时从它的记录里算。`task_costs` 新增一份 `explorer`（第 4.10 节），不再混在派出者那份里。

### 4.6 准入和并发

- **任务层（D5、D10）：** 子 agent 算进任务的并发上限，用自己的一类：`task_concurrency_limits` 加一个角色 `subagent`，任务默认 4 个。它不用 consult 的 `WorkerPool.slot_for`：那会让子 agent 和 Worker 抢同一类位子，Worker 占满位子、都在等自己的子 agent 时就卡死。排队中的派生状态是 `queued`，等到位子以后才启动。
- **账号层（接[引擎可靠性 RFC](engine-reliability-v0.zh-CN.md) 第 5 节）：**
  - 子 agent 的运行也要向它 Profile 所在的账号要槽位，车道跟派出者一样：你的任务是 `USER_TASK`，定时任务是 `BACKGROUND`；
  - 原 RFC 第 5.1 节「嵌套不等待」说的是回合里同步调用的子运行。这里的子 agent 可以和父运行并行跑，所以要正常排队；
  - **借位规则（新增）：** 派出者在 `looper_agent_wait`（或带等待的 spawn）里阻塞时不发模型请求，它的一个账号槽位就借出去：
    - 只借一个槽位，借给它正在等的子 agent 里排在最前面、还在排队的那一个（不论它等的是 1 个还是 8 个）。其余的照常排队；
    - 借来的槽位在这个子 agent 结束，或者派出者的等待返回时归还。等待到时（最多 `wait_max_seconds`）一定返回，所以借出去的槽位不会被长期占着；
    - 被借出槽位的父运行在等待返回后，照常重新要槽位；
    - 账号层的死锁就靠这一条避免；任务层不需要借位，因为 `subagent` 是单独的一类。
  - 账号冻结（额度用完）时，子 agent 和其他运行一样排队等重置，借位也不例外。派出者在等它时，返回的状态是 `queued`，带 `held: quota` 和重置时间。
- **数量：** 每次运行最多 `subagents_per_run`（默认 4），每个任务最多 `subagents_per_task`（默认 20）。

### 4.7 持久化运行树（D8）

`agent_spawns` 加 `explorer` 种类。explorer 的状态：

| 从 | 事件 | 到 |
|---|---|---|
| — | 校验通过，在任务锁里建行（带幂等键和预留） | `queued` |
| `queued` | 拿到任务和账号的位子，子进程启动 | `asking`（写入 `child_record_id` 和子进程的身份：pid 和启动时间） |
| `asking` | 子进程结束，记录已关 | `finishing` |
| `finishing` | 记账、解析结果、交付登记都完成 | `answered` 或 `failed` 或 `over_budget` |
| `queued` | 取消（第 4.8 节） | `cancelled`（带 `cancel_reason`） |
| `asking` | 取消 | `cancelling`，进程树确认结束以后到 `cancelled` |
| `finishing` | 取消 | 不变：结果已经有了，照常完成 |

- **每次转换都是 CAS**（沿用 `agent_spawns.move(expect=…)`）：只从调用方以为的那个状态转。两个调用方抢同一行时，后到的什么都不改，拿到 None。
- **完成中（`finishing`）：** 子进程结束和结果交付之间的那一段。重启时遇到 `finishing` 的行，按它的子运行记录把记账、解析和交付做完，不重跑。
- **重启（D15）。** 恢复路径从不新起子进程：
  - **`asking`，计划内的重启（热升级），并且这个子运行是经保留运行启动的：** 由新进程接管，接着跑。只有可保留的运行时有这种情况（第 4.3 节）；
  - **`asking`，其他情况**（意外退出、没开 keeper、运行时不可保留）：先按记下的进程身份结束残留的进程树（pid 和启动时间都对上才结束，防止误杀复用了 pid 的进程），再记为 `cancelled`，`cancel_reason` 是 `server`。按失败分型这不算失败（Looper 自己停的）。派出者收到通知，自己决定要不要再派（新的键）；
  - **`cancelling`：** 同上，先结束残留进程，再记为 `cancelled`；
  - **`queued`：** 保留，恢复以后照常排队。它还没有进程，所以不算「重派」。
- **为什么不会重复派出：**
  - 恢复路径不新起子进程；
  - 派出者因为丢了返回而重试同一个调用，带的是同一个幂等键，只会拿到已有的那一行；
  - 唯一会让同一个派生再跑一次的，是第 4.9 节 `act` 下的瞬时重试。它只在服务正常运行时、对刚刚结束的一次失败做一次，算同一个派生的第 2 次尝试（`attempt` = 2），不是恢复路径做的。
- **结果和失败**写在行上：`result`（第 4.3 节的结构）、`failure_class` 和 `failure_cause`（第 4.9 节）、`detail`、`spent_usd`、`finished_at`、`delivered_at`。

### 4.8 取消和清理

| 什么时候 | 取消谁 | `cancel_reason` |
|---|---|---|
| 派出者调用 `looper_agent_cancel` | 那一个 | `requested` |
| Worker 或 Reviewer 的运行结束（不论怎么结束） | 它没结束的子 agent | `parent_ended` |
| 规划者的回合结束 | 不取消：规划者的子 agent 跨回合有效 | — |
| 步骤被重新规划掉、被跳过，或者它的 Worker 被杀 | 这个步骤的子 agent | `step_dropped` |
| 任务停止、取消、失败、完成，或者从头开始 | 这个任务全部没结束的子 agent | `task_ended` |
| 操作者在 Dashboard 或 API 上取消 | 那一个 | `operator` |
| 服务意外退出 | 见第 4.7 节 | `server` |

- **怎么取消：**
  1. `queued` 的行直接 CAS 到 `cancelled`，释放预留；
  2. `asking` 的行先 CAS 到 `cancelling`。CAS 没抢到，说明它已经结束或者正在被别人取消，就什么都不做；
  3. 结束子进程树（`proc_tree.end`，保留运行则让 keeper 结束）。结束不掉时照 Worker 的做法升级为 kill；
  4. 进程树确认结束以后，CAS 到 `cancelled`，从记录里记账、释放预留，记录本身关成 `interrupted`。

  第 3 步失败或者服务在这中间退出，行停在 `cancelling`，记着子进程的身份；下次启动时恢复路径会再结束一次（第 4.7 节）。孤儿进程就靠这个兜底。
- **进程内的子节点：** explorer 关掉了运行时自己的子 agent 工具，所以它没有进程内的子节点，不会有 Looper 看不到的孤儿。派出者（Worker）自己的原生子 agent 跑在它的进程里，随它的进程树一起结束，这和今天一样。
- **清理：** 子 agent 的私有临时目录（系统提示文件、运行时的 home）和检出，在任何结局下都删掉；残留的检出由启动时的恢复路径按目录清掉（和 consult 的 `.local/consults` 一样）。explorer 没有 MCP 文件。
- **级联：** 第一期只有一层，级联就是「父节点结束时取消它的子 agent」；表里各项都在父节点或任务的结束路径上触发（`WorkerPool` 的运行结束、`PlannerAgent` 的停止和重新规划、`TaskRouter` 的任务结束）。

### 4.9 每个节点的失败类型（接引擎可靠性 RFC 第 4 节）

- 子 agent 的运行和其他运行一样过 `ClaudeRunRecorder`，所以失败分型打开时，它的记录有 `failure_class` 和 `failure_cause`。
- 派生行另外存一份。启动以前就失败的也要有类型，这时没有记录：
  - Profile 跑不了或运行时做不到只读：`environment`/`profile`；
  - 预算或花费未知：`judgment`/`budget`。
- **交回派出者的失败**是类型化的：`{"state": "failed", "failure": {"class": …, "cause": …}, "detail": …}`。
- **失败分型处于 `act` 时**（引擎可靠性 RFC 的第四个 PR 之后）：
  - 额度不足：派生回到 `queued`，等账号重置（`held: quota`）。派出者已经结束的就取消；
  - 瞬时故障：在服务正常运行时自动再跑一次（`attempt` = 2，新的子运行，同一个派生行，同一份预留，扣掉第 1 次已花的）。恢复路径不做这件事（第 4.7 节）；
  - 环境问题、需要判断：直接以失败交回。
- **熔断：** 同一次运行连续 3 个子 agent 失败（不论类型），这次运行之后的派生都拒绝，原因码 `breaker`。这样一个误判不会一直烧钱。

### 4.10 可见性（D9）

- **接口：** `runs.tree {task_id}`（操作者只读）返回任务的运行树。
  - 节点：每个运行的 `id`、`kind`、`parent_record_id`、派出者（角色和座位）、创建原因（派生的 `brief.goal`，没有派生的节点用它的角色）、模型、状态、开始和结束时间、自身花费、子树花费、结果摘要（`summary` 的前 300 字）、失败类型；
  - 派生：它的状态、交付方式和 `cancel_reason`；
  - 等待区间：派出者在 `looper_agent_wait` 里阻塞的时段，从它的工具调用事件读出。
- **任务页新增「运行树」标签：**
  - 树：规划者的回合、Worker、Reviewer、advisor 请教、consult 和 explorer。节点上有上面那些字段；子树花费和自身花费分开写；
  - 时间线：每个节点一条横条，派出者等待的那段用虚线。这样能看出哪些在并行、谁在等谁；
  - 窄屏时时间线改为按时间排的列表。
- **花费：** `task_costs` 新增 `explorer` 一份；任务概览的花费行写成「规划者 · 执行者 · 评审者 · 子 agent」。
- **原有视图：** Agents 标签把 explorer 运行列在派出它的运行下面，和 advisor 一样。流水线不画 explorer，否则会太乱；在步骤卡片上只显示一个计数。

### 4.11 和原生子 agent 的关系

- **原生子 agent 保留，而且默认开。** 本文不替换、不禁用 Claude Code 的 `Agent` 工具和 agent teams，也不关其他运行时的同类工具。只有 explorer 自己的运行关掉了它们（第 4.3 节）。
- **以后要不要关：** 下一项（「原生子 agent 收口」）加一个项目设置 `native_subagents`，默认开；用本文的数据在评测集上对比开和关的质量、成本、延迟，由维护者决定要不要把默认改成关。
- **工具说明里的分工只是提示：** 打开 `subagents` 的项目里，Worker 的工具说明会写：只读调研用 `looper_agent_spawn`；要改文件的活请规划者加步骤；原生子 agent 不用来改文件。代码不强制这一句，原生子 agent 照样能改文件。这一句是模型能看到的内容，只在开关打开时注入，要跑评测（第 11 节）。
- **原生子 agent 不画成运行树的节点：** 它们的使用量从事件里统计，算作指标（第 8 节）。它们没有自己的记录，硬画出来只会误导。
- **`subagents` 关着时逐字节不变的范围：** 新的三个工具、它们的工具说明、规划者和 Worker 的提示词、命令行和 `task_costs` 的形状都和现在一样。原生子 agent 的行为不在这个开关的范围里，本来就不变。

## 5. 数据模型

全部是新增，可以在线执行。

```sql
-- 派生：explorer 种类、新状态和持久化字段
ALTER TABLE agent_spawns
    ADD COLUMN idempotency_key TEXT CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    ADD COLUMN spawner         TEXT,          -- 派出者的座位：planner、worker@s2
    ADD COLUMN profile_id      TEXT,
    ADD COLUMN delivery        TEXT CHECK (delivery IN ('tool', 'room', 'turn')),
    ADD COLUMN delivered_at    TIMESTAMPTZ,
    ADD COLUMN cancel_reason   TEXT CHECK (cancel_reason IN ('requested', 'parent_ended', 'step_dropped',
                                                             'task_ended', 'operator', 'server')),
    ADD COLUMN failure_class   TEXT,
    ADD COLUMN failure_cause   TEXT,
    ADD COLUMN attempt         INTEGER NOT NULL DEFAULT 1 CHECK (attempt BETWEEN 1 AND 2),
    ADD COLUMN child_process   JSONB CHECK (child_process IS NULL OR jsonb_typeof(child_process) = 'object'),
                                              -- 子进程的身份：pid 和启动时间，恢复时按它结束残留进程
    ADD COLUMN parent_spend_known BOOLEAN;    -- 预留时父运行的花费是否已知（第 4.5 节）
-- kind 加 explorer；state 加 queued、finishing、cancelling、cancelled（finished 约束随之加上 cancelled）；
-- failure 的一对值沿用 llm_records_failure_check 的同一个检查（failures.check_sql）。
-- 约束走 _ONLINE_STEPS：先 NOT VALID，再 VALIDATE。
CREATE UNIQUE INDEX idx_agent_spawns_key ON agent_spawns (parent_record_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;                       -- 在线建
ALTER TABLE agent_spawns ADD CONSTRAINT agent_spawns_cancel_reason
    CHECK ((state IN ('cancelling', 'cancelled')) = (cancel_reason IS NOT NULL)) NOT VALID;

-- 运行类型、角色、并发
-- llm_records_run_kind_check 加 'explorer'（RUN_KINDS）；agent_setting_layers_role_check 加 'explorer'；
-- task_concurrency_limits 的 role 加 'subagent'；role_configs 加 explorer 一行（seeds.sql），内置 Profile 照 advisor 的做法播种。

-- 项目设置
ALTER TABLE projects
    ADD COLUMN subagents                  TEXT CHECK (subagents IN ('off', 'on')),
    ADD COLUMN subagent_budget_usd        DOUBLE PRECISION CHECK (subagent_budget_usd BETWEEN 0.01 AND 5),
    ADD COLUMN subagent_task_budget_usd   DOUBLE PRECISION CHECK (subagent_task_budget_usd BETWEEN 0.01 AND 50),
    ADD COLUMN subagents_per_run          INTEGER CHECK (subagents_per_run BETWEEN 0 AND 20),
    ADD COLUMN subagents_per_task         INTEGER CHECK (subagents_per_task BETWEEN 0 AND 100);
```

- `agent_spawns` 是小表（每次请教一行），约束照常 NOT VALID 后校验；`llm_records` 的检查扩展照 `outbound_check` 加入 `run_kind` 时的做法。
- `schema.sql` 是冲突热点，每个 PR 的迁移都有名字，主干先合了别的迁移就合并主干、重新排序，不 rebase。
- 配置导出（`looper-config`）带上项目的这几个字段；导入旧文件时它们为空（关）。

## 6. 设置（默认关）

| 设置 | 默认 | 范围 | 管什么 |
|---|---|---|---|
| 项目 `subagents` | 空（关） | `off`、`on` | 这个项目的规划者和 Worker 有没有这几个工具、工具说明和结果交付 |
| 项目 `subagent_budget_usd` | 空 → explorer 角色的 `run_budget_usd`，再空 → 0.50 | 0.01–5 | 一个子 agent 的默认预算 |
| 项目 `subagent_task_budget_usd` | 空 → 3.00 | 0.01–50 | 一个任务全部子 agent 的上限 |
| 项目 `subagents_per_run` | 空 → 4 | 0–20 | 一次运行最多派几个 |
| 项目 `subagents_per_task` | 空 → 20 | 0–100 | 一个任务最多派几个 |
| 任务并发 `subagent` | 角色层 → 4 | 1–64 | 一个任务同时跑几个子 agent（`PUT /api/tasks/:id/agents` 和 `looper_task_concurrency` 都能改） |
| explorer 角色（Settings → Roles） | 内置 `claude-code/sonnet-5-5`，超时 300 秒，40 轮 | — | 它的 Profile、环境、提示、上限 |
| `wait_max_seconds` | 120 | 常量，第一期不做设置 | 一次等待的最长时间，和办公台的 `room_wait_max_seconds` 上限一致 |

- **关闭时：** 工具列表里没有这几个工具，提示词、契约、命令行、`task_costs` 的形状都和现在一样，每个 PR 有测试证明。
- **定时任务：** 跟随它项目的设置，不另设开关。

## 7. 失败语义（这一层自己出问题时）

| 情况 | 行为 |
|---|---|
| 派生的数据库写失败 | 不启动子进程，工具返回 `unavailable`，派出者可以重试（同一个键） |
| 子进程启动失败 | `failed`，带 `start_failure` 的类型（引擎可靠性 RFC 第 4.2 节） |
| 结果写回失败（`finishing` 卡住） | 每分钟重试，重启时也会完成；派出者等它时看到 `finishing` |
| 交付失败（办公台写不进、规划者已停） | `delivered_at` 留空；派出者下次 `looper_agent_wait`（不带 `spawn_ids` 也行）仍能拿到 |
| 取消时进程结束不掉 | 照 Worker 的做法升级为 kill，并记一条告警；行停在 `cancelling`，下次启动按进程身份再结束一次 |
| 父运行的花费读不到 | 父运行剩余这一项不参与，只用子树上限和任务上限卡（和第 4.5 节同一个口径）；派生行记 `parent_spend_known = false` |
| 检出建不了或删不掉 | 建不了就拒绝派生（`no_checkout`），不在原目录跑；删不掉的由启动时的恢复路径再删 |
| 任务锁等太久 | 等 5 秒仍拿不到就返回 `unavailable`，派出者可以重试（同一个键） |
| 借位逻辑出错 | 不借，子 agent 正常排队（可能更慢，但不会卡死，因为子 agent 有自己的任务并发类别） |

## 8. 指标和告警

- 每个任务：子 agent 的数量、按状态和失败类型、子树花费占任务花费的比例、平均等待时长、取消原因分布；
- 拒绝：按原因码计数（`limit_run`、`budget`、`profile`……），看上限是否合适；
- 去重：同一个幂等键第二次到来的次数，验收「热升级后重复派发为 0」靠它，数字应该只增加「返回已有行」，不增加子运行；
- 规划者：派了 explorer 的任务，规划者的上下文规模和花费，对比没派的同类任务（评测里看，第 11 节）；
- 原生子 agent：Worker 运行里 `Agent` 工具的调用次数和 `local_agent` 的任务数，作为收口那一项的基线（第 2.2 节：抽样里 32% 的 Worker 运行在用）；
- 告警：
  - 一个任务的子树花费超过 `subagent_task_budget_usd` 的 80%，记活动一条；
  - 一天里因熔断拒绝超过 5 次，自检 WARN；
  - `finishing` 卡住超过 10 分钟，自检 WARN。

## 9. 入口

| 入口 | 变化 |
|---|---|
| Dashboard | 项目设置里加子 agent 的开关和上限；任务页加「运行树」标签；Agents 标签列出 explorer；花费分出子 agent 一份；运行卡片显示派出原因和失败类型 |
| 飞书、Telegram | 没有新的对话入口。管家查看任务时（`ops/tasks/get`）花费行多一份子 agent；项目设置经管家现有的项目工具修改，要你确认，和改 advisor 开关一样 |
| CLI | `runs.tree` 和项目设置的新字段经生成的 `loopercli` 命令可用 |
| 规划者、Worker | 打开开关以后多 4 个工具和一段工具说明（模型可见，要评测） |

## 10. PR 拆分

| PR | 内容 | 开关 | 依赖 | 模型可见 |
|---|---|---|---|---|
| PR1 派生服务和数据 | 迁移（第 5 节）；`Access.EXPLORE` 和第 4.3 节表里每个运行时的只读启动、检出；explorer 角色、内置 Profile、契约和解析；`SpawnService`：校验、任务锁里的预留、幂等、状态机（含 `cancelling`）、CAS、取消和级联、按进程身份的恢复、记账、`task_costs` 的 explorer 份；工具还没接 | 项目 `subagents`（没有入口，走不到） | — | 否 |
| PR2 Worker 的工具 | `looper_agent_spawn`/`wait`/`cancel` 挂在 `run.` 令牌上；桥接的调用 id 和 `RETRY_SAFETY`；`room` 交付；Worker 的工具说明；Worker 结束时级联取消；真实 CLI 跑一次 explorer | 项目 `subagents` | PR1 | 是：工具和说明，要评测 |
| PR3 规划者 | 工具挂在 `planner.` 令牌上；[PLAN] 前派 explorer；回合内等待；`turn` 交付和 [ROOM] 唤醒；规划者的工具说明 | 同上 | PR2 | 是，要评测 |
| PR4 运行树 | `runs.tree`；任务页「运行树」标签（树和时间线）；花费分份；项目设置界面；截图（1280、768、390） | 同上 | PR1 | 否 |
| PR5 准入接入 | 子 agent 的账号准入和借位规则（第 4.6 节） | 引擎可靠性的 `engine.admission.enabled` | PR1，以及引擎可靠性 RFC 的准入 PR | 否 |
| PR6 什么时候派的指引和对比评测 | 派 explorer 的时机写进规划者和 Worker 的指引（只在 `subagents` 打开时注入）；评测集上对比派与不派的质量、花费、规划者上下文 | 同上 | PR2、PR3 | 是 |

- PR4 可以和 PR2、PR3 并行。新逻辑放在新模块（`looper/agents/subagents.py`、`looper/agents/ops/subagents.py`），`planner.py`、`worker.py` 和管家只接线。

## 11. 测试与验证（给实现 PR）

- **先写测试：**
  - 校验顺序里每一项的拒绝；
  - 同一个键返回同一行，并发两次也只派一个；
  - 不同键的两个派生同时到达，预留不超卖（两条连接并发跑，任务剩余只够一个）；
  - 父运行花费不知道时，父运行剩余不参与，派生行记 `parent_spend_known = false`；
  - 每个状态转换和 CAS 冲突；
  - 第 4.8 节取消表的每一行；
  - 重启时 `finishing` 被完成；`asking` 只在保留运行时被接管，其余先按进程身份结束残留进程再取消；`cancelling` 被收尾；`queued` 保留；恢复路径不起新进程，重复派发为 0；
  - 预算切分的每个分支；
  - 子树花费超过父运行上限时停父运行；
  - 嵌套被拒（explorer 的令牌和工具列表里都没有派生工具）。
- **关闭时逐字节不变：** 规划者、Worker 的提示词、工具列表、命令行和 `task_costs` 的形状，各 PR 一条测试。
- **只读：** 第 4.3 节表里每个运行时的 explore 启动参数有快照测试（包括：没有 MCP、没有网页、没有原生子 agent 工具，Pi 不带 `mcp__looper__*`）；`CHECK` 运行时被拒；`TOOLS` 运行时只在检出里跑，检出包含未提交的改动，派出者的 index 和分支不变；Claude Code、Codex 用真实 CLI 各跑一次，确认写操作被拒。
- **真实路径：** 一次性 `looper serve`（空闲端口、新库、一次性 runtime root）。用替身运行时跑一个任务：Worker 派两个 explorer，一个等、一个推送，取消一个，停任务，重启服务，然后看运行树、花费和去重计数。浏览器截图。
- **评测：** PR2、PR3、PR6 改了模型可见的内容，用真实 Claude CLI 跑评测对比（`evals/README.md`）。PR6 的对比评测要先把预算报给维护者。

## 12. 维护者的答复（2026-11-22）

本节保留当时的问题和推荐。维护者全部按推荐答复，记为第 3 节的 D10–D20（最后一列）。Q5、Q6 的推荐在独立评审后按代码修正了细节，修正写在最后一列，决定本身不变。

| 编号 | 问题 | 当时的推荐 | 决定 |
|---|---|---|---|
| Q1 | 子 agent 算进任务并发的哪一类 | **自己一类 `subagent`，默认 4**。算进 Worker 的上限会在 Worker 占满位子、都在等自己的子 agent 时卡死 | D10 |
| Q2 | Reviewer 能不能派 explorer | **第一期不能**。和已定的「规划者和 Worker」一致；评审要的是独立判断 | D11 |
| Q3 | advisor 要不要也走 `looper_agent_spawn` | **不走**，规划者照旧用 [DECISION] 的 `advise`。请求格式、三道检查和证据规则都挂在契约那条路上，不再开一个口子 | D12 |
| Q4 | explorer 能不能上网 | **不能**。等围栏和对外审查落地以后再考虑 | D13 |
| Q5 | explorer 读的是现场还是快照 | **现场**（派出者的工作目录），做不到只读的运行时用检出 | D14。评审后修正：用检出的是 `TOOLS` 方式的 OMP、Grok、Pi（原文误写成 OMP、Pi）；`CHECK` 方式的一律拒绝（第 4.3 节） |
| Q6 | 热升级时 explorer 怎么办 | **开了 keeper 就作为保留运行接管**，否则取消、由派出者重派 | D15。评审后修正：只有可保留的运行时（今天只有 Claude Code，非 steer、非托管）能被接管；其他的即使开了 keeper 也记 `cancelled`/`server`，恢复路径不起新进程（第 4.7 节） |
| Q7 | 规划者没等到的结果怎么送 | **办公台消息**（[ROOM] 回合唤醒），没有办公台时附在下一个回合；不加新的回合类型 | D16 |
| Q8 | Worker 结束时还没结束的子 agent | **取消**。没人等的结果只会白花钱 | D17 |
| Q9 | 默认上限 | **一个 0.50 USD，一个任务 3.00 USD，一次运行 4 个，一个任务 20 个，同时 4 个**；上线后按拒绝计数调 | D18 |
| Q10 | explorer 的默认模型 | **`claude-code/sonnet-5-5`** 作为内置值，换更便宜的模型由 PR6 的评测决定 | D19 |
| Q11 | 结果没过结构检查时 | **交回原文，标「未通过结构检查」** | D20 |

## 13. 风险

| 风险 | 怎么发现 | 怎么控制 |
|---|---|---|
| 子 agent 花费失控 | 子树花费占比、每任务的子 agent 数 | 三层上限（单个、子树、任务）；子树算进父运行上限；熔断 |
| 父等子卡死 | `queued` 时长、借位次数 | 子 agent 自己的并发类别；账号层的借位 |
| 只读约束漏了 | consult 已有的 `wrote_files` 检查；真实 CLI 的只读测试 | 做不到只读的运行时不允许派；OMP、Pi 跑在检出里 |
| 重复派发 | 幂等键第二次到来的次数、同一键的子运行数 | 唯一索引；从不自动重派 |
| 规划者被结果打断太频繁 | [ROOM] 回合的次数 | 结果合并进一条消息；规划者能按需取，不用每条都看 |
| 和原生子 agent 并存，反而更贵 | 第 8 节的原生子 agent 基线和评测 | 工具说明分工；原生收口那一项再做决定 |
