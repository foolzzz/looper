# RFC：度量与评测底座 v0（任务结果指标、尝试记录、评测集与基线）

- **状态：** 草案，等维护者评审。第 3 节是推荐的决定，第 15 节是待定问题，每条都附了推荐的默认做法。评审通过后按第 13 节的 PR 顺序实现。
- **需求：** 回答「Looper 把任务做对了没有、花了多少」：
  - 任务成功了多少；
  - 一次通过的步骤占多少，返工多少次；
  - 评审拦下的是不是真问题、有没有漏；
  - 每个被接受的改动花了多少钱，花了多久。

  答案只从代码在决定点写下的类型化记录里算，不从日志文字里扒。评测集用同一套记录出报告，每次改策略都和基线对比。
- **里程碑命名：** 按维护者的产品设计，本 RFC 是其中的 M1（度量与评测底座，产品设计第 6.2 节和第 10.3 节），是「把任务做对」这条线（M1 → M2 可执行验收 → M3 升级阶梯与 advisor）的第一块。开发计划第 2、3 节里的 M1 是另一件已交付的事（上下文隔离与可复现），两套编号互不影响。
- **相关：**
  - [Advisor RFC](advisor-v0.zh-CN.md)：`advisor_advice.attempt_id` 留给本文的 `step_attempts`；它的看板指标和三组对照评测等本文的难题集。
  - [助理层 RFC](assistant-layer-v0.zh-CN.md)：第 4.1 节是管家回合的度量（`turn_metrics`、`manager_metrics.get`），第 14 节是出口标准。本文管任务这一侧，看板放在同一页。
  - [Agent 系统架构](../agent-system.md)，以及 [`evals/README.md`](../../../evals/README.md) 的 "Prompt regression"、"Which tier to run"、"Assistant experience"。
  - [开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md)第 1 节：评测档位和基线刷新的规则。
- **核对基线：** `main` `1f2ac843e`。第 4 节的现状是读代码得出的，每项标了出处。本文没有启动服务，也没有调用模型。
- **术语：**
  - **尝试（attempt）：** 一个工作步骤从派出一个 Worker 运行开始，到 Planner 对这次结果做出决定为止。中间的评审、advisor 请教都算在这次尝试里。重试、被重新打开，都是同一步骤的下一次尝试。
  - **被接受的步骤：** `kind = work` 的步骤进入 `verified`，并且有它的 `STEP_ACCEPTED` 日志。本文的「被接受的改动」就指它（第 3 节 D8）。
  - **决定：** Planner 对一次尝试的结论：`accept`、`retry`、`replan`、`fail`，或者这一步被 `skip`，或者任务先结束了。
  - **等价花费：** 订阅登录的运行由运行时报告的花费，不是真花的钱（`llm_records` 的 `accounting_basis`）。所有花费指标都把它和按供应商价格计费的花费分开列。

## 1. 背景与目标

Looper 现在能说出「这个任务花了多少钱」（`task_costs`），能在评测里说出「这个用例过没过」（`evals/prompt_regression`），但下面这些问题回答不了，或者只能靠读日志文字：

1. 一个步骤试了几次、每次用的哪个 Profile、每次为什么失败、每次花了多少。`task_steps.result` 每次尝试都被覆盖，运行记录上也没有尝试编号。
2. Planner 对每次尝试的决定。只有打开 `decision_snapshots` 时才有类型化的记录（默认关），而且只留 30 天。
3. 失败的原因。`RunOutcome.RUN_FAILED` 把出错、超时、触到花费上限混在一起；超时和花费上限只在错误文字里。
4. 任务的结局。重启任务会清掉 `tasks` 上的结局字段；「用户停止」和「服务重启时停掉」在行上分不出来。
5. 评审有没有漏。接受以后又发现问题的信号没有统一的类型（面板评审的晚到评审、验证失败、用户打回各走各的）。
6. 评测指标的口径。评测的一部分指标（Planner 违反契约的次数）是从日志文字里数出来的，和线上的口径对不上。

**目标：**

- 每次尝试一行类型化记录，每个任务结局一行不可变记录；花费能沿运行树归到尝试和任务。
- 一组按结果统计的指标（第 5 节），每个指标都写明来源、口径和样本要求。
- 只读接口、任务页的「指标」卡片、控制台首页的「任务质量」一组，以及默认关闭的回退告警。
- 评测集的分层结构和基线规则，评测报告从同一套记录读数。新增 3 个「第一次大概率失败」的难题用例，作为 M2、M3 的对照。
- 核心 PR 不改任何模型可见的内容。所有写入都是代码在已有的决定点上做的，读取只走接口和界面。

**不做：**

- 可执行验收（`validation_commands` 由代码执行）是 M2。本文只给检查结果留出失败分类的位置，不建 `step_check_results`。
- 不追踪任务分支是否被合进主干：Looper 不建 MR/PR，也不记录合并（第 15 节 Q3）。
- 不做用户满意度打分，只做一个类型化的「交付有问题」标记（D9）。
- 不改评测的计分方式（用例的通过条件仍由各用例的检查函数决定），只换读数的来源，并统一报告。

## 2. 用户入口的变化

| 入口 | 变化 |
|---|---|
| 控制台 | 任务页多一张「指标」卡片；首页多一组「任务质量」；已完成的任务多一个「交付有问题」按钮（操作者专用） |
| CLI | `loopercli metrics task_outcomes`、`loopercli metrics step_attempts`（由 v1 目录生成）；评测命令多出尝试和花费拆分几列 |
| 飞书、Telegram | 默认没有变化；回退告警打开后，每天最多一条，发给操作者（D12） |
| Agent | 没有变化：不加工具，不改提示词，不改契约 |

## 3. 决定（推荐）

| 编号 | 决定 | 理由 |
|---|---|---|
| D1 | **只用类型化记录。** 每个指标都由代码在决定点写下的列或枚举算出来。日志文字、`activity` 的摘要、日志条目的正文都不当数据源。评测里从日志数出来的指标改为读同样的记录（第 9.2 节）。 | 线上和评测是同一个口径。改一行日志不会悄悄改掉一个指标 |
| D2 | **新表 `step_attempts`：** 每次尝试一行，派出 Worker 时由代码写入，Planner 做出决定时补全（第 6.1 节）。 | 尝试是返工、一次通过率和花费拆分的共同单位；`task_steps` 每次都会被覆盖 |
| D3 | **新表 `task_outcomes`：** 任务每到一次终态写一行，以后不再改，任务重启也不删（第 6.2 节）。 | 重启会清掉 `tasks` 上的结局；结局历史和「谁停的」需要一张只追加的表 |
| D4 | **`llm_records` 只加一列可空的 `attempt_id`。** Worker、Reviewer 的运行和 Planner 为这次尝试做决定的那一轮写它；子运行（advisor 请教、判别）沿 `parent_record_id` 归到同一次尝试。 | 大表只加一列，不改其他行；`step_id` 和 Profile 已经在 `metadata` 里，尝试表上也有，不在大表上再存一份 |
| D5 | **失败分类是一个枚举 `FailureKind`**，由代码按类型化的结果映射（第 6.3 节）。`RunOutcome.RUN_FAILED` 拆成出错、超时、触到花费上限、进程崩溃，拆分依据是运行时已有的类型化信号，不读错误文字。 | 产品设计要求「失败原因分类」；现在超时和花费上限只有文字 |
| D6 | **决定总是记录。** Planner 应用决定时（`_decided`）把它写到这次尝试上，不受 `decision_snapshots` 开关影响。`decision_snapshots` 保留为可选的完整快照。 | 决定是指标的核心字段，不能默认不记 |
| D7 | **重新打开算一次新尝试**，带类型化的触发原因（`retry`、`reopen_late_review`、`reopen_verification`）。漏检的信号就是这些重新打开。 | 「评审漏了什么」有了统一的计数方式，不用分别看三处 |
| D8 | **「被接受的改动」= 被接受的工作步骤。** 花费指标同时给两个：每个被接受步骤的花费（这一步所有尝试的 Worker、评审、advisor 花费，失败的尝试也算），每个完成任务的花费（任务的全部运行，含规划）。 | 步骤是 Looper 里最小的、有明确接受动作的交付单位；任务分支是否合入主干 Looper 看不到 |
| D9 | **用户打回有一个类型化标记：** 已完成的任务上，操作者可以标「交付有问题」并选一个原因（`task_feedback`，操作者专用，不进任何 agent 的上下文）。运行中的补充说明和需求修改，已有类型化的记录（`SUPPLEMENT` 日志条目、`task_requirement_versions`），一起计入「用户纠正」。 | 产品设计的质量指标包括「被用户打回的比例」；现在完成以后的打回没有任何记录 |
| D10 | **花费分开列：** 每个花费指标都分「按供应商价格」和「等价花费」两列，沿用 `spend_since` 的划分。 | 订阅登录的数字不是钱，混在一起会误导 |
| D11 | **指标有样本要求：** 比率类指标只在样本数不少于 20 时显示数值，否则显示「样本不足（n=…）」；百分比都附 Wilson 95% 区间。 | 单个任务的偶然结果不能当趋势 |
| D12 | **回退告警默认关闭**，开关是 `metrics.alerts.enabled`。打开以后每天检查一次，最多发一条（第 8 节）。 | 先有一周以上的数据，再决定阈值是否合适 |
| D13 | **评测分四层，基线按「套件 × 模型」提交**，每次发布前的全量档位刷新。PR 只跑受影响的用例，和基线对比时按第 9.3 节的规则标出回退，由人判断，不自动拦合并。 | 样本小、真实模型贵；自动拦截会被噪声误伤 |
| D14 | **核心 PR 不改模型可见内容。** 新增的难题用例只在评测里。 | 度量本身不能改变被度量的东西 |

## 4. 现状（读代码得出）

| 方面 | 现状 | 出处 |
|---|---|---|
| 任务结局 | `tasks.status` 是自由文本，没有 CHECK，取值集合见 `STATUS_GROUPS`。`update_task_status` 在 completed、failed、cancelled 时写 `completed_at`。`fail_kind` 只允许 `task_budget` | `looper/db/tasks.py`；`looper/db/schema.sql` `tasks` |
| 重启 | 清掉 status、`completed_at`、`fail_kind`、error、summary 和 `process_record`；日志条目和 `llm_records` 保留 | `looper/db/tasks.py`（重启任务的那段） |
| 停止和取消 | 取消只来自用户或管家的控制；`stopped` 既可能是用户停止，也可能是重启时被收尾，行上分不出来 | `looper/agents/projects.py`；`looper/db/tasks.py` |
| 步骤 | `task_steps` 的 `status` 是自由文本，`retries` 是重试次数，`feedback[]` 每次重试追加一条；每次尝试都覆盖 `result` | `looper/db/schema.sql` `task_steps`；`looper/agents/tiers/planner.py` |
| 决定 | `accept/review/retry/replan/fail/advise/proceed` 在 `_decided` 里应用；只有打开 `decision_snapshots` 时才有类型化的快照，默认关，留 30 天 | `planner_protocol.py`；`planner.py` `_decided`；`looper/db/decision_snapshots.py` |
| 日志条目 | 种类有 CHECK，但只有 `summary`、`learnings`、`via` 三列，没有结构化的载荷；有的摘要是 JSON，有的是文字 | `looper/db/schema.sql` `task_journal`；`looper/agents/tiers/journal.py` |
| 运行结果 | Worker 和 Reviewer 的运行在 `metadata.outcome` 里记 `RunOutcome`（completed、reported_failed、files_mismatch、passed、not_passed、contract、run_failed）；`metadata.step_id`、`metadata.profile` 也在 | `looper/agents/tiers/worker.py`；`looper/agents/claude_cli.py`；`looper/db/llm_records.py` |
| 运行树 | `llm_records.run_kind`（有 CHECK）和 `parent_record_id` 已有；advisor、判别的运行把发起它的回合记为父运行 | `looper/db/schema.sql` `llm_records`；`looper/agents/spawn.py` |
| 评审 | 结论是布尔值 `passed`，加上 `findings[{severity: blocking/suggestion, where, text}]`；裁决可以把一条阻塞降成建议 | `worker.py`；`review_adjudication.py` |
| 已有的统计 | `task_costs`：按角色和模型的花费；`role_runs`：按角色、步骤、结果分组；任务页的角色统计里有 `review_pass_rate`、`contract_violation_rate`、retries | `looper/db/llm_records.py`；`looper/agents/task_roles.py` |
| advisor | `advisor_advice.attempt` 是现在唯一类型化的尝试编号，`attempt_id` 留给本文；`unlocked` 记下这次尝试后来有没有通过 | `looper/db/schema.sql` `advisor_advice`；`docs/architecture/rfcs/advisor-v0.zh-CN.md` D6 |
| 时间 | 任务有 `created_at`、`completed_at`、`attempt_ended_at`，没有开始时间；步骤的 `started_at` 是第一次派出、`completed_at` 是第一次到终态，都不是接受的时间；关卡、等待有各自的打开和关闭时间 | `looper/db/schema.sql`；`looper/db/plans.py` |
| 评测 | `prompt_regression` 每个用例输出 pass/fail/error、`error_class`、retries、reviews、花费、token，表里给 Wilson 区间；违反契约的次数从日志文字里数 | `evals/README.md`；`evals/prompt_regression/metrics.py` |
| 管家这一侧 | 已有：管家每个回合的回执延迟、首条回复延迟、空交付（`turn_metrics`）；主动消息和回复率；`manager_metrics.get`；首页「管家」一组 | 助理层 RFC 第 4.1 节；#1488 |

## 5. 指标定义

所有指标都可以按时间窗口（默认最近 7 天，对照前 28 天）、项目、Profile（尝试用的那个）、步骤难度（`difficulty`）过滤。比率类遵守 D11。

### 5.1 任务

| 指标 | 定义 | 来源 |
|---|---|---|
| 任务成功率 | 窗口内结束的任务里 `outcome = completed` 的比例；用户取消（`cancelled`）和服务重启收尾（`stop_cause = restart`）不计入分母 | `task_outcomes` |
| 预算失败率 | `outcome = failed` 且 `failure_kind = task_budget` 的比例 | `task_outcomes` |
| 每个完成任务的花费 | 完成任务的全部运行花费（规划、执行、评审、advisor、判别），分「按供应商价格」和「等价花费」 | `task_outcomes` 的花费快照（等于 `task_costs`） |
| 任务时长 | 从第一个 Planner 运行开始到终态的时间（`started_at → ended_at`），P50、P95 | `task_outcomes` |
| agent 时长 | 任务时长减去等人的时间（关卡从打开到关闭、等待从打开到关闭，重叠只算一次） | `task_outcomes.human_wait_ms` |
| 排队时间 | `tasks.created_at → task_outcomes.started_at` | `tasks`、`task_outcomes` |
| 用户纠正率 | 有补充说明、需求修改，或被标「交付有问题」的任务所占比例，三项分开列 | `task_journal`（`SUPPLEMENT`）、`task_requirement_versions`、`task_feedback` |

### 5.2 步骤与返工

| 指标 | 定义 | 来源 |
|---|---|---|
| 一次通过率 | 被接受的工作步骤里，第 1 次尝试就被接受、之后也没有重新打开的比例 | `step_attempts` |
| 每个被接受步骤的尝试次数 | 被接受步骤的平均尝试次数和分布（1、2、3、4 次及以上） | `step_attempts` |
| 返工率 | 尝试里触发原因是 `retry` 或 `reopen_*` 的比例 | `step_attempts.trigger` |
| 失败原因分布 | 没被接受的尝试按 `failure_kind` 计数 | `step_attempts.failure_kind` |
| 每个被接受步骤的花费 | 这一步所有尝试的花费（Worker、评审、advisor 及其子运行），分「首次尝试」和「重试」，分「按供应商价格」和「等价花费」 | `step_attempts` 加运行树 |
| 步骤时长 | 第一次派出到被接受（`STEP_ACCEPTED` 的时间），P50、P95 | `step_attempts` |

### 5.3 评审

| 指标 | 定义 | 来源 |
|---|---|---|
| 评审拦截率 | 有评审的尝试里评审没通过（`review_outcome = not_passed`）的比例 | `step_attempts` |
| 有效拦截率 | 评审没通过的尝试里，这一步后来被接受的比例（也就是拦下以后修好了） | `step_attempts` |
| 漏检数 | 被接受以后又被重新打开的尝试数（`trigger` 是 `reopen_late_review` 或 `reopen_verification`），再加上被标「交付有问题」、原因为「有缺陷」的完成任务数 | `step_attempts`、`task_feedback` |
| 评审捕获率 | 有效拦截 ÷（有效拦截 + 漏检）；分母在 D11 的要求以下时不显示 | 同上 |
| 裁决率 | 有阻塞结论被裁决降级的尝试所占比例 | `step_attempts.adjudicated` |

**口径说明：** 评审拦截率高不一定是好事（可能是评审过严，或者 Worker 太弱），所以总是和有效拦截率、裁决率一起看。漏检只统计 Looper 看得到的信号；任务交付以后在 Looper 之外发现的问题，要靠「交付有问题」标记才能计入（D9）。

### 5.4 契约与运行

| 指标 | 定义 | 来源 |
|---|---|---|
| 违反契约率 | Worker、Reviewer 运行里 `outcome = contract` 的比例；Planner 回复被解析器拒绝的次数 ÷ Planner 回合数 | `llm_records.metadata.outcome`；新增 `llm_records.metadata.contract_rejections`（由解析器写入，第 6.4 节） |
| 运行出错率 | 运行里 `failure_kind` 属于运行类（出错、超时、花费上限、崩溃）的比例 | `step_attempts` |

## 6. 数据模型与写入点

### 6.1 `step_attempts`

```sql
-- step attempts begin
CREATE TABLE IF NOT EXISTS step_attempts (
    id               BIGSERIAL PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id          TEXT NOT NULL REFERENCES task_steps(id) ON DELETE CASCADE,
    attempt          INTEGER NOT NULL CHECK (attempt >= 1),
    trigger          TEXT NOT NULL CHECK (trigger IN ('first', 'retry', 'reopen_late_review', 'reopen_verification')),
    profile_id       TEXT,                    -- 这次尝试的 Worker 运行用的 Agent Profile
    worker_record_id BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    run_outcome      TEXT CHECK (run_outcome IN ('completed', 'reported_failed', 'files_mismatch', 'contract',
                                                 'run_error', 'timeout', 'run_cap', 'crash')),
    review_outcome   TEXT CHECK (review_outcome IN ('none', 'passed', 'not_passed', 'passed_by_adjudication')),
    review_runs      INTEGER NOT NULL DEFAULT 0 CHECK (review_runs >= 0),
    blocking_findings INTEGER NOT NULL DEFAULT 0 CHECK (blocking_findings >= 0),
    adjudicated      BOOLEAN NOT NULL DEFAULT false,
    decision         TEXT CHECK (decision IN ('accept', 'retry', 'replan', 'fail', 'skip', 'task_ended')),
    failure_kind     TEXT CHECK (failure_kind IN ('review_rejected', 'contract', 'files_mismatch', 'reported_failed',
                                                  'run_error', 'timeout', 'run_cap', 'crash', 'task_budget',
                                                  'check_failed', 'stopped')),
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at       TIMESTAMPTZ,
    UNIQUE (step_id, attempt),
    CONSTRAINT step_attempts_decided CHECK ((decision IS NULL) = (decided_at IS NULL)),
    CONSTRAINT step_attempts_failure CHECK (decision IS DISTINCT FROM 'accept' OR failure_kind IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_step_attempts_task ON step_attempts (task_id, step_id, attempt);
CREATE INDEX IF NOT EXISTS idx_step_attempts_decided ON step_attempts (decided_at) WHERE decided_at IS NOT NULL;
-- step attempts end
```

| 时点 | 谁写 | 写什么 |
|---|---|---|
| Planner 派出 Worker（`PlannerAgent._dispatch_step`） | 派发代码 | 新行：`attempt = retries + 1`，`trigger`（第一次为 `first`，重试为 `retry`，重新打开为对应原因），`profile_id`，`started_at`；Worker 运行的记录打开以后回填 `worker_record_id`，并在运行记录上写 `attempt_id` |
| Worker 运行结束（`worker.judge_result`） | Worker 的收尾代码 | `run_outcome`：由 `RunOutcome` 映射，`run_failed` 按第 6.3 节拆开 |
| 评审运行结束（`PlannerAgent._review_done`） | Planner | `review_runs + 1`；最后一轮的 `review_outcome`；这一轮阻塞结论的条数；评审运行的记录上写同一个 `attempt_id` |
| 裁决（`review_adjudication.adjudicate`） | Planner | `adjudicated = true`；`review_outcome = passed_by_adjudication` |
| Planner 应用决定（`PlannerAgent._decided`） | Planner | `decision`、`decided_at`，没被接受时还有 `failure_kind`（第 6.3 节）；做这个决定的 Planner 回合的记录上写 `attempt_id` |
| 步骤被跳过，或任务先结束 | Planner 收尾、任务收尾 | 还开着的尝试补 `decision = skip` 或 `task_ended`，`failure_kind = stopped`（被停止时） |

- **失败语义：** 这些写入都是观测，数据库出错只记日志，不影响任务。唯一的例外是派出时的插入：插入失败时任务照常派出，这次尝试在指标里算缺失（第 7 节的接口会报告缺失的尝试数）。
- **一一对应：** 每个 Worker 运行对应一次尝试，验收测试会检查这一点（第 14 节）。
- **`advisor_advice.attempt_id`：** 检查点和 [DECISION] 里的请教把对应尝试的 id 填进去。外键用 `NOT VALID` 加上，再由 `_ONLINE_STEPS` 验证（Advisor RFC D6 里约定的那一步）。

### 6.2 `task_outcomes`

```sql
-- task outcomes begin
CREATE TABLE IF NOT EXISTS task_outcomes (
    id             BIGSERIAL PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_no         INTEGER NOT NULL CHECK (run_no >= 1),   -- 第几次运行这个任务（重启加一）
    outcome        TEXT NOT NULL CHECK (outcome IN ('completed', 'failed', 'cancelled', 'stopped')),
    stop_cause     TEXT CHECK (stop_cause IN ('user_stop', 'user_cancel', 'manager_cancel', 'restart', 'planner_end',
                                             'task_budget', 'error')),
    failure_kind   TEXT CHECK (failure_kind IN ('task_budget', 'planner_failed', 'steps_failed', 'error')),
    started_at     TIMESTAMPTZ,                            -- 这次运行的第一个 Planner 运行
    ended_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    human_wait_ms  BIGINT NOT NULL DEFAULT 0,
    steps_accepted INTEGER NOT NULL DEFAULT 0,
    steps_failed   INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    first_pass_steps INTEGER NOT NULL DEFAULT 0,
    provider_priced_usd DOUBLE PRECISION,
    equivalent_usd DOUBLE PRECISION,
    unpriced_runs  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (task_id, run_no)
);
CREATE INDEX IF NOT EXISTS idx_task_outcomes_ended ON task_outcomes (ended_at);
-- task outcomes end
```

- **写入点：** 任务每次进入终态时写一行，就在 `update_task_status` 的这些调用方旁边：Planner 给出最终状态、用户或管家取消、用户停止、服务重启时被收尾。`stop_cause` 由调用方按自己的路径传入类型化的值；不再从 `stopped` 这个状态去猜。
- **花费快照：** 写入时按 `task_costs` 的口径决定哪些运行算任务的花费，按 `spend_since` 分成两种计费，只计这个任务里 `created_at` 落在这次运行时间段内的运行。之后才结束的运行不改快照，接口会另外报告「快照之后的花费」。
- **重启：** 重启任务时 `run_no + 1`，以前的行保留。任务成功率按每个任务最后一次运行的结局算；重启了几次另外统计。

### 6.3 失败分类

| `failure_kind` | 映射自（只看类型化的值） |
|---|---|
| `review_rejected` | `review_outcome = not_passed`，决定是 `retry`、`replan` 或 `fail` |
| `contract` | `RunOutcome.CONTRACT` |
| `files_mismatch` | `RunOutcome.FILES_MISMATCH` |
| `reported_failed` | `RunOutcome.REPORTED_FAILED` |
| `run_error` | `RunOutcome.RUN_FAILED`，并且下面三条都不成立 |
| `timeout` | 运行因为角色的超时被结束；超时由 Looper 发起，结束时由代码打上类型化的标记，不读错误文字 |
| `run_cap` | 运行的结果事件 subtype 是 `error_max_budget_usd`，或者 Looper 的软上限（`cap_stop`）结束了运行 |
| `crash` | 运行在拿到结果之前就退出了（没有结果事件，退出码不为 0） |
| `task_budget` | 任务预算用完时还开着的尝试 |
| `check_failed` | 留给 M2：可执行检查没通过 |
| `stopped` | 任务被停止或取消时还开着的尝试 |

- **实现：** Worker 现在已经在代码里区分了超时、软上限和没有结果这几种情况（`worker.py` 里结束运行的分支），把这些分支各自带一个类型化的 `RunFailure` 值传到收尾就行，不新增判断。
- **`RunOutcome` 不改。** `run_failed` 仍然是运行记录 `metadata.outcome` 的值，拆分只体现在 `step_attempts`。这样已有读 `metadata.outcome` 的地方（`role_runs`、角色统计）都不用动。

### 6.4 其他写入

- **`llm_records.attempt_id`：** 可空的 `BIGINT`。迁移只做 `ADD COLUMN`，不带默认值；外键（`ON DELETE SET NULL`）用 `NOT VALID` 加上，部分索引 `WHERE attempt_id IS NOT NULL`，两者都放进 `_ONLINE_STEPS`，在提交之后验证、并发建索引。写法照 `_RUN_TREE`。
- **Planner 违反契约的计数：** Planner 的回复解析器拒绝一次回复时，在这一轮的运行记录 `metadata.contract_rejections` 里加一，带类型化的原因（解析器已有的拒绝种类）。评测改为读这个值，不再数日志里的 `Planner reply broke the … contract`。
- **`task_feedback`：** 操作者在已完成的任务上标「交付有问题」时写入，一个任务最多一条，可以改，也可以撤销。

```sql
-- task feedback begin
CREATE TABLE IF NOT EXISTS task_feedback (
    task_id    TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    verdict    TEXT NOT NULL CHECK (verdict IN ('defect', 'incomplete', 'wrong_approach', 'other')),
    note       TEXT CHECK (char_length(note) <= 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- task feedback end
```

  `note` 只在控制台显示，不进任何 agent 的上下文。

### 6.5 迁移

| 变化 | 迁移 | 在线安全 |
|---|---|---|
| 新表 `step_attempts`、`task_outcomes`、`task_feedback` | 各自一个命名迁移，在 `schema.sql` 里用块标记标出，照 `_LEDGER` 的写法 | 都是新表 |
| `llm_records.attempt_id` | 命名迁移 `attempt_ids`：只加列；外键 `NOT VALID`；`_ONLINE_STEPS` 验证外键、并发建部分索引 | 大表：不扫描、不重写 |
| `advisor_advice.attempt_id` 的外键 | 同一个命名迁移，外键 `NOT VALID`，`_ONLINE_STEPS` 验证 | 表不大，但仍照同样的写法 |

**历史数据：** 不回填。指标只覆盖迁移之后开始的尝试和任务；接口报告覆盖的起始时间。历史任务仍可以从 `task_costs` 看花费。

## 7. 读取：接口、任务页和首页

- **v1 只读操作**（`operator_only`；`loopercli` 由目录生成）：
  - `metrics.task_outcomes`：输入 `{days, project?, profile?, difficulty?}`，输出第 5.1–5.4 节的各项指标。每项都带 `n`、数值或 `insufficient`、Wilson 区间、对照窗口的数值，另外给 `coverage_since` 和 `missing_attempts`。
  - `metrics.step_attempts`：输入 `{task_id}`，输出这个任务每一步的每次尝试（第 6.1 节的列，加上沿运行树算出的花费，分首次和重试、分 Worker / 评审 / advisor），以及整个任务的规划花费。
- **任务页的「指标」卡片**（`web/src/pages/task-detail.tsx`）：一次通过的步骤数和总步骤数、尝试次数，花费拆分（首次和重试；规划、执行、评审），失败原因。还没结束的任务显示「进行中」，数字随运行更新。
- **首页的「任务质量」一组**（和「管家」一组并列）：最近 7 天对照前 28 天的任务成功率、一次通过率、每个被接受步骤的尝试次数、评审捕获率、每个被接受步骤和每个完成任务的花费（两种计费分开）、agent 时长的 P50 和 P95、漏检数。可以按项目、Profile 切换。
- **「交付有问题」按钮：** 已完成任务的页面上，只有操作者能看到；点开选原因，写 `task_feedback`。

## 8. 线上监控与告警

**开关：** `metrics.alerts.enabled`，默认关；用 v1 操作 `metrics_alerts.get` 和 `metrics_alerts.update` 读写，只限操作者。

每天检查一次（和 Advisor 告警同一种调度），比较最近 7 天和前 28 天。样本不够（n < 20）时不判。

| 规则 | 条件 | 级别 |
|---|---|---|
| 任务成功率下降 | 7 天的 Wilson 上限低于 28 天的点估计，并且绝对值下降不少于 10 个百分点 | 提醒 |
| 一次通过率下降 | 同上，阈值 10 个百分点 | 提醒 |
| 每个被接受步骤的花费上升 | 7 天的中位数比 28 天高 50% 以上（两种计费分别判断） | 提醒 |
| 漏检增加 | 7 天的漏检数不少于 3，而且高于 28 天的周均值的 2 倍 | 提醒 |
| 尝试缺失 | `missing_attempts` 大于 0：写入路径坏了 | 立刻提醒（只此一条，不受每天一条的限制） |

- **收敛：** 同一条规则触发以后 7 天内不再发，除非情况更差（又跨过一个阈值档）。恢复的时候发一条恢复消息。
- **送达：** 走现有的 `ChannelRegistry.notify`，按 Advisor 告警的目标设置发送。第 2 期的送达层（P2.6）打开以后，告警是它的一种主动消息，受免打扰时段和每天上限约束。
- **日志：** 每次检查记一行结构化日志：规则、两个窗口的数值、样本数、结论。

## 9. 评测

### 9.1 分层

| 层 | 内容 | 什么时候跑 | 花费 |
|---|---|---|---|
| L0 单元与契约测试 | pytest：解析器、映射、指标的计算函数 | 每个 PR，CI | 0 |
| L1 假模式流程评测 | `prompt_regression --mode fake`、对话的假模式、助理体验评测的假模式 | 改动流程代码时 | 0 |
| L2 真实模式回归 | 改动涉及的任务和对话（开发计划第 1 节第 5 条的档位） | 改动模型可见内容的 PR | 小 |
| L3 全量档位 | 所有套件、所有模型，`--reps 3` | 每次发布前 | 中 |
| 难题集 | 第 9.4 节的 3 个用例，以后 M2、M3 的对照用例也放在这里 | L3；以及 M2、M3 的 PR | 中 |

套件有 `tasks`、`hard`、`dialogs`、`assistant`、`processor`、`lark_group`。每个套件的用例有稳定的 id，结果 JSON 都带 schema 版本。

### 9.2 每个用例输出什么

在现有输出（outcome、error_class、retries、reviews、花费、token）的基础上，任务类用例从 `metrics.step_attempts` 读出：
- 尝试次数和一次通过的步骤数；
- 花费拆分（首次和重试；规划、执行、评审、advisor）；
- 失败原因分布；
- 违反契约的次数，从 `metadata.contract_rejections` 和 `outcome = contract` 读，不再数日志。

对比表多出几列：一次通过率、每个通过用例的平均尝试次数、每个通过用例的平均花费。

### 9.3 基线规则

- **一套件、一模型、一个文件：** `evals/prompt_regression/baselines/<suite>-real-<model>.json`，沿用现有的命名（`real-<model>.json` 就是 `tasks` 套件）。文件里记录 commit、日期、重复次数、运行时版本（例如 `claude --version`）和每一行的完整结果。
- **刷新：** 发布前的全量档位跑完后刷新（开发计划第 1 节）；新增用例时，只给它补一行基线，不重跑整个套件；用例的通过条件改了，就作废这一行基线并重新跑。
- **对比的结论（标出来，由人判断）：**
  - 套件总通过率的 Wilson 区间和基线的不重叠，标「回退」；
  - 某个用例在两边都至少重复 2 次、从全过变成有失败，标「用例回退」；
  - 每个通过用例的平均花费上升 25% 以上，或平均尝试次数多出 0.5 次以上，标「代价回退」；
  - 错误（error）单独列，不算失败，也不算通过。
- **从不取最好的一次：** 重跑是一次新运行，结果照实记录（现有规则）。
- **报告附在 PR 上：** 改动模型可见内容的 PR 附上对比表；有「回退」标记时写明是接受（说明理由），还是修改后再跑。

### 9.4 难题集（3 个用例）

产品设计要求的「第一次大概率失败」的用例，作为 M2（可执行验收、重试交接包）和 M3（升级阶梯、advisor）的对照。三个用例都在合成仓库里，不需要网络。

| id | 情形 | 通过条件 |
|---|---|---|
| `hidden_precondition` | 需求是加一个功能，测试只有在一个没写进需求、但仓库里能找到的前提下才能通过（例如测试脚本要求某个环境变量，说明写在 `Makefile` 的注释里） | 任务完成，测试通过；记录这一步的尝试次数 |
| `rethink_bug` | 修一个 bug：最直接的修法能让报错的那条测试通过，但会让另一条已有的测试失败；正确的修法要改另一个模块里的计算方式 | 任务完成，两条测试都通过，而且没有删改已有的测试 |
| `misleading_error` | 报错信息指向一个文件，真正的原因在另一个文件的配置里；照报错改那个文件修不好 | 任务完成，改动落在正确的文件上，测试通过 |

- 这三个用例主要看的是尝试次数、花费和最终是否通过，不要求第一次就通过。
- 进 L3 档位，以及 M2、M3 的 PR。第一次的真实基线在本文第 ③ 个 PR 合入后跑，预算另报。

## 10. 关闭时不变、模型可见性

- **没有新开关的部分（写尝试和结局记录）：** 只加数据，不改行为。验收测试检查同一个任务在有这些写入和没有时，派出的命令行、提示词、工具列表完全一样（比对哈希）。
- **告警：** 默认关；关闭时不做检查，也不发任何消息。
- **「交付有问题」：** 只在控制台出现，不进任何 agent 的上下文。
- **模型可见：** 没有。第 ③ 个 PR 只改评测。难题用例只在评测里用。

## 11. 失败语义

| 地方 | 出错时 |
|---|---|
| 写 `step_attempts`、`task_outcomes`、`attempt_id` | 只记日志；任务照常进行；接口报告缺失的尝试数；告警规则「尝试缺失」会提醒 |
| 沿运行树算花费 | 运行树断了（父运行被删掉）时，那部分花费归到任务级的「未归属」，不丢 |
| 指标接口 | 某一项算不出来时，这一项返回 `error`，其他项照常返回 |
| 告警检查 | 出错只记日志，第二天再检查 |

## 12. 风险

- **尝试的边界：** 一次尝试里可能有多轮评审（面板评审、角色评审），还可能有 advisor 请教；所以尝试按 Planner 的决定来切，不按运行来切。面板评审的每个席位各算一次 `review_runs`。
- **口径变化：** 「被接受的改动」定义成被接受的步骤以后，步骤拆得越细，每个步骤的花费就越低。所以首页总是把「每个完成任务的花费」并列展示。
- **小样本：** 个人使用的量很小，比率类指标经常样本不足，D11 会让很多格子显示「样本不足」。这是故意的。
- **大表：** `llm_records` 只加一列，外键和索引在线做。沿运行树的花费汇总只在单个任务范围内做递归查询，首页的数字从 `task_outcomes` 和 `step_attempts` 的快照算，不在大表上做全表递归。
- **评测花费：** 第 9.2 节的新列都是读已有数据，不增加评测花费。难题集每次 L3 多出 3 个用例乘以重复次数，预算在第 ③ 个 PR 里报。

## 13. PR 拆分

| PR | 内容 | 依赖 | 默认关闭时 | 模型可见 | 验收 |
|---|---|---|---|---|---|
| ① | `step_attempts`、`task_outcomes`、`llm_records.attempt_id`、`advisor_advice.attempt_id` 外键、迁移；派发、收尾、决定、任务终态这几处写入；`RunFailure` 类型化；Planner 违反契约计数写进运行记录 | — | 不涉及开关；派出的命令行和提示词逐字节不变 | 否 | 第 14 节第 1、2、3 条 |
| ② | `metrics.task_outcomes`、`metrics.step_attempts`；任务页「指标」卡片；首页「任务质量」一组；`task_feedback` 和「交付有问题」按钮 | ① | — | 否 | 第 14 节第 4 条；截图 |
| ③ | 评测从 `metrics.step_attempts` 读尝试、花费拆分和违反契约次数；对比表的新列和回退标记；基线规则写进 `evals/README.md`；难题集 3 个用例（假模式通过） | ① | — | 否（只改评测） | 第 14 节第 5 条 |
| ④ | 回退告警：`metrics.alerts.enabled`、每天检查、规则、收敛、恢复消息 | ② | 不检查、不发消息 | 否 | 第 14 节第 6 条 |

- **并行：** ① 合入以后，② 和 ③ 可以同时做；④ 在 ② 之后。
- **冲突：** ① 动 `planner.py` 和 `worker.py` 的派发、收尾和决定几处，按「`manager.py` 接线 PR 同时不超过 2 个」那条规则排队（助理层 RFC 第 13 节），也和 M2 的 PR 错开。新逻辑放进新模块（`looper/agents/attempts.py`），planner 和 worker 里只做接线。

## 14. 验收（出口标准）

1. 在一个多步骤、有重试、有评审的任务跑完后：每个 Worker 运行都对应 `step_attempts` 里的一行，尝试编号和 `task_steps.retries` 对得上；每个评审运行和决定回合都带着对应尝试的 `attempt_id`。
2. 同一个任务按尝试汇总的花费，加上规划花费和「未归属」的花费，等于 `task_costs` 的总数（两种计费分别相等）。
3. 任务停止、取消、预算用完、重启以后再完成：`task_outcomes` 里每次运行都有一行，`stop_cause` 正确；重启没有删掉以前的行。
4. 指标接口在合成数据上算出第 5 节的每一项，样本不足时返回 `insufficient`；任务页卡片和首页一组有截图（1280、390）。
5. 评测在假模式下输出新的几列；同一组用例连跑两次，表的结构稳定；难题集 3 个用例在假模式下能跑通，也能失败（检查函数确实能区分对错）。
6. 告警打开后：注入一组合成数据，跨过阈值时发一条，恢复时发一条，7 天内同一条规则不重复发；关闭时不发。

## 15. 待定问题（各有推荐的默认做法）

| 编号 | 问题 | 选项 | 推荐 |
|---|---|---|---|
| Q1 | 「被接受的改动」用什么单位 | (a) 被接受的工作步骤；(b) 完成的任务；(c) 合进主干的提交 | **(a)，同时并列展示 (b)。** (c) 是最终价值，但 Looper 看不到合并（Q3） |
| Q2 | 重新打开要不要算新尝试 | (a) 算，带触发原因；(b) 不算，记在原尝试上 | **(a)。** 漏检因此有了统一的计数方式 |
| Q3 | 要不要追踪任务分支被合进主干 | (a) 不追踪；(b) 记下任务分支名，定期查本地仓库主干是否包含 `result_commit` | **(a)，先不做。** (b) 只对本地仓库有效，而且「没合」的原因很多。等「交付有问题」有了数据再说 |
| Q4 | 历史数据要不要回填 | (a) 不回填；(b) 从日志条目和运行记录尽量重建 | **(a)。** 重建要读文字，违反 D1。接口写明覆盖的起始时间 |
| Q5 | 样本门槛 | 20 / 10 / 不设 | **20**，附 Wilson 区间。量小的时候宁可显示「样本不足」 |
| Q6 | 回退告警的比较窗口 | 7 天对 28 天 / 1 天对 7 天 | **7 天对 28 天。** 个人使用的量，1 天的样本太少 |
| Q7 | 「交付有问题」要不要让管家也能记 | (a) 只限操作者在控制台和 CLI；(b) 也给管家一个工具 | **(a)。** (b) 会改模型可见内容，而且转述不能代替你本人的判断（账本 RFC 的同一条原则） |
| Q8 | 评测对比要不要自动拦合并 | (a) 只标出来，由人判断；(b) Wilson 区间不重叠时自动失败 | **(a)。** 样本小，真实模型的方差大，自动拦截会被噪声误伤 |
| Q9 | 难题集的真实基线什么时候跑 | (a) ③ 合入后马上跑；(b) 等 M2 开工前再跑 | **(a)。** M2 需要一个「改动之前」的数字，越早有越好；预算另报 |
| Q10 | Planner 做规划的花费要不要分摊到步骤 | (a) 不分摊，算在任务一级；(b) 按步骤数平均分摊 | **(a)。** 分摊方式是主观的；任务一级的「每个完成任务的花费」已经包括了规划 |
