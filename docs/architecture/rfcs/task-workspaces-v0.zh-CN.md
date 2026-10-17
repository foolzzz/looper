# RFC：任务的工作区——在已有的目录上开任务（v0）

- **状态：** 提案，维护者已定方向（第 4 节标「维护者定」的几条），其余为本 RFC 的建议，实现见 TW1（第 10 节）。
- **依赖：** [并行步骤的工作目录隔离 RFC](step-workdir-isolation-v0.zh-CN.md)（I1、I2）：任务的 worktree 里，并行步骤照旧各有
  自己的步骤 worktree。
- **最近更新：** 2026-10-17

## 1. 背景与目标

Looper 会用在很多已有的项目上，这些项目本来就有自己的工作目录（用户的检出、在里面用 Claude Code 工作过）。建任务时用户点名
一个工作区目录，任务应当：

- 用上这个目录已有的上下文：仓库自己的 `CLAUDE.md`、`AGENTS.md`、`.claude/settings.json` 今天就会加载；缺的是**操作者在这个
  目录上用 Claude Code 攒下的自动记忆**（`~/.claude/projects/<目录>/memory/`），Looper 现在用 `autoMemoryEnabled: false`
  把它关掉了；
- 不弄乱用户的检出：任务默认在这个仓库的一个 git worktree 里、自己的分支上做，做完把分支交给用户，不替用户合并；
- 同一个工作区上可以同时有几个任务，只提醒、不阻止。

**不做：** 不写操作者的任何 Claude Code 文件（硬规则，第 4 节 W6）；不自动合并、不推送；不读会话记录（W7，留作后续）。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 |
|---|---|
| 建任务 | `POST /api/tasks/create`（`projects.create_task`）、管家的 `looper_task_create`、定时任务（`scheduler._create_task`）都只记 `tasks.work_dir`：显式给的目录，否则项目的默认目录。任务建出来是 `init`/`ready`，开始时 TaskRouter 给它一个 Planner（`_assign_task`），`work_dir` 随 `TASK_REQUEST` 交给 Planner |
| 运行目录 | Planner、Worker、Reviewer 都在 `work_dir`（或 I1 的步骤 worktree）里跑；同一个目录上的几个任务互相看得见、会互相覆盖 |
| 结束 | 完成、失败、停止、取消都由 TaskRouter 收尾（`_handle_planner_status` → `_teardown_task`）；删除任务在 `http_server._handle_task_delete` |
| 重启 | `_handle_restart`：`continue` 沿用计划，`start_over` 删掉步骤（和步骤 worktree）再规划；服务启动时 `resume_tasks`、`start_tasks` 接着跑 |
| Claude Code 的上下文 | 隔离参数（`claude_cli.isolation_args`）只加载项目自己的设置和 `CLAUDE.md`/`AGENTS.md`，关掉自动记忆；Codex、OMP 由各自的适配器读项目说明（#97） |
| 被忽略的本地状态 | 步骤 worktree 可以按项目设置 `step_workspace_links` 把 `.venv`、`.env` 等链接回任务目录（I2） |

## 3. 概念

| 概念 | 定义 |
|---|---|
| 工作区 W | 用户建任务时点名的目录（不点名就是项目的默认目录）。存为 `tasks.workspace_dir` |
| 运行方式 | `worktree`：任务在 W 所在仓库的一个 git worktree 里跑；`in_place`：任务直接在 W 里跑（今天的做法） |
| 任务 worktree | `<runtime_root>/task-worktrees/<task_id>`，检出任务分支 T；任务目录是它里面 W 在仓库里的同一位置 |
| 任务分支 T | `looper/<task_id>/task`，从基点建出 |
| 基点 | 任务第一次开始时 W 检出的提交，或用户点名的分支、标签、提交 |
| 操作者笔记 | 操作者的 Claude Code 为 W 记下的自动记忆：`<Claude 配置目录>/projects/<键>/memory/` 下的 `MEMORY.md` 和它列出的文件 |

## 4. 决策

| # | 决策 | 理由 |
|---|---|---|
| W1 | **运行方式**（维护者定：优先 worktree）。按顺序取：建任务时显式给的 `workspace_mode`；建任务时点名了一个**不同于项目默认目录**的工作区 → `worktree`；否则项目设置 `task_workspaces`（`in_place`、`worktree`；`NULL` = `in_place`）。新建项目（接口、任务表单里新建、管家新建）不给这一项时写 `worktree`；已有项目和导入的项目保持 `NULL`，行为不变（告警监控项目照旧原地跑） | 维护者定：新项目和点名了工作区的任务用 worktree，已有项目不变。点名的目录和项目默认目录相同时按项目设置：否则在表单里重新填一遍默认目录就会悄悄换了运行方式 |
| W2 | **任务 worktree 在任务第一次开始时建**（`git worktree add -b looper/<task_id>/task <路径> <基点>`），不在建任务时建：建了不开始的任务什么都不留。W 不在 git 里 → 原地跑，activity 和任务页写明原因；基点解析不出来 → 这次开始失败，任务失败并写明（建任务时的检查接口已经提醒过） | 分支名用 `looper/<task_id>/task` 而不是 `looper/<task_id>`：步骤分支是 `looper/<task_id>/<key>-<n>`，git 的引用名里两者不能并存 |
| W3 | **结束时**（维护者定：分支留给用户，不自动合并）：完成 → 把任务 worktree 里没提交的改动按 I1 的 D8 提交到 T（Looper 身份、尾注、不签名、不跑钩子、链接不进结果），删掉 worktree，保留 T，activity、任务页和结束通知写明 T 和怎么合并。失败、停止、取消 → worktree 原样保留，`continue` 重启时接着用（同一路径、同一分支、未提交的改动都在）。删除任务 → 先提交剩余改动，再删 worktree，保留 T。`start_over` → 同一个 worktree、同一个 T 上重新规划（和原地任务的 start over 不还原目录一样）。服务启动时清扫：没有任务或任务已完成的任务 worktree 按「完成」处理 | 和步骤 worktree 的 Q4 一致：没做完的留着，用户和重启都还用得上；做完的只留分支。先提交再删，删 worktree 不会丢任何东西 |
| W4 | **被忽略的本地状态**：I2 的 `step_workspace_links` 改名为 `workspace_links`，一个清单管两种 worktree：任务 worktree 里链接回 W，任务 worktree 里的步骤 worktree 链接回任务目录（链接再指回 W）。**链接，不复制** | 复制 `node_modules`、`.venv` 又慢又占盘，复制的 `.env` 会和用户的那份分叉；链接意味着任务往 `.venv` 里装包会改到用户的环境，这是 Q3 已经接受的取舍。改名趁 I2 还没发布：两种 worktree 用两个清单只会让人困惑 |
| W5 | **只提醒，不阻止**（维护者定）。检查接口 `GET /api/workspaces/check` 和建任务的回答给出 `warnings`（类型化：`not_git`、`uncommitted`、`shared`、`large_checkout`），建任务表单在提交前显示，管家的回复里由代码附上，任务开始时也写进 activity。`shared`：同一个仓库（按主检出算）上还有进行中的任务：worktree 方式提醒「各在自己的分支上，合并时可能冲突」，原地方式提醒「会互相覆盖文件」 | 两个任务各有自己的 worktree，不会冲突；原地方式本来就会，值得说清。提醒由代码生成，不靠模型转述 |
| W6 | **操作者笔记**（维护者定：只读，按项目开关）。项目设置 `operator_notes`（`on`、`off`；`NULL` = `off`）。打开时，规划者、执行者、评审者的系统提示词在项目知识之后多一节「操作者为这个目录记下的笔记（只读）」：`MEMORY.md` 和它链接的、在记忆目录里的 `.md` 文件，合计不超过 24 KiB（`MEMORY.md` 在前，超出的文件只列名字和大小）；开头说明这些是操作者自己的背景笔记，可能过时，和任务要求冲突时以任务为准，不要去改它们。运行记录的 `prompt_layers` 记下哈希、文件和是否截断。三种运行时都一样（这是 Looper 写进提示词的文字，不靠 Claude Code 的功能）；管家没有。**Looper 只读这些文件**（打开、读取、列目录），从不创建、修改、移动、删除 `~/.claude` 下的任何东西，也不把路径告诉 agent | 维护者定。默认关：这是操作者的私人笔记，按项目明确打开；关着时提示词逐字节不变 |
| W7 | **会话历史不做**，记为后续：给规划者一个只读工具，在这个目录的历史会话里按关键词搜，返回有长度上限、经过脱敏（`log_redaction`）的片段 | 会话记录大（单个可达几十 MB）、含密钥和私人内容，整份注入不可行；搜索工具要单独设计权限和脱敏 |

**笔记的位置**（在 Claude Code 2.1.285 上实测：临时 `CLAUDE_CONFIG_DIR`、无效的 API key，读 init 事件的 `memory_paths.auto`）：

- 键是 W 所在仓库**主检出**的顶层（`git rev-parse --git-common-dir` 的上一级），解析符号链接；子目录和同一仓库的其他 worktree
  共用这一份；不在 git 里就是目录本身。所以从任务 worktree、步骤 worktree 里也算得出同一个键，不用额外传 W。
- 目录名是把键里每个非字母数字的字符换成 `-`；超过 200 个字符时取前 200 个，接 `-` 和键的 djb2 哈希（取绝对值，36 进制）。
- 配置目录是 `$CLAUDE_CONFIG_DIR`，否则 `~/.claude`。操作者在设置里用 `autoMemoryDirectory` 改了位置的，这一期找不到笔记（任务页
  显示「没有找到」），记为已知差距。测试和评测用 `LOOPER_OPERATOR_NOTES_ROOT` 指向一个代替 `<配置目录>/projects` 的目录。

**备选（不采用）：**

- 打开 Claude Code 自己的自动记忆（`autoMemoryEnabled: true`）：只对 Claude Code 有效，而且 agent 会往操作者的记忆里写，违反只读。
- 在建任务时就建任务 worktree：建了不开始的任务（定时任务的就绪任务、用户反悔的任务）会留下分支。
- 任务结束一律删 worktree（失败、停止也删）：`continue` 时只能从分支重建，未提交的改动要先提交成 Looper 的提交；而用户停下任务
  多半是要看看现场。
- 复制被忽略的文件：见 W4。

## 5. 数据模型

```
tasks
  workspace_dir   TEXT NOT NULL DEFAULT ''   W；'' = 这一期之前建的任务（work_dir 就是工作区）
  workspace_mode  TEXT  'in_place' | 'worktree'；NULL = in_place
  workspace_base  TEXT NOT NULL DEFAULT ''   用户点名的基点；'' = W 当时检出的提交

task_worktrees                       一个任务的 worktree（每个任务至多一个）
  task_id      → tasks ON DELETE CASCADE，主键
  repo_dir     W 所在仓库的顶层
  subdir       W 相对 repo_dir 的路径
  path         <runtime_root>/task-worktrees/<task_id>
  branch       looper/<task_id>/task
  base_commit  基点解析出的提交
  links        链接了的路径
  state        'active' | 'removed'
  result_commit  删除时 T 的末端；之前为 NULL
  created_at, removed_at

projects
  task_workspaces  TEXT 'in_place' | 'worktree'；NULL = in_place
  operator_notes   TEXT 'off' | 'on'；NULL = off
  workspace_links  TEXT[]（由 step_workspace_links 改名）
```

`tasks.work_dir` 仍是任务实际运行的目录：原地方式是 W，worktree 方式在开始时改成任务 worktree 里的任务目录。规划者、执行者、
评审者、I1 的步骤 worktree 都只认 `work_dir`，不用改。

## 6. 流程

1. **建任务**：按 W1 定运行方式，存 `workspace_dir`、`workspace_mode`、`workspace_base`；回答带 W5 的 `warnings`。
2. **开始**（TaskRouter 在给任务 Planner 之前，`task_workspaces.prepare`；开始、`continue`、`start_over`、服务启动时接着跑都走这里）：
   原地方式什么都不做。worktree 方式：已有 `active` 的任务 worktree → 确认目录还在（丢了就从 T 重建，链接重建）；否则检查 W
   （在不在 git 里），解析基点，`git worktree add -b T`，按 `workspace_links` 链接，写 `task_worktrees`，把 `tasks.work_dir`
   改成任务目录，activity 记下 T、基点、链接和 W 里没带进来的未提交改动。之后的规划者在 T 上工作；并行步骤按 I1 在 T 上建
   步骤 worktree、合回 T。
3. **结束**（`task_workspaces.finish`）：按 W3。完成时的 activity 和结束通知（飞书、Telegram）多一行：「结果在分支 T（W 所在的
   仓库），确认后自己合并：`git merge T`」。
4. **删除任务**：先删步骤 worktree（它们的仓库目录是任务 worktree），再按 W3 处理任务 worktree，最后删行。
5. **清扫**：服务启动时，和步骤 worktree 的清扫一起。

## 7. 入口

| 入口 | 改动 |
|---|---|
| 仪表盘建任务 | 工作区目录（可选，留空用项目默认；下拉建议已有项目和最近任务的目录）、运行方式（默认值按 W1 显示）、基点（worktree 方式可选）；目录一变就调检查接口，显示仓库、分支、未提交的改动、同一仓库上进行中的任务、有没有操作者笔记和项目是否打开了它 |
| 任务页 | Overview 显示工作区、运行方式；worktree 方式显示 T、基点、任务 worktree 在不在，结束后给出看改动、合并、删除分支的命令（在 W 里执行）。worktree 方式的任务不显示 I2 的撤销命令（它们改的是 T，不是用户的分支） |
| 项目设置 | 「任务在哪里跑」（原地、工作区的 worktree）、「操作者的笔记」开关、链接清单改名为「链接进 Looper 的 worktree」 |
| 管家 `looper_task_create` | `work_dir` 即工作区；新增 `workspace_mode`、`base`；回复里代码附上的任务配置多一行运行方式和 W5 的提醒 |
| 定时任务 | 建出的任务按项目设置（不点名工作区）。worktree 方式的项目每次触发都会留下一个分支，写进文档；按定时任务覆盖运行方式留作后续 |
| 重启、删除 | 见第 6 节 |
| 飞书、Telegram | 只有结束通知多一行分支 |
| CLI | 不变 |

## 8. 与其他功能的关系

| 功能 | 怎么处理 |
|---|---|
| 步骤 worktree（I1、I2） | 任务目录是任务 worktree：D1 的前提在 T 上判断，检查点和合并提交落在 T 上；步骤 worktree 的仓库目录是任务 worktree，所以删除任务时先删它们 |
| 项目说明（#97） | 被跟踪的 `CLAUDE.md`、`AGENTS.md` 在任务 worktree 里；没被跟踪的不在，需要的话放进链接清单 |
| 项目知识 | 不变；路径型文档仍指向项目默认目录里的文件 |
| 角色、关卡、办公台 | 不变：它们只认 `work_dir` |
| 评测 | 现有任务建项目时显式写 `task_workspaces: in_place`，结果和基线可比；新增两个读笔记的任务（第 11 节） |

## 9. 风险与观测

| 风险 | 处理 |
|---|---|
| 用户在 W 里删了分支 T 或用 `git worktree prune` 删了记录 | 开始时目录或分支不在 → 从基点重建会丢掉 T 上的提交，所以 T 不在时这次开始失败并写明，不悄悄重建 |
| 用户在 W 里检出了 T | 任务 worktree 已经检出 T 时 git 拒绝；任务 worktree 删掉之后用户检出 T 是正常用法 |
| 每个任务一份完整检出 | 检出超过 1 GiB 时 W5 提醒（`large_checkout`），不阻止 |
| 定时任务在 worktree 方式下积累分支 | 写进文档；按定时任务覆盖留作后续 |
| 笔记里有过时或错误的内容 | 提示词开头说明以任务为准；评测 `operator_notes_stale` 检查仓库里的事实压过笔记 |
| 笔记含敏感内容 | 默认关；打开是按项目的明确选择；进了运行记录（`system_prompt`）就和项目背景一样在本机数据库里 |

**观测：** activity 记录任务 worktree 的建立（T、基点、链接、没带进来的改动）、重建、结束时的剩余改动提交和删除、清扫；任务页
显示 T；运行记录的 `prompt_layers.project.operator_notes` 记哈希和文件。

## 10. 分期

| 阶段 | 内容 |
|---|---|
| TW1 | 本 RFC 的全部：W1–W6，数据模型、开始与结束、检查接口、仪表盘、管家、定时任务、通知、评测 |
| TW2（后续） | W7 的会话搜索工具；按定时任务覆盖运行方式；可选的复制清单 |

## 11. 测试

- 单元（真实 git 仓库，签名开着而 gpg 不可用，每个钩子写标记文件）：W1 的运行方式按表驱动；建、复用、丢失后重建、T 不在时失败；
  结束时提交剩余改动、链接不进结果、删 worktree 留 T；基点为分支、标签、提交和不存在的；W 不在 git 里；`shared` 和
  `uncommitted` 提醒；笔记的键（子目录、worktree、符号链接、仓库外、超过 200 个字符的路径），读 `MEMORY.md` 和链接的文件、
  忽略指向记忆目录以外的链接、24 KiB 截断、关着时提示词逐字节不变、只读（测试里把记忆目录设成只读）。
- PG：`task_worktrees` 的读写；重启、`start_over`、删除、清扫；建任务接口的 `warnings`。
- 真实服务 + 替身 Claude：worktree 方式的任务做完后 W 的检出和工作区不变、T 上有结果；停止后 `continue` 接着用同一个 worktree；
  同一个 W 上两个任务；原地方式和 W 不在 git 里；无头浏览器看建任务表单的提醒和任务页的分支。
- 真实模型：笔记是模型可见的改动，管家的工具说明也变了，按 `evals/README.md` 跑 haiku 全部任务 `--reps 3`、opus 的
  `--tasks planner --reps 3`、两档管家对话；新增 `operator_notes`（只有笔记里有的事实决定答案）和 `operator_notes_stale`
  （笔记里的旧事实和仓库里的不一致，以仓库为准）。
