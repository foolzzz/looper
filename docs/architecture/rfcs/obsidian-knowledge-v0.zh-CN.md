# RFC：Obsidian 知识库作为 Looper 的本地知识（v0）

- **状态：** 已实现（v2.12.0）。第 2 节的「维护者决定」已定；「实现选择」是本 RFC 按建议写下的，备选和理由在第 7 节。
- **相关：** [项目知识](../agent-system.md#project-knowledge-documents-read-on-demand)（M3，项目自己的文档）、
  [Manager 平台工具 RFC](manager-platform-tools-v0.zh-CN.md)（MCP 端点和令牌）、[任务的 agent 房间](agent-room-v0.zh-CN.md)
  （运行的令牌）、[规划者只读](planner-read-only-v0.zh-CN.md)。
- **最近更新：** 2026-10-19

## 1. 背景与目标

运维者把长期知识放在一个 Obsidian 知识库里：一个本机目录，里面是 Markdown 笔记，通常用 git 管理。其中有 agent
共同维护的记忆目录（运维者的安装里是 `codex-mem/`，规则写在 `codex-mem/AGENTS.md`），也有运维者自己的笔记
（`mine/`）。Looper 让 agent 和运维者的个人配置隔离（[M1](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md)），
所以运维者装给 Claude Code 的 Obsidian 技能到不了 Looper 的 agent。

**目标：** Looper 自己提供一套与运行时无关的知识库工具（Looper 的 MCP 工具），在 Claude Code、Codex、Grok、OMP 上
表现一致：任务的 agent 按需读整个知识库，只在配置的可写目录里写，每次写入都是知识库仓库里一次可回滚的提交，
任务结束时规划者按知识库自己的规则做一次收尾整理。

**不做（v0）：** 删除、改名笔记；管家读写知识库（见 D3）；同一 UID 下的隔离（见第 6 节）。

## 2. 决定

维护者决定（定稿）：

- **M1** agent 可以读整个知识库（包括个人笔记），只能写配置的可写目录；个人笔记（`mine/`）永远不写。
- **M2** 写可写目录不需要确认，直接生效：每次写入是知识库 git 仓库里的一次提交，在任务动态里可见，可以回滚。
- **M3** 任务进行中随时可写；任务结束时规划者按知识库自己的规则文件做一次收尾。
- **M4** 与运行时无关：功能是 Looper 的 MCP 工具，四种运行时一样。

实现选择（本 RFC 建议，第 7 节有备选）：

- **D1** 系统设置 `knowledge_vault` 是 `system_settings` 里的一个 JSON 值，没有这一行就是没配置。项目开关是项目执行
  设置的 `vault`（`off`/`on`，未设置为关），新项目默认关；系统设置没配置时，项目开着也没有任何效果。
- **D2** 系统设置是本机路径，不进配置导出；项目的 `vault` 是执行设置的一列，随项目导出和导入。
- **D3** 工具给任务的规划者、Worker 和 Reviewer。管家只能在设置工具里查看和修改这项系统设置（修改要用户确认），
  没有读写知识库的工具：管家不属于任何项目，项目开关管不到它（记入待办）。
- **D4** Worker 和 Reviewer 的运行在项目开了房间**或**知识库时拿运行令牌（原来只在开房间时拿），令牌记下它能用哪些
  工具（房间的、知识库的）。规划者用它自己的令牌。
- **D5** 三类角色都能写；规划者和它的其他写工具一样，只在 Looper 发给它的回合里能写（`needs_user_turn`）。
- **D6** Looper 是知识库唯一的写入方：每个知识库一把 `threading.Lock`（按解析后的根目录），一次写入从比对 sha256、
  原子替换到 `git add` 加只提交这一个路径的 `git commit` 都在一个工作线程里、持着这把锁做完，所以不论调用来自哪个协程
  或线程都是串行的。不是 git 仓库时照写不提交，并告诉 agent 和任务动态。
- **D7** 收尾回合 `[VAULT]`：任务完成（经验沉淀之后）或失败时，在给 TaskRouter 发最终状态之前，规划者多一轮。
  条件：知识库已配置且有可写目录、项目开着 `vault`、任务日志不空、任务没花到上限。失败只记一条警告，不改变任务结果。

## 3. 数据与接口

**系统设置**（`system_settings` 的 `knowledge_vault`）：`{"path": 绝对路径, "writable": [相对目录], "rules_file": 相对路径或 null}`。
保存时校验，任何一条不过都是 400，原样说明：

- `path` 是绝对路径、存在、是目录；存的是解析后的真实路径；
- `writable` 每一项是知识库里的相对路径：不空、不是绝对路径、没有 `..`、没有以 `.` 开头的部分（`.git`、`.obsidian`），
  存在且是目录，自己不是、路径上也不经过符号链接（写入只走真实目录，所以保存时就拒绝），解析后仍在知识库里；去掉末尾的
  `/`，不重复；空列表即只读；
- `rules_file` 同样是知识库里的相对路径，存在、是普通文件，解析后仍在知识库里。

`POST /api/v1/knowledge_vault/get {}` 的 `OpOk.result` 回 `{configured, path, writable, rules_file, git, problem}`：`git` 是知识库是否在 git 仓库里，
`problem` 是已保存的配置现在不成立的原因（如目录被删），这时工具一律拒绝并给出这条原因。
`POST /api/v1/knowledge_vault/update` 收 `{path, writable?, rules_file?}`，`{"path": null}` 清除配置；回 get 的完整内容；operator 单次执行，client token 先确认再执行。

**项目开关**：`projects.vault`（`CHECK (vault IN ('off','on'))`，NULL 为关），在 `EXECUTION_COLUMNS` 里，和其他执行设置
一样由项目接口读写。

**界面**：设置里新增 Knowledge vault 页（路径、可写目录、规则文件，保存、清除，显示是否是 git 仓库和 `problem`）；
项目执行设置新增 Knowledge vault 开关（关 / 开），项目页摘要在开着时显示一行。

**管家**：`looper_settings` 新增 `knowledge_vault` 动作（`knowledge_vault` 对象，字段同 PUT；`{"path": null}` 清除）；
每次修改都先给用户看修改前后并确认；`view` 带上当前配置。

## 4. 工具

三个 MCP 工具，都走运行已有的令牌（规划者的 `planner.` 令牌、运行的 `run.` 令牌），只在任务的项目开着 `vault`
且系统设置成立时列出。工具列表在运行开始（规划者每一轮、Worker 和 Reviewer 每次运行）
时确定，之后不变；每次调用都重新读项目开关和系统设置，关掉后调用被拒绝（`forbidden`）。没有可写目录时不列出写工具。
路径一律相对知识库根目录，用 `/` 分隔。

| 工具 | 参数 | 做什么 |
|---|---|---|
| `looper_vault_search` | `query`，`limit`（1–30，默认 10） | 只搜 `.md`：`query` 按空白拆成词（不分大小写），路径或正文里有任何一个词就算命中；先按命中的词数、再按命中次数排序，回路径、命中次数和第一处命中附近的片段（不超过 200 字符） |
| `looper_vault_read` | `path` | 一个文本文件的内容（UTF-8，不含 NUL）和整个文件的 sha256；超过 64 KiB 只回前 64 KiB 并标 `truncated` |
| `looper_vault_write` | `path`、`content`、`expected_sha256`、`reason` | 写一篇 Markdown 笔记的全文（见下） |

**读的边界**：拒绝绝对路径、`..`、以 `.` 开头的部分（`.git` 里可能有凭据）、目录、解析后在知识库外的路径（包括指向
外面的符号链接）和非文本文件。搜索不跟随符号链接，跳过以 `.` 开头的目录和文件（包括写入留下的 `.looper-*.tmp`）、
超过 512 KiB 的文件；一次搜索最多看 10,000 个文件、读 32 MiB、用 5 秒，碰到任何一个上限就停下并回 `truncated`
（运维者的知识库约 270 篇笔记，这些上限只是保险）；搜索和读都在线程里做，不阻塞服务。

**写**（`looper_vault_write`）：

1. 校验：路径同读的规则，后缀是 `.md`，在某个可写目录下（不在时拒绝并列出可写目录，`mine/` 就是这样被拒的）；从知识库根目录的 fd 逐层打开上级目录，拒绝符号链接（可写目录自身也是符号链接时同样拒绝）；
   后续创建目录、读取、临时文件和替换都相对目录 fd 操作，不跟随符号链接。目标若存在，必须是普通文件、不是符号链接、不超过 64 KiB（更大的文件读不全，
   不能整篇改写）。`content` 不超过 64 KiB、不含 NUL；`reason` 一行，1–200 字符。每个令牌累计最多写 256 KiB。
2. 在知识库的锁里（D6）：读当前 sha256。`expected_sha256` 为 null（或不给）而文件已存在、给了而文件不存在、或与当前不一致，
   都是冲突（`conflict`），要求重新读；内容与当前相同则不写、不提交（`unchanged`）。
3. 原子写：同目录下的临时文件（`.looper-*.tmp`）写入并 fsync，`os.replace` 换上，保留原文件的权限；缺的上级目录在可写
   目录里创建。任何一步失败都删掉临时文件、原文件不变；进程在中途被杀时可能留下临时文件，它以 `.` 开头，搜索和读都
   跳过它。
4. git：知识库在 git 仓库里（`git rev-parse --show-toplevel`）时 `git --literal-pathspecs -C <根目录> add -- <路径>`，再
   `git --literal-pathspecs -C <根目录> -c commit.gpgsign=false commit -m "looper: <任务 id> <角色>: <reason>" -- <路径>`：只提交这个路径，运维者暂存的
   其他改动不受影响；作者取知识库仓库自己的配置；不加任何工具署名。角色是 `planner` 或运行的席位（`worker@s2`、
   `reviewer@s2`、`backend_rd@s2`）。git 命令有 30 秒超时，`GIT_TERMINAL_PROMPT=0`，去掉 `GIT_DIR` 等环境变量。
   提交照常运行知识库自己的 hook（那是运维者的策略，不加 `--no-verify`），30 秒超时也约束 hook。运维者或别的程序同时在
   知识库里跑 git 时，`git add`/`git commit` 会因为 `index.lock` 失败：只对这种失败重试，共 3 次，两次等待分别为 0.25 秒和 0.5 秒。
   仍失败（或别的失败）时文件已写入，回 `commit: null`，`note` 带 git 第一行错误（最多 200 字符），失败时另带 `git_exit`。
5. 记录：任务动态一行 `Vault: <角色> wrote <路径> (commit <sha 前 12 位>): <reason>`（未提交时只记录固定状态：`not committed: commit failed, git exit N`、
   `not committed: git timed out` 或 `not committed: not a git repository`，不带 git 输出）；
   被拒的写也记一行。日志和动态里只有路径、提交和理由，没有文件内容。

回给 agent：`{path, sha256, bytes, commit, note, git_exit}`（冲突时带当前 sha256，不带内容）。

## 5. 模型看得到的内容

- **工具描述**：上面三个工具各一句（`looper/agents/ops/vault.py`）。
- **工具说明**：规划者的工具说明（`guide_for`）和 Worker、Reviewer 的系统提示词（任务和房间之后的 `## Knowledge vault`，
  单独一层 `vault`）各加一行，只在项目开着知识库时出现；没有可写目录时只说能读。项目关着知识库、或系统设置没配置时，
  规划者和 Worker、Reviewer 的系统提示词与改动前逐字节相同（有测试），所以「关」这条路不需要提示词评测；「开」这条路
  的证据是第 8 节的真实运行，发布前的全量评测覆盖其余。
- **收尾回合**（`TurnKind.VAULT`）：

  ```
  [VAULT] The task completed|failed (<原因>). Keep what later work should know in the operator's knowledge vault,
  following its rules below: search before you add, read a note before you change it (pass its sha256; null for
  a new note), and write only under <可写目录>. Paths are relative to the vault; Looper commits each write. Write
  nothing when nothing will still hold. Do not change any other files.
  The vault's rules (<规则文件>): <内容，最多 16,000 字符>
  <任务日志，同经验沉淀回合>
  回复末尾一个 json 块：{"vault": "一行：写了什么，或为什么没写"}
  ```

  任务动态记 `Vault closeout: <那一行>`；回合失败或回复不合约定记一条警告。

## 6. 已知限制

- agent 能读个人笔记（M1 的取舍）：项目开着知识库时，任务的 agent 读得到 `mine/` 下的一切，内容会进入模型的上下文和
  运行记录。
- 能用 shell 的 agent（Worker，以及没有只读限制的运行时）仍可以直接改知识库的文件：工具是受支持的写法，不是强制边界；
  Looper 不提供同一 UID 下的隔离。
- 只有 Looper 这个进程持锁：运维者同时在 Obsidian 里改同一篇笔记，靠 sha256 比对发现冲突，Looper 不会覆盖它不知道的版本，
  但运维者那边的编辑器可能覆盖 Looper 的写入。
- 写入从 `/` 起逐级打开知识库路径上的目录（macOS 用 `O_SEARCH`、Linux 用 `O_PATH`，只需进入权限），所以路径上每一级目录都必须让服务的用户能进入，知识库目录本身还要可读、可进入（Linux 的 `O_PATH` 不检查这一层，另外查）；保存设置时就按同样的方式检查，进不去是 400（「every folder above the vault must be readable by the server user」），不会等到写入时才失败。
- 搜索是纯文本匹配，没有索引；很大的知识库只看前 10,000 个文件。

## 7. 备选与理由

- **项目开关的默认值**：默认关（维护者决定）。系统设置是总开关，项目开关是逐项目的同意。
- **管家也给读工具**（D3 的备选）：管家的工具列表是固定的，加上就要在没有配置时也列出、并改管家的提示词；它不属于
  项目，项目开关管不到。v0 只给它设置动作，读知识库作为待办。
- **单独的知识库令牌**（D4 的备选）：每个运行会有两个 `looper` MCP 服务，名字冲突；复用运行令牌只需要在令牌上记下工具族。
- **读超过上限时截断**（备选是拒绝）：截断能让 agent 看到开头；写入时拒绝改写超过上限的文件，避免丢掉没读到的部分。
- **任一词命中、按命中词数排序**（备选是每个词都要命中，或整句子串）：笔记里的说法和查询的用词常常不同。最初要求每个词都命中，真实运行里 sonnet 的规划者搜「calc staging server port」，笔记写的是「Staging listens on port …」，没有「server」，于是一条也没搜到，它就去问了用户；改成任一词命中、命中词多的在前之后，同一查询排在第一。仍是纯文本，不引入索引。
- **`commit.gpgsign=false`**：签名可能弹出 pinentry 把写入卡到超时；Looper 的提交是机器提交，理由写在提交信息里。

## 8. 测试

先写测试，每条在修复前失败：设置校验（相对路径、不存在、不是目录、可写目录不存在或经符号链接逃出、规则文件在外面）；
读工具（上限、`..`、指向外面的符号链接、非文本、以 `.` 开头的路径）；写工具（只能写可写目录、`mine/` 被拒、
`expected_sha256` 冲突、经真实工具入口的两个并发写入恰好一个冲突、原子替换和失败时不留临时文件、提交信息和任务动态、
只提交这个路径、持有 `index.lock` 时重试后提交或回 `committed: false`、非 git 知识库）；搜索的文件数、字节数和时间上限；
关着时提示词逐字节不变；
收尾回合在任务结束时只发一次，项目关着时不发；工具按角色列出（规划者、Worker、Reviewer；房间关着而知识库开着时运行
也有令牌）。测试只用临时目录里新建的 git 仓库。

真实路径一次：`looper serve`（19040 端口、新数据库、临时知识库：带 `codex-mem/AGENTS.md` 和 `mine/` 的 git 仓库），一个
真实 Claude Code 任务读一篇笔记、收尾时在 `codex-mem/` 下写入并提交；尝试写 `mine/` 被拒。
