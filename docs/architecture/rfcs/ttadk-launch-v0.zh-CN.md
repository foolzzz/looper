# RFC：TTADK 启动模式 v0（`ttadk-codex` 与 `ttadk-cursoragent`）

- **状态：** 已批准（2026-10-30），按第 5 节的推荐决定实施；第 11 节的三个待决问题先按推荐默认值执行，维护者可随时调整。
- **需求（维护者，2026-10-30）：** 开发要支持配置 TTADK 模式，codex 和 cursor-agent 都要。TTADK 是组织要求的开发模式，会记录工作量。agent 要能配置成 `ttadk-codex` 和 `ttadk-cursoragent` 两种模式。
- **相关：** [多运行时](multi-runtime-agents-v0.zh-CN.md)（P1 登录链接、P5 续接规则）、[Grok 运行时](grok-runtime-v0.zh-CN.md)（厂商 CLI 加自有登录的接法）、[Worker 引导](worker-steering-v0.zh-CN.md)（Codex `app-server`）、[开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 1 节（测试与验证方式）。
- **核对基线：** `main` `6a3cbb94e`（v2.14.26，代码与 `181743944` 相同）。外部工具：TTADK 1.0.27（npm 包，只读源码）、Codex CLI 0.162.0、Cursor CLI `cursor-agent` 2026.10.01-14929f9（只读 `--help` 和随包的 JS）。实跑见第 2 节，共 3 次 Codex 模型调用、0 次 Cursor 模型调用。
- **引用约定：** 仓库文件写「路径:行」，相对仓库根。TTADK 写成 `ttadk:<包内路径>:<行>`（`dist/` 下）；Cursor CLI 是压缩过的单文件 bundle，写成 `cursor:index.js@<字符偏移>`。
- **术语：** **启动器（launcher）** 指运行时的可执行文件怎样被启动：`native` 直接启动，`ttadk` 经 TTADK 启动。**TTADK 网关**指 TTADK 给 Codex 注入的模型网关 provider。**前导输出**指 TTADK 在拉起工具之前写到 stdout 的横幅和状态行。

## 1. 需求与目标

### 1.1 背景

TTADK 是组织使用的 AI 编码工具包装器：`ttadk code -t <工具>`（别名 `ttadk codex`、`ttadk cursor-cli`）先做登录和准备，再把 Codex、Cursor CLI 等工具作为子进程拉起，让这次使用计入组织的 AI 贡献和工作量统计。Looper 现在直接启动 `codex` 和（尚未接入的）`cursor-agent`，这些运行不进统计。

### 1.2 目标

- Agent Profile 能选 `ttadk-codex`、`ttadk-cursoragent` 两种模式，控制台、管家工具和 `loopercli` 用现有的 `runtime_id` 字段就能选，不加新字段。
- 经 TTADK 的运行与原生运行用同一个适配器、同一份隔离、同一种事件格式；四类角色和咨询能用的范围与原生相同（第 7 节）。
- 失败时说清原因和原生的替代，**从不悄悄退回原生启动**（那样会丢掉工作量记录）。
- 每次运行的记录写明它经过了 TTADK、TTADK 的版本和模式。
- 新增原生的 `cursor-agent` 运行时（Cursor CLI），`ttadk-cursoragent` 建在它上面。
- 不配 TTADK 的安装，命令、prompt、事件和记录逐字节不变。

### 1.3 不做

- 不用 `ttadk --app`、`--wrapper`、`-p` 代理模式：前两个会改写 `~/.codex/config.toml` 或 IDE 设置（`ttadk:core/ai-tools/profiles/codex.js:56-105`、`ttadk:core/ai-tools/profiles/codex-wrapper.js:374-459`）。
- 不做 TTADK-only 模式（SSO 登录、网关自己的模型）：它需要新的 provider 和模型目录，见第 11 节问题 3。
- 不绕过 TTADK 自己拼网关参数（`ttadk get_params` 加手写 `-c`）：那依赖 TTADK 的内部布局，不是它公开的契约，而且不发 TTADK 的启动事件。
- 不改操作者的 TTADK 设置（`~/.ttadk/setting.json`）、Cursor 配置和钥匙串。
- v0 的 `cursor-agent` 不接 API key、不走 ACP 常驻进程（第 6.6 节）。

## 2. 实测与源码证据（2026-10-30）

### 2.1 怎么测的

- Codex：一个照 Looper 登录家目录布局写的临时 `CODEX_HOME`（`config.toml` 与 `runtimes/codex.py:477-500` 的登录配置相同，`auth.json` 是指向操作者登录文件的符号链接，没有读取或复制它），工作目录是一个不在 git 仓库里的临时目录。进程按 Looper 的方式启动：`start_new_session=True`，stdin/stdout/stderr 都是管道，提示写进 stdin 后关闭（同 `proc_tree.py:335-340`、`runtimes/per_turn.py:261-289`）。
- Cursor：临时 `HOME`，一个 `--plugin-dir` 插件目录（规则文件 + 一个本地 stdio MCP 探针服务器），提示从 stdin 读。
- 没有用 `--app`、`--wrapper`；没有改 `~/.codex/config.toml`（实测前后 `CODEX_HOME` 里的 `config.toml` 哈希不变，操作者的 `~/.cursor/cli-config.json`、`mcp.json` 修改时间不变）；没有用真实 `HOME` 跑 Cursor（那会执行 `~/.claude/settings.json` 里的 hook，见 2.4）。

### 2.2 Codex 经 TTADK（✅ 实跑）

| # | 命令（省略 `env CODEX_HOME=<home>`） | 结果 |
|---|---|---|
| P1 | `ttadk codex --skip-check --mode mixed -- exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -m gpt-6.1-sol -c model_reasoning_effort="low" -`，提示 "Reply with exactly: pong" 从管道写入 | ✅ 回复 `pong`。stdout 先有 **19 行非 JSON 前导输出**（横幅、`Login successful. Welcome, <邮箱>`、`Codex mode: Mixed (ChatGPT subscription + TTADK)`、两行"没有仓库信息，无法统计 AI 贡献率"、一行模型提示），然后是完整的 `thread.started`、`turn.started`、`item.completed`、`turn.completed`。冷启动：TTADK 的准备用了 17.8 秒，`thread.started` 在 19.9 秒；`turn.completed` 之后 4.0 秒 stdout 才结束。退出码 0。`config.toml` 没被改 |
| P2 | 同上，加 `resume <P1 的 thread>` | ✅ 续接成功（同一个 thread id，输入 token 从 13329 涨到 26676，历史在）。热启动：准备 3.7 秒，`thread.started` 在 7.0 秒；`turn.completed` 之后 2.0 秒结束。进程树：`node ttadk` → `node codex`（npm 包装）→ 原生 `codex` → Codex 的 `zsh`，前三个在 TTADK 的进程组里，`zsh` 自建进程组 |
| P3 | 原生 `codex exec … resume <同一 thread> -` | ✅ 原生续接经 TTADK 建的会话成功，缓存命中 38400/40043 token，说明 mixed 模式背后是同一个 ChatGPT 订阅后端。原生 `thread.started` 在 0.67 秒 |
| P4 | `ttadk codex --skip-check --mode mixed -- app-server`，stdin 发 JSON-RPC `initialize`（不调模型） | ✅ 跳过 19 行前导输出后，5.05 秒收到 `{"id":1,"result":{…,"codexHome":<home>}}`：stdin 和 stdout 原样透传，`CODEX_HOME` 也照样生效。向进程组发 SIGTERM：node 立即以信号退出（-15），整棵树 0.5 秒内全部结束 |

P2 的进程树里能看到 TTADK 注入给 codex 的参数，都在 Looper 的参数之前：`-c model_provider="ttadk"`、`-c model_providers.ttadk.base_url=<网关>`、`wire_api="responses"`、`requires_openai_auth=true`、`env_http_headers.X-Source=…` 等。

### 2.3 TTADK 源码要点（只读，未实跑的标"推断"）

| 事实 | 依据 | 对 Looper 的影响 |
|---|---|---|
| `--version`/`-v` **出现在 argv 任何位置**都只打印 TTADK 的版本并退出 | `ttadk:index.js:12-14`、`:29-31` | 探测 TTADK 版本用 `ttadk --version`；Codex 版本不能经 TTADK 问，Looper 传给工具的参数里不能有 `-v` |
| `code` 和别名命令 `allowUnknownOption`，但不 `passThroughOptions`：TTADK 自己的选项（`-m`、`-p`、`-h`…）出现在哪都会被吃掉，除非放在 `--` 之后 | `ttadk:core/command/code.js:251-268`；commander `lib/command.js:1777-1860` | 工具参数一律放在 `--` 之后。否则 Codex 的 `-m` 被当成 TTADK 的模型、`-p` 被当成代理模式 |
| `-a "<参数>"` 用简化的 shell-words 拆分：空参数消失，引号里不能转义 | `ttadk:core/utils/string-utils.js:177-225` | 不用 `-a` |
| 工具用 `cross-spawn`，`stdio:'inherit'`、`detached:false`、`shell:false`；node 留作父进程 | `ttadk:core/command/code.js:1183-1188` | stdin/stdout 原样透传（P4 实测）；工具和 node 在同一进程组，`proc_tree` 的整树结束照常有效 |
| 拉起工具之前，横幅和状态行**总是**写到 stdout，没有开关 | `ttadk:index.js:36-40`、`ttadk:core/command/code.js:909`、`:933`、`:947`、`:96-97` | Looper 的两个读取器本来就跳过非 JSON 行（`looper/agents/claude_cli.py:422-437`、`runtimes/codex_steer.py:138-143`），不用改 |
| 工具退出后 TTADK **总是以 0 退出**；准备阶段失败以 1 退出 | `ttadk:core/command/code.js:1232-1238`、`ttadk:core/utils/error-utils.js:39-73` | 成败只看事件。Worker 本来就把"没有 result 事件"判为失败（`looper/agents/tiers/worker.py:212`、`:256-258`） |
| SIGTERM/SIGINT 时 node 由 `signal-exit` 立即以该信号退出，**不转发给工具** | `proper-lockfile` 的 `signal-exit/index.js:111-134` | 只杀 node 会留下孤儿；Looper 按进程组和整树结束，不受影响（P4） |
| `--skip-check` 跳过版本检查、升级提示（inquirer，不看 TTY）和插件同步 | `ttadk:core/command/code.js:571-575`、`:1521-1592` | 必带 `--skip-check` |
| 仍会联网校验 SSO；缓存的令牌不可用时进入**设备授权**：在 stdout 打印网址、**打开浏览器**、阻塞等待 | `ttadk:core/utils/sso.js:523-645`、`ttadk:core/command/code.js:880-906` | 启动前先做不交互的登录检查（`ttadk models -t <工具> -f json`，失败时报"没有缓存的 SSO 凭据"，不进设备授权：`ttadk:core/command/models.js:29-42`） |
| `--mode auto` 用 `codex login status` 判断（遵守 `CODEX_HOME`）：ChatGPT 登录 → mixed，否则 → TTADK-only；TTADK-only 不给 TTADK 的 `-m` 时弹出**交互式模型选择**，它从 stdin 读按键 | `ttadk:core/ai-tools/profiles/codex-runtime.js:77-147`、`ttadk:core/utils/model-selection.js:26`、`ttadk:core/command/code.js:1029-1036` | 推断：管道上的提示会被当成按键吃掉。所以**不用 auto**，显式 `--mode mixed`：登录不是 ChatGPT 时 TTADK 以错误退出（`E_CODEX_MIXED_REQUIRES_CHATGPT`，`code.js:848-858`），失败即关闭 |
| mixed 模式注入的网关 provider 用 `requires_openai_auth=true` 带 ChatGPT 令牌，另加 SSO 头和订阅头；命令行 `-c` 优先于 `config.toml` | `ttadk:core/ai-tools/profiles/codex-wrapper.js:76-119`、`ttadk:core/ai-tools/profiles/codex.js:31-55` | Looper 给 API key provider 写的 `model_provider = "looper"` 会被覆盖：TTADK 模式只配 `codex-login` |
| 子进程环境多出 `TTADK_OPENAI_API_KEY`（SSO 令牌）、`TTADK_HEADER_*`、`EXEC_SOURCE=ttadk`、`TTADK_LAUNCH_ID` | `ttadk:core/ai-tools/env.js`、`ttadk:core/command/code.js:1160-1174` | agent 的 shell 不该拿到 SSO 令牌：Codex 默认排除名字含 `KEY` 的变量（推断），Looper 再显式排除一次 |
| 工作量的主记录是网关上的**每次请求**：请求头带 `X-Source: ttadk`、`X-Project-Root: <cwd>`、`X-Repo-Info`（cwd 附近带 `origin` 的 git 仓库）、`X-TTADK-Launch-Id` | `ttadk:core/utils/sso.js:861-874`、`ttadk:core/utils/llm-request-headers.js:156-175`、`ttadk:core/utils/git-utils.js:434-572` | 推断：已发出的请求在运行被停止时也已记下。cwd 不在仓库里时只记工作量、不记贡献率（P1 的警告）。Worker 在任务的仓库或 worktree 里跑，有仓库信息；管家的工作目录通常没有 |
| 另有一个启动事件 `ttadk_code`，在工具结束时发出；`CI=1` 或 `TTADK_NO_TELEMETRY` 会关掉它；node 先被杀则不发 | `ttadk:core/telemetry/tracker.js:46-49`、`ttadk:core/telemetry/event-api.js:59-79` | **不设 `CI=1`**（与原先笔记的做法相反）。它的代价是工具退出后 node 还要发一次请求（P1、P2 的 2-4 秒） |
| `CI=1` 另外只关掉 shell 补全的自动安装；stdin 不是 TTY 时这一步本来就跳过，也可用 `TTADK_DISABLE_COMPLETION_AUTO_INSTALL=1` 关 | `ttadk:core/command/completion-auto-install.js:73-95` | Looper 设 `TTADK_DISABLE_COMPLETION_AUTO_INSTALL=1` 兜底，不碰操作者的 `~/.zshrc` |
| `cursor-cli` 的命令默认是 **`agent`**，找不到时才用 `cursor-agent`；可在 `~/.ttadk/setting.json` 的 `ai_tool_commands.cursor-cli` 改 | `ttadk:core/types.js:93`、`ttadk:core/ai-tools/profiles/cursor-cli.js:36`、`ttadk:core/command/code.js:1178-1206` | `agent` 也可能指向 Grok 等其他工具；即使设置为 `"cursor-cli": "agent"`，也不能据名字判定它是 Cursor。不处理的话，`ttadk code -t cursor-cli` 会拉起 Grok（第 6.7 节） |
| `cursor-cli` 没有任何注入（不改 MCP、不注入网关），只有 `EXEC_SOURCE`、`TTADK_LAUNCH_ID`、`PATH`；源码注释写"User handles tracking"，除 `ttadk_code` 启动事件外不记任何东西 | `ttadk:core/ai-tools/ai-tool-registry.js:169-175`、`ttadk:core/command/code.js:79`、`:1044-1048` | `ttadk-cursoragent` 的"工作量记录"只有那个启动事件，见第 11 节问题 1 |
| TTADK 的设置和 SSO 缓存在 `~/.ttadk/`（推断：经 node 的 `os.homedir()`，在 macOS/Linux 上取 `HOME`） | `ttadk:core/config-manager/config-manager.js:246-283` | 给 Cursor 换 `HOME` 时不能一起换掉 TTADK 的，否则 TTADK 找不到登录、进入设备授权（第 6.7 节） |

### 2.4 Cursor CLI 2026.10.01（📄 只读 bundle 和 `--help`；❌ 实跑没有走到模型）

| 事实 | 依据 |
|---|---|
| headless：`-p --output-format stream-json`；没有位置参数且 stdin 不是 TTY 时，从 stdin 读到 EOF 作为提示（单轮） | `--help`；`cursor:1962.index.js@9564`、`@15810` |
| 事件：`system/init`（`session_id`、`model`、`cwd`、`apiKeySource`）、`user`、`assistant`（文本）、`tool_call` 的 `started`/`completed`（`call_id`、protobuf JSON 形式的 `tool_call`，如 `shellToolCall`、`editToolCall`、`mcpToolCall`）、`thinking/*`、`result/success`（`duration_ms`、`result`、`session_id`，可选 `usage: {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens}`）。**不报花费**；出错时没有 `result`，错误在 stderr，退出码 1 | `cursor:1962.index.js@557426`（`headless.ts`）、`cursor:index.js@5644003` |
| 会话：`session_id` 就是 chatId；`--resume <chatId>` 可配 `--print`，要在同一个工作目录续接；会话存在 `${CURSOR_CONFIG_DIR}/chats/<md5(cwd)>/<chatId>/store.db` | `cursor:index.js@4335769`、`cursor:1376.index.js@65` |
| 审批：headless 下不加 `--force` 一律拒绝；团队可禁用 headless 或 "Run Everything"；未信任的目录退出，`--trust` 放行 | `cursor:3129.index.js@256`；Cursor 公开文档 permissions、headless |
| MCP：用户级 `mcp.json` 写死在 `os.homedir()/.cursor/`，**不跟 `CURSOR_CONFIG_DIR` 走**，没有参数指定别的文件；插件根目录的 `.mcp.json` 也会加载，且不需要审批（只有项目级要审批） | `cursor:index.js@4334857`、`cursor:3129.index.js@13396` |
| `--plugin-dir` 认 `.cursor-plugin/plugin.json` 或 `.claude-plugin/plugin.json`；插件可带 `rules/<名>.md`（frontmatter `alwaysApply: true`）、`commands/`、`skills/`、`.mcp.json` | `cursor:index.js` 插件安装与发现代码（`U=[".cursor-plugin/plugin.json",".claude-plugin/plugin.json","plugin.json"]`） |
| 用真实 `HOME` 时会读 `~/.claude/settings.json` 的 hook 并执行、读 `~/.claude/commands`、把 `~/.claude/plugins/marketplaces` 登记到 Cursor 账号 | `cursor:190.index.js@73836`、`cursor:1877.index.js@65`、`cursor:index.js@4528959` |
| 启动脚本 `exec` 成 node，PID 就是 node；SIGTERM 退出码 143；shell 工具的命令 `detached:true`，各自一个进程组 | `cursor:index.js@2121845`、`@7849889` |
| 只能用 Cursor 后端，不能接任意 OpenAI/Anthropic 端点 | `cursor:index.js@2520064` |
| 凭据：macOS 默认在登录钥匙串；`AGENT_CLI_CREDENTIAL_STORE=file` 改用 `~/.cursor/auth.json`（按 `HOME`），`memory` 只在进程内 | `cursor:index.js@2665134`、`@4278587` |
| `<data>/projects` 路径超过 84 个字符时，worker 套接字放到 `/tmp/.cursor/<slug>` | `cursor:index.js@4336419` |

Cursor 的两次实跑（都没有调用到模型）：

| # | 做法 | 结果 |
|---|---|---|
| 隔离 HOME 实验 | 临时 `HOME`，其他照常 | ❌ `Error: Authentication required. Please run 'agent login' first`，退出码 1：**换了 `HOME` 就找不到钥匙串里的登录** |
| 钥匙串目录链接实验 | 临时 `HOME`，另把 `~/Library/Keychains` 链接进去 | ❌ 240 秒内 stdout 没有任何输出，MCP 探针没被启动，被超时结束；原因未查明（推断：钥匙串访问在等待授权）。不能作为方案 |

未核实、要在实现时补测的 Cursor 事实，以及各自的核实步骤，列在第 10.2 节。

## 3. Looper 现状

### 3.1 运行时怎样定义和启动

| 方面 | 现在 | 对 TTADK 的含义 |
|---|---|---|
| 运行时类型 | 代码里一个类型一个适配器（`RuntimeAdapter`），`ADAPTERS` 按类型 id 登记（`looper/agents/runtimes/__init__.py:546-561`）。适配器负责能力、协议、认证方式、探测、绑定 provider、拼命令、解析事件和计费（同文件 `:314-536`） | 包一层启动器不需要新的事件格式，只改命令前缀、环境和探测 |
| 运行时实例 | `agent_runtimes` 的一行是"某类型 + 可执行文件 + 最近一次探测"（`looper/db/schema.sql:17-32`）。现在只有内置行：`rt-claude-code` 随 schema 建，其他类型在默认可执行文件出现在 PATH 上时注册为 `rt-<type>`（`looper/agents/profile_registry.py:1116-1142`）。v1 只有 `registry.view` 和 `runtimes.probe`，Agent Runtimes 页只读（`looper/agents/v1_domains/registry.py:130-138`） | 新增"同一类型、经 TTADK 启动"的内置行，沿用注册和探测，不加写接口 |
| Agent Profile | 引用一行运行时和一个模型，另有 effort、agent teams、自定义 prompt（`profile_registry.py:356-393`，`schema.sql:107-119`）。控制台、管家工具（`looper/agents/ops/config.py:236`）和由 OpenAPI 生成的 `loopercli`（`looper/loopercli.py:5`）都用 `runtime_id` 建档 | 选 `runtime_id = rt-ttadk-codex` 就是"这个 agent 用 ttadk-codex 模式" |
| 可执行文件 | 只在 `Launch.executable()` 一处决定：运行时行的 `executable`，否则类型的默认值（`looper/agents/agent_settings.py:932-938`）。管家、规划者、Worker、咨询都经它（`tiers/manager.py:2437`、`tiers/planner.py:3306`、`tiers/worker.py:1021-1032`、`consult.py:371-377`） | TTADK 行的默认可执行文件是 `ttadk`，在这里按启动器取 |
| 子进程与结束 | `proc_tree.spawn` 以 `start_new_session=True` 启动，三个管道（`proc_tree.py:335-340`、`tiers/worker.py:365-369`）。结束时先读一次 `ps` 快照，向整棵树和树里进程自建的进程组发 SIGTERM，3 秒后 SIGKILL（`proc_tree.py:1-27`） | 多一层 node 不影响结束（P4） |
| 续接 | `can_resume` 只看运行时类型和 provider（`runtimes/__init__.py:564-574`）；会话行记 `runtime_type`、`provider_id`、`profile_id`（`schema.sql:331-344`） | 类型不变、provider 不变，经 TTADK 与原生之间切换会续接；P3 证明这在 mixed 模式下可行（第 5 节 D5） |
| 运行记录 | `Launch.record()` 写进每次运行的 metadata：profile（含运行时 id、名称、类型）、模型、计价规则，再加适配器的 `launch_record`（`agent_settings.py:1004-1043`） | 启动器、TTADK 版本和模式在这里留痕 |
| 按类型的名单与目录 | 会话回收按 `(类型, 目录)` 判断 `.local/<type>/sessions`（`looper/agents/read_only_copies.py:182-208`）；办公台的会话保留（`looper/agents/room.py:1139-1150`）；咨询的只读名单（`consult.py:79`）；Codex 登录检查写死 `.local/codex`（`runtimes/codex_login.py:162-167`） | 新类型要逐一登记；同类型的第二行则一处都不用改 |

### 3.2 Codex 适配器

- 一次运行：`env CODEX_HOME=<Looper 的目录> codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -m <模型> [-c model_reasoning_effort=…] [--add-dir …] [--ephemeral] -`，提示写进 stdin 后关闭（`runtimes/codex.py:189-253`、`:296-303`）。
- 能被引导的 Worker（探测到 `steer`）改用 `codex app-server`，JSON-RPC 按行走 stdio（`codex.py:255-275`、`runtimes/codex_steer.py:100-143`）。`steer` 由探测时的 `app-server generate-json-schema` 决定（`codex.py:129-164`）。
- 管家、规划者每轮一个 `exec` 进程续接（`ResidentCodex`，`codex.py:911-958`）。它要求命令以 `env CODEX_HOME=<home> <可执行文件>` 开头，用第三个元素做 `shutil.which` 检查（`codex.py:935-940`、`runtimes/per_turn.py:192-193`）。
- 隔离：`CODEX_HOME` 在 `<runtime_root>/.local/codex/` 下，只放 Looper 写的 `config.toml` 和 `AGENTS.md`；用 ChatGPT 登录时 `auth.json` 是指向操作者登录文件的链接（`codex.py:435-500`、`:693-724`；`codex_login.py:67-73`）。API key provider 写 `model_provider = "looper"`；ChatGPT 登录不写。
- 平台工具：Looper 的 MCP 服务器（`python -m looper.cli mcp-server`，带地址和令牌，`looper/agents/ops/service.py:678-681`）写成 `[mcp_servers.looper]`（`codex.py:401-432`、`:509-518`）。
- 用量：一轮结束时报 token，Looper 按 provider 价格折算（`codex.py:321-324`、`:766-830`）。

### 3.3 各角色对 CLI 运行时的要求

管家、规划者要 `resident_session` 和 `resume`，Worker、Reviewer 要 `run`（`runtimes/__init__.py:177-182`）。管家、规划者的平台工具在 CLI 运行时上全靠 MCP。管家的"停止"只在能中断的常驻会话上提供（`can_interrupt`，同文件 `:351-354`）；逐轮续接的运行时拒绝停止，不会改成杀进程。咨询要能分叉一次已结束运行的会话并只读运行（同文件 `:447-452`）。

## 4. 配置形态：候选方案与取舍

| | 方案一：Agent Profile 加 `launcher` 字段 | 方案二：新运行时类型 `ttadk-codex`、`ttadk-cursoragent` | 方案三（推荐）：同一类型的第二行内置运行时，行上有类型化的 `launcher` |
|---|---|---|---|
| 用户怎么选 | profile 表单多一个"启动方式"，只对 Codex、Cursor Agent 显示 | 运行时下拉多两项 | 运行时下拉多两行：`ttadk-codex`、`ttadk-cursoragent`；profile 不加字段 |
| 控制台、管家工具、`loopercli` | profile 表单、v1 schema、管家工具字段、导出格式都要加 `launcher` | 不用改 | 不用改；运行时视图多一个只读的 `launcher` |
| 探测 | `rt-codex` 只探测原生 codex；TTADK 在不在只能到启动时才知道 | 每类型一行，各自探测 | 每行各自探测：`rt-ttadk-codex` 探测 TTADK 版本和它将拉起的 codex，结果显示在 Agent Runtimes 页 |
| 存储与按类型的名单 | 不变 | `.local/<type>/…`、会话回收、办公台会话保留、咨询名单、Codex 登录检查都要认新类型；漏一处就会误删会话（`read_only_copies.py:195-208` 按类型删目录）或漏查登录 | 不变 |
| 登录配对 | 不变 | `codex-login` 只配 `oauth_runtime_type == runtime.type` 的运行时（`profile_registry.py:337`、`:658`；`runtimes/codex.py:174`；`web/src/components/agent-profiles.tsx:52`），要引入"登录所属类型" | 不变 |
| 续接 | 不变（P3：可以续接） | 换类型即新会话 | 不变（P3：可以续接） |
| 规则写法 | 类型化字段 | 类型 id 是注册表的键，但要给现有字符串名单加项 | 类型化字段：`Launcher` 枚举，适配器声明支持哪些启动器 |

**推荐方案三。** TTADK 改的是"这个运行时的可执行文件是什么、怎么拉起、探测出来能做什么"，这正是运行时实例（`agent_runtimes` 的一行）的含义；选它的入口是 profile 已有的 `runtime_id`，三个入口不用加字段。方案二要动的地方最分散，以后每加一个按类型的名单都可能漏掉 TTADK 的变体；方案一把运行时的事放到 profile 上，探测和能力就对不上。

## 5. 决定一览

| # | 决定 | 依据与备选 |
|---|---|---|
| D1 | **配置形态：** `agent_runtimes` 加 `launcher TEXT NOT NULL DEFAULT 'native' CHECK (launcher IN ('native','ttadk'))`；`RuntimeSpec.launcher: Launcher`。适配器用 `launchers: ClassVar[frozenset[Launcher]]` 声明支持哪些（Codex、Cursor Agent 声明 `{NATIVE, TTADK}`，其他只有 `NATIVE`）。`ttadk` 在 PATH 上时注册内置行 `rt-ttadk-codex`（名称 `ttadk-codex`，类型 `codex`）和 `rt-ttadk-cursoragent`（名称 `ttadk-cursoragent`，类型 `cursor-agent`），`executable` 为空即用 `ttadk` | 第 4 节。行名照维护者的叫法，这就是界面上显示的模式名 |
| D2 | **Codex 的 TTADK 模式：** 命令 `env CODEX_HOME=<home> ttadk codex --skip-check --mode mixed -- exec …`（引导用 `… -- app-server`），模型照旧以 Codex 自己的 `-m <模型>` 放在 `--` 之后；不用 TTADK 的 `-m`，不用 `-a`。只配 `codex-login` provider | 维护者建议的 `--mode auto` 在登录不是 ChatGPT 时会落到 TTADK-only，再弹出读 stdin 的模型选择（2.3）。显式 `mixed` 失败即关闭。API key provider 的 `model_provider` 会被 TTADK 覆盖（2.3），配上去就是悄悄换了 provider，所以在建档时拒绝 |
| D3 | **环境：** 设 `TTADK_DISABLE_COMPLETION_AUTO_INSTALL=1`；**不设 `CI=1`**；TTADK 运行的 `config.toml` 在 `[shell_environment_policy]` 里排除 `TTADK_OPENAI_API_KEY`。这些变量归适配器所有（`provider_env_owned`） | `CI=1` 只关掉启动事件和补全安装（2.3），关掉启动事件可能少一条工作量记录；代价是每次运行结束多 2-4 秒 |
| D4 | **失败：** 启动前检查，失败时拒绝启动，原因用类型化的 `TtadkProblem` 表达：`NOT_FOUND`（没有 `ttadk`）、`NOT_SIGNED_IN`（`ttadk models -t <工具> -f json` 以非零退出）、`CHECK_FAILED`（这个检查超时或输出读不懂）、`WRONG_TOOL`（`cursor-cli` 会拉起的不是 Cursor CLI，第 6.7 节）；ChatGPT 登录照旧由 `codex_login.launch_problem` 检查，mixed 模式下 TTADK 自己也会拒绝非 ChatGPT 登录。准备阶段失败（没有任何 JSON 就结束）时，运行失败信息带 TTADK 的 stderr 末尾。每条信息都写原生的替代："或者把这个 Agent Profile 的运行时换成 `codex`（不计入 TTADK 统计）"。**从不退回原生** | 维护者的要求；Worker 本来把没有 `result` 判为失败（2.3） |
| D5 | **续接：** `can_resume` 不变，原生和 TTADK 之间切换照常续接 | P3：mixed 模式背后是同一个 ChatGPT 订阅后端，原生续接了 TTADK 建的会话，缓存命中。以后若做 TTADK-only，它是另一个 provider，按 P5 会新开会话 |
| D6 | **记录：** `Launch.record()` 的 `profile.runtime` 加 `launcher`；TTADK 运行的 `launch_record` 加 `{"launcher": {"kind": "ttadk", "version": "<探测到的版本>", "tool": "codex"或"cursor-cli", "mode": "mixed"或null}}`。运行时视图加 `launcher`，版本写成 `ttadk 1.0.27 / codex-cli 0.162.0` | 不另建表：运行记录本来就是每次运行的审计 |
| D7 | **新的原生 `cursor-agent` 运行时**（第 6 节），内置 provider `prov-cursor-login`（Cursor 自己的登录）；`ttadk-cursoragent` 是它的 TTADK 行 | 维护者要求 cursor-agent 也有 TTADK 模式，原生运行时是前提 |
| D8 | **Cursor 的登录在隔离下用文件存储加链接：** 运行设 `AGENT_CLI_CREDENTIAL_STORE=file`，每个 Looper 家目录的 `.cursor/auth.json` 是指向操作者登录文件的符号链接（同 Codex、Grok 的 P1 做法，`runtimes/linked_login.py`）；操作者先在本机用文件存储登录一次 | 隔离 HOME 实验：换了 `HOME` 找不到钥匙串里的登录；钥匙串目录链接实验：链接钥匙串目录会挂住。用真实 `HOME` 会执行操作者 `~/.claude` 里的 hook（2.4），不可接受。**未核实**，第 10.2 节第 1 项 |
| D9 | **`ttadk-cursoragent` 的 `agent`：** Looper 在自己的目录里放一个 `agent` 垫片，把它所在目录放到传给 TTADK 的 `PATH` 最前面；垫片把 `HOME` 换成 Looper 的 Cursor 家目录后 `exec` 真正的 `cursor-agent`。TTADK 的设置把 `cursor-cli` 映射成 `agent` 以外的命令时拒绝启动（`WRONG_TOOL`） | 本机两个 `agent` 都是 Grok（2.3）；TTADK 自己要真实 `HOME` 找 SSO 登录，Cursor 要 Looper 的 `HOME` 做隔离，只有在 TTADK 之后换 |

## 6. 设计

### 6.1 启动器（`Launcher`）

- `runtimes/__init__.py` 加 `class Launcher(str, Enum): NATIVE = "native"; TTADK = "ttadk"`，`RuntimeAdapter.launchers: ClassVar[frozenset[Launcher]] = frozenset({Launcher.NATIVE})`。
- `RuntimeSpec` 加 `launcher: Launcher = Launcher.NATIVE`；`AgentProfile.from_row` 读 `runtime_launcher`（`looper/db/profile_registry.py` 的 profile 查询把 `r.launcher` 一并选出）。
- `Launch.executable()`：运行时行的 `executable`，否则 TTADK 行取 `"ttadk"`，原生行照旧（`agent_settings.py:932-938`）。适配器从 `launch.profile.runtime.launcher` 得知要不要加 TTADK 的前缀，不比较字符串。
- 新模块 `runtimes/ttadk.py`（只放两种工具共用的部分）：
  - TTADK 的工具名由适配器声明（`ttadk_tool: ClassVar[str | None]`，Codex 是 `codex`，Cursor Agent 是 `cursor-cli`），行名同样由适配器声明（`ttadk_runtime_name`）；
  - `prefix(executable, tool) -> list[str]`：`[executable, "codex", "--skip-check", "--mode", "mixed", "--"]` 或 `[executable, "code", "-t", "cursor-cli", "--skip-check", "--"]`；
  - `environment(base)` 设置 `TTADK_DISABLE_COMPLETION_AUTO_INSTALL=1`，以及从继承环境去掉的 `CI`、`TTADK_NO_TELEMETRY`、`TTADK_DEBUG`、`DEBUG`（`DEBUG=ttadk:*` 会把诊断日志写到 stdout）、`FORCE_COLOR`（会给前导输出加颜色）；
  - `probe_version(executable) -> tuple[str | None, str | None]`（版本和错误）：`ttadk --version`，快速路径，不联网（`ttadk:index.js:29-31`）；
  - `sign_in_problem(executable, tool) -> TtadkProblem | None`：`ttadk models -t <工具> -f json`，20 秒超时，按退出码和 JSON 判断，结果缓存 60 秒（同 `codex_login.cached_probe` 的做法）；
  - `TtadkProblem`（枚举）及每种的说明与修复办法；
  - `check_args(args)`：工具参数里有 `-v`、`--version`、`handoff`、`--debug`、`--verbose`、`--log-level` 时抛 `RuntimeUnsupported`（2.3：这些在 `--` 之后仍被 TTADK 处理）。Looper 自己拼的参数里本来没有这些，这是防将来回归的边界检查。
- 注册：`register_found_runtimes` 对声明了 `Launcher.TTADK` 的适配器，在 `ttadk` 在 PATH 上时另插一行 `rt-ttadk-<名>`（`builtin_runtime_id` 按 `(类型, 启动器)` 生成；`cursor-agent` 的行名照维护者写作 `ttadk-cursoragent`，由适配器的 `ttadk_runtime_name` 给出）。`probe_runtime` 照常能"重新检测"这一行。
- 导入导出：运行时条目多一个可选的 `launcher`（只在不是 `native` 时写）；导入时按名称对上已有的行，启动器不同就跳过并说明（`looper/agents/config_transfer.py:1127-1160`）。
- 视图：`runtime_view` 和 v1 的 `RUNTIME` schema 加 `launcher`，以及按 `auth_kinds_for(launcher)` 算出的 `auth_kinds`（控制台据此过滤 provider，规则不在 TypeScript 里重写一遍）；`type_view` 不变（类型表一个类型一项）。

### 6.2 Codex 经 TTADK

命令（一次运行；常驻会话再加 `resume <thread>`；`app-server` 同理）：

```
env CODEX_HOME=<home> ttadk codex --skip-check --mode mixed --
    exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox
    -m <模型> [-c model_reasoning_effort=…] [--add-dir …] [--ephemeral] -
```

- `CodexRuntime._command` 和 `steer_command` 把 `executable` 换成 `self._program(executable, launch)`：原生是 `[executable]`，TTADK 是 `ttadk.prefix(...)`。命令仍以 `env CODEX_HOME=<home> <ttadk>` 开头，`ResidentCodex._split` 的约定和 `shutil.which` 检查照旧成立（`codex.py:935-940`），`_turn_argv`、`consult_command`、`after_run` 都按位置取 `CODEX_HOME`，不用改。
- `config.toml`：只在 TTADK 运行里，登录配置的 `[shell_environment_policy]` 另加 `exclude = ["TTADK_OPENAI_API_KEY"]`。家目录按内容哈希命名，原生运行的家目录不变。
- 配对：`CodexRuntime.check_pairing` 对 TTADK 行只收 `AuthKind.OAUTH`（`codex-login`）：API key provider 报"TTADK 会把 Codex 的 provider 换成它的网关，provider X 的 key 不会被用到；用 codex-login，或换成 codex 运行时"。适配器的 `check_pairing` 拿不到运行时行，所以 `RuntimeAdapter` 加 `auth_kinds_for(launcher) -> frozenset[AuthKind]`（默认返回 `auth_kinds`，Codex 对 TTADK 返回 `{OAUTH}`），`profile_registry.check_pairing` 和 `ResolvedModel.check_runtime` 用它代替 `adapter.auth_kinds`（`profile_registry.py:339`、`:662`）。
- 登录：`login_problem` 先走 `codex_login.launch_problem()`，再走 `ttadk.sign_in_problem`。两者任一有问题，运行不启动。
- 探测：TTADK 行的探测 = `ttadk --version` + 原生 `codex --version` + 原生 `app-server generate-json-schema`（决定 `steer`）。TTADK 拉起的是它设置里 `ai_tool_commands.codex` 指的命令，默认就是 PATH 上的 `codex`（`ttadk:core/command/code.js:840-845`）；设置里的命令是单个词时探测该命令；含参数等多个词时探测 PATH 上的 `codex`，并在 `probe_error` 里说明这个限制。经 TTADK 问 Codex 的版本不可行（`-v` 被 TTADK 截走）。
- 事件：不变。前导输出被现有读取器跳过；TTADK 的退出码不参与判断：`Launch.exit_code_reliable` 对 TTADK 行为假，Worker 在没有可用 `result` 时即使退出码是 0 也走带 stderr 的失败分支（`tiers/worker.py:1278-1283`），任务动态里能看到 TTADK 的报错。
- 时延：每个进程多出 TTADK 的准备时间（热 3.7 秒、冷 17.8 秒）和结束后的 2-4 秒（P1、P2）。管家、规划者每轮一个进程，每轮都要付；这是 TTADK 模式的已知代价，写进运行时视图的说明，不做优化（第 9 节）。

### 6.3 各角色（`ttadk-codex`）

| 角色 | 能否用 | 说明 |
|---|---|---|
| 管家、规划者 | ✅ | 每轮一个 `exec` 进程续接（P2）。MCP 平台工具写在 `config.toml` 里，TTADK 不碰它（P1 前后哈希不变）。不能中断，与原生 Codex 相同 |
| Worker、Reviewer | ✅ | `exec` 或可引导时 `app-server`（P4 的握手经 TTADK 透传） |
| 咨询 | ✅ | `consult_command` 的分叉家目录同样经 TTADK |

### 6.4 原生 `cursor-agent` 运行时

新适配器 `runtimes/cursor_agent.py`，类型 `cursor-agent`，标签 Cursor Agent，默认可执行文件 `cursor-agent`。

- **一次运行（Worker、Reviewer）：**

  ```
  env HOME=<家目录> AGENT_CLI_CREDENTIAL_STORE=file cursor-agent
      -p --output-format stream-json --force --trust --sandbox disabled --disable-auto-update
      --model <模型> --plugin-dir <Looper 插件> [--plugin-dir <已装插件>…] [--add-dir <知识目录>…]
  ```

  提示从 stdin 写入后关闭（`write_turn` 照 Codex 的写法，再写一轮抛 `RuntimeError`）；提示不进 argv。
- **常驻会话（管家、规划者）：** 逐轮续接（`per_turn.PerTurnSession`），第二轮起加 `--resume <chatId>`，chatId 取自 `system/init` 的 `session_id`；要在同一工作目录续接，常驻 owner 的工作目录本来固定。会话在家目录的 `.cursor/chats/` 里，所以每个常驻 owner 有自己的家目录 `<runtime_root>/.local/cursor-agent/sessions/<label>`，每轮前从这一轮的运行家目录同步配置和链接（同 `codex._sync_home`）。
- **system prompt：** 写进 Looper 插件的 `rules/looper.md`（`alwaysApply: true`），前面是适配器的运行时说明（只读你最后一条消息；平台工具在 MCP 服务器 `looper` 里），后面是 Looper 的 prompt。
- **MCP：** 运行的 `mcp_config`（含 `looper` 服务器和令牌）写进 Looper 插件根目录的 `.mcp.json`（插件级不需要审批，2.4）。stdio 服务器的 `env` 加回操作者的 `HOME`（同 Grok，第 7 节）。HTTP 服务器照写；不支持的记进 `launch_record`。
- **已装插件和 skill：** Looper 的已装插件本来是 Claude Code 格式（`.claude-plugin/plugin.json`），Cursor 的 `--plugin-dir` 认这个格式，直接逐个传。插件里 Cursor 不支持的部分照实记进 `runtime_catalog.skipped`（核实前先保守：只声明 skill 和 MCP 能用）。
- **隔离：** `HOME` 指向 Looper 的家目录 `<runtime_root>/.local/cursor-agent/runs/<sha256>`，这样不读操作者的 `~/.cursor/mcp.json`、`hooks.json`、`~/.claude/settings.json` 的 hook、`~/.claude/commands`、skill，也不把 `~/.claude/plugins/marketplaces` 登记到 Cursor 账号（2.4）。去掉继承的 `CURSOR_CONFIG_DIR`、`CURSOR_DATA_DIR`、`XDG_CONFIG_HOME`、`CURSOR_API_KEY`、`CURSOR_AUTH_TOKEN`、`CURSOR_API_ENDPOINT`；agent 的 shell 和 MCP 服务器拿回操作者的 `HOME`（Cursor 的 shell 环境怎样设，**未核实**，第 10.2 节第 5 项）。
- **登录（D8）：** 内置 provider `prov-cursor-login`（vendor `cursor-login`，OAuth，`oauth_runtime_type = 'cursor-agent'`，端点协议沿用 `grok-login` 的写法记为 `openai-responses`，只用于配对）。家目录的 `.cursor/auth.json` 链接到操作者的 `~/.cursor/auth.json`；`linked_login.inspect_homes` 每次运行后和服务启动时检查链接。操作者的登录文件不存在时，运行不启动，原因写"先在本机运行 `AGENT_CLI_CREDENTIAL_STORE=file cursor-agent login`"。
- **事件（`CursorStream`）：** `system/init` 加 `runtime: "cursor-agent"`；`assistant` 原样；`tool_call/started` → `assistant` 的 `tool_use`（名字取 `tool_call` 的 oneof 键，如 `shellToolCall` → `shell`，`mcpToolCall` → `mcp__<server>__<tool>`），`tool_call/completed` → `user` 的 `tool_result`；`result/success` 原样，另把 `usage` 换成 `modelUsage`（`inputTokens`、`outputTokens`、`cacheReadInputTokens`、`cacheCreationInputTokens`）并发一个内部 `usage` 事件；`thinking/*` 合成 `thinking` 块；其余事件记成 `system`。出错时 Cursor 不发 `result`：适配器不补，交给"没有 result 即失败"的现有规则，`failure_detail` 带 stderr 末尾。
- **强度：** Cursor 没有强度参数（只有模型 id 的方括号写法），v0 不支持 effort（`effort_levels = ()`）。
- **花费：** Cursor 不报花费，`cursor-login` 的模型没有价格：只记 token，花费未知（`cost_source = provider_price`，没有价格时就是未知）；`runtime_cost_capped = False`，建档时照 Grok 的做法警告单次上限停不住它。
- **只读：** 规划者和咨询用 `ReadOnly.CHECK`（规划者每轮前后比较任务目录）。`--mode plan` 下 MCP 工具还能不能用没核实，规划者离不开它的平台工具，所以 v0 不用 plan 模式；咨询不在 `consult.RUNTIME_READ_ONLY` 里，按现有规则在一次性副本里跑。
- **取消：** 进程组 SIGTERM，退出码 143，没有 `result`；shell 命令自建进程组，`proc_tree` 按树和组结束它们（推断，第 10.2 节第 6 项）。
- **能力：** `run`、`resident_session`、`resume`、`mcp`、`add_dirs`；不声明 `structured_output`、`claude_plugins`、`cost_report`、`steer`。
- **探测：** `cursor-agent --version`（`2026.10.01-14929f9`），带一个临时 `HOME`。

### 6.5 各角色（`cursor-agent`）

| 角色 | 能否用 | 说明 |
|---|---|---|
| 管家、规划者 | ✅（核实 MCP 后） | 逐轮 `--resume`；平台工具靠插件 `.mcp.json`；不能中断 |
| Worker、Reviewer | ✅ | 一次 `-p` 运行；运行中收不到规划者的消息（与 Grok headless 相同） |
| 咨询 | ⚠️ 一次性副本 | 没有分叉会话的参数（`consult_command` 抛 `RuntimeUnsupported`，按现有规则转给规划者） |

### 6.6 不选的 Cursor 接法

- **ACP（`agent acp`）：** 有 `session/new` 注入 MCP、`session/load`、`session/cancel`，能做可中断的常驻会话，但 `session/prompt` 不报用量（2.4 的调研），且要一个新的 JSON-RPC 客户端。v0 先用 headless 加 `--resume`，ACP 记为后续（同 Grok 先 headless 后 ACP 的顺序）。
- **API key（`CURSOR_API_KEY`）：** 用 key 认证时 Cursor 会把换来的令牌写进凭据存储，默认是钥匙串，可能覆盖操作者的登录（2.4）；要配 `AGENT_CLI_CREDENTIAL_STORE=memory`，团队策略也可能不允许 key。v0 不做。
- **真实 `HOME` 加 `CURSOR_CONFIG_DIR`：** 登录能用，但会执行操作者的 Claude Code hook，并登记插件市场（2.4）。不接受。

### 6.7 Cursor Agent 经 TTADK（`ttadk-cursoragent`）

```
env PATH=<Looper 的垫片目录>:<原 PATH> LOOPER_CURSOR_HOME=<家目录> AGENT_CLI_CREDENTIAL_STORE=file
    ttadk code -t cursor-cli --skip-check --
    -p --output-format stream-json --force --trust --sandbox disabled --disable-auto-update --model <模型> …
```

- 进程本身用操作者的 `HOME`，TTADK 才找得到它的 SSO 登录。
- 垫片 `<runtime_root>/.local/cursor-agent/ttadk-bin/agent`（0700，内容由 Looper 生成，按内容重写）：

  ```sh
  #!/bin/sh
  # Written by Looper: TTADK runs `agent` for cursor-cli; this is Cursor's CLI with Looper's home.
  export HOME="$LOOPER_CURSOR_HOME"
  exec '<解析好的 cursor-agent 绝对路径>' "$@"
  ```

  `LOOPER_CURSOR_HOME` 没设时垫片以 2 退出并在 stderr 说明，不带操作者的 `HOME` 运行。
- 启动前：读 TTADK 设置里的 `ai_tool_commands.cursor-cli`（只读这一项），不是 `agent` 时 `WRONG_TOOL`："TTADK 的设置让 cursor-cli 运行 `<命令>`，Looper 不能确认它是 Cursor CLI"。
- 提示从 stdin 进（TTADK 继承 stdin），不作位置参数（位置参数里的 `-m…` 会被 TTADK 吃掉，2.3）。
- 记录：`launch_record` 的 `launcher.tool = "cursor-cli"`。TTADK 对 cursor-cli 只发启动事件；停止和超时时 node 先死，事件发不出（2.3），见第 11 节问题 1。
- 各角色与原生 `cursor-agent` 相同；每个进程多出 TTADK 的准备时间（推断与 Codex 相近，未实测）。

## 7. 安全与隔离

- **不改操作者的配置。** TTADK 只在不带 `--skip-check` 时同步插件、写项目和 `CODEX_HOME` 的配置（`ttadk:core/ai-tools/codex-hooks.js:45-58`），Looper 总是带它；P1 前后 `config.toml` 哈希不变。`--app`、`--wrapper`、`-p` 永远不传，`ttadk.check_args` 不管这些（它们只能出现在 `--` 之前，由 Looper 自己拼），由命令构造保证并有测试钉住。
- **SSO 令牌。** TTADK 把 SSO 令牌放进 codex 子进程的 `TTADK_OPENAI_API_KEY`；Looper 在 TTADK 运行的 `config.toml` 里把它排除出 agent 的 shell 环境。令牌从不进运行记录和日志：`launch_record` 只记版本、工具和模式；前导输出里的邮箱被现有读取器丢弃，`failure_detail` 去掉网址和邮箱。
- **TTADK 写的文件**都在 `~/.ttadk/`（登录状态缓存、安装 id、刷新后的登录），是 TTADK 自己的状态，与操作者在终端里用 TTADK 相同。
- **Cursor。** 换 `HOME` 是为了不执行操作者的 Claude Code hook、不读个人 MCP 和 skill、不把插件市场登记到 Cursor 账号；登录以链接共用，从不复制（D8）。agent 在 shell 里改回 `HOME` 仍能碰到登录，与 Codex、Grok 相同，Looper 管不到。
- **垫片**只在 `ttadk-cursoragent` 的进程 `PATH` 里，不在操作者的 `PATH` 里；它执行的绝对路径由 Looper 在启动时解析（`shutil.which("cursor-agent")` 的 `realpath`）。

## 8. 可观测性

| 看什么 | 在哪 |
|---|---|
| 这次运行是否经过 TTADK、版本、工具、模式 | 运行记录 `metadata.launcher` 和 `metadata.profile.runtime.launcher`；任务动态的 `$ …` 命令行本来就显示 `ttadk codex …` |
| TTADK 能不能用 | Agent Runtimes 页 `ttadk-codex`/`ttadk-cursoragent` 行的版本和 `probe_error`；`looper doctor` 与 `/api/v1/system/status` 新增 `ttadk` 检查（只在有 Agent Profile 用 TTADK 行时检查）：`ttadk` 不在 PATH、没有登录 → fail，检查超时 → warn |
| 启动为什么失败 | 任务动态的错误行：`TtadkProblem` 的说明和修复办法，或 TTADK stderr 的末尾 |
| 时延 | 现有的回合耗时；TTADK 的额外开销能从 `init` 事件的时间与进程启动时间之差看出。v0 不加新指标 |

## 9. 风险

| 风险 | 影响 | 处理 |
|---|---|---|
| SSO 在检查之后、启动之前失效，TTADK 进入设备授权：打开浏览器、阻塞 | 这次运行挂到它的超时为止 | 启动前检查（60 秒缓存）把窗口压到很小；残余风险记为已知，后续可加"启动后 N 秒没有任何 JSON 就结束"的看门狗（第 12 节待办） |
| TTADK 升级后改了参数解析、前导输出或注入方式 | 启动失败或悄悄换了行为 | 探测记版本；真实路径验证 用真实 TTADK 跑 `exec`、续接和 `app-server`；以后可加一个 `installed` 标记的 nightly 用例（只做 `app-server` 握手，不调模型） |
| 每个进程多 4-20 秒 | 管家、规划者每轮更慢 | 已知代价，写进 RFC 和运行时说明；不做常驻优化 |
| Codex 的 `-c` 来源顺序 | TTADK 的 `-c` 在根上；Looper 若将来在命令行上加 `-c model_provider=…` 会绕过网关 | `ttadk` 模式下命令里出现 `model_provider` 的 `-c` 时 `RuntimeUnsupported`，有测试 |
| cursor-cli 的工作量记录很薄 | 只有启动事件，停止时还会丢 | 第 11 节问题 1 |
| Cursor 团队策略禁用 headless 或 "Run Everything" | `--force` 运行直接退出 1 | stderr 进运行失败信息；Cursor 登录验证 开工前的那次实跑即可证明 |
| `agent` 被别的工具占用（本机是 Grok） | TTADK 拉起错误的程序 | 垫片（D9）+ 设置检查 |

## 10. 测试与验证

### 10.1 测试（按开发计划第 1 节：一条规则一个测试，测在离入口最近的一层）

- 启动器：TTADK 行的注册条件（`ttadk` 在不在 PATH）、视图、导入导出里的 `launcher`；`auth_kinds_for` 拒绝 API key provider（profile 建档层，一个参数化测试）。
- Codex 命令：TTADK 行的 `run_command`、`resident_command` + `resume`、`steer_command`、`consult_command` 都以 `env CODEX_HOME=… ttadk codex --skip-check --mode mixed --` 开头，且 `--` 之后与原生逐字节相同（一个参数化测试，对比原生命令）；原生命令不变（现有测试守住）。
- 事件：一个假的 `ttadk` 脚本先打印若干前导行再 `exec` 一个假 Codex（`tests/` 里现有的替身方式），Worker 和常驻会话都能读出结果；假 TTADK 以 1 退出且没有 JSON 时，运行失败信息带 stderr 末尾。
- 失败：每个 `TtadkProblem` 一行参数化，入口是 `login_problem`；断言从不起原生进程。
- Cursor：`CursorStream` 用按 2.4 手写的 stream-json 样本（真实路径验证 实跑后替换为真实样本）测翻译；命令、家目录、插件目录、登录链接的布局各一条；垫片内容与 `PATH` 顺序一条。
- 不测：TTADK 和 Cursor 自身的行为（交给 真实路径验证 的真实验证）。

### 10.2 未核实的事实与核实步骤

| # | 事实 | 怎么核实（每项最多一次 "pong" 级调用） |
|---|---|---|
| 1 | Cursor 用 `AGENT_CLI_CREDENTIAL_STORE=file` 登录后，换了 `HOME` 的家目录经 `.cursor/auth.json` 链接能认证；刷新时是否原地写回（还是把链接换成文件） | 操作者在本机 `AGENT_CLI_CREDENTIAL_STORE=file cursor-agent login`；Looper 布局的家目录跑一次 pong；前后 `lstat` 链接和操作者文件的 inode、修改时间 |
| 2 | 提示从 stdin 读（无位置参数） | 同上一次运行 |
| 3 | `--plugin-dir` 的 `rules/looper.md`（`alwaysApply`）和 `.mcp.json` 生效 | 同一次运行：规则要求回复以固定标记结尾，提示要求调用探针 MCP 的 `ping` 并原样回复；看回复和探针日志 |
| 4 | stream-json 的真实形状、`usage` 是否出现 | 同一次运行的原始输出，存为 `CursorStream` 的测试样本 |
| 5 | Cursor 的 shell 拿到的 `HOME` | 同一次运行里让它执行 `echo $HOME` 并回复 |
| 6 | SIGTERM 后 shell 子进程和 `worker-server` 是否残留 | 让一次运行执行 `sleep 60` 时按 `proc_tree.end` 结束，再看进程表 |
| 7 | `ttadk code -t cursor-cli` 经垫片拉起 Cursor 且 stream-json 原样透传 | 按第 6.7 节的命令跑一次 pong |
| 8 | 团队策略是否允许 headless 和 `--force` | 第 1 项那次运行成功即可证明 |
| 9 | Codex 经 TTADK 时 MCP 平台工具可用（`config.toml` 不变已证实，工具调用未跑） | 管家在 `ttadk-codex` 上跑一轮会调用 `looper` 工具的对话（真实路径验证） |
| 10 | `ttadk models -t codex -f json` 在未登录时以非零退出、不进设备授权 | 读代码已确认路径（`ttadk:core/command/models.js:29-42`）；实测需要先登出 SSO，不做，单测用替身覆盖 |

## 11. 默认行为的变化与待决问题

### 11.1 默认行为的变化

- 没有 `ttadk` 的安装：没有变化（不注册 TTADK 行，原生命令逐字节不变）。
- 有 `ttadk` 的安装：Agent Runtimes 多出 `ttadk-codex`（以及 Cursor 的 TTADK 接入 之后的 `ttadk-cursoragent`）；不选它们的 Agent Profile 不受影响。
- `cursor-agent` 在 PATH 上的安装：多出运行时 `cursor-agent` 和 provider `cursor-login`。
- 入口：Telegram、飞书和 CLI 不需要改，它们经管家和任务流程运行，运行时由 Agent Profile 决定；控制台的 Agent Profile 表单和管家工具用已有的 `runtime_id`。

### 11.2 待决问题（附推荐默认值）

1. **cursor-cli 在 TTADK 里算不算工作量？** TTADK 对 cursor-cli 不注入网关，只发一个启动事件（2.3），运行被停止时这个事件还会丢。**推荐：** 照做 `ttadk-cursoragent`（在原生 `cursor-agent` 之上只多一张卡），不设 `CI=1` 保住启动事件；请维护者向 TTADK 的维护方确认 cursor-cli 的统计口径，如果统计靠 Cursor 自己的企业账号，`ttadk-cursoragent` 与原生的差别只在这个事件。
2. **Cursor 的登录用什么？** 钥匙串登录在隔离下不可用（隔离 HOME 实验、钥匙串目录链接实验）。**推荐：** 文件存储登录加链接（D8），操作者在本机用 `AGENT_CLI_CREDENTIAL_STORE=file cursor-agent login` 登录一次；核实不通过（10.2 第 1 项）再改用 Cursor API key 加 `AGENT_CLI_CREDENTIAL_STORE=memory`（一个 API key 类型的 provider）。
3. **要不要 TTADK-only 模式（SSO + 网关模型，不用 ChatGPT 订阅）？** **推荐：** v0 不做。以后要做时，新增内置 provider `ttadk-gateway`（模型取自 `ttadk models -t codex -f json`），只配 TTADK 行，命令改成 `--mode ttadk-only -m <网关模型> --`；它是另一个 provider，按 P5 自动新开会话。

## 12. 实施顺序与后续

先交付运行时行的启动器字段、TTADK 公共启动模块、Codex 的注册与探测、命令、失败记录和自检。Cursor 的接入依次交付登录验证、单轮运行与隔离、逐轮续接，再接 TTADK 包装器。界面随后补充启动器说明、provider 过滤与登录帮助；最后验证真实 TTADK 路径。

后续待办：TTADK 启动看门狗（第 9 节）；Cursor 的 ACP 常驻会话（6.6）；TTADK-only 模式（11.2，待维护者决定）。
