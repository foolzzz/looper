# RFC：多运行时 agent 架构（v0）

- **状态：** 设计已定（[长程 Agent 上下文 RFC](long-horizon-agent-context-v0.zh-CN.md) 决策 10），M9a 已交付
  （looper #62；与本文的出入记在开发计划 M9a 的"核实后确定的做法"）；本文是 M9 的设计依据和待决问题清单。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 的 M9 一节。
- **最近更新：** 2026-10-14

## 1. 背景与目标

现在每个 agent 都是"Claude Code CLI + Claude 订阅 OAuth"。启动参数、隔离、事件解析、常驻会话和花费统计都写在
`loopx/agents/claude_cli.py` 和各个 tier 里，换模型只能换 Claude 系列。

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
| **运行时类型** | 代码里的一个适配器，负责把 LoopX 的一次运行变成某个 agent 程序的一次执行 | `claude-code`、`codex`、`omp`、`claude-agent-sdk`、`openai-agents-sdk`、`norma` |
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
| `agents` | 名称、运行时实例、provider、模型、默认 effort、运行时选项、启用 |
| `agent_setting_layers.agent` | 取代原来的 `model`。其他分层字段（harness、effort、环境变量、单次上限）不变 |
| `tasks.agent_overrides` | 任务按角色覆盖用哪个 agent，形如 `{planner: …, worker: …, reviewer: …}`；这是唯一的任务级配置 |
| `agent_sessions` | 增加 agent 或运行时字段，用来发现"换了运行时，会话不能续接"（第 13 节 P5） |
| 运行记录（`llm_records`） | 增加 agent、运行时、provider、模型，以及花费来源（`runtime` 或 `provider_price`） |

内置并自动注册：运行时 `claude-code`、provider `claude-subscription`，以及与现有分层配置里各个模型对应的 agent。
迁移后各角色解析出的模型保持不变。原来的 `llm_profiles` 表和 `agent_configs.runtime`、`llm_profile_id` 两列从未接入
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

✅ 已核实，❓ 待核实，— 不适用。CLI 的核实依据是本机版本的 `--help`，2026-10-14。

| 能力 | Claude Code 2.1.285 | Codex 0.162.0 | OMP 18.4.3 | Claude Agent SDK | OpenAI Agents SDK | norma |
|---|---|---|---|---|---|---|
| 一次性运行 | ✅ `--print` stream-json | ✅ `exec --json` | ❓ | ❓ | ❓ | 待定 |
| 常驻会话与续接 | ✅ stream-json 输入 + `--resume` | ❓ `exec resume <id>`（逐轮起进程）、`app-server`（实验性）、`queue` | ❓ | ❓ | ❓ | 待定 |
| 强制结构化输出 | 靠 prompt 契约 | ✅ `--output-schema`（常驻轮次能否用 ❓） | ❓ | ❓ | ❓ | 待定 |
| skill 与插件 | ✅ Claude 插件，`--plugin-dir` | ❓ 有 `codex plugin`，格式和是否读 `SKILL.md` 待核实 | ❓ | ❓ | — | 待定 |
| MCP | ✅ `--mcp-config` + `--strict-mcp-config`（M8b） | ❓ `codex mcp`、`config.toml` 的 `[mcp_servers]` 或 `-c` | ❓ | ❓ | ❓ | 待定 |
| 额外目录（知识） | ✅ `--add-dir` | ✅ `--add-dir` | ❓ | ❓ | — | 待定 |
| 不限权限 | ✅ `--dangerously-skip-permissions` | ✅ `--dangerously-bypass-approvals-and-sandbox` | ❓ | ❓ | — | 待定 |
| 隔离 | ✅ `--setting-sources project` 等（M1） | ❓ 独立 `CODEX_HOME`，其中有全局 `AGENTS.md`、`config.toml` 和登录 | ❓ | ❓ | — | 待定 |
| 非 git 目录 | ✅ | ✅ 需要 `--skip-git-repo-check` | ❓ | — | — | 待定 |
| 花费报告 | `total_cost_usd`，按 Claude 价格计算 | 只有 token 数（❓） | ❓ | ❓ | 只有 token 数 | 待定 |
| 压缩事件 | ✅ `compact_boundary` | ❓ | ❓ | ❓ | — | 待定 |
| 自主轮（#26） | ✅ | ❓ | ❓ | ❓ | — | 待定 |

决策 10 要求所有运行时都支持所有角色。矩阵里 ❓ 的项，在对应阶段开工时第一件事就是实测，结果写回本表。

## 7. provider 绑定

- **Claude Code + 订阅**：保持现状。
- **Claude Code + API key 或兼容接口（GLM、Kimi、CPA 的 `/v1/messages` 等）**：
  - 注入 `ANTHROPIC_BASE_URL`，以及 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`；
  - 主模型用 `ANTHROPIC_MODEL` 或 `--model`；
  - **必须同时映射后台模型**：`ANTHROPIC_DEFAULT_HAIKU_MODEL`、`ANTHROPIC_SMALL_FAST_MODEL`、
    `ANTHROPIC_DEFAULT_SONNET_MODEL`、`ANTHROPIC_DEFAULT_OPUS_MODEL`、`CLAUDE_CODE_SUBAGENT_MODEL`。否则 Claude Code
    做标题、摘要和子 agent 时，会拿 Claude 的模型名去请求第三方接口；
  - 这些变量在 2.1.285 的二进制里都存在，`apiKeyHelper` 也在，具体语义和"API key 是否优先于订阅登录"在 M9b 实测。
- **Codex + 登录**：见第 13 节 P1。
- **Codex + API key（含 CPA）**：用 `model_providers.<id>` 配置 `base_url`、密钥所在的环境变量和 `wire_api`，经
  `-c` 或隔离目录里的 `config.toml` 传入，在 M9c 实测。
- **SDK**：直接传入 key 和 base_url。
- **开发测试**：用本机 CPA（`127.0.0.1:8317`）。它同时提供 `/v1/responses`、`/v1/chat/completions` 和 Anthropic 格式的
  `/v1/messages`。key 在运行时从 CPA 的配置文件读取，不进仓库、不进日志。

## 8. 花费与上限

- **统一口径**：每次运行记录花费来源。
  - `runtime`：只在 Claude Code 接 Anthropic 官方接口或订阅时可信。
  - `provider_price`：用 token 数乘以 provider 登记的价格（含缓存读写），适用于其他所有情况。Claude Code 接第三方
    接口时，`total_cost_usd` 仍按 Claude 的价格计算，不能用。
- **订阅类 provider**：花费是按 API 价格折算的"等价花费"，不是实际支出，界面上要区分显示。
- **单次上限**：`--max-budget-usd` 只在 `runtime` 口径可信时使用。其他情况由 LoopX 从事件流累计用量，超过上限就
  停止这次运行。这是软上限，可能多出一个请求。
- **任务上限**（M5）和"提高上限并继续"（#43）一律按统一口径计算。

## 9. 目录、知识与 prompt

- **目录条目**（M8）标注兼容的运行时类型：Claude 插件只挂给 Claude 系运行时，MCP 由适配器翻译。装给某个角色、
  但它当前运行时不兼容的条目会跳过，并记在运行记录里，不会让运行失败。
- **知识**：Claude Code 和 Codex 都有 `--add-dir`。没有这个能力的运行时，方案待定。
- **prompt**：
  - harness 和角色 prompt 保持与运行时无关。核查过，现有 prompt 没有写死工具名；
  - 运行时之间的差异（工具名、怎么输出结构化结果）由适配器在代码契约层前追加一小段"运行时说明"；
  - 回复契约（Planner 的计划和决策、Worker 和 Reviewer 的报告）不变，Codex 可以考虑用 `--output-schema` 加强约束。

## 10. 隔离与凭据

- 每个运行时一份隔离配方，延续决策 4 和 7：只用 LoopX 拼装的内容和项目仓库自带的配置，不读操作者的个人配置。
- **API key**：存在数据库里，静态未加密，接口打码。注入子进程环境后，不限权限的 agent 能读到，见 P3。
- **OAuth**：只在发放它的运行时内使用。

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

M9b 与 M9c 并行。

## 13. 待决问题与风险

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| **P1** | **Codex 登录在隔离下会互相刷掉 token。** Codex 的 refresh_token 会轮换（CPA 运维中实测过）。每次运行复制一份 `CODEX_HOME` 的话，一个实例刷新 token 后，其他实例手里的就失效了 | Codex + 登录不可用，或者时好时坏 | **已定（2026-10-14）：** Codex 默认走 API key，开发阶段用 CPA。之后如果需要 Codex 登录，所有实例共用同一个 `auth.json`（符号链接）并加文件锁，只隔离配置 |
| **P2** | **其他运行时的常驻会话怎么实现还不知道。** 可能是 `exec resume`（每轮起一个进程、重新加载上下文）、`app-server`（实验性）或 `queue`。自主轮和压缩事件也可能没有对应物 | "所有运行时都支持所有角色"的工期和稳定性 | **已定（2026-10-14）：** 先用"每轮起进程 + 续接会话"（逐轮续接）；连续接都不支持的运行时，每轮开新会话，由 LoopX 注入完整状态（计划、任务日志、最近对话的摘要）。M9c 第一步做技术验证，结果写回能力矩阵 |
| **P3** | **provider 的密钥对 agent 可见。** 不限权限的 agent 能读自己的环境变量 | 密钥可能被模型写进输出或带出去 | **已定（2026-10-14）：** 近期接受，在本文写明。之后再做一个本地代理持有密钥，agent 只拿到本地地址（做法类似 CPA） |
| **P4** | **第三方模型遵守契约、使用工具的质量参差不齐**，可能需要按模型调整 prompt | Planner、Manager 不稳定 | **已定（2026-10-14）：** 先不做按模型的 prompt 变体，所有 agent 用同一套 prompt，运行时差异放在适配器追加的"运行时说明"里；靠 M6 评测把关，评测不过的 agent 不配给 Planner 和 Manager |
| P5 | **运行中换 agent。** 常驻会话绑定在运行时上，换了 agent 就不能续接 | 层级或任务覆盖改了以后，Planner、Manager 要新开会话 | **已定（2026-10-14）：** 改配置立即生效，与"改了下次运行就生效"一致：Worker、Reviewer 下一步就用新 agent；Planner、Manager 在同一个运行时里换模型照旧续接，换了运行时就新开会话，用 M7 的计划状态和任务日志把进度重新注入，并在任务页记一条"已更换 agent" |
| P6 | **花费口径不一致**（见第 8 节） | 观测和上限失真 | **已定（2026-10-14）：** 随版本内置常见模型的价格表，provider 的模型目录里可以覆盖；查不到价格的模型只显示 token 数，注册 agent 时提示花费上限对它无效。订阅类 provider 一律按 API 价格折算，界面标"等价"，花费上限同样生效（防止把订阅额度耗光） |
| P7 | **effort 档位不对应。** Claude 是 low/medium/high/xhigh，OpenAI 是 minimal/low/medium/high | 分层配置里的 effort 含义不清 | 适配器里维护映射表；provider 的模型目录声明支持哪些档位，不支持的给出提示 |
| P8 | **并发与限流** | 某个 provider 被打满（比如 CPA 自身的并发限制） | **已定（2026-10-14）：** 默认不设上限，与现在一致；需要时在 provider 上设置，超出的排队等待，不会失败 |
| P9 | **目录跨运行时**：Claude 插件和 Codex 插件格式不同；`SKILL.md` 是否通用还要核实 | 装了的 skill 在某些运行时上加载不了 | **已定（2026-10-14）：** 能转换就转换（MCP 翻译成对应格式，运行时原生支持的 skill 按它的格式挂载）；转换不了的 skill 降为可读文档：像项目知识一样只读挂载，并在 prompt 里给出"可用 skill：名称、描述、路径"的索引，由模型按需读取；插件里的 hook、命令等无法转换的部分跳过，并记在运行记录里。目录界面显示每个条目兼容哪些运行时 |
| P10 | **OMP 的能力还不清楚** | — | **已定（2026-10-14）：** 在 M9c 之后单列 M9e：等 Codex 把适配器接口验证过，先只读核实 OMP 的能力、填好能力矩阵，再写适配器；开发用 CPA |
| P11 | **norma SDK 的接入形态** | — | 建议放在独立的子进程里，便于隔离，崩溃也不会拖垮服务；双协议意味着它能配任意一种 provider |
| P12 | **后续方向**（本阶段不做） | — | 按成本和质量路由模型、失败自动换 agent、同一步骤让多个 agent 交叉审查 |
