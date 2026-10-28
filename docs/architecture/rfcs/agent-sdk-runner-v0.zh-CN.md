# RFC：Agent SDK 运行器 v0（Looper 宿主驱动原生语言的 SDK 运行器）

- **状态：** 维护者 2026-10-27 同意按本 RFC 开发，从 S0 开始；第 17 节的待决问题先按推荐默认值实施，维护者可随时调整。第 1.2 节和第 4 节标「维护者已定」的条目，由维护者在 2026-10-26 的对话中决定；其余是本 RFC 的建议。同进程多会话、每会话 cwd/env、同一次运行内引导、按会话取消和短时 RSS，已由 2026-10-26 离线实测（runner spike）验证，结果见第 2.3 节；spike 没有覆盖的项仍标「需要验证」。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.0 节，批准后新增一项（建议第 24 项）。它接在 M9d（SDK 运行时与 Norma 循环移植）之后，取代 M9g（pi）和 DSH 运行时里驱动 CLI 的路径。
- **相关：** [API 运行时](api-runtime-v0.zh-CN.md)（进程内 `AgentLoop`、`TurnTools`、Norma 移植）、[pi 运行时](pi-runtime-v0.zh-CN.md)、[dsh 运行时](dsh-runtime-v0.zh-CN.md)、[Grok 运行时](grok-runtime-v0.zh-CN.md)（ACP）、[多运行时](multi-runtime-agents-v0.zh-CN.md)、[Worker 引导](worker-steering-v0.zh-CN.md)、[Agent 办公台](agent-room-v0.zh-CN.md)、[停止管家的回复 v2](chat-stop-v2.zh-CN.md)。
- **核对基线：** 仓库按 `main` `d6778eb0c`（v2.13.144）核对。SDK 只读核对已安装的包和源码，没有启动 SDK、调用模型或读取凭据：`@earendil-works/pi-coding-agent` 1.3.0；`@deepseek-ai/dsh` 0.2.1-rc.2；Norma 是维护者提供的 `main` 源码快照。快照没有 git 元数据，它和 API 运行时 RFC 第 5 节的 `2f158eaa6` 是不是同一提交，需要验证。
- **引用约定：** 仓库文件用相对仓库根目录的路径。SDK 文件写成「包:包内路径:行」：`pi:` 是 `pi-coding-agent@1.3.0`，`pi-ai:`、`pi-agent-core:` 是它自带的依赖；`dsh:` 是 `@deepseek-ai/dsh@0.2.1-rc.2`，`dsh-<x>:` 是同版本的 `@deepseek-ai/dsh-<x>`；`norma:` 是上面的 Norma 快照。
- **术语（维护者已定）：** **宿主**指 Looper 本身（`looper serve`，Python）。每种 SDK 一个原生语言进程，叫 **SDK 运行器**（`runner`）：Pi 运行器和 DSH 运行器用 TypeScript，Norma 运行器用 Go。Looper 一侧有 **RunnerBridge**（每条运行器连接一个）和 **RunnerSupervisor**（起停运行器进程、准入）。两者之间的协议叫 **Looper runner protocol**，运行器在握手时声明的能力叫 **runner capabilities**。

## 1. 需求与目标

### 1.1 背景

Looper 现在通过 CLI 驱动 Pi 和 DSH。管家和规划者每一轮都起一个新进程再续接会话；平台工具要么走 HTTP 扩展，要么走 MCP 子进程；停止要杀进程树；DSH 运行中收不到消息；花费只能在用量报上来之后判断（第 2 节）。维护者的判断是：CLI 接入不够方便，应该改为直接调用各家的 agent SDK。

### 1.2 维护者的决定（2026-10-26）

1. **投入 SDK 运行时，不再驱动 CLI。** Pi、DSH、Norma 三种 SDK 在本 RFC 里同等设计，每种都有完整的运行器设计（第 9–11 节）。
2. **每种 SDK 用它自己的语言。** Pi、DSH 是 Node 上的 TypeScript/JavaScript，Norma 是 Go。不把 Pi、DSH 移植成 Python。Norma 在 API 运行时里已有的 Python 移植保留，两者的分工见第 11.6 节。
3. **Looper 用 JSON-RPC 2.0 驱动运行器进程。** 传输用 stdio：运行器是子进程，按行分隔 JSON，stdout 只走协议，stderr 放诊断；不用 HTTP。消息格式与传输无关，以后可以为远程运行器加 WebSocket。
4. **进程模型（方案 1）：** 每种 SDK 一个运行器进程，复用多个 agent 会话。运行器在握手时声明能否按会话隔离（cwd、env、HOME、工具层权限）；每个运行器有会话上限，限制一次崩溃的影响面；满了，Looper 再起一个同种的运行器（每种 SDK 一个小池子）。每个 agent 单独一个进程是按需选择的放置，用于对隔离敏感的情形：信任域不同（如运行不可信代码）、进程级的网络或插件配置不同、实测事件循环延迟过高；不能隔离的运行器也退回这种放置。两种放置用同一个协议，切换只是配置。
5. **并发上限是准入控制，不只取决于机器快慢**（第 5.3 节）。每个运行器取三项的最小值：内存预算 ÷ 实测的每会话 RSS；事件循环或调度器的健康；硬性的会话上限。另有 Looper 跨所有运行器执行的整机上限：每个 provider 账号的并发模型请求数、并发工具进程数（加负载保护）、花费预算。默认值来自参考机器上的压测；在那之前的暂定值是每运行器 Pi 10、DSH 20、Norma 50 个会话（推测，需要压测验证），压测之后用实测值替换。
6. **所有 agent 将来共用一条消息总线。** 每个由 SDK 承载的 agent 都是总线上的一等参与者：每条运行器连接有一个 RunnerBridge，它在总线上替每个托管 agent 订阅并转发信封；托管 agent 发出的信封经 RunnerBridge 回到总线。两个 agent 即使在同一个运行器里，它们之间的消息也一律经过 Looper 的总线。投递语义：载荷是 `MessageEnvelope` 的 JSON；运行器按 `message_id` 确认之后，日志才把它记为已送达（至少一次，运行器按 `message_id` 去重）；同一目标内保序；每个运行器的在途窗口有界（背压）；重连后重投。运行器之间从不直连，所以总线核心以后可以换成外部 broker（如 PostgreSQL 表加 LISTEN/NOTIFY，或 NATS），信封、RunnerBridge 和运行器都不用改。
7. **引导在同一个 trace 里生效**（第 6.3 节）。每一轮有 Looper 分配的 `trace_id`；运行中送达的引导在下一个安全边界注入这一轮，不开新轮、不换 trace；只有 agent 空闲时，引导才开出新的一轮和新的 trace。Looper 的运行记录、用量记录和日志都以 `trace_id` 为键，控制台和日志能看到引导落在它改变的那一轮里。
8. **以后会给 SDK agent 加 skill 和 extension。** 协议和运行器要给 Looper 提供的 skill、extension 留扩展点，对 Pi 映射到它原生的 extension、skill、package。
9. **花费上限归 Looper，不归 SDK。** Looper 用自己的价目表定价；运行器在每次模型请求之前（包括 SDK 内部的压缩、重试、子 agent、工具内部的模型调用）向 Looper 发反向请求 `budget/authorize` 并等待答复；Looper 拒绝时，这次请求不发出，这一轮以类型化的 `capped` 结束。请求之后运行器上报原始 token 数，Looper 对账，必要时取消。SDK 自己算的费用只作参考。本机 stdio 的往返耗时相对模型延迟可以忽略（推测，第 13 节的 `authorize` 延迟指标负责证实）。
10. **一般原则：** SDK 提供能力（循环、工具、会话、压缩、扩展）；运行器提供机制；Looper（Python）决定策略（预算、隔离的取值、确认、审计、路由）。
11. **接入顺序：** Pi → DSH → Norma（第 16 节）。

维护者关于 Norma 与 Go 的并发效率的说法是假设，不是决定：本 RFC 以调研证据为准，与证据不符的地方照证据写并说明；性能预期一律标「推测，需要压测验证」，由第 15.5 节的压测决定。

### 1.3 目标

- 三个运行器讲同一个 Looper runner protocol，并通过同一套一致性测试（第 15.1 节）；Looper 一侧只有一套 RunnerBridge、RunnerSupervisor 和协议实现。
- 管家和规划者的会话常驻在运行器里，不再每轮起进程；执行者和评审者在运行器里运行，运行中的引导在同一个 trace 内生效。
- 平台工具经反向请求在 Looper 进程内执行，确认、授权、留痕、轮次归属和 100 KB 结果上限都与进程内的 `ManagerTurnTools` 一致（`looper/agents/runtimes/api/tools.py:10-35`）。
- 每次模型请求之前由 Looper 授权，花费上限从「用量到了才判断」变成「请求之前就挡住」；整机的模型并发、工具进程数也由 Looper 统一放行。
- 托管 agent 能收发总线信封；投递语义可测、可恢复。

### 1.4 不做

- 不把 Pi、DSH 移植到 Python；不把 API 运行时的 Python 管家换成 Go 的 Norma。
- 不做操作系统沙箱。v0 的隔离在工具层（第 12 节），与 API 运行时 RFC 第 5 节「工具边界」的说法一致。
- v0 不做远程运行器（WebSocket）和外部 broker，只保证协议和信封不妨碍以后加。
- SDK 运行器跑不了时不自动回退到 CLI（同 API 运行时 RFC D8）。
- 不使用 Norma 的 `noa`、`noaadapter`，也不使用、不移植 Norma 那段源自 Claude Code 的 compaction prompt（第 11.4 节）。

## 2. 现状与差距

### 2.1 Looper 现在怎么接 Pi 和 DSH

| 方面 | 现在 | 差距 |
|---|---|---|
| 进程 | Pi 的管家、规划者由 `ResidentPi(PerTurnSession)` 每轮起一个 `--mode json` 进程，用精确的 `--session <file>` 续接（`looper/agents/runtimes/pi.py:621-697`）；执行者、评审者每次运行一个进程（`pi.py:238-266`）。DSH 四个角色都是每轮一个 headless 进程（`looper/agents/runtimes/dsh.py:293-309`） | 每轮冷启动；按轮起进程的运行时 `can_interrupt = False`，管家的停止只能拒绝（`looper/agents/runtimes/__init__.py:321-324`） |
| 引导 | Pi 只有需要引导的执行者、评审者换成 `--mode rpc`（`pi.py:272-291`、`looper/agents/runtimes/pi_steer.py`），ACK queued 只说明已入队，运行结束瞬间到达的引导可能不被处理（Worker 引导 RFC 第 6 节）。DSH 读完一次提示就关 stdin（`dsh.py:480-485`） | DSH 收不到运行中的消息；Pi 没有接 `abort` / `clear_queue`；引导是否真的进了模型看不到 |
| 平台工具 | Pi 的自有扩展直接调 Looper 的 agent HTTP API（`looper/agents/runtimes/pi_extension/looper.ts:1-17`）；DSH 加载 `dsh-mcp-client`，起 Python 的 `looper mcp-server` 子进程，再调 HTTP API（`looper/agents/ops/service.py:655-658`、`looper/agents/runtimes/dsh_bootstrap.mjs:6-28`）。CLI 管家定期做工具探测（`looper/agents/tiers/manager.py:1113-1115`） | 运行令牌在子进程的环境里；DSH 多一个 MCP 子进程；管家的回合绑定要靠一个文件（`LOOPER_AGENT_TURN_FILE`） |
| 用量与花费 | Pi 每条 `message_end` 计一次用量（`pi.py:415-418`）；DSH 只有 root step 的用量，压缩、搜索、子 agent 的用量不保证出现（`dsh.py:178-199`） | 软上限在用量到达的请求边界才判断，挡不住已经发出的请求；两家的 SDK 都没有美元上限 |
| 清理 | 停止和超时都杀进程树；服务崩溃后，已经退出的 agent 留下的后代认不回来（计划第 5.2 节） | — |

**SDK 能去掉的：** 每轮冷启动；HTTP 工具桥和 MCP 子进程，以及子进程环境里的令牌；「杀进程才能停」（三家都有进程内的取消）。SDK 也给出了 provider 层的接口，可以在每次请求前后插入授权和用量。

**SDK 去不掉、也不能据此保证的：** 运行器自己仍是一个进程；工具起的 shell、PTY 仍有子进程，崩溃清理不会自动消失；SDK 内部的每个请求是否都经过被包装的 provider 层，每家都要验证（第 9–11 节）；三家都没有美元上限；同进程共享不是操作系统级隔离。以上是从接口推出的范围，不是性能实测。

### 2.2 三种 SDK 对比

| 维度 | Pi | DSH | Norma |
|---|---|---|---|
| 语言 | TypeScript/ESM；Node ≥ 22.19.0（`pi:package.json`） | TypeScript/ESM（Node） | Go；`norma:go.mod:3` 写 `go 1.26.3`，与 README 的 1.23+ 不一致 |
| 成熟度 | 1.3.0 正式版，公开 SDK 和 `.d.ts` | 0.2.1-rc.2 预发布；SDK 握手里的版本仍写 `0.0.1`（`dsh-sdk-jsonrpc-server:lib/index.js:143-146`） | 维护者自有代码，`main` 快照，没有版本号；以后唯一上游是 `loopgo-main:go/internal/norma` |
| 同进程多会话 | 有前提地支持：每次 `createAgentSession` 都新建 ModelRuntime、SettingsManager、SessionManager、ResourceLoader（`pi:dist/core/sdk.js:69`、`:253`、`:303`）；安装包里没有并发多会话的示例或测试，离线实测通过（第 2.3 节） | 有前提地支持，三者里最明确：一个 Cordis root 下 `ctx.agents.create/resume` 建多个 agent，各有自己的 driver（`dsh-agent:lib/index.js:310`、`:445`）；现成的 JSON-RPC 服务已能同时跑多个会话（`dsh-sdk-jsonrpc-server:lib/index.js:47`、`:188`） | 有前提地支持：每个 agent 一个 `Session`（`norma:agentcore/agentcore.go:247`），同一 `Session` 不能并发 `Prompt`（`:228-230`）；`coordinator.RunParallel` 用 goroutine 扇出（`norma:coordinator/coordinator.go:107`）；仓库里没有测试在一个进程里跑多个独立 Session，离线实测通过（第 2.3 节） |
| 每会话隔离的抓手 | `cwd`、`agentDir`、`modelRuntime`、`sessionManager`、`settingsManager`、`resourceLoader`、工具清单（`pi:dist/core/sdk.d.ts:10-64`）；bash 有 `operations`、`spawnHook`（`pi:dist/core/tools/bash.d.ts:40-80`）；没有 env 选项 | 创建时的 `meta.cwd`（`dsh-agent-loop:lib/index.js:1823`）；bash 接收 `request.env`，叠在清洗过的父环境上（`dsh-bash-local:lib/index.js:88-107`）；agent 作用域的工具和 guard；`DSH_HOME` 是 composition 级的 | `WorkingDir`、`BashEnv`、Tools、权限模式、Hooks（`norma:agentcore/agentcore.go:27-204`）；没有 `add_dirs`，绝对路径不受限（`norma:tool/helpers.go:89-95`）；`BashEnv` 叠加在进程环境上，不是白名单（`norma:tool/bash.go:159-165`） |
| 运行中引导 | 原生：`steer` 在当前 assistant 回合的工具结束后、下一次模型请求前注入；`followUp` 在当前运行结束后继续；`abort` 等到空闲，且不清队列（`pi:docs/sdk.md:64-70`、`pi:dist/core/agent-session.d.ts:541-583`、`:636-650`） | 进程内接口原生：`steer`（下一个步边界，空闲时开一轮）、`followup`（独立的下一轮）、`inject`（只进下一个 pre-step，不唤醒）、`cancel(cause, {keepInbox})`（`dsh-agent:lib/types/runtime-types.d.ts:138-209`）；现成的 SDK wire 没有 steer | 没有：`Session` 只有 `Prompt`、`Resume`、`Close`、`Reset`、`Messages`、`Usage`（`norma:agentcore/agentcore.go:337-436`）；钩子里只有 `UserPromptSubmit`、`Stop` 能注入 `AdditionalContext`（`norma:hook/hook.go:14-26`、`:48`），能模拟 follow-up，不能在轮中引导；用 `Compactor.Pre` 自建队列可以在下一次请求前注入（离线实测，第 2.3 节）；取消靠 ctx |
| 自定义工具 | `customTools` 或扩展的 `registerTool`，参数用 TypeBox，普通 JSON schema 可归一化后注册（pi 运行时 RFC 第 4 节） | `ctx.tools.register`，自有 schema DSL，必须有 output JSON Schema 和 renderer（`dsh-tools:lib/types/index.d.ts:105-128`） | `CoreTool` 接口，schema 是 JSON Schema 的 map（`norma:tool/tool.go:85-94`） |
| 会话持久化 | JSONL 条目树，header v3；`SessionManager.open/create/continueRecent`，fork（`pi:docs/session-format.md:8-30`） | 每会话一个目录，不可变的 generation 文件；resume 保持 cwd，拒绝已被占用的会话（`dsh-session-persistence-jsonl:lib/types/index.d.ts:1-25`） | JSONL transcript，`Session.Resume`，续接时丢掉末尾没配对的 tool_use；没有 fork（`norma:agentcore/agentcore.go:364-393`） |
| 压缩 | 自动，加手动 `compact()`（`pi:dist/core/agent-session.d.ts:722`） | `BasicCompactionEngine`：步间压力、溢出恢复、`compactNow`（`dsh-compaction-basic:lib/types/index.d.ts:24-79`） | 自动（Micro、Auto、Reactive），没有手动接口，摘要多发一次模型请求（`norma:compaction/compaction.go:36-57`）；它的提示词不能用（第 11.4 节） |
| 用量与费用字段 | 每条消息的 usage 分 input（未缓存）、output、cacheRead、cacheWrite；`getSessionStats()` 是全会话累计，含按模型价算的 cost（`pi:dist/core/agent-session.d.ts:888`）；没有美元上限 | `TokenUsage`：input（未缓存）、output、total、cacheRead、cacheWrite、reasoning（`dsh-llm:lib/types/types.d.ts:152-174`）；没有美元字段 | `Usage` 四项，`InputTokens` 含缓存；没有 reasoning 字段；没有价格和美元上限（`norma:llm/types.go:101-107`） |
| 扩展与 skill | extensions、skills、prompt templates、packages、MCP 扩展（`pi:dist/core/resource-loader.d.ts:70-110`、`pi:docs/packages.md`） | Cordis 插件与 patch、`skill-filesystem`、`dsh-mcp-client`、同进程的 subagent spawn/fork | `skill`（SKILL.md）、`mcp`（只有 stdio）、`subagent`、`coordinator`、`memory`、`hook`、`plan` 包 |
| 原生的运行或 trace id | 没有；Looper 的 `trace_id` 对应「会话 + 一次 agent 生命周期（`agent_start` 到 `agent_settled`）」 | 没有；对应「`sessionId` + turn」 | 没有；对应「`SessionID` + 一次 `Prompt` 调用」 |
| 许可 | MIT | MIT | MIT，`Copyright (c) 2026 The Norma Authors`。这是维护者的决定；快照里没有许可文件（同 API 运行时 RFC 第 5 节的说法） |

### 2.3 离线实测（2026-10-26 runner spike）

**环境与方法：** Darwin arm64、32 GiB 内存；Node v24.20.0；Go 1.26.3；Pi 1.3.0、DSH 0.2.1-rc.2、Norma `main` 快照。用内存里的假 provider，按各 SDK 真实的流式协议先发一次 bash 工具调用、再发最终回答；不调用真实模型，网络拦截器记录的连接尝试为 0；bash 是真实执行的；并发由 provider 内的 barrier 证明（两个请求都进入之后才放行），不是比较开始时间；41 项交叉核对全部通过。环境隔离用父进程里的哨兵变量做正向对照。

| 项目 | Pi | DSH | Norma |
|---|---|---|---|
| 同进程两个会话的模型请求同时在途 | 通过：共用一个 ModelRuntime，`createAgentSession` 两次，`Promise.all` 两个 `prompt` | 通过：一个 Cordis Context，`ctx.agents.create` 两次，各自 `followup` | 通过：`NewSession` 两次，两个 goroutine 各自消费 `Prompt` |
| 50 个模型请求同时在途 | 通过 | 通过 | 通过 |
| 每会话工具 cwd | 通过：`createBashToolDefinition(cwd)` | 通过：`meta.cwd` 加作用域工具；原生 shell 的 workdir 也通过 | 通过：`Options.WorkingDir` |
| 每会话 HOME 与自定义变量 | 通过 | 通过 | 通过 |
| 干净环境（不继承父进程） | 原生通过：bash 的 `spawnHook` 整个替换 env；这也会去掉 `PI_*` 会话变量，需要的话要放进白名单 | 原生做不到（清洗过的父环境加覆盖）；作用域内的自定义工具显式给 env，通过 | 原生做不到（`BashEnv` = `os.Environ()` 加追加项）；自定义 bash 工具设 `cmd.Dir`、`cmd.Env`，通过 |
| 引导进入同一次运行的下一次模型请求（不再调一次 prompt） | 通过：bash 运行中 `session.steer`，只有一次 `agent_start`/`agent_end` | 通过：`agent.steer` 和 `inject`，会话记录里只有一个 turn、两个 step | 原生做不到；`Stop` 钩子不满足（引导到第 3 次请求才出现）；`Compactor.Pre` 队列的变通做法通过（第 2 次请求就出现，历史完整保留） |
| 两个会话都在模型请求中时取消其中一个 | 通过：`session.abort` 约 2 ms，另一个会话正常完成 | 通过：`agent.cancel` 约 0.3 ms | 通过：只取消那次 `Prompt` 的 ctx |
| 进程级副作用 | 导入 SDK 就加上 SIGINT、SIGTERM、SIGHUP 的监听，释放会话后不移除；共享的 provider 注册表；进程级的 detached 子进程 PID 集合 | 子进程模块装了一个进程级的 exit hook，Context 释放后仍在 | 没有按会话的进程级副作用；构建需要与 Go 1.26.3 匹配的 `GOROOT` |

**50 个会话各跑一轮的运行器 RSS**（只含运行器进程，不含 bash 子进程；短时单次测量，各 SDK 的组合和采样点不同，不能当作容量结论或横向排名）：

| | 基线 → 跑完 | 约每会话 | 其他 |
|---|---|---|---|
| Pi | 137.7 → 146.6 MiB | 0.18 MiB | 事件循环延迟 p99 18.0 ms |
| DSH（最小 kernel 组合，不含持久化、终端、压缩） | 57.3 → 84.7 MiB | 0.55 MiB | 事件循环延迟 p99 38.6 ms |
| Norma | 13.0 → 22.7 MiB | 0.19 MiB | 活动 goroutine 51，结束后 1 |

按这些数字推算（推测）：50 个 agent 若是 50 个 Pi 进程，约 50 × 138 MiB ≈ 6.7 GiB；放在一个运行器里约 147 MiB。这支持第 5.2 节的进程模型。

**spike 没有覆盖、仍需要验证的：** SDK 的真实传输；长上下文；持久化与续接；真实模型上的取消；子进程的总 RSS；长时间运行的内存趋势；开启压缩和重试之后的请求路径（Pi 的 spike 关掉了压缩和重试）。

## 3. 候选方案与取舍

| 方案 | 做法 | 复杂度 | 性能 | 可维护性 | 故障半径 |
|---|---|---|---|---|---|
| **(a) Looper 自有协议 + 原生 SDK 运行器，stdio**（推荐） | 每种 SDK 一个薄运行器（TS 或 Go），直接调 SDK；Looper 一侧一套 RunnerBridge | 中：三个运行器各一份映射，Looper 一侧一份协议 | 常驻会话，没有每轮起进程；反向调用走本机管道 | 好：协议归 Looper，SDK 升级只动对应运行器；策略只在 Looper | 一个运行器进程，受会话上限约束；崩溃不影响 `looper serve` |
| (b) 复用各 CLI 自带的协议（Pi `--mode rpc`、DSH 的 SDK stdio 或 ACP） | 每家一个 Python 驱动，讲对方的协议 | 中偏高：三套不同的协议、事件和恢复语义 | 与 (a) 接近，但 Pi RPC 每会话一个进程 | 差：能力不齐。DSH 的 SDK wire 没有 steer、单会话 cancel、自定义工具、system prompt、compact（`dsh-sdk-protocol:lib/types/types.d.ts:128-158`），ACP 忙时拒绝 prompt（dsh RFC 第 2 节）；Pi RPC 没有反向工具调用；Norma 没有任何现成的协议服务 | 每会话一个进程，最小 |
| (c) HTTP JSON-RPC 或 WebSocket 的运行器服务 | 运行器监听端口，Looper 当客户端 | 中：多了端口、鉴权、重连 | 与 (a) 相同 | 中：多一套监听和令牌管理 | 与 (a) 相同；多一个同机其他进程能连的端口 |
| (d) 移植成 Python | 三家在 `looper serve` 进程里重写 | 极高：三份 coding harness | 进程内，没有 IPC | 差：要长期跟三家上游 | 崩溃和内存问题直接落在服务进程上 |

维护者已排除 (d)。(b) 的问题是能力不齐、语义不一致：平台工具的反向调用、请求前授权、trace 内引导，这几家的现成协议都没有。(c) 解决的是远程部署，v0 用不上，却要多管端口和令牌。选 (a)；需要远程运行器时，在 (a) 上加 WebSocket 传输即可。

| 传输 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **stdio**（子进程，按行分隔的 JSON） | 不开端口，没有鉴权问题；父进程退出时子进程读到 EOF；Pi RPC、DSH SDK、ACP、MCP 都是这种形状 | 只能本机；stdout 不能混入别的输出 | v0 采用 |
| HTTP JSON-RPC / WebSocket | 能远程；WebSocket 双向 | 要端口、令牌、TLS、重连 | 以后给远程运行器用，同一套消息 |
| gRPC | 有类型的 schema，有流 | 三种语言都要加生成代码和依赖；与现有 JSON 信封、内部事件还要再转一层 | 不采用 |

## 4. 决定一览

| # | 决定 | 来源 |
|---|---|---|
| D1 | 用原生语言的 SDK 运行器取代驱动 CLI；Pi（TS）、DSH（TS）、Norma（Go）同等设计；不把 Pi、DSH 移植到 Python | 维护者已定 |
| D2 | 宿主是 Looper（Python）；运行器是每种 SDK 的原生进程；Looper 一侧是 RunnerBridge、RunnerSupervisor | 维护者已定 |
| D3 | SDK 提供能力，运行器提供机制，Looper 决定策略。运行器不保存策略状态，只执行 Looper 给出的值 | 维护者已定 |
| D4 | Looper runner protocol：JSON-RPC 2.0，一条连接上三类消息（会话控制、反向请求、总线信封）；stdio，不用 HTTP；格式与传输无关（第 6 节） | 维护者已定 |
| D5 | 进程模型方案 1：每种 SDK 一个运行器进程复用多个会话，满了起同种的下一个；每 agent 一个进程是按需选择的放置；同一协议，切换只是配置。证据：三家都是「有前提地支持」同进程多会话（第 2.2 节），离线实测通过（第 2.3 节）；每个会话串行跑轮次，不同会话并发 | 维护者已定 |
| D6 | 准入控制取多项上限的最小值（第 5.3 节）；暂定值 Pi 10、DSH 20、Norma 50，压测后替换 | 维护者已定；阈值为本 RFC 建议 |
| D7 | 统一总线：托管 agent 是一等参与者；agent 间消息必经总线；按 `message_id` 确认后才记已送达；同一目标保序；在途窗口有界；重连后重投；运行器之间不直连 | 维护者已定 |
| D8 | 每一轮一个 Looper 分配的 `trace_id`，贯穿这一轮的所有消息；运行中的引导在同一 trace 内的下一个安全边界生效；空闲时的引导、follow-up 开新 trace，分别记下起因 `cause_message_id` 和 `parent_trace_id`；`llm_records` 加这三列（第 6.3 节） | 维护者已定；列与字段为本 RFC 建议 |
| D9 | 托管 agent 有自己的总线地址；总线加显式的 `lease`/`ack`；新消息类型 `AGENT_MESSAGE`、`AGENT_NOTICE`；忙时按消息类型投成 steer 或 follow-up，空闲时开一轮，轮次由所属 tier 登记（第 7 节） | 维护者已定方向；细节为本 RFC 建议 |
| D10 | 花费：运行器上报每次请求的原始 token，Looper 按自己的价目表定价；每次模型请求前 `budget/authorize`，拒绝即 `capped`；请求之后对账，超了就 `session/cancel` 并记下超出额；SDK 自算的费用只作参考 | 维护者已定 |
| D11 | 平台工具经反向请求 `tools/call` 回到 Looper，在进程内调用与 HTTP 处理器相同的 ops，令牌和回合绑定不出 Looper；100 KB 上限由 Looper 执行；工具名用原名 `looper_*` | 本 RFC 建议 |
| D12 | skill 与 extension 扩展点：`session/new` 带 Looper 选定的扩展清单，运行器映射到 SDK 原生机制，加载与省略记进运行记录 | 维护者已定方向；映射为本 RFC 建议 |
| D13 | 隔离 v0 在工具层：任务 cwd 与显式 `add_dirs`、环境白名单、每会话私有 HOME；取值由 Looper 给出；运行器启动时冻结进程环境，运行中从不 chdir、从不按会话 setenv；每次起子进程都显式给绝对 cwd 和完整 env（第 12 节） | 本 RFC 建议 |
| D14 | 会话历史用 SDK 原生格式，放在 Looper 拥有的私有目录；`agent_sessions` 仍只是指针；Norma 与 `api` 共用中性条目放到后续（Q1） | 本 RFC 建议 |
| D15 | 运行时类型：`pi`、`dsh` 沿用现有类型，在各自阶段改由运行器承载，原来按轮起 CLI 的路径在阶段完成时删除（Profile 不用迁移）；`norma` 是新类型 | 本 RFC 建议，依据维护者的阶段划分 |
| D16 | 版本门禁：Pi 接受已验证的 1.3.x；DSH 只接受精确的 0.2.1-rc.2；Norma 按提交固定；运行器在 `initialize` 里自检 SDK 的导出并报告版本 | 本 RFC 建议 |
| D17 | 停止是会话作用域的：取消这一轮，等工具和后代收敛，再释放这个实例；全局 kill 只在整个运行器退出时用；SDK 运行时的管家 `can_interrupt = True` | 本 RFC 建议 |
| D18 | 阶段：S0 协议、RunnerBridge、RunnerSupervisor、准入、假运行器与一致性测试；S1 Pi；S2 DSH；S3 Norma（含上游的每会话收件箱）与三者的容量对比 | 维护者已定 |

## 5. 架构

### 5.1 分层

```text
looper serve（宿主，Python）
  tiers：ManagerAgent / PlannerAgent / WorkerPool —— ResidentSession / RunHandle 接口
    RunnerSession（ResidentSession）    ProcessRun / RunnerChannel（RunHandle，各自提供 RunChannel）
      RunnerBridge（每条运行器连接一个）
        ├─ 会话控制：session/*；把 session/update 翻成内部事件，按 trace_id 记录
        ├─ tools/call → ManagerOps / PlannerOps / RoomOps（与 HTTP 处理器同一个函数）
        ├─ budget/authorize、exec/acquire → 价目表、各级上限、整机的模型并发与工具名额
        └─ 总线：lease / ack MessageBus，替托管 agent 收发信封
      RunnerSupervisor：放置、准入、起停、健康检查、重启
        │ Looper runner protocol（JSON-RPC 2.0，stdio）
运行器进程（原生语言）：pi-runner（TS）/ dsh-runner（TS）/ norma-runner（Go）
  会话表 → 每个会话一个 SDK 实例（AgentSession / agent.ctx / agentcore.Session）
  包装过的 provider 层（authorize + 每请求用量）、Looper 工具代理、隔离执行器、扩展加载
```

- **RunnerSession** 实现 `ResidentSession`（`looper/agents/runtimes/runner/session.py`），给管家和规划者用：`start` 解析 `Descriptor` 并申请会话；`run_turn` 分配 `trace_id`，发 `session/prompt`，等这个 trace 的 `settled`；`interrupt` 是 `session/cancel`；`stop` 是 `session/close`。`shares_bridge = False`，`pid=None`：共用运行器不进入 owner 的进程树清理，运行记录另记 `runner_id`。Pi 当前的 `steer(text, *, message_id, if_idle="reject")` 只接受运行中同 trace 的引导；空闲拒绝，不支持自动开新轮。
- **RunHandle**（`looper/agents/runtimes/channel.py`）统一执行者、评审者和咨询的生命周期：`command`、`pid`、`channel`、`launch/end/wait`、错误输出和丢弃输入。`ProcessRun` 封装 CLI 进程与进程树清理；`RunnerChannel` 同时实现 `RunHandle` 和 `RunChannel`，`pid=None`，不把共用运行器交给单次运行的进程树清理。`RuntimeAdapter.hosts_runs` 默认 false；为 true 时 `WorkerPool` 和咨询直接用 `hosted_run`，启动失败也不回退 CLI。
- **RunnerChannel** 的 `launch` 新建或分叉会话，`start` 发 `session/prompt`；超时、取消和花费上限用 `session/cancel`，`end/wait` 关闭并释放会话。`send` 是 `if_idle=reject` 的 `session/steer`，返回 `DeliveryResult`；`steer_dropped` 经 `dropped_input` 回到 `missed_messages`，由规划者在下一次决策中处理。运行记录的 `command` 是可解析的 runner `Descriptor`，不是 CLI 命令。
- **RunnerBridge** 是协议的 Looper 一端，每条连接一个。它持有每个会话的绑定（管家的回合绑定、规划者或运行的令牌、总线地址、预算范围、当前 trace），这些都不发给运行器。
- **RunnerSupervisor** 起停运行器进程，做放置、准入、健康检查和重启。

### 5.2 进程模型与放置

- **默认放置（共享）：** 每种 SDK 一个运行器进程；满了（第 5.3 节）再起一个同种的，组成每种 SDK 的小池子。新会话放进池里还能准入的运行器。
- **进程级配置组：** 一个进程里的会话共用只能在进程级设置的东西，配置不同就进不同的运行器：Pi 的 HTTP 代理和全局 dispatcher（`pi:dist/core/http-dispatcher.js:42`、`:68`）；DSH 的代理模块、`.env` 加载和 provider 路由（`dsh-http-proxy:lib/index.js:291-400`、`dsh-app-boot:lib/index.js:3725`、`dsh-llm:lib/index.js:1812-1834`）；Norma 的 `AGENT_CORE_DISABLE_*`、`NORMA_RIPGREP_*` 开关（`norma:tool/task.go:585-591`、`norma:tool/ripgrep.go:38-54`）。
- **每 agent 一个进程（按需）：** 用于信任域不同、进程级配置不同、实测事件循环延迟过高的情形；运行器声明 `per_session_isolation: false` 时也用它。v0 的开关是 Settings → Agent Runtimes 上每种运行时的放置设置（`shared` 默认、`per_agent`）；按项目划分信任域列入以后的工作。协议不变。
- **一个会话一次只跑一轮；** 运行中的额外输入走 steer 或 follow-up；不同会话并发。

### 5.3 准入控制

新会话要进某个运行器、某次模型请求或工具进程要开始，都要过 Looper 的准入。

| 上限 | 作用范围 | 判定 | 暂定默认（推测，需要压测验证） |
|---|---|---|---|
| (a) 内存 | 每运行器 | 会话数 < 运行器内存预算 ÷ 实测的每会话 RSS | 运行器内存预算：整机内存的 50% 按运行器数平分 |
| (b) 调度健康 | 每运行器 | Node 事件循环延迟 p99、Go 调度延迟 p99 低于阈值才接新会话；超过就停止接，已有会话不受影响 | Node 100 ms；Go 20 ms |
| (c) 崩溃影响面 | 每运行器 | 会话数 < 硬上限，资源够也不超过 | Pi 10、DSH 20、Norma 50 |
| (d) 模型并发 | 每个 provider 账号，整机 | `budget/authorize` 在并发请求达到 provider 的配额时排队等待，名额空出来再放行 | provider 的可选设置 `max_concurrent_requests`，空表示不限 |
| (e) 工具进程 | 整机 | `exec/acquire` 在名额用满或 1 分钟负载均值超过阈值时排队 | 名额 = max(1, 核数 ÷ 2)；负载阈值 = 核数 |
| (f) 花费 | 每轮、每次运行、每任务 | `budget/authorize`（第 8.4 节） | 现有的各级上限 |

- 每运行器的上限 = (a)、(b)、(c) 的最小值。池里没有运行器能准入时，在整机内存预算允许的范围内再起一个；仍不行就让会话的启动排队，排队超过 120 秒以 `CAPACITY` 失败，原因写明是哪一项。
- (d) 的配额来自上游的速率和并发限制，由操作者在 LLM Providers 里填；不填就不限，与现在一样。
- (e) 只管 SDK 运行器起的工具进程（bash、测试、构建，这些才是真正吃 CPU 的）；CLI 运行时的工具不在 Looper 的控制里。SDK 内部不经运行器执行器的子进程（如 Norma 的 ripgrep、DSH 的 sandbox 探测）不计入，列为已知缺口。
- 离线实测里 50 个会话各跑一轮的数字（Pi 事件循环 p99 18.0 ms、DSH 38.6 ms，每会话 RSS 0.18–0.55 MiB，第 2.3 节）都在这些暂定阈值之内；它们是短时单次测量，不含长上下文和子进程，不能据此提高默认值。
- 所有默认值都由第 15.5 节在参考机器上的压测替换；发布说明写明当时的取值和依据。

### 5.4 生命周期

- **起进程：** RunnerSupervisor 用白名单环境起运行器（只有 PATH、语言区域、运行器私有的 TMPDIR 和 SDK 的进程级开关；没有 provider key，没有 Looper 令牌），cwd 是运行器自己的私有目录，放进单独的进程组；记录进程 id 和启动时间，与现在的 agent 记录一样，服务重启时清扫。
- **握手：** `initialize` 协商版本、交换能力（第 6.2 节）。握手失败、版本不在门禁列表里：这个运行时不可用，原因写进 Settings → Agent Runtimes，不回退。
- **会话：** `session/new` 或 `session/load` → 多轮 → `session/close`。状态：opening → idle ⇄ running → closing → closed；强制释放过的会话记为 poisoned，不再接受请求。
- **健康：** 每 10 秒一次 `runner/health`，30 秒没有回答就判定挂死，杀掉重启。
- **崩溃与重启：** 在途的轮次以 `lost` 结束（与现在「进程死在一轮中间，没有 result」同一条路径）；按 1、2、4…60 秒退避重启，10 分钟内重启 5 次判定崩溃循环，这个运行时标为不可用，新的轮次明确报错。会话在下一次用到时 `session/load`，从 SDK 自己的持久化恢复；没确认的总线信封在会话重新打开后重投。
- **服务停机：** `looper serve` 先对每个运行器发 `shutdown`（停止接新请求，取消在途轮次，写完持久化，回答后退出）；5 秒内不退出就杀进程组。运行器读到 stdin EOF 时也按 `shutdown` 处理。

### 5.5 目录结构（维护者 2026-10-27 认可）

**源码。** Looper 仓库放宿主一侧和 TypeScript 运行器；Norma 运行器放在 Go 移植版的模块里，因为 `go/internal/norma` 是 Go 的 internal 包，别的模块导入不了。

```text
looper 仓库
├─ looper/agents/runtimes/runner/   宿主一侧（Python）
│   ├─ protocol.py      消息类型、错误码、版本（类型化）
│   ├─ bridge.py        RunnerBridge：每条连接一个；tools/call、budget/authorize、总线
│   ├─ supervisor.py    RunnerSupervisor：起停、放置、准入、健康
│   └─ session.py       RunnerSession / RunnerChannel，接现有的 tier
├─ runners/                         原生语言的运行器
│   ├─ protocol/        协议的 JSON Schema 和与语言无关的一致性用例（每个运行器都要通过）
│   ├─ fake/            假运行器：S0 和一致性测试用
│   ├─ pi/              TypeScript：src/{main,session,provider,tools,exec,extensions}.ts 与 test/
│   └─ dsh/             TypeScript，结构同 pi/
└─ tests/

Go 移植版（loopgo-main）
├─ go/internal/norma/            Norma 副本，唯一上游
├─ go/internal/runner/           Go 的协议实现：读写 goroutine、会话表、准入信号量
└─ go/cmd/looper-norma-runner/   入口
```

`runners/pi` 的源码经 `looper/agents/runtimes/runner/pi_runner -> ../../../../runners/pi` 符号链接进入 wheel 的 package data，由 `importlib.resources` 定位 `pi_runner/src/main.ts`，Node ≥ 22.19 直接运行；不打包 SDK，SDK 从探测到的 `pi` 可执行文件的真实路径解析。DSH 沿用同一发布方案；Norma 运行器由 CI 按平台编译，`install.sh` 校验 SHA-256 后下载（Q6）。

**运行时。** 每个运行器进程在 Looper 的 runtime root 下有一个私有目录，同时是它的 cwd；常驻 owner 的持久状态单独放置，避免运行器重启时丢失，不指向操作者的目录。

```text
<runtime_root>/runners/
├─ state/pi/<owner>/          Manager / Planner 的持久 JSONL；跨 runner generation 保留
├─ pi-1/                      每个运行器进程一个；池子扩容时编号递增（pi-2 …）
│   ├─ tmp/                   私有 TMPDIR
│   └─ sessions/<会话 id>/
│       ├─ agent/             Pi 的 agentDir：只有设置和模型，没有 auth
│       ├─ home/              工具子进程的 HOME
│       └─ native/            SDK 原生的会话文件（Pi 的 JSONL）
├─ dsh-1/                     同上；DSH_HOME 按运行器设（它是进程级的配置）
└─ norma-1/                   同上；native/ 放 Norma 的 transcript
```

任务的工作区仍是现有的任务 worktree，经 `cwd` 与 `add_dirs` 交给运行器；`agent_sessions` 保存会话指针，运行器重启后按 owner 的状态目录执行 `session/load`。会话关闭清理临时会话目录，owner 状态目录的删除与过期清扫仍待实现。

## 6. 协议

### 6.1 连接与分帧

- JSON-RPC 2.0。一个运行器进程一条连接：stdin 是 Looper → 运行器，stdout 是运行器 → Looper。UTF-8，一行一个 JSON 对象，只用 LF 分行（U+2028、U+2029 不算换行，与 `PiStream` 相同）；单行最多 4 MiB。
- stdout 只走协议。出现不能解析的行就是协议违约：这个运行器标为损坏并重启，不跳过坏行继续读。stderr 是诊断：Looper 按行读，每行截到 8 KiB，限速，经 `log_redaction` 脱敏后写进服务日志（`looper/agents/log_redaction.py:30-37`）。
- 双向请求的 id 不冲突：Looper 发出的用 `l-<n>`，运行器发出的用 `r-<n>`。
- 会话级消息都带 `session_id`；轮次级消息带 `trace_id`；与模型请求有关的带 `request_id`（运行器生成，一次请求一个）。
- 以后加 WebSocket 时，一个文本帧一条消息，方法和字段不变。

### 6.2 握手与版本

`initialize`（Looper → 运行器），只能调一次，第二次回错误。

- **参数：** `protocol_versions`（如 `["0.1"]`）、`looper_version`、`host_capabilities`（`tools_call`、`budget_authorize`、`exec_slots`、`bus`）。
- **结果：** `protocol_version`、`runner: {kind, version}`、`sdk: {name, version}`、`capabilities`：

| 能力 | 含义 |
|---|---|
| `per_session_isolation` | 能按会话执行 cwd + `add_dirs`、环境白名单、私有 HOME（第 12 节）。为 false 时每 agent 一个进程 |
| `per_session_credentials` | 每个会话能带自己的 provider key。为 false 时，provider 绑定也进放置键 |
| `max_sessions` | 运行器自己的硬上限；Looper 取它与第 5.3 节 (c) 的较小值 |
| `steer` | `{in_trace, boundary, idle_start}`：能否在同一 trace 内引导、注入点（如 `after_tool_batch`）、空闲时能否开一轮 |
| `follow_up`、`cancel` | 对应控制是否可用 |
| `compaction` | `{auto, manual, events}` |
| `usage` | `{per_request, buckets, reasoning_in_output}` |
| `authorize_covers` | 运行器保证会先授权的请求种类，如 `["turn", "retry", "compaction", "subagent", "tool"]`；不在列表里的种类，Looper 视为预算缺口（第 14 节） |
| `exec_slots` | 起工具进程前是否先 `exec/acquire` |
| `resume` | 能否从持久化续接 |
| `fork` | 能否以 `session/new.fork_from` 从只读副本创建新会话（agent room 咨询用）；Pi 已声明 true，宿主在启动咨询前检查 |
| `extensions` | 支持的扩展种类，如 `["skill_dir", "runner_extension"]` |
| `tools` | `{custom: true, builtin: [...]}` |

- **版本规则：** `MAJOR.MINOR`；小版本只加字段和方法；未知字段忽略；能力缺省视为 false；Looper 不调用对方没声明的方法；主版本不同就握手失败。
- 运行器在握手时自检 SDK：版本在门禁列表里，用到的导出都在（做法同 `dsh_bootstrap.mjs:6-28` 检查 `Config`、`inject`、`apply`）。不通过就回 `VERSION_MISMATCH`。

### 6.3 trace 与轮次

- **一轮一个 trace。** 每一轮有一个 Looper 生成的 `trace_id`，`session/prompt` 带上；这一轮的所有 `session/update`、`budget/authorize`、`tools/call`、`exec/acquire`、`request_end` 和 `bus/publish` 都带同一个 `trace_id`。
- **运行中的引导留在同一 trace。** 运行器把它注入正在跑的循环的下一个安全边界（这一批工具结果之后、下一次模型请求之前），不开新轮、不换 trace；真的进入模型时发 `steer_applied`，带 `trace_id` 和位置（`after_request_id`、`after_tool_batch`）。
- **空闲时的引导开新 trace。** `session/steer` 带 `if_idle`：`start`（默认）时用请求里的 `new_trace_id` 开一轮，记下起因 `cause_message_id`（即这条引导的 `message_id`）；`reject` 时回 `NOT_RUNNING`。
- **与结束竞争的引导不会丢。** 引导已经排队、这一轮却在注入前结束了：`if_idle=start` 时运行器用 `new_trace_id` 开一轮，`steer_applied` 标 `new_turn: true`；`if_idle=reject` 时发 `steer_dropped`（`reason: turn_ended`），由 Looper 另行处理（执行者的引导回到 `missed_messages`）。
- **follow-up 在当前这一轮静止后开新 trace，** `parent_trace_id` 指向当前这一轮；开始时发 `follow_up_started`。
- **取消丢弃排队的引导和 follow-up，** 各发一条 `steer_dropped`（`reason: cancelled`）。
- **记录：** `llm_records`（`looper/db/schema.sql:1974`）加 `trace_id`、`parent_trace_id`、`cause_message_id` 三列，`trace_id` 建索引；内部的 usage 事件和服务日志都带 `trace_id`；`steer_applied`、`steer_dropped` 写进这一轮的 `llm_record_events`。`trace_id` 与信封的 `correlation_id` 不同：后者串起一个用户请求跨 tier 的所有消息，前者标识一个会话里的一轮；轮次记录两个都存。

### 6.4 会话控制（Looper → 运行器的请求）

方法名在合适的地方与 ACP 对齐（`looper/agents/runtimes/grok_acp.py:1-30`）。

| 方法 | 主要参数 | 结果 | 说明 |
|---|---|---|---|
| `session/new` | `session_key`、`role`、`address`（总线地址）、`cwd`、`add_dirs`、`env`（白名单之后的完整环境）、`home`、`state_dir`、`provider: {protocol, base_url, model, api_key, effort, context_window}`、`system_prompt: {mode, text}`、`tools: {builtin, read_only, platform}`、`extensions`、`limits: {max_requests_per_turn, tool_timeout_s, tool_output_bytes}`、可选 `fork_from: {session_id}` | `{session_id}`（SDK 原生的新 id） | `api_key` 只在内存里，任何一端都不写日志；分叉从 `state_dir` 中的源副本新建会话 |
| `session/load` | 同上，加 `session_id`，不允许 `fork_from` | `{session_id, resumed: true}` | 找不到或已被占用回 `SESSION_NOT_FOUND`、`SESSION_OWNED`，Looper 走现有的续接失败路径 |
| `session/prompt` | `session_id`、`trace_id`、`text`、可选 `tools`（这一轮的平台工具快照，变了才带）、可选 `cause_message_id` | `{accepted: true}` | 这一轮以 `settled` 更新结束（第 6.5 节）；正在跑时回 `SESSION_BUSY` |
| `session/steer` | `session_id`、`trace_id`（Looper 认为正在跑的那一轮）、`message_id`、`text`、`if_idle`、`new_trace_id` | `{status: "queued"}` 或 `{status: "started"}` | 按 `message_id` 去重；结果只说明已接受，进没进模型看 `steer_applied` |
| `session/follow_up` | `session_id`、`trace_id`、`message_id`、`text`、`new_trace_id` | `{status: "queued"}` 或 `{status: "started"}`（空闲时立即开始） | 当前这一轮静止后开始 |
| `session/cancel` | `session_id`、`trace_id` | `{}` | 这一轮以 `cancelled` 静止；5 秒内没有静止，运行器强制释放这个实例（`forced: true`），会话记为 poisoned |
| `session/compact` | `session_id` | `{tokens_before, tokens_after}` | 只在 `compaction.manual` 时可用 |
| `session/close` | `session_id` | `{}` | 释放 SDK 实例，持久化保留，空出名额 |

咨询是新 owner 和新会话 id，所以 `fork_from` 属于 `session/new`，而不是保持原 id、恢复原 owner 的 `session/load`；二者互斥。宿主先把保留会话复制成只读源文件，Pi 用 `SessionManager.forkFrom` 创建自己的可写历史；咨询结束删除整个 fork 目录。`limits.tool_timeout_s` 是每会话的 bash 上限：工具传入更短的 timeout 可进一步收紧，省略或非正数也不能绕过宿主上限。

### 6.5 流式更新（运行器 → Looper 的通知 `session/update`）

每条都带 `session_id`、`trace_id`、`kind`。

| `kind` | 字段 | Looper 的内部事件（`looper/agents/runtimes/__init__.py` 文档里的 stream-json 形状） |
|---|---|---|
| `text_delta`、`thinking_delta` | `text` | `stream_event` 的 `text_delta`；可合并（第 6.11 节） |
| `message` | 一条完整的 assistant 消息：文本、thinking 摘要、tool_use（带 SDK 的调用 id） | `assistant` |
| `tool_result` | `call_id`、`is_error`、截断后的文本 | `user` 里的 `tool_result` |
| `request_end` | `request_id`、`kind`（同 `authorize`）、`model`、`status`、`input_uncached`、`output`、`cache_read`、`cache_write`、`reasoning`（可空）、`complete` | `pricing.usage_event`（`looper/agents/runtimes/pricing.py:234`），由 Looper 定价；同时释放 (d) 的名额。每个获准的请求恰好一条 |
| `compaction` | `tokens_before`、`tokens_after`、`trigger`、`ok` | `compact_boundary`；失败只留诊断 |
| `retry` | `request_id`、`attempt`、`reason` | 诊断 |
| `steer_applied` | `message_id`、`after_request_id`、`after_tool_batch`、`new_turn` | 记进这一轮；执行者的引导记一条活动 |
| `follow_up_started` | `message_id`、`parent_trace_id` | 新一轮开始 |
| `steer_dropped` | `message_id`、`reason`（`cancelled`、`turn_ended`、`session_closed`） | 交回调用方 |
| `settled` | `outcome`、`final_text`、`requests`、`usage_complete` | `result` |

- **轮次结果 `outcome`：** `completed`、`cancelled`、`capped`、`max_requests`、`failed`（带错误种类，第 6.9 节）。`lost` 不是运行器给的：运行器崩溃时由 Looper 补上。
- `settled` 只在 SDK 到达静止点后才发（Pi `agent_settled`，DSH `whenIdle()`，Norma `KindResult`）；它是这个 trace 的最后一条更新。运行器只有一个写出点，同一会话的更新严格有序。

### 6.6 反向请求（运行器 → Looper）

- **`tools/call`**：参数 `session_id`、`trace_id`、`call_id`、`name`、`arguments`；结果 `{text, is_error, ran}`，`text` 已由 Looper 截到 100 KB。Looper 只在这个会话正在跑这个 trace 时执行；否则回 `stopped`，与 `ops.stop_turn` 之后的调用一致。
- **`budget/authorize`**：参数 `session_id`、`trace_id`、`request_id`、`kind`（`turn`、`retry`、`compaction`、`subagent`、`tool`、`title`、`other`）、`provider`、`model`、`estimated_input_tokens`、`max_output_tokens`；结果 `{allow: true}` 或 `{allow: false, reason}`（`turn_cap`、`run_cap`、`task_cap`、`max_requests`、`stopped`、`provider_saturated`）。Looper 可以让它排队（第 5.3 节 (d)）；运行器一直等到答复，这一轮被取消或连接断开时按拒绝处理。
- **`exec/acquire`**：参数 `session_id`、`trace_id`、`kind`（`shell`、`process`）；结果 `{slot_id}`，可能排队（第 5.3 节 (e)）。用完发通知 `exec/release {slot_id}`；连接断开时这个运行器的名额全部收回。

### 6.7 总线信封（第三类消息）

| 方法 | 方向 | 参数 | 说明 |
|---|---|---|---|
| `bus/deliver` | Looper → 运行器（通知） | `session_id`、`envelope`（`MessageEnvelope.to_dict()`）、`text`（Looper 渲染给模型的文本）、`delivery: {method, trace_id, new_trace_id, if_idle}`，`method` 为 `steer`、`follow_up`、`start` | 同一会话按顺序发；运行器按 6.3 节的规则执行 |
| `bus/ack` | 运行器 → Looper（通知） | `message_ids` | 信封的消息写进 SDK 的会话记录之后（`steer_applied`、`follow_up_started` 或开出的这一轮已记录用户消息），或因 Looper 的取消被丢弃之后，才确认 |
| `bus/publish` | 运行器 → Looper（请求） | `session_id`、`trace_id`、`envelope`（运行器生成 `message_id`，`source` 不用填） | 结果 `{message_id}`：已写进日志。重试用同一个 `message_id`，Looper 去重 |

### 6.8 运行器管理

- `runner/health`（Looper → 运行器）：结果有 `sessions`、`running`、`inflight_requests`、`tool_processes`、`rss_bytes`、`event_loop_lag_ms`（Node）或 `goroutines`、`sched_latency_ms`（Go）。
- `shutdown`（Looper → 运行器）：见第 5.4 节。
- `rpc/cancel`（双向通知）：`{id}`，取消对方一个还没回答的请求（JSON-RPC 本身没有取消，做法同 LSP 的 `$/cancelRequest`）。

### 6.9 错误码

JSON-RPC 标准码照用（-32700、-32600、-32601、-32602、-32603）。应用错误码：

| 码 | 名称 | Looper 的处理 |
|---|---|---|
| -32001 | `SESSION_NOT_FOUND` | `session_started = false`，走现有的续接失败、交接、续接简报路径 |
| -32002 | `SESSION_OWNED` | 同上，另记一条诊断（单 owner 规则被打破） |
| -32003 | `SESSION_BUSY` | tier 的逻辑错误：记错误，这一轮失败 |
| -32004 | `NOT_RUNNING` | `if_idle=reject` 的引导：`send` 返回拒绝，消息走 `missed_messages`（Worker 引导 RFC D5） |
| -32005 | `TRACE_MISMATCH` | 同上 |
| -32006 | `CAPACITY` | RunnerSupervisor 另找或另起运行器 |
| -32007 | `UNSUPPORTED` | 能力没声明：Looper 的缺陷，记错误 |
| -32008 | `ISOLATION_REFUSED` | 运行器无法执行要求的隔离值：会话不开，这一轮失败，原因写明 |
| -32009 | `VERSION_MISMATCH` | 运行时不可用 |
| -32010 | `PROVIDER_AUTH` | 「provider 拒绝了 API key」（API 运行时 RFC 第 6.6 节的同一句） |
| -32011 | `PROVIDER_UNAVAILABLE` | 「模型接口暂时不可用」，附状态码 |
| -32012 | `CONTEXT_OVERFLOW` | 现有的上下文超限处理 |
| -32013 | `OUTPUT_TRUNCATED` | 「回复超过长度上限，没有完成」 |

provider 的错误也可以作为 `settled` 里 `outcome: failed` 的 `error.kind` 出现，取值同上；错误文本有界，经脱敏。

### 6.10 取消

- **Looper → 运行器：** `session/cancel` 取消这一轮；`rpc/cancel` 取消一个反向请求的等待（例如这一轮已停止，排队中的 `authorize`、`exec/acquire` 不用再等）。
- **运行器 → Looper：** SDK 取消正在跑的平台工具时，运行器对那个 `tools/call` 发 `rpc/cancel`。Looper 不打断已经开始的一次写库：调用跑完，结果丢弃；还没开始的不再开始；room inbox 这类长等待立即结束。超时和取消都不能证明服务端没有执行（pi 运行时 RFC 第 4 节同一说法）。
- **会话作用域：** 取消一个会话只结束这个会话的工具子进程；运行器为每个会话单独记录子进程，从不调用 SDK 的「杀掉全部子进程」（Pi 有全局的子进程集合，`pi:dist/utils/shell.js:138`）。

### 6.11 顺序、确认与背压

- **顺序：** 同一会话的 `session/update` 有序；同一目标的 `bus/deliver` 有序；不同会话之间不保证。
- **Looper → 运行器：** 每个运行器的总线在途窗口默认 64 个信封或 1 MiB，取先到者；满了，RunnerBridge 不再从总线队列取，消息留在队列里，日志里仍是未送达。
- **运行器 → Looper：** 运行器的写出队列有界；超过高水位时相邻的 `text_delta` 合并；其余更新一条不丢，写不出去就让 SDK 的事件回调等待，背压一直传到 SDK。
- **Looper 一侧：** 一个读协程读 stdout，按会话分发到有界队列；某个会话处理慢，只影响这个会话的 delta 合并，不阻塞读协程。
- **确认超时：** 30 秒没有 `bus/ack` 只记诊断和指标，不在同一条连接上重投；重投只发生在重连之后。等待长工具跑完的引导可以合法地超过 30 秒，诊断只用于发现卡住。

## 7. 消息总线集成

### 7.1 现状

`MessageBus` 是进程内的 asyncio 总线（`looper/agents/bus.py:111-298`）：每个 agent 一个队列（`:202-206`）；发布之前把信封写进 JSONL 日志并 fsync（`:56-61`、`:163-168`），状态更新不写日志（`:24-31`）；`subscribe` 在消费者取下一条时把上一条记为已送达（`:208-215`）；`settle` 把仍在队列里的信封记为已送达，用于用户停止（`:127-129`）；重启时重放没送达的信封，但规划者、执行者的直接丢弃（`:21-23`、`:131-161`）。评审者的地址也是 `worker` 类型（`looper/agents/tiers/planner.py:2169`、`looper/agents/message.py:384-385`），同样适用。记为已送达的那一行没有 fsync（`bus.py:63-67`），崩溃后已确认的信封可能被重放，所以语义本来就是至少一次。现在总线上的参与者是 Python 的 tier 对象，运行时进程不在总线上。

### 7.2 托管 agent 的地址与消息类型

- 托管 agent 有自己的地址，不占用 tier 的地址：`agent_type` 与对应 tier 相同（`manager`、`planner`、`worker`），`agent_id` 是 tier 的 id 加后缀 `.agent`，如 `planner-<task_id>.agent`、`worker-<run_id>.agent`（Q5）。tier 仍在原地址上收它现有的消息（`USER_MESSAGE`、`TASK_REQUEST`、`WORK_ITEM`、`WORKER_INPUT`、`KILL_WORK` 等），继续用会话控制驱动它的会话。`agent_type` 不变，第 7.1 节的重启丢弃规则自动适用于按任务存在的托管 agent。
- **新消息类型：** `AGENT_MESSAGE`（agent 之间的正文，来自托管 agent 或 Looper 的服务）、`AGENT_NOTICE`（Looper 给 agent 的一句提醒，如 agent room 的「有新消息给你」）。v0 只有这两类投给托管 agent。
- SDK 自己的子 agent（Norma 的 `subagent`、`coordinator`，DSH 的 spawn/fork）属于父会话，不是 Looper 的 agent，没有总线地址；它们的模型请求照样经过 `budget/authorize`，`trace_id` 是父会话这一轮的。

### 7.3 投递

- RunnerBridge 不用 `subscribe`，改用总线新增的两个显式接口：`lease(agent_id)` 取出一条但不记已送达；`ack(message_id)` 才记已送达。RunnerBridge 收到 `bus/ack` 才调 `ack`。
- **投递方式由 Looper 决定（策略），由所属 tier 给出：** 托管 agent 忙时，`AGENT_NOTICE` 投成 `steer`（短提醒，在同一 trace 的下一个边界进入），`AGENT_MESSAGE` 投成 `follow_up`（正文不打断正在做的事，与 agent room RFC D2「不推正文」一致）；空闲时投成 `start`，开一轮。凡是可能开出新一轮的投递，tier 先登记这一轮（新 `trace_id`、回合绑定、记录器、预算范围，`cause_message_id` 是信封的 `message_id`），这一轮的工具调用、预算和结果处理与 tier 自己开的轮次完全一样。
- 运行器只负责执行：

| `method` | Pi | DSH | Norma |
|---|---|---|---|
| `steer` | `session.steer` | `agent.steer`（运行中） | 上游新增的每会话收件箱；合入之前用 `Compactor.Pre` 队列过渡（第 11.3 节） |
| `follow_up` | 运行器排队，`agent_settled` 之后用新 trace 再 `prompt`（Pi 原生的 `followUp` 会在同一次运行里继续，静止点在它之后，分不出两轮，需要验证） | 运行器排队，`whenIdle()` 之后用新 trace 再 `followup` | 运行器排队，`KindResult` 之后用新 trace 再 `Prompt` |
| `start` | `session.prompt` | `agent.followup` | `Session.Prompt` |

- **去重：** 运行器把每个已交给 SDK 的 `message_id` 记进 SDK 的会话记录（Pi 的自定义条目，DSH 的 `UserMessage` 自带 id，Norma 的 transcript），`session/load` 时重建去重集合；运行器重启后的重投不会让同一条消息进入模型两次。三家的写法各需要验证。
- **重投：** RunnerBridge 为每个目标保留「已发未确认」列表；连接断开或运行器重启后，会话重新打开时先按原顺序重投这些，再从总线队列取新的。
- **停止：** 用户停止一轮时，投给这个托管 agent、还没确认的信封按现有的 `take_queue` 加 `settle` 处理（`bus.py:275-285`），不重放；任务结束后，按任务存在的托管 agent 的残留信封记为已送达并记一行日志。

### 7.4 托管 agent 发出的信封

- 运行器用 `bus/publish` 发出，RunnerBridge 盖上 `source`（这个会话的地址，忽略运行器给的值），按角色检查允许的消息类型和目标（v0：`AGENT_MESSAGE`，目标限于同一任务的 agent，与 agent room 的范围矩阵一致），写日志后发布。
- 两个 agent 在同一个运行器里，也走「运行器 → RunnerBridge → 总线 → RunnerBridge → 运行器」，不在运行器里抄近路：留痕、限额和重启语义只有一处。
- agent room 现有的工具照旧经 `tools/call` 执行，消息照旧入库（agent room RFC D2）。总线信封只用于提醒和以后的直接消息，不取代办公台的存储。

### 7.5 换成外部 broker

RunnerBridge 只依赖总线的 `publish`、`lease`、`ack`、`settle` 和按消息类型的观察者。总线核心换成 PostgreSQL 表（按目标的未确认行，`LISTEN/NOTIFY` 唤醒）或 NATS JetStream（按目标的 subject，显式 ack）时，信封、RunnerBridge 和运行器都不变。

## 8. 工具、预算、skill 与 extension（三种运行器共用）

### 8.1 平台工具：反向 `tools/call`

- **定义：** `session/new` 带这个角色的平台工具清单；管家每一轮在 `session/prompt` 里带当前快照（工具范围会随场景管家变化，API 运行时 RFC 第 6.2 节）。名字、说明、`inputSchema` 原样不动，运行器按 SDK 的格式注册：Pi 用 `customTools`；DSH 在 agent 作用域里 `agent.ctx.tools.register`，output schema 写成通用 JSON；Norma 用 `tool.Build`，标为非只读、非并发安全（`norma:tool/tool.go:96-163` 的默认值）。
- **工具名：** 用原名（`looper_tasks` 等），与 `api` 一样，不再加 `mcp__looper__` 前缀。这是模型可见内容的变化，要跑评测（第 15.7 节）。
- **执行：** RunnerBridge 按会话的角色分派到与 HTTP 处理器相同的函数：管家是 `ManagerOps.call(name, args, binding=<这一轮的绑定>, call_id=...)`（`looper/agents/http_server.py:881-887`，做法同 `looper/agents/runtimes/api/tools.py:19-35`）；规划者是 `PlannerOps.call(<令牌>, ...)`（`http_server.py:925-930`）；执行者和评审者是 `RoomOps.call(<令牌>, ...)`（`http_server.py:903-908`）。令牌和绑定只在 RunnerBridge 的会话表里。
- **不变：** 两步确认、自主轮只读、范围检查、`manager_actions` 留痕都在 `ops.call` 里。参数不是 JSON 对象时回 `invalid` 且不写审计行（`tools.py:24-27`）。
- **结果：** 超过 102,400 个 UTF-8 字节时由 Looper 截断（`tools.py:29-34` 的逻辑，提到共用位置）；运行器不再截。Norma 对工具输出有 30,000 字符的总上限（`norma:harness/tools.go:102-108`），运行器要为平台工具把它调到不低于 100 KB。
- **超时：** 沿用 120 秒；room inbox 的 `wait_seconds` 用 `max(120, wait_seconds + 20)`（pi 运行时 RFC 第 4 节）。
- **去掉：** Pi 的 HTTP 扩展桥和 `LOOPER_AGENT_TURN_FILE`；DSH 的 `looper mcp-server` 子进程；SDK 运行时的管家不做工具探测（同 `api`，`looper/agents/tiers/manager.py:1113-1115`）。

### 8.2 SDK 内置工具

bash、读写、编辑、搜索在运行器里执行，受第 12 节的隔离约束；起子进程之前先 `exec/acquire`。只读角色（规划者、咨询）由 Looper 给出允许的工具名集合：Pi 用工具清单加 `tool_call` 拦截（`pi:docs/extensions.md:170-178`）；DSH 用 `tools.guard`（同 `dsh_bootstrap.mjs:21-28`）；Norma 用 `AllowedTools` 加 `PreToolUse` 钩子。`ReadOnly` 记为 `PERMISSIONS`，拒绝写进运行记录。

### 8.3 skill 与 extension 扩展点

`session/new.extensions` 是 Looper 选定的清单，每项有 `kind`、`id`、`version`、`path`（Looper 物化的只读副本）、`config`。v0 的种类：`skill_dir`（SKILL.md 目录）和 `runner_extension`（运行器语言写的模块）。哪个角色拿到哪些扩展由 Looper 决定；运行器只加载给它的，SDK 自己的发现一律关闭。不支持的种类记进 `launch_record.runtime_catalog` 的省略清单并写明原因（同 dsh 运行时 RFC 第 5 节「能力目录」）。

| 种类 | Pi | DSH | Norma |
|---|---|---|---|
| `skill_dir` | `DefaultResourceLoader` 的 `additionalSkillPaths`，`skillsOverride` 去掉默认发现的（`pi:dist/core/resource-loader.d.ts:70-110`） | `skill-filesystem` 的 `customSkillDirs`，`includeDefaultRoots: false`、`watch: false` | `skill.LoadDir(dir)` 交给 `Options.Skills`（`norma:skill/skill.go:204-290`） |
| `runner_extension` | Pi 扩展：内联的 `extensionFactories` 或 `additionalExtensionPaths`；以后支持 Pi package（`pi:docs/packages.md`） | 在 `agents.create` 的 `setup(agentCtx, agent)` 里装 Cordis 插件 | Go 没有可靠的动态加载：扩展编进运行器，按 `id` 启用 |
| 能力目录里的 MCP（之后） | Pi 的 MCP 扩展（SDK 默认不加载，`pi:docs/sdk.md:114-116`） | `dsh-mcp-client` | `mcp` 包只有 stdio、不能设 cwd（`norma:mcp/stdio.go:27-61`），需运行器自己的启动器 |

平台工具不走这个扩展点，由 `tools/call` 提供，任何角色都不能关掉。

### 8.4 预算与用量

- **定价：** `request_end` 报每次请求的原始 token。Looper 用 `price_cost`（`looper/agents/runtimes/pricing.py:216`）按 provider 价格定价，模型目录优先，内置价目表兜底；查不到价格就只记 token，花费为未知，不记 0。SDK 自算的费用（Pi 的 `getSessionStats().cost` 等）只进诊断。
- **请求之前：** 运行器包装 SDK 的模型请求层（各家的包装点见第 9–11 节），每次请求前发 `budget/authorize`。Looper 的判定：`本轮已花 + 本次估计 ≤ 本轮上限`，对单次运行、任务上限同样成立，本轮请求数没到上限，provider 的并发名额可用。估计 = 估计输入 × 未缓存输入价 + min(max_output, 8,192) × 输出价（Q8）。拒绝时运行器不发请求，这一轮以 `capped` 结束。
- **请求之后：** RunnerBridge 收到 `request_end` 后重新计算；Looper 自己的累计到达上限时（例如估计偏低），发 `session/cancel`，在运行记录里记下超出额。用户在一轮中途调低上限，下一次 `authorize` 就按新值判断。
- **完整性：** 某次请求没有用量，这一轮 `usage_complete = false`，花费为未知（同 API 运行时 RFC 第 8.3 节）。运行器发现一次没法归到某个会话的模型请求时，拒绝它并记 `usage_unattributed`（第 13 节的告警）。
- **与现在的区别：** CLI 运行时是「用量到了才判断」的软上限；SDK 运行时在请求之前挡住。剩下的缺口是各家 SDK 内部有没有绕过包装层的请求，在第 9–11 节逐项标出。

## 9. Pi 运行器（TypeScript）

### 9.1 进程结构

Node 进程，导入已安装的 `@earendil-works/pi-coding-agent`：从探测到的 `pi` 可执行文件的真实路径解析包（同 `dsh_bootstrap.mjs` 的 `createRequire(realpathSync(executable))`），不打包、不在运行时安装。源码用 TypeScript，只用可擦除的语法，由 Node ≥ 22.19 自带的类型擦除直接运行，版本门禁与安装包入口由测试覆盖。

所有会话共用一个 JS 事件循环。Pi 的会话 JSONL 是同步写的，settings、auth 的同步锁重试有 20 ms 忙等、最多 10 次（`pi:dist/core/session-manager.js:754`、`:812`、`pi:dist/core/settings-manager.js:126`、`pi:dist/core/auth-storage.js:45`），会卡住同进程的其他会话。运行器用内存里的 SettingsManager，不用 AuthStorage，避开锁路径；事件循环延迟上报，并参与准入（第 5.3 节 (b)）。离线实测里 50 个会话各跑一轮时 p99 是 18.0 ms（第 2.3 节，短时测量）。

导入 SDK 就会加上 SIGINT、SIGTERM、SIGHUP 的监听，释放会话也不移除（第 2.3 节）。运行器接管这些信号，并以 stdin EOF、`shutdown` 或信号统一触发退出与清理；SIGTERM 等待 hooks 的路径已覆盖（第 9.6 节）。

### 9.2 协议映射

| 协议 | Pi 1.3.0 |
|---|---|
| `initialize` | 核对 1.3.x 和用到的导出；能力：`per_session_isolation: true`（第 9.3 节）、`per_session_credentials: true`（每会话一个 ModelRuntime 加 `setRuntimeApiKey`，隔离验证见第 9.6 节）、`steer: {in_trace: true, boundary: "after_tool_batch", idle_start: false}`、`cancel`、`fork: true`、`authorize_covers: [turn, retry, compaction]`；follow_up、manual compaction/events 仍待做 |
| `session/new` | `createAgentSession({cwd, agentDir, modelRuntime, model, thinkingLevel, sessionManager, settingsManager, resourceLoader, tools, customTools})`（`pi:dist/core/sdk.d.ts:10-64`）：每会话自己的 ModelRuntime、`SessionManager.create(cwd, state_dir)`、内存 SettingsManager（关掉 cache warming、遥测）、`DefaultResourceLoader`（`noContextFiles`，只加载 Looper 给的扩展和 skill，`systemPrompt` 或 `appendSystemPrompt`）；`customTools` 是平台工具代理和运行器包装过的内置工具 |
| `session/load` | `SessionManager.open(<state_dir 里唯一匹配的文件>)` 后同上；保持原 session id，不允许 `fork_from` |
| `session/new.fork_from` | 从宿主在 `state_dir` 准备的只读副本，经 `SessionManager.forkFrom(source, cwd, state_dir)` 创建不同 id 的会话；源会话不写，咨询结束清理 fork 目录 |
| `session/prompt` | `session.prompt(text)`；`agent_settled` 发 `settled`，`agent_end` 之后还可能重试或继续（`pi:docs/sdk.md:72-94`） |
| `session/steer` | 运行中 `session.steer(text)`（`pi:dist/core/agent-session.d.ts:570`）：当前回合的工具结束后、下一次请求前进入，同一 trace。离线实测：bash 运行中调用，下一次 provider 请求就带上了这条消息，全程只有一次 `agent_start`/`agent_end`（第 2.3 节）。`message_end` 的 user 消息用于跟踪，进入 provider 请求后才发 `steer_applied`；空闲时拒绝，结束竞争见第 9.6 节 |
| `session/follow_up` | 运行器排队，见第 7.3 节 |
| `session/cancel` | 先 `session.clearQueue()`，再 `await session.abort()`（`:636`、`:650`；`abort` 不清队列）；清掉的引导各发 `steer_dropped` |
| `session/compact` | `session.compact()`（`:722`） |
| `session/close` | `session.dispose()`（`:387`）：停止工作、使扩展上下文失效、移除订阅 |
| 事件 | `session.subscribe`（`:380`）：`message_update` 的 `text_delta` → delta；`message_end` → `message`、`tool_result`；`compaction_end` → `compaction`；`auto_retry` → `retry` |
| 用量 | 每条 assistant `message_end.usage`、`compaction_end` 的用量、toolResult 里嵌套的用量各记一次 `request_end`；input 已是未缓存的，cacheRead、cacheWrite 分列，reasoning 含在 output（`looper/agents/runtimes/pi.py:415-418`）。`getSessionStats()` 是累计值，只进 `runner/health` |
| `budget/authorize` | 在 ModelRuntime 上注册 Looper 自己的 provider（`registerProvider(id, {api, streamSimple, models, ...})`，`pi:dist/core/model-runtime.d.ts:117`），它的 `streamSimple` 先授权，再委托给 pi-ai 对这个协议的实现；spike 用同样的注册方式接上了假 provider（第 2.3 节）。`createAgentSession` 把 `streamFn` 绑定到这个 runtime（`pi:dist/core/sdk.js:253`、`:303`）。自动压缩、自动重试均从这里授权和归属（第 9.6 节），拒绝发生在请求发送前；咨询分叉只复制已存历史，不生成分支摘要 |

### 9.3 隔离

- **cwd：** 显式绝对路径。SDK 里多处默认取 `process.cwd()`（`pi:dist/core/sdk.js:70`、`pi:dist/utils/paths.js:83`），运行器不依赖这些默认值，也不调用按进程 cwd 解析路径的导出功能（`pi:dist/core/session-export.js:26`）。
- **路径：** Looper 内联扩展的 `tool_call` 处理器拦截读、写、编辑、搜索、列目录工具里落在 cwd 和 `add_dirs` 之外的路径（`{block: true, reason}`）。
- **env 与 HOME：** `createAgentSession` 没有 env 选项，默认 shell 每次都从 `process.env` 派生环境（`pi:dist/utils/shell.js:115`）。运行器用 `createBashToolDefinition(cwd, {operations, spawnHook})` 自己构造 bash，以同名 `customTools` 覆盖官方的 bash：`spawnHook` 把 env 整个换成 Looper 给的完整环境（含私有 HOME），`operations` 记录这个会话的子进程并先 `exec/acquire`（`pi:dist/core/tools/bash.d.ts:40-80`）。离线实测：替换后父进程的哨兵变量不再出现，合并 `context.env` 的正向对照则会出现（第 2.3 节）。替换也会去掉 `PI_*` 会话变量；Looper 默认不放进白名单。扩展的 `execCommand` 只传 cwd 不传 env（`pi:dist/core/exec.js:10`），所以 v0 不加载会起进程的第三方扩展。
- **agentDir：** 每会话一个私有目录。它只影响 auth、models、settings、资源和默认会话目录，不改变 Node 看到的 HOME（`pi:dist/core/sdk.js:70`）。
- **进程级设置：** `applyHttpProxySettings`、`configureHttpDispatcher` 改进程全局（`pi:dist/core/http-dispatcher.js:42`、`:68`），启动时按 Looper 的代理配置调一次。只导入 SDK，不嵌入 CLI 的 main，避免装上交互模式的信号处理。
- **全局状态：** 旧的 `pi-ai/compat` 有全局 provider 注册表，`pi-agent-core` 有全局 `defaultStreamFn`（`pi-ai:dist/compat.js:49`、`:121`、`pi:dist/core/sdk.js:21`）；主路径显式绑定 runtime，运行器不调用这些全局接口。扩展 factory 的缓存是全局的，只有一个活动 cwd（`pi:dist/core/extensions/loader.js:86`、`:461`）；Looper 的内联扩展不依赖模块级变量。

### 9.4 角色与会话

- **管家、规划者：** 常驻的 RunnerSession，一个 owner 一个 Pi 会话，跨轮不重启；服务或运行器重启后 `session/load`。规划者用只读工具（read、grep、find、ls 加平台工具）。
- **执行者、评审者：** RunnerChannel；两者均保留 read、bash、edit、write 加平台工具，与 CLI 路径同组工具。文件工具只能访问任务目录与 `add_dirs`（含真实路径检查）；bash 不受路径约束，上限为该角色的 `timeout_seconds`。`max_turns` 在运行器路径限制模型请求数，未设置时 1000；每次请求前检查运行和任务花费上限。规划者的引导经 `session/steer` 在同一 trace 内生效；丢弃的引导交回规划者决策。
- **咨询：** 以 Worker 角色、`read_only=true` 分叉保留会话，只提供 read、grep、find、ls，不给 bash、edit、write 或平台工具；源 JSONL 保留不变，结束后删除副本和 fork 会话。agent room 的保留目录为 `.local/pi/run-sessions/<task>/<run>/`。
- **共享容量：** 四类角色与咨询共用一个 runner 的 10 个会话，`CAPACITY` 按启动失败结束步骤；没有 CLI fallback。
- **一个会话文件只有一个活动 owner。** 全局的文件写队列只保证同一文件串行写（`pi:dist/core/tools/file-mutation-queue.js:1`），不保证多个 agent 改同一工作树时业务正确，这由步骤的工作目录隔离负责。

### 9.5 取代 `pi.py` 里的哪些路径（S1 完成时删除）

| 现在 | 之后 |
|---|---|
| `ResidentPi(PerTurnSession)` 每轮一个 JSON 进程（`pi.py:621-697`） | RunnerSession |
| 执行者、评审者的 `-p --mode json` 运行（`pi.py:214-266`） | RunnerChannel |
| `PiChannel` 的 RPC 引导（`pi_steer.py`） | trace 内的 `session/steer` |
| `pi_extension/looper.ts` 经 HTTP 调平台工具，令牌在环境里，回合绑定靠文件 | 运行器注册工具代理，经 `tools/call` 回到 Looper |
| `PiStream` 翻译 JSON 事件（`pi.py:422-597`） | 运行器映射成 `session/update`，Looper 一侧一份通用翻译 |
| 生成 `models.json`、`settings.json`，key 在 `LOOPER_PROVIDER_API_KEY`（`pi.py:741-884`） | 内存里的 ModelRuntime、SettingsManager；key 在 `session/new` |

### 9.6 实现进展与验证缺口

Pi 1.3.0 配 loopback provider 的定向测试已覆盖每会话 ModelRuntime/key 隔离及独立取消、owner JSONL 的一次续接、路径与 bash 环境隔离；SIGTERM 接管后等待清理 hooks，runner 丢失时回收工具进程。重试和自动压缩经过注册 provider，分别以 `retry`、`compaction` 授权和记账；被拒重试不发请求，压缩不能归属时明确报 `usage_unattributed`，不冒充完整用量。

引导从用户消息事件跟踪，但只有该消息进入真实 provider 请求才发 `steer_applied`；与结束、取消或关闭竞争时必须发 `steer_applied` 或 `steer_dropped`，被丢弃的消息同时从持久上下文省略，不能漏到下一轮。空闲引导仍拒绝，不宣称 `idle_start`；独立的 compaction 通知更新仍待补。

ops-file opt-in `[runtimes] pi = "runner"` 已覆盖 Manager、Planner、Worker、Reviewer 与咨询，默认仍为 `"cli"`，启动读取一次；一个 runner、10 会话后 `CAPACITY`，provider env 拒绝、无 CLI fallback。新增 installed 隔离 serve 验证覆盖两步任务、同 trace 引导、Reviewer、只读咨询分叉、逐请求计费，以及 Worker 请求前预算拒绝、会话级取消、运行器崩溃与重启后的任务收敛；本轮在 Pi 1.3.0、Node 24.20.0 上的两项 SDK 工具集/只读分叉测试通过；serve 文件的 4 项测试因未设置一次性 `LOOPER_TEST_DATABASE_URL` 跳过（包括本卡两项），serve 结论仍未验证，不能计为通过。

Pi CLI 与 runner 的真实任务对照命令及门禁已在 [evals/README.md](../../../evals/README.md#pi) 准备；待 lead 通过本地 CPA、`gpt-6-astra` 跑 9 个任务各 2 次，逐任务要求 runner 通过数至少为 CLI 减一，错误单列并重跑一次，花费上限截断须提高上限重跑。此次尚未运行，结果未验证；`reviewer_*` 用 Claude Code Reviewer，不能比较 Pi Reviewer，后者由 installed 测试覆盖。容量数字、运行时卡、长上下文、长期内存/事件循环延迟、thinking 钳制和全部生产 schema 普查仍开放。

## 10. DSH 运行器（TypeScript）

### 10.1 进程结构与 RC 风险

- Node 进程，用 DSH 的进程内接口，而不是它的 SDK stdio wire：那个 wire 只有 `initialize`、`session/prompt`、`session/resume`、`session/wait`、`session/working-directory/get|set`、`shutdown`（`dsh-sdk-protocol:lib/types/types.d.ts:128-158`），没有 steer、单会话 cancel、自定义工具、system prompt、compact，而且新会话共用 `initialize` 时给的 cwd 和模型（`dsh-sdk-jsonrpc-server:lib/index.js:120`、`:219`）。
- 启动时调一次 `runProfile(options)`，得到 `{ctx, shutdown}`（`dsh:lib/types/profile-boot.d.ts:69-96`）；一个 Cordis root、一个 factory、每个会话一个 agent 作用域。composition 沿用 dsh 运行时 RFC 第 5 节的 overlay（关掉遥测、会话日志上传、标题请求、设置编辑、插件管理、账号路由，`tool-web` 默认不搜索），去掉 headless runner。`runProfile` 会装 SIGTERM、SIGINT 处理器（`dsh:lib/profile-boot-C14i-8IN.js:255`、`:274`），只装一次；运行器的退出仍以 stdin EOF 和 `shutdown` 为准。
- 运行器的 cwd 和 `DSH_HOME` 都是私有目录，里面没有 `.env`：`loadLayeredEnv` 会从调用时的 cwd 和 DSH home 读 `.env` 写进 `process.env`（`dsh-app-boot:lib/index.js:3725`）。代理模块会改 `process.env` 和全局 dispatcher（`dsh-http-proxy:lib/index.js:291-400`），只在启动时设一次。
- **RC 风险：** 只接受精确的 0.2.1-rc.2，用到的 `@deepseek-ai/*` 包都要是这个版本；`initialize` 自检导出形状。升级要先在安装包上重跑协议映射测试和一致性测试，通过后才加进门禁列表；未知版本登记为「不支持」并给出原因，不按 semver 大小推断兼容。

### 10.2 协议映射

| 协议 | DSH 0.2.1-rc.2 |
|---|---|
| `initialize` | 版本与导出自检；能力：`per_session_isolation: true`（第 10.3 节）、`per_session_credentials: false`（见下）、`steer: {in_trace: true, boundary: "next_step", idle_start: true}`、`follow_up`、`cancel`、`compaction: {auto, manual}`、`fork`（快照复制） |
| `session/new` | `ctx.agents.create({sessionId, meta: {cwd}, agentOptions: {provider, model, reasoningEffort, maxTokens}, setup(agentCtx, agent)})`（`dsh-agent:lib/types/index.d.ts:80-145`）；`setup` 里注册作用域内的平台工具代理、`systemPrompt.section({name: 'looper', order: 20000, text, interpolate: false})`、只读和路径 guard、会话自己的 bash 执行器 |
| `session/load` | `ctx.agents.resume({resumeSessionId, ...})`；被别的 owner 占用时拒绝（跨进程写锁，`dsh-session-persistence-jsonl:lib/index.js:619`） |
| `session/prompt` | `agent.followup(message)`（`message` 带 Looper 的 id），`await agent.whenIdle()` 后发 `settled` |
| `session/steer` | 运行中 `agent.steer(message)`：下一个步边界进入，同一 trace。离线实测：bash 运行中调用 `steer` 和 `inject`，下一次请求都带上了，会话记录里只有一个 turn、两个 step（第 2.3 节）。`steer_applied` 取自 inbox 消费的事件（事件名需要验证）。空闲时按 `start` 处理，不直接调 `steer`（它在空闲时会自己开一轮，`dsh-agent:lib/types/runtime-types.d.ts:194-200`，绕过 Looper 的轮次登记） |
| `session/follow_up` | 运行器排队，见第 7.3 节；`inject`（只进下一个 pre-step，不唤醒）v0 不用 |
| `session/cancel` | `agent.cancel(cause, {keepInbox: false})`，再 `whenIdle()`；清掉的引导各发 `steer_dropped` |
| `session/close` | `handle.dispose()`：停止并等 loop 退出，注销 Agent 和 Session，释放作用域 |
| 事件 | 会话的 durable 事件（`agent/status`、assistant 消息、工具事件），root listener 能看到所有 agent，按 sessionId 路由（`dsh-scope:lib/index.js:288`）；token 级 delta 来自 `llm/stream` 的流块 |
| 用量 | 在 `llm/stream` 里逐请求取 `TokenUsage`：input 未缓存，cacheRead、cacheWrite 分开，reasoning 已含在 output（`dsh-llm:lib/types/types.d.ts:152-174`；dsh 运行时 RFC 第 7 节） |
| `budget/authorize` | `llm/stream` 是 waterfall 事件，监听者可以调 `next()`，也可以自己产出块来短路（`dsh-llm:lib/types/index.d.ts:33-45`）：先授权再 `next()`，拒绝就产出中止的结束块。所有走 `llm.stream()` 的请求都经过它，包括压缩摘要；声明的注释写明它包住每一次流式模型调用，含重试、重放和路由。agent loop 发的请求带进程内的身份标记，手工构造的请求（压缩等）没有（同上），它们怎么归到会话和 trace，需要验证；归不到就拒绝并告警 |

**provider 路由是进程级的：** LLM adapter 注册在 Cordis root 的服务上，同一路由不能注册两次（`dsh-llm:lib/index.js:1812-1834`），DeepSeek 路由从环境变量读 key。所以按 provider 绑定（key 加 base URL）分运行器：同一绑定的会话共用一个 DSH 运行器。能否在 agent 作用域里注册带自己 key 的路由，需要验证。

### 10.3 隔离

- **cwd：** 创建时给绝对路径的 `meta.cwd`。sandbox 的 workspaceRoot 取会话 header 里原始的 cwd，之后 `workingDirectory.set` 不会移动边界（`dsh-sandbox-policy:lib/index.js:132`），这正好符合「边界就是任务 cwd」；`add_dirs` 怎么进 fs-sandbox 的允许范围，需要验证。
- **env 与 HOME：** bash-local 把 `request.env` 叠在清洗过的父环境上（`dsh-bash-local:lib/index.js:88-107`），没有整个替换的开关，标准的会话创建也没有统一的 env、HOME 字段。离线实测证实原生 shell 仍会带出父进程的哨兵变量；作用域内的自定义工具显式给 env 则不会（第 2.3 节）。所以运行器在 agent 作用域里注册自己的 bash 执行器，显式给出完整的 env（含私有 HOME），并先 `exec/acquire`。`DSH_HOME` 是 composition 级的，进程内所有会话共用；持久化按会话分目录，每个会话只有一个写 owner。
- **只读：** `tools.guard` 拒绝允许集合之外的工具（同 `dsh_bootstrap.mjs:21-28`）。
- **同步阻塞：** sandbox 探测用 `spawnSync`，子进程输出回写用 `openSync`/`writeSync`（`dsh-sandbox-local:lib/index.js:100`、`dsh-subprocess-local:lib/output.js:141`），一个会话的大输出会拖慢同进程的其他会话；限制每会话输出，事件循环延迟参与准入。
- **子 agent：** DSH 的 spawn/fork 子 agent 在同一进程里经 `ownerCtx.agents.create/resume` 创建（`dsh-subagent:lib/index.js:1793`），默认深度 1、同时 8 个。v0 关掉，等子 agent 的环境传递和授权归属验证之后再开。

### 10.4 角色与会话

四个角色都能跑，沿用 dsh 运行时 RFC 的 provider 约束：只接 DeepSeek 的 API key 和 Messages 协议，精确的模型白名单和 effort。管家、规划者是常驻会话；执行者、评审者第一次有运行中引导。咨询沿用「复制已结束会话的快照，再只读续接」，不写源会话。`dsh.py` 里每轮一个 headless 进程的 `ResidentDsh` 和一次运行一个进程的 run 由 RunnerSession、RunnerChannel 取代；`dsh_bootstrap.mjs` 的 prompt section、只读 guard 搬进运行器的 `setup`；平台工具不再经 `dsh-mcp-client` 和 `looper mcp-server`。S2 完成时删除这些 CLI 路径。

### 10.5 已知缺口（需要验证）

离线实测已证明同进程并发、每会话 cwd、作用域工具给出的干净环境、同一 turn 内的引导、按会话取消（第 2.3 节），用的是最小 kernel 组合。仍需验证：完整组合（持久化、终端、压缩、遥测关闭的 overlay）下的行为和内存；子进程模块那个进程级 exit hook 在长时间运行里的影响；手工构造的请求能否归到会话和 trace；压缩摘要、web search、子 agent 的用量是否都经过 `llm/stream`（dsh 运行时 RFC 第 7 节记为未知）；`add_dirs` 怎么进 fs-sandbox；作用域内带 key 的 provider 路由；崩溃后会话的写锁能否被新进程接管；作用域内的事件和工具在多个 agent 之间互不串扰；引导进入模型的事件；RC 之后的接口变化。

## 11. Norma 运行器（Go）

### 11.1 进程结构

Norma 运行器是一个 Go 程序（暂名 `looper-norma-runner`），一个进程承载多个 Norma 会话，是三个里面向高并发的那一个。Norma 和 Go 的并发效率更高，是维护者的假设。离线实测里 50 个会话各跑一轮，运行器 RSS 从 13.0 MiB 到 22.7 MiB，活动 goroutine 51 个、结束后回到 1 个（第 2.3 节）；这是短时单次测量，吞吐、长期内存和延迟的优势仍属推测，需要压测验证（第 15.5 节）。

- **读写：** 一个 goroutine 读 stdin、解码、分发；一个 goroutine 独占写 stdout，从有界 channel 取消息（第 6.11 节的背压就是这个 channel）。
- **会话：** 每个会话一个 owner goroutine，串行跑轮次（`Session` 不能并发 `Prompt`，`norma:agentcore/agentcore.go:228-230`）；每一轮、每个平台工具调用各一个 goroutine；每个会话、每一轮各一个 `context.Context`，取消就是 cancel ctx。会话表用互斥锁保护。
- **HTTP：** 所有会话共用一个 `http.Transport`（连接复用）；代理由 Looper 显式给出，不用 `http.ProxyFromEnvironment`（Norma 默认客户端用它，`norma:llm/provider.go:216-220`）。provider 配置能否接收自定义 `http.Client` 需要验证，不能的话列入上游改动。
- **panic：** 运行器自己起的 goroutine（会话 owner、轮次、平台工具）都 `recover()`，一个会话 panic 记为这个会话失败，进程继续。Norma 内部起的 goroutine（如工具执行，`norma:harness/streaming.go:64-94`）里的 panic，运行器的 `recover()` 接不住，会让整个进程退出；harness 是否自己 recover 需要验证，没有的话列入上游改动。
- **环境：** 从不 `os.Chdir`、`os.Setenv`；工具一律用 `exec.Cmd` 显式设 `Dir`、`Env`。RunnerSupervisor 起进程时一次设好 `NORMA_RIPGREP_NO_INSTALL=1`（否则首次 Grep 会 `npm install -g`，`norma:tool/ripgrep.go:29-72`）和私有 TMPDIR；从运行器的环境里去掉 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY` 等变量（provider 没显式配置时会读它们，`norma:llm/provider.go:180-205`）。
- **上限：** 会话数、在途模型请求数、工具子进程数各一个信号量，取值来自 Looper 的准入（第 5.3 节）。

### 11.2 协议映射

| 协议 | Norma |
|---|---|
| `initialize` | 报告构建时写入的 Norma 提交和能力：`per_session_isolation: true`（靠第 11.3 节的运行器工具）、`per_session_credentials: true`、`steer: {in_trace: true, boundary: "after_tool_batch", idle_start: true}`（上游收件箱合入之前由 `Compactor.Pre` 过渡，第 11.3 节）、`follow_up`、`cancel`、`compaction: {auto: true, manual: false}`、`fork: false` |
| `session/new` | `agentcore.NewSession(Options{...})`（`norma:agentcore/agentcore.go:247`）：`Provider` 是包装过的 provider；`SystemPrompt` 或 `AppendSystemPrompt`；`Tools` 是运行器给的清单；`WorkingDir` 一定是绝对路径（空值会退回进程 cwd）；`Transcript` 写到 `state_dir`；`SessionID`；`Compactor` 是带 Looper 摘要器的 `compaction.New`；`DisableBackgroundTasks: true`；不开 `Memory` |
| `session/load` | 同上，再 `Session.Resume(session_id)`（`:364-393`） |
| `session/prompt` | `Session.Prompt(ctx, text)` 迭代事件，到 `KindResult` 为止（`norma:harness/event.go:14-22`、`:37-49`）；`Terminal.Reason` 映射为 `outcome`：`completed` → completed，`aborted_*` → cancelled，`max_turns` → max_requests，其余 → failed |
| `session/steer` | 写进这个会话的收件箱（第 11.3 节）；同一次 `Prompt` 调用的循环在这批工具结果之后、下一次模型请求之前取出它，**不再调用一次 `Prompt`**，所以仍是同一轮、同一 trace。上游收件箱合入之前，由运行器的 `Compactor.Pre` 队列在下一次请求前取出 |
| `session/follow_up` | 运行器排队，`KindResult` 之后用新 trace 再调 `Prompt` |
| `session/cancel` | cancel 这一轮的 ctx：读流时报 `aborted_streaming`，工具中报 `aborted_tools`；没跑完的工具得到「tool execution interrupted」的结果且不被等待（`norma:harness/streaming.go:166-213`）；前台 bash 按进程组结束；收件箱里没取出的引导各发 `steer_dropped` |
| `session/close` | `Session.Close()` |
| 事件 | `text`、`thinking` → delta；`tool_use`、`tool_result` → 对应更新；`KindUsage` 是累计值，不用来记账 |
| 用量 | provider 包装层逐请求收集（`message_start`、`message_delta` 里的 usage，`norma:llm/provider.go:18-37`）；`InputTokens` 含缓存（`norma:llm/types.go:101-107`），未缓存输入 = input − cache_read − cache_write，算出负数按 0 并标不完整；没有 reasoning 字段，报为空 |
| `budget/authorize` | 包装 `llm.Provider` 的 `Stream`、`Complete`（`norma:llm/provider.go:160-163`，写法可参照 `norma:llm/ratelimit.go:125-160`），请求前授权，拒绝时返回错误，这一轮以 `model_error` 结束、运行器报 `capped`（`norma:harness/query.go:432`）。压缩摘要用同一个包装过的 provider 构造；provider 内部的建连重试（`norma:llm/retry.go:14-72`）在一次授权之内，建连失败不产生 token；子 agent 是否用父会话的 provider 需要验证（v0 关掉子 agent） |

### 11.3 每会话收件箱（必需的上游改动）与隔离

Norma 没有运行中引导的接口（第 2.2 节）。`UserPromptSubmit`、`Stop` 钩子的 `AdditionalContext` 能模拟 follow-up，不能在一轮中间引导；`PostToolUse` 钩子没有返回值（`norma:harness/query.go:171-172`），不能借它注入。离线实测（第 2.3 节）：`Stop` 钩子返回 blockingErrors 时，引导到第 3 次请求才出现，不满足「下一次请求」；自定义 `Compactor` 在 `Pre` 里取空一个线程安全队列、追加 user 消息时，第 2 次请求就出现，历史完整保留。

**过渡做法：** 运行器在上游收件箱合入之前用 `Compactor.Pre` 队列。这个 compactor 包住真正的压缩器（带 Looper 摘要器的 `compaction.New`），先注入再委托；队列、顺序和这一轮结束后的清理都由运行器负责，取消或异常结束时把剩下的引导以 `steer_dropped` 丢弃，绝不留给下一轮。`Compactor.Pre` 本来是做上下文管理的，用来注入消息是借用，所以只作过渡。

**必需的上游改动：** 在 Norma 的 harness 循环里加一个每会话收件箱：

- `Session.Steer(msg)` 把消息放进收件箱；harness 循环在每批工具结果追加之后、下一次模型请求之前取空收件箱，作为 user 消息追加并写进 transcript（带 `message_id`），同时发出一个事件（运行器据此发 `steer_applied`，带这次之前的请求 id 和工具批次号）。
- 取消时收件箱里剩下的消息被丢弃并逐条报告；这一轮结束时剩下的消息交回调用方（运行器据此开新 trace 或发 `steer_dropped`）。
- follow-up 不进 harness：由运行器在这一轮结束后排队调用。
- 做在唯一上游 `loopgo-main:go/internal/norma` 里，带测试（第 15.3 节的三条 trace 测试在 Go 里各有一份）；同样的收件箱移植进 `api` 的 Python `AgentLoop`（它现在也没有引导），保持两者行为一致。

隔离由运行器的工具补齐（机制），取值来自 Looper：

- **路径：** 每个文件工具外包一层，绝对路径和 `..` 都要落在 cwd 或 `add_dirs` 内；也可以用 `PreToolUse` 钩子拦截（`norma:hook/hook.go:14-26`，在权限检查之后执行）。
- **bash：** 运行器自己的 bash 工具（`tool.Build` 加 `exec.CommandContext`），`cmd.Dir` 是会话的 cwd，`cmd.Env` 就是 Looper 给的完整环境（含私有 HOME），不叠加进程环境；Norma 的 `BashEnv` 追加在 `os.Environ()` 之后（`norma:tool/bash.go:159-165`），不能当白名单。离线实测证实了两者的区别（第 2.3 节）。起进程前 `exec/acquire`。
- **后台任务：** 关掉（`DisableBackgroundTasks`）。默认任务目录在系统临时目录下，新建会话时会删掉别的会话超过 24 小时的目录，不看对方是否还活着（`norma:tool/task.go:167-209`），常驻的运行器会误删老会话的产物。
- **子 agent 与 coordinator：** v0 不开放，子 agent 不继承父会话的 `BashEnv` 和私有 HOME（`norma:subagent/subagent.go:124-135`）。
- **MCP：** v0 不开（stdio 启动器没有 cwd，且继承运行器环境，`norma:mcp/stdio.go:27-61`）。
- **memory：** 不开（`memory.Store` 写入不加锁，`norma:memory/memory.go:102-116`）。
- **权限：** Looper 的 agent 不逐个确认工具（M1.7）；拒绝规则先于模式生效（`norma:permission/permission.go:86-136`）；只读角色用 `AllowedTools` 和钩子。

### 11.4 会话与压缩

- 会话是 Norma 的 JSONL transcript（`norma:transcript/transcript.go:36-48`），放在 Looper 给的 `state_dir`；`agent_sessions.session_id` 存 Norma 的会话 id。
- 压缩用 Norma 的自动触发，但摘要器换成 Looper 原创的摘要提示词，与 `api` 交接用的同一份（API 运行时 RFC D6）。Norma 自带的摘要提示词源自 Claude Code（在 `norma:compaction/summarizer.go` 文件后部），不使用、不移植、不引用。没有手动压缩，`session/compact` 不可用。
- 没有 fork：咨询由运行器复制 transcript 再 `Resume` 副本，需要验证。

### 11.5 其他上游改动

除了收件箱，以下也放在 Norma 的副本里（自有代码），不改 Pi、DSH：bash 只用给定的环境、不叠加 `os.Environ()`；子 agent 继承父会话的执行环境；MCP stdio 启动器能设 cwd；provider 配置接受自定义 `http.Client`；工具执行的 goroutine 里 recover panic；后台任务目录只清已结束的会话。`go/internal/norma` 是 Go 的 internal 包，别的 module 不能导入，所以运行器放进 loopgo 的 module（Q6）。

### 11.6 与 `api` 里的 Python Norma 的关系

| | `api`（Python，进程内） | Norma 运行器（Go） |
|---|---|---|
| 角色 | 只有管家 | 四个角色 |
| 工具 | 只有平台工具 | 平台工具加编码工具 |
| 进程 | 没有额外进程 | 一个运行器进程承载多个会话 |
| 会话存储 | 中性条目（计划存 `api_session_items`，现在暂存内存，`looper/agents/runtimes/api/session.py:33-44`、`:90-102`） | Norma 原生 JSONL（v0） |
| 适合 | 不需要文件和命令的管家，延迟最低 | 编码角色；需要文件或命令的管家；大量并发的执行者（并发收益属推测，以压测为准） |

- **行为兼容（两者都要做到）：** 相同的工具定义和工具名；相同的工具结果文本和 100 KB 规则（都由 Looper 执行）；相同的用量口径；相同的停止语义（取消请求；已经开始的平台工具在 Looper 一侧跑完）；相同的每会话收件箱和 trace 内引导；相同的交接摘要提示词；条目词汇有一张对照表：Norma 的 `llm.Message` 内容块（text、thinking、tool_use、tool_result，工具结果是 user 角色）对应 `api` 的 `SessionItem`（message、tool-call、tool-result、reasoning）。
- **已知差异：** Norma 会并行执行并发安全的工具，并且可能在流结束前开始执行（`norma:harness/streaming.go:10-15`）；`api` 在完整响应之后串行执行（API 运行时 RFC 第 6.1 节）。平台工具标为非并发安全、独占执行；内置工具是否在流结束前开始执行，需要验证。取消时 Norma 不等未完成的工具，`api` 等它做完；这对平台工具没有影响，因为 Looper 一侧总会把已开始的调用跑完。
- **会话能否互换：** v0 不能，换运行时照旧是「交接 + 新会话 + 续接简报」。以后可以让 Looper 拥有两者共同的中性历史：运行器把每次请求的条目批次报给 Looper，`session/load` 时由 Looper 交回。这需要 Norma 能把任意条目装进 `Session`（现在只能从 JSONL `Resume`；可以驱动底层的 `harness.Query` 并实现 `harness.Recorder`，`norma:harness/query.go:156-165`）。列为后续（Q1）。

### 11.7 角色、许可、发布与缺口

- **角色：** 四个角色。协议取自 Profile 选中的模型：`anthropic-messages`、`openai-chat`、`openai-responses` 三种 Norma 都支持（`norma:llm/provider.go:42-48`）；只接 API key。
- **许可：** 按维护者的决定保留 MIT 来源和版权行；运行器的二进制随附 `THIRD_PARTY_NOTICES`，与 `looper/agents/runtimes/api/THIRD_PARTY_NOTICES.txt` 一致；Looper 保持 Apache-2.0。
- **发布：** 构建需要 Go 1.26.3（`norma:go.mod:3`），离线实测里 `GOROOT` 指向旧版本时编译失败，换成 1.26.3 才通过（第 2.3 节）；这与 Go 移植版此前「保持 Go 1.24」的决定冲突（Q14）。发布方式见 Q6。
- **已知缺口（需要验证）：** 离线实测已证明同进程并发、每会话 cwd、自定义工具给出的干净环境、`Compactor.Pre` 过渡引导、按会话取消（第 2.3 节）；仍需验证：持久化与 `Resume`、真实 provider 的取消、长上下文与长时间运行的内存；panic 是否从 Norma 内部 goroutine 逸出；子 agent 是否用父会话的 provider；Read、Glob、Grep 不响应 ctx（`norma:tool/files.go:37`、`norma:tool/search.go:38`），取消时是否拖长；`Retry-After` 不被遵守对 429 的影响；内置工具是否在流结束前开始执行。

## 12. 安全与隔离

**v0 的边界在工具层，不是操作系统沙箱**（同 API 运行时 RFC 第 5 节）。同一用户下的工具进程仍能读运行器私有目录里别的会话的文件；需要更强的隔离时用每 agent 一个进程（第 5.2 节）或以后的容器。

| 边界 | Looper 决定（策略） | 运行器执行（机制） |
|---|---|---|
| 文件 | 任务 cwd 与显式 `add_dirs` | 文件工具拒绝范围外的绝对路径和 `..`；子进程的 cwd 显式设为绝对路径 |
| 环境 | 白名单之后的完整环境 | 每次起子进程都用这份环境，不叠加运行器的进程环境；MCP、扩展、后台 shell、SDK 子 agent 都适用 |
| HOME | 每会话私有的 HOME | 放进上面的环境；SDK 自己的配置目录（Pi 的 agentDir、DSH 的 `DSH_HOME`）另设，也不指向操作者目录 |
| 工具 | 每个角色允许的工具、只读与否 | SDK 的工具清单加拦截或 guard |
| 网络 | 代理配置 | 启动时设一次 |

- **密钥：** provider key 只在 `session/new` 的参数里，运行器只放内存，不进运行器的环境、argv、日志和 SDK 写出的文件（Pi 用内存 SettingsManager，不写 `models.json`）；两边都把它注册进脱敏。运行令牌和回合绑定不出 Looper。
- **运行器的环境：** 只给白名单，没有 provider key，没有 Looper 令牌，没有其他运行时的登录目录变量。
- **没有端口：** 运行器不监听任何端口，只用 stdin、stdout、stderr。
- **启动环境冻结：** 进程级设置（HOME、`DSH_HOME`、代理、SDK 开关）在起进程之前决定；运行中从不 chdir，从不按会话改 `process.env` 或 `os.Setenv`。
- **目录：** 运行器私有目录在 runtime root 的 `.local/runners/<kind>/` 下，目录 0700、文件 0600；每个会话的 `state_dir`、HOME、工具输出目录各自独立。

## 13. 可观测性

**指标**（v0 写进 `/api/v1/system/status` 的运行器一节和结构化日志，不新建监控平台，同 API 运行时 RFC 第 10 节）：

| 指标 | 口径 |
|---|---|
| `runner_processes`、`runner_sessions{kind}`、`runner_session_limit{kind,reason}` | 当前值；生效的上限及决定它的那一项（内存、健康、硬上限） |
| `admission_rejections_total{scope,reason}` | 会话、模型请求、工具进程被拒或排队的次数，按第 5.3 节的六项分 |
| `runner_rss_bytes`、`rss_per_session_bytes` | 进程 RSS；每会话按 RSS ÷ 活动会话数，压测里另做堆快照 |
| `runner_event_loop_lag_ms`（Node）、`runner_sched_latency_ms`、`runner_goroutines`（Go） | p50、p99、最大值 |
| `runner_restarts_total{reason}` | 崩溃、挂死、协议违约、stdout 污染 |
| `tool_slots_in_use`、`load_average_1m` | 整机工具名额 |
| `model_inflight{provider}` | 每个 provider 账号的在途模型请求 |
| `authorize_latency_ms`、`authorize_denied_total{reason}` | 往返 p50、p99；本机 stdio 往返预计远小于模型延迟（推测，以此为准） |
| `steer_apply_latency_ms` | 从 `session/steer` 接受到 `steer_applied`；另计 `steer_dropped_total{reason}` |
| `bus_inflight{runner}`、`bus_ack_latency_ms`、`bus_redeliveries_total` | 在途数；deliver 到 ack 的 p50、p99 |
| `tools_call_latency_ms{tool}`、`tools_call_errors_total` | Looper 一侧从收到到回答 |
| `model_requests_total{kind}`、`usage_unattributed_total` | 按请求种类；归不到会话的请求 |

**告警：**

| 级别 | 条件 |
|---|---|
| P1 | 崩溃循环（10 分钟内重启 5 次）；`usage_unattributed_total` 增加（预算有漏洞） |
| P2 | `authorize_latency_ms` p99 连续 5 分钟超过 50 ms；事件循环延迟 p99 连续 5 分钟超过准入阈值；会话启动排队超过 60 秒；在途窗口满超过 60 秒；`steer_dropped_total{reason=turn_ended}` 增加；协议违约 |

v0 的告警进服务日志（固定事件名）和 system status 的标志；推送到渠道列入非核心待办。

**日志：** 每条都带 `trace_id`。每次模型请求一条 INFO（会话、trace、种类、模型、耗时、四类 token、`authorize` 结果与等待时间、上游请求 id），每次工具调用一条，每次引导一条（接受、生效位置、丢弃原因）；都不写内容和 key。stderr 按第 6.1 节截断、限速、脱敏。

**看板：** Settings → Agent Runtimes 的运行时卡显示运行器进程数、会话数、生效的上限、最近重启；Runs 页按运行时筛选，一轮的详情按 `trace_id` 列出其中的引导。图表放进非核心待办。

**出了问题怎么发现、怎么定位：** 用户在聊天或任务页看到第 6.9 节的错误；按 `trace_id` 在日志里找到那一轮的每次请求、工具调用和引导；`runner_restarts_total` 和 stderr 定位运行器问题；`admission_rejections_total` 说明为什么排队。

## 14. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 运行器在一轮中间崩溃 | 同进程所有会话的在途轮次丢失 | 硬会话上限限制影响面；隔离敏感的放进单独进程；`lost` 走现有崩溃路径；会话从 SDK 持久化恢复；信封重投 |
| 上游版本漂移 | 运行器调用的接口变了 | 版本门禁与握手自检；DSH 只认精确版本；升级先跑安装包上的映射测试和一致性测试 |
| stdout 被污染 | 协议解析失败 | 运行器把 `console.log` 和 SDK 日志重定向到 stderr；坏行即重启并计数；一致性测试故意打印一行验证 |
| 至少一次带来的重复 | 同一条消息进入模型两次 | 按 `message_id` 去重，集合从 SDK 会话记录重建；`bus/publish` 用运行器生成的 id 去重 |
| 引导在竞争中丢失 | 用户的纠正没生效 | 第 6.3 节的竞争规则：要么同一 trace 内生效，要么开新 trace，要么 `steer_dropped` 交回；一致性测试覆盖 |
| 背压卡住 | 慢会话拖住其他会话，或消息堆积 | 每会话有界队列；delta 可合并；窗口满告警；ack 超时只告警不重投 |
| 预算有漏洞 | SDK 内部请求绕过包装层 | `authorize_covers` 显式声明；归不到会话的请求拒绝并告警；对账后取消；缺口列在第 9.6、10.5、11.7 节 |
| 同进程不是安全隔离 | 一个会话的工具能读另一个会话的文件 | 工具层边界；按需每 agent 一个进程；发布说明写明 |
| Node 单事件循环 | 同步 I/O 或大输出拖慢全部会话 | 事件循环延迟参与准入；每会话输出上限；上限由压测定 |
| 准入的暂定值不合适 | 过早排队或机器过载 | 压测替换；指标给出拒绝原因；值可配置 |
| DSH 是 RC | 正式版前接口改变 | 精确版本、兼容门禁；升级必须重跑测试 |
| Norma 依赖上游改动和 Go 发布 | S3 被阻塞 | 上游改动先行、各自带测试；Q6 的发布方式；四个平台安装烟测 |

## 15. 测试计划

按计划第 1 节：测在离入口最近的一层；不测 mock 的调用链；一条规则一个测试；依赖本机 SDK 的测试标 `installed`、放进 nightly。

1. **一致性测试（所有运行器必须通过）：** 一套与语言无关的 Python 测试，经协议驱动任意运行器，配一个本地假模型服务（脚本化的文本、工具调用、用量、延迟）和脚本化的工具场景。覆盖：握手和版本不符；`session/new`、`load`、`close`；一轮到 `settled`；每个 `session/update` 种类；`tools/call` 往返；每个获准请求恰好一条 `request_end`；`authorize` 拒绝即 `capped`、请求未发出；`exec/acquire` 排队；读流时、工具中取消；`bus/deliver`、`ack`、重连后重投、去重；stdout 污染即重启；错误码。S0 用假运行器跑通它，之后每个运行器的阶段都以它全绿为门槛。
2. **Looper 一侧（假运行器）：** 从 RunnerSession、RunnerChannel 和真实的 ops 往下测：管家的两步确认和留痕、规划者的范围、运行令牌；100 KB 截断；`authorize` 的各级上限与 provider 并发排队；对账后取消；准入的每一项（内存、健康、硬上限、工具名额、负载保护）及排队超时；`trace_id` 三列写入 `llm_records`；一轮中途崩溃得到 `lost`、下一轮 `session/load`。PostgreSQL 部分用一次性数据库。
3. **trace 内引导（一致性测试的一部分，Norma 在 Go 里另有一份）：** 工具批次进行中送达的引导，在同一 trace 的下一次模型请求之前进入；与结束竞争的引导，要么在那个 trace 里生效，要么报告开出新一轮，绝不丢失；取消把排队的引导以 `steer_dropped` 丢弃。
4. **不调模型的同进程多会话验证（压测之前做）：** 用假 provider 和 barrier 证明两个会话的轮次同时在途；两个 cwd 里有同名文件，各读各的；不同的 HOME 和环境；不同的工具策略和模型路由；交叉取消；resume 只有一个 owner；背压。另外：Pi 验证扩展 reload 和 compat 注册互不影响；DSH 验证作用域内的事件和工具、原始 sandbox root 与当前 cwd 分离；Norma 验证关掉后台任务后不删别的会话目录、子 agent 的环境、MCP 的 cwd。每一项先做正向对照，证明测试方式本身能看到串扰。2026-10-26 的离线实测已在三家 SDK 上做过其中的并发、cwd、环境、引导和取消（第 2.3 节）；但 spike 的 harness 不是运行器，每个阶段仍要在真实的运行器上把这些项作为 `installed` 测试重跑，并补上 spike 没覆盖的持久化续接。
5. **压测：** N = 50、100、200 个并发 agent 对一个假模型服务（固定延迟和 token 流），三种运行器同样负载，在参考机器上跑。测每会话 RSS、goroutine 数或事件循环延迟、调度延迟、CPU、`authorize` 和 `tools/call` 往返的 p50/p99、每轮额外开销（从 `session/prompt` 到第一次模型请求，从最后一个响应到 `settled`）、引导生效延迟。第 5.3 节的默认值按结果定；第 11.1 节的并发收益以这里的数据为准。
6. **真实路径：** 本 worktree 的 `looper serve`、全新数据库、8767 以外的端口、一次性的 `--runtime-root`；确认 `sys.executable` 和 `looper.__file__`；外部渠道关闭，用替身渠道。先用假 provider 跑四个角色，再用真实 provider 各跑一次烟测；控制台、Telegram、飞书（替身）、CLI 各入口核对一遍。
7. **评测：** 只在模型可见内容变化时跑（计划第 1 节第 5 条）：平台工具名去掉 `mcp__looper__` 前缀（Pi）、各运行器的运行时说明、Norma 用 Looper 原创的摘要提示词。按 [evals/README.md](../../../evals/README.md) 附真实模式的 prompt 评测对照；新运行时担任一个角色，另按多运行时 RFC 第 11 节跑全部对话。
8. PR 的 Validation 写明新增的测试行数和生产代码行数。

## 16. 阶段

每张卡一个 PR，可以单独评审、单独回滚。大小：S 约 1 天，M 约 2–3 天，L 约 4–5 天（粗估）。

**每个运行器阶段的完成标准：** 一致性测试全绿；隔离的 `looper serve` 上的真实路径验证；第 15.4 节的不调模型验证全部有结论；第 15.5 节在这个运行器上的容量数字；控制台、Telegram、飞书、CLI 各入口核对；第 15.7 节需要的评测。

| 阶段 | 卡片 | 完成标准（另加上面的通用标准） |
|---|---|---|
| **S0 协议与 Looper 一侧** | ① 协议说明、JSON Schema 与一致性测试（M）；② RunnerSupervisor：起停、放置、健康、重启（M）；③ 准入控制与整机名额：内存、健康、硬上限、`exec/acquire`、负载保护（M）；④ RunnerBridge 会话控制、RunnerSession、RunnerChannel、内部事件、trace（L）；⑤ `tools/call` 分派与共用的 100 KB 截断（S）；⑥ `budget/authorize`、provider 并发、对账（M）；⑦ 总线 `lease`/`ack`、两种新消息类型、投递与去重（M）；⑧ `llm_records` 的三列与日志字段（S）；⑨ 假运行器（M） | 第 15.1–15.3 节在假运行器上全绿；隔离服务上，假运行器（只在测试里登记的运行时类型）跑完一轮管家对话并调用平台工具；没有用户可见变化 |
| **S1 Pi 运行器**（本分支：①–⑦ 已实现；⑧ 的验证与评测已准备、待 lead 实跑，容量数字和运行时卡未完成；⑨ 未完成，见 §9.6） | ① 运行器骨架：握手、版本门禁、会话表、读写、健康（M）；② `session/new`、`load` 与隔离：cwd、路径拦截、bash 的 `spawnHook`/`operations`、私有 agentDir、内存设置（L）；③ 包装 ModelRuntime：`authorize`、`request_end`、事件映射（M）；④ 平台工具代理、内联扩展、`skill_dir`（M）；⑤ 管家、规划者改由运行器承载，`pi` 类型切换（M）；⑥ 执行者、评审者：RunnerChannel、trace 内引导、取消、咨询分叉（L）；⑦ `WorkerPool` 的运行句柄抽象（M）；⑧ 验证、容量数字、评测、运行时卡（M）；⑨ 删除按轮起 CLI 的 Pi 路径（S） | 已安装的 pi 1.3.0 配假 provider 跑通四个角色、只读、用量、trace 内引导和取消；真实 Responses 烟测；与原 CLI 路径的评测对照；运行时卡截图一张 |
| **S2 DSH 运行器** | ① 运行器骨架、一次 `runProfile`、兼容门禁（M）；② `agents.create`、`resume` 与 `setup`：工具、prompt section、guard、bash 执行器（L）；③ `llm/stream` 授权、用量、请求归属（M）；④ 常驻与运行两类角色、引导、取消、快照咨询（M）；⑤ 按 provider 绑定分运行器（S）；⑥ 验证、容量数字、DeepSeek 真实烟测（M）；⑦ 删除 CLI 的 DSH 路径（S） | 第 10.5 节的缺口逐项给出结论或保留为开放项；DeepSeek 真实烟测 |
| **S3 Norma 运行器** | ① 上游：每会话收件箱与 trace 内引导，带测试（M）；② 上游：干净环境的 bash、子 agent 环境、`http.Client`、panic recover、任务目录清理、MCP cwd（M）；③ `api` 的 Python `AgentLoop` 移植同一收件箱（M）；④ Go 运行器：协议、会话 goroutine、读写、准入、健康（L）；⑤ 工具：路径包装、干净环境执行器、平台工具代理、Looper 摘要器，以及上游合入前的 `Compactor.Pre` 过渡引导（M）；⑥ provider 包装：`authorize` 与用量（S）；⑦ `norma` 运行时类型、Profile、registry、运行时卡（M）；⑧ 构建与发布二进制、`install.sh`（M）；⑨ 验证与三种运行器的容量对比（M） | 第 11.6 节的行为兼容表在同一个假服务上逐项核对；四个平台的安装烟测；三种运行器的容量对比写进本 RFC，替换第 5.3 节的暂定值 |
| 之后 | WebSocket 远程运行器；外部 broker；Looper 拥有的中性历史（Norma 与 `api` 互换会话）；能力目录里的 MCP、扩展与 Pi package；按项目划分信任域；Go 版 Looper（loopgo）驱动同一套运行器 | 各自另立 RFC 或在本 RFC 补节 |

**顺序（维护者已定 Pi → DSH → Norma）的理由：** S0 先做，三个运行器共用它，用假运行器就能把协议、预算、准入和总线的负例测完。Pi 是正式版，控制最全，每会话选项都是显式的，现有的 `pi` CLI 运行时和评测脚本可以直接对照。DSH 的进程内接口原生支持多 agent 和引导，但它是 RC，provider 路由是进程级的。Norma 最后：它要先在上游补收件箱等改动，还要解决 Go 的构建与发布；三种运行器的容量对比放在这一阶段一并完成。

## 17. 默认行为的变化与待决问题

### 17.1 默认行为的变化

- `pi`、`dsh` 两个运行时类型在 S1、S2 完成时改由运行器承载，选了它们的 Profile 不用迁移，但行为会变：
  - 管家的停止真正中断这一轮（`can_interrupt = True`），控制台、Telegram、飞书的 `/stop` 都适用；
  - 花费上限在请求之前挡住，可能比现在更早结束一轮（估计按上界算）；
  - 平台工具名从 `mcp__looper__*` 变成 `looper_*`（Pi，模型可见）；
  - 进程列表里看到的是每种运行器的少数几个常驻进程，而不是每轮一个；
  - DSH 的执行者、评审者第一次能在运行中收到规划者的消息；执行者的引导在同一 trace 内生效，竞争中丢失的引导会回到决策轮。
- `norma` 是新的运行时类型，没有 Profile 选它之前没有变化。计划 M9d 写的「不新增 `norma` 运行时类型」指的是 `api` 里的 Python 移植；批准后要同步改这一句。
- 总线多了托管 agent 的地址、`AGENT_MESSAGE`、`AGENT_NOTICE` 和 `lease`/`ack`；现有消息类型的路由不变。`llm_records` 多三列。
- LLM Providers 多一个可选的 `max_concurrent_requests`，不填与现在一样；Settings → Agent Runtimes 多每种运行时的放置设置。
- 入口：控制台改运行时卡和 Profile 表单（运行时列表来自 registry，不加页面）；Telegram、飞书沿用 `/profile`、`/stop`，协议不改；CLI 和 `loopercli` 沿 registry，没有新参数。

### 17.2 待决问题（附推荐默认值）

| # | 问题 | 推荐默认值 |
|---|---|---|
| Q1 | 保留 SDK 原生的会话树，还是转成 Looper 的中性历史？ | 原生（D14）。Pi、DSH 一直用原生格式；Norma 在后续阶段改由 Looper 拥有中性历史，以便与 `api` 互换 |
| Q2 | 每运行器的默认会话上限 | 暂定 Pi 10、DSH 20、Norma 50（推测，需要压测验证）；离线实测的短时数字都在这些值之内（第 2.3 节），但不据此提高；每个运行器阶段的容量数字和 S3 的三者对比之后用实测值替换 |
| Q3 | S0 之后第一批真实角色 | S1 先上 Pi 的管家和规划者，再上执行者和评审者 |
| Q4 | 运行时类型 | `pi`、`dsh` 沿用并切到运行器；新增 `norma` |
| Q5 | 托管 agent 的地址 | `agent_type` 同 tier，`agent_id` 为 tier 的 id 加 `.agent` |
| Q6 | Norma 运行器的代码放哪里、怎么发布 | 放进 loopgo 的 module（如 `go/cmd/looper-norma-runner`），因为 `internal` 包只能在那里导入；发布时 CI 构建 darwin-arm64、darwin-amd64、linux-amd64、linux-arm64 四个二进制，附在 release 上，`install.sh` 校验 SHA-256 后下载；Looper 像其他可执行文件一样探测它 |
| Q7 | Pi、DSH 运行器直接跑 TS，还是发布时编译 | 只用可擦除语法的 TS，由 Node ≥ 22.19 直接运行（默认开启类型擦除需要验证）；不行就在发布时编译成 ESM 随 wheel 分发；运行时不装 npm 包 |
| Q8 | `authorize` 的估计规则 | 估计输入 × 未缓存输入价 + min(max_output, 8,192) × 输出价；任一级上限放不下就拒绝 |
| Q9 | 平台工具名 | 原名 `looper_*`，与 `api` 一致；跑评测确认 |
| Q10 | 删除 CLI 路径时要不要留一个版本的退路 | 不留；回退靠上一个发布版本，发布说明写明 |
| Q11 | 准入的阈值 | 事件循环延迟 p99 100 ms、Go 调度延迟 p99 20 ms、工具名额核数 ÷ 2、负载阈值等于核数、会话启动排队 120 秒（都是推测，压测后替换） |
| Q12 | 在途窗口与 ack 超时 | 每运行器 64 个信封或 1 MiB；ack 超过 30 秒只告警，重连后才重投 |
| Q13 | 文件名 | 已按新术语命名为 `agent-sdk-runner-v0` |
| Q14 | Norma 要求 Go 1.26.3，Go 移植版此前决定保持 Go 1.24 | 先试把副本的 `go` 指令降到 1.24 并跑全部测试（Norma 用了 `iter`，1.23 起就有；是否用到 1.25 以后的特性需要验证）；能降就降，不能降再由维护者决定是否升级 Go 移植版的工具链 |
