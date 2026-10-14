# RFC：多运行时 agent 架构（v0）

- **状态：** 设计已定（[长程 Agent 上下文 RFC](long-horizon-agent-context-v0.zh-CN.md) 决策 10），M9a 已交付
  （looper #62），M9b 已交付（第 7、8 节写明核实结果），M9c 已交付（looper #67，第 6 节写明核实结果），M9e 已交付
  （looper #70，第 6 节写明核实结果），Codex 登录已交付（P1）；与本文的出入记在
  开发计划各阶段的"核实后确定的做法"；本文是 M9 的设计依据和待决问题清单。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 的 M9 一节。
- **最近更新：** 2026-10-14

## 1. 背景与目标

现在每个 agent 都是"Claude Code CLI + Claude 订阅 OAuth"。启动参数、隔离、事件解析、常驻会话和花费统计都写在
`looper/agents/claude_cli.py` 和各个 tier 里，换模型只能换 Claude 系列。

**目标：**

- 同一套任务流程（Manager、Planner、Worker、Reviewer）可以跑在不同的运行时和模型上；
- 用哪个 agent 按层级配置，任务可以按角色覆盖；
- 新增一种运行时只需要写一个适配器；
- 不做任何配置时，行为与现在逐字节一致。

**不做：**

- 不把一个运行时的 OAuth token 取出来给另一个运行时用；
- 本阶段不做模型路由、自动降级和按成本调度，这些列为后续方向（第 13 节）。

## 2. 概念

| 概念 | 定义 | 例子 |
|---|---|---|
| **运行时类型** | 代码里的一个适配器，负责把 Looper 的一次运行变成某个 agent 程序的一次执行 | `claude-code`、`codex`、`omp`、`claude-agent-sdk`、`openai-agents-sdk`、`norma` |
| **运行时实例** | 用户注册的某个类型的具体安装：二进制或 SDK 位置、探测到的版本和能力、默认参数 | 本机的 `claude` 2.1.285、`codex` 0.162.0 |
| **协议** | provider 对外提供的接口形态 | `anthropic-messages`、`openai-responses`、`openai-chat` |
| **LLM provider** | 凭据、接口地址、支持的协议和模型目录 | Claude 订阅、Codex 登录、Anthropic key、OpenAI key、GLM、Kimi、本机 CPA |
| **agent** | 运行时实例 + provider + 模型，加上默认 effort 和运行时选项 | "Claude Code + 订阅 + opus"、"Codex + CPA + gpt-6.1-sol" |

两条硬约束：

1. **协议匹配**：运行时要求的协议与 provider 支持的协议必须有交集。Claude Code 和 Claude Agent SDK 要
   `anthropic-messages`，Codex 要 `openai-responses`，norma 两类都可以。
2. **OAuth 绑定**：OAuth 类 provider 只能配给发放它的运行时类型。Claude 订阅只配 Claude Code，以及基于它的
   Claude Agent SDK；Codex 登录只配 Codex。

## 3. 数据模型

| 表或字段 | 主要内容 |
|---|---|
| `agent_runtimes` | 名称、类型、位置（二进制路径或模块）、探测到的版本、能力（探测结果加类型声明）、默认选项、启用 |
| `llm_providers` | 名称、协议列表、认证方式（`oauth`，以及它绑定的运行时类型；或 `api_key`）、`base_url`、密钥（单独存放，接口打码，同 M8b）、模型目录（每个模型的上下文窗口、输入/输出/缓存读写价格、支持的 effort 档位）、并发上限、启用 |
| `agent_profiles` | 名称、运行时实例、provider、模型、默认 effort、运行时选项、启用 |
| `agent_setting_layers.profile_id` | 取代原来的 `model`。其他分层字段（harness、effort、环境变量、单次上限）不变 |
| `task_profile_overrides` | 任务按角色覆盖用哪个 Agent Profile（`task_id`、`role`、`profile_id`，接口字段 `profile_overrides`，形如 `{planner: …, worker: …, reviewer: …}`）；这是唯一的任务级配置 |
| `agent_sessions` | 记录运行时类型、provider 和跑它的 Agent Profile（`profile_id`）；运行时类型和 provider 任一变化都不能续接（第 13 节 P5） |
| 运行记录（`llm_records`） | 增加 Agent Profile（`metadata.profile`）、运行时、provider、模型，以及花费来源（`runtime` 或 `provider_price`） |

注：本文起草时把 Agent Profile 叫作 agent（表 `agents`、`task_agent_overrides`，字段 `agent`、`agent_overrides`）；表名、
接口和代码后来改用控制台的叫法，"agent" 只指运行中的实例，上表与下文的表名、字段名已按新名字写。

内置并自动注册：运行时 `claude-code`、provider `claude-subscription`，以及与现有分层配置里各个模型对应的 agent。
迁移后各角色解析出的模型保持不变。原来的 `llm_profiles` 表和角色配置（`role_configs`）的 `runtime`、`llm_profile_id` 两列从未接入
启动路径，由新模型取代后删除。M8e 的导出文件同步加入这三张表，provider 的密钥照例留占位符。

## 4. 选用哪个 agent

一次运行由（角色、项目、任务）决定，用第一个有值的：

1. 任务对这个角色的覆盖；
2. 项目里这个角色的层；
3. 项目层；
4. 角色层；
5. 系统层；
6. 内置 agent（Claude Code + 订阅 + 默认模型）。

选定之后在两个时机校验协议、OAuth 绑定和角色所需的能力：配置保存时，以及运行启动时。启动时不通过，任务直接
失败，并写明原因，不会悄悄换成别的 agent。其他分层字段（harness、effort、环境变量、单次上限）照决策 9 解析，
与选中哪个 agent 无关。

## 5. 运行时适配器接口

每个运行时类型实现同一个接口，由现有的 Claude Code 路径原样抽出，作为第一个实现：

| 方法 | 作用 |
|---|---|
| `probe()` | 查版本和能力，只运行 `--version` 一类命令，不调用模型 |
| `capabilities` | 声明一次性运行、常驻会话与续接、结构化输出、skill/插件格式、MCP、额外目录、压缩事件、自主轮、花费报告 |
| `bind_provider(provider, model, effort)` | 把 provider 变成这个运行时认的环境变量、配置文件或参数（第 7 节） |
| `materialize(catalog, knowledge)` | 按运行时的格式写出只读副本：插件或 skill 目录、MCP 配置、知识挂载 |
| `build_launch(spec)` | 生成 argv、环境变量、文件（system prompt 文件等）和工作目录，包含隔离配方和"不限权限"的参数 |
| `parse(stream)` | 把运行时的事件流解析成内部事件 |
| `resident` | 常驻会话的启动、发送、续接和停止（Manager、Planner 用） |
| `cost(usage, provider)` | 计算本次花费，并给出花费来源（第 8 节） |

**内部事件**：`init`（会话 id、可用工具、skill、MCP）、`text`、`tool_use`、`tool_result`、`usage`、`compaction`、
`autonomous_turn`、`result`（成功或失败、最终文字）、`error`。

dashboard、M2 的观测、M6 的评测和 M7 的任务日志都只认内部事件。`worker.py` 里现在按 Claude 工具名（`Read`、`Edit`）
生成活动摘要的代码，要挪进 Claude Code 适配器。

## 6. 能力矩阵

✅ 已核实，❓ 待核实，❌ 不支持，— 不适用。CLI 的核实依据是本机版本的 `--help`，2026-10-14；Codex 一列另经本机 CPA
（`gpt-5.5`）实跑核实（M9c），登录相关的几项另经 0.162.0 源码（tag `rust-v0.162.0`）和本机 ChatGPT 登录实跑核实；OMP 一列另经本机 CPA（`gpt-5.5`）实跑、请求记录代理和上游 v18.4.3 的源码与文档核实（M9e）。

| 能力 | Claude Code 2.1.285 | Codex 0.162.0 | OMP 18.4.3 | Claude Agent SDK | OpenAI Agents SDK | norma |
|---|---|---|---|---|---|---|
| 一次性运行 | ✅ `--print` stream-json | ✅ `exec --json`，提示用 `-` 从 stdin 读完为止，一个进程一轮 | ✅ `-p --mode json`，提示从 stdin 读（非终端的 stdin 自动作为首条消息），一个进程一轮；首行是会话头（`type: "session"`、`id`），之后是 `agent_start`、`turn_start`、`message_start`/`message_update`/`message_end`、`tool_execution_*`、`turn_end`、`agent_end` | ❓ | ❓ | 待定 |
| 常驻会话与续接 | ✅ stream-json 输入 + `--resume` | ✅ 逐轮续接：每轮起一个 `exec … resume <id> -` 进程；`thread.started` 的 `thread_id` 就是会话 id，续接时不变；会话不存在时退出码 1、没有任何事件。`app-server` 是实验性的 JSON-RPC，未采用；`queue` 只投给 app-server 守护进程上的会话，到不了 `exec` | ✅ 逐轮续接：每轮起一个 `-p … --session-dir <目录> --resume <id>` 进程；会话头的 `id` 就是会话 id，续接时不变；会话不存在时退出码 1、没有任何事件。`--mode rpc` 是常驻进程的 JSON 命令协议，未采用 | ❓ | ❓ | 待定 |
| 运行中追加用户轮（引导 Worker） | ✅ stream-json 输入 | ❌ `exec` 只读一次提示；`queue` 也送不进正在跑的 `exec` | ❌ `-p` 只读一次 stdin；`--mode rpc` 能在运行中插话（未采用） | ❓ | ❓ | 待定 |
| system prompt 注入 | ✅ `--append-system-prompt-file`，快照关闭 | ✅ `CODEX_HOME/AGENTS.md`：作为用户消息进会话，排在项目 `AGENTS.md` 之前，不截断；续接时内容变了会补发"取代此前的 AGENTS.md"。`developer_instructions`（`-c` 或配置）只在会话开始时生效，续接时改了不生效；`model_instructions_file` 会替换 Codex 自带的基础指令 | ✅ `--append-system-prompt <文件>`：追加在 omp 自带 prompt 和项目上下文之后，是真正的 system prompt；每个进程重新构建，续接时内容变了就用新的 | ❓ | ❓ | 待定 |
| 强制结构化输出 | 靠 prompt 契约 | ✅ `--output-schema`，续接轮次也生效（Looper 未启用） | 靠 prompt 契约 | ❓ | ❓ | 待定 |
| skill 与插件 | ✅ Claude 插件，`--plugin-dir` | 原生读 `SKILL.md`（开发者消息里列名称、描述和路径），但列表总带着 `$HOME/.agents/skills`，只有改 `HOME` 才去得掉，所以关掉原生列表（`skills.include_instructions = false`），skill 按 P9 降为可读文档；Claude 插件不加载 | ✅ `--plugin-dir` 原生加载 Claude 插件的 `skills/<名称>/SKILL.md`（按 frontmatter 的 name 命名，没有插件前缀）、`commands/`、`agents/`；Claude 格式的 `hooks/hooks.json` 不加载；插件自带的 MCP 服务器会加载，Looper 用 `disabledExtensions` 关掉 | ❓ | — | 待定 |
| MCP | ✅ `--mcp-config` + `--strict-mcp-config`（M8b） | ✅ `config.toml` 的 `[mcp_servers.<名称>]`：stdio（`command`、`args`、`env`）和 streamable HTTP（`url`、`http_headers`）；没有 SSE | ✅ 与 Claude Code 相同的 `mcpServers` 格式（stdio、HTTP、SSE），Looper 放进家目录里生成的插件 `looper`，工具名 `mcp__looper_<服务器>_<工具>`；默认挂在 `xd://` 下经 read/write 调用，`tools.xdev: false` 改成顶层工具 | ❓ | ❓ | 待定 |
| 额外目录（知识） | ✅ `--add-dir` | ✅ `--add-dir`，放在 `resume` 之前也有效；目录里的 `AGENTS.md` 不加载 | ✅ `--add-dir`，但目录里和它往上的 `AGENTS.md` 等会作为上下文加载，Looper 不用（第 9 节） | ❓ | — | 待定 |
| 不限权限 | ✅ `--dangerously-skip-permissions` | ✅ `--dangerously-bypass-approvals-and-sandbox` | ✅ `tools.approvalMode: yolo`（默认值，Looper 在覆盖配置里写明） | ❓ | — | 待定 |
| 隔离 | ✅ `--setting-sources project` 等（M1） | ✅ 独立 `CODEX_HOME`，只放生成的 `config.toml` 和 `AGENTS.md`，不读 `~/.codex`；另需关掉原生 skill 列表（见上）和插件同步（`features.plugins = false`，否则每个新目录克隆约 25 MB 的插件市场）；用 Codex 登录时另有一个指向操作者登录文件的 `auth.json` 符号链接，并关掉 ChatGPT 应用（`features.apps = false`，否则每个目录缓存约 2.6 MB 的应用工具列表），见 P1 | ✅ `HOME` 指向 Looper 的家目录（见下），不读 `~/.omp`、`~/.claude`、`~/.codex`、`~/.agents`、`~/.env`；工作目录不在 git 仓库里时另关掉向上查找的发现来源 | ❓ | — | 待定 |
| 非 git 目录 | ✅ | ✅ 需要 `--skip-git-repo-check` | ✅ | — | — | 待定 |
| 花费报告 | ✅ `total_cost_usd` 和 `modelUsage[*].costUSD` 一律按 Claude 价格计算（不认识的模型标 `costBasis: "unknown"`）；`modelUsage` 按模型给出四种 token，接其他 provider 时由 Looper 按 provider 价格计算 | 只有 token：`turn.completed.usage`，是会话累计值，由 Looper 按 provider 价格计算 | 只有 token：每个请求一条（助手消息的 `usage`），由 Looper 按 provider 价格计算；子 agent 的用量在 `task` 工具结果里汇总 | ❓ | 只有 token 数 | 待定 |
| 压缩事件 | ✅ `compact_boundary` | ❌ `--json` 不报，只出一条提示性的 `error` 条目 | ✅ `auto_compaction_start`/`auto_compaction_end`（`result.tokensBefore`） | ❓ | — | 待定 |
| 自主轮（#26） | ✅ | — 一个进程只跑一轮 | — 一个进程只跑一轮 | ❓ | — | 待定 |
| agent teams（Agent Profile 的 `agent_teams`） | ✅ `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` 加 `--teammate-mode in-process`（默认的 `auto` 在 tmux 里会把队友开成单独的 tmux 进程，花费不进结果）：队友在同一进程里作为后台任务运行，它的消息和它的结束各引出一轮自主轮，Worker 关了 stdin 也一样；每个结果的累计值都含队友的 token。队友的模型请求不在事件流里（它的助手消息在），也拿不到 `--append-system-prompt-file` | — | — | ❓ | — | 待定 |

Codex 的其他核实结果：

- **事件**：`thread.started`（`thread_id`）、`turn.started`、`item.started`/`item.completed`（`agent_message`、`reasoning`、
  `command_execution`、`file_change`、`mcp_tool_call`，以及 `web_search`、`todo_list`、`error`）、`turn.completed`（`usage`）、
  `turn.failed`（`error.message`）和顶层 `error`（如重连提示）。一轮可以有多条 `agent_message`，最后一条是回复。
- **用量**：`input_tokens` 含 `cached_input_tokens`，`output_tokens` 含 `reasoning_output_tokens`，另有
  `cache_write_input_tokens`。数值是会话累计值，续接后接着累计；被杀掉的那一轮不计入，下一轮从上次落盘的累计值算起。
  没有单次请求的用量，所以记不到上下文规模。
- **结束**：被 SIGTERM 时退出码是 0、没有 `turn.completed`；它正在跑的 shell 命令不会随之结束。
- **环境变量**：shell 命令默认继承 Codex 进程的全部环境变量，provider 的密钥要用 `shell_environment_policy.exclude` 排除。
  `shell_environment_policy.set` 在继承、排除之后覆盖变量，Codex 只在其后再加 `CODEX_THREAD_ID` 等自己的变量，
  不会把 `CODEX_HOME` 改回去（0.162.0 源码 `protocol/src/shell_environment.rs`，并经实跑核实，见 P1）。
- **登录**（0.162.0 源码与实跑核实，2026-10-14）：
  - `cli_auth_credentials_store = "file"` 时登录在 `$CODEX_HOME/auth.json`，路径不能单独配置；刷新后的登录是截断
    原文件再原地写入（无临时文件和 rename、无 `O_NOFOLLOW`、无文件锁），所以顺着符号链接写回目标文件，链接和目标的
    inode 都不变；进程内串行，跨进程不加锁，刷新前重读磁盘，磁盘上已变就采用；只在 access token 离过期不到 5 分钟时
    主动刷新；
  - `keyring` 模式按 `CODEX_HOME` 的规范路径区分钥匙串条目，不能共享；`auto` 模式第一次保存就写进钥匙串并删掉
    `auth.json`，等于分叉；
  - `CODEX_API_KEY`、`CODEX_ACCESS_TOKEN` 在环境里时优先于登录文件；
  - `codex login status` 写到 stderr：`Logged in using ChatGPT` 退出码 0，`Not logged in` 退出码 1，API key 登录
    也是退出码 0（`Logged in using an API key - …`，key 打码），只读本地文件、不向服务端验证；悬空的链接读作
    `Not logged in`；
  - `codex logout` 会在服务端吊销 refresh token 再删掉 `auth.json`。

OMP 的其他核实结果：

- **家目录**：omp 的所有用户级路径都在 `HOME` 下：`~/.omp`（设置、`models.yml`、会话、日志、已装插件、原生库）、
  外部工具的用户目录（`~/.claude`、`~/.codex`、`~/.gemini`、`~/.agents`，其中 `~/.agents/skills` 默认加载），还有
  `~/.env`（启动时读进 omp 自己的环境）。`PI_CODING_AGENT_DIR` 只搬走 `~/.omp/agent`，`~/.omp` 下的日志和插件仍然
  读写，所以 Looper 改的是 `HOME`。另有几个变量会把这些路径指回操作者的目录（`OMP_PROFILE`、`PI_CODING_AGENT_DIR`、
  `PI_CONFIG_DIR`、`PI_CONFIG_FILES`、`XDG_*_HOME`、`CLAUDE_CONFIG_DIR`、`COPILOT_HOME`），启动时去掉。原生库每个家
  目录要解压一份（约 167 MB），各家目录用符号链接共用一份。
- **向上查找**：项目级的 skill、`AGENTS.md`/`CLAUDE.md` 和 `.omp` 目录从工作目录往上找，到 git 仓库根为止；不在
  仓库里时以 `HOME` 为界。`HOME` 换掉以后，仓库外的工作目录会一直找到 `/`，把操作者家目录里的 `.agents/skills`、
  `.claude/skills`、`.omp/skills` 和 `AGENTS.md` 当成项目内容加载（用替身家目录实测到）。所以工作目录不在仓库里时
  关掉 `native`、`claude`、`agents`、`agents-md`、`claude-md` 这几个发现来源，代价是工作目录自己的这几类文件也不读。
  `task` 工具的子 agent 定义（`.omp/agents`）无论在不在仓库都一直找到 `/`，关不掉，记为差距。
- **设置**：项目的 `.omp/config.yml` 会覆盖全局设置，所以 Looper 的设置放在 `--config` 覆盖文件里（优先级更高）：
  只启用 agent 的模型（`enabledModels`，各角色 `modelRoles` 都指向它），`retry.modelFallback`、
  `startup.checkUpdate`、`dev.autoqa` 关掉，`tools.xdev` 关掉（MCP 工具成为顶层工具）。
- **provider**：`models.yml` 的自定义 provider（`baseUrl`、`api`、`models`）；`apiKey` 写环境变量名，omp 先当变量名
  读，所以密钥只在环境里。`api` 实测 `openai-responses`、`openai-completions`（即 `openai-chat`）和
  `anthropic-messages` 三种都能经 CPA 跑通；模型的 `cost` 写 0，花费由 Looper 计算。effort 对应 `--thinking`。
- **shell**：agent 的 shell 命令继承 omp 的环境。Looper 用 `PI_SHELL_PREFIX`（omp 给每条 shell 命令加的前缀）把
  `HOME` 等变量恢复成操作者的，并去掉 provider 的密钥，所以 shell 里的 git、gh 等工具照常使用操作者的配置。
- **事件与用量**：助手消息的 `message_end` 带这次请求的 `usage`：`input` 不含缓存读，另有 `cacheRead`、
  `cacheWrite`、`output`，所以能记上下文规模，单次上限也能在请求之间判断。失败时助手消息的 `stopReason` 是
  `error`（带 `errorMessage`），退出码 1。被 SIGTERM 时正在跑的 shell 命令一并结束，没有 `agent_end`。
  skill 描述压缩成路由提示（用 smol 角色的模型，结果缓存在家目录）、压缩摘要等后台请求不在事件流里。

决策 10 要求所有运行时都支持所有角色。矩阵里 ❓ 的项，在对应阶段开工时第一件事就是实测，结果写回本表。

## 7. provider 绑定

- **Claude Code + 订阅**：保持现状。
- **Claude Code + API key 或兼容接口（GLM、Kimi、CPA 的 `/v1/messages` 等）**，M9b 的做法：
  - 有 `base_url`（第三方接口）：注入 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`，并从继承的环境里去掉
    `ANTHROPIC_API_KEY`；没有 `base_url` 或指向 `api.anthropic.com`（Anthropic 官方）：注入 `ANTHROPIC_API_KEY`，去掉
    `ANTHROPIC_AUTH_TOKEN`（以及没有 `base_url` 时的 `ANTHROPIC_BASE_URL`）；
  - 主模型用 `--model`；
  - 第三方接口同时映射后台模型：`ANTHROPIC_DEFAULT_OPUS_MODEL`、`ANTHROPIC_DEFAULT_SONNET_MODEL`、
    `CLAUDE_CODE_SUBAGENT_MODEL` 指向 agent 的模型，`ANTHROPIC_DEFAULT_HAIKU_MODEL`、`ANTHROPIC_SMALL_FAST_MODEL`
    指向 provider 模型目录里这个模型的 `background_model`（没填就是模型本身）；
  - provider 自带的环境变量（`env`，如 GLM 指南里的 `API_TIMEOUT_MS`、`CLAUDE_CODE_AUTO_COMPACT_WINDOW`）跟在后面，
    但不能覆盖上面这些由绑定决定的变量；
  - 密钥只在运行启动时从 `llm_provider_secrets` 读出，只进子进程环境，不进 argv、运行记录、日志和导出。
- **核实（Claude Code 2.1.285，`--print` stream-json，2026-10-14）**：在 Anthropic 格式接口前放一个只记录模型名和
  认证头种类的转发代理，操作者的订阅保持登录：
  - `ANTHROPIC_API_KEY` 以 `x-api-key` 发出，init 事件 `apiKeySource` 为 `"ANTHROPIC_API_KEY"`；
    `ANTHROPIC_AUTH_TOKEN` 以 `Authorization: Bearer` 发出，`apiKeySource` 为 `"none"`，和订阅登录时一样，所以
    `apiKeySource` 区分不了 token 和订阅；两者都只带 key，不带订阅的 token，Claude Code 在 stderr 提示 key
    "takes precedence over your claude.ai login"；两个变量同时设置时两个头都发；
  - `--model` 优先于 `ANTHROPIC_MODEL`，不传 `--model` 时用 `ANTHROPIC_MODEL`；
  - 不带模型启动的子 agent 用主模型，设置了 `CLAUDE_CODE_SUBAGENT_MODEL` 时用它；模型自己选别名（`haiku`、`sonnet`、
    `opus`）启动的子 agent 请求这个别名对应的模型（未映射时是 `claude-haiku-4-5-20251001`，CPA 返回 400），
    `CLAUDE_CODE_SUBAGENT_MODEL` 管不到别名，`ANTHROPIC_DEFAULT_*_MODEL` 能映射；
  - 模型名带 `[1m]`（如 `glm-5.3[1m]`）时，请求里的模型名去掉后缀、加上 `context-1m` beta，`modelUsage` 的键仍带
    `[1m]`；价格按去掉后缀和日期后缀的模型名查；
  - `apiKeyHelper` 未使用。
- **预设**：GLM（BigModel 国内、Z.ai 国际）、DeepSeek、Kimi（Moonshot 国内、国际）各带 Anthropic 格式和 OpenAI
  格式的接口地址、常用模型、建议的后台模型和厂商 Claude Code 指南里的额外变量，均取自厂商官方文档（2026-10-14，
  见 `provider_presets.py`）；本机 CPA 作为 OpenAI 类 provider（`openai-responses`、`openai-chat`，
  `http://127.0.0.1:8317/v1`），也提供 Anthropic 格式接口（`http://127.0.0.1:8317`）。一个 provider 只有一个
  `base_url`，所以从预设建 provider 时选其中一种接口。
- **Codex + 登录**（内置 provider `codex-login`，OAuth，只配 Codex 运行时）：每次运行的 `CODEX_HOME` 里 `auth.json`
  是指向操作者登录文件（服务端 `CODEX_HOME` 里的，否则 `~/.codex` 里的）绝对路径的符号链接，生成的 `config.toml`
  写 `cli_auth_credentials_store = "file"`，不写 `model_provider`（用 Codex 内置的 OpenAI provider），去掉环境里的
  `CODEX_API_KEY`、`CODEX_ACCESS_TOKEN`；花费按内置价目表折算为订阅等价花费，查不到价格的模型只记 token。详见第 13 节 P1。
- **Codex + API key（含 CPA）**：隔离目录里生成的 `config.toml` 写 `model_provider` 和 `[model_providers.<id>]`
  （`base_url`、`env_key`、`wire_api = "responses"`），密钥只放进 `env_key` 指名的环境变量（M9c 已核实）。
- **OMP + API key（含 CPA）**：家目录里生成的 `models.yml` 写 provider `looper`（`baseUrl`、`api`、只含 agent 模型的
  `models`，价格写 0），`apiKey` 写环境变量名 `LOOPER_PROVIDER_API_KEY`，密钥只在这个变量里；`api` 按 provider 的协议
  依次选 `openai-responses`、`openai-completions`（`openai-chat`）、`anthropic-messages`，没有 `base_url` 时用厂商官方
  地址。模型用 `--model looper/<模型>`，effort 用 `--thinking`；模型目录里 `efforts` 为空列表的模型按不推理处理
  （M9e 已核实，三种 `api` 都经 CPA 跑通）。
- **SDK**：直接传入 key 和 base_url。
- **开发测试**：用本机 CPA（`127.0.0.1:8317`）。它同时提供 `/v1/responses`、`/v1/chat/completions` 和 Anthropic 格式的
  `/v1/messages`。key 在运行时从 CPA 的配置文件读取，不进仓库、不进日志。

### 常用组合（2026-10-14）

| 组合 | 运行时 | provider | 协议 | 阶段 |
|---|---|---|---|---|
| Claude Code + 订阅 | claude-code | Claude 订阅（OAuth） | anthropic-messages | 已支持 |
| Claude Code + GLM | claude-code | GLM API key | anthropic-messages | 已支持（M9b） |
| Claude Code + DeepSeek | claude-code | DeepSeek API key | anthropic-messages | 已支持（M9b；真实 key 待维护者验证） |
| Claude Code + Kimi 等兼容接口 | claude-code | 对应 API key | anthropic-messages | 已支持（M9b；真实 key 待维护者验证） |
| Codex + CPA | codex | CPA（OpenAI 类 provider，预设 `cpa`） | openai-responses | 已支持（M9c） |
| Codex + Codex 登录 | codex | 内置 `codex-login`（ChatGPT OAuth） | openai-responses | 已支持（计划第 5.0 节第 3 项） |
| OMP + CPA | omp | CPA（预设 `cpa`） | openai-responses（也支持 `openai-chat`、`anthropic-messages`） | 已支持（M9e） |

GLM、DeepSeek 等同时提供 anthropic 和 openai 两种协议，一般配 Claude Code；M9b 内置它们的 provider 预设（两种协议的接口地址按官方文档核实），用户只填 key。

## 8. 花费与上限

- **统一口径**：每次运行记录花费来源。
  - `runtime`：只在 Claude Code 接 Anthropic 官方接口或订阅时可信。
  - `provider_price`：用 token 数乘以 provider 登记的价格（含缓存读写），适用于其他所有情况。Claude Code 接第三方
    接口时，`total_cost_usd` 仍按 Claude 的价格计算，不能用。
- **订阅类 provider**：花费是按 API 价格折算的"等价花费"，不是实际支出，界面上要区分显示。
- **单次上限**：`--max-budget-usd` 只在 `runtime` 口径可信时使用。其他情况由 Looper 从事件流累计用量，超过上限就
  停止这次运行。这是软上限，可能多出一个请求。
  - M9b 的做法：适配器把每个请求的 token（Claude Code 取 `message_start` 的模型和 `message_delta` 的最终用量）
    报成内部 `usage` 事件，运行记录器按 provider 价格累加，超过 `run_budget_usd` 就停止：Worker 杀掉进程；
    Manager、Planner 的这一轮结束并杀掉进程，会话累计值加上这一轮的花费，下一轮新起进程续接。上限按"一次运行"
    计算（Worker 一次运行，或常驻会话的一轮）；
  - 子 agent 的请求和 Claude Code 的后台请求不出现在事件流里，只在结果的 `modelUsage` 里，计入这次运行记下的花费，
    不参与中途停止；
  - 运行记下的花费取结果 `modelUsage` 里每个模型的四种 token 乘以价格；没有结果（被停止、超时）时取事件累计值；
    缺价格的模型只记 token。
- **任务上限**（M5）和"提高上限并继续"（#43）一律按统一口径计算。

## 9. 目录、知识与 prompt

- **目录条目**（M8）标注兼容的运行时类型：Claude 插件只挂给 Claude 系运行时，MCP 由适配器翻译。装给某个角色、
  但它当前运行时不兼容的条目会跳过，并记在运行记录里，不会让运行失败。
- **知识**：Claude Code 和 Codex 都有 `--add-dir`。OMP 也有，但它会把每个额外目录当成工作区，读它往上直到 `/` 的
  `AGENTS.md` 等上下文文件，所以 OMP 不挂目录，按 prompt 里知识索引给出的路径读（索引写明了每个文档所在的目录）。
- **prompt**：
  - harness 和角色 prompt 保持与运行时无关。核查过，现有 prompt 没有写死工具名；
  - 运行时之间的差异（工具名、怎么输出结构化结果）由适配器在代码契约层前追加一小段"运行时说明"；
  - 回复契约（Planner 的计划和决策、Worker 和 Reviewer 的报告）不变，Codex 可以考虑用 `--output-schema` 加强约束。

## 10. 隔离与凭据

- 每个运行时一份隔离配方，延续决策 4 和 7：只用 Looper 拼装的内容和项目仓库自带的配置，不读操作者的个人配置。
- **API key**：存在数据库里，静态未加密，接口打码。注入子进程环境后，不限权限的 agent 能读到，见 P3。
- **OAuth**：只在发放它的运行时内使用。Codex 的登录以符号链接共用操作者的登录文件，从不复制（P1）；OMP 没有
  自己的登录。

## 11. 评测与验收

- M6 评测可以指定 agent。每个新 agent 先跑一次真实模式评测，按 agent 维护基线，对比成功率、契约违约次数、
  Planner 是否改文件和花费。
- "支持某个角色"的验收标准：用这个 agent 担任该角色，M6 评测全部通过。

## 12. 分期

| 阶段 | 内容 | 依赖 |
|---|---|---|
| M9a | 注册与选择、校验、任务级覆盖；Claude Code 抽成第一个适配器，行为逐字节一致；数据模型按第 3 节（含会话的运行时绑定、provider 并发上限） | M8d |
| M9b | Claude Code 接 API key 和兼容接口：provider 注入、后台模型映射、按 provider 价格计算花费、自己执行的单次上限 | M9a |
| M9c | Codex 运行时：先做常驻会话的技术验证，再实现四类角色；隔离与登录（P1）、MCP 翻译、目录兼容 | M9a |
| M9d | SDK 运行时与 norma | M9a，norma 就绪 |
| M9e | OMP 运行时：先只读核实能力、填能力矩阵，再实现四类角色（P10） | M9c |

M9b 与 M9c 并行。

## 13. 待决问题与风险

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| **P1** | **Codex 登录在隔离下会互相刷掉 token。** Codex 的 refresh_token 会轮换（CPA 运维中实测过）。每次运行复制一份 `CODEX_HOME` 的话，一个实例刷新 token 后，其他实例手里的就失效了 | Codex + 登录不可用，或者时好时坏 | **已定并实现（2026-10-14，计划第 5.0 节第 3 项）：** 所有实例共用操作者的同一个登录文件，只隔离配置。外部加文件锁做不到（刷新发生在 Codex 进程内部，Looper 插不进去），改为：<br>1. 每个运行目录和常驻会话目录的 `auth.json` 是指向登录文件绝对路径的符号链接，从不复制、不用硬链接（硬链接会钉住旧 inode）；配置写死 `cli_auth_credentials_store = "file"`（`auto`、`keyring` 会分叉，第 6 节），去掉 `CODEX_API_KEY`、`CODEX_ACCESS_TOKEN`；Codex 原地写回刷新后的登录，经链接写进操作者的文件，各进程刷新前重读磁盘；<br>2. 绕过审批和沙箱的 agent 的 shell 继承 `CODEX_HOME`，在里面执行 `codex logout` 会在服务端吊销操作者的登录。`shell_environment_policy.set` 把 shell 里的 `CODEX_HOME` 指向一个没有登录的空目录（`<runtime_root>/.local/codex/shell-home`），实跑核实 agent 的 `echo "$CODEX_HOME"` 得到这个目录，`codex login status` 得到 `Not logged in`。agent 若故意改回 `~/.codex` 仍能碰到登录，这和不经 Looper 的任何本机进程一样，Looper 管不到；<br>3. 每次登录运行之后、`looper serve` 启动时，用 `lstat` 检查每个目录的 `auth.json` 仍是指向登录文件的链接；变成普通文件（分叉：一份 token 副本，操作者文件里的 refresh token 可能已失效）、指向别处或在登录目录里不见了，都删掉该条目、放回链接，并记下问题：之后用这个登录的运行一律不启动并写明原因，任务活动、provider 和 `codex_login` 检查里都有警告，直到操作者在本机核对 `codex login status` 后在界面清除。<br>失效方式：刷新若改成先写临时文件再 rename，会把链接换成普通文件，由第 3 条发现并止损（升级 Codex 后要重做一次假文件实验）；操作者自己 `codex logout` 后链接悬空，运行因找不到登录文件而不启动，失败即关闭；写入不是原子的，并发的进程可能读到半截文件，这是 Codex 自身就有的风险，没有新增 |
| **P2** | **其他运行时的常驻会话怎么实现还不知道。** 可能是 `exec resume`（每轮起一个进程、重新加载上下文）、`app-server`（实验性）或 `queue`。自主轮和压缩事件也可能没有对应物 | "所有运行时都支持所有角色"的工期和稳定性 | **已定（2026-10-14）：** 先用"每轮起进程 + 续接会话"（逐轮续接）；连续接都不支持的运行时，每轮开新会话，由 Looper 注入完整状态（计划、任务日志、最近对话的摘要）。M9c 第一步做技术验证，结果写回能力矩阵。**M9c 核实：** Codex 走逐轮续接（每轮一个 `exec resume` 进程），续接不了的会话照旧换新会话；没有压缩事件和自主轮，也不能在 Worker 运行中追加用户轮（第 6 节）。**M9e 核实：** OMP 同样走逐轮续接（每轮一个 `omp -p --session-dir … --resume <id>` 进程，与 Codex 共用 `per_turn.PerTurnSession`）；有压缩事件和逐请求的用量，没有自主轮，`-p` 下不能在 Worker 运行中追加用户轮（`--mode rpc` 可以，未采用） |
| **P3** | **provider 的密钥对 agent 可见。** 不限权限的 agent 能读自己的环境变量 | 密钥可能被模型写进输出或带出去 | **已定（2026-10-14）：** 近期接受，在本文写明。之后再做一个本地代理持有密钥，agent 只拿到本地地址（做法类似 CPA） |
| **P4** | **第三方模型遵守契约、使用工具的质量参差不齐**，可能需要按模型调整 prompt | Planner、Manager 不稳定 | **已定（2026-10-14）：** 先不做按模型的 prompt 变体，所有 agent 用同一套 prompt，运行时差异放在适配器追加的"运行时说明"里；靠 M6 评测把关，评测不过的 agent 不配给 Planner 和 Manager |
| P5 | **运行中换 agent。** 常驻会话绑定在运行时和 provider 上 | 层级或任务覆盖改了以后，Planner、Manager 可能要新开会话 | **已定（2026-10-14）：** 改配置立即生效：Worker、Reviewer 下一步就用新 agent。Planner、Manager **只有运行时类型和 provider 都不变时才续接**（例如同一订阅下换模型）；**换 provider（任意方向：订阅→API key、API key→订阅、两家 API key 之间）或换运行时，一律新开会话**，因为各 provider 协议里的字段（思考签名、tool_use id、reasoning item 等）互不兼容。新会话靠 M7 的计划状态、任务日志和 M7c 的交接摘要补回进度，任务页记一条"已更换 agent"。M9b 已实现：会话记下 `provider_id`，判断只在 `session_switch.agent_switch`（`runtimes.can_resume`）一处 |
| P6 | **花费口径不一致**（见第 8 节） | 观测和上限失真 | **已定（2026-10-14）：** 随版本内置常见模型的价格表，provider 的模型目录里可以覆盖；查不到价格的模型只显示 token 数，注册 agent 时提示花费上限对它无效。订阅类 provider 一律按 API 价格折算，界面标"等价"，花费上限同样生效（防止把订阅额度耗光）。M9b 已实现：价格表在 `runtimes/pricing.py`（Claude、GPT、GLM、DeepSeek、Kimi，官方美元标价，2026-10-14） |
| P7 | **effort 档位不对应。** Claude 是 low/medium/high/xhigh，OpenAI 是 minimal/low/medium/high | 分层配置里的 effort 含义不清 | 适配器里维护映射表；provider 的模型目录声明支持哪些档位，不支持的给出提示 |
| P8 | **并发与限流** | 某个 provider 被打满（比如 CPA 自身的并发限制） | **已定（2026-10-14）：** 默认不设上限，与现在一致；需要时在 provider 上设置，超出的排队等待，不会失败 |
| P9 | **目录跨运行时**：Claude 插件和 Codex 插件格式不同；`SKILL.md` 是否通用还要核实 | 装了的 skill 在某些运行时上加载不了 | **已定（2026-10-14）：** 能转换就转换（MCP 翻译成对应格式，运行时原生支持的 skill 按它的格式挂载）；转换不了的 skill 降为可读文档：像项目知识一样只读挂载，并在 prompt 里给出"可用 skill：名称、描述、路径"的索引，由模型按需读取；插件里的 hook、命令等无法转换的部分跳过，并记在运行记录里。目录界面显示每个条目兼容哪些运行时。**M9e：** OMP 原生加载 Claude 插件（`--plugin-dir`：skill、命令、agent），插件的 hook 和输出样式不加载、插件自带的 MCP 服务器用 `disabledExtensions` 关掉，都记在运行记录里；Looper 的 MCP 服务器放进家目录里生成的插件 |
| P10 | **OMP 的能力还不清楚** | — | **已定（2026-10-14）：** 在 M9c 之后单列 M9e：等 Codex 把适配器接口验证过，先只读核实 OMP 的能力、填好能力矩阵，再写适配器；开发用 CPA。**M9e 已核实**（第 6 节 OMP 一列）：隔离靠换 `HOME`，仓库外的工作目录另关掉向上查找的发现来源；`task` 子 agent 定义向上查找到 `/` 关不掉，记为差距 |
| P11 | **norma SDK 的接入形态** | — | 建议放在独立的子进程里，便于隔离，崩溃也不会拖垮服务；双协议意味着它能配任意一种 provider |
| P12 | **后续方向**（本阶段不做） | — | 按成本和质量路由模型、失败自动换 agent、同一步骤让多个 agent 交叉审查 |
