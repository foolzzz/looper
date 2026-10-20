# RFC：定时任务共用一条规划者会话（v0）

- **状态：** 维护者已定（第 3 节 M1、M2，2026-10-19）。第 4–11 节是据此写下的设计，尚未实现。第 8 节不做阈值交接：规划者会话从不自动重置，长度由运行时自己的压缩封住。
- **相关：** [定时任务](scheduled-tasks-v0.zh-CN.md)、[多运行时](multi-runtime-agents-v0.zh-CN.md) 的会话续接（P5）。
- **最近更新：** 2026-10-19

## 1. 背景与目标

一次触发建出一个任务（`scheduler._create_task`，`tasks.schedule_id`）。这个任务的规划者从空白会话开始规划，跑完进程就拆掉。下一次触发又是一个新的 `planner-<task_id>`。最近几次运行的结果已经在需求末尾的 `scheduler.run_context` 里，原话、看过的代码和推理都不在。

**目标：**

- 非并行的定时任务（`overlap` 为 `skip` 或 `queue`）在各次运行之间保留一条规划者会话。每次触发仍是自己的任务：运行记录、花费、超时、活动都按这次运行记。
- 规划者记得更早的运行做了什么，同一条会话上的提示词缓存得以复用。
- 会话只有用户重置时才换掉。失败、改需求、改时间规则、改前置检查都不自动清。

**不做：**

- 不改系统提示，也不改 `REPLY_CONTRACT`。
- 不让 `overlap` 为 `parallel` 的运行共用会话。
- 不加定时任务的 CLI。飞书和 Telegram 不新开入口，经管家的定时任务工具到达。
- 不把会话 id 放进接口或页面。不导出、不导入已存会话。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 |
|---|---|
| 一次触发 | `Scheduler._create_task` 建 `task-<id>`，需求是定时任务的需求接上 `scheduler.run_context`（哪一次触发、前置检查输出、未完成的上一轮、最近 `CONTEXT_RUNS` 即 3 次运行、无人值守）。`overlap` 为 `skip` 或 `queue` 时，上一次的任务还在就不并行开下一个；`parallel` 在 `max_parallel_runs` 以内并行，前置检查彼此不重叠 |
| 规划者的会话 | 每个任务一个 `PlannerAgent`（`looper/agents/tiers/planner.py`）。`agent_id` 为 `planner-<task_id>`。会话在 `agent_sessions`（`looper/db/agent_sessions.py`）：`session_id`、`session_totals`、`runtime_type`、`provider_id`、`profile_id`。运行中只有 `Planner._save_session` 写这一行（导入旧文件的 `state_import` 不在每次运行的路径上） |
| 续接 | `_run_turn` 在有 `plan.session_id` 时把 `runtime.resume_args` 接在常驻命令后面（Claude Code 为 `--resume <id>`）。`session_totals` 是 `run_usage` 只计这次进程花费的基线 |
| 不能续 | `session_switch.profile_switch` 在每轮之前看已存的运行时和 provider：`runtimes.can_resume` 要求两者都不变，只换模型可以续。不能续则 `_switch_session`：旧进程还在就先一轮 `[HANDOFF]`（`_handoff_message`，至多 `journal.HANDOFF_MAX_CHARS` 即 3000 字），然后 `_drop_session` 忘掉会话。运行时没能续上（`turn.session_started` 为假）抛 `_SessionNotResumed`，记一条 error，再 `_drop_session(session_lost(...))` 并用新会话重试这一轮。新会话的第一轮带 `[RESUME]`（`_resume_brief`）。活动里已有 `The Planner starts a new session (...)` |
| 新计划 | `_plan_and_dispatch` 建一个没有会话的 `TaskPlan`，停掉进程，清掉待发的交接，`_save_session` 写下空会话，然后 `_ask(TurnKind.PLAN, _plan_message(...))`。注释写明新计划是一段新对话，不带 resume brief。服务重启走 `TASK_RESUME`：`_load_plan` 读回这个任务自己的会话，不重新规划 |
| 第一轮的消息 | `_plan_message` 以 `[PLAN] Plan the task in the Task section...` 开头，有补充说明和角色模板时接在后面，最后是看项目、回复 PLAN JSON。需求在系统提示的 Task 节（`plan_task_section`），不在这条用户消息里 |
| 上下文变长 | 有压缩事件时，`compact_boundary` 经 `run_usage.compaction` 记进该次运行的 `compactions`。哪些运行时自己压缩见第 8 节。代码里没有「上下文满了就交接」的触发 |
| 重置类入口 | 管家 `looper_schedules`（`looper/agents/ops/schedules.py`）的删除、启用、立即运行，以及提高或取消花费上限的修改，是 `high`：`_confirming` 给出中文说明，用户下一条消息确认后才执行。页面上删除等操作用 `confirm()`。`looper/cli.py` 没有定时任务命令 |
| 导出 | `config_transfer.schedule_entry` 只拷 `_SCHEDULE_FIELDS`（名称、时间规则、需求、重叠、前置检查、花费、通知等）。不导出是否启用、运行记录、目录。导入遇到未知键时把这一条定时任务跳过（`_Skip`） |

## 3. 维护者决定

| # | 决定 |
|---|---|
| **M1** | 一条定时任务在各次运行之间保留一条规划者会话。每次触发仍建自己的任务（运行、预算、超时、活动都按这次记），这次任务的规划者接着定时任务的会话。规划者记得更早的运行做了什么，提示词缓存得以复用 |
| **M2** | 只有用户重置才换掉这条会话：控制台按钮、HTTP API、管家的定时任务工具。失败不自动重置，编辑不自动重置 |

## 4. 存储

`schedules` 增加：

| 列 | 含义 |
|---|---|
| `planner_session_id` | 已存会话的 id；空为还没有，或已经重置 |
| `planner_session_totals` | `jsonb`，不是空对象时与 `agent_sessions.session_totals` 同形（`Usage.to_dict()`） |
| `planner_session_runtime` | 写下会话的运行时类型 |
| `planner_session_provider` | 写下会话的 provider |
| `planner_session_profile` | 写下会话的 Agent Profile id |
| `planner_session_task_id` | holder：当前拥有并更新这条会话的任务。任务删除时置空（`ON DELETE SET NULL`），会话 id 保留 |
| `planner_session_since` | 当前这个会话 id 写下的时刻。同一个 id 后来只更新花费累计时不动。状态行的「since」和接口的 `updated_at` 用它，不用 `schedules.updated_at`（改需求也会动那一列） |

`tasks` 增加 `continues_schedule_session boolean NOT NULL DEFAULT false`。只有这一轮的进程确实续上了已存会话（`turn.session_started`，且续的是认领时复制的那个 id）才设为 true。(b)(c)(d) 和并行、第一次、重置之后的全新运行保持 false。

迁移沿用 `schema.sql` 的写法：`ADD COLUMN IF NOT EXISTS`，并用一次性的 `schema_migrations` 名 `schedule_planner_session` 包住。再跑一遍是空操作。已有任务的新列是 false，已有定时任务的会话列是空。

**未采用：** 单独一张表，一个定时任务一行，和这些列没有额外的价值。把会话放进 `agent_sessions`、`agent_id` 用 `schedule-<id>`：规划者的保存和续接都按 `planner-<task_id>`，第二条键要另写一条保存路径，现有的 `resume_args` 也读不到。

## 5. 运行开始时认领

只在非并行定时任务的规划者开始一份**新计划**时认领，位于 `_plan_and_dispatch`、第一轮之前、现有的那次 `_save_session` 之前。`TASK_RESUME`、`_restart_plan`、`_continue_plan` 不认领：会话已经在这个任务的 `agent_sessions` 行上。`_restart_plan` 没有计划可接着做、因而再次进入 `_plan_and_dispatch` 时，holder 已是另一个还在跑的任务则不抢（见下），这次当全新计划，不写穿。

认领在一个事务里锁住定时任务那一行（`db_schedules.lock_schedule`，`SELECT … FOR UPDATE`），和重置抢同一把锁：

1. **并行**（`overlap = parallel`）：不读、不写。这次运行全新。已存会话留在原处。活动记一条 `phase`（第 7 节）。
2. **有已存会话：** 把 id、累计、运行时、provider、profile 复制进 `planner-<task_id>`，填进这份 `TaskPlan`。holder 改为这个任务。`planner_session_since` 不变（还是同一条会话）。第一轮因此走现有的 `resume_args`，不走 `[RESUME]`。`[RESUME]` 只在这条会话被丢掉、新开会话时出现。
3. **没有已存会话**（第一次，或重置之后）：任务从空白会话开始，并成为 holder。会话 id 等第一次 `_save_session` 写穿。

holder 指向的上一个任务已经结束时，新的认领直接改到自己。非并行本来就不会在上一次任务还在时再开一个。

**写穿：** `Planner._save_session` 在写完 `agent_sessions` 之后执行：

```sql
UPDATE schedules
SET planner_session_id = $1,
    planner_session_totals = $2,
    planner_session_runtime = $3,
    planner_session_provider = $4,
    planner_session_profile = $5,
    planner_session_since = CASE WHEN planner_session_id IS DISTINCT FROM $1 THEN now()
                                 ELSE planner_session_since END
WHERE id = $6 AND planner_session_task_id = $7
```

`$7` 是这个任务。重置把 holder 清掉之后，这一句改到 0 行，进行中的运行写不回去。写穿失败只记日志，与今天 `_save_session` 失败一样；任务自己的那一行已经留下，holder 还是它时，下一次保存会再写穿。

**未采用：** 写穿不看 holder、谁后写谁赢。重置和进行中的保存会互相覆盖。只存会话 id：`profile_switch` 要运行时和 provider（`can_resume`），`session_totals` 是续接后只计本轮花费的基线。

## 6. 续接运行的第一轮

系统提示不改。`_system_prompt` 和 `REPLY_CONTRACT` 都不加字。需求、前置检查输出和最近几次运行的结果仍在 Task 节：那是 `run_requirement` 接上的 `run_context`，每次运行本来就不同。

续接已存会话时，`_plan_message` 在今天的正文前加两段，由纯函数 `continuing_run_preface(task_id, previous)` 生成。`previous` 是上一条已经结束的 `schedule_runs`（`finished_at` 非空，不是这次任务）；没有则为空，第二段不出现。状态用 `scheduler._STATUS_WORDS` 里已有的中文。摘要取 `summary`，没有则取 `error`，按 `scheduler.SUMMARY_CHARS`（300）截断，与 `run_context` 的摘录同一限度。措辞固定：

```
这是这个定时任务的一次新运行，任务编号是 {task_id}。上面这段对话都是更早的运行；那些运行的任务编号、步骤、计划和关卡都不属于这个任务。这个任务的需求和前置检查输出在后面（这一轮的 Task 一节）。做这次的需求；更早的运行只当作记忆。

上一轮：{状态} · {摘要或错误}

不要沿用更早一次运行的步骤编号；这个任务的步骤用新的编号。
```

认领或续接了会话的非并行运行（第一次、续接、重置之后的下一次，以及换会话后仍写穿的那一次），PLAN 用户消息在正文前再放这一轮的需求原文（定时任务需求加上运行上下文，含前置检查输出）。标题固定为 `This run's requirement (kept in the conversation for later runs):`。截断与 Task 节相同（`TASK_REQUIREMENT_LIMIT`，超出时同一句 `[clipped: N more characters]`）。后来续上的会话里因此有每一次更早运行的输入。系统提示不变。

非定时任务、并行运行、认领失败、holder 被占用：`_plan_message` 与今天逐字节相同，用测试钉住。

组消息的时机：`profile_switch` 在送出之前就判定不能续时，不附加上面那两段续接说明；需求原文仍在，因为这次运行仍写穿替换后的会话。`_SessionNotResumed` 的重试改用清掉续接标记之后的 `_plan_message`：没有续接说明，仍带这一轮的需求，不把已经带说明的那一份再送一遍。今天的重试复用同一份消息；这一处分支只在「复制来的会话没能续上」时发生。

**未采用：** 把这段写进系统提示。常驻进程的系统提示一变就要重启再续接，回复约定也会变；这段只属于续接的那一轮用户消息。每次运行改开新会话、只把上一轮总结塞进去：`run_context` 已经带了最近三次，新会话丢掉原话和缓存，与 M1 相反。

## 7. 回退

四种情况都记一条任务活动，`kind` 为 `phase`，都**不**重置。现有的换会话活动（`The Planner starts a new session (...)`）和 `_SessionNotResumed` 的 error 照旧。(b)(c) 的新会话经第 5 节的写穿替换已存会话，因为旧的已经不能用；留着的话，以后每一次都会重新全新。下一次运行续接的是替换后的这条。

| | 情形 | 这次运行 | 已存会话 | 活动摘要 |
|---|---|---|---|---|
| (a) | `overlap` 为 `parallel` | 全新，不认领、不写穿 | 不动 | `Schedule Planner session left as stored: this run overlaps (parallel) and starts fresh` |
| (b) | `profile_switch` 判定当前 profile 的运行时或 provider 续不了已存会话 | 走现有的 `_switch_session`。新任务刚启动，`_plan_and_dispatch` 已停掉进程，旧进程不在，写不出交接，与今天「旧进程不在」相同 | 新会话替换它 | `Schedule Planner session replaced: the stored session cannot be resumed ({switch.describe()}); this run starts fresh` |
| (c) | 运行时续不上或会话已经不在（`_SessionNotResumed`） | 现有路径：丢掉会话，新会话重试这一轮，第一轮带 `[RESUME]` | 新会话替换它 | `Schedule Planner session replaced: the stored session could not be resumed ({detail}); this run starts fresh` |
| (d) | 认领失败（数据库错误） | 全新 | 不动 | `Schedule Planner session not claimed: {error}; this run starts fresh` |

续上时另记一条 `phase`：`Continuing the schedule's Planner session`。`continues_schedule_session` 在这一刻设为 true。

错误文字截到活动摘要能放下的长度（现有 `_activity` 的调用方常用 500 字上限）。

## 8. 上下文变长

不因为上下文变长而重置（M2）。规划者会话从不自动重置。不做阈值交接。会话多长由运行时自己的压缩封住。

运行时先压缩同一条会话。压缩之后会话仍不可用，才落到第 7 节 (b) 或 (c)。

现有的交接已经把连续性留下来：旧进程还在时先一轮 `[HANDOFF]`，新会话的第一轮带 `[RESUME]`（`_resume_brief`：为什么是新会话、任务走到哪、交接或「没有交接，剩下的是任务日志」）。新的 `session_id` 经 `_save_session` 写穿。holder 还是这次任务时，定时任务上的会话换成新的，下一次运行续接它。这不算重置。

一条会话比单个任务长得多，压缩会更频繁。压缩之后仍不可用时才交接，次数会多几次。接受这一点：M1 要的是跨运行的记忆和同一条会话上的缓存；交接至多 3000 字，计划和这次的需求仍由代码每轮放进消息和 Task 节。

各运行时自己压缩的情况：

- Claude Code 自动压缩。`compact_boundary` 事件记进该次运行的 `compactions`。
- Grok 上报压缩事件（`looper/agents/runtimes/grok.py` 的 `Capability.COMPACTION_EVENTS`），因此会压缩。
- OMP 的压缩摘要是后台请求，Looper 不上报（`looper/agents/runtimes/omp.py` 的模块说明），因此会压缩，但不上报。
- Codex：Looper 的适配器写明该运行时不上报压缩（`looper/agents/runtimes/codex.py`）。Codex 自己会不会压缩很长的会话，需要验证。

**未采用：** 上下文超过某个阈值就重置。那会在用户没有要求时丢掉记忆，也让「以后每次都全新」重新出现。按阈值交接：认领时看已存会话最后一次的 `context_tokens`，超过阈值这次运行就不接着整段旧会话，先一轮 `[HANDOFF]`，再新开会话，第一轮带 `[RESUME]`，记忆经交接摘要带过去。维护者的决定：不做阈值交接；规划者会话从不自动重置；长度由运行时自己的压缩封住。

## 9. 重置

一次 `UPDATE` 清掉第 4 节的会话列和 holder（`planner_session_totals` 回到 `{}`），返回是否清掉过已存会话（`planner_session_id` 原先非空）。下一次运行走第 5 节的「没有已存会话」。

进行中的运行留着自己的 `agent_sessions` 行，直到结束。holder 已经空了，它的 `_save_session` 写穿改到 0 行。

| 入口 | 做法 |
|---|---|
| HTTP | `POST /api/schedules/<id>/reset-planner-session`。路由放在 `http_server.py` 现有的 `/api/schedules/<id>` 旁边，调用者校验与 `PUT`、`DELETE /api/schedules/<id>` 相同。定时任务属于项目，不另做一套授权。未知 id 为 404。存在则 200，`{"cleared": true\|false}` |
| 管家 | `looper_schedules` 增加动作 `reset_planner_session`。与删除、启用、立即运行一样升到 `high`：`_confirming` 先给出说明，用户下一条消息确认后才执行。说明写明：已存会话会清掉，下一次运行从新会话开始；正在进行的运行留着自己的会话直到结束，并且不会写回。当前没有已存会话时，说明里写明这一点 |
| 控制台 | 定时任务行上（`project-schedules.tsx` 里与 Enable、Run now 一排）放按钮 “Reset Planner session”，用页面上已有的 `confirm()`。确认文案说明下一次运行从新会话开始，进行中的这次不受影响、也不会写回 |
| 飞书、Telegram | 不新开入口。用户对管家说重置，走上面的工具 |
| CLI | 不改 |

**未采用：** 失败时或保存会话失败时自动调用这次重置。进行中的运行会被切掉，与 M2 相反。

## 10. 可见性

定时任务的接口对象（`schedules.schedule_view`，列表和单条共用）增加 `planner_session`：

```json
{"active": true, "runtime": "claude-code", "profile": "prof-…",
 "last_task_id": "task-…", "last_task_title": "alarm-triage · 10-19 09:00",
 "updated_at": "2026-10-19T01:00:00+00:00"}
```

`active` 为 `planner_session_id` 非空。`runtime`、`profile` 是存下的运行时类型和 profile id。`last_task_id` 是 holder。`last_task_title` 由 holder 任务的 `task_name` 填上，只为状态行，不是表列。`updated_at` 是 `planner_session_since`。没有会话时 `active` 为 false，其余为 null。任何响应都不带会话 id。

定时任务行在重置按钮旁边一行状态：

- 有会话：`Planner session: continuing since <时区里的 since>，last used by run <任务名>`
- 没有：`Planner session: none yet`

时间用这个定时任务的时区，与行上的下次触发相同。`parallel` 的行也显示：它的运行不认领、不写穿，但从 `skip` 改成 `parallel` 之前留下的会话还在，重置仍可清掉。

任务页（`task-detail.tsx`，紧挨现有的定时任务标记）在 `continues_schedule_session` 为 true 时显示 “continues the schedule's Planner session”。`GET /api/tasks/:id` 带上这个布尔。

## 11. 导出、导入与编辑

已存会话是运行时状态。`schedule_entry` 的白名单不含这些列，导出里没有。导入不读这些列。文件里若出现它们，现有的未知字段检查把这条定时任务整条跳过（`_Skip`，原因 `unknown field …`），不会把会话写进来。导出测试断言这些键不在。

编辑需求、时间规则或前置检查都不碰会话列。下一次运行的第一轮在 Task 节里看到新的需求（和 `run_context`），续接说明仍告诉它：上面是记忆，需求才是这次要做的。把 `overlap` 改成 `parallel` 也不清会话；并行运行不动它，改回 `skip` 或 `queue` 之后的下一次运行继续它。

删除定时任务时，会话列随那一行消失。进行中的任务留着自己的 `agent_sessions`；`schedule_id` 按现有的 `ON DELETE SET NULL` 置空，写穿的 `WHERE id = …` 改到 0 行。

## 12. 默认行为的变化

实现的 PR 在描述里写明：

- 每个非并行定时任务的第二次及以后的运行，规划者接着同一条会话，不再每次一个空白规划者。这是 M1 要的变化。第一次运行、重置之后的下一次、以及 `parallel`，规划者仍从空白会话开始。
- 续接的那一轮，PLAN 的用户消息多出第 6 节的两段。系统提示的字节数不因本设计增加。
- `parallel` 的定时任务与今天相同：每次运行一个新的规划者会话，已存会话既不读也不写。

## 13. 风险

| 风险 | 处理 |
|---|---|
| 会话无限变长 | 运行时自己的压缩把长度封住；压缩之后仍不可用则走现有交接，新会话写穿（第 8 节）。不另加阈值重置，也不做阈值交接 |
| 每次运行的花费：缓存使输入变便宜，长上下文使输入变贵 | 第一轮的 token 已经记在这次运行的 `llm_records`（`run_usage`：输入、缓存读、缓存写、`context_tokens`）。不新增指标，看这一轮的用量 |
| 运行间隔长于提示词缓存的 TTL：缓存过期后按全价重读整段会话，花费随会话变长而上升 | 提示词缓存只在两次运行近于它的 TTL 时有用（分钟级，最长大约一小时）。按天触发，或按小时但两次之间留有余量，下一次会看到缓存已经过期，于是把已经变长的整段会话按输入全价再读一遍。一条 15 万 token 的会话，对比全新规划者第一轮大约 2 万 token，每次运行的输入大约是 7 倍。花费可以随会话变长而上升，和靠缓存省 token 相反。这时省下来的是记忆，不是缓存。会话多长由运行时自己的压缩封住。仍不新增指标，看这次运行的 `llm_records` |
| 会话里留着过时的判断 | 第 6 节写明更早的运行只是记忆，这次的需求才是要做的；需求每轮在 Task 节里 |
| 认领和第一次写穿之间崩溃 | 定时任务上的会话 id 还是认领前的那份（写穿没发生）。崩溃那次任务的 `agent_sessions` 行是一份拷贝，没有进程再读。下一次认领从定时任务重新复制到新任务的行 |
| 重置与认领交错 | 两者都锁定时任务那一行。重置先提交：这次认领读到空，本轮全新并成为 holder，它的新会话就是重置之后的第一条。认领先提交：重置清掉 holder，这次运行写不回，再下一次全新 |
| 两个并行运行共用一条会话 | (a) 不认领、不写穿。非并行在调度上不会同时有两个任务 |
| 更早一次运行被用户「重新规划」时抢走 holder | holder 指向另一个还在跑的任务时不抢，这次不写穿（第 5 节） |

## 14. 测试

单元和数据库（真实 PG）：

- 第二次及以后的运行：认领把已存会话复制进 `planner-<task_id>`，第一轮的命令带 `resume_args`，`continues_schedule_session` 为 true，写穿更新累计且 `planner_session_since` 不变；第三次续的是第二次写下的 id。
- 第 7 节四种回退各一条：活动里有那一行 `phase` 和其中的原因；(a)(d) 不改已存会话；(b)(c) 的新会话 id 写进定时任务，这次任务的 `continues_schedule_session` 为 false，再下一次运行续接替换后的 id。
- `parallel` 的两次运行都不读、不写已存会话，两个 `agent_sessions` 行彼此不同。
- 重置：`cleared` 与清空后的列；进行中的运行之后的 `_save_session` 不写回；它自己的会话还在；下一次运行的 `_plan_message` 没有续接说明，并带上这一轮的需求原文。
- 交接：一次运行中途走 `_drop_session` 再写下新会话，定时任务上的 id 换成新的；下一次运行复制的是新 id。这不算重置（holder 仍是发生交接的那次任务，直到它结束）。
- 非定时任务和并行运行：`_plan_message` 与今天的拼接逐字节相同。第一次认领：用户消息带这一轮的需求原文，没有续接说明。续接运行的消息等于 `continuing_run_preface`、这一轮的需求原文、今天的正文。系统提示不含这段中文，也不含需求标题。
- 导出的定时任务对象没有 `planner_session` 各列；导入不写入它们。
- `POST /api/schedules/<id>/reset-planner-session` 的 200 和 404；管家工具未确认时 `needs_confirmation`，确认后清掉，与删除同一套 `high` 约定。
- 迁移跑两遍，第二遍不改列、不报错。
- 改需求、改 cron、改 `gate_command` 之后会话列仍在。

**真实路径：** 一次性 `looper serve`（新库、一次性端口），规划者用 sonnet。对一条非并行定时任务手动触发两次：第一次运行里让规划者确认一个只在对话里出现的事实，第二次的需求问这个事实，它应当答得出来。然后重置，第三次手动触发，规划者应当不记得该事实（`_plan_message` 无续接说明，会话是新的）。并行定时任务不进这条路径。
