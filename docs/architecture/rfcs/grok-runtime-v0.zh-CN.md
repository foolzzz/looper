# RFC：Grok 运行时（v0）

- **状态：** 已实现（M9f）。第 2 节的接口事实取自本机 Grok CLI 1.0.62 的 `--help`、它随附的用户文档
  （`$GROK_HOME/docs/user-guide/`）和第 12 节的实跑核实（2026-10-17，Grok 自己的登录，`grok-4.7`）；仍未核实的
  几项在第 12 节写明。
- **依据：** [多运行时 agent 架构 RFC](multi-runtime-agents-v0.zh-CN.md)（适配器接口第 5 节、隔离第 10 节、P1 登录）；
  [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 M9 节。
- **最近更新：** 2026-10-17

## 1. 背景与目标

Looper 已有三种运行时：Claude Code、Codex（含它自己的 ChatGPT 登录 `codex-login`）和 OMP。维护者要求（2026-10-17）
本版本加入第四种：xAI 的 Grok CLI（`grok`，Grok Build），四类角色（管家、规划者、Worker、Reviewer）都能用，并像
`prov-codex-login` 那样内置一个用 Grok 自己 OAuth 登录的 provider `prov-grok-login`。

本机装的是原生的 `grok` 1.0.62（"Grok Build TUI"，`~/.grok/downloads/grok-macos-aarch64`），带 OAuth 的
`grok login`。GitHub 上的 `superagent-ai/grok-cli` 是另一个产品（TypeScript/Bun 写的 `grok-dev`，要 xAI API key，参数是
`--prompt`、`--format json`），参数和事件格式都不同，Looper 不针对它。

**目标：**

- 运行时类型 `grok`，`looper serve` 在 PATH 上找到 `grok` 时自动注册内置运行时 `rt-grok`；
- 内置 provider `prov-grok-login`（OAuth，只配 Grok 运行时），共用操作者在本机的登录，不复制凭据；
- 隔离：不读操作者的 `~/.grok` 配置、skill、记忆、插件和 hook，也不读 Grok 兼容加载的 `~/.claude`、`~/.cursor`、
  `~/.agents`；
- 管家和规划者的平台工具、办公台（Agent Room）的 `run.` 令牌经 MCP 注入；
- Agent Runtimes 页列出 Grok，Agent Profiles 能选 `grok` 加 `grok-login`；`looper doctor` 检查 Grok 登录；
- 不配 Grok 的安装，行为、prompt 和命令行逐字节不变。

**不做（本期）：**

- Grok 接 API key provider（xAI key、CPA 等）：Grok 的自定义模型支持 `chat_completions`、`responses`、`messages`
  三种接口（`[model.<id>]` 的 `api_backend`、`base_url`、`env_key`），以后可以照 OMP 的做法接，本期只做登录；
- ACP（`grok agent stdio`）常驻进程：见第 5 节。

## 2. Grok CLI 的非交互接口（1.0.62）

✅ 实跑核实，📄 只有文档，❌ 不支持。

| 能力 | Grok 1.0.62 |
|---|---|
| 一次性运行 | ✅ `--prompt-file /dev/stdin`：headless 不读 stdin 里的提示（文档原话），但把 `/dev/stdin` 当文件读完整个管道；跑完这一轮就退出 |
| 事件流 | ✅ `--output-format streaming-messages-json` 就是 Claude Code 的 stream-json：`system`/`init`（`session_id`、`model`、`apiKeySource: "oauth"`、`tools`、`skills`、`mcp_servers`）、每次模型响应一条 `assistant`（`text`、`thinking`、`tool_use`，带这次响应的 `usage`）、`user`（`tool_result`）、终止的 `result`（`total_cost_usd`、`usage`、`modelUsage`）。不加 `--include-partial-messages`，就没有 `stream_event` |
| 常驻会话与续接 | ✅ 每轮一个进程，`--resume <id>`：同一 `GROK_HOME`、同一工作目录续接，记得上一轮；`result` 的 token 和花费只算这一轮。会话不存在：退出码 1，stdout 没有事件（stderr 说先在本地找，再去 xAI 的会话库找，404）。会话存在 `$GROK_HOME/sessions/<编码后的工作目录>/<id>/` |
| 分叉 | ✅ `--resume <id> --fork-session`：新会话 id，带着原会话的历史 |
| 运行中追加用户轮 | ❌ headless 的流是只读的；双向交互只有 ACP（第 5 节） |
| system prompt 注入 | ✅ 家目录的规则文件 `$GROK_HOME/rules/*.md`，每次启动都读（不要求信任目录）；`--rules` 只收文字；`--system-prompt-override` 整个替换 |
| skill 与插件 | ✅ 原生 `SKILL.md`。📄 `[skills] paths` 加目录。**按 `HOME` 找的用户级来源**：`~/.agents/skills`、`~/.claude/plugins` 里的 Claude Code 插件（连同它们的 skill、hook、agent 和 MCP 服务器）、`~/.claude/settings.json` 的权限规则，`compat` 开关都管不到（实测：只设 `GROK_HOME` 时 `grok inspect` 列出 210 个 skill、4 个插件、一个 HTTP MCP 服务器） |
| MCP | ✅ `config.toml` 的 `[mcp_servers.<名称>]`：stdio（`command`、`args`、`env`）、HTTP（`url`、`headers`）、SSE（`url`、`type = "sse"`，`grok mcp add -t sse` 的写法）。模型先 `search_tool` 查、再 `use_tool` 调，工具名 `<服务器>__<工具>`；`init` 里这个服务器是 `pending`。`GROK_CONFIG` 覆盖层不收 MCP |
| 项目说明文件 | ✅ 仓库根到工作目录每一级都读，每一级读**所有**认得的文件名（`Agents.md`、`Claude.md`、`CLAUDE.md`、`CLAUDE.local.md`、`AGENT.md`、`AGENTS.md`）；不在仓库里时只读工作目录；另读 `<dir>/.grok/rules/*.md`。要信任目录：`--trust`（`--help` 不列出，但接受，信任记在 `$GROK_HOME/trusted_folders.toml`） |
| 额外目录（知识） | ❌ 没有 `--add-dir`；按 prompt 里知识索引给出的路径读（与 OMP 同） |
| 不限权限 | ✅ `--always-approve`（`permissionMode: "bypassPermissions"`）；沙箱默认 `off` |
| 只读 | ✅ `--tools read_file,grep,list_dir` 在 `--resume … --fork-session` 上生效：写不了文件，工作目录不变。📄 沙箱在会话开始时定下，续接时换 `--sandbox` 被拒绝 |
| 强度 | ✅ `--effort`：`grok-4.7` 只有 `low`、`medium`、`high`、`xhigh`；`max` 让运行在请求之前失败（`result` 是 `error_during_execution`，"unknown effort level 'max'"，退出码 1） |
| 花费报告 | ✅ 用 Grok 自己的登录时 `result.total_cost_usd` 也有值（如一轮 $0.0148），`modelUsage` 的键是服务端的模型名（`grok-4.7-build`）。📄 不知道时这个格式写 0 |
| 压缩事件 | 📄 `system`/`compact_boundary`；没有实跑触发 |
| 结束 | ✅ 被 SIGTERM 时 Grok 连同它的 shell 和 shell 里的命令一起结束（3 秒内），没有 `result` |
| leader 进程 | ✅ `[cli] use_leader = false` 时 headless 不起、不连 leader，`~/.grok` 和 Looper 的家目录里都没有 socket |

## 3. 命令行

### 3.1 一次运行（Worker、Reviewer）

```
env GROK_HOME=<家目录> HOME=<家目录> grok
  --prompt-file /dev/stdin                    # 提示仍由 write_turn 写进 stdin 再关掉，不进 argv
  --output-format streaming-messages-json
  --always-approve --trust --no-auto-update
  -m <模型> [--effort <强度>] [--max-turns <N>]
```

- 提示不进命令行：Looper 的运行记录、任务动态和 `ps` 都看得到 argv。`write_turn` 照 Codex、OMP 的写法写完即关，
  再写一轮抛 `RuntimeError`。
- system prompt 写进家目录的 `rules/looper.md`：前面是适配器的"运行时说明"（"Looper 只读你最后一条消息"；有 MCP
  服务器时再说怎么调它们的工具），后面是 Looper 的 prompt 原文。
- `HOME` 也指向这个家目录：Grok 按 `HOME` 找的用户级来源（第 2 节）都不在那里。
- 工作目录由 Looper 给子进程，不另传 `--cwd`；强度原样传 `--effort`（`max` 由 Grok 拒绝，见第 2 节）。

### 3.2 常驻会话（管家、规划者）

逐轮续接（`per_turn.PerTurnSession`，`ResidentGrok`）：每轮一个 3.1 的进程，第二轮起加 `--resume <id>`；会话 id
取自 `init` 的 `session_id`。会话在 `GROK_HOME` 里，而一次运行的家目录随生成的配置变（第 7 节），所以每个常驻 owner
有自己的家目录 `<runtime_root>/.local/grok/sessions/<label>`（同时是它的 `HOME`），每轮开始前从这一轮的运行家目录
同步 `config.toml`、`rules/looper.md`、登录链接和共享目录的链接（同 `codex._sync_home`）。system prompt 改了（命令
变了）就重启会话，续接时 Grok 读新的规则文件。开新会话的那一轮删掉这个 owner 之前的会话。

### 3.3 办公台的会话保留与咨询（另一条线在做）

- 办公台要求 Worker、Reviewer 的运行保留会话（Agent Room RFC D13，`run_command` 的 `session_dir`）：Grok 一直把
  会话存在 `GROK_HOME` 里，没有不存的选项；`session_dir` 就是这次运行的家目录，按 3.2 的方式同步配置和链接。
- 咨询是原会话的分叉：`--resume <id> --fork-session`；只读见第 9 节。

## 4. 事件解析（`GrokStream`）

### 4.1 透传与补齐

- `system`（`init`、`compact_boundary` 等）：透传，`init` 加 `runtime: "grok"`；
- `assistant`：透传，后面跟一个内部 `usage` 事件，是这次响应的 token（`message.usage` 就是这次响应的最终用量，
  与 `--include-partial-messages` 时 `message_delta` 的数相同），所以上下文规模有记录；
- `user`：透传；Grok 把命令输出写成字节数组（`{"type": "Bash", "output": [72, 79, …]}`），适配器解成文字，运行记录
  里才读得懂；
- `result`：透传，`modelUsage` 补齐四种 token（Grok 的行可能缺一项，缺一项 `run_usage.reported_totals` 就整列算不出）；
  `total_cost_usd` 为 0 而 token 不为 0 时去掉（Grok 不知道花费），见 4.3。

### 4.2 工具名与动态

`tool_summary` 认 Grok 的工具名（实测是 shell 路线的名字：`run_terminal_command`、`read_file`、`list_dir`、`grep`、
`search_replace`、`write`、`spawn_subagent`、`search_tool`、`use_tool`…，文档里另一条执行路线的名字也认）。`use_tool`
显示它调的 `tool_name`（如 `looper__looper_tasks`）。在一条长命令跑着时被结束的运行只写出了 `init`：调用工具的那条
`assistant` 要等工具跑完才出现，所以长命令跑着时任务动态里还看不到它。

### 4.3 花费

`cost_source` 是 `runtime`：花费就是 Grok 结果里的 `total_cost_usd`，是 xAI 给这次运行盖的数，用自己的登录时也有
（`grok-login` 照例标"等价"）。读到 0 而 token 不为 0 时当作不知道，这次运行只记 token。

- Grok 没有花费上限参数，花费也只在一轮结束时报，所以**单次上限停不住 Grok 的运行**：注册 Agent Profile 时照此警告
  （`RuntimeAdapter.runtime_cost_capped = False`）；任务上限照常按记下的花费算。
- 备选是 `provider_price`：Looper 按 provider 价格算 token。内置价目表没有 xAI 模型，也没有可核对的官方价目，结果是
  只记 token、看不到花费、任务上限也算不进去，所以不用。

## 5. 运行中插话

headless 不能插话：Worker 跑起来以后规划者的 `forward`、`nudge` 只能留给这一步的决策轮，与 Codex、OMP 相同，记为
差距（计划第 5.2 节）。Grok 自己有插话（TUI 的 `steer`、子 agent 的 `delivery: steer/interject`），对外只能走 ACP：
`grok agent --always-approve stdio` 是 JSON-RPC 的常驻进程。实测它的 `initialize` 应答（不调用模型）声明了
`loadSession`、`sessionCapabilities`（`list`、`resume`、`close`）、MCP 的 HTTP 和 SSE、`x.ai/hooks`、`x.ai/pluginDirs`，
没有声明运行中追加输入的扩展；要不要改走 ACP（要写 JSON-RPC 客户端、权限回调和 `session/update` 的翻译）以后再定。

## 6. MCP 注入

- 运行的 `mcp_config`（Claude Code 格式，`library_with_server` 已把 Looper 的 `looper` 服务器和令牌放进去）翻译成
  家目录 `config.toml` 的 `[mcp_servers.<名称>]`（第 2 节的写法）；stdio 服务器的 `env` 加回操作者的 `HOME` 等变量
  （第 7 节），因为 Grok 用自己的环境启动它们。翻不了的记在运行记录的 `runtime_catalog.skipped`。
- 令牌在 `config.toml` 里，家目录 0700、文件 0600，与 Codex 相同；令牌不同家目录就不同（规划者按任务、办公台按运行）。
- 模型经 `search_tool`/`use_tool` 调，名字是 `looper__<工具>`。"运行时说明"写一句：这些服务器的工具不在你自己的
  工具里，用 `use_tool` 和完整的名字 `<server>__<tool>` 调（`looper__looper_tasks` 是 `looper` 的 `looper_tasks`），
  `search_tool` 列出它们。
- MCP 调用的上限：`tool_timeout_sec` 默认 6000 秒，长于 `looper mcp-server` 自己的 120 秒，不用改。
- 管家的工具调用由服务端（`manager_actions`）记录，不依赖流里的名字。

## 7. 隔离

每个 Grok 进程的 `GROK_HOME` 和 `HOME` 都是 Looper 自己的目录 `<runtime_root>/.local/grok/…`（0700）：

- `runs/<sha256>`：一次运行的家目录，按生成内容（`config.toml`、`rules/looper.md`、登录链接）取名，同样内容的运行
  共用，一天没写过的在写新目录时删掉（同 `codex._remove_idle_homes`）；
- `sessions/<label>`：常驻 owner 的家目录（3.2）；
- `shared/bundled`、`shared/docs`：Grok 往每个新家目录解压内置 skill（13 MB）和用户文档（0.8 MB）；每个家目录的
  这两项是指向这里的链接，第一个运行经链接解压进来，之后的运行看到是新的就不写（实测：链接不变，文件修改时间不变）；
- `shell-home`：agent 的 shell 拿到的 `GROK_HOME`，里面没有登录。

生成的 `config.toml`：

| 设置 | 为什么 |
|---|---|
| `[compat.claude]`、`[compat.cursor]` 的 `skills`、`rules`、`agents`、`mcps`、`hooks` 全为 `false` | 否则读 `~/.claude`、`~/.claude.json`、`~/.cursor`。项目里通用名的 `CLAUDE.md` 不受影响（第 2 节），项目的 `.claude/skills`、`.claude/rules`、`.claude/CLAUDE.md` 一并不读 |
| `[memory] enabled = false` | 跨会话记忆是 Grok 自己的，不该跨任务带东西 |
| `[cli] auto_update = false`、`use_leader = false` | 不在运行里升级；不共用 leader |
| `[features] telemetry = false`，`[telemetry] trace_upload = false` | 自动化运行不上传会话和轨迹（文档："disable session/trace uploads"） |
| `[shell_environment_policy] set = { GROK_HOME = "<shell-home>", HOME = "<操作者的>", … }` | agent 的 shell 拿回操作者的 `HOME` 和 XDG 等目录（git、gh 照常用操作者的配置），`GROK_HOME` 却是没有登录的目录，在 shell 里跑 `grok`（包括 `grok logout`）碰不到登录链接（实测 shell 里 `echo $HOME $GROK_HOME` 得到这两个值） |
| `[skills] paths` | 装给这个角色的插件的 `skills` 目录（第 9 节） |
| `[mcp_servers.*]` | 第 6 节 |

环境变量：设 `GROK_DISABLE_AUTOUPDATER=1`、`GROK_MEMORY=0`，以及 `GROK_CLAUDE_*_ENABLED`、`GROK_CURSOR_*_ENABLED`
全为 `false`（环境变量优先于 `config.toml`）；`GROK_HOME`、`HOME` 写在命令里；去掉 `GROK_CONFIG`、`GROK_CONFIG_PATH`、
`GROK_AGENT`、`GROK_SANDBOX`、`XDG_*_HOME`、`CLAUDE_CONFIG_DIR`，以及 `XAI_API_KEY`、`GROK_CODE_XAI_API_KEY`、
`GROK_AUTH`（登录失效时 Grok 会悄悄退回 key 计费）。这些变量由适配器拥有（`provider_env_owned`）。实测
`grok inspect --json`：只设 `GROK_HOME` 时列出操作者的 210 个 skill、4 个 Claude Code 插件、5 个 hook 和
`~/.claude/settings.json` 的 8 条权限；再设 `HOME` 后只剩 Looper 的规则文件和 Grok 内置的 3 个子 agent。

项目自己的内容照常读（RFC 决策 4、7）：仓库的说明文件、`.grok/rules`、`.grok/skills`、`.grok/config.toml` 里的 MCP
和权限、`.grok/hooks`、仓库根的 `.mcp.json`、项目的 `.claude/settings.json`。本机的托管策略
（`/Library/Application Support/ClaudeCode/managed-settings.json`，Grok 把它当成建议）不在用户目录里，照常生效。

## 8. Grok 登录（`prov-grok-login`）

做法沿用 Codex 登录（多运行时 RFC P1）：

1. 每个家目录里 `auth.json` 是指向操作者登录文件绝对路径的符号链接（服务端环境的 `GROK_HOME` 里的 `auth.json`，
   否则 `~/.grok/auth.json`），从不复制、不用硬链接。实测带链接的家目录能跑，运行后链接还是链接。
2. **刷新怎样写回没有核实**：Grok 闭源，文档只说用户 `config.toml` 的保存"沿着末级符号链接写到目标"，没说
   `auth.json`。维护者决定不为核实去强制刷新操作者的真实 token（2026-10-17）。核实期间的 18 次运行前后只 `stat`
   操作者的 `auth.json` 和 `auth.json.lock`：inode、修改时间、大小都没变，也就是没有发生刷新。`auth.json.lock` 不链接
   （用途不明）。
3. 所以照 Codex 的做法兜底：每次运行之后、`looper serve` 启动时，用 `lstat` 检查每个家目录的链接
   （`linked_login.inspect_homes`）：变成普通文件（刷新写成了副本：操作者文件里的 refresh token 可能已失效）、指向
   别处或不见了，都删掉该条目、放回链接，记下问题；之后用这个登录的运行一律不启动并写明原因，provider、任务动态和
   `grok_login` 检查里都有警告，直到操作者在本机确认 Grok 还登录着、在界面清除。
4. 与 Codex 登录共用的链接、检查、记下和清除问题的代码在 `runtimes/linked_login.py`；`codex_login` 的行为不变。
5. 登录状态：Grok 没有 `login status` 子命令，只看登录文件在不在（`stat`，不读内容）。
6. 提示的模型：`grok-4.7`、`grok-4.7-build-fast`、`grok-4.6`、`grok-4.5`（本机 Grok 的模型目录，协议都是
   `responses`，上下文 256k）；provider 的协议写 `openai-responses`。

失效方式：操作者 `grok logout` 后链接悬空，运行因为没有登录而不启动；agent 故意在 shell 里把 `GROK_HOME` 改回
`~/.grok` 仍能碰到登录，和本机任何进程一样，Looper 管不到。

## 9. 只读、skill 与插件

- Reviewer 与今天的所有运行时一样不限权限（RFC 决策 8）。
- 咨询（Agent Room D14）：沙箱在会话开始时定下，原运行是 `off` 的会话分叉出来不能换成 `read-only`；工具层可以：
  `--resume <id> --fork-session --tools read_file,grep,list_dir` 实测生效（模型想写文件，找不到写文件的工具，工作
  目录不变），可以作为 `read_only.enforced: "runtime"`。
- skill：装给角色的插件的 `skills` 目录写进 `[skills] paths`，由 Grok 原生加载；插件的命令、agent、hook、MCP 文件和
  输出样式不给，记在运行记录里。不声明 `claude_plugins`（不是整个插件都加载）。

## 10. 注册、探测、doctor 和界面

- 适配器 `looper/agents/runtimes/grok.py`，类型 `grok`，标签 Grok，默认可执行文件 `grok`；能力：`run`、
  `resident_session`、`resume`、`mcp`、`compaction_events`、`cost_report`。协议 `openai-responses`；认证 `oauth`。
- 探测：`grok --version`（`grok 1.0.62 (1474f9cbdd91)`），带一个临时的空 `GROK_HOME`。
- 内置行：`schema.sql` 加 `prov-grok-login`；`rt-grok` 照现有机制在 PATH 上找到时注册。
- doctor 和 `/api/status`：`grok_login` 检查，只在有 Agent Profile 用它时检查：记下的分叉问题是 fail；没有登录文件
  是 fail；否则 ok。
- 界面：Agent Runtimes 的类型表加 Grok CLI；LLM Providers 和 Agent Profiles 里登录的状态与修复命令按运行时显示，顶部
  横幅也显示 `grok_login` 的失败。
- 入口：Telegram、飞书和 CLI 不需要改：它们经管家和任务流程运行，运行时由 Agent Profile 决定。

## 11. 评测

`--register-agent` 的规格：

```json
{"runtime": {"type": "grok"}, "provider": {"provider_id": "prov-grok-login"},
 "agent": {"name": "grok/grok-4.7", "model": "grok-4.7", "effort": "high"}}
```

## 12. 实跑核实（2026-10-17）

一次性的工作目录和家目录，Grok 自己的登录，`grok-4.7`（`--effort low`），共约 12 次模型调用：

| # | 事实 | 结果 |
|---|---|---|
| 1 | `--prompt-file /dev/stdin` | ✅ 读完管道里的提示 |
| 2 | `--trust`、`--no-auto-update` | ✅ 都接受；`--trust` 后项目的 `AGENTS.md` 读到了 |
| 3 | 事件流形状 | ✅ 第 2、4 节；`assistant.message.usage` 是单次的，`result.modelUsage` 键是 `grok-4.7-build`，OAuth 下 `total_cost_usd` 有值 |
| 4 | 续接 | ✅ 记得上一轮；token 和花费只算这一轮；会话不存在时退出码 1、没有事件 |
| 5 | 新家目录的大小 | ✅ 第一次调用模型后约 14 MB（`bundled/` 13 MB、`docs/` 0.8 MB），共享链接后每个家目录约 1 MB 以下 |
| 6 | 隔离 | ✅ 第 7 节；只设 `GROK_HOME` 不够，还要 `HOME` |
| 7 | leader | ✅ 没有起，也没有连操作者的 |
| 8 | 登录链接 | ✅ 能跑，链接不变；刷新的写法**未核实**（第 8 节） |
| 9 | MCP | ✅ stdio 服务器加载，`search_tool` → `use_tool probe__ping` 拿到结果；SSE 的写法见第 2 节 |
| 10 | SIGTERM | ✅ Grok、shell 和 shell 里的命令一起结束，没有 `result` |
| 11 | `--effort max` | ✅ 运行在请求前失败（第 2 节） |
| 12 | 分叉上的 `--tools` | ✅ 只读生效（第 9 节） |
| 13 | ACP `initialize` | ✅ 第 5 节 |
| 14 | 压缩事件的字段 | ❓ 没有便宜的办法触发，未核实 |
| 15 | 子 agent 的响应是否带 `parent_tool_use_id` 出现在流里 | ❓ 未触发；子 agent 的 token 算在结果里（文档） |

## 13. 决定与备选

| 决定 | 备选与理由 |
|---|---|
| headless 每轮一个进程，不走 ACP | ACP 能常驻、可能能插话，但要新写 JSON-RPC 客户端和翻译层；headless 的事件流就是 Looper 的内部格式，逐轮续接的代码已被 Codex、OMP 验证 |
| 事件流用 `streaming-messages-json`，不加 partial | `streaming-json` 要多一层翻译；partial 只多出 `stream_event`，每次响应的用量 `assistant` 已经带着 |
| system prompt 放家目录的 `rules/looper.md` | `--rules` 只收文字，prompt 会进 argv 和运行记录；`--system-prompt-override` 会去掉 Grok 自己的工具说明 |
| `HOME` 也指向家目录，shell 和 stdio MCP 服务器再拿回操作者的 | 只靠 `compat` 开关挡不住 `~/.agents/skills`、`~/.claude/plugins` 和 `~/.claude/settings.json`（第 7 节实测） |
| 内置 skill 和文档共享一份 | 每个运行一份 14 MB；办公台的令牌让每次运行一个家目录 |
| 花费用 Grok 自己的数（`runtime`） | 起草时以为 OAuth 下不报花费、打算只记 token；实测有值，用它才看得到花费、任务上限才算得进去；代价是单次上限停不住，注册时警告 |
| 本期只做登录，不做 API key | 维护者的要求是登录；API key 的路径（`[model.<id>]`）清楚，留作后续 |
| 自动化运行关遥测和会话上传 | 隔离的家目录不带操作者的偏好，默认值会上传会话和轨迹 |
| 抽出两种登录共用的链接代码 | 复制一份 `codex_login` 改名更快，但两份一样的分叉检查以后要改两处 |
