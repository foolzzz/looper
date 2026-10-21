# RFC：控制台聊天输入框：暂存消息、历史和 Esc 停止回复（v0）

- **状态：** 已实现（下一个版本，见 CHANGELOG 的 Unreleased）。第 3 节的决定按维护者给的四条需求写成；实现中已核实的「需要验证」处已写明结果，仍标「需要验证」的几处是还没在真实运行时上跑过的。
- **发布范围：** 暂存队列和历史随下一个版本发布；**停止回复（Esc、Stop 按钮、`POST /api/chat/stop`、Telegram 和飞书的 `/stop`）放在系统设置 `chat.stop_enabled` 后面，默认关闭**，设置页（Settings → Chat）标明是实验功能，等停止路径重新设计后再默认开放。关闭时：`POST /api/chat/stop` 答 409 `{"state": "disabled"}` 且什么都不改，`GET /api/chat/turn` 照常；输入框没有 Stop 按钮，Esc 只还原正在翻看的草稿，输入 `/stop` 也不发请求；Telegram 和飞书的 `/stop` 由 Looper 直接答「停止功能暂未开放」，不进模型、不中断任何东西；Telegram 的命令菜单不列 `/stop`（菜单在服务启动时设置，改设置后重启生效）。下文 D3、D8 和 D2 描述的是开关打开时的行为。队列和历史在两种状态下都一样。
- **相关：** [引导运行中的 Worker](worker-steering-v0.zh-CN.md)（各运行时的中断原语，3.1、3.2 节已实测 Codex `turn/interrupt` 和 OMP `abort`）、[Grok 运行时](grok-runtime-v0.zh-CN.md)（`session/cancel`）、[操作者接口鉴权](operator-api-auth-v0.zh-CN.md)、管家会话的 Agent Profile RFC（另一分支，未合并：它改聊天头部和「New session」面板，本 RFC 不碰这两处）。
- **代码基线：** v2.13.0（`3236c4242`）。

## 1. 需求与目标

维护者的需求：

1. 管家还没回复完时，用户照样能输入并按 Enter：消息进入输入框上方的暂存区（像 Claude Code TUI 的队列）。回复结束后，暂存的消息合成一条一起发出。
2. 方向键上把暂存的消息拉回输入框编辑。
3. 消息历史：上、下键翻看用户自己发过的消息（同 Claude Code TUI），网页上手感一致。
4. Esc 停止管家当前的回复（各运行时能否支持、怎么中断，由代码决定）。

**不做：**

- 不把暂存的消息「引导」进正在跑的这一轮（那是 steering，管家没有，见 worker-steering RFC D2）。
- 不停止管家自己发起的轮（后台工作结束后的 `autonomous` 轮）、`[HANDOFF]` 轮和 `/new` 重置。
- 不停止还在排队、尚未开始的消息（见 D7）。
- 不改规划者：输入框是同一个组件，暂存和历史对规划者聊天同样生效，但没有 Stop。
- 不撤销已经执行的工具调用。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 |
|---|---|
| 输入框 | `web/src/pages/chat.tsx` 的 `<form>`（930-965 行）：一个 `sending` 布尔量，回复期间按钮禁用，`handleSend`（499 行）直接 `return`；Enter 发送、Shift+Enter 换行，输入法的 Enter 由 `isComposing \|\| keyCode === 229` 挡掉（945 行）。没有历史，没有停止 |
| 回复流 | `sendChat` → `POST /api/chat/send` → `events_url`，`streamSSE` 读 `answer.delta`、`agent.phase`、`answer.final`（`web.py:_envelope_to_sse_event` 270-303 行）。`answer.final` 丢掉了回复的 `payload.error`，页面分不清出错和正常回复。`iter_sse_events` 300 秒截止（`web.py:230`） |
| 管家一轮 | `_message_loop`（`manager.py:458`）一次处理一条信封；`_invoke_claude` 在 `_turn_lock` 下跑 `_run_turn`；回复期间到达的消息留在总线队列里，之后各自一轮。`busy`（452 行）只在 `/api/overview` 里出现 |
| 停一轮的现有机制 | 单轮花费软上限：`run_turn(..., stop=lambda: recorder.cap_stop)`（`manager.py:788`），每个事件后问一次，返回 `CapStop` 就以合成的结果结束，进程被杀，花费计入会话累计。`BaseException` 路径（790-798 行）`stop(kill=True)` 后 `recorder.finish(status=ended_by(exc))`，取消记为 `interrupted` |
| 记录 | `llm_records`，状态 `running/completed/failed/interrupted`（`db/llm_records.py:16-24`）；`ClaudeRunRecorder.finish(status=…)`、`annotate(metadata)`（`claude_cli.py:1180`、1220 行）；没有结果时用量取 `_usage_so_far` |
| 会话续接 | 会话 id 一出现就写库（`manager.py:731-734`）；进程被杀后下一轮以 `resume_args` 重启续接 |
| 命令 | 只有 `/new`：`is_new_session_command`（`manager.py:222`）在 `_handle_user_message` 里先判断，走同一个队列，所以要等当前一轮结束（`tests/test_manager_new_session.py:425-454`） |
| 渠道 | Telegram `CommandHandler("new", …)`（`telegram.py:135`），其他命令被 `~filters.COMMAND` 丢掉；飞书原样转文字；飞书状态行按回复的 `payload.error` 显示「❌ 出错」（`lark.py:166`） |

各运行时作为管家时（`runtimes/__init__.py:209` 的 `ResidentSession` 只有 `start/run_turn/stop`，没有中断）：

| 运行时 | 管家的会话 | 现有中断原语 | 停止后能拿到的 |
|---|---|---|---|
| Claude Code | 常驻一个 `--input-format stream-json` 进程（`ResidentClaude`，`claude_cli.py:684`） | 已实现：`write_control` 写 `{"type":"control_request","request_id":…,"request":{"subtype":"interrupt"}}`，等这一轮的 `result` 至多 `INTERRUPT_GRACE_SEC`（5 秒），没有就杀进程（2.1.285 的回应和随后的 `result` 需要验证：目前只有离线测试，没在真实 Claude 上跑过） | 增量文字、每次请求的 `usage`；结果里的会话累计 |
| Codex | 每轮一个 `codex exec --json … resume <thread> -`（`ResidentCodex`，`codex.py:911`，`PerTurnSession`） | `exec` 没有；`app-server` 的 `turn/interrupt` 只给 Worker 用 | 增量文字；**没有用量**（只在 `turn.completed` 报，被杀就没有） |
| OMP | 每轮一个 `omp -p --mode json --resume`（`omp.py:928`） | `--mode rpc` 的 `abort` 只给 Worker | 每条消息带用量（`omp.py:57-62`） |
| Pi | 每轮一个 `pi -p --mode json --session <file>`（`pi.py:613`） | rpc 的 `abort`（要先 `clear_queue`）只给 Worker | 每条消息带用量 |
| Grok | ACP 常驻（`AcpGrok`，`grok_acp.py:339`）；退回每轮一个进程（`ResidentGrok`） | **有：** `session/cancel`（`_cancel`，`grok_acp.py:723`），0.02 秒内以 `stopReason: cancelled` 返回，会话保留 | `_meta.usage`、已有文字 |
| DSH | 每轮一个 headless 进程（`dsh.py:535`） | 没有（`DshStream.cancel` 只给结果打标记） | 每步用量；不完整时 `usage_complete=False` |

## 3. 决定

### D1 暂存区只在前端，按标签页存在 `sessionStorage`

暂存的消息还不是消息：没发出、不进 `chat_messages`、管家不知道。放服务端要新表、新的跨标签同步和「谁来发」的归属，换来的只是另一个标签页也能看到暂存，不值得。

- 存在页面状态里，并镜像到 `sessionStorage["looper-chat-staged-<session_id>"]`（字符串数组）：刷新同一个标签页还在，关掉标签页就没了，别的标签页看不到（`sessionStorage` 按标签页分）。换聊天线程或目标时各自一份。
- **只有在回复正常结束（`outcome: "answered"`，D6）时自动发出。** 其他结局（出错、被停止、等待确认、流断开后不知道结局、刷新、发送失败）一律不发，把暂存的消息按顺序合并后**放回输入框**（放在已有草稿前面，中间空一行），交给用户改了再发。这与 Claude Code TUI 中断时把队列放回输入框一致，也保证没有一条消息在用户没看到上一轮结局的情况下自动发出。
  - 刷新：页面加载时若有暂存，先问 `GET /api/chat/turn`（D5）。这个聊天的回复还在跑：保留暂存，显示「Still replying」和 Stop，每 3 秒再问一次；结束后重读消息，暂存放回输入框（看不到结局，不自动发）。没在跑：直接放回输入框。
  - 第二个标签页：页面挂载、`visibilitychange` 回到可见时问 `GET /api/chat/turn`；这个聊天有回复在跑（别的标签页发的）时，本页 Enter 同样进暂存；本页没有那一轮的流，结束靠轮询，结束后放回输入框。两个标签页同时发的话，服务端照旧排队，不会并发。
  - 发送失败（网络错误、409）：合并后的那条回到输入框，输入框下方写明原因；不重试。
- **回复进行中绝不发送暂存：** 「回复进行中」= 本页这条消息的流还没收到 `answer.final`，或 `GET /api/chat/turn` 说这个聊天有一轮在跑。合并发送只在 `answer.final` 的处理里做一次，之后 `sending` 立即为真。
- 合并用 `"\n\n"`（空一行）连接，各条先去掉首尾空白、空的丢掉。合并后是**一条**用户消息：一个气泡、一行 `chat_messages`、管家一轮、历史里一条。不加编号或「以下是几条消息」之类的包装：用户写的就是模型看到的。
- 暂存区上限 20 条；满了 Enter 不收，提示「Queue is full」。

### D2 键位：先看输入法，再看回复状态，再看光标

所有键先过同一个守卫：`e.nativeEvent.isComposing || e.keyCode === 229` 时什么都不做，交给输入法（中文输入选词的 Enter、方向键、Esc 都不触发下面任何动作；Safari 在 `compositionend` 之后补发的 keydown 带 229）。有 Ctrl/Alt/Meta 修饰时也不处理。判断写成一个纯函数 `composerKeyAction(state, key) → Action`（`web/src/lib/composer.ts`），组件只执行它返回的动作。

| 键 | 条件（按顺序取第一个成立的） | 动作 |
|---|---|---|
| Enter | Shift 按着 | 换行（浏览器默认） |
| Enter | 草稿去空白后正好是 `/stop`（不分大小写）且回复进行中 | 等同 Esc 停止，清空草稿 |
| Enter | 回复进行中，草稿非空 | 草稿进暂存区末尾，清空草稿 |
| Enter | 回复进行中，草稿为空 | 无 |
| Enter | 空闲，草稿非空 | 发送（暂存区此时应为空；若不空，与草稿合并后一起发） |
| ↑ | 选区不是一个点，或光标前有换行（不在第一行） | 浏览器默认（多行草稿里上移光标） |
| ↑ | 正在浏览历史 | 换成更早的一条（到最早一条后不动） |
| ↑ | 暂存区非空 | 暂存全部合并，放到草稿前面（空一行），清空暂存区，光标放到末尾 |
| ↑ | 历史非空 | 进入浏览：记下当前草稿，显示最近一条 |
| ↓ | 没在浏览历史，或光标后有换行（不在最后一行） | 浏览器默认 |
| ↓ | 浏览到最近一条之后 | 恢复记下的草稿，退出浏览 |
| ↓ | 正在浏览 | 换成更新的一条 |
| Esc | 回复进行中（本页的流或服务端报告的这一轮），且本页知道它的 `message_id` | 停止（D3）；草稿和暂存不动 |
| Esc | 正在浏览历史 | 恢复记下的草稿，退出浏览 |
| Esc | 其他 | 无（不清空草稿，免得误删） |

- 浏览历史时取出的条目放进输入框，光标在末尾，所以多行条目里 ↑ 先在行间移动，到第一行再换更早的一条。
- 浏览时一旦编辑（`onChange`），就退出浏览：改过的文字就是草稿，记下的草稿丢弃。
- 快捷键只在输入框有焦点时生效（处理器挂在 `textarea` 上），不抢页面其他控件的 Esc（下拉框等）。

**触屏（没有这些键）：**

- 回复进行中，发送按钮变成「Queue」（同一个位置、同一个图标加一个小队列标记，`aria-label="Queue message"`），旁边多一个 Stop 按钮（方块图标，`aria-label="Stop the reply (Esc)"`），桌面同样显示，44px 目标。
- 暂存区每条有删除（✕），整体有「Edit」按钮，等同 ↑ 拉回。
- 触屏 v0 不提供历史浏览：之前的消息就在上方的列表里。记为非核心待办（长按自己的消息「再编辑」）。

### D3 停止是一条有类型的路径：HTTP → 管家 → 运行时

管家的消息循环在等当前一轮，所以停止不能走总线队列（`/new` 就是这样要等一轮结束）。

1. **接口：** `POST /api/chat/stop`，体 `{"message_id": "<这一轮回答的用户消息 id>"}`。`/api/` 下都是 `operator_auth.Access.OPERATOR`（`operator_auth.py:292-311`），不另加鉴权；管家和规划者的令牌进不来（写一条测试钉住）。
2. **管家的轮状态：** `ManagerAgent` 在 `_handle_user_message` 里、调 `_invoke_claude` 之前设 `self._current = CurrentTurn(message_id, channel, chat, started_at, stop=None)`，`finally` 清掉。`stop` 是 `StopRequest(via, at)` 或 None。
3. **`ManagerAgent.stop_turn(message_id, *, via) -> StopResult`**（不拿 `_turn_lock`，它被这一轮拿着）。`StopResult` 是枚举：
   - `STOPPING`：记下 `StopRequest`，发一条 `agent.phase`「Stopping…」，在后台任务里调 `self._claude.interrupt()`（D4），立即返回；
   - `ALREADY_STOPPING`：同一轮第二次停，什么都不做；
   - `NOT_RUNNING`：没有用户轮在跑（空闲、`autonomous` 轮、`/new` 重置、交接）；
   - `OTHER_MESSAGE`：在跑的是另一条消息的回答。
4. **一轮还没起进程时**（读设置、交接、续接失败重试之前）：`_run_turn` 在调 `run_turn` 前和重试前看 `self._current.stop`，已有就不启动进程，直接按停止收尾（没有花费）。另外 `stop=` 改为 `lambda: recorder.cap_stop or self._user_stop(recorder)`：`interrupt()` 那时还没东西可停，第一个事件到来时也会停下。
5. **运行时返回：** `ResidentTurn` 多一个字段 `stopped: StopMethod | None`（`control`、`cancel`、`kill`）。被停的一轮的 `result_event` 是运行时自己的结果（Claude Code 中断后的结果、Grok 的 `cancelled`），或合成的 `user_stop_result()`（带 `USER_STOP` 标记，像 `SOFT_CAP` 一样让 `finish` 用 `_usage_so_far` 补用量）。
6. **记录：** `recorder.finish(output_text=已流出的文字 or None, result_event, error="stopped by the user", status=RecordStatus.INTERRUPTED, usage=turn.usage)`，再 `annotate({"user_stop": {"via": "web"|"telegram"|"lark"|"lark_bot", "method": "control"|"cancel"|"kill", "requested_at", "usage_complete": bool}})`。会话累计照常前移并写库（`_save_session_state`）；会话 id 不清，下一轮续接（被杀的进程按现有路径以 `--resume` 重启）。Runs 页的运行卡片已显示 `interrupted` 状态，不改界面。
7. **回复：** `_handle_user_message` 捕获 `TurnStopped(partial_text)`，`_send_reply(envelope, stopped_reply(partial, turn.blocks), outcome=ReplyOutcome.STOPPED)`，不带 `error`（飞书状态行不显示「❌」，`status_line.finish(stopped=True)` 显示「已停止」，已实现，在 `channels/status_line.py`）。文字：有已流出的文字时是它加一行 `（已停止）`；没有时是 `已停止，这次没有回复。`。已执行的工具块（`turn.blocks`）照现有 `_with_blocks` 附上，让用户看到停止前做了什么。web 渠道照旧把它存成 `<id>:reply`。
8. **确认：** 被停的一轮里提出、等待用户确认的高风险操作作废（`ManagerOps.drop_asked_in(message_id)`：删掉 `asked_in` 是这条消息的 `_Pending`，并从 `turn.blocks` 去掉它的块）。用户按了停止，就不该留下一句「回复确认就执行」。

### D4 每个运行时怎么中断

`ResidentSession` 协议加 `async def interrupt(self) -> StopMethod | None`：没有一轮在跑时返回 None；否则开始中断并返回方法，正在跑的 `run_turn` 随后以 `stopped` 返回。

| 运行时 | `interrupt()` | 进程 | 用量 | 需要验证 |
|---|---|---|---|---|
| Claude Code（`ResidentClaude`） | 往 stdin 写 `control_request` `interrupt`（新的 `write_control`），标记这一轮 `stopping`；等这一轮的 `result` 至多 `INTERRUPT_GRACE_SEC = 5`；`control_response` 不交给 `on_event`。超时没有结果：`stop(kill=True)`，合成结果，方法记 `kill` | `control`：进程留着，下一轮直接用；`kill`：下一轮 `--resume` 重启 | `control`：`turn_usage(result)`；`kill`：`_usage_so_far` | 2.1.285 是否在 `--print --input-format stream-json` 下接受 `control_request`（SDK 用的同一协议）；中断后是否写 `result`（预期 `subtype` 为 `error_during_execution`、带会话累计）；正在跑的 Bash 工具是否被结束；`run_in_background` 的后台命令是否留着（留着的话之后可能有一个 `autonomous` 轮，照现有路径处理） |
| Codex（`ResidentCodex`） | `PerTurnSession.interrupt`：设 `_stopping`，`proc_tree.end(当前进程)` | 这一轮的进程树被结束；`_session` 保留、`_ended` 不置，下一轮 `exec resume <thread>` | 没有（`usage_complete: false`，记 0，Runs 写明「usage not reported」） | 被杀的 `exec` 已写下的 rollout 能否被 `resume` 续上，续上后模型看得到被打断的那段 |
| OMP（`ResidentOmp`） | 同上 | 同上，`--resume <id>` | `_usage_so_far`（每条消息有） | 被杀后会话文件可续 |
| Pi（`ResidentPi`） | 同上 | 同上，`--session <file>` | `_usage_so_far` | 同上 |
| DSH（`ResidentDsh`） | 同上；`ResidentDsh._turn_ended` 对 `stopped` 的轮不把会话标为过期，`run_turn` 对「没有开会话就被停」的轮不抛错 | 同上，`--session-id` | 每步用量；不完整时 `usage_complete: false` | 被杀后 `--session-id` 可续 |
| Grok ACP（`AcpGrok`） | 调现有 `_cancel()`：`session/cancel`，等 `stopReason: cancelled`，`CANCEL_GRACE_SEC = 5` 后走现有 `_end_process` | `cancel`：进程和会话留着；超时退化为 `kill` | `AcpTurn.finish` 的 `_meta.usage` | `cancelled` 之后同一会话下一轮正常（RFC 已实测，按管家路径再测一次） |
| Grok 每轮进程（`ResidentGrok`） | `PerTurnSession.interrupt` | 同 Codex | `_usage_so_far` | — |

每轮一个进程的运行时用「结束进程树」而不是各自的 rpc `abort`：管家在这些运行时上本来就不是 rpc 模式，换模式是另一件事（worker-steering RFC D2 明确不做管家）。优雅中止（Codex app-server `turn/interrupt`、OMP/Pi rpc `abort`）记为非核心待办。

### D5 回复状态对页面可见：`GET /api/chat/turn`

`GET /api/chat/turn?session_id=<web 聊天的 id>` → `{"turn": null}` 或 `{"turn": {"message_id", "client_message_id", "channel", "started_at", "stopping": bool}}`：`message_id` 是信封 id，`client_message_id` 是页面自己发的 id（可为 null），页面两个都认；只在管家正在跑的用户轮属于这个聊天（`_message_origin` 的 `chat` 就是 web 的 `session_id`，`manager.py:256`）时返回。缺 `session_id` 400；服务没有管家 503。只用于刷新、第二个标签页和流断开后的判断（D1），本页自己发的消息仍以流为准。

### D6 回复的结局是一个有类型的字段

`AGENT_REPLY` 的 payload 加 `outcome`：`ReplyOutcome` 枚举 `answered | failed | stopped | confirm`。`_send_reply` 的 `error=` 非空时默认 `failed`；这一轮结束时 `ops` 里有 `asked_in` 等于这条消息的待确认操作时为 `confirm`；停止为 `stopped`；其余 `answered`。`web.py:_envelope_to_sse_event` 把 `outcome` 放进 `answer.final` 的 payload（缺省按 `answered` 读，老页面不受影响）。前端只在 `answered` 时自动发暂存（D1）。

### D7 不停止尚未开始的消息

本页的消息排在别的渠道的一轮后面时（服务端队列），Esc 得到 `OTHER_MESSAGE`；页面在输入框下方写「The Manager is answering another message first; this one has not started.」。要做「撤回排队中的消息」需要在 `_message_loop` 出队时检查撤回表，属于另一件事，记为非核心待办。

### D8 Telegram、飞书：支持 `/stop`

管家现在一轮可以跑很久（几十个工具调用），在手机上只能等。`/stop` 很便宜：

- **识别：** `is_stop_command(text)`：去空白、不分大小写，正好是 `/stop`。`/stop foo`、`/stopx` 是给模型的消息，与 `/new` 相同规则。
- **绕过队列：** 管家 `start()` 时 `bus.subscribe_topic(USER_MESSAGE, self._claim_stop, consumes=True)`（`bus.py:207`，`_hand_to_consumer` 在入队前同步调用，认领即标记已送达）：发给管家、文字是 `/stop` 的信封被认领，在一个任务里处理，不进 `_message_loop`。所以它能在一轮进行中到达。
- **范围：** 只停**同一个聊天**（`_message_origin` 的 `(channel, chat)`）的这一轮。群里任一在白名单里的成员都能停这个群的回复。别的聊天的回复不受影响。
- **回复：** 总是简短回一句：停了 → `已停止当前回复。`（被停那一轮自己的回复照 D3 第 7 条另发）；没有 → `现在没有正在进行的回复。`。不存进 `chat_messages`（它不改变对话）。
- **Telegram：** `CommandHandler("stop", self._cmd_stop, has_args=False)`，转发文字 `/stop`；`_BOT_COMMANDS`、`/start`、`/help` 加一行「/stop — 停止管家当前的回复」。名单外的发送者照旧没有回复。
- **飞书：** 不改代码（文字原样转来，群聊已去掉 @）；补一条测试钉住群聊里 `@bot /stop` 到达管家是 `/stop`。`lark_bot` 同一个适配器，一并覆盖。
- **控制台：** 回复进行中输入 `/stop` 回车等同 Esc（D2）；空闲时发 `/stop` 经 `/api/chat/send` 到总线被认领，回「现在没有正在进行的回复。」，页面的流照常收到 `answer.final`。

### D9 历史：这个聊天里用户自己的消息

- **来源：** 页面已经加载的消息列表（`GET /api/chat/messages` 最新 200 条，或它的 `localStorage` 缓存），取 `role === "user"` 的内容，加上本页之后发出的消息。不另存 `localStorage`：消息已经在服务端和页面缓存里，第二份只会不一致。
- **范围：** 当前打开的聊天线程（web 的管家聊天，或某个任务的规划者聊天）。不跨线程、不跨渠道（飞书、Telegram 的对话本来只读）。
- **大小与去重：** 最近 100 条；相邻重复只留一条（像 bash 的 `ignoredups`），不相邻的重复保留顺序；空白条目丢弃。合并发出的暂存消息是一条。`/new`、`/stop` 也在历史里（Claude Code 同样保留斜杠命令）。
- **草稿：** 第一次 ↑ 进入浏览时记下草稿，↓ 越过最近一条或 Esc 时恢复；编辑即退出浏览（D2）。换线程时浏览状态清空。

## 4. 前端结构

- `web/src/lib/composer.ts`：纯函数 `joinStaged`、`composerKeyAction`、`historyFrom(messages)`、`HistoryCursor`（`older/newer/reset`），不碰 DOM，单测覆盖键位表每一行。
- `web/src/components/chat-composer.tsx`：暂存区（列表在输入框上方，标题「Queued · sent when the reply ends」，每条最多显示 3 行、✕ 删除、「Edit」拉回）、`textarea`、Send/Queue 按钮、Stop 按钮、一行状态或错误（`role="status"`）。只用 `@theme` 令牌和 `components/ui` 的 Button；颜色之外有文字和图标；暂存区出现/消失用 150ms 透明度过渡并遵守 `prefers-reduced-motion`。
- `web/src/pages/chat.tsx` 只换掉 `<form>`（930-965 行）、改 `handleSend` 和 SSE 处理（`answer.final` 读 `outcome`、成功后发暂存）。**不动标题栏和「New session」按钮**（Agent Profile 分支要改它们）。
- 被停止的回复：气泡末尾的 `（已停止）` 是服务端文字；页面另在气泡下加一个「Stopped」小标签（图标 + 文字，由 `outcome` 决定，不解析文字）。
- 回复进行中占位文字：「Replying… Enter queues, Esc stops」；空闲：照旧。
- `api.ts`：`stopChatReply(messageId)`、`fetchChatTurn(sessionId)`、`ChatReplyOutcome` 类型。

## 5. HTTP API

| 方法与路径 | 行为 |
|---|---|
| `POST /api/chat/stop` | 体 `{"message_id"}`。200 `{"state": "stopping"\|"already_stopping", "method": "control"\|"cancel"\|"kill"\|null}`（`method` 为 null：进程还没起，D3 第 4 条）；409 `{"state": "not_running"\|"other_message", "error"}`；400 缺 `message_id` 或 JSON 坏；503 没有管家 |
| `GET /api/chat/turn` | D5 |
| `POST /api/chat/send` | 不变 |
| `GET /api/chat/events?cid=` | `answer.final` 的 payload 多 `outcome`（D6） |

路由加在 `http_server._route_api`（421-432 行一组）。CLI 不改：CLI 没有聊天入口。

## 6. 失败情形

| 情形 | 结果 |
|---|---|
| Esc 时回复刚好结束 | 409 `not_running`；页面收到的 `answer.final` 照常处理，不显示错误 |
| 连按两次 Esc | 第二次 `already_stopping`，不报错 |
| Claude Code 不认 `control_request` 或 5 秒内没有结果 | 退化为结束进程（`method: kill`），下一轮 `--resume`；记录写明 |
| 每轮进程被结束后续接失败 | 现有路径：`resuming and not session_started` 时丢掉会话、带简报新开一轮（`manager.py:800-816`） |
| Codex 被停，没有用量 | 记 `usage_complete: false`，花费 0，Runs 写明；会话累计不变 |
| 停止时 Looper 工具调用正在执行（HTTP 已发到 Looper） | 那次操作可能已经完成，照常记在 `manager_actions`；不撤销；下一轮的提示（第 7 节）让模型先核实 |
| 停止请求时进程还没起 | 不启动进程，按停止收尾，没有花费，`method: null` |
| 停止过程中服务停止 | 现有 `ManagerAgent.stop` 路径：取消、杀进程，记 `interrupted` |
| 流 300 秒截止、页面断网 | 页面改为轮询 `GET /api/chat/turn`，结束后重读消息，暂存放回输入框 |
| 暂存时切换聊天线程或目标 | 暂存随原线程留在 `sessionStorage`，切回来时放回输入框 |
| 管家出错（`outcome: failed`） | 暂存放回输入框，不发 |
| 管家要求确认（`outcome: confirm`） | 暂存放回输入框，不发：否则暂存里的话会被当成对确认的回答 |
| Telegram `/stop` 时没有这个聊天的回复 | 回「现在没有正在进行的回复。」 |

## 7. 模型可见的改动

- **被停之后的下一轮：** 管家记下 `self._stopped_note`（被停消息的前 80 个字符），下一个用户轮（任何渠道，会话是全局的）由 `_build_user_message` 的 `note` 放在消息前，用完即清；`/new` 也清掉：

  ```
  [Looper] The user stopped your previous reply (to: "<前 80 个字符>") before it finished. Tool calls it had
  already made were not undone; check their effects before you rely on them or repeat them.
  ```

  理由：每轮一个进程的运行时被杀后，会话里可能只有半截助手消息或什么都没有；Claude Code 的中断在会话里留什么也需要验证。由 Looper 统一说一句，所有运行时一样。
- 合并发出的暂存消息原样是用户消息，不加包装（D1）。
- 系统提示、`TOOLS_CONTRACT`、`[RESUME]`、`[HANDOFF]` 不变。
- 按计划第 1 节：这是模型可见的改动，跑 dialogs 评测（第 8 节）。

## 8. 测试与验证

**单元测试**（按卡片顺序）：

- `tests/test_manager_stop.py`：替身 `ResidentSession`（`run_turn` 等一个事件、`interrupt` 让它返回 `stopped`）；`stop_turn` 的四种结果；记录 `interrupted` + `user_stop`；回复 `outcome: stopped`、不带 `error`；会话 id 和累计保留；进程未起时不启动；下一轮的 `note`、`/new` 清掉它；被停一轮的待确认作废；`confirm` 结局。
- `tests/test_chat_stop_api.py`：两个接口的状态码、`answer.final` 的 `outcome`、鉴权（无凭据 401，代理令牌进不来）。
- `tests/test_per_turn_interrupt.py`：一个会睡的每轮替身进程；`interrupt` 结束进程树、会话保留、下一轮带续接参数；OMP/Pi 用量取到；DSH 不标过期。
- `tests/test_resident_claude_interrupt.py`：stream-json 替身收到 `control_request` 后写 `control_response` 和一个错误 `result`；另一个替身不理会，5 秒（测试里调小）后被杀、方法 `kill`。
- `tests/test_grok_acp.py` 加管家路径的 `interrupt`（`fake_grok_acp.py`）。
- `tests/test_manager_stop_command.py`：`/stop` 在一轮进行中被认领并停止同一聊天的轮；别的聊天不停；空闲时的回复；Telegram 命令注册和转发、名单外不转；飞书群聊 `@bot /stop`。
- `web/src/lib/composer.test.ts`：键位表每一行（含 IME、修饰键、多行光标）、合并、历史去重和上限。
- `web/src/components/chat-composer.test.tsx`：暂存、拉回、删除、Stop 按钮、触屏按钮、IME 不触发。
- `web/src/pages/chat-composer-flow.test.tsx`：`answered` 后合并发出一次；`failed`、`stopped`、`confirm` 放回输入框；刷新时 `sessionStorage` 和 `GET /api/chat/turn`；发送失败回到输入框。

**停止矩阵**（`tests/test_stop_matrix.py`，真实子进程和管道）：每个管家运行时路径 × 停止落点，每格都检查同几件事：停止不丢（这一轮以停止结束，绝不当成回答）；有界时间内结束，这一轮起的进程树全部结束（Claude Code 的 `control`、Grok 的 `cancel` 按设计保留常驻进程，这一轮起的工具进程不留）；这一轮的事件不进下一轮；下一轮照常回答并续接同一会话。确认作废与运行时无关，在 `ManagerOps` 层（`tests/test_manager_stop.py`）。

| 落点 | Claude Code 常驻 | 每轮一个进程（Codex/OMP/Pi/DSH） | Grok ACP |
|---|---|---|---|
| (a) 进程起来之前 | 进程在 `start` 里起，归管家：`test_stop_before_the_process_starts`、`test_a_stop_while_the_process_fails_to_start_ends_the_turn_stopped` | `test_per_turn_stop_in_every_window[before_spawn]`、`test_per_turn_interrupt.test_a_stop_before_the_process_exists_is_kept` | `[before_spawn]`（`initialize` 不答：结束进程，不退回每轮进程）、`test_grok_acp.test_a_stop_while_the_session_is_readied_sends_no_prompt` |
| (b) 起了、还没有事件 | `test_resident_claude_stop_in_every_window[silent-*]` | `[silent]` | `[silent]` |
| (c) 流式输出中 | `[streaming-*]` | `[streaming]` | `[streaming]` |
| (d) 往进程写被堵住 | `[blocked-*]`（写、drain、等结果共用一个宽限） | `[blocked]` | `[blocked]`（提示写不进去：宽限后结束进程） |
| (e) 工具调用进行中 | `[tool-*]` | `[tool]` | `[tool]` |
| (f) 有了结果、收尾之前 | `[done-*]`；运行时已无可停时由管家兜底：`test_a_stop_after_the_runtime_answered_ends_the_turn_stopped` | `[done]`、`test_per_turn_stop_after_the_process_exited_before_the_turn_returned` | `[done]`、`test_grok_acp.test_a_normal_result_queued_before_a_stop_does_not_end_the_turn_as_success` |

管家自己的窗口：这一轮的结局一经记录（回答或失败），停止返回 `not_running`（`test_a_stop_once_the_turn_is_recorded_is_not_running_and_changes_nothing`）；「Stopping…」状态发得慢时，这一轮的中断不会落到下一轮（`test_a_stop_whose_status_is_slow_never_interrupts_the_next_turn`）。

**dialogs 评测**（`evals/prompt_regression/dialogs.py`）：新对话 `stop_then_continue`：发「数一下有几个项目，然后逐个看它们最近的任务」，第一个工具调用后调 `POST /api/chat/stop`；再发「刚才停在哪了？」。检查：第一轮记录 `interrupted`、`user_stop.via = web`；第二轮输入以 `[Looper] The user stopped` 开头；回复不声称刚才的工作做完了。fake 模式进 CI；real 模式跑 haiku 和 opus xhigh 两档各 2 次（需要验证：dialogs 驱动能否在一轮中途调接口，可能要加一个「事件后动作」钩子）。

**真实路径**（AGENTS.md：worktree 里的 `looper serve`、新数据库、8767 以外的端口、临时 `--runtime-root`；不碰 `~/.looper`）：

1. 管家用 Claude Code：发一条会跑长 Bash 的消息（如「运行 `sleep 60; echo done` 然后告诉我结果」），等工具开始后在输入框按 Esc。核对：几秒内气泡出现「Stopped」，`sleep` 进程没了（`ps`），Runs 里这一轮 `interrupted`、`user_stop.method` 是 `control`（或 `kill` 并写明原因），花费非零；下一条「刚才怎么了」答得出被停止，且同一会话（Runs 的会话 id 不变）。
2. 再换一个运行时（OMP 或 Codex，本机有哪个用哪个；Codex 要看用量缺口的显示）重复第 1 步，`method` 是 `kill`，下一轮续接同一会话。
3. 回复进行中连打三条（其中一条中文，用输入法打，选词的 Enter 不进暂存），↑ 拉回改一条，再 Enter；回复结束后只发出一条合并消息。
4. ↑/↓ 翻历史、多行草稿里 ↑ 只移光标、Esc 恢复草稿。
5. 刷新页面（暂存还在、回复还在跑时显示 Stop）；开第二个标签页。
6. Telegram 或飞书发长任务后发 `/stop`（渠道不可用就写明缺口）。
7. 截图：暂存区（空、两条、满）、回复中（Queue + Stop）、被停止的气泡、错误后放回输入框，桌面 1280 px 和窄屏 768 px；截图去掉本机路径。

## 9. 默认行为的变化

- 回复进行中按 Enter 以前什么都不发（按钮禁用、`handleSend` 返回），现在进暂存区并在回复成功后自动发出。
- Esc 在聊天输入框里以前没有作用，现在停止回复。
- Telegram 多一个 `/stop` 命令；飞书、Telegram 里正好是 `/stop` 的消息不再交给模型。
- 管家被停后的下一轮多一行 `[Looper]` 说明。
