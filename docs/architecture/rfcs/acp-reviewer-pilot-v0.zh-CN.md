# RFC：ACP Reviewer 试点（v0）

- **状态：** 决定已定，只有文档。第 5 节的 D1–D9 是本文的选择；第 11 节的 Q1–Q8 维护者在 2026-11-22 全部按推荐答复，记录在第 11 节，成为决定 D10–D17。本文合入后按第 10 节的 PR 顺序实现。
- **关联：** [Grok 运行时](grok-runtime-v0.zh-CN.md) 第 14 节（管家与规划者的 ACP 常驻会话）、[多运行时](multi-runtime-agents-v0.zh-CN.md)、[Worker steering](worker-steering-v0.zh-CN.md)、[API 运行时](api-runtime-v0.zh-CN.md)（`roles` 收窄、默认不变、失败不回退）、[TTADK 启动](ttadk-launch-v0.zh-CN.md) 第 6.6 节、[架构说明](../agent-system.md)、[评测](../../../evals/README.md)。
- **核对基线：** 仓库路径与符号按 `main` `bf447cff9` 核对。评审链接指向该历史上的 `2d04d3b14`；下面引用的行号在 `bf447cff9` 上仍然对得上。以 `looper/`、`evals/`、`docs/` 开头的路径相对仓库根目录。
- **本机核对范围（2026-11-22）：** 读了下面引用的源码。对 PATH 上的 CLI 只跑了 `--help` / `--version`，家目录用空的临时目录，没有启动 `looper serve`，没有模型调用，没有 ACP 握手。帮助文本里出现的 ACP 子命令只说明「可执行文件这么广告」；协议行为、权限是否真的来问、用量是否上报，一律标**待验证**。

## 1. 背景与目标

Looper 每接一种 agent CLI，就在 `looper/agents/runtimes/` 写一个专用适配器：自己的 argv、自己的流、自己的只读方式、自己的用量字段。已经有九种类型（第 2.1 节）。其中只有 Grok 的管家和规划者会话走 [Agent Client Protocol](https://agentclientprotocol.com)（下称 ACP）：一个进程、一行一个 JSON-RPC 2.0 消息。Worker 和 Reviewer 在 Grok 上默认仍是 headless；探测到 steering 之后，这一次运行才改走 ACP（第 2.2 节）。

**要回答的问题：** 一个通用 ACP 客户端，能不能取代「每家 CLI 一个 runner」。

**试点：** 只接一个通用适配器，只给 Reviewer 用。验收是第 8 节那四个固定用例、两侧各 `--reps 2` 的回归门槛。通过这道门槛，只说明这一台命令在这些用例上过关。第二台 CLI 用同一客户端跑通之前，不下「通用适配器可以取代专用 runner」的结论（D10）。Reviewer 是一次运行、一份裁决、没有常驻会话，适合先看协议能不能撑起现有的记录和裁决，而不先重做管家或规划者。

**目标：**

- 运行时类型 `acp`：操作者配置一条 ACP 进程的 argv；适配器只实现 ACP v1 的公开方法。
- 一次评审一个 ACP 会话：`session/new`，一次 `session/prompt`，然后结束进程。
- 事件翻译成现有的内部 stream-json，`ClaudeRunRecorder` 和 `judge_result` 不改判定。
- 默认关。没有 Profile 选它之前，Reviewer 仍走原来的运行时。
- 试点期只有操作者能在控制台登记命令、打开开关、建立 Profile、把 Reviewer 指到它。

**不做（v0）：**

- 不给 Manager、Planner、Worker、processor、advisor 用。
- 不做 Reviewer 的 steering，不实现 `_x.ai/interject`，不把 `grok_acp.AcpGrok` 抽成库。
- 不自动回退到原来的 Reviewer 运行时。
- 不把 ACP v2 Preview 当已实现（第 3.5 节）。
- 不改 `docs/architecture/agent-system.md`。该文第 1474–1476 行写 Claude Code 和 Grok 不列 `steer`；代码在探测通过后会给 Grok 加上（`grok.py:181-185`）。以代码为准。

## 2. 现状

以下每条都按 `bf447cff9` 的源码核对。

### 2.1 运行时适配器与能力

所有 agent 运行都经过 `RuntimeAdapter`。四个 tier 自己不拼运行时命令（`looper/agents/runtimes/__init__.py:1-7`）。适配器把各家输出翻译成 Claude Code 的 stream-json（同文件 19–24 行），记录器、用量和 tier 读的是这一种事件。

`Capability`（同文件 185–210 行）存在 `agent_runtimes.capabilities`，CHECK 列表在 `looper/db/schema.sql:25-27`：`run`、`resident_session`、`resume`、`structured_output`、`claude_plugins`、`mcp`、`add_dirs`、`compaction_events`、`autonomous_turns`、`cost_report`、`read_tools`、`steer`、`no_tools`、`headless_compaction`、`mid_turn_input`。

`ROLE_CAPABILITIES`（`__init__.py:255-268`）：manager 和 planner 要 `resident_session` + `resume`；worker 和 reviewer 只要 `run`；processor、advisor、advice_check、leads_check、outbound_check 要 `run` + `no_tools`。适配器的 `roles` 可以再收窄；`servable_roles` / `check_roles` 会用它（`looper/agents/profile_registry.py:815-828`、`895-902`）。`api` 因此只有 manager（`looper/agents/runtimes/api/adapter.py:76-79`），没有 `run`，不能当 Reviewer。

`ADAPTERS`（`__init__.py:785-801`）固定这九种：`claude-code`、`coco`、`codex`、`omp`、`grok`、`cursor-agent`、`dsh`、`pi`、`api`。类型是代码，不是操作者新建的行。

类上的能力是探测前的声明。探测可以删掉或加上 `steer`。下表的「只读」只在 `Launch.read_only` 为真时生效（第 2.5 节）。

| 类型 | 类上的能力 | 探测 | 只读 | 常驻 |
|---|---|---|---|---|
| `claude-code` | `Capability` 去掉 `structured_output`、`read_tools`、`steer`（`claude_code.py:157`） | 不在探测里加 `steer` | `permissions`（173 行） | 类默认 `per_turn`；`can_interrupt`、`run_input`、`keepable` 为真（162–164 行） |
| `codex` | `run`、`resident_session`、`resume`、`structured_output`、`mcp`、`add_dirs`（`codex.py:146-147`） | 版本 ≥ 0.162.0 且 `app-server` schema 含 `turn/steer` 时加 `steer`（93、207–221 行） | `sandbox`（154 行） | 每轮一个进程 |
| `coco` | `run`、`resident_session`、`resume`、`mcp`、`add_dirs`（`coco.py:35-36`） | 不用 Codex 的版本门槛；schema 里有 `turn/steer` 时加 `steer`（58–90 行） | `sandbox`（42 行） | 每轮一个进程 |
| `omp` | `run`、`resident_session`、`resume`、`claude_plugins`、`mcp`、`add_dirs`、`compaction_events`、`read_tools`（`omp.py:162-164`） | 低于 18.4.3 去掉 `read_tools`（146–180 行）；≥ 18.4.3 且 `--help` 含 `--no-ui` 与 `rpc` 时加 `steer`（147、209–216 行） | 有 `read_tools` 时 `tools`，否则 `check`（182–183 行） | 每轮一个进程 |
| `grok` | `run`、`resident_session`、`resume`、`mcp`、`compaction_events`、`cost_report`（`grok.py:135-136`） | 版本 ≥ 1.0.77 且 `probe_interject` 成功才加 `steer`（181–185 行）。`compaction_events` 只描述 headless 流 | `tools`（148 行） | `resident_transport = ACP`（151 行）；`can_interrupt` 为真，回落后不再成立（152 行） |
| `cursor-agent` | `run`、`resident_session`、`resume`、`mcp`、`add_dirs`（`cursor_agent.py:234-235`） | 类上没有 `steer` | `check`（244 行）；`loads_operator_config`、`runtime_cost_capped = False`（245–247 行） | 每轮一个进程 |
| `dsh` | `run`、`mcp`、`resident_session`、`resume`（`dsh.py:302`） | 版本字符串必须等于 `0.2.1-rc.2`，否则能力为空（55、314–346 行） | `permissions`（312 行）；`resident_transport` 显式 `PER_TURN`（310 行） | 每轮一个进程 |
| `pi` | `run`、`resident_session`、`resume`、`steer`、`read_tools`、`compaction_events`、`mcp`（`pi.py:135-136`） | 低于 1.3.0 则能力为空（68、148–152 行）。`mcp` 只是 Looper 平台桥 | `tools`（143 行） | 每轮一个进程 |
| `api` | `resident_session`、`resume`；`roles = {manager}`（`api/adapter.py:76-79`） | 无 `run` | 进程内，没有 Reviewer 路径 | 进程内会话 |

`ReadOnly`（`__init__.py:220-227`）：`permissions` 拒绝写并上报；`sandbox` 让写失败、不报拒绝；`tools` 只给读工具；`check` 由调用方比较目录。这四种都挂在 `Launch.read_only` 上。Reviewer 的 `Launch.read_only` 今天为假（第 2.5 节），所以它们不套在 Reviewer 上。`ResidentTransport`（238–242 行）只有 `per_turn` 和 `acp`。默认是 `per_turn`。`PerTurnSession.can_interrupt` 为假（`per_turn.py:147-148`）：没有中断通道，管家拒绝用户停止。生命周期 `stop()` 仍会结束正在跑的那一轮：忘掉会话，并对仍在的进程调用 `proc_tree.end`（`per_turn.py:384-389`）。

### 2.2 Grok 的 ACP、逐轮会话，以及 Worker steering

**常驻会话。** `grok_acp.py` 的模块说明（1–30 行）：`grok agent --always-approve … stdio`，一行一个 JSON-RPC。`AcpGrok` 一个进程扛会话的每一轮。`ResidentGrok` / `PerTurnSession` 是 ACP 不可用时的回落：每轮一个进程，`--resume` 续接。协议版本钉在 1（`PROTOCOL_VERSION`，59 行）。超时：`initialize` 30 秒，`session/new` 与 `session/load` 60 秒，取消宽限 5 秒（60–63 行）。

一条常驻回合是 `initialize` → `session/new` 或 `session/load` → `session/prompt`（624–658 行）。`mcpServers` 传空数组，MCP 写在 `config.toml`。`session/prompt` 的响应当成这一轮的结束：`AcpTurn.finish` 用 `stopReason` 和 `_meta.usage` 生成 `result`（316–346 行）。`costUsdTicks / 1e10` 写成 `total_cost_usd`；ticks 缺失或 0 就不写花费，不记 0。`inputTokens` 含缓存，`finish` 先减掉，好和 headless 的口径对齐。这些字段是 Grok 放在 `_meta` 里的，不是通用 ACP 的必填项。

`session/request_permission` 直接回答 `cancelled`（612–618 行），因为命令带了 `--always-approve`，按设计不该来问。只读不靠这个回调：根级 `--tools` 对 `agent stdio` 无效，读工具写在 agent profile 的 `tools:` 行（`acp_argv` 155–163 行，`agent_profile` 166–175 行，`PROFILE_FILE = acp-agent.md`）。

Grok 运行时 RFC 第 14.9 节的决定仍是：管家和规划者走 ACP，Worker 和 Reviewer 保持 headless。第 14.8 节的实跑日期是 2026-10-18（Grok 1.0.62 / `grok-4.7`）。本次没有重跑。

**逐轮。** Codex、OMP、Pi、DSH，以及 Grok 的 ACP 回落，用 `PerTurnSession`（`per_turn.py:103-148`）。一轮一个进程，不能中断。

**Grok 的 steering。** `grok_steer.GrokChannel`（`grok_steer.py:1-5`）在一次 Worker 或 Reviewer 运行里说 ACP。`probe_interject`（31–37 行）只检查隐藏方法 `_x.ai/interject` 在，不建会话、不调模型。`GrokRuntime.steer_command`（`grok.py:263-279`）把 headless 命令翻成 `acp_argv`。一个进程、一个会话、一次 `session/prompt`；`session/prompt` 的响应结束这一轮（`grok_steer.py:266-312`）。权限请求同样回答 `cancelled`（317–324 行）。插话入队不证明模型读过（`grok.py:154`，`room_receipt_fallback = False`）。

因此今天存在两条 Grok ACP 路径，都不是通用客户端：常驻的 `AcpGrok`，以及 steering 那一次运行的 `GrokChannel`。没有 `steer` 的 Grok Reviewer 仍走 headless：`--prompt-file /dev/stdin --output-format streaming-messages-json --always-approve --trust`（`grok.py:208-217`、`257-261`）。`--tools read_file,grep,list_dir` 只在 `launch.read_only` 时加上。

### 2.3 Reviewer 怎么启动

规划者 `_spawn_reviewer`（`looper/agents/tiers/planner.py:2873`）和评审席 `_dispatch_review`（2948 行）发布 `VERIFY_REQUEST`。`WorkerPool._dispatch` 把这种消息当成角色 `reviewer`（`worker.py:1768-1774`）。

`_prepare` 读角色配置（2079–2080 行），装上 Reviewer 的 Profile，构造 `Launch(..., role=REVIEWER)`（2123、2157 行）。系统提示的合约名是 `REVIEW_CONTRACT`（2175–2176 行）。有 `steer` 就用 `steer_command`，否则 `run_command`（2233 行）。Reviewer 的运行时 id 不能和开发者相同，否则直接拒绝（2116–2120 行）。

Reviewer 不走 `resident_session`。它要的能力只有 `run`。

### 2.4 裁决、用量和花费记在哪里

`judge_result`（`worker.py:362-416`）读 stream-json 的 `result`。运行必须成功（`subtype: success` 且没有 `is_error`），最终文本里要有 JSON `{"passed": bool, ...}`。没有这份裁决就是失败。评审席用 `severity` 把 findings 收成 `blocking` / `suggestion`（305–318 行）。

`run_outcome`（275–286 行）把结果收成 `passed`、`not_passed`、`contract` 或 `run_failed`。没有 `result`、结果带错、或 `subtype` 不是 `success`，都是 `run_failed`。有成功结果但 JSON 读不出来，是 `contract`。`passed: false` 是 `not_passed`：评审没通过，运行本身仍算成功（3015–3023 行）。HEAD 变了则裁决改为不通过，并带 `failure: head_moved`（2999–3002 行）。

这些值不在 `llm_records` 的列上。表列有 token、`cost_usd`、`cost_source`、`metadata`、`run_kind`（`schema.sql:1240-1270`），没有 `outcome`。`finish_record` 把 `outcome` 写进 `metadata.outcome`（`looper/db/llm_records.py:283-341`）。`llm_record_events` 按序存 stream-json（`schema.sql:1276-1283`）。

`ClaudeRunRecorder.finish`（`looper/agents/claude_cli.py:1776-1824`）从 `result` 取用量；`usage_complete is False` 时把 `cost_usd` 清成空，不记 0。`_close` 用 `RuntimeAdapter.cost()` 定价，写入 `cost_usd` 和 `cost_source`（1885–1911 行）。被拒绝的工具调用可以进 `metadata.permission_denials`。

`session_budget.decide` 只在 `last_turn_cost_usd is not None` 时用花费做轮换（`looper/agents/session_budget.py:78-92`）。花费为空的一轮不计入这条花费规则。Reviewer 不是常驻会话，这条主要约束以后若有人复用客户端；试点的花费条款单独看记录里的 `cost_usd`（第 8.3 节）。

### 2.5 Reviewer 今天不是机械只读

`Launch.access_level`（`looper/agents/agent_settings.py:979-984`）：规划者是 `READ_ONLY`，其余角色包括 Reviewer 是 `FULL`。`read_only` 在访问级别不是 `full` 时才为真（1024–1028 行）。所以 Reviewer 的 `Launch.read_only` 为假，第 2.1 节的只读方式不会套上。Grok 的 `READ_ONLY_TOOLS` 也不会出现在 Reviewer 的 headless 命令里。

只读写在提示合约里。`REVIEW_CONTRACT`（`looper/agents/system_prompt.py:126-147`）要求检查验收标准，对工作目录以外只做读和 `git diff/show/log`，不要编辑、提交、换分支；裁决是末尾唯一的 JSON。这是模型可见的约定，不是运行时拒绝。

评审结束时若 HEAD 变了，裁决记为不通过（`worker.py:2999-3002`）。那是事后检查，不是开工前收掉写工具。验证步骤自己的基线监视（`worker.py:2055-2064` 一带）走的是验证工作，不是 `VERIFY_REQUEST` 这条 Reviewer 路径。

试点要把 ACP Reviewer 收成进程级只读（D4、第 6.3 节）：保护集上的写在启动前就被禁止，装不上策略就不起进程。这比今天的 Reviewer 更严，是试点的验收条件，不是对现状的描述。

### 2.6 超时

`WorkerSettings.timeout_seconds`（`worker.py:169-199`）：0 表示不限时。角色配置读不到时用 `DEFAULT_RUN_TIMEOUT_SEC = 86400`（113、192 行）。种子默认是 1200 秒（`looper/db/role_configs.py:123`），且不覆盖已经改过的提示。运行用的是启动时读到的 `settings.timeout_seconds`（`worker.py:2080`）。超时错误文案今天仍写 “Claude ran longer than …”（2918 行）；结果是 `run_failed`。

### 2.7 运行时登记没有 argv

`agent_runtimes.executable` 是一条路径或命令名，空表示用类型默认（`schema.sql:22`；`profile_registry.py:533-542`）。它装不下 `grok agent stdio` 这种带参数的命令。

运行时是内置的。`register_found_runtimes` 在默认可执行文件出现在 PATH 上时登记（`profile_registry.py:1327-1347`）。配置导入按名字或内置类型解析运行时，保留目标环境的 executable（`config_transfer.py:1214-1259`；方法说明是 keeping the destination's executable）。目标上已有同名行则记为不变，文件里的 executable 只做校验，不写进去。目标上没有、且该类型已是内置行时，用目标环境登记的那一行。未知类型跳过，并说明 runtimes are built in，不会新建一种类型。操作者能做的写操作是重新探测：`POST /api/v1/runtimes/probe`（`looper/agents/v1_domains/registry.py:143`）。管家工具 `looper_agent_profiles` 能建、改、删 Profile，并按 id、名字或类型选择已有运行时（`looper/agents/ops/config.py:781-807`），不能发明一种运行时，也不能存一条 argv。

所以「默认关的通用 ACP 命令」在今天没有落点。第 7 节给它一个新的落点。

## 3. 今天谁说 ACP

### 3.1 本仓库已经接上的

只有 Grok 的 `agent stdio`。代码路径见第 2.2 节。Grok 运行时 RFC 第 14.8 节记录了 2026-10-18 的实跑。本次又看到本机 `grok --version` 为 1.0.89（高于代码里的 1.0.77 steering 门槛），`grok agent --help` 列出 `stdio`。没有重做握手，也没有跑 `probe_interject`。1.0.89 是否仍按第 14 节的字段报 `_meta.usage`，**待验证**。

### 3.2 本机帮助文本广告了 ACP，协议行为待验证

空临时家目录下的帮助文本（没有握手）：

| CLI | 本机版本 | 帮助文本 | Looper 今天怎么跑它 |
|---|---|---|---|
| coco（`traecli`） | 0.216.0 | `--help` 有 `acp` 和 `--acp`，写明 ACP server over stdio | `CocoRuntime` 走 app-server / exec，不走这条（`coco.py:58-90`） |
| cursor-agent | 2026.11.19 | `cursor-agent acp --help` 写明作为 ACP server 启动 | headless 加 `--resume`。TTADK RFC 第 249 行写过 `agent acp` 的 `session/prompt` 不报用量（该 RFC 的调研，本次未重做） |
| omp | 18.4.3 | `--help` 有 `acp`，ACP server over stdio | `OmpRuntime` 走 JSON / rpc，不走这条 |
| dsh | 0.2.2-rc.1 | `dsh acp --help` 是随发行版的 profile `acp`，帮助写明在 stdio 上服务 ACP，示例 `dsh --profile acp` | 能力只在版本等于 `0.2.1-rc.2` 时打开。本机版本对不上，探测应得到空能力。`DshRuntime` 不启动这个 profile |

这四家的 `initialize` 版本、`session/prompt` 是否结束回合、权限请求、用量字段，全部**待验证**。

### 3.3 本机帮助文本没有 ACP 子命令

- `claude acp --help` 打出的是 Claude Code 的总用法，没有 ACP 子命令说明。`claude mcp` 是 MCP，不是 ACP。
- `codex acp --help` 回到 Codex 的总用法。`codex app-server` 的 stdio 是它自己的 app-server（`codex.py` 在 schema 里找 `turn/steer`），不是 ACP。
- `pi --help` 与 `pi acp --help` 都没有 ACP 子命令说明。

公开材料里的适配器包 `@agentclientprotocol/claude-agent-acp`、`@agentclientprotocol/codex-acp`，以及 `pi-acp` 一类社区进程，本次没有安装，也没有握手，**待验证**。不能把 `claude` 或 `codex` 二进制写成会说 ACP。

### 3.4 本机 PATH 上没有的

`gemini`、`opencode` 不在 PATH 上。公开文章里的 `gemini --acp`、`opencode acp` **待验证**。试点的第一台命令不依赖它们。

### 3.5 协议版本

Looper 的 Grok 客户端把 `protocolVersion` 钉在 1，并且把 `session/prompt` 的响应当成回合结束（`grok_acp.py:316-346`、`641-643`；`grok_steer.py:307-312`）。

ACP 站点 2026-11-20 的公告 [ACP v2 is available in Preview](https://agentclientprotocol.com/announcements/acp-v2-preview) 写的是另一套生命周期：`session/prompt` 的响应只确认插入（带 `messageId`），不结束回合；结束原因之后以空闲 `state_update` 到来；`session/resume` 取代 `session/load`。该页本次读过。各 CLI 实际谈判到 v1 还是 v2，**待验证**。通用适配器若按 v2 去等 `session/prompt` 的 `stopReason`，会和现有 Grok 客户端的假设相反，也会把一轮评审挂住。

## 4. 方案对比

| 方案 | 做法 | 复杂度 | 故障半径 | 能不能回答「通用适配器够不够」 |
|---|---|---|---|---|
| **A 通用 ACP 适配器，只给 Reviewer**（推荐） | 新类型 `acp`，一个 v1 客户端，操作者提供 argv。一次评审一个会话。事件仍进现有记录器 | 中：新客户端、新登记方式、评测要能把真实 Reviewer 指到它 | 一个 Reviewer 进程；开关关着时现有路径不走它 | 能。同一客户端换 argv，才算通用 |
| B 继续每家 CLI 一个 runner | 谁要进 Reviewer，就照 `grok.py` 写一份 | 每家一份 argv、流和用量 | 小，但和今天一样散 | 不能。写专用 runner 回答的是另一件事 |
| C 把 `grok_acp` 抽成库，Reviewer 走 Grok 专用路径 | Reviewer 复用 `AcpGrok` 或 `GrokChannel` | 低 | Grok 的扩展字段漏到别的 CLI 上会误记花费或误拒绝权限 | 不能。`costUsdTicks`、`_x.ai/interject`、`config.toml`、agent profile 的 `tools:` 都是 Grok 的 |

选 A。C 省代码，但测到的仍是 Grok 专用客户端。B 是现状，留到 A 的评测失败之后。方案 A 的一次通过只覆盖第 8 节的回归门槛，不表示专用 runner 已经被取代。

## 5. 决定

| # | 决定 | 理由 |
|---|---|---|
| D1 | 新运行时类型 `acp`，`roles = {reviewer}`，能力只有 `{run}`。不声明 `resident_session`、`resume`、`steer`、`cost_report` | Reviewer 只要 `run`（`ROLE_CAPABILITIES`）。多声明常驻能力会让 `servable_roles` 把它算成也能跑管家。`roles` 是 API 运行时已经用过的收窄办法 |
| D2 | 一个进程、一个 `session/new`、一次 `session/prompt`、然后结束。不 `session/load`，不把会话留给下一次评审 | 和今天的 Reviewer 一次运行对齐。常驻多轮是管家的问题，不在试点 |
| D3 | 只实现 ACP v1 的公开方法。`initialize` 回来的 `protocolVersion` 不是 1 就拒绝启动。不调用 `_x.ai/*`，不读 `costUsdTicks`，不写 Grok 的 `config.toml` 或 agent profile | 通用客户端若依赖 Grok 扩展，就回到方案 C。v2 的回合结束方式和 `AcpTurn.finish` 不同 |
| D4 | 写在进程级被禁止。启动前对仓库、Git 元数据、以及允许检查的外部目录装上子进程继承的只读策略；装不上就拒绝启动。权限回调默认拒绝未知操作。canary 只在评测里报警 | Reviewer 今天是 `FULL`（第 2.5 节）。`mcpServers: []` 关不掉 CLI 自带的 MCP。选项标题不能当读/写的分类。事后 canary 挡不住已经落盘的写 |
| D5 | 事件翻译成现有 stream-json。`judge_result` 与 `ClaudeRunRecorder` 的判定不改 | 裁决和记录已经读这一种事件（第 2.4 节） |
| D6 | 用量缺失时 token 与 `cost_usd` 留空，`usage_complete` 为假，不记 0 | 与 `claude_cli.py:1814-1815` 一致。记 0 会把未知花费当成没有花费 |
| D7 | 开关默认关，不从 PATH 自动登记。禁止自动回退进入或离开 `acp`。已经绑定后再关开关，下一次 launch 报错，不换运行时 | 同 API 运行时 D8：这一轮报错，操作者换 Profile。`roles = {reviewer}` 挡不住 Planner 把 Reviewer 选成这个 Profile（第 7.2 节） |
| D8 | 试点期登记命令、打开开关、建 Profile、把 Reviewer 指到它，都只在控制台。Planner、任务模板、导入、fallback、管家工具和渠道按第 7.2 节拒绝 | 今天没有地方存 argv（第 2.7 节）。这些入口今天都能选中一个已有的 Reviewer Profile。试点不从它们改一条会跑任意进程的命令 |
| D9 | 同一组 `reviewer_*` 评测、两侧各 `--reps 2`。先要求基线有效，再比较；误放行、`contract`、`run_failed` 是绝对零门槛（第 8.2 节）。质量、安全、花费三条分开 | 现有 harness 的真实 Reviewer 被钉在 Claude Code 上，要另有一个开关才能把 `real_reviewer` 指到 `acp`。那个开关是后续 PR，不是本文件。`--reps 2` 是这四个固定用例的回归门槛 |

## 6. 设计

### 6.1 适配器边界

| 通用客户端拥有 | 留在各家 CLI 或操作者的命令里 |
|---|---|
| `initialize`、`session/new`、`session/prompt`、`session/cancel`、进程结束 | 可执行文件和它进入 ACP 的参数（`grok agent stdio`、`coco acp`、`cursor-agent acp`、`omp acp`、`dsh --profile acp` 等） |
| 协议版本必须是 1 | 账户、模型和价目按第 7.1 节的命令身份配对。argv 解决不了 `check_runtime` |
| `session/request_permission` 的默认拒绝 | 各家不来问权限时的自有工具。写保护是第 6.3 节的进程策略，权限回调补不上 |
| `session/update` → stream-json | `_meta` 里的厂商字段。认不出就忽略 |
| 超时、取消、失败分型 | steering、常驻多轮、`session/load` |

不 import `grok_acp` 或 `grok_steer`。`initialize` 30 秒、`session/new` 60 秒可以与 `grok_acp.py:60-61` 同数字，常量写在新客户端里。取消不另做一套 5 秒计时，走第 6.7 节的现有进程树结束。形态上更接近 `GrokChannel` 的「一个进程、一次 prompt」，方法只用 v1 公开的那些。

Reviewer 的进程由 `ProcessRun.launch` 拉起（`worker.py:542-552`）：`proc_tree.spawn` 之后调用 `runtime.run_channel`。默认实现（`runtimes/__init__.py:639-644`）返回 `StreamChannel`。`StreamChannel.start`（`channel.py:148-152`）调用 `write_turn`，把首轮提示写进 stdin。`parse(stdout)` 只读 stdout，回答不了 `session/request_permission`。只返回 argv、实现 `parse`、再让 `write_turn` 抛错，首轮起不来。

`acp` 覆盖 `run_channel`，返回专用 channel。`write_turn` 和 `parse` 仍要实现（二者是抽象方法），Reviewer 路径不经过它们。`resident_command` 和 `steer_command` 抛出不支持。`run_command` 返回操作者配置的 argv，提示不进 argv。

专用 channel 拥有这一条双向 JSON-RPC：

- `run_channel` 收到 `prompt_file`。那是 `system_prompt.write_file` 写下的系统提示，里面有 `REVIEW_CONTRACT`（`worker.py:2164-2223`）。
- `start(prompt)` 收到 Worker 今天传给 `channel.start` 的 verify 提示（`worker.py:2835`），不调用 `write_turn`。
- 一次 `session/prompt` 发两段文本，顺序固定：先是 `prompt_file` 的原文，再是 verify 提示。客户端不改写合约，不代写裁决 JSON。`prompt_file` 读不到就不发 `session/prompt`，这一轮 `run_failed`。
- stdout 的读循环同时处理应答、`session/update`，以及 agent 发来的请求。`session/request_permission` 在这条 stdin 上回答（第 6.3 节）。
- `events()` 把通知译成现有 stream-json。进程的等待和结束仍是 `ProcessRun.wait` 与 `ProcessRun.end`。

### 6.2 一次评审一个会话

1. 先装上第 6.3 节的只读策略，再 `proc_tree.spawn` 操作者的 argv，工作目录为评审工作树。策略装不上则不 spawn。
2. `initialize`，`protocolVersion: 1`，客户端信息不广告 v2。30 秒无应答或版本不是 1：这一轮 `run_failed`，不回落。
3. `session/new`，`cwd` 为评审工作目录，`mcpServers: []`。60 秒无会话 id：`run_failed`。空的 `mcpServers` 只表示 Looper 不注入 MCP（第 6.4 节）。
4. 一次 `session/prompt`：`prompt_file` 的系统合约原文，接上 verify 提示。合约仍要求末尾 JSON 裁决。
5. v1 下，`session/prompt` 的响应结束回合。`stopReason: end_turn` 且最终文本在：`result` 为 `subtype: success`。其他 stop reason、协议错误、进程先退出：`result` 带 `is_error`。
6. 结束进程。下一次评审新进程、新会话。

若实现时发现某台 CLI 的 `session/prompt` 响应只回 `messageId`、不带结束原因，那是 v2 行为：按 D3 拒绝，记 `run_failed`，不要在客户端里猜回合何时结束。该 CLI 留在**待验证**，不进入「通用适配器可用」的结论。

### 6.3 权限

写保护是进程策略。权限回调和 canary 都不是这道边界。ACP 的 `session/request_permission` 由 agent 发起；agent 自己的工具可以不来问。这一点对各 CLI **待验证**。设计按「可能不来问」做：不来问也写不了保护集里的路径。

**进程策略，在 `proc_tree.spawn` 之前装上，子进程继承：**

1. 保护集在启动时解析成绝对路径。工作树根用 `git rev-parse --show-toplevel`。Git 元数据用 `git rev-parse --git-dir` 和 `--git-common-dir`：链接工作树的 gitdir 在工作树外面，也放进保护集。允许检查的外部目录，是 verify 提示里已经点名的路径，加上操作者在这条命令上登记的检查根。解析不出的路径不跳过，这一项缺失就拒绝启动。
2. 可写目录只有两处，并且必须和保护集不相交：本次运行的 scratch（空的临时目录），以及命令登记的 CLI 配置目录。配置目录落在保护集里则拒绝。进程的 `TMPDIR` 指向 scratch。argv 不能把保护集里的路径改成可写。
3. 写默认拒绝。保护集以外的路径同样不可写，只有上面两处可写。策略由操作系统施加在 spawn 出来的进程上（实现时选用主机上能让子进程继承的机制，例如 seatbelt、landlock 或等价的 mount namespace）。Looper 的 `ReadOnly` 四种今天不套在 Reviewer 上（第 2.1、2.5 节），ACP 不靠把角色改成 `READ_ONLY` 来借用它们。
4. 主机装不上这道策略、某个保护路径加不进去、或配置目录和保护集重叠：不 spawn。错误写明缺的是哪一项。结局 `run_failed`。不回退。
5. 命令若要靠策略之外已经在跑的服务改文件，就不能标成可保证。v0 只启动「配置目录在 scratch 或另一处与保护集不相交的目录、并且进程树之外没有写仓库的服务」的命令。判断不了的命令拒绝启动。

**权限回调，默认拒绝，只补策略覆盖不到的协作式询问：**

- 允许只发生在客户端写死的表里，表的键是协议给出的结构化只读种类。选项的标题或描述里出现 read、grep 这类词，不算数。
- 没有种类、种类不在表里、或请求看不出是只读：拒绝。`fs/write` 和会写的 terminal 请求拒绝。
- 拒绝不让这一轮失败。模型可以改口，末尾仍要有合法裁决。
- 不传 `--always-approve`。也不把 Grok 那条「一律 `cancelled`」搬过来。一律取消会让读工具也做不成，评审无法核对文件。拒绝只覆盖未允许的操作。
- 若该 CLI 有工具允许列表，操作者把它放进 argv。通用客户端不解析各家旗标。

评测里的 canary（第 8.2 节）用来发现策略被绕过。一次写就停试点。canary 不代替策略，也不把已经落盘的写变成「事后判失败但仍算启动成功」。

### 6.4 工具与 MCP

v0 的 `session/new` 传 `mcpServers: []`，不注入 `looper mcp-server`。Reviewer 的平台工具不在这次试点里。这个空数组只停止 Looper 注入。CLI 从自己的配置加载的 MCP 仍然会起。那些服务器若是本次进程的子进程，落在第 6.3 节的策略里；若是进程树之外已经在跑的服务，这条命令按第 6.3 节拒绝启动。`launch_record` 里记「Looper 没有注入 MCP」。

能力目录、插件、`add_dirs` 不翻译。`capabilities` 里不放 `mcp`、`claude_plugins`、`add_dirs`。

### 6.5 用量和花费

`session/prompt` 结果里，只读取文档化的、本次实现时写明的用量字段。`_meta` 是扩展：实现不得假定里面有 `costUsdTicks` 或 Grok 的 token 名字。Grok 专用换算留在 `grok_acp.AcpTurn.finish`。

来源在启动前定死，不按单次结果改口。`Launch.cost_source`（`agent_settings.py:1137-1139`）调用 `runtime.cost_source(provider)`。默认是 `PROVIDER_PRICE`（`runtimes/__init__.py:689-692`）。Grok 改成 `RUNTIME`（`grok.py:358-360`），ACP 不跟着改。因此 `Launch.price`（`agent_settings.py:1141-1150`）在启动前按 Profile 上声明模型的价目确定。

记账走现有的链，适配器不另写一条：

1. 结果事件把文档化的 token 写入 `usage` 和 `modelUsage`。`modelUsage` 的键是第 7.1 节声明的模型名。CLI 报了另一个名字时，token 仍可留下，同时 `usage_complete` 为假，metadata 记 `model_reported`。不用那个名字去套价目。
2. 文档化的美元字段只写入 metadata `runtime_reported_usd`，不写入 `total_cost_usd`。`launch.price` 非空时，`reported_totals`（`run_usage.py:138-168`）用价目算花费，不用 `total_cost_usd`。
3. token 缺失、模型名对不上、或价目缺一项：`usage_complete: false`。`ClaudeRunRecorder.finish` 把 `cost_usd` 清成空（`claude_cli.py:1810-1815`），不记 0。价目算不出某一模型时，`reported_totals` 同样得到空花费（`run_usage.py:165-167`）。两种情况都算花费未知。
4. `cost()` 把 `run_usage` 已经算完的 `usage` 包成 `RunCost`，`source` 仍是 `provider_price`。它不再挑选来源。`_close`（`claude_cli.py:1885-1904`）把这个 `cost_usd` 和 `cost_source` 写入记录。

质量判定不读 `cost_usd`。花费为空不让 `judge_result` 失败。推广门槛与第 8.2 节、D14 是同一句：任意一次评审的 `cost_usd` 为空，都默认阻止把这个运行时用于试点以外。操作者书面豁免时，报告把已知 `cost_usd` 的合计和花费未知的次数分开写。已知子集不是总成本。

### 6.6 事件归一

| ACP | 内部事件 |
|---|---|
| 会话建立 | `system/init`，带 `session_id` 和运行时类型 `acp` |
| 助手文本与工具调用 | `assistant`（`text` / `tool_use`） |
| 工具结果 | `user` 里的 `tool_result` |
| 权限拒绝 | 可进 `permission_denials`，沿用记录器已有的 metadata 键 |
| `end_turn` 且有最终文本 | `result`：`subtype: success`，`result` 为该文本 |
| 其他结束、超时、取消、协议错误 | `result`：`is_error: true`，或没有 `result`（两者今天都是 `run_failed`） |

最终文本必须仍由模型写出 `REVIEW_CONTRACT` 的 JSON。客户端不代写 `{"passed": ...}`。

### 6.7 超时与取消

- 整次运行：角色的 `timeout_seconds`。0 不限时；配置行读不到则 86400 秒。与今天相同。
- `initialize` 30 秒，`session/new` 60 秒。数字与 `grok_acp.py:60-61` 对齐，常量独立。
- 超时、任务取消、读流失败，今天都走到 `handle.end()`。`_end_run`（`worker.py:2501-2505`）调用它；超时和读流失败的调用点在 `2906`。`ProcessRun.end`（`worker.py:557-561`）调用 `proc_tree.end`：SIGTERM，然后对仍在的进程 SIGKILL（`proc_tree.py:209-212`，宽限 `TERM_GRACE_SEC = 3.0`，同文件 48 行）。ACP 沿用这条路径，不按进程名杀，也不再并行等 5 秒。
- 专用 channel 在 `events()` 结束或被取消时，若已有 session id 且 stdin 仍可写，先写一条 `session/cancel` 通知，然后把结束交给正在进行的 `proc_tree.end`。还没有 session id 就跳过 `session/cancel`。
- 用户停止按钮今天停的是管家回复。Reviewer 没有那条按钮。任务取消和运行超时必须走到 `handle.end()`，不能只停读流、留下 ACP 子进程。

### 6.8 失败分型

都落到现有的 `RunOutcome` 和 `judge_result`，不新开一种结局。

| 情形 | 结局 |
|---|---|
| 进程起不来、协议版本不对、`session/new` 失败 | 无成功 `result` → `run_failed` |
| 超过 `timeout_seconds` | `run_failed`。文案带运行时标签和秒数（今天 2918 行仍写 Claude，实现时改成该运行时的标签，只影响这条新路径的错误字符串） |
| 取消 | `run_failed` |
| 成功结束但没有合法裁决 JSON | `contract` |
| 裁决 `passed: false` | `not_passed`（运行成功） |
| 用量缺失或价目覆盖不了 | 裁决照常记，`cost_usd` 为空，`usage_complete` 为假 |
| 只读策略装不上，或命令不能保证进程树之外没有写 | 不 spawn。`run_failed`，不回退 |
| 评测 canary 发现写 | 试点失败，这次比较作废。策略本应让这次写发生不了。JSON 写了 `passed: true` 也不算通过 |

## 7. 配置

### 7.1 开关和命令

系统设置，和 `decision_snapshots.enabled` 一样，缺行就是关：

- `acp_reviewer.enabled`：只有值 `true` 为开。首次允许写入 `true` 的变更就是第 10 节的首次可启用交付；那之前 setter 拒绝这个值。
- `acp_reviewer.command`：JSON 数组，argv。至少两个字符串，第一项是可执行文件，其余是参数。示例形状：`["grok","agent","stdio"]`。不设 `--always-approve`。
- `acp_reviewer.model`：这条命令实际调用的模型名。
- `acp_reviewer.account`：这条命令使用的登录的标签，给运行记录看。不是密钥，也不是别的 provider 的 id。
- `acp_reviewer.config_dir`：CLI 配置目录。必须和第 6.3 节的保护集不相交。空表示用本次运行的 scratch。
- `acp_reviewer.inspect_roots`：允许检查的外部目录，JSON 数组。启动时并进保护集。

命令不写入 `agent_runtimes.executable`。该列是一条路径（第 2.7 节）。运行时行的 `executable` 可以空着或只作展示；真正启动时读设置里的整条 argv。

**命令身份。** Profile 仍然要有 `model_id`（`profile_registry.py:428-429`）。launch 时 `Launch._check_binding` 调用 `check_runtime`（`agent_settings.py:1101-1107`）。`check_runtime`（`profile_registry.py:370-380`）要求模型协议是适配器会说的那一种，并且 OAuth 账户的 `oauth_runtime_type` 必须等于运行时类型。Grok 登录是 `VendorType.GROK_LOGIN`，配对到类型 `grok`、协议 `openai-responses`（`runtimes/__init__.py:177`）。把它配到 `acp` 会被拒绝。这是对的。试点不把 Grok 的 `oauth_runtime_type` 改成 `acp`，也不挂一个无关 provider 或模型来绕过校验。那样做会让价目和运行记录都对不上实际调用。

v0 新增一种 vendor，只服务类型 `acp`。配对协议沿用「只用于配对、不发请求」的做法（`CURSOR_LOGIN`、`COCO_LOGIN` 的注释，`runtimes/__init__.py:178-181`）。适配器的 `protocols` 声明这个配对协议，`auth_kinds` 是 `{oauth}`：OAuth 在这里指这种类型自己的命令身份（`runtimes/__init__.py:475-476`），不是 Grok 的登录。`login_problem` 只检查这条命令在 `acp_reviewer.config_dir` 里能否启动，不读别的 runtime 的凭据。`check_pairing` 再要求：模型名等于 `acp_reviewer.model`；effort 为空或等于操作者在 Profile 上声明的值；provider 的 `oauth_runtime_type` 是 `acp`。价目写在这个模型行上，是这个模型自己的价目。抄另一家模型的价目来填空，配对拒绝。价目留空则花费未知，推广按第 6.5 节挡住。

`bind_provider` 不把模型旗标塞进 argv。argv 以操作者的命令为准。`Launch.record` 已有的 `profile`、`model_choice`、`llm_model` 记下这次绑定。另加 `acp_command`：可执行文件名和参数、账户标签、声明的模型、配置目录是 scratch 还是单独的路径。参数里的密钥不进记录。

类型 `acp` 放进 `ADAPTERS`，`default_executable` 为空。`register_found_runtimes` 跳过它：没有一个叫 `acp` 的默认二进制。开关关着、命令为空、或模型名和账户标签缺一：不登记、选择器拒绝、配置导入跳过并写明试点未开。开关打开且命令、模型、账户都齐：登记内置行，Profile 才能选类型 `acp`。

改命令或关开关不追停已经在跑的那一轮，也不删除已经建好的 Profile 行。关开关之后选择器不再把它当成可选用。下一轮 launch 再读。开关已关或命令不再满足第 6.3 节时，这一轮报错，不回退（第 7.3 节）。

### 7.2 谁能选

`roles = {reviewer}` 之后，`check_roles` 会拒绝把这个 Profile 用于 manager、planner、worker。它挡不住「角色就是 reviewer」的入口。同一运行时守卫照旧：Reviewer 的运行时 id 不能和开发者相同。

试点期在服务端守住下面每一处。拒绝都是明确错误，不改用另一个 Profile。

| 入口 | 试点期 |
|---|---|
| 控制台 | 操作者写命令、打开开关、建 Profile、把 Reviewer 角色指到它。关着时运行时列表里不出现可选用的 `acp` |
| 管家工具 `looper_agent_profiles`、`looper_settings` | 创建、更新或选择类型 `acp` 时拒绝 |
| Planner `looper_task_profile_set` | 拒绝。实现是 `_profile_set`（`ops/planner.py:479`）。`PLANNER_CHOICE_ROLES` 含 reviewer（`agent_settings.py:175`），所以 `roles = {reviewer}` 不会拦住它。`choosable_profiles`（`projects.py:1378`）不把类型 `acp` 放进可选列表 |
| Planner `looper_task_role_set`、任务角色、任务模板、workflow | 拒绝把 `acp` Profile 写进角色定义或模板。模板自带的 profile 若是 `acp`，这一次定义报错，不静默改用内置角色（今天模板 profile 会跟着生效，见 `_role_set`，`ops/planner.py:735-738`） |
| 配置导入 | 类型 `acp` 的 Profile，以及把它选进角色层、模板或 fallback 的条目，都跳过并写明原因。Profile 导入会调用 `check_runtime`（`config_transfer.py:1448`）。导入不打开开关，也不把目标环境上的 Reviewer 指到 `acp`。运行时行仍按第 2.7 节保留目标环境的 executable |
| fallback | 保存时，`fallback_profile_id` 的两端有一端是 `acp` 就拒绝。启动时 `WorkerPool._execute` 在 `worker.py:2351` 调用 `_fallback_profile`（定义在 `2375`），后者调用 `fallback_settings`（`agent_settings.py:830`）。这一跳的两端有 `acp` 时不换 Profile，这一轮报错 |
| Telegram、飞书 | 不新增命令。`/profile` 不能把 Reviewer 换成 `acp`，因为试点期选择器拒绝这个类型 |
| CLI | 不新增参数 |
| launch | 解析出类型 `acp` 之后再读 `acp_reviewer.enabled`、命令和只读策略。开关已关、命令为空、模型或账户对不上、策略装不上：`run_failed`，错误写明哪一项。不回退。这覆盖「先绑上 ACP，再关开关」 |

渠道和 CLI 在试点期没有新界面，是因为这条命令能启动操作者给出的任意进程，登记权留在控制台。评测通过之后，若要让现有的角色选择器选它，另开一次变更；`roles` 仍限制只有 Reviewer，第 7.2 节的服务端限制要逐条重新决定，不能因为 `roles` 只含 reviewer 就视为已经守住。

### 7.3 对用户可见的行为

开关关着，且没有任何 Profile 的运行时类型是 `acp`：Prompt、工具、命令行和行为与今天逐字节相同。已经存在 `acp` 绑定时，下一次 launch 报错，不换回别的运行时。

开关开着且 Reviewer 的 Profile 选了它：只有这个角色的运行换成 ACP 进程。规划者、Worker、管家不变。每次 launch 再读开关、命令和只读策略。任一项不满足，或 ACP 评审得到 `run_failed` 或 `contract`：这一轮报错，规划者按今天的失败路径处理。不换回 Claude Code，也不换回原来的 Grok headless，也不跟随 `fallback_profile_id`。

## 8. 评测

### 8.1 用例

复用 `evals/prompt_regression` 里 `READ_ONLY_TASKS` 的四个 Reviewer 任务（`evals/prompt_regression/tasks.py:1525-1623`；说明在 `evals/README.md:54-57`）：

| 任务 | 脚本期望 | 评审次数 |
|---|---|---|
| `reviewer_retry_note` | 1 次评审，1 次重试；修正后的工作要通过 | 1 |
| `reviewer_unrelated_note` | 2 次评审，2 次重试；缺区域的那次不得通过 | 2 |
| `reviewer_elsewhere` | 1 次评审，0 次重试；摘要在另一个 worktree | 1 |
| `reviewer_elsewhere_wrong` | 2 次评审，1 次重试；第一份少区域，必须先失败 | 2 |

按各任务 expect 的评审次数，一轮四个任务共 6 次评审（1+2+1+2）。`--reps 2` 则一侧 12 次，基线与 ACP 两侧共 24 次真实 Reviewer 运行。这是各任务都落在 expect 里的次数。额外重试会再增加调用，24 不是上限。规划者和 Worker 由脚本扮演（`real_reviewer: True`），不计入模型账单。

今天的 harness 做不到把这 12 次指到任意运行时。`fake_claude.py:68-72` 写明 `real_reviewer` 时只有 Reviewer 是 Claude Code。`runner.py:246-281` 的 `require_claude` / `install_claude` 把真实二进制钉在 `claude` 上。后续 PR 给评测一个开关，让 `real_reviewer` 使用类型 `acp` 的 Profile，规划者和 Worker 仍走脚本。本 RFC 不改 harness。

假模式和 `REVIEWER_RUN_COST = 0.001`（`fake_claude.py:90-91`）只检查管道，不作为模型证据，也不当账单。

基线是同一套任务、同一次评测启动前，用现有 Reviewer 运行时（harness 今天就是 Claude Code）跑的那一侧。两侧的脚本、重复次数、工作目录策略相同。只有 Reviewer 的运行时不同。

### 8.2 回归门槛

`--reps 2` 是这四个固定用例的试点回归门槛。先判定基线有效，再看 ACP。双方都是 `0/2`、同样误放行、或同样 `run_failed`，不算过关。

**有效基线（比较开始之前）：**

- 四个任务各自 `2/2`：两次重复都通过该任务的 `check()`。
- harness 的行里没有 `outcome = error`（`evals/prompt_regression/runner.py:99-102`），没有 `skipped`（同文件 930 行一带），结果文件没有 `aborted`（同文件 1000、1171 行）。
- 误放行、`outcome = contract`、`outcome = run_failed` 都是 0。这三项是绝对零门槛，不跟一个失败的基线比「少于或等于」。

基线不满足上面任一条：这次比较没有结果。不把 ACP 配成同样的失败记成通过，也不用这份基线去放行 ACP。

**然后看 ACP，用同一组绝对条件：**

- 四个任务各自 `2/2`。
- 同样没有 `error`、`skipped`、`aborted`。
- 误放行、`contract`、`run_failed` 都是 0。误放行指脚本里应该失败的那次尝试被记成 `metadata.outcome = passed`。
- 每个任务的评审次数落在该任务 expect 的最大次数里。超出 expect 的重试使调用次数高于第 8.1 节的 24，这一侧不算过关。

**安全（比今天严）：** 写保护是第 6.3 节的进程策略，评测开始前策略必须已经能装上。每次评审前后再看 git status 与一个 canary 文件，覆盖工作区和 Git 元数据。出现写则试点失败，这次比较作废，即使质量条件都满足。HEAD 没动但工作区脏了，也算写。canary 用来发现策略被绕过。

**花费（单独一条，不参与质量通过）：** 报告每次评审的 `cost_usd`。任意一次为空，都默认阻止把这个运行时用于试点以外的 Reviewer。操作者书面豁免时，报告分开写已知 `cost_usd` 的合计，以及花费未知的次数。已知子集不是总成本。有数字之后，推广与否看那份报告，不在本 RFC 里预设美元上限。门槛与第 6.5 节、D14 是同一句。

### 8.3 真实花费的数量级

评审用例的美元数**待验证**。本 RFC 不跑模型。能引用的只有：

- 假模式的 `REVIEWER_RUN_COST` 是 0.001，不是账单。
- Grok 运行时 RFC 第 14.8 节第 2 项：2026-10-18，管家 ACP 冷启动一轮约 $0.074，同进程第二轮约 $0.0093（`grok-4.7`，effort low，含 MCP）。那是管家会话，不是这四个评审任务，也不能当作 Reviewer 单次的上界。

条件估算，只在「一次 ACP 评审的花费不超过那一次管家冷启动」时成立：按第 8.1 节的期望次数，ACP 一侧 12 次约 12 × $0.074 = $0.888。Claude 基线另计。超出 expect 的重试再加调用。这不是上界，也不是通过线。排真实评测之前，先冻住模型和 effort，抽一次样，再设实跑预算。

## 9. 发布、回滚、风险

**发布：** 合并顺序见第 10 节。类型进仓库之后开关仍关。操作者在丢弃的数据库上打开，把 Reviewer 指到一条 argv，先跑第 8 节。通过之前，发布说明里只写「实验，默认关」。

**回滚：** 把 `acp_reviewer.enabled` 设回非 `true`，或把 Reviewer 的 Profile 换回原来的运行时。正在跑的那一轮取消（第 6.7 节）。下一轮 launch 看到开关已关就报错，不起 ACP 进程，也不跟随 fallback。不需要迁移；不改已有运行时的行。

| 风险 | 结果 | 发现方式 |
|---|---|---|
| CLI 不请求权限就写盘，或子进程之外的服务写盘 | 策略装不上或命令不能保证时，进程不起 | 第 6.3 节拒绝启动。评测 canary 再查一次；一次写就停试点 |
| v2 客户端把 `session/prompt` 当成结束 | 回合挂到超时 | 版本不是 1 就拒绝；超时是 `run_failed` |
| 用量只在 `_meta`，或价目对不上声明的模型 | `cost_usd` 为空 | 任意一次为空就挡住试点以外的推广（第 6.5 节） |
| 操作者 argv 指到非 ACP 进程 | 启动失败 | `run_failed`，不回退 |
| Planner、模板、导入或 fallback 把 `acp` 带到别的角色，或在开关关上之后继续用 | 错误角色跑了任意进程，或关开关之后仍在跑 | 第 7.2 节逐条拒绝。launch 再查一次开关。`roles = {reviewer}` 单独不算守住 |
| 架构文档与 Grok `steer` 不一致 | 读者以为 Grok 不能 steer | 本 RFC 指到 `grok.py:181-185`。不在这次改架构文档 |
| 评测 harness 仍把真实 Reviewer 钉在 Claude Code | 没有对照数据就推广 | 第 10 节的评测 PR 完成之前，不把 `acp` 放进默认 Profile |

入口：控制台有新的操作者设置；Telegram、飞书、CLI 在试点期无新命令（第 7.2 节）。缺了评测开关的话，交付只到「能配置、默认关」，不能声称验收通过。

## 10. PR 拆分

1. **本文件。** 只加这篇 RFC。开关还不存在。
2. **首次可启用。** `acp` 适配器、覆盖 `run_channel` 的 v1 会话、设置、命令身份与配对、进程级只读边界、默认拒绝的权限回调、事件与 `cost()` 接线、launch 时复查开关、禁止进入或离开 `acp` 的回退。开关默认关。这一变更是第一次允许把 `acp_reviewer.enabled` 设为 `true`。边界不在同一变更里时，这个值不能生效：setter 拒绝，launch 也拒绝。不含评测对照。
3. **评测开关。** `real_reviewer` 可以指向 `acp` Profile，跑第 8.1 节的四个任务，写出第 8.2 节的对照。真实运行的花费在这一 PR 的说明里报，不写进仓库。本 RFC 不跑模型。

## 11. 维护者的答复（2026-11-22）

维护者对下面 8 个问题全部按推荐答复；每条记为一个决定（D10–D17，和 Q1–Q8 顺序对应）。

| 决定 | 问题 | 答复（维护者） | 理由 |
|---|---|---|---|
| D10（Q1） | 第一台 CLI 用谁？ | Grok `agent stdio`。客户端仍不 import `grok_acp`。第二台用同一客户端跑通之前，不能下「通用适配器可以取代专用 runner」的结论 | 它是仓库里唯一有代码和 2026-10-18 实跑的 ACP 说话方。本机帮助文本还广告了 coco、cursor-agent、omp、dsh 的 ACP 入口，但都没握手 |
| D11（Q2） | 谈判 v1 还是 v2？ | 钉 v1。其他版本拒绝 | 同 `grok_acp.py:641-643`。v2 等有 CLI 被验证只用 v2、并且回合结束方式写进下一版 RFC 再做 |
| D12（Q3） | 试点带不带 Reviewer steering？ | 不带 | Reviewer 只要 `run`。`steer` 留在各专用适配器上 |
| D13（Q4） | ACP 起不来时回退吗？ | 不回退。进入或离开 `acp` 的 fallback 都拒绝。开关在绑定之后关掉，下一次 launch 报错，不换运行时 | 同 API 运行时 D8 |
| D14（Q5） | 花费未知算不算试点失败？ | 质量与花费分开，未知花费不让 `judge_result` 失败。任意一次 `cost_usd` 为空，都默认阻止试点以外的推广。书面豁免时分开报告已知合计与未知次数，已知子集不是总成本 | 质量判定不读 `cost_usd`。记 0 会把未知花费当成没有花费（D6） |
| D15（Q6） | 管家或 Planner 能不能代操作者选择已建好的 `acp` Profile？ | 试点期不能。`looper_task_profile_set`、`looper_task_role_set`、任务模板、workflow 和配置导入都拒绝。操作者在控制台登记并指定 Reviewer | 之后若开放，逐条重新决定第 7.2 节的入口；`roles` 仍只有 reviewer，并且单独不算守住 |
| D16（Q7） | 只读收到和今天的 Reviewer 一样（仅提示），还是进程级禁止写？ | 进程级只读边界（D4）。权限回调默认拒绝未知操作，canary 只做补充 | 今天的 Reviewer 是 `FULL`。试点的安全条比现状严 |
| D17（Q8） | `session/new` 要不要注入 Looper MCP？ | 不注入。`mcpServers: []` 只停止 Looper 注入 | 关不掉 CLI 自带的 MCP（第 6.4 节）。Reviewer 试点只核对仓库里的工作。平台工具另说 |
