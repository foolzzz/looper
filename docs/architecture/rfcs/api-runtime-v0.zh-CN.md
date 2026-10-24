# RFC：API 运行时 v0（管家直连模型 API）

- **状态：** 已批准（2026-10-24），阶段 1 待做。维护者先定了阶段顺序：第一阶段接 `openai` Python SDK，用本机 CPA 的 OpenAI 协议模型验证；第二阶段接 `anthropic` Python SDK，目标是 DeepSeek、GLM、Kimi、OpenCode 的 Anthropic 格式接口，用维护者已有的 DeepSeek key 验证；以后有官方 Anthropic key，只是多登记一个 provider，不改代码。同日维护者按本文的建议批准了其余决定（第 4 节、第 14 节）。开发排期见第 12 节；第 15 节是仍需真实运行核实的项。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.0 节第 23 项。它落实 M9d「SDK 运行时」里不依赖 norma 的部分，以及[管家平台工具 RFC](manager-platform-tools-v0.zh-CN.md) D8：「原生 tool 不单做，等 SDK 运行时上线时从同一份操作目录生成」。
- **相关：** [多运行时](multi-runtime-agents-v0.zh-CN.md)、[dsh 运行时](dsh-runtime-v0.zh-CN.md)、[停止管家的回复 v2](chat-stop-v2.zh-CN.md)、[管家会话的 Agent Profile](manager-session-profile-v0.zh-CN.md)、[场景管家](scenario-managers.zh-CN.md)、[LLM 模型层](llm-models-v0.zh-CN.md)。
- **代码基线：** `main` `787610ec2`（v2.13.62）。SDK 的接口在一次性虚拟环境里离线核对过：`openai` 3.37.0、`anthropic` 2.3.0。全程没有发出带 key 的请求。
- **路径约定：** 下文的代码路径相对 `looper/agents/`；`schema.sql` 指 `looper/db/schema.sql`，`dialogs.py` 指 `evals/prompt_regression/dialogs.py`；SDK 源码的行号对应上面两个版本。

## 1. 背景与目标

**现状：**

- 每个管家都是一个 CLI 进程。Claude Code 常驻；Codex、OMP、Pi、DSH 每轮起一个进程；Grok 走 ACP（`runtimes/__init__.py:218-248` 的 `ResidentSession`）。管家只用 Looper 的平台工具，却要拉起整套编码 harness：运行时自带的提示和工具定义、`looper mcp-server` 子进程，还有每 5 分钟一次、起子进程的工具探测（`ops/service.py:297`、`tiers/manager.py:1071`）。
- 每轮一个进程的运行时不能中断，所以管家拒绝停止（chat-stop-v2 第 6.6 节）。
- provider 的 key 进了 agent 子进程的环境，不限权限的 agent 能读到（多运行时 RFC P3）。

**目标：**

- 新增一种运行时，在 `looper serve` 进程里用官方 SDK 直接调用模型 API，跑管家的 agent 循环。工具就是 Looper 的平台操作，在进程内调用。
- 走现有的 provider 层：协议取自 Agent Profile 选中的模型（`ModelSpec.protocol`），`base_url` 取自这个协议的 endpoint，key 取自 provider（`profile_registry.py:270-372`）。
- 循环与协议无关。第一阶段做 OpenAI Responses，第二阶段做 Anthropic Messages，只多一个编解码器，循环不变。
- 两步确认、留痕、轮次归属、停止和花费记录都与现在完全一样。

**不做：**

- v0 只给管家用。规划者、执行者、评审者要读写工作目录、跑命令，另立 RFC。
- 不加载 skill、插件和能力目录里的外部 MCP 服务器（第 9 节）。
- 不自动回退到 CLI 管家（同管家会话 RFC D7）。
- 不改 CLI 运行时的任何行为，也不做按模型的 prompt 变体（多运行时 RFC P4）。

## 2. 对用户的变化

| 入口 | 变化 | 不变 |
|---|---|---|
| 控制台 | Settings → Agent Runtimes 多一张「Model API (SDK)」卡：自动检测，显示 SDK 版本，能跑的角色只有 Manager。Agent Profiles 可以选运行时 `api` 和 provider 里协议受支持的模型；Roles 页只有 Manager 一行能选它。聊天的停止按钮可用（真的中断）。Runs 页里，这类运行的 command 是一行描述，不是 argv | 流式回复、配置块、确认说明、任务页、花费页 |
| Telegram、飞书 | `/profile` 会列出 API Profile；`/stop` 在 API 管家上真的停掉这一轮 | 渠道协议、白名单、发消息逻辑 |
| CLI 与 `loopercli` | 没有新参数。`loopercli chat` 照旧和管家对话；管家工具 `looper_agent_profiles` 能建 `api` Profile，因为运行时只是一个类型 | `looper serve`、`looper doctor`、v1 API |
| 管家的行为 | API 管家只有平台工具：不能读文件、跑命令、上网，也不能用 skill。需要这些时，它会提议建任务（运行时说明里写明） | 平台工具的集合、范围（场景管家的 `OpsScope`）和风险档 |

默认不变：没有 Profile 选它之前，所有管家照旧跑在原来的运行时上。

## 3. 方案

| 方案 | 做法 | 复杂度 | 延迟与花费 | 故障半径 | 可维护性 |
|---|---|---|---|---|---|
| **(a) 新运行时类型 `api`**（选定） | 一个 `RuntimeAdapter`，加一个进程内的 `ResidentSession`（`ApiSession`）；循环在一个小接口后面，与协议无关，每种协议一个编解码器；v0 用类型化的角色限制只开放管家 | 中：适配器里的 CLI 方法要给出明确的「不支持」；管家改 4 处 | 没有进程、MCP 子进程和探测子进程；请求里也没有 CLI 自带的提示和工具定义 | 一个会话对象；SDK 的异常止于这一轮 | 复用 Profile、`/profile`、换会话与交接、记录器、花费、停止和评测；以后给规划者用，只需加工作区工具 |
| (b) 管家专用循环 | 在 `ManagerAgent` 里按运行时分支，直接调用 SDK 和 `ManagerOps` | 起步低，之后高：记录、停止、续接、交接、花费都要在管家里再写一遍 | 同 (a) | 同 (a) | 差：`tiers/manager.py` 已有 2351 行，每个管家功能都要维护两条路径；违反「tier 不自己起运行时」（`runtimes/__init__.py:5-7`）；规划者用不上 |
| (c) 进程外的 API 运行器 | 新命令 `looper api-agent` 在子进程里跑循环、输出 stream-json，工具仍经 `looper mcp-server`，复用 `PerTurnSession` | 中 | 每轮要起进程和 MCP 子进程（约 0.3–1 秒，管家工具 RFC 第 10 节）；停止又变回杀进程 | 最小：崩溃不影响服务 | 多一套进程协议；进程内的好处（能中断、不要桥、key 不进子进程）都没有了 |

选 (a)。(c) 的隔离对 SDK 帮助不大：SDK 全是异步网络 I/O，而 `looper serve` 本来就在同一个事件循环里跑 Telegram、飞书和数据库客户端。P11 为 norma 建议子进程，是为了隔离一个完整的 harness；SDK 只是 HTTP 客户端，情况不同。另外也考虑过让 SDK 自带的循环来跑工具（`anthropic` 的 `beta.messages.tool_runner`、OpenAI Agents SDK），没有采用：两家各一套，停止、软上限、轮次归属和留痕都插不进去。

## 4. 决定

全部已定：D2 的阶段顺序是维护者先定的，其余由维护者 2026-10-24 按本文的建议批准（第 14 节）。

| # | 决定 | 理由 |
|---|---|---|
| D1 | 新运行时类型 `api`（方案 a），v0 只能跑管家。适配器加一个类型化字段 `roles = {"manager"}`，`servable_roles` 和 `check_roles`（`profile_registry.py:728、789`）按它收窄 | 它有 `resident_session` 和 `resume`，按 `ROLE_CAPABILITIES`（`runtimes/__init__.py:174`）会被算成也能跑规划者。用类型化字段而不新增能力，已登记运行时存下的能力就不用迁移 |
| D2 | 第一阶段 `openai` SDK + CPA，第二阶段 `anthropic` SDK + DeepSeek（另有 GLM、Kimi、OpenCode）。第一阶段只做 OpenAI Responses，传 `store: false`，每次请求发完整输入，不用 `previous_response_id` | 第 8.1 节 |
| D3 | 循环放在接口 `AgentLoop` 后面（第 5 节），默认实现与协议无关，由编解码器（`ProtocolCodec`）负责请求参数、流事件翻译和会话条目的原生格式 | 第二阶段只加一个编解码器；以后别的 harness（如 norma）也只需替换循环 |
| D4 | 工具在进程内调用 `ManagerOps.call`（`ops/service.py:333`），带上这一轮的 binding 和模型给的调用 id，不经过 MCP 桥、HTTP 和令牌 | 校验、分档、确认和留痕仍只有一处（管家工具 RFC D5），进程内调用的就是 HTTP 处理器调用的那个函数（`http_server.py:1156-1191`） |
| D5 | 会话原文由 Looper 存进 PostgreSQL 新表 `api_session_items`，用原生格式，每完成一次模型请求写一次；会话丢弃后删除 | CLI 运行时的会话由它们自己存成文件；API 运行时没有别人替它存 |
| D6 | 上下文接近上限时走现有的「交接 + 新会话 + 续接简报」，不另写压缩提示词。阈值取窗口的 70%，且不超过 200k token | 复用已经评测过的模型可见内容（`session_switch`、`tiers/manager.py:1910-2060`）；200k 是为了控制花费（第 7.3 节） |
| D7 | 停止就是取消正在进行的模型请求，`can_interrupt = True` | 离线核对过：取消 SDK 的流立即返回，服务端也看到连接关闭（第 6.5 节） |
| D8 | 不回退：API Profile 跑不了时，这一轮回错，用户用 `/profile` 换回去 | 同管家会话 RFC D7 |
| D9 | SDK 作为普通依赖，设上界，只在这个运行时里延迟导入 | 第 14 节第 3 项 |
| D10 | 每轮最多 30 次模型请求，超出就以错误结束这一轮 | CLI 管家没有上限。30 次足够建任务、确认、改设置这类对话，也能挡住死循环 |
| D11 | 准入门槛：所有对话 `--reps 2`，每个对话的通过次数不低于同模型 CLI 对照减 1，确认类对话全部通过 | 第 9 节；达不到的，发布说明里只写「实验」 |

## 5. 运行时与接口

三层，各有一个小接口：

| 层 | 职责 | 接口 |
|---|---|---|
| `ApiSession` | tier 看到的常驻会话：会话 id、原文存取、停止的接线、内部事件、续接 | `ResidentSession`（现有，`runtimes/__init__.py:218`），外加 `InProcessTools.set_turn_tools(tools)` |
| `AgentLoop` | 跑一轮：输入一轮（系统提示、这一轮的用户文本、会话原文的句柄、工具列表）；输出流式事件和工具调用（经 `TurnTools`）；能被取消；报告用量 | `async run(turn, *, tools, cancel, emit) -> LoopResult` |
| `ProtocolCodec` | 一种协议：请求参数、流事件到中性事件、原生会话条目 | Responses（阶段 1）、Messages（阶段 2） |

以后 norma（M9d）若以 Python 的形式接入，可以作为 `AgentLoop` 的另一种实现，`ApiSession`、provider 绑定和管家都不用改。这是可能的后续阶段，不在本文范围内。

**适配器**（`looper/agents/runtimes/api.py`，名字暂定）：

- `type = "api"`，`label = "Model API (SDK)"`；`protocols = {openai-responses}`，第二阶段加 `anthropic-messages`；`auth_kinds = {api_key}`；`capabilities = {resident_session, resume}`；`can_interrupt = True`。
- `can_replace_system_prompt = True`：这个运行时没有自带的提示，追加和替换得到的是同一份文本，记录里照写所选的模式。
- 没有可执行文件：`probe` 只导入 SDK、读版本。`_detect`（`profile_registry.py:1119`）对这种类型不查 PATH，SDK 能导入就登记 `rt-api`。
- `bind_provider` 只接 API key，把 key 放进 `ProviderBinding.env` 的 `LOOPER_PROVIDER_API_KEY`，与 Codex、OMP 一样，不进 argv、记录和日志。
- `resident_command` 返回一行描述（`looper-api --protocol … --model … --base-url … --system-prompt-file …`，续接时加 `--session <id>`），给运行记录、Runs 页和重启签名用，由 `ApiSession` 自己解析，里面没有 key。
- `run_command`、`write_turn`、`parse` 这些 CLI 方法抛 `RuntimeUnsupported`。
- `environment(launch)` 只返回绑定的变量，不含 `child_env.inherited()`。这个字典只交给 `ApiSession.start`，不用来起子进程。
- `launch_record` 列出没有加载的条目：skill、插件、目录里的 MCP。

**`ApiSession`：**

- `start` 记下描述和 key（只在内存里），建 SDK 客户端；`run_turn` 跑一轮（第 6 节）；`stop` 关闭客户端；`shares_bridge = False`。
- `stale(signature)` 照现有的签名比较。签名变了只换客户端的配置，会话原文仍从表里续接。

**管家的改动**（只有这四处）：

1. `ApiSession` 另外实现一个小协议 `InProcessTools`。每轮 `run_turn`（`tiers/manager.py:1596`）之前，管家给它这一轮的 `TurnTools`：`listing()` 就是 `ops.current_listing()`；`call(name, args, call_id)` 就是 `ops.call(..., binding=token.binding, call_id=...)`。
2. 这一轮 `Turn.require_match` 取 `shares_bridge`（`tiers/manager.py:1063`），本来就是假：进程内的调用不需要和流里的调用对上。
3. 工具探测（`tiers/manager.py:1071`）对进程内工具的会话跳过：没有 MCP 子进程可探。
4. 准备一轮时，多一个换会话的原因 `SwitchReason.CONTEXT_FULL`（`session_switch.py:38`，第 7.3 节）。

## 6. 一轮怎么跑

### 6.1 消息与轮次

- 这一轮的用户文本就是管家现在交给运行时的那段文本，原样作为一条 user 消息：新会话第一轮的续接简报，加上 `_build_user_message`（`tiers/manager.py:2135`）拼好的渠道行、Projects、Ready tasks、Open gates、确认提示和停止说明。
- 循环：发请求，流式读完；有工具调用就按顺序逐个执行，把结果追加进会话，再发请求；没有工具调用的那次回复就是最终回复。
- 一次请求里的多个工具调用按模型给的顺序串行执行，Responses 传 `parallel_tool_calls: false`。工具只在这次请求的流完整结束后才执行，所以流中途出错时还没有副作用。
- 每轮最多 30 次模型请求（D10）。

### 6.2 工具

- 工具定义来自 `ops.current_listing()`，名字、说明和 `inputSchema` 原样不动，只按协议换外层：
  - Responses：`{"type": "function", "name", "description", "parameters", "strict": false}`。Looper 的 schema 里有可选参数，而严格模式要求全部必填；
  - Messages：`{"name", "description", "input_schema"}`。
- 工具列表每轮重读，所以场景管家改了工具范围，下一轮就生效。CLI 管家要为此重启进程（`tiers/manager.py:1116-1119`）。
- 工具结果是 `ops.call` 的回答，以 JSON 文本交给模型，与 `looper mcp-server` 交给 CLI 的文本相同；`status` 为 `error` 时，Messages 带上 `is_error`。单条结果最多 100 KB（第 7.4 节）。
- 模型给了不认识的工具名，或者参数不是 JSON：不调用 ops，回一条 `invalid` 结果让模型改正，写法与 ops 自己的 `INVALID` 相同。

### 6.3 确认与留痕

都不变，由 `ops.call` 自己负责：分档、两步确认（`_ask`，`ops/service.py:440`）、自主轮只读、范围、`manager_actions` 留痕。`begin_turn` 给出的确认提示照旧进 `turn_text`；`Turn.blocks`（配置块、确认说明）照旧由管家接在回复后面。API 运行时不碰这些代码。

### 6.4 流式与内部事件

`ApiSession` 把 SDK 的流翻译成内部事件（Claude Code 的 stream-json 形状，`runtimes/__init__.py:19-24`），管家的 `_converse` 照常处理：

| 来源 | 内部事件 |
|---|---|
| 一轮开始 | `system/init`，带 `session_id`、模型和工具名 |
| 文本增量（Responses 的 `response.output_text.delta`，Messages 的 `text_delta`） | `stream_event` 里的 `text_delta`，用于渠道上的流式显示 |
| 一次请求完成 | `assistant` 消息，内容有 `text`、`tool_use`（id 取 `call_id` 或 `tool_use.id`）和 `thinking`（只放推理摘要，不放加密内容）；`message.usage` 按第 8.3 节换算，供上下文规模的读数用 |
| 一次请求的用量 | `pricing.usage_event`（`runtimes/pricing.py:234`），记录器据此累计花费、判断软上限 |
| 工具执行完 | `user` 消息里的 `tool_result` |
| 一轮结束 | `result`：成功时 `subtype: success`；失败时 `is_error: true`，带有界的原因（第 6.6 节）；超过软上限时用现有的 `soft_cap_result`（`claude_cli.py:240`） |

Telegram 和飞书照旧只收最终回复，控制台照旧逐字显示。

### 6.5 停止

- 用户停止（Esc、停止按钮、`/stop`）时，请求的是这一轮的 `TurnCancel`（chat-stop-v2 第 4.1 节）。`ApiSession` 在每次请求、每次工具调用之前登记 `on_request`：
  - 正在流式读的请求：取消它的任务；
  - 正在执行的工具调用：等它做完，不打断一次写库，也不再开始下一个。
- `ops.stop_turn` 已经让这一轮之后到达的调用一律返回 `stopped`（`ops/service.py:606`）。
- 离线核对过，本地假服务、两个 SDK 都试了：取消读流的任务后约 1 ms 返回，服务端随即看到连接关闭。
- 会话原文要保持协议上合法：
  - 被取消的请求不写入；
  - 已执行的工具调用连同结果写入；
  - 同一次请求里没执行的调用，补一条「未执行：用户停止了这一轮」的结果。
- 下一轮照旧带上停止说明（chat-stop-v2 第 7 节）。
- `ResidentTurn.stopped` 为 `cancel`。被取消的那次请求没有用量，所以 `usage_complete = false`，花费记 `null`，不记 0。
- 规则 R1 不变：停止请求之后，不再把任何事件交给 `on_event`。

### 6.6 超时、重试与错误

- **客户端设置：**
  - 连接最多 10 秒；流里两块数据之间最多 120 秒；单次请求最多 600 秒；
  - `max_retries = 2`：SDK 在拿到响应之前，对连接错误和 408、409、429、5xx 重试，并遵守 `Retry-After`（据 SDK 源码 `_base_client._should_retry`）。
- **流中途断开：** 这次请求的工具还没执行，Looper 重发一次；再断就算失败。断掉那次的用量未知，`usage_complete = false`。
- **用户看到的错误**由 `failure_detail` 给出：有界，经过 `log_redaction` 去掉 key，走管家现有的失败回复（`tiers/manager.py:1666-1675`）：

| 情形 | 用户看到 |
|---|---|
| 401、403 | provider 拒绝了 API key，请到 Settings → LLM Providers 更新 |
| 404，或模型不存在 | provider 没有这个模型 |
| 重试后仍是 429、5xx 或连接失败 | 模型接口暂时不可用，附上状态码 |
| 上下文超限 | 发生在这一轮的第一次请求：还没有副作用，照「会话续接失败」处理，开新会话带简报重跑这条消息（`tiers/manager.py:1626` 的现有路径）。发生在之后的请求：这一轮失败，下一轮先换新会话 |
| 输出被截断（Responses 的 `incomplete`，Messages 的 `max_tokens`） | 回复超过长度上限，没有完成 |
| 超过 30 次请求 | 工具调用的轮数超过上限，已经停下；已执行的操作没有撤销 |

## 7. 会话与上下文

### 7.1 存储

- 新表 `api_session_items(session_id, seq, turn, protocol, model, item JSONB, created_at)`，主键 `(session_id, seq)`。条目是编解码器自己的原生格式（Responses 的 input/output item，Messages 的 message），循环不读里面的内容。
- 一次模型请求完成（连同这批工具结果）后，在一个事务里写入。
- 会话 id 由 `ApiSession` 生成（`api-<uuid>`），经 `system/init` 交给管家，照旧存进 `agent_sessions.session_id`（`schema.sql:323`）；`session_totals`、`runtime_type`、`provider_id` 的用法不变。
- `chat_messages`（每个渠道聊天的显示记录）不变，也不拿来重建模型会话，因为它没有工具调用。
- 会话被丢弃后删除它的条目，包括 `/new`、换 provider、续接失败、因上下文换会话这几种情况。审计记录仍在 `llm_record_events`。

### 7.2 续接

- 服务重启后的第一轮按 `--session` 从表里读回原文。读不到（被删了，或从没写过）就是 `session_started = false`，走现有的续接失败路径（`tiers/manager.py:1626`）。
- **协议变了不续接。** 同一个 provider 下换成另一种协议的模型时，`can_resume`（`runtimes/__init__.py:514`）只看运行时类型和 provider，会判可以续接。`ApiSession` 发现原文的协议与这次的不同，就按续接失败处理，开新会话带简报。
- **同协议换模型可以续接。** 但 Responses 的加密推理条目只回传给产生它的模型，按条目的 `model` 过滤。跨模型能不能带入，需要验证。
- **一轮只写了一半。** 服务被强杀时，一轮可能只写了一半：有工具调用，没有最终回复。续接时补一条代码写的 assistant 文本「（上一轮没有完成）」，让原文保持协议形状。这与 Claude Code 续接一个被中断的会话一样，已执行的操作不撤销。

### 7.3 上下文上限

- 窗口取模型目录里的 `context_window`，例如 CPA 的 `gpt-6.1-sol` 在预设里是 1,050,000（`provider_presets.py:191`）。没有登记窗口的模型按 200k 计。
- 每次请求后记下它的提示规模：input + cache read + cache write。超过阈值（D6）时，下一个用户轮开始前换会话：
  - 原因是 `SwitchReason.CONTEXT_FULL`；
  - 旧会话照现有规则先做一轮 `[HANDOFF]`；
  - 新会话的第一轮带 `[RESUME]` 简报（最近的对话和交接内容）；
  - 记进这次运行的 `compactions`（`trigger: auto`，换会话前后的 token 数）。
- 200k 的上限是为了花费：`gpt-6.1-sol` 的标价按 272K 以内的输入计（`runtimes/pricing.py:109`）。管家的状态大都在平台里，需要时可以用工具再读。

### 7.4 单条工具结果

超过 100 KB 的工具结果截断，末尾写明原来的长度，并建议缩小查询范围。Claude Code 对 MCP 输出本来也有上限（当前版本的默认值需要验证），所以这是对齐，不是新的限制。

### 7.5 提示缓存

- 前缀保持稳定：系统提示、工具定义、历史原文依次在前。每轮会变的内容（Projects、Ready tasks 等）本来就在用户消息里，不在前缀中。
- **Responses：** 缓存是自动的。另传 `prompt_cache_key = <会话 id>` 提高命中率，CPA 是否转发它，需要验证。CPA 会报缓存读：经 CPA 的 Codex 用量里有 `cached_input_tokens`（多运行时 RFC 第 6 节）。
- **Messages（第二阶段）：** 系统提示的最后一块、最后一条消息各打一个 `cache_control: ephemeral`。这是逐块的写法；SDK 2.3.0 还有顶层的 `cache_control`，兼容接口接不接受，需要验证。DeepSeek 会自动缓存（`runtimes/pricing.py:129`）。

## 8. Provider 层

### 8.1 协议选择

- 协议由 Profile 选中的模型决定（`ResolvedModel.model.protocol`）。`base_url` 取这个协议的 endpoint；key 取自 provider，启动时由 `agent_settings._with_api_key` 读出（`agent_settings.py:804-823`）。适配器不在账号里另挑接口或模型（`bind_provider` 的约定，`runtimes/__init__.py:349`）。
- 第一阶段只认 `openai-responses`。选了 Chat Completions 模型的 Profile，在登记时就被拒（`ResolvedModel.check_runtime`，`profile_registry.py:351`）。

**为什么第一阶段用 Responses：**

- CPA 预设的 OpenAI 接口同时登记了两种协议（`provider_presets.py:188-197`），每个模型各有一条。Looper 经 CPA 已经真实跑通的是 Responses（Codex，M9c）。OMP 经 CPA 三种协议都跑通过，OMP 自己也是优先选 Responses。
- 推理能延续：Responses 用 `include: ["reasoning.encrypted_content"]`，在 `store: false` 下把推理条目带回下一次请求，工具循环里的推理不会断。Chat Completions 没有标准字段，推理在每两次工具调用之间都会丢掉。
- effort 直接对应 `reasoning.effort`；用量里有 `cached_tokens` 和 `cache_write_tokens`（SDK 的类型已核对）。
- 代价：Responses 的流事件多，SDK 里有 59 种。编解码器只认需要的几种，其余的忽略，并记一条有界的诊断。
- 传 `store: false`、每次发完整输入：会话原文本来就存在 Looper，不依赖上游存储；CPA 是否支持存储的响应也不知道。`store: false` 是 Codex 自己的用法，推测 CPA 支持，需要验证。

**第二阶段的 `anthropic-messages`：**

- 目标是 DeepSeek、GLM（BigModel、Z.ai）、Kimi（国内、国际）和 OpenCode 的 Anthropic 接口（`provider_presets.py:140-210`）。官方 Anthropic key 就是一个没有 `base_url`（或指向官方地址）的 provider。
- 模型名去掉 `[1m]` 后缀再发（`api_model` 加 `model_names.parse_model_name`），dsh 也是这样映射的。
- OpenCode 的 Anthropic 接口只列 Claude 模型（`provider_presets.py:137`）。有它的 key 时，可以拿来跑 sonnet 级的对照。

### 8.2 客户端与凭据

- 客户端显式传入 `api_key`（或 `auth_token`）和 `base_url`。核对过：收到显式的 key 后，SDK 不再读 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`，也不读 Anthropic 的 profile 配置（`anthropic` 构造函数的说明）。
- `anthropic` 2.3.0 把 `api_key` 和 `auth_token` 都以 `Authorization: Bearer` 发出（`anthropic/_client.py:376-387`）。M9b 里，第三方 Anthropic 接口本来就用 Bearer（`ANTHROPIC_AUTH_TOKEN`），GLM BigModel 已真实跑通；DeepSeek、Kimi、OpenCode 接不接受，需要验证。
- 但 SDK 构造客户端时仍会读服务进程的另一些环境变量：`OPENAI_ORG_ID`、`OPENAI_PROJECT_ID`、`OPENAI_ADMIN_KEY`、`OPENAI_CUSTOM_HEADERS`、`ANTHROPIC_CUSTOM_HEADERS`，以及 `OPENAI_LOG`、`ANTHROPIC_LOG`（`openai` 的 `_client.py:1060-1122`、`anthropic` 的 `_client.py:693-706`，以及两者的 `_utils/_logs.py`）。它们会给请求加上额外的请求头，或打开调试日志。服务环境里有其中任何一个时，运行时在启动边界报错并写出变量名（不写值），不静默忽略。
- key 注册进 `log_redaction.add_secret`；SDK 和 HTTP 库的 logger 固定在 WARNING。key 只在内存里，不进 argv、运行记录、日志、会话原文和错误文本。API 管家没有 shell，所以 P3（key 对 agent 可见）在这个运行时上不再成立。
- 代理变量照服务环境生效，与子进程一致。CPA 在 127.0.0.1，`NO_PROXY` 需要包含它，运维说明里写明。
- **CPA 的 key：** 在产品路径上，CPA 和其他 API key provider 一样，key 存在 `llm_provider_secrets`。真实验证时，由验收脚本在运行时从 CPA 的配置文件把 key 读进内存，经 eval 的 `api_key_env` 交给被测 provider（多运行时 RFC 第 7 节「开发测试」、dsh RFC 第 9 节），不回显，不写文件。

### 8.3 用量与花费

| Looper 字段 | Responses（`usage`） | Messages（`message_start` + `message_delta`） |
|---|---|---|
| `input_tokens`（未缓存的输入） | `input_tokens − cached_tokens − cache_write_tokens` | `input_tokens` |
| `cache_read_tokens` | `input_tokens_details.cached_tokens` | `cache_read_input_tokens` |
| `cache_creation_tokens` | `input_tokens_details.cache_write_tokens` | `cache_creation_input_tokens` |
| `output_tokens` | `output_tokens`（含推理） | `output_tokens` |

- Responses 的 `input_tokens` 含缓存读，这是 OpenAI 的口径，Codex 经 CPA 的用量也是如此。它是否也含缓存写、CPA 实际报什么，需要验证。算出负数时按 0 计，并标 `usage_complete = false`。
- Messages 的口径与 dsh 的发布代码一致：缓存不含在 `input_tokens` 里（dsh RFC 第 7 节）。
- 花费：`cost_source = provider_price`，按模型目录的价格算，目录里没有就用内置价目表（`runtimes/pricing.py:103`，`gpt-6.1-sol` 和 `deepseek-v4-pro` 都在表里）；查不到价格就只记 token。CPA 背后是订阅，记的是按标价折算的等价花费。
- 每次请求一条 `usage_event`。管家每轮的软上限（`run_budget_usd`）照旧在两次请求之间判断，可能多出一个请求。

### 8.4 各接口的能力差异

不在运行时里探测，因为探测会花钱、要 key。做法：

- 按协议给出默认的请求参数；
- 模型目录的 `efforts` 决定推理，空列表表示不推理，同 OMP、Pi；
- 第一次真实请求里接口拒绝某个参数时，失败原因写明是哪个参数；
- 只有真实运行证明某家不同，才按 `VendorType`（类型化的厂商字段，`runtimes/__init__.py:96`）加一条覆盖，不按 URL 子串判断。

✅ 已核实，❓ 需要验证：

| 能力 | Responses（CPA） | Messages（DeepSeek、GLM、Kimi、OpenCode） |
|---|---|---|
| 工具调用 | ✅ Codex 经 CPA 调用 MCP 工具（M9c）；❓ 直接用函数工具 | ✅ Claude Code + GLM BigModel（v2.6.0）；✅ dsh 在 DeepSeek Messages 上，四个角色都调用了 MCP 工具（dsh RFC 第 9 节）；❓ Kimi、OpenCode |
| 流式 | ✅ Codex 经 CPA | ✅ GLM（Claude Code）；❓ DeepSeek（dsh 的 JSONL 不是 token 流） |
| 推理、effort | ✅ `model_reasoning_effort` 经 CPA；❓ 在 `store: false` 下回传加密推理 | ❓ `output_config.effort`、`thinking` 和签名回传；dsh 对 DeepSeek 用 `low/high/max` |
| 提示缓存 | ✅ 会报缓存读（Codex 经 CPA）；❓ `prompt_cache_key` | ✅ DeepSeek 自动缓存，用量分桶按 dsh 代码映射；❓ 真实数值、`cache_control` 是否被接受 |
| 流里的用量 | ✅ SDK 类型里有 `response.completed.usage`；❓ CPA 实际给不给 | ❓ `message_delta.usage` |
| 关闭并行工具调用 | ❓ `parallel_tool_calls: false` | 不传，Looper 串行执行即可 |

Looper 的 effort 档位是 `low/medium/high/xhigh/max`（`agent_settings.py:126`），与两个 SDK 的取值同名（核对过 `ReasoningEffort` 和 `OutputConfigParam.effort`），所以不需要映射表。

## 9. 模型可见内容与评测

**沿用不改：**

- 管家的系统提示：harness、角色、`TOOLS_CONTRACT`（`tiers/manager.py:172`）、场景管家的范围；
- 工具的名字、说明和 schema；
- 每轮的用户消息；
- 工具结果的文本。

管家内置的提示词模式本来就是 `replace`（system-prompt-mode RFC 决定 1），CLI 管家看到的主要也是 Looper 的文本；提示里的工具名本来就写成 `looper_*`，不用改。

**变化：**

- 没有 CLI 自带的工具定义（Bash、Read 等）和环境说明，也没有 skill 和插件。预计每次请求少约一万 token 的运行时自带内容：M1.6 实测 Claude Code 最小的一次运行，首个请求就有 15,674 token，而本运行时的工具定义约 15 KB（管家工具 RFC 第 11 节）。这是推测，第 12 节第 7 步实测。
- 新增一段运行时说明，与其他运行时一样由 `runtime_prompt_text` 放在 harness 前面。大意是：「你只有 Looper 的平台工具（looper_*），不能读文件、执行命令、上网或使用 skill；需要这些时，向用户提议建任务。」
- 单条工具结果截断时附的那句说明。

**评测**（计划第 1 节第 5 条）：这些是模型可见内容的改动，而且运行时变了，模型也不是 Claude，所以按「新运行时担任一个角色，M6 评测全部通过」（多运行时 RFC 第 11 节）跑全部对话，不只跑改动涉及的。

- **阶段 1：** 管家用 `api` + CPA + `gpt-6.1-sol`，effort `high`。这是 CPA 预设里最强的一档，`luna` 是小档。全部对话 `--reps 2`。
  - 对照组：同一模型的 Codex 管家（`codex` + CPA + `gpt-6.1-sol`），两者只差运行时；
  - 再与 `dialogs-real-opus.json` 做描述性的比较。模型不同，差异不归因于运行时；
  - 其他角色照旧用 `--model sonnet`。
- **阶段 2：** 管家用 `api` + DeepSeek 的 Anthropic 接口 + `deepseek-v4-pro`，全部对话 `--reps 2`。
  - 对照组：Claude Code + 同一个 DeepSeek 接口和模型（M9b 的路径）；
  - GLM、Kimi、OpenCode 有 key 时，跑 `status`、`confirm_create_start`、`cancel_confirmation` 三个对话，没有 key 就报告缺口。
- 从不用 haiku 当管家，也不拿 haiku 的基线当门槛。
- **评测需要一个只给管家登记 Profile 的选项：** `dialogs --manager-agent SPEC.json`。现有的 `--register-agent` 把 Profile 放进系统层、四个角色都用它（`dialogs.py:2252-2256`），而 API Profile 放在系统层会被 `check_roles` 拒绝。示例 spec 放在 `evals/agents/api-cpa.example.json`，里面只写 `api_key_env`。
- fake 模式的对话仍由假 Claude 扮演管家，它测的是管家的逻辑；API 运行时自己的协议行为，由第 13 节的假服务测试覆盖。

## 10. 可观测性

- **日志：** 每次模型请求一行结构化日志：管家 id、协议、模型、`base_url` 的主机名、HTTP 状态、耗时、首个 token 的耗时、重试次数、四类 token、上游的请求 id（`x-request-id` 或响应 id）。不写内容，不写 key。
- **运行记录：** 照旧每轮一条 `llm_records`（记录器、`cost_source`、`context_tokens`、`compactions`），事件进 `llm_record_events`。`metadata.api` 多记：协议、请求数、重试数、各次请求的上游 id（最多 30 个）、`usage_complete`。
- **看板：** 不新建监控平台。在 Runs 页按运行时筛选，成功率、耗时、token、花费就能看出 API 管家和 CLI 管家的差别；未知的花费单列，不填 0。
- **出了问题怎么发现、怎么定位：**
  - 发现：用户在聊天里直接看到第 6.6 节的错误；
  - 定位：服务日志按请求 id 找到上游的那次请求；`manager_actions` 看工具调用；`llm_record_events` 还原整轮。
  - 先在真实路径上跑稳，再决定要不要在 `/api/status` 里加计数（与 `turn_binding` 并列）。

## 11. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 兼容接口的参数差异（第 8.4 节的 ❓ 项） | 请求被 400 拒绝，或用量不准 | 按协议给保守的默认值；错误里写明是哪个参数；真实运行后更新第 8.4 节；需要时按 `VendorType` 加类型化的覆盖 |
| 非 Claude 模型当管家的质量（不先确认就建任务、声称做了其实没做、选错工具） | 误操作 | 高影响操作由代码两步确认（管家工具 RFC D2），自主轮只读；D11 的门槛；不作为默认 |
| 进程内运行，故障半径扩大到服务进程 | SDK 卡住事件循环，或内存增长 | 全部用异步客户端；每次请求都有超时；异常止于这一轮；每个管家一个客户端；方案 (c) 留作退路 |
| key 在服务内存里，SDK 会读环境变量 | 额外的请求头或日志把东西带出去 | 显式传凭据；第 8.2 节列的环境变量在启动边界报错；`log_redaction` |
| 依赖变重 | 安装体积、供应链 | `openai` 3.37.0 带进 `httpx2`、`pydantic`、`anyio`、`jiter` 等；`httpx2` 与 Looper 依赖的 `httpx<1` 是两个不同的包，不冲突。设上界、延迟导入、CI 覆盖 |
| API 管家做不了 CLI 管家能做的事（读文件、跑命令） | 用户说「看看这个仓库」，得不到直接的回答 | 运行时说明让它提议建任务；第 2 节和发布说明里写明 |
| 长会话的花费 | 每次请求都重发整个上下文 | 前缀缓存；D6 的阈值；`/new` |

## 12. 上线与开发排期

- **启用：** 不加开关，选 Profile 就是开关。可以在三处选一个 `api` Profile：Settings → Roles → Manager；某个场景管家的 Profile；或者在一个聊天里发 `/profile <n>`。最后一种只影响这条会话，最适合试用。
- **退回：** 发 `/profile 0`，或者选回 CLI Profile。换运行时按现有规则，先交接再开新会话（管家会话 RFC D3）。不自动退回。

### 12.1 开发排期

每一步一个 PR，各自是完整、可单独回滚的结果（计划第 1 节第 1 条）。第 4、5 步可以并行。

| 步 | 内容 | 依赖 | 完成时 |
|---|---|---|---|
| 1 | 运行时类型与 Profile、provider 接线：`api` 适配器、类型化的角色限制、按 SDK 检测、只接 API key 的 Responses 绑定、描述行；`openai` 依赖 | — | 适配器先不进 `ADAPTERS`，用户看不到；注册表的角色限制与无可执行文件的检测有测试 |
| 2 | 协议无关的循环与 Responses 编解码器：`AgentLoop`、`ProtocolCodec`、重试与超时、请求上限、错误分类、每次请求的用量；本地假 Responses 服务 | 1 | 不接管家；循环在假服务上跑通文本、工具循环和各种错误 |
| 3 | 管家跑在 `api` 上：`ApiSession`、进程内平台工具（`TurnTools` → `ManagerOps.call`，与 MCP 桥给出的文本和留痕一致）、单条工具结果上限、内部事件翻译、管家的改动、运行时说明、软上限；适配器进 `ADAPTERS` | 1、2 | 管家能在 `api` Profile 上回答、调工具、两步确认；会话只在进程内，重启后走续接失败的现有路径；停止仍被拒绝 |
| 4 | 会话表与续接、上下文换会话：`api_session_items`、跨重启续接、协议不同不续接、半轮补齐、会话丢弃后删除；`SwitchReason.CONTEXT_FULL`、阈值、第一次请求超限时重跑 | 3 | 重启后接着同一会话；长会话按 D6 交接换新 |
| 5 | 停止与用量记录：`can_interrupt = True`、取消请求、停止后原文合法、`usage_complete`、`metadata.api`、每次请求的结构化日志 | 3 | 停止在控制台和渠道上可用；Runs 里每轮的 token、花费、未知用量都对 |
| 6 | 入口：控制台（运行时卡、Profile 表单、Roles、停止按钮）、Telegram 与飞书的 `/profile`、`/stop`，CLI 与 `loopercli` 的核对；`agent-system.md` 一节 | 5 | 各入口按第 2 节表现，截图一张 |
| 7 | CPA 真实验证与评测：`dialogs --manager-agent`、示例 spec、验收脚本；隔离服务上的真实 smoke；全部对话与同模型 Codex 管家对照；把结果写回第 8.4、15 节 | 4、5、6 | D11 的门槛 |

**阶段 2**（阶段 1 全部合入之后）：

| 步 | 内容 | 依赖 |
|---|---|---|
| 2.1 | `anthropic` 编解码器：Bearer 认证、`[1m]` 后缀、缓存标记、thinking 块回传、`max_tokens`；假 Messages 服务；`anthropic` 依赖 | 阶段 1 第 2、3 步 |
| 2.2 | DeepSeek 真实验证：key 在运行时读进内存，全部对话与 Claude Code + DeepSeek 对照；GLM、Kimi、OpenCode 有 key 时跑三个对话；核实第 8.4 节右列 | 2.1 |

**之后：** 需要时再做 Chat Completions 编解码器；目录里的外部 MCP；norma 作为另一种 `AgentLoop`（M9d）；规划者（需要工作区工具，另立 RFC）。

## 13. 测试计划

按计划第 1 节第 4 条：测在离入口最近的那一层，不测 mock 的调用链。

- **单元测试（参数化）：**
  - 编解码器的请求参数：系统提示、工具外层、`strict: false`、effort、缓存键；
  - 流事件到内部事件的翻译；
  - 用量换算表的每一行，含算出负数和缺字段的情况；
  - 错误分类：401、404、429、5xx、上下文超限、输出截断、轮数超限；
  - 环境变量的拒绝；
  - 角色限制：`api` Profile 放在系统层、配给规划者，都被拒。
- **假服务测试**（真实 SDK 加本地 HTTP 假接口，不 mock SDK）：
  - 一轮纯文本；一轮里多个工具调用串行执行；工具出错；
  - 流中途断开时重发一次；
  - 停止：在读流时；在执行工具时（结果写入，没执行的补「未执行」）；
  - 软上限；
  - 续接：读回原文；原文缺失时走续接失败；协议不同时开新会话；
  - 提示规模超过阈值时触发换会话。
- **PostgreSQL 测试**（一次性数据库，`LOOPER_TEST_DATABASE_URL`）：`api_session_items` 每次请求写入、事务边界、会话丢弃后删除；`agent_sessions` 和 `manager_actions` 读回。
- **真实路径：**
  - 本 worktree 的 `looper serve`、全新数据库、8767 以外的端口、一次性的 `--runtime-root`；
  - 确认 `sys.executable` 和 `looper.__file__` 指向本 worktree；
  - 外部渠道关闭，用替身渠道；
  - CPA 的 key 由脚本在运行时读进内存。
- **评测：** 见第 9 节。
- PR 的 Validation 里写明新增的测试行数和生产代码行数。

## 14. 维护者的决定（2026-10-24）

以下 9 项都按本文的建议批准，已写进第 4 节：

1. 方案：新运行时类型 `api`，v0 只给管家用（D1）。
2. 第一阶段的协议：Responses，`store: false`，每次发完整输入；不做 Chat Completions（D2）。
3. SDK 依赖：普通依赖，设上界、延迟导入，不做可选的 `looper[api]`（D9）。
4. 工具执行：进程内调用 `ManagerOps.call`，不经 MCP，也不经 HTTP（D4）。
5. 会话原文：存进 PostgreSQL 新表，会话丢弃后删除（D5）。
6. 上下文阈值：窗口的 70%，且不超过 200k token，到了就交接、换会话（D6）。
7. 能力边界：v0 的 API 管家只有平台工具，外部 MCP 放到之后。
8. 每轮最多 30 次模型请求（D10）。
9. 准入门槛：阶段 1 用 `gpt-6.1-sol`、阶段 2 用 `deepseek-v4-pro`，全部对话 `--reps 2`；每个对话的通过次数不低于同模型 CLI 对照减 1；确认类对话（`cancel_confirmation`、`config_tools`、`provider_key`、`schedule_*`、`client_relay_cancel`）全部通过（D11）。CPA 实际还提供更强的一档（例如 `gpt-6-astra`）时，阶段 1 改用它。

## 15. 需要验证（汇总）

都要在真实运行里核实，结果写回第 8.4 节：

- CPA 实际提供哪些模型（是否有 `gpt-6.1-sol`）；是否接受 `store: false`、`include: ["reasoning.encrypted_content"]`、`prompt_cache_key` 和 `parallel_tool_calls: false`；流里是否带 `response.completed.usage`；`input_tokens` 是否含缓存写；
- Responses 的加密推理条目能否跨模型带入；
- DeepSeek、Kimi、OpenCode 的 Anthropic 接口是否接受 Bearer、`cache_control` 和 `thinking` / `output_config.effort`；它们流里的用量分桶；
- SDK 在流中途断开时不重试：这是读源码得出的，没有实跑；
- Claude Code 当前版本对 MCP 输出的默认上限；
- 每次请求少约一万 token 的估计；
- 非 Claude 模型担任管家的质量：只有评测能回答。
