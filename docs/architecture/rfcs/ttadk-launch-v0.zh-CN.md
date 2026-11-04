# RFC：TTADK 启动模式 v0（`ttadk-codex` 与 `ttadk-cursoragent`）

- **状态：** 已批准（2026-10-30），按第 5 节的推荐决定实施；第 11 节的三个待决问题先按推荐默认值执行，维护者可随时调整。**2026-11-04 修订：** 维护者决定 Cursor 沿用操作者本机现有的登录（macOS 登录钥匙串里的团队登录），D8、D9 和第 2.4、6.4、6.6、6.7、7、8、9、10、11 节按此改写，第 11.2 节问题 2 已定。
- **需求（维护者，2026-10-30）：** 开发要支持配置 TTADK 模式，codex 和 cursor-agent 都要。TTADK 是组织要求的开发模式，会记录工作量。agent 要能配置成 `ttadk-codex` 和 `ttadk-cursoragent` 两种模式。
- **相关：** [多运行时](multi-runtime-agents-v0.zh-CN.md)（P1 登录链接、P5 续接规则）、[Grok 运行时](grok-runtime-v0.zh-CN.md)（厂商 CLI 加自有登录的接法）、[Worker 引导](worker-steering-v0.zh-CN.md)（Codex `app-server`）、[开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 1 节（测试与验证方式）。
- **核对基线：** `main` `6a3cbb94e`（v2.14.26，代码与 `181743944` 相同）。外部工具：TTADK 1.0.27（npm 包，只读源码）、Codex CLI 0.162.0、Cursor CLI `cursor-agent` 2026.10.01-14929f9（只读 `--help` 和随包的 JS）；Cursor 部分 2026-11-04 用 2026.10.22-27dcf5f 复核（只读随包的 JS，另实跑两次），TTADK 的 cursor-cli 路径用 1.0.37 复核（只读源码）。实跑见第 2 节，共 3 次 Codex 模型调用、2 次 Cursor 模型调用（真实 HOME 与双目录实验）。
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
- v0 的 `cursor-agent` 不走 ACP 常驻进程（第 6.6 节）；Cursor 不另建登录：不用文件存储（`AGENT_CLI_CREDENTIAL_STORE=file`）、不用 API key、不发登录链接，只用操作者本机现有的登录（D8）。

## 2. 实测与源码证据（2026-10-30）

### 2.1 怎么测的

- Codex：一个照 Looper 登录家目录布局写的临时 `CODEX_HOME`（`config.toml` 与 `runtimes/codex.py:477-500` 的登录配置相同，`auth.json` 是指向操作者登录文件的符号链接，没有读取或复制它），工作目录是一个不在 git 仓库里的临时目录。进程按 Looper 的方式启动：`start_new_session=True`，stdin/stdout/stderr 都是管道，提示写进 stdin 后关闭（同 `proc_tree.py:335-340`、`runtimes/per_turn.py:261-289`）。
- Cursor（2026-10-30，隔离 HOME 实验和钥匙串目录链接实验）：临时 `HOME`，一个 `--plugin-dir` 插件目录（规则文件 + 一个本地 stdio MCP 探针服务器），提示从 stdin 读。
- Cursor（2026-11-04，真实 HOME 与双目录实验，CLI 2026.10.22）：按 D8 用操作者的真实 `HOME`（钥匙串里的团队登录），环境里去掉 `CURSOR_API_KEY`、`CURSOR_AUTH_TOKEN`、`CURSOR_API_ENDPOINT`、`AGENT_CLI_CREDENTIAL_STORE`，`CURSOR_CONFIG_DIR` 指向一个临时目录（双目录实验另把 `CURSOR_DATA_DIR` 指向临时目录），`NO_OPEN_BROWSER=1`；`cursor-agent -p --output-format stream-json --force --trust`，没有位置参数，提示从 stdin 写入；工作目录是新建的 git 仓库；没有 `--plugin-dir`。
- 没有用 `--app`、`--wrapper`；没有改 `~/.codex/config.toml`（实测前后 `CODEX_HOME` 里的 `config.toml` 哈希不变，操作者的 `~/.cursor/cli-config.json`、`mcp.json` 修改时间不变）。没有读取钥匙串或任何凭据文件。

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
| TTADK 的设置和 SSO 缓存在 `~/.ttadk/`（推断：经 node 的 `os.homedir()`，在 macOS/Linux 上取 `HOME`） | `ttadk:core/config-manager/config-manager.js:246-283` | TTADK 和 Cursor 都要操作者的 `HOME`（Cursor 的钥匙串登录只在那里可用，D8），两者不冲突；进程不换 `HOME`（第 6.7 节） |

### 2.4 Cursor CLI（📄 2026.10.01 与 2026.10.22 的 bundle 和 `--help`；✅ 2026.10.22 实跑两次，真实 HOME 与双目录实验）

| 事实 | 依据 |
|---|---|
| headless：`-p --output-format stream-json`；没有位置参数且 stdin 不是 TTY 时，从 stdin 读到 EOF 作为提示（单轮） | `--help`；`cursor:1962.index.js@9564`、`@15810` |
| 事件（✅ 真实 HOME 与双目录实验）：`system/init`（键 `apiKeySource`、`cwd`、`model`、`permissionMode`、`session_id`、`subtype`、`type`）；`user`（回显提示）；`assistant`（一段文本；`--model` 为 Auto 时第一段以 `> Auto routed to <模型>` 加空行开头）；`tool_call` 的 `started`/`completed`（`call_id`；`tool_call` 有一个以 `ToolCall` 结尾的键，如 `shellToolCall`，里面是 `args`、`description`，completed 另有 `result`，如 `result.success.{stdout, stderr, exitCode, interleavedOutput}`；同级还有 `hookAdditionalContexts`、`toolCallId`、`startedAtMs`/`completedAtMs`）；`thinking/delta`（`text`）与 `thinking/completed`（不带文本）；`connection/reconnecting`（`attempt`）与 `connection/reconnected`；`retry/starting` 与 `retry/resuming`（`attempt`）；`result/success`（`duration_ms`、`is_error`、`result`、`session_id`、`request_id`、`usage: {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens}`，`inputTokens` 不含缓存读）。**每次重试时，`retry/starting` 之前已发出的那段助手文本在 `retry/resuming` 之后重新生成一遍，`result.result` 把各次尝试的文本拼在一起**（双目录实验：`pongpongpong`），所以报告不能直接用它。**不报花费**；出错时没有 `result`，错误在 stderr，退出码 1 | `cursor:1962.index.js@557426`（`headless.ts`）、`cursor:index.js@5644003`；真实 HOME 与双目录实验的原始输出 |
| 会话：`session_id` 就是 chatId；`--resume <chatId>` 可配 `--print`，要在同一个工作目录续接；会话存在 `${CURSOR_CONFIG_DIR}/chats/<md5(cwd)>/<chatId>/store.db` | `cursor:index.js@4335769`、`cursor:1376.index.js@65` |
| 审批：headless 下不加 `--force` 一律拒绝；未信任的目录退出，`--trust` 放行。团队托管设置由服务端下发、取不到时放行（fail-open），可禁用 headless（`-p` 直接退出）、"Run Everything"（`--force` 无效）或 MCP；所验证的团队策略允许 headless 和 `--force`（真实 HOME 与双目录实验） | `cursor:3129.index.js@256`；Cursor 公开文档 permissions、headless；2026.10.22 bundle |
| MCP：用户级 `mcp.json` 写死在 `os.homedir()/.cursor/`，**不跟 `CURSOR_CONFIG_DIR` 走**，没有参数指定别的文件，用户级服务器不需要审批；插件根目录的 `.mcp.json` 也会加载，同样不需要审批（只有项目级要审批）。某个用户级服务器可以按工作区停用：`CURSOR_DATA_DIR/projects/<slug>/mcp-disabled.json`（服务器名的 JSON 数组；文件名和格式未核实） | `cursor:index.js@4334857`、`cursor:3129.index.js@13396`；2026.10.22 bundle |
| `--plugin-dir` 认 `.cursor-plugin/plugin.json` 或 `.claude-plugin/plugin.json`；插件可带 `rules/<名>.md`（frontmatter `alwaysApply: true`）、`commands/`、`skills/`、`.mcp.json` | `cursor:index.js` 插件安装与发现代码（`U=[".cursor-plugin/plugin.json",".claude-plugin/plugin.json","plugin.json"]`） |
| Claude Code 兼容写死打开（`getThirdPartyExtensibilityEnabled = () => true`）：读 `~/.claude/settings.json` 的 hook 和权限并照做、读 `~/.claude/commands`（只有 headless 的隐藏参数 `--exclude-workspace-context` 跳过它，同时去掉项目的规则、skill 和笔记）、`.claude` 下的 skill、agent 和 `CLAUDE.md`、启用的 Claude 插件，并把 `~/.claude/plugins/marketplaces` 导入 Cursor 账号。没有参数、环境变量、`cli-config.json` 键、statsig 开关或团队设置能关掉它 | `cursor:190.index.js@73836`、`cursor:1877.index.js@65`、`cursor:index.js@4528959`；2026.10.22 bundle |
| 固定在 `os.homedir()` 下、不跟两个目录变量走的：`~/.cursor/agent-cli-state.json`、`hooks.json`、`mcp.json`、skill（`~/.cursor`、`~/.claude`、`~/.codex`、`~/.grok`、`~/.agents` 下）、用户规则、插件（市场和缓存）、团队托管 hook（每次启动重写）、worktrees（可用 `CURSOR_WORKTREES_ROOT` 改）。规则从工作目录一路收集到 `/`：工作目录在 `HOME` 下时，`~/AGENTS.md`、`~/CLAUDE.md`（如有）也会读到 | 2026.10.22 bundle |
| `CURSOR_CONFIG_DIR`：`cli-config.json`、`chats/`、`permissions.json`、`statsig-cache.json`、`acp-*`。`CURSOR_DATA_DIR`（或 `--data-dir`）：`projects/<slug>/`（`mcp-approvals`、`mcp-disabled`、`mcp-auth`、`.workspace-trusted`、`terminals`、`worker.log`、`agent-transcripts/`）；不设时落在 `~/.cursor/projects/`（真实 HOME 实验） | 2026.10.22 bundle；真实 HOME 与双目录实验|
| 启动脚本 `exec` 成 node，PID 就是 node；SIGTERM 退出码 143；shell 工具的命令 `detached:true`，各自一个进程组 | `cursor:index.js@2121845`、`@7849889` |
| 只能用 Cursor 后端，不能接任意 OpenAI/Anthropic 端点 | `cursor:index.js@2520064` |
| 凭据：macOS 默认在登录钥匙串，经系统凭据工具读写，不指定钥匙串文件；`AGENT_CLI_CREDENTIAL_STORE` 取 `file`（`~/.cursor/auth.json`，按 `HOME`）、`memory`（只在进程内）或 `default`。`--auth-token`/`CURSOR_AUTH_TOKEN` 给的令牌会被写进钥匙串，所以 Looper 必须去掉它们 | `cursor:index.js@2665134`、`@4278587`；2026.10.22 bundle |
| `cursor-agent status`（别名 `whoami`）有 `--format text\|json`。json 打印 `{status, isAuthenticated, hasAccessToken, hasRefreshToken, message?, userInfo?}`，`status` 取 `authenticated`、`partially-authenticated`（缺刷新令牌）、`unauthenticated`，三种都以 0 退出；检查本身出错时打印 `{status: "error", message}` 并以 1 退出。有令牌时会联网取用户信息，取不到仍算 `authenticated`。文本格式只在取到邮箱时写 `Logged in as <邮箱>`，否则写 `Login successful!`（另一行说明取不到用户信息）。2026-11-04 在操作者的 `HOME` 和临时双目录下已核实 authenticated JSON（1.3–2.0 秒），未在操作者的 `~/.cursor/projects` 留下内容；不保存或输出 `userInfo` 内容 | `cursor:752.index.js`（2026.10.22，`src/commands/status.ts`）、`cursor:index.js@2041148`（2026.10.22，`--format`） |
| `<data>/projects` 路径超过 84 个字符时，worker 套接字放到 `/tmp/.cursor/<slug>` | `cursor:index.js@4336419` |

Cursor 的实跑（隔离 HOME 实验和钥匙串目录链接实验在 2026-10-30 用 2026.10.01，没有调用到模型；真实 HOME 与双目录实验在 2026-11-04 用 2026.10.22，按 2.1 的做法）：

| # | 做法 | 结果 |
|---|---|---|
| 隔离 HOME 实验 | 临时 `HOME`，其他照常 | ❌ `Error: Authentication required. Please run 'agent login' first`，退出码 1：**换了 `HOME` 就找不到钥匙串里的登录** |
| 钥匙串目录链接实验| 临时 `HOME`，另把 `~/Library/Keychains` 链接进去 | ❌ 240 秒内 stdout 没有任何输出，MCP 探针没被启动，被超时结束；原因未查明（推断：钥匙串访问在等待授权）。不能作为方案 |
| 真实 HOME 实验| 真实 `HOME`，只设 `CURSOR_CONFIG_DIR`；提示要求执行一次 `echo HOME=$HOME`，再回复 `pong <那一行>` | ✅ 退出码 0，用时 87 秒。`init` 的 `apiKeySource: "login"`、`model: "Auto Balance"`；第一段助手文本以 `> Auto routed to Grok 4.7` 开头。一次 `shellToolCall`，shell 里的 `HOME` 是操作者的真实家目录。两次断线（`connection/reconnecting` → `reconnected`），每次接着 `retry/starting` → `retry/resuming`，`pong HOME=…` 共生成三遍，`result.result` 是开头那段话加三遍拼接；最后一次 `retry/resuming` 之后的文本才是答案。`usage` 为 95/8/19584/0，`duration_ms` 70585。英文提示得到中文回复，思考里提到操作者的个人 skill（推断：操作者的个人指令和 Claude 插件的 skill 都加载了）。操作者 `~/.cursor` 的 `cli-config.json`、`agent-cli-state.json`、`mcp.json`、`prompt_history.json` 修改时间和大小前后不变；`CURSOR_CONFIG_DIR` 里出现 `cli-config.json`、`statsig-cache.json`、`chats/<md5(cwd)>/<chatId>/`；没设 `CURSOR_DATA_DIR`，项目数据（`agent-transcripts/`、`mcp-auth.json`、`worker.log`）写进了操作者的 `~/.cursor/projects/<slug>/` |
| 双目录实验| 同真实 HOME 实验，另设 `CURSOR_DATA_DIR`；提示 `Reply with exactly the word pong and nothing else.` | ✅ 退出码 0，用时 41 秒。同样两次断线重试，`result.result` 为 `> Auto routed to Grok 4.7\n\npongpongpong`，最后一次 `retry/resuming` 之后的文本是 `pong`。`CURSOR_DATA_DIR` 里出现 `projects/<slug>/`（`mcp-auth.json`、`.workspace-trusted`、`worker.log`、`repo.json`、`agent-transcripts/`）。启动后约 6 秒，Cursor 的团队托管 hook 目录下 20 个团队托管 hook 脚本和 `~/.cursor/skills-cursor/.sync-manifest.json` 的修改时间更新（按时间推断是这次启动写的，操作者自己启动 Cursor 时也一样） |

真实 HOME 与双目录实验共同说明：团队策略允许 headless 和 `--force`；不设文件存储，钥匙串里的登录照样可用；提示从 stdin 读；所观察的 `UserPromptSubmit` hook 没有记下这两次的提示（它在 headless 下没有触发；别的事件的 hook 未核实）。真实 HOME 实验的原始输出去掉路径和 id 后存为测试样本（第 10.1 节）。

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
| D7 | **新的原生 `cursor-agent` 运行时**（第 6 节），内置 provider `prov-cursor-login`（操作者本机的 Cursor 登录，D8）；`ttadk-cursoragent` 是它的 TTADK 行 | 维护者要求 cursor-agent 也有 TTADK 模式，原生运行时是前提 |
| D8 | **Cursor 用操作者本机现有的登录（维护者 2026-11-04 决定）：** 在 macOS 上就是登录钥匙串里的团队登录（`cursor-agent status` 显示已登录）。不用文件存储、不用 API key、不发登录链接。Cursor 进程保留操作者的真实 `HOME`；Looper 给每次运行（常驻会话则是每个 owner）一对自己的目录，`CURSOR_CONFIG_DIR`（`cli-config.json`、`chats/`、权限）和 `CURSOR_DATA_DIR`（`projects/<slug>/`：信任、MCP 批准与认证、worker 日志）；去掉 `AGENT_CLI_CREDENTIAL_STORE`、`CURSOR_API_KEY`、`CURSOR_AUTH_TOKEN`、`CURSOR_API_ENDPOINT`，设 `NO_OPEN_BROWSER=1`。登录状态由 `cursor-agent status --format json` 判断（第 6.4 节）。**代价（维护者已接受）：** 操作者的个人 Cursor 配置和 Claude Code 配置会加载，关不掉（第 7 节）。这与 Claude Code 运行时（`--setting-sources project`，`looper/agents/claude_cli.py:40-67`）以及 Codex、Grok（各用 Looper 自己的家目录）都不同，文档和 Agent Runtimes 页要明说 | 真实 HOME 与双目录实验：真实 `HOME` 加 Looper 的目录，钥匙串登录可用（`apiKeySource: "login"`），操作者 `~/.cursor` 的配置文件不变；隔离 HOME 实验和钥匙串目录链接实验：换了 `HOME` 找不到钥匙串里的登录，链接钥匙串目录会挂住。不设 `CURSOR_DATA_DIR` 时项目数据写进操作者的 `~/.cursor/projects/`（真实 HOME 实验）。`CURSOR_AUTH_TOKEN` 会被写进钥匙串（2.4），必须去掉。原先的"文件存储加链接"要操作者另登录一次，不采用（第 6.6 节） |
| D9 | **`ttadk-cursoragent` 的 `agent`：** Looper 在自己的目录里放一个 `agent` 垫片，把它所在目录放到传给 TTADK 的 `PATH` 最前面；垫片只 `exec` Looper 启动时解析好的真正 `cursor-agent`：不改 `HOME`、不设文件存储，`CURSOR_CONFIG_DIR` 或 `CURSOR_DATA_DIR` 没设时拒绝运行，并去掉 TTADK 注入的 `TTADK_JWT_TOKEN`（第 6.7 节）。两个目录由 Looper 写进它给 `ttadk` 的环境。TTADK 的设置把 `cursor-cli` 映射成 `agent` 以外的命令时拒绝启动（`WRONG_TOOL`） | `agent` 可能是 Grok 等其他工具（2.3），所以垫片仍然需要；D8 之后 TTADK 和 Cursor 都用操作者的 `HOME`，不再需要在 TTADK 之后换 `HOME` |

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
  env CURSOR_CONFIG_DIR=<运行目录>/config CURSOR_DATA_DIR=<运行目录>/data cursor-agent
      -p --output-format stream-json --force --trust --sandbox disabled --disable-auto-update
      --model <模型> --plugin-dir <运行目录>/looper-plugin [--plugin-dir <已装插件>…] [--add-dir <知识目录>…]
  ```

  提示从 stdin 写入后关闭（`write_turn` 照 Codex 的写法，再写一轮抛 `RuntimeError`）；提示不进 argv。`HOME` 是操作者的；环境是 `child_env.inherited()` 加设置和 provider 的变量，不含 `AGENT_CLI_CREDENTIAL_STORE`、`CURSOR_API_KEY`、`CURSOR_AUTH_TOKEN`、`CURSOR_API_ENDPOINT`（设置或 provider 写了其中任何一个就拒绝启动），另设 `NO_OPEN_BROWSER=1`。
- **运行目录：** `<runtime_root>/.local/cursor-agent/runs/<sha256>/`（0700，按插件内容命名，同 `grok.write_home`；一天没写过的目录被删掉），里面是 `config/`（`CURSOR_CONFIG_DIR`）、`data/`（`CURSOR_DATA_DIR`）和 `looper-plugin/`。这些都是 Looper 自己的状态，不落到操作者的 `~/.cursor/`。
- **常驻会话（管家、规划者）：** 逐轮续接（`per_turn.PerTurnSession`），第二轮起加 `--resume <chatId>`，chatId 取自 `system/init` 的 `session_id`。会话在 `CURSOR_CONFIG_DIR/chats/<md5(cwd)>/<chatId>/`，所以续接要同一个 `CURSOR_CONFIG_DIR` 和同一个工作目录（常驻 owner 的工作目录本来固定）。每个常驻 owner 有自己的目录 `<runtime_root>/.local/cursor-agent/sessions/<label>/`，同样分 `config/`、`data/`、`looper-plugin/`；每轮前从这一轮的运行目录同步插件（同 `codex._sync_home` 的做法），运行目录过期删掉也不影响会话。
- **system prompt：** 写进 Looper 插件的 `rules/looper.md`（`alwaysApply: true`），前面是适配器的运行时说明（只读你最后一条消息；平台工具在 MCP 服务器 `looper` 里），后面是 Looper 的 prompt。
- **MCP：** 运行的 `mcp_config`（含 `looper` 服务器和令牌）写进 Looper 插件根目录的 `.mcp.json`（插件级不需要审批，2.4）。stdio 服务器的命令、参数和 `env` 照写（`HOME` 本来就是操作者的），HTTP 服务器照写；不支持的记进 `launch_record`。操作者 `~/.cursor/mcp.json` 的用户级服务器和启用的 Claude 插件带的 MCP 服务器也会加载（第 7 节）。
- **已装插件和 skill：** Looper 的已装插件本来是 Claude Code 格式（`.claude-plugin/plugin.json`），Cursor 的 `--plugin-dir` 认这个格式，直接逐个传。插件里 Cursor 不支持的部分照实记进 `runtime_catalog.skipped`（核实前先保守：只声明 skill 和 MCP 能用）。
- **隔离（D8）：** 没有。Cursor 进程用操作者的 `HOME`，操作者的个人 Cursor 和 Claude Code 配置会加载（第 7 节）；Looper 只管住自己写的东西（上面的运行目录）。agent 的 shell 和 MCP 服务器拿到的也是操作者的 `HOME`（真实 HOME 实验）。`isolation_id = "cursor-operator-home/v1"`；运行时类型声明 `loads_operator_config = True`（运行时视图带出，Agent Runtimes 页据此写明）；`launch_record` 记 `personal_config: ["cursor", "claude-code"]`；prompt 视图多一块"操作者的 Cursor 与 Claude Code 配置（不显示）"。
- **登录（D8）：** 内置 provider `prov-cursor-login`（vendor `cursor-login`，OAuth，`oauth_runtime_type = 'cursor-agent'`，端点协议沿用 `grok-login` 的写法记为 `openai-responses`，只用于配对），代表操作者本机的 Cursor 登录。登录检查是 `cursor-agent status --format json`：操作者的 `HOME`，一对用完即删的临时 `CURSOR_CONFIG_DIR`/`CURSOR_DATA_DIR`，去掉上面那 4 个变量，`NO_OPEN_BROWSER=1`，stdin 为空，20 秒超时。以 0 退出且 `status` 为 `authenticated`、`isAuthenticated` 为真才算已登录；`unauthenticated`、`partially-authenticated` 为未登录；退出码非 0、超时、读不懂为检查失败。结果缓存 30 秒；列 provider 时立即返回已有缓存（即使已过期），没有缓存时返回检查中，缺失或过期时最多启动一个后台检查。启动检查与 doctor 同步取得有效缓存或执行检查。未登录时运行不启动，原因写"Cursor is not signed in on this machine: run `cursor-agent login`"；检查失败时写明原因。状态显示在 Settings → LLM Providers 和 `cursor_login` 自检里（自检只在有 Agent Profile 用它时做）。Looper 不读钥匙串、不读凭据文件；没有链接，也没有要操作者清除的"分叉"记录。用 JSON 而不是匹配文本里的 `Logged in as`：文本只在取到邮箱时才这样写（2.4）。
- **事件（`CursorStream`）：** `system/init` 加 `runtime: "cursor-agent"`；回显提示的 `user` 丢掉；`assistant` 原样；`tool_call/started` → `assistant` 的 `tool_use`（名字取 `tool_call` 里以 `ToolCall` 结尾的那个键，如 `shellToolCall` → `shell`，`mcpToolCall` → `mcp__<server>__<tool>`），`tool_call/completed` → `user` 的 `tool_result`；`thinking/delta` 的文本攒起来，`thinking/completed` 时合成一个 `thinking` 块；`connection/*`、`retry/*` → `system` 事件 `cursor_connection`、`cursor_retry`（带阶段和 `attempt`）。**报告**是最后一条助手消息：最后一个 `tool_call` 和最后一个 `retry/resuming` 之后的助手文本（同 `CodexStream` 的 `_last_text` 和 `claude_cli.answer_text` 的约定：工具调用之前说的话不算），去掉开头的 `> Auto routed to <模型>` 一行，实际模型另记为 `routed_model`；`result/success` 的 `result` 换成这份报告（不用 `result.result`，它拼接了各次尝试），`usage` 换成 `modelUsage`（`inputTokens`、`outputTokens`、`cacheReadInputTokens`、`cacheCreationInputTokens`）并先发一个内部 `usage` 事件；其余事件记成 `system`。出错时 Cursor 不发 `result`：适配器不补，交给"没有 result 即失败"的现有规则，`failure_detail` 带 stderr 末尾。
- **强度：** Cursor 没有强度参数（只有模型 id 的方括号写法），v0 不支持 effort（`effort_levels = ()`）。
- **花费：** Cursor 不报花费，`cursor-login` 的模型没有价格：只记 token，花费未知（`cost_source = provider_price`，没有价格时就是未知）；`runtime_cost_capped = False`，建档时照 Grok 的做法警告单次上限停不住它。
- **只读：** 规划者和咨询用 `ReadOnly.CHECK`（规划者每轮前后比较任务目录）。`--mode plan` 下 MCP 工具还能不能用没核实，规划者离不开它的平台工具，所以 v0 不用 plan 模式；咨询不在 `consult.RUNTIME_READ_ONLY` 里，按现有规则在一次性副本里跑。
- **取消：** 进程组 SIGTERM，退出码 143，没有 `result`；shell 命令自建进程组，`proc_tree` 按树和组结束它们（推断，第 10.2 节第 6 项）。
- **能力：** `run`、`resident_session`、`resume`、`mcp`、`add_dirs`；不声明 `structured_output`、`claude_plugins`、`cost_report`、`steer`。
- **探测：** `cursor-agent --version`（如 `2026.10.22-27dcf5f`），环境同登录检查（操作者的 `HOME`，用完即删的两个目录）。

### 6.5 各角色（`cursor-agent`）

| 角色 | 能否用 | 说明 |
|---|---|---|
| 管家、规划者 | ✅（核实 MCP 后） | 逐轮 `--resume`；平台工具靠插件 `.mcp.json`；不能中断 |
| Worker、Reviewer | ✅ | 一次 `-p` 运行；运行中收不到规划者的消息（与 Grok headless 相同） |
| 咨询 | ⚠️ 一次性副本 | 没有分叉会话的参数（`consult_command` 抛 `RuntimeUnsupported`，按现有规则转给规划者） |

### 6.6 不选的 Cursor 接法

- **ACP（`agent acp`）：** 有 `session/new` 注入 MCP、`session/load`、`session/cancel`，能做可中断的常驻会话，但 `session/prompt` 不报用量（2.4 的调研），且要一个新的 JSON-RPC 客户端。v0 先用 headless 加 `--resume`，ACP 记为后续（同 Grok 先 headless 后 ACP 的顺序）。
- **API key（`CURSOR_API_KEY`）：** 用 key 认证时 Cursor 会把换来的令牌写进凭据存储，默认是钥匙串，可能覆盖操作者的登录（2.4）；要配 `AGENT_CLI_CREDENTIAL_STORE=memory`，团队策略也可能不允许 key。维护者不用（D8）。
- **临时 `HOME` 加文件存储登录链接（原 D8）：** 能把操作者的个人配置挡在外面，但要操作者另用 `AGENT_CLI_CREDENTIAL_STORE=file cursor-agent login` 登录一次，Looper 再把每个家目录的 `.cursor/auth.json` 链接过去，还要检查和修复链接。维护者 2026-11-04 决定沿用现有的钥匙串登录，不采用。
- **`--exclude-workspace-context`（隐藏参数）：** 能跳过 `~/.claude/commands`，但同时去掉项目的规则、skill 和笔记，而 Looper 要项目的 `AGENTS.md` 等；也挡不住 hook、MCP 和个人 skill。不用。
- **按工作区停用个人 MCP（`mcp-disabled.json`，2.4）：** 能挡住 `~/.cursor/mcp.json` 里的服务器，但文件名和格式未核实，要先知道服务器名，也挡不住 Claude 插件带的服务器。v0 不做，记为后续。

### 6.7 Cursor Agent 经 TTADK（`ttadk-cursoragent`）

```
env CURSOR_CONFIG_DIR=<运行目录>/config CURSOR_DATA_DIR=<运行目录>/data PATH=<Looper 的垫片目录>:<原 PATH>
    ttadk code -t cursor-cli --skip-check --
    -p --output-format stream-json --force --trust --sandbox disabled --disable-auto-update --model <模型> …
```

- 进程用操作者的 `HOME`：TTADK 找得到它的 SSO 登录，Cursor 找得到钥匙串里的登录（D8）。两个目录和原生运行的一样，由 Looper 写进给 `ttadk` 的环境，TTADK 原样传给工具（`cursor-cli` 不注入，2.3）。不设文件存储。
- 垫片 `<runtime_root>/.local/cursor-agent/ttadk-bin/agent`（0700，内容由 Looper 生成，按内容重写）。`agent` 可能指向 Grok 等其他工具，所以仍需要垫片；它只确认这是 Looper 的 Cursor 运行、去掉 `TTADK_JWT_TOKEN`，然后 `exec` 真正的 `cursor-agent`：

  ```sh
  #!/bin/sh
  # Written by Looper: TTADK runs `agent` for cursor-cli; this runs Cursor's CLI in Looper's Cursor directories.
  [ -n "$CURSOR_CONFIG_DIR" ] && [ -n "$CURSOR_DATA_DIR" ] || { echo "..." >&2; exit 2; }
  unset TTADK_JWT_TOKEN
  exec '<解析好的 cursor-agent 绝对路径>' "$@"
  ```

  两个目录有一个没设时垫片以 2 退出并在 stderr 说明，不在操作者默认的 Cursor 目录里运行。
- TTADK 1.0.37（2026-11-04 只读 `ttadk:core/command/code.js:1150-1290`、`ttadk:core/ai-tools/profiles/cursor-cli.js`）：`cursor-cli` 仍默认运行 `agent`，找不到时回退 `cursor-agent`；`cursor-cli` 没有 `PATH` 前缀，垫片目录仍在最前；工具的环境在没有预设时多出 `TTADK_JWT_TOKEN`（TTADK 换来的令牌，供 AI 调用 TTADK 的工具），Looper 的 agent 用不到，垫片去掉它（推断：Cursor 的 shell 会继承它，第 10.2 节第 13 项）；TTADK 设置里 `code.approve_mcps` 为真时会给 `cursor-cli` 加 `--approve-mcps`。
- 启动前：读 TTADK 设置里的 `ai_tool_commands.cursor-cli`（只读这一项），不是 `agent` 时 `WRONG_TOOL`："TTADK 的设置让 cursor-cli 运行 `<命令>`，Looper 不能确认它是 Cursor CLI"。
- 提示从 stdin 进（TTADK 继承 stdin），不作位置参数（位置参数里的 `-m…` 会被 TTADK 吃掉，2.3）。
- 记录：`launch_record` 的 `launcher.tool = "cursor-cli"`。TTADK 对 cursor-cli 只发启动事件；停止和超时时 node 先死，事件发不出（2.3），见第 11 节问题 1。
- 各角色与原生 `cursor-agent` 相同；每个进程多出 TTADK 的准备时间（推断与 Codex 相近，未实测）。

## 7. 安全与隔离

- **不改操作者的配置。** TTADK 只在不带 `--skip-check` 时同步插件、写项目和 `CODEX_HOME` 的配置（`ttadk:core/ai-tools/codex-hooks.js:45-58`），Looper 总是带它；P1 前后 `config.toml` 哈希不变。`--app`、`--wrapper`、`-p` 永远不传，`ttadk.check_args` 不管这些（它们只能出现在 `--` 之前，由 Looper 自己拼），由命令构造保证并有测试钉住。
- **SSO 令牌。** TTADK 把 SSO 令牌放进 codex 子进程的 `TTADK_OPENAI_API_KEY`；Looper 在 TTADK 运行的 `config.toml` 里把它排除出 agent 的 shell 环境。令牌从不进运行记录和日志：`launch_record` 只记版本、工具和模式；前导输出里的邮箱被现有读取器丢弃，`failure_detail` 去掉网址和邮箱。
- **TTADK 写的文件**都在 `~/.ttadk/`（登录状态缓存、安装 id、刷新后的登录），是 TTADK 自己的状态，与操作者在终端里用 TTADK 相同。
- **Cursor 的登录（D8）。** Looper 不读钥匙串、不读凭据文件，只跑 `cursor-agent status` 看结果；去掉会让 Cursor 改用 key、令牌、别的端点或别的凭据存储的变量（`CURSOR_AUTH_TOKEN` 会被写进钥匙串，绝不传）。agent 的 shell 本来就是操作者的 `HOME`，能用操作者能用的一切，与 Codex、Grok 的 shell 拿回操作者 `HOME` 时相同。
- **Cursor 运行会加载的操作者配置（关不掉，维护者已接受）：**
  - Cursor 的：`~/.cursor/hooks.json` 的 hook，`~/.cursor/mcp.json` 的用户级 MCP 服务器（不需要审批），`~/.cursor` 下的 skill、用户规则和插件，团队托管设置和 Cursor 的团队托管目录里的团队 hook（每次启动重写，双目录实验）；
  - Claude Code 的（兼容写死打开，2.4）：`~/.claude/settings.json` 的 hook 和权限、`~/.claude/commands`、`~/.claude/CLAUDE.md`、启用的 Claude 插件（skill、MCP 服务器等）；另有 `~/.codex`、`~/.grok`、`~/.agents` 下的 skill；
  - 规则从工作目录收集到 `/`：工作目录在 `HOME` 下时，`~/AGENTS.md`、`~/CLAUDE.md`（如有）也进来；
  - 账号侧：每次启动把 `~/.claude/plugins/marketplaces` 导入操作者的 Cursor 账号；
  - 后果：操作者的个人 hook 可能在 Looper 的运行里执行（真实 HOME 与双目录实验里观察的 `UserPromptSubmit` hook 没有触发，别的事件未核实）；个人指令会改变 agent 的行为（真实 HOME 实验：英文提示得到中文回复）；个人 MCP 服务器和 skill 进入 agent 的工具和上下文（真实 HOME 实验的思考提到个人 skill）；个人配置里若有同名的 `looper` MCP 服务器，哪一个生效未核实。
  - 与其他运行时的差别：Claude Code 用 `--setting-sources project` 排除个人配置（`looper/agents/claude_cli.py:40-67`），Codex、Grok 用 Looper 自己的家目录；Cursor 是唯一加载操作者个人配置的运行时。`agent-system.md`、Agent Runtimes 页和每次运行的记录（`personal_config`）都写明。
- **Cursor 写到操作者目录的东西。** 真实 HOME 实验前后操作者的 `cli-config.json`、`agent-cli-state.json`、`mcp.json`、`prompt_history.json` 不变；Cursor 每次启动自己重写团队托管 hook 目录和 `~/.cursor/skills-cursor/` 的同步清单（双目录实验，与操作者自己用 Cursor 时相同）。Looper 总是设 `CURSOR_DATA_DIR`，项目数据不进 `~/.cursor/projects/`（真实 HOME 实验没设时进了）。
- **垫片**只在 `ttadk-cursoragent` 的进程 `PATH` 里，不在操作者的 `PATH` 里；它执行的绝对路径由 Looper 在启动时解析（`shutil.which("cursor-agent")` 的 `realpath`），并去掉 `TTADK_JWT_TOKEN`（6.7）。

## 8. 可观测性

| 看什么 | 在哪 |
|---|---|
| 这次运行是否经过 TTADK、版本、工具、模式 | 运行记录 `metadata.launcher` 和 `metadata.profile.runtime.launcher`；任务动态的 `$ …` 命令行本来就显示 `ttadk codex …` |
| TTADK 能不能用 | Agent Runtimes 页 `ttadk-codex`/`ttadk-cursoragent` 行的版本和 `probe_error`；`looper doctor` 与 `/api/v1/system/status` 新增 `ttadk` 检查（只在有 Agent Profile 用 TTADK 行时检查）：`ttadk` 不在 PATH、没有登录 → fail，检查超时 → warn |
| 启动为什么失败 | 任务动态的错误行：`TtadkProblem` 的说明和修复办法，或 TTADK stderr 的末尾 |
| Cursor 登录能不能用 | Settings → LLM Providers 的 `cursor-login` 行（`cursor-agent status --format json` 的结果，30 秒缓存，后台刷新，列表不等待检查）；`looper doctor` 与 `/api/v1/system/status` 的 `cursor_login` 检查（只在有 Agent Profile 用它时检查）：未登录 → fail，检查失败 → warn |
| Cursor 运行有没有断线重试、Auto 用了哪个模型 | 任务动态里的 `cursor_connection`、`cursor_retry` 系统事件；运行记录里的 `routed_model` |
| 时延 | 现有的回合耗时；TTADK 的额外开销能从 `init` 事件的时间与进程启动时间之差看出。v0 不加新指标 |

## 9. 风险

| 风险 | 影响 | 处理 |
|---|---|---|
| SSO 在检查之后、启动之前失效，TTADK 进入设备授权：打开浏览器、阻塞 | 这次运行挂到它的超时为止 | 启动前检查（60 秒缓存）把窗口压到很小；残余风险记为已知，后续可加"启动后 N 秒没有任何 JSON 就结束"的看门狗（第 12 节待办） |
| TTADK 升级后改了参数解析、前导输出或注入方式 | 启动失败或悄悄换了行为 | 探测记版本；真实路径验证用真实 TTADK 跑 `exec`、续接和 `app-server`；以后可加一个 `installed` 标记的 nightly 用例（只做 `app-server` 握手，不调模型） |
| 每个进程多 4-20 秒 | 管家、规划者每轮更慢 | 已知代价，写进 RFC 和运行时说明；不做常驻优化 |
| Codex 的 `-c` 来源顺序 | TTADK 的 `-c` 在根上；Looper 若将来在命令行上加 `-c model_provider=…` 会绕过网关 | `ttadk` 模式下命令里出现 `model_provider` 的 `-c` 时 `RuntimeUnsupported`，有测试 |
| cursor-cli 的工作量记录很薄 | 只有启动事件，停止时还会丢 | 第 11 节问题 1 |
| Cursor 团队策略以后禁用 headless 或 "Run Everything" | 运行直接退出 1，或 `--force` 无效 | 现在允许（真实 HOME 与双目录实验）；设置由服务端下发、取不到时放行，随时可能变；stderr 进运行失败信息 |
| 操作者的个人配置进入 Looper 的 Cursor 运行（D8） | 个人 hook 可能执行；回复语言和行为随个人指令变；个人 MCP 和 skill 进入工具集；同名的个人 `looper` MCP 服务器可能顶替平台工具（未核实） | 维护者已接受；文档、Agent Runtimes 页和运行记录写明；按工作区停用个人 MCP 记为后续（6.6） |
| 从服务进程读钥匙串 | `looper serve` 不在操作者的图形登录会话里（SSH、系统级服务）或钥匙串锁着时，Cursor 可能读不到登录，或像钥匙串目录链接实验那样等授权挂住 | 登录检查 20 秒超时、判为检查失败、拒绝启动；真实 HOME 与双目录实验在终端里跑，服务进程里未核实（10.2 第 11 项） |
| 断线重试重复输出 | `result.result` 拼接各次尝试；重试前的助手文本在任务动态里出现两遍 | 报告只取最后一条消息（6.4）；重复的动态行可以接受 |
| 列 provider 时刷新 Cursor 登录检查 | 装了 `cursor-agent` 的机器上，打开控制台时每 30 秒最多一个后台子进程和一次联网（取用户信息），并由 Cursor 读钥匙串 | 列表立即返回已有缓存，无缓存时显示检查中；最多一个后台检查，20 秒超时；不保存或输出 `userInfo` 内容 |
| `agent` 被别的工具占用（如 Grok） | TTADK 拉起错误的程序 | 垫片（D9）+ 设置检查 |

## 10. 测试与验证

### 10.1 测试（按开发计划第 1 节：一条规则一个测试，测在离入口最近的一层）

- 启动器：TTADK 行的注册条件（`ttadk` 在不在 PATH）、视图、导入导出里的 `launcher`；`auth_kinds_for` 拒绝 API key provider（profile 建档层，一个参数化测试）。
- Codex 命令：TTADK 行的 `run_command`、`resident_command` + `resume`、`steer_command`、`consult_command` 都以 `env CODEX_HOME=… ttadk codex --skip-check --mode mixed --` 开头，且 `--` 之后与原生逐字节相同（一个参数化测试，对比原生命令）；原生命令不变（现有测试守住）。
- 事件：一个假的 `ttadk` 脚本先打印若干前导行再 `exec` 一个假 Codex（`tests/` 里现有的替身方式），Worker 和常驻会话都能读出结果；假 TTADK 以 1 退出且没有 JSON 时，运行失败信息带 stderr 末尾。
- 失败：每个 `TtadkProblem` 一行参数化，入口是 `login_problem`；断言从不起原生进程。
- Cursor：`CursorStream` 用真实 HOME 实验的真实输出测翻译（路径、会话、请求和调用 id 换成占位符，存为 `tests/fixtures/cursor-stream-sample.jsonl`，含两次重试），另加一个手写的无重试小样本；命令、两个目录和插件目录的布局各一条；登录检查的各个状态一条（替身 `cursor-agent`）；一个耗时 2 秒的检查替身验证 provider 视图在 0.2 秒内返回检查中，完成后返回检查结果，且已有过期缓存也立即返回；垫片内容与 `PATH` 顺序一条。测试里 `PATH` 上最前面是一个拒绝一切的 `cursor-agent` 替身，不碰操作者的登录。
- 不测：TTADK 和 Cursor 自身的行为（交给真实路径验证的真实验证）。

### 10.2 未核实的事实与核实步骤

| # | 事实 | 怎么核实（每项最多一次 "pong" 级调用） |
|---|---|---|
| 1 | ✅ 钥匙串里的团队登录在 Looper 的布局下可用（真实 `HOME`，Looper 的 `CURSOR_CONFIG_DIR`/`CURSOR_DATA_DIR`，去掉 4 个变量）。原来的"文件存储加链接"随 D8 作废 | 已核实：真实 HOME 与双目录实验以 0 退出，`apiKeySource: "login"` |
| 2 | ✅ 提示从 stdin 读（无位置参数） | 已核实：真实 HOME 与双目录实验|
| 3 | `--plugin-dir` 的 `rules/looper.md`（`alwaysApply`）和 `.mcp.json` 生效 | 未核实（真实 HOME 与双目录实验没带插件目录）。真实路径验证的一次运行：规则要求回复以固定标记结尾，提示要求调用探针 MCP 的 `ping` 并原样回复；看回复和探针日志 |
| 4 | ✅ stream-json 的真实形状，`usage` 出现 | 已核实：真实 HOME 与双目录实验（2.4 的事件一行）；新看到 `connection/*`、`retry/*`，`result.result` 拼接各次尝试。真实 HOME 实验的输出去掉路径和 id 后是测试样本 |
| 5 | ✅ Cursor 的 shell 拿到的 `HOME` | 已核实：真实 HOME 实验里是操作者的真实家目录 |
| 6 | SIGTERM 后 shell 子进程和 `worker-server` 是否残留 | 未核实。让一次运行执行 `sleep 60` 时按 `proc_tree.end` 结束，再看进程表 |
| 7 | `ttadk code -t cursor-cli` 经垫片拉起 Cursor 且 stream-json 原样透传 | 未核实。按第 6.7 节的命令跑一次 pong，进程树应是 `ttadk` → 垫片 → Cursor 的 node，不是 Grok |
| 8 | ✅ 团队策略允许 headless 和 `--force` | 已核实：真实 HOME 与双目录实验|
| 9 | Codex 经 TTADK 时 MCP 平台工具可用（`config.toml` 不变已证实，工具调用未跑） | 管家在 `ttadk-codex` 上跑一轮会调用 `looper` 工具的对话（真实路径验证） |
| 10 | `ttadk models -t codex -f json` 在未登录时以非零退出、不进设备授权 | 读代码已确认路径（`ttadk:core/command/models.js:29-42`）；实测需要先登出 SSO，不做，单测用替身覆盖 |
| 11 | `cursor-agent status --format json` 在 Looper 的环境下（操作者的 `HOME`、临时的两个目录）按 2.4 输出，已登录时 `status` 为 `authenticated`；从按操作者服务方式（launchd）运行的 `looper serve` 里也读得到钥匙串登录 | 2026-11-04 已核实 JSON 形状：退出码 0，`status: "authenticated"`、`isAuthenticated: true`，另有 `hasAccessToken`、`hasRefreshToken` 和 `userInfo`，用时 1.3–2.0 秒，操作者的 `~/.cursor/projects` 无新增内容；不保存或输出 `userInfo` 内容。服务进程的钥匙串访问仍需在上线后看 Settings → LLM Providers 的 `cursor-login` 行核实 |
| 12 | 常驻会话 `--resume` 之后 `result.usage` 是这一轮的还是整个会话累计的 | 管家在 `cursor-agent` 上跑两轮，比较两轮的 `usage` |
| 13 | TTADK 1.0.37 注入的 `TTADK_JWT_TOKEN` 到不了 agent 的 shell（`cursor-cli` 由垫片去掉；`codex` 靠 Codex 默认排除名字含 `TOKEN` 的变量，推断） | `ttadk-cursoragent` 和 `ttadk-codex` 各跑一次，让 agent 执行 `env` 并只回答有没有这个变量 |

## 11. 默认行为的变化与待决问题

### 11.1 默认行为的变化

- 没有 `ttadk` 的安装：没有变化（不注册 TTADK 行，原生命令逐字节不变）。
- 有 `ttadk` 的安装：Agent Runtimes 多出 `ttadk-codex`（以及 Cursor 的 TTADK 接入之后的 `ttadk-cursoragent`）；不选它们的 Agent Profile 不受影响。
- 所有安装：LLM Providers 多出内置的 `cursor-login`（与 `grok-login` 一样随 seed 写入）。列 provider 时立即返回已有登录缓存，无缓存时显示检查中；缓存缺失或超过 30 秒时最多启动一个后台 `cursor-agent status --format json` 检查，不等待子进程。Cursor 会联网取用户信息并读钥匙串，Looper 不保存或输出 `userInfo` 内容。
- `cursor-agent` 在 PATH 上的安装：多出运行时 `cursor-agent`。
- **Cursor 运行加载操作者的个人配置**（D8，维护者已接受）：与 Claude Code、Codex、Grok 运行时不同，Cursor 进程用操作者的真实 `HOME`，个人的 Cursor 和 Claude Code 配置（hook、MCP、skill、规则、插件）都会生效；Agent Runtimes 页、`agent-system.md` 和每次运行的记录写明。
- 入口：Telegram、飞书和 CLI 不需要改，它们经管家和任务流程运行，运行时由 Agent Profile 决定；控制台的 Agent Profile 表单和管家工具用已有的 `runtime_id`。

### 11.2 待决问题（附推荐默认值）

1. **cursor-cli 在 TTADK 里算不算工作量？** TTADK 对 cursor-cli 不注入网关，只发一个启动事件（2.3），运行被停止时这个事件还会丢。**推荐：** 照做 `ttadk-cursoragent`（在原生 `cursor-agent` 之上只多一张卡），不设 `CI=1` 保住启动事件；请维护者向 TTADK 的维护方确认 cursor-cli 的统计口径，如果统计靠 Cursor 自己的企业账号，`ttadk-cursoragent` 与原生的差别只在这个事件。补充（2026-11-04，推断）：Cursor 的团队托管设置每次启动都装上一组团队 hook（覆盖会话开始、提交提示、shell、文件编辑、结束等事件，双目录实验），组织可能借此统计 Cursor 的用量；这些 hook 在 headless 下是否执行未核实。
2. **Cursor 的登录用什么？** **已定（维护者，2026-11-04）：** 沿用操作者本机现有的登录（macOS 钥匙串里的团队登录）；不用文件存储、API key 或登录链接；接受操作者的个人配置会加载（D8，第 7 节）。
3. **要不要 TTADK-only 模式（SSO + 网关模型，不用 ChatGPT 订阅）？** **推荐：** v0 不做。以后要做时，新增内置 provider `ttadk-gateway`（模型取自 `ttadk models -t codex -f json`），只配 TTADK 行，命令改成 `--mode ttadk-only -m <网关模型> --`；它是另一个 provider，按 P5 自动新开会话。

## 12. 实施顺序与后续

先交付运行时行的启动器字段、TTADK 公共启动模块、Codex 的注册与探测、命令、失败记录和自检。Cursor 的接入依次交付 `cursor-login` provider 与登录检查（`cursor-agent status`）、单轮运行与事件翻译（含重试）、Looper 的两个目录和插件目录、逐轮续接，再接 TTADK 包装器。钥匙串登录、stdin 提示和团队 headless 策略已于 2026-11-04 通过真实 HOME 与双目录实验核实（10.2 第 1、2、8 项）。界面随后补充启动器说明、provider 过滤和 Cursor Agent 的个人配置说明；Cursor 的登录帮助随登录检查交付；最后验证真实 TTADK 路径。

后续待办：TTADK 启动看门狗（第 9 节）；Cursor 的 ACP 常驻会话（6.6）；按工作区停用操作者的个人 MCP（6.6）；TTADK-only 模式（11.2，待维护者决定）。
