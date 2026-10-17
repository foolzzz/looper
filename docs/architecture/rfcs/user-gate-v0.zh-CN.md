# RFC：用户关卡（User Gate，v0）

- **状态：** 已批准（第 4 节 D1–D4 由维护者在 2026-10-17 定下，Q4 同日由维护者确认；D5–D13 和第 19 节的问题同日按建议定下，包括和维护者原写法不同的几处：D8、D10、D12、D13 和代码里的 `user_gate` 命名）；G1 已实现（协议、规划者、存储、接口和任务页，这一期默认关闭），G2 未开始。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.0 节第 16 项。
- **最近更新：** 2026-10-17

## 1. 背景与目标

今天规划者在 PLAN 回合一次定下全部步骤。需求含糊时它只能自己猜：选一种存储、一种输出格式、一个探索方向，猜错了后面的步骤白做。用户能做的只有事后加补充说明或改需求，由规划者重新规划，那时工作已经做了一部分。

维护者的要求（2026-10-17）：做了一部分工作之后，流水线（DAG）里可以有一个**用户关卡节点**：用户和规划者讨论（直接，或经管家），直到规划者判断信息够了；然后规划者按讨论定下的东西，规划后面的流水线和探索方向。

**目标：**

- 关卡是计划里的一种步骤，规划者在 PLAN 或之后任何一轮放进计划；依赖它的步骤等它，无关的分支照常运行；
- 讨论在规划者自己的会话里进行（完整上下文）；关卡打开期间规划者继续探索，按新发现更新或取消关卡；
- 结论类型化地存下来，之后的每个执行者、评审者都看得到；
- 用户在哪里都能答：任务页，飞书和 Telegram 的私聊、群聊（回复关卡消息），和管家的聊天；
- 默认对所有项目开启（定时任务除外），项目可以关闭。

**不做：**

- 执行者、评审者不能开关卡，只有规划者能；
- 第一期不做计划确认模式、超时后按假设继续、关卡统计页（维护者定的第二期，第 18 节），也不做飞书交互卡片和 Telegram inline 按钮（非核心）；
- 不按文字判断用户的意图：路由只按渠道的消息编号，结束讨论只走按钮、管家工具，或由规划者自己判断。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 |
|---|---|
| 计划 | `StepSpec` 有五个键：`id`、`instruction`、`acceptance_criteria`、`depends_on`、`review`。`_step` 拒绝未知键，`_check_new_steps` 查编号、依赖和环。已开始（`worker_id` 不为空）的步骤不能改 |
| 派发 | `_dispatch_ready` 派出依赖都已 `verified` 的 `pending` 步骤，隔离的项目先过 `_plan_round`。所有步骤都 `verified` 或 `skipped` 时任务完成，有 `failed` 时任务失败 |
| 回合 | PLAN、DECISION、CHAT、RESTART、SUPERVISE（另有 LESSONS、HANDOFF），一个接一个执行（消息循环加 `_turn_lock`），违约的回复有一次修复回合 |
| 任务变更 | 补充说明存为 `supplement` 日志条目，`delivered_at` 为空即未送达。每个事件之后，`_deliver_changes` 在一轮 CHAT 里送达，回复执行完才标为已送达 |
| 状态与分组 | 规划者经 `GOAL_STATUS` 报告状态，TaskRouter 把它存为任务状态。`STATUS_GROUPS` 是唯一的分组处，`ACTIVE_STATUSES`、`EDITABLE_STATUSES` 由分组推出；列表筛选、计数和 Dashboard 的计数都按状态分组。`task.py` 的重启判断里另有一个写死的状态列表 |
| 规划者进程 | Claude Code 规划者常驻一个进程。进程停掉后，下一轮以 `--resume` 续接同一会话（`_run_turn`）。Codex、OMP 本来就每轮起一个进程。一个任务从开始到结束占项目规划者并发的一个名额（`TaskRouter._running_in`，内置每项目 8 个） |
| 渠道收消息 | 飞书：lark-cli 1.0.97 的扁平事件带 `reply_to`（直接回复的那条消息）、`root_id` 和 `thread_id`（`lark-cli event schema im.message.receive_v1`），适配器现在都没用；群里只处理 @ 了机器人的消息（#142）。Telegram：`update.message.reply_to_message` 可用，适配器没用，`channel_context` 里也没有消息编号。两个渠道都把消息交给管家 |
| 渠道发消息 | 任务结束通知和规划者改并发的通知经 `CHAT_NOTICE` 发到任务的 `NotifyTarget`（飞书用户、飞书群并 @ 发起人、Telegram 聊天）。它走总线，拿不到发出消息的编号。`ChannelRegistry.notify` 直接调适配器、有回执：Telegram 的回执带 `external_id`；飞书的 `notify` 用 `run` 而不是 `run_json`，拿不到编号，而且只能发给用户。飞书的状态消息用 `run_json` 加 `sent_message_id` 拿到编号 |
| 聊天记录 | `chat_messages` 按 `session_id` 存。`list_sessions` 和 `recent_conversations` 把不以 `planner-` 开头的会话都当作管家的会话，后者还会被读进管家的上下文 |
| 管家 | 每条用户消息前面附「Projects」和「Ready tasks」。15 个工具的定义共 13.4 KB，管家 RFC 第 11 节的预算是 15 KB |
| 定时任务 | `tasks.schedule_id` 标出定时运行，定时任务删除后置空；运行上下文里约定「不要等用户回答」（定时任务 RFC 第 7 节）。gate 一词已被定时任务的前置检查占用：`gate_command`、`schedule_gate.py`，运行状态 `gate_failed`、`skipped_gate` |
| 项目执行设置 | `projects` 上的类型化列，`project_execution.py` 拥有类型和校验，约定「每个字段为空即今天的行为」，并随项目设置导出导入（`EXECUTION_COLUMNS`） |

## 3. 概念

| 概念 | 定义 |
|---|---|
| 关卡（gate） | 计划里的一种步骤（`kind: "gate"`）：一个话题和一组要问用户的问题。依赖它的步骤等它通过 |
| 议程（agenda） | 关卡要问的问题。每项有编号（`q1`、`q2`……，由代码编）、问题、可选的选项、状态（`open`、`resolved`、`dropped`）和答案 |
| 讨论回合 `[GATE]` | 用户在关卡里发了消息、或要求结束讨论时，规划者在自己的会话里做的一轮 |
| 结论 | 关卡通过时规划者写的 `decisions`、`assumptions`、`open`，存为一条补充说明（`via` 为 `gate`） |
| 关卡消息 | Looper 发到渠道的、和某个关卡有关的消息：打开时的卡片、更新、提醒、取消、结论，以及规划者在讨论里的回复。用户回复其中任何一条，都送到这个关卡 |
| 有人值守 | 不是定时任务建出的任务。只有有人值守的任务能开关卡 |

## 4. 决策

| # | 决策 | 理由 |
|---|---|---|
| **D1** | **规划者是讨论的对方，管家只转达。** 用户在任务页说的、在渠道里回复关卡消息的，直接到规划者；在和管家的主聊天里说的，管家用工具原样转给规划者，规划者的回复再发回这个聊天 | 维护者选定。规划者有任务的完整上下文，结论要落到它的计划上；管家转述会丢信息 |
| **D2** | **默认对所有项目开启，定时任务建出的任务除外**：代码拒绝它的规划者开关卡，并让它自己决定、写明假设。项目可以关闭（`user_gates` 为 `off`） | 维护者选定。规划者的回复约定对所有有人值守的任务都变了，这是模型可见的默认行为变化（第 5 节），按评测对比（第 16 节） |
| **D3** | **等的时候继续探索，默认不超时。** 关卡打开期间，规划者照常派出不依赖它的步骤；有了新发现（DECISION、CHAT 回合），它可以更新关卡（问题、发现），或取消它（探索已经回答了），每次都告诉用户。关卡一直等，按项目设置的间隔（默认每天）经同样的渠道提醒，任务页显示已经等了多久 | 维护者选定。用户不在线时任务不空转，探索还可能让问题不必再问 |
| **D4** | **第二期**：计划确认模式（PLAN 之后紧接一个关卡，按项目开关，默认关闭）、可选的超时后按假设继续、关卡统计页 | 维护者选定 |
| D5 | **关卡是步骤，不是新动作。** PLAN、DECISION 的 `replan`、CHAT 的 `add_steps` 和 `replan` 里都能放；依赖它的步骤可以先不规划，关卡通过时再加 | 维护者的设计。计划仍是一张依赖图，派发、并行和重启沿用同一套代码 |
| D6 | **讨论回合的回复是 CHAT 的形状加三个键**：`agenda`（议程的全部条目和各自的状态）、`gate`（`continue` 或 `close`）、`summary`（关闭时必填）。可以带 CHAT 的动作，关闭和规划后面的步骤在同一轮完成 | 维护者的设计。复用 CHAT 的校验和执行代码 |
| D7 | **更新和取消关卡用 DECISION、CHAT 回复里的类型化列表 `gates`**：`{"id", "action": "update" \| "cancel", …}` | 维护者要求类型化的表达。和计划的动作分开，一轮里可以既决定步骤、又改关卡 |
| D8 | **打开的关卡算已开始**：`replan` 不能悄悄去掉它，要去掉就在 `gates` 里取消，并告诉用户。还没打开的关卡和没开始的步骤一样，可以被重新规划掉（`skipped`） | 细化维护者的「重新规划掉（skipped）」，已接受：关卡打开后用户可能已经在答，去掉它要明说 |
| D9 | **路由只按消息编号。** Looper 记下它发到渠道的每条关卡消息的编号；用户的回复（飞书的 `reply_to`、`root_id`，Telegram 的 `reply_to_message`）对上编号就送到关卡，由代码在管家的模型之前处理；对不上的照常交给管家 | 不按文字猜。对上了也省掉管家的一轮 |
| D10 | **任务状态 `waiting_user`，分组 Needs you。** 只要有关卡在等用户，规划者就把任务状态报为 `waiting_user`，`STATUS_GROUPS` 把它归入新分组 `needs_you`；计划自己的状态仍是 `executing`，所以规划者里所有按计划状态判断的地方都不变 | 和维护者的写法不同，已接受（Q1）。维护者原来要「有关卡打开就进 Needs you，没有别的在跑时为 `waiting_user`」。分组今天只由状态决定，两条都满足需要给分组函数和按状态分组的查询加第二个输入（关卡数），或者加两个新状态。一个状态最简单；并行的步骤还在跑，看进度（「2 执行中」）和 Pipeline 就知道 |
| D11 | **结论是一条已送达的补充说明**：关卡通过时，结论在同一个事务里存为 `supplement`（`via` `gate`，`delivered_at` 立即填上），不再触发 CHAT 回合 | 维护者的设计。结论是规划者自己写的，不用再送给它一次 |
| D12 | **讨论记录用自己的类型化表，不用 `chat_messages`** | 和维护者的写法不同，已接受（Q2）。讨论记录要带 `chat_messages` 没有的字段：发言人（群聊）、点了哪一项的哪个选项、来自哪个渠道的哪条消息（规划者的回复要回复在它下面），还要一个送达游标；而且 `gate:` 开头的会话会被当作管家的会话列出、读进管家的上下文（第 2 节） |
| D13 | **分两期交付，第二期才把默认改成开启。** G1 是协议、规划者、存储、接口和任务页，这一期 `user_gates` 为空时仍是关闭；G2 加渠道、路由和管家，同时把空值改为开启 | 只有 G1 时，飞书、Telegram 上建的任务开了关卡，用户既收不到也答不了，任务会停住。最终状态符合 D2；已接受（Q3） |

名称（已接受）：模型看到的是 `gate` 和 `[GATE]`；代码、表和接口用 `user_gate`，因为定时任务的前置检查已经叫 gate。日志类型只存在于任务里，不会混淆，沿用维护者给的 `gate_*`。

## 5. 默认行为的变化

实现的 PR 在描述里逐条列出：

- 在开启关卡的项目里（G2 起是默认），有人值守的任务的规划者回复约定多一节关卡说明，CHAT、DECISION 多一个可选键 `gates`，G1 实测多 2,855 个字符（约 2.8 KB）。这是模型可见的变化。`user_gates` 为 `off` 的项目和定时任务的规划者，prompt 逐字节不变。
- 任务可能停下来等用户，默认没有超时。
- 新任务状态 `waiting_user`、新分组 `needs_you`：`GET /api/tasks?group=` 多一个取值，Dashboard 多一格，`ACTIVE_STATUSES` 和由它推出的 `EDITABLE_STATUSES` 随之包括它（删除保护、服务重启后恢复、侧栏计数、规划者令牌的任务状态检查）。
- `project_execution` 的约定「为空即今天的行为」对 `user_gates` 不再成立（G2 起为空即开启），模块说明随之改。
- 在飞书、Telegram 里回复关卡消息的消息，不再交给管家的模型（G2）。
- 管家的工具约定多一个工具和一段用法；有关卡在等待时，消息前面多一节「Open gates」。这也是模型可见的变化，跑管家评测（G2）。

## 6. 生命周期

```
         依赖都已通过               用户的第一条消息           规划者关闭，或用户结束讨论
pending ──────────────→ open ──────────────────→ discussing ──────────────────────→ passed
   │                     │                           │
   │ 被重新规划掉          └──────── gates 里 cancel ──┴──────→ cancelled（探索回答了，或不再需要）
   ↓
skipped                  （update：改话题、议程或发现，状态不变）
```

| 关卡状态 | 步骤状态（`task_steps.status`） | 依赖它的步骤 |
|---|---|---|
| `pending` | `pending` | 等 |
| `open`、`discussing` | `waiting_user`（新值） | 等 |
| `passed`、`cancelled` | `verified` | 可以开始 |
| `skipped` | `skipped` | 和它一起被重新规划 |

- 映射只在一处（`user_gates.py`）。`ready_steps`、`is_complete`、`last_open`、`view().done` 都不用改：通过和取消的关卡就是做完的步骤，打开的关卡既不待派发、也没做完。
- 没打开过就被取消的关卡（探索在它的依赖做完之前就回答了）也记 `cancelled`，不通知用户。
- 至少一个关卡为 `open` 或 `discussing` 时，规划者报 `waiting_user`；否则照旧报 `executing`、`verifying`。

## 7. 协议（模型可见）

### 7.1 关卡步骤

```json
{"id": "g1", "kind": "gate", "topic": "存储选型", "depends_on": ["s1"],
 "agenda": [{"question": "数据要和现有服务共用一个库吗？", "options": ["共用 PostgreSQL", "单独 SQLite"]},
            {"question": "历史数据要保留多久？"}],
 "findings": "现有服务用 PostgreSQL 15（db/schema.sql）"}
```

- 不带 `kind` 的步骤就是今天的工作步骤，形状不变；`kind` 只能是 `gate`。
- 关卡的键：`id`、`kind`、`topic`、`agenda`、`depends_on` 必填，`findings` 可选；`instruction`、`acceptance_criteria`、`review`、`role`、`review_role` 都不能出现。
- `topic` 非空、至多 200 字；`agenda` 至少一项，每项的 `question` 非空、至多 500 字，`options` 可选、至多 6 个、各至多 100 字（任务页显示为按钮，渠道里显示为文字，用户回文字或序号都行，由规划者理解）。
- 编号和工作步骤共用一个命名空间；工作步骤和关卡都可以依赖关卡。
- 项目的 `review_policy` 为 `all` 时，关卡也不评审（`_add_steps` 不给关卡设 `review`）。
- 不能开关卡的任务（定时任务建出的，或项目关闭了关卡）：回复里出现关卡就是违约，修复回合的消息写明原因和做法：「这个任务无人值守（由定时任务启动）／这个项目关闭了用户关卡，不能开关卡。自己决定，把假设写进依赖它的步骤的说明里，并在最终总结的 open_issues 里列出」。这些任务的约定里不提关卡。

### 7.2 讨论回合 `[GATE]`

**触发**：关卡里有规划者还没看过的用户消息，或用户要求结束讨论。送达规则和任务变更一样：每个事件的回合之后送达；规划者正在跑一轮时，等这一轮结束；同时攒下的多条合成一轮；回复执行完才推进送达游标，回合失败或服务重启后再给一次。

**消息**（大意）：

```
[GATE] Gate g1 "存储选型": discussing, opened 3 h ago, turn 2.
Agenda:
- q1 [open] 数据要和现有服务共用一个库吗？ (options: 共用 PostgreSQL / 单独 SQLite)
- q2 [resolved] 历史数据要保留多久？ → 90 天
Your findings so far: 现有服务用 PostgreSQL 15（db/schema.sql）
New messages (oldest first):
- (lark, 张三, 10:02, chose "共用 PostgreSQL" for q1) 就用现在那个库
<计划行>  Running now: s3 (worker-…)  <补充说明和 Progress and findings>
Reply with the GATE JSON.
```

用户要求结束讨论时，消息开头写明：「用户结束了讨论，要求按现在的结论继续：这一轮必须 `"gate": "close"`，每个还开着的问题都要在 `assumptions` 里写明你的假设。」新会话的第一轮（不论哪种回合，包括 `--resume` 续接失败后新开的会话）在 resume brief 里带上每个等待中关卡到目前的讨论记录（每个至多 8,000 字，放不下时略去最早的，并说明略去了几条；这一轮要送达的用户消息不在其中，在 `[GATE]` 消息里）。

**回复**：

```json
{"reply": "...", "gate": "continue",
 "agenda": [{"id": "q1", "question": "...", "status": "open"},
            {"id": "q2", "question": "...", "status": "resolved", "answer": "90 天"},
            {"question": "要不要迁移旧数据？", "options": ["迁移", "不迁移"]}],
 "action": "none"}
```

关闭，并在同一轮里规划后面的步骤：

```json
{"reply": "...", "gate": "close", "agenda": [...],
 "summary": {"decisions": ["共用现有的 PostgreSQL", "保留 90 天"], "assumptions": [], "open": []},
 "action": "add_steps", "steps": [...]}
```

**校验**：

- `reply`、`gate`、`agenda` 必填；`summary` 只在 `close` 时出现，而且必填；
- `agenda` 列出现有的每一项，漏了就是违约（「agenda must list q3; mark it dropped to remove it」）；新的项不带 `id`，代码接着编号；`resolved` 的项要有 `answer`；
- `close`：用户没有要求结束时，每一项都要 `resolved` 或 `dropped`；用户要求结束时可以留 `open` 的项，但 `assumptions` 的条数不能少于还开着的项数；
- `action` 和 CHAT 相同（`none`、`forward`、`add_steps`、`replan` 及其 `stop`），校验也相同；`add_steps`、`replan` 的新步骤可以依赖这个关卡；
- 关闭后任务没有别的没做完的步骤、这一轮也没加步骤时，任务就此完成，回复要带 `final`（同 DECISION 的规则）。

规划者可以在这一轮里读项目、跑无害的命令来回答问题；推荐的规划者提示词本来就允许。

### 7.3 更新和取消关卡：`gates`

DECISION 和 CHAT（含变更回合和 RESTART）的回复多一个可选键：

```json
"gates": [{"id": "g1", "action": "update", "findings": "s1 发现现有服务已经有 events 表",
           "note": "补充了 s1 的发现，q1 可能不用答了"},
          {"id": "g2", "action": "cancel", "resolution": "config/retention.yaml 已经定了 90 天"}]
```

- `update`：`topic`、`agenda`（同 7.2 的完整条目）、`findings` 至少给一个；关卡已经打开时要有 `note`，即发给用户的一句话；
- `cancel`：`resolution` 必填；
- `id` 必须是 `pending`、`open` 或 `discussing` 的关卡，每个至多出现一次；SUPERVISE 回复没有这个键，巡检不带来新发现；
- 代码先执行 `gates`，再执行决定或动作，并按执行 `gates` 之后的计划判断这一决定会不会完成任务（会，就要带 `final`）。所以同一轮里可以先取消关卡、再 `replan`。

### 7.4 约定和用法

只在能开关卡的任务里出现，接在回复约定里；模型可见，实现用英文，大意：

- 开头一句改为「每条消息以 [PLAN]、[DECISION]、[CHAT]、[RESTART] 或 [GATE] 开头」；`[CHAT]` 一节说明关卡里的消息不是补充说明，以 `[GATE]` 送来；
- 「只在三条都成立时开关卡：要决定的事会改变后面一大块工作（做什么、方案的取舍、只有用户知道的约束）；需求、补充说明、项目背景和看一眼代码都回答不了；猜错会让后面的工作白做。否则自己决定，把假设写进步骤说明。一个关卡里把要问的一次问完。关卡放在依赖答案的步骤前面；不依赖答案的部分照常派出去探索；后面做什么取决于答案时，先不规划那些步骤，关卡通过时再加。讨论中每个问题答清楚就标 resolved，都清楚了就关闭，不要多聊。」
- `[GATE]` 一节和 `gates` 键的形状（7.2、7.3）。

## 8. 规划者里的流程（代码）

- **打开**：`_dispatch_ready` 先挑出就绪的关卡，它们不进 `_plan_round`、没有工作区，也不查花费。一个事务里写：步骤 `waiting_user`，关卡 `open` 和 `opened_at`，日志 `gate_opened`，一条 `looper` 消息（卡片）。然后经渠道发出（第 9 节），报告任务状态 `waiting_user`。
- **唤醒**：新消息类型 `GATE_MESSAGE` 只唤醒规划者（同 `TASK_CHANGE`）。`_handle` 在每个事件之后先送达任务变更，再给每个有未读消息或结束请求的关卡做一轮 `[GATE]`。
- **执行讨论回合的回复**：一个事务里写议程、`turns + 1`、规划者的回复消息，并推进送达游标。关卡在等谁不存：有用户消息还没送达时等规划者，否则等用户。关闭时还写 `passed`、`summary`、`closed_at`、结论（补充说明，至多 3,000 字）、日志 `gate_closed`（怎么关的、几条决定和假设、计划改了什么），步骤改为 `verified`。然后执行 `action`（复用 `_apply_chat`），发出规划者的回复和结论消息，派发就绪的步骤。
- **关闭之后才到的消息**：这一轮读完消息之后、关卡关闭或取消之前到的消息，在关闭（或取消）的同一个事务里转存为补充说明（`via` `gate_late`），讨论记录里记一条 `late` 说明，送达游标推进到它们之后；用户的这条消息唤醒的事件之后，在 CHAT 里送达，不会丢。关闭时锁住关卡这一行，之后再到的消息因为关卡已不在等用户而被拒绝（409）。同一个事件里任务就完成了的话，它和任务结束时才到的其他补充说明一样留在日志里。关闭那一轮的 `add_steps` 和关闭写在同一个事务里；`replan` 要先停掉执行者、删工作区，这些不在事务里，所以仍在关闭之后执行。
- **更新、取消**：DECISION、CHAT 的 `gates` 由代码执行。更新写关卡和日志 `gate_updated`，已打开的关卡通知用户；取消写 `cancelled`、`resolution` 和日志 `gate_cancelled`，步骤改为 `verified`，打开过的通知用户。
- **结束讨论**：`POST /api/tasks/:id/gates/:step/end` 或管家工具的 `end: true` 记一条 `end` 类型的用户消息，唤醒规划者；只要这条消息还没送达，下一轮 `[GATE]` 就必须关闭（不另存标记）。这一轮修复后仍违约或失败时，关卡保持 `discussing`，用户收到错误，可以再结束一次。代码不替规划者关闭：关卡之后的步骤可能还没规划，代码关了它，任务会直接完成。
- **讨论回合失败**：告诉用户出错了，消息保持未送达，并记下失败（`failures`、`failed_through`、`failed_at`）。这时关卡等用户（`waiting_on` 为 `user`，`not_delivered` 是没送达的条数，提醒照常），不再算等规划者。规划者在 1 分钟和 5 分钟后各自动重试一次（`GATE_RETRY_WAIT_SEC`，由等待循环发起）；还失败就等用户的下一条消息，它连同没送达的一起再给一次。用户的新消息重新开始计数。「接着做」时清零，重启后再给一次。
- **服务重启**（`_resume`）：关卡的状态在库里，不重新通知；未读的消息在第一个事件之后送达。
- **停止**：关卡保持原状，提醒暂停。这时用户回复关卡消息，消息存下，回复「任务已停止，消息已保存，任务继续后交给规划者」。
- **接着做**（`continue`）：`_restart_plan` 不把 `waiting_user` 的关卡放回未开始。`[RESTART]` 的计划行写出每个关卡的状态，未读消息在那一轮之后的 `[GATE]` 里送达；仍打开的关卡发一条「任务继续了，仍在等你」。
- **从头再来**：关卡、讨论记录和发件记录随步骤一起删（`reset_task_for_restart`）。已经存下的结论是补充说明，和其他补充说明一样保留，新计划在 PLAN 里看得到（Q9）。
- **等待时的进程**：任务为 `waiting_user`、没有步骤在跑、15 分钟没有回合时，停掉常驻的 Claude Code 进程，下一轮以 `--resume` 续接（`_run_turn` 已有的路径）；续接不了而新开会话时，resume brief 带上等待中关卡的讨论记录（7.2）。PlannerAgent 本身和它占的规划者名额都保留（Q6）。
- **计划行**：决策、巡检、聊天和讨论消息的计划行写出关卡，例如 `- g1 [gate: discussing, waited 3 h] "存储选型" (depends on: s1; agenda: 1 of 2 resolved)`；通过的加一行结论摘要，取消的加 `resolution`。没有关卡的任务，计划行不变。

## 9. 渠道与路由

### 9.1 关卡消息发到哪里

| 任务来自 | 发到 |
|---|---|
| 飞书私聊 | 那个用户的私聊（`NotifyTarget` 为用户） |
| 飞书群 | 那个群，@ 发起人（`NotifyTarget` 为群并带 mention） |
| Telegram（私聊或群） | 那个聊天 |
| 网页（任务页、网页聊天） | 任务页；另外发到系统设置「Gates of dashboard tasks」指定的一个聊天：渠道加该渠道白名单里的一个聊天，同定时任务的通知目标。没有设置时只在任务页，Settings → Channels 上提示去设置（Q5） |
| 定时任务 | 没有关卡 |

- 关卡消息不走总线，直接调适配器（同 `Scheduler` 用 `registry.notify`，由 `serve.py` 传入发送函数），拿到发出的消息编号，写进发件记录。适配器新增 `post(target, text, reply_to=None) -> DeliveryReceipt`：飞书用 `+messages-send` 或 `+messages-reply` 加 `run_json` 和 `sent_message_id`（同状态消息）；Telegram 用 `send_message` 的 `reply_to_message_id`（python-telegram-bot 22.8 支持，内部转成 `reply_parameters`），回执本来就带 `external_id`。
- 发送失败记 `failed` 和原因，任务页的关卡面板上写明「没能发到飞书：…」，活动里记一条 error，下一次提醒再发。
- 文字由代码写（同管家 RFC 的 D7），规划者的 `reply` 原样发出。卡片的大意：

  ```
  ⏸ 需要你决定 · <任务名>
  <话题>
  1. <问题>（选项：A / B）
  2. <问题>
  目前的发现：<findings，截断>
  直接回复这条消息作答。
  在群里回复请 @机器人。          ← 只在发到飞书群的卡片里有
  <任务链接>
  ```

  飞书群里等用户回复的关卡消息（卡片、更新、提醒、规划者在讨论中的回复）都带「在群里回复请 @机器人」这一行；私聊和 Telegram 没有这一行。

  另有更新（「✏️ 规划者更新了问题」和 `note`）、提醒（「⏰ 还在等你 · 已等 26 小时」）、取消（「✅ 不用回答了：<resolution>」）、结论（「✅ 讨论结束」：决定、假设和接下来计划的改动）。

### 9.2 用户的回复怎么到关卡

| 渠道 | 怎么认出是在回复关卡 | 规划者的回复发到哪里 |
|---|---|---|
| 飞书私聊 | 事件的 `reply_to` 或 `root_id` 是一条关卡消息 | 回复在用户那条消息下（`+messages-reply`） |
| 飞书群 | 回复关卡消息时 @机器人（维护者 2026-10-17 确认，Q4），事件的 `reply_to` 或 `root_id` 是一条关卡消息。群里照旧只处理 @ 了机器人的消息（#142），没 @ 的回复不会到，不为关卡放宽。带 @ 的回复事件是否带 `reply_to`、`root_id`，留到实现时验证（第 16 节） | 同上 |
| Telegram 私聊 | `reply_to_message.message_id` 是一条关卡消息 | 回复在用户那条消息下（`reply_to_message_id`） |
| Telegram 群 | 同上。按 Telegram Bot API 的说明，隐私模式下机器人也会收到回复它自己消息的消息；留到实现时在真实的群里验证（第 16 节） | 同上 |
| 任务页 | 关卡面板的输入框和选项按钮 | 关卡面板 |
| 和管家的主聊天（没有回复关卡消息） | 管家在消息前面看到「Open gates」，用 `looper_task_gate_reply` 转达；可能是几个关卡中的哪一个时，先问用户（第 10 节） | 飞书、Telegram：回复在用户那条消息下；网页聊天：任务页的关卡面板，管家的回复说明去哪里看 |

Telegram 的 `reply_to_message` 只给直接回复的那一条，飞书的 `root_id` 给整串回复的第一条。所以 Looper 发出的每条关卡消息（卡片、规划者的回复、提醒）都记编号，用户的消息也记编号，回复其中任何一条都对得上。

**路由**（G2，`Manager._handle_user_message` 在调用模型之前，由代码做）：

1. 适配器把回复的引用放进 `channel_context`：飞书的 `reply_to`、`root_id`，Telegram 的 `reply_to_message_id`；Telegram 的 `channel_context` 同时补上这条消息自己的 `message_id`；
2. 按（渠道、聊天、编号）在发件记录和关卡的用户消息里查；
3. 对上了，关卡为 `open` 或 `discussing`，任务在运行：存为关卡的用户消息（群聊记发言人的名字），唤醒规划者，不调用管家的模型。适配器已经加的表情（飞书「Get」、Telegram 私聊 👀）表示收到了，规划者的回复随后回复在这条消息下；
4. 对上了，任务停止或失败，关卡没有关闭：存下，回复「任务已停止……」（第 8 节）；
5. 对上了，但关卡已经通过、取消或跳过，或者任务已经完成：照常交给管家，前面附一句代码写的说明「这条消息回复的是任务 X 的关卡 g1，它已经通过」，管家可以把它作为补充说明转给规划者；
6. 没对上：照常交给管家。

发送者白名单照旧先在适配器里检查；群里任何在白名单里的人都可以回答，讨论记录写出是谁。

## 10. 管家（G2）

- **上下文**：每条用户消息前面，在「Ready tasks」之后加「Open gates」（有等待中的关卡才加，至多 10 个，最近打开的在前）：任务编号、项目、关卡编号和话题、已经等了多久、在等谁、还开着的问题（每个截断到 80 字）。
- **工具 `looper_task_gate_reply`**（write，只在回答用户的那一轮里）：`{"task_id", "gate": "<关卡编号>", "text": "<用户原话>", "end"?: true}`。存为关卡的用户消息（`via` `manager:<渠道>`），同时记下用户那条消息在渠道里的编号，规划者的回复回复在它下面；`end` 为真时同时要求结束讨论。关卡不在等用户时返回 `conflict`，说明它现在的状态。
- `looper_tasks` 的 `get` 带出关卡（随 `GET /api/tasks/:id`），`looper_overview` 带出等待中的关卡数。
- **约定**（加进 `TOOLS_CONTRACT`，大意）：「用户的话是在回答或谈论一个等待中的关卡时，用 looper_task_gate_reply 把原话转给它的规划者（用户想按现在的结论继续时加 end: true）；可能是几个关卡中的哪一个时，先问用户。不要替规划者回答关卡里的问题。」
- **大小**：新工具的定义约 0.6 KB，管家的工具定义从 13.4 KB 到约 14 KB，仍在 15 KB 的预算内；T3 剩下的工具要连同它一起算，实测记入管家 RFC 第 11 节。

## 11. 界面与入口

| 位置 | 变化 |
|---|---|
| Dashboard | 分组「Needs you」排在第一，有任务时用 warning 色 |
| Tasks | 筛选多一个 Needs you，状态徽标「Waiting for you」 |
| 任务页 Overview | 有关卡在等时，最上面是关卡面板：为什么停下（话题和规划者的发现）、已经等了多久、议程清单（开着的、已答的及答案、去掉的）、讨论记录（发言人、来自哪里、时间）、开着的问题的选项按钮、输入框、「End discussion, go on with current conclusions」按钮、关卡消息发到了哪里以及发送失败。通过后换成结论卡片（决定、假设、仍开着的）和计划的改动（关闭那一轮加了、去掉了、停掉了哪些步骤）；更早的关卡按时间先后收起 |
| 任务页 Pipeline | 关卡节点的形状和步骤不同（带暂停图标的六边形），颜色和文字一起表示状态：等你（warning，「Waiting for you」）、讨论中（primary，「Discussing」）、通过（success，「Passed」）、取消和跳过（虚线，「Cancelled」「Skipped」）；点击打开关卡面板 |
| Chat 页的规划者线程 | 有关卡在等时顶部一条提示，链接到关卡面板；在这里发的仍是补充说明（Q12）（G2） |
| 项目设置 → Execution | 「User gates」（On / Off）和「Gate reminders」（每 N 小时，0 为不提醒） |
| Settings → Channels | 「Gates of dashboard tasks」：渠道和白名单里的一个聊天，空为只在任务页（G2） |
| HTTP 接口 | `GET /api/tasks/:id` 多 `gates`（每个关卡的状态、议程、发现、等待时长、结论、计划改动、发件记录）；`GET /api/tasks/:id/gates/:step/messages`；`POST /api/tasks/:id/gates/:step/messages`（`{text, agenda_item?, option?}`，关卡不在等用户时 409；回答的 `delivered` 为 `queued`，任务没在运行时为 `kept`，即任务继续后才交给规划者）；`POST /api/tasks/:id/gates/:step/end`；`GET /api/tasks?group=needs_you`；项目的两个设置走 `PUT /api/projects/:id`，渠道的那一个走系统设置的接口 |
| 实时 | 关卡的每次变化写一条活动，任务页随现有的活动流刷新 |
| 飞书、Telegram | 第 9 节（G2） |
| CLI | 不变：CLI 没有任务类命令 |

界面按 `docs/design.md` 的界面一节做：只用令牌和 `components/ui` 的组件，状态都有文字，在 1440 和 390 宽度下用无头浏览器截图核对。

## 12. 数据模型

```
task_user_gates                一个关卡
  step_id            → task_steps ON DELETE CASCADE，主键
  task_id            → tasks ON DELETE CASCADE
  topic              非空
  agenda             JSONB：[{id, question, options[], status: open | resolved | dropped, answer}]
  findings           规划者给用户看的发现；可空
  state              'pending' | 'open' | 'discussing' | 'passed' | 'cancelled' | 'skipped'
  ended_by_user      通过时：是不是用户结束的
  summary            JSONB {decisions[], assumptions[], open[]}：只在 passed 时有（约束）
  resolution         cancelled 时的原因
  plan_change        JSONB：关闭那一轮加了、去掉、停掉的步骤
  delivered_through  规划者已经看过的最后一条用户消息（task_user_gate_messages.id）
  turns, reminders
  opened_at, first_reply_at, closed_at, last_reminded_at, created_at, updated_at

task_user_gate_messages        讨论记录，按 id 先后
  id                 BIGSERIAL
  step_id            → task_user_gates ON DELETE CASCADE
  role               'user' | 'planner' | 'looper'
  kind               user：'message' | 'end'；planner：'reply'；
                     looper：'opened' | 'updated' | 'reminder' | 'cancelled' | 'closed' | 'resumed' | 'error'
  text
  author             群聊里发言人的名字；私聊、任务页为 NULL
  via                'dashboard' | 'lark' | 'telegram' | 'manager:<渠道>'
  agenda_item, option  任务页的选项按钮：哪一项、哪个选项
  channel, chat, external_id  来自渠道的用户消息在哪个聊天、编号是什么（规划者的回复回复在它下面）
  created_at

task_user_gate_posts           Looper 发到渠道的关卡消息：路由表，也是发件记录
  id
  message_id         → task_user_gate_messages ON DELETE CASCADE
  channel            'lark' | 'telegram'
  chat, external_id  失败时 external_id 为 NULL
  status             'sent' | 'failed'
  error, created_at
  UNIQUE (channel, chat, external_id)
```

- `task_steps` 加 `kind`（`'work'`，默认；`'gate'`）。关卡步骤的 `instruction` 存话题，工作步骤专用的列留空。
- `PlanStep` 加 `kind`；关卡的 `started` 为「不是 pending」。`PlanView` 加 `gates`（`{编号: 状态}`），供 `gates` 和 `replan` 的校验用。
- 任务日志加 `gate_opened`、`gate_updated`、`gate_cancelled`、`gate_closed`，只给任务页看（同 `profile_choice`）；规划者从计划行看到关卡。
- 项目执行设置：`user_gates`（`on` | `off`；G1 为空即 `off`，G2 起为空即 `on`）和 `gate_reminder_hours`（0–168，空为 24，0 为不提醒），随项目设置导出导入。
- 系统设置 `user_gates.dashboard_target`：`{channel, chat}`，或为空（G2）。
- `STATUS_GROUPS` 加 `"waiting_user": TaskGroup.NEEDS_YOU`；`ACTIVE_STATUSES` 改为包括 `NEEDS_YOU` 分组；`task.py` 重启判断里写死的状态列表改为由分组推出，或至少加上 `waiting_user`。

## 13. 提醒

`looper serve` 里一个后台循环（每 5 分钟一次）给满足以下条件的关卡发提醒：关卡为 `open` 或 `discussing`，在等用户（没有未送达的用户消息），任务在运行（停止的任务不提醒），项目的 `gate_reminder_hours` 不为 0，离「打开、用户上一条消息、规划者上一条回复、上一次提醒」中最晚的那个超过了这个间隔。提醒发到和卡片同样的目标，记一条 `reminder` 消息和发件记录，`reminders` 加一。提醒也是关卡消息，回复它同样送到关卡。G1 只有任务页，提醒只记一条活动，渠道里的提醒随 G2。

## 14. 与其他设计的关系

| 设计 | 关系 |
|---|---|
| 规划者定义的角色（#161） | 没有语义上的冲突：关卡不跑执行者和评审者，`role`、`review_role` 在关卡上一律拒绝，`_check_roles` 跳过关卡；`reply_contract(max_retries, *, roles, gates)` 两节各自可选、互不影响；按角色的统计不计关卡。文本上重叠：`planner_protocol.py` 的 `StepSpec`、`_step`、`_check_new_steps` 和约定，`planner.py` 的 `PlanStep`、`_step_row`、`_add_steps`，`db/plans.py`，`schema.sql` 里日志类型的约束（各自一个迁移），`task-detail.tsx` 的 Pipeline。所以在 #161 合入之后再开工 |
| 并行步骤的目录隔离 | 关卡不进 `_plan_round`，没有工作区；依赖关卡的步骤在关卡通过后照常开 worktree |
| Agent 办公台（Agent Room，另一份 RFC） | 关卡是用户和规划者之间的讨论，办公台是 agent 之间的。执行者需要用户决定时在办公台里升级给规划者，由规划者决定是否开关卡；执行者自己不能开关卡，也不在运行中等关卡（它的步骤要么按假设做完，要么被规划者停掉、放到关卡之后）。两边的名字不重叠（`[GATE]` 与 `[ROOM]`，`task_user_gate*` 与 `room_*`） |
| 管家平台工具（任务变更、CHAT 的 `replan`） | CHAT（含变更回合和 RESTART）多 `gates`；结论是补充说明，沿用 `supplement` 的存储和它在规划者、执行者、评审者、任务页的出现位置；`looper_task_instruct` 不变 |
| 定时任务 | 定时任务建出的任务不能开关卡（D2），prompt 不变；运行上下文里「不要等用户回答」的约定和代码的拒绝一致。定时任务删除后 `schedule_id` 置空，剩下那次运行此后算有人值守，可以开关卡；这种情况少，按现有字段判断，不另加列 |
| 花费上限 | 讨论回合计入任务的花费；讨论中达到上限的任务照旧停下 |
| 巡检 | 只看运行中的步骤，不变 |
| 换会话、交接 | 关卡在库里，计划行写出它们的状态；新会话第一轮的 resume brief 带等待中关卡的讨论记录 |
| 经验沉淀（LESSONS） | 关卡的日志条目进 LESSONS 回合，规划者可以总结出「这类需求先问 X」 |

## 15. 观测

**每个关卡记下**（类型化的列）：打开、第一次回复、关闭的时间，讨论回合数，提醒次数，结局（通过：规划者关闭或用户结束；取消；跳过），关闭那一轮的计划改动（加了、去掉、停掉几步），消息数，发送失败数。

**指标**（G1 由接口和评测给出，跨任务的统计页在第二期）：

| 指标 | 口径 |
|---|---|
| 每个项目的关卡数 | 打开过的关卡，按项目、按天 |
| 首次回复用时 | `first_reply_at - opened_at`，取中位数和 P90 |
| 等待时长 | 打开到关闭 |
| 讨论回合数 | 每个关卡的 `turns` |
| 结局分布 | passed（规划者关闭 / 用户结束）、cancelled、skipped |
| 关卡之后的计划改动 | 关闭那一轮的步骤增减 |
| 有无关卡的成功率 | 有关卡通过的任务和没有关卡的任务，各自完成数占结束数的比例 |
| 误路由 | 回复已关闭关卡的消息数；管家转达前问「是哪一个」的次数 |
| 发送失败 | 按渠道统计失败的关卡消息 |

**活动**：「Gate g1 opened: 存储选型 (sent to lark)」「Gate g1: a message from lark」「Gate g1 passed: 2 decisions, 1 assumption; plan: +3 steps」「Gate g1 cancelled: …」「Gate g1 reminder sent (waited 24 h)」；发送失败记为 error。

**告警**：第一期不加；提醒就是对用户的告警。运维看发送失败和等待时长的分布（第二期的统计页）。

## 16. 评测与测试

**规划者评测，真实模式**（haiku，对照已提交的基线，`evals/README.md`）。评测运行器扮演用户：有关卡打开时，按任务的脚本经 `POST /api/tasks/:id/gates/:step/messages` 回答，或调用 `/end`。

- 现有 16 个任务都是有人值守的，prompt 变了，这就是默认行为的变化：对比成功率、步骤数、花费和违约。新指标 `gates_opened` 预期都是 0，因为这些需求都清楚，开关卡就是多问。真有关卡打开时，运行器回答一次「没有偏好，你来定」，并记下来。
- 新增五个任务：
  - `gate_ambiguous`：需求留一个决定结果的选择给用户（例如配置文件用 JSON 还是 TOML），脚本回答 TOML 和一个键名。断言开了关卡、至少一轮讨论、关卡通过、结论记成了补充说明、执行者的任务一节里有结论、最终文件是 TOML 且键名对；
  - `gate_not_needed`：一个清楚的任务。断言一个关卡都没开；
  - `gate_end_early`：同 `gate_ambiguous`，但脚本第一轮就结束讨论。断言关闭那一轮写了假设，任务完成；
  - `gate_parallel_branch`：需求有一个清楚的部分 A 和一个含糊的部分 B。断言关卡只挡住 B，A 的执行者在关卡打开期间跑过（运行记录的时间和关卡的打开时段重叠）；
  - `gate_discoverable`：问题的答案要由某一步的工作得出（需求要求执行者先跑 `probe.sh` 测出端口），用户答不了。断言一个关卡都没打开（`max_gates_opened: 0`，约定写明能查到的不要问；依赖那一步、在打开前就取消的关卡没问过用户，不算），任务完成、文件对。脚本在关卡打开 120 秒后结束讨论，开错了的关卡很快判为失败，而不是等到超时。（原名 `gate_explore_cancel`，原来只作观察项，无论开没开关卡都通过。）
  - 脚本的话说完了规划者还在问时，运行器结束讨论，关卡按假设关闭，`gate_asks_beyond_script` 记下这样的关卡数。
- 新指标：`gates_opened`、`gate_turns`、`gates_ended_by_user`、`gates_cancelled`、`gate_parallel_runs`、`gate_plan_changes`、`assumptions_stated`。
- 替身模式覆盖五个新任务的流程。

**管家评测**（G2，在管家的对话脚本上新增）：用户在主聊天里回答一个等待中的关卡，断言 `looper_task_gate_reply` 转的是原话；两个关卡都可能时先问；回复一个已经通过的关卡时不调用这个工具，作为补充说明转达。

**单元和数据库测试**（真实 PG）：

- 协议：关卡步骤的形状和拒绝（多余的键、空议程、关卡上的 `role`、不能开关卡时的消息）；`[GATE]` 回复（漏项、`resolved` 没有答案、关闭时还有开着的项、强制关闭时的假设、`summary` 只在关闭时、`final`）；`gates` 的更新和取消（状态、重复、SUPERVISE 不带）；打开的关卡不能被 `replan` 去掉；项目关闭和定时任务的规划者 prompt 逐字节不变；
- 规划者：打开；送达（忙时排到下一轮、多条合成一轮、失败和重启后仍未送达、关闭之后才到的消息变成补充说明）；关闭（同一事务、补充说明、步骤 `verified`、派发）；取消和更新；强制关闭失败时保持 `discussing`；停止、接着做、从头再来、服务重启；等待时停掉进程后续接；
- 状态：`waiting_user` 的进出、分组、`ACTIVE_STATUSES`；
- 路由：按编号对上（飞书 `reply_to`、`root_id`，Telegram `reply_to_message_id`）、对不上、关卡已关、任务停止；群里没 @ 的消息照旧被丢弃；
- 提醒：间隔、等谁、停止的任务不提醒、0 为不提醒。

**真实路径**：一次性的 `looper serve`（新库、非 8767 端口、一次性 runtime root），用真实 haiku 跑 `gate_ambiguous`，经接口核对。G2 用替身的飞书、Telegram 适配器走一遍发卡片、回复、规划者在线程里回复；两条渠道事实在真实的群里各验证一次：飞书群里带 @ 的回复事件带 `reply_to`、`root_id`（回复时要 @机器人这条规则本身已由维护者确认），Telegram 群在隐私模式下收得到回复机器人消息的消息。这要发真实的消息，先征得维护者同意，用测试群并标明是测试；没验证就在 PR 里写明缺口。替身适配器的测试另外覆盖带 @ 的回复对上关卡、不带 @ 的被丢弃。界面改动在 1440 和 390 宽度下用无头浏览器截图。

## 17. 风险

| 风险 | 处理 |
|---|---|
| 多问：清楚的任务也开关卡，打扰用户 | 约定写明三条都成立才开（7.4）；现有 16 个评测任务的 `gates_opened` 应为 0；任务页和指标显示关卡数；一个关卡里一次问完；项目可以关闭 |
| 讨论不收尾 | 约定要求答清就关闭；消息写出回合数；用户随时可以结束讨论；每轮都计入花费，受任务的花费上限约束 |
| 回复送错地方 | 只按渠道的消息编号对上；对不上交给管家，有歧义时管家先问；关卡已关时作为补充说明；群里只认白名单里的人 |
| 无人值守的任务停住 | 定时任务由代码拒绝开关卡（D2），它的约定里不提关卡 |
| 用户一直不回 | 默认每天提醒，任务页显示等了多久，任务进 Needs you；任务占着项目的一个规划者名额（Q6），空闲的进程会停掉；第二期可选超时后按假设继续 |
| 结论挤占补充说明的预算 | 每条结论至多 3,000 字；补充说明的总预算是 8,000 字，超过时最早的被略去并说明数量。几个关卡的结论都很长时可能挤掉用户早先的补充说明，在评测里观察 |
| 上下文变大 | 约定只在开启的、有人值守的任务里多一节，G1 实测约 2.8 KB；管家多约 0.6 KB |
| 飞书群里回复忘了 @机器人 | 维护者确认的规则（Q4）。群里的每条关卡消息都写明「在群里回复请 @机器人」；没 @ 的回复到不了，用户可能以为已经答了，提醒会再来 |
| 关卡消息发不出去（渠道没开、登录失效） | 发件记录写失败原因，任务页显示，活动记 error，下一次提醒再发 |
| 用户的回复里有提示注入 | 和补充说明同一信任级：发言人都在白名单里；回复不改契约、工具和权限 |
| 本机接口没有令牌（威胁模型） | 关卡的路由和其他 `/api/tasks` 路由一样只有 Origin/Host 校验。G1 里规划者、执行者、评审者的令牌和 MCP 工具都不写关卡消息，但执行者的 shell 仍然可以 POST 关卡消息，规划者据此关闭后，结论写的是「Settled with the user」，之后每个执行者和评审者都会读到。接口暴露本身不是新问题，这个后果是新的。G2 之前要定：给关卡消息的写入加用户凭据，或者在结论和讨论记录里标明消息的来路 |

## 18. 分期

| 期 | 内容 | 依赖 |
|---|---|---|
| G1 | 协议（关卡步骤、`[GATE]`、`gates`）、约定和用法（只在能开关卡的任务里）、对定时任务和关闭的项目的拒绝；数据模型；规划者的打开、送达、关闭、更新、取消、强制关闭、停止和重启、服务重启、等待时停掉进程；`waiting_user` 和 Needs you；结论补充说明；四类日志；接口；项目的两个设置（这一期 `user_gates` 为空即关闭）；提醒循环（这一期只记活动）；任务页的关卡面板、Pipeline 节点、Dashboard 和 Tasks 的分组；规划者评测（现有 16 个的对比、5 个新任务、替身模式）。✅ 已实现 | #161 合入 |
| G2 | 渠道：适配器的 `post` 和回复引用，发卡片、更新、提醒、取消、结论，规划者的回复在线程里；路由；网页任务的通知目标；管家的「Open gates」、`looper_task_gate_reply` 和约定，以及管家评测；飞书群、Telegram 群各一条渠道事实的真实验证（征得维护者同意，用测试群）；`user_gates` 为空改为开启（默认行为的变化） | G1 |
| 第二期（维护者的 D4） | 计划确认模式（按项目开关，默认关闭）；超时后按假设继续（项目设置，默认不超时）；关卡统计页（第 15 节的指标，按项目） | G2 |
| 非核心 | 飞书交互卡片上的按钮（要消费 `card.action.trigger` 事件）、Telegram inline 按钮；侧栏的等待数；Dashboard 上的「Waiting for you」列表 | G2 |

## 19. 已决问题（2026-10-17 答复）

| # | 问题 | 结论 |
|---|---|---|
| Q1 | 任务状态：只要有关卡在等就报 `waiting_user`（D10），还是只在没别的在跑时报、在跑时另算分组 | **已定：一个状态。** 分组只由状态决定的约定不变，按状态分组的查询不用加第二个输入；还在跑什么，看进度和 Pipeline。没有采用的做法：两个新状态（`waiting_user`，和「执行中且有关卡在等」），都归 Needs you |
| Q2 | 讨论记录放 `chat_messages`，还是单独的表 | **已定：单独的表**（D12）。放 `chat_messages` 就要给它加列（发言人、选项、来源消息）和送达游标，并让两处列管家会话的查询排除 `gate:` 前缀 |
| Q3 | 默认开启从哪一期起 | **已定：G2**（D13）。只有 G1 时，飞书、Telegram 上的任务开了关卡没人收得到 |
| Q4 | 飞书群里不 @ 机器人的回复要不要收 | **已定（维护者 2026-10-17）：不收**，群里回复关卡要 @机器人，群里的关卡消息写明「在群里回复请 @机器人」；#142「群里只处理 @」的规则不变。私聊和 Telegram 照第 9.2 节 |
| Q5 | 网页建的任务，关卡发到哪里 | **已定：系统设置 `user_gates.dashboard_target`**（渠道加该渠道白名单里的一个聊天），代码里**默认为空**，即只在任务页，Settings → Channels 上提示去设置；部署时由运维者选定。代码里没有「运维者是谁」的类型化信息，按白名单里只有一个人去猜是推断 |
| Q6 | 等待中的任务占不占项目的规划者名额 | **已定：占。** 项目的规划者上限常用来保护共享的目录（上限为 1 的项目一次只跑一个任务），而等待中的任务目录里有做了一半的工作，释放名额会让另一个任务进来跑。空闲的进程停掉，省内存 |
| Q7 | 要不要限制一个任务的关卡数 | **已定：第一期不限**，靠约定和评测，任务页可见；评测里出现多问时再加项目设置 |
| Q8 | 打开的关卡能不能被 `replan` 直接去掉 | **已定：不能**（D8）：在 `gates` 里取消，并告诉用户 |
| Q9 | 从头再来时，已经存下的结论（补充说明）删不删 | **已定：保留。** 补充说明从不删除，用户做的决定对新计划仍然有效；想彻底重来就改需求 |
| Q10 | 任务停止时用户回复关卡 | **已定：存下，任务继续后交给规划者**，并告诉用户 |
| Q11 | 用户结束讨论后，那一轮失败了 | **已定：关卡保持讨论中，告诉用户，可以再结束一次**；代码不替规划者关闭 |
| Q12 | 关卡打开时，用户在 Chat 页的规划者线程里发的话要不要送进关卡 | **已定：不送**，仍是补充说明，规划者可以在 CHAT 回合里用 `gates` 更新或取消关卡；线程顶部提示去关卡面板。「有关卡就送进关卡」会让用户分不清哪句话进了哪里 |
