# RFC：决策快照（v0）

- **状态：** 提案，第一步（只记录）随本 RFC 一起实现，默认关闭。
- **关联：** 任务引擎加固（失败分型、全局准入和公平队列、持久化运行树）都会改「下一步做什么」的判定；本 RFC 先把判定的输入和结果记下来，让这些改动上线前能拿历史任务离线重放、比对。
- **最近更新：** 2026-11-20

## 1. 背景与目标

一个任务往下走，每一步都是一次决策，分两类：

- **代码做的决策：** 每次派发时「现在哪些步骤能动」。`TaskPlan.ready_steps` 按 `depends_on` 和 `ready_when` 算出就绪的步骤，`_dispatch_ready_impl` 按种类分开（开关卡、开等待、派评审席位、派 Worker），开了步骤工作区的项目再由 `step_workspaces.plan_round` 决定这一轮有几个工作步骤能开始。这几个函数本身已经是纯函数。
- **模型做的决策：** Worker 或 Reviewer 结果回来后，Planner 的 `[DECISION]` 回合选 `accept`、`review`、`retry`、`replan` 或 `fail`。可选范围由代码先算好（`DecisionContext.allowed()`），模型在其中选一个，并给出理由。

现在留下来的只有结果：活动流里一行「Planner decision on step …: retry: …」，`llm_records` 里有这一回合的输入输出和元数据。没有记下决策当时看到的输入，所以：

- 改了派发逻辑（比如失败分型、公平队列），没法拿历史任务验证「同样的输入，新代码是否做出同样的决定，不同在哪」；
- 一个任务卡住时，看不到当时每一步在什么状态、为什么没被派发；
- Planner 的某次决定是在什么 prompt（各层的哈希）、什么工具、什么模型、多大上下文下做的，要翻原始运行记录拼。

**目标：**

- 每次决策记一条快照：决策读到的输入、做出的决定；
- 代码做的决策能离线重放：用快照里的输入重跑同一组纯函数，和记录的决定逐项比对；
- 模型做的决策记下代码给它的约束（允许哪些决定）和它看到了什么（回合的运行记录摘要），重放时复算约束；
- 第一步只记录、不改行为；默认关闭，关闭时 prompt、工具列表、命令行和行为逐字节不变。

**不做（v0）：** 不改任何决策；不重跑模型回合；不记 Manager 的回合（见 4.3）；不做控制台页面；Worker 和 Reviewer 用哪个 Agent Profile 的选择不在其中。

## 2. 方案对比

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A 只扩充活动流 | 在现有活动行里多写几个字段 | 不加表 | 活动行是给人看的文本，结构不稳定，不能重放；会把更多内容推到 SSE 和控制台 |
| B 快照表 + 纯函数重放（推荐） | 新表 `decision_snapshots`，每次决策一行：输入、决定；代码决策用同一组纯函数重放 | 结构化、可重放；重放直接调用生产代码，不另写一份判定逻辑；只读 Planner 手里已有的状态 | 多一张表和一次写入；输入漏记一项时重放会对不上（这正是要发现的） |
| C 事件溯源 | 记下 Planner 收到的每个事件和全部状态，重放整个 Planner | 能重放一切 | 要重放模型回合、git 和数据库读写，成本和复杂度远超需要 |

**推荐 B。** 第一步只覆盖任务引擎里最核心的两类决策（派发、步骤决定），其余后续按需加。

## 3. 开关

系统设置 `decision_snapshots.enabled`，值为 `true` 时开，没有这一行或其他值都是关（和 `ledger.enabled` 一样，控制台暂不提供开关）：

```sql
INSERT INTO system_settings (key, value) VALUES ('decision_snapshots.enabled', 'true')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
```

- 每次派发前和每次应用步骤决定前读一次（主键查询），改了立刻生效，不用重启；读失败按关处理并记一条警告。
- 关闭时一行都不写。派发前多出的一次读在拿项目派发锁之前，派发过程中不多任何等待。
- 开启时模型可见的内容也不变：快照只读 Planner 已有的状态和运行记录，从不回流到 prompt、工具或命令行；写失败只记警告，决策照常进行。

## 4. 快照记什么

### 4.1 派发（`dispatch`）

每次 `_dispatch_ready_impl` 一行，在工作步骤开始之前写入（开关卡、等待和评审席位已经按决定打开）。

**输入（`inputs`）：**

- `isolate_steps`：项目是否开了步骤工作区（它改变 `completed` 叶子的判定）；
- `steps`：每个步骤里就绪判定读到的字段，且只有这些：`id`、`key`、`kind`（`work`、`gate`、`wait`、`review`）、`status`、`depends_on`、`ready_when`、`reviews`（评审席位评的步骤）、`result` 里的 `status` 和 `passed`、关卡的 `state`、等待的 `state` 和 `outcome`；
- `round`（只在这一轮做了工作区分轮时）：`in_flight`（在途步骤各自是否在自己的 worktree 里）、`ready`（就绪的工作步骤各自是否已有 worktree）、`repo`（`repo`、`unusable`、`outside_git`），即 `plan_round` 的全部输入，按它被调用时的样子记（开关卡和评审之后）。

**决定（`decision`）：** `ready`（就绪步骤按顺序，各带种类）、`round`（`start`、`isolate`）、`start`（这一轮要开始的工作步骤）。之后 `_dispatch_step` 因花费上限、角色读不到或 worktree 建不起来没开始的，是派发的结果，不是这次决定，照旧记在活动流里。

**重放：** 用 `steps` 构造只含这些字段的步骤投影，调用 `TaskPlan.ready_steps`；有 `round` 时调用 `step_workspaces.plan_round`；得到的 `ready`、`round`、`start` 和记录逐项相同才算一致。重放调用的就是生产代码，不复制判定逻辑。

**去重：** 同一任务里，输入和决定都与上一条派发快照相同的，不再写。

### 4.2 步骤决定（`step_decision`）

Planner 的决定被应用时（`_apply_decision` 开头）一行，带 `step_id` 和该回合运行记录的 `record_id`。覆盖 `accept`、`review`、`retry`、`replan`、`fail`；`advise` 和关键步骤开工前的 `proceed`（顾问流程）不在 v0。

**输入：**

- `step`、`key`、`attempt`（这一步的第几次尝试）；
- `context`：代码为这一回合算出的 `DecisionContext` 全部字段（结果种类、是否成功、是否必须评审、重试次数和上限、是否最后一个未完成步骤、是否升级、是否允许请教顾问等）；
- `turn`：这一回合运行记录的摘要，从 `llm_records` 读：Agent Profile 的 id 和运行时类型、模型和 effort、`system_prompt_sha256`、`prompt_layers` 每层只留其中的 sha256 字段、命令行里的工具参数（`--tools`、`--allowedTools`、`--disallowedTools` 的值，不用这些参数的运行时为空）、只读方式、已装能力的 `library` 哈希，以及大小：回合消息的字符数、`input_tokens`、`context_tokens`、`cache_read_tokens`。

**决定：** `allowed`（代码允许的决定，按顺序）、`decision`、`reason`（`retry` 时是给 Worker 的反馈；截到 500 字符，与活动行保留的相同）、对关卡和等待的改动（id 和动作）、`replan` 的新步骤 id。

**重放：** 用 `context` 重建 `DecisionContext`，复算 `allowed()`，与记录相同、且记录的决定在其中，才算一致。模型的选择不重跑；要比较不同 prompt 或模型下的选择，评测可以按 `record_id` 取回当时的输入，这不在 v0。

### 4.3 Manager

v0 不记。Manager 回合里的决定是一次次工具调用，已经一行一条记在 `manager_actions`（带回合的渠道和消息），没有一个由代码判定、可以纯函数重放的「下一步」；要记它看到的 prompt 层和工具，可以照 4.2 的 `turn` 摘要加一类 `manager_turn`，等有具体的评测需求再加。

## 5. 存储

```sql
CREATE TABLE decision_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('dispatch', 'step_decision')),
    step_id       TEXT,           -- step_decision 必填，dispatch 为空
    record_id     BIGINT,         -- 该回合的 llm_records 行；不建外键
    inputs        JSONB NOT NULL, -- 对象
    inputs_sha256 TEXT NOT NULL,
    decision      JSONB NOT NULL, -- 对象
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- (task_id, kind, id) 和 (created_at) 上各一个索引
```

- 迁移名 `decision_snapshots`：只新建一张空表，不改已有的表，不扫描、不重写。`record_id` 不建外键，免得迁移时锁住大表 `llm_records`，快照里已经带了回合的摘要，记录不在了也能看。
- `inputs` 带 `version`（v0 为 1）；格式变了就升版本，旧版本的快照重放时标为「格式不同」，不报错。
- 只在本机 PostgreSQL 里，不是权威数据。

## 6. 保留

- 随任务删除（外键级联）；任务重新规划（start over）后旧快照保留，作为那一次的历史。
- 每小时的 housekeeping 按批删除 30 天前的快照，删除数进它的日志行（`decision_snapshots_deleted`）。
- 量级估计：一个 10 步的任务每次派发约 2–3 KB，整个任务几十 KB；去重之后更少。

## 7. 隐私

- 不存消息原文：没有需求、步骤说明、用户消息、Worker 的输出、系统 prompt 文本。步骤只存 id、标签、状态和结构化字段；prompt 只存哈希；Agent Profile 只存 id 和运行时类型（不存自定义 prompt）。
- 唯一的自由文本是决定的 `reason`，截到 500 字符，与活动行一致；它本来就在该回合运行记录的输出里。
- 快照只在本机数据库里，不进 prompt，不发到任何渠道，不进控制台的实时流。

## 8. 第一步（本 PR 实现）

- `looper/agents/tiers/decisions.py`：采集（派发、步骤决定）、回合摘要和重放，全部是纯函数，外加两个写入入口（先读开关；任何异常只记警告）；
- `looper/db/decision_snapshots.py`：开关、写入（含派发去重）、按任务读取、读回合记录、按批清理；
- `planner.py` 只加接线：`_dispatch_ready` 读开关，`_dispatch_ready_impl` 采集并在工作步骤开始前写入，`_plan_round` 交出它给 `plan_round` 的输入和结果，`_apply_decision` 开头写步骤决定；
- 迁移 `decision_snapshots`；housekeeping 清理 30 天前的快照；
- 命令 `looper decisions replay <task_id> [--json]`：直接读数据库（服务停着也能用），逐条重放并比对。退出码：0 全部一致，1 没有快照，3 有不一致，6 数据库不可用。

入口：控制台、Telegram 和飞书不变（只记录，没有要展示或操作的东西）；CLI 新增上面这条只读命令。

## 9. 后续

- 引擎加固的各项（失败分型、公平队列、持久化运行树）改派发或决定之前，先用历史快照重放，比对新旧决定的差异，作为它们的验收材料之一；
- 记录证明完整之后（重放长期一致），把派发改成由一个以快照输入为参数的纯函数直接给出决定，代码按它执行；
- 需要时加 Manager 回合（4.3）、评测里按 `record_id` 重跑模型决策、控制台的任务页展示。

## 10. 风险与监控

| 风险 | 应对 |
|---|---|
| 漏记一项输入，重放对不上 | 重放就是检测手段：`looper decisions replay` 退出码 3 并列出差异；测试覆盖关卡、等待、评审席位、条件依赖和工作区分轮 |
| 写入拖慢派发 | 每次派发一条插入（含去重判断），在项目派发锁内约 1 ms 量级；关闭时没有写入 |
| 表增长 | 去重 + 30 天清理 + 随任务删除 |
| 读开关或写快照失败 | 只记警告（`could not record a decision snapshot`），决策照常；不会因为快照让任务失败 |
| 快照泄露内容 | 见第 7 节；测试断言回合摘要里没有 prompt 文本和自定义 prompt |
