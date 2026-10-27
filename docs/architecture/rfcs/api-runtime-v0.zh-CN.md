# RFC：API 运行时 v0（管家直连模型 API）

- **状态：** 已批准（2026-10-24），按维护者 2026-10-26 的决定更新。阶段 1 的运行时类型与接线已合入；中性循环（2a）、版本化条目和来源 notice 已合入；Responses 编解码器（2b）与本地假服务已合入；管家会话与进程内平台工具已合入。中性会话持久化、跨重启续接、drop 清理与上下文交接换会话已在分支实现；停止与用量记录已实现，原创摘要提示词的真实 CLI 专项对照已通过（第 9 节）。阶段 1 移植 Norma 循环，接 `openai` Python SDK，用 CPA 的 OpenAI 协议模型验证；阶段 2 接 `anthropic` Python SDK，目标是 DeepSeek、GLM、Kimi、OpenCode 的 Anthropic 格式接口，用维护者已有的 DeepSeek key 验证；以后有官方 Anthropic key，只是多登记一个 provider，不改代码。决定见第 4、14 节，排期见第 12 节；第 15 节汇总实测结果与仍需真实运行核实的项。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.0 节第 23 项。它落实 M9d 的 SDK 运行时与 Norma 循环移植，以及[管家平台工具 RFC](manager-platform-tools-v0.zh-CN.md) D8：「原生 tool 不单做，等 SDK 运行时上线时从同一份操作目录生成」。
- **相关：** [多运行时](multi-runtime-agents-v0.zh-CN.md)、[dsh 运行时](dsh-runtime-v0.zh-CN.md)、[停止管家的回复 v2](chat-stop-v2.zh-CN.md)、[管家会话的 Agent Profile](manager-session-profile-v0.zh-CN.md)、[场景管家](scenario-managers.zh-CN.md)、[LLM 模型层](llm-models-v0.zh-CN.md)。
- **核对基线：** 仓库路径与符号按 `main` `61a097507`（v2.13.133）核对；SDK 接口沿用初版的一次性虚拟环境离线核对结果：`openai` 3.37.0、`anthropic` 2.3.0。该次离线核对没有发出带 key 的请求。
- **路径约定：** 以 `looper/`、`evals/` 开头的路径相对仓库根目录，其余代码路径相对 `looper/agents/`；`schema.sql` 指 `looper/db/schema.sql`，`dialogs.py` 指 `evals/prompt_regression/dialogs.py`；SDK 源码的行号对应上面两个版本。

## 1. 背景与目标

**现状：**

- 原有管家运行时使用 CLI 进程；`api` 已能在服务进程内回答并调用平台工具。CLI 路径中，Claude Code 常驻；Codex、OMP、Pi、DSH 每轮起一个进程；Grok 走 ACP（`runtimes/__init__.py:218-248` 的 `ResidentSession`）。管家只用 Looper 的平台工具，却要拉起整套编码 harness：运行时自带的提示和工具定义、`looper mcp-server` 子进程，还有每 5 分钟一次、起子进程的工具探测（`ops/service.py:297`、`tiers/manager.py:1113`）。
- 每轮一个进程的运行时不能中断，所以管家拒绝停止（chat-stop-v2 第 6.6 节）。
- provider 的 key 进了 agent 子进程的环境，不限权限的 agent 能读到（多运行时 RFC P3）。

**目标：**

- 新增一种运行时，在 `looper serve` 进程里用官方 SDK 直接调用模型 API，跑管家的 agent 循环。工具就是 Looper 的平台操作，在进程内调用。
- 走现有的 provider 层：协议取自 Agent Profile 选中的模型（`ModelSpec.protocol`），`base_url` 取自这个协议的 endpoint，key 取自 provider（`profile_registry.py:270-372`）。
- 阶段 1 将 Norma 的 agentcore turn/tool cycle，以及所需的 harness、llm、tool、hook、transcript 行为移植为类型化的 asyncio Python，放在 `AgentLoop` 后面；不照搬 Go 写法，不引入新框架。循环与协议无关，Responses 是第一个 `ProtocolCodec`，阶段 2 加 Messages 编解码器。
- 两步确认、留痕、轮次归属、停止和花费记录都与现在完全一样。

**不做：**

- v0 只给管家用。规划者、执行者、评审者要读写工作目录、跑命令，另立 RFC。
- 不加载 skill、插件和能力目录里的外部 MCP 服务器（第 9 节）。
- 不自动回退到 CLI 管家（同管家会话 RFC D7）。
- 不改 CLI 运行时的任何行为，也不做按模型的 prompt 变体（多运行时 RFC P4）。
- 阶段 1 不加模型可调用的文件或进程工具，不移植 Norma 的 builtin workbench 工具、`noa` 或 `noaadapter`。以后这类工具必须限制在任务 cwd 与显式 `add_dirs` 内，使用环境白名单和每个运行时私有的 HOME；这是工具边界，不是 OS 沙箱，也不改变共享服务的 HOME。

## 2. 对用户的变化

| 入口 | 变化 | 不变 |
|---|---|---|
| 控制台 | Settings → Agent Runtimes 多一张「Model API (SDK)」卡：按 Python 包自动检测，显示 SDK 版本与「Runs inside looper serve」，能跑的角色只有 Manager；缺包时显示 Not detected 与重装 Looper 的探测错误，不显示路径。保留 Re-detect，不再单列 planned Norma。Agent Profiles 可以选运行时 `api` 和 provider 里的 `openai-responses` 模型，沿用共享 effort 列表，不显示 agent teams。Roles、项目与任务的选择器只有 Manager 能选它。聊天的停止按钮可用（真的中断）。Runs 页里，这类运行的 command 是一行描述，不是 argv | 流式回复、配置块、确认说明、任务页、花费页 |
| Telegram、飞书 | `/profile` 会列出 API Profile，`/profile <n>` 可以选择；`/stop` 在 API 管家上真的停掉这一轮 | 渠道协议、白名单、发消息逻辑 |
| CLI 与 `loopercli` | 没有新参数。`loopercli chat` 照旧和管家对话；v1 注册表读操作列出 `api` 类型及 `in_process: true`；管家工具 `looper_agent_profiles` 能建 `api` Profile，因为运行时只是一个类型 | `looper serve`、选择 api Profile 前后的 `looper doctor` 输出、v1 OpenAPI 生成命令的方式 |
| 管家的行为 | API 管家只有平台工具：不能读文件、跑命令、上网，也不能用 skill。需要这些时，它会提议建任务（运行时说明里写明） | 平台工具的集合、范围（场景管家的 `OpsScope`）和风险档 |

默认不变：没有 Profile 选它之前，所有管家照旧跑在原来的运行时上。

## 3. 方案

| 方案 | 做法 | 复杂度 | 延迟与花费 | 故障半径 | 可维护性 |
|---|---|---|---|---|---|
| **(a) 新运行时类型 `api`**（选定） | 一个 `RuntimeAdapter`，加一个进程内的 `ResidentSession`（`ApiSession`）；阶段 1 将 Norma 循环及所需行为移植成类型化 asyncio Python，置于 `AgentLoop` 后，每种协议一个编解码器；v0 只开放管家 | 中：适配器里的 CLI 方法要给出明确的「不支持」；管家接线 | 没有进程、MCP 子进程和探测子进程；请求里也没有 CLI 自带的提示和工具定义 | 一个会话对象；循环和 SDK 的异常止于这一轮 | 复用 Profile、`/profile`、换会话与交接、记录器、花费、停止和评测；规划者及工作区工具另立 RFC |
| (b) 管家专用循环 | 在 `ManagerAgent` 里按运行时分支，直接调用 SDK 和 `ManagerOps` | 起步低，之后高：记录、停止、续接、交接、花费都要在管家里再写一遍 | 同 (a) | 同 (a) | 差：`tiers/manager.py` 已承担会话与轮次管理，每个管家功能都要维护两条路径；违反「tier 不自己起运行时」（`runtimes/__init__.py:5-7`）；规划者用不上 |
| (c) 进程外的 API 运行器 | 新命令 `looper api-agent` 在子进程里跑循环、输出 stream-json，工具仍经 `looper mcp-server`，复用 `PerTurnSession` | 中 | 每轮要起进程和 MCP 子进程（约 0.3–1 秒，管家工具 RFC 第 10 节）；停止又变回杀进程 | 最小：崩溃不影响服务 | 多一套进程协议；进程内的好处（能中断、不要桥、key 不进子进程）都没有了 |

选 (a)。阶段 1 只移植所需循环行为，工具仍是进程内的平台操作，没有完整编码 harness 的文件或进程工具；模型请求用异步 SDK，与 Telegram、飞书和数据库客户端共享事件循环。多运行时 RFC P11 对完整 harness 的子进程建议不用于这里；移植也不新增 `norma` 运行时类型。另外也考虑过让 SDK 自带的循环来跑工具（`anthropic` 的 `beta.messages.tool_runner`、OpenAI Agents SDK），没有采用：两家各一套，停止、软上限、轮次归属和留痕都插不进去。

## 4. 决定

全部已定：阶段顺序与原方案于 2026-10-24 批准；循环来源、中性会话、许可、交接提示词和工具边界按维护者 2026-10-26 的决定替换（第 14 节）。

| # | 决定 | 理由 |
|---|---|---|
| D1 | 新运行时类型 `api`（方案 a），v0 只能跑管家。适配器加一个类型化字段 `roles = {"manager"}`，`servable_roles` 和 `check_roles`（`profile_registry.py:729、794`）按它收窄 | 它有 `resident_session` 和 `resume`，按 `ROLE_CAPABILITIES`（`runtimes/__init__.py:174`）会被算成也能跑规划者。用类型化字段而不新增能力，已登记运行时存下的能力就不用迁移 |
| D2 | 第一阶段 `openai` SDK + CPA，第二阶段 `anthropic` SDK + DeepSeek（另有 GLM、Kimi、OpenCode）。第一阶段只做 OpenAI Responses，传 `store: false`，每次请求发完整输入，不用 `previous_response_id` | 第 8.1 节 |
| D3 | 阶段 1 把 Norma 的 agentcore turn/tool cycle 和所需 harness、llm、tool、hook、transcript 行为移植成类型化 asyncio Python，置于 `AgentLoop` 后面（第 5 节）；`ProtocolCodec` 在请求边界翻译 wire 数据与中性条目，Responses 最先接入 | 一个 `api` 运行时，v0 只跑管家；阶段 2 只加编解码器；不引入 Go 写法或新框架 |
| D4 | 工具在进程内调用 `ManagerOps.call`（`ops/service.py:333`），带上这一轮的 binding 和模型给的调用 id，不经过 MCP 桥、HTTP 和令牌 | 校验、分档、确认和留痕仍只有一处（管家工具 RFC D5），进程内调用的就是 HTTP 处理器调用的那个函数（`http_server.py:875-882`） |
| D5 | Looper 在 PostgreSQL 中拥有协议中性的会话：`api_sessions(session_id PRIMARY KEY, protocol)` 每会话一行元数据；`api_session_items(session_id, seq, turn, model, item JSONB, created_at)` 存版本化的中性消息、工具调用、工具结果与推理条目。首次完成请求时，元数据和条目批次在同一事务内写入；之后每次请求原子追加；丢弃时删除元数据并级联删除条目 | `agent_sessions` 仍是 agent 指向当前会话的指针；codec 在请求时翻译原生 wire 数据，不把 native output 数组当作中性格式；v0 换协议仍开新会话 |
| D6 | 上下文接近上限时仍走「交接 + 新会话 + 续接简报」；阈值取窗口的 70%，且不超过 200k token。Looper 为 `api` 原创交接摘要提示词，不复制 Norma 源自 Claude Code 的 compaction prompt、措辞或示例；CLI 交接文本不变 | 会话与上下文实现负责该提示词及计划 §1 要求的定向真实 Claude CLI 对照；最终 CPA 全对话准入仍单独执行（第 7.3、9 节） |
| D7 | 停止就是取消正在进行的模型请求，`can_interrupt = True` | 离线核对过：取消 SDK 的流立即返回，服务端也看到连接关闭（第 6.5 节） |
| D8 | 不回退：API Profile 跑不了时，这一轮回错，用户用 `/profile` 换回去 | 同管家会话 RFC D7 |
| D9 | SDK 作为普通依赖，设上界，只在这个运行时里延迟导入 | 第 14 节第 3 项 |
| D10 | 每轮最多 30 次模型请求，超出就以错误结束这一轮 | CLI 管家没有上限。30 次足够建任务、确认、改设置这类对话，也能挡住死循环 |
| D11 | 准入门槛：所有对话 `--reps 2`，每个对话的通过次数不低于同模型 CLI 对照减 1，确认类对话全部通过 | 第 9 节；达不到的，发布说明里只写「实验」 |

## 5. 运行时与接口

三层，各有一个小接口：

| 层 | 职责 | 接口 |
|---|---|---|
| `ApiSession` | tier 看到的常驻会话：会话 id、中性条目存取、停止的接线、内部事件、续接 | `ResidentSession`（现有，`runtimes/__init__.py:218`），外加 `InProcessTools.set_turn_tools(tools)` |
| `AgentLoop` | Norma 循环的类型化 asyncio Python 移植：输入系统提示、用户文本、中性会话条目、工具列表；输出流式事件和工具调用（经 `TurnTools`）；能被取消；报告用量 | `loop.py:AgentLoop.run(...) -> LoopResult`、`aclose()`；`NormaAgentLoop(codec)` 可注入脚本编解码器 |
| `ProtocolCodec` | 请求时把中性条目编码为原生 wire 输入，把原生流事件与输出解码为中性事件和条目；负责协议参数及最小 opaque replay 字段的重放 | `loop.py:ProtocolCodec.stream(...) -> CodecStream`、`aclose()`；Responses（阶段 1）已实现，Messages（阶段 2）待实现；不拥有存储格式 |

Norma 是阶段 1 的循环来源。后续唯一上游是 Go 移植版的副本 `loopgo-main:go/internal/norma`；制作该副本是独立任务，不属于 Python 实现。这里只保留 Looper 需要的 harness、llm、tool、hook、transcript 语义，以 Python 类型和 asyncio 表达；不移植 builtin workbench 工具、`noa` 或 `noaadapter`，不引入新框架。

**来源与许可：** 维护者明确决定按 Norma 的 MIT 来源保留 `Copyright (c) 2026 The Norma Authors` 和完整 MIT 文本，已放在 `looper/agents/runtimes/api/THIRD_PARTY_NOTICES.txt`；派生模块的 docstring 引用它，并随 wheel 分发。Looper 继续使用 Apache-2.0。这是维护者提供的许可决定，不声称在源快照中发现了许可文件；来源为 main 快照 `2f158eaa6e6eda46389cb429d5d61dd054665722`。

**循环契约（2a，已合入）：** `Transcript.items/append` 使用冻结的 `SessionItem`（消息、原始工具调用、工具结果、推理）；JSON 使用 `version: 1` 与显式 `kind`，坏格式抛 `TranscriptFormatError`。每个完成请求连同工具结果原子追加，首次追加才存用户输入；串行工具只在完整响应后执行。`CodecStream.snapshot()` 留下失败或取消请求的未知用量；断流一轮最多重发一次，计入请求上限并校验、抑制已显示前缀。`ReasoningItem` 仅携带摘要和最小 `ReplayState`，具体 codec 须在副本上按来源协议和模型筛选 opaque state，保留原历史。`AgentLoop.aclose()` 释放协议客户端；`loop_for` 为 Responses 懒加载新的 `NormaAgentLoop(ResponsesCodec())`，其余协议明确拒绝启动；客户端由该循环独占，重复轮次复用，关闭可重复执行。

**工具边界：** 任何 api-loop 文件或进程工具都必须在任务 cwd 与显式 `add_dirs` 内工作，采用环境白名单和每个运行时私有 HOME，不能继承整个服务进程环境；这不是 OS 沙箱，也不能通过修改共享服务的 HOME 来实现。阶段 1 不提供这些工具，只给 Manager 进程内平台操作。可信服务自身的持久化和提示文件 I/O 不属于模型可调用的文件工具。

**适配器**（`looper/agents/runtimes/api/adapter.py`，已加入 `ADAPTERS`；以下同时列出后续接线要求）：

- `type = "api"`，`label = "Model API (SDK)"`；`protocols = {openai-responses}`，第二阶段加 `anthropic-messages`；`auth_kinds = {api_key}`；`capabilities = {resident_session, resume}`；`roles = {manager}` 限定只跑管家；`in_process = True`；`can_interrupt = True`。
- `can_replace_system_prompt = True`：这个运行时没有自带的提示，追加和替换得到的是同一份文本，记录里照写所选的模式。
- 没有可执行文件：`probe` 只用 `importlib.metadata.version("openai")` 读包版本，不导入 SDK、不请求模型。`_detect` 对进程内类型不查 PATH，包已安装时才登记 `rt-api`；缺包时说明需要重装 Looper。`openai>=3.37,<4` 是普通依赖，SDK 只在这个运行时的函数内延迟导入。
- `bind_provider` 只接 API key，把 key 放进 `ProviderBinding.env` 的 `LOOPER_PROVIDER_API_KEY`，与 Codex、OMP 一样，不进 argv、记录和日志。
- `resident_command` 返回一行描述（`looper-api --protocol … --model … --base-url … --system-prompt-file …`，续接时加 `--session <id>`），给运行记录、Runs 页和重启签名用，由 `ApiSession` 自己解析，里面没有 key。
- `run_command`、`write_turn`、`parse` 这些 CLI 方法抛 `RuntimeUnsupported`。
- `resident_session` 返回 `ApiSession`，描述行只在进程内解析，不会交给子进程。
- `environment(launch)` 只返回绑定的变量，不含 `child_env.inherited()`。这个字典只交给 `ApiSession.start`，不用来起子进程。
- `launch_record` 列出没有加载的条目：skill、插件、目录里的 MCP。

**`ApiSession`：**

- `start` 记下描述和 key（只在内存里），创建独占循环，SDK 客户端延迟打开；`run_turn` 复用循环跑一轮（第 6 节）；`stop` 关闭循环与客户端，保留历史；`shares_bridge = False`、`can_interrupt = True`。
- `stale(signature)` 照现有的签名比较。每轮新建 `DbTranscript`，按同一个会话 id 从 PostgreSQL 读回版本化 `SessionItem`；签名改变或服务重启后仍可续接。元数据或条目不存在、协议不符、格式或版本无效时，走管家现有的新会话重跑路径。

**管家的接线**（前 3 项已合入，上下文与交接已在分支实现，真实提示词专项验收已通过）：

1. `ApiSession` 另外实现一个小协议 `InProcessTools`。每轮 `run_turn`（`tiers/manager.py:1644`）之前，管家给它这一轮的 `TurnTools`：`specs()` 映射 `ops.current_listing()`；`call(call)` 就是 `ops.call(..., binding=token.binding, call_id=...)`。
2. 这一轮 `Turn.require_match` 取 `shares_bridge`（`tiers/manager.py:1105、1248`），本来就是假：进程内的调用不需要和流里的调用对上。
3. 工具探测（`tiers/manager.py:1113`）对进程内工具的会话跳过：没有 MCP 子进程可探。切换运行时后的第一轮沿用切换前的探测判断；被拒绝的桥请求仍标记该轮并报告工具不可用。
4. 准备一轮时，多一个换会话的原因 `SwitchReason.CONTEXT_FULL`（`session_switch.py:38`，第 7.3 节）。
5. 仅 `api` 的交接轮使用 Looper 原创的摘要提示词；CLI 运行时仍用现有交接文本（第 7.3 节）。

## 6. 一轮怎么跑

### 6.1 消息与轮次

- 这一轮的用户文本就是管家现在交给运行时的那段文本，原样作为一条 user 消息：新会话第一轮的续接简报，加上 `_build_user_message`（`tiers/manager.py:2197`）拼好的渠道行、Projects、Ready tasks、Open gates、确认提示和停止说明。
- 循环：发请求，流式读完；有工具调用就按顺序逐个执行，把结果追加进会话，再发请求；没有工具调用的那次回复就是最终回复。
- 一次请求里的多个工具调用按模型给的顺序串行执行，Responses 传 `parallel_tool_calls: false`。工具只在这次请求的流完整结束后才执行，所以流中途出错时还没有副作用。
- 每轮最多 30 次模型请求（D10）。

### 6.2 工具

- 阶段 1 仅有 Manager 的进程内平台操作；不加文件、进程工具，不带 Norma builtin workbench 工具、`noa` 或 `noaadapter`。以后若加文件、进程工具，须落实第 5 节的 cwd/`add_dirs`、环境白名单和私有 HOME 边界；可信服务的持久化、提示文件 I/O 不因此变成模型工具。
- 工具定义来自 `ops.current_listing()`，名字、说明和 `inputSchema` 原样不动，只按协议换外层：
  - Responses：`{"type": "function", "name", "description", "parameters", "strict": false}`。Looper 的 schema 里有可选参数，而严格模式要求全部必填；
  - Messages：`{"name", "description", "input_schema"}`。
- 工具列表每轮只读一次，初始化事件和每个模型请求共用同一份快照；场景管家改了工具范围，下一轮就生效。CLI 管家要为此重启进程（`tiers/manager.py:1154-1161`）。
- 工具结果是 `ops.call` 的回答，以 JSON 文本交给模型，与 `looper mcp-server` 交给 CLI 的文本相同；`status` 为 `error` 时，Messages 带上 `is_error`。单条结果最多 100 KB（第 7.4 节）。
- 参数不是 JSON 对象时，不调用 ops，回一条 `invalid` 结果让模型改正，不写审计行。未知工具名交给 ops，沿用 MCP 的 `invalid` 回答且不写审计行。

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

Telegram 和飞书照旧只收最终回复。控制台从首请求开始实时转发文本增量，停止时保留已展示的文字。首请求在产生任何输出前超限才自动换会话重跑；若已展示文字后上游才报告超限，本轮报错、下一轮再换会话，避免重复展示。

### 6.5 停止

`api` 支持用户停止，沿用现有 `chat.stop_enabled` 设置和 Manager 的停止状态机。

- 用户停止（Esc、停止按钮、`/stop`）时，请求的是这一轮的 `TurnCancel`（chat-stop-v2 第 4.1 节）。循环订阅取消请求，并在每次请求、每次工具调用之前检查：
  - 正在流式读的请求：取消它的任务；
  - 正在执行的工具调用：等它做完，不打断一次写库，也不再开始下一个。
- `ops.stop_turn` 已经让这一轮之后到达的调用一律返回 `stopped`（`ops/service.py:606`）。
- 离线核对过，本地假服务、两个 SDK 都试了：取消读流的任务后约 1 ms 返回，服务端随即看到连接关闭。
- 会话原文要保持协议上合法：
  - 被取消的请求不写入；
  - 已执行的工具调用连同结果写入；
  - 同一次请求里没执行的调用，补一条「未执行：用户停止了这一轮」的结果。
- 下一轮照旧带上停止说明（chat-stop-v2 第 7 节）。
- `ResidentTurn.stopped` 为 `cancel`。首个请求前停止不发送请求，用量保持未知且 `usage_complete = true`；已有请求被取消时，保留已完成请求的用量，`usage_complete = false`，花费记 `null`，不记 0。最终答复完成后才收到停止，也保留该请求的完整用量。
- `interrupt()` 请求当前轮的取消，最多等 5 秒后返回 `cancel`；没有运行中的轮次时返回 `None`。未完成请求保留请求快照，不补发 `RequestDone`。
- 规则 R1 不变：停止请求之后，不再把任何事件交给 `on_event`。

### 6.6 超时、重试与错误

- **客户端设置：**
  - 连接最多 10 秒；流里两块数据之间最多 120 秒；单次请求最多 600 秒，覆盖 SDK 重试与所有读流，取消时关闭流；
  - `max_retries = 2`：SDK 在拿到响应之前，对连接错误和 408、409、429、5xx 重试，并遵守 `Retry-After`（据 SDK 源码 `_base_client._should_retry`）。Responses 编解码器固定按这些状态重试，不接受 provider 的 `x-should-retry` 覆盖；HTTP 请求钩子记录实际尝试次数，首次尝试记 0 次重试。
- **流中途断开：** 这次请求的工具还没执行，Looper 重发一次；再断就算失败。断掉那次的用量未知，`usage_complete = false`。
- **用户看到的错误**由 `failure_detail` 给出：有界，经过 `log_redaction` 去掉 key，走管家现有的失败回复（`tiers/manager.py:1733-1737`）：

| 情形 | 用户看到 |
|---|---|
| 401、403 | provider 拒绝了 API key，请到 Settings → LLM Providers 更新 |
| 404，或模型不存在 | provider 没有这个模型 |
| 重试后仍是 429、5xx 或连接失败 | 模型接口暂时不可用，附上状态码 |
| 上下文超限 | 本轮第一次请求超限时，先用不含新消息的旧历史完成无工具交接摘要，再清理旧会话并重跑；摘要失败保留旧历史并报错。没有旧历史时保留已有交接摘要、开新会话重跑一次；后续请求超限时本轮报错，下一轮开始前交接换会话 |
| 输出被截断（Responses 的 `incomplete`，Messages 的 `max_tokens`） | 回复超过长度上限，没有完成 |
| 超过 30 次请求 | 工具调用的轮数超过上限，已经停下；已执行的操作没有撤销 |

## 7. 会话与上下文

### 7.1 存储

- 新表 `api_sessions(session_id PRIMARY KEY, protocol)`，每个 api 会话一行元数据；协议属于会话，不在每个条目上重复。
- 新表 `api_session_items(session_id, seq, turn, model, item JSONB, created_at)`，主键 `(session_id, seq)`，`session_id` 外键指向 `api_sessions`，`ON DELETE CASCADE`。`item` 是带格式版本与类型的中性条目，覆盖 message、tool-call、tool-result、reasoning；由 Looper 定义类型与存储契约，不由 codec 定义。
- 中性 reasoning 条目可附协议和来源模型标识，以及重放必需的最小类型化 opaque 字段（如加密推理、签名）；不能把整段原生 output 数组存下就称为中性格式。codec 在请求时编码、解码原生 wire 数据。
- 一次模型请求完成（连同这批工具结果）后，在一个事务里原子追加整个条目批次。同一会话的追加与删除共用 advisory lock，序号连续；首次完成请求在同一事务内创建元数据行、校验协议并写入已序列化的 v1 `SessionItem` 批次。模型请求未完成即被取消时不创建元数据、不写半个批次；已完成请求在工具执行期间遇到停止时，按第 6.5 节补齐结果后整批写入。会话原文是运行时状态，不随配置导出。
- 会话 id 由 `ApiSession` 生成（`api-<uuid>`），经 `system/init` 交给管家，照旧存进 `agent_sessions.session_id`（`schema.sql:325`）；`session_totals`、`runtime_type`、`provider_id` 的用法不变。
- `chat_messages`（每个渠道聊天的显示记录）不变，也不拿来重建模型会话，因为它没有工具调用。
- 会话被丢弃后删除 `api_sessions` 元数据并级联删除条目，包括 `/new`、换 provider、续接失败、因上下文换会话这几种情况。`agent_sessions` 仍只保存 agent 当前会话的指针及既有状态，不接管会话内容；审计记录仍在 `llm_record_events`。

### 7.2 续接

- 服务重启后的第一轮按 `--session` 从表里读回原文。读不到（被删了，或从没写过）就是 `session_started = false`，走现有的续接失败路径（`tiers/manager.py:1699-1718`）。
- **协议变了不续接。** 中性存储不承诺 v0 跨协议续接。同一个 provider 下换协议时，`can_resume`（`runtimes/__init__.py:518`）只看运行时类型和 provider，会判可以续接；`ApiSession` 读出 `api_sessions.protocol`，发现不同就按续接失败处理，开新会话带简报。
- **同协议换模型可以续接。** 重放时按来源 `model` 去掉其他模型的 opaque reasoning 字段，不把加密推理或签名发给另一个模型；仍保留可重放的中性消息、工具调用和结果。
- **一轮只写了一半。** 服务被强杀时，一轮可能停在工具结果后，没有最终 assistant 回复。续接时在下一条用户消息之前暂存 assistant 文本 `(the previous turn did not finish)`，并与下一次成功请求的首批条目原子写入一次；新请求失败不单独落修复文本。完整的空 assistant 回复也算完成，已执行的操作不撤销。

### 7.3 上下文上限

- 窗口取模型目录里的 `context_window`，例如 CPA 的 `gpt-6.1-sol` 在预设里是 1,050,000（`provider_presets.py:191`）。没有登记窗口的模型按 200k 计。
- 每次完成请求后记下它的 `prompt_tokens`。阈值为 `min(int(0.7 * (context_window or 200_000)), 200_000)`；超过阈值（D6）时，下一个用户轮开始前换会话。提示规模按会话 id 暂存内存，重启后须完成一次请求才重新可用：
  - 原因是 `SwitchReason.CONTEXT_FULL`；
  - 旧会话先做一轮 `[HANDOFF]`，`api` 使用 Looper 原创的交接摘要提示词，显式绑定零个工具，模型虚构的调用不能执行平台操作；不复制 Norma 源自 Claude Code 的 compaction prompt、措辞或示例，CLI 的交接文本保持不变；
  - 新会话的第一轮带 `[RESUME]` 简报（最近的对话和交接内容）；
  - 记进新一轮运行的 `compactions`（`trigger: auto`、`pre_tokens` 为旧会话提示规模）。第一次请求直接超限时，旧历史仍有效，先用不含新消息的旧历史完成无工具摘要，再清理旧会话、本轮在新会话重跑；后续请求超限则本轮报错、下一轮交接换会话。API 上下文交接失败、超限或没有摘要时保留旧会话和原文，本轮报错，后续可以重试交接。新会话首请求超限重试时也保留已有交接摘要。
- 200k 的上限是为了花费：`gpt-6.1-sol` 的标价按 272K 以内的输入计（`runtimes/pricing.py:109`）。管家的状态大都在平台里，需要时可以用工具再读。
- 会话与上下文实现一并交付该提示词，并按计划 §1 做定向真实 Claude CLI 对照（含交接与续接场景）；这是该实现的必需验收，不等到最终 CPA 全对话门槛才验证，也不替代它。

### 7.4 单条工具结果

仅 api 的单条工具结果超过 102,400 个 UTF-8 字节时截断，保留不拆字符的最长前缀，连同原始字节数和缩小查询范围的提示不超过该上限。CLI 的工具文本保持不变；Claude Code 当前版本的 MCP 默认上限仍需要验证。

### 7.5 提示缓存

- 前缀保持稳定：系统提示、工具定义、历史原文依次在前。每轮会变的内容（Projects、Ready tasks 等）本来就在用户消息里，不在前缀中。
- **Responses：** 缓存是自动的。另传 `prompt_cache_key = <会话 id>` 提高命中率，CPA 是否转发它，需要验证。CPA 会报缓存读：经 CPA 的 Codex 用量里有 `cached_input_tokens`（多运行时 RFC 第 6 节）。
- **Messages（第二阶段）：** 系统提示的最后一块、最后一条消息各打一个 `cache_control: ephemeral`。这是逐块的写法；SDK 2.3.0 还有顶层的 `cache_control`，兼容接口接不接受，需要验证。DeepSeek 会自动缓存（`runtimes/pricing.py:129`）。

## 8. Provider 层

### 8.1 协议选择

- 协议由 Profile 选中的模型决定（`ResolvedModel.model.protocol`）。`base_url` 取这个协议的 endpoint；key 取自 provider，启动时由 `agent_settings._with_api_key` 读出（`agent_settings.py:804-823`）。适配器不在账号里另挑接口或模型（`bind_provider` 的约定，`runtimes/__init__.py:353`）。
- 第一阶段只认 `openai-responses`。选了 Chat Completions 模型的 Profile，在登记时就被拒（`ResolvedModel.check_runtime`，`profile_registry.py:352`）。

**为什么第一阶段用 Responses：**

- CPA 预设的 OpenAI 接口同时登记了两种协议（`provider_presets.py:188-197`），每个模型各有一条。Looper 经 CPA 已经真实跑通的是 Responses（Codex，M9c）。OMP 经 CPA 三种协议都跑通过，OMP 自己也是优先选 Responses。
- 推理能延续：Responses 用 `include: ["reasoning.encrypted_content"]`，在 `store: false` 下把推理条目带回下一次请求，工具循环里的推理不会断。Chat Completions 没有标准字段，推理在每两次工具调用之间都会丢掉。
- effort 直接对应 `reasoning.effort`；用量里有 `cached_tokens` 和 `cache_write_tokens`（SDK 的类型已核对）。
- 代价：Responses 的流事件多。编解码器按输出索引与 id 跟踪条目，以 `response.completed` 的有序 output 为准；failed、incomplete、无终态或非法事件不执行工具。其余事件忽略，每个请求最多记录一条有界的事件名诊断，不记录载荷。
- 传 `store: false`、每次发完整输入：会话原文本来就存在 Looper，不依赖上游存储；CPA 是否支持存储的响应也不知道。`store: false` 是 Codex 自己的用法，推测 CPA 支持，需要验证。

**第二阶段的 `anthropic-messages`：**

- 目标是 DeepSeek、GLM（BigModel、Z.ai）、Kimi（国内、国际）和 OpenCode 的 Anthropic 接口（`provider_presets.py:140-210`）。官方 Anthropic key 就是一个没有 `base_url`（或指向官方地址）的 provider。
- 模型名去掉 `[1m]` 后缀再发（`api_model` 加 `model_names.parse_model_name`），dsh 也是这样映射的。
- OpenCode 的 Anthropic 接口只列 Claude 模型（`provider_presets.py:137`）。有它的 key 时，可以拿来跑 sonnet 级的对照。

### 8.2 客户端与凭据

- 客户端显式传入 `api_key`（或 `auth_token`）和 `base_url`。本分支 Responses 编解码器使用已安装的 `openai` 3.39.0 与其实际传输层 `httpx2`；SDK 仅在运行时函数内导入，不继承 HTTP 代理环境。核对过：收到显式的 key 后，SDK 不再读 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`，也不读 Anthropic 的 profile 配置（`anthropic` 构造函数的说明）。
- `anthropic` 2.3.0 把 `api_key` 和 `auth_token` 都以 `Authorization: Bearer` 发出（`anthropic/_client.py:376-387`）。M9b 里，第三方 Anthropic 接口本来就用 Bearer（`ANTHROPIC_AUTH_TOKEN`），GLM BigModel 已真实跑通；DeepSeek、Kimi、OpenCode 接不接受，需要验证。
- 但 SDK 构造客户端时仍会读服务进程的另一些环境变量：`OPENAI_ORG_ID`、`OPENAI_PROJECT_ID`、`OPENAI_ADMIN_KEY`、`OPENAI_CUSTOM_HEADERS`、`ANTHROPIC_CUSTOM_HEADERS`，以及 `OPENAI_LOG`、`ANTHROPIC_LOG`（`openai` 的 `_client.py:1060-1122`、`anthropic` 的 `_client.py:693-706`，以及两者的 `_utils/_logs.py`）。它们会给请求加上额外的请求头，或打开调试日志。服务环境里有其中任何一个时，运行时在启动边界报错并写出变量名（不写值），不静默忽略。
- key 注册进 `log_redaction.add_secret`；SDK 和 HTTP 库的 logger 固定在 WARNING。key 只在内存里，不进 argv、运行记录、日志、会话原文和错误文本。API 管家没有 shell，所以 P3（key 对 agent 可见）在这个运行时上不再成立。
- 代理配置只从显式环境白名单选取并交给客户端，不授权继承整个服务环境。CPA 在 127.0.0.1，选用代理时 `NO_PROXY` 需要包含它，运维说明里写明。若以后增加文件、进程工具，工具环境同样只用白名单和每个运行时私有 HOME；不改共享服务的 HOME，不宣称 OS 沙箱。阶段 1 只有进程内平台操作。
- **CPA 的 key：** 在产品路径上，CPA 和其他 API key provider 一样，key 存在 `llm_provider_secrets`。本次真实验证只在运行时通过 `LOOPER_EVAL_API_KEY` 提供 CPA 客户端 key，经 eval 的 `api_key_env` 交给被测 provider，不回显，不写入仓库。

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
- 已完成请求在停止前发送一条 `usage_event`，缺失用量时带 `usage_complete = false`；被中止的请求仅从最终请求快照计入记录，停止后不补发事件。任一请求用量不完整时，整轮花费保持 `null`，已知 token 保留，未报告的 token 保持未知。管家每轮的软上限（`run_budget_usd`）照旧在两次请求之间判断，可能多出一个请求。

### 8.4 各接口的能力差异

不在运行时里探测，因为探测会花钱、要 key。做法：

- 按协议给出默认的请求参数；
- 模型目录的 `efforts` 决定推理，空列表表示不推理，同 OMP、Pi；
- 第一次真实请求里接口拒绝某个参数时，失败原因写明是哪个参数；
- 只有真实运行证明某家不同，才按 `VendorType`（类型化的厂商字段，`runtimes/__init__.py:96`）加一条覆盖，不按 URL 子串判断。

✅ 已核实，❓ 需要验证：

| 能力 | Responses（CPA） | Messages（DeepSeek、GLM、Kimi、OpenCode） |
|---|---|---|
| 工具调用 | ✅ Codex 经 CPA 调用 MCP 工具（M9c）；✅ api 管家直接用函数工具，真实 smoke 与全对话验证通过 D11（第 9 节） | ✅ Claude Code + GLM BigModel（v2.6.0）；✅ dsh 在 DeepSeek Messages 上，四个角色都调用了 MCP 工具（dsh RFC 第 9 节）；❓ Kimi、OpenCode |
| 流式 | ✅ Codex 经 CPA | ✅ GLM（Claude Code）；❓ DeepSeek（dsh 的 JSONL 不是 token 流） |
| 推理、effort | ✅ `model_reasoning_effort` 经 CPA；❓ 在 `store: false` 下回传加密推理 | ❓ `output_config.effort`、`thinking` 和签名回传；dsh 对 DeepSeek 用 `low/high/max` |
| 提示缓存 | ✅ 会报缓存读（Codex 经 CPA）；❓ `prompt_cache_key` | ✅ DeepSeek 自动缓存，用量分桶按 dsh 代码映射；❓ 真实数值、`cache_control` 是否被接受 |
| 流里的用量 | ✅ SDK 类型里有 `response.completed.usage`；❓ CPA 实际给不给 | ❓ `message_delta.usage` |
| 关闭并行工具调用 | ❓ `parallel_tool_calls: false` | 不传，Looper 串行执行即可 |

**CPA 实测：** 本地 CPA 代理提供了 `gpt-6-astra`，api 与 Codex 管家两份 spec 均使用它，其他角色为 Claude Code sonnet。api smoke 为 4/4、$0.44；全部对话 `--reps 2 --jobs 2`，Codex 为 78/84 个已评分项通过、另有 4 个 harness error，记录 $13.68；api 为 82/88、$9.06。双方均评分的对话花费分别为 $6.82 与 $4.53。D11 已满足；共同失败与 `session_profile_switch` 检查修正后的双方 2/2 结果见第 9 节。上述结果不单独证明加密推理、缓存参数、用量分桶或数据库读回，未有测量依据的 ❓ 项仍保留。

Looper 的 effort 档位是 `low/medium/high/xhigh/max`（`agent_settings.py:126`），与两个 SDK 的取值同名（核对过 `ReasoningEffort` 和 `OutputConfigParam.effort`），所以不需要映射表。

## 9. 模型可见内容与评测

**沿用不改：**

- 管家的系统提示：harness、角色、`TOOLS_CONTRACT`（`tiers/manager.py:175`）、场景管家的范围；
- 工具的名字、说明和 schema；
- 每轮的用户消息；
- 工具结果的文本。

管家内置的提示词模式本来就是 `replace`（system-prompt-mode RFC 决定 1），CLI 管家看到的主要也是 Looper 的文本；提示里的工具名本来就写成 `looper_*`，不用改。

**变化：**

- 没有 CLI 自带的工具定义（Bash、Read 等）和环境说明，也没有 skill 和插件。预计每次请求少约一万 token 的运行时自带内容：M1.6 实测 Claude Code 最小的一次运行，首个请求就有 15,674 token，而本运行时的工具定义约 15 KB（管家工具 RFC 第 11 节）。这是推测，第 12 节第 8 步实测。
- 新增一段运行时说明，与其他运行时一样由 `runtime_prompt_text` 放在 harness 前面。大意是：「你只有 Looper 的平台工具（looper_*），不能读文件、执行命令、上网或使用 skill；需要这些时，向用户提议建任务。」
- 单条工具结果截断时附的那句说明。
- `api` 的交接摘要提示词由 Looper 原创，属于新增模型可见内容；不复制 Norma 源自 Claude Code 的 compaction prompt、措辞或示例。CLI 交接文本保持不变。

**评测**（计划第 1 节第 5 条）：`tests/test_api_handoff_installed_db.py` 提供显式启用的专项对照：在本地 Responses 假服务上捕获生产 Manager 交接输入，用真实 Claude CLI 原样评测，再把真实摘要回放进相同的换会话链路（续接响应由替身模型提供）。公开合成对话覆盖约束、带证据的已完成动作、尚未执行且待确认的动作、承诺及未决问题；新旧 Looper prompt 分别跑 sonnet high 两次、opus xhigh 一次，opus 首次失败才补第二次。保留 `LOOPER_TEST_REAL_API_HANDOFF=1` 开关及调用、用例、总花费上限；macOS 使用仅链接 `Library/Keychains` 的临时 HOME，认证预检与摘要调用共用环境白名单：只透传 `PATH`、`TMPDIR`、`USER`、`LOGNAME` 及显式凭据变量，`HOME` 指向临时目录，不读取操作员的 Claude 配置；其他平台可使用显式凭据环境变量。原始产物只留在忽略的 `evals/runs/`。

2026-10-26 实测：补齐 keychain 账户选择所需的 `USER`、`LOGNAME` 后，隔离的 `claude auth status --json` 返回 `loggedIn=true`。在一次性 PostgreSQL 上以 `LOOPER_TEST_REAL_API_HANDOFF=1`、`pytest -n 1` 仅运行该文件，结果为 **3 passed**，无跳过或费用上限中止；数据库已删除。

| 模型与 effort | 新旧 prompt 对照 | 事实保留 | 虚构动作 | 真实摘要续接 | 费用（USD） |
|---|---|---|---|---|---|
| claude-sonnet-5-5 high（各 2 次） | 新 2/2、旧 2/2 通过 | 每次 5/5 | 新旧均 0 | 新 2/2、旧 2/2 成功 | 新 $0.0369938，旧 $0.0218680；合计 $0.0588618 |
| claude-opus-5-5 xhigh（各 1 次） | 新 1/1、旧 1/1 通过，无补跑 | 每次 5/5 | 新旧均 0 | 新 1/1、旧 1/1 成功 | 新 $0.0479120，旧 $0.0243890；合计 $0.0723010 |

总费用 **$0.1311628**。新旧 prompt 的事实、虚构动作与续接评分持平，未触发改词重跑；本次候选费用较高，但两个模型的候选首调用均为冷缓存、对照有缓存命中，不能据此推断稳定的成本差异。续接验证的是将真实摘要注入生产 Manager 的新会话并完成替身响应轮次，不是第二次真实模型推理。sonnet 对话基线仍缺该场景；本专项对照已通过，不能替代最终 CPA 全对话准入。收尾以 `pytest -n 2` 回归 API session、持久会话、context switch、handoff 与 Manager session switch：88 passed、2 skipped（仅跳过已单独执行的 opt-in 真实对比），一次性数据库已删除。

**最终准入：** 运行时变了，模型也不是 Claude，所以另按「新运行时担任一个角色，M6 评测全部通过」（多运行时 RFC 第 11 节）跑全部对话，不只跑改动涉及的；不能用提示词的定向对照替代。

- **阶段 1：** 管家用 `api` + CPA，effort `high`。预检模型列表，提供 `gpt-6-astra` 时两份 spec 都用它，否则用 `gpt-6.1-sol`，两者都没有就停止；本次 CPA 提供了 `gpt-6-astra`。全部对话 `--reps 2 --jobs 2`。
  - 对照组：同一模型的 Codex 管家（本次为 `codex` + CPA + `gpt-6-astra`），两者只差运行时；
  - 再与 `dialogs-real-opus.json` 做描述性的比较。模型不同，差异不归因于运行时；
  - 其他角色照旧用 `--model sonnet`。
- **阶段 2：** 管家用 `api` + DeepSeek 的 Anthropic 接口 + `deepseek-v4-pro`，全部对话 `--reps 2`。
  - 对照组：Claude Code + 同一个 DeepSeek 接口和模型（M9b 的路径）；
  - GLM、Kimi、OpenCode 有 key 时，跑 `status`、`confirm_create_start`、`cancel_confirmation` 三个对话，没有 key 就报告缺口。
- 从不用 haiku 当管家，也不拿 haiku 的基线当门槛。
- **评测入口已实现：** `dialogs --manager-agent SPEC.json` 每个用例登记一次 Profile，通过现行 v1 `agent_settings/update_role` 只选择 Manager 的角色层，其他角色保持 `--model` / `--agent`。它与 `--manager-model`、`--manager-effort`、`--register-agent` 互斥，fake 模式拒绝；原有 `--register-agent` 的系统层选择保持不变。示例为 `evals/agents/api-cpa.example.json` 与 `evals/agents/codex-cpa.example.json`，provider endpoint 与 model protocol 显式分开，凭据只声明 `api_key_env`。
- fake 模式的对话仍由假 Claude 扮演管家，它测的是管家的逻辑；API 运行时自己的协议行为，由第 13 节的假服务测试覆盖。

**CPA 验证结果：** 已在本地 CPA 代理完成真实 smoke 与两组全对话对照，客户端 key 仅在运行时通过 `LOOPER_EVAL_API_KEY` 提供。运行方式见 [evals/README.md](../../../evals/README.md#the-api-runtime-on-cpa)。

| 运行 | 实测结果 | 记录花费 |
|---|---|---|
| api smoke：`status`、`confirm_create_start`、`cancel_confirmation`、`stop_then_continue`，`--reps 1` | 4/4 通过 | $0.44 |
| Codex 管家对照，全部对话 | 78/84 个已评分项通过，另有 4 个 harness error：`stop_then_continue`、`withdraw_then_continue` 的两次重复均出错 | $13.68 |
| api 管家，全部对话 | 82/88 通过 | $9.06 |

双方均评分的对话记录花费为 Codex $6.82、api $4.53。**D11 已满足：** 每个对话的 api 通过次数均不低于 Codex 对照减 1；api 在各对话持平或更好，对照出现 harness error 的两个对话在 api 下均为 2/2，但对照错误仍按错误报告。`cancel_confirmation`、`config_tools`、`provider_key`、全部 `schedule_*` 和 `client_relay_cancel` 均为 2/2。

共同失败不属于 api 回归：`model_missing_endpoint` 双方均为 0/2（管家没有通过工具读取任何内容）；`start_no_wait` 双方均为 0/2（回复没有说明任务的问题会交给用户）；`session_profile_switch` 首轮双方均为 0/2，原因是 eval 检查比较了带 effort 后缀的 Profile 显示名。本分支修正检查后，该对话在 api 和 Codex 管家下均为 2/2；这次定向复跑与上表首轮全量统计分开记录。

本次测量未提供逐请求 token 差值、中性会话条目、`api_sessions.protocol` 读回或 Opus 描述性比较结论，不据此关闭第 8.4、15 节的独立能力检查。原创交接提示词专项对照已随合入的 [PR #688](https://github.com/foolzzz/looper/pull/688) 的 RFC 结果表记录，保持独立且不变；本次未改提示词，也不以全对话评测替代该专项对照。

## 10. 可观测性

- **日志：** 每次模型请求（含失败、取消）在 `looper.agents.runtimes.api` 写一条 INFO `api request`，结构化字段位于 `api_request`：管家 id、协议、模型、`base_url` 的主机名、HTTP 状态、耗时、首个 token 的耗时、重试次数、四类 token、用量完整性 `complete`、上游的请求 id（`x-request-id` 或响应 id）。不写内容，不写 key。
- **运行记录：** 照旧每轮一条 `llm_records`（记录器、`cost_source`、`context_tokens`、`compactions`），事件进 `llm_record_events`。`metadata.api` 记下 `protocol`、`requests`、`retries`、`upstream_ids`（最多 30 个）与缺少完整用量的请求数 `incomplete`；结果事件的 `usage_complete = false` 使花费保持未知。
- **看板：** 不新建监控平台。在 Runs 页按运行时筛选，成功率、耗时、token、花费就能看出 API 管家和 CLI 管家的差别；未知的花费单列，不填 0。
- **出了问题怎么发现、怎么定位：**
  - 发现：用户在聊天里直接看到第 6.6 节的错误；
  - 定位：服务日志按请求 id 找到上游的那次请求；`manager_actions` 看工具调用；`llm_record_events` 还原整轮。
  - 先在真实路径上跑稳，再决定要不要在 `/api/v1/system/status` 里加计数（与 `turn_binding` 并列）。

## 11. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 兼容接口的参数差异（第 8.4 节的 ❓ 项） | 请求被 400 拒绝，或用量不准 | 按协议给保守的默认值；错误里写明是哪个参数；真实运行后更新第 8.4 节；需要时按 `VendorType` 加类型化的覆盖 |
| 非 Claude 模型当管家的质量（不先确认就建任务、声称做了其实没做、选错工具） | 误操作 | 高影响操作由代码两步确认（管家工具 RFC D2），自主轮只读；D11 的门槛；不作为默认 |
| 进程内运行，故障半径扩大到服务进程 | SDK 卡住事件循环，或内存增长 | 全部用异步客户端；每次请求都有超时；异常止于这一轮；每个管家一个客户端；方案 (c) 留作退路 |
| key 在服务内存里，SDK 会读环境变量 | 额外的请求头或日志把东西带出去 | 显式传凭据；第 8.2 节列的环境变量在启动边界报错；`log_redaction` |
| 未来的文件、进程工具越过任务边界 | 访问任务外的文件或服务环境中的秘密 | 限定任务 cwd 与显式 `add_dirs`，环境白名单、每个运行时私有 HOME；不改服务 HOME，不声称 OS 沙箱；阶段 1 不加这些工具，可信服务自身 I/O 不属于模型工具 |
| 依赖变重 | 安装体积、供应链 | `openai` 3.37.0 带进 `httpx2`、`pydantic`、`anyio`、`jiter` 等；`httpx2` 与 Looper 依赖的 `httpx<1` 是两个不同的包，不冲突。设上界、延迟导入、CI 覆盖 |
| API 管家做不了 CLI 管家能做的事（读文件、跑命令） | 用户说「看看这个仓库」，得不到直接的回答 | 运行时说明让它提议建任务；第 2 节和发布说明里写明 |
| 长会话的花费 | 每次请求都重发整个上下文 | 前缀缓存；D6 的阈值；`/new` |

## 12. 上线与开发排期

- **启用：** 不加开关，选 Profile 就是开关。可以在三处选一个 `api` Profile：Settings → Roles → Manager；某个场景管家的 Profile；或者在一个聊天里发 `/profile <n>`。最后一种只影响这条会话，最适合试用。
- **退回：** 发 `/profile 0`，或者选回 CLI Profile。换运行时按现有规则，先交接再开新会话（管家会话 RFC D3）。不自动退回。

### 12.1 开发排期

每一步一个 PR，各自是完整、可单独回滚的结果（计划第 1 节第 1 条）。第 1–7 步已合入；第 8 步的评测入口、示例、真实 CPA smoke 与全对话对照已完成，D11 已满足，实测结果见第 8.4、9、15 节。

| 步 | 内容 | 依赖 | 完成时 |
|---|---|---|---|
| 1 | 运行时类型与 Profile、provider 接线：`api` 适配器、类型化的角色限制、按 SDK 检测、只接 API key 的 Responses 绑定、描述行、私有提示文件；`openai` 依赖 | — | 已合入；当时适配器未登记，第 4 步开放；注册表的角色限制与无可执行文件的检测有测试 |
| 2（2a） | 中性 Norma 循环与接口：将 agentcore turn/tool cycle 及所需 harness、llm、tool、hook、transcript 行为移植为类型化 asyncio Python；`AgentLoop`、`ProtocolCodec`、中性条目与工具接口；来源 notice、派生 docstring 引用和 wheel 分发 | 1 | 已合入；不接管家或 SDK；注入式测试验证中性 turn/tool 契约；不加 workbench 工具、`noa`、`noaadapter` 或新框架；制作 Go 上游副本另做 |
| 3（2b） | Responses 编解码器与 SDK：wire 与中性条目互译、重试与超时、请求上限、错误分类、每次请求的用量；本地假 Responses 服务 | 2 | 已合入；真实 SDK 经本地假服务验证文本、工具循环、错误、重试计数、用量与取消；真实 CPA 验证结果见第 8 步 |
| 4 | 管家跑在 `api` 上：`ApiSession`、进程内平台工具（`TurnTools` → `ManagerOps.call`，与 MCP 桥给出的文本和留痕一致）、单条工具结果上限、内部事件翻译、管家的改动、运行时说明、软上限；适配器进 `ADAPTERS` | 1、2、3 | 已合入；管家能在 `api` Profile 上回答、调工具、两步确认并按 provider 价格记账；会话只在进程内，重启后走续接失败的现有路径；停止见第 6 步。真实 CPA 与 api-only 文本评测结果见第 8 步 |
| 5 | 中性持久会话与上下文：`api_sessions`、版本化 `api_session_items`、首次元数据与请求批次同事务、跨重启续接、换协议新会话、跨模型不重放外来 opaque reasoning、半轮补齐、drop 级联清理；`CONTEXT_FULL`、阈值、第一次请求超限时重跑；原创 api 交接摘要提示词及其计划 §1 定向真实 Claude CLI 对照 | 4 | 重启后接着同一会话；长会话按 D6 交接换新；专项评测完成，CLI 交接文本不变 |
| 6 | 停止与用量记录：`can_interrupt = True`、取消请求、停止后原文合法、`usage_complete`、`metadata.api`、每次请求的结构化日志 | 4 | 分支实现，本地假 HTTP 与一次性 PostgreSQL 已验证：控制台与渠道取消当前请求；保留工具结果与已知 token，缺失用量时花费为未知；记录请求数、重试、不完整请求数、上游 id 与逐请求日志；真实 CPA 验证结果见第 8 步 |
| 7 | 入口：控制台（运行时卡、Profile 表单、Roles、停止按钮）、Telegram 与飞书的 `/profile`、`/stop`，CLI 与 `loopercli` 的核对；`agent-system.md` 一节 | 6 | 各入口按第 2 节表现，截图一张 |
| 8 | CPA 真实验证与评测：`dialogs --manager-agent`、示例 spec、验收脚本；隔离服务上的真实 smoke；全部对话与同模型 Codex 管家对照；把结果写回第 8.4、15 节 | 5、6、7 | 已完成真实 CPA 验证，双方用 `gpt-6-astra`，其他角色 sonnet；smoke 4/4、$0.44；Codex 78/84 加 4 个 harness error、$13.68，api 82/88、$9.06；共同评分对话 $6.82 / $4.53，D11 已满足；共同失败及 Profile 检查修正后双方 2/2 见第 9 节；独立于第 5 步的交接提示词评测 |

**阶段 2**（阶段 1 全部合入之后）：

| 步 | 内容 | 依赖 |
|---|---|---|
| 2.1 | `anthropic` 编解码器：Bearer 认证、`[1m]` 后缀、缓存标记、thinking 块回传、`max_tokens`；假 Messages 服务；`anthropic` 依赖 | 阶段 1 第 2、3、4 步 |
| 2.2 | DeepSeek 真实验证：key 在运行时读进内存，全部对话与 Claude Code + DeepSeek 对照；GLM、Kimi、OpenCode 有 key 时跑三个对话；核实第 8.4 节右列 | 2.1 |

**之后：** 需要时再做 Chat Completions 编解码器；目录里的外部 MCP；规划者（需要工作区工具，另立 RFC）。Norma 已是阶段 1 的循环来源；后续只跟踪 `loopgo-main:go/internal/norma`，副本制作独立于 Python 实现。

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
- **PostgreSQL 测试**（一次性数据库，`LOOPER_TEST_DATABASE_URL`）：首次完成请求的 `api_sessions` 元数据与中性条目批次同事务、后续请求原子追加、取消不留半批、会话丢弃后元数据删除与条目级联清理；`agent_sessions` 指针和 `manager_actions` 读回。
- **真实路径：**
  - 本 worktree 的 `looper serve`、全新数据库、8767 以外的端口、一次性的 `--runtime-root`；
  - 确认 `sys.executable` 和 `looper.__file__` 指向本 worktree；
  - 外部渠道关闭，用替身渠道；
  - CPA 的 key 仅在运行时通过 `LOOPER_EVAL_API_KEY` 提供，经 `api_key_env` 传入，不写入仓库。
- **评测：** 见第 9 节。
- PR 的 Validation 里写明新增的测试行数和生产代码行数。

## 14. 维护者的决定（2026-10-24，2026-10-26 更新）

以下决定已写进第 4 节；循环来源、中性存储、许可、原创交接提示词和工具边界采用 2026-10-26 的决定：

1. 方案：新运行时类型 `api`，v0 只给管家用（D1）。
2. 第一阶段的协议：Responses，`store: false`，每次发完整输入；不做 Chat Completions（D2）。
3. SDK 依赖：普通依赖，设上界、延迟导入，不做可选的 `looper[api]`（D9）。
4. 工具执行：进程内调用 `ManagerOps.call`，不经 MCP，也不经 HTTP（D4）。
5. 会话：PostgreSQL 的 `api_sessions` 元数据与版本化中性 `api_session_items`，首次请求同事务写入，之后请求原子追加，drop 删除元数据并级联条目；`agent_sessions` 保持当前会话指针，v0 换协议仍开新会话（D5）。
6. 上下文阈值：窗口的 70%，且不超过 200k token，到了就交接、换会话；Looper 原创 api 交接摘要提示词，不复制 Norma 的 Claude Code 衍生 prompt、措辞或示例；会话实现负责定向真实 Claude CLI 对照，最终 CPA 全对话准入另做；CLI 文本不变（D6）。
7. 能力边界：v0 仅 Manager 的进程内平台工具；不移植 Norma workbench 工具、`noa`、`noaadapter`，外部 MCP 放到之后。任何未来文件、进程工具须限定 cwd 与显式 `add_dirs`、环境白名单和私有 HOME，不改共享服务 HOME，不声称 OS 沙箱；可信服务持久化、提示文件 I/O 不属于模型工具。
8. 每轮最多 30 次模型请求（D10）。
9. 准入门槛：阶段 1 用 `gpt-6.1-sol`、阶段 2 用 `deepseek-v4-pro`，全部对话 `--reps 2`；每个对话的通过次数不低于同模型 CLI 对照减 1；确认类对话（`cancel_confirmation`、`config_tools`、`provider_key`、`schedule_*`、`client_relay_cancel`）全部通过（D11）。CPA 实际还提供更强的一档（例如 `gpt-6-astra`）时，阶段 1 改用它。
10. 循环来源：阶段 1 即将 Norma agentcore 及所需行为移植为类型化 asyncio Python，放在 `AgentLoop` 后，Responses 为首个 codec；唯一后续上游是 `loopgo-main:go/internal/norma`，制作副本另做（D3）。
11. 许可：维护者决定保留 Norma 的 MIT 来源、版权行及完整 MIT 文本，循环实现时创建第 5 节所述 notice、派生模块引用和 wheel 分发；Looper 保持 Apache-2.0，不声称从快照找到了许可文件。

## 15. 验证结果与待验证项（汇总）

**已测：** CPA 提供 `gpt-6-astra`，api 与 Codex 管家均使用它，其他角色为 Claude Code sonnet，全对话 `--reps 2 --jobs 2`。api smoke 4/4、$0.44；Codex 78/84 个已评分项通过、另有 4 个 harness error、$13.68；api 82/88、$9.06。双方均评分的对话为 $6.82 / $4.53，D11 已满足，确认类对话全部 2/2。共同失败为 `model_missing_endpoint`、`start_no_wait` 和首轮的 `session_profile_switch`（各 0/2）；最后一项是带 effort 后缀的显示名比较错误，本分支修正后双方均 2/2，单独记定向复跑，不改首轮总数。详见第 9 节；#688 的真实 Claude CLI 交接提示词专项对照保持独立且不变。

以下独立能力仍需真实运行核实，结果写回第 8.4 节；上述对话分数与记录花费不作为逐请求 token 或数据库读回的证明：

- CPA 请求参数 `store: false`、`include: ["reasoning.encrypted_content"]`、`prompt_cache_key` 和 `parallel_tool_calls: false` 的逐项核实；流里的 `response.completed.usage` 与 `input_tokens` 是否含缓存写；
- Responses 加密推理在同模型续接时能否正确重放；同协议换模型按第 7.2 节去掉外来 opaque reasoning，不尝试跨模型回传；
- DeepSeek、Kimi、OpenCode 的 Anthropic 接口是否接受 Bearer、`cache_control` 和 `thinking` / `output_config.effort`；它们流里的用量分桶；
- SDK 在流中途断开时不重试：这是读源码得出的，没有实跑；
- Claude Code 当前版本对 MCP 输出的默认上限；
- 每次请求少约一万 token 的估计及逐请求 token 差值；中性会话条目与 `api_sessions.protocol` 读回；
- Opus 描述性比较；模型不同，不用于 D11 判定。
