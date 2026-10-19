# RFC：引导运行中的 Codex 和 OMP Worker（v0）

- **状态：** 提案，第 5 节的决定按建议写下；核实结果（第 3 节）来自真实运行时。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.2 节的「Codex 运行时的已知差距（M9c）」和「OMP 运行时的已知差距（M9e）」。Grok 的引导走 ACP，另见 Grok 运行时 RFC。
- **相关：** [多运行时 RFC](multi-runtime-agents-v0.zh-CN.md) 第 6 节能力矩阵的「运行中追加用户轮」一行。

## 1. 背景与目标

Claude Code 的 Worker 靠 stream-json 输入引导：规划者转来的用户消息或它自己的纠偏，作为 Worker 的下一个用户轮写进正在跑的进程。Codex 和 OMP 现在都是「一个进程一轮、提示读到 stdin 结束」，`write_turn` 关掉 stdin，之后再写就抛 `RuntimeError`，`WorkerPool.send_input` 返回 False，规划者把消息记进 `step.missed_messages`，留给这一步的决策轮。评测里聊天纠偏任务因此在 Codex 和 OMP 上都多一次重试（M9c、M9e 的评测记录）。

**目标：**

- 用运行时自己的双向协议引导运行中的 Codex 和 OMP Worker，规划者的消息在这一步还在跑时就到达，不再等决策轮；
- 其余行为不变：隔离（家目录、环境变量）、`proc_tree` 杀进程树、用量与花费、超时、会话保留（agent room 的会诊）都和每轮一个进程时一样；
- 协议不可用（版本太旧、探测失败、协议握手失败）时退回现在的每轮一个进程，消息仍留给决策轮。

**不做：** Manager 和规划者的常驻会话（本来就是每轮一个进程，没有「运行中」可引导）；room 提醒（`WorkerPool.remind`，只给 Claude Code，见 D5）；Grok（见 Grok 运行时 RFC）。

## 2. 现状

| 环节 | 现在怎么做 |
|---|---|
| Worker 进程 | `WorkerPool` 起 `runtime.run_command(...)` 的进程，`runtime.write_turn(stdin, prompt)` 写提示，`runtime.parse(stdout)` 读事件，第一个 `result` 之后关 stdin |
| 引导 | `WorkerPool.send_input` → `runtime.write_turn(stdin, text)`；Claude Code 写一条 stream-json 用户消息；Codex、OMP 的 `write_turn` 在 stdin 已关时抛 `RuntimeError` |
| 失败的处理 | `send_input` 返回 False → 规划者 `_nudge_worker` 把消息放进 `step.missed_messages`，记一条「arrived after … finished」的活动；这一步结束后的决策轮带上它 |

## 3. 核实结果（本机 CPA，gpt-5.6-luna，2026-10-18）

探针脚本用 Looper 自己生成的隔离目录（`CODEX_HOME`、`HOME`）和 CPA 提供者起真实进程，密钥运行时读取、不进日志。

### 3.1 Codex 0.162.0：`codex app-server`（实验性 JSON-RPC，stdio 一行一条）

- **握手：** `initialize`（`clientInfo`）→ 通知 `initialized` → `thread/start`（`cwd`、`model`、`approvalPolicy: "never"`、`sandbox: "danger-full-access"`、`ephemeral`）→ `turn/start`（`threadId`、`input: [{type: "text", text}]`、`effort`）。`initialize` 的结果里 `codexHome` 就是进程的 `CODEX_HOME`，所以 Looper 生成的 `config.toml`（提供者、MCP）和 `AGENTS.md` 照常生效；非 git 目录不需要 `--skip-git-repo-check`。
- **引导：** `turn/steer`（`threadId`、`expectedTurnId`、`input`）在运行中的一轮上成功，返回同一个 `turnId`。实测：Worker 正在跑一条 25 秒的 shell 命令，第 8 秒引导「回复末尾加一个词」，消息立即作为同一轮的用户消息进入（`item/started` userMessage），模型随即重新采样，命令继续跑完，最终回复带上了这个词，整个运行仍是一个 `turn`。
- **失败方式（都是 JSON-RPC 错误，不影响运行）：** `expectedTurnId` 与当前轮不符 → `-32600 expected active turn id ... but found ...`；一轮已结束 → `-32600 no active turn to steer`。
- **中断：** `turn/interrupt` 返回 `{}`，随后 `turn/completed` 的 `status` 是 `interrupted`。
- **事件：** 通知 `thread/started`、`turn/started`、`item/started`/`item/completed`（`userMessage`、`reasoning`、`commandExecution`、`agentMessage`、`fileChange`、`mcpToolCall`、`webSearch` 等，驼峰字段）、`item/agentMessage/delta`、`thread/tokenUsage/updated`（`total` 是线程累计值：`inputTokens` 含 `cachedInputTokens`，`outputTokens` 含 `reasoningOutputTokens`）、`turn/completed`（`status`：`completed`/`interrupted`/`failed`）、`error`（`willRetry`）。服务端请求（审批、`item/tool/requestUserInput`、`mcpServer/elicitation/request` 等）必须回应，否则这一轮会等下去；`approvalPolicy: "never"` 时实测没有出现。
- **结束：** stdin 关闭后进程退出码 0；运行中被杀（SIGTERM）不留 `turn/completed`，和 `exec` 一样。
- **能力探测（不调模型）：** `codex app-server generate-json-schema --out <目录>`（约 0.3 秒）生成的 `ClientRequest.json` 列出 `turn/steer`；老版本没有这个子命令或没有这个方法。

### 3.2 OMP 18.4.3：`--mode rpc`（JSON 行，命令和事件同一条管道）

- **命令（stdin，一行一条，`id` 可选）：** `prompt`（`message`）、`steer`（`message`）、`follow_up`、`abort`、`get_state`、`set_interrupt_mode`、`negotiate_protocol`（`protocolVersion`）等；每条命令有一条 `{type: "response", command, success, error?}` 回复。进程先发 `ready`（`supportedProtocolVersions`）。
- **引导：** `steer` 在运行中成功。事件里该消息是 `message_start` 的用户消息，带 `steering: true`。
- **打断方式：** 默认 `interruptMode` 是 `immediate`：引导到达时正在跑的 shell 命令被「后台化」（工具结果是「Backgrounded early to handle an incoming message; the command keeps running」），这一次 `agent_end` 带 `isTerminal: false`，命令的输出稍后作为后续消息注入。`set_interrupt_mode` 设成 `wait` 后，引导等当前工具调用结束再进入（实测命令跑满 25 秒，之后才进入引导消息，最终 `agent_end` 的 `isTerminal` 是 true），和 Claude Code「下一个工具结果处折入」一致，不改变命令的行为。
- **失败方式：** 坏行 → `{type: "response", command: "parse", success: false}`；未知命令 → `Unknown command`；缺字段 → 带 JS 报错文字的失败回复。会话都不受影响。
- **结束：** `agent_end` 之后进程不退出，`prompt_result` 和 `session_settled` 随后到；stdin 关闭后进程退出码 0；**运行中关 stdin 会中止当前运行**（实测第 8 秒关 stdin，正在跑的命令被结束、进程退出、没有 `agent_end`）。所以 Looper 只能在 `result` 之后才关 stdin，和 Claude Code 一致。
- **事件：** 与 `--mode json` 相同的 `agent_start`、`turn_start`、`message_start`/`message_end`、`tool_execution_*`、`agent_end`，多了 `response`、`ready`、`prompt_result`、`session_settled`、`available_commands_update`、`extension_ui_request`（`--no-ui` 可关）。`--mode json` 的首行会话头在这里没有，会话 id 来自 `get_state` 的 `sessionId`。

## 4. 方案

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A 保持现状 | 每轮一个进程，消息留给决策轮 | 不改代码 | 评测里聊天纠偏每次多一次重试，用户的纠正要等这一步跑完 |
| B 协议驱动（推荐） | Codex 走 `app-server`，OMP 走 `--mode rpc`，Worker 的输入输出经一个「运行通道」；能力探测通过才启用 | 真正的运行中引导；隔离、杀进程树、用量、超时都不变 | 多一个协议驱动（两个运行时各约 300 行）和一套事件翻译；Codex 的 `app-server` 是实验性接口 |
| C 中断后重发 | 收到消息时 `exec` 被杀，带着消息 `resume` | 不用新协议 | 正在跑的命令和半个回复丢了，等于重试；Codex 被杀不留 `turn.completed`，用量记不全 |

**推荐 B，A 保留为回退。** B 里 Codex 的接口实验性这一点，用能力探测（见 D1）和失败即报错（D4）收住。

## 5. 设计与决定

### D1 能力 `steer`，由探测决定

新增运行时能力 `Capability.STEER`：「运行中的 Worker 或 Reviewer 能收到规划者的消息」。Codex 适配器在版本不低于 0.162.0、且 `generate-json-schema` 列出 `turn/steer` 时才声明；OMP 在版本不低于 18.4.3、且 `--help` 提到 `rpc` 时才声明；探测失败、版本读不出来、更老的版本一律不声明（`capabilities_for`，与 `READ_TOOLS` 同一个模式）。Claude Code 不列这个能力（它靠 `run_input`）。`agent_runtimes.capabilities` 的检查约束加入 `steer`（新库和一次性迁移，与 `read_tools` 相同）。已注册的运行时要点一次 Re-detect 才有这个能力；没点之前走每轮一个进程，行为不变。

### D2 范围：Worker 和 Reviewer 的单次运行

`Launch.steers`：运行时的已探测能力含 `steer` 且角色是 Worker 或 Reviewer。Manager、规划者和会诊（`consult_command`）不变。

### D3 运行通道

`WorkerPool` 不再直接碰 stdin：每次运行有一个通道（`runtimes/channel.py` 的 `RunChannel`）：`start(prompt)` 发出提示，`events()` 给出内部事件，`send(text)` 送入引导，`end_input()` 表示不再有输入。默认实现 `StreamChannel` 包住现有的 `write_turn`/`parse`/关 stdin，所以 Claude Code、Grok 和没有 `steer` 的 Codex、OMP 的行为逐字节不变。有 `steer` 时，适配器的 `steer_command` 给出 `codex app-server` 或 `omp --mode rpc` 的命令（隔离、提供者、系统提示照旧），`run_channel` 给出协议驱动。命令不同，但同一个进程对象交给 `proc_tree`，杀进程树、记录、超时、花费上限都走原来的路径。

### D4 协议失败即报错，不悄悄重跑

握手失败（`initialize`、`thread/start`、`turn/start` 或 omp 的 `prompt` 没有成功回复）、进程在提示送达前退出：这次运行以明确错误失败（`result` 为错误），由规划者按失败处理（重试走同一个通道）。不在运行时自动改用每轮一个进程：提示可能已被模型执行一部分，悄悄重跑会重复副作用。要退回每轮一个进程，让运行时失去 `steer`（升级前的版本、探测失败）。

### D5 引导的送达和回执

`send(text)` 在服务端接受后才返回（Codex 的 `turn/steer` 成功回复、OMP 的 `steer` 成功回复）；等待轮 id 的握手最多 4 秒（总线请求的超时是 5 秒）。服务端拒绝（没有活动的轮、轮 id 不符、进程已退出、通道已结束）就抛 `RuntimeError`，`send_input` 返回 False，走现有的 `missed_messages` 路径。送达的消息记一条活动，和 Claude Code 一样。room 提醒（`remind`）仍只给 Claude Code。

### D6 OMP 用 `wait` 打断方式

握手时 `set_interrupt_mode wait`（见 3.2）：引导等当前工具调用结束再进入，不把正在跑的命令后台化。Codex 的引导立即进入，命令继续跑（3.1），无需额外设置。

### D7 服务端请求一律回错误

Codex 的服务端请求（审批、用户输入、MCP elicitation 等）回 JSON-RPC 错误并记一条 `system` 事件，不让这一轮等下去；`approvalPolicy: "never"` 加 `danger-full-access` 下实测不会出现。OMP 的 `extension_ui_request` 用 `--no-ui` 关掉。

### D8 事件翻译复用

Codex 的 `app-server` 通知先翻译成 `exec --json` 的事件形状（`thread.started`、`item.*`、`turn.completed` 等，字段从驼峰改下划线），再交给现有的 `CodexStream`，所以工具摘要、`tool_use`/`tool_result`、`usage` 事件和 `result` 与每轮一个进程时一致。OMP 的 rpc 事件直接交给现有的 `OmpStream`，丢掉 `response`、`ready`、`prompt_result`、`session_settled`、`available_commands_update`，并用 `get_state` 的 `sessionId` 合成会话头。

### D9 会话保留

有会话目录的运行（agent room 席位，会诊要续接它的会话）：Codex 的线程不设 `ephemeral`，`CODEX_HOME` 用会话目录（与 `run_command` 的 `session_dir` 做法相同），线程 id 作为 `session_id`；OMP 用 `--session-dir`，其余用 `--no-session`。

### 失败方式一览

| 情形 | 行为 |
|---|---|
| 版本老、探测失败、没点 Re-detect | 无 `steer`，每轮一个进程，消息留给决策轮 |
| 握手失败、提示送达前进程退出 | 运行失败并带明确错误（D4） |
| 引导时没有活动的轮、轮 id 不符、通道已结束 | `send` 抛 `RuntimeError`，`send_input` 返回 False，消息留给决策轮 |
| 运行中进程崩溃 | `events()` 在 EOF 结束，没有 `result`，和每轮一个进程的崩溃同一条路径 |
| 协议里的坏行 | Codex 一侧忽略（记 debug 日志）；OMP 一侧收到失败回复，运行不受影响 |
| 服务端请求 | 回错误（D7） |
| 超时、花费上限 | 由 `WorkerPool` 用 `proc_tree` 杀整棵树，与现在相同 |

## 6. 已知差距

- Codex 的 `app-server` 是实验性接口，字段可能随版本变；探测只看版本下限和 `turn/steer` 在不在，协议字段变了要靠真实运行的评测发现。
- 引导在运行结束瞬间到达：服务端已接受但这一轮正好结束时，这条消息可能不被处理；回执只说明服务端接受了它。
- Codex 不报压缩和单次请求的用量（与 `exec` 相同）；`thread/tokenUsage/updated` 给线程累计值，Looper 照旧在一轮结束时判断单次上限。

## 7. 评测

目标评测：聊天纠偏（`chat_correction`）和 `single_step`，Codex 和 OMP 各一个 agent（CPA），`--reps 2`，与 M9c、M9e 的记录比较重试次数；结果记入开发计划的账本。
