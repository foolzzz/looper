# RFC：任务引擎可靠性 v0（失败分型、全局准入、用量与限流信号、降级规则）

- **状态：** 已定，等实现。第 3 节的 D1–D8 来自维护者的产品设计（第 4 期「引擎更稳」和「模型接入层」两节）。第 15 节的 Q1–Q11 维护者已在 2026-11-20 全部按推荐答复，记为 D9–D19。实现按第 13 节的 PR 顺序，还没有开工。
- **需求：** 四件事。(1) **失败分型**：步骤失败先由代码分成四类：额度不足（等重置）、瞬时故障（有限次重试）、环境问题（带类型进你的关卡）、需要判断（进 Planner 的 `[DECISION]`）；同一步骤连续失败 3 次熔断。(2) **全局准入和公平队列**：按账号设并发槽位；你发起的优先，重试和恢复其次，等得最久的定期提前；遇到 429 冻结这个账号的槽位，直到额度重置。(3) **用量和限流信号**：按账号汇总 token、429 次数和额度重置时间，调度器读这份数据做准入。(4) **降级规则类型化**：备用 Profile 的触发条件分成额度不足和供应商故障两类；后台角色可以自动切换，管家和 Planner 切换前先告诉你。
- **相关：** [Agent 系统架构](../agent-system.md)（「Concurrency limits」「Runtimes, LLM providers and Agent Profiles」「What a run spent」「Model request recording」「How a task runs」）；[Advisor RFC](advisor-v0.zh-CN.md)（`ChildFailure.RATE_LIMITED`，本文把它推广到所有运行）；[用户关卡 RFC](user-gate-v0.zh-CN.md)；[热升级 RFC](hot-upgrade-v0.zh-CN.md)（保留运行和排空）。
- **核对基线：** `main` `26d1b3a82`。第 2 节的「现状」读代码得出，每项标了出处；第 2.5 节的数字来自一个长期运行的实例，只用只读接口（`runs.list`、`runs.events`、`tasks.page`、`tasks.detail`、`llmrec_settings.get`）汇总，不含任何内容。
- **术语：**
  - **账号：** `llm_providers` 的一行（例如内置的 `claude-subscription`、`codex-login`，或一个 API key 账号）。额度和限流都按账号算。
  - **失败类（`FailureClass`）：** 代码给一次失败的运行定的类，四个值之一（第 4.1 节）。
  - **失败原因（`FailureCause`）：** 类下面更细的类型化原因，例如 `usage_limit`、`connection_failed`（第 4.1 节）。
  - **准入：** 一次运行（或常驻会话的一个回合）开始前向账号要一个槽位。
  - **车道（`Lane`）：** 等槽位时的优先级（第 5.2 节）。
  - **冻结：** 账号返回「额度用完」后，在重置时间之前不再放行新的运行（第 5.3 节）。

## 1. 背景与目标

现在一次运行失败以后，Looper 不区分为什么失败：

- Worker 或 Reviewer 的运行失败一律是 `RunOutcome.RUN_FAILED`，交给 Planner 的 `[DECISION]` 回合，由模型决定重试、重新规划还是失败；
- Planner 的规划回合失败直接让任务失败（「Planning failed: …」），决定回合失败退回步骤自己的结论；
- 同时跑多少运行只按任务算，账号层面没有上限（`llm_providers.max_concurrency` 存了但没人执行）；
- 一个账号的额度用完以后，每个运行各自撞一次 429，任务一个接一个失败，定时任务照常触发、照常失败。

第 2.5 节的数据：线上实例近 40 天有 84 次运行因订阅额度用完而失败，其中 80 次发生在 Looper 已经从同一个账号拿到「已拒绝，某时重置」之后；56 个任务因此失败（占失败任务的 24%）。交给 Planner 的 Worker/Reviewer 失败里，一半根本不需要模型判断。

**目标：**

- 失败先由代码分类，不需要判断的不进 Planner，按类处理；分类有记录，可以统计；
- 账号额度用完时，Looper 等重置，而不是让后面的运行接着失败；
- 账号层面有并发上限和优先级，你在等的回合先跑；
- 用量和限流信号按账号汇总，Dashboard 和接口能看到「这个账号本周用了多少、什么时候重置」；
- 备用 Profile 的触发条件类型化，换模型不悄悄发生；
- 每项各有开关，默认关闭；关闭时提示词、工具列表、命令行和行为逐字节不变。

**不在目标内：**

- 多个账号轮换来绕过限流（违反订阅条款的风险，D7）；
- 小模型调用通道、按角色的每日预算（模型接入层的另两项，另行设计）；
- 接管各运行时自己的模型连接或会话缓存；
- 修改请求记录（`llmrec`）的默认值：本文不依赖它（第 2.4 节）。

## 2. 现状（读代码得出）

### 2.1 失败今天怎么走

| 谁失败 | 今天怎么处理 | 出处 |
|---|---|---|
| Worker/Reviewer 的运行 | `run_outcome` 把没有结果、`is_error`、`subtype` 不是 `success` 的一律记为 `RUN_FAILED`（`metadata.outcome`），结果 `{"status": "failed", "error": …}` 发给 Planner，进 `[DECISION]` | `looper/agents/tiers/worker.py` `RunOutcome`、`run_outcome`、`_work_failed` |
| Planner 的规划回合 | `PlannerTurnError`，任务失败，`tasks.error` 写「Planning failed: …」，没有重试 | `looper/agents/tiers/planner.py` `_run_turn`（「turn did not succeed」那段） |
| Planner 的决定回合 | 退回步骤自己的结论，不派 Reviewer | `planner.py` `_undecided` |
| Manager 的回合 | 把错误回给用户 | `looper/agents/tiers/manager.py` `_run_turn` |
| advisor、judge 的子运行 | 已经类型化：`ChildFailure`（`UNAVAILABLE`、`RUNTIME`、`TIMEOUT`、`BUDGET`、`REFUSED`、`RATE_LIMITED`），`RATE_LIMITED` 来自结果的 `api_error_status == 429` | `looper/agents/spawn.py` |
| API 运行时 | 已经类型化：`ErrorKind`（`AUTH`、`RATE_LIMITED`、`UNAVAILABLE`、`CONTEXT_OVERFLOW` 等），按 HTTP 状态和错误码分 | `looper/agents/runtimes/api/loop.py`、`responses.py` |
| 任务的结束原因 | `tasks.fail_kind` 只有一个值 `task_budget`；其他失败只有 `tasks.error` 里的文字 | `looper/db/tasks.py` `FailKind` |
| 备用 Profile | 只在启动失败、或在第一个模型事件之前非零退出（以及报告 0 回合的错误结果）时切换，原因是字符串（`start_error`、`exited before any model event: …`）；Planner 只在 Profile 配置错误时切，Manager 不切 | `worker.py` `_fallback_profile`；架构文档「Agent Profiles」 |
| 运行时自己的登录坏了 | Codex、Grok、coco 的登录被发现「分叉」后，整个账号在启动时被拒，直到操作者清除（`clear_login_problem`） | `looper/agents/profile_registry.py`；`agent_settings._login_problem` |

今天的备用 Profile 不看原因，只看「有没有过模型事件」：额度在运行中途用完的 Worker（有过模型事件）不切；一开始就被拒的 Worker（结果 `num_turns: 1`，合成的 assistant 消息和零用量都不算模型事件）在非零退出时会切，退出码是不是非零需要验证；Planner 只在 Profile 配置错误时切，Manager 不切。所以同一个「额度用完」，今天有的切、有的不切，切到哪里也不分原因。

### 2.2 并发今天怎么算

只按任务算（`WorkerPool._take_slot`、`load_task_limit`）：一个任务的 Worker 默认最多 16 个、Reviewer 8 个，Planner 和 Manager 不限，不同任务之间互不等待。`llm_providers.max_concurrency` 有列、有检查约束，但「stored, not enforced」（架构文档「Concurrency limits」）。热升级排空时 `_take_slot` 把上限当成 0。

### 2.3 额度和限流信号在哪里

| 运行时 | 信号 | 今天 Looper 用了吗 |
|---|---|---|
| Claude Code（`stream-json`，实测 2.1.329 的记录） | 每次带工具的运行有一条 `{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed" \| "allowed_warning" \| "rejected", "rateLimitType": "seven_day", "resetsAt": <epoch 秒>, "unifiedWindows": {"seven_day": {"status", "resetsAt", "utilization"}}, "overageStatus", "isUsingOverage"}}`；额度用完时 `result` 带 `is_error: true`、`api_error: "usage_limit_reached"`、`api_error_status: 429`、`api_error_type: "rate_limit_error"`、`terminal_reason: "api_error"`；网络错误时 `api_error: "connection_failed"`，前面有 `system/api_retry` 事件（`attempt`、`max_retries`、`error_status`、`retry_delay_ms`）。较早的版本（11 月 15 日的记录）`result` 上没有 `api_error*` 字段，只有 `rate_limit_event` 和 assistant 消息的 `error: "rate_limit"` | 事件原样存进 `llm_record_events`；只有 `spawn.py` 读 `api_error_status` |
| Codex app-server（探测到 steer 能力时 Worker/Reviewer 的运行，`codex_steer.py`；字段来自 codex-cli 0.169.0 生成的协议 schema） | `account/rateLimits/updated`（`primary`、`secondary` 窗口：`usedPercent`、`windowDurationMins`、`resetsAt`；`rateLimitReachedType`；`limitId`）；`account/rateLimits/read` 请求；`error` 通知的 `codexErrorInfo`（`usageLimitExceeded`、`rateLimitExceeded`、`serverOverloaded`、`internalServerError`、`unauthorized`、`contextWindowExceeded`、`httpConnectionFailed{httpStatusCode}`、`responseStreamConnectionFailed`、`responseStreamDisconnected` 等） | 只读 `thread/tokenUsage/updated`；`error` 只留 `message` 文字，`codexErrorInfo` 丢掉 |
| Codex exec（Manager、Planner 的逐回合运行，以及没有 steer 能力时的运行） | `turn.failed`、`error` 只有 `message` 文字 | 文字进 `errors` |
| Grok、Cursor、coco、OMP、Pi、DSH | 没有找到类型化的额度信号 | — |
| API 运行时 | HTTP 状态、错误码（已映射成 `ErrorKind`）；响应头里的限流头没有读 | `ErrorKind.RATE_LIMITED` |
| 本机代理（`llmrec`） | `llm_requests.status`、`response_headers`（白名单内的头） | 只用于记录 |

### 2.4 请求记录覆盖多少

`llmrec` 只覆盖新启动的 Claude Code 运行和 API 运行时；Codex、OMP、Pi、DSH、Grok、Cursor、TTADK 一律 `skipped`（`SkipReason.NOT_COVERED`，原因在 `llmrec/control.py` 的 `NOT_COVERED_DETAILS`）。Claude 订阅登录的运行还要 `llmrec.include_oauth`，默认关；关着时 `attach` 直接返回，不写 `metadata.llmrec`（所以这类运行连 `skipped` 标记都没有）。

线上实例：`llmrec.enabled` 是 `true`，`include_oauth` 是 `false`，代理收到的请求数和存着的请求数都是 0。**订阅流量的请求记录覆盖率是 0**；就算打开 `include_oauth`，Codex 和 Grok 仍然是 `not_covered`。所以用量和限流信号不能靠代理，要从运行时自己的输出里解析（第 6 节）。运行记录（`llm_records`）本身的 token 和花费覆盖得很好（下表）。

### 2.5 线上数据（只读汇总）

样本：运行记录 11,998 条（2026-10-12 至 11-20），任务 1,223 个，其中失败 230 个。

consult 运行在 2026-11-11 之前记录的花费偏高（记成了被分叉会话的累计用量），见 #1222；本节唯一的花费合计（下面的 $73）来自 11-11 20:00（UTC）以后新启动的 Worker 运行，不含这些行，数据保持原样。

**运行时和用量覆盖**

| 账号类型 | 运行数 | 有 token 数 | 有花费 | 有请求记录 |
|---|---|---|---|---|
| Claude Code，订阅登录 | 8,915（74%） | 8,787（98.6%） | 8,770 | 0 |
| Codex，ChatGPT 登录 | 2,983（25%） | 2,922（98.0%） | 2,922 | 0（490 条标 `not_covered`，其余早于这个标记） |
| API key（OMP） | 70 | 69 | 69 | 0（26 条 `not_covered`） |
| 没有 Profile 元数据 | 30 | 1 | 3 | 0 |

`rate_limit_event` 抽样（11 月 14 日以后 46 条 Claude Code 运行）：带工具的角色（Manager、Planner、Worker、Reviewer）40 条里 29 条有一条，`utilization` 0.01–0.96，`status` 有 `allowed`、`allowed_warning`、`rejected`；processor 6 条都没有。processor 的记录只留类型化事件（`processor.typed_events`），原始输出里有没有这条事件需要验证。

**额度用完的失败**（`result` 或输出是「You've hit your weekly limit · resets …」）

| 日期 | 失败运行 | 跨度 | 其中 Planner 回合 | 其中 Worker |
|---|---|---|---|---|
| 10-18 | 4 | 05:09–06:40 | 4 | 0 |
| 11-11 | 7 | 20:00–21:31 | 1 | 6 |
| 11-15 | 57 | 00:28–22:30 | 55 | 2 |
| 11-20 | 16 | 02:14–03:28 | 15 | 1 |

- 合计 84 次；每个窗口的第一次躲不掉，后面 80 次发生在账号已经报告「已拒绝」和重置时间之后（连锁失败）。
- Planner 回合 75 次，中位 1.9 秒、花费 0：撞上就失败。它们让 56 个任务在规划时失败（11-15 有 53 个，11-20 有 3 个），全部是三个定时任务建出来的；11-15 那次给出的重置时间在 3 天以后，定时任务每半小时照常触发、照常失败。
- Worker 9 次，中位跑了 7.9 分钟，最长 91 分钟，按 API 价折算 $73 的工作被截断。11-11 那 6 个 Worker 是同时在跑的，在同一分钟一起结束：冻结挡不住这一类，只有提前看到用量（第 6 节）才有办法（推测，需要验证当时的 `utilization`）。

**Worker/Reviewer 的失败**（`metadata.outcome`）

| 结局 | 数量 | 按本文会分到 |
|---|---|---|
| `run_failed`：Looper 自己停的（热升级、停止，`CancelledError`） | 78 | 不算失败（第 4.1 节） |
| `run_failed`：启动超时（keeper 10 秒内没打开控制 socket） | 54 | 瞬时故障 |
| `run_failed`：Codex 非零退出（事件里是「workspace routing discovery timed out」的重连失败、「model is at capacity」） | 24 | 瞬时故障 |
| `run_failed`：API 不可达（`connection_failed`） | 14 | 瞬时故障 |
| `run_failed`：额度用完 | 9 | 额度不足 |
| `reported_failed`：Worker 报告失败 | 62 | 需要判断 |
| `contract`：没有报告或报告读不了 | 39 | 需要判断 |
| `not_passed`：Reviewer 没通过 | 372 | 需要判断（这是评审结论，不算运行失败） |

去掉 Looper 自己停的和评审结论，进 `[DECISION]` 的失败结果 202 个，**101 个（50%）不需要判断**。Planner 自己的失败回合 142 个：额度 75、瞬时（几乎都是 API 不可达）39、环境（主要是工作目录不存在）12，其余 16 个（超时、提示太长等），89% 不需要判断。

**突发：** API 不可达共 37 次，最多一小时 9 次；启动超时 54 次，最多一小时 11 次。Claude Code 自己已经对连接错误重试 10 次（约 3 分钟）才放弃，所以 Looper 再重试要等更久。

**并发**（11-01 至 11-20，按分钟采样同时在跑的运行）：Claude 订阅 P50 0、P95 8、P99 13、最多 21；Codex P95 1、P99 3、最多 11。

**备用 Profile：** 0 次运行走过备用链（没有配置）。

## 3. 已定的前提

| 编号 | 内容 |
|---|---|
| D1 | 步骤失败先由代码分成四类：额度不足（等重置）、瞬时故障（有限次重试）、环境问题（带类型进你的关卡）、需要判断（进 Planner 的 `[DECISION]`） |
| D2 | 同一步骤连续失败 3 次熔断，防止误判后反复烧钱 |
| D3 | 按账号设并发槽位；你发起的优先，重试和恢复其次，等得最久的定期提前 |
| D4 | 遇到 429 冻结该账号的槽位，直到额度重置 |
| D5 | 用量、429 次数和重置时间按账号汇总，调度器读这份数据做准入；不经过本机代理的运行时，从运行结果里解析 |
| D6 | 备用 Profile 的触发条件分成额度不足和供应商故障两类；后台角色可以自动切换；管家和 Planner 切换前先告诉你，不悄悄换模型 |
| D7 | 不做多账号轮换绕过限流；不做语义缓存和通用网关 |
| D8 | 暂时不用 API key，所有模型调用都走订阅登录的 Claude Code、Codex、Grok（本文的信号以这三者为主，API 运行时顺带） |
| D9 | 冻结和槽位按整个账号算，不按窗口或模型拆，也不按 `vendor_type` 合计（Q1） |
| D10 | 准入打开时，账号的任何一个窗口 `utilization ≥ 0.95`，就暂停这个账号的 `BACKGROUND` 车道，其他车道照常（Q2） |
| D11 | 冻结时你给 Manager 发消息，代码立刻回一句（重置时间，有备用 Profile 时附上可选的备用），消息留在队列里，重置后照常回答（Q3） |
| D12 | 冻结期间，定时任务建出的任务最多等 6 小时或这个定时任务的一个周期，取小的；超过就以 `fail_kind = quota_exhausted` 结束（Q4） |
| D13 | 环境问题挂起不受 `user_gates` 开关限制，所有项目和定时任务都进「需要你」（Q5） |
| D14 | Worker、Reviewer 是否自动切备用 Profile，跟 Profile 的 `fallback_on` 走，默认只有 `launch_failure`（Q6） |
| D15 | Planner 切备用 Profile 前通知你并等确认，没有确认就等重置（Q7） |
| D16 | Codex 冻结到期后，用 app-server 的 `account/rateLimits/read` 探测（不花模型调用）；开工前验证，用不了就放行等得最久的一个运行（Q8） |
| D17 | 准入打开但 `max_concurrency` 为空时不限；文档建议 Claude 订阅账号设 10（Q9） |
| D18 | 重新派发时给 Worker 加一行代码写的说明（类、原因、工作目录里可能有改动），跑真实评测（Q10） |
| D19 | keeper 启动超时（10 秒没打开控制 socket）算瞬时故障（Q11） |

验收口径（产品设计给的）：交给 Planner 的非判断类失败占比下降；429 引发的连锁失败次数下降。基线见第 2.5 节：50% 和 80 次。

## 4. 失败分型

### 4.1 类型

`looper/agents/failures.py`（新模块）：

```python
class FailureClass(StrEnum):
    QUOTA = "quota"              # 额度不足：等重置
    TRANSIENT = "transient"      # 瞬时故障：有限次重试
    ENVIRONMENT = "environment"  # 环境问题：等你处理
    JUDGMENT = "judgment"        # 需要判断：交给 Planner

class FailureCause(StrEnum):
    USAGE_LIMIT = "usage_limit"              # QUOTA：订阅窗口用完、usageLimitExceeded、insufficient_quota
    RATE_LIMIT = "rate_limit"                # QUOTA：短时限流 429、rateLimitExceeded
    CONNECTION_FAILED = "connection_failed"  # TRANSIENT：DNS、连接、流中断
    PROVIDER_OVERLOADED = "provider_overloaded"  # TRANSIENT：5xx、529、serverOverloaded、at capacity
    START_TIMEOUT = "start_timeout"          # TRANSIENT：Looper 本机启动超时（keeper 控制 socket）
    RUNTIME_MISSING = "runtime_missing"      # ENVIRONMENT：可执行文件、工作目录不存在
    LOGIN = "login"                          # ENVIRONMENT：未登录、401/403、登录分叉
    TLS = "tls"                              # ENVIRONMENT：证书、代理配置
    PROFILE = "profile"                      # ENVIRONMENT：profile_problem、账号被停用
    TIMEOUT = "timeout"                      # JUDGMENT：运行超时
    CONTEXT = "context"                      # JUDGMENT：提示太长、上下文溢出
    BUDGET = "budget"                        # JUDGMENT：运行花费上限
    REPORT = "report"                        # JUDGMENT：报告失败、契约违反、文件不符
    UNKNOWN = "unknown"                      # JUDGMENT：分不出来

@dataclass(frozen=True)
class Failure:
    cls: FailureClass
    cause: FailureCause
    signal: Literal["typed", "text"]   # 来自类型化字段，还是来自运行时适配器的文字规则
    resets_at: datetime | None = None  # QUOTA 才有
    detail: str = ""                   # 有界、脱敏的说明，给活动和通知
```

- **Looper 自己停的不算失败。** `RecordStatus.interrupted`（停止、取消、热升级、服务停止）走现有的重启和保留运行，`classify` 返回 `None`。
- **分不出来就是需要判断。** `UNKNOWN` 归 `JUDGMENT`，也就是今天的行为；分类出错（异常）同样当 `JUDGMENT`，记一条日志。
- **类和原因都是类型，不是文字匹配。** 能拿到类型化字段的（Claude Code 的 `api_error`/`api_error_status`、`rate_limit_event`，Codex app-server 的 `codexErrorInfo`，API 运行时的 `ErrorKind`，Looper 自己抛的启动异常）一律用字段；只有字段缺失时（较早的 Claude Code、Codex exec、Grok）才用运行时适配器自己的文字规则，结果带 `signal: "text"`，每条规则有录制的样本测试。

### 4.2 分类在哪里做

- 每个运行时适配器加一个钩子 `failure_signal(result_event, events, *, exit_code, stderr_head) -> Failure | None`（`looper/agents/runtimes/__init__.py`，和 `failure_detail` 并列；默认返回 `None`，也就是 `UNKNOWN`）。实现：Claude Code、Codex（app-server 和 exec 两条路）、API 运行时；Grok 第一期只有文字规则（需要验证它的报错样子）。
- `failures.classify(...)` 合并三处来源，优先级从高到低：Looper 自己的类型化异常（`RunStartError` 的子类、`RuntimeUnsupported`、`profile_problem`、`_login_problem`）→ 运行时钩子 → 运行结局（`RunOutcome.REPORTED_FAILED`、`CONTRACT`、`FILES_MISMATCH` 和超时、花费上限都是 `JUDGMENT`）。
- 调用点：`ClaudeRunRecorder.finish` 之前，Worker/Reviewer（`worker.py` `_execute_once`）、Planner 回合（`planner.py` `_run_turn`）、Manager 回合（`manager.py` `_run_turn`，只接线）、processor（`processor.run`）、子运行（`spawn.run_child`：`ChildFailure.RATE_LIMITED` 映射到 `QUOTA`）。
- Codex app-server 的读取（`codex_steer.py`）把 `error` 通知的 `codexErrorInfo` 原样放进翻译后的事件（今天丢掉了）；这改的是记录的事件，不是给模型的内容。

### 4.3 每类怎么处理（`engine.failure_typing = act`）

| 类 | Worker/Reviewer 的步骤 | 算不算 Planner 的重试次数 | 进不进 Planner |
|---|---|---|---|
| 额度不足 | 步骤挂起（`step_holds.kind = quota`），到 `resets_at`（准入打开时以账号的冻结为准）后重新派发，走「恢复」车道；没有重置时间时按第 5.3 节的退避 | 不算 | 不进，除非熔断 |
| 瞬时故障 | 挂起到退避结束（第一次 2 分钟，第二次 10 分钟，各加减 20% 抖动）后重新派发 | 不算 | 不进，除非熔断 |
| 环境问题 | 步骤挂起（`kind = environment`），任务报告 `waiting_user`（「需要你」分组），通知任务的来源会话；你选「已修好，重试」「交给 Planner」「停止任务」之一 | 不算 | 你选「交给 Planner」时进 |
| 需要判断 | 和今天一样 | 和今天一样 | 进 |

- **重新派发的说明：** 新的 Worker 收到同样的指令，末尾加一行代码写的说明：「上一次运行没有完成：<类>（<原因>）。工作目录里可能有它留下的改动。」这一行是模型可见的，PR4 要跑真实评测（D18、第 14 节）。
- **挂起的步骤不算开始。** 步骤保持 `pending`，派发时跳过有挂起的步骤；`replan` 可以照常替换它（挂起随之删除）。
- **环境问题不走 Planner 定义的用户关卡。** 它是系统挂起，不需要项目或定时任务打开 `user_gates`（D13）；它复用任务的 `waiting_user` 状态和通知渠道，不复用 `task_user_gates`（那张表的主键是关卡步骤）。

### 4.4 各角色自己回合的失败

| 回合 | 额度不足 | 瞬时故障 | 环境问题 |
|---|---|---|---|
| Planner 规划、决定、`[CHAT]` 等回合 | 回合挂起，重置后在同一会话里重发同一条消息；定时任务建出的任务最多等 6 小时或这个定时任务的一个周期，取小的，超过就以 `fail_kind = quota_exhausted` 结束（D12） | 按 4.3 的退避重发同一回合；连续 3 次失败走今天的失败路径（规划失败让任务失败，决定失败退回步骤结论） | 规划回合：任务失败，`fail_kind = environment`；其他回合：今天的路径 |
| Manager 回合 | 代码回一句：「<账号> 的额度已用完（<窗口>），<本地时间> 重置。」有备用 Profile 时问要不要换（第 7 节）；消息留在队列里，重置后照常回答（D11） | 不自动重试（用户在等，Claude Code 已经重试过 3 分钟），回一句带类型的说明 | 回一句带类型的说明 |
| processor（订阅、抽取、判别） | 这次运行挂起，最多等 6 小时，超过记失败（带类） | 退避后重试，最多 2 次 | 记失败（带类） |

Planner 和 Manager 的回合挂起只在内存里；服务重启时，任务按现有的恢复路径继续，准入会重新挂起它。

### 4.5 熔断

- **连续失败数（`streak`）：** 一个步骤从它最近一次「运行本身成功」（结局不是 `run_failed`）之后，失败的运行有几次，四类都算，自动重试和 Planner 的重试都算；挂起期间没有启动的运行不算。
- **熔断：** `streak` 到 3，这个步骤的自动处理停掉，下一次失败（不论类）直接进 Planner 的 `[DECISION]`，消息里列出这几次失败的类和原因。Planner 的选项和重试次数照旧（`max_step_retries`）。步骤再有一次运行本身成功，熔断解除。
- Planner、Manager、processor 的回合用同一个阈值（4.4 节的「连续 3 次」）。
- 阈值第一期是常量 3，不做设置（AGENTS.md：不为还不存在的调用方加选项）。

### 4.6 记录

- `llm_records.failure_class`、`failure_cause`：失败运行的类和原因（成功的、关闭开关时的、以前的都是 NULL）；`metadata.failure` 留 `signal`、`resets_at`、`detail`。
- `step_failures`：步骤每一次失败一行（类、原因、`streak`、采取的动作），只增不改，供熔断和统计用。
- `step_holds`：步骤现在的挂起（一个步骤至多一行），解除时删除，历史在 `step_failures` 和任务日志（`task_journal`，新 kind `failure_hold`）里。
- `tasks.fail_kind` 加两个值：`quota_exhausted`、`environment`。

## 5. 全局准入和公平队列

### 5.1 槽位

- **按账号。** 上限是 `llm_providers.max_concurrency`（已有的列，NULL 不限）。准入打开时才执行。冻结和槽位都按整个账号算；账号下的窗口或模型不拆，同一 `vendor_type` 的几个账号也不合计（D9）。NULL 时不限（D17），文档建议 Claude 订阅账号设 10（线上 P95 8、P99 13）。
- **什么占槽位：** Worker、Reviewer 的一次运行（启动到进程结束）；Planner、Manager 常驻会话的一个回合（发出消息到结果）；processor 的一次运行；consult、advisor、judge 的一次运行。常驻会话空闲时不占。Claude Code 自己的子 agent、teammate 算在它的运行里。
- **嵌套不等待：** 在一个回合进行中由它的工具调用发起的运行（例如 Manager 回合里的 consult）借用父回合的槽位：计数，但不排队，避免父等子、子等父的死锁。
- **热升级：** 排空时不放行；被保留的运行在新进程接管时重新登记它们的槽位（`kept_run` 的接管路径）。
- **和任务上限叠加：** 先过任务自己的上限（`_take_slot`，不变），再过账号的准入。

### 5.2 车道和提前

```python
class Lane(IntEnum):
    INTERACTIVE = 0  # 你在等的回合：回答你消息的 Manager 回合、Planner 的 [CHAT]/[GATE] 回合、「立即运行」
    USER_TASK = 1    # 你（或 Manager 应你的要求）建的任务的 Planner、Worker、Reviewer
    RECOVERY = 2     # 重试和恢复：挂起解除后的重新派发、[RESTART] 回合、服务启动时恢复的任务
    BACKGROUND = 3   # 定时任务建的任务、订阅和 processor、Manager 的收尾回合
```

- 车道由代码按回合类型和任务来源（`tasks.schedule_id`、`TurnKind`）决定，不由模型决定。
- 同一车道先来先放。
- **提前：** 每等满 5 分钟，等待者的有效车道提前一档，最多提到 `USER_TASK`（等得再久也不插到你正在等的回合前面）。
- **后台收紧（D10）：** 账号的任何一个窗口 `utilization ≥ 0.95`（第 6 节的最新信号）时，这个账号的 `BACKGROUND` 车道暂停放行，其他车道照常；信号回落到 0.95 以下、窗口重置或账号冻结后又解冻时恢复。正在跑的不打断。
- **你在等的回合的保护：** `INTERACTIVE` 的等待者等了 2 分钟还没放行（槽位被长运行占满、账号没冻结），就超额放行，记一条告警。账号冻结时不适用（第 4.4 节的回复先发出）。

### 5.3 冻结

账号的准入状态（`provider_admission.state`）：

| 从 | 事件 | 到 | 说明 |
|---|---|---|---|
| `open` | 一次运行分到 `QUOTA`，带 `resets_at` | `frozen`，`until = resets_at + 60 秒` | 正在跑的不打断 |
| `open` | `QUOTA`，没有 `resets_at` | `frozen`，`until = now + 退避[k]` | 退避 5、10、20、40、60 分钟，之后一直 60 |
| `frozen` | 到了 `until` | `probing` | |
| `probing` | 放行一个探测：等得最久的那个等待者 | — | 同时只有一个探测 |
| `probing` | 探测运行没有 `QUOTA` 就结束，或者收到 `allowed` 信号 | `open` | 放行其余等待者 |
| `probing` | 探测运行又是 `QUOTA` | `frozen` | 新的 `until`；没有重置时间时 `k + 1` |
| `probing` | 探测运行因别的原因失败 | `probing` | 下一个等待者当探测 |
| `frozen` | 冻结之后开始的运行收到 `allowed` 信号（例如你换了账号登录） | `open` | |
| `frozen` | 操作者清除（`providers.limits_clear`） | `open` | 记 `actor = operator` |

- `resets_at` 要在 `(now, now + 8 天]` 里，否则当作没有。
- 状态写进 `provider_admission`，转换只在单个进程的锁里做，每次转换在 `provider_admission_events` 记一行；重启后读回，所以重启不会让恢复的任务一起再撞一次 429。
- Codex 账号到期后用 app-server 的 `account/rateLimits/read` 探测，不花模型调用（D16）；开工前验证这个请求可用（第 14 节验证 2），用不了就照上表放行等得最久的一个运行。

### 5.4 接入点

| 运行 | 接在哪里 |
|---|---|
| Worker、Reviewer | `WorkerPool._take_slot` 之后、`handle.launch()` 之前；备用 Profile 换了账号时重新准入 |
| Planner 回合 | `Planner._run_turn` 开头 |
| Manager 回合 | `Manager._run_turn` 开头（只接线，逻辑在新模块） |
| processor | `processor.run` |
| consult、advisor、judge | `spawn.run_child`、`consult.py` 的启动处；嵌套时借父槽位 |

接口：`async with admission.slot(provider_id, lane, owner=..., parent=None) as grant:`，在 `looper/agents/admission.py`。关着时它直接放行，不读数据库。

### 5.5 你看到什么

- 在等槽位的运行：任务活动「等 <账号> 的槽位（<n> 个在跑，上限 <m>）」，和今天「Worker waits for room in this task」同一种写法；
- 账号冻结：任务活动「<账号> 额度已用完，<本地时间> 重置，到时继续」；Dashboard 的「Agents running now」旁边一行；通知你一次（走 advisor 告警的同一条路径：`gate_service.dashboard_target` 指向的会话，没有就只进日志；每次冻结一条）；
- Settings → LLM Providers 的账号行：状态、重置时间、在等的数量，和「现在重新检查」（操作者清除）。

## 6. 用量和限流信号

### 6.1 来源

| 运行时 | 解析什么 | 窗口名 |
|---|---|---|
| Claude Code | `rate_limit_event.rate_limit_info` 的每个 `unifiedWindows` 项和顶层 `rateLimitType`；`result` 的 `api_error_status`、`api_error` | 原样用 `rateLimitType`（实测只见过 `seven_day`） |
| Codex app-server | `account/rateLimits/updated` 的 `primary`、`secondary`（稀疏更新，合并进上一次的快照） | `primary`、`secondary`（带 `windowDurationMins`） |
| API 运行时 | 429 的 `retry-after`；限流响应头（第一期只读 `retry-after`） | `requests` |
| 本机代理 | 不用（第 2.4 节）；以后覆盖了再加 `source = proxy` | — |

状态统一成 `allowed`、`warning`（Claude Code 的 `allowed_warning`）、`rejected`。解析在运行时的事件解析里做（不是事后读 `llm_record_events`），这样 processor 这类只留类型化事件的运行也能拿到（需要验证它的原始输出里有没有这条事件）。

### 6.2 汇总

- **当前窗口：** `provider_limit_windows`，每个账号每个窗口一行，按 `observed_at` 只保留最新的。
- **用量：** 从 `llm_records` 按账号和天汇总运行数、token、花费、`QUOTA` 失败数，不另存（账号取 `metadata.profile.provider.id`，加一个表达式索引）。订阅账号的花费是运行时按 API 价报告的折算值，界面上写明「按 API 价折算」。
- **读接口：** `providers.limits {provider_id?}`：每个账号的准入状态、当前窗口、最近 7 天按天的用量和 `QUOTA` 次数；生成的 `loopercli` 命令随之有。只读，操作者权限。
- **展示：** Settings → LLM Providers 的账号行展开后一块「额度」；自检加一行「<账号> 本周已用 <x>%」，`utilization ≥ 0.9` 为 WARN。

## 7. 降级规则类型化

- `agent_profiles.fallback_on TEXT[] NOT NULL DEFAULT '{launch_failure}'`，取值 `launch_failure`（今天的触发条件）、`quota`、`provider_fault`。默认值就是今天的行为。
- `metadata.fallback[].reason` 改成类型化的 `trigger`，原来的文字留在 `detail`。
- **供应商故障：** 同一账号 10 分钟内来自至少 3 个运行的 `TRANSIENT` 失败达到 5 次，账号标 `degraded` 10 分钟（`provider_admission.state`，不影响放行，只用于降级和告警）。
- **谁能自动切：**

| 角色 | `quota` / `provider_fault` 触发时 |
|---|---|
| processor、订阅、以后的后台角色 | 按 Profile 的 `fallback_on` 自动切，记活动 |
| Worker、Reviewer | 同上（和今天的启动失败切换一样记日志、不占重试，D14） |
| Planner | 不自动切（D15）：步骤或回合挂起，通知你「<账号> 额度已用完，要换到 <备用 Profile> 继续吗」，两个动作：换（这个任务后面的 Planner 回合都用备用）、等重置（默认） |
| Manager | 不自动切：第 4.4 节的回复里带「换到 <备用 Profile>」的选项，你确认后这个会话换 Profile（现有的 `manager_session.select_profile`） |

- 常驻会话绑定运行时和账号（`agent_sessions.runtime_type`、`provider_id`），换到别的账号会新开会话，带现有的续接摘要。

## 8. 数据模型

全部是新增，可以在线执行。

```sql
-- 运行的失败类型（失败运行才有值）
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS failure_class TEXT;
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS failure_cause TEXT;
-- 约束走 _ONLINE_STEPS：先 NOT VALID，再 VALIDATE
ALTER TABLE llm_records ADD CONSTRAINT llm_records_failure_class_check
    CHECK (failure_class IN ('quota', 'transient', 'environment', 'judgment')) NOT VALID;
ALTER TABLE llm_records ADD CONSTRAINT llm_records_failure_pair_check
    CHECK ((failure_class IS NULL) = (failure_cause IS NULL)) NOT VALID;
CREATE INDEX IF NOT EXISTS idx_llm_records_provider_day
    ON llm_records ((metadata->'profile'->'provider'->>'id'), created_at);   -- 在线建

-- 账号的当前限流窗口
CREATE TABLE IF NOT EXISTS provider_limit_windows (
    provider_id     TEXT NOT NULL REFERENCES llm_providers(id) ON DELETE CASCADE,
    window_name     TEXT NOT NULL CHECK (window_name ~ '^[a-z0-9_]{1,40}$'),
    status          TEXT NOT NULL CHECK (status IN ('allowed', 'warning', 'rejected')),
    utilization     DOUBLE PRECISION CHECK (utilization BETWEEN 0 AND 1),
    resets_at       TIMESTAMPTZ,
    window_minutes  INTEGER CHECK (window_minutes > 0),
    source          TEXT NOT NULL CHECK (source IN ('claude_stream', 'codex_app_server', 'api_headers', 'proxy')),
    record_id       BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    observed_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (provider_id, window_name)
);

-- 账号的准入状态
CREATE TABLE IF NOT EXISTS provider_admission (
    provider_id      TEXT PRIMARY KEY REFERENCES llm_providers(id) ON DELETE CASCADE,
    state            TEXT NOT NULL CHECK (state IN ('open', 'frozen', 'probing', 'degraded')),
    reason           TEXT CHECK (reason IN ('quota', 'provider_fault')),
    until            TIMESTAMPTZ,
    until_known      BOOLEAN NOT NULL DEFAULT true,
    backoff_step     INTEGER NOT NULL DEFAULT 0 CHECK (backoff_step >= 0),
    record_id        BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,  -- 引起这次状态的运行
    since            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((state = 'open') = (reason IS NULL)),
    CHECK (state NOT IN ('frozen', 'degraded') OR until IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS provider_admission_events (
    id           BIGSERIAL PRIMARY KEY,
    provider_id  TEXT NOT NULL REFERENCES llm_providers(id) ON DELETE CASCADE,
    from_state   TEXT NOT NULL,
    to_state     TEXT NOT NULL,
    reason       TEXT,
    until        TIMESTAMPTZ,
    record_id    BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    actor        TEXT NOT NULL CHECK (actor IN ('code', 'operator')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);  -- 保留 90 天，随每小时的清理删除

-- 步骤的失败和挂起
CREATE TABLE IF NOT EXISTS step_failures (
    id             BIGSERIAL PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id        TEXT NOT NULL REFERENCES task_steps(id) ON DELETE CASCADE,
    record_id      BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    role           TEXT NOT NULL CHECK (role IN ('worker', 'reviewer')),
    failure_class  TEXT NOT NULL CHECK (failure_class IN ('quota', 'transient', 'environment', 'judgment')),
    failure_cause  TEXT NOT NULL,
    streak         INTEGER NOT NULL CHECK (streak >= 1),
    action         TEXT NOT NULL CHECK (action IN ('hold', 'planner', 'breaker')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_step_failures_step ON step_failures (step_id, id);
CREATE TABLE IF NOT EXISTS step_holds (
    step_id      TEXT PRIMARY KEY REFERENCES task_steps(id) ON DELETE CASCADE,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('quota', 'transient', 'environment')),
    cause        TEXT NOT NULL,
    provider_id  TEXT REFERENCES llm_providers(id) ON DELETE SET NULL,
    until        TIMESTAMPTZ,
    failure_id   BIGINT NOT NULL REFERENCES step_failures(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((kind = 'environment') = (until IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_step_holds_task ON step_holds (task_id);

-- 任务的结束原因、备用 Profile 的触发条件
-- tasks_fail_kind_check 换成 ('task_budget', 'quota_exhausted', 'environment')，走 _ONLINE_STEPS
ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS fallback_on TEXT[] NOT NULL DEFAULT '{launch_failure}'
    CHECK (fallback_on <@ ARRAY['launch_failure', 'quota', 'provider_fault'] AND cardinality(fallback_on) >= 1);
```

- 表名、列名以实现 PR 为准；`schema.sql` 是冲突热点，每个 PR 的迁移都有名字，主干先合了别的迁移就合并主干、重新排序，不 rebase。
- `llm_records` 加的是可空列，不重写；约束用 `NOT VALID` 加 `VALIDATE`；表达式索引在线建。
- 配置导出（`looper-config`）带上 `fallback_on`；导入旧文件时缺省为 `{launch_failure}`。

## 9. 设置

| 设置 | 默认 | 取值 | 管什么 | PR |
|---|---|---|---|---|
| `engine.failure_typing` | `off` | `off`、`record`、`act` | `record`：只分类和记录；`act`：按第 4 节处理 | PR1（`record`）、PR4（`act`） |
| `engine.limit_signals.enabled` | `false` | 布尔 | 解析和保存限流窗口，展示用量 | PR2 |
| `engine.admission.enabled` | `false` | 布尔 | 执行账号槽位、车道和冻结 | PR3 |
| `llm_providers.max_concurrency` | NULL（已有） | 1 及以上 | 账号的槽位上限，准入打开时才生效 | PR3 |
| `agent_profiles.fallback_on` | `{launch_failure}` | 见第 7 节 | 备用 Profile 的触发条件 | PR5 |

- 前三个是系统设置（`system_settings`），在 Settings 和 Manager 的 `looper_settings`（`view` 能读；改动要你确认）里，和其他系统设置一样；后两个是账号和 Profile 上的字段，在 Settings → LLM Providers 和 Agent Profiles 里改，随配置导出导入。
- 阈值第一期是常量，不做设置：熔断 3、退避 2/10 分钟、冻结余量 60 秒、无重置时间时的退避 5→60 分钟、提前间隔 5 分钟、`INTERACTIVE` 保护 2 分钟、后台最长等待 6 小时（定时任务建出的任务取它和一个周期中小的）、后台收紧 `utilization ≥ 0.95`、供应商故障 5 次/3 个运行/10 分钟。
- **关闭时：** 提示词、工具列表、命令行、运行记录的内容和任务行为逐字节不变；新列为 NULL，新表为空。每个 PR 有测试证明。
- **运行中关掉：** 准入关掉时立即放行所有等待者；失败处理从 `act` 改回别的值时，已有的挂起立即解除并按今天的路径交给 Planner（不会有步骤永远挂着）。

## 10. 失败语义（这一层自己出问题时）

| 情况 | 行为 | 理由 |
|---|---|---|
| 分类抛异常 | 当 `JUDGMENT`，记 ERROR 日志 | 退回今天的路径 |
| 限流信号解析失败 | 忽略这条信号，计数加一，不改状态 | 信号只是辅助 |
| 准入状态写库失败 | 内存里的状态照常生效，记 ERROR；重启后可能丢，下一次 429 会重新冻结 | 不让数据库问题挡住运行 |
| 准入控制器内部出错 | 放行（fail-open），记 ERROR | 可靠性层不能成为新的单点 |
| 冻结误判（其实没用完） | 到 `until` 自动探测；你可以随时清除；冻结之后开始的运行收到 `allowed` 也会解冻 | 误冻结最多等到重置 |
| 漏判（额度用完但分成了别的类） | 走今天的路径；熔断在 3 次后停住自动处理 | 熔断就是为这个设的 |
| `resets_at` 不可信（过去、超过 8 天） | 当作没有，走退避 | |
| 挂起的步骤所在任务被停止、取消、重新规划 | 挂起删除 | 和步骤的生命周期一致 |
| 服务重启 | 准入状态读回；内存里的等待队列由恢复路径重建，走 `RECOVERY` 车道 | |

## 11. 指标和告警

| 指标 | 口径 | 基线（第 2.5 节） | 看哪里 |
|---|---|---|---|
| 交给 Planner 的非判断类失败占比 | 进 `[DECISION]` 的失败结果里 `failure_class ≠ judgment` 的占比（不含 Looper 自己停的和评审结论） | 50%（101/202） | `providers.limits` 旁的统计接口、Dashboard |
| 429 连锁失败 | 账号已有一个带重置时间的 `rejected` 之后、重置之前又以 `QUOTA` 失败的运行数 | 80 次 | 同上 |
| 因额度失败的任务 | `fail_kind = quota_exhausted` 加上规划时撞额度的任务 | 56 个 | 同上 |
| 失败按类、原因、运行时、角色、天 | `llm_records.failure_class/cause` | — | 同上 |
| 准入等待 | 每个账号、每个车道的等待数和等待时长 P50/P95，提前次数，超额放行次数 | — | Settings → LLM Providers、自检 |
| 冻结 | 次数、时长、探测次数和结果 | — | 同上，`provider_admission_events` |
| 熔断 | 每天打开次数 | — | 同上 |
| 降级 | 每天按 `trigger` 和角色的切换次数 | 0 | 同上 |
| 额度用量 | 每个账号每个窗口的 `utilization` 和信号新鲜度 | — | 同上 |

告警（和 advisor 的告警同一条路径，`advisor_alerts` 那样按类去重，重启不重发）：

- 账号冻结：每次冻结一条，写重置时间和在等的数量；
- 账号某个窗口 `utilization ≥ 0.9`：每个窗口每个周期一条；
- 环境问题挂起超过 1 小时：每个步骤一条提醒；
- 熔断一天超过 5 次、`INTERACTIVE` 超额放行一天超过 3 次：自检 WARN。

## 12. 入口

| 入口 | 变化 |
|---|---|
| Dashboard | 运行卡片显示失败类；任务活动写挂起和重派；「需要你」分组出现环境问题挂起，带三个动作；Settings → LLM Providers 的账号行有额度、准入状态、槽位上限和「现在重新检查」 |
| 飞书、Telegram | 冻结和高用量的告警发到告警会话；环境问题挂起的通知发任务的来源会话，回复「重试」「交给 Planner」「停止」由 Manager 的工具处理（PR4 加 `looper_task_hold` 工具，关闭时不出现在工具列表里）；Manager 在冻结时的回复和换 Profile 的询问（PR3、PR5） |
| CLI | 新的接口（`providers.limits`、`providers.limits_clear`、`tasks.hold_release`）自动生成 `loopercli` 命令 |

## 13. PR 拆分

| PR | 内容 | 开关 | 依赖 | 模型可见 |
|---|---|---|---|---|
| PR1 失败分型（只记录） | `failures.py`；运行时钩子（Claude Code、Codex 两条路、API）；`codex_steer.py` 保留 `codexErrorInfo`；`llm_records.failure_class/cause`；运行卡片显示类；统计接口 | `engine.failure_typing = record` | — | 否 |
| PR2 用量和限流信号 | 解析 `rate_limit_event`、`account/rateLimits/updated`；`provider_limit_windows`；`providers.limits`；LLM Providers 的额度块；自检一行 | `engine.limit_signals.enabled` | — | 否 |
| PR3 全局准入和冻结 | `admission.py`；`provider_admission(_events)`；执行 `max_concurrency`；车道、提前、后台收紧、冻结、探测；接入点；`providers.limits_clear`；冻结通知和 Manager 的冻结回复 | `engine.admission.enabled` | PR1、PR2 | Manager 的冻结回复是代码写的固定文字，不进模型上下文 |
| PR4 失败处理和熔断 | `step_failures`、`step_holds`；Worker/Reviewer 的路由、重派说明；Planner、processor 回合的挂起和重试；环境问题挂起、`tasks.hold_release`、`looper_task_hold`；`tasks.fail_kind` 新值；`[DECISION]` 消息里的失败历史 | `engine.failure_typing = act` | PR1（准入可有可无：没有准入时额度挂起按 `resets_at` 等） | 是：重派说明、`[DECISION]` 的失败历史、新工具；跑真实评测 |
| PR5 降级规则类型化 | `agent_profiles.fallback_on`；`metadata.fallback[].trigger`；`degraded` 状态；后台和 Worker/Reviewer 的自动切换；Planner、Manager 的询问 | `fallback_on` 默认值 | PR1、PR3、PR4 | 否（询问是代码写的） |

- PR1 和 PR2 可以并行。新逻辑放在新模块；`manager.py` 只接线，PR 描述写明改了哪几行。
- 本 PR 不改 `docs/plans/…` 的台账；第 5 节待办状态随实现 PR 改。

## 14. 测试与验证（给实现 PR）

- **先写测试：** 每条分类规则一条录制样本（Claude Code 的 429 流、`connection_failed` 流、较早版本没有 `api_error` 的 429 流；Codex app-server 的 `usageLimitExceeded`、`serverOverloaded`、`account/rateLimits/updated`；Codex exec 的文字；Looper 自己的启动异常），样本脱敏后放 `tests/fixtures/`。
- **关闭时逐字节不变：** 每个 PR 一条测试：开关关着，Planner/Worker/Manager 的提示词、工具列表、命令行和运行记录与主干相同。
- **准入：** 车道顺序、提前、嵌套借槽位、排空、保留运行接管、冻结的每条转换、重启读回、运行中关闭开关时放行全部。
- **真实路径：** 一次性 `looper serve`（空闲端口、新库、一次性 runtime root），用替身运行时发出 `rate_limit_event` 和 429 结果，验证冻结、探测、解冻和挂起重派；真实额度没法安全地触发，这一点写进 PR 的未测项。
- **真实评测：** PR4 改了模型可见内容，用真实 Claude CLI 跑一次评测对比（`evals/README.md`）。
- **开工前验证：**
  1. processor 的原始输出里有没有 `rate_limit_event`（只留类型化事件，记录里看不到）。**PR1 的结论（读代码）：** 记录里看不到是 `processor.typed_events` 只留 `system` 和 `result` 事件；`ClaudeRunRecorder.account()` 在这个过滤之前看到每一个事件，所以只要 Claude Code 发了，失败分型和以后的限流信号都拿得到（有测试）。没有工具的运行里 Claude Code 发不发这条事件，录制样本里没有（样本早于这条事件），需要一次真实运行确认，留给 PR2；
  2. Codex app-server 是否真的发 `account/rateLimits/updated`、`resetsAt` 的单位、`account/rateLimits/read` 能否在不花模型调用的情况下当探测；
  3. Codex exec 和 Grok 额度用完时的报错文字。**PR1 的结论（读二进制里的字符串，不触发真实额度）：** codex-cli 0.169.0 的文字是「You've hit your usage limit…」「Quota exceeded. Check your plan and billing details.」「exceeded retry limit, last status: 429 …」「Selected model is at capacity…」「stream disconnected before completion…」，PR1 按这些分类（`signal: text`）。Grok 的二进制和录制样本里没有找到稳定的额度提示，PR1 不为 Grok 分类（`unknown`）；
  4. Claude Code 的 `rateLimitType` 还有哪些值（是否有按模型的窗口）。

## 15. 维护者的答复（2026-11-20）

本节保留问题、选项和当时的推荐。Q1–Q11 全部按推荐答复，记为第 3 节的 D9–D19。

| 编号 | 问题 | 选项 | 当时的推荐 → 答复 |
|---|---|---|---|
| Q1 | 冻结和槽位的粒度 | (a) 整个账号；(b) 账号下按窗口或模型；(c) 再加一层同 `vendor_type` 的合计 | **(a)**。线上只见过 `seven_day` 一个窗口；按模型的窗口观测到再拆（第 14 节验证 4）。(c) 没有调用方 → **答复：** 按推荐（D9） |
| Q2 | 用量接近上限时要不要提前收紧 | (a) 不收紧，只告警；(b) 某个窗口 `utilization ≥ 0.95` 时暂停 `BACKGROUND` 车道，其他车道照常 | **(b)**，随 PR3，准入打开时生效。11-11 那次 6 个同时在跑的 Worker 一起被截断，冻结挡不住，只有提前收紧后台才能少丢工作 → **答复：** 按推荐（D10） |
| Q3 | 冻结时你给 Manager 发消息 | (a) 代码立刻回一句（重置时间、可选的备用 Profile），消息排队，重置后照常回答；(b) 回一句，消息丢弃，你重发 | **(a)**。不丢消息；你可以用停止取消 → **答复：** 按推荐（D11） |
| Q4 | 冻结期间定时任务建出的任务 | (a) 一直等；(b) 最多等 6 小时（或定时任务的一个周期，取小的），超过以 `quota_exhausted` 结束；(c) 冻结时定时任务直接跳过触发 | **(b)**。每半小时的汇报等 3 天没有意义；(c) 要改定时任务的触发逻辑，影响面大 → **答复：** 按推荐（D12） |
| Q5 | 环境问题挂起是否受 `user_gates` 开关限制 | (a) 不受，所有项目和定时任务都进「需要你」；(b) 只在打开了用户关卡的地方，其他地方直接交给 Planner | **(a)**。它是系统挂起，不是 Planner 的讨论关卡；交给 Planner 只会让模型猜 → **答复：** 按推荐（D13） |
| Q6 | Worker、Reviewer 算不算可以自动切换的「后台角色」 | (a) 跟 Profile 的 `fallback_on` 走，默认只有 `launch_failure`；(b) 一律不自动切；(c) 一律自动切 | **(a)**。默认和今天一样，操作者在 Profile 上勾了才切 → **答复：** 按推荐（D14） |
| Q7 | Planner 切换前怎么「告诉你」 | (a) 通知后等你确认（默认等重置）；(b) 通知后自动切 | **(a)**。D6 说不悄悄换；夜里没人确认时就是等重置，和不切一样 → **答复：** 按推荐（D15） |
| Q8 | Codex 冻结到期后怎么探测 | (a) `account/rateLimits/read`，不花模型调用；(b) 放行等得最久的一个运行 | 能用 **(a)** 就用（第 14 节验证 2），否则 **(b)** → **答复：** 按推荐（D16） |
| Q9 | 准入打开但 `max_concurrency` 为空 | (a) 不限，只有冻结和后台收紧生效；(b) 给订阅账号一个内置默认值 | **(a)**，不悄悄改并发；文档建议 Claude 订阅设 10（线上 P95 8、P99 13） → **答复：** 按推荐（D17） |
| Q10 | 瞬时故障重派时给 Worker 的说明 | (a) 一行代码写的说明（类、原因、工作目录可能有改动）；(b) 不加说明，指令完全相同 | **(a)**。重派的 Worker 看到上一次留下的改动时知道为什么；走真实评测 → **答复：** 按推荐（D18） |
| Q11 | 启动超时（keeper 10 秒没打开控制 socket）算瞬时还是环境 | (a) 瞬时；(b) 环境 | **(a)**。线上 54 次集中在几个小时里，是本机繁忙时的启动竞争，重试能过；一直失败时熔断会交给 Planner → **答复：** 按推荐（D19） |

## 16. 风险

| 风险 | 怎么发现 | 怎么控制 |
|---|---|---|
| 误冻结，任务白等 | 冻结次数、探测结果、`frozen` 时收到的 `allowed` | 到期自动探测；你可以随时清除；`allowed` 信号解冻 |
| 文字规则误判 | `signal = text` 的分类数、熔断次数 | 只在没有类型化字段时用；每条有样本测试；熔断兜底 |
| 槽位设小了，前台变慢 | `INTERACTIVE` 等待 P95、超额放行次数 | 你在等的回合有 2 分钟保护；上限默认不限 |
| 自动重派重复做事 | 重派次数、熔断次数 | 每个步骤最多自动处理到 `streak` 3；重派说明告诉 Worker 工作目录里可能有改动 |
| 挂起的步骤被忘掉 | 环境问题挂起时长 | 1 小时提醒；任务在「需要你」分组 |
| 运行时升级改了事件字段 | 信号新鲜度、`signal = text` 占比上升 | 字段缺失时退回文字规则或 `UNKNOWN`，行为退回今天 |
