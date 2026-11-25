# RFC：工作模式——任务管家、直接模式与编排模式、多任务控制台（v0）

- **状态：** 提案，只有文档，不含实现。产品方向由维护者定（第 1 节）：任务是第一位的对象；先建任务，再在任务里说；临时的事留给主管家；每个任务默认直接模式，需要时交给后台编排；任务里和操作者说话的是这个任务的「任务管家」，规划者不变、留在它背后编排；界面做在现有的网页控制台里。本文按代码核对这个方向并给出实现，代码说做不到或要先确认的地方写在第 3.2 节和第 22 节。第 4 节的决定和第 23 节的已定问题都标明了以后可以改。
- **相关：** [群聊里的能力范围 RFC](chat-scopes-v0.zh-CN.md)；[助理层 RFC](assistant-layer-v0.zh-CN.md) 第 4.2 节（常驻进程的内存）；[任务的工作区 RFC](task-workspaces-v0.zh-CN.md)（W1–W7）；[对外动作审查 RFC](outbound-review-v0.zh-CN.md)；[系统提示词模式 RFC](system-prompt-mode-v0.zh-CN.md)；[定时任务共用一条规划者会话 RFC](schedule-planner-session-v0.zh-CN.md)；[用户关卡 RFC](user-gate-v0.zh-CN.md)；[管家台账 RFC](manager-completion-ledger-v0.zh-CN.md)；已合入的 #1736（第 0 步）。
- **代码基线：** `origin/main` `021ed06e9`；本文涉及的守卫、状态检查、会话存储和分工都按这个版本核对，出处路径相对 `looper/agents/`，`schema.sql`、`db/…` 在 `looper/db/` 下，`web/…`、`docs/…` 相对仓库根。本文没有启动服务、执行 SQL 或调用模型；规格中的验收留给实现卡。Claude Code 的参数存在不等于组合行为已经验证，同会话换权限、换模型和钩子组合都要过任务卡 1.0。

## 1. 出发点

### 1.1 两种使用场景

| | 工作模式 | 远程模式 |
|---|---|---|
| 场景 | 在电脑前 | 不在电脑前 |
| 入口 | 控制台的新界面，其次是命令行 | 飞书 |
| 用法 | 几个任务并行，每个任务一段能随时切换、隔几天还能接着说的对话；新事先建任务再在里面说；临时的事交给主管家 | 收通知、回关卡和确认、发短指令、看状态（晨报、`/todo`、`/status`、提醒）、交待一件临时的事；不在飞书里并行处理多个任务 |

目标：在电脑前用 Looper 处理几件事，要和同时开几个编码 CLI 会话一样顺手，同时保留 Looper 的后台执行、评审和记忆。

### 1.2 一个任务的两种做法

- **直接模式（默认）：** 任务管家和操作者对话，自己在任务的目录里读、改、跑，像在编码 CLI 里一样；一句纠正马上生效。
- **编排模式：** 任务管家把活交给后台：规划者拆步骤、派给执行者，评审者把关，规划者根据结果做决定；任务管家在对话里汇报。用在时间长、能并行、要独立评审的活，或操作者要离开的时候。
- **切换：** 说「交给后台」就切到编排；后台跑完、失败或停止时自动回到直接；中途「我来接手」也回到直接。

**分层：** 操作者对任务管家，任务管家对规划者，规划者对执行者和评审者。在任务里，操作者只和任务管家说话；消息直接进任务对话，不经过主管家，也不转述；停止、暂停、开始、接手由代码立刻执行，不排在模型后面。

### 1.3 优先级和范围

1. 工作模式的最小第一步最先做：新建任务、进对话、任务管家在任务目录里直接干活、过程看得见、能打断（第 11 节），加上控制台里全部任务看得见能切换（第 12 节）。
2. 「飞书里一个任务一个话题」排到最后并重新评估；远程模式也许只要「回复任务的通知就是对这个任务说话」这一层（第 16 节）。
3. 远程模式已经上线的能力（投递层、晨报、「需要你处理」、状态卡、台账）照原计划做完。
4. 两种场景的衔接：飞书里关于某个任务的一句话落进那个任务的对话（第 16 节）。
5. 不在范围：主管家自己的改进（现有管家仍用 REPLACE，不受影响）；桌面外壳（以后的待办；新界面只用现有的 HTTP API 和流式通道、每个任务对话有自己的网址、在单独窗口里也能用）；另造「工作会话」对象；拆分主管家的那一条对话；用模型猜「说的是哪个任务」；OS 级隔离。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 | 出处 |
|---|---|---|
| 网页对话线程 | 只是显示分组，都进主管家的 main 会话；只有 `/new` 换上下文 | `web/src/pages/chat.tsx:284-286`、`:1301`；`session_routing.py:46`、`:79-80` |
| 建任务 | 需求必填，建出来是就绪（`init`），要另外开始；管家先澄清、确认后才建 | `v1_domains/task_create.py:58-73`、`projects.py:560-574`；`tiers/manager.py:267`、`:320`、`:339` |
| 规划者 | 任务开始时才建；回答是受检查的结构化 JSON；一次只跑一个回合；在 `work_dir` 里跑；默认只读 | `tiers/task.py:397-417`；`tiers/planner_protocol.py:120-140`、`:800`；`tiers/planner.py:5398`；`agent_settings.py:1024-1031` |
| 常驻进程 | `ResidentClaude` 是一个和拥有者无关的部件：起一个 stream-json 进程、一次喂一个用户回合，事件交给回调，支持按请求中断、回合中预留输入、软上限停止 | `claude_cli.py:1017-1160`、`:1279-1305`、`:1562` |
| 管家的交互 | 分段输出（`STATUS_UPDATE` 的 delta → `answer.delta`）、停止（`chat.stop_enabled`，默认关）、回合中并入（只对默认管家，默认关）、会话预算、压缩 | `channels/web.py:327-339`；`chat_settings.py:64`；`mid_turn_input.py:35-38`；`tiers/manager_budget.py:194` |
| 管家的会话路由 | `SessionKey(kind, channel_id, chat)` 按「消息从哪来」划分；路由会话用空 library、只用平台工具，`HOME`、`CLAUDE_CONFIG_DIR` 改到会话根下，在会话根里跑；`manager_sessions` 只许 group / dm；常驻上限按管家 | `session_routing.py:45-88`；`tiers/manager.py:969-1025`、`:1857-1866`；`schema.sql:1909-1929`；`tiers/manager_routing.py:136-161`、`:383-407` |
| 隔离 | Looper 启动的 Claude Code 用 `--setting-sources project`、`--strict-mcp-config`、排除上层 CLAUDE.md、关自动记忆、拒绝别的会话发来的消息；每个运行都设 `LOOPER_AGENT_RUN` | `claude_cli.py:42-72`、`:142-170`；`agent_settings.py:1150` |
| 系统提示词模式 | 有 `APPEND` 和 `REPLACE` 两种；管家角色内置 `REPLACE` | `system_prompt_mode.py:6-10`；`agent_settings.py:186` |
| 对外动作审查 | 打开时，管家的 Claude Code 每次调用工具前跑一个 PreToolUse 钩子，失败即拒绝 | `outbound_hook.py:1-20`、`:220-232` |
| 中断能力 | Claude Code 的常驻会话能中断；Codex 和普通 DSH 的常驻会话按回合起进程、不能中断；DSH 经 runner 时声明能中断，未验证 | `runtimes/claude_code.py:157-169`；`runtimes/__init__.py:534-538`；`runtimes/dsh.py:302`、`:310`；`runtimes/dsh_runner.py:69` |
| 花费上限 | 按任务全部运行累计；到上限时停掉任务、记为失败 | `tiers/planner.py:6851-6912`；`db/tasks.py:139` 的 `FailKind.TASK_BUDGET` |
| 费用拦截 | 给任务挂拦截时，把所有不是终态的任务改成 `awaiting_budget` | `db/cost_holds.py:21-22` |
| 任务列表接口 | `tasks.page` 按创建时间倒序、每页最多 200；Chat 页只列最近 30 个任务 | `v1_domains/task_views.py:146-163`、`projects.py:62`；`chat_targets.py:21-22` |
| 常驻进程的内存 | 每个空闲约 240 MB，带子进程树约 310 MB | 助理层 RFC 第 4.2 节 |

## 3. 任务管家

### 3.1 为什么是任务管家，不是规划者

默认是直接模式：对话方要自己读改跑，回答要分段、能随时停、能在回合中途收新消息、上下文长了能压缩。这些在管家的会话里都有（第 2 节），规划者一样都没有，而且规划者每一轮都是受检查的结构化输出。把这些搬到规划者上，等于在最复杂的组件里重造一套、改它的契约；动手时的大量工具输出还会进规划者的上下文，之后每个决策回合都更贵，压缩还可能丢计划状态；规划者在等步骤结果时对话要排队。所以规划者原样不动，任务管家是独立的会话，不排在规划者后面。

### 3.2 怎么做：独立的一层，不走管家的会话路由

**核对结论：** 把任务管家做成 `ManagerAgent` 的一种会话（`SessionRouter` 里的 `task:<id>` 键）有四处障碍，都在能力范围 RFC 第 1b 步拥有的代码里，其中会话路由那一组已经开成 #1747：

| 障碍 | 出处 | 任务管家要的 |
|---|---|---|
| 会话键按「消息从哪来」算（`session_key_for`、`_user_key`），没有按目标路由的路 | `session_routing.py:72-88`、`tiers/manager_routing.py:197-208` | 按任务 id 找会话 |
| 路由会话的启动：空 library、只用平台工具、`HOME` 和 `CLAUDE_CONFIG_DIR` 改到会话根、在会话根里跑 | `tiers/manager.py:969-1025`、`:1857-1866` | 全部工具（直接模式）、操作者本人的环境、在任务的工作区里跑 |
| `manager_sessions` 的种类和键约束只认 group / dm，1b 正在改这两条 | `schema.sql:1909-1929` | 不碰 |
| 常驻上限按管家计（1b 改成全机统一的池） | `tiers/manager_routing.py:383-407` | 自己的上限（第 9 节） |

所以任务管家做成**独立的一层** `TaskManagerAgent`（新文件 `tiers/task_manager.py`），直接建在 `ResidentClaude` 上：起进程、喂回合、事件回调、中断、回合中预留输入、软上限都是现成的（`claude_cli.py:1017-1160`、`:1279-1305`、`:1562`）。复用的是管家交互机制的底层部件，不是 `ManagerAgent` 的消息循环：

| 复用 | 新写（一个任务、一个会话，比管家简单） |
|---|---|
| `ResidentClaude` 的回合、中断、预留输入；Claude Code 的常驻命令（`runtimes/claude_code.py:410-430`）；`STATUS_UPDATE` delta 到 `answer.delta` 的映射（`channels/web.py:327-331`）；运行记录（`llm_records`）；`untrusted.fence()`；`APPEND` 模式；`manager_budget.bound` 的测量、压缩和轮换规则（第 9 节） | 这个任务的消息队列和回合串行；停止和预留输入的入口；工具调用记录的事件；会话的保存和续接（`agent_sessions` 的 `task-manager-<task_id>` 一行，这张表不归 1b）；它自己的常驻上限和闲置回收 |

- **会话键**仍是 `task:<task_id>`：它是任务对话的地址（`operator_chat.send` 的目标 `kind: task`、对话线程 `task-chat:<task_id>`），不是 `manager_sessions` 的一行。不依赖 1b 的会话路由；共用文件里的独立改动按第 19 节协调，不能把「不改路由」写成「不改同一文件」。
- **只对操作者开放**，固定是全能力，不经过能力范围的解析。

### 3.3 它怎么启动

- **工作目录：** 任务的工作区（原地任务是 `work_dir`；worktree 任务是任务 worktree 里的位置，第一次需要时由 `task_workspaces.prepare` 建好，`task_workspaces.py:324-385`）。
- **权限：** 直接模式给全部工具、跳过权限确认（同执行者，`agent_settings.py:1031`）；编排模式下写权限在后台，任务管家改用 Looper 自己的隔离加只读启动（`isolation_args` 加 `read_only_args`，`claude_cli.py:142-195`、`:377-407`）续同一条会话：不载入操作者的设置，操作者的 allow 规则和 MCP 服务就放宽不了它。模式一变，启动签名就变，停掉进程、按同一个会话 id 续上。会诊是 `--resume --fork-session`，原会话不动（`consult.py:14-22`），**不能证明**同会话换权限可行；后者需要任务卡 1.0 的真实 CLI 离线探针验证，失败就报告，不暗中分叉或放宽权限。
- **操作者本人的环境：** 直接模式自己组启动参数，不调用 `isolation_args`：该函数只要收到 `hooks` 就强制 `--setting-sources ""`（`claude_cli.py:191`），会把用户设置一起关掉。让操作者的用户设置、全局指令、记忆和 skill 生效。三处仍由 Looper 加上（经只属于这次启动的 `--settings`）：拒绝别的会话发来的消息（`crossSessionInbound: refuse`）；对外动作审查的 PreToolUse 钩子（`outbound_hook.py:220-232`）；禁掉和 Looper 撞名的内置工具。不设 `LOOPER_AGENT_RUN`，改设 `LOOPER_TASK_MANAGER=<task_id>`；本机命令按第 18 节的类型化表拒绝或只限本任务。这是减速带，不是隔离，不能声称它挡得住绕开 CLI 的调用。
- **前置条件：** `--settings` 里的 PreToolUse 钩子（对外动作审查）和用户设置里的钩子要能同时执行。第 1 步开工前先验证（任务卡 1.0）：用真实的 Claude Code CLI 接本地的假模型服务，返回固定的工具调用，在临时的 `HOME` 和 `CLAUDE_CONFIG_DIR` 里放一份合成的用户设置，不调用真实模型、不碰操作者的 `~/.claude`（做法同能力范围 RFC 第 10 节的离线探针）。只有必须真跑一轮模型才能证明时，才先估价、并进评测预算，由协调者去问维护者。验证不过，任务管家就不能用操作者本人的环境启动，这算推翻性的问题，要立刻报告。同一张卡还要测：用户设置、仓库里提交的 `.claude/settings.json`、模型在会话里写出的 `.claude/settings.json` 带 `disableAllHooks` 时，审查钩子还执行不执行（今天的隔离正是为此不载入设置文件，`claude_cli.py:142-170`）。
- **提示词：** `APPEND`：保留 Claude Code 自带的提示，追加任务简报（第 14 节的 `compose`，别人写的内容围起来）和一小段 Looper 说明（这个任务是什么、怎么交给后台、怎么看后台的进展、哪些工具绑定在这个任务上）。这是新的会话类型；现有管家仍用 `REPLACE`。
- **角色和模型：** 任务管家有自己的角色 `task_manager`，`role_configs` 一行，拥有自己的职责提示和配置，不沿用所属管家的 Profile。今天角色枚举在 `agent_settings.py:146-158`，角色表在 `schema.sql:555-567`；模型和运行时由 Agent Profile 承载，不能塞回 `role_configs.model`（`agent_settings.py:1432-1452`）。新增角色的默认 Profile 是 Claude Code + **Sonnet**，角色层可以选 Profile；任务的显式选择优先于角色层，不读所属管家的选择。按任务覆盖复用 `task_profile_overrides`，将其 role CHECK 扩展为允许 `task_manager`（今天只有三个角色，`schema.sql:573-578`），不借用 `agent_sessions.selected_profile_id`。
- **按任务切换：** 新建任务时和任务面板里都能选模型，至少有 Sonnet、Opus 两个 Claude Code Profile；`tasks.create_draft` 可带 `task_manager_profile_id`，另有操作者专用 `tasks.task_manager_profile_set(task_id, profile_id)`，返回读回的有效模型、运行时、provider 和配置来源。切换和发送共用任务串行队列：有回合时返回 `task_manager_busy`（409，让操作者先 Stop），空闲时停掉进程、写覆盖、按新模型和原 `session_id` 重启续接；续接失败才走第 10 节的显式 `session_fresh`。不清对话、计划或累计花费，也不改 Planner/Worker/Reviewer。模型、effort、provider、模式和提示哈希都进启动签名。切换前后同会话的真实 CLI 验证在 1.0，Opus 的少量真实确认在第 20 节。
- **运行时：** v0 只在 Claude Code 上（能中断常驻回合，`runtimes/claude_code.py:169`）。创建、角色配置、按任务切换和每次启动都校验有效 Profile；其他运行时拒绝，类型化原因 `task_manager_runtime_unsupported`（带 requested runtime 和 supported = `claude_code`）。配置无效就不启动，不悄悄换成 Claude Code；两个入口读回同一原因。

### 3.4 它的工具

经 Looper 的 MCP 服务，凭据绑定这个任务，服务端逐次校验，碰不到别的任务：

| 工具 | 什么时候 | 作用 |
|---|---|---|
| `task_status` | 任何时候 | 读计划、步骤和结果、关卡、最近的活动、工作区状态 |
| `task_requirement_set` | 没在编排时 | 写一版需求（`edit_requirement` 的同一条路径） |
| `task_handoff` | 直接模式 | 交给后台（第 6.1 节）；需求没确认时出确认卡片 |
| `task_supplement` | 编排模式 | 向规划者补充：代码按本轮绑定的用户消息 id 读取原话，校验属于本任务和操作者；工具不接受模型提供的原话，改写字段丢弃。只允许另加一条 `manager_note`（至多 200 个 Unicode 码点，独立字段，标为模型所写），超长拒绝。原文不 strip、不截断，不走 `journal.kept_text`（`projects.py:1308-1309`、`tiers/journal.py:214-217`）；沿用 pending/delivered 流程（`db/task_changes.py:102-120`），新增原文及消息引用字段。规划者的决定作为任务对话事件 |
| `task_gate_answer` | 编排模式、有等你的关卡 | 把操作者的话交给那个关卡，原话由代码附上 |
| `task_pause`、`task_stop`、`task_take_over` | 编排模式 | 暂停（不再派新步骤）、停止、接手（第 6.2 节） |
| `task_review` | 直接模式 | 请评审者看当前的改动（第 15 节） |

操作者在界面上点按钮或发固定命令时，这些动作由代码直接执行，不经过模型（第 5 节）。

## 4. 决定一览

| # | 决定 | 落点 |
|---|---|---|
| D1 | 任务是第一位的对象，每个任务从建出来就有对话，地址 `task:<task_id>`；主管家的临时对话仍是一条 | 第 1、5 节 |
| D2 | 任务里和操作者说话的是任务管家：一个只属于这个任务的 Claude Code 会话；规划者不变，留在它背后编排 | 第 3 节、23.1 |
| D3 | 任务管家是独立的一层，建在 `ResidentClaude` 上，会话存在 `agent_sessions`，不走管家的会话路由、不用 `manager_sessions`；共用文件的独立改动按第 19 节协调 | 3.2、第 19 节 |
| D4 | 启动：在任务的工作区里，直接模式全部工具、编排模式只读；操作者本人的环境，加上拒绝跨会话消息、对外动作审查和禁掉撞名的工具；`APPEND` 加任务简报；v0 只在 Claude Code 上 | 3.3 |
| D5 | 消息：直接模式由任务管家自己做；编排模式仍先到任务管家，进展问题它读任务状态回答，要改计划时用 `task_supplement`，原话由代码附上；关卡回答由代码送到关卡；控制由代码执行 | 第 5 节 |
| D6 | 新状态 `draft`（从没交给过后台）；模式（`direct`、`orchestrated`）由状态推出，不另存一列；结束后模式回到直接，再交给后台就是又一次运行 | 第 7 节 |
| D7 | 交给后台和接手都是类型化的转换，由代码执行交接、检查点和写权限的移交 | 第 6 节 |
| D8 | 写权限：每个任务自己的排他租约，保证本任务的任务管家和后台不同时写；跨任务维持 W5，只提醒，不按目录互斥 | 第 8 节、22 |
| D9 | 任务管家全机最多同时 6 个，闲置 30 分钟回收；满了先回收最久没用的空闲的，都在忙时排队并显示；和管家会话的上限分开 | 第 9 节 |
| D10 | 任务管家的花费算进任务的上限；到了上限只拒绝这一轮，不让任务失败；费用拦截不改草稿和就绪任务的状态 | 第 9 节、7.3 |
| D11 | 续不上原会话时对话里插一行类型化的事件；完成的 worktree 任务只在分支还在时建回，否则只读打开 | 第 10 节 |
| D12 | 控制台的新页面 `#work`，和 Chat 页并存；列表是全部任务，定时任务按计划折叠；切换快、不丢输入；未看的进展按操作者存在服务端 | 第 12 节 |
| D13 | 命令行的 `looper task enter` 接的是同一个任务管家会话，同一时间只有一处持有它 | 第 14 节 |
| D14 | 开关：`tasks.conversations.enabled`（第 1、2、4、6 步）、`tasks.orchestration_switch.enabled`（第 3、5 步），都默认关 | 第 20 节 |
| D15 | 独立的 `task_manager` 角色配置，默认 Sonnet；创建和任务面板可按任务换 Sonnet / Opus，停进程后续同一会话；仅支持 Claude Code，其他运行时类型化拒绝 | 3.3 |

## 5. 消息怎么走

| 操作者在任务里发一句话 | 直接模式 | 编排模式 |
|---|---|---|
| 谁接 | 任务管家 | 任务管家 |
| 问进展、问原因 | 自己查了回答 | 任务管家只读续接同一条会话，用 `task_status` 读任务状态和工作区自己回答，不去问规划者 |
| 要做事或纠正 | 自己马上做 | 用 `task_supplement` 交给规划者，原话由代码原样附上，任务管家只加一句说明；界面写明「已收到，在规划者下一次决定时处理」；规划者的决定作为一条事件显示在同一段对话里 |
| 回答关卡 | — | 在关卡卡片里回复时由代码直接送到关卡；在对话里说时任务管家用 `task_gate_answer`，原话由代码附上 |
| 停、暂停、开始、接手 | 由代码立刻执行（按钮或固定命令） | 同左 |

- **回合中的消息：** 任务管家的回合进行中又来的话，界面显示「排队中」，到下一个回合边界送达；由开关控制并入正在跑的回合（`ResidentClaude.write_mid_turn`，`claude_cli.py:1321`；`:1279-1305` 是结算预留输入）。不能复用只向默认管家开放的入口条件（`mid_turn_input.py:35-38`）。
- **停止：** 输入框的 Stop、Esc 或 `/stop`，由代码调用 `ResidentClaude.interrupt`（`claude_cli.py:1562`），不等回合结束。
- **固定命令**（只认整句，去掉首尾空白、不分大小写，同关卡的 `END_WORDS`，`gate_routing.py:45`）：`/stop`、`/pause`、`/background`、`/takeover`、`/done`、`/start`，以及按钮。
- **过程看得见：** 回答按 delta 分段；工具调用由代码生成一行记录（工具名、参数摘要、结果摘要，有长度上限），作为新事件 `agent.tool`；回合结束时一条代码写的小结（改了哪些文件、提交到哪里）。
- 规划者的 `[CHAT]` 入口保留，但不再面向操作者；旧 Chat 页的 planner 目标照旧可用。

## 6. 模式切换

### 6.1 直接 → 编排：交给后台

- **触发：** 操作者说「交给后台」、点按钮、发 `/background`，或任务管家用 `task_handoff` 提议而操作者同意。
- **代码执行：**
  1. 没有任务管家的回合在跑（有就排在它后面，或先停掉）。
  2. 需求：任务管家写需求文字、已经做了什么、接下来做什么（一个只写交接的回合，结构化输出至多 3000 个 Unicode 码点），明确标为**任务管家写的摘要（模型所写）**，不是操作者原话。工作区状态（文件、未提交的改动、HEAD）由代码读。原话由下面的确定性算法另附。操作者确认需求（卡片：「开始」或改一句）；说过「直接开始」就跳过。确认前任务停在就绪（`init`）；后来有新纠正就使旧确认失效，取新快照重做交接，不让最近的纠正落在交接外。
  3. 检查点：worktree 任务把剩下的改动提交到任务分支 T；原地任务不替操作者提交，只记下 HEAD 和未提交的文件，有未提交的改动时在对话里提醒。
  4. 写权限：停掉直接模式的进程及其可写子进程后，按第 8 节移交本任务的排他租约；任务管家用隔离加只读启动续同一条会话，不持写租约。
  5. 记一行 `task_handoffs`（方向、需求版本、模型摘要、原话摘录及引用、快照上界、截断元数据、工作区状态）；这几个独立字段组成 `via = handoff` 的补充说明，原话不经过模型和 `journal.kept_text`，执行者和评审者都看得到。
  6. 状态 → `pending`（模式随之是 `orchestrated`）；TaskRouter 照今天起规划者，规划者在**它自己的新会话**里按需求和交接规划（规划者不变）。这个任务以前跑过时，这是又一次运行：`run_no` 加一，规划者走 `_restart_plan` 的续接（6.3）。

**交接原话的唯一截取规则（代码执行）：** 在任务串行队列内固定本次交接的数据库快照，保存 `snapshot_max_id`；只选本任务里、代码确认来自操作者且在直接模式发送的消息。按 `chat_messages.id DESC` 读取（今天分页也按存储 id 排序，`db/chat.py:261-267`），不按时间戳、不重查漂移中的最新页。字符一律按 Unicode 码点（Python `len(str)`）计，不 strip、不正规化；至多 20 条、正文合计至多 4000 个码点。第一条永远保留：若它单条超过 4000，就取前 4000 个码点后停止；每条后续消息必须整条放得下，**遇到第一条放不下的就停止，不跳过找更短的，也不切开它**。满 20 条或 4000 码点立即停。每条保留 `id`、`message_id`、原长度、保留长度及 `truncated`；截断条在正文外显式标「此原消息仅保留前 4000 字符，后文已截断」，被省略的旧消息另记 `omitted_older = true`。引用头和标记不计正文预算，不能混进原话。选取和交付都保持倒序。转 pending 前在任务串行锁内重查快照上界，有新操作者消息就拒绝旧确认、生成新快照。因此最新纠正所在的消息不会整条被丢掉；超长时仍有它的前 4000 字符和明确标记。验收覆盖恰好放满、第一条超长、后续放不下即停、同时间不同 id、确认期间新纠正和重复交接读回一致。

### 6.2 编排 → 直接

- **自动转回：** 后台跑完、失败或停止时，状态到终态，模式随之是 `direct`。任务管家拿到一段代码生成的摘要（做完了哪些步骤、结果、改了什么、哪些关卡开着），下一句话就用全部工具接着做。
- **中途接手**（`/takeover`、按钮，或任务管家的 `task_take_over`），由代码执行，两种方式：
  - `finish`（默认）：不再派新步骤，正在跑的步骤做完并按今天的规则验收；这期间状态是 `handing_back`。最后一个步骤结束后 → 直接。
  - `stop`：立刻停掉正在跑的执行者和评审者（今天停任务的路径），没做完的步骤回到未开始 → 直接。
  计划保留，下次交给后台时接着它做。
- **暂停**（`/pause`、`task_pause`）：不再派新步骤，正在跑的照常做完，状态停在编排；`/start` 继续派。

### 6.3 再交给后台时的规划者

任务以前有计划时，走 `_restart_plan`（`tiers/planner.py:4392-4493`），新增类型化的 `RestartMode.REOPEN` 和 `TurnKind.REOPEN`（今天的枚举分别在 `message.py:96-106`、`tiers/planner_protocol.py:120-142`）。新增 `[REOPEN]` 这一种输入回合，输出仍用 `ChatReply` / `parse_chat` 和今天的 action、gates、waits 校验，不改其他回合的契约：

- 只对 REOPEN 跳过 `:4458-4460` 的「计划完成就直接返回」，先读交接再问一次模型；`_ended`（`:4496-4500`）没有 completed 分支，完成的任务用自己的完成开头。
- 等待中的关卡、讨论、开放的 wait 及恢复通知（`:4439-4452`）保留，`reset_task_for_restart(keep_steps=True)` 清 `completed_at` 后关卡重新收回复（7.3）。
- 答 `none` 先标交接送达；计划仍完成才再完成，否则按已有步骤继续。`add_steps` / `replan` 等仍由现有校验和应用路径处理。
- REOPEN 回合失败时**不派任何步骤**、不标交接送达，调用现有 `_fail_plan(..., fail_kind=exc.fail_kind)`（`tiers/planner.py:6704`，普通规划错误为 NULL，额度错误沿用已有种类）；`reopen_turn_failed` 只作为事件原因，不能写进 `tasks.fail_kind`（`db/tasks.py:136-141`、`schema.sql:299`，迁移 `step_failure_handling.py:131` 的 CHECK 也没有它）。原 `[RESTART]` 的完成即返回、回合失败后照旧跑未开始步骤（`:4472-4474`）和消息逐字保持。
- 定时任务的会话可能被 `_claim_schedule_session` 换成计划共用会话（`tiers/planner.py:7111-7141`）。凡有 `schedule_id` 的任务都拒绝再次交给后台，原因 `scheduled_task_reopen_unsupported`，不读写共享 Planner 会话、不改 `continues_schedule_session`；可直接只读问状态或另建普通任务。绑定意图规则的任务同样拒绝，原因 `intent_task_reopen_unsupported`，不重算规则那次执行。

这是 Planner 模型可见内容里唯一的新增回合；验收要同时冻住 `[RESTART]` 的既有行为。

## 7. 状态机

### 7.1 状态和模式

| 状态 | 存的值 | 模式 | 谁能写 |
|---|---|---|---|
| 草稿：从没交给过后台，可以没有需求 | `draft`（新，组 `TaskGroup.DRAFT`） | `direct` | 任务管家 |
| 就绪：需求写好、等确认 | `init`（复用） | `direct` | 任务管家 |
| 编排中、等你 | 今天的那些 | `orchestrated` | 后台 |
| 交回中 | `handing_back`（新，组 `IN_PROGRESS`） | `orchestrated` | 后台，做完正在跑的为止 |
| 已结束 | `completed`、`failed`、`stopped`、`cancelled` | `direct` | 任务管家 |

- `draft` 还需要：它的意思是「从没交给过后台」。现有的 `init` 意思是「有需求、没开始、只能由用户开始」（`db/tasks.py:21-23`），建任务要求有需求（`projects.py:572-574`）。`tasks.create_draft` 是新操作，不放宽 `tasks.create` 的非空校验（`v1_domains/task_create.py:59`）；草稿没需求时存空串 `''`，不存 NULL（`schema.sql:279` 是 `TEXT NOT NULL`）。`tasks_draft_shape` 要求没有计划、没有 schedule/意图规则绑定、未完成、未启动后台；首次交接前 `run_no` 保持初值。
- **分组不变量：** `TaskGroup.DRAFT` 不属于 `NEEDS_YOU` / `IN_PROGRESS`，因此 `draft` 绝不进 `ACTIVE_STATUSES`（今天在 `db/tasks.py:93-94` 推导）；`handing_back` 属于 `IN_PROGRESS`，在第 5 步加入后按活跃任务恢复，不能自动重派新步骤。
- 模式不存列，由状态推出：`ACTIVE_STATUSES` 里的是 `orchestrated`，其余是 `direct`（`db/tasks.py` 里一个函数，任务卡 1.2）。上表每个状态正好对应一个模式，另存一列只会和状态不一致（两次写之间崩溃、旧行要回填）。`handing_back` 和它唯一的写入方、服务启动时怎么处理它，一起在第 5 步加。旧路径建的任务（`tasks.create`、管家、定时任务）在第一次有人打开它的对话之前没有任务管家（第 9 节）。

### 7.2 转换

```
新建（控制台、转成任务、管家建草稿）                     → draft, direct
draft / 已结束 ──交给后台（需求已确认或「直接开始」）──→ pending, orchestrated
draft / 已结束 ──交给后台（需求待确认）──→ init ──开始──→ pending, orchestrated
编排中 ──跑完 / 失败 / 停止──→ 已结束, direct
编排中 ──接手 finish──→ handing_back ──做完──→ 已结束（stopped）, direct
编排中 ──接手 stop──→ 已结束（stopped）, direct
已结束（direct）──操作者接着说──→ 状态不变，任务管家直接干活
direct 任务 ──/done──→ completed
draft、init ──删除──→ （删除）
```

- 一行：**`draft（direct）──交给后台──→ 编排 ──跑完或接手──→ 已结束（direct）──再交给后台──→ 编排（又一次运行）`**。
- 结束不是终点，但「结束后接着做」不需要单独的重开机制：接着说就是直接模式；再交给后台就是又一次运行（`run_no` 加一，`task_runs` 记一行）。
- 开始、交给后台、接手、完成只由操作者触发（按钮、固定命令，或任务管家的工具在操作者要求时调用）。

### 7.3 依赖终态和就绪的地方

每处按可观察结果验收，组的穷举测试覆盖所有状态；第 1 步落实草稿相关项，第 3 步落实再交接，第 5 步才引入 `handing_back`：

| 位置 | 今天的假设 | 要做的 |
|---|---|---|
| `db/cost_holds.py:21-22`、`cost_holds.py:101-104` | SQL 排除终态，内存却无条件写 `awaiting_budget` | 两处都排除 `draft`、`init` 和终态，数据库和内存状态一致；费用拒绝只拒回合。`handing_back` 保留交回意图，不能被拦截覆盖后恢复派发 |
| `db/cost_guard.py:24` | 不是终态的任务的运行都算进窗口 | 任务管家的回合本来就该算 |
| `projects.py:1288-1291` | `EDITABLE_STATUSES` 是就绪和进行中 | 加 `draft` |
| `db/tasks.py:79-85` | `STATUS_FILTERS` 没有草稿键 | 增加 `draft` 过滤，列表、计数不漏草稿 |
| `ops/tasks.py:547-555` | 就绪的任务不收补充说明 | 草稿和就绪的话进任务对话 |
| `tiers/task.py:742-746` | 能重启的状态里有就绪 | 确认没有草稿 |
| `scheduler.py:125`、`db/schedules.py:305` | 「忙」= 进行中加就绪 | 定时任务不产生草稿（约束 `tasks_draft_shape`）；`handing_back` 仍占忙位 |
| `serve.py:571-590` | `tasks_left_running` 恢复有 live plan 的活跃任务，其余标 stopped | 草稿和 init 不启动；`handing_back` 有 live plan 时恢复交回意图，只收尾在途步骤，不派新步骤，然后 stopped；无 live plan 时标 stopped 并交回，不能卡在 handing_back 或从头规划。交回意图独立于 Planner 阶段持久化在第 5 步的转换记录中 |
| `db/user_gates.py:278-288`、`gate_routing.py:16` | 关卡只在进行中、`completed_at` 为空的任务上收回复 | 再交给后台时清掉 `completed_at`（`reset_task_for_restart` 已经这样做） |
| `task_notice.py:153-155`、`completion_events.py:143-164`、`task_notice_delivery.py:19-24` | 判断结束和按消息 id 去重是两件事 | `/done` 也算结束；以 `(task_id, run_no, end)` 生成稳定结束事件 id，同一次结束重投不重发，又一次运行有新 id、只通知本次发起处，不重发原来源通知 |
| `memory_retro_service.py:70`、`db/memory_retro.py:19` | 完成、失败的任务做复盘 | 第二次结束按游标只看新的部分 |
| `db/room.py:185-186` | 按 `completed_at` 留最近的房间 | 再交给后台时清掉，确认房间不被清掉 |
| `intent_rule_engine.py:605-612`、`:765-783` | 只启动 init；非终态、非 init 会走 stop | 规则仍只生成 init，不生成或绑定 draft；防御性遇到 draft 拒绝规则绑定/开始，撤销时不调用 control_task(stop)。状态不是 init 也不能把 draft 记成 acknowledged；原 init/终态路径保持。规则任务拒绝再交给后台（6.3） |
| `intents.py:692` | `resume_start` 只重试 init | 保持；draft 不能自动开始，交接确认才转 init/pending |
| `overview.py:45`、`db/ledger.py:745` | 活跃项目分组只有 NEEDS_YOU / IN_PROGRESS / READY | 增加 DRAFT 作为有待办的项目，独立计数；不因只有草稿就标闲置，也不把它算后台运行 |
| `tiers/manager.py:370`、`:3735` | `READY_TASKS_LISTED = 10`，只列 init | 第 6 步建草稿时同步列 draft，和 init 分组、各最多 10 条，注明未启动；不改会话路由（第 19 节的共用文件） |
| `task_workspaces.py:402-461` | 完成就提交并删 worktree；清扫只认 `active` | 不加新状态：建回后是 `active`，启动清扫照今天把完成任务的 worktree 提交到分支再删，下一句话再建回（第 10 节） |
| `tiers/task.py:794-828` | 结束收尾只跑一次 | `/done` 走同一条收尾；按运行号再跑；`db.tasks._task_ended`（`db/tasks.py:542`）只通知 listener |
| Tasks 页、侧栏计数、晨报、「需要你处理」 | 按状态分组 | `draft` 单独成组，不算进行中；超过 14 天没动的草稿在「需要你处理」里提一次 |
| `db/tasks.py:480`、`:531`、`db/agent_sessions.py:162-166` | 删除清 Planner，会话名单用于保留目录 | 删除任务也清任务管家；重启后台保留任务管家；启动只保留目录、不据此拉起进程（第 10 节） |

其余按 `ACTIVE_STATUSES` 读的地方沿用分组：`db/tasks.py:280/:435/:599`、`db/room.py`、`wait_service.py`、`subagents.py`、`failure_handling.py`、`db/projects.py`、`db/agent_sessions.py`、`ops/planner.py`、`db/waits.py`、`db/step_failures.py`、`role_api.py`、`project_move.py`、`ops/subagents.py`、`http_server.py`、`tiers/planner.py`、`room.py`、`profile_registry.py`、`failure_notices.py`、`consult.py`。它们不因新增 DRAFT 自动启动草稿；第 5 步必须验证 `handing_back` 仍占并发、可停止、不可删除、可收在途结果。单靠改映射不能证明恢复和派发正确。

## 8. 工作区和写权限

- **一个任务，一方写：** 直接模式下任务管家写，编排模式下后台写，由模式（从状态推出）和 6.1、6.2 的转换保证；第 1 步里，任务管家在回合中时「开始」被拒（409），编排中的回合只读启动；任务管家在编排模式下是只读启动，不只是不该写。
- **每任务排他租约：** 键为稳定的 `(database_instance_id, task_id)`，不是 checkout 路径，不用共享锁。锁文件在用户级状态目录 `${XDG_STATE_HOME:-~/.local/state}/looper/task-leases/`，不随 runtime root 改变；database_instance_id 是在 system_settings 一次初始化并读回的 UUID（并发初始化只取同一已提交值），不使用连接串或凭据作键。服务和 CLI 从同一数据库实例身份解析同一锁，测试才覆盖 `LOOPER_LEASE_DIR`。只支持本机本地文件系统，其他情况拒绝 `lease_unsupported`。任务管家的直接回合、终端的直接会话、后台的整个执行期和工作区清扫/删除都须持这一个任务的 `flock(LOCK_EX)`；编排模式的任务管家只读、不拿写租约。不同任务即使目录相同也不争这把锁。
- **顺序和崩溃：** 所有开始、交接和直接回合先拿任务租约，再在事务内重读状态和会话占用，持锁写状态后才启动写入方；不允许「查没有会话 → 写 pending」之间插进直接会话。转换失败不启动下一方；交回时先确认后台所有可写进程退出，再放租约，任务管家下一轮重新取锁和读状态。锁描述符由写进程继承（不设 close-on-exec），控制父进程被 SIGKILL 而子进程仍活着时不能提前解锁；进程组停止要确认所有可写后代退出，最后持有者退出才由内核释放，无超时抢锁。清扫拿不到锁就跳过，不删正在用的 worktree。拒绝时返回 `task_writer_busy` 和持有方；没有强行接管。这是 Looper 管理进程之间的本机互斥，不阻止操作者另开 shell 写文件。
- **两个任务共用一个原地目录：** 维持 W5，只提醒、不阻止；「同一时间只有一方写」只在同一个任务内部强制。任务面板上显示「这个目录还有另一个任务在用」（`others_active_in`，`db/tasks.py:96-102`）。
- **检查点：** worktree 任务交给后台时自动提交到任务分支 T（Looper 的身份、不跑钩子、尾注 `Looper-Task`，`step_workspaces.commit_pending`，`step_workspaces.py:690`），方便回退；原地任务不替操作者提交，只记录当时的状态，有未提交的改动时提醒。直接模式里 worktree 任务每个改了文件的回合也提交一次，可以「撤回上一轮」（只撤最后一轮，`git reset --keep`）；原地任务每轮列出改了哪些文件。
- **新建任务时选在哪干活：** 一个已有的目录就地干，或在项目仓库上开一个 worktree，默认用项目现有的设置（W1）。这也覆盖了「把已有目录登记成任务」：选那个目录、就地。

## 9. 并发、预算与资源

- **按需起：** 任务管家在有消息时才起，用 `--resume` 续上会话；闲置 30 分钟停进程（会话 id 留着）。不是操作者亲手建的任务（定时任务、规则建的任务）平时没有任务管家，操作者第一次打开它的对话时才起，第一轮带一份代码生成的任务现状。
- **上限：** 新设置 `tasks.managers.max_resident`，默认 6，全机同时活着的任务管家最多 6 个；新消息要起一个而已经满了时，先停掉最久没说话的那个空闲的；6 个都在回合中时，新消息排队，对话里显示「排队中：已有 6 个任务管家在工作」，有一个结束就轮到它。和管家会话的常驻上限（归能力范围 RFC）是两个设置；规划者不在任何一个里面。
- **内存：** 每个空闲约 240 MB，带子进程树约 310 MB（助理层 RFC 第 4.2 节），6 个约 1.5–1.9 GB；按需恢复只比常驻慢 1–2 秒，内存紧的机器调到 4。
- **花费：** 任务管家的每个回合记在 `llm_records`，带 `task_id`、`agent_id = task-manager-<id>`、role、有效 model/profile 和 cost，算进任务上限（自己的 `budget_usd`，没有就用项目的）。共享费用比较函数，但调用方政策分开：直接回合沿用 `_within_budget` 的读取失败放行（`tiers/planner.py:6866-6873`），记录 `budget_unverifiable`；到上限只拒这一轮、状态不变。再交给后台的预检则在 reset 前拒绝费用读取失败或未知费用，不能复用放行政策；提高上限只写本任务，不调用会改项目上限的 `tasks.continue`（`task_configuration.py:61-70`）。费用拦截照样生效，但不改草稿、就绪和终态（7.3）。
- **上下文维护：** 在 `task_manager.py` 实现 `manager_budget.bound` 的等价流程，复用 `session_budget` 的决定规则，不把 `ManagerSession` 或 `manager_sessions` 接进来。每轮结束从**该轮** `llm_records.context_tokens/cost_usd` 读测量，写 `agent_sessions.save_measurements(task-manager-<id>, ...)`；未知值写 NULL，不沿用上一轮。按 `scheduler_settings.session_context_max_tokens()` 和角色的回合预算决定 KEEP / COMPACT / ROTATE（现有模式在 `tiers/manager_budget.py:171-207`）。有 headless compaction 能力时，在同一串行队列和会话锁内、保持原权限跑 `run_turn("/compact")`，限时、记录 compaction 边界及 post_tokens；维护回合也以本任务 id 入账。只有成功且压后低于阈值才保留，否则下轮用任务简报和原话摘录轮换，插入 `session_fresh(reason=context_bound)`（现有确认流程 `tiers/manager_budget.py:213-248`）。`ResidentClaude` 的软停只是杀进程，不当作压缩。直接模式维护持任务写租约，编排模式维护仍只读、不争后台的写租约；维护不能和输入、切模型、交接同时跑；指标名在第 21 节。
- **执行并发：** 直接模式不派步骤，不占执行者和评审者的并发。

## 10. 续接

- 同一个工作区路径、同一个运行时和 provider、会话记录还在时，`--resume` 原会话。
- **存储：** `agent_sessions` 以 `task-manager-<id>` 为键，`profile_id` 存本轮实际 Profile，`selected_profile_id` 永远 NULL；后者的 CHECK 仅允许 manager / manager-%（`schema.sql:398-408`），按任务选择存 3.3 的覆盖表。上下文和末轮花费列来自 `manager_session_bounds` 迁移（order 660），不是建表原文（`db/migrations/manager_session_bounds.py:8-15`）。复用 `save_session` / `save_measurements`，不放宽 selected_profile CHECK。
- **清理与重启：** `db/tasks.delete_task`（`:471-482`）先由代码停任务管家、等子进程退出并拿租约，再在同一删除事务清 `task-manager-<id>`、任务对话线程和附属覆盖/交接行；删草稿也走这条路。`reset_task_for_restart`（`db/tasks.py:508-542`）只重置后台，不删任务管家会话；`keep_steps=False` 时仍只删 Planner。完成或闲置回收只停进程，保留会话。`saved_sessions()`（`db/agent_sessions.py:162-166`）是 `read_only_copies.py:238-246` 的保留目录名单，包含任务管家但**不作为启动名单**；服务重启不凭这些行启动 Planner 或任务管家，任务管家仍等首次消息。移动任务按 task id 保留会话，线程归属更新为新管家，不复制旧主管家的上下文。
- 续不上时开新会话，第一轮带简报和对话线程的摘录，并在对话里插一行类型化的事件 `session_fresh`：「原会话续不上，已用摘要重开」，带原因。不悄悄换会话。
- **完成的 worktree 任务**（worktree 在完成时删了，`task_workspaces.py:402-440`），操作者回来说话时：由 `task_workspaces.reopen` 决定（任务卡 1.2）：分支 T 还在、没有别处检出 → `prepare` 按 T 在原路径建回（`task_workspaces.py:351-356` 已有这条路），行回到 `active`，续原会话；T 没了或正检出在别处 → 只读打开：任务管家在工作区 W 里只读回答（新会话，插 `session_fresh`，原因 `branch_gone` 或 `branch_checked_out`），不能再交给后台。先查再调 `prepare`，因为分支没了时 `prepare` 会从基点悄悄开一个新分支。
- 不加 `kept` 状态：建回的 worktree 是 `active`；服务启动时 `sweep` 仍按今天的完成任务规则提交到分支再删（`task_workspaces.py:442-461`），但先拿第 8 节的任务排他租约，拿不到就跳过；下一句话再建回。`WorktreeState`、schema CHECK 和 `list_active` 不加状态（`task_workspaces.py:63-65`、`schema.sql:509`、`db/task_worktrees.py:41-46`）。直接模式每个改文件的回合提交，所以代提交的最多是最后一轮未提交部分；被忽略的文件和今天完成时一样不保留。
- 需要验证：Claude Code 换了工作目录后 `--resume` 能不能找到原会话；它自己会不会清理很久以前的会话记录。

## 11. 第 1 步：任务管家的直接会话

**做完能做什么：** 新建一个任务，任务管家在任务目录里直接干活，像在 Claude Code 里一样：回答分段、工具调用看得见、随时能停、只对操作者开放、写权限互斥。

| 卡 | 内容 | 粗估（人日） |
|---|---|---|
| 1.0 | 前置验证（排在最前）：真实 CLI 接假模型服务，临时 `HOME` 和 `CLAUDE_CONFIG_DIR`、合成配置。验证 PreToolUse 双来源、SessionStart 注入、四种 `disableAllHooks` 情况（3.3、22），以及同会话直接/隔离只读切换、Sonnet/Opus 切换后上下文仍在；保存 argv、工具清单和钩子哨兵的公开安全结论，不调用模型、不碰操作者配置 | 0.5–1 |
| 1.1 | 输入项目、标题、工作区选择和可选 `task_manager_profile_id`，新 `tasks.create_draft` 输出 task id、draft/direct、有效模型；无需求存 `''`。落点：新的 `v1_domains/task_conversations.py`、`projects.py`、`db/tasks.py`、角色和 Profile 注册、`db/role_configs.py`、`task_profile_overrides` CHECK、独立迁移。新角色 `task_manager` 默认 Claude Code Sonnet；独立开关 get/update，不进 chat_settings。验收：空需求草稿可建，旧 tasks.create 空需求仍拒绝；非操作者/错误项目/其他运行时拒绝；关着时旧路径和提示逐字不变 | 1.5–2 |
| 1.2 | 输入任务 id 和控制动作，输出持久化状态/派生模式或类型化拒绝。落点：`db/tasks.py` 的模式函数、`tasks.done`、`task_workspaces.reopen`、第 8 节的每任务租约、新操作及 7.3 的本步状态检查。验收：草稿不恢复后台；/done 幂等；分支还在建回、分支没了/被检出只读且不能交接；并发开始和直接回合只有一个可写 | 0.5–1 |
| 1.3 | 输入任务消息、有效角色配置和会话状态，输出 delta/agent.tool、回合记录和持久化会话。落点：新 `tiers/task_manager.py`，按 3.3 自组直接启动参数（不调用 isolation_args）、按第 9 节维护上下文和预算；`agent_sessions` 保存/清理及 `read_only_copies` 只保留；worktree 每轮提交。验收：中断/队列、6 个上限/闲置回收、原会话续接/显式 session_fresh、每轮 context/cost、压缩成功/失败/未知、跨权限和模型重启续接、不支持运行时拒绝 | 3–4 |
| 1.4 | 任务对话的地址：`operator_chat.send` 的目标 `kind: task`、对话线程 `task-chat:<task_id>`（`chat_threads` 归任务的管家，`chat_messages.channel` 记入口）、停止的入口、固定命令 | 1–1.5 |
| 1.5 | 输入任务绑定 MCP 凭据和工具请求，输出本任务结果或拒绝。落点：新任务管家 MCP 授权模块、`task_status` / `task_requirement_set`、`cli.py` 分发表及第 18 节各本机守卫。验收：外任务 id、未知命令/工具、全部 deny 叶子、伪造原话、非操作者；枚举 parser 叶子保证表无遗漏，旧 LOOPER_AGENT_RUN 行为不变 | 0.5–1 |
| 1.6 | 验证：全新数据库、非 8767 端口、临时 runtime root 上跑 `looper serve`，配假模型走完「新建 → 直接干活 → 停止 → 接着说 → 完成」；写好任务管家的对话评测用例，真实运行列进 PR 等批准 | 1 |

合计 8–11.5 人日（含 1.0）；不含 1.0 是 7.5–10.5。这些就是本文内的任务卡边界，不引用仓库外的「已写成」任务卡。1.0 先出报告，由维护者确认钩子风险的处理；**报告出来且前置条件通过之前，任何会启动任务管家的卡都不开 PR**。离线探针不能证明的行为写为未验证，付费后备验证另行批准；不自动改用操作者环境启动。1.1 的纯状态/配置部分、2.1、2.2 可以先做；共用文件按第 19 节排顺序。

1.4 的输入是操作者发送/停止请求（目标 `kind: task`），输出线程消息和事件流；落点是 `chat_targets.py`、`v1_domains/operator_chat_streams.py`、新的任务对话读写模块、`channels/web.py`。它调用现有 `db/chat.save_message`，不改该函数；另存消息的任务归属、可信发送者及发送时的派生模式供 6.1 取原话。验收要从 HTTP 发送到持久化后分页读回，再测重复发送去重、非操作者拒绝和 Stop 无需排队。1.6 交付 1.1–1.5 的完整 smoke 证据及第 20 节用例；真实评测尚未批准时只提交用例和待验证项，不写通过。所有新操作/工具声明 action level 和一句理由，校验/权限错误用类型化结果。

## 12. 第 2 步：多任务控制台

### 12.1 页面

新页面 `#work`，在 `App.tsx` 的 `PAGES` 里登记（`web/src/App.tsx:25`），和 Chat 页并存，默认入口不变；侧栏多一项「Work」，只在开关打开时出现。地址 `#work`、`#work/manager`、`#work/task/<task_id>`，在单独窗口里直接打开也能用。照 `docs/design.md` 的「界面设计」一节。

| 区域 | 内容 |
|---|---|
| 左栏 | 「新建任务」（页面上唯一的主色按钮，也可以按 `N`）、搜索框、置顶「管家」、任务列表（12.2） |
| 中间 | 选中项的对话：分段的回答、「在做什么」一行、可以展开的工具记录、回合小结、`session_fresh` 事件；编排中穿插关键活动、规划者的决定和关卡卡片（可以直接回复）；输入框（暂存、历史、Stop 沿用 `ChatComposer`） |
| 右栏 | 状态和模式、控制（交给后台、接手、暂停、完成）、本任务模型选择（Sonnet / Opus，显示有效来源和切换结果）、需求（带版本）、计划和各步骤、关卡、改动的文件、花费；选中管家时是「需要你处理」的清单。新建任务也有同一模型选择，默认 Sonnet |

### 12.2 任务列表（验收）

- **全部任务**，不是最近 30 个（Chat 页的上限，`chat_targets.py:21-22`）；默认视图之外的分页加载；每个任务，包括已结束的，都能从列表里找到并进入。
- **默认视图：** 没结束的（草稿、就绪、编排中、等你、交回中），按最近动态倒序，按项目分组。定时任务产生的任务按计划折叠，一个计划一行：计划名、最近一次的结果和时间、连续失败的次数。已结束的在一个可以折叠的分区里，最新的在前，可以按项目和状态筛选。
- **每一行：** 状态和模式（文字和图标）、等不等你、有没有没看过的进展（圆点）。
- **「看过」：** 服务端新表 `task_seen(principal, task_id, seen_activity_seq, seen_message_id, seen_at)`，按操作者存，控制台和命令行共用；打开对话时写入当时最新的序号，只往前走。
- **接口：** 只读的 `tasks.work_list`，分区 `open`、`ended`、`schedules`，游标分页（不用 offset），每页至多三条语句；用到 `idx_activity_task`、`idx_task_user_gates_waiting`、`idx_tasks_schedule`，部分索引是否需要由 2.1 的 EXPLAIN 和测量决定，迁移序号不在 RFC 中预占。字段、查询和量法就在下方 2.1。

### 12.3 切换（验收）

- 点一次就切换，不刷新页面，后台照常跑；每个任务记着自己的未发输入（`sessionStorage`）和滚动位置；回来时停在离开的位置，上面一条分隔线「你离开之后」；先用缓存画出来，再在后台补。
- **快速切换：** `Ctrl/Cmd-K`，按标题、需求文字和 MR 号过滤（服务端的确定性解析 `tasks.select`，规则在第 14.3 节），Enter 打开。

### 12.4 性能（验收）

在灌了约两千个任务（大多是定时任务产生的、已结束）的一次性数据库上，用无头浏览器量：打开 `#work` 到列表第一屏可以操作约 1 秒；点一个任务到对话可以输入 300 毫秒以内（有缓存）、第一次打开 800 毫秒以内；`tasks.work_list` 的 `open` 和 `schedules` 在服务端 100 毫秒以内；`tasks.select` 150 毫秒以内。做不到时写明量到的数和原因，不放宽。

### 12.5 卡片

| 卡 | 内容 | 粗估（人日） |
|---|---|---|
| 2.1 | 输入分区/项目/状态/cursor/limit，输出分页行、next_cursor；`tasks.mark_seen` 输入任务及已看到的两种序号、输出读回游标。落点：新 `db/task_work_list.py`、v1 操作、task_seen 迁移，接口和 SQL 见下文。验收：各分区全部可达、同时间游标稳定、已读只增不减、SQL 次数和 12.4 性能 | 1.5–2 |
| 2.2 | 输入 `selector`、可选 cwd、all，输出 `selected(task_id)` / `candidates(rows)` / `not_found` 的判别联合；规则完整按 14.3。落点：新 `task_selection.py`、`tasks.select` 操作、共用 CLI 调用。验收：id/MR/文字/cwd、歧义和无匹配、90 天边界、最多 10/20 条；解析后再次校验访问权和 own-task-only，不调用模型 | 1–1.5 |
| 2.3 | 输入开关、路由和 2.1/2.2 响应，输出 `#work`、左栏和快速切换。落点：`App.tsx`、侧栏、新 `pages/work.tsx`、api 类型；不改旧入口。验收：开关关着路由/侧栏不可见；空/错误/分页、Cmd-K、键盘返回、已结束可达，四宽度截图不入仓库 | 2–2.5 |
| 2.4 | 中间的对话：从 `chat.tsx` 抽出流式处理为 `useReplyStream`（旧页面行为不变），工具记录、Stop、排队、`session_fresh`、活动和关卡卡片（抽出 `task-detail.tsx:285` 的 `LiveActivityFeed`，`task-gates.tsx` 原样用），未发输入和滚动位置 | 2.5–3 |
| 2.5 | 右栏，改动的文件用新只读操作 `tasks.changes`；模型通过 3.3 的专用切换操作写入并读回。验收：创建选 Sonnet/Opus、面板切换后会话续上，busy/不支持运行时/失败结果不伪装成功；切模型不动后台角色 | 1.5–2 |
| 2.6 | 验证：12.4 的性能，四种宽度的截图（不提交），空、出错、开关关着几种状态，键盘 | 1–1.5 |

合计 9.5–12.5 人日。2.1、2.2、2.3 和第 1 步并行；2.4 等 1.4 合入后接上任务对话。

**2.1 的接口和查询：** `tasks.work_list({partition, project_id?, schedule_id?, status?, cursor?, limit=50})`，limit 为 1–100；输出 `rows`、`next_cursor`，普通行有 `task_id, title, project_id, status, mode, schedule_id, recent_at, latest_activity_seq, latest_message_id, waiting_gates, unread`。`open` 是非终态、非定时的任务（含 draft/init），`ended` 是终态、非定时任务；定时任务一律在 schedules，一行一个 schedule，带 schedule_id、名称、最新 task_id/结果/时间及连续失败数。展开计划后用 partition=schedules 加 schedule_id 返回该计划的各次任务行（不再折叠），不能漏掉历史运行。查询边界如下：

1. 主查询在可见任务里筛项目/状态/分区；对每个任务 `LEFT JOIN LATERAL (SELECT seq, created_at FROM activity WHERE task_id=t.id ORDER BY seq DESC LIMIT 1)`，再用同形的 chat_messages 查询（`session_id='task-chat:' || t.id ORDER BY id DESC LIMIT 1`），`recent_at = greatest(t.updated_at, activity.created_at, chat.created_at)`。外层按 `(recent_at, task_id) DESC`，下一页用 `(recent_at, task_id) < (:cursor_at, :cursor_id)`，取 limit+1。schedules 先按 schedule_id 聚合、取最新运行，再以 `(recent_at, schedule_id)` 作游标；连续失败数按各次任务 `created_at,id DESC` 数到第一条非 failed 为止。条件先按用户权限收窄，不拼接用户 SQL；游标带版本、分区和筛选摘要，不匹配拒绝 `invalid_cursor`。查询中读到更新的数据可移动排序，前端按 id 去重；静态数据分页必须完整且不重复。
2. 第二条 SQL 按本页 task ids 批量取等待关卡，使用 `idx_task_user_gates_waiting`；第三条批量取当前 principal 的 task_seen。应用层比较已读序号和最新序号得 unread，不逐行再查。现有索引出处 `schema.sql:332/:914/:1240/:1329-1330`；迁移是否加索引，以这两千任务的 EXPLAIN ANALYZE 和 100 ms 目标为准，不声称当前已达标。
3. `task_seen` 以 `(principal, task_id)` 为主键、task 外键级联删除；`tasks.mark_seen` 的两个序号先校验属于本任务、且不超调用时最新值，再 `ON CONFLICT ... DO UPDATE SET seen_activity_seq=greatest(old, new), seen_message_id=greatest(old, new)`，这里 message id 用存储整数 id。未发出的消息不计已读；非法归属拒绝。两个并发客户端倒序回写的结果也只向前。

2.4 的输入是 1.4 的事件流和历史分页，输出中间对话及未发输入/滚动恢复；落点是新的共用 hook、work 对话组件及 `chat.tsx` / `task-detail.tsx` 的最小抽取。验收至少覆盖旧 Chat 同样流事件的回归、切走再回来不丢输入、Stop、关卡、session_fresh 和模型切换提示。它与 #1747 共用 `task-detail.tsx`，不是可直接无冲突并行的卡。2.6 按 12.4 在一次性服务测量，从浏览器收集截图、耗时和错误状态；禁止把「页面能渲染」当作交互/性能通过。

## 13. 第 3 步：交给后台和转回

6.1 的交给后台（交接回合、需求确认卡片、检查点、写权限移交、`task_handoffs`、启动规划者）；6.2 的自动转回和代码生成的摘要；编排模式下的消息路径（`task_supplement`、`task_gate_answer`、规划者决定的事件）；6.3 的 `REOPEN` 控制流和 `task_runs`；再交给后台时的预算预检（共用费用比较，但不用 `_within_budget` 的读取失败放行政策；在重置之前做，到了上限必须给新上限，只写本任务的 `tasks.budget_usd`；费用读不到或未知时拒绝，定时/规则任务也拒绝）。开关 `tasks.orchestration_switch.enabled`，默认关。粗估 5–8 人日。

**验证：** 两个方向的每种情况各一个测试；交接文字由代码校验、工作区状态由代码读；`task_supplement` 交给规划者的是操作者的原话；关卡回答送到关卡；编排模式下任务管家的启动参数里没有写工具；`[REOPEN]` 收得到交接；规划者的 `[REOPEN]` 跑真实评测；一次性 serve 上「直接干一半 → 交给后台 → 回来问进展 → 跑完自动转回 → 接着改 → 再交给后台」。

## 14. 第 4 步：命令行

### 14.1 `looper task enter`

- 接的是**同一个任务管家会话**：在任务的工作区里 `claude --resume <任务管家的会话 id>`，用操作者本人的环境，所以网页和终端看到的是同一份上下文。
- **同一时间只有一处持有这个会话：** 会话锁文件和第 8 节任务租约在同一用户级目录，用同一实例/任务键再加 `.session.lock`。`enter` 通过服务端串行队列停掉网页侧任务管家（在回合中则先等结束或由操作者 Stop），再取会话排他锁；直接模式另取本任务写租约，复查状态后才启动 Claude Code，锁都由子进程继承。父进程被杀而 Claude Code 还在时锁仍在。网页显示「这个任务的会话正在终端里打开着」，不并发发消息；终端退出后网页续同一会话。
- 编排模式或分支不可恢复时，同样用 Looper 的 `isolation_args` 加 `read_only_args`，不把可退出的 `--permission-mode plan` 当只读边界；直接模式用 3.3 的启动。切模式也必须先结束终端持有的旧进程，不越过会话锁。
- 第一次进入、Looper 那边还没有这个任务的会话时，`enter` 用 `--session-id` 建一个，存进 `agent_sessions`，之后两边都续它。

### 14.2 `looper task ask`、`brief`

- `looper task ask [--task <选择>] <text>`：把一句话发进任务对话，等任务管家的回答打印出来；在 `enter` 打开的会话里不用（那里本来就是任务管家）。
- `looper task brief [<选择>]`：打印任务简报（`task_brief.compose`，任务管家的追加提示也用它）。

### 14.3 不用任务 id 找到任务

`enter`、`ask --task`、`brief` 和控制台的快速切换用同一个确定性的解析（`tasks.select`，任务卡 2.2），结果是一个任务、一张候选表或没找到，从不调用模型：任务 id；省略时按 `cwd` 的真实路径找最深的那个任务目录，找不到列出最近 14 天的任务（最多 20 个）；一个 MR（链接、`!N`、`#N`、`MR N`、`PR N`、1 到 7 位纯数字），和等待步骤的 `github_pr` 检查以及标题、需求、原话、补充说明里的引用比较；别的文字按词全部命中标题或最新需求。范围最近 90 天（`--all` 全部）；只有恰好一个匹配才直接进入，几个就列出（最多 10 行），没有就明说。

命令都在 Looper 启动的 agent 里拒绝（`LOOPER_AGENT_RUN`，同 `ledger_commands.py:152-161`）；在任务管家里（`LOOPER_TASK_MANAGER`）只能作用于它自己的任务。退出码是这组命令自己的：0 完成，2 用法，3 列出了候选、什么都没做，4 拒绝，5 没有这个任务，6 数据库不可用，7 找不到 Claude Code。粗估 3–5 人日。

## 15. 第 5 步：中途接手、评审和回退

6.2 的中途接手（`finish`、`stop`、`handing_back`）和暂停；直接模式下「评审一下」：一个独立的评审者看当前的改动（从上次评审，没有就是工作区的基点，到现在），结论放回对话，不阻挡任何东西，花费算在任务上；worktree 任务「撤回上一轮」。粗估 4–6 人日。

## 16. 第 6 步：两种场景的衔接

- **转成任务：** 主管家对话里每条消息有「转成任务」：选起止两条，生成一个草稿任务，选中的消息由代码复制进它的对话作为开场（带标注，管家的回复和别人的话围起来），切到这个任务；原对话不动。主管家判断一件事值得建任务时，建的也是草稿并把操作者带进去（新工具 `looper_task_draft`，建之前要确认，`tiers/manager.py:339`）；操作者明确说「直接跑」的才直接交给后台。这改了管家的模型可见内容，跑真实评测。新草稿默认任务管家的 Sonnet，不继承发起管家的模型。
- **飞书回复落进任务对话：** 发到飞书的任务通知记下消息 id 和任务（新表 `task_notice_posts`，做法同 `task_user_gate_posts`，`schema.sql:953-962`）。操作者回复其中一条时，先按关卡帖的规则（`gate_routing.py`），不是关卡帖就按这张表找到任务，这句话交给任务对话，任务管家在对话里回答，回答也回到这条飞书消息下面。只认操作者本人；群里别人的回复照今天交给管家。
- 粗估 3–5 人日。
- **第 7 步：飞书话题**，做完第 6 步再评估要不要；要做时，哪些任务开话题、群里谁能加入，在那一步开工前定。

## 17. 主管家、场景管家和任务管家

- 主管家照旧处理临时的事和跨任务的事，仍用 `REPLACE`；它在飞书里被点名某个任务时照今天用 `looper_task_instruct`，这条补充说明也作为一条消息记进那个任务的对话线程（入口 `lark`），回到电脑前看得到。
- 群里的场景管家面向群里的人，按群的能力范围办事，和任务管家互不影响；群里的人不进任务对话。

## 18. 安全

- **只对操作者开放：** 任务对话的发送、建草稿、交给后台、接手、完成、模型切换、`tasks.work_list`、`tasks.mark_seen`、`tasks.select`、`tasks.changes` 都是 `operator_only`；API 客户端答 `operator_only`。管家的 `looper_task_draft` 只在操作者的回合里能用（`ops/tasks.py` 调用时用 `operator_call(call.turn)`，`ops/service.py:1493`，只调用、签名不变）。飞书里回复任务通知只认操作者本人。
- **操作者本人的环境：** 直接模式能读写操作者能读写的东西；用户设置、钩子和 MCP 会加载，但其中调用下表拒绝的 Looper 入口也被拒绝，不能声称所有操作者检索命令照常可用。Looper 的三处附加限制要由任务卡 1.0 证明不能被 `disableAllHooks` 等设置关掉；没有证明之前不启动，不把规格当已验证的隔离。
- **本机命令的类型化结果：** `CommandPolicy = ALLOW | OWN_TASK_ONLY | DENY`。在 `looper/cli.py:421` parse 后、任何分发和副作用前检查；未知叶子默认 DENY，通常退出 4、原因 `task_manager_command_denied`。OWN_TASK_ONLY 必须在解析选择器后再校验实际 task id 等于 `LOOPER_TASK_MANAGER`，令牌也必须绑定该任务，否则 `task_manager_task_mismatch`。保留原 `LOOPER_AGENT_RUN` 的所有行为（两变量都有时先应用旧规则，绝不借新变量放行）。安全的全局 `--version` 和 parser help 保留。

| CLI 叶子（含全部参数变体） | LOOPER_TASK_MANAGER 下的结果 | 当前 origin/main 的分发/定义 |
|---|---|---|
| `version` | ALLOW | `looper/cli.py:97-98/:422-425` |
| `serve`；`service install/probe/restart/upgrade/status/uninstall` | DENY，包括 status/probe | `looper/cli.py:23/:345-415` |
| `auth set-password/clear-password`；`config init-ops`；`doctor` | DENY | `looper/cli.py:41-77` |
| `profile show` | DENY（静默空输出、退出 0，见下表） | `looper/cli.py:86-90` |
| `claude-code profile`（含 `--hook`）、`mcp`、`status`、`install`、`uninstall` | DENY；hook 静默例外见下表；dry-run 也不放行 | `looper/cli.py:102-130` |
| `ledger list/show/add/close/confirm` | DENY；读也不扩大到全局台账 | `looper/cli.py:191-231` |
| `leads search/mark/delete/eval-export` | DENY，没有 export 别名 | `looper/cli.py:235-269` |
| `memory list/show/check/lessons/reject/retry/revoke/renew/delete` | DENY | `looper/cli.py:282-322` |
| `knowledge migrate`（含无 `--execute` 的预览） | DENY | `looper/cli.py:272-279` |
| `msgflow import/delete/leads-backfill/parity/why` | DENY，包括只读和 dry-run | `looper/cli.py:133-188` |
| `decisions replay` | DENY | `looper/cli.py:325-337` |
| `mcp-server` | OWN_TASK_ONLY，只接受任务管家专用凭据，服务端逐工具校验任务绑定，不能退回操作者令牌 | `looper/cli.py:91-96` |
| 新 `task enter/ask/brief` | OWN_TASK_ONLY；不能省略 id 后绕过解析结果检查。enter 仍须会话/写租约，递归进入已占用的自身会话拒绝 session_busy | 第 14 节（尚未实现） |

现有所有 agent 标记检查逐处对应如下；既覆盖 CLI 表，也在可单独调用的函数入口复用同一判定，不能只加总分发漏掉钩子或 MCP 入口。标记定义在 `looper/child_env.py:60`，注入在 `agent_settings.py:1150`；全仓 `git grep` 的其余命中是注释/注入，不是额外守卫。

| 当前守卫（origin/main） | LOOPER_AGENT_RUN 今天的行为 | LOOPER_TASK_MANAGER 的类型化结果 |
|---|---|---|
| `ledger_commands.py:152-161` | add/close/confirm 拒绝，读放行 | DENY 写；本 RFC 的 CLI 表也 DENY 读，不给任务管家操作者身份的台账确认能力 |
| `leads_commands.py:288` | 全组拒绝（含 search） | DENY 全组 |
| `memory_commands.py:167` | 全组拒绝（含读和 check） | DENY 全组 |
| `knowledge_commands.py:44` | migrate 全拒绝 | DENY，包括预览 |
| `profile_commands.py:21` | `read_text` 返回空串 | DENY，沿用静默空串/退出 0 的展示，不查询画像 |
| `claude_code_entry.py:58` | 拒绝取画像；hook 无输出/退出 0，非 hook stderr/退出 4（`:69-77`） | DENY，沿用这两种展示；不能让 SessionStart 钩子因拒绝输出报错噪声 |
| `claude_code_entry.py:323` | `edit_settings` 拒绝 | DENY，不能安装、卸载或改操作者设置 |
| `claude_code_entry.py:481` | 安装/卸载分发拒绝 | DENY，包括 dry-run；与 edit_settings 双入口一致 |
| `claude_code_mcp.py:146` | operator retrieval MCP 拒绝 | DENY，不启动操作者检索服务；任务只读状态通过任务绑定 MCP 读取 |

负向验收遍历每个 DENY 叶子（含新增 knowledge、check、eval-export、dry-run、hook 静默），加上外任务 id、解析后指向别的任务、未知命令和错误 MCP 凭据；正向对照 version 和 own-task 的状态读取，递归枚举 parser 叶子防漏。`LOOPER_TASK_MANAGER` 和 `LOOPER_AGENT_RUN` 都只是**减速带，不是隔离**：同系统账号的 shell 能取消变量、读操作者令牌或绕开 CLI 调接口，本文不声称阻止这些行为。

- **注入：** 简报、转成任务的开场、交接、代码生成的摘要、规划者的决定事件里别人写的内容，由 `untrusted.fence()` 围起来，这几处总是打开。
- **私有内容：** Looper 不读操作者的 Claude Code 会话记录，也不写操作者的 Claude Code 文件；会话 id 只在本机；交接文字和转来的开场会被执行者和评审者看到，界面写明。

## 19. 和能力范围 RFC 的分工

- 任务管家不走管家的会话路由（3.2）。本文不改 `session_routing` 的 SessionKind/键/解析、`tiers/manager_routing.py`、`manager_sessions` / `db/manager_sessions.py`、`tiers/group_session.py`、`ops/service.py` 的授权、运行时的范围启动模式。任务对话用新读写模块调用 `db/chat.save_message`，不改它、`save_channel_message`、`activity_since` 或 `latest_id`；也不改渠道可信 sender_kind 的判定。
- 那一步原先计划留给本文的扩展点（内置集合 `task_session`、放宽「受众是操作者当且仅当范围是 `full`」、`SessionKey` 加 `TASK`）都不需要。
- 本文拥有：`db/tasks.py` 的状态分组；`projects` 的建任务、`check_task_control`、`start_ready_task`、`control_task`、`store_chat_supplement`、`EDITABLE_STATUSES`；`tiers/task.py` 的开始、重启、收尾和恢复；`tiers/planner.py` 的 `_restart_plan`、`_restart_message`；新的 `tiers/task_manager.py`；`chat_targets.py` 的 `kind: task`；`channels/web.py` 的 `agent.tool` 事件；`v1_domains/operator_chat_streams.py` 的 `run`；`v1_domains/task_create.py`、`task_views.py` 和新的几个操作；`ops/tasks.py`；`task_workspaces` 的 `reopen` 和它对 `prepare` 的调用；`db/cost_holds.py` 的状态排除；`web/src/pages/work.tsx` 和新组件（从 `chat.tsx`、`task-detail.tsx` 只抽出，不改行为）；`cli.py` 的 `task` 命令组。
- **文件重叠是存在的：** 已按 `git diff --name-only origin/main origin/codex/scopes-1b-routing` 核对 #1747。本文后续卡会共用 `agent_settings.py`（注册 task_manager 角色和配置）、`runtimes/__init__.py`（只在 TOOL_ROLES 注册新角色，现有列表在 `:280`，不改范围启动模式）、`tiers/manager.py`（第 6 步的 looper_task_draft、草稿清单）、`ops/__init__.py`（只注册新工具，不改待确认）、`channels/lark.py`（第 6 步通知回复入口）、`web/src/lib/api.ts`（新接口类型）、`web/src/pages/task-detail.tsx`（2.4 抽 LiveActivityFeed，当前 `:285`）。这些不是同一职责，但确实是同一文件：实现卡与 #1747 的 owner 协调顺序，按最新 main 做最小补丁，复跑双方受影响的测试；不再承诺无文件冲突。第 6 步对 manager/Lark 的改动排在相应 1b 入口稳定后。`claude_cli.py` 也在 #1747 diff 中，本文只调用现有 ResidentClaude/只读参数，不为任务管家修改它；直接模式自组参数，避免 hooks 导致 setting-sources 被清空（3.3）。`channels/web.py` 和 `db/chat.py` 不在该 diff 里。
- 共用的 `untrusted.Source` 只加 `TASK_REQUIREMENT` 值。`chat_scopes` 的迁移 2040 已在 main（`db/migrations/chat_scopes.py:6`），不是本文的预留号。本文**不锁定** 2050/2060/2070；实现卡建迁移前必须扫描最新 main 及全部开放 PR 的 order，再由协调者分配不冲突的号，角色注册/覆盖 CHECK 和 task_seen 各列入对应迁移。

## 20. 分步计划

| 步 | 内容 | 依赖 | 开关 | 粗估（人日） |
|---|---|---|---|---|
| 0 | 任务页的「和这个任务对话」入口（#1736） | — | — | 已做 |
| 1 | 任务管家的直接会话（第 11 节） | — | `tasks.conversations.enabled` | 7.5–10.5，另加前置验证 1.0 的 0.5–1 |
| 2 | 多任务控制台（第 12 节） | 和 1 并行；2.4 等 1.4 | 同上 | 9.5–12.5 |
| 3 | 交给后台和转回（第 13 节） | 1 | `tasks.orchestration_switch.enabled` | 5–8 |
| 4 | 命令行进入同一个会话（第 14 节） | 1、2.2 | 无：新命令 | 3–5 |
| 5 | 中途接手、评审、回退（第 15 节） | 3 | 同第 3 步 | 4–6 |
| 6 | 转成任务、飞书回复落进任务对话（第 16 节） | 1 | 跟 `tasks.conversations.enabled` | 3–5 |
| 7 | 飞书话题 | 6 之后再评估 | — | 待定 |

- 第 1、2 步合起来才算「可以用新方式工作」，合计 17–23 人日（不含 1.0），两条线并行时日历上约 10–12 个工作日。第 3 步做完两种模式才完整；在那之前要交给后台，用现有的方式建任务。
- **模型可见的部分：** 任务管家的追加提示和工具描述（第 1 步）、交接内容和编排模式下的工具（第 3 步）、规划者的 `[REOPEN]` 那一轮（第 3 步）、命令行的简报（第 4 步）、主管家的 `looper_task_draft`（第 6 步）。规划者契约的其他部分不动。

**评测和预算：** 以下是待维护者批准的真实评测计划，本 RFC 不执行。先写用例、跑离线探针，再按 `evals/README.md` 的 real-mode 比较流程执行；本设计的模型选择和次数按下表，不能套通用全量 Opus 档位扩大。先完成并接受 Sonnet 的任务管家/建草稿用例，再做一次任务管家切到 Opus 的小确认，只验证切模型后续接、任务绑定、只读工具和费用仍在界内，不在 Opus 上重跑整套。Planner 的三个 `[REOPEN]` 用例用**执行前读回的实际 Planner 有效模型/Profile**，基线和候选固定同一个，不强制 Sonnet，也不公开实际配置；这是测试目标配置的读取，不向真实服务发任务。

| 评测 | 固定用例 | 首轮次数 | 每次上限（`--case-budget-usd`） | 含补跑的分项上限（`--total-cap-usd` 初始值） |
|---|---|---|---|---|
| 任务管家，Sonnet 先行 | 8 个：夹具中直接改并产简报、Stop 后续接、只读问进展、原话补充及单独说明、回答关卡、拒绝外任务、确定性交接摘录、编排只读续接 | 8 × 2 = 16 | 2 美元 | 16 美元 |
| 主管家建草稿，Sonnet | 1 个：确认后建草稿、默认任务管家 Sonnet、拒绝非操作者 | 1 × 2 = 2 | 2 美元 | 3 美元 |
| Planner `[REOPEN]`，实际有效模型 | 3 个：已完成计划、新交接的失败任务、带原话和等待关卡；用预置计划只评新增回合，不重跑 Worker/Reviewer | 3 × 1 = 3 | 1 美元 | 3 美元 |
| 任务管家 Opus 切换确认 | 1 个：Sonnet 会话停进程 → Opus 续接 → 编排只读续接，验证上下文、工具和花费；再切回 Sonnet | 1 × 1 = 1 | 3 美元 | 3 美元 |

首轮精确为 **22 次用例执行**；每个首轮执行最多补跑 1 次，总次数最多 **44 次**，不是必须花完或必须补跑。总金额硬上限 **25 美元**（16 + 3 + 3 + 3），其中所有 Opus 调用合计硬上限 **6 美元**：切换确认整例至多 3 美元，若实际 Planner 用 Opus，其分项至多 3 美元；其他角色不升级为 Opus。切换确认中的 Sonnet 回合也算该例 3 美元内。多回合用例和压缩维护都算同一例的额度，不能另开命令绕过。

`--total-cap-usd` 只约束单次命令（`evals/README.md:650/:1100-1105`），所以执行方另外保存同一批的已花台账。每次运行/重跑前读回已花，将本次 cap 设为「分项剩余额度、25 美元总剩余额度、若含 Opus 再加 6 美元 Opus 剩余额度」的最小值，并预留在途调用；每例也扣自己的剩余额度。并发分项统一预留，不各拿一份完整 cap。首次失败才允许补跑，不能因截断、超预算或重开进程重置额度。钱先到就停，报告未完成次数，不能写成全覆盖通过；要扩大范围另请维护者批准。任务卡 1.0 默认不调用模型；确需付费后备时先报告并批准，也从上述剩余 25 美元内扣，不另加隐藏预算。两个功能开关在对应真实评测被接受前保持默认关。

**凭据：** 真实评测给服务和全部子进程一次性的 `HOME`、`CLAUDE_CONFIG_DIR`，只放固定的合成设置，不复制操作者的配置、钩子、MCP 或会话。默认候选是操作者提供的订阅 OAuth token（`CLAUDE_CODE_OAUTH_TOKEN`）；API key（`ANTHROPIC_API_KEY`）是替代方案，**由操作者选择**，不自动降级或改计费来源。凭据只注入该次评测环境，不进用例、命令日志、报告和仓库；订阅仍按评测记录的费用口径检查上述上限，不把订阅解释成无预算。尚未选择或不可用就不跑真实评测。

## 21. 可观测性

**指标**（进 `manager_metrics.get` 的新字段 `work_mode`）：

| 指标 | 维度 | 用来看什么 |
|---|---|---|
| 任务对话回合数、主管家对话回合数 | 模式、入口 | 在电脑前的交互是否大多在任务对话里 |
| 发出到第一段文字、排队等待 | 模式 | 任务管家是否「不排队」 |
| 停止次数和从按下到停住的时间 | 模式 | 「随时能停」是否成立 |
| 交给后台、自动转回、中途接手的次数 | 方向、方式 | 两种模式的用法 |
| 活着的任务管家数、满了以后回收和排队的次数、续接落到 `session_fresh` 的次数 | 原因 | 第 9、10 节 |
| `task_manager_context_tokens`、`task_manager_turn_cost_usd` | task_id、turn/record id、model、mode、maintenance | 每个任务管家回合的上下文大小和花费序列；来自该轮 llm_records，未知为 NULL，不冒充 0；会话末轮值写 agent_sessions |
| `task_manager_compactions_total`、`task_manager_session_rotations_total` | 原因、结果 | 压缩是否成功、压后 tokens 是否下降；失败/超时轮换是否带 session_fresh；压缩的前后值记在维护记录 |
| 同一件事拆成多个任务的比例 | 项目 | 再交给后台的次数对新建任务数 |
| `tasks.work_list`、切换的耗时 | 分区 | 12.4 的目标 |
| 不变量违例 | — | 编排模式下任务管家的启动带写工具；同一个任务两方同时持有写权限；非操作者进了任务对话；任务管家碰到别的任务。应恒为 0 |

**告警**（经 `operator_dm_target` 发到操作者私聊）：不变量违例，严重，每次都发；任务管家起不来（运行时不支持之外的原因），高，每小时一次；超过 14 天没动的草稿在「需要你处理」里提一次，不告警。

## 22. 风险和需要确认的地方

| 风险或需要确认 | 应对或问题 |
|---|---|
| 任务管家用操作者本人的环境：用户设置、钩子、MCP、全局指令、自动记忆都生效，而且不设 `LOOPER_AGENT_RUN` | 已定：用 `LOOPER_TASK_MANAGER` 按第 18 节逐叶子拒绝操作者入口或只限本任务，不能把加载设置等同于放行其中所有命令。有负向测试；变量是减速带，shell 能取消它或绕开 CLI，不能当隔离 |
| `--settings` 里的钩子和用户设置里的钩子是否都执行（对外动作审查靠它） | 前置条件，任务卡 1.0 先验证，不调用模型。不过就不能用操作者本人的环境启动，这是推翻性的问题，要立刻报告 |
| 同一目录同一时间只有一方写 | 已定：只在同一个任务内部强制；两个任务共用一个原地目录时维持 W5，只提醒，任务面板上写明 |
| 设置里的 `disableAllHooks` 可能关掉审查钩子：任务管家载入设置文件，模型能在工作区写一个 | 任务卡 1.0 测四种情况（用户设置、提交的、会话中写出的、只载入用户设置时）；能关掉就报告，由维护者定缓解办法。1.0 未报告并通过之前，没有任务管家启动卡开 PR |
| 一次性配置没有操作者的登录 | 第 20 节由操作者选凭据：默认订阅 OAuth token，API key 备选；临时 HOME/CLAUDE_CONFIG_DIR，不复制真实配置 |
| 模型和运行时选择 | 已定：task_manager 独立角色，默认 Sonnet，创建/面板按任务换 Sonnet/Opus，停进程续接；不是 Claude Code 时 task_manager_runtime_unsupported，不继承所属管家的 Profile，不静默替换 |
| 编排模式下，要改计划的话经任务管家交给规划者，多一跳 | 原话由代码原样附上；进展问题任务管家自己读状态回答；只有改计划才多走这一步 |
| 管家交互机制在任务管家里另写了一份消息循环 | 只写单会话、单入口的那一小部分，底层部件复用；1b 合入后可以评估合并 |
| 续接落到新会话 | `session_fresh` 事件；完成的 worktree 任务在分支还在时建回 |
| 「完成」不再是终点 | 7.3 的表逐处核对 |
| 界面改动面大 | 新页面并存，旧页面只抽出共用的部分 |

## 23. 已定的问题

各项默认值以后都可以改。

### 23.1 任务里和操作者说话的是任务管家

**决定：** 一个只属于这个任务的 Claude Code 会话，直接模式下自己动手，编排模式下指挥规划者并汇报；规划者不变。**理由：** 第 3.1 节。**以后可以改。**

### 23.2 任务管家做成独立的一层

**决定：** 建在 `ResidentClaude` 上，会话存在 `agent_sessions`，不走管家的会话路由；独立 `task_manager` 角色配置，默认 Sonnet，创建/面板可以按任务选 Sonnet / Opus（3.3）。**理由：** 任务身份、工作目录和模型选择都属于任务，不属于发起它的管家；共用文件按第 19 节协调。

### 23.3 编排模式下的三条规则

**决定：** 向规划者补充时，操作者原话由代码按消息 id 原样附上，模型改写字段丢弃，只能另加一条独立说明；规划者决定作为任务对话事件。交接中的模型摘要标明来源，原话按 6.1 的固定快照/id 倒序/遇到放不下即停的规则由代码摘录，最新消息不丢弃。编排模式下任务管家隔离只读续同一会话，进展问题它自己答，不问规划者。**理由：** 保住原话出处和最新纠正。**以后可以改。**

### 23.4 `LOOPER_TASK_MANAGER` 和 W5

**决定：** 任务管家里不设 `LOOPER_AGENT_RUN`，改设 `LOOPER_TASK_MANAGER`，第 18 节逐处给出 DENY / OWN_TASK_ONLY；变量只是减速带。每任务独立排他租约，两个任务共用原地目录维持 W5，只提醒。**理由：** 本任务的后台和直接会话需要互斥，跨任务目录不强制互斥。**以后可以改。**

### 23.5 其他

- 需求只在交给后台时确认一次，说「直接开始」跳过（6.1）。
- 新界面先并存，以后替换（第 12 节）。
- 任务管家上限 6 个、闲置 30 分钟回收；满了先回收空闲的，都在忙时排队并显示（第 9 节）。
- 结束后接着说就是直接模式，再交给后台就是又一次运行（7.2）。
- 又一次运行的结束只通知发起的地方，不通知原来的来源聊天。
- 完成的 worktree 任务只在分支还在时建回，否则只读打开（第 10 节）。
- 上限按任务累计，到了上限再交给后台要给新上限，花费读不到时拒绝（第 13 节）。
- 不用任务 id 也能找到任务，解析是确定性的代码（14.3）。
- 把 CLI 会话分叉给规划者不做，列为以后的探索。
- 飞书话题待定（第 16 节）。

## 24. 默认行为与用户入口

**默认行为：** 两个开关都默认关。关着时：没有草稿、没有任务管家、没有 `#work` 页，Chat 页、管家的工具描述、建任务和开始的路径、渠道路由都和今天逐字节相同（任务卡 1.1 的冻结测试）。新的只读操作（`tasks.work_list`、`tasks.select`）只是多出来，不改任何现有路径。

**用户入口：**

- **控制台：** 第 1、2 步起 `#work` 页；第 3 步起交给后台和自动转回；第 5 步起中途接手、评审、撤回；第 6 步起在主管家对话里转成任务。旧 Chat 页不变。
- **命令行：** 第 4 步起 `looper task enter / ask / brief`，接同一个任务管家会话。`loopercli` 的命令不变；新的 v1 操作它也会列出来，只有操作者能调用。
- **飞书：** 第 6 步起回复任务通知就是对那个任务说话，主管家建的任务是草稿；不在飞书里切换多个任务。
- **Telegram：** 主管家的路径同飞书；回复通知落进任务对话在第 6 步只做飞书。
