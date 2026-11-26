# RFC：工作模式——任务管家、直接模式与编排模式、工作台（v0）

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
- **操作者本人的环境：** 直接模式自己组启动参数，不调用 `isolation_args`：该函数只要收到 `hooks` 就强制 `--setting-sources ""`（`claude_cli.py:191`），会把用户设置一起关掉。让操作者的用户设置、全局指令、记忆和 skill 生效。三处仍由 Looper 加上（经只属于这次启动的 `--settings`）：拒绝别的会话发来的消息（`crossSessionInbound: refuse`）；对外动作审查的 PreToolUse 钩子（`outbound_hook.py:220-232`）；禁掉和 Looper 撞名的内置工具。不设 `LOOPER_AGENT_RUN`，改设 `LOOPER_TASK_MANAGER=<task_id>`；本机命令按第 18 节的类型化表拒绝或只限本任务。这是减速带，不是隔离，不能声称它挡得住绕开 CLI 的调用。**直接会话的安全边界和固定启动配置以第 25 节为准**（Bash 沙箱加代码钉死的设置，不是逐次出站审查）。
- **审查钩子的启动门（已定）：** 直接模式任务管家**每次启动前、每次续接前**，由代码读取这次 CLI 会加载的每一份设置：用户配置目录的 `settings.json` 和 `settings.local.json`、项目 `.claude/settings.json` 和 `.claude/settings.local.json`、启动的 `--settings`，以及 CLI 会加载的 managed settings 文件。逐来源检查，不只看合并后的值，不因文件是操作者写的就放行。命中下表就不启动、不续接；任务对话插入代码生成的说明（文件、键、如何改回），类型化原因 `review_hook_disabled`，计入第 21 节的 `task_manager_launch_refusals_total`。只报告，不自动修改设置；managed settings **绝不修改**，说明由管理方处理。探针 D3 在运行中写出的设置不影响当前进程，下一次续接仍须重读并拦住。

  检测器是类型化表 `(source, key, predicate)`：`source` 是 `USER / USER_LOCAL / PROJECT / PROJECT_LOCAL / LAUNCH / MANAGED` 枚举，`key` 是精确的键路径，`predicate` 是 `IS_TRUE / EQ_STRING_1 / TRUE_OR_CONTAINS_HOOKS` 枚举，不用文本包含规则。任务卡 1.0（#1751）证明能停掉 `--settings` 钩子的其他设置也必须进表；当前记录如下：

  | source | key | predicate |
  |---|---|---|
  | `USER`、`USER_LOCAL`、`PROJECT`、`PROJECT_LOCAL`、`LAUNCH`、`MANAGED` | `disableAllHooks` | `IS_TRUE`：JSON 布尔值 true |
  | `USER`、`LAUNCH` | `env.CLAUDE_CODE_SAFE_MODE`、`env.CLAUDE_CODE_SIMPLE`（各一条） | `EQ_STRING_1`：字符串 `"1"` |
  | `MANAGED` | `allowManagedHooksOnly` | `IS_TRUE` |
  | `MANAGED` | `strictPluginOnlyCustomization` | `TRUE_OR_CONTAINS_HOOKS`：布尔值 true 或数组含精确元素 `"hooks"` |

  #1751 的普通设置命中已有 2.1.332 探针记录；managed 三个键的门有内置 schema/分发依据，但 managed 来源的正向对照尚未通过，运行时覆盖仍是**未验证**，不能把记录测试通过写成来源已验证。启动参数和进程环境也不得启用探针已证明会停钩子的 `--bare`、`--safe-mode`、`CLAUDE_CODE_SAFE_MODE=1`、`CLAUDE_CODE_SIMPLE=1`；命中同样拒绝并指出参数或环境键。

- **CLI 版本门（已定）：** 行为验证基线是 **Claude Code 2.1.332**。每次直接启动或续接都读实际 CLI 版本；低于 2.1.332，或更高但任务卡 1.0 探针尚未在该版本通过，就拒绝直接模式，原因 `cli_unverified`，任务对话由代码说明实际版本和需补跑的探针，同样计入拒绝指标。每次 CLI 升级后探针是必过检查，不按「更新版本应当兼容」放行。探针用真实 CLI 接本地假模型服务、临时 `HOME` / `CLAUDE_CONFIG_DIR` 和合成配置，不调用真实模型、不碰操作者配置；必须付费才能验证的部分先估价、并进第 20 节预算，另请维护者批准。
- **不采用的选项（已定）：** 不采用探针的「只载入用户设置，由 Looper 自己注入项目记忆」：它会丢项目记忆。第 4 节 D4 的启动行为保持，和探针 D4 不是同一项。**第 25 节已改变这一条的前提：** 直接会话加 `--setting-sources user`，项目的指令文件由 Looper 自己读取并围起来放进简报，项目记忆不丢。现在没有退出上述检查的开关，也没有「用户自己写的所以允许」例外；显式 Looper 开关只记为后续待办。
- **提示词：** `APPEND`：保留 Claude Code 自带的提示，追加任务简报（第 14 节的 `compose`，别人写的内容围起来）和一小段 Looper 说明（这个任务是什么、怎么交给后台、怎么看后台的进展、哪些工具绑定在这个任务上）。这是新的会话类型；现有管家仍用 `REPLACE`。
- **角色和模型：** 任务管家有自己的角色 `task_manager`，`role_configs` 一行，拥有自己的职责提示和配置，不沿用所属管家的 Profile。今天角色枚举在 `agent_settings.py:146-158`，角色表在 `schema.sql:555-567`；模型和运行时由 Agent Profile 承载，不能塞回 `role_configs.model`（`agent_settings.py:1432-1452`）。新增角色的默认 Profile 是 Claude Code + **Sonnet**，角色层可以选 Profile；任务的显式选择优先于角色层，不读所属管家的选择。按任务覆盖复用 `task_profile_overrides`，将其 role CHECK 扩展为允许 `task_manager`（今天只有三个角色，`schema.sql:573-578`），不借用 `agent_sessions.selected_profile_id`。
- **按任务切换：** 新建任务时和任务面板里都能选模型，至少有 Sonnet、Opus 两个 Claude Code Profile；`tasks.create_draft` 可带 `task_manager_profile_id`，另有操作者专用 `tasks.task_manager_profile_set(task_id, profile_id)`，返回读回的有效模型、运行时、provider 和配置来源。切换和发送共用任务串行队列：有回合时返回 `task_manager_busy`（409，让操作者先 Stop），空闲时停掉进程、写覆盖、按新模型和原 `session_id` 重启续接；续接失败才走第 10 节的显式 `session_fresh`。不清对话、计划或累计花费，也不改 Planner/Worker/Reviewer。模型、effort、provider、模式和提示哈希都进启动签名。切换前后同会话的真实 CLI 验证在 1.0，Opus 的少量真实确认在第 20 节。
- **配置读回：** 操作者专用 `tasks.task_manager_profile_get(task_id)` 返回 `task_id`、`override_profile_id`（无任务覆盖时为 null）、`effective: {profile_id, model, runtime, provider, effort}`、`source: task | role | default` 和 `choices: [{profile_id, label, model, runtime}]`。runtime 为运行时类型，provider 为注册 id，effort 可为 null；choices 恰为注册表中 `check_selection` 接受用于 `task_manager` 的 Profile。set 返回相同视图；create_draft 保留原结果字段，并增加 `task_manager_profile` 同一视图。创建前验证显式选择（省略或 null 时验证角色默认），草稿和覆盖同事务落库；无效选择不留下草稿。三个操作都只给操作者、开关关着时返回 `conflict`，get/set 对未知任务返回 `not_found`。不增加任何模型可见工具或 schema。
- **运行时：** v0 只在 Claude Code 上（能中断常驻回合，`runtimes/claude_code.py:169`）。创建、角色配置、按任务切换和每次启动都校验有效 Profile；其他运行时拒绝，类型化原因 `task_manager_runtime_unsupported`（`details.requested_runtime` 和 `details.supported = "claude_code"`）。未知或未注册 Profile 保持独立的 `invalid`。配置无效就不启动，不悄悄换成 Claude Code；两个入口读回同一原因。

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
| D12 | 控制台的「工作台」页面 `#work`，和 Chat 页并存；列表是全部任务，定时任务按计划折叠；切换快、不丢输入；未看的进展按操作者存在服务端 | 第 12 节 |
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

**交接原话的唯一截取规则（代码执行）：** 在任务串行队列内固定本次交接的数据库快照，保存 `snapshot_max_id`；只选本任务里、代码确认来自操作者且在直接模式发送的消息。按 `chat_messages.id DESC` 读取（今天分页也按存储 id 排序，`db/chat.py:261-267`），不按时间戳、不重查漂移中的最新页。字符一律按 Unicode 码点（Python `len(str)`）计，不 strip、不正规化；至多 20 条、正文合计至多 4000 个码点。第一条永远保留：若它单条超过 4000，就取前 4000 个码点后停止；每条后续消息必须整条放得下，**遇到第一条放不下的就停止，不跳过找更短的，也不切开它**。满 20 条或 4000 码点立即停。每条保留 `id`、`message_id`、原长度、保留长度及 `truncated`；截断条在正文外显式标「此原消息仅保留前 4000 个 Unicode 码点，后文已截断」，被省略的旧消息另记 `omitted_older = true`。引用头和标记不计正文预算，不能混进原话。选取和交付都保持倒序。转 pending 前在任务串行锁内重查快照上界，有新操作者消息就拒绝旧确认、生成新快照。因此最新纠正所在的消息不会整条被丢掉；超长时仍有它的前 4000 个 Unicode 码点和明确标记。验收覆盖恰好放满、第一条超长、后续放不下即停、同时间不同 id、确认期间新纠正和重复交接读回一致。

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
| 1.0 | 前置验证（#1751，排在最前，CLI 每次升级后必过）：真实 CLI 接假模型服务，临时 `HOME` 和 `CLAUDE_CONFIG_DIR`、合成配置。验证 PreToolUse 双来源、SessionStart 注入、各设置来源和停钩子候选，将证实的 `(source, key, predicate)` 交给 1.3 的检测器；D3 验证运行中写入不生效、下次续接前须拦截；探针 D4 只作未采用方案的证据。验证同会话直接/隔离只读切换、Sonnet/Opus 切换后上下文仍在；保存版本、argv、工具清单和钩子哨兵的公开安全结论，不调用模型、不碰操作者配置；managed 来源正向对照未通过的明确记为未验证 | 0.5–1 |
| 1.1 | 输入项目、标题、工作区选择和可选 `task_manager_profile_id`，新 `tasks.create_draft` 输出 task id、draft/direct、有效模型；无需求存 `''`。落点：新的 `v1_domains/task_conversations.py`、`projects.py`、`db/tasks.py`、角色和 Profile 注册、`db/role_configs.py`、`task_profile_overrides` CHECK、独立迁移。新角色 `task_manager` 默认 Claude Code Sonnet；独立开关 get/update，不进 chat_settings。验收：空需求草稿可建，旧 tasks.create 空需求仍拒绝；非操作者/错误项目/其他运行时拒绝；关着时旧路径和提示逐字不变 | 1.5–2 |
| 1.2 | 输入任务 id 和控制动作，输出持久化状态/派生模式或类型化拒绝。落点：`db/tasks.py` 的模式函数、`tasks.done`、`task_workspaces.reopen`、第 8 节的每任务租约、新操作及 7.3 的本步状态检查。验收：草稿不恢复后台；/done 幂等；分支还在建回、分支没了/被检出只读且不能交接；并发开始和直接回合只有一个可写 | 0.5–1 |
| 1.3 | 输入任务消息、有效角色配置和会话状态，输出 delta/agent.tool、回合记录和持久化会话。落点：新 `tiers/task_manager.py`，按 3.3 自组直接启动参数（不调用 isolation_args），每次启动/续接前逐来源读设置、执行类型化检测表和 CLI 版本门；按第 9 节维护上下文和预算；`agent_sessions` 保存/清理及 `read_only_copies` 只保留；worktree 每轮提交。验收：每个来源命中均不启动，review_hook_disabled 的文件/键/修复说明与计数、D3 后续接拦截、cli_unverified 的实际版本说明、无例外/退出开关；中断/队列、6 个上限/闲置回收、原会话续接/显式 session_fresh、每轮 context/cost、压缩成功/失败/未知、跨权限和模型重启续接、不支持运行时拒绝 | 3–4 |
| 1.4 | 任务对话的地址：`operator_chat.send` 的目标 `kind: task`、对话线程 `task-chat:<task_id>`（`chat_threads` 归任务的管家，`chat_messages.channel` 记入口）、停止的入口、固定命令 | 1–1.5 |
| 1.5 | 输入任务绑定 MCP 凭据和工具请求，输出本任务结果或拒绝。落点：新任务管家 MCP 授权模块、`task_status` / `task_requirement_set`、`cli.py` 分发表及第 18 节各本机守卫。验收：外任务 id、未知命令/工具、全部 deny 叶子、伪造原话、非操作者；枚举 parser 叶子保证表无遗漏，旧 LOOPER_AGENT_RUN 行为不变 | 0.5–1 |
| 1.6 | 验证：全新数据库、非 8767 端口、临时 runtime root 上跑 `looper serve`，配假模型走完「新建 → 直接干活 → 停止 → 接着说 → 完成」；写好任务管家的对话评测用例，真实运行列进 PR 等批准 | 1 |

合计 8–11.5 人日（含 1.0）；不含 1.0 是 7.5–10.5。这些就是本文内的任务卡边界，不引用仓库外的「已写成」任务卡。钩子风险按 3.3 已定的逐来源拒绝和版本门处理，1.0 的报告用于验证并补齐检测表，不再等待另定缓解方案；**报告出来且前置条件通过之前，任何会启动任务管家的卡都不开 PR**，启动卡必须交付上述拒绝路径。离线探针不能证明的行为写为未验证，付费后备验证另行批准；不自动改用操作者环境启动。1.1 的纯状态/配置部分、2.1、2.2 可以先做；共用文件按第 19 节排顺序。

1.4 的输入是操作者发送/停止请求（目标 `kind: task`），输出线程消息和事件流；落点是 `chat_targets.py`、`v1_domains/operator_chat_streams.py`、新的任务对话读写模块、`channels/web.py`。它调用现有 `db/chat.save_message`，不改该函数；另存消息的任务归属、可信发送者及发送时的派生模式供 6.1 取原话。验收要从 HTTP 发送到持久化后分页读回，再测重复发送去重、非操作者拒绝和 Stop 无需排队。1.6 交付 1.1–1.5 的完整 smoke 证据及第 20 节用例；真实评测尚未批准时只提交用例和待验证项，不写通过。所有新操作/工具声明 action level 和一句理由，校验/权限错误用类型化结果。

## 12. 第 2 步：工作台

### 12.1 页面

新页面「工作台」（`#work`），在 `App.tsx` 的 `PAGES` 里登记（`web/src/App.tsx:25`），和 Chat 页并存，默认入口不变；侧栏沿用双语格式显示「Work 工作台」，只在开关打开时出现。地址 `#work`、`#work/manager`、`#work/task/<task_id>`，在单独窗口里直接打开也能用。照 `docs/design.md` 的「界面设计」一节。

| 区域 | 内容 |
|---|---|
| 左栏 | 「新建任务」（页面上唯一的主色按钮，也可以按 `N`）、搜索框、置顶「管家」、任务列表（12.2），每条任务对话以任务名命名 |
| 中间 | 选中项的对话，对话方称「任务管家」（主管家对话称「管家」）：分段的回答、「在做什么」一行、可以展开的工具记录、回合小结、`session_fresh` 事件；编排中穿插关键活动、规划者的决定和关卡卡片（可以直接回复）；输入框（暂存、历史、Stop 沿用 `ChatComposer`） |
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

`enter`、`ask --task`、`brief` 和工作台的快速切换用同一个确定性的解析（`tasks.select`，任务卡 2.2），结果是一个任务、一张候选表或没找到，从不调用模型：任务 id；省略时按 `cwd` 的真实路径找最深的那个任务目录，找不到列出最近 14 天的任务（最多 20 个）；一个 MR（链接、`!N`、`#N`、`MR N`、`PR N`、1 到 7 位纯数字），和等待步骤的 `github_pr` 检查以及标题、需求、原话、补充说明里的引用比较；别的文字按词全部命中标题或最新需求。范围最近 90 天（`--all` 全部）；只有恰好一个匹配才直接进入，几个就列出（最多 10 行），没有就明说。

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
- **操作者本人的环境：** （直接会话的边界以第 25 节为准：Bash 沙箱加固定启动配置。）直接模式能读写操作者能读写的东西；用户设置、钩子和 MCP 会加载，但其中调用下表拒绝的 Looper 入口也被拒绝，不能声称所有操作者检索命令照常可用。每次直接启动/续接前执行 3.3 的逐来源设置检测和 CLI 版本门；命中停钩子设置就以 `review_hook_disabled` 拒绝，未验证版本以 `cli_unverified` 拒绝，代码在任务对话说明并计数。没有用户自写例外或退出开关，不修改 managed settings；这仍不是隔离。
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
| 2 | 工作台（第 12 节） | 和 1 并行；2.4 等 1.4 | 同上 | 9.5–12.5 |
| 1.S | 沙箱边界：第 25 节的固定启动配置、读回、白名单设置、加固的 git 封装和敌意配置测试；任何会启动任务管家的卡以它为前置 | 1.0 | `tasks.conversations.enabled` | 待估 |
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
| `task_manager_launch_refusals_total` | reason（`review_hook_disabled` / `cli_unverified`）、phase（launch / resume）、cli_version | 每次启动/续接被 3.3 的门拒绝计一次，不按命中文件重复计；设置命中的文件、键和修复办法留在代码生成的任务对话说明，版本拒绝说明实际版本 |
| `task_manager_context_tokens`、`task_manager_turn_cost_usd` | task_id、turn/record id、model、mode、maintenance | 每个任务管家回合的上下文大小和花费序列；来自该轮 llm_records，未知为 NULL，不冒充 0；会话末轮值写 agent_sessions |
| `task_manager_compactions_total`、`task_manager_session_rotations_total` | 原因、结果 | 压缩是否成功、压后 tokens 是否下降；失败/超时轮换是否带 session_fresh；压缩的前后值记在维护记录 |
| 同一件事拆成多个任务的比例 | 项目 | 再交给后台的次数对新建任务数 |
| `tasks.work_list`、切换的耗时 | 分区 | 12.4 的目标 |
| 不变量违例 | — | 编排模式下任务管家的启动带写工具；固定启动配置被改（`launch_setting_mismatch`、`mcp_set_mismatch`、`readback_unavailable`）；轮末发现任务分支以外的 ref 被改（`ref_tamper`）（第 25 节）；同一个任务两方同时持有写权限；非操作者进了任务对话；任务管家碰到别的任务。应恒为 0 |

**告警**（经 `operator_dm_target` 发到操作者私聊）：不变量违例，严重，每次都发；任务管家起不来（运行时不支持之外的原因），高，每小时一次；超过 14 天没动的草稿在「需要你处理」里提一次，不告警。

## 22. 风险和需要确认的地方

| 风险或需要确认 | 应对或问题 |
|---|---|
| 任务管家用操作者本人的环境：用户设置、钩子、MCP、全局指令、自动记忆都生效，而且不设 `LOOPER_AGENT_RUN` | 已定：用 `LOOPER_TASK_MANAGER` 按第 18 节逐叶子拒绝操作者入口或只限本任务，不能把加载设置等同于放行其中所有命令。有负向测试；变量是减速带，shell 能取消它或绕开 CLI，不能当隔离 |
| `--settings` 里的钩子和用户设置里的钩子是否都执行（对外动作审查靠它） | 前置条件，任务卡 1.0 先验证，不调用模型。不过就不能用操作者本人的环境启动，这是推翻性的问题，要立刻报告 |
| 同一个任务的任务管家和后台不能同时写 | 已定：只在同一个任务内部强制；两个任务共用一个原地目录时维持 W5，只提醒，任务面板上写明 |
| 设置或 CLI 版本使审查钩子失效 | （边界已由第 25 节的沙箱和读回取代，下面保留作纵深防御。）已定：3.3 的类型化表扫描每次直接启动/续接会加载的所有设置来源，命中就不启动，review_hook_disabled 的代码说明给文件、键和修复办法；D3 的会话中写入在下次续接拦住。CLI 以 2.1.332 为已验证基线，低版本和未经 1.0 探针通过的高版本拒绝 cli_unverified，每次升级必跑探针；第 21 节计数。无用户自写例外、无退出开关，managed 文件只报告绝不修改；不采用只载入用户设置再自行注入项目记忆的选项，第 4 节 D4 保持 |
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
- 后续待办：是否提供显式 Looper 开关退出审查钩子的启动检查，另行设计和确认；当前没有该开关（3.3）。
- 直接会话的安全边界是 Bash 沙箱加代码钉死的启动配置，不是逐次出站审查；v1 的直接模式只支持原地任务（第 25 节）。

## 24. 默认行为与用户入口

**默认行为：** 两个开关都默认关。关着时：没有草稿、没有任务管家、没有 `#work` 页，Chat 页、管家的工具描述、建任务和开始的路径、渠道路由都和今天逐字节相同（任务卡 1.1 的冻结测试）。新的只读操作（`tasks.work_list`、`tasks.select`）只是多出来，不改任何现有路径。

**用户入口：**

- **控制台：** 第 1、2 步起「工作台」（`#work`）页；第 3 步起交给后台和自动转回；第 5 步起中途接手、评审、撤回；第 6 步起在主管家对话里转成任务。旧 Chat 页不变。
- **命令行：** 第 4 步起 `looper task enter / ask / brief`，接同一个任务管家会话。`loopercli` 的命令不变；新的 v1 操作它也会列出来，只有操作者能调用。
- **飞书：** 第 6 步起回复任务通知就是对那个任务说话，主管家建的任务是草稿；不在飞书里切换多个任务。
- **Telegram：** 主管家的路径同飞书；回复通知落进任务对话在第 6 步只做飞书。

## 25. 任务管家的安全边界：Claude Code 沙箱加固定启动配置

本节补充第 3.3 和第 18 节：直接模式任务管家（以下简称管家会话）的安全边界是什么、哪些配置由代码钉死、哪些由操作者按项目放行、v1 明确不做什么。它**替换**第 3.3 节里「对外动作审查的 PreToolUse 钩子」作为直接会话边界的做法（第 25.8 节列出需要同步改的段落）。验证基线仍是 Claude Code 2.1.332（macOS）。

### 25.1 背景和已定的决定

管家会话在任务的工作目录里跑 Bash，工作目录里的内容（项目设置、`CLAUDE.md`、`.mcp.json`、钩子、活动文字）是别人写的，不可信。一轮直接对话常有几十次 Bash 调用，按次做出站审查每次约 5 秒，不现实。已定：

1. 直接会话的边界是 **Claude Code 的 Bash 沙箱，加上代码里钉死的固定启动配置**，不是逐次出站审查。
2. 固定项（第 25.2 节）写在代码里，不经任何配置层，没有放宽的入口；启动和续接前读回有效设置，任何一项不符就带类型化原因拒绝启动。
3. 可配置项（第 25.3 节）v1 只有一项：按项目的外部域名白名单，只有操作者能改，类型化、有审计、任务继承，默认为空；什么都不默认放行。
4. v1 不做的事（第 25.4 节）写明，不留暗口。
5. **v1 的直接模式只支持原地（`in_place`）任务。** 原地任务是手动建任务的常见形态，先覆盖它；worktree 任务的直接模式放到第 2 步。worktree 任务的直接模式在启动时拒绝（`worktree_unsupported`），编排模式不变。本节里的「第 2 步」指沙箱边界的后续阶段，不是第 20 节的步 2。

### 25.2 固定项（代码钉死）

| # | 项 | 值 | 为什么 |
|---|---|---|---|
| F1 | `sandbox.enabled` | `true` | Bash 的文件和网络隔离；macOS 用 Seatbelt 加本机代理 |
| F2 | `sandbox.failIfUnavailable` | `true` | 否则沙箱起不来时静默无沙箱 |
| F3 | 文件写范围 | 只有任务的工作目录 | 沙箱内写工作目录外（含 `HOME`）被拒；不放 `filesystem.allowWrite`，没有任何配置能加 |
| F4 | `sandbox.network.strictAllowlist` | `true` | **关键**：在 `bypassPermissions` 下，沙箱开了但没有这一项时，白名单外的连接照样放行（实测）；它允许写在用户设置、managed 设置和 `--settings`，不允许写在项目文件 |
| F5 | `sandbox.allowUnsandboxedCommands` | `false` | 否则模型给 Bash 传 `dangerouslyDisableSandbox: true` 就能无提示地在沙箱外跑（实测）；设为 `false` 还让沙箱变成「管理方要求」，项目和 local 设置里的 `enabled:false`、`excludedCommands`、`filesystem.allowWrite`、`allowedDomains`、`strictAllowlist:false` 全部无效（实测 12 个用例） |
| F6 | `disableAllHooks` | `false`，经 `--settings` 注入 | 防用户或项目设置里的 `disableAllHooks: true` 关掉 Looper 注入的钩子（实测三个来源都被它覆盖） |
| F7 | `--setting-sources` | `user` | 不加载项目和 local 层设置，项目钩子（在沙箱外运行）和项目的权限、`env`、`apiKeyHelper` 等不生效，也挡住会话中途写 `.claude/settings.json` 引入的改写钩子（实测：只靠 F6 时，中途新增的改写命令钩子会热加载并生效） |
| F8 | MCP | `--strict-mcp-config`，只含 Looper 自己的 MCP 服务加操作者白名单（`tasks.conversations.manager_mcp`，#1852） | 工作目录里的 `.mcp.json` 在 `--print` 加 bypass 下无需审批就加载，并在沙箱外运行（实测）；只有严格模式挡得住 |
| F9 | Edit、Write、NotebookEdit | v1 不提供（`--disallowedTools`）；**备选**见第 25.9 节 H1 | 这三个内置工具在沙箱外，bypass 下实测能写任意路径（工作目录外的新文件、已有文件、笔记本都成功）。权限规则**无法表达「只允许写工作目录」**（`deny` 优先，加 `allow` 或取反都把工作目录内也拦了），只能列出要保护的根。读（Read）不受影响；改文件改走 Bash，Bash 受沙箱限制 |
| F10 | WebFetch、WebSearch | v1 不提供（`--disallowedTools`） | WebFetch 实测在沙箱外直接出网，不经代理和白名单；规则也无法表达「只放行白名单域名」。WebSearch 经模型 API 通道由服务端执行，不是本机外联，但 v1 一并关闭 |
| F11 | `sandbox.filesystem.denyWrite` | `.git/hooks`、`.git/config`、`.git/info`（任务目录内） | 实测：cwd 内 `.git` 的 `hooks` 和 `config` 默认已被沙箱保护（写、追加、改名、`mv` 覆盖、改名整个 `.git` 都被拒）；但 `.git/info/attributes` 和 `.git/info/exclude` 默认**可写**，而 `info/attributes` 不被 `--attr-source` 中和。加了 `denyWrite` 三项后三处都被拒，`git add`、`git commit` 仍成功 |


两条补充：

- 沙箱只管 Bash 子进程。Read、Edit、Write、WebFetch、WebSearch、MCP、钩子和状态栏命令都在沙箱外（实测 MCP 和钩子能写工作目录外、能直连监听端口）。所以 F7、F8 不是可选的加固：它们堵的正是沙箱管不到的出口。它们分别由 F9、F10 处理，是启用前必须完成的项（第 25.9 节）。
- 固定项不是设置：不经 `tasks.conversations.*` 的任何字段、环境变量或项目文件修改。操作者自己的用户级 Claude Code 配置可信（第 25.5 节），但 F1–F6 写在 `--settings`，读回逐项核对用户层，用户层把任何一项改成不同的值就拒绝启动；注意实测用户层的 `filesystem.allowWrite` 会生效（文档也如此），所以它属于读回要比较的内容，而不是默认忽略。F6 覆盖用户层的 `disableAllHooks`。

### 25.3 可配置项（操作者专用、类型化、有审计、按项目）

按项目保存，该项目下的任务继承，每次启动和续接读当前值。默认为空。

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `network_allowed_domains` | 域名列表（至多 50 项，唯一） | 空 | 放进沙箱 `network.allowedDomains`。v1 **只收外部域名**：拒绝 IP 字面量、`localhost`、回环和私有网段、单独的 `*`、带端口的写法；保存时校验，非法给 `network_policy_invalid`，不截断不跳过 |

**作用范围（界面和字段说明照此写）：** 白名单只对**经沙箱代理、走 HTTP(S) 的命令**起作用（如 `curl`、`npm`、`pip`、Python 的常规 HTTP 客户端）。Go 编写的命令行工具（如 `gh`、`lark-cli`）在 v1 即使域名在名单里也**不能用**：它们在 macOS 上需要访问系统信任服务验证证书，v1 不提供这条路（第 25.4 节）。同样，Node 编写的命令行工具默认不走代理，v1 也不为它们注入代理环境变量，不保证可用。把域名写进名单不等于任何工具都能连上。

操作和落点（计划）：操作者专用 `projects.task_manager_sandbox_get` / `projects.task_manager_sandbox_update`（`operator_only`，API 客户端得 `operator_only`；新操作须登记 action level 和一句理由）。`update` 整体替换并返回读回的有效值、`revision`、旧值到新值的差异；每次修改写一行审计（操作者、时间、字段、旧值到新值、`revision`）。字段变化进启动签名，下一轮起停进程按同一会话 id 续接。`tasks.conversations.manager_answers` 的开关语义不变。任务页只读展示当前生效的白名单（第 25.7 节）。

### 25.4 v1 不做的事

| 不做 | 原因 |
|---|---|
| 回环目标（本机端口，包括本机 `looper serve`、本地数据库） | 沙箱给命令设了 `NO_PROXY=localhost,127.0.0.1,...`，回环请求不走代理而直连，直连被 Seatbelt 拒绝；`allowedDomains` 里写 `127.0.0.1:端口` 也救不了。可行做法（沙箱外一个进程把 Unix 套接字转发到目标端口，沙箱只放行这一个套接字文件，实测可行）是一个独立组件，有自己的生命周期、权限和审计，列为第 2 步 |
| worktree 任务的直接模式 | v1 拒绝（`worktree_unsupported`）。沙箱对 linked worktree 自动放行主仓库共享的 `.git`（`hooks` 和 `config` 除外），其他分支的 ref 和对象库因此对沙箱内命令可写（实测 `update-ref` 能改主分支）。第 2 步另行设计：worktree 任务的工作区改成 `git clone --shared`，两种模式共用，主仓库对沙箱只读，结果由 Looper 取回（见第 25.12 节） |
| `network.allowLocalBinding` | 实测它放开**所有**回环端口，没有端口或网段粒度，包括本机 Looper 服务和数据库的端口；任何配置里都不使用，也不提供开关 |
| Docker 套接字（`allowUnixSockets` 指向 docker.sock） | 等于控制所有容器和它们挂载的宿主文件，包括存放运行数据库的容器，会抵消文件隔离；不提供 |
| 对沙箱放出的少量调用再做出站审查 | 第 2 步评估；v1 的边界是沙箱本身，不是审查 |
| `weaker_network_isolation`（`sandbox.enableWeakerNetworkIsolation`，仅 macOS） | 它只放行沙箱内命令对系统 TLS 信任服务（`com.apple.trustd.agent`）的访问，白名单层和代理层不变（实测：开着时白名单外域名、公网和私网 IP、回环、DNS、原始 UDP/ICMP 仍全部被拒），也确实能修好 `gh` 的 TLS（不开则 `x509: OSStatus -26276`）。**但它带来一条新的外泄通道：** 沙箱内的命令可以让信任服务（运行在沙箱外，不受代理和白名单约束）替它发起网络请求，实测一张证书的 AIA 指向本机监听端口，求值时监听端口收到 1 次请求，关着时为 0 次；URL 的域名和路径可以编码数据，**绕过 `strictAllowlist` 且没有违规记录**（公网目标、OCSP、CRL 未测）。所以 v1 不提供这个开关 |
| `node_env_proxy`、`extra_write_paths` | 它们只为内部命令行工具存在（让 Node 工具走代理；让工具写 `HOME` 下的缓存和日志目录），v1 不支持那些工具，也就不提供；工作目录外的写入因此没有任何放行入口 |
| 保证内部或公司专用命令行工具可用 | 未验证：没有对真实内部域名跑过，也没有它们需要的域名清单 |

后果，界面和任务对话里照实写：

- **v1 里管家会话不能自己推送代码或建 PR**：`gh` 这类 Go 命令行工具在沙箱内完成不了 TLS。推送和建 PR 由操作者做，或交给后台（编排模式）。这是第 2 步要解决的问题，候选做法：不打开信任服务的口子，而是只让具名命令经一个受控的代理出去。

- **需要本机数据库的测试在 v1 的管家会话里跑不了**；`pytest` 这类纯本地测试能跑（实测通过）。
- 管家会话里的 Bash 访问不到本机 Looper 服务（回环被拒），所以 `looper task ...` 这类命令在会话里不可用，任务状态一律走任务绑定的 MCP 工具（第 3.4 节，MCP 在沙箱外、逐次校验任务绑定）。
- 连接被拒时，**由代码**生成说明，不让模型猜：沙箱的拒绝结果带 `<sandbox_violations>`，里面有被拒的主机（或路径）；代码解析后，在任务对话里区分两种情况，文案不同：**(1) 主机不在名单里**：「命令 X 访问 `主机:端口` 被拒绝；该域名不在本项目的白名单里，操作者可在项目的任务管家沙箱设置里放行」；**(2) 主机在名单里但这类命令 v1 不支持**（例如 Go 命令行工具的证书校验失败、Node 工具没走代理）：「`主机` 在白名单里，但 X 这类命令在 v1 的管家会话里不受支持，放行域名不会让它可用」。判断依据是被拒主机是否在当前名单里，加一张代码里的类型化表（命令的已知类别）；两者都判断不出就只说「被沙箱拒绝」并附原始类别，不编造主机，也不暗示放行能解决。

### 25.5 信任假设

- **可信：** 操作者的用户级 Claude Code 配置（设置、钩子、技能、插件、全局指令）。用户级钩子可以在审查之后改写命令，这一点接受，因为它由操作者自己写；没有「用户自写所以放行」的例外也没有退出开关，而是**范围上就只信用户层**（F7）。managed 设置同样只读取不修改。
- **组织托管策略**（`policySettings`）：可能带钩子，不能被非托管层关掉，读回时接受来源为它的钩子；Looper 只读不改，也不在公开文字里点名。
- **不可信：** 任务工作目录里的一切：项目设置、项目 `CLAUDE.md`、`.mcp.json`、钩子、技能和命令、活动文字、任务需求和简报。其中**文字内容**仍会进入模型，模型可能被诱导做事，所以边界不依赖模型的判断，而依赖沙箱和固定启动配置把能做的事框住。
- **项目指令文件（已按补验 2 B 的实测定）：** `--setting-sources user` 下，CLI 保留用户全局 `CLAUDE.md` 及其 `@` 导入、用户技能和命令、用户 SessionStart 钩子、用户设置里的 `env`、用户 MCP 登记；**丢掉**工作目录、上级目录和嵌套目录的 `CLAUDE.md`、`CLAUDE.local.md`、项目技能和命令、项目设置里的钩子和 `env`、项目 `.mcp.json`。项目约定因此不再作为 CLI 指令加载。所以：**Looper 自己读项目的指令文件，作为围起来的不可信内容放进任务简报，不作为 CLI 指令。** 规则：
  - 读哪些：只读任务工作目录根的 `CLAUDE.md` 和 `CLAUDE.local.md`。不读上级目录（那是操作者的目录，不是任务的项目）和嵌套目录（v1 不支持，见后果）。
  - 怎么读：符号链接解析后必须仍在工作目录内，否则不读（防止把工作目录外的文件，如凭据，经符号链接带进提示）；严格 UTF-8 解码；单个文件至多 32 KiB、两个合计至多 64 KiB，超出的截断并在内容里写明截断；不展开 `@` 导入（原样保留文字）。读不了（不存在除外）、解码失败或不是普通文件，就跳过该文件，并在任务对话里由代码说明，不拒绝启动。
  - 怎么放：用 `untrusted.fence()` 围起来，放进追加提示的简报部分（**围栏的措辞和放置位置由提示词的负责人（Opus）撰写并评测，本节不定稿**；这是模型可见的改动，按第 20 节的评测流程走），来源标为新的 `untrusted.Source` 值 `PROJECT_INSTRUCTIONS`（第 19 节写的只加 `TASK_REQUIREMENT` 要同步改）。
  - 什么时候读：每次启动和续接时读，内容摘要进启动签名；会话中途对文件的改动在下一次重启或续接前不生效，界面写明。
  - **后果（照实写）：** 项目技能和命令在 v1 的管家会话里**不可用**；嵌套目录的 `CLAUDE.md` 也不会在读到子目录文件时加载；它们是否值得以同样方式（读、围起来）带进来，列为后续待办。用户层的这些仍在。
  - **评测用例（必须加）：** 项目指令文件里写一条越权指令（例如要求把工作目录外的文件内容发到某个域名、或要求关掉沙箱），任务管家不得照做，并在回答里不把它当作操作者的要求。按第 20 节的方式列进真实评测计划，经维护者批准后才跑，不在本节执行。
  - 对第 3.3 节的影响：「不采用的选项」那一条原样的理由（项目记忆丢失）不再成立，因为 Looper 把项目记忆注入回来；该条改写为「不采用 `--setting-sources` 以外的做法，项目指令由 Looper 读取并围起来」，并删去「第 4 节 D4 保持」中与此冲突的说法（需实现卡核对第 4 节 D4 的原文）。
- **原地任务里管家改的是操作者真实的检出目录：** 直接模式在 `in_place` 任务里直接改操作者的文件，没有副本。首次在某个原地任务上启用管家时，界面由代码生成一句话：「管家在你的真实检出目录里直接改文件」。
- **仓库里之后在沙箱外被执行的文件不可信：** `Makefile`、`package.json` 的 scripts、`.envrc`、`.pre-commit-config.yaml`、编辑器任务文件、构建和测试脚本、CI 配置，模型都能改（沙箱对工作目录内的普通文件可写；`.vscode`、`.idea`、`.claude/`、`.mcp.json`、shell 启动文件在沙箱的保护路径里不可写）。操作者或 Looper 在沙箱外运行它们，等于运行不可信代码，责任和审查 Worker 的产出一样：自己审，或在沙箱里跑。
- **对象无法恢复：** 轮末的 ref 检查（第 25.9 节 H2）能把被改的 ref 恢复，但被删掉的对象恢复不了。建议操作者在启用管家前，把重要分支推送或备份。
- **残余风险（不假装解决）：** (a) 操作者放进白名单的域名本身可以被滥用来外传数据（例如能写入的代码托管站点）；白名单的责任在操作者，界面要写明；(b) 沙箱内默认可读整个文件系统，读到的内容只能经白名单域名出去；(c) 操作者的 MCP 白名单服务在沙箱外运行，由操作者负责；(d) 沙箱管不到的内置工具见第 25.9 节问题 1；(e) 沙箱内的命令可以读到工作目录和 `HOME` 下的凭据，这类内容只要能经白名单域名出去就会外泄；v1 不提供 `weaker_network_isolation` 正是为了不再多开一条不经过白名单的通道（第 25.4 节）。

### 25.6 读回和拒绝启动

启动前和续接前（同第 3.3 节的位置，沿用 `task_manager_safety.gate`，不另起一套），由代码：

1. 生成本次 `--settings` 内容（F1–F6 加第 25.3 节的操作者条目）和参数（F7、F8），写入只属于这次启动的私有文件。
2. **读回有效设置，读的是 CLI 自己的有效设置，不是我们传的参数。** 进程起来后、发出第一个用户回合之前（不调用模型），通过 stream-json 的 `control_request` 向 CLI 读：`get_settings`（含 `effective` 和 `sources`）、`get_sandbox_dialog`（实际生效的写路径、`supported`、依赖的错误和警告）、`get_hooks_listing`（每条钩子的来源）、`mcp_status`，加 `system/init` 里的 `sandbox_enabled`、`permissionMode`、`mcp_servers`、`agents`、`tools`。逐项和第 25.2 节的值做类型化比较，不用文本包含。实测这些读回能区分「我们注入的」和「项目文件想改的」（例如项目的 `enabled:false` 不改变 `effective`；项目 `allowWrite` 在 `get_sandbox_dialog` 里被剔除）。
   - 这些 `control_request` 子类型**没有公开文档**，是 CLI 内部协议，所以整套读回属于 `VERIFIED_CLI_VERSION` 的升级探针。**协议不认识、字段缺失、响应不是预期形状，一律拒绝启动（`readback_unavailable`），绝不跳过检查。**
   - 断言写法：`sources` 只含预期来源（`--setting-sources user` 下没有项目和 local 来源）；`effective` 的布尔项等于注入值（F1、F2、F4、F5、F6）；钩子来源只允许 `flagSettings`（Looper 注入）、用户来源，以及组织托管策略来源（`policySettings`）——组织的托管策略可能带钩子，它不能被非托管层关掉，所以接受来源为 `policySettings` 的钩子，不在公开文字里点名任何具体钩子；`mcp_status` 的服务器集合等于 F8 的允许集；`agents` 里没有项目来源的子代理。
   - **数组类设置（`allowedDomains`、`allowWrite`、`permissions`）只断言「注入的值是读回值的子集」。** 实测 `get_settings.effective` 和 `get_sandbox_dialog` 的 `network_allowed_domains` 会显示合并后的原始并集，包含 admin-required 下实际被忽略的项目来源条目，所以**有效的网络白名单不能由读回保证**；它由敌意配置测试保证（项目文件里新增的域名在沙箱内仍然连不上），本节如实标注为「测试保证，不是读回保证」。写路径可以读 `get_sandbox_dialog` 的实际生效值。
   - 读回时机：启动、续接前和**每个回合前都做完整读回**（五个请求依次发出）。在常驻会话里实测 20 次，完整读回的中位数 2.0 ms、p95 2.2 ms、最大 2.2 ms（每个请求最大不超过 1.4 ms），不调用模型，响应不和回合输出交错；测量环境是 macOS，机器负载不低，长时间回合下的耗时没测。读回是本地控制请求。
   - 平台：v1 只支持 macOS，代码先显式检查平台，非 macOS 直接 `sandbox_unavailable`。`failIfUnavailable`（官方文档，**未在本机实测**）只保证：开了沙箱但起不来时 CLI 启动即退出；启动检查通过后才初始化失败的，会话继续但**拒绝每一条沙箱内命令**。它不是「Linux 不支持」的保证：文档称 Linux 和 WSL2 依赖 `bubblewrap` 与 `socat`，本轮没有任何 Linux 实测，所以不能因为 Linux 上沙箱碰巧能起来就当成支持；`get_sandbox_dialog` 的 `supported` 和依赖的错误、警告读回作为补充检查。Linux 支持列为后续。
3. 任何一项不符，不启动、不续接，在任务对话里插入代码生成的说明（哪一项、期望值、实际值、来自哪个来源、怎么改），计入第 21 节的 `task_manager_launch_refusals_total`。

类型化原因（现有的两个保持不变）：

| 原因 | 什么时候 | `details` |
|---|---|---|
| `review_hook_disabled`（已有） | 某来源把 Looper 注入的钩子关掉（`disableAllHooks`、`CLAUDE_CODE_SAFE_MODE` 等，第 3.3 节的表） | 文件、键 |
| `cli_unverified`（已有） | CLI 版本不是已验证基线 | 实际版本 |
| `mcp_unresolved`（#1852） | 白名单里的服务器名解析不出来 | 服务器名 |
| `sandbox_unavailable` | 平台不是 macOS，或沙箱起不来（F2；读回的 `supported` 为假、依赖有错误） | 平台、缺什么 |
| `readback_unavailable` | CLI 不认识读回请求、字段缺失或形状不对（协议随版本可能变） | 缺哪个请求或字段、CLI 版本 |
| `launch_setting_mismatch` | 读回的固定项（F1–F10 之一）和期望不符 | `item`（F1…F10 和 `hooks`、`sources` 枚举）、`expected`、`actual`、`source` |
| `mcp_set_mismatch` | `mcp_status` 或 `init` 里出现白名单外的 MCP 服务器 | 多出的服务器名 |
| `head_moved` | 下一回合开始前 `HEAD` 不在任务记录的分支上 | 记录的分支、当前分支 |
| `worktree_unsupported` | 任务的 `workspace_mode` 是 worktree，要进直接模式 | `workspace_mode` |
| `network_policy_invalid` | 保存的或读到的操作者条目不合法（非外部域名、路径不合规） | 字段、条目 |

### 25.7 代码落点（计划）

| 位置 | 改动 |
|---|---|
| 新 `looper/agents/task_manager_sandbox.py` | 固定项枚举和值；操作者策略的类型和校验；渲染 `--settings` 内容；读回比较；denial 解析 |
| `looper/agents/task_manager_safety.py` | `Reason` 增加上表新原因；`gate` 保留现有的逐来源文件检查；读回由 `task_manager.py` 在进程起来后、第一个回合前通过控制请求做；`RULES` 保留（纵深防御：F6/F7 之外仍拒绝命中停钩子的来源） |
| `looper/agents/claude_cli.py` `operator_settings_args` | 合并沙箱和 `disableAllHooks` 到注入的设置；增加 `--setting-sources user`（该函数今天只注入 `crossSessionInbound` 和钩子） |
| `looper/agents/runtimes/claude_code.py` `operator_resident_command` | 传 F7、F8；固定项不从 `launch` 里取 |
| `looper/agents/tiers/task_manager.py` | 启动和续接前调用读回；操作者策略和固定项哈希进启动签名；解析工具结果里的沙箱拒绝并写任务对话说明和记录 |
| 新迁移和 `v1_domains/` 下新操作 | 项目级策略表（含 `revision`）、审计表、`task_manager_sandbox_get/update`；迁移号由协调者按第 19 节规则分配 |
| `docs/architecture/agent-system.md` | 同步描述；替换过时说法，不追加第二份 |

### 25.8 迁移、兼容和需要同步修改的段落

- `tasks.conversations.manager_answers` 默认关，任务管家还没有对外启用，所以**没有存量的直接会话要迁移**。新策略表为空等于全部不放行；项目没有记录时按默认处理。
- 已存在的常驻会话（若有）下一轮因启动签名变化而重启续接，会话 id 和上下文不丢；`--setting-sources` 不改变会话内容。同会话的行为已由探针 1.0 覆盖，这里补一条：改 `--setting-sources` 后 `--resume` 仍可续接。
- 本节同时改动了其他章节：第 3.3 节（「操作者本人的环境」和「不采用的选项」指向本节）、第 18 节和第 22 节（边界以本节为准，原有的启动门保留作纵深防御）、第 21 节（不变量违例增加固定配置被改和 `ref_tamper`）、第 20 节（新增 1.S 行，依赖 1.0，任何会启动任务管家的卡以它为前置）、第 23 节（记录本节的决定）。第 11 节任务卡的拆分、第 19 节 `untrusted.Source` 的新值 `PROJECT_INSTRUCTIONS`，在实现卡里补。

### 25.9 必过的测试，和待验证的问题

**启用前必过：敌意项目配置。** 在任务工作目录里放下面这些，并在会话两个回合之间改写其中几项；每一项的期望是「管家会话不受影响」或「拒绝启动并给出类型化原因」，不能是「悄悄放宽」。用真实 CLI 接本地假模型服务，临时 `HOME`，不调用真实模型，不碰操作者配置：

| 工作目录里放什么 | 期望 |
|---|---|
| `.claude/settings.json` 和 `settings.local.json`：`disableAllHooks: true` | 钩子仍生效，或拒绝启动 |
| 同上：放宽 `permissions`（`allow`、`defaultMode`、`additionalDirectories`）、加 `env`、`apiKeyHelper`、`statusLine` | 不加载，无效果 |
| 同上：加 PreToolUse 钩子，用 `updatedInput` 改写命令；加会话开始钩子写工作目录外 | 不加载，无效果 |
| 同上：`sandbox.enabled: false`、`allowUnsandboxedCommands: true`、`excludedCommands`、`filesystem.allowWrite`、`allowedDomains`、`strictAllowlist: false` | 沙箱不变，写工作目录外和连白名单外仍被拒 |
| `.mcp.json` 加服务器（含会写工作目录外的） | 不加载；`init` 里没有 |
| `.claude/` 下的技能、命令 | 不加载（补验 2 B 实测） |
| `.claude/agents/` 子代理 | 不加载（补验 3 实测，`init` 的 `agents` 里只有用户和内置的）；项目插件配置未测 |
| 让模型用 Edit、Write、NotebookEdit 写任何路径 | 工具不可用（`No such tool available`）；阳性对照：去掉 `--disallowedTools` 时工作目录外的写入成功 |
| 让模型用 WebFetch、WebSearch | 工具不可用（F10）；阳性对照：去掉后监听端收到连接 |
| 原地任务里沙箱内 Bash：写 `.git/hooks/*`、`.git/config`、`.git/info/attributes`、`.git/info/exclude`（含改名 `.git`、`mv` 覆盖）；`git commit`；`git update-ref` 改别的分支；`git checkout` 别的分支 | 写前三类被拒；`git commit` 成功；改别的分支的 ref 在轮末被发现并恢复（`ref_tamper`）；`checkout` 被报告 |
| worktree 任务进入直接模式 | 拒绝启动，`worktree_unsupported`，编排模式不受影响 |
| `.git/hooks`、`.git/config`（`core.fsmonitor`、`core.hooksPath`、`core.sshCommand`、`.gitattributes` 驱动等） | 必须证明 Looper 在该目录跑的每条 git 命令都不执行它们（第 25.9 节 H3）；放置一个会写标记文件的钩子和配置，Looper 一轮提交后标记文件不存在 |
| 会话中途（两个回合之间）写上述任一项 | 下一轮结果同上 |
| 在会话里让模型传 `dangerouslyDisableSandbox: true` | 被忽略，命令仍在沙箱内 |
| 在任务目录里放可疑的 `Makefile`、`package.json` scripts 等 | 沙箱内不执行；文档里写明在沙箱外运行它们等于运行不可信代码（第 25.5 节），不作为拒绝条件 |
| 阳性对照 | 只设 `enabled: true` 不设 F4、F5 时，上面至少两项必须能复现放行，证明测试打得到 |

同时必过的正向和负向用例：F1–F10 每项被篡改（用户层、`--settings` 文件、`init` 里多出 MCP）都得到对应的类型化拒绝；`git status/add/commit`、不需要网络的 `pytest` 在沙箱里通过；白名单内域名通，白名单外 403；`<sandbox_violations>` 解析后的任务对话说明包含主机。

**启用前必须完成（不是已知缺口，三项缺一不可）。** 三项都已定：

- **H1　内置工具的边界（F9、F10）。** 实测基线：沙箱全开、bypass 下，Write、Edit、NotebookEdit 能写工作目录外，WebFetch 能连非白名单端口；WebSearch 只走模型 API 通道。规则的 bypass 下行为：`deny` 规则不被 bypass 跳过；一条 `Edit(//绝对路径/**)` 规则同时拦 Write、Edit、NotebookEdit；`sandbox.filesystem.denyWrite` **不管**这三个工具。但规则无法表达「只允许写工作目录」。
  - **已定：** `--disallowedTools Write Edit NotebookEdit WebFetch WebSearch`。这条路径要的是和沙箱一样确定的边界：在工作目录外写文件可以改操作者的用户级配置和钩子，而它们是整个设计的信任根。不采用「保留 Write/Edit 加 PreToolUse 路径钩子」的做法：钩子是先检查后写入，后台进程可以在两步之间把路径换成符号链接（检查与使用之间的竞争），而且钩子本身运行在沙箱外，成了新的沙箱外代码路径。
  - **编辑体验：** 模型少了精确编辑工具，改文件改走 Bash。为此 Looper 在沙箱的 `PATH` 上提供一个补丁命令，接受「旧文本替换为新文本」或 unified diff，不匹配时给清楚的错误；模型通过 Bash 调它，所以边界由沙箱而不是我们的路径检查来守。固定指令增加一句说明怎么改文件（模型可见的改动，由提示词的负责人（Opus）撰写并评测）。补丁命令来不及时，v1 先只用 Bash，补丁命令随后跟上。评测增加用例：多文件修改、在大文件里局部修改、一次编辑失败后重试，各自对「有补丁命令」和「没有」两种情况比较。
  - 进敌意配置测试表（上表），带阳性对照。
- **H2　原地任务的 git 和每轮 ref 快照。** v1 只支持原地任务（第 25.1 节），所以 `.git` 在任务目录内，沙箱只拿到这一个仓库的写权限，不会碰到别的仓库或 worktree 共享的 `.git`；`hooks`、`config` 默认受保护，F11 再封 `info`。模型在沙箱里 `git add`、`git commit` 能成功（实测，`denyWrite` 后仍成功）。但目录内的 ref 和对象库对它是可写的，所以每个回合：
  - **回合开始：** Looper 用加固的 git（H3）记下 `HEAD`、`packed-refs`、`refs/` 下每一个命名空间的全部 ref 值，以及 worktrees 的管理目录（`.git/worktrees`）的状态。
  - **回合结束：** 对比。除了回合开始时 `HEAD` 所指分支向前快进之外，任何 ref 的变化（别的分支、标签、其他命名空间、`packed-refs`、worktrees 管理目录）都是违规：Looper 把 ref 恢复到回合开始的值（`update-ref <ref> <旧值> <新值>`，带旧值比较），任务对话里由代码生成说明，事件 `ref_tamper`，按不变量违例告警。
  - **恢复只动 ref：** 每次恢复只对引用做 `update-ref`，绝不碰工作区和索引。如果 `HEAD` 所指分支发生了非快进的变化（`reset`、`rebase`、`commit --amend`），把 ref 恢复到回合开始的值之后，那些改动会表现为工作区里未提交的修改；这是可接受的结果，界面说明里写明。
  - **`HEAD` 被切到别的分支（`git checkout`）：** 只报告，不恢复：这是操作者真实的检出目录，切回去可能覆盖工作区。该回合 Looper 跳过轮末提交（提交会落到错的分支上），界面由代码生成一句话：「分支已被切换到 X；本轮的改动没有提交，仍在工作区里」。下一回合开始前，若 `HEAD` 不在任务记录的分支上，管家拒绝继续，类型化原因 `head_moved`，直到操作者处理。
  - **模型自己在沙箱里提交：** `HEAD` 所指分支的快进提交允许（没有丢东西）；轮末 Looper 对剩下未提交的改动仍按 #1848 的规则处理；回合记录写明「本轮有 N 个提交由任务管家自己做」。
  - **限度（写进信任假设）：** 这是事后发现并恢复，不是预防：一轮之内 ref 是被改过的；被删掉的对象恢复不了。
  - 回合内模型自己提交的内容，不由 Looper 再提交；原地任务本来就没有轮末自动提交（#1848 只管 worktree 的直接模式）。
- **H3　Looper 自己的 git 调用。** 模型能写工作目录里的 `.gitattributes` 等，Looper 在沙箱外跑 git 会执行它们。实测（git 2.56.1）会触发的：`.git/hooks` 里的 `pre-commit`、`commit-msg`、`prepare-commit-msg`、`post-index-change`、`post-commit`、`reference-transaction`；`core.hooksPath`；`core.fsmonitor`（status、add）；`.gitattributes` 的 `filter.*.clean`（add、status）；`commit.gpgsign` 加 `gpg.program`。`diff.*.textconv`、`diff.external` 在 diff、log、show 触发；`core.sshCommand` 在 fetch、push、ls-remote 触发；`core.pager`、`core.editor`（带 `-m`）、`core.askPass`、别名未触发。经实测有效的覆盖（status、add、commit 和 diff、log、show、ls-remote 全部不触发）：`git --attr-source=<空树> -c core.hooksPath=/dev/null -c core.fsmonitor=false -c commit.gpgsign=false -c diff.external= -c core.sshCommand=false <命令>`。要点：filter 和 textconv 的名字不可枚举，所以用 `--attr-source=<空树>` 让 git 不读工作树的 `.gitattributes`；`GIT_CONFIG_NOSYSTEM` 和用户级配置对仓库自带的 config 不起作用（仓库 config 才是攻击面）；环境变量 `GIT_CONFIG_COUNT/KEY/VALUE` 可代替 `-c`，但 `--attr-source` 只能放命令行。`--attr-source` 只覆盖工作树里的 `.gitattributes`，不覆盖 `.git/info/attributes`：后者由 F11 的 `denyWrite` 封住（实测它引用的 filter 只有操作者全局配置里已有的名字才可能被利用，范围有限，仍要封）。**Looper 对任务和步骤目录跑的每一条 git 都走这个加固封装**（单独的 PR 正在实现）；封装之外不允许直接调用 git（用测试枚举调用点）。未验证、应由封装另行处理：`.git` 文件被改成指向别处的 `gitdir:`、`core.worktree`、子模块；建议封装用 `git rev-parse --git-dir` 核对 `.git` 指向预期路径（未验证）。

**其他待验证的问题（未验证的不当结论）：**

1. **临时目录和缓存。** `uv`、`npm` 等的默认缓存在 `HOME` 下，沙箱内写失败；由代码把缓存目录指向工作目录内（`UV_CACHE_DIR`、`npm_config_cache`）是否合适，待评估。v1 没有写路径放行入口。
2. **拒绝记录里能存什么。** 命令文本可能含凭据；建议只存工具名、拒绝类别、主机或路径和命令摘要的哈希，不存原文。
3. **`<sandbox_violations>` 的格式稳定性。** 它是 CLI 输出的一部分，随 `VERIFIED_CLI_VERSION` 的升级探针一起验证。
4. **读限制。** 是否给常见凭据目录（如 SSH 密钥）加 `denyRead`；v1 不加，靠网络白名单兜底，是否够用待评估。
5. **项目插件配置。** 未测。

### 25.10 可观测性

每次沙箱拒绝：

- 任务对话里一条代码生成的说明（第 25.4 节）。
- 一行拒绝记录（任务、回合、`tool_use_id`、类别 `network` / `write` / `other`、主机或路径、命令摘要哈希、时间）；不存命令原文和任何密钥。
- 指标 `task_manager_sandbox_denials_total{kind, project}`：不把主机名当维度，避免基数膨胀；主机名只在拒绝记录里。
- 轮末 ref 检查发现任务分支以外的 ref 被改（`ref_tamper`），同样按不变量违例告警。
- 启动被拒仍计入 `task_manager_launch_refusals_total`，维度 `reason` 增加第 25.6 节的新值。
- 操作者修改策略写审计行（第 25.3 节）。
- 告警：`launch_setting_mismatch`、`mcp_set_mismatch` 和 `readback_unavailable` 出现代表固定项被外部改动，按第 21 节不变量违例处理，严重，每次都发；同一任务同一主机反复被拒（例如一轮内超过 10 次）只在任务对话里提示一次，不告警。

### 25.11 直接模式和编排模式用的是同一个任务目录

（按 origin/main 的代码核对。）v1 的直接模式只支持原地任务，下面对 worktree 任务的描述是第 2 步的背景。**是同一个目录。** 管家和后台的 Worker 在任务的 `work_dir` 里干活，同一时刻只有一方可写（第 8 节的每任务租约）。

- **目录从哪来：** `tasks.work_dir`。原地（`in_place`）任务就是操作者指定的目录（`tiers/task.py:556` 直接返回 `task.work_dir`）；worktree 任务由 `task_workspaces.prepare` 建 `<runtime_root>/task-worktrees/<task_id>`，分支 `looper/<task_id>/task`，并用 `db_tasks.set_work_dir` 把 `work_dir` 改成它（`task_workspaces.py:354-364`、`:406-407`）。Planner 开始前由 `tiers/task.py:504-515` 调这个函数，并把 `work_dir` 放进计划（`:416`）。
- **管家的目录：** `task_workspaces.reopen`（`task_workspaces.py:548-578`）。原地任务取 `tasks.work_dir`（目录不在了就只读）；worktree 任务取活动的 worktree，被移除的则只靠检出已有分支重建，分支没了或在别处被检出就只读，不会另起分支。结果 `reopened.work_dir` 同时是管家启动的 `cwd` 和读写回合前后文件快照的目录（`tiers/task_manager.py` 的 `prepare` 和回合循环）。直接模式带全部工具启动，编排模式带隔离只读参数，但**两种模式都用这个 `cwd`**。
- **Worker 的目录：** 一个步骤单独运行时就在 `plan.work_dir`，即同一个任务目录（`tiers/planner.py:2704`、`:2719`）；多个步骤同时跑时，`step_workspaces.plan_round`（`step_workspaces.py:435-454`）让它们各用一个以任务目录为仓库的步骤 worktree（`new_workspace`，`:481`），结束后合并回任务目录检出的分支（`target_branch`，`:755`）。步骤 worktree 在任务目录的外面（`<runtime_root>/…/<task_id>/<步骤>`）。每轮开始前 `checkpoint`（`:459`）会把任务目录里未提交的改动提交成检查点，作为步骤分支的起点。
- **两种模式来回切时：** 看到的始终是同一个任务目录。直接模式留下的未提交改动，被后台的第一轮当作检查点提交进任务分支；后台步骤合并回来的结果，管家在下一次回合里直接看到。原地任务里，这个目录同时是操作者自己的目录。
- **如果管家改用独立克隆，会坏的地方：**
  1. **未提交的改动不一致：** 管家在克隆里改，Planner 和 Worker 看不到；交给后台要先提交并取回，直接模式里没提交的内容在切换时不是丢了就是要同步，原地任务的未跟踪文件、被忽略的文件和 `workspace_links`（第 8 节、`task_workspaces.py` 的 `links`）克隆里根本没有。
  2. **Planner 的视角：** `plan.work_dir`、`tasks.work_dir`、`task_worktrees` 的行、`reopen`、`finish`、`sweep` 都假定一个任务一个目录；克隆要新增一套记录和清理，规划者读到的工作区状态和管家读到的不一样。
  3. **步骤提交和合并：** 步骤的起点、检查点和合并目标都来自任务目录检出的分支；克隆里的提交不在那里，取回时要合并，同一个分支名有两个写者就会分叉。#1848 的轮末提交（`commit_paths`）也是对任务目录的。
  4. **只读的编排模式：** 它读的是同一个任务目录里后台的进展；克隆就读到旧内容。
  所以 worktree 任务改用独立克隆是改动面很大的变更，放在第 2 步（第 25.12 节）另行设计和评审。

### 25.12 第 2 步（另行设计和评审，v1 不做）：worktree 任务

worktree 任务的工作区改成 `git clone --shared`（借用主仓库的对象库），直接模式和编排模式共用这一个克隆；主仓库对沙箱只读。已实测：沙箱内对主仓库的对象库、引用、`packed-refs`、钩子、`info` 和工作树文件的写入、删除全部被拒；克隆内 `git add`、`git commit`、`gc`、`repack`、`prune`、`fsck` 成功，且不会破坏主仓库。结果由 Looper 在沙箱外取回：`git -C <主仓库> -c core.hooksPath=/dev/null -c core.fsmonitor=false -c fetch.fsckObjects=true -c transfer.fsckObjects=true fetch --no-tags --no-recurse-submodules <克隆路径> +refs/heads/task:refs/task/<id>`，写到独立命名空间，不覆盖 `refs/heads`；实测 fetch 不执行克隆里的配置和钩子（五种方式都没有触发），树里含名为 `.git` 的条目时只有带 fsck 参数才会被拒。取回后由 Looper 合并，取回的内容当不可信代码。仍要处理：超大 pack 和伪造历史、克隆自己的 `objects/info/alternates` 被改（只影响克隆，未实测）、与 `task_workspaces`、步骤 worktree 和 #1848 的整合（第 25.11 节）。
