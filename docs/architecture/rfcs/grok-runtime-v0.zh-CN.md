# RFC：Grok 运行时（v0）

- **状态：** 设计稿。第 2 节的接口事实取自本机 Grok CLI 1.0.62 的 `--help` 和它随附的用户文档
  （`$GROK_HOME/docs/user-guide/`），标 ❓ 的要先实跑核实（第 12 节），核实结果写回本文。
- **依据：** [多运行时 agent 架构 RFC](multi-runtime-agents-v0.zh-CN.md)（适配器接口第 5 节、隔离第 10 节、P1 登录）；
  [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 M9 节。
- **最近更新：** 2026-10-17

## 1. 背景与目标

Looper 已有三种运行时：Claude Code、Codex（含它自己的 ChatGPT 登录 `codex-login`）和 OMP。维护者要求（2026-10-17）
本版本加入第四种：xAI 的 Grok CLI（`grok`，Grok Build），四类角色（管家、规划者、Worker、Reviewer）都能用，并像
`prov-codex-login` 那样内置一个用 Grok 自己 OAuth 登录的 provider `prov-grok-login`。

**目标：**

- 运行时类型 `grok`，`looper serve` 在 PATH 上找到 `grok` 时自动注册内置运行时 `rt-grok`；
- 内置 provider `prov-grok-login`（OAuth，只配 Grok 运行时），共用操作者在本机的登录，不复制凭据；
- 隔离：不读操作者的 `~/.grok` 配置、skill、记忆、插件和 hook，也不读 Grok 兼容加载的 `~/.claude`、`~/.cursor`；
- 管家和规划者的平台工具、办公台（Agent Room）的 `run.` 令牌经 MCP 注入；
- Agent Runtimes 页列出 Grok，Agent Profiles 能选 `grok` 加 `grok-login`；`looper doctor` 检查 Grok 登录；
- 不配 Grok 的安装，行为、prompt 和命令行逐字节不变。

**不做（本期）：**

- Grok 接 API key provider（xAI key、CPA 等）：Grok 的自定义模型支持 `chat_completions`、`responses`、`messages`
  三种接口（`[model.<id>]` 的 `api_backend`、`base_url`、`env_key`），以后可以照 OMP 的做法接，本期只做登录；
- ACP（`grok agent stdio`）常驻进程：见第 5 节；
- xAI 模型的内置价格：没有可核对的官方价目，登录的运行只记 token（第 4.3 节）。

## 2. Grok CLI 的非交互接口（1.0.62）

✅ 文档写明，❓ 待实跑核实（第 12 节），❌ 不支持。

| 能力 | Grok 1.0.62 |
|---|---|
| 一次性运行 | ✅ 有 `-p <提示>`、`--prompt-file <路径>` 或 `--prompt-json` 时进入 headless：跑完这一轮就退出。**headless 不读 stdin 里的提示**（文档原话："does not read piped stdin into the prompt"）。❓ `--prompt-file /dev/stdin` 能否读到管道里的提示（第 3.1 节） |
| 事件流 | ✅ `--output-format streaming-messages-json`：Anthropic Messages / Claude Code 的 stream-json 线格式，`system`/`init`、`assistant`（每次模型响应一条，含 `text`、`thinking`、`tool_use`）、`user`（`tool_result`）、`system`/`compact_boundary`、终止的 `result`；另有 xAI 原生的 `streaming-json`（ACP 的 `session/update` 投影，`end` 事件带花费字段） |
| 常驻会话与续接 | ✅ 每轮一个进程：`-r/--resume <会话 id>` 续接，`-s/--session-id <UUID>` 给新会话指定 id，`--fork-session` 分叉。会话存在 `$GROK_HOME/sessions/<编码后的工作目录>/<id>/`。❓ 会话不存在时的退出码和事件；按 id 续接是否要求同一工作目录 |
| 运行中追加用户轮 | ❌ headless 的流是只读的（"Like streaming-json, this stream is read only"）；双向交互只有 ACP（`grok agent stdio`，第 5 节） |
| system prompt 注入 | ✅ `--rules <文字>`（别名 `--append-system-prompt`，只收文字）；`--system-prompt-override` 整个替换；家目录规则 `$GROK_HOME/rules/*.md` 每次启动都读（"Always scanned; applies to all projects"）。❓ 续接时规则改了是否用新的 |
| 结构化输出 | ✅ `--json-schema`（隐含 `--output-format json`，不能与事件流同用），Looper 不用，契约靠 prompt |
| skill 与插件 | ✅ 原生 `SKILL.md`：`./.grok/skills`、仓库根的 `.grok/skills`、`$GROK_HOME/skills`，以及兼容的 `.claude/skills`、`.cursor/skills`、各层的 `.agents/skills`；插件目录 `[plugins] paths`（读 `.claude-plugin/` 清单）。❓ `~/.agents/skills` 是否随 `GROK_HOME` 搬走 |
| MCP | ✅ `config.toml` 的 `[mcp_servers.<名称>]`：stdio（`command`、`args`、`env`）、HTTP/SSE（`url`、`headers`）；模型经两个元工具调用：`search_tool` 查、`use_tool` 调，工具名是 `<服务器>__<工具>`。`GROK_CONFIG`/`GROK_CONFIG_PATH` 覆盖层不收 MCP（白名单只有 `models`、`features`、`toolset`、`shell_environment_policy` 的过滤字段）。❓ SSE 的写法 |
| 项目说明文件 | ✅ 仓库根到工作目录每一级都读，每一级读**所有**认得的文件名：`Agents.md`、`Claude.md`、`CLAUDE.md`、`CLAUDE.local.md`、`AGENT.md`、`AGENTS.md`（`AGENTS.md` 和 `CLAUDE.md` 都在时两个都读）；不在仓库里时只读工作目录；另读 `<dir>/.grok/rules/*.md`。**启动时加载要求信任这个目录**（`--trust` 或交互授权）。❓ `--trust` 在 headless 下的效果（`--help` 不列出） |
| 额外目录（知识） | ❌ 没有 `--add-dir`；按 prompt 里知识索引给出的路径读（与 OMP 同） |
| 不限权限 | ✅ `--always-approve`（别名 `--yolo`，等于 `--permission-mode bypassPermissions`）；`--sandbox off` 是默认 |
| 隔离 | ✅ `GROK_HOME` 改掉整个用户目录（配置、登录、会话、记忆、日志、skill、插件、规则）；Claude、Cursor 兼容扫描（`~/.claude/skills`、`~/.claude/rules`、`~/.claude/CLAUDE*.md`、`~/.claude.json` 的 MCP、`~/.claude/settings.json` 的 hook，Cursor 同理）按 `HOME` 找，要单独关（第 7 节） |
| 非 git 目录 | ✅ |
| 花费报告 | ✅ `result.total_cost_usd`、`modelUsage[*].costUSD`；但"OAuth 路径常常不报"，报不全时这个格式**回落为 0**，全零的 `usage` 也表示"未知"。所以 Looper 不用它（第 4.3 节） |
| 压缩事件 | ✅ `system`/`compact_boundary`（"marks an auto compaction"）。❓ 字段 |
| 自主轮 | 后台任务（shell、子 agent、workflow）结束时 Grok 自己再跑一轮，headless 等它最多 10 分钟（`--background-wait-timeout`、`--no-wait-for-background`）。不声明 `autonomous_turns`：Looper 只取一次运行的最后一个 `result`（Worker 本来就这样读） |
| 登录 | ✅ `$GROK_HOME/auth.json`（0600），旁边有 `auth.json.lock`；后台自动刷新（过期前 5 分钟，`GROK_AUTH_EARLY_INVALIDATION_SECS`），401 时也刷新；外部改了 `auth.json` 会热加载。凭据优先级：模型自己的 `api_key`/`env_key` > 登录 > `XAI_API_KEY`。❓ 刷新后怎样写回（第 8 节） |
| leader 进程 | 多个客户端可以共用一个 leader（默认 `~/.grok/leader.sock`，`--leader-socket` 改路径，`[cli] use_leader` 默认关）。❓ headless 是否连上操作者的 leader |

## 3. 命令行

### 3.1 一次运行（Worker、Reviewer）

```
env GROK_HOME=<家目录> grok
  --prompt-file /dev/stdin                    # 提示仍由 write_turn 写进 stdin 再关掉，不进 argv
  --output-format streaming-messages-json
  --always-approve --trust --no-auto-update
  --leader-socket <家目录>/leader.sock
  -m <模型> [--effort <强度>] [--max-turns <N>]
```

- 提示不进命令行：Looper 的运行记录、任务动态和 `ps` 都看得到 argv，提示可能很长。现有的接口约定是"提示和之后的
  用户轮从 stdin 来"（`run_command`、`write_turn`），`/dev/stdin` 让 Grok 按文件读完整个管道，`write_turn` 照 Codex、
  OMP 的写法写完即关；再写一轮抛 `RuntimeError`。备选：每次运行把提示写进家目录里的临时文件再 `--prompt-file` 它
  （要改 `run_command` 的约定，并负责删文件），只在 `/dev/stdin` 实测不行时用。
- system prompt 写进家目录的 `rules/looper.md`：前面是适配器的"运行时说明"（第 6 节的 MCP 调用方式、"Looper 只读你
  最后一条消息"），后面是 Looper 的 prompt 原文。不用 `--rules`：它只收文字，会把整个 prompt 放进 argv。
- 工作目录由 Looper 给子进程（`cwd`），不另传 `--cwd`。
- 强度：Looper 的 `low`、`medium`、`high`、`xhigh` 原样传 `--effort`；本机目录里的 Grok 模型提供 `low`–`xhigh`
  （`grok-4.5` 没有 `xhigh`），`max` 没有模型提供，由 Grok 拒绝（❓ 报错方式）。
- `--max-turns`：Worker 有轮数上限时传。

### 3.2 常驻会话（管家、规划者）

与 Codex 相同的逐轮续接（`per_turn.PerTurnSession`，`ResidentGrok`）：每轮一个 3.1 的进程，第二轮起加 `-r <会话 id>`；
会话 id 取自 `init` 事件的 `session_id`。会话要留在同一个 `GROK_HOME` 里才续接得上，而一次运行的家目录随生成的
配置变（第 7 节），所以每个常驻 owner 有自己的家目录 `<runtime_root>/.local/grok/sessions/<label>`，每轮开始前从这一轮
的运行家目录同步 `config.toml`、`rules/looper.md` 和登录链接（同 `codex._sync_home`）。system prompt 改了（命令变了）
就重启会话，续接时用新的规则文件（❓）。开新会话的那一轮删掉这个 owner 之前的会话。

### 3.3 办公台的会话保留与咨询（另一条线在做）

- 办公台要求 Worker、Reviewer 的运行保留会话（Agent Room RFC D13，`run_command` 的 `session_dir`）：Grok 本来就把
  会话存在 `GROK_HOME` 里，`session_dir` 就是这次运行的家目录，按 3.2 的方式同步配置和登录链接。
- 咨询是原会话的分叉：`-r <id> --fork-session`（Grok 原生支持）。只读见第 9 节。

## 4. 事件解析（`GrokStream`）

`streaming-messages-json` 本来就是 Looper 的内部事件形状（Claude Code 的 stream-json），适配器只做三件事：

### 4.1 透传与补齐

- `system`/`init`：透传，加 `runtime: "grok"`；`session_id` 是会话 id；
- `assistant`：透传 `text`、`thinking`、`tool_use` 块（内联的 `server_tool_use`、`web_search_tool_result` 块也留着，
  记录器不认的块不影响它）；
- `user`：透传 `tool_result`；
- `system`/`compact_boundary`：透传（`compaction_events`）；
- `result`：透传 `subtype`、`is_error`、`result`、`num_turns`、`duration_ms`、`errors`；`modelUsage` 重写成四种 token
  齐全的形状（Grok 的行可能缺 `cacheCreationInputTokens`，缺一项 `run_usage.reported_totals` 就整列算不出）。

### 4.2 每次请求的用量

每条 `assistant` 是一次模型响应，带这次响应的 `message.usage`（❓ 是否单次而不是累计）：适配器在它之后发一个内部
`usage` 事件（`pricing.usage_event`），与 OMP 相同，所以上下文规模有记录、单次软上限能在请求之间判断（模型有价格时）。
子 agent 的响应若带 `parent_tool_use_id` 出现在流里，同样计入（❓）；不出现时只在结果的 `modelUsage` 里，与
Claude Code 的队友一样不参与中途停止。

### 4.3 花费

`cost_source` 一律是 `provider_price`：Looper 按 provider 的价格算 token，不用 `total_cost_usd`（OAuth 下常不报，报
不全时回落为 0，0 不能当"免费"）。`grok-login` 是订阅类登录，花费照例标"等价"；但内置价目表里没有 xAI 模型，所以
这些运行只记 token，Agent Profile 注册时照现有逻辑警告"单次上限对它无效"（`cost_warnings`）。操作者可以在 provider
的模型目录里填价格，填了就生效。

### 4.4 工具名与动态

`tool_summary` 认 Grok 的工具名（两条执行路线的名字都认，❓ 本机走哪条）：`read_file`/`Read`、`list_dir`/`Glob`、
`grep`/`Grep`、`run_terminal_cmd`/`run_terminal_command`/`Shell`、`search_replace`/`StrReplace`/`Write`、`write`、
`web_search`、`web_fetch`、`task`/`spawn_subagent`、`use_tool`（显示它调的 `tool_name`）、`search_tool`。

## 5. 运行中插话

headless 不能插话：Worker 跑起来以后规划者的 `forward`、`nudge` 只能留给这一步的决策轮，与 Codex、OMP 相同，记为
差距（计划第 5.2 节）。Grok 本身有插话（TUI 的 `steer`、子 agent 的 `delivery: steer/interject`），对外只经 ACP：
`grok agent --always-approve stdio` 是 JSON-RPC 的常驻进程（`initialize`、`session/new`、`session/prompt`、
`session/update` 通知），还有 `x.ai/` 前缀的扩展方法。改走 ACP 能得到真正的常驻会话，可能还有运行中插话，但要写
JSON-RPC 客户端、权限回调、`session/update` 到内部事件的翻译，工作量和风险都在 headless 的数倍，本期不做；
❓ 它的 `initialize` 应答里有没有插话类的扩展方法（第 12 节第 13 项），结果写在这里供以后决定。

## 6. MCP 注入

- 运行的 `mcp_config`（Claude Code 格式，`library_with_server` 已把 Looper 的 `looper` 服务器和令牌放进去）翻译成
  家目录 `config.toml` 的 `[mcp_servers.<名称>]`：stdio 保留 `command`、`args`、`env`；HTTP 写 `url`、`headers`；SSE
  按 `grok mcp add -t sse` 写出的形状（❓）。翻不了的记在运行记录的 `runtime_catalog.skipped`。
- 令牌在 `config.toml` 里，家目录 0700、文件 0600，与 Codex 相同；令牌不同家目录就不同（规划者按任务、办公台按运行）。
- 模型经 `search_tool`/`use_tool` 调 MCP 工具，名字是 `looper__<工具>`（如 `looper__looper_tasks`）。Looper 的
  prompt 不写死工具名，"运行时说明"补一句：Looper 的平台工具是 MCP 服务器 `looper` 的工具，用 `use_tool` 调，名字
  是 `looper__<工具名>`，不知道有哪些时先 `search_tool`。
- MCP 调用的上限：`tool_timeout_sec` 默认 6000 秒，长于 `looper mcp-server` 自己的 120 秒，不用改。
- 动态与评测：流里的调用是 `tool_use` `use_tool`、`input.tool_name` 是 `looper__…`。管家的工具调用由服务端
  （`manager_actions`）记录，不依赖流里的名字。

## 7. 隔离

每个 Grok 进程的 `GROK_HOME` 是 Looper 自己的目录 `<runtime_root>/.local/grok/…`（0700）：

- `runs/<sha256>`：一次运行的家目录，按生成内容（`config.toml`、`rules/looper.md`、登录链接）取名，同样内容的运行
  共用，一天没写过的在写新目录时删掉（同 `codex._remove_idle_homes`）；
- `sessions/<label>`：常驻 owner 的家目录（3.2）；
- `shell-home`：agent 的 shell 拿到的 `GROK_HOME`，里面没有登录（见下）。

生成的 `config.toml`：

| 设置 | 为什么 |
|---|---|
| `[compat.claude]`、`[compat.cursor]` 的 `skills`、`rules`、`agents`、`mcps`、`hooks` 全为 `false` | 否则读操作者的 `~/.claude`、`~/.claude.json`、`~/.cursor`。项目里通用名的 `CLAUDE.md` 不受影响（文档写明仍认），项目的 `.claude/skills`、`.claude/rules` 也一并不读，记在运行记录里 |
| `[memory] enabled = false` | 跨会话记忆是 Grok 自己的，不该跨任务带东西 |
| `[cli] auto_update = false`、`use_leader = false` | 不在运行里升级；不共用 leader |
| `[features] telemetry = "off"`，`[telemetry] trace_upload = false` | 自动化运行不上传会话轨迹（与 OMP 关掉问题报告同理），记为决定 |
| `[shell_environment_policy] set = { GROK_HOME = "<shell-home>" }` | agent 在 shell 里跑 `grok`（包括 `grok logout`）碰不到登录链接（同 Codex 的 P1 第 2 条） |
| `[mcp_servers.*]` | 第 6 节 |
| 需要时 `[skills] ignore = ["~/.agents/skills"]` | 只在实测发现 `~/.agents/skills` 不随 `GROK_HOME` 走时加（❓） |

环境变量：设 `GROK_HOME`、`GROK_DISABLE_AUTOUPDATER=1`、`GROK_MEMORY=0`，以及 `GROK_CLAUDE_*_ENABLED`、
`GROK_CURSOR_*_ENABLED` 全为 `0`（环境变量优先于 `config.toml`，服务端环境里若开着会盖过配置）；去掉会把 Grok 指回
别处的 `GROK_CONFIG`、`GROK_CONFIG_PATH`、`GROK_AGENT`、`GROK_SANDBOX`；用登录时去掉 `XAI_API_KEY`、
`GROK_CODE_XAI_API_KEY`（登录失效时 Grok 会悄悄退回 key 计费）。这些变量由适配器拥有（`provider_env_owned`）。

`--leader-socket <家目录>/leader.sock`：即使 Grok 起了 leader，也在 Looper 的目录里，不连操作者的
`~/.grok/leader.sock`（❓ 运行后没有残留进程）。

项目自己的内容照常读（RFC 决策 4、7：只用 Looper 拼装的和项目仓库自带的）：仓库的说明文件、`.grok/rules`、
`.grok/skills`、`.grok/config.toml` 里的 MCP 和权限、`.grok/hooks`、仓库根的 `.mcp.json`。它们要求目录已信任，所以
命令带 `--trust`。

Looper 的 skill（目录里装给角色的插件）：Grok 读 `SKILL.md`，插件目录写进 `[plugins] paths`（❓ 能否加载 Claude
插件格式的目录）；加载不了时按 P9 降为可读文档（同 Codex）。

## 8. Grok 登录（`prov-grok-login`）

做法沿用 Codex 登录（多运行时 RFC P1），差别在 Grok 的写法要实测：

1. 每个用登录的家目录里，`auth.json` 是指向操作者登录文件绝对路径的符号链接（服务端环境的 `GROK_HOME` 里的
   `auth.json`，否则 `~/.grok/auth.json`），从不复制、不用硬链接；`auth.json.lock` 也是指向操作者那个锁文件的链接，
   让 Grok 自己的跨进程锁落在同一个 inode 上（❓ Grok 用它做什么）。
2. 刷新：Grok 在进程内刷新、写回 `auth.json`。文档写明它保存用户 `config.toml` 时"沿着末级符号链接写到目标"、
   链接不变；`auth.json` 是否同样处理要实测（❓）。若是，刷新经链接写进操作者的文件，操作者的 TUI 靠热加载拿到新
   token；若是先写临时文件再 rename 到链接所在目录，链接会变成普通文件（分叉）。
3. 每次登录运行之后、`looper serve` 启动时，用 `lstat` 检查每个家目录的链接（同 `codex_login.inspect_homes`）：
   变成普通文件、指向别处或不见了，都删掉该条目、放回链接，记下问题；之后用这个登录的运行一律不启动并写明原因，
   provider、任务动态和 `grok_login` 检查里都有警告，直到操作者在本机确认 Grok 还登录着、在界面清除。
4. 实现上把 `codex_login` 里与运行时无关的部分（链接、检查、记下和清除问题）抽成两种登录共用的代码，每种登录给出
   自己的登录文件、锁文件、问题键和修复说明；`codex_login` 的行为不变。
5. 登录状态：Grok 没有 `login status` 子命令。只读本地状态：登录文件在不在（`lstat`，不读内容）；❓ `grok inspect
   --json` 在只有链接的探测家目录里能否报出登录方式（不调用模型），能就用它。
6. 提示的模型：本机 Grok 目录里的 `grok-4.7`、`grok-4.7-build-fast`、`grok-4.6`、`grok-4.5`（协议都是
   `responses`，上下文 256k）。provider 的协议写 `openai-responses`。

失效方式：操作者 `grok logout` 后链接悬空，运行因为没有登录而失败（失败即关闭）；agent 故意在 shell 里把 `GROK_HOME`
改回 `~/.grok` 仍能碰到登录，和本机任何进程一样，Looper 管不到。

## 9. 只读：评审与咨询

- Reviewer 与今天的所有运行时一样不限权限（RFC 决策 8），不做运行时层面的只读。
- 咨询（Agent Room D14）：Grok 的沙箱在会话开始时定下，续接时传别的 `--sandbox` 会被拒绝（文档写明），所以原运行是
  `off` 的会话分叉出来不能换成 `read-only`。能用的是工具层：分叉时加 `--tools` 只给读、搜索类工具，或
  `--deny "Edit" --deny "Write" --deny "Bash"`（❓ 续接和分叉时是否生效）。验证通过前，咨询的运行记录写
  `read_only.enforced: "prompt"`，在可以丢弃的快照里跑。

## 10. 注册、探测、doctor 和界面

- 适配器 `looper/agents/runtimes/grok.py`，类型 `grok`，标签 Grok，默认可执行文件 `grok`；能力：`run`、
  `resident_session`、`resume`、`mcp`、`compaction_events`（`claude_plugins`、`add_dirs` 视第 12 节核实结果）。协议
  `openai-responses`；认证 `oauth`（只有自己的登录）。
- 探测：`grok --version`（输出 `grok 1.0.62 (…) [stable]`），带一个临时的空 `GROK_HOME`，不读操作者的。
- 内置行：`schema.sql` 加 `prov-grok-login`（`grok-login`，`openai-responses`，`oauth`，`grok`）；`rt-grok` 照现有
  机制在 PATH 上找到时注册。
- doctor 和 `/api/status`：`grok_login` 检查，只在有 Agent Profile 用它时检查：记下的分叉问题是 fail；没有登录文件
  是 fail；否则 ok。服务启动时检查家目录的链接（同 Codex）。
- 界面：Agent Runtimes 的类型表加 Grok CLI；Agent Profiles 和 LLM Providers 里登录的状态、修复命令按运行时显示
  （`grok login`，不再写死 `codex login`）；顶部横幅也显示 `grok_login` 的失败。
- 入口：Telegram、飞书和 CLI 不需要改：它们经管家和任务流程运行，运行时由 Agent Profile 决定。

## 11. 评测

`--register-agent` 的规格：

```json
{"runtime": {"type": "grok"}, "provider": {"provider_id": "prov-grok-login"},
 "agent": {"name": "grok/grok-4.7", "model": "grok-4.7", "effort": "high"}}
```

按维护者要求跑：`run --mode real --tasks single_step,dependent_steps,reviewed_step --reps 2`，以及
`dialogs --dialogs status --reps 2`（管家的工具调用走 MCP）。登录的运行只有 token，报告 token 数。

## 12. 需要实跑核实的事实

文档没写或写得含糊，又决定了实现的，开工时用几次便宜的真实运行核实（一次性的工作目录和家目录，不碰操作者的
`~/.grok` 配置和仓库）：

| # | 事实 | 为什么要知道 |
|---|---|---|
| 1 | `--prompt-file /dev/stdin` 读完管道里的提示 | 决定提示走 stdin 还是临时文件（3.1） |
| 2 | 隐藏的参数 `--trust`、`--no-auto-update` 被接受，`--trust` 让项目说明文件加载 | 命令行和项目说明文件（7） |
| 3 | `streaming-messages-json` 的真实形状：`init` 的字段、本机走哪条执行路线（工具名）、`assistant.message.usage` 是单次的、`result.modelUsage` 的键、OAuth 下 `total_cost_usd` 的值 | 解析器（4） |
| 4 | 续接：同一家目录、同一工作目录 `-r <id>` 记得上一轮；会话不存在时的退出码和事件；续接时规则文件改了用新的；`result` 的 token 是这一轮的还是会话累计 | 常驻会话（3.2）和每轮用量 |
| 5 | 新家目录里 Grok 写了什么、多大（`bundled/`、`docs/`、`marketplace-cache/` 是否每个家目录一份） | 家目录按运行分还是共享大目录（7） |
| 6 | 隔离：`grok inspect --json` 在 Looper 的家目录里列出的规则、skill、MCP、hook 来源不含 `~/.grok`、`~/.claude`、`~/.cursor`、`~/.agents`；运行前后这些目录没有新文件 | 隔离（7） |
| 7 | leader：运行时有没有起或连 leader，结束后没有残留进程 | `--leader-socket`、`use_leader`（7） |
| 8 | 登录链接：带链接的家目录能跑；运行后链接还是链接；操作者登录文件的 inode、修改时间（只 `stat`）；`auth.json.lock` 的用途 | 第 8 节 |
| 9 | MCP：stdio 服务器加载，模型经 `use_tool` 调到 `looper__…`，流里的形状；SSE 的配置写法 | 第 6 节 |
| 10 | 被 SIGTERM 时的退出码和事件，shell 子进程是否随之结束 | `proc_tree` 收尾 |
| 11 | `--effort` 各档被接受，`max` 怎么报错 | 3.1 |
| 12 | `--tools`、`--deny` 在 `-r … --fork-session` 时生效 | 第 9 节 |
| 13 | `grok agent stdio` 的 `initialize` 应答里的扩展方法（不调用模型） | 第 5 节以后的决定 |
| 14 | 压缩事件的字段 | 很难便宜地触发，核实不了就标未核实 |

## 13. 决定与备选

| 决定 | 备选与理由 |
|---|---|
| headless `-p` 每轮一个进程，不走 ACP | ACP 能常驻、可能能插话，但要新写 JSON-RPC 客户端和翻译层；headless 的事件流就是 Looper 的内部格式，逐轮续接的代码（`PerTurnSession`）已被 Codex、OMP 验证。以后需要插话时再评估 ACP（第 5 节） |
| 事件流用 `streaming-messages-json` | `streaming-json` 的 `end` 在花费未知时省略字段而不是写 0，更诚实；但 Looper 本来就不用运行时的花费（4.3），而 messages 格式免去一层翻译 |
| system prompt 放家目录的 `rules/looper.md` | `--rules` 只收文字，prompt 会进 argv 和运行记录；`--system-prompt-override` 会去掉 Grok 自己的工具说明 |
| 本期只做登录，不做 API key | 维护者的要求是登录；API key 的路径（`[model.<id>]`）清楚，留作后续 |
| 不内置 xAI 价格 | 没有可核对的官方价目；只记 token，操作者可在 provider 目录里填价格 |
| 自动化运行关遥测和轨迹上传 | 隔离的家目录不带操作者的偏好，默认值可能上传会话轨迹；关掉更保守 |
| 抽出两种登录共用的链接代码 | 复制一份 `codex_login` 改名更快，但两份一样的分叉检查以后要改两处 |
