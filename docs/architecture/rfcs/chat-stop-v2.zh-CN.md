# RFC：停止管家的回复 v2：整轮一个停止令牌

- **状态：** 已实现（`chat.stop_enabled` 后面，默认仍关；第 10 节的标准满足后才默认打开）。实现卡片在 `.lead/stopv2/`（本仓库之外），按第 9 节的顺序做。第 6.3 节「需要验证」的几项尚无真实路径报告（卡片 14 的脚本是 `scripts/` 里的真实路径脚本，结论待跑出后补上），仍标「需要验证」。
- **取代：** [聊天输入框 v0](chat-composer-v0.zh-CN.md) 的 D3、D4、D7 和第 8 节的停止矩阵。v0 的 D1、D2、D5、D6、D8、D9（暂存、键位、`GET /api/chat/turn`、`outcome`、`/stop` 识别、历史）不变，下文只写改了什么。
- **代码基线：** v2.13.15（`349c4e11e`），停止在 `chat.stop_enabled` 后面，默认关。
- **目标：** 停止做到「构造上正确」，然后按第 10 节的标准把 `chat.stop_enabled` 默认打开。

## 1. 为什么要 v2

v0 合并前评审了八轮，每轮修掉一个窗口，下一轮又找到一个：

| 轮次发现的窗口 | 根因 |
|---|---|
| 轮登记之前（持久化用户消息、发「thinking」、MCP 探测、`/gates` 和门引用查找、读设置）来的停止答 `not_running`，模型照样启动 | `self._current` 在 `_handle_user_message` 中途才设（`manager.py:285`），之前的 `await` 都不在保护内 |
| 进程起来之前、第一个事件之前的中断丢失 | 中断是「对当前进程做一个动作」（`interrupt()`），没有进程时只能靠各处的 `_stopping` 标志补洞（`per_turn.py:196`、`:208`，`grok_acp.py:686`、`:718`） |
| 自己开进程组的工具进程活下来 | 中断前没有给进程树拍快照，Claude 先退出后子进程被重新挂到 1 号进程 |
| 句柄清掉后对 stdout/stderr 的无界读取 | `stderr_tail`（`per_turn.py:50`）、`proc_tree.end` 之后的 `process.wait()` 没有上限 |
| 停止之后队列里的结果还被当成回答处理 | 读循环在停止后还把事件交给 `on_event` |
| Grok ACP 退回每轮进程时停止丢失 | 停止是 `AcpGrok` 的字段，`ResidentGrok` 不知道 |
| 被停那一轮迟到的 MCP 工具请求算到下一轮头上 | `ManagerOps.call` 按**到达时**的 `self._turn` 归属（`service.py:201`） |

共同点：停止是在若干**点**上检查的，而不是这一轮从第一刻起就带着的**属性**。每加一个 `await`、一个运行时分支、一次重试，就多一个没检查的点。

v2 的做法：消息一到就生成一个令牌（第一个 `await` 之前），整轮所有阶段、所有运行时、所有工具调用都拿着同一个令牌；停止只是把令牌置位，各处通过「注册回调、已置位就立即触发」拿到它，不存在「检查之前」这个时刻。

## 2. 决定一览

| # | 决定 | 落点 |
|---|---|---|
| D1 | 每条发给管家的用户消息在**总线投递时**（同步，入队之前）得到一个 `TurnToken`，登记在管家的 `TurnRegistry`；有类型的阶段 `received → preparing → starting → running → stopping → ended`，转移只能经 `advance()` | 新模块 `looper/agents/tiers/turn_token.py`；`manager.py:_claim_stop`（569）改为 `_receive` |
| D2 | 停止 = `token.request_stop(via)`：同步、幂等，任何非终止阶段都接受；排队中的消息也能停（取消 v0 D7）。停止只在有中断的运行时上提供（Claude Code、Grok ACP，`can_interrupt`）；每轮一个进程的运行时拒绝停止（6.6） | `ManagerAgent.stop_turn` |
| D3 | 准备阶段每个 `await` 都经 `token.step()`（与停止赛跑，可放弃）或 `token.keep()`（必须完成、有上限，完成后再看停止）；一条 AST 测试钉住「准备函数里没有别的 `await`」，故障注入测试逐个步骤停止 | `manager.py` 拆成 `_prepare_turn`/`_start_turn`/`_run_model`/`_end_turn` |
| D4 | 运行时只认一个取消原语 `TurnCancel`，作为 `run_turn(..., cancel=)` 传入；`on_request(cb)` 在已置位时立即调用，所以「起进程之前」「第一个事件之前」「退回旧路径时」都不需要特判 | 新模块 `looper/agents/runtimes/cancel.py`；四条运行时路径 |
| D5 | 每轮一个 `TurnTree`：进程起来时拍快照，每个事件和每 0.5 秒刷新，清理按快照的并集（pid + 启动时间）结束；所有等待和读取都有上限 | `proc_tree.py` |
| D6 | 工具调用归属于发出它的那一轮：桥接进程每次 `tools/call` 读一个固定路径的绑定文件，带 `X-Looper-Turn` 头；常驻运行时的写操作另外要和这一轮流里的 `tool_use` 对上；已结束或已停止的轮的请求一律拒绝，确认也一样 | `mcp_server.py`、`ops/service.py`、`http_server.py:_handle_agent_api` |
| D7 | 记录：`interrupted` + `user_stop{via, phase, method, requested_at, usage_complete}`；模型没看到消息时下一轮得到「撤回」说明，看到了得到「被停」说明；会话 id 不清，可续接 | `manager.py`、`claude_cli.py:ClaudeRunRecorder` |
| D8 | 停止矩阵（运行时 × 阶段/窗口）作为常驻测试，加准备阶段的故障注入；真实路径在 Claude Code 和 OMP + DeepSeek 上各跑一遍；满足第 10 节标准才默认打开 | `tests/`、`evals/stop_realpath.py` |

## 3. 令牌与状态机（D1、D2）

### 3.1 何时生成

`ManagerAgent.start()` 已经用 `bus.subscribe_topic(USER_MESSAGE, self._claim_stop, consumes=True)` 在投递时同步截取 `/stop`（`bus.py:167` `_hand_to_consumer` 在入队前同步调用）。v2 把它改成 `_receive(envelope) -> bool`：

- 目标不是管家：`False`。
- 文字正好是 `/stop`：照旧认领，起任务处理（D2 的渠道语义见 3.4），`True`。
- 其他：`self._turns.receive(envelope)` 生成并登记令牌（阶段 `received`），返回 `False`，消息照常入队。

这一步在 `bus.publish` 里、`POST /api/chat/send` 返回之前完成，也在日志重放（重启后重新投递未送达的消息）时完成。所以页面收到 send 的应答之后发的任何停止，都能找到令牌。从此没有「轮登记之前」。

### 3.2 `TurnToken`

```python
class TurnPhase(str, Enum):
    RECEIVED = "received"      # 已投递，在管家队列里
    PREPARING = "preparing"    # 被 _message_loop 取出：持久化、命令、门、探测、读设置、简报
    STARTING = "starting"      # 起/换运行时：交接、启动进程、写 MCP 绑定；提示还没写给模型
    RUNNING = "running"        # 提示已写给运行时（模型看到了这条消息）
    STOPPING = "stopping"      # 用户要求停止，各处正在收尾
    ENDED = "ended"            # 结局已定（回答、失败、停止、由代码回答的命令）

@dataclass(eq=False)
class TurnToken:
    message_id: str; client_message_id: str | None; channel: str; chat: str
    received_at: datetime
    binding: str               # secrets.token_urlsafe(18)，D6 的工具调用绑定 id
    phase: TurnPhase = RECEIVED
    stop: StopRequest | None   # via, at, phase（停止到达时的阶段）
    cancel: TurnCancel         # 给运行时的那一个原语（D4）
    ops_turn: Turn | None      # ManagerOps 的 Turn
    kind: TurnKind = MODEL     # MODEL | CODE（/new、/gates、档案命令、交给门的回复）
    outcome: ReplyOutcome | None
```

转移表（`advance(to)` 之外不许改 `phase`，非法转移抛 `AssertionError`）：

| 从 | 到 | 由谁 |
|---|---|---|
| received | preparing | `_message_loop` 取出 |
| preparing | starting | 准备完，开始起运行时 |
| starting | running | 运行时写出提示（`cancel.sent()`，见 4.1） |
| received/preparing/starting/running | stopping | `request_stop` |
| 任何非 ended | ended | `settle(outcome)` |

两条规则让每个阶段都检查令牌，而不是靠调用方记得：

1. `advance()` 遇到 `stopping` 时不转移，抛 `TurnStopped`。所以每次阶段转移本身就是检查点。
2. `settle(outcome)` 是同步的，并且只在**没有 await 隔开**的情况下和最后一次 `cancel.requested` 判断写在一起（`_end_turn` 里）；`stop_turn` 对 `ended` 答 `not_running`。v0 的 `settled` 布尔量（`manager.py:398`）并入 `ended`。

`kind = CODE` 的消息（准备阶段分类出来的 `/new`、`/gates`、档案命令、门路由）在**动手之前**最后检查一次令牌，然后 `settle(ANSWERED)`：动手之后的停止答 `not_running`，和 v0「不停止 `/new`」一致；动手之前的停止让它什么都不做，回 `已停止，这次没有回复。`。

### 3.3 `TurnRegistry` 和 `stop_turn`

- `TurnRegistry`：按 `message_id` 和 `client_message_id` 两个键查活着的令牌（非 `ended`），保持投递顺序；`settle` 时移除。`ManagerAgent.stop()` 清空并把仍在 `received` 的令牌结束为停止。
- `stop_turn(message_id, *, via) -> StopResult`，全同步部分在第一个 `await` 之前做完：
  1. 查不到或已 `ended`：`NOT_RUNNING`；已 `stopping`：`ALREADY_STOPPING`。
  2. `token.request_stop(via)`：记 `StopRequest(via, at, phase)`，阶段 → `stopping`，`token.cancel.request()`（运行时的回调立即触发），`ops.stop_turn(token.ops_turn)`（已在 v0，`service.py:318`）。
  3. `bus.settle(message_id)`：把信封在日志里标为已送达，重启后不会重放一条用户已停止的消息（`bus.py:55` `mark_delivered` 的公开包装）。
  4. 然后才 `await` 发「Stopping…」状态；它慢也没关系：停止已经属于这个令牌，不会落到下一轮（v0 那个 `test_a_stop_whose_status_is_slow_never_interrupts_the_next_turn` 的问题在构造上消失）。
- `StopResult` 去掉 `OTHER_MESSAGE` 的产生路径：每条消息都有自己的令牌，停的是哪条就是哪条。API 继续接受和文档化这个值一个版本（前端已处理），不再返回。
- 排队中的消息（`received`）也能停：`_message_loop` 取出时 `advance(PREPARING)` 就抛 `TurnStopped`，持久化用户消息后回停止回复，`outcome: stopped`。这取代 v0 D7「不停止尚未开始的消息」：前端不用改（它照常等 `answer.final`），v0 D7 那句「The Manager is answering another message first」不再出现。

### 3.4 渠道与 API

- `POST /api/chat/stop`：200 `{"state": "stopping"|"already_stopping", "phase": "<停止到达时的阶段>", "method": null}`。`method` 在 200 时一般还不知道（中断在后台进行）；`GET /api/chat/turn` 在知道后报出。409 `not_running`、`disabled` 不变；管家的运行时没有中断时 409 `cannot_interrupt`（「This runtime cannot interrupt a reply.」，6.6）。
- `GET /api/chat/turn?session_id=`：返回这个 web 聊天里**最早的**活令牌（正在答的，或排队中的），多一个 `state` 字段（阶段名）、`method` 和 `can_interrupt`（管家的运行时能否中断回复）。`/api/registry` 的 `runtime_types[].can_interrupt` 是运行时类型的同一能力（`RuntimeAdapter.can_interrupt`）。页面在 `can_interrupt` 为假时把停止按钮显示为禁用，提示同一句话，Esc 不做事。
- Telegram、飞书 `/stop`：停这个聊天的**所有**活令牌（正在答的和排队的）。回复：停了 1 条 → `已停止当前回复。`；还取消了排队的 → `已停止当前回复，另取消了 N 条排队的消息。`；没有 → `现在没有正在进行的回复。`；运行时没有中断 → `当前运行时不能中断回复，回复会照常完成。`，什么都不停。
- `chat.stop_enabled` 关闭时：令牌照常生成（无副作用），停止入口照 v0 拒绝。

## 4. 运行时：一个取消原语（D4、D5）

### 4.1 `TurnCancel`

```python
class TurnCancel:
    requested: bool                       # 只读
    def request(self) -> bool: ...        # 同步、幂等；第一次返回 True，并同步依次调用回调
    def on_request(self, cb: Callable[[], None]) -> Callable[[], None]:
        """注册回调；已经请求过就立刻调用。返回注销函数。"""
    async def race(self, aw: Awaitable[T], *, timeout: float | None = None) -> T:
        """等 aw，停止先到就取消它并抛 TurnCancelled；超时抛 TimeoutError。"""
    def sent(self) -> None: ...           # 运行时写出了提示：令牌 starting → running
    prompt_sent: bool
```

- 回调必须是同步的；需要 `await` 的收尾由回调起一个任务，运行时把任务留着，`run_turn` 返回前**有上限地**等它。
- 不传 `cancel=` 的调用方（规划者、交接以外的内部轮）拿到一个永不请求的实例，行为不变。
- `ResidentSession.interrupt()` 保留为薄包装：「请求当前这一轮的 cancel，等它结束（有上限），返回方法」，供现有测试和规划者以后使用；管家不再调用它。
- `ResidentTurn` 加 `prompt_sent: bool`。`stopped: StopMethod | None` 在 `cancel.requested` 且运行时据此结束时必有值。
- 统一规则 R1：**请求之后，运行时不再把这一轮的任何事件交给 `on_event`**，结果事件只在内部用来取用量。这一条关掉「停止之后队列里的结果被当成回答」。
- 统一规则 R2：**`on_request` 在每个阶段入口注册**（起进程前、起进程后、写提示前、读循环开始前、退回旧路径时）。因为已请求时立即触发，注册得晚也不丢。

### 4.2 `TurnTree`（进程树）

`proc_tree.track(process, *, label) -> TurnTree`，进程起来的那一刻（`spawn` 返回后、写任何东西之前）拍快照：

- `seen: dict[pid, start_time]`：进程本身、父链在树里的、树成员领导的进程组的成员（`_tree` 的现有规则）。
- `refresh()`：读循环每个事件调一次，另有一个 0.5 秒的计时任务；新出现的进程连同启动时间并入 `seen`。工具 `setsid` 出去、父进程退出后被重新挂靠，只要被刷新到过一次就还在 `seen` 里。
- `end()`：结束 `seen ∪ 当前树`，每个 pid 先核对启动时间（防 pid 复用），SIGTERM → `TERM_GRACE_SEC` → SIGKILL，`REAP_SEC = 2` 内回收不了就记日志放弃等待，不挂住这一轮。按进程组从树里够不到的 `seen` 成员（被刷新到过、之后自己 `setsid` 出组、父进程又退出了）不靠 pgid 找：按快照里的 pid ＋ 启动时间逐个独立回收（连同它领导的组），启动时间对不上的不碰。
- `begin_end()`：常驻进程被允许自己退出（`stop()` 不带 `kill`）之前，刷新并把树写进进程记录，记录留到 `end` 完成；服务器在这期间死掉，下次启动的 `sweep` 照记录结束剩下的。
- 用户的停止只在运行时不理中断时用到它：杀掉智能体时连同进程组和树一起结束（`end()`）。运行时按中断正常结束了这一轮，就不发任何信号（6.6）。

有上限的等待一览（测试里调小）：

| 常量 | 值 | 用途 |
|---|---|---|
| `INTERRUPT_GRACE_SEC`（Claude） | 5 | 写 control_request、drain、等 result 共用 |
| `CANCEL_GRACE_SEC`（Grok ACP） | 5 | 写 `session/cancel`、等 `cancelled` 共用 |
| `TERM_GRACE_SEC` | 3 | SIGTERM 到 SIGKILL |
| `REAP_SEC` | 2 | SIGKILL 之后回收 |
| `_DRAIN_SEC` | 2 | 进程退出后读完 stdout |
| `STDERR_DRAIN_SEC` | 1 | 每轮进程的 stderr 尾巴（`stderr_tail` 加上限） |
| `STOP_REPLY_BOUND` | 15 | 从停止到回停止回复的总上限（矩阵断言用） |

### 4.3 四条路径

| 路径 | `on_request` 做什么 | 进程 | 用量 | 会话 |
|---|---|---|---|---|
| Claude Code 常驻（`ResidentClaude`） | 提示没写出：不写，直接以停止返回；`result` 已派发、`run_turn` 还没返回（输出端已经忘了这一轮，停止按 `run_turn` 自己的 `_user_turn` 认）：什么都不写、不发信号，以 `control` 返回，用量完整；已写出：起任务写 `control_request interrupt`，`INTERRUPT_GRACE_SEC` 内等这一轮的 `result`（`control`，到此为止，不发信号）；超时或写不进：`stop(kill=True)` 按快照结束智能体、进程组和整棵树（`kill`） | `control`：常驻进程留着；`kill`：下一轮 `--resume` | `control`：结果里的累计差（被打断的那次请求不计，`usage_complete=False`）；`kill`：`_usage_so_far` | id 不变 |
| 每轮一个进程（`PerTurnSession`：Codex/OMP/Pi/DSH/Grok 每轮） | 没有中断：它跑着时管家拒绝停止（`can_interrupt` 为假，6.6），不杀它的进程。`cancel` 交给它之前已请求（停止在换运行时或退回之前就到了）：不起进程，以停止返回；起了之后才请求（只有不经 `stop_turn` 的调用方）：`tree.end()` | 这一轮的整棵树结束；`_session` 保留、`_ended` 不置 | `stream.usage_so_far()`；Codex 没有，`usage_complete=False` | 下一轮带 `resume_args` |
| Grok ACP（`AcpGrok`） | 还在准备会话：结束进程（`kill`）；提示已发：`session/cancel`，`CANCEL_GRACE_SEC` 内等 `stopReason: cancelled`（`cancel`，到此为止，不发信号）；超时：`_end_process`（`kill`） | `cancel`：进程和会话留着 | `_meta.usage` | 留着 |
| Grok ACP 退回每轮进程 | **同一个** `cancel` 传给 `self._legacy.run_turn(..., cancel=cancel)`；已请求就立即生效（不起进程）。退回之后 `can_interrupt` 为假，新的停止被拒绝 | 同每轮进程 | 同上 | 同上 |

删掉的特判：`per_turn.py:196` 和 `:208` 的两处 `_stopping` 检查、`grok_acp.py:686` 和 `:718` 的退回/准备中特判、`claude_cli.py` 的 `_user_stopped`/`_capped` 借道。它们都变成「在入口注册 `on_request`」。

规划者的软上限 `stop=` 钩子不变（`CapStop` 还是每个事件后问一次），用户停止不再借道 `stop=`（v0 的 `lambda: recorder.cap_stop or self._user_stop(recorder)`，`manager.py:1071`，去掉 `_user_stop`）。

## 5. 管家这一侧的阶段（D3）

`_handle_user_message` 拆开，每段只做一个阶段：

```
_message_loop 取出 → token.advance(PREPARING)
_prepare_turn(token)   持久化、命令/门分类、探测、读设置、库、提示文件、简报 → Prepared
_start_turn(token, p)  advance(STARTING)；会话切换与交接、换运行时、start、recorder、MCP 绑定
_run_model(token, p)   run_turn(..., cancel=token.cancel)；运行时调 cancel.sent() → RUNNING
_end_turn(token, t)    同步判定：cancel.requested → 停止；否则 settle(结局)；然后记录、保存、回复
```

- `token.step(name, aw)`：与停止赛跑，停止先到就取消 `aw` 并抛 `TurnStopped`。用于可放弃的步骤：发状态、`tools_problem`（线程里的探测被放弃，结果丢掉）、`_gate_command`、`_route_to_gate`、`_load_db_config`、`_load_session_state`、`_load_agent_settings`、`load_installed`、`library_with_server`、写提示文件、`_resume_brief`、`_build_user_message`。
- `token.keep(name, aw, timeout, *, undo=None)`：停止不能打断它，跑完（有上限）再检查停止。用于持久化用户消息、`_save_session_state`、`recorder.start`/`finish`、`self._claude.start`（进程起了一半被取消会成孤儿）、`self._claude.stop`。步骤始终归这一轮所有（不用 `shield` 放它独自跑下去）：这一轮的任务被取消（管家关闭）或超时，`keep` 先取消步骤并等它结束，再跑 `undo`（各自有 `ABANDON_SEC` 上限，期间再来的取消先压着，做完再抛），然后才让取消或 `TimeoutError` 传出去。`claude_start` 的 `undo` 是 `self._claude.stop(kill=True)`：关闭返回之后不会再冒出一个常驻进程。
- 交接（`_switch_session` → `_handoff`）在 `starting` 里，把 `token.cancel` 传给交接的 `run_turn`。被停：交接作废，**不**丢旧会话、不 `_claude.stop()`，`TurnStopped` 抛出；下一轮重新计算 `profile_switch`，再做一次交接。什么都不丢。
- 续接失败重试（`manager.py:1107-1125`，`resume_lost`）：转移表里没有 `running → starting`，重试走专门的 `token.restart()`：没有停止时把阶段退回 `starting`，有停止就抛 `TurnStopped`。
- 每个名字都在 `PREPARATION_STEPS`（`manager.py` 的元组常量）里。两条测试钉住它：AST 测试扫 `_prepare_turn`、`_start_turn` 的函数体，断言每个 `Await` 节点都是 `token.step(...)`/`token.keep(...)`、没有 `async with`/`async for`，且用到的名字集合等于 `PREPARATION_STEPS`；故障注入测试对 `PREPARATION_STEPS` 参数化（第 8 节）。以后加一个 `await` 而没走 `step`/`keep`，AST 测试先红；走了而没登记名字，同样红；登记了，自动进入故障注入矩阵。

## 6. 工具调用的归属（D6）

### 6.1 难点

管家的 MCP 入口（`looper mcp-server`，`ops/service.py:357` `server_config`）在常驻进程启动时固定：命令、参数、环境都在配置文件里，算进进程的签名。Claude Code 和 Grok ACP 的桥接进程跨轮存活；每轮一个进程的运行时每轮起一个新的桥接。现在 `ManagerOps.call` 按请求**到达时**的 `self._turn` 归属，所以被停那一轮迟到的请求会算到下一轮。

### 6.2 绑定文件 + 请求头

- 固定路径 `<runtime_root>/.local/manager-turn.json`（0600），路径放进 MCP 入口的环境 `LOOPER_AGENT_TURN_FILE`。路径不变，所以签名不变、进程不用重启。
- 内容 `{"epoch": n, "turn": "<binding>"|null}`，`ManagerOps.publish_binding(binding | None)` 同步原子写（临时文件 + `os.replace`）。同步是有意的：必须在提示写给运行时之前落盘。
- 桥接（`mcp_server.py:_call_tool` / `AgentClient.call_tool`）在**读到 `tools/call` 那一行时**读文件，请求带 `X-Looper-Turn: <binding>`；有运行时自己的调用 id（`params._meta` 的 `claudecode/toolUseId`，或 `toolCallId`）时另带 `X-Looper-Call`。文件不存在或坏了：不带头。`tools/list` 和探测不读文件。
- Pi 不经 `looper mcp-server`，走自带的 HTTP 桥（`pi_extension/looper.ts`）：`bridge_env` 把 MCP 入口环境里的 `LOOPER_AGENT_TURN_FILE` 一并交给扩展（Pi 的 shell 子进程照样 unset 它），扩展每次调用读文件、带 `X-Looper-Turn`，并把 Pi 自己的 `toolCallId` 作为 `X-Looper-Call`。其他运行时（Codex、OMP、DSH、Grok）把 MCP 入口的 `env` 整个抄进自己的配置，变量随之到达桥接。
- `http_server._handle_agent_api`（739）把两个头传给 `ops.call(name, arguments, binding=, call_id=)`。
- `ManagerOps` 按 binding 查 `Turn`：`register(turn, binding)` 在 `begin_turn` 时登记，最近 64 个保留（结束的也留着，好给迟到的请求准确的错误）。开关打开时：查不到或没头当作 Claude 自己的轮（`Turn(channel=None, chat=None)`），只许读——与现在没有轮时一样，`self._turn` 不再参与归属，只留给 `refuse()` 标记。开关关着（默认）时绑定只观察，见 6.5。

发布时机（不变式 W：**绑定只在上一轮的运行时确认安静之后才换**）：

| 路径 | 写 binding | 写 null |
|---|---|---|
| 每轮一个进程 | `_start_turn` 末尾，起进程之前 | `run_turn` 返回之后（这一轮的进程树已经结束） |
| Claude 常驻、Grok ACP | 写提示之前 | 这一轮的 `result` 被读到、或 `control`/`cancel` 收尾完成、或进程树结束之后 |

每轮一个进程的运行时在构造上就是对的：桥接是这一轮进程树的成员，树结束之前文件不会换，树结束之后这个桥接已经不存在；已经发出的 HTTP 请求带的是它读到的旧 binding，按旧轮的状态处理。

### 6.3 常驻运行时的边界：和流里的调用对上

常驻桥接读文件的时刻晚于模型发出调用的时刻（行在管道里等桥接被调度）。极端情况下：第 N 轮发出调用 → 被中断 → Looper 读到 result、换成第 N+1 轮的 binding → 桥接这才读那一行。只靠文件会把第 N 轮的调用算给第 N+1 轮。

所以对常驻运行时（`ResidentSession.shares_bridge`：`ResidentClaude` 为真，`AcpGrok` 不在退回模式时为真），**非只读的调用和 `looper_confirm`** 还要对上这一轮自己的流：

- 管家的 `on_event` 看到这一轮的 `tool_use` 块，调 `ops.expect(binding, tool, input, call_id=block["id"])`。登记是同步的，在事件交到 `on_event` 的那一刻做完（`_converse` 的 `observe`，连同记账和软上限要用的用量）；记录事件（数据库）、保存会话和发状态排在一个按序处理的任务里，`run_turn` 返回后等它做完。运行时是读完一个事件、`on_event` 返回之后才读下一行，所以记录慢不会拖住下一个 `tool_use` 的读取和登记（#339 的第二条：记录一个事件花了 2.1 秒，后面那个正常的写调用等过了 `MATCH_WAIT_SEC`，被拒为 `not_this_turn`）。停止请求之后这个任务不再往聊天里发状态（R1）。这个任务每处理完一个事件让出一次事件循环：运行时一次读到的一大串增量（Claude Code 会把整段回复的流事件一起写出）排在它前面时，用户看到第一个增量就发的停止立刻被处理，而不是等前面读到的增量全部发完（真实路径格 e：之前停止后 17–19 秒才有 `answer.final`）。状态（`STATUS_UPDATE`：增量、阶段）不写消息总线的日志：重启后重放只会出一条过时的状态，而日志每条都同步 `fsync`（本机约 20 毫秒），流式回复每个增量都要付一次。登记和展示分开：子 agent 的 `assistant` 事件（带 `parent_tool_use_id`）不进回复、不发状态，但它的 `tool_use` 照样登记——子 agent 经同一个桥发的写调用也是这一轮的。名字和参数先规范化成桥接请求里的样子（`_looper_call`）：去掉 `mcp__looper__`（Claude Code）或 `looper__`（Grok）前缀；Grok 经 `use_tool` 调 MCP 工具，`{"tool_name": "looper__<tool>", "tool_input": {...}}` 解包成内层的工具名和参数（`tool_input` 是 JSON 字符串时先解析）。Grok 的 MCP 请求不带调用 id，只能按名字和参数对上，所以这一步不能省。
- `ops.call` 对这类调用：有 `X-Looper-Call` 时按 id 找；没有时按 `(tool, 规范化 JSON 参数)` 找这一轮还没用掉的期望，最多等 `MATCH_WAIT_SEC = 2`（流里的事件可能晚于 HTTP 到达）。找到就消耗掉并执行；找不到就拒绝，`ErrorCode.NOT_THIS_TURN`：「这个调用不是正在进行的这一轮发出的，没有执行」。
- 只读调用不做匹配（归错轮只影响 `manager_actions` 的记账，不改东西）。
- 每轮一个进程的运行时不需要匹配。
- 要不要匹配（`Turn.require_match`）由实际跑这一轮的会话决定：在发布这一轮 binding 的时候（`_publish_binding`，已经换过运行时之后）按 `self._claude.shares_bridge` 设，不在准备阶段按换之前的会话设。这一轮的调用要读到它的 binding 才会带上它，所以之前不存在要匹配的调用（#339 的第三条：从每轮进程换到常驻运行时的第一轮没有匹配）。

需要验证（真实路径第 3 步）：Claude Code 当前版本的 `tools/call` 是否带 `_meta["claudecode/toolUseId"]`；stream-json 是否在执行工具之前写出带完整 `input` 的 `assistant` 事件；Grok ACP 的 `tool_call` 更新里的名字和 `rawInput`。任何一项不成立，匹配退回到「按名字和参数、等 2 秒」，测试两种都覆盖。

### 6.4 拒绝与确认

`_check_answering`（`service.py:391`）照旧：`STOPPED` → `ErrorCode.STOPPED`，`ENDED` → `ErrorCode.EXPIRED`；查到的是哪一轮由 binding 决定，不再是到达时刻。确认：

- 被停那一轮提出的确认在 `request_stop` 时同步作废（v0 已有，`ops.stop_turn`）。
- `looper_confirm` 只在 binding 指向的、处于 `answering` 且 `pending.answer_turn == turn.message_id` 的轮里执行；迟到的确认（binding 指向已停止/结束的轮）被拒绝，`_pending` 删掉，下一轮不会再看到「回复确认就执行」。
- `escalate` 期间被停：`escalate` 之后的第二次 `_check_answering` 已有（`service.py:215`），不登记确认。

### 6.5 开关关着时：绑定只观察

`chat.stop_enabled` 关着（默认）时，绑定照常发布、照常登记 `expect`，但**从不因此拒绝调用**：调用按到达时的 `self._turn` 归属，与绑定之前的 main 完全一样。每一处不一致记一条结构化警告并计数，真实使用里先看清绑定对不对，再让它生效：

| 原因 | 什么情况 | 开关打开时会怎样 |
|---|---|---|
| `no_binding` | 有用户轮在跑，请求没带 `X-Looper-Turn` | 当作 Claude 自己的轮，写被拒（`needs_user_turn`） |
| `unknown_binding` | 带的 binding 不是登记过的任何一轮 | 同上 |
| `other_turn` | binding 指向的不是正在跑的那一轮（迟到的上一轮调用；轮结束后到达的调用） | 按 binding 那一轮的状态拒绝（`stopped`/`expired`） |
| `not_this_turn` | 常驻运行时的写调用或 `looper_confirm`，这一轮流里没有对得上的 `tool_use` | 等 `MATCH_WAIT_SEC` 后拒绝（`not_this_turn`） |

- 观察模式下 `not_this_turn` 的匹配在调用旁边单独跑（同样等 `MATCH_WAIT_SEC`、同样消耗期望），调用不等它，不加延迟。
- 日志：`looper.agents.ops.service` 的 WARNING，`turn binding mismatch (observe-only, the call runs): reason=… runtime=… tool=… message=… total=N`，记录的 `extra` 里有 `binding_mismatch = {reason, runtime, tool, enforced}`。`runtime` 是这一轮的运行时类型（`Turn.runtime`，管家拿到 `Launch` 时填上）。
- 计数：`ManagerOps.binding_report()`，`GET /api/status` 的 `turn_binding`：`{"enforced": bool, "mismatches": N, "by": [{reason, runtime, tool, count}], "matches": [{by, when, count}]}`，从服务器启动累计。开关打开后同样记录和计数（`enforced: true`），只是这时会拒绝。`matches` 是做过匹配的调用：`by` 为 `id`（桥接带了运行时的调用 id）或 `arguments`，`when` 为 `at_arrival`（请求到达时 `tool_use` 已经读到）、`waited`（等到了）或 `missed`；6.3 节的需要验证项据此在真实路径上读出。
- 不放宽、也不收紧：关着时轮结束、轮被停的调用只在 main 本来就拒绝的地方拒绝（没有轮在跑时的写 → `needs_user_turn`；`self._turn` 已被停 → `stopped`）。

### 6.6 停止的契约：结束这一轮，不追进程

停止（Esc、停止按钮、`/stop`）结束当前这一轮，和 Claude Code 里按 Esc 一样；它不去找这一轮起过的进程。

- **只在有中断的运行时上提供**：Claude Code（control 请求 `interrupt`）和 Grok ACP（`session/cancel`）。运行时类型的能力是 `RuntimeAdapter.can_interrupt`，跑着的会话的是 `ResidentSession.can_interrupt`（Grok 退回每轮进程后为假）；管家按自己当前会话的这一项判断。
- **运行时按中断结束了这一轮，就到此为止**：不发任何信号。这一轮起的工具进程（后台命令也一样）运行时自己结束了就结束了，留下的归运行时管，Looper 不追。
- **只在运行时不理中断时杀进程**：宽限（`INTERRUPT_GRACE_SEC`、`CANCEL_GRACE_SEC`）内没结束这一轮，就把智能体连同它的进程组和 `proc_tree` 找得到的树一起结束（`kill`），和关掉一个不响应的智能体一样。
- **没有中断的运行时不提供停止**：每轮一个进程的运行时（Codex exec、OMP `-p`、Pi、DSH）没有中断通道，「运行时不能中断，我们也不中断」。`POST /api/chat/stop` 答 409 `cannot_interrupt`，Telegram 和飞书的 `/stop` 回 `当前运行时不能中断回复，回复会照常完成。`，网页的停止按钮禁用并提示原因，Esc 不做事。停止从不杀每轮进程的运行时的进程。
- **脱离的进程永远不追**：通过 `setsid`、两次 fork 或祖先退出脱离了智能体进程树的进程，不在停止的范围里，所有平台都一样。

## 7. 记录、下一轮、续接（D7）

- **有进程起来过**（`starting` 及之后才有 `ClaudeRunRecorder`）：`finish(status=INTERRUPTED, error="stopped by the user", output_text=已流出的文字 or None, usage=turn.usage)`，`annotate({"user_stop": {"via", "phase", "method", "requested_at", "usage_complete"}})`。`phase` 是停止到达时的阶段；`method` 是 `control|cancel|kill|null`（`null`：进程没起或提示没写出）。用量：运行时报到哪算到哪；完全不知道（Codex 被杀）时 `usage_complete=false`、花费记 `null` 而不是 0，Runs 显示「usage not reported」。会话累计按已知部分前移并 `_save_session_state`。
- **没有进程起来过**（`received`/`preparing` 停止）：不建 `llm_records` 行（什么都没跑），只有聊天里的用户消息和停止回复；日志一行 `manager: stopped <id> in <phase> before the model started`。
- **下一轮：** `_stopped_notes`（最多 3 条，任何渠道的下一个用户轮带上，用完即清，`/new` 也清）：
  - 提示已写出（`turn.prompt_sent`）：沿用 v0 的句子 `[Looper] The user stopped your previous reply (to: "<前 80 字>") before it finished. Tool calls it had already made were not undone; check their effects before you rely on them or repeat them.`
  - 提示没写出：`[Looper] The user withdrew a message before you saw it (it began: "<前 80 字>"). Do not act on it unless they ask again.`
- **续接：** 会话 id 不清。Claude `control`/Grok `cancel` 的进程留着直接用；`kill` 之后下一轮按现有路径 `--resume`/`session/load`；每轮进程保留 `_session`，DSH 不标过期。新会话的第一轮在会话命名之前被停：会话 id 本来就没有，下一轮照新会话处理（带简报）。续接失败走现有的 `resume_lost` 重试（`manager.py:1107`）。
- **不变式检查：** `_end_turn` 里，令牌被请求过停止却以 `ANSWERED` 结束，记一条 `logger.error("stop lost ...")` 并按停止处理。正常情况下不可能发生；第 10 节的标准之一是它在试用期为零。

## 8. 测试

### 8.1 单元

| 文件 | 内容 |
|---|---|
| `tests/test_turn_token.py` | 转移表每一格（合法/非法）；`request_stop` 幂等；`advance` 在 `stopping` 抛 `TurnStopped`；`TurnCancel.on_request` 已请求时立即触发、注销、`race` 的三种结局 |
| `tests/test_manager_stop.py`（改） | 投递即登记；`received`、`preparing`、`starting`、`running` 停止各自的回复、记录、下一轮说明；排队消息可停；`/new` 动手前后；交接被停不丢会话；`stop lost` 不变式 |
| `tests/test_manager_stop_faults.py`（新） | 故障注入：对 `PREPARATION_STEPS` 参数化，在该步骤挂住，停止，放开；断言模型的 `run_turn` 从未被调用（步骤在 `running` 之前）、`STOP_REPLY_BOUND` 内回 `outcome: stopped`、没有成功记录、没有遗留确认；外加 AST 测试（第 5 节） |
| `tests/test_proc_tree.py`（加） | `TurnTree`：快照、刷新抓到 `setsid` 出去的孙进程、父进程先退出后照样结束、pid 复用不误杀、`REAP_SEC` 上限 |
| `tests/test_per_turn_interrupt.py`、`tests/test_resident_claude_interrupt.py`、`tests/test_grok_acp.py`（改） | `cancel=` 在各入口的语义；R1（停止后没有事件进 `on_event`）；退回旧路径时停止不丢；中断成功后不发任何信号；不理中断时智能体连同进程组被杀；每轮一个进程和退回的 Grok `can_interrupt` 为假 |
| `tests/test_mcp_server.py`、`tests/test_manager_ops.py`、`tests/test_manager_turn_binding.py`（新） | 绑定文件读写与头；按 binding 归属；迟到请求（含确认）被拒；常驻运行时的匹配：按 id、按参数、等待上限、自己的轮的调用不匹配被拒 |

### 8.2 停止矩阵（常驻测试）

`tests/test_stop_matrix.py` 保留 v0 的运行时级矩阵（真实子进程、真实管道），扩成两层：

**运行时层**（每格断言：以停止结束、绝不当回答；`STOP_REPLY_BOUND` 内结束；`control`/`cancel` 不发任何信号，常驻进程和这一轮的工具进程都留着；`kill` 结束智能体、进程组和它的树；停止之后没有事件进 `on_event`；下一轮照常回答并续接同一会话。每轮进程的格测的是 `cancel=` 这个运行时原语，管家不会对跑着的每轮进程请求它）：

| 窗口 | Claude 常驻（deaf / obeys） | 每轮进程 ×（codex, omp, pi, dsh） | Grok ACP | Grok 退回 |
|---|---|---|---|---|
| 起进程之前（cancel 已请求再调 `run_turn`） | ✓ | ✓ | ✓ | ✓ |
| 起进程中（spawn 挂住） | — | ✓ | ✓ | ✓ |
| 起了没事件 | ✓ | ✓ | ✓ | ✓ |
| 流式输出中 | ✓ | ✓ | ✓ | ✓ |
| 写被堵住 | ✓ | ✓ | ✓ | — |
| 工具进行中（工具 `setsid` 出去一个孙进程） | ✓ | ✓ | ✓ | ✓ |
| 有了结果、返回之前 | ✓ | ✓ | ✓ | ✓ |
| 进程退出后读尾巴时 | ✓ | ✓ | — | ✓ |

**管家层**（新，`tests/test_manager_stop_matrix.py`）：阶段 × 运行时替身，管家用真实的 `ManagerAgent`、`agent_db`、总线，运行时用上面同一批替身进程：`received`（排在另一轮后面）、`preparing`（故障注入挂在第一个步骤）、`starting`（交接中、启动中）、`running`（流式、工具中）、`ended` 之后（`not_running`、什么都不改）。每格还检查：`/api/chat/turn` 的 `state`、记录的 `user_stop.phase`、下一轮的说明是「被停」还是「撤回」、迟到的工具请求被拒、工具中被停时只有不理中断的运行时的进程被结束。每轮一个进程的运行时（omp、codex）只有一格：跑着的和排队的停止都答 `cannot_interrupt`，不发信号，两条回复照常完成。

### 8.3 dialogs 评测

沿用 v0 的 `stop_then_continue`，加一个 `withdraw_then_continue`：发消息后立即停（`received`/`preparing`），再问「我刚才说了什么」，检查第二轮输入以 `[Looper] The user withdrew` 开头、回复不声称做过事。

### 8.4 真实路径

`evals/stop_realpath.py`（卡片 14）：在 worktree 里起 `looper serve`（新数据库、临时 `--runtime-root`、8767 以外的空闲端口，不碰 `~/.looper`），打开 `chat.stop_enabled`，设置管家档案，经 HTTP 驱动；每格重复 3 次，输出 Markdown 报告（不含本机路径）：

1. **Claude Code：** (a) 发送后立即停（`received`/`preparing`）；(b) 「运行 `sleep 60; echo done`」，工具开始后停；(c) 长文本输出中停；(d) 脱离进程树的 `setsid` 进程：不在范围内（6.6），不设格；(e) 让它调用一个高风险工具（会要求确认），确认块出现前停。核对：`STOP_REPLY_BOUND` 内 `outcome: stopped`；`kill` 时智能体、其进程组和它的树已结束，`control` 时 Claude 留下的进程只记录、不算失败（归 Claude 管）；记录 `interrupted` + `user_stop.phase/method`（`control`，或 `kill` 并写明）；`manager_actions` 里停止之后的调用是 `stopped`；没有遗留确认；下一轮「刚才怎么了」答得出，会话 id 不变。
2. **OMP + DeepSeek：** 每轮一个进程、没有中断：工具开始后停，核对停止被拒（409 `cannot_interrupt`）、回复照常完成（脚本的格 r）。
3. **6.3 节的需要验证项：** 记下 Claude Code 版本、`tools/call` 的 `_meta`、`assistant` 与工具执行的先后。
4. **Telegram 或飞书：** 长任务中发 `/stop`，排队两条再 `/stop`（渠道不可用就写明缺口）。

## 9. 实现顺序

卡片（`.lead/stopv2/card-N.md`）按依赖顺序：

1. 令牌、阶段和取消原语（纯模块）
2. 管家：投递即登记、`TurnRegistry`、`stop_turn` 作用于令牌（过渡期仍用 `interrupt()` 停运行时）
3. API 和渠道：`state`/`phase`、`/stop` 停这个聊天的所有活令牌
4. 管家准备阶段：`step`/`keep`、`PREPARATION_STEPS`、AST 测试、故障注入
5. `TurnTree` 和有上限的回收
6. 运行时契约 `cancel=` + 每轮一个进程
7. Claude Code 常驻
8. Grok ACP 和它的退回路径
9. 管家把 `token.cancel` 传给运行时，`_end_turn` 的判定，交接可停
10. 记录、用量未知、下一轮的两种说明
11. MCP 绑定文件、桥接请求头、按 binding 归属
12. 常驻运行时的调用匹配
13. 停止矩阵 v2（运行时层补格 + 管家层）和 dialogs 评测
14. 真实路径脚本
15. 文档
16. 默认打开（只在第 10 节标准满足后执行）

## 10. 默认打开的标准

全部满足才执行卡片 16：

1. 第 8.1、8.2 节的测试在 CI 的 macOS 和 Linux 上全绿，并在本机连续跑 20 遍停止相关的测试文件无一次失败（`for i in $(seq 20); do pytest -q tests/test_stop_matrix.py tests/test_manager_stop_matrix.py tests/test_manager_stop_faults.py || break; done`）。
2. AST 测试通过：准备阶段没有未登记的 `await`。
3. 真实路径报告：Claude Code 和 OMP + DeepSeek 的每一格 3/3 通过（格 d，脱离的进程，不在范围内，6.6）；6.3 节的需要验证项有结论，匹配方式据此定下。
4. dialogs 评测 `stop_then_continue`、`withdraw_then_continue`：fake 模式进 CI；real 模式 haiku 两次通过。
5. 维护者打开开关自用，累计至少 30 次真实停止（含 Telegram 或飞书至少 5 次），日志里 `stop lost` 为零，没有不理中断而被杀的智能体留下进程组或树的报告（中断成功后运行时留下的进程、脱离的进程不算，6.6）。
6. 绑定观察（6.5）：开关关着的真实使用里，每个用到的运行时（至少 Claude Code、Grok ACP、Pi 和一个每轮一个进程的运行时）都有写调用，`/api/status` 的 `turn_binding.mismatches` 为零，日志里没有 `turn binding mismatch`。有一条就先查清原因、修好，计数从修好的版本重新算。

默认打开时：`DEFAULT_STOP_ENABLED = True`；数据库里显式存的 `false` 仍然生效（用户关过的保持关）；设置页去掉「实验」标记；Telegram 菜单照 `stop_enabled` 列出 `/stop`；CHANGELOG 写明行为变化（Esc 停止、排队消息可停、`/stop`）。

## 11. 默认行为的变化（相对 v2.13.15，开关打开时）

- 排队中的消息可以停止（v0 是 `other_message`）。
- 停止只在有中断的运行时（Claude Code、Grok ACP）上提供；每轮一个进程的运行时拒绝停止（`cannot_interrupt`），回复照常完成。停止结束这一轮，不追这一轮起的进程（6.6）。
- `/stop` 停这个聊天正在答的和排队的所有消息。
- 停止之后迟到的工具调用一律拒绝；常驻运行时上和这一轮流里对不上的写调用被拒绝（`not_this_turn`）；没带或带错 `X-Looper-Turn` 的写调用被拒绝（`needs_user_turn`）。开关关着时这些都只记日志和计数（6.5）。
- 模型没看到就被停的消息：没有运行记录；下一轮带「撤回」说明。
- 用量完全未知的停止记 `null`，不再记 0。
