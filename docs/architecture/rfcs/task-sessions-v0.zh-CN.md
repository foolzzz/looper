# RFC：任务对话（先建任务再谈、进入任务、续接已结束的任务，v0）

- **状态：** 提案，只有文档，不含实现。本版围绕新的首要需求重组：任务是第一位的对象，每个任务从建出来那一刻起就有自己的一段对话；临时的事仍在管家的那一条对话里。第 4 节的决定按本文建议写下；原来待定的问题已经定下，第 16 节逐条写成本版本的决定，各附理由，各项默认值以后都可以改。飞书话题的两个问题留到那一步开工前再定（16.8）。
- **相关：** [群聊里的能力范围 RFC](chat-scopes-v0.zh-CN.md) 第 7 节（会话键与指纹）和第 16 节（留下的任务维度）；[助理层 RFC](assistant-layer-v0.zh-CN.md) 第 4.2 节（常驻会话的内存）和第 5.3 节（会话路由）；[任务的工作区 RFC](task-workspaces-v0.zh-CN.md)（W1、W3、W5、W6、W7）；[规划者只读 RFC](planner-read-only-v0.zh-CN.md)；[定时任务共用一条规划者会话 RFC](schedule-planner-session-v0.zh-CN.md)；[用户关卡 RFC](user-gate-v0.zh-CN.md)；[agent room RFC](agent-room-v0.zh-CN.md)（会诊）；[管家台账 RFC](manager-completion-ledger-v0.zh-CN.md)；开放的 #1736（第 13 节第 0 步）。
- **代码基线：** `main` `e574e70dd`。初稿按 `20ed06508` 写成，其后被引用的文件里只有 `projects.py`、`cli.py`、`db/projects.py` 和 `tiers/manager.py`（第 1555 行之后，本文引用的行都在它前面）有改动；所有行号都按 `e574e70dd` 重新核对过。第 2 节的现状是读代码得出的，每项标了出处；出处路径相对 `looper/agents/`，`schema.sql`、`db/…` 在 `looper/db/` 下，`web/…`、`docs/…` 相对仓库根。本文没有启动服务，没有调用模型，文中的 SQL 也没有在数据库上执行过（由各步的迁移测试在一次性 PostgreSQL 上验证）。用到的 Claude Code 参数（`--session-id`、`--resume`、`--fork-session`、`--name`、`--settings`、`--permission-mode plan`、`--add-dir`、`--append-system-prompt`）只在 2.1.332 的 `claude --help` 里确认存在；它们的行为凡是没有现成代码证明的，都标了「需要验证」。

## 1. 问题与目标

操作者每天同时在处理好几件事：在编码 CLI 里，一件事有自己的目录和一条会话，离开几天再回来接着做，几件事各在各的会话里。Looper 这边却只有一个入口和一条对话流：

1. **只有一条对话。** 控制台里「新建对话」只是新开一个显示分组，管家仍是同一条会话，模型看得到别的分组里说过的话（第 2 节）。几件事挤在一起。
2. **要先说清楚才能建任务。** 建任务必须带需求（`v1_domains/task_create.py:59`、`projects.py:574`），管家的提示词要求先在对话里澄清、确认后再建（`tiers/manager.py:319`、`:338`）。想到一件新事时，没有「先给它一个地方、再在里面谈」的路。
3. **对话是单向的。** 让管家把话转给任务，管家把原话存成补充说明、唤醒规划者（`ops/tasks.py:547-571`），规划者的回答不会回到这段对话里。
4. **结束的任务接不上。** 补充说明只收运行中的任务（`ops/tasks.py:553-555`）；重启只对失败、停止、取消的任务（`db/tasks.py:105-108`、`projects.py:1253-1258`）；规划者对已结束的任务直接回「任务已结束……不能再发消息」（`tiers/planner.py:4535-4536`）。追问只能变成新任务，同一件事散成好几个任务。

「说的是哪个任务」不是主要问题，操作者一般知道自己在说哪件事，只是说出来的是几个字或一个 MR 号，不是任务 id。

**两种工作方式，本文都要支持：**

- **新任务：先建，再谈。** 知道这是一件新事时，操作者先建一个任务（可以是空的，或者只有标题和项目、目录），再在这个任务自己的对话里讲问题、把需求谈清楚、开始、看进度、继续补充，任务结束后还回到这里接着问、接着做。
- **临时的事：不建任务。** 直接跟管家说，就是今天管家的那一条对话。它保持一条，不拆成很多条。

**目标：**

- 任务从建出来就有自己的对话（会话键 `task:<task_id>`），在任务里说话、在任务里得到回答，不经管家转话。
- 任务多一个草稿状态：还没有需求、正在对话里谈；不开始、不占运行并发、可以一直放着、可以删。
- 控制台里，左边是管家（置顶）和任务列表，中间是选中项的对话（流式），旁边是任务的计划、步骤、关卡和文件改动；「新建任务」一点就进入一个空任务的对话。
- 同一段任务对话可以从控制台、CLI（`looper task enter`）进入，以后从飞书话题进入，看到的是同一个上下文。
- 已结束的任务在同一个任务上接着做：在它的对话里说「接着做」，背后是一个类型化的转换。
- 能从管家的对话里把一段话转成一个任务，带着这段话开始。
- 反过来，能把一个已有目录（可选连同一条 CLI 会话）登记成任务，交给后台的 agent。
- 每一步都能单独发布；改变既有行为的地方都有默认关的开关，关着时逐字节不变。

**不做：**

- 不另造一个「工作会话」对象：一件事就是一个任务。
- 不把管家的那一条对话拆成多条，也不改它的会话（main）和默认管家的群会话。
- 不用模型去猜「用户说的是哪个任务」：自由文本和 MR 号由确定性的代码解析，只有恰好一个匹配时才直接进入，几个就列出来挑，没有就明说（7.4、16.10）。
- 能力范围 RFC 的范围模型只按 10.4 扩展（加内置集合 `task_session` 和相应的约束），其余不改。
- 不把操作者的 CLI 会话记录交给 Looper 的 agent；Looper 不读会话记录（工作区 RFC 的 W7 仍然成立）。
- 不给操作者在 agent room 里设座位（`room_messages.sender_kind`，`schema.sql:976`）。
- 不做新的桌面应用，界面就在现有的控制台里。不做 OS 级隔离。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 | 出处 |
|---|---|---|
| Chat 页 | 左上列出所有 agent：默认管家、场景管家、最近 30 个任务各自的规划者、执行者、评审者；选中管家时按渠道列出它的对话，网页对话能接着聊，飞书和 Telegram 的只读 | `docs/design.md` 的「Web」一节、`chat_targets.py:60-122` |
| 网页对话线程 | 只是显示分组：新建按钮写着「a new chat thread; the Manager's session goes on」，线程 id 由前端生成，只用来给 `chat_messages` 分组。网页、API 客户端和操作者私聊都进 main 会话，共用管家的一条常驻 Claude 会话，所以模型看得到别的线程里说过的话。真正换上下文的只有 `/new`（「New session」），它重置整个管家会话 | `web/src/pages/chat.tsx:284-286`、`:1301`；`session_routing.py:46`、`:79-80`；`docs/design.md` 同节 |
| 流式 | 管家的答复按 `answer.delta` 分段推送，另有一行 `agent.phase`（在想什么、用哪个工具）；工具的参数和结果看不到。规划者的 `[CHAT]` 答复只有一条最终消息 | `channels/web.py:327-339`、`chat.tsx:1059`、`:1097-1099`；`tiers/planner.py:4538-4546` |
| 停止和插话 | Stop、Esc、`/stop` 都在 `chat.stop_enabled` 后面，默认关；规划者的聊天没有 Stop。回复期间按 Enter 只进暂存区，回复结束后合成一条发出；并入正在跑的那一轮要开 `manager.mid_turn_input.enabled`，只对默认管家，默认关 | `chat_settings.py:64`、`mid_turn_input.py:35-38`、`docs/design.md` 同节 |
| 任务的进度 | 实时的 activity 流在任务页；Chat 页选中规划者时，旁边折叠显示它最近 10 次运行 | `web/src/pages/task-detail.tsx:306`、`chat.tsx:118`、`:1414` |
| 建任务 | 控制台的 `tasks.create` 和管家的 `looper_task_create`：选已有项目或新建项目，`work_dir` 可省（用项目默认目录），可选 `workspace_mode`、`base` 和各角色的 Profile；需求必填；建出来是就绪（存的状态是 `init`），要另外开始 | `v1_domains/task_create.py:58-73`、`ops/tasks.py:725-754`、`projects.py:560-574`、`:1214` |
| 管家建任务的规矩 | 先澄清、确认后才建；只在用户说了才开始 | `tiers/manager.py:266`、`:319`、`:338` |
| 和规划者对话 | `operator_chat.send` 可以把目标设成运行中任务的规划者，只有操作者能用；消息先存成 `via = chat` 的补充说明，规划者在 `[CHAT]` 回合里作答 | `v1_domains/operator_chat_streams.py:47-60`、`chat_targets.py:34-57`、`projects.py:1317-1327`、`tiers/planner.py:4528-4570` |
| 任务状态 | 一张表把状态分组：进行中、等你、就绪、需要关注、完成、取消；`ACTIVE_STATUSES` 是前两组 | `db/tasks.py:26-59`、`:93-94` |
| 重启 | `restart`、`start_over` 只对失败、停止、取消的任务，且要有目录；`RestartMode` 有 `continue`、`start_over`、`at_cap` | `db/tasks.py:105-108`、`projects.py:1241-1281`、`message.py:96-105`、`tiers/task.py:714-790` |
| 每次运行 | 重启把 `tasks.run_no` 加一、清掉结果；每次运行结束在 `task_outcomes` 写一行 | `db/tasks.py:508-542`、`db/migrations/task_outcomes.py:7-26` |
| `continue` 这个名字 | v1 的 `tasks.continue` 是「提高因上限失败的任务的上限，接着跑」，高风险 | `v1_domains/task_configuration.py:54-57`、`:61-70` |
| 规划者会话 | `agent_sessions` 里 `planner-<task_id>` 一行，任务结束后还在；只有 `start_over` 和删除任务才删。规划者在任务的 `work_dir` 里跑，只在任务运行时存在 | `schema.sql:392-410`、`db/tasks.py:131-133`、`:520-531`、`tiers/planner.py:5398` |
| 只读启动 | 规划者的运行去掉写文件的工具，权限模式钉成 manual 且没有人答提示，所以会改东西的操作一律被拒 | `claude_cli.py:355-365`、`runtimes/claude_code.py:177-178`、`:329-331` |
| Claude Code 会话 | 规划者、执行者、评审者用操作者自己的配置目录，会话由 Claude Code 自己保存；路由出来的管家会话把 `CLAUDE_CONFIG_DIR`、`HOME` 等改到会话根下，在会话自己的根目录里跑 | `claude_cli.py:42-72`、`consult.py:9-17`、`tiers/manager.py:967-1023` |
| 管家的会话键 | `SessionKey(kind, channel_id, chat)`，`kind` 是 main / group / dm；`manager_sessions.kind` 只许 group / dm | `session_routing.py:45-88`、`schema.sql:1909-1929`、`tiers/manager_routing.py:136-161` |
| 常驻会话的内存 | 每个常驻会话空闲约 240 MB，带子进程树约 310 MB；按需恢复到首次回复 1.8–3.4 秒，常驻只省 1–2 秒 | 助理层 RFC 第 4.2 节、`session_routing.py:31` |
| 任务的目录 | worktree 任务在 `<runtime_root>/task-worktrees/<task_id>`、分支 `looper/<task_id>/task`；完成时提交剩余改动、删 worktree、留分支并记下 `result_commit` | `schema.sql:292-298`、`:501-514`、`task_workspaces.py:1-24`、`:324-440` |
| 操作者自己的 Claude Code | 可选的 SessionStart 钩子注入操作者画像（上限 3000 字），在 Looper 启动的 agent 里不输出；可选的只读 MCP 用派生的受限凭据 | `claude_code_entry.py:29-31`、`:53-78`、`operator_auth.py:333-350`、`child_env.py:60` |
| 操作者本地写的先例 | `looper ledger` 直接读写数据库，写操作要 `--session` 标签，在 agent 里拒绝，是减速带不是隔离 | `ledger_commands.py:1-8`、`:152-161` |

## 3. 候选方案与取舍

**一件事用什么对象表示：**

| | A. 任务本身，带一段任务对话（采用） | B. 另造一个「工作会话」，任务挂在它下面 | C. 把管家的对话拆成几条各自独立的上下文 |
|---|---|---|---|
| 对象 | 任务；会话键 `task:<task_id>` | 工作会话和任务两层 | 管家的多条会话 |
| 和今天的关系 | 任务已经有目录、计划、步骤、关卡、花费、台账；只缺对话和「还没有需求」这个状态 | 多一层对象，任务、会话、群会话三种概念并存 | 不碰任务；临时的事和要做的事仍混在一起 |
| 「先建再谈」 | 草稿状态 | 先建工作会话，谈清楚再建任务，又回到「先谈后建」 | 不支持 |
| 结束后接着做 | 回到这个任务的对话 | 要在两层之间找 | 不支持 |

**采用 A。** 操作者心里的「一件事」就是任务，任务已经有其他所有的东西，缺的只是从一开始就有的对话。B 让会话概念变成三种；C 解决不了「先建再谈」和「结束后接着做」。

**入口的先后：** 先做控制台里的任务对话（第 1 步），再做 CLI（第 2 步），再做重新打开（第 3 步）。原因：任务对话是地基，CLI 和重新打开都只是进入同一段对话的另一种方式；重新打开在实际使用里是最大的缺口，但它的入口就是任务对话，所以排在地基之后。

## 4. 决定一览

| # | 决定 | 落点 |
|---|---|---|
| D1 | 任务是第一位的对象。每个任务从建出来就有一段任务对话，会话键 `task:<task_id>`；管家的临时对话仍是 main 那一条，不拆 | 5.1、10.1 |
| D2 | 新状态 `draft`（草稿）：可以没有需求，在对话里谈；不开始、不在 `ACTIVE_STATUSES` 里、不占运行并发、不过期、可以删。离开草稿只有一条路：需求和目录都有了，操作者说开始 | 5.2、16.11 |
| D3 | 谁来答：任务有活着的规划者时由规划者答（`[CHAT]`），其余时候（草稿、就绪、结束后）由这个任务所属管家的任务会话答。两个角色，一段对话：操作者始终在任务里说、在任务里得到回答 | 5.3、16.1 |
| D4 | 任务会话只读：用规划者同样的只读启动，在会话自己的根目录里跑，任务有目录时用 `--add-dir` 加进来；按需启动，闲置回收，用 `--resume` 续上 | 5.4、第 12 节 |
| D5 | 交接由代码做：开始时，任务会话写下的需求（操作者确认过）和草稿对话的摘录作为补充说明交给规划者；结束后，任务会话读简报和这段对话线程的摘录 | 5.3、5.4 |
| D6 | 进度写在同一处：中间的对话按时间穿插这个任务的关键活动和关卡卡片，取自现有的 activity 流 | 5.5、第 6 节 |
| D7 | 管家对话里的一段可以转成草稿任务，选中的消息由代码复制过去作为开场，原对话不动 | 5.6 |
| D8 | 控制台：Chat 页改成左列（「新建任务」按钮、置顶的管家、按项目分组的任务）、中间（选中项的对话）、旁边（任务面板）。开关 `tasks.conversations.enabled` 关着时，Chat 页和今天逐字节相同 | 第 6 节 |
| D9 | `looper task enter [<选择>]` 在终端里进同一段任务对话；`looper task claude [<选择>]` 打开操作者自己的 Claude Code 会话，在任务目录里动手；两者共用 7.4 的解析 | 第 7 节 |
| D10 | 动手会话（`claude`）有两种方式：`observe`（plan 模式）和 `work`。`work` 要先拿到这个 checkout 的排他租约；任务的运行持有共享租约，所以进行中的任务上 `work` 被拒并说明持有者，不能强行接管 | 7.3、11.1、16.3 |
| D11 | 反向登记默认原地；只交摘要，不交会话记录；把 CLI 会话分叉给规划者不做 | 第 8 节、16.5、16.9 |
| D12 | 已结束的任务在同一个任务上重新打开：类型化转换 `reopen`（`RestartMode.REOPEN`），在任务对话里由任务会话发起、操作者确认；改 `_restart_plan` 的控制流，让计划已完成的任务也收到 `[REOPEN]`。定时任务产生的任务和绑了意图规则的任务 v0 不能重新打开 | 第 9 节 |
| D13 | 花费上限按任务累计。预检和规划者用同一个谓词，在重置之前做；到了上限必须在命令里给新上限，只写这个任务的 `tasks.budget_usd`；花费读不到时拒绝 | 9.4、16.7 |
| D14 | 每次运行的起点记在新表 `task_runs`；重新打开的运行只把进度和结束告诉发起它的地方，默认不通知原来的来源聊天 | 9.6、9.7、16.2 |
| D15 | 完成的 worktree 任务，worktree 不自动重建，以 `work` 进入或重新打开时才建回：分支 T 在就按 T，没了就按 `result_commit`，都没了就拒绝；为操作者建回的记成新状态 `operator`。新任务第一次建 worktree 的路径不变 | 9.5、16.4 |
| D16 | `SessionKind.TASK` 存在 `manager_sessions` 新加的 `task_id` 列上；渠道和聊天降为回复路由。和能力范围 RFC 组合：任务维度不进指纹，键是 `task:<task_id>~<指纹>`，以后有共享受众时加 `@<channel_id>:<chat>`；内置集合 `task_session` 由本文加，操作者 ⇔ 范围的规则在三处一起扩展 | 第 10 节 |
| D17 | 任务会话可以只读这个任务的工作目录，只对受众 operator，是对能力范围 RFC 的 D21 的明确例外；任务会话的范围直接构造成（`operator`, `task_session`），不走那篇第 6 节的解析表 | 5.4、10.4、16.6 |
| D18 | 所有新的写操作只给操作者 | 11.2 |
| D19 | 资源：任务会话不常驻，和其他路由会话共用全机常驻上限（默认 6，约 1.5–1.9 GB），闲置 30 分钟回收 | 第 12 节 |
| D20 | 执行顺序 0 → 1 → 2 → 3 → 4 → 5 → 6；第 1 步里的 1.1、1.3 和能力范围 RFC 的第 1b 步并行，1.2 在它合入之后；分工到函数（13.2） | 第 13 节 |
| D21 | 开关：`tasks.conversations.enabled`（第 1、2、4 步）、`tasks.reopen.enabled`（第 3 步）、按 bot 的 `task_threads`（第 6 步），都默认关 | 第 13 节、第 17 节 |

## 5. 任务对话

### 5.1 两种方式

| | 新任务 | 临时的事 |
|---|---|---|
| 入口 | 控制台的「新建任务」，或从管家对话转过来（5.6） | 置顶的「管家」，或飞书、Telegram 里直接跟管家说 |
| 对象 | 任务（一开始是草稿） | 没有任务 |
| 对话 | 这个任务自己的对话，会话键 `task:<task_id>` | 管家的 main 会话，一条 |
| 谁答 | 5.3 | 管家，和今天一样 |

管家在临时对话里仍可以照今天的规矩建任务（先澄清、确认后建）。两种方式的分界由操作者决定：点「新建任务」就是新任务。

### 5.2 草稿状态与状态机

**新状态 `draft`**，加进 `STATUS_GROUPS`（`db/tasks.py:39-59`），新组 `TaskGroup.DRAFT`。

| 性质 | 规则 |
|---|---|
| 需求 | 可以为空。`tasks.requirement` 仍是 `NOT NULL`，草稿存空串；新加 CHECK `status = 'draft' OR btrim(requirement) <> ''`，先 `NOT VALID` 再验证（已有的行由 `create_task_core` 保证非空，迁移时核对） |
| 项目和目录 | 可以都不给。没给项目的草稿放进内置项目 `inbox`（「未归类」，没有默认目录），所以 `tasks.project_id NOT NULL` 和所有 `JOIN projects` 的查询都不用改；有了方向后用 `move_task`（`projects.py:1202`，任何状态都能挪）换到真正的项目 |
| 运行 | 不开始，没有规划者，不进 `ACTIVE_STATUSES`，所以不算进侧栏的进行中数、项目的规划者并发和服务启动时的恢复 |
| 期限 | 没有，草稿可以一直放着 |
| 删除 | 可以，走现有的删除任务；它的任务会话行随任务删除（`manager_sessions.task_id` 的外键），对话线程的消息一起删 |
| 台账 | 草稿不产生台账条目，开始时才有（实现时核对台账建任务条目的那一处，`db/ledger.py:764`） |
| 计入 | 任务列表和 Tasks 页按 `draft` 组显示；`EDITABLE_STATUSES`、`AGENTS_EDITABLE_STATUSES`（`projects.py:1288-1291`）加上 `draft` |

**转换：**

```
（新建任务 / 从管家对话转来）            → draft
draft    ──start（需求非空、目录可解析、操作者说开始）──→ pending → 规划者 [PLAN]
draft    ──delete──→ （删除）
init     ──start──→ pending           （今天的路径，不变）
进行中 / 等你 ──结束──→ completed / failed / stopped / cancelled   （不变）
结束     ──restart / start_over──→ pending   （不变，completed 除外）
结束     ──reopen（第 9 节）──→ pending
```

- `start` 对草稿的检查：需求非空（`not_requirement`）；目录可解析，即 `work_dir` 非空或项目有默认目录（`no_directory`，所以 `inbox` 里的草稿要先给目录或换项目）；开关开着。检查放在 `start_ready_task`（`projects.py:1214`）里，草稿和 `init` 走同一个 `TASK_START`，同样要拿 11.1 的共享租约。
- 不做 `draft → init`：要「建好但先不开始」，留在草稿就行。
- 开始时，任务会话写下的需求成为需求的第 1 版（`task_requirement_versions`，`schema.sql:519-527`）；草稿对话的摘录由代码拼出（最近 40 条，操作者的话标为操作者的，别人写的和模型的话围起来），存成一条 `via = draft`、来自操作者的补充说明，在规划者的第一轮 `[PLAN]` 里送达（现有的补充说明路径）。

### 5.3 谁来答

| | A. 规划者从建任务起一直答（一个角色，分阶段） | B. 两个角色，一段对话（采用） | C. 任务会话一直答，运行中经工具问规划者 |
|---|---|---|---|
| 上下文 | 一条会话贯穿草稿、规划、运行、结束 | 两条：任务会话（草稿、就绪、结束后）和规划者（运行中）；交接由代码做（D5） | 一条对话，但规划者知道的要转一手 |
| 草稿阶段 | 要在任务开始前起规划者：今天规划者只在任务运行时存在，算项目的规划者并发，而且在 `work_dir` 里跑（`tiers/planner.py:5398`）；草稿可能还没有目录，之后换了目录，按目录保存的会话可能续不上（需要验证） | 任务会话按需起，在自己的根目录里跑（`tiers/manager.py:967-1023`），目录以后用 `--add-dir` 加，续接不受影响 | 同 B |
| 运行中 | 双向（`[CHAT]`） | 双向（`[CHAT]`，同今天） | 两跳：任务会话问、规划者答、任务会话再转述；慢一倍，可能走样 |
| 要改的东西 | 规划者契约加一个对话阶段（大量模型可见内容，规划者评测全要重跑）；规划者答复的流式、停止、渠道投递都要新做 | 管家已有流式、停止、投递、续接和回收；规划者不改 | 同 B，另加一个同步问规划者的工具 |
| 只读检查 | 规划者本来只读 | 任务会话用规划者同样的只读启动（5.4） | 同 B |

**采用 B。** 操作者看到的只有一段对话：同一个线程、同一个入口，每句话都在任务里得到回答，没有转话。运行中由最懂这个任务的规划者直接答；其余时候由做对话的那一层答，现成的流式、停止、续接都能用。代价是两条上下文，用代码做的交接补上（D5）。A 的连续性最好，但要把规划者从一个结构化的计划协议改成也能闲聊的角色，改动和评测成本都大得多；C 又回到了转述。

入口只调一个函数：

```
answerer(task) -> Planner | TaskSession | Queued | Refused(reason)
  有活着的规划者（task_router.has_planner）           → Planner：现有的 planner 目标
  进行中，但规划者还没起来（pending、assigned）         → Queued：存成补充说明，代码回一句「任务正在启动，规划者会看到这条」
  草稿、就绪、完成、失败、停止、取消，开关开着          → TaskSession(task:<task_id>)
  开关关着                                            → 今天的行为：只有活着的规划者能收消息
```

- **竞态：** 消息发给规划者时任务恰好结束，规划者今天回固定文字（`tiers/planner.py:4535-4536`）。开关开着时，改成回一个类型化的结果（`AGENT_REPLY` 的 `payload.refused = "task_ended"`），入口收到后改投任务会话一次；还不行就如实告诉用户。改投计数，每条消息最多一次。开关关着时，规划者的回复保持今天的文字，不带这个字段。
- 重新打开以后规划者又活了，下一条消息自动回到规划者；任务会话保持不动，下次结束后接着用，开场带上中间这段对话的摘录。
- 只读分叉（会诊）不当回答者，是任务会话的一个工具：问某个已结束的步骤座位。

### 5.4 任务会话：启动、输入、工具、花费

- **启动：** 任务所属管家的一条路由会话，键 `task:<task_id>`（第 10 节）。用规划者同样的只读启动（`Launch.read_only`，`claude_cli.py:355-365`）：没有写文件的工具，会改东西的操作被拒。在会话自己的根目录里跑，`CLAUDE_CONFIG_DIR` 等改到会话根下（`tiers/manager.py:967-1023`），所以按目录保存的会话永远在同一处、`--resume` 续得上；任务有目录时用 `--add-dir <目录>` 加进来，目录变了只改启动签名，停掉进程后按同一个会话 id 续接。读不限于这个目录，和操作者的 main 会话一样能读这台机器上操作者能读的东西，因为受众只有操作者（D17）。不支持只读启动的运行时拒启，带类型化原因，不退回更宽的启动（能力范围 RFC 的 D15）。
- **输入**（类型化清单，逐项冻结测试，做法同 #1708 的 `SessionAllowlist`）：固定的任务会话契约（新写，公开，英文）；7.3 的简报（同一个 `compose`，上限放到 12000）；这段任务对话线程的最近摘录；从管家对话转来时的开场摘录（5.6）；本条消息。不纳入：操作者画像、台账、全局稳定块、别的任务、别的聊天，理由同能力范围 RFC 的 D12：以后答复可能发进飞书话题。
- **契约要点**（新的模型可见内容，跑真实管家评测）：这是任务 X 的对话；按任务状态行事：草稿和就绪时，和操作者把需求谈清楚，可以只读地查目录，用 `task_requirement_set` 写下需求、用 `task_workspace_set` 提出项目和目录，只在操作者说开始时调 `task_start`；结束后，回答这个任务做了什么，操作者要接着做时用 `task_reopen`（先确认）。自己不改文件，改动由开始或重新打开之后任务的 agent 做。
- **工具**（服务端按会话绑定的 `task_id` 逐次校验，参数里的任务不是这一个就答 `not_found`）：

| 工具 | 什么时候能用 | 作用 |
|---|---|---|
| `task_detail`、`task_activity` | 任何时候 | 读 |
| `task_requirement_set` | 草稿、就绪 | 写一版需求（`edit_requirement` 的同一条路径，带版本） |
| `task_workspace_set` | 草稿 | 换项目（`move_task`）、给目录和工作区方式，回答里带 `task_workspaces.check` 的提醒 |
| `task_start` | 草稿、就绪 | 开始（5.2 的检查）；契约要求只在操作者说了之后调 |
| `task_diff` | 开始过以后 | 这个任务的改动：worktree 任务是基点到 T，原地任务是计划的 `start_commit` 到 HEAD，有行数上限 |
| `task_consult` | 结束后 | 问一个已结束的步骤座位，走现有会诊，次数和花费算在任务上；任务整体结束后会诊是否可用要验证 |
| `task_reopen` | 结束后 | 第 9 节的转换，先确认 |

- **花费：** 任务会话的回合算管家的花费，不算进任务的上限，否则谈得多了反而开不了；会诊和开始、重新打开之后的运行算任务的。

### 5.5 进度写在同一处

- 中间的对话按时间穿插这个任务的关键活动：阶段变化、步骤验收或失败、关卡打开和关闭、任务结束，取自现有的 `tasks.activity_stream`（`web/src/pages/task-detail.tsx:306` 已经在用），由前端合并，不写进任何模型的上下文。等你的关卡显示成卡片，在卡片里直接回复（复用 `web/src/components/task-gates.tsx`）。
- 这些事件不是对话消息：任务会话下一次回答时，通过简报的「进度」和「上次以来」看到它们；规划者本来就知道。
- 任务完成或失败时，对话里有一条代码写的结束卡片（结果总结、分支、怎么合并），后面接着就能说话。

### 5.6 把管家对话转成任务

- **控制台：** 管家对话里每条消息有「转成任务」：选起止两条消息，生成一个草稿任务，选中的消息由代码复制进它的对话线程，作为一段带标注的开场（「从管家对话带来」，操作者的话标为操作者的，管家的回复和别人的话围起来），然后切到这个任务。原来的管家对话不动。
- **管家：** 开关开着时，管家多一个工具 `looper_task_draft(title, from_message_ids)`，可以提议「这像一件新事，要转成任务吗」；创建要操作者确认（同今天建任务的规矩，`tiers/manager.py:338`），建好后回一个链接。这是新的模型可见内容，跑真实管家评测。
- 草稿的任务会话第一轮带上这段开场，所以接着说「就按刚才说的」时接得上。

## 6. 控制台

### 6.1 布局

改的是现有的 Chat 页（`#chat`），照 `docs/design.md` 的「界面设计」一节：令牌和 `components/ui` 组件，颜色只表示状态加一处主要强调，状态一定同时有文字或图标。地址：`#chat/manager`（管家）、`#chat/task/<task_id>`（任务对话）；#1736 的「和这个任务对话」链接改到 `#chat/task/<task_id>`。

| 区域 | 内容 |
|---|---|
| 左列 | 最上面是「新建任务」，页面上唯一的主色按钮；下面一行置顶的「管家」（未读和「等你」数）；再下面是任务，按项目分组、可折叠，组内先排等你的，再排进行中、草稿、最近 14 天结束的；每行标题（空草稿显示「未命名任务」）、状态的文字和图标、等你的数；底部链到 Tasks 页看全部 |
| 中间 | 选中项的对话：流式文字和那一行在做什么（今天的 `answer.delta` 和 `agent.phase`），任务对话里穿插 5.5 的活动和关卡卡片；输入框沿用今天的暂存、历史和 Stop（Stop 仍跟 `chat.stop_enabled`）。运行中规划者的答复是一条最终消息（今天如此，流式另记为非核心待办） |
| 旁边 | 选中任务时的任务面板：需求（带版本；草稿和就绪时可以直接改）、项目和目录（草稿时可以改）、「开始」（草稿和就绪时出现；缺需求或目录时不可用，写明缺什么）、计划和步骤（复用任务页的数据，紧凑列表）、关卡、文件改动（`task_diff` 的统计，链到完整的 diff）、花费；「Runs」标签里是今天 Chat 页的规划者和执行者、评审者的只读运行视图 |

- **新建任务：** 一点就在当前选中的项目组里（没有就进 `inbox`）建一个草稿，选中它，光标落在输入框里，占位文字「说说这件事……」；不弹表单。一个字没说的空草稿，面板里有「删除这个草稿」，一步确认。
- **管家：** 选中置顶的「管家」时，中间就是今天的管家视图，包括按渠道列出的对话、「New session」和 Agent 控件，一处不改。
- **场景管家：** 列在置顶的「管家」下面，各自一行，行为不变。v0 的「新建任务」只建在默认管家名下。

### 6.2 状态与断点

- 断点照 `docs/design.md`：宽屏三栏；中等宽度两栏，任务面板成为对话上方的一个标签；窄屏单列，列表 → 对话（返回按钮），任务面板是对话里的一个标签。`pointer-coarse` 下交互目标不小于 44 px。
- 状态：列表为空（只有「新建任务」和管家）、加载中、读失败（写明原因，可重试）、任务会话起不来（写明原因，比如运行时不支持只读启动）、开关关着（Chat 页和今天逐字节相同，没有「新建任务」）。
- 不显示会话 id、本机路径以外的东西也照 `docs/design.md` 的隐私规则；截图去掉本机路径。

## 7. CLI

### 7.1 命令

```
looper task enter  [<选择>] [--all]                                     # 终端里进任务对话
looper task ask    [--task <选择>] <text> [--timeout SECONDS]          # 发一条，等答复
looper task claude [<选择>] [--all] [--mode observe|work] [--new] [--claude PATH]   # 操作者自己的 Claude Code 会话
looper task brief  [<选择>] [--all] [--full] [--hook]
looper task sessions [<task_id>]
looper task register --dir PATH --requirement TEXT [--handoff-file FILE] [--session ID]
                     [--project NAME] [--workspace-mode worktree|in_place] [--start]
looper task reopen <task_id> <text> [--budget-usd N] [--yes]
```

- 都带 `--runtime-root`、`--config`（`cli.py` 的 `_add_location_flags`），`--claude` 的默认值同 `looper claude-code`（`cli.py:122`）。读操作直接读本机数据库；`enter`、`ask`、`reopen`、`register --start` 经回环地址、用本机的操作者令牌（`operator_auth.read_token`）调本机服务。
- `<选择>` 可以省略，可以是几个字或一个 MR，也可以是任务 id（7.4）。
- 退出码是这组命令自己的（和 `looper ledger` 不同，那里的 3 是「台账没开」，`ledger_commands.py:8`）：0 完成，2 用法，3 列出了候选、什么都没做，4 拒绝（规则或身份），5 没有这个任务，6 数据库不可用，7 找不到 Claude Code（`claude`）。拒绝时打印类型化原因、持有者（如果是租约）和下一步怎么做。
- 在 Looper 启动的 agent 里（`LOOPER_AGENT_RUN`）一律拒绝，做法同 `ledger_commands.py:152-161`。

### 7.2 `looper task enter`：在终端里进任务对话

- 终端里的一个对话客户端：先打印任务、状态和最近几条对话，然后每行输入经 `operator_chat.send` 发给这个任务（目标 `kind: task`，第 1 步的 1.2 加进 `resolve_chat_target`，`chat_targets.py:34-57`），按 5.3 的 `answerer` 交给规划者或任务会话，跟着 `reply_events` 流式打印答复；5.5 的关键活动以一行摘要插进来；Ctrl-C 在回复期间等于 Stop（跟 `chat.stop_enabled`），空闲时退出。
- 和控制台看到的是同一段对话、同一个上下文：同一个会话键、同一个对话线程，消息记下入口 `cli`。
- `looper task ask` 是它的一次性版本：发一条，等最终答复打印出来（默认等 300 秒；超时只说还没答，消息已经存下）。

### 7.3 `looper task claude`：操作者自己的 Claude Code 会话

要自己动手改代码时用它：在任务的目录里打开（或续上）操作者自己的 Claude Code 会话，带上任务简报。这条会话是操作者的，不是任务对话；会话里要和任务说话，就运行 `looper task ask "…"`。

**目录和方式：**

| 任务 | 工作区方式 | 目录 | 默认方式 | `work` |
|---|---|---|---|---|
| 草稿 | 任意 | 有目录时 `work_dir`，没有就拒绝 `no_directory` | `work` | 先拿租约 |
| 就绪（存的状态是 `init`） | 任意 | `work_dir` | `work` | 先拿租约 |
| 进行中、等你 | 原地 | `work_dir` | `observe` | 要显式 `--mode work`；共享租约在任务的运行手里，拒绝 `lease_held`，说明持有者（哪些任务、第几次运行、从什么时候起），提示停掉任务或等它结束 |
| 进行中、等你 | worktree | 任务 worktree 里的任务目录（`work_dir`） | `observe` | 同上 |
| 失败、停止、取消 | 原地；worktree（保留着） | `work_dir` | `work` | 先拿租约 |
| 完成 | 原地 | `work_dir` | `work` | 先拿租约 |
| 完成 | worktree（已删） | `observe`：工作区 W（`workspace_dir`），简报写明结果在分支 T、没有检出在这里；`work`：拿到租约后在原路径建回任务 worktree（9.5 的规则），进它里面的任务目录 | `work` | 先拿租约；T 正检出在别处时拒绝 `branch_checked_out`；T 和 `result_commit` 都没了时拒绝 `branch_gone` |

- `observe` 给 Claude Code 加 `--permission-mode plan`：改文件、跑有副作用的命令之前，要操作者同意退出 plan 模式。这是防误改，不是隔离。方式在启动时定下，要换就退出再进。
- 为操作者建回的 worktree 记成 `operator` 状态（9.5），Looper 不替操作者提交；离开时打印它的路径和未提交的文件数；重新打开时它交还给任务。
- 第一次进入时记下目录。以后任务的目录变了，拒绝并提示 `--new`：Claude Code 按目录找会话，换了目录可能续不上（需要验证）。

**会话 id 与续接：**

```sql
CREATE TABLE IF NOT EXISTS task_operator_sessions (
    task_id              TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    runtime_type         TEXT NOT NULL CHECK (runtime_type = 'claude-code'),   -- 以后别的 CLI 放宽这条
    session_id           UUID NOT NULL,
    cwd                  TEXT NOT NULL,
    origin               TEXT NOT NULL CHECK (origin IN ('claude', 'register')),
    previous_session_ids UUID[] NOT NULL DEFAULT '{}',
    last_mode            TEXT CHECK (last_mode IN ('observe', 'work')),
    seen_activity_seq    BIGINT,          -- 上次离开时读到的 activity.seq（简报的「上次离开以后」）
    seen_journal_id      BIGINT,          -- 同上，task_journal.id
    entered_count        INTEGER NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_entered_at      TIMESTAMPTZ,
    last_left_at         TIMESTAMPTZ
);
```

- 第一次：`claude --session-id <新 uuid> --name "<任务标题> · <task_id>" --settings <钩子 JSON> [--permission-mode plan]`，工作目录是上表的目录。`looper task claude` 作为父进程等 Claude Code 退出，不 `exec`，因为退出后要推进游标；`work` 的租约描述符不带 close-on-exec，由 Claude Code 继承，所以父进程被杀掉、Claude Code 还在写时，租约仍然被持有（11.1）。
- 以后：`claude --resume <session_id> --settings <…> [--permission-mode plan]`。续不上时不自动新开，提示 `--new`。
- 会话 id 只留在本机：`looper task sessions` 列出来，不进 API、页面和日志。任务详情（`tasks.detail`）只多一个 `operator_session` 字段：上次进入时间、方式、现在是否在里面。v0 只支持 Claude Code。

**简报：** `task_brief.compose(task_id, since=None, limit=6000)` 只用数据库里的类型化字段拼出，不调用模型；任务会话用同一个函数（5.4）。固定的说明用英文，数据原样放进去。

| 节 | 内容 | 上限（字符） | 围栏 |
|---|---|---|---|
| 头 | 任务 id、标题、状态、项目、第几次运行、工作区方式和分支、这次进入的方式；一句话说明简报是 Looper 给的数据 | — | 否，代码写的 |
| 需求 | 最新版本的需求；草稿没有需求时写明 | 1500 | 来源不是操作者本人时围起来（新 `Source.TASK_REQUIREMENT`） |
| 补充说明 | 最近 5 条 | 每条 300 | 不是操作者给的用 `Source.SUPPLEMENT` |
| 计划 | 每步一行：键、状态、角色、说明的前 120 字 | 1500 | `Source.JOURNAL` |
| 进度 | 结束时的总结；还在跑就是日志的最后 3 条 | 1000 | `Source.JOURNAL` |
| 等你的 | 打开的关卡：话题和最后一条消息 | 500 | `Source.GATE_MESSAGE` |
| 上次以来 | 续上时：游标之后的阶段变化、步骤结束、错误，最多 10 条 | 1000 | `Source.JOURNAL` |
| 怎么做 | 固定的句子：用 `looper task ask` 和任务说话；`observe` 时说明任务的 agent 正在这个目录里改文件 | — | 否 |

- 围栏由 `untrusted.fence()` 生成，简报里总是打开，不看 `untrusted_fence.enabled`（同能力范围 RFC 的 D16）。超出上限的节截断并写明省掉多少；`looper task brief --full` 打印不截断的版本。不放：操作者画像、台账、别的任务、记忆。

**SessionStart 钩子：**

- 每次启动传 `--settings`，内容只有一个钩子：`{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "<looper> task brief <task_id> --hook --runtime-root <…>", "timeout": 5}]}]}}`，命令行由 `claude_code_entry.desired_handler` 的同一套办法拼出（`claude_code_entry.py:140-146`）。钩子只存在于这次启动，不写进用户设置（工作区 RFC W6）。
- 钩子看 Claude Code 传来的 `source`：`startup`、`clear`、`compact` 给完整简报，所以压缩以后简报还在；`resume` 再加「上次以来」。
- `--hook` 时的约束同画像钩子：本地读数据库，1 秒内读不到就不输出；任何失败都不写 stderr、退出 0；在 Looper 启动的 agent 里不输出（`claude_code_entry.py:69-73`）。不带 `--hook` 时，原因打到 stderr，按 7.1 的退出码退出（`claude_code_entry.py:74-78`）。`looper task claude` 启动前自己先拼一次，拼不出就打印一行原因，照常启动。
- 钩子不推进游标；Claude Code 退出后由父进程推进。
- 需要验证：`--settings` 里的钩子和用户设置里的钩子是否都执行，`source` 的取值，`additionalContext` 的长度限制。验证不通过时退回 `--append-system-prompt-file`（每次启动重新生成，退出后删除），只作备选。

**在会话里说话：** 父进程给 Claude Code 的环境加 `LOOPER_TASK_SESSION=<task_id>`：带这个变量时，`looper task ask`、`reopen` 不用指明任务，也只接受这一个任务（别的拒绝 `other_task`），写操作记 `via = cli_task_session`。这是减速带：会话以操作者的系统账号运行。不自动写回：会话里发生的事，只有操作者用 `ask` 说出来，任务才知道。

### 7.4 不用任务 id 找到任务

`enter`、`ask --task`、`claude` 和 `brief` 用同一个解析函数 `task_select.resolve(选择, cwd)`。它是确定性的代码，只读数据库，从不调用模型，结果是三种之一：一个任务、一张候选表、没找到。

| 选择 | 规则 |
|---|---|
| 任务 id（`task-` 加 12 位十六进制，`projects.py:619`） | 就是这个任务，没有就退出码 5 |
| 省略 | 先看环境变量：在 `looper task claude` 打开的会话里（`LOOPER_TASK_SESSION`）就是那个任务。否则取 `cwd` 的真实路径，找目录等于它或包含它的任务：任务的 `work_dir`、`workspace_dir`、活着的或 `operator` 状态的任务 worktree。只保留最深的那一层目录；剩一个就是它；剩几个时，其中恰好一个在进行中就取那个，否则列出来。一个都没有，就列出最近的任务：最近 14 天有动态的（含草稿），最多 20 个，按最近动态倒序，每行标题、项目、状态、最近动态的时间 |
| 一个 MR | 整个选择是一个 MR：带 `/pull/<N>` 或 `/merge_requests/<N>` 的链接（同时取出仓库路径），或 `!N`、`#N`、`MR N`、`PR N`，或 1 到 7 位的纯数字。和任务关联的 MR 比较：任务等待步骤里的 `github_pr` 检查（仓库和编号，`waits.py:228-244`），以及用同一套规则从标题、需求、原话和补充说明里认出的引用；任务文本里的纯数字不算引用。链接里有仓库时，仓库和编号都要对上，或者编号对上、引用本身没写仓库 |
| 别的文字 | 先规范化：NFKC（全角转半角）、转小写、空白合成一个空格、去掉两头的引号。再按空格切成词（连着写的中文整段算一个词），每个词都要是任务文本的字面子串才算匹配（不是通配符，同 `db/tasks.py:690-691` 的任务搜索）。看的字段只有标题和最新版本的需求 |

- **范围：** 文字和 MR 只在最近 90 天有动态的任务里找，`--all` 找全部。「最近动态」是这个任务最新一条 activity 或对话消息的时间，都没有时用 `tasks.updated_at`。
- **排序：** MR 先排仓库也对上的，再排只有编号对上的；文字先排每个词都在标题里的，再排要靠需求才凑齐的。同一档里进行中的排前面，然后按最近动态倒序。
- **什么算模糊：** 只有恰好一个匹配时才直接进入。两个及以上一律列出来让操作者挑（最多 10 行，多出的只报个数），排第一的也不自动选。没有匹配时明说没找到，写出查了哪些字段和多大范围，提示 `--all` 或任务 id，从不退回模糊匹配或「最像的一个」。
- **挑选：** 在终端里，候选表带编号，输入编号就进那个任务，回车直接退出（退出码 3）。不在终端里时（比如会话里的模型用 Bash 调 `ask`），只打印带任务 id 的候选表，退出码 3，什么都不做。`reopen` 用同一个解析，但它是写操作，只接受唯一的结果，或者操作者在终端里挑出的那一个。

## 8. 反向：把已有目录登记成任务

**流程：**

1. 操作者在自己的会话里让模型写一份交接说明（做到哪、改了什么、怎么验证、下一步），存成文件，自己看过。
2. `looper task register --dir . --requirement "…" --handoff-file handoff.md [--session <这条会话的 id>] [--start]`。
3. Looper 走和 `looper_task_create` 相同的建任务路径，建一个就绪任务；交接说明存成一条 `via = handoff`、来自操作者的补充说明。`--start` 时立即开始。
4. 规划者第一轮 `[PLAN]` 拿到需求，交接说明作为补充说明在同一轮送达。任务对话从这里开始，之后和别的任务一样。

| 交过去 | 不交过去 |
|---|---|
| 需求（操作者写的） | CLI 会话记录：消息、工具结果、读过的文件内容 |
| 交接说明（在会话里写、操作者看过） | 用户级设置、用户级 CLAUDE.md、个人 skill、MCP、权限放行记录 |
| 目录和它的 git 状态：默认原地，就是这个目录，包括未提交的改动；显式选 worktree 时从当前提交建分支 | 会话里的待办、plan 模式这类会话状态 |
| 操作者为这个目录记下的笔记：W6 开着时本来就读 | 操作者画像：Looper 的 agent 本来就不带 |
| `--session` 给的会话 id：记成这个任务的操作者会话，之后 `looper task claude` 续上它 | — |

- **工作区方式：** 默认原地（`in_place`），不按 W1 推：目录留在原处，Looper 挂到它上面。要 worktree 就显式 `--workspace-mode worktree`；这时目录里有未提交的改动就拒绝（`uncommitted_in_worktree`），因为任务的 worktree 看不到它们。原地意味着任务的 agent 就在操作者刚才工作的目录里改文件：`register --start` 的输出写明这一点；之后操作者用 `looper task claude` 回来，按 11.1 的租约进。
- **不把 CLI 会话分叉给规划者（以后的探索，16.9）：** 规划者和操作者用同一个 Claude Code 配置目录，所以在同一目录下 `--resume <id> --fork-session` 也许找得到这条会话（需要验证）。本版本不做：规划者的会话是结构化的计划协议，CLI 会话不是；会话记录里可能有私人内容，而规划者的输出会进 activity、room 和渠道通知；启动参数和会话产生时不同；worktree 任务的目录不同，本来就续不上。
- 登记出来的任务默认属于默认管家（`--manager` 可选），来源是 CLI，没有渠道通知目标。

## 9. 重新打开已结束的任务

任务结束以后，操作者回到它的对话里说「接着做……」：任务会话（5.3）调 `task_reopen`，操作者确认，背后是这一节的转换。控制台的「重新打开」和 `looper task reopen` 走同一条路。

### 9.1 候选

| | A. 同一个任务重新打开（采用） | B. 关联的后续任务 | C. 只把 `restart` 放宽到完成的任务 |
|---|---|---|---|
| 做法 | 类型化转换 `reopen`：带续接说明，`run_no` 加一，规划者续自己的会话和计划 | 新任务带 `follows_task_id`，规划者会话从前一个任务分叉，worktree 从 T 建新分支 | `restart` 接受 `completed` |
| 任务 id、对话、台账条目 | 不变 | 都换新的 | 不变 |
| 规划者的上下文 | 自己的会话、计划，加一轮 `[REOPEN]` | 分叉来的会话，加前一个任务的简报 | 自己的会话，但没有续接说明 |
| 预算 | 累计（9.4） | 新任务新上限 | 累计 |
| 风险 | 「完成」不再是终态，只跑一次的收尾要逐处核对（9.8） | 同一件事仍然散成几个任务 | 规划者不知道为什么又开始了 |

**采用 A。** 要解决的就是「同一件事散成好几个任务」；在任务对话这个模型里，回到同一个任务接着说也最自然。B 留给「这是一件新事，只是和上一件有关」，本文不做。

### 9.2 转换

`RestartMode.REOPEN = "reopen"`，由 `check_task_control(task_id, "reopen")` 检查：

| 当前状态或条件 | `reopen` | 说明 |
|---|---|---|
| 草稿、就绪 | 拒绝 `not_started` | 用 `start` |
| 进行中、等你 | 拒绝 `still_running` | 直接在任务对话里说，规划者在答 |
| 失败、停止、取消 | 允许 | 就是「带说明的 `restart`」 |
| 完成 | 允许 | 新增 |
| 没有目录 | 拒绝 `no_directory` | 同 `restart`（`projects.py:1257-1258`） |
| 定时任务产生的任务（`tasks.schedule_id` 不为空） | 拒绝 `scheduled_task` | 它的规划者用的是定时任务共用的会话（`_claim_schedule_session`，`tiers/planner.py:7111-7138`），不是 `planner-<task_id>`；重新打开要么续不到这次运行真正用的会话，要么把 `[REOPEN]` 写进下一次定时运行还要接着用的会话。要追问，改定时任务的需求或立即运行一次 |
| 绑了意图规则的任务（`config.intent_rule_bound`） | 拒绝 `rule_bound` | 开始路径会处理规则执行的租约（`projects.py:1214-1238`），`reset_task_for_restart` 不碰那些行；重新打开算不算那次执行，v0 不定 |
| 这个 checkout 的排他租约在操作者的 `work` 会话手里 | 拒绝 `lease_held`，说明持有者 | 11.1 |
| 已经到上限，命令里没给新上限 | 拒绝 `budget_exhausted` | 9.4 |
| 任务有上限，花费读不到 | 拒绝 `budget_unavailable` | 9.4 |
| worktree 任务的分支 T 正检出在别处 | 拒绝 `branch_checked_out` | 9.5 |
| worktree 任务的 T 和 `result_commit` 都没了 | 拒绝 `branch_gone` | 9.5 |

**入口：** 任务会话的 `task_reopen`；v1 操作 `tasks.reopen`（`task_id`、`text`、可选的 `budget_usd`；`operator_only`，控制台和 `looper task reopen` 用它）；管家工具 `looper_task_control` 的新动作 `reopen`，多一个 `text` 参数（开关关着时工具的描述和参数逐字节不变）。

**执行顺序：**

1. **检查**（`check_task_control`，发消息之前）：上表每一行，包括 9.4 的预算预检和 9.5 的分支检查。任何一行不过，什么都不写。
2. **一个事务：** 给了新上限就写进这个任务的 `tasks.budget_usd`；续接说明存成补充说明（`via = reopen`，`from_operator = true`，未送达）。新上限因此在规划者启动之前就已经写好。
3. **发 `TASK_RESTART`**，`mode = reopen`，带上补充说明的 id、入口、回复路由，以及 CLI 入口的会话标签（下面第 5 条用）。
4. **TaskRouter 的 `_handle_restart`** 只在这个模式下多接受 `completed`（`tiers/task.py:742-746`）。写任何状态之前，先用同一个谓词再算一次预算，再拿这个 checkout 的共享租约（11.1）；任何一项不过就不开始，activity 写明原因，补充说明留着，下一次运行时送达。都过了，其余同 `continue`：`close_attempts` 只关当前 `run_no` 上还没裁决的尝试，已经裁决的不改；然后在一个事务里 `reset_task_for_restart(keep_steps=True)` 把 `run_no` 加一，并写 `task_runs` 的一行（9.7）；最后发 `TASK_REQUEST`，带上 `restart = reopen` 和结束时的状态。
5. **台账：** 任务有台账条目、台账也开着时，第 4 条提交之后写一条 `note` 事件，`actor_kind = user`，`via` 是入口。入口是 CLI 时，`session_label` 必填（`schema.sql:1831-1832`）：在 `looper task claude` 打开的会话里是 `task-session:<task_id>`，否则是 `cli:task-reopen`。条目的状态本来就读任务的状态（`db/ledger.py:213`），所以这条 note 只是记录：写失败时记日志、计数（`task_reopen_ledger_note_failures`），不中止、也不回滚重新打开。

不带说明的 `restart` 行为不变。

### 9.3 规划者拿到什么

规划者续自己的会话（`planner-<task_id>`，`agent_sessions` 里还在；定时任务的任务在 9.2 已经拒绝）；运行时或 provider 变了续不上时，走现有的 `[HANDOFF]` / `[RESUME]`（`tiers/planner.py:5986`、`:6033`）。然后是一轮 `[REOPEN]`，回答用 CHAT 的形状。

**控制流（只换消息不够）：** 代码走 `_restart_plan`（`tiers/planner.py:4392-4492`），`RestartMode.REOPEN` 改三处：

1. 到保存这一步为止和 `continue` 一样，等待中的关卡那一段（`:4439-4451`）原样保留：它清掉关卡的失败计数，并给每个仍在等的关卡发「The task was restarted; this gate still waits for you.」。超限即返回的那一句（`:4412`）也保留，9.4 的预检已经保证它这时会放行。
2. `:4458-4460` 在计划已经完成时直接 `_complete_plan` 并返回（`is_complete`：每一步都是 verified 或 skipped，`:585-586`）。这一句只对 `continue` 生效；`reopen` 跳过它，接着发 `[REOPEN]`，否则规划者永远看不到续接说明，任务会立刻又完成。
3. `[REOPEN]` 的答复：`add_steps` 或 `replan` 照 `_apply_chat` 走；`none` 时，先 `_mark_delivered`，计划仍然完成就 `_complete_plan`，答复的 `final` 作为新的总结。这一轮失败时：计划已经完成的，任务以类型化原因 `reopen_turn_failed` 失败，续接说明不标送达，下一次重新打开或重启时再送；计划没完成的，照 `continue` 的退路让没开始的步骤照常跑，续接说明留到规划者下一轮 CHAT 送达。

消息（完成的任务用下面这句自己的开头；`_ended`（`:4496-4500`）只有 failed、cancelled、stopped 三种说法，不复用）：

```
[REOPEN] The task completed on <date> (run 1): <final summary, clipped>. The operator reopened it (run 2) with the
request below. The accepted steps stay done. Since it ended: <code-written workspace changes>.
<the reopen request: a supplement from the operator>
<other changes you have not had>
<plan view, journal>
Decide what the request needs: add_steps or replan. Reply "none" only when nothing is needed, saying why in
"reply"; the task then completes again.
```

- **结束以后目录里发生了什么**由代码算出：worktree 任务是分支 T 上 `result_commit` 之后的提交（主题行，最多 20 条），以及 T 还在不在；原地任务是结束时目录的 HEAD（新列 `task_outcomes.end_commit`，结束写这一行时一并写下）之后的提交，加上未提交的文件清单（最多 50 个）。操作者用 `looper task claude` 改过的东西就这样到了规划者那里。
- 失败、停止、取消的任务重新打开时，没做完的步骤照 `continue` 回到未开始，消息开头用 `_ended` 给出的结束原因。
- `[REOPEN]` 是新的模型可见内容：第 3 步用真实 Claude CLI 跑规划者评测（开发计划第 1 节）。

### 9.4 预算

- **累计：** `_within_budget` 读这个任务所有运行的花费（`tiers/planner.py:6851-6887`），重新打开不重置。
- **同一个谓词：** 上限是任务自己的 `budget_usd`，没有就用项目的 `task_budget_usd`（`db/tasks.py:126-128`）；花费是 `task_costs` 的 `cost_usd`，有未定价的模型时用已知的部分；`spent < cap` 才算还有额度。这段从 `_within_budget` 里抽成一个共用函数，规划者和重新打开都调它；`_within_budget` 在运行中的行为（包括读不到花费时放行，`:6866-6873`）不变。
- **在重置之前预检：** `check_task_control` 先算一次，TaskRouter 在 `reset_task_for_restart` 之前再算一次（9.2 执行顺序的第 1、4 条）。已经到上限时，命令里必须给出新的上限（`--budget-usd N`；控制台、任务会话和管家的 `reopen` 同理），而且要高于已花费，否则拒绝 `budget_exhausted`，答复里写出已花费和上限。
- **读不到花费：** 任务有上限而花费读不到时，预检拒绝（`budget_unavailable`），和运行中的放行不同：重新打开是一个「再花钱」的决定，没有这个数就守不住「从不悄悄接着花」。没有上限的任务不用读花费。
- **只写这个任务：** 新上限只写这个任务自己的 `tasks.budget_usd`，从不写项目的上限。`tasks.continue` 在任务没有自己的上限时提高的是项目的 per-task 上限，会波及项目里的其他任务（`v1_domains/task_configuration.py:61-70`）；`reopen` 不那样做。它沿用同一套高风险确认的机制，但文案说的是这个任务：「要把任务「<名字>」自己的花费上限设为 $N（已花费 $S，原来的上限 $C 来自任务 / 项目）并重新打开吗？只改这个任务，项目和其他任务的上限不变。」
- 不做每次运行单独的上限：分开算会让「多开几次」绕过上限。

### 9.5 工作区

`prepare`（`task_workspaces.py:324-385`）今天的路径：行是 `active` 时沿用，目录丢了走 `_restore`（`:387-400`），分支没了就报错，不重建；没有行或行是 `removed` 时，先算基点 `_base_commit(repo, workspace_base)`（`:346`，点名的基点，否则 W 现在的提交），分支在就 `worktree add <路径> T`（`:353`），不在就 `worktree add -b T <路径> <基点>`（`:355`）。最后这一支也是新任务第一次建 worktree 的路径，本文不改它。

| 方式与状态 | 重新打开时（以 `work` 进入时相同） |
|---|---|
| 原地 | 同一个目录；同一目录上有别的进行中任务时提醒（W5） |
| worktree，失败、停止、取消（行是 `active`） | 照今天：沿用；目录丢了走 `_restore`，分支没了就报错 |
| worktree，完成，T 还在 | 照今天：`:353` 按 T 建回，T 上的提交都在 |
| worktree，完成，T 已删（例如合并后删了分支） | 不走 `:355`。`reopen` 给 `prepare` 传 `from_commit = result_commit`（`finish` 删 worktree 时记下的 T 的末端，`schema.sql:510`）：这个提交还在仓库里（合并过就一定在），就 `worktree add -b T <路径> <result_commit>`，activity 写明；不在了就在执行顺序的第 1 条检查时拒绝 `branch_gone`。只存在于已删的 T 上、又被 git 回收的提交找不回来，Looper 不假装能 |
| worktree，完成，操作者以 `work` 进入时已经建回（`operator` 状态） | 沿用这个 worktree，状态改回 `active`；未提交的改动交给任务，列进 `[REOPEN]` 的目录变化 |
| worktree，完成，T 正检出在别处（例如操作者在 W 里切到了 T） | 在执行顺序的第 1 条检查时拒绝 `branch_checked_out`，告诉操作者 T 检出在哪、怎么放开；不替操作者切分支 |

**`operator` 状态要改的地方**（今天写不进去）：

- `task_worktrees.state` 的 CHECK（`schema.sql:509`）加 `'operator'`；`WorktreeState`（`task_workspaces.py:63-65`）加 `OPERATOR`。
- 启动清扫只认 `list_active` 给的行（`db/task_worktrees.py:40-47`，只取 `active`），别的目录当孤儿，先提交再删（`task_workspaces.py:442-461`）。改成取 `active` 和 `operator`：`operator` 的目录进已知集合，从不收尾；清扫在收尾或删除任何目录之前，先非阻塞地试这个 checkout 的排他租约（11.1），拿不到就跳过这一次。
- `finish` 对不是 `active` 的行直接返回（`:413`），既不提交也不删。加一条删除任务的分支：`operator` 的行在删除任务时照 `active` 的做法先提交剩余改动、再删目录、留分支；完成收尾用不到它，`operator` 只出现在已完成的任务上。
- `prepare` 把 `operator` 的行当 `active` 处理（目录在就沿用，丢了走 `_restore`），然后改回 `active`。
- `looper task claude` 建回时先拿排他租约，再执行和上表相同的 git 命令，最后把行写成 `operator`。

### 9.6 通知

- 重新打开的运行，进度和结束只告诉发起它的地方（`task_runs.reply_route`）：任务对话里发起的就在任务对话里（所有入口都看得到）；管家在某个聊天里发起的回到那个聊天；以后在飞书话题里发起的回到话题。
- 原来的来源聊天（`origin_reply_route`）不再收到这次运行的通知：那里是当初提需求的地方，重新打开是操作者的事。这是本版本的默认（16.2）。

### 9.7 `task_runs`

```sql
CREATE TABLE IF NOT EXISTS task_runs (
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_no        INTEGER NOT NULL CHECK (run_no >= 2),   -- 第一次运行没有行，和今天一样
    reason        TEXT NOT NULL CHECK (reason IN ('restart', 'start_over', 'at_cap', 'reopen')),
    via           TEXT NOT NULL CHECK (via IN ('dashboard', 'lark', 'telegram', 'cli', 'api', 'system')),
    ended_status  TEXT NOT NULL,                           -- 开始前任务的状态
    supplement_id BIGINT,                                  -- reopen 的续接说明（task_journal.id）
    reply_route   JSONB CHECK (reply_route IS NULL OR jsonb_typeof(reply_route) = 'object'),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, run_no),
    CONSTRAINT task_runs_reopen CHECK ((reason = 'reopen') = (supplement_id IS NOT NULL))
);
```

- 每条重启路径都写一行，和 `reset_task_for_restart` 在同一个事务里；`task_outcomes` 记结束，两张表按（任务, `run_no`）对上。`via` 的取值和台账事件一致。
- 通知路由：运行号大于 1、且这一行有 `reply_route` 时用它；否则照今天，用任务的来源。现有的 `restart` 不写 `reply_route`，行为不变。

### 9.8 「完成」不再是终态：要核对的地方

第 3 步逐处核对，每处一个测试：

- `AGENTS_EDITABLE_STATUSES` 的注释说「只有完成任务的 agent 是最终的」（`projects.py:1290-1291`）：重新打开后按进行中处理。
- 工作区：9.5 列出的 CHECK、枚举、`list_active`、清扫和 `finish` 的删除分支。
- 关卡：重新打开后任务不再是完成，仍在等的关卡又开始收回复（`gate_routing.py:16` 只在任务完成时把回复交给管家）；9.3 保留的那段会给它们发「still waits」。
- 结束时只跑一次的收尾：`_teardown_task`、`_task_ended`（`tiers/task.py:794-828`）以及挂在任务结束上的记忆收尾、结束通知、成本释放，默认按运行号再跑一次。`reset_task_for_restart` 里的 `db.tasks._task_ended`（`db/tasks.py:542`）只通知 listener：确认它们不会因为重置再记一条结果，也不会再发结束通知。
- 尝试：`close_attempts` 只关当前 `run_no` 上还没裁决的尝试，发生在加一之前。
- 控制台把「完成」当最终状态的地方：任务页的按钮，Chat 页的「task ended · read-only」。

## 10. 任务会话键

### 10.1 键与表

```python
class SessionKind(str, Enum):
    MAIN = "main"
    GROUP = "group"
    DM = "dm"
    TASK = "task"  # one task's conversation, whichever entry a message comes from


@dataclass(frozen=True)
class SessionKey:
    kind: SessionKind
    channel_id: str | None = None
    chat: str | None = None
    task_id: str | None = None  # TASK only, and then no channel or chat
```

- 文本是 `task:<task_id>`，带指纹时接 `~<指纹>`（10.3）；会话归这个任务的 `manager_id`。`__post_init__` 检查：`TASK` 要有 `task_id`，不能有渠道和聊天；其他种类不能有 `task_id`。
- **一个键覆盖所有种类**（不另造第三种会话概念）：`main`（管家的 main 会话，存在 `agent_sessions`）；`group:<channel>:<chat>~<指纹>` 和 `dm:<channel>:<chat>~<指纹>`（能力范围 RFC 第 7 节）；`task:<task_id>~<指纹>`（本文）。都由列拼出，代码不解析文本。
- `manager_sessions` 的改动（新列可空，现有的行不受影响；内联 CHECK 的约束名在实现时按库里实际的名字核对）：

```sql
ALTER TABLE manager_sessions
    ADD COLUMN IF NOT EXISTS task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    ALTER COLUMN channel_id DROP NOT NULL,
    ALTER COLUMN chat DROP NOT NULL,
    DROP CONSTRAINT manager_sessions_kind_check,
    ADD CONSTRAINT manager_sessions_kind_check CHECK (kind IN ('group', 'dm', 'task')),
    -- 任务会话只有 task_id；群和私聊会话照旧要有渠道和聊天
    ADD CONSTRAINT manager_sessions_task_shape CHECK (CASE WHEN kind = 'task'
        THEN task_id IS NOT NULL AND channel_id IS NULL AND chat IS NULL
        ELSE task_id IS NULL AND channel_id IS NOT NULL AND chat IS NOT NULL END),
    DROP CONSTRAINT manager_sessions_key,
    -- scope_fingerprint 由能力范围 RFC 的 1b 加上；本文的 1.2 排在它之后（13.2）
    ADD CONSTRAINT manager_sessions_key CHECK (session_key = CASE kind
        WHEN 'task' THEN 'task:' || task_id
        ELSE kind || ':' || channel_id || ':' || chat END || coalesce('~' || scope_fingerprint, ''));
```

- **对话记录：** 任务对话有自己的线程 id `task-chat:<task_id>`，在 `chat_threads` 里归这个任务的管家；`chat_messages` 每行另记它从哪个入口来（新列 `entry`：`dashboard`、`cli`、`lark`）。线程 id 和会话键是两样东西，代码从不拿会话键去查 `chat_messages`（能力范围 RFC 第 7 节的规则）。规划者在运行中答的 `[CHAT]` 也写进这个线程。
- **任务换了管家：** `move_task`（`projects.py:1202`）把任务挪到别的管家之后，旧管家名下的任务会话行同一个事务里写上 `retired_at`，下一条消息在新管家名下开新会话，开场带对话线程的摘录；线程改归新管家。

### 10.2 回复路由

```python
@dataclass(frozen=True)
class ReplyRoute:
    entry: Entry                       # DASHBOARD | CLI | LARK_THREAD
    channel_id: str | None = None      # LARK_THREAD: the bot
    chat: str | None = None            # LARK_THREAD: the chat
    root_message_id: str | None = None # LARK_THREAD: the task's root post
    correlation_id: str | None = None  # DASHBOARD, CLI: the reply stream
```

- 每条进入任务对话的消息带自己的 `ReplyRoute`，答复按它发回；会话本身不记「上一条从哪来」。控制台里问一句、CLI 里问一句，进的是同一条会话，答复各回各处，线程里两条都在。
- 和 `tasks.origin_reply_route` 的关系：那是任务的来源，用来发结束通知；`ReplyRoute` 是一条消息的答复去处。两者都是数据，都不进会话键。

### 10.3 和能力范围 RFC 的组合

能力范围 RFC 第 16 节为任务维度留了三条约束，逐条对上：

- **按列取值。** `manager_sessions` 加 `task_id` 一列；键由 `kind`、`task_id`（以后加上 `channel_id`、`chat`）和 `scope_fingerprint` 拼出，代码不解析键的文本。
- **任务维度不进指纹。** 指纹只描述「能做什么」：任务会话的指纹哈希集合、版本、受众和启动模式 `read_only`，不含任务 id 和目录路径，所以同一个范围下换一个任务，指纹不变。键的形状：

```
task:<task_id>~<fingerprint>                       v0：只有操作者一个受众，范围是内置集合 task_session
task:<task_id>@<channel_id>:<chat>~<fingerprint>   以后：群里别人对着任务说话（共享受众），每个聊天一条
```

  `~` 仍是指纹的分隔符；聊天段用 `@`，不和 `~`、渠道线程 id 的 `#manager…` 后缀混淆。任务是键的主体，因为任务对话不在某一个聊天里，飞书话题只是它的一个入口。真有「某个群里的人对着某个任务说话」时，聊天作为第二段加进来，仍然每个（聊天, 范围）一条会话。v0 不做共享受众。
- **摘录按线程取。** 任务会话的摘录按任务对话的线程 id 取（10.1），不动范围模型。
- **先后。** 本文的 1.2 排在那篇 1b 合入之后（13.2），所以任务会话的键从一开始就带指纹，键约束由 1.2 写成 10.1 里那个合在一起的形式。那篇的 id 检查列了五个内置集合：`chat_only`、`task_entry`、`readonly_qa`、`full`、`legacy`（那篇 D3、5.4），1b 写入其中的 `chat_only`、`full`、`legacy`，另外两个在那篇第 2、3 步写入。`task_session` 不在其中任何一步里，由本文的 1.2 加；id 检查以 10.4 的 SQL 为准，六个都列上，不会拒绝已有的行。

### 10.4 任务会话的范围（本文的 1.2）

**内置集合 `task_session`**，由代码定义，迁移写入第 1 版，Settings 里改不了：

```json
[{"kind": "chat"},
 {"kind": "tasks", "projects": "manager", "visibility": "bound_task",
  "actions": ["read", "requirement", "workspace", "start", "reopen"]},
 {"kind": "local.read_only"}]
```

这一步同时扩展能力项的类型（那篇 5.1）：`tasks.actions` 加 `requirement`、`workspace`、`start`（那篇已有 `start`，这里沿用）和 `reopen`；`tasks.visibility` 加 `bound_task`（只有会话绑定的那个任务）；新能力项 `local.read_only`，即规划者的只读启动（5.4），启动模式记为 `read_only`。这些新值只在 `task_session` 里合法，写进别的集合一律被拒（那篇 D2 的写入校验）。集合不带 `shell`，那篇的 `capability_set_versions_shell_check` 不用改。

**对 D21 的明确例外：** 那篇的 D21 和第 15.8 节规定 v0 的受限范围不读本地文件，第 5.1 节的 `files.read.roots` 在 v0 只接受 `chat_attachments`。`local.read_only` 读本地文件，所以它是例外，只对受众 operator 的任务会话成立；它不用 `files.read` 的根，那篇第 3 步定根列表时不受影响。以后的共享受众（10.3）和群里的任何范围都拿不到它，符合那篇的 D8（全能力只给操作者）。第 6 步如果给群里来的任务开话题，答复会被群里的人看到，这条要在那时一起重新看（16.8）。

**操作者 ⇔ `full` 这条规则扩展成一条，三处同时改：**

> 受众是操作者时，范围是 `full`（群和私聊会话），或者 `task_session`（任务会话）；`task_session` 只用在 `kind = task` 的会话上，受众只能是操作者；`kind = task` 的会话，范围只能是 `task_session`。

1. **`Resolved`**（那篇第 6 节的解析结果）：构造检查加上会话种类。`kind is TASK` 时只接受（`operator`, `task_session`）；其他种类照旧，另外拒绝 `task_session`。违反就抛错，消息不结算，按不变量违例告警，和那篇的 D8 一样。**任务会话不走那篇第 6 节的五步解析表**（那张表第 1 步不看会话种类，把操作者一律解析成 `full`，走进去会先得到 `full`、再被这条检查拒绝）：`answerer`（5.3）在确认发送者是操作者之后，直接构造 `Resolved(audience=operator, scope=task_session, kind=TASK)`。
2. **`SessionScope`**（那篇 1b 里会话记下的范围）：同一条检查，在构造时做。
3. **`manager_sessions_scope_audience_check`**，以及 `capability_sets` 的 id 检查、群里能用的那几处范围检查：

```sql
ALTER TABLE capability_sets
    DROP CONSTRAINT capability_sets_id_check,
    -- 以那篇 1b 合入时的内置 id 列表为准，只加 'task_session'
    ADD CONSTRAINT capability_sets_id_check
        CHECK (id IN ('chat_only', 'task_entry', 'readonly_qa', 'full', 'legacy', 'task_session')
               OR id ~ '^set-[a-z][a-z0-9-]{1,30}$');
ALTER TABLE manager_sessions
    DROP CONSTRAINT manager_sessions_scope_audience_check,
    ADD CONSTRAINT manager_sessions_scope_audience_check CHECK (coalesce(CASE
        WHEN kind = 'task' THEN scope_id = 'task_session' AND audience = 'operator'
        ELSE (scope_id IS NULL AND audience IS NULL)
             OR (scope_id IS NOT NULL AND audience IS NOT NULL AND scope_id <> 'task_session'
                 AND (audience = 'operator') = (scope_id = 'full')) END, false));
-- 群永远拿不到 task_session：群默认、建议范围和按人覆盖都排除它
ALTER TABLE manager_chat_bindings
    DROP CONSTRAINT manager_chat_bindings_scope_check,
    ADD CONSTRAINT manager_chat_bindings_scope_check
        CHECK (default_scope IS DISTINCT FROM 'full' AND default_scope IS DISTINCT FROM 'task_session'
               AND (default_scope IS DISTINCT FROM 'legacy' OR legacy_allowed)),
    DROP CONSTRAINT manager_chat_bindings_suggested_check,
    ADD CONSTRAINT manager_chat_bindings_suggested_check
        CHECK (suggested_scope IS NULL OR suggested_scope NOT IN ('full', 'legacy', 'task_session'));
ALTER TABLE binding_principal_scopes
    DROP CONSTRAINT binding_principal_scopes_scope_id_check,
    ADD CONSTRAINT binding_principal_scopes_scope_id_check
        CHECK (scope_id NOT IN ('full', 'legacy', 'task_session'));
```

- `coalesce(…, false)` 是因为 `kind = 'task'` 而范围或受众为空时，`scope_id = 'task_session'` 的结果是 NULL，CHECK 会放行（那篇 5.4 讲过同样的坑）。`ELSE` 分支里先判了非空，不会得出 NULL。
- 约束名按那篇 1b 合入后库里实际的名字核对；`binding_principal_scopes` 那条是列上的内联约束，名字由 PostgreSQL 生成。
- 以后有了共享受众的任务会话，在 `kind = 'task'` 分支里加上它的范围，同样三处一起改。

## 11. 并发与安全

### 11.1 同一个 checkout 的两个写入方

**保证（16.3）：** 同一个 checkout 上，操作者的一次 `work` 会话和任何任务的后台运行不会同时写。两个任务之间仍按 W5 只提醒。`observe` 退出 plan 模式以后写文件，不在这个保证之内：plan 模式不是隔离。任务会话是只读的，不参与。

| 情形 | 处理 |
|---|---|
| 任务进行中（原地或 worktree），操作者 `looper task claude` | 默认 `observe`。显式 `--mode work` 时要排他租约，任务的运行持有共享租约，所以拒绝 `lease_held`，说明持有者，提示停掉任务或等它结束 |
| 操作者以 `work` 持有排他租约，这个 checkout 上有任务要开始、重启、重新打开或在服务启动时恢复 | 不开始，`lease_held`，说明持有者是操作者的 `work` 会话、从什么时候起；操作者退出后再来 |
| 两个任务在同一个 checkout 上 | 只提醒（W5），互不阻挡 |
| 两个终端同时 `looper task claude` 同一个任务 | 第二个拒绝 `already_entered`：一条 Claude Code 会话不能两个进程一起写 |

**单位是 checkout，不是任务：** 租约的键是目录所在 checkout 的真实路径，即 `git rev-parse --show-toplevel` 的结果再取 `realpath`；不在 git 里就是目录本身的 `realpath`。原地任务用 `work_dir` 所在的 checkout；worktree 任务用任务 worktree 的路径（`<runtime_root>/task-worktrees/<task_id>`，还没建时也按这个路径算）。同一个 checkout 的任何子目录、经过符号链接的写法，都落在同一把锁上。`others_active_in` 只比较 `work_dir` 字面相等（`db/tasks.py:96-102`），仍然只用于 W5 的提醒，不参与互斥。

**锁：** 每个 checkout 一个锁文件，文件名是键的 sha256 前 16 位，放在用户级的状态目录 `${XDG_STATE_HOME:-~/.local/state}/looper/checkout-leases/` 下，不放在 runtime root 下：CLI 和服务可能用不同的 runtime root，按主机和用户放，两边才会碰到同一把锁。测试用 `LOOPER_LEASE_DIR` 指到临时目录。`flock` 只在本机有效，Looper 本来就是单机的；这个目录不在本地文件系统上时，`looper task claude` 拒绝（`lease_unsupported`）。

- 任务的运行持有**共享锁**（`LOCK_SH`）：几个任务可以同在一个 checkout，互不阻挡，符合 W5。
- 操作者的 `work` 会话持有**排他锁**（`LOCK_EX`）：挡住所有任务运行和别的 `work` 会话。
- 持有者把自己写进旁边的说明文件：任务 id 和运行号，或者会话 id、Claude Code 的进程号和开始时间。被拒的一方读它来说明是谁；说明文件只用来显示，互斥只看锁。

**顺序：**

- **任务开始：** 包括草稿和就绪的开始、重启、重新打开（TaskRouter 的 `_handle_start`、`_handle_restart`），以及服务启动时的 `resume_tasks`、`start_tasks`（`tiers/task.py:270-300`）。① 算出键；② 非阻塞地拿共享锁，拿不到就不开始，activity 写明持有者，任务状态不动；服务启动时恢复的任务记成 stopped，原因 `operator_session_open`，操作者退出后可以重启；③ 写状态（`pending`，或者恢复）；④ 一直持有，到 `_teardown_task` 写完最终状态才放开。`check_task_control` 和 `start_ready_task`（`projects.py:1214-1261`）另外先试一次，好及早给出拒绝，但它们不是判断点。没有操作者会话时共享锁总能拿到，开始路径的行为不变，只多一个打开的文件描述符。
- **`looper task claude --mode work`：** ① 算出键；② 非阻塞地拿排他锁，拿不到就拒绝 `lease_held`，按说明文件说出持有者；③ 拿到以后才写 `task_operator_sessions` 的行和说明文件；④ 启动 Claude Code。互斥只看锁，所以行先写还是后写都不影响结果，不存在「路由先看到没有行，随后写 pending」的空档。
- **清扫**（9.5）：收尾或删除任何任务 worktree 目录之前，非阻塞地拿这个 checkout 的排他锁，拿不到就跳过这一次。

**进程的生命周期：** 父进程打开锁文件时不设 close-on-exec，Claude Code 启动时继承这个描述符。`flock` 跟着打开的文件描述走，所有引用它的进程都退出后才释放：父进程被 SIGKILL 而 Claude Code 还在写，锁仍然在；两者都退出了，锁才放开。Claude Code 跑在终端的前台进程组里，关终端时两者一起收到信号。Claude Code 用 shell 启动、脱离出去、又没有继承这个描述符的后台进程，不在这个保证之内（同能力范围 RFC 第 7 节的说法）。

- **没有强行接管。** 被拒的一方只能等对方放开。要立刻收回，操作者自己退出会话，或者停掉任务。
- `<task_id>.session.lock`（同一个目录下）：`looper task claude` 在整个会话期间持有，两种方式都持有，只用来拒绝第二个进程（`already_entered`），以及得出 `tasks.detail` 的「现在是否在里面」。`observe` 不持有 checkout 的锁，任务在操作者旁观时结束，照样可以被重新打开。

plan 模式和租约管的是 Looper 这一侧的动作和误操作，不是隔离：操作者的会话和任务的 agent 都以操作者的系统账号运行。

### 11.2 只有操作者

| 入口 | 规则 |
|---|---|
| `looper task *` | 在 Looper 启动的 agent 里拒绝（`LOOPER_AGENT_RUN`，由 `Launch.environment()` 设置，`agent_settings.py:1150`）；调服务时用本机的操作者令牌；`looper task claude` 打开的会话里只能对同一个任务（7.3） |
| v1 操作 | 建草稿、发给任务对话、`tasks.reopen` 都是 `operator_only`，API 客户端调用答 `operator_only`（`http_server.py` 在 `principal.kind != "operator"` 时拒绝） |
| 管家 | `looper_task_control` 的 `reopen` 和 `looper_task_draft` 只在操作者的回合里能用：`ops/tasks.py` 在调用时用 `operator_call(call.turn)`（`ops/service.py:1474`，只调用、签名不变）判断，不靠按轮过滤工具清单（13.2）；执行前先确认；开关关着时工具描述逐字节不变 |
| 任务会话 | v0 只有操作者一个受众。第 6 步里，飞书话题中别人的回复不进任务会话，照今天的群路由走 |
| 能力范围 | 群里的 `task_entry` 等范围不含 `requirement`、`workspace`、`reopen`；以后要给，作为 `tasks.actions` 的新动作显式加 |

### 11.3 注入内容

- 简报、任务会话的输入（含从管家对话转来的开场）、草稿对话交给规划者的摘录、`[REOPEN]` 里别人写的内容，都由 `untrusted.fence()` 围起来，这几处总是打开。
- 在操作者自己的 Claude Code 会话里，简报是数据，操作者说的话才是指令，简报开头写明这一点。

### 11.4 私有内容

- Looper 从不读操作者的 Claude Code 会话记录，也不写操作者的 Claude Code 文件；`looper task claude` 只传启动参数。
- 会话 id 只在本机的表里和 `looper task sessions` 的输出里。
- 交接说明和从管家对话转来的开场，都是操作者看过、主动交出的文字；它们会被这个任务的 agent 看到，界面和命令输出里写明这一点。

## 12. 资源

- **不是一个任务一个常驻进程。** 任务会话在有消息时才启动；闲置 30 分钟回收（只停进程，会话 id 留在 `manager_sessions`，下一条消息用 `--resume` 续上），和能力范围 RFC 15.5 的设置是同一个（`manager.sessions.idle_reclaim_seconds`）。草稿放多久都不占进程。
- **常驻上限：** 任务会话和其他路由会话共用全机常驻上限（那篇 D17 的 `manager.sessions.max_resident_total`，默认 6）。按助理层 RFC 第 4.2 节的实测，每个常驻会话空闲约 240 MB，带子进程树约 310 MB，6 个约 1.5–1.9 GB，不含管家的 main 会话和任务的规划者、执行者、评审者（它们按项目并发另算）。超出上限时排队，状态显示「排队中：其他会话正忙」，按最久没说话的先回收。
- **建议：** 默认 6 不变。同时在谈的任务通常只有几个，按需恢复到首次回复约 1.8–3.4 秒、常驻只省 1–2 秒（同一节的实测），所以不值得为了快一点多留进程；内存紧的机器调到 4（约 1.0–1.25 GB）。
- 任务会话的 `--resume` 永远在同一个工作目录（会话根）里，目录变化用 `--add-dir`，不会因为换目录续不上（5.4）。

## 13. 分步计划

### 13.1 步骤与顺序

| 步 | 内容 | 依赖 | 开关 | 粗估 |
|---|---|---|---|---|
| 0 | #1736：任务列表和任务页上的「和这个任务对话」入口 | 无 | 无，只加入口 | 已在评审 |
| 1 | **最小可用：** 控制台里新建任务 → 它自己的对话 → 谈清楚需求 → 开始 → 在同一处看进度、接着谈 | — | `tasks.conversations.enabled`，默认关 | 11–16 人日 |
| 1.1 | 草稿状态和转换（5.2）、内置项目 `inbox`、建草稿的 v1 操作、草稿的开始检查和删除 | 无，和能力范围 1b 并行 | 同上 | 2–3 人日 |
| 1.2 | 任务会话：`SessionKind.TASK`、`manager_sessions` 迁移、`answerer` 和改投、只读启动和按需回收、输入清单、契约、任务工具、开始时的交接、`task_session` 集合和 10.4 的约束；`resolve_chat_target` 加 `kind: task` | 1.1；能力范围 1b 合入之后 | 同上 | 5–7 人日 |
| 1.3 | 控制台：Chat 页的新布局（第 6 节），对话里穿插活动和关卡卡片，任务面板 | 1.1；接草稿和结束后的对话要 1.2 | 同上 | 4–6 人日 |
| 2 | CLI：`looper task enter`、`ask`（进同一段任务对话）、`claude`（动手会话、简报钩子、checkout 租约）、`brief`、`sessions`，以及 7.4 的解析 | 1.2 | 无：新命令；租约让开始路径多拿一把共享锁，没有操作者会话时总能拿到 | 6–8 人日 |
| 3 | 重新打开：第 9 节的转换、`[REOPEN]` 的控制流、`task_runs`、`task_outcomes.end_commit`、预算、工作区和 `operator` 状态、台账、通知；任务会话的 `task_reopen`、`looper task reopen`、管家工具和控制台按钮；9.8 的逐处核对 | 1.2（入口）；租约来自 2 | `tasks.reopen.enabled`，默认关 | 4–6 人日 |
| 4 | 把管家对话转成任务（5.6）：控制台的「转成任务」和管家的 `looper_task_draft` | 1 | 跟 `tasks.conversations.enabled` | 2–3 人日 |
| 5 | `looper task register` 和交接说明（第 8 节） | 2 | 无 | 3–5 人日 |
| 6 | 飞书一个任务一个话题：映射表、根帖、进度 / 关卡 / 结束回复到话题、入站按话题路由（关卡帖优先）、只认操作者 | 1.2；16.8 先定 | 按 bot 的 `task_threads`，默认关 | 6–10 人日 |

合计约 32–48 人日，不含第 0 步。每一步都能单独评审、单独回滚。

**第 1 步复用的东西：** 规划者的 `[CHAT]` 回合和 planner 目标（`chat_targets.py:52-56`、`operator_chat.send`），所以运行中的对话不用新写；任务页的计划、步骤、Pipeline 数据和关卡面板（`web/src/components/task-gates.tsx`）；`tasks.activity_stream` 和前端的订阅（`task-detail.tsx:306`）；#1736 的任务入口；管家会话的流式、停止、续接、闲置回收和路由会话的根目录；规划者的只读启动；`edit_requirement`、`move_task`、`start_ready_task`。

**试一下（第 1 步）：** 打开控制台的 Chat 页，点「新建任务」，马上进入一个空任务的对话；说出要做的事，它会追问、只读地看一下目录，在旁边的面板里写出需求和目录；说「开始」，规划者接手，计划、步骤和关卡出现在同一段对话里，接着发消息就是在和规划者说。任务完成后回到它，在同一处接着问；第 3 步以后，说「接着做……」就重新打开。

**试一下（第 2 步）：** 在任务的目录里直接运行 `looper task enter`，别处运行 `looper task enter "<标题里的几个字或一个 MR 号>"`，有几个匹配时先挑一个；终端里就是那个任务的对话，和控制台里看到的是同一段。要自己动手改代码时运行 `looper task claude`，进行中的任务处于 plan 模式；在那条会话里用 `looper task ask "…"` 和任务说话。

### 13.2 和能力范围 RFC 第 1b 步的分工

那一步和本文并行开发。会话路由、`manager_sessions` 和 `ops/service.py` 里的授权都归那一步；本文的 1.1、1.3 和那一步同时做，不碰它拥有的代码；本文的 1.2 要改其中几处，所以排在它合入之后、基于合入后的 `main` 开工。本文任何一步都可以调用 `operator_call`，但不改它的签名。

| | 本文的 1.1、1.3 以及第 2（`claude` 部分）、3、5 步拥有 | 能力范围 RFC 第 1b 步拥有（本文的 1.2 合入之前，其他步都不碰） |
|---|---|---|
| 会话路由 | 不碰 | `session_routing` 的 `SessionKind`、`SessionKey`、`session_key_for`、`resident_label`、`routes`；`tiers/manager_routing.py` 的 `SessionRouter.target`、`_user_key`、`session`、`admit`（常驻上限） |
| 管家会话的存储与启动 | 不碰 | `manager_sessions` 表和 `db/manager_sessions.py`；`tiers/manager.py` 的会话启动、`_persist_chat`、`_build_user_message`；`tiers/group_session.py` 的 `opening`、`excerpt`、`brief` |
| 平台工具的授权 | 只调用 `operator_call`（`ops/service.py:1474`），签名不变 | `ops/service.py` 的 `ManagerOps.current_listing`（按轮过滤清单）、`_authorize`、`_operator_turn`、`_confirm` 和两处高风险检查；`ops/__init__.py` 的待确认 |
| 运行时与渠道 | 不碰 | 运行时的启动模式声明；渠道写 `sender_kind` |
| 任务状态与开始 | `db/tasks.py` 的 `STATUS_GROUPS`、`TaskGroup`（`draft`）；`projects` 的 `create_task_core`（草稿）、`check_task_control`、`start_ready_task`、`control_task`、`store_chat_supplement`、`EDITABLE_STATUSES`；`tiers/task.py` 的 `_handle_start`、`_handle_restart`、`_start`、`resume_tasks`、`start_tasks`、`_teardown_task`（租约的拿和放） | 不碰 |
| 规划者 | `tiers/planner.py` 的 `_restart_plan`、`_restart_message`（加 `[REOPEN]`） | 不碰 |
| 管家的任务工具 | `ops/tasks.py` 的 `_control`、`_confirming` 和 `looper_task_control` 的描述（带开关） | 不碰 |
| 工作区 | `task_workspaces` 的 `WorktreeState`、`prepare`（`from_commit`）、`sweep`、`finish`；`db/task_worktrees.py` 的 `list_active`；`task_worktrees` 的 state CHECK | 不碰 |
| 读与操作 | `v1_domains/task_views.py` 的 `tasks.detail`；`v1_domains/task_create.py`（建草稿）；新操作 `tasks.reopen` | 不碰 |
| 界面 | `web/src/pages/chat.tsx` 和它的新组件 | 不碰 |
| 新模块和表 | `cli.py` 的 `task` 命令组；新模块 `task_entry`、`task_select`、`task_brief`、`task_commands`；`task_operator_sessions`、`task_runs` 及迁移 | — |
| 共用的小改动 | `untrusted.Source` 加一个值 `TASK_REQUIREMENT`，只加不改 | 用 `untrusted.fence`，不改它 |

- 本文的 1.2 合入时要改右栏里的 `SessionKind`、`SessionKey`、`manager_sessions`、`SessionRouter.target`，以及 10.4 的 `Resolved`、`SessionScope` 和几条范围约束；`task_session` 集合由 1.2 加，不进那篇 1b。
- 「`reopen` 和 `looper_task_draft` 只给操作者的回合」在调用时判断，不靠按轮过滤工具清单：那篇 1b 要把那段改成按范围取清单。
- 迁移的序号在实现时按 `main` 和开放的 PR 重新核对，两边的迁移互不修改对方的表。

### 13.3 每步的验证（照开发计划第 1 节）

- **1.1：** 5.2 的每条转换和拒绝各一个测试（没需求、没目录、`inbox` 里没给目录、开关关着）；草稿不进 `ACTIVE_STATUSES`、不算并发、服务启动时不恢复；删除草稿时任务会话行和对话线程一起删；新 CHECK 的写入被拒负例；开关关着时 `tasks.create` 和所有现有路径逐字节不变。
- **1.2：** `answerer` 每个分支一个测试；`SessionKey` 的形状检查，10.1 和 10.4 的每条 CHECK 各有一个写入被拒的负例（含 `task` 会话的范围或受众为空、`task_session` 出现在群会话或群绑定上、`task` 会话受众是 `shared`），`Resolved` 和 `SessionScope` 的同一组负例，在一次性 PostgreSQL 上跑迁移，旧行保留；改投最多一次；只读启动的参数冻结（没有写工具，不跳过权限），目录变化后用同一个会话 id 续上；输入清单冻结，哨兵门禁证明输入里没有画像、台账、别的任务，并有本任务的正向对照；工具对别的任务答 `not_found`；开始时交给规划者的摘录由 `fence` 生成；任务会话契约是新的模型可见内容，跑真实管家评测。
- **1.3：** 无头浏览器在 1280、768、390 宽度和 844 × 390 横屏下：新建任务一点就进空对话；草稿谈完开始，进度和关卡出现在同一段对话里；结束后接着问；空、加载、出错、不可用、开关关着几种状态；键盘和焦点；截图附在 PR 里。用全新数据库在非 8767 端口、临时 runtime root 上跑 `looper serve`，配假模型，走完整个流程并读回任务、对话线程和会话行。
- **第 2 步：** 7.4 的解析每条规则一个参数化用例，并确认没有模型调用；`enter` 和控制台进的是同一个会话（同一个键、同一个线程）；租约：任务运行和 `work` 两种先后顺序各一个测试，被拒时说出持有者；同一个 checkout 的子目录和符号链接写法落在同一把锁上；SIGKILL 父进程之后 Claude Code 子进程还在时锁仍被持有；服务启动时恢复撞上排他锁时任务记成 stopped；清扫拿不到锁时跳过；带 `LOOPER_AGENT_RUN` 时拒绝；简报的截断和围栏；开工前先用真实 Claude Code 验证 7.3 标了「需要验证」的几件事，再按 13.1 的「试一下」跑一遍。
- **第 3 步：** 9.2 表每行一个测试；完成的计划收到 `[REOPEN]`（不会被 `_complete_plan` 抢先），`none` 时再次完成，这一轮失败时任务以 `reopen_turn_failed` 失败且续接说明未送达；仍在等的关卡收到「still waits」；定时任务和绑了规则的任务被拒；预检在重置之前，花费读不到时拒绝，新上限只写任务自己的列；台账 note 写失败不中止；`operator` 状态的 worktree 被清扫跳过，重新打开后变回 `active`、未提交的改动还在；T 已删时按 `result_commit` 建回，提交也没了时拒绝，新任务第一次建 worktree 的路径逐字节不变；`task_runs` 和 `task_outcomes` 对得上；9.8 每处一个测试；`[REOPEN]` 跑真实规划者评测；一次性 serve 上完成一个任务、在对话里重新打开、再完成，读回两次运行。
- **第 4 步：** 选中的消息原样进新草稿的开场，原对话不变；管家的 `looper_task_draft` 不确认就不建；跑真实管家评测。
- **第 5 步：** 默认原地，未提交的改动在任务里；显式 worktree 且有未提交改动时拒绝；交接说明出现在规划者的第一轮里。
- **第 6 步：** 开工前先定 16.8，并核实群里的话题回复是否必须 @ bot，以及发送频率和编辑上限。渠道替身上：任务开始时发根帖，进度进话题，操作者的回复进任务会话，别人的回复走群路由，关卡帖的回复仍进关卡。

## 14. 可观测性

**指标**（服务端的进 `manager_metrics.get` 的新字段 `task_conversations`；CLI 侧的计数在 `task_operator_sessions` 里）：

| 指标 | 维度 | 口径 | 用来看什么 |
|---|---|---|---|
| `task_drafts` | 结果（开始、删除、还在） | 每个草稿结束时 | 先建再谈用得多不多，草稿多久变成开始 |
| `task_conversation_turns` | 回答者（规划者、任务会话、排队）、入口、任务状态组 | 每回合 | 各入口和各阶段的用量 |
| `task_session_starts` | 原因（首次、续接、回收后恢复）、运行时 | 每次启动 | 冷启动多不多 |
| `task_session_start_refusals` | 运行时、原因 | 每次拒启 | 告警 |
| 常驻会话数、排队等待时长 | 全机 | 采样 | 第 12 节的上限合不合适 |
| `task_session_reroutes` | 无 | 每次改投 | 应该很少 |
| `task_promotions` | 入口（控制台、管家） | 每次 | 临时的事转成任务有多常见 |
| `task_enter`、`task_claude` | 方式、是否续上、任务状态组 | 每次进入 | CLI 用不用 |
| `task_select` | 结果（唯一、候选、没找到）、选择的种类（省略、文字、MR、id） | 每次解析 | 解析规则是否够用 |
| `task_lease_refusals` | 被拒的一方、持有者 | 每次 | 两个写入方撞上的频率 |
| `task_reopens`、`task_reopen_refusals` | 原来的状态、入口；原因 | 每次 | 续接替代了多少新建 |
| 重新打开后的结果 | `task_outcomes` 里 `run_no > 1` 的行按结果分 | 已有表 | 重新打开有没有用 |
| 不变量违例 | 无 | 非操作者的消息进了任务会话；一条消息改投两次以上；任务会话的启动参数里有写工具或跳过权限；`observe` 启动的参数里没有 plan 模式 | 应恒为 0 |

**告警**（经 `operator_dm_target` 发到操作者私聊）：不变量违例，严重，每次都发；任务会话因运行时不支持而拒启，高，按（管家, 运行时）每小时一次；有消息排队超过 2 分钟，低，只上指标页。

**演练：** 1.2 的测试里用一个不支持只读启动的假运行时触发拒启，读回计数和私聊；第 2 步的测试里人为占住租约再开始任务，读回拒绝原因和计数。

## 15. 风险

| 风险 | 应对 |
|---|---|
| 两条上下文，规划者不知道草稿里谈过什么细节 | 开始时由代码交接：确认过的需求加草稿对话的摘录；结束后任务会话读简报和线程摘录 |
| 草稿越积越多 | 不占进程、不占并发；列表按组折叠，草稿可一步删除；只计数不提醒 |
| 任务会话多了占内存 | 不常驻，按需恢复，全机上限加排队（第 12 节） |
| 操作者以为 plan 模式是隔离 | 简报和 `looper task claude` 的输出都写明这是防误改；真正挡住的是 Looper 这一侧的开始和重新打开（租约） |
| Claude Code 参数的行为随版本变化 | 开工前验证并写进测试；版本和验证时不同时只提醒，照常启动，因为这是操作者自己的会话 |
| 重新打开让「完成」不再可信 | 每次运行在 `task_runs` 和 `task_outcomes` 里各一行；控制台显示第几次运行；9.8 逐处核对 |
| 累计上限挡住重新打开 | 拒绝时写出已花费和上限；在命令里给新上限，走现有的确认 |
| 为操作者建回的 worktree 一直留着 | 只在 `work` 进入时建回；离开时打印路径和未提交的文件数；重新打开时交还给任务，删除任务时照删除的规则收尾 |
| 飞书话题的平台限制 | 第 6 步开工前核实，默认关 |
| 交接说明或转来的开场里带了私人内容 | 由操作者选、操作者看过才交；它们会被任务的 agent 看到，界面和输出里写明 |

## 16. 已定的问题

下面每条写成本版本的决定（16.8 除外，它留到第 6 步开工前）。各项默认值以后都可以改。

### 16.1 规划者活着时规划者答，其余时候任务会话答

**决定：** 两个角色，一段对话（5.3 的 B）：任务有活着的规划者时由规划者答，草稿、就绪和结束后由任务所属管家的任务会话答。

**理由：** 操作者始终在任务里说、在任务里得到回答。运行中由最懂这个任务的规划者直接答；其余时候由做对话的那一层答，流式、停止、续接都是现成的。让规划者从草稿起一直答，要把它改成也能闲聊的角色，还要在任务开始前起规划者进程；规划者会话的只读分叉每问一次都是新分叉，前后接不上，所以只当工具。以后可以改，例如等规划者的答复也能流式、停止之后，再评估让它覆盖草稿阶段。

### 16.2 重新打开不通知原来的来源聊天

**决定：** 重新打开的运行只把进度和结束告诉发起它的地方，默认不通知原来的来源聊天（9.6）。

**理由：** 来源聊天是当初提需求的地方，可能是一个群；重新打开是操作者自己的后续，往那里补发通知，别人会以为又来了一件新事。以后可以改，例如按任务加一个「也通知来源」的选项。

### 16.3 动手会话：进行中的任务默认 `observe`，`work` 要显式并先拿租约

**决定：** `looper task claude` 对进行中的任务默认 `observe`；`work` 要显式 `--mode work`，并且先拿这个 checkout 的排他租约；任务的运行持有共享租约，所以被拒绝并说明是谁持有，不能强行接管（7.3、11.1）。保证的范围：同一个 checkout 上，操作者的一次 `work` 和任何任务的后台运行不会同时写；两个任务之间照 W5 只提醒；`observe` 退出 plan 模式后写文件不在保证之内。

**理由：** 任务的 agent 正在这个目录里写，两个写入方会互相覆盖。按 checkout 的真实路径加锁，「现在操作者和任务谁在写」只有一个答案，被拒的一方也知道在等谁；锁跟着 Claude Code 进程走，父进程被杀掉也不会放开。不做强行接管，因为接管就是在 agent 写到一半时把目录抢过来。以后可以改，例如加一个「先停任务再以 `work` 进入」的组合命令。

### 16.4 完成任务的 worktree 按需建回

**决定：** 完成的 worktree 任务不自动重建 worktree；以 `work` 进入或重新打开时才建回（9.5）。

**理由：** 大多数完成的任务不会再回来，自动重建既占磁盘，又和启动清扫冲突。按需建回只在有人真要写的时候发生：T 还在时提交都在；T 被删了，按记下的 `result_commit` 建回，合并过的提交因此都在；只存在于已删的 T 上、又被 git 回收了的提交找不回来，这时拒绝并说明，不假装能建回。以后可以改，例如 `observe` 也建回一份只读的检出。

### 16.5 登记已有目录默认原地

**决定：** `looper task register` 默认 `in_place`：目录留在原处，Looper 挂到它上面（第 8 节）。

**理由：** 登记的是正在进行的工作，未提交的改动就在这个目录里；worktree 看不到它们，后台 agent 会在旧代码上做。以后可以改，例如目录干净时建议 worktree。

### 16.6 任务会话可以只读这个任务的目录

**决定：** 受众是操作者的任务会话用规划者的只读启动，能读这个任务的目录（能力项 `local.read_only`）；别的受众都不行。这是对能力范围 RFC 的 D21 和第 15.8 节的明确例外。任务会话的范围直接构造成（`operator`, `task_session`），不走那篇第 6 节的解析表（10.4）。

**理由：** 谈需求和结束后的问题，大多要看代码和改动。不改文件、只给操作者，不扩大任何别人的能力，符合那篇的 D8；D21 管的是群里的受众能借管家读到什么，这条例外不给群里任何人新的读权限。用规划者现成的只读启动，不用等那篇第 3 步的受限只读。以后可以改，例如受限只读验证过以后，把读的范围收窄到这个目录。

### 16.7 上限按任务累计，到了上限要给新上限才能重新打开

**决定：** 花费上限按任务累计。已经到上限时，重新打开必须在命令里给出新的上限，否则拒绝；新上限只写这个任务（9.4）。

**理由：** 一件事的总花费才是要守住的数，按次计算会让「多开几次」绕过上限；要求在命令里给新上限，任务就不会在没人点头时接着花。以后可以改，例如允许每次运行另设一个小上限。

### 16.8 未定：飞书话题

哪些任务开话题、群里谁能加入任务的对话，第 6 步开工前再定，本版本不定。到时一起定的还有：群里来的任务如果开话题，16.6 的读权限要不要收窄，因为答复群里的人看得到；共享受众的键形状已经在 10.3 留好。

### 16.9 不做：把 CLI 会话分叉给规划者

**决定：** 不做，列为以后的探索（第 8 节）。

**理由：** 两边的协议不同；会话记录里的私人内容会流进 agent 的输出；启动参数不同；worktree 任务的目录也不同。以后探索时，先在一次性目录里验证这样续接能不能成功、规划者在这样的会话里能不能守住计划协议，再决定要不要做。

### 16.10 不用任务 id 也能找到任务

**决定：** `enter`、`ask`、`claude`、`brief` 接受三种写法：省略（按所在目录或打开的会话，否则列出最近的任务）、几个字或一个 MR、任务 id。解析是确定性的代码，不调用模型；只有恰好一个匹配时才直接进入，几个就列出来挑，没有就明说（7.4）。

**理由：** 回到旧任务时，人说的几乎总是几个字或一个 MR 号，不是任务 id。只有唯一匹配才自动进入，因为进错任务比多挑一次代价大；不用模型，结果可复现、可测试，也不花钱。14 天、20 个、90 天这几个数以后都可以改。

### 16.11 草稿放在 `inbox` 项目里，不放宽 `project_id`

**决定：** 没给项目的草稿放进内置项目 `inbox`（「未归类」，没有默认目录）；离开草稿前要有目录（5.2）。

**理由：** 让 `tasks.project_id` 可以为空，所有 `JOIN projects` 的查询都要改成外连接，草稿还会从按项目的列表里消失；一个内置项目只多一行数据，任务的其他规则全都不变。以后可以改，例如按最近用过的项目给草稿一个默认值。

## 17. 默认行为与用户入口

**默认行为：** `tasks.conversations.enabled`、`tasks.reopen.enabled`、`task_threads` 都默认关。关着时：Chat 页、管家的工具描述、建任务和开始的路径、渠道路由都和今天逐字节相同；没有草稿，也没有任务会话。第 2 步的命令是新命令，不运行就什么都不变；它带来的共享锁让开始路径多一个打开的文件描述符，没有操作者会话时总能拿到。

**用户入口：**

- **控制台：** 第 1 步起，Chat 页左边是「新建任务」、置顶的管家和按项目分组的任务，中间是对话，旁边是任务面板（第 6 节）；第 3 步起，结束的任务在对话里或面板里重新打开；第 4 步起，管家对话里能把一段话转成任务。
- **CLI：** 第 2 步起 `looper task enter / ask / claude / brief / sessions`，第 3 步起 `reopen`，第 5 步起 `register`。`loopercli` 的命令不变；新的 v1 操作生成后它也会列出来，但只有操作者能调用。
- **飞书：** 第 3 步起，管家可以按操作者的话重新打开任务，先确认；第 4 步起，管家可以提议把一段对话转成任务；第 6 步起一个任务一个话题，给哪些任务开在那一步开工前定（16.8）。
- **Telegram：** 第 3、4 步的管家路径同飞书；话题不在本文范围，以后单独定。
