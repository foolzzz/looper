# RFC：工作模式——任务对话与多任务控制台（v0）

- **状态：** 提案，只有文档，不含实现。产品方向由维护者定（第 1 节）：任务是第一位的对象；先建任务，再在任务里说；临时的事留给主管家；界面做在现有的网页控制台里。本文按代码核对这个方向，第 5 节写核对的结论，做不到或代价太大的地方以本文为准。第 4 节的决定和第 21 节的已定问题都标明了以后可以改。第 1、2 步写到可以直接切任务卡的程度（第 8、9 节）。
- **相关：** [群聊里的能力范围 RFC](chat-scopes-v0.zh-CN.md)（第 7 节会话键、第 16 节任务维度）；[助理层 RFC](assistant-layer-v0.zh-CN.md) 第 4.2 节（常驻进程的内存）；[任务的工作区 RFC](task-workspaces-v0.zh-CN.md)（W1、W2、W3、W5、W6、W7）；[规划者只读 RFC](planner-read-only-v0.zh-CN.md)；[定时任务共用一条规划者会话 RFC](schedule-planner-session-v0.zh-CN.md)；[用户关卡 RFC](user-gate-v0.zh-CN.md)；[agent room RFC](agent-room-v0.zh-CN.md)；[管家台账 RFC](manager-completion-ledger-v0.zh-CN.md)；已合入的 #1736（第 0 步）。
- **代码基线：** `main` `e574e70dd`。初稿按 `20ed06508` 写成，其后被引用的文件里只有 `projects.py`、`cli.py`、`db/projects.py` 和 `tiers/manager.py`（第 1555 行之后，本文引用的行都在它前面）有改动；所有行号都按 `e574e70dd` 核对过。出处路径相对 `looper/agents/`，`schema.sql`、`db/…` 在 `looper/db/` 下，`web/…`、`docs/…` 相对仓库根。本文没有启动服务，没有调用模型，文中的 SQL 没有在数据库上执行过。规划者回合时长、排队和续接的结论来自一份只读的实际部署统计，具体数字留在维护者处，本文只写它们推出的设计约束。Claude Code 的参数只在 2.1.332 的 `claude --help` 里确认存在，行为凡是没有现成代码证明的都标了「需要验证」。

## 1. 出发点：两种模式

| | 工作模式 | 远程模式 |
|---|---|---|
| 场景 | 在电脑前 | 不在电脑前 |
| 入口 | 控制台的新界面，其次是命令行 | 飞书 |
| 用法 | 几个任务并行，每个任务一段可以随时切换、隔几天还能接着说的对话。知道是新任务，就先建、再在任务里说问题；临时的事不建任务，直接交给主管家 | 收通知、回关卡和确认、发短指令、看状态（晨报、`/todo`、`/status`、提醒）、交待一件临时的事。不在飞书里并行处理多个任务 |
| 现状 | 缺得最多，是每天的主要用法 | 已有的能力大多在这边 |

目标：在电脑前用 Looper 处理几件事，要和同时开几个编码 CLI 会话一样顺手，同时保留 Looper 的后台执行、评审和记忆。

**由此定下的优先级：**

1. 工作模式的最小第一步最先做（第 8、9 节）。
2. 「飞书里一个任务一个话题」排到最后，并重新评估要不要：远程模式也许只要轻的一层，即回复某个任务的通知就是对这个任务说话（第 12 节）。
3. 远程模式已经上线的能力（投递层、晨报、「需要你处理」、状态卡、台账）照原计划做完，本文不改它们的角色。
4. 两种模式之间的衔接：飞书里关于某个任务的一句话，落进那个任务的对话，回到电脑前在任务里看得到（第 12 节）。
5. 主管家自己的改进（系统提示词用 append 模式、给管家装 skill）优先级更低，不在本文范围。

**界面做在现有的 `web/` 控制台里**，现在不做桌面应用。给以后的桌面外壳留余地，但不为它多做任何东西：新界面只用现有的 HTTP API 和流式通道，除了浏览器不依赖别的；路由和状态在单独的窗口里也能用；每个任务的对话有自己的网址，可以直接打开。桌面外壳是以后的待办，本文不设计。

**不做：** 另造「工作会话」对象；把主管家的那一条对话拆成多条；用模型猜「说的是哪个任务」（解析是确定性的代码，第 11.4 节）；把操作者的 CLI 会话记录交给 Looper 的 agent；给操作者在 agent room 里设座位；OS 级隔离。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 | 出处 |
|---|---|---|
| 网页对话线程 | 只是显示分组：网页、API 客户端和操作者私聊都进主管家的 main 会话，模型看得到别的线程里说过的话；只有 `/new` 换上下文，而且是整体重开 | `web/src/pages/chat.tsx:284-286`、`:1301`；`session_routing.py:46`、`:79-80` |
| 流式 | 管家的答复按 `answer.delta` 分段；规划者的 `[CHAT]` 答复只有一条最终消息 | `channels/web.py:327-339`；`tiers/planner.py:4538-4546` |
| 常驻命令 | 规划者和管家的常驻 Claude 都带 `--include-partial-messages`，部分输出本来就流过来 | `runtimes/claude_code.py:410-430` |
| 建任务 | `tasks.create` 和 `looper_task_create` 的需求必填，建出来是就绪（存的状态是 `init`），要另外开始；管家的规矩是先澄清、确认后才建 | `v1_domains/task_create.py:58-73`、`projects.py:560-574`；`tiers/manager.py:266`、`:319`、`:338` |
| 规划者的生命周期 | TaskRouter 在任务开始时才建规划者（`_assign_task`）；`[PLAN]` 前停掉进程，按「新计划是一段新对话」开新会话，只有定时任务会借用存下的会话；规划者在任务的 `work_dir` 里跑 | `tiers/task.py:397-417`；`tiers/planner.py:1303-1334`、`:7111-7138`、`:5398` |
| 规划者的回合 | 一个规划者同一时间只跑一个回合，事件按到达顺序处理；等关卡时闲置 15 分钟停掉常驻进程，下一轮续接 | `docs/design.md` 的「Web」一节；`tiers/planner.py:287`、`:2620-2640` |
| 和规划者对话 | `operator_chat.send` 的 planner 目标只对活着的规划者；规划者对已结束的任务回固定文字 | `chat_targets.py:52-56`；`tiers/planner.py:4532-4536` |
| 并发 | 按项目限制的只有执行者和评审者，规划者没有上限 | `agent_settings.py:177` |
| 只读启动 | 规划者的运行去掉写文件的工具，权限模式钉成 manual 且没有人答提示 | `claude_cli.py:355-365`、`runtimes/claude_code.py:177-178` |
| 会话续接 | 规划者的会话存在 `agent_sessions` 的 `planner-<task_id>`；续不上（运行时或 provider 变了、会话找不到）时开新会话并带 `[RESUME]` 简报；Claude Code 的会话按工作目录保存 | `schema.sql:392-410`、`tiers/planner.py:5523-5526`、`:5825`；`consult.py:9-17` |
| 花费上限 | 任务的上限按全部运行累计；运行中读不到花费时放行；到上限时停掉任务、记为失败 | `tiers/planner.py:6851-6887` |
| 费用拦截 | 给任务挂费用拦截时，把所有不是终态的任务改成 `awaiting_budget` | `db/cost_holds.py:21-22` |
| 任务列表接口 | `tasks.page` 按创建时间倒序、每页最多 200，花费是批量读的（没有逐个任务的查询），但没有「最近动态」「等你」「未看」这几项；Chat 页只列最近 30 个任务 | `v1_domains/task_views.py:146-163`、`projects.py:62`、`:773-840`、`db/tasks.py:714-750`；`chat_targets.py:21-22` |
| 页面路由 | 控制台用 hash 路由，页面在 `PAGES` 里登记，Chat 页切走时保持挂载 | `web/src/App.tsx:25`、`:47-94`、`:156-160` |
| 可复用的前端 | 输入框 `ChatComposer`；任务页的实时动态 `LiveActivityFeed`、需求 `TaskRequirement`；关卡面板 `task-gates.tsx`；花费 `task-cost.tsx`；Markdown；对话页里的流式处理在 `ChatPage` 内部 | `web/src/components/chat-composer.tsx`、`task-gates.tsx`、`task-cost.tsx`；`web/src/pages/task-detail.tsx:284`、`:482`；`chat.tsx:1000-1110` |
| 常驻进程的内存 | 每个常驻会话空闲约 240 MB，带子进程树约 310 MB | 助理层 RFC 第 4.2 节 |

## 3. 方案取舍

**一件事用什么对象表示：** 任务本身（采用）。另造「工作会话」会让任务、会话、群会话三种概念并存；把主管家的对话拆成多条，解决不了「先建再谈」和「结束后接着做」。

**任务里和操作者说话的是谁：**

| | A. 任务的规划者全程（采用） | B. 另一个对话角色，规划者活着时交给规划者 | C. 主管家带着这个任务的上下文来答 |
|---|---|---|---|
| 转话 | 没有：操作者的话直接到规划者 | 运行中没有；草稿和结束后由另一个角色答，开始时要交接 | 有 |
| 上下文 | 一条会话贯穿草稿、规划、运行、结束（worktree 任务在开始时换一次会话，带交接，第 5.4 节） | 两条 | 主管家的上下文是全局的，隔离不干净 |
| 要改的 | 规划者多一种不跑计划的「对话」生命周期、两种新回合、流式（第 5 节） | 一种新的会话种类和能力范围（要动能力范围 RFC 拥有的代码） | 主管家的会话路由 |
| 和能力范围 RFC 的关系 | 不碰会话路由和 `manager_sessions` | 要扩展它的范围模型和约束 | 要改它的路由 |

**采用 A。** 核对结果（第 5 节）：A 做得到；代价是规划者在决策回合里时，操作者的话要排队，以及一部分续接会落到新会话。这两点都有缓解，没有推翻 A。A 还有一个代码上的好处：任务对话完全在规划者这一侧，不碰能力范围 RFC 正在改的会话路由（第 17 节）。

**界面怎么上线：** 新界面作为新页面 `#work`，和现在的 Chat 页并存，默认入口不变；用顺了再替换（第 21.3 节）。

## 4. 决定一览

| # | 决定 | 落点 |
|---|---|---|
| D1 | 任务是第一位的对象。每个任务从建出来就有一段任务对话，地址 `task:<task_id>`；主管家的临时对话仍是 main 那一条，不拆 | 第 5、6 节 |
| D2 | 任务里和操作者说话的是这个任务的规划者，全程同一个角色（做法 A） | 第 5 节、21.1 |
| D3 | 新状态 `draft`（草稿）：可以没有需求，规划者在对话里澄清、只读查证；不开始执行、不占执行并发、不过期、可以删。「待启动」复用今天的 `init`：需求写成了文字，等操作者点头 | 第 6 节 |
| D4 | 开始前默认确认一次需求：规划者把对话整理成需求，操作者点「开始」或改一句；在草稿里直接说「直接开始」跳过这一步。开始是操作者的动作（按钮、或代码识别的几个固定说法），规划者自己不能开始任务 | 6.2、21.2 |
| D5 | 草稿和结束后的规划者是「对话规划者」：有消息时才起，闲置 30 分钟回收，用 `--resume` 续上；全机同时活着的对话规划者默认最多 6 个，超出时回收最久没说话的；运行中的规划者不在这个上限里 | 第 7 节 |
| D6 | 草稿和结束后的对话回合是这个任务的运行，花费算进任务的上限；到了上限只拒绝这一轮、不让任务失败 | 第 7 节 |
| D7 | 规划者的对话回答分段出现；它在等别的回合时，界面写明它在做什么、这句话排第几 | 5.2、5.3 |
| D8 | 结束后接着问，就是回到这个任务的对话：规划者续原会话回答；要接着做时它提出「重新打开」，操作者确认后走类型化的 `reopen` 转换 | 第 10 节 |
| D9 | 控制台的新页面 `#work`：左边管家（置顶）和全部任务，中间对话（含活动和关卡），右边任务信息；和 Chat 页并存 | 第 9 节、21.3 |
| D10 | 任务列表是全部任务，不是最近 30 个；切换一次点击、不刷新页面、不丢未发的输入；有键盘快速切换；每行显示状态、等不等你、有没有没看过的进展，「看过」按操作者存在服务端 | 9.3–9.5 |
| D11 | `looper task enter` 在任务目录里打开操作者自己的 Claude Code 会话，带任务简报；在里面用 `looper task ask` 和规划者说话，问答写进同一段任务对话；动手要先拿 checkout 租约 | 第 11 节 |
| D12 | 主管家对话里的一段可以转成草稿任务；飞书里回复任务的通知，这句话落进那个任务的对话 | 第 12 节 |
| D13 | 不改能力范围 RFC 拥有的代码：任务对话不用 `manager_sessions`，不加会话种类，不加能力集合 | 第 17 节 |
| D14 | 所有新的写操作只给操作者 | 第 16 节 |
| D15 | 开关：`tasks.conversations.enabled`（第 1、2、4、5 步）、`tasks.reopen.enabled`（第 3 步），都默认关；第 6 步的各项各自带开关 | 第 18 节 |

## 5. 核对做法 A

### 5.1 草稿里能不能有规划者

能，但要加一种生命周期。今天的规划者只在任务运行时存在：TaskRouter 在开始时建它（`tiers/task.py:397-417`），它的第一轮就是 `[PLAN]`，而且 `[PLAN]` 之前会停掉进程、开一条新会话（`tiers/planner.py:1303-1334`）。要加的：

| 要加的 | 做法 | 先例 |
|---|---|---|
| 不跑计划的规划者 | TaskRouter 多一张表 `_conversations`，放草稿、就绪和已结束任务的规划者；它们不写任务状态、不派步骤，只答对话 | `_planners`（`tiers/task.py:417`）只放运行中的 |
| 两种新回合 | `[DRAFT]`（草稿和就绪）和 `[AFTER]`（结束后），答复是 CHAT 的形状，允许的动作各自固定（6.2、10.1） | `[CHAT]`、`[RESTART]` 都是 CHAT 形状（`tiers/planner_protocol.py:120-140`、`:800`） |
| 草稿的任务说明 | 系统提示词的任务一节在没有需求时写明「需求还没写成，先和操作者谈清楚」 | `plan_task_section`（`system_prompt.py:278`） |
| `[PLAN]` 接着草稿的会话 | 任务有草稿会话时不清空，`[PLAN]` 在同一条会话里接着说 | 定时任务借用存下的会话（`_claim_schedule_session`，`tiers/planner.py:7111-7138`） |
| 只读 | 不用改：规划者本来就是只读启动 | `claude_cli.py:355-365` |

**目录：** 规划者在 `work_dir` 里跑（`tiers/planner.py:5398`）。草稿建出来时目录就要能定下来：给了目录就用它，否则用项目的默认目录（项目没有默认目录、又配了项目根时，照今天的做法分配一个，`v1_domains/task_create.py:103-105`）；都定不下来就拒绝建（`no_directory`）。原地任务从草稿到运行一直在同一个目录。worktree 任务的 worktree 照 W2 在开始时才建，所以草稿的规划者在工作区 W 里跑，开始后 `work_dir` 换成 worktree 里的位置，见 5.4。

### 5.2 排队

规划者一次只跑一个回合。按实际部署的统计，决策回合和规划回合常常要几十秒，长的要几分钟；规划者处在回合中的时间大约占任务时长的五分之一到四分之一；遇到它正忙时，平均还要再等将近一分钟。所以运行中的任务里，操作者的话大多数时候马上被处理，少数时候要等一个回合。今天经管家转给规划者的补充说明，送到规划者手里的时间更长，所以 A 至少不比今天差。

**缓解：**

- 界面写明它在做什么、这句话排第几（代码写的，不靠模型）：「规划者正在做第 3 步的决定（已 25 秒），你的消息排第 1」。
- 回答分段出现（5.3），第一段文字比整轮回答早得多。
- 第 6 步接入「回合中并入」：运行中的规划者回合里，把操作者的话作为下一个用户轮写进正在跑的进程，和执行者的引导是同一个机制（`WorkerPool.send_input`，引导执行者 RFC）。
- 如果实测排队过长，再考虑拆成两个角色（做法 B）；这条记在第 20 节的风险里。

### 5.3 回答能不能分段

能。规划者的常驻命令已经带 `--include-partial-messages`（`runtimes/claude_code.py:419`），部分输出本来就流过来，只是规划者没转发。难点是 CHAT 的答复是一个 JSON：

- 契约要求 `"reply"` 是 JSON 的第一个键（今天的示例本来就这样写，`tiers/planner_protocol.py` 的 CHAT 示例）。
- 规划者边收边解：看到 `{"reply": "` 之后，把字符串里已经完整的字符解码后，作为 `STATUS_UPDATE` 的 delta 发给提问的一方，网页渠道照今天的做法转成 `answer.delta`（`channels/web.py:327-331`）；字符串结束就停。
- 模型把别的键放在前面时不分段，只在结束时给完整答复，不报错。
- 只对来自对话的回合分段（`[DRAFT]`、`[AFTER]`、`[CHAT]`）；决策、规划等回合不发 delta。

### 5.4 结束后能不能续上原会话

大多数情况能，续不上时有现成的退路：

| 情况 | 结果 |
|---|---|
| 原地任务，运行时和 provider 没变 | 在同一个目录里 `--resume` 原会话 |
| worktree 任务，失败、停止、取消（worktree 保留着） | 同上 |
| worktree 任务，完成（worktree 已删） | 第一句话进来时，按 10.3 的规则在原路径建回 worktree（状态 `kept`），再续原会话 |
| 运行时或 provider 变了，或者会话找不到 | 现有的退路：开新会话，第一轮带 `[RESUME]` 简报（`tiers/planner.py:5523-5526`） |
| 草稿是 worktree 任务，开始时目录变了 | 对话规划者在停下之前跑一轮现有的 `[HANDOFF]`（至多 3000 字，`tiers/planner.py:5986`），交接存进 `agent_sessions.pending_handoff`（`schema.sql:405`），`[PLAN]` 在 worktree 里的新会话里带上它 |

- 实际部署的统计里，重启后有一部分续接落到了新会话，退路照常工作。任务对话因此不承诺「一定是同一条会话」，只承诺「一定接得上」：续不上时，规划者从简报和这段对话的摘录接着说，界面写明「换了一条新会话」。
- 需要验证：Claude Code 换了工作目录后 `--resume` 能不能找到原会话；Claude Code 自己会不会清理很久以前的会话记录。两者都只影响「是同一条会话」还是「走退路」，不影响能不能接着说。

### 5.5 规划者在对话里自己做什么

只读的查证（读代码、看日志、跑不改东西的命令）规划者在这一轮里自己做完，直接回答；要改东西、要长时间跑的，排成步骤交给执行者，并在对话里说排了什么。草稿阶段只能查证和澄清，不能派步骤：派步骤要先开始任务。

## 6. 任务的生命周期

### 6.1 状态

| 状态 | 存的值 | 含义 | 规划者 | 占执行并发 |
|---|---|---|---|---|
| 草稿 | `draft`（新） | 刚建，需求还没写成，在对话里谈 | 对话规划者，按需 | 否 |
| 待启动 | `init`（复用） | 需求已经写成文字，等操作者点头 | 对话规划者，按需（有草稿会话时） | 否 |
| 进行中、等你 | 今天的那些 | 规划、执行、评审在跑；或卡在关卡上 | 运行中的规划者 | 是（执行者、评审者） |
| 已结束 | `completed`、`failed`、`stopped`、`cancelled` | 结束了，可以接着问、可以重新打开 | 对话规划者，按需 | 否 |

`init` 能复用：它今天的意思就是「有需求、没开始过、只能由用户开始」（`db/tasks.py:21-23`）。有没有草稿会话只是 `agent_sessions` 里有没有一行，不是另一种状态。`draft` 加进 `STATUS_GROUPS`（`db/tasks.py:39-59`），新组 `TaskGroup.DRAFT`，不进 `ACTIVE_STATUSES`。

### 6.2 转换

```
新建（控制台、转成任务、管家建草稿）               → draft
draft ──规划者写下需求（[DRAFT] 的 requirement 动作）──→ init
draft ──「直接开始」──→ 规划者写下需求的一轮 → pending
init  ──「开始」（按钮、/start、或固定说法）──→ pending     （今天的 start_ready_task）
init  ──规划者改需求（[DRAFT] 的 requirement 动作）──→ init  （需求加一版）
draft、init ──删除──→ （删除，连同对话和会话）
进行中 ──结束──→ 已结束                                  （不变）
已结束 ──reopen（第 10 节）──→ pending
```

- **开始只由操作者触发。** 三种方式：任务面板的「开始」按钮；对话里发 `/start`；对话里一句话正好是「开始」「直接开始」「start」之一（去掉首尾空白、不分大小写），由 Looper 自己处理，不交给模型（同关卡的 `END_WORDS`，`gate_routing.py:45`）。规划者的 `[DRAFT]` 动作里没有「开始」。
- **「直接开始」：** 在草稿里发 `/start` 或说「直接开始」，Looper 先让规划者跑一轮只写需求的 `[DRAFT]`（强制 `requirement` 动作），写成后直接开始，不停在待启动。这一轮失败就留在草稿，告诉操作者。
- **开始的检查**（`start_ready_task`，`projects.py:1214`，草稿和 `init` 走同一条 `TASK_START`）：需求非空（`not_requirement`）；目录能定下来（`no_directory`）；开关开着；拿到 checkout 的共享租约（16.1）。
- **需求的版本：** 规划者每次写需求都是 `edit_requirement` 的同一条路径，记一版（`task_requirement_versions`，`schema.sql:519-527`）；操作者也可以在任务面板里直接改。
- **标题：** 可以空着。规划者在第一轮 `[DRAFT]` 的答复里给一个 `title`（至多 80 字），任务还没有标题时代码写进去；操作者随时可以改名。
- **删除：** 草稿和待启动可以删，走现有的删除任务；对话线程的消息和 `agent_sessions` 的那一行一起删。

### 6.3 依赖终态和「就绪」的地方

加 `draft`、让「完成」可以重新打开，下面这些地方要逐处改或确认，各一个测试：

| 位置 | 今天的假设 | 要做的 |
|---|---|---|
| `db/cost_holds.py:21-22` | 不是终态的任务都可以改成 `awaiting_budget` | 排除 `draft` 和 `init`：对话回合到了拦截只拒绝这一轮（第 7 节） |
| `db/cost_guard.py:24` | 不是终态的任务的运行都算进窗口 | 草稿的对话回合本来就该算，确认不重复 |
| `projects.py:1288-1291` | `EDITABLE_STATUSES` 是就绪和进行中；注释说「只有完成任务的 agent 是最终的」 | 加 `draft`；重新打开后按进行中处理 |
| `ops/tasks.py:547-555` | 就绪的任务不收补充说明 | 草稿和就绪的话进对话，不当补充说明 |
| `scheduler.py:125`、`db/schedules.py:305` | 「忙」= 进行中加就绪 | 定时任务不产生草稿，确认不受影响 |
| `db/user_gates.py:278-288` | 等你的关卡只看进行中、`completed_at` 为空的任务 | 重新打开时清掉 `completed_at`（`reset_task_for_restart` 已经这样做），确认关卡又能收回复 |
| `gate_routing.py:16` | 任务完成后，回复关卡帖交给管家 | 重新打开后不再是完成，关卡照常收回复 |
| `task_notice.py:154-155`、`completion_events.py:328-351` | 结束通知每次结束发一次 | 重新打开的运行按 10.4 只通知发起的地方 |
| `memory_retro_service.py:70`、`db/memory_retro.py:19` | 完成和失败的任务做记忆复盘 | 第二次结束按游标只看新的部分（`memory_retro_cursors` 已经按任务记游标） |
| `db/room.py:185-186` | 清扫按 `completed_at` 留最近的房间 | 重新打开清掉 `completed_at`，确认房间不被清掉 |
| `intent_rule_engine.py:765-783` | 规则的执行看任务是否结束 | 绑了规则的任务 v0 不能重新打开（10.2） |
| `task_workspaces.py:402-461` | 完成就提交并删 worktree；清扫按 `active` 认目录 | 新状态 `kept`（10.3） |
| `tiers/task.py:794-828` | 结束收尾只跑一次 | 按运行号再跑一次；`db.tasks._task_ended`（`db/tasks.py:542`）只通知 listener，确认不重复记结果 |
| `chat_targets.py:122`、`chat.tsx:1243` | 结束的任务只读 | 旧 Chat 页不变；新页面按第 9 节 |
| Tasks 页、侧栏的计数、晨报、「需要你处理」 | 按状态分组计数 | `draft` 单独成组，不算进行中；晨报和「需要你处理」不提草稿（超过 14 天没动的草稿在「需要你处理」里提一次） |

## 7. 预算、并发与资源

- **花费：** 对话回合（`[DRAFT]`、`[AFTER]`、运行中的 `[CHAT]`）都是这个任务的运行，记在 `llm_records` 上，花费算进任务的上限（任务自己的 `budget_usd`，没有就用项目的 `task_budget_usd`，`db/tasks.py:126-128`）。
- **到了上限：** 对话回合之前用和 `_within_budget` 同一个谓词（抽成共用函数）算一次。已经到上限时不跑这一轮，Looper 回一句「这个任务已到花费上限 $C（已花费 $S），提高上限后再说」，任务状态不变，不像运行中那样停掉任务、记为失败（`tiers/planner.py:6851-6887`）。费用拦截照样生效，但不改草稿和待启动的状态（6.3）。
- **执行并发：** 草稿、待启动和已结束的任务不派步骤，所以不占执行者和评审者的并发（`agent_settings.py:177`）。
- **对话规划者的上限：** 新设置 `tasks.conversations.max_resident`，默认 6：全机同时活着的对话规划者最多 6 个，超出时停掉最久没说话的那个（会话 id 留着，下一句话续上）。闲置 30 分钟回收（`tasks.conversations.idle_seconds`，沿用规划者等关卡时停进程的做法，`tiers/planner.py:287`、`:2620-2640`）。运行中的规划者不在这个上限里，照今天的生命周期。这个上限和管家会话的常驻上限是两个设置：管家那个归能力范围 RFC。
- **内存：** 按助理层 RFC 第 4.2 节的实测，每个常驻进程空闲约 240 MB，带子进程树约 310 MB，6 个约 1.5–1.9 GB。按需恢复到第一次回答只比常驻慢 1–2 秒，所以不值得多留进程；内存紧的机器调到 4。

## 8. 第 1 步：草稿和规划者对话

**做完能做什么：** 新建一个空任务，在里面和规划者把事情说清楚，再开始；运行中接着说；结束后回来接着问。界面先用最小的入口（第 9 节的页面可以并行做，第 1 步合入后接上草稿）。

**卡片：**

| 卡 | 内容 | 输入 → 输出 | 文件 | 测试 | 粗估 |
|---|---|---|---|---|---|
| 1.1 草稿状态 | `draft` 和 `TaskGroup.DRAFT`；`tasks.create` 加 `draft: true`，此时需求可以为空、标题可以为空、项目必选、目录要能定下来；迁移加 CHECK `status = 'draft' OR btrim(requirement) <> ''`（先 `NOT VALID` 再验证）；删除草稿；6.3 表里的状态检查 | `{project_id, title?, work_dir?, workspace_mode?, draft: true}` → `{task_id, status: "draft"}`；错误 `no_directory` | `db/tasks.py`、`projects.py`（`create_task_core`、`EDITABLE_STATUSES`）、`v1_domains/task_create.py`、`db/cost_holds.py`、迁移 | 每条转换和拒绝；草稿不进 `ACTIVE_STATUSES`、不算并发、服务启动时不恢复；CHECK 的写入被拒负例；开关关着时现有路径逐字节不变 | 1.5–2 |
| 1.2 对话规划者 | TaskRouter 的 `_conversations`；有消息时建并启动，闲置回收，全机上限和最久未用先停；工作目录和 5.4 的各种续接；已结束任务的规划者加载已有的计划只读 | 一条发给任务的消息 → 对话规划者处理；进程数不超过上限 | `tiers/task.py`、`tiers/planner.py`（对话模式） | 起、停、回收、上限；四种续接情况各一个；运行中和对话两张表不交叉 | 2–3 |
| 1.3 两种新回合 | `TurnKind.DRAFT`、`TurnKind.AFTER`；契约（新的模型可见内容）；解析：`[DRAFT]` 的动作 `none`、`requirement{text}`，可选 `title`；`[AFTER]` 的动作 `none`（第 3 步再加 `propose_reopen`）；`requirement` 写一版需求并把草稿改成 `init` | 操作者的话 → `{"reply", "action", ...}` → 需求和状态的变化 | `tiers/planner_protocol.py`、`tiers/planner.py`、`system_prompt.py` | 解析的正例和负例；`requirement` 的版本和状态；`[AFTER]` 不能派步骤 | 1.5–2 |
| 1.4 开始 | 6.2 的三种开始方式；「直接开始」的一轮；`[PLAN]` 接着草稿的会话（`_plan_and_dispatch` 不清空有草稿会话的任务）；worktree 草稿开始前的 `[HANDOFF]` | `/start`、按钮或固定说法 → `pending` → `[PLAN]` 在同一条会话里 | `projects.py`（`start_ready_task`）、`tiers/planner.py`、`chat_commands.py` | 固定说法只认整句；规划者的答复里没有开始；原地草稿的 `[PLAN]` 用同一个会话 id；worktree 草稿带交接 | 1–1.5 |
| 1.5 分段和排队 | 5.3 的分段；5.2 的「在做什么、排第几」；对话回合的预算检查（第 7 节） | 部分输出 → `answer.delta`；排队时一行状态 | `tiers/planner.py`、`channels/web.py`（不改，复用 delta） | 「reply」在前时分段、在后时只给最终答复；到上限时拒绝这一轮、任务状态不变 | 1–1.5 |
| 1.6 任务对话的地址 | `operator_chat.send` 的目标加 `kind: task`：有运行中的规划者就交给它，否则交给对话规划者（按需起），任务在 `pending`、`assigned` 时存成补充说明并回一句「正在启动」；消息和答复都存进对话线程 `task-chat:<task_id>`（`chat_threads` 归这个任务的管家，`chat_messages` 多一列 `entry`） | `{target: {kind: "task", task_id}, text}` → `correlation_id`，`reply_events` 流式 | `chat_targets.py`、`v1_domains/operator_chat_streams.py`、`db/chat.py` | 每种任务状态的去向；只有操作者能发；旧的 planner 目标不变 | 1 |
| 1.7 验证 | 规划者契约改动的真实评测（开发计划第 1 节）；全新数据库、非 8767 端口、临时 runtime root 上跑 `looper serve`，配假模型走完「建草稿 → 谈 → 写需求 → 开始 → 运行中说话 → 结束后接着问」 | — | `evals/` | 读回任务、对话线程、会话行、花费 | 1 |

合计 9–12 人日。

**验收：** 开关打开后，`tasks.create` 带 `draft: true` 能建出没有需求的任务；对它发消息由它的规划者分段回答，只读查证有效；规划者写下需求后任务变成待启动；操作者开始后，`[PLAN]` 和草稿是同一条会话（原地任务），worktree 任务带上交接；运行中发的话由运行中的规划者回答；结束后发的话由对话规划者回答；全机对话规划者不超过上限；到了上限的对话回合被拒绝而任务不失败；开关关着时一切和今天相同。

## 9. 第 2 步：多任务控制台

### 9.1 页面

新页面 `#work`，在 `App.tsx` 的 `PAGES` 里登记（`web/src/App.tsx:25`），和 Chat 页并存；侧栏多一项「Work」，默认入口不变。地址：`#work`（默认选中主管家）、`#work/manager`、`#work/task/<task_id>`，在单独的窗口里直接打开也能用。照 `docs/design.md` 的「界面设计」一节：令牌和 `components/ui` 组件、一处主要强调、状态同时有文字或图标。

| 区域 | 内容 |
|---|---|
| 左栏 | 最上面「新建任务」（页面上唯一的主色按钮）和搜索框；下面置顶「管家」；再下面是任务列表（9.3） |
| 中间 | 选中项的对话：分段出现的回答、「在做什么」一行；任务对话里按时间穿插关键活动（阶段变化、步骤开始和结束、关卡打开和关闭、任务结束）和关卡卡片（可以直接回复）；底部输入框（暂存、历史、Stop 沿用今天的 `ChatComposer`） |
| 右栏 | 选中任务时：需求（带版本，草稿和待启动时可以改）、「开始」（草稿和待启动时，缺什么写明）、计划和各步骤的状态、关卡、改动的文件、花费；选中管家时：「需要你处理」的清单 |

- **新建任务：** 点按钮或按快捷键（`N`，焦点不在输入框时），立刻在上一次用过的项目里建一个草稿（上一次的项目记在这个浏览器里），选中它，光标落在输入框里；项目在右栏可以改。
- **管家：** 选中「管家」时，中间是今天的管家对话视图，一处不改。
- **窄屏：** 宽屏三栏；中等宽度两栏，右栏成为对话上方的一个标签；窄屏单列，列表 → 对话（返回按钮），任务信息是对话里的一个标签。`pointer-coarse` 下交互目标不小于 44 px。

### 9.2 复用什么

| 现有的 | 怎么用 |
|---|---|
| `ChatComposer`（`web/src/components/chat-composer.tsx`） | 原样用，每个任务各一份未发的输入 |
| `ChatPage` 里的流式处理（`chat.tsx:1000-1110`） | 抽成一个钩子 `useReplyStream`，旧页面和新页面共用 |
| `LiveActivityFeed`（`task-detail.tsx:284`） | 抽成组件，按时间和对话合并 |
| `TaskRequirement`（`task-detail.tsx:482`）、`task-gates.tsx`、`task-cost.tsx`、`markdown.tsx` | 原样或小改后放进右栏和关卡卡片 |
| 管家对话视图 | 选中「管家」时整块嵌入 |
| `tasks.detail` | 右栏的数据，切换时在后台取 |

三个现有页面合计约 5,400 行，新页面不改它们的行为，只把上面几块抽出来共用。

### 9.3 任务列表（验收）

- **全部任务。** 列表里是全部任务，不是最近 30 个（Chat 页的上限，`chat_targets.py:21-22`）。默认视图之外的任务分页加载，每个任务，包括已结束的，都能从列表里找到并进入。
- **默认视图：** 所有没结束的（草稿、待启动、进行中、等你），按最近动态倒序，按项目分组。已结束的在一个可以折叠的分区里，最新的在前，可以按项目和状态筛选，向下滚动时分页加载。定时任务产生的任务和 Tasks 页一样默认收在「定时任务」筛选后面，打开筛选就能看到。
- **每一行一眼看得出：** 状态（文字和图标）；等不等你（打开的、等你回复的关卡数，或状态是等你）；有没有没看过的进展（圆点）。
- **「看过」存在哪：** 服务端新表 `task_seen(principal, task_id, seen_activity_seq, seen_message_id, seen_at)`，按操作者存（`principal` 是操作者的身份，今天只有一个操作者，留出这一列）。打开一个任务的对话时，前端把当时最新的 activity 序号和对话消息 id 写进去；「没看过」= 这个任务的关键活动或对话消息比记下的新。控制台和 CLI 共用这份记录。
- **列表接口：** 新的 v1 操作 `tasks.work_list`（只读）。

```
tasks.work_list({section: "open" | "ended", project_id?, statuses?, include_scheduled?, cursor?, limit<=100})
→ {tasks: [{task_id, title, requirement_head, project_id, project_name, status, group,
            last_activity_at, waiting_gates, needs_you, unseen, run_no, created_at, ended_at}],
   next_cursor, counts}
```

  - `open`：没结束的任务，按 `last_activity_at` 倒序，一次全给（这一组通常很小），超过 `limit` 才分页。`last_activity_at` = 这个任务最新一条 activity 或对话消息的时间，用 `idx_activity_task`（`schema.sql`）和对话线程的索引各取一行（`LATERAL … ORDER BY … LIMIT 1`）。
  - `ended`：已结束的任务，按结束时间倒序，游标分页。加一个索引 `idx_tasks_ended ON tasks (COALESCE(completed_at, attempt_ended_at, updated_at) DESC, id) WHERE status IN ('completed','failed','stopped','cancelled')`。
  - `waiting_gates` 用已有的部分索引 `idx_task_user_gates_waiting`（`schema.sql:914`）一次按任务 id 列表聚合；`unseen` 和 `task_seen` 连接；项目名在同一条查询里。整页一条查询加至多两条按 id 列表的批量查询，没有逐个任务的读取。今天的 `tasks.page` 也没有逐个任务的读取，但它按创建时间排序、没有这几个字段，所以另开一个操作，不改它。
- **快速切换：** `Ctrl/Cmd-K` 打开，输入几个字按标题、需求文字和 MR 号过滤（规则同 11.4，在服务端用同一个函数，新的只读操作 `tasks.select`，CLI 也用它），↑↓ 选，Enter 打开。

### 9.4 切换（验收）

- 点一次就切换，不刷新页面，后台照常跑。
- 未发的输入从不丢：每个任务记着自己的未发输入（这个浏览器的 `sessionStorage`，和今天的暂存区一样）和滚动位置；回来时停在离开的位置，上面一条分隔线「你离开之后」。
- 切换时先用缓存的对话立刻画出来，再在后台补新消息和右栏。

### 9.5 性能（验收）

在一个灌了 2,000 个任务（其中九成已结束）、每个任务有几十条活动的一次性数据库上，用无头浏览器量：

| 指标 | 目标 |
|---|---|
| 打开 `#work` 到列表第一屏可以操作 | 1 秒以内 |
| 点一个任务到它的对话可以输入 | 300 毫秒以内（有缓存时）；第一次打开 800 毫秒以内 |
| `tasks.work_list` 的 `open` 分区（服务端） | 100 毫秒以内 |
| 快速切换的过滤（服务端） | 150 毫秒以内 |

做不到时在 PR 里写明量到的数和原因，不放宽验收。

### 9.6 卡片

| 卡 | 内容 | 文件 | 测试 | 粗估 |
|---|---|---|---|---|
| 2.1 列表和「看过」接口 | `tasks.work_list`、`task_seen` 表和 `tasks.mark_seen`、`idx_tasks_ended` | `v1_domains/`（新文件）、`db/tasks.py`、迁移 | 字段、排序、游标、计数；整页的查询次数固定；`unseen` 按操作者 | 1.5–2 |
| 2.2 快速切换的解析 | `tasks.select`（11.4 的规则，服务端） | `task_select.py`（新）、`v1_domains/` | 每条规则一个参数化用例；没有模型调用 | 1 |
| 2.3 页面骨架和左栏 | `#work` 路由、侧栏项、任务列表（分组、折叠、分页、筛选）、新建任务、搜索框、快速切换 | `web/src/pages/work.tsx`（新）、`web/src/components/work-*.tsx`（新）、`App.tsx`、`sidebar.tsx` | 组件测试：全部任务可达、默认视图、未看圆点、键盘 | 2–2.5 |
| 2.4 中间的对话 | 抽出 `useReplyStream`；任务对话（`kind: task` 目标）；活动和关卡卡片穿插；未发输入和滚动位置；「你离开之后」 | `chat.tsx`（只抽出，不改行为）、`task-detail.tsx`（只抽出 `LiveActivityFeed`）、新组件 | 旧 Chat 页的测试照常通过；切换不丢输入 | 2–3 |
| 2.5 右栏 | 需求、开始、步骤、关卡、改动的文件、花费；改动的文件用新的只读操作 `tasks.changes`（worktree 任务是基点到分支，原地任务是计划的 `start_commit` 到 HEAD，`git diff --name-status`，有条数上限，沿用 `step_workspaces` 的 git 运行器，`step_workspaces.py:282`） | 新组件、`v1_domains/`、`task_workspaces.py` | 每种状态下右栏显示什么 | 1.5–2 |
| 2.6 验证 | 9.5 的性能；1280、768、390 宽度和 844 × 390 横屏的截图；空、加载、出错、开关关着几种状态；键盘和焦点 | — | — | 1–1.5 |

合计 9–12 人日。第 2 步和第 1 步并行：2.1、2.2、2.3 不依赖第 1 步；2.4 先对着现有的 planner 目标做运行中的任务，1.6 合入后改用 `kind: task` 接上草稿和结束后的对话。

**验收：** 9.3、9.4、9.5 的每一条；开关关着时 `#work` 不出现，Chat 页和今天逐字节相同。

## 10. 第 3 步：结束后接着做

### 10.1 在对话里重新打开

- 结束后操作者在任务对话里说话，对话规划者用 `[AFTER]` 回答（第 1 步）。第 3 步给 `[AFTER]` 加一个动作 `propose_reopen{text}`：规划者判断操作者要的是接着做，就把要做的事写成一段续接说明。
- 对话里出现一张卡片「按这段重新打开」（可以改），操作者点确认、或发 `/reopen`，才走下面的转换。规划者自己不能重新打开。
- 控制台右栏的「重新打开」、`looper task reopen` 走同一条路。

### 10.2 转换

`RestartMode.REOPEN = "reopen"`，由 `check_task_control(task_id, "reopen")` 检查：

| 当前状态或条件 | 结果 |
|---|---|
| 草稿、待启动 | 拒绝 `not_started`，用开始 |
| 进行中、等你 | 拒绝 `still_running`，直接在对话里说 |
| 失败、停止、取消、完成 | 允许 |
| 没有目录 | 拒绝 `no_directory`（同 `restart`，`projects.py:1257-1258`） |
| 定时任务产生的任务（`tasks.schedule_id` 不为空） | 拒绝 `scheduled_task`：它的规划者用的是定时任务共用的会话（`tiers/planner.py:7111-7138`），重新打开会把内容写进下一次定时运行还要接着用的会话；要追问，改定时任务的需求或立即运行一次 |
| 绑了意图规则的任务（`config.intent_rule_bound`） | 拒绝 `rule_bound`：开始路径处理规则执行的租约（`projects.py:1214-1238`），重置不碰那些行，v0 不定这算不算那次执行 |
| checkout 的排他租约在操作者手里 | 拒绝 `lease_held`，说明持有者（16.1） |
| 已经到上限、没给新上限 | 拒绝 `budget_exhausted`（10.5） |
| 任务有上限、花费读不到 | 拒绝 `budget_unavailable`（10.5） |
| worktree 任务的分支 T 正检出在别处 | 拒绝 `branch_checked_out`（10.3） |
| worktree 任务的 T 和 `result_commit` 都没了 | 拒绝 `branch_gone`（10.3） |

**执行顺序：**

1. `check_task_control`：上表每一行，包括预算预检和分支检查；任何一行不过，什么都不写。
2. 一个事务：给了新上限就写进这个任务的 `tasks.budget_usd`；续接说明存成补充说明（`via = reopen`，`from_operator = true`，未送达）。
3. 发 `TASK_RESTART`，`mode = reopen`，带上补充说明的 id、入口、回复路由和 CLI 的会话标签。
4. TaskRouter 的 `_handle_restart` 只在这个模式下多接受 `completed`（`tiers/task.py:742-746`）。写状态之前再算一次预算、拿 checkout 的共享租约；不过就不开始，activity 写明原因，补充说明留着。都过了：停掉这个任务的对话规划者（会话 id 留着）；`close_attempts` 只关当前 `run_no` 上没裁决的尝试；一个事务里 `reset_task_for_restart(keep_steps=True)` 把 `run_no` 加一，并写 `task_runs` 的一行（10.4）；发 `TASK_REQUEST`，带 `restart = reopen` 和结束时的状态。
5. 台账：任务有台账条目、台账也开着时，第 4 条提交之后写一条 `note`，`actor_kind = user`，`via` 是入口；入口是 CLI 时 `session_label` 必填（`schema.sql:1831-1832`），在 `looper task enter` 的会话里是 `task-session:<task_id>`，否则是 `cli:task-reopen`。写失败只记日志和计数，不中止。

**规划者那边**走 `_restart_plan`（`tiers/planner.py:4392-4492`），`REOPEN` 改三处：等待中的关卡那一段（`:4439-4451`）原样保留，照样发「still waits」；`:4458-4460` 在计划已完成时直接 `_complete_plan` 并返回，这一句只对 `continue` 生效，`reopen` 跳过它，接着发 `[REOPEN]`；`[REOPEN]` 答 `none` 时先标送达，计划仍完成就再完成，这一轮失败时计划已完成的任务以 `reopen_turn_failed` 失败、续接说明不标送达。`[REOPEN]` 的开头对完成的任务用自己的一句，不复用 `_ended`（`:4496-4500` 只有三种说法）。消息里有代码算出的「结束以后目录里发生了什么」：worktree 任务是 T 上 `result_commit` 之后的提交，原地任务是结束时的 HEAD（新列 `task_outcomes.end_commit`）之后的提交和未提交的文件。规划者续自己的会话，续不上走 5.4 的退路。

### 10.3 工作区

`prepare`（`task_workspaces.py:324-385`）今天：行是 `active` 时沿用，目录丢了走 `_restore`（`:387-400`），分支没了就报错；没有行或行是 `removed` 时，先算基点（`:346`），分支在就 `worktree add <路径> T`（`:353`），不在就 `worktree add -b T <路径> <基点>`（`:355`）。最后这一支也是新任务第一次建 worktree 的路径，不改。

| 情况 | 做法 |
|---|---|
| 原地 | 同一个目录，W5 照常提醒 |
| worktree，失败、停止、取消 | 照今天沿用 |
| worktree，完成，T 还在 | 照今天按 T 建回 |
| worktree，完成，T 已删 | `reopen` 和对话规划者传 `from_commit = result_commit`（`schema.sql:510`）：这个提交还在就 `worktree add -b T <路径> <result_commit>`；不在了就拒绝 `branch_gone`。只存在于已删的 T 上、又被 git 回收的提交找不回来 |
| worktree，完成，T 正检出在别处 | 拒绝 `branch_checked_out`，说明在哪，不替操作者切分支 |

**新状态 `kept`：** 为对话规划者或操作者的动手会话建回的 worktree（5.4、11.2）。要改：`task_worktrees.state` 的 CHECK（`schema.sql:509`）和 `WorktreeState`（`task_workspaces.py:63-65`）加 `kept`；启动清扫只认 `list_active`（`db/task_worktrees.py:40-47`），改成也认 `kept`，干净的 `kept` 目录（没有未提交的改动）在启动时照完成的做法删掉，有改动的留着，清扫前先非阻塞地试 checkout 的排他锁，拿不到就跳过；`finish` 对不是 `active` 的行直接返回（`:413`），加一条删除任务的分支，`kept` 的行照 `active` 的做法先提交再删；对话规划者被回收时，干净的 `kept` 目录一起删掉；重新打开时 `kept` 改回 `active`，未提交的改动交给任务。

### 10.4 通知与 `task_runs`

- 重新打开的运行，进度和结束只告诉发起它的地方：任务对话里发起的就在任务对话里；管家在某个聊天里发起的回到那个聊天。原来的来源聊天默认不再收到（21.5）。
- 新表 `task_runs(task_id, run_no >= 2, reason ∈ {restart, start_over, at_cap, reopen}, via, ended_status, supplement_id, reply_route, created_at)`，主键（任务, `run_no`），`reason = reopen` 当且仅当有续接说明。每条重启路径都在 `reset_task_for_restart` 的事务里写一行，和 `task_outcomes` 按运行号对上。现有的 `restart` 不写 `reply_route`，通知照今天。

### 10.5 预算

- 上限按任务累计，重新打开不重置。预检和 `_within_budget` 用同一个谓词，在 `check_task_control` 和 TaskRouter 重置之前各算一次。
- 已经到上限时，命令里必须给新上限（高于已花费），否则拒绝 `budget_exhausted`；任务有上限而花费读不到时拒绝 `budget_unavailable`，和运行中读不到时放行不同，因为重新打开是一个「再花钱」的决定。
- 新上限只写这个任务的 `tasks.budget_usd`，从不写项目的上限（不像 `tasks.continue` 在任务没有自己的上限时提高项目的，`v1_domains/task_configuration.py:61-70`）；确认文案说的是这个任务。

**粗估** 4–6 人日。**验证：** 上表每行一个测试；完成的计划收到 `[REOPEN]`；关卡的「still waits」；`kept` 的清扫和删除；`task_runs` 和 `task_outcomes` 对得上；6.3 表各一个测试；`[REOPEN]` 跑真实规划者评测；一次性 serve 上「完成 → 在对话里接着问 → 重新打开 → 再完成」。

## 11. 第 4 步：命令行

### 11.1 命令

```
looper task enter  [<选择>] [--all] [--mode observe|work] [--new] [--claude PATH]
looper task ask    [--task <选择>] <text> [--timeout SECONDS]
looper task brief  [<选择>] [--all] [--full] [--hook]
looper task sessions [<task_id>]
looper task reopen <task_id> <text> [--budget-usd N] [--yes]                          # 第 3 步
looper task register --dir PATH --requirement TEXT [--handoff-file FILE] [--session ID] ...   # 第 7 步
```

- 都带 `--runtime-root`、`--config`，`--claude` 的默认值同 `looper claude-code`（`cli.py:122`）。退出码是这组命令自己的：0 完成，2 用法，3 列出了候选、什么都没做，4 拒绝，5 没有这个任务，6 数据库不可用，7 找不到 Claude Code（和 `looper ledger` 不同，那里的 3 是「台账没开」，`ledger_commands.py:8`）。
- 在 Looper 启动的 agent 里（`LOOPER_AGENT_RUN`）一律拒绝（同 `ledger_commands.py:152-161`）；调服务时用本机的操作者令牌。

### 11.2 `looper task enter`

在任务目录里打开操作者自己的 Claude Code 会话，带任务简报；离开再进来续上同一条会话。

| 任务 | 目录 | 默认方式 | `work` |
|---|---|---|---|
| 草稿、待启动 | `work_dir` | `work` | 先拿租约 |
| 进行中、等你 | `work_dir` | `observe`（`--permission-mode plan`） | 要显式 `--mode work`；共享租约在任务的运行手里，拒绝 `lease_held` 并说明持有者 |
| 失败、停止、取消；原地任务完成 | `work_dir` | `work` | 先拿租约 |
| worktree 任务完成 | `observe`：工作区 W，简报写明结果在分支 T；`work`：拿到租约后按 10.3 建回（`kept`） | `work` | 先拿租约 |

- **会话：** 第一次 `claude --session-id <新 uuid> --name "<标题> · <task_id>" --settings <钩子>`；以后 `claude --resume <id>`；续不上提示 `--new`。会话 id 存在新表 `task_operator_sessions`（任务 id、会话 id、目录、上次方式、上次离开时的 activity 和对话游标），只在本机，`looper task sessions` 列出来，不进 API 和页面。父进程不 `exec`，退出后推进游标；`work` 的租约描述符由 Claude Code 继承（16.1）。
- **简报：** `task_brief.compose(task_id)` 由代码拼出：头（任务、状态、第几次运行、目录和分支、方式）、需求、补充说明、计划、进度、等你的关卡、「上次离开以后」、怎么用 `ask`；别人写的内容由 `untrusted.fence()` 围起来，总是打开；总长上限 6000 字符，截断写明，`--full` 打印全文。
- **钩子：** 每次启动用 `--settings` 传一个只属于这次启动的 SessionStart 钩子（`looper task brief <id> --hook`），不写用户设置；`startup`、`clear`、`compact` 给完整简报，`resume` 再加「上次离开以后」。`--hook` 时失败不写 stderr、退出 0，在 agent 里不输出（同 `claude_code_entry.py:69-73`）；不带 `--hook` 时原因打到 stderr（`:74-78`）。需要验证：`--settings` 里的钩子和用户设置里的钩子是否都执行、`source` 的取值、`additionalContext` 的上限；不行就退回 `--append-system-prompt-file`。

### 11.3 `looper task ask`

把一句话发进任务对话（`kind: task` 目标，1.6），等规划者的答复打印出来（默认等 300 秒，超时只说还没答），问答在任务对话里，控制台看得到，消息记入口 `cli`。在 `enter` 打开的会话里（环境变量 `LOOPER_TASK_SESSION`）不用指明任务，也只接受这一个任务。

### 11.4 不用任务 id 找到任务

`enter`、`ask --task`、`brief` 和控制台的快速切换用同一个确定性的解析（服务端的 `tasks.select`，第 2 步），结果是一个任务、一张候选表或没找到，从不调用模型：

| 选择 | 规则 |
|---|---|
| 任务 id（`task-` 加 12 位十六进制，`projects.py:619`） | 就是这个任务 |
| 省略 | 先看 `LOOPER_TASK_SESSION`；否则取 `cwd` 的真实路径，找 `work_dir`、`workspace_dir`、活着的或 `kept` 的任务 worktree 等于它或包含它的任务，只留最深的一层；剩一个就是它，几个时恰好一个进行中就取它，否则列出来；都没有就列出最近 14 天有动态的任务（含草稿），最多 20 个 |
| 一个 MR | 整个选择是一个 MR：`/pull/<N>` 或 `/merge_requests/<N>` 的链接（带仓库），或 `!N`、`#N`、`MR N`、`PR N`、1 到 7 位纯数字；和任务等待步骤的 `github_pr` 检查（`waits.py:228-244`）以及标题、需求、原话、补充说明里认出的引用比较；任务文本里的纯数字不算引用 |
| 别的文字 | NFKC、转小写、空白合一、去两头的引号；按空格切词，每个词都要是标题或最新需求的字面子串（同 `db/tasks.py:690-691`） |

范围最近 90 天（`--all` 全部）；排序先看匹配的档次，再看是否进行中，再按最近动态；只有恰好一个匹配才直接进入，几个就列出（最多 10 行），没有就明说查了什么。

**粗估** 4–6 人日（解析已在第 2 步）。**验证：** 11.2 表每行一个测试；租约的先后顺序、子目录和符号链接、父进程被杀；钩子的几种 `source`；开工前用真实 Claude Code 验证上面标了「需要验证」的几件事。

## 12. 第 5 步：两种模式之间的衔接

- **转成任务：** 主管家对话里每条消息有「转成任务」：选起止两条，生成一个草稿任务（项目默认上一次用的），选中的消息由代码复制进它的对话作为开场（带标注：操作者的话标为操作者的，管家的回复围起来），然后切到这个任务；原对话不动。主管家判断一件事值得建任务时，开关开着时建的也是草稿（新工具 `looper_task_draft`，建之前要操作者确认，同今天的规矩，`tiers/manager.py:338`），回一个链接，不再在主对话里连问一串。这改了管家的模型可见内容，跑真实管家评测。
- **飞书回复落进任务对话：** Looper 发到飞书的任务通知（结束、关卡、进度、重新打开的结果）记下消息 id 和任务（新表 `task_notice_posts`，做法同 `task_user_gate_posts`，`schema.sql:953-962`）。操作者回复其中一条时，先按关卡帖的规则（`gate_routing.py`），不是关卡帖就按这张表找到任务，这句话交给 1.6 的任务对话：规划者在任务对话里回答，答复同时回到这条飞书消息下面。群里只认操作者本人的回复，别人的回复照今天交给管家。管家在私聊里被点名某个任务时，照今天用 `looper_task_instruct`；这条补充说明也作为一条消息记进那个任务的对话线程（入口 `lark`），回到电脑前看得到。
- **粗估** 4–6 人日（转成任务 2–3，飞书回复 2–3）。

## 13. 第 6 步：过程看得见、能打断

- 工具调用显示一行可以展开的记录（做了什么、结果摘要）：网页渠道多一种事件，带工具名、参数摘要和结果摘要，由代码截断，不放原始输出。
- 对话回合能停止：对话规划者的回合可以停，运行中规划者的回合不停（停在决策中间会留下半截计划），只撤回排队的消息。
- 回合中并入：运行中规划者的 `[CHAT]` 回合里，操作者的新话作为下一个用户轮写进进程（5.2）。
- 各项各自带开关，默认关。粗估 4–6 人日。

## 14. 第 7 步：把已有目录登记成任务

`looper task register` 默认原地：目录留在原处，Looper 挂上去。交过去的只有需求、操作者在自己会话里写好并看过的交接说明（存成 `via = handoff` 的补充说明）、目录和它的 git 状态；`--session` 的会话 id 记成这个任务的操作者会话，之后 `looper task enter` 续上它。不交会话记录、用户级设置和工具。显式选 worktree 而目录有未提交的改动时拒绝（`uncommitted_in_worktree`）。把 CLI 会话分叉给规划者不做，列为以后的探索（21.7）。粗估 3–5 人日。

## 15. 第 8 步：飞书话题（待定）

做完第 5 步再评估。远程模式不在飞书里并行处理多个任务，第 5 步的「回复通知就是对任务说话」也许就够了。真要做时，哪些任务开话题、群里谁能加入，在那一步开工前定。

## 16. 并发与安全

### 16.1 同一个 checkout 的两个写入方

- **保证：** 同一个 checkout 上，操作者的一次 `work` 会话和任何任务的后台运行不会同时写。两个任务之间照 W5 只提醒。`observe` 退出 plan 模式后写文件不在保证之内。对话规划者是只读的，不参与。
- **单位：** 目录所在 checkout 的真实路径（`git rev-parse --show-toplevel` 再取 `realpath`；不在 git 里就是目录本身）；worktree 任务用任务 worktree 的路径，还没建时也按这个路径算。子目录和符号链接都落在同一把锁上。`others_active_in`（`db/tasks.py:96-102`）只做字面相等，仍只用于 W5 的提醒。
- **锁：** 每个 checkout 一个锁文件（键的 sha256 前 16 位），放在用户级状态目录 `${XDG_STATE_HOME:-~/.local/state}/looper/checkout-leases/`，不放在 runtime root 下，这样 CLI 和服务用不同的 runtime root 也碰得到同一把锁；测试用 `LOOPER_LEASE_DIR`。不在本地文件系统上时拒绝（`lease_unsupported`）。任务的运行持共享锁，几个任务可以同在一个 checkout；`work` 会话持排他锁。持有者写进旁边的说明文件，被拒的一方据此说明是谁；互斥只看锁。
- **顺序：** 任务开始（草稿和待启动的开始、重启、重新打开、服务启动时的 `resume_tasks` 和 `start_tasks`，`tiers/task.py:270-300`）在写状态之前非阻塞地拿共享锁，拿不到就不开始（服务启动时恢复的任务记成 stopped，原因 `operator_session_open`），一直持有到 `_teardown_task` 写完最终状态。`check_task_control` 和 `start_ready_task` 另外先试一次，及早拒绝。`work` 先拿排他锁，再写行和说明文件，再启动 Claude Code。清扫删目录之前也试排他锁。
- **进程：** 锁文件描述符不设 close-on-exec，由 Claude Code 继承；所有引用它的进程都退出后锁才释放，所以父进程被杀而 Claude Code 还在写时锁仍在。脱离进程树又没继承描述符的后台进程不在保证内。
- **没有强行接管。** 被拒的一方只能等对方放开。

### 16.2 只有操作者

- 任务对话只有操作者能进：`kind: task` 的发送、建草稿、`tasks.reopen`、`tasks.work_list`、`tasks.mark_seen` 都是 `operator_only`；API 客户端调用答 `operator_only`。群里的人通过场景管家接触任务，走能力范围 RFC 的规则。
- 管家的 `looper_task_draft` 和 `reopen` 动作只在操作者的回合里能用，`ops/tasks.py` 在调用时用 `operator_call(call.turn)`（`ops/service.py:1474`，只调用、签名不变）判断，不靠按轮过滤工具清单。
- 飞书里回复任务通知，只认操作者本人；`looper task *` 在 Looper 启动的 agent 里拒绝。

### 16.3 注入内容

- 简报、转成任务的开场、`[REOPEN]` 里别人写的内容，由 `untrusted.fence()` 围起来，这几处总是打开。
- **不跟随设计稿的一处：** 设计稿说任务对话带常驻画像。本文不给规划者注入操作者画像：规划者的话会写进步骤说明、room 和通知，被执行者、评审者看到；规划者的输入保持只有这个任务的内容。要用画像里的偏好，操作者在对话里说，或者以后单独设计一个按项给规划者的白名单。

### 16.4 私有内容

Looper 从不读操作者的 Claude Code 会话记录，也不写操作者的 Claude Code 文件；会话 id 只在本机；交接说明和转来的开场是操作者看过、主动交出的文字，界面和命令输出写明它们会被任务的 agent 看到。

## 17. 和能力范围 RFC 的分工

做法 A 让任务对话完全在规划者这一侧：地址 `task:<task_id>` 由 1.6 的路由直接交给这个任务的规划者，存的是规划者自己的 `agent_sessions` 行，不是管家会话。所以：

- 本文**不改**能力范围 RFC 第 1b 步拥有的任何代码：`session_routing`（`SessionKind`、`SessionKey`、`session_key_for`、`resident_label`、`routes`）、`tiers/manager_routing.py`（`SessionRouter` 及其常驻上限）、`manager_sessions` 表和 `db/manager_sessions.py`、`tiers/manager.py` 的会话启动和输入、`tiers/group_session.py`、`ops/service.py` 的授权（`current_listing`、`_authorize`、`_operator_turn`、`_confirm` 和两处高风险检查）、`ops/__init__.py` 的待确认、运行时的启动模式声明、渠道写 `sender_kind`。`operator_call` 只调用，签名不变。
- 那一步计划交给本文的几个扩展点（内置集合 `task_session`、放宽「受众是操作者当且仅当范围是 `full`」的三处、`SessionKey` 加 `TASK`、键约束改成 CASE 形式）在做法 A 下**都不需要**，那一步不用留。以后真要换成做法 B，这几处再按那一步写好的扩展点做。
- 本文拥有：`db/tasks.py` 的状态分组；`projects` 的 `create_task_core`、`check_task_control`、`start_ready_task`、`control_task`、`store_chat_supplement`、`EDITABLE_STATUSES`；`tiers/task.py` 的 `_assign_task`、`_handle_start`、`_handle_restart`、`_start`、`resume_tasks`、`start_tasks`、`_teardown_task` 和新的 `_conversations`；`tiers/planner.py` 和 `planner_protocol.py` 的新回合、`_plan_and_dispatch`、`_restart_plan`、`_restart_message`；`chat_targets.py` 的 `kind: task`；`v1_domains/operator_chat_streams.py` 的 `run`；`v1_domains/task_create.py`、`task_views.py` 和新的列表、解析、改动、看过几个操作；`ops/tasks.py` 的 `_control`、`_confirming` 和工具描述；`task_workspaces` 的 `WorktreeState`、`prepare`、`sweep`、`finish`；`db/cost_holds.py` 的状态排除；`web/src/pages/work.tsx` 和新组件（从 `chat.tsx`、`task-detail.tsx` 只抽出，不改行为）；`cli.py` 的 `task` 命令组和新模块、新表。
- 共用：`untrusted.Source` 加一个值 `TASK_REQUIREMENT`，只加不改；两边的迁移互不修改对方的表，序号实现时按 `main` 核对。
- 管家在飞书里回复任务通知的路由（第 5 步）在 `gate_routing` 之后、管家的会话路由之前，不改会话路由本身。

## 18. 分步计划

| 步 | 内容 | 依赖 | 开关 | 粗估 |
|---|---|---|---|---|
| 0 | 任务页的「和这个任务对话」入口（#1736） | — | — | 已做 |
| 1 | 草稿和规划者对话（第 8 节） | — | `tasks.conversations.enabled` | 9–12 |
| 2 | 多任务控制台（第 9 节） | 和 1 并行；2.4 的草稿部分等 1.6 | 同上 | 9–12 |
| 3 | 结束后接着做（第 10 节） | 1 | `tasks.reopen.enabled` | 4–6 |
| 4 | 命令行（第 11 节） | 1.6、2.2 | 无：新命令；开始路径多拿一把共享锁 | 4–6 |
| 5 | 转成任务、飞书回复落进任务对话（第 12 节） | 1 | 跟 `tasks.conversations.enabled` | 4–6 |
| 6 | 过程看得见、能打断（第 13 节） | 1、2 | 各项各自的开关 | 4–6 |
| 7 | 登记已有目录（第 14 节） | 4 | 无 | 3–5 |
| 8 | 飞书话题（第 15 节） | 5 之后再评估 | — | 待定 |

- 第 1、2 步合起来才算「可以用新方式工作」，合计 18–24 人日，并行时日历上约 9–12 个工作日。比设计稿估的多，主要在三处：对话规划者的生命周期和续接（1.2）、`[PLAN]` 接着草稿会话和 worktree 交接（1.4）、列表接口和「看过」（2.1）。
- 第 3、4 步紧跟其后，互不依赖，可以并行。
- 会改变模型可见内容的是 1.3、1.4（规划者的契约和回合）、第 3 步（`[REOPEN]`、`[AFTER]` 的新动作）、第 4 步（简报）和第 5 步（管家的 `looper_task_draft`），各跑一次真实模型评测（开发计划第 1 节）。

## 19. 可观测性

**指标**（进 `manager_metrics.get` 的新字段 `work_mode`；CLI 侧的计数在 `task_operator_sessions` 里）：

| 指标 | 维度 | 用来看什么 |
|---|---|---|
| 任务对话回合数、主管家对话回合数 | 入口 | 在电脑前的交互是否大多发生在任务对话里 |
| 重新打开次数和新建任务数 | 项目 | 同一件事是否还在拆成多个任务 |
| 发消息到第一段文字的时间、排队等待的时间 | 规划者在做的回合种类 | 5.2 的排队到底多长；要不要考虑做法 B |
| 活着的对话规划者数、回收次数、续接落到退路的次数 | 原因 | 第 7 节的上限和 5.4 的续接 |
| 草稿数和存活时间 | 结果（开始、删除、还在） | 草稿多久变成开始 |
| `tasks.work_list` 和切换的耗时 | 分区 | 9.5 的目标 |
| 租约拒绝 | 被拒的一方、持有者 | 两个写入方撞上的频率 |
| 不变量违例 | — | 规划者在草稿里开始了任务；对话规划者写了任务状态；非操作者进了任务对话。应恒为 0 |

**告警**（经 `operator_dm_target` 发到操作者私聊）：不变量违例，严重，每次都发；对话规划者起不来，高，每小时一次；超过 14 天没动的草稿在「需要你处理」里提一次，不告警。

## 20. 风险

| 风险 | 应对 |
|---|---|
| 规划者在决策回合里，操作者的话要排队 | 写明它在做什么、排第几；回答分段；第 6 步接入回合中并入；实测等待过长再考虑做法 B |
| 续接落到新会话，丢掉逐字的上下文 | 退路带简报和对话摘录；界面写明换了会话；完成的 worktree 任务按需建回以续上原会话 |
| 对话规划者多了占内存 | 不常驻，按需恢复，全机上限（第 7 节） |
| 草稿越攒越多 | 不占进程和并发；一键删除；超过 14 天没动的提醒一次 |
| 「完成」不再是终点 | 6.3 的表逐处核对，各一个测试 |
| 操作者在命令行动手，后台执行者也在改 | 进行中默认只看不改；动手先拿 checkout 的排他锁，拿不到就说明是谁占着 |
| 每个任务一份上下文，花费上升 | 空闲回收；对话回合算进任务的上限；读回每个任务对话的花费 |
| 界面改动面大 | 新页面并存，默认入口不变；旧页面只抽出共用的部分，不改行为 |

## 21. 已定的问题

各项默认值以后都可以改。

### 21.1 任务里和操作者说话的是规划者

**决定：** 这个任务的规划者，全程同一个角色（做法 A）。**理由：** 操作者的话不经转述；规划者本来就按任务有自己的会话、知道计划和每一步的结果；核对下来做得到（第 5 节），还不碰能力范围 RFC 正在改的会话路由。**以后可以改：** 实测排队过长时拆成两个角色。

### 21.2 开始前确认一次需求，可以跳过

**决定：** 默认要：规划者把对话整理成需求，任务停在待启动，操作者点「开始」或改一句；在草稿里说「直接开始」或发 `/start` 跳过。开始只由操作者触发（6.2）。**理由：** 后台执行和评审要一份明确的需求作依据；开始是要花钱、要改文件的决定，由代码认操作者的动作，不靠模型判断「用户是不是想开始」。**以后可以改：** 例如按项目设成默认跳过。

### 21.3 新界面先并存，以后替换

**决定：** `#work` 作为新页面和 Chat 页并存，默认入口不变。**理由：** 改动面大，并存可以随时退回；旧页面的行为一处不改。**以后可以改：** 用顺了把默认入口换成 `#work`，再删旧页面。

### 21.4 对话规划者有自己的常驻上限

**决定：** `tasks.conversations.max_resident` 默认 6，闲置 30 分钟回收，运行中的规划者不算（第 7 节）。**理由：** 管家会话的常驻上限归能力范围 RFC，规划者也不在里面；混在一起会改那边的代码。**以后可以改：** 例如和管家会话合成一个全机上限。

### 21.5 重新打开不通知原来的来源聊天

**决定：** 只告诉发起重新打开的地方（10.4）。**理由：** 来源聊天可能是一个群，重新打开是操作者自己的后续。**以后可以改：** 按任务加「也通知来源」。

### 21.6 动手会话：进行中默认只看，动手先拿锁

**决定：** `looper task enter` 对进行中的任务默认 `observe`，`work` 要显式并先拿 checkout 的排他锁，拿不到就说明持有者，不能强行接管（11.2、16.1）。**理由：** 两个写入方会互相覆盖；按 checkout 加锁，谁在写只有一个答案。**以后可以改：** 例如加「先停任务再进入」的组合命令。

### 21.7 其他

- 完成任务的 worktree 按需建回，不自动重建（10.3）；以后可以让 `observe` 也建回只读检出。
- 登记已有目录默认原地（第 14 节）；以后可以在目录干净时建议 worktree。
- 上限按任务累计，到了上限重新打开要给新上限（10.5）；以后可以允许每次运行另设小上限。
- 不用任务 id 也能找到任务，解析是确定性的代码（11.4）；14 天、20 个、90 天以后都可以改。
- 把 CLI 会话分叉给规划者不做，列为以后的探索：两边协议不同，会话记录里的私人内容会流进 agent 的输出。
- 飞书话题待定（第 15 节）。

## 22. 默认行为与用户入口

**默认行为：** `tasks.conversations.enabled`、`tasks.reopen.enabled` 默认关。关着时：没有草稿、没有对话规划者、没有 `#work` 页，Chat 页、管家的工具描述、建任务和开始的路径、渠道路由都和今天逐字节相同。第 4 步的命令是新命令；它带来的共享锁让开始路径多一个打开的文件描述符，没有操作者会话时总能拿到。

**用户入口：**

- **控制台：** 第 1、2 步起 `#work` 页（第 9 节）；第 3 步起在对话或右栏里重新打开；第 5 步起在主管家对话里转成任务。旧 Chat 页不变。
- **命令行：** 第 4 步起 `looper task enter / ask / brief / sessions`，第 3 步起 `reopen`，第 7 步起 `register`。`loopercli` 的命令不变；新的 v1 操作它也会列出来，只有操作者能调用。
- **飞书：** 第 5 步起回复任务通知就是对那个任务说话，管家可以建草稿；第 3 步起管家可以按操作者的话重新打开任务。不在飞书里切换多个任务。
- **Telegram：** 管家的路径同飞书；回复通知落进任务对话在第 5 步只做飞书。
