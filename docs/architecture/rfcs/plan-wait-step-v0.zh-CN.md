# RFC：等待步骤（Wait Step，v0）

- **状态：** 已批准（第 4 节 M1–M3 由维护者在 2026-10-19 定下；D1–D12 是实现前按建议定下的做法，理由写在各条里）；实现中。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5 节。
- **最近更新：** 2026-10-19

## 1. 背景与目标

今天计划里的每一步都要一个执行者去做。任务要等外部的事（拉取请求的作者推了修改、CI 跑完、别的系统的任务结束）时，规划者只能：让执行者在一步里轮询（占着执行者和并发名额，花模型的钱），或者让任务先结束、由用户之后重启。

典型的例子：一个任务评审拉取请求、发出意见；作者之后修改。任务应该在拉取请求有了新的 head（或过了一段时间）之后回来再评审一次，等待期间不占执行者、不跑模型回合：

```
评审 PR → 发出意见 → 等待（PR 的 head 变了，每 10 分钟查一次，最多 3 天）→ 再评审
```

**目标：**

- 等待是计划里的一种步骤（`kind: "wait"`），规划者自己决定加不加；依赖它的步骤等它，其余步骤照常运行；
- 等待按时间、加可选的检查结束：Looper 自己定时检查，不跑模型；
- 等待期间不占执行者、不占并发名额，规划者常驻的进程按关卡的规则停掉；
- 等待存在数据库里，服务重启后接着等；
- 超时才告诉用户，然后由规划者决定怎么做；
- 任务页能看到等待的状态，用户可以「立即继续」或「取消」。

**不做：**

- 执行者、评审者不能加等待，只有规划者能；
- 不做别的检查类型（只有 `github_pr` 和 `command`）；不做按文字判断「好了没有」；
- 管家不加新工具（第 10 节）；渠道里不能操作等待（只收超时通知）；
- 不做跨任务的等待统计页（非核心）。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 |
|---|---|
| 步骤种类 | `task_steps.kind` 为 `work` 或 `gate`。关卡（`planner_protocol.GateSpec`、`user_gates`、`db.user_gates`）是不由执行者做的步骤：依赖做完后打开，依赖它的步骤等它，其余照常运行；打开的关卡算已开始，`replan` 不能悄悄去掉它 |
| 派发 | `_dispatch_ready` 先打开就绪的关卡，再派出就绪的工作步骤；`ready_steps`、`is_complete`、`has_failed` 只看步骤状态 |
| 唤醒规划者 | 外部写库之后发一条消息给 `planner_address(task_id)`（关卡是 `GATE_MESSAGE`），规划者在每个事件末尾（`_handle` 的最后）读库、处理 |
| 空闲 | 只有关卡在等、没有步骤在跑、`GATE_IDLE_STOP_SEC` 内没有回合时，规划者常驻的 Claude 进程停掉，下一轮续接会话（`_idle_waiting`、`_idle_loop`） |
| 任务状态 | 规划者经 `GOAL_STATUS` 报告；有关卡在等时报 `waiting_user`。`STATUS_GROUPS` 是唯一的分组处，`ACTIVE_STATUSES` 由它推出，服务启动时恢复其中的任务 |
| 后台循环 | `serve.py` 启动 `GateReminders`、`Scheduler` 等循环，每个都是 `start`/`stop` 加 `run_once` |
| 系统设置 | `scheduler_settings.py`（#237）：`system_settings` 里的键，同步校验要用的值放在内存，启动时 `load`、修改时 `update` 刷新；Settings → Scheduler 编辑 |
| 项目设置 | `projects` 上的类型化列，`project_execution.py` 负责类型和校验，`EXECUTION_COLUMNS` 随项目导出导入 |
| 通知渠道 | `ops/planner.py` 的并发变更通知：任务来自飞书或 Telegram（`notify_channel`、`NotifyTarget.from_columns`）时，发一条 `CHAT_NOTICE`；否则只记活动 |
| 子进程 | `child_env.inherited()` 是子进程的环境；`proc_tree` 结束一个进程和它的子进程 |

## 3. 概念

| 概念 | 定义 |
|---|---|
| 等待（wait） | 计划里的一种步骤（`kind: "wait"`）：一个话题、最长等多久、可选的最早时间和检查。依赖它的步骤等它结束 |
| 检查（check） | Looper 定时运行、判断「可以继续了」的东西：`github_pr`（Looper 用固定的参数调 `gh`）或 `command`（项目允许时，一条 shell 命令） |
| 基线（baseline） | `github_pr` 检查在等待打开后第一次运行时记下的 head SHA 和评论数，之后和它比 |
| 截止时间（deadline） | 打开时间加 `max_wait_seconds`。过了还没就绪就超时 |
| `[WAIT]` 回合 | 等待超时、或用户取消了它时，规划者做的一轮：延长、继续、让任务失败，或改计划 |

## 4. 决策

| # | 决策 | 理由 |
|---|---|---|
| **M1** | **按时间加可选的检查结束。** 最早时间已过、且（有检查时）检查报告就绪，等待就结束；没有检查的等待到时间就结束 | 维护者定的 |
| **M2** | **规划者自己决定加等待**，在系统限制之内（最长等待、最短检查间隔）。等待在任务活动和 Dashboard 上可见 | 维护者定的 |
| **M3** | **只在超时时通知用户**（最长等待过了、条件一直没满足），经任务的渠道路由，和别的任务通知一样；然后规划者得到一轮来决定。正常结束不通知 | 维护者定的 |
| D1 | **等待是步骤，形状照关卡。** PLAN、DECISION 的 `replan`、CHAT 的 `add_steps` 和 `replan` 里都能放；打开的等待算已开始，`replan` 不能去掉它，要结束就用 `waits` 取消 | 派发、并行和重启沿用同一套代码；关卡已经证明了这个形状 |
| D2 | **所有任务都能加等待**，包括定时任务建出的任务；不加项目开关。只有 `command` 检查要项目开 `wait_commands` | 等待不需要人在场；`github_pr` 只读、参数固定，不扩大规划者的权限 |
| D3 | **`command` 检查默认关闭**（项目设置 `wait_commands`，为空即 `off`）。关闭时协议拒绝它，修复回合说明原因和做法；约定里也不提它 | 规划者是只读角色，这条命令以服务的用户身份在工作目录里运行，等于给了它执行任意命令的能力，只能由项目明确打开 |
| D4 | **`max_wait_seconds` 必填。** 1 到系统最长等待 | 维护者要求「总要设最长等待」；必填比默认值更不容易让任务无声地等七天 |
| D5 | **检查和超时由服务里的一个循环（`WaitService`）负责，计划的改动只由规划者做。** 循环把等待的行从 `waiting` 改为 `ready` 或 `timed_out`（有条件的 `UPDATE … WHERE state = 'waiting'`），再唤醒规划者（`WAIT_EVENT`）；规划者在事件末尾读到它，把步骤标为完成、派出依赖它的步骤，或者开一轮 `[WAIT]` | 规划者在内存里持有计划，外部直接改步骤会和它不一致；关卡的消息也是这样先写库、再唤醒 |
| D6 | **就绪时不唤醒模型。** 步骤直接完成，结果是一段简短的说明（为什么继续、检查的输出，限 2 KiB），依赖它的步骤像读关卡结论一样读它 | 维护者的设计：等待期间、结束时都不花模型回合 |
| D7 | **用户的「立即继续」和「取消」只在 `waiting` 状态可用。** 立即继续等于就绪（不唤醒模型）；取消把状态改为 `cancel_requested`，规划者得到一轮 `[WAIT]` | 超时和取消之后由规划者决定，这时不再接受用户的按钮，避免和它的决定竞争；两个按钮也用有条件的 `UPDATE`，和循环先到先得 |
| D8 | **任务状态 `waiting`，分组 In progress。** 有关卡在等时仍报 `waiting_user`；否则有等待没结束、且没有步骤在跑（`_IN_FLIGHT`）时报 `waiting`；其余照旧 | 任务在等外部的事，不需要用户，不进 Needs you；还有步骤在跑时报 `waiting` 会误导 |
| D9 | **系统限制是两个系统设置**，照 #237 的做法：`waits.max_wait_seconds`（默认 7 天，60 秒到 30 天）、`waits.min_check_seconds`（默认 300 秒，10 秒到 1 天）。放在 Settings → Scheduler 页的「Waits」一节 | 最短检查间隔防止规划者把 `gh` 或命令跑得太勤；下限放低是为了测试和真实路径验证 |
| D10 | **`[WAIT]` 回合的回复形状写在它的消息里，不进约定。** 约定只多 PLAN 里的一段（何时等待、形状、`waits` 取消） | 维护者要求不用等待的运行，prompt 只多这一段；LESSONS、VAULT、HANDOFF 也是消息自带形状 |
| D11 | **停掉的任务不检查。** 循环只看任务状态在 `ACTIVE_STATUSES` 里的等待；任务重启后接着等，截止时间不变（停了很久的，重启后很快超时，通知用户） | 不给停掉的任务发超时通知；截止时间是对外的承诺，不因停止而顺延 |
| D12 | **检查不记日志。** 输出只存进等待的行（限 2 KiB），日志只写「就绪 / 未就绪 / 出错（退出码）」 | 命令的输出可能带凭据或内部信息 |

名称：模型看到的是 `wait`、`waits` 和 `[WAIT]`；代码是 `waits.py`（类型和限制）、`wait_service.py`（循环、接口）、`db/waits.py`（行），表 `task_waits`。

## 5. 默认行为的变化

实现的 PR 在描述里逐条列出：

- 所有任务的规划者约定在 PLAN 里多一段等待说明（第 7.4 节），DECISION、CHAT 多一个可选键 `waits`。这是模型可见的变化，按第 14 节的评测对比。
- 新步骤种类 `wait`，新步骤状态 `waiting`，新任务状态 `waiting`（分组 In progress，`ACTIVE_STATUSES` 随之包括它：删除保护、服务重启后恢复、侧栏计数）。
- 新项目设置 `wait_commands`，默认关闭。
- 新系统设置 `waits.max_wait_seconds`、`waits.min_check_seconds`。
- 服务多一个后台循环；`github_pr` 检查用服务所在用户的 `gh` 登录状态调 GitHub 接口（只读）。
- 等待超时时，任务来自飞书或 Telegram 的，那个聊天收到一条通知。

## 6. 生命周期

```
          依赖都已完成              最早时间已过且检查就绪 / 用户「立即继续」
pending ─────────────→ waiting ──────────────────────────────→ ready ──(规划者读到)──→ done
   │                     │ │
   │被重新规划掉           │ └─ 截止时间已过 ─→ timed_out ───┐
   ↓                     └── 用户「取消」 ──→ cancel_requested ─┤ [WAIT] 回合
skipped                                                     ├─ extend → waiting（新截止时间）
                 waits 里 cancel（规划者，任何未结束的状态）→ done   ├─ proceed → done
                                                            └─ fail → failed
```

| 等待状态 | 谁写 | 步骤状态（`task_steps.status`） | 依赖它的步骤 |
|---|---|---|---|
| `pending` | 规划者 | `pending` | 等 |
| `waiting` | 规划者（打开、延长） | `waiting`（新值） | 等 |
| `ready` | 循环、用户的「立即继续」 | `waiting` | 等（规划者读到之后完成） |
| `timed_out`、`cancel_requested` | 循环、用户的「取消」 | `waiting` | 等 |
| `done` | 规划者 | `verified` | 可以开始 |
| `failed` | 规划者 | `failed` | 任务失败 |
| `skipped` | 规划者 | `skipped` | 和它一起被重新规划 |

- 映射只在一处（`waits.py` 的 `STEP_STATUS`）。`done` 记一个结局 `outcome`：`ready`（时间到或检查就绪）、`resumed`（用户立即继续）、`proceeded`（规划者在 `[WAIT]` 回合里继续）、`cancelled`（规划者在 `waits` 里取消）。
- `ready`、`timed_out`、`cancel_requested` 是「等规划者读」的状态，规划者每个事件末尾读一次；任务没在跑时，留到重启。
- 规划者读到 `ready` 时步骤可能已经完成（它刚在 `waits` 里取消了），那就只把行标为 `done`。
- 规划者从这些状态的转换都带条件（期望的旧状态）；条件不成立说明别人先改了，下一个事件重读。

## 7. 协议（模型可见）

### 7.1 等待步骤

```json
{"id": "w1", "kind": "wait", "topic": "作者推了修改", "depends_on": ["s1"], "max_wait_seconds": 259200,
 "check": {"type": "github_pr", "repo": "owner/name", "number": 12, "ready_when": "head_changed"},
 "every_seconds": 600}
```

- 键：`id`、`kind`、`topic`、`depends_on`、`max_wait_seconds` 必填；`not_before_seconds` 或 `not_before`（二选一）、`check`、`every_seconds` 可选。`instruction`、`acceptance_criteria`、`review`、`role`、`review_roles` 都不能出现。
- `topic`：非空，至多 200 字，等的是什么。
- `max_wait_seconds`：整数，1 到系统最长等待。
- `not_before_seconds`：整数，0 到 `max_wait_seconds`，从打开时算；`not_before`：带时区的 ISO 8601 时间，不晚于现在加系统最长等待。都不给时最早时间就是打开时间。
- `check`（可选），按 `type` 分：
  - `{"type": "github_pr", "repo": "owner/name", "number": N, "ready_when": "head_changed" | "checks_finished" | "new_comments"}`：`repo` 匹配 `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`，`number` 是正整数；没有别的键。
  - `{"type": "command", "command": "..."}`：非空，至多 2,000 字；只有项目的 `wait_commands` 为 `on` 时允许，否则拒绝：「this project does not let a wait run a command (wait_commands is off): use a github_pr check, or a time only (not_before_seconds)」。
- 没有检查的等待必须给 `not_before_seconds`（大于 0）或 `not_before`，否则它一打开就结束，没有意义。
- `every_seconds`：只在有检查时可给，整数，系统最短检查间隔到系统最长等待；不给时取 `max(最短间隔, min(600, max_wait_seconds))`。
- 编号和工作步骤、关卡共用一个命名空间；任何步骤都可以依赖等待。
- 项目的 `review_policy` 为 `all` 时，等待也不评审。
- 校验失败时给出具体原因，进修复回合，和关卡一样。

### 7.2 取消：`waits`

DECISION 和 CHAT（含 RESTART）的回复可以带：

```json
{"waits": [{"id": "w1", "action": "cancel", "resolution": "作者已经在评论里说不改了"}]}
```

- `id` 是一个没结束的等待（`pending`、`waiting`、`ready`、`timed_out`、`cancel_requested`）；每个等待至多一次；`action` 只有 `cancel`；`resolution` 必填，至多 1,000 字。
- 取消的等待算完成（`done`，结局 `cancelled`），依赖它的步骤可以开始，读到的结果是 `resolution`。和关卡一样：一个回复的取消让所有步骤都结束时，它要带 `final`。

### 7.3 `[WAIT]` 回合

**触发**：等待变为 `timed_out` 或 `cancel_requested`，规划者在事件末尾读到。失败的回合按关卡的重试间隔（60 秒、300 秒）再来，之后在每个事件末尾再来。

**消息**（形状由消息自带，D10）：

```
[WAIT] Wait w1 "作者推了修改" timed out: it waited 3 d (max_wait_seconds 259200) and its check never reported ready.
Check: github_pr owner/name#12, ready_when head_changed, every 600 s; 431 checks, the last at 09:10 UTC: not ready.
Last output (capped): head 1a2b3c4 is the head the wait started with
The user was told in their Lark chat.
（或：The user cancelled this wait from the dashboard.）

<计划，正在运行的步骤，进度>
Steps waiting for it: s3.

Reply in the CHAT shape plus "wait":
{"reply": "...", "action": "none", "wait": {"action": "extend", "max_wait_seconds": 86400}}
- wait "extend": wait max_wait_seconds more from now (1 to 604800); the check goes on. Not after the user cancelled.
- wait "proceed": stop waiting; the steps that depend on it start now. "resolution" (required) says why; they read it.
- wait "fail": the task cannot go on; "reason" (required).
- To ask the user first, proceed and add a gate (action "replan") that the waiting steps depend on.
- action: as in [CHAT]. "final" as in [DECISION], when the message says proceeding without new steps completes the task.
Reply with the WAIT JSON.
```

- 回复：`reply`（必填，记进活动）、`wait`（必填）、CHAT 的 `action` 及其键，和可选的 `final`。
- `extend`：`max_wait_seconds` 是从现在起再等多久，1 到系统最长等待；新截止时间是现在加它，下一次检查在现在。
- `proceed`：步骤完成（结局 `proceeded`），结果是 `resolution`。
- `fail`：步骤失败（结果是 `reason`），任务按今天的规则失败。
- 等待是任务最后一个没结束的步骤、`proceed` 且不加步骤时，回复必须带 `final`。
- 用户取消后 `extend` 被拒绝：「the user cancelled this wait: proceed, fail, or change the plan」。

### 7.4 约定里的新段（每个规划者都有）

PLAN 的说明里，关卡说明之后多一段（`{command}` 只在 `wait_commands` 为 `on` 时出现，数字取当时的系统设置）：

```
- Waits: when something outside the task must happen before some steps can
  run (a pull request's author pushes fixes, CI finishes, another system's
  job ends), a step may be a wait instead of a Worker that polls:
  {"id": "w1", "kind": "wait", "topic": "...", "depends_on": ["s1"], "max_wait_seconds": 259200, "check": {"type": "github_pr", "repo": "owner/name", "number": 12, "ready_when": "head_changed"}, "every_seconds": 600}
  It opens when the steps it depends on are done. Looper runs the check every
  every_seconds (at least 300, default 600) with no turn of yours, and the
  steps that depend on it start once it is ready; they read why. check:
  github_pr ("ready_when": "head_changed", "checks_finished" or
  "new_comments"){command}. Without a check, give "not_before_seconds" (or
  "not_before", an ISO time) and it ends then. Always set max_wait_seconds
  (at most 604800): when it passes, the user is told and you get a [WAIT]
  turn. Prefer a typed check. Never wait for what a step of this task does:
  depend on that step. In [DECISION] and [CHAT], "waits": [{"id": "w1",
  "action": "cancel", "resolution": "..."}] ends a wait now; it counts as done.
```

`{command}` 为：`, or {"type": "command", "command": "..."}: a shell command run in the working directory, exit 0 is ready`。

## 8. 规划者里的流程（代码）

- `PlanStep.wait: Wait | None`，像 `gate`。`started`：等待的状态不是 `pending`。`_add_steps` 给等待建 `Wait`，`review` 恒为假。
- `_dispatch_ready`：就绪的等待先打开（`_open_wait`）：状态 `waiting`，记下 `opened_at`、`due_at`（最早时间）、`deadline_at`、`next_check_at`（等于 `opened_at`，`github_pr` 的第一次检查只记基线），日志条目 `wait_opened`，活动「Wait w1 opened: … (github_pr owner/name#12 head_changed every 10 min; at most 3 d)」，再报一次进度。
- `_deliver_waits`：在 `_handle` 末尾、`_deliver_gate_messages` 之后。读任务里状态为 `ready`、`timed_out`、`cancel_requested` 的行：`ready` 的完成步骤（结果 `{"status": "ready", "summary": <说明>}`，日志 `wait_closed`，活动），然后 `_settle_plan`；另两种开一轮 `[WAIT]`（`parse_wait`），按回复延长、继续、失败，并执行回复的 CHAT 动作（照 `_close_gate` 的写法：加步骤和结束等待同一个事务）。
- `waits` 取消：DECISION、CHAT、RESTART 的回复在动作之前执行（像 `gates`）。
- `_report_progress`：按 D8 选状态；进度一行多「N 等待中」。
- `_idle_waiting`：有关卡在等，或有等待没结束，且没有步骤在跑。
- `_idle_tick`：失败的 `[WAIT]` 回合到了重试时间时，发 `wait_retry` 的 tick。
- 重启（`_restart_plan`）：没结束的等待保持原状态。`_load_plan` 读回等待。
- `_plan_lines` 里等待步骤写成 `w1 [wait, waiting: github_pr owner/name#12 head_changed, until <deadline>]`。

## 9. 循环（`WaitService`）

- `serve.py` 里和 `GateReminders` 一起启动、停止。每 `WAIT_TICK_SEC`（15 秒）一次 `run_once`；测试直接调 `run_once(now)`。
- 每次取状态为 `waiting`、任务状态在 `ACTIVE_STATUSES` 里、且 `next_check_at <= now` 或 `deadline_at <= now` 的行，至多同时跑 4 个检查（信号量）。
- 对每一行，按顺序：
  1. 有检查、到了 `next_check_at`：运行检查。`github_pr` 的基线为空时只记基线（不判断就绪）。否则 `now >= due_at` 且就绪 → `ready`。不论结果，记下 `checks`、`last_checked_at`、`last_result`（`ready`、`not_ready`、`error`）、`last_output`、`last_error`，`next_check_at = now + every_seconds`。
  2. 没有检查、`now >= due_at` → `ready`（说明：「time reached」）。
  3. 还在 `waiting`、`now >= deadline_at` → `timed_out`，发超时通知（第 11 节）。
- 状态改变用有条件的 `UPDATE … WHERE state = 'waiting'`，改成了才唤醒规划者：`WAIT_EVENT`（新消息类型）发给 `planner_address(task_id)`，只在 `has_planner(task_id)` 时发；否则留到重启。
- **`github_pr`**：`["gh", "pr", "view", str(number), "--repo", repo, "--json", "headRefOid,state,comments,reviews,statusCheckRollup"]`，没有 shell，参数只来自校验过的字段。解析 JSON：
  - `head_changed`：`headRefOid` 和基线不同；
  - `checks_finished`：`statusCheckRollup` 至少一项，且每项都结束（CheckRun 的 `status` 为 `COMPLETED`；StatusContext 的 `state` 不是 `PENDING`、`EXPECTED`）；
  - `new_comments`：`comments` 加 `reviews` 的条数多于基线；
  - `state` 为 `MERGED` 或 `CLOSED` 时也就绪，说明写明。
  - 退出码不为 0、超时、JSON 不对：`error`，下一次照常检查。
- **`command`**：`asyncio.create_subprocess_shell`，工作目录是任务的目录，环境是 `child_env.inherited()`，自成进程组；退出码 0 就绪，其他未就绪；60 秒超时时用 `proc_tree` 结束整棵进程树，记 `error`。运行时项目的 `wait_commands` 已关闭：不运行，记 `error`（「wait_commands is off」）。
- 输出：stdout 和 stderr 合在一起，取最后 2,048 字节（UTF-8 解码时替换坏字节）。只存进行，不进日志（D12）。
- 就绪的说明（`resolution`，也是步骤的结果）：「Ready: <为什么>」+ 截断过的输出。例如「Ready: the head of owner/name#12 changed (1a2b3c4 → 5d6e7f8)」。

## 10. 渠道、管家与入口

| 入口 | 变化 |
|---|---|
| Dashboard | 任务页：等待步骤的图标和状态、等待卡片（话题、检查、最早时间、截止时间、下一次检查、检查次数和最后结果、输出、结局、超时通知是否发出），`waiting` 状态下「Resume now」「Cancel」两个按钮；任务列表认识 `waiting` 状态；Settings → Scheduler 多「Waits」一节；项目设置多 `wait_commands` |
| Telegram、飞书 | 只收超时通知，经现有的 `CHAT_NOTICE`；其他不变 |
| 管家 | 不加工具：任务视图里的状态 `waiting` 和步骤列表已经说明在等；用户要立即继续或取消，到任务页 |
| CLI | 不变：等待只由规划者加，由 Dashboard 操作 |

## 11. 超时通知

- `timed_out` 写成后，循环记一条活动（「Wait w1 timed out after 3 d: <topic>; the Planner decides next」）。
- 任务来自飞书或 Telegram（`notify_channel`、`NotifyTarget.from_columns`）：发一条 `CHAT_NOTICE`，照 `ops/planner._notify`：
  `⏳ 等待超时 · <任务名>` / `<话题>` / `等了 3 天，条件一直没有满足；规划者接下来决定怎么做。` / 任务链接。
- 行上记 `notice`：`sent`（发到了渠道）、`dashboard`（任务没有渠道，只在任务页）或 `failed`（发送出错），任务页显示。
- 正常结束不通知（M3）。

## 12. 接口

- `GET /api/tasks/:id` 多 `waits`：计划顺序，每项 `wait_view`：`step`、`step_id`、`state`、`outcome`、`topic`、`check`、`every_seconds`、`opened_at`、`due_at`、`deadline_at`、`next_check_at`、`last_checked_at`、`checks`、`last_result`、`last_output`、`last_error`、`resolution`、`notice`、`closed_at`、`depends_on`。
- `POST /api/tasks/:id/waits/:step/resume`、`.../cancel`：404 没有这个任务或等待，409 等待不在 `waiting`（「wait w1 is timed_out: its Planner decides what happens next」）或任务已结束；成功返回 `wait_view`，并唤醒规划者。
- `GET`、`PUT /api/wait-settings`：`{"max_wait_seconds", "min_check_seconds"}`，各带来源（`setting`、`built-in`）；`PUT` 校验范围和「最短间隔不大于最长等待」，400 时说明。

## 13. 数据模型

```sql
-- task_steps.kind 加 'wait'（一次性迁移：重建 CHECK）；task_journal.kind 加 'wait_opened'、'wait_extended'、'wait_closed'。
ALTER TABLE projects ADD COLUMN IF NOT EXISTS wait_commands TEXT CHECK (wait_commands IN ('off', 'on'));
CREATE TABLE IF NOT EXISTS task_waits (
    step_id          TEXT PRIMARY KEY REFERENCES task_steps(id) ON DELETE CASCADE,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    topic            TEXT NOT NULL CHECK (topic <> ''),
    spec             JSONB NOT NULL,   -- max_wait_seconds, not_before_seconds, not_before, check, every_seconds
    state            TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'waiting', 'ready', 'timed_out',
                     'cancel_requested', 'done', 'failed', 'skipped')),
    outcome          TEXT CHECK (outcome IN ('ready', 'resumed', 'proceeded', 'cancelled')),
    opened_at        TIMESTAMPTZ,
    due_at           TIMESTAMPTZ,
    deadline_at      TIMESTAMPTZ,
    next_check_at    TIMESTAMPTZ,
    baseline         JSONB,            -- github_pr: {"head", "comments"}
    checks           INTEGER NOT NULL DEFAULT 0,
    last_checked_at  TIMESTAMPTZ,
    last_result      TEXT CHECK (last_result IN ('ready', 'not_ready', 'error')),
    last_output      TEXT NOT NULL DEFAULT '',   -- at most 2,048 bytes
    last_error       TEXT NOT NULL DEFAULT '',
    resolution       TEXT NOT NULL DEFAULT '',
    notice           TEXT CHECK (notice IN ('sent', 'dashboard', 'failed')),
    extensions       INTEGER NOT NULL DEFAULT 0,
    failures         INTEGER NOT NULL DEFAULT 0, -- [WAIT] turns in a row that failed
    failed_at        TIMESTAMPTZ,
    closed_at        TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT task_waits_outcome_done CHECK ((outcome IS NOT NULL) = (state = 'done'))
);
CREATE INDEX IF NOT EXISTS idx_task_waits_task ON task_waits (task_id);
CREATE INDEX IF NOT EXISTS idx_task_waits_due ON task_waits (next_check_at) WHERE state = 'waiting';
```

- 规划者经 `db_plans.save` 在保存步骤的同一个事务里写行（像关卡）；从 `ready`、`timed_out`、`cancel_requested`、`waiting` 出发的转换带期望的旧状态。
- 循环和接口只写 `waiting` 的行（`ready`、`timed_out`、`cancel_requested` 和检查的列）。

## 14. 评测与测试

**规划者评测**（`evals/README.md`，按 2026-10-18 的评测政策只跑涉及的任务）：

- 新任务 `wait_pr_rereview`：评审一个拉取请求、写出意见，作者之后会推修改，推了之后再评审。断言计划里有一个 `github_pr` 检查（`head_changed` 或 `new_comments`）的等待，且再评审的步骤依赖它（`min_waits_opened: 1`、`min_wait_dependents: 1`）。运行器放一个 `gh` 替身在服务的 `PATH` 最前（不调真的 GitHub），等待打开后经 `.../resume` 立即继续，任务跑完。
- 反方向：`wait_not_needed`：一步要等另一步的结果（先构建、再测试）。断言没有等待（`max_waits_opened: 0`）。
- 替身模式覆盖两个新任务；真实模式 haiku 和 opus（这是规划者的约定变化），`--reps 2`。

**单元和数据库测试**（先写，每条在实现前失败）：

- 协议：每个键的校验（必填、范围、二选一、类型化检查的每个字段、未知键）；`wait_commands` 关闭时拒绝 `command`；`waits` 取消的校验；`parse_wait` 的三种动作、`extend` 的范围、取消后拒绝 `extend`、`final` 的要求。
- `WaitService.run_once`（真 PG）：只按时间结束；检查就绪；先未就绪后就绪；检查出错、超时；`github_pr` 的固定参数（`gh` 替身记下 argv）、基线和三种 `ready_when`；`command` 用 `child_env` 的环境和 2 KiB 上限、输出不进日志；超时写 `timed_out` 并发通知（有渠道和没有渠道）；停掉的任务不检查；限制来自设置。
- 规划者：等待打开不派执行者、不占并发；就绪后依赖它的步骤派出、读到结果；超时 → `[WAIT]` 回合 → 延长 / 继续 / 失败；立即继续；取消 → `[WAIT]`；`waits` 取消；重启后接着等；任务状态 `waiting` 的选择。
- 接口：两个按钮的 200、404、409；设置的读写和校验。

**真实路径**：`looper serve`（端口 19050，新库，临时 runtime root），sonnet 规划者：一个项目开 `wait_commands`、等一个文件出现的任务，文件出现后依赖它的步骤运行；一个短限制下超时的等待，产生通知（Dashboard，不发真的渠道）和 `[WAIT]` 回合。无头浏览器截图等待步骤和按钮。

## 15. 观测

- 每个等待记下：打开、截止、关闭的时间，检查次数，最后一次结果和错误，延长次数，结局，超时通知是否发出。
- 活动：「Wait w1 opened …」「Wait w1 ready: …」「Wait w1 check failed (exit 1)」（同一个错误连续出现只记第一次）、「Wait w1 timed out …」「Wait w1 extended by 1 d」「Wait w1 resumed by the user」。
- 指标（由接口和评测给出）：每个项目的等待数、等待时长（打开到关闭）、结局分布、超时率、检查出错率。
- 告警：不加；超时通知就是对用户的告警。

## 16. 风险

| 风险 | 应对 |
|---|---|
| `command` 检查等于让规划者执行任意命令 | 默认关闭（D3），项目设置写明后果；60 秒超时、结束整棵进程树；输出限长、不进日志 |
| `gh` 的登录状态过期、限流 | 记 `error`，下一次照常检查；一直出错就超时、通知用户。最短间隔 300 秒，一个等待一天至多 288 次调用 |
| 规划者滥用等待（为自己能做的事等） | 约定写明「不要等本任务的步骤做的事」；评测的反方向任务 |
| 循环和用户、规划者同时改一行 | 只在规定的状态里各写各的，转换都带条件（第 6、13 节） |
| 服务停了很久 | 重启后过了截止时间的等待很快超时，通知用户（D11） |
| 基线在打开到第一次检查之间就变了 | 循环在打开后的下一个 tick（≤15 秒）记基线；这段时间里的变化会错过，写进已知限制 |
