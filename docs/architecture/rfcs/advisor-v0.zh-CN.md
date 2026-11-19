# RFC：Advisor 第一期（只有规划者能请教）v0

- **状态：** 草案。方向由维护者确认（第 4 节的 D1–D14）；第 18 节的开工前验证已在 2026-11-19 实测。本文合入后按第 17 节的 PR 顺序实现。
- **需求：** 规划者遇到难题时，能向一个更强的模型请教一次。请教用代码检查过的抽象请教包：advisor 看不到仓库、任务和本机，只给类型化的建议，采不采纳由规划者决定，结果可以统计。
- **相关：** [Agent 系统架构](../agent-system.md) 的 "A task's agent room"（room consult：只读启动、预算、超时、写入检测、进程树回收）和 "The processor: runs with no tools"（无工具运行）；[规划者只读 RFC](planner-read-only-v0.zh-CN.md)；[规划者定义的角色 RFC](planner-defined-roles-v0.zh-CN.md)；[开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 1 节和第 5 节。
- **核对基线：** `main` `961070ce4`。本文读了代码，并用真实 Claude Code 2.1.328 做了第 18 节的几次小调用。
- **里程碑命名：** 按维护者的设计文档，本 RFC 是其中 M3（升级阶梯与 advisor）的第一期。开发计划第 2、3 节里的 M3 是另一件已交付的事（项目知识），两套编号互不影响。
- **术语：**
  - **请教（consult）：** 规划者向 advisor 提一次问题，包括最多 2 轮回问。一次请教是 `agent_spawns` 的一行。
  - **请教包（AdviceRequest v1）：** 发给 advisor 的全部内容（第 9 节）。
  - **对照表（bindings）：** 请教包里的抽象名和真实引用的对照，只存在本地，不发出去。
  - **判别（advice_check）：** 发出前用便宜模型再判一次：能否独立看懂、有没有泄漏、能否回答。
  - **room consult：** 现有的「向已结束的席位提问」（`looper/agents/consult.py`）。和本文的请教是两件事，只共用启动和限额的做法。

## 1. 背景与目标

现在规划者只能自己判断：步骤失败了，要么带着反馈重试，要么重新规划，要么让任务失败。它看不到现场，会话很长，每次深入诊断都很贵，还会把上下文撑大。设计文档的方案是：增加一个 advisor 角色，绑定任意 Agent Profile（默认 Claude Code + Fable 5.2）；advisor 不带工具，只读规划者写的抽象请教包，按固定格式给出依据要点和建议，规划者决定采纳哪些。

**第一期的目标：**

- 有两个入口：(a) `critical` 步骤的执行者开工前，代码强制请教一次（检查点，类型 `plan_review`）；(b) 执行者或评审失败、允许 retry 的 [DECISION] 里，规划者可以选择请教（可选，受上限约束）；
- 请教包发出前，依次过三道检查：格式、代码、判别。不过就退回规划者重写，最多退回 2 次，之后这次请教记为 `blocked`，步骤不带建议继续；
- advisor 在系统临时目录下的空目录里启动：系统提示整段替换，不带任何工具，每轮回问都起新进程；
- 记录类型化的建议、采纳情况，以及请教后下一次尝试有没有通过（`unlocked`）；花费计入任务；
- 项目开关 `advisor` 默认关闭。关闭时，回复契约、规划者和执行者的每条消息、工具列表、每个 agent 的命令行都和 `main` 逐字节相同。

**第一期不做：**

- **升级阶梯（第 3 级「同一失败签名第二次失败」）：** 要等 M2 的可执行检查产生失败签名。不从评审结论里拼签名。`advisor_advice.source` 已经预留 `ladder`，第一期不写这个值；
- **explorer 子 agent：** 要等 M4 的派生工具。第一期规划者只拿到代码给的本地素材（最多 8,000 字，不外发），见第 11.2 节。素材不够时，规划者只能在请教包里写明缺什么，或者不请教；
- **难度路由（`project_tier_profiles`）：** 第一期 `difficulty` 只用于检查点，不按难度选 Profile；
- **执行者请教、advisor 子 agent、多厂商同时请教、三组对照评测的真跑：** 第 16 节只做评测框架和花费的 dry-run 估算，真跑要先请维护者批准预算。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 |
|---|---|
| 规划者的决定 | `planner_protocol.Decision`：`accept`、`review`、`retry`、`replan`、`fail`。`DecisionContext.allowed()` 按结果算出允许的决定，`PlannerAgent._decision_message` 在消息末尾写 `Allowed decisions now: …`，`parse_decision` 拒收不在其中的决定 |
| 回复不合格 | `PlannerAgent._ask`：`ProtocolError` 时发一次 `[KIND] Your last reply broke the reply contract: …`，要求按同样的格式重答；还不合格就是 `PlannerTurnError`，按执行者或评审者自己的结论处理 |
| 重试 | `task_steps.retries` 是这一步已经重试的次数，第 N 次尝试时等于 N−1。`retry` 的 `feedback` 追加到 `task_steps.feedback`，由新的执行者在输入里读到 |
| room consult | `consult.py`：`Launch(..., consult=True)` 让运行时只读（Claude Code 只留 Read、Glob、Grep），`single_run.run_once` 用一个截止时间管写入提示、读回复和等退出，结束时回收整个进程树；事后按记录的花费检查上限，超了或未知就丢掉回答。限额在项目执行设置里：`room_consult_budget_usd`（默认 0.50）、`room_consult_timeout_seconds`（300）、`room_consults_per_task`（10） |
| 无工具运行 | `processor.run`：`Launch(role=PROCESSOR)`，`Launch.no_tools` 为真，Claude Code 用 `claude_cli.NO_TOOLS_ARGS`（`--permission-mode manual --permission-prompts none --tools "" --disable-slash-commands --no-session-persistence`），加上封闭的隔离参数（`--setting-sources ""`、`disableAllHooks`、`--strict-mcp-config`，不加载上级目录的 `CLAUDE.md` 和 `AGENTS.md`），系统提示整段替换（`--system-prompt-file`） |
| 访问级别 | `Launch` 有一个布尔字段 `consult`，另有两个推导出来的属性：`read_only`（规划者或 consult）、`no_tools`（processor）。几个运行时适配器分别读这三处 |
| 运行记录 | `llm_records` 一行是一次运行，靠 `agent_id` 前缀区分角色（`planner-`、`worker-`、`reviewer-`、`processor:`、`consult-`）；没有父子关系，也没有类型列。任务花费（`task_costs`）按前缀只算规划者、执行者和评审者 |
| 默认 Profile | 角色没有配置 Profile 时，用内置的 `agent-claude-code-opus`（`claude-code/opus`）；订阅登录的模型目录里只有 `opus`、`sonnet`、`haiku` 三个别名 |
| 价格表 | `runtimes/pricing.py` 有 `claude-fable-5-1`，没有 `claude-fable-5-2`；Claude Code 用自己的登录时，花费取运行时自报的数（`CostSource.RUNTIME`），价格表只在按服务商价格计费时用 |

## 3. 概念

| 概念 | 定义 |
|---|---|
| advisor | 一个角色，和执行者、评审者并列。绑定一个 Agent Profile，默认 `claude-code/fable-5-2`。每轮是一次独立的运行：不带工具，在空目录里启动 |
| advice_check | 判别角色，默认 `claude-code/haiku-5-5`。一次独立的无工具运行，只看请教包 |
| 请教的来源 `source` | `checkpoint`（检查点，代码强制）、`optional`（规划者自己选）、`ladder`（升级阶梯，第一期不用） |
| 请教的类型 `kind` | `diagnose`、`design_choice`、`plan_review`、`unblock`。每种类型有自己的必填项（第 9.2 节） |
| 难度 `difficulty` | PLAN 步骤的字段：`routine`、`hard`、`critical`，存在 `task_steps.difficulty`。第一期只有 `critical` 有作用：触发检查点 |
| 采纳 `advice_adoption` | 规划者在拿到回答后的那次决定里写明：哪条建议、采纳到什么程度、用在哪些步骤、理由是什么 |
| 解锁 `unlocked` | 请教之后的那次尝试有没有通过：评审通过（或者不需要评审、被接受）为真 |

## 4. 决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 只有规划者能请教。执行者不能请教，也不能派 advisor 子 agent；执行者卡住时在报告里写清楚，或在 room 里问规划者 | 计划是唯一的事实来源；执行者各自照建议改方向，几个步骤就会互相打架 |
| D2 | 第一期两个入口：检查点（代码强制）和 [DECISION] 里的可选项。升级阶梯等 M2 | 阶梯要靠类型化的失败签名触发，现在没有 |
| D3 | 项目开关 `advisor`，默认 `off`。关闭时模型能看到的内容和命令行逐字节不变，用测试钉住 | 新行为默认关闭（仓库规则） |
| D4 | `difficulty` 进入第一期，只用于检查点：PLAN 步骤字段加 `task_steps.difficulty` 列；只有开关打开时 PLAN 契约才提到它 | 检查点需要知道哪一步是 critical；难度路由留到以后 |
| D5 | 不在代码里写运行时和模型：advisor 和 advice_check 是角色，默认 Profile 是种子数据（`claude-code/fable-5-2` 用完整 ID `claude-fable-5-2`，`claude-code/haiku-5-5` 用 `claude-haiku-5-5`），都能在 Settings → Roles 里通过现有的分层设置修改 | 运行时和模型只能来自配置 |
| D6 | `advisor_advice` 不依赖 M1：存 `task_id`、`step_id` 和尝试编号（由现有的 `task_steps.retries` 算出），另留一个可空的 `attempt_id`，由 M1 填写并加约束 | M1 的 `step_attempts` 还没有 |
| D7 | 每次请教默认 1 美元，每轮的 `--max-budget-usd` 取这次请教剩下的预算；每步最多 2 次；每个任务的次数和总预算与 room consult 的限额一样放在项目执行设置里；超过任何一个上限，就不提供 advise | 预算可控；做法和已有限额一致 |
| D8 | 第一期没有 explorer：代码给规划者最多 8,000 字的本地素材，只给规划者看、不外发。PR 里写明这个缺口 | explorer 需要 M4 的派生工具 |
| D9 | 启动：工作目录是 `tempfile.gettempdir()` 下的空目录 `looper-advisor/<run_id>`，路径里没有用户名，用完删掉；系统提示整段替换为 advisor 合同；不带工具；复用现有的隔离参数；每轮回问都是新进程 | 请求里的 `# Environment` 消息会带上工作目录的绝对路径（第 18 节） |
| D10 | 三道检查按顺序跑：格式 → 代码 → 判别。没过就通过现有的「回复不合格，请重答」路径退回规划者，并写明要改什么；退回 2 次后这次请教记为 `blocked`，步骤不带建议继续。拦截只记类别和位置，不记命中的内容 | 设计文档的方案 B：两类问题都有兜底 |
| D11 | advisor 的回答按 AdviceResponse v1 解析，解析不了或校验不过，这次请教就是 `failed`；不用 `--json-schema` | `--json-schema` 会加一个 `StructuredOutput` 工具，运行也会多一轮（第 18 节） |
| D12 | 拿到回答后的那次决定必须带 `advice_adoption`，否则代码拒收；采纳了建议时，重试（或开工）的执行者在第一轮输入里读到被采纳的建议 | 建议落到任务上由规划者负责，数据要能统计 |
| D13 | `Launch` 的 `consult` 布尔和推导出的 `read_only` 收成一个类型化的访问级别：`full`、`read_only`、`consult`、`advisor` | 仓库规则：用类型化状态，不用散落的布尔 |
| D14 | 新增内部函数 `spawn(kind, parent, brief, inputs, profile, budget)`，建在 consult 和 processor 已有的机制上；第一期只有 advisor 一种 | M4 的 MCP 工具也调用它 |

## 5. 数据模型

迁移都是增量的：只加表和可空的列，不改已有的行。可以推导的就回填，推导不出的留空。

### 5.1 `llm_records` 加两列

```sql
ALTER TABLE llm_records
    ADD COLUMN parent_record_id BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    ADD COLUMN run_kind TEXT CHECK (run_kind IN ('manager', 'planner', 'worker', 'reviewer', 'processor',
                                                 'consult', 'advisor', 'advice_check'));
CREATE INDEX idx_llm_records_parent ON llm_records (parent_record_id) WHERE parent_record_id IS NOT NULL;
```

- **回填：** `run_kind` 按现有的 `agent_id` 前缀回填（`manager` / `manager-`、`planner-`、`worker-`、`reviewer-`、`processor:`、`consult-`），其余留空。`parent_record_id` 推导不出来，旧行全部留空；
- **写入：** `ClaudeRunRecorder` 建记录时，按 `Launch` 写入 `run_kind`（规则和回填一样）。子运行由 `spawn` 传入 `parent_record_id`；
- **任务花费：** `task_costs` 和任务的花费上限把 `run_kind` 为 `advisor`、`advice_check` 的运行算进去（按 `run_kind`，不靠 `agent_id` 前缀），按角色的拆分里多两项。主分支的数据里没有这两类运行，所以已有任务的数字不变。

### 5.2 `agent_spawns`：一次请教一行

```sql
CREATE TABLE agent_spawns (
    id               BIGSERIAL PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id          TEXT REFERENCES task_steps(id) ON DELETE SET NULL,
    parent_record_id BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,  -- 发起请教的规划者那次运行
    child_record_id  BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,  -- 最后一轮 advisor 运行
    kind             TEXT NOT NULL CHECK (kind IN ('advisor')),             -- M4 加 explorer
    brief            JSONB NOT NULL CHECK (jsonb_typeof(brief) = 'object'),  -- 来源、类型、尝试编号、触发原因
    inputs           JSONB NOT NULL CHECK (jsonb_typeof(inputs) = 'object'), -- 当前的请教包和对照表
    budget_usd       NUMERIC(10, 4) NOT NULL CHECK (budget_usd > 0),
    spent_usd        NUMERIC(10, 6) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
    state            TEXT NOT NULL CHECK (state IN ('drafting', 'linting', 'asking', 'needs_info',
                                                    'answered', 'blocked', 'failed', 'over_budget')),
    returns          INTEGER NOT NULL DEFAULT 0 CHECK (returns BETWEEN 0 AND 2),
    rounds           INTEGER NOT NULL DEFAULT 0 CHECK (rounds BETWEEN 0 AND 3),
    result           JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(result) = 'object'),
    detail           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    CONSTRAINT agent_spawns_finished CHECK ((state IN ('answered', 'blocked', 'failed', 'over_budget'))
                                            = (finished_at IS NOT NULL))
);
CREATE INDEX idx_agent_spawns_task ON agent_spawns (task_id, id);
CREATE INDEX idx_agent_spawns_step ON agent_spawns (step_id) WHERE step_id IS NOT NULL;
CREATE INDEX idx_agent_spawns_open ON agent_spawns (task_id) WHERE finished_at IS NULL;
```

**状态和迁移规则**（`db/agent_spawns.py` 里的 `SpawnState` 枚举和一张迁移表；每次更新都带 `expect`，状态不符就不改，返回 None）：

| 从 | 到 | 什么时候 |
|---|---|---|
| （新建） | `drafting` | 代码发出检查点回合；或者规划者在 [DECISION] 里选了 advise（随即进入 `linting`） |
| `drafting` | `linting` | 规划者交来请教包 |
| `drafting` | `failed` | 规划者没交请教包（回合失败、超时、不合格且修复回合也不合格） |
| `linting` | `drafting` | 有一道检查没过，退回规划者（`returns` 加 1） |
| `linting` | `blocked` | 已经退回 2 次，第 3 次仍没过 |
| `linting` | `asking` | 三道检查都过了，开始一轮 advisor 运行（`rounds` 加 1） |
| `linting` / `drafting` | `over_budget` | 剩下的预算不够再跑一轮（判别的花费也计入） |
| `asking` | `answered` | 回答解析通过，状态是 `answered` 或 `out_of_scope` |
| `asking` | `needs_info` | 回答是 `needs_info`，而且还有回问轮次 |
| `asking` | `failed` | 进程起不来、超时、拒答、解析失败；或者第 3 轮仍是 `needs_info` |
| `asking` | `over_budget` | 撞到 `--max-budget-usd`，或者花费超过上限、或者花费未知 |
| `needs_info` | `linting` | 规划者在 [ADVICE] 回合里补充了信息 |
| `needs_info` | `failed` | 规划者撤回请教，或者 [ADVICE] 回合失败 |

- **`result`** 只记类型化的内容：每轮的 `{round, record_id, cost_usd, status, model}`；每次检查的 `{return, stage, category, field, start, end}`（只记类别和位置，不记命中的内容，见 D10）；判别的分数和缺项；失败原因（`failure`：`refused`、`timeout`、`parse`、`runtime`、`rounds_exhausted`、`withdrawn`、`no_packet`）；
- **重启：** 服务启动时，`asking` 和 `linting` 的行改成 `failed`（`detail` 写 `interrupted by a restart`），因为那次运行已经没有了；`drafting` 和 `needs_info` 的行由规划者的恢复流程接着处理：恢复时如果没有可用的回合，就记为 `failed`，步骤不带建议继续。

### 5.3 `advisor_advice`：建议和采纳

```sql
CREATE TABLE advisor_advice (
    spawn_id     BIGINT PRIMARY KEY REFERENCES agent_spawns(id) ON DELETE CASCADE,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id      TEXT REFERENCES task_steps(id) ON DELETE SET NULL,
    attempt      INTEGER NOT NULL CHECK (attempt >= 1),   -- 建议是为这一步的第几次尝试准备的
    attempt_id   BIGINT,                                  -- M1 的 step_attempts 建好后再填写并加外键
    source       TEXT NOT NULL CHECK (source IN ('ladder', 'checkpoint', 'optional')),
    kind         TEXT NOT NULL CHECK (kind IN ('diagnose', 'design_choice', 'plan_review', 'unblock')),
    question     JSONB NOT NULL CHECK (jsonb_typeof(question) = 'object'),  -- 最后发出的请教包，含 prior_rounds
    advice       JSONB CHECK (advice IS NULL OR jsonb_typeof(advice) = 'object'),  -- 解析后的 AdviceResponse
    adoption     TEXT CHECK (adoption IN ('adopted', 'partial', 'rejected')),
    adoption_detail JSONB CHECK (adoption_detail IS NULL OR jsonb_typeof(adoption_detail) = 'object'),
    unlocked     BOOLEAN,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    adopted_at   TIMESTAMPTZ,
    unlocked_at  TIMESTAMPTZ,
    CONSTRAINT advisor_advice_adoption CHECK (adoption IS NULL OR advice IS NOT NULL),
    CONSTRAINT advisor_advice_unlocked CHECK ((unlocked IS NULL) = (unlocked_at IS NULL))
);
CREATE INDEX idx_advisor_advice_task ON advisor_advice (task_id);
CREATE INDEX idx_advisor_advice_step ON advisor_advice (step_id, attempt) WHERE step_id IS NOT NULL;
```

- **尝试编号：** 检查点的建议是为第 `retries + 1` 次尝试准备的（这一步还没跑过时就是 1）；可选请教发生在第 `retries + 1` 次尝试失败之后，建议为的是第 `retries + 2` 次；
- **`unlocked`** 在第 `attempt` 次尝试有结论时回填：评审通过为真，评审没过为假；不需要评审的步骤，被接受为真，否则为假。这一步被重新规划掉、被跳过，或者任务先结束了，就一直留空。

### 5.4 `task_steps.difficulty` 和项目执行设置

```sql
ALTER TABLE task_steps ADD COLUMN difficulty TEXT CHECK (difficulty IN ('routine', 'hard', 'critical'));
```

项目执行设置（`projects` 的列，`project_execution.py` 负责类型和校验，和 room 的限额放在一起）：

| 字段 | 范围 | 不设时 | 说明 |
|---|---|---|---|
| `advisor` | `on` / `off` | `off` | 总开关 |
| `advisor_consult_budget_usd` | 0.01–5 | 1.00 | 一次请教（包括回问和判别）最多花多少 |
| `advisor_consults_per_step` | 0–10 | 2 | 一步最多请教几次（检查点和可选的都算） |
| `advisor_consults_per_task` | 0–100 | 10 | 一个任务最多请教几次 |
| `advisor_budget_usd` | 0.01–50 | 5.00 | 一个任务的请教总共最多花多少 |
| `advisor_timeout_seconds` | 30–1,800 | 300 | advisor 每轮最多跑多久 |

`task_steps.difficulty` 在 PR1 随其他迁移加上；项目执行设置的列在 PR3 和功能一起加。

## 6. 访问级别

`agent_settings.Access`：

| 值 | 谁 | 运行时怎么做 |
|---|---|---|
| `full` | 执行者、评审者、管家（默认） | 全部工具（`permission_args`） |
| `read_only` | 规划者 | 现有的只读参数（`read_only_args`） |
| `consult` | room consult | 只有读取工具（`consult_args`），系统提示追加 |
| `advisor` | advisor、advice_check | 不带工具（`no_tools` 为真，用 `NO_TOOLS_ARGS` 和封闭隔离），系统提示整段替换 |

- `Launch.access: Access | None = None`：留空时取角色的默认值（规划者是 `read_only`，其余是 `full`）；`Launch.access_level` 是解析后的值；
- `read_only`、`consult`、`no_tools` 保留成只读属性，都由 `access_level` 推出（`no_tools` 另外对 processor 角色为真），所以各运行时适配器的读法不变。`Launch(..., consult=True)` 改写成 `Launch(..., access=Access.CONSULT)`；
- **不变的保证：** PR1 对每个角色、每个运行时适配器生成一遍命令行，和 `main` 上算出的 sha256 对比（测试里写死），一字不差。

## 7. 子运行原语 `spawn`

`looper/agents/spawn.py`：

```python
class SpawnKind(str, Enum):
    ADVISOR = "advisor"

@dataclass(frozen=True)
class SpawnParent:
    task_id: str
    step_id: str | None
    record_id: int | None         # 规划者那次运行的 llm_records.id

async def spawn(kind: SpawnKind, parent: SpawnParent, brief: Mapping[str, Any], inputs: Mapping[str, Any],
                profile: AgentSettings, budget: float, *, child: ChildRun) -> SpawnOutcome: ...
async def run_child(child: ChildRun, *, parent: SpawnParent, settings: AgentSettings, cap_usd: float) -> ChildResult: ...
```

- **`run_child`** 跑一个新进程，跑完就结束。它把 consult 和 processor 现有的机制合在一处：`Launch(access=Access.ADVISOR)`；一个私有的空目录（advisor 的放在 `tempfile.gettempdir()/looper-advisor/<run_id>`，权限 700，用完删掉）；`system_prompt.write_file` 写系统提示；运行时的 `run_command`；`ClaudeRunRecorder` 带 `whole_process=True`，并写入 `run_kind` 和 `parent_record_id`；`single_run.run_once` 给一个截止时间，结束时回收进程树；按记录的花费检查上限，回退到运行时自报的数（`processor.run_cost`）；`budget_error`、`unsuccessful`、拒答（第 10.3 节）都转成类型化的失败。它不碰数据库里的 `agent_spawns`；
- **`spawn`** 建一行 `agent_spawns`（直接进入 `asking`），调一次 `run_child`，把结果写回这一行，返回结果。M4 的 explorer 就用它。advisor 的请教要先过检查、再跑最多 3 轮，所以 `AdvisorService`（PR2）直接用 `db/agent_spawns` 的迁移函数和 `run_child`，不走 `spawn` 这个一步到位的入口；
- **拒绝启动的情况**（不起进程，直接返回失败）：Profile 解析出错；运行时没有 `no_tools` 能力；系统提示不能替换；模型按服务商价格计费却没有完整价格（`budget_unavailable`）；模型要走本地网关（网关会记录请求）；剩下的预算低于一轮的下限（0.10 美元，因为 `--max-budget-usd` 是软上限，见第 18 节）。

## 8. 两个角色、默认 Profile 和启动参数

- **角色：** `AgentType` 加 `advisor` 和 `advice_check`。迁移放宽 `agent_setting_layers.role` 和 `schedule_profile_overrides.role` 的检查（做法和 processor 一样），`role_configs` 加两行（角色 prompt、超时、轮数，Settings → Roles 里能改）。两个角色都只能用有 `no_tools` 能力的运行时，`check_roles` 在选 Profile 时就拒绝不行的；
- **默认 Profile（种子数据，不写在 Python 里）：**`seeds.sql` 给订阅登录的模型目录加 `claude-fable-5-2` 和 `claude-haiku-5-5` 两个模型（固定的 UUIDv5 ID），加两个内置 Profile `claude-code/fable-5-2`、`claude-code/haiku-5-5`，再给 `advisor`、`advice_check` 两个角色各种一行 `scope = 'role'` 的设置层，指向这两个 Profile。都是 `ON CONFLICT DO NOTHING`：运维者改过的不会被覆盖；把角色的 Profile 清空后，这一行的 `profile_id` 是 NULL，种子也不会再填回去；
- **价格表：** `BUILTIN_PRICES` 加 `claude-fable-5-2`：输入 8、输出 40、缓存读 0.20、缓存写 10（5 分钟，按 1.25 倍输入），单位都是美元每百万 token；`CLAUDE_CODE_ALIASES` 加 `fable → claude-fable-5-2`。注意：Claude Code 这次写的是 1 小时缓存（按 2 倍输入计价，第 18 节），表里只有一个缓存写价格，所以按服务商价格计费时会少算这一项。订阅登录用运行时自报的数，不受影响；
- **启动参数**（Claude Code，复用 processor 的无工具路径，`max_turns = 2`）：

```bash
claude --print --verbose --output-format stream-json --include-partial-messages --input-format stream-json
  <封闭的隔离参数：--setting-sources "" --settings <disableAllHooks> --strict-mcp-config …>
  --system-prompt-file <advisor 合同> --system-prompt-snapshot off
  --permission-mode manual --permission-prompts none --tools "" --disable-slash-commands --no-session-persistence
  --model claude-fable-5-2 --effort high --max-budget-usd <这次请教剩下的预算> --max-turns 2
# 工作目录：tempfile.gettempdir()/looper-advisor/<run_id>，空目录，路径里没有用户名，跑完删掉
```

  设计文档里的每个参数都在安装的 CLI 上核对过（第 18 节）：`--system-prompt-file` 和 `--max-turns` 在 `--help` 里没有列出，但 CLI 接受，实测有效；不加 `--json-schema`（D11）；不加 `--fallback-model`，以免拒答后悄悄换成别的模型来回答。

## 9. 请教包

### 9.1 AdviceRequest v1

规划者在回复里写 `advice_request`，就是发给 advisor 的全部内容；对照表放在另一个字段 `advice_bindings` 里，只存在本地。

```json
{
  "v": "advice_request/1",
  "kind": "diagnose | design_choice | plan_review | unblock",
  "problem": "能独立看懂的问题陈述，只用通用术语，≤ 1,200 字",
  "goal": "≤ 400 字",
  "acceptance": ["1–5 条"],
  "constraints": ["≤ 8 条"],
  "stack": ["≤ 8 条，例如 Python 3.12、PostgreSQL 16"],
  "attempts": [{"approach": "…", "signal": "…", "learned": "…"}],
  "artifacts": [{"kind": "snippet | pseudocode | interface | log_excerpt", "lang": "python", "content": "≤ 2,000 字"}],
  "questions": [{"id": "q1", "text": "…"}],
  "options": [{"id": "o1", "summary": "…", "pros": "…", "cons": "…"}],
  "prior_rounds": [{"needs_info_id": "n1", "answer": "…"}]
}
```

`advice_bindings`：`[{"alias": "服务 A", "refers_to": "本地的真实名称或位置"}]`，最多 30 条。

### 9.2 第一道：格式检查（`ProtocolError`，走现有的修复回合）

- 必填：`v`、`kind`、`problem`、`goal`、`questions`（1–3 个，`id` 唯一）；`attempts` 最多 5 条，`artifacts` 最多 3 段，`options` 最多 3 个；每个字段不超过上面的长度，整个请教包的 JSON 不超过 12,000 个字符；不能有其他键；
- **按类型的必填项：**
  - `diagnose`：`attempts` 至少 1 条（每条都有 `signal`），`artifacts` 至少 1 段（snippet、pseudocode 或 log_excerpt），`stack` 至少 1 条；
  - `design_choice`：`constraints` 至少 1 条，`options` 2–3 个；
  - `plan_review`：`artifacts` 里至少 1 段 `interface` 或 `pseudocode`（抽象后的步骤和依赖、接口和数据结构的改动），`constraints` 里写迁移和回滚方式；
  - `unblock`：`attempts` 至少 1 条，`questions` 写清缺什么信息；
- 检查点回合的请教包必须是 `plan_review`。

### 9.3 第二道：代码检查（`looper/agents/advice_lint.py`）

每条命中都记成 `{stage: "code", category, field, start, end}`。退回给规划者的消息只写类别和位置（「`problem` 第 120–160 个字符像是一个本地路径」），**不回显命中的内容**。

| 类别 | 查什么 |
|---|---|
| `secret` | 常见的密钥格式（`sk-…`、`ghp_…`、`AKIA…`、`-----BEGIN … PRIVATE KEY-----`、`xox[abp]-…`、长的 base64 或十六进制串）；`log_redaction` 里登记过的敏感值 |
| `binding` | `advice_bindings` 里的真实值（`refers_to`）原样出现在请教包里 |
| `local_path` | 绝对路径（`/Users/`、`/home/`、`C:\`、`~/`），运行时根目录、任务目录、项目目录的路径 |
| `url` | 任何 URL（第一期没有白名单），以及项目设置里登记的内部域名 |
| `identity` | 邮箱、IP 地址、长 ID（≥ 16 位的十六进制，或者 UUID）、任务 ID、步骤 ID、执行者 ID |
| `raw_log` | 带时间戳的行、堆栈（`Traceback`、`at …(…:…)`），或者一段 `log_excerpt` 超过 20 行 |
| `dangling` | 「上面」「见日志」「above」「see the log」这类指代；提到请教包里没定义的文件名（`*.py`、`*.ts` 这类带扩展名的名字） |
| `verbatim` | 和本地素材逐字重合超过 30%（按 12 字的片段比较） |

### 9.4 第三道：判别（`advice_check` 角色）

- 输入只有请教包本身（JSON），不带素材和对照表；系统提示是判别合同，要求只回一个 JSON 对象：

```json
{"standalone": true, "leaks": [{"category": "identity", "field": "problem"}], "answerable": 0.0, "missing": ["…"]}
```

- `standalone` 为假、`leaks` 不为空，或者 `answerable < 0.6`，都算不过；退回的消息列出 `missing` 和泄漏的类别与字段；
- 判别本身失败（起不来、超时、回复解析不了），这次请教记为 `failed`（`failure: judge`），不会不经判别就发出去；
- 每次判别都是一次 `run_kind = advice_check` 的运行，花费计入这次请教的预算（实测每次约 0.0004 美元）。

### 9.5 退回

不过检查的请教包，通过现有的「回复不合格，请重答」路径退回：消息格式和 `_ask` 的修复回合一样（`[DECISION] Your advice_request did not pass the checks: … Answer the same request again.`），后面附上要改的地方。和契约修复回合不同，它最多退回 2 次（`agent_spawns.returns`），第 3 次仍不过就记为 `blocked`：检查点直接让执行者照原计划开工，可选请教回到原来那个 [DECISION]，但不再提供 advise；两种情况都写一条任务日志。

## 10. 回答

### 10.1 advisor 合同（系统提示，整段替换）

角色 prompt（`role_configs.advisor`，有推荐值，可在 Settings → Roles 里改）加上代码写死的回答合同。合同要点：你只看到这个请教包，其他什么都看不到；只回答请教包里的问题；只给依据要点，不写推理过程；信息不够就回 `needs_info`；超出能力就回 `out_of_scope`；最后一条消息只有一个 JSON 对象。

### 10.2 AdviceResponse v1

```json
{
  "status": "answered | needs_info | out_of_scope",
  "answers": [{"question_id": "q1", "conclusion": "…", "basis": ["≤ 5 条"], "confidence": "low | med | high",
               "assumptions": ["…"]}],
  "options": [{"id": "o1", "verdict": "prefer | viable | avoid", "why": "…"}],
  "next_steps": [{"id": "s1", "action": "…", "verify": "…"}],
  "risks": ["≤ 3 条"],
  "needs_info": [{"id": "n1", "question": "…", "why": "…", "blocking": true}]
}
```

- 解析：取最后一条消息结尾的那个 JSON 对象（和执行者报告用同一个 `extract_report`）；`answered` 必须对每个问题都有一条 `answers`，`question_id` 只能是请教包里的；`options[].id` 只能是请教包里的；`needs_info` 状态必须有 1–3 条 `needs_info`；长度超限的字段截断，不认识的键丢掉；
- 解析失败或校验不过，这次请教就是 `failed`（`failure: parse`）。

### 10.3 拒答

不刻意去试拒答。安装的 CLI 是这样处理的：一条 `stop_reason` 为 `refusal` 的 assistant 消息（`stop_details` 里有类别）；配置了备用模型时，还会有一个 `system` 事件，子类型 `model_refusal_fallback`，然后换模型重试。advisor 不配备用模型；只要流里出现 `stop_reason: refusal`，或者出现 `model_refusal_fallback` 事件，或者结果里有非 Profile 模型的用量，这次请教就记为 `failed`（`failure: refused`），拒答的类别记进 `result`。这一节是读安装文件得出的推断，PR2 会用单测覆盖这几种事件的形状。

### 10.4 回问

advisor 回 `needs_info`、而且这次请教还没用完 2 轮回问时，代码给规划者发一个 [ADVICE] 回合，列出 advisor 的问题。规划者按下面的格式回复，补充的内容同样要过三道检查：

```json
{"advice": "supplement", "answers": [{"needs_info_id": "n1", "answer": "…"}]}
{"advice": "withdraw", "reason": "…"}
```

补充的内容追加进 `prior_rounds`，然后起一个新的 advisor 进程（不复用会话），花费都算在同一次请教的预算里。

## 11. 规划者这边（PR3，开关打开时才有）

### 11.1 回复契约和消息

- **PLAN 契约：** 步骤可以写 `"difficulty": "routine | hard | critical"`（不写就是空），并写明 `critical` 的含义：改接口、改数据结构、做迁移这类一步出错代价很高的改动；
- **DECISION 契约：** 新增 `advise` 决定，带 `advice_request`（和可选的 `advice_bindings`）；新增 `proceed` 决定，只在检查点的回答之后出现：照计划开工，用 `feedback` 写给执行者的话；拿到回答后的那次决定必须带 `advice_adoption`；写明抽象规则（改名，不删事实；结构、版本、错误类型、数量级、耗时都保留；日志去掉时间戳和 ID；代码最多 3 段，去掉路径和内部名称）；
- **[ADVICE] 回合**的契约，格式见第 10.4 节；
- **可编辑的角色 prompt 不改**，只在代码追加的契约里加两句：建议只是输入，采不采纳由你决定；advise 是可选项时，先想清楚卡在哪里、是不是查一下资料就能解决。

### 11.2 本地素材（只给规划者看，不外发）

请教回合（检查点、可选 advise 的 [DECISION]、[ADVICE]）里，代码在 `Local material for your advice request (do not copy it verbatim)` 下附上最多 8,000 字的素材，按顺序取，超出的截断：

1. 本步的说明和验收标准；
2. 执行者最近两次的报告（摘要和错误），以及评审的结论和发现；
3. 这一步的 diff 文件列表（有 worktree 时）；
4. 之前的请教和采纳情况（这一步的）；
5. 依赖步骤的摘要。

素材用于代码检查里的 `verbatim` 比较。第一期没有 explorer，素材不够时，规划者只能在请教包里说明缺什么，或者不请教。

### 11.3 检查点

`difficulty = critical` 的工作步骤，第一次派执行者之前（重试不算），如果开关打开、这一步和这个任务都还没到上限，代码先给规划者发：

```plaintext
[DECISION] Step s2 (step 2) is marked critical. Before its Worker starts, ask the advisor to review your plan for it.
<本地素材>
Advice budget left: $1.00 for this consult, 2 consults left for this step, $5.00 for the task.
Allowed decisions now: advise (kind plan_review).
```

回答回来以后的 [DECISION] 允许 `proceed`、`replan`、`fail`，并且必须带 `advice_adoption`。请教失败、被拦（`blocked`）、超预算，或者规划者没交请教包时，这一步照原计划开工，并写一条任务日志；不会因为 advisor 出问题卡住任务。

### 11.4 可选的 advise

[DECISION] 里允许 `retry`（执行者或评审失败后），而且这一步、这个任务都还没到上限时，`Allowed decisions now` 里多一个 `advise`，消息末尾写上剩下的预算和次数。规划者选了 advise 后，回答回来以后的 [DECISION] 仍是同一个结果，允许 `retry`、`replan`、`fail`（不再提供 advise），必须带 `advice_adoption`。

### 11.5 采纳和重试

```json
"advice_adoption": {"adoption": "adopted | partial | rejected",
                    "items": [{"ref": "q1 | o1 | s1", "use": "adopted | partial | rejected",
                               "steps": ["s2"], "why": "…"}]}
```

- 拿到回答后的那次决定缺了 `advice_adoption`，`parse_decision` 抛 `ProtocolError`，走修复回合；
- 决定是 `retry` 或 `proceed`、而且有没被拒绝的条目时，代码把这些条目（结论、依据、下一步和验证方式）写成 `## Advice the Planner adopted`，放进执行者的第一轮输入，紧跟在规划者的反馈后面；
- 采纳情况写进 `advisor_advice.adoption` 和 `adoption_detail`，同时写一条任务日志。

### 11.6 状态和重启

请教进行中，这一步的状态是 `deciding`（和 [DECISION] 一样）；规划者重启时，`drafting` 或 `needs_info` 的请教按第 5.2 节处理，然后重新发起那次决定（`_decide_again`）。

## 12. 限额与预算

- **提供 advise 之前**查四项：这一步的请教次数 < `advisor_consults_per_step`；这个任务的 < `advisor_consults_per_task`；这个任务已花的请教费 + 这次的预算 ≤ `advisor_budget_usd`；任务本身的花费上限还有余量。任何一项超了，消息里就不出现 advise，检查点也不发；
- **每轮之前：** 剩下的预算 = `advisor_consult_budget_usd` − 这次请教已花的（包括判别），低于 0.10 美元就记为 `over_budget`，不再启动。每轮的 `--max-budget-usd` 取剩下的预算；
- **每轮之后：** 按记录的花费（或运行时自报的数）累加到 `agent_spawns.spent_usd`；撞到上限、花费超过上限、或者花费未知，都记为 `over_budget`，并丢掉这一轮的回答；
- 请教的花费计入任务的花费和任务的花费上限。

## 13. 默认关闭的保证

- `advisor = off`（默认）时：PLAN、DECISION 契约不提 `difficulty`、`advise`、`proceed`、`advice_adoption`；[DECISION] 消息不变；没有检查点、没有 [ADVICE] 回合；执行者的输入不变；`parse_plan` 遇到 `difficulty` 照旧按未知键拒收；
- **测试：** 用一组固定的计划、步骤结果和设置，生成规划者的系统提示、PLAN 和 DECISION 消息、执行者的提示、工具列表和每个角色的命令行，和 `main` 上算出的 sha256 对比（写死在测试里）；开关打开时，同一组输入必须不同（证明测试确实覆盖到了改动的路径）。

## 14. 界面（PR4）

- **任务页：** 步骤卡片上加「请教」小节，按时间列出这一步的每次请教：来源（检查点 / 可选）、状态、advisor 的结论（每个问题一行）、采纳情况、是否解锁、花费；被拦的请教只显示类别和次数；
- **Settings → Roles：** 列出 Advisor 和 advice_check，可以改 Profile、角色 prompt、超时；
- **项目设置：** `advisor` 开关和第 5.4 节的限额；
- 按 `docs/design.md` 的界面规则做，用一次性的 serve 拍无头浏览器截图。

## 15. 观测

- **指标**（任务页和 `metrics` 读数，后续可以加看板）：请教次数（按来源和状态）、每次的花费、采纳率、解锁率（和同一步不请教时的对照留到 M1）、检查的拦截率和退回率（按类别）、回问率、拒答率、每轮延迟；
- **告警**（设计文档定的阈值，第一期只在任务日志和指标里体现，接入告警通道留到以后）：拦截率 > 30%、采纳率 < 20%、拒答率 > 5%、每周花费超预算；
- **排查：** 每轮 advisor 和每次判别都是一行 `llm_records`，`parent_record_id` 指向规划者那次运行；`agent_spawns.result` 记每轮和每次检查。

## 16. 评测与测试

- **单测**（测试先行，规则在最近的一层测一次）：`Access` 的推导和命令行不变；迁移和回填；状态迁移表；格式检查的每条规则；代码检查的每个类别（命中时不记内容）；判别回复的解析；AdviceResponse 的解析和每种失败；拒答事件；预算的四项检查和每轮的下限；`advice_adoption` 缺失时拒收；采纳的建议进入执行者输入；`unlocked` 的回填；开关关闭时逐字节不变；
- **真实路径：** 一次性的 `looper serve`，新数据库，不用运维者的 8767 端口；PR2 做一次真实 CLI 的来回；PR3 跑一个带 `critical` 步骤的任务（用替身跑一次、用真实 CLI 跑一次），看到检查点请教、采纳和带建议的重试；
- **prompt eval（PR3）：** `evals/` 的真实模式（`--claude ~/.local/bin/claude`，`--reps 2`），并附上和 main 的对比。要能看到：强制请教时，第一次写出的请教包能过检查；收到回答后的决定带了 `advice_adoption`；开关关闭时 prompt 逐字节不变；advise 是可选项时，规划者不会每次都去请教；
- **三组对照评测（原始上下文 / 抽象请教包 / 只调高 effort）：** 第一期只做框架和花费的 dry-run 估算，估算附在 PR3；真跑等 M1 的难题集和维护者批准预算。通过标准不变：抽象组的通过率比原始组低不超过 5 个百分点，成本更低，泄漏为 0；泄漏按整个请求体算，包括第 18 节的固定内容。

## 17. 分期

每个 PR 都从最新的 `origin/main` 开出，可以单独评审、单独回滚：

| PR | 内容 | 改不改模型可见内容 |
|---|---|---|
| PR0 | 本 RFC | 否（只有文档） |
| PR1 | 迁移（`agent_spawns`、`advisor_advice`、`llm_records.parent_record_id` 和 `run_kind`、`task_steps.difficulty`）；`Launch` 的访问级别；`spawn` 原语；命令行不变的测试 | 否 |
| PR2 | advisor 和 advice_check 角色、种子 Profile、价格表、启动、请教包和回答的解析、三道检查、`agent_spawns` 的状态、`llm_records` 的 `run_kind` 和父记录；开关仍然关着，任务里走不到；PR 里附一次真实 CLI 的来回 | 否（任务里走不到） |
| PR3 | 规划者这边（第 11 节）、项目设置、`unlocked` 回填、prompt eval、三组评测的框架和 dry-run 估算 | 是，只在开关打开时 |
| PR4 | 界面（第 14 节） | 否 |

## 18. 开工前验证（2026-11-19，Claude Code 2.1.328）

用本地的抓包服务（`ANTHROPIC_BASE_URL` 指向它，只记请求体、不记请求头，返回 400，抓到的内容事后删掉）和几次真实调用：

| 问题 | 结果 |
|---|---|
| 第 8 节的参数在空的临时目录下 | 系统提示只剩两行固定前缀，后面是替换后的合同；工具 0 个。请求里还有一条 `# Environment` 消息：工作目录的绝对路径（系统临时目录下，没有用户名）、是否 git 仓库、平台、shell、系统版本、模型名、日期，以及一行 `USD budget`；`metadata.user_id` 里有 device_id、account_uuid、session_id |
| 账号邮箱提醒能不能关 | **订阅登录下关不掉**：没有文档化的参数、设置或环境变量。邮箱取自登录账号的信息，只在订阅登录时加进第一条用户消息。用 API key 登录时没有（实测）。用新的空 `CLAUDE_CONFIG_DIR` 加 `CLAUDE_CODE_OAUTH_TOKEN` 时，用假 token 实测也没有；按安装文件，这种 token 默认只有推理权限，拿不到账号信息，但这一点还要用真的 token 再测一次。怎么处理由维护者决定（第 19 节 Q1） |
| `--system-prompt-file`、`--max-turns` | `--help` 里没有列出，但 CLI 接受，实测有效 |
| `--tools ""` + stream-json + `--json-schema` | 能一起用，但 `--json-schema` 会加一个 `StructuredOutput` 工具（init 里的工具列表不再为空），运行也要 2 轮，所以不用它（D11） |
| Fable 拒答 | 没有刻意去试，按安装文件推断，见第 10.3 节 |
| `--max-budget-usd` 的准确性 | Claude Code 自报的花费和按价格算的完全一致（输入 8、输出 40，1 小时缓存写按 16 美元每百万 token）。它是软上限：上限 0.01 美元的一轮实际花了 0.056 美元，结果是 `error_max_budget_usd`，模型已经写出的回答被丢掉了。所以每轮之前要先查剩下的预算（第 12 节的 0.10 美元下限） |
| 一轮的花费 | 两个小问题实测 0.038 美元（5 秒）和 0.056 美元（13 秒）；`claude-haiku-5-5` 的一次判别 0.0004 美元（3 秒），订阅登录能用 |

## 19. 待定问题

- **Q1 账号邮箱提醒**（维护者决定）：(A) 接受：邮箱只发给 Anthropic，它本来就持有这个账号的信息，泄漏评测把它列为已知的固定内容；(B) advisor 用一个单独的长期 token（`claude setup-token`）和单独的空配置目录，仍然走订阅额度，需要运维者做一次 setup-token，再实测一次；(C) advisor 用 API key，按量付费。推荐 B。在定下来之前，PR2 的启动给配置目录和登录方式留一个由 Profile 的服务商决定的入口，A、B、C 都不用改代码。
- **Q2** `advisor_budget_usd` 的默认值 5 美元是按每次请教 1 美元、每任务 10 次的上限估的上限值；实际花费要等 PR3 的评测再调。
