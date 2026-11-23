# RFC：项目 skill 的生命周期（v0）

- **状态：** 决定已定，只有文档。第 4 节的 D1–D15 是本文的选择；第 14 节的 Q1–Q12 维护者在 2026-11-22 全部按建议答复，记录在第 14 节，成为决定 D16–D27。本文合入后按第 13 节的 PR 顺序实现。
- **编号：** 本文是产品路线图的 P3.4「skill 生命周期」。开发计划里已交付的 M4（项目 skill）是它的前置，不是它。
- **需求：** 项目 skill 今天只有「写、改、删」三个动作：改了就覆盖，没有历史、不能回滚；只有操作员能写，agent 学到的经验没有一条正规的路变成 skill；一个 skill 有没有用，没有任何数据。要让 skill 像代码一样有版本、有评审、有度量、能下线。
- **范围：** 版本与回滚；谁能写（操作员直接写，agent 只能提议、操作员确认）；变更的评审；按项目启用与弃用、删除；用已有的度量和评测衡量一个 skill 有没有用；skill 到达各运行的方式与覆盖。不含 skill 里带脚本或二进制文件（今天一个 skill 只有一个 `SKILL.md`，见第 2 节）、跨项目的 skill 市场、自动生成 skill 而不经操作员确认。
- **相关：** 度量与评测底座 RFC（#1523，未合入时没有链接）（`step_attempts`、一次通过率、基线规则，#1523）；[记忆写入路径 RFC](memory-write-path-v0.zh-CN.md)（确认绑定内容哈希的做法）；[Planner 定义角色 RFC](planner-defined-roles-v0.zh-CN.md)（角色改动产生新版本的先例）；可执行验收 RFC（#1520，同样未合入时没有链接）。
- **核对基线：** `main` `c8b510cc5`；度量 RFC 读自其分支 `codex/rfc-m1-metrics-eval`（写本文时未合入）。第 2 节的现状是读代码得出的；本文没有启动服务、没有调用模型。标「推测」的是没有读到代码或没有验证的判断。
- **术语：**
  - **skill：** 一个目录项，只有一个文件 `SKILL.md`（frontmatter 的 `name`、`description`，加正文）。
  - **项目 skill：** `origin = 'project'` 的目录项，归一个项目所有（`owner_project_id`），可安装到别的项目，不能全局安装。
  - **版本：** skill 在某一刻的完整内容和它的 `sha256`。运行的记录里带着它加载的每个条目的 `sha256`，所以版本能对上运行。
  - **提议（proposal）：** agent 起草、还没有任何运行用到的一个 skill 新建或修改，等操作员确认。

## 1. 背景与目标

项目 skill 解决的是「这个项目反复要做的事，每次都让 agent 重新摸索」：部署检查、发版步骤、某个仓库的测试套路。它们进不了提示词（每个已安装的 skill 的名字和描述会列进每个运行的上下文，所以有 20 个的上限），按需用 Skill 工具读取。

随着 skill 变多会出现三类问题：

- **改不得、退不了。** 编辑直接覆盖条目，且影响所有安装它的项目；改坏了没有上一版可回。
- **没有正规的来路。** agent 在任务里学到的东西，只能靠 Worker 报告里的 `learnings` 或者操作员手抄成 skill。
- **不知道有没有用。** 没有人能回答「这个 skill 被用过几次、用了之后任务是不是更顺」。无人评估的 skill 只会越积越多，每个都在吃每个运行的上下文。

**目标：**

- 每次改动留一个只追加的版本；回滚是「以旧内容建一个新版本」，历史不丢；
- agent 只能提议，操作员在看过完整内容和差异后确认；确认绑定内容哈希；
- 项目里每个 skill 有明确的状态：启用、停用、弃用、删除；
- 一个 skill 的使用和效果由代码从运行记录和度量里算出，附样本量，不下结论、不自动下线；
- 说清每个运行时能不能加载 skill，覆盖不到的如实显示；
- 不用提议功能的项目，启动参数、提示词和每个运行的加载结果与今天逐字节相同。

**不做：**

- 不让 agent 直接写、改、删 skill；不让它确认自己的提议；
- 不自动弃用或删除 skill，只给出「建议复核」的标记；
- 不做 skill 内的脚本、资源文件、子目录；
- 不做按效果自动选择 skill 版本或灰度；
- 不新增系统设置。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做（出处） |
|---|---|
| 形状 | 项目 skill 是目录项（`library_items`，`origin = project`），一个文件 `SKILL.md`；名字小写 slug、至多 64 字符，描述至多 1024，正文至多 64 KiB，每个项目至多写 20 个（`project_skills.py`） |
| 写入口 | 操作员接口 `project_skills.{add,get,update,delete}`（`v1_domains/project_skills.py`）和项目页的 Skills 标签；`delete` 是高风险、要确认；API 客户端可调用 |
| 改动 | `update` 原地改条目，对所有安装它的项目生效；`library_items.sha256` 是文件路径、哈希和可执行位的哈希，随内容变；没有历史表 |
| 安装 | `library_installs`：每个（条目，项目）一行，有角色集合（planner、worker、reviewer）和 `enabled` 开关；项目 skill 只能装到项目，不能全局；写入时默认装进自己的项目并启用 |
| 加载 | 每个 Worker、Reviewer 运行和每个 Planner 回合调 `library.load_installed`：读启用的安装，条目写成按 `sha256` 命名的只读副本，skill 链接进生成的插件 `project`（Claude Code 里叫 `project:<name>`）。读不了目录时运行不带它们，记录里有 `library.error` |
| 运行记录 | 每个运行的记录元数据里有 `library`：`sha256`、角色、项目和每个条目的 `view()`（含 `sha256`）（`library.py` 的 `record()`、`agent_settings`）。**记录能说明装了什么，不能说明模型有没有真的调用它**；文档明写「读回的元数据不证明真实的 skill 调用」 |
| 运行时覆盖 | Claude Code 加载插件和 skill（`--plugin-dir`，名为 `project:<name>`）；Codex 用 `--add-dir` 只读挂载 skill，并在运行时笔记里给出文件路径，由模型自己读文件，没有 Skill 工具调用；Grok 把插件的 `skills` 目录写进 `[skills] paths`，不用 `--add-dir`；Pi 跳过目录里的插件和 skill；其余记入 `runtime_catalog.skipped`（`agent-system.md` 各运行时一节） |
| 来自 agent 的路 | Worker 和 Reviewer 的报告可带 `learnings`，其中 `scope: project` 的会被记成项目经验（`journal.reported_learnings`）；Planner 能为任务定义角色（有版本），角色的 `items` 可以指定已装的 skill；没有 agent 创建 skill 的路径 |
| 度量 | 现有评测 `prompt_regression` 每个用例有 outcome、重试、评审、花费；#1523 增加 `step_attempts`（每次尝试一行，含 Profile、结局、花费）和 `task_outcomes`，以及 Wilson 区间的基线规则。两者都没有「这次运行加载了哪个 skill」这一维 |
| 复核提示 | 没有；一个 skill 建好之后没有任何东西会再看它 |

结论：加载和记录这一半已经就位（运行记录带着每个条目的内容哈希），缺的是历史、agent 的来路、状态和度量。

## 3. 概念

| 概念 | 定义 |
|---|---|
| 版本 | `skill_versions` 的一行：条目、序号、完整内容、`sha256`、作者种类和作者、原因。只追加。条目当前指向的版本就是各运行加载的内容。**运行记录对上的是内容哈希 `sha256`，不是版本号：回滚写的新版本和被回滚到的旧版本内容、哈希都相同，在记录里无法区分；一个哈希可以对应多个版本号**（度量按哈希统计，第 7 节） |
| 作者种类 | `operator`（操作员在仪表盘或经 API 写）、`proposal`（来自一个已确认的提议，记下提议人）、`rollback`（回滚产生，记下回到哪一版） |
| 提议 | `skill_proposals` 的一行：新建或修改某个 skill 的完整新内容及其哈希 `content_sha256`、动机、出处（任务、步骤、运行）和状态 `pending`/`confirmed`/`confirmed_edited`/`rejected`/`expired`/`superseded`。`update` 的提议记下提议时条目的当前哈希 `base_sha256`（等于当时的 `library_items.sha256`）。提议对运行没有任何影响 |
| 生命周期状态 | 条目的 `lifecycle` 只有 `active` 和 `deprecated`（不再被新运行加载，仍保留、可恢复）。`disabled` 是安装的 `enabled` 开关（今天已有，按项目），不是条目状态。**删除不是 `lifecycle` 的取值**：条目和它的 `library_files` 一并删除，只剩 `skill_versions` 里的墓碑（见 D8） |
| 使用 | 运行的记录里列出了该 skill 的版本（已加载），加上流里出现了对它的 Skill 工具调用（已调用，第 7 节） |
| 建议复核 | 代码按固定规则打的标记：长期没被调用，或加载后一次通过率的区间与对照不重叠，只是提示，不改变任何状态，也不表示变差或变好 |

## 4. 决策（建议）

| # | 决策 | 理由 |
|---|---|---|
| D1 | **版本只追加，当前版本是条目上的一个指针。** 新表 `skill_versions`；`update` 先写版本、再移指针，同一个事务；回滚是以旧内容写一个新版本（作者种类 `rollback`），内容和哈希与旧版本相同。历史不删；删除 skill 时条目和 `library_files` 级联删除，`skill_versions` 的行留作 90 天墓碑（D8），之后清理 | 回滚要能再回滚；「谁在什么时候把什么改成什么」是评审和复盘的基本事实 |
| D2 | **谁能写：操作员直接写；agent（Planner、Worker、Reviewer、Manager）只能提议。** 提议只写 `skill_proposals`，不碰 `library_items`，因此对任何运行没有影响。**确认只能在仪表盘做**（确认事件的 `via` 只能是 `dashboard`，数据库约束）：确认页显示完整内容或差异，操作员看过才确认；确认绑定提议的 `content_sha256`。Manager、Planner、API 客户端、命令行，以及 Lark、Telegram 的私聊都不能确认 | 模型不能批准自己起草、操作员没看过的指令：skill 正文会进以后每个运行的上下文，是持续生效的提示注入面。渠道通知不带正文，所以渠道里确认等于确认没看过的内容 |
| D3 | **提议功能是项目设置 `skill_proposals`（`off` 默认 / `on`）。** `off` 时 agent 没有提议的路径、契约里不提它、接口和界面没有新东西。`on` 时：Planner 在任务收尾回合可以带 `skill_proposals`（至多 2 个，每个含完整内容和动机）；Manager 多一个只写提议的工具。Worker 和 Reviewer 的 `learnings` 不自动变成提议，只作为 Planner 提议时的出处 | 打开前对线上不可见；提议由对全任务有视野的 Planner 把关，而不是每个 Worker 各提一个 |
| D4 | **提议必须由代码校验后才能进入待确认，且复用今天写入口的校验，不另写一份：** 名字、描述、正文分别过 `project_skills.skill_name`、`skill_description`、`skill_content`（上限、格式、以 `---` 开头的拒绝，同今天）；**描述和正文都**要过同一个密钥检查，因为描述每个运行都会列进上下文；同一项目已有 20 个时只能提「修改」；同名的待确认提议只留最新一个（旧的置 `superseded`）。不检查「链接是否指向工作目录之外」：一个被多个项目安装的 skill 没有唯一的工作目录 | 一条规则一个类型化的检查，不靠 prompt 里的话；上限和今天的写入一致；描述和正文是同样的注入面 |
| D5 | **确认的界面是差异，不是全文。** 项目页 Skills 标签多「待确认」一栏：新建显示全文，修改显示相对当前版本的差异；每条带出处（哪个任务、哪个步骤、哪次运行）和动机；操作员可以确认、拒绝（带原因，原因回给提议的 agent 当反馈）或先编辑再确认（编辑换哈希，等于操作员自己写，作者种类记 `operator`，提议记 `confirmed_edited`）。7 天未处理的提议过期，可在页面恢复 | 所见即所得；拒绝原因让 Planner 下次不重复 |
| D6 | **安装跟随当前版本。** 一个 skill 装进多个项目时，各项目都加载条目的当前版本；改动影响所有安装它的项目，这与今天一致。因此改一个被多个项目安装的 skill 时，确认和编辑的界面列出会受影响的项目。需要「只改这个项目的」的，复制成新 skill（分叉），不做逐项目的版本固定（D18） | 保持今天的语义，不引入第二套版本绑定 |
| D7 | **弃用是条目级的状态：`deprecated` 的 skill 不再被新运行加载，安装行保留，可一键恢复。** 它与安装的 `enabled` 开关不同：开关是「这个项目暂时不要」，弃用是「这个 skill 整体不再推荐」。已经在跑的运行保持它们开始时的加载结果。弃用不删除任何东西 | 给「想删又不敢删」一个可逆的中间态；让度量能区分「停用」和「真不需要」 |
| D8 | **删除沿用今天的高风险确认，并留墓碑。** 删除移出所有项目的安装，条目和 `library_files` 级联删除；`skill_versions` 的行保留为墓碑。90 天内可以「恢复」：用**原来的 `item_id`** 以最后一个版本重建条目和文件，**不恢复任何安装**，要重新装；`item_id` 已被占用则拒绝。墓碑到期后行被清理 | 误删可救；保留原 id，旧运行记录里的引用仍能对上；不恢复安装，避免悄悄重新开启 |
| D9 | **度量只报事实和样本量，不下结论。** 对每个（skill、内容哈希）从运行记录的 `library.items[].sha256` 和 #1523 的 `step_attempts` 算：加载它的尝试数、其中已调用的占比、一次通过率、平均花费；对照组是同一项目同期没加载它的尝试。口径和窗口与 #1523 一致（第 7 节）。每个比率带 Wilson 区间，样本少于 20 个尝试时显示「样本不足」而不是比率。页面和接口明写：这是观察数据，不是对照实验，任务难度可能不同。因为回滚版本与旧版本同哈希，页面把同一哈希的版本合并显示 | #1523 的规则（不取最好的一次、区间、不下结论）；skill 常常加在难的任务上，朴素对比会反过来 |
| D10 | **「真的被用了吗」从流里的 Skill 工具调用读，不从安装记录推，且只对有该调用的运行时算。** Claude Code 的流里有对 `project:<name>` 的 Skill 调用才算已调用（推测，PR 5 开工前用真实命令验证）；Codex（读挂载的文件）和 Grok（读 `[skills] paths` 下的文件）没有这种调用，它们的「已调用」显示为「未知」，不显示为 0，也不进调用率的分母；记录只说明加载了，不算使用 | 避免把「装了」说成「用了」；文档已明写读回元数据不证明调用 |
| D11 | **因果证据来自评测，不来自线上。** 评测套件新增「skill 开/关」成对用例：同一任务同一模型，一边装这个 skill、一边不装，`--reps` 次，报通过率和平均花费的差，带区间。提议被确认前不强制跑；操作员可以在确认页上点「对它跑评测」（耗费真实调用，显示预计花费后才开始）。评测用的是 `prompt_regression` 的现有框架，基线规则同度量 RFC | 线上对比有混杂，成对评测才能回答「有它比没它好吗」；花钱的评测由操作员决定要不要跑 |
| D12 | **「建议复核」由固定规则产生：** 已加载的尝试超过 30 个但调用率为 0；或超过 90 天没有被调用；或加载它的一次通过率的区间与没加载的对照不重叠。只在项目页的该 skill 行上标出原因和区间，不判断好坏，不发通知，不改状态 | 无人评估的 skill 会一直吃上下文；提示让操作员去看，决定权仍在人；区间不重叠只说明值得看，不说明变差 |
| D13 | **上下文预算可见。** 项目页显示该项目每个运行会列出的 skill 名字和描述的总字符数；超过 8000 字符时标出，提醒弃用或合并。不新增硬上限（今天的 20 个上限不变） | 每个已安装的 skill 都在每个运行的上下文里；成本要摆在操作员眼前 |
| D14 | **运行时覆盖如实显示，不做翻译。** skill 行显示它在各运行时的加载状态（Claude Code：加载；其他适配器：来自 `runtime_catalog.skipped` 的原因；Pi：跳过）；度量只统计实际加载它的运行。v0 不为不支持的运行时翻译或改写 skill | 翻译会让同一个 skill 在不同运行时是两份内容，版本和度量都对不上 |
| D15 | **不用新功能时，运行侧逐字节不变。** 约束的对象是：每个运行的加载结果、启动参数、提示词和 `library` 记录（`view()`、`record()` 不加字段）。`update` 多写一行版本，不改条目内容的哈希。版本历史和 `versions` 操作始终存在（PR 1 起所有项目都有）；`skill_proposals` 为 `off` 的项目，契约里没有 `skill_proposals`，接口和界面没有提议的东西（PR 4 的测试对照 `main` 的契约文字）。`deprecated` 只有被用过才出现 |  同助理层和验收 RFC 的要求；历史是新增的只读功能，不属于运行侧 |

密钥检查是一个函数 `skill_secrets.find(text) -> list[Finding]`，规则是代码里的一张固定正则表，每条一个名字（例如 PEM 私钥头 `-----BEGIN [A-Z ]*PRIVATE KEY-----`、`AKIA[0-9A-Z]{16}`、`gh[pousr]_[A-Za-z0-9]{36,}`、`sk-[A-Za-z0-9_-]{20,}`、`xox[abprs]-[A-Za-z0-9-]{10,}`、`Bearer\s+[A-Za-z0-9._~+/=-]{20,}`、`(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*\S{8,}`），命中只返回规则名和位置，不返回内容。每条规则有一个应命中、一个不应命中的单测；与仓库已有的日志脱敏（`log_redaction.py`）能否共用同一张表，在 PR 3 开工时核对（推测，没有读过它的规则）。这是兜底，不保证拦住所有秘密，确认页的差异仍由操作员看。

名称：代码是 `skill_versions.py`（版本、回滚、墓碑）、`skill_proposals.py`（提议的类型、校验、确认）、`skill_usage.py`（使用与度量的查询）；表 `skill_versions`、`skill_proposals`；接口沿用 `project_skills.*` 并新增 `versions`、`rollback`、`deprecate`、`restore`、`proposals.list/get/confirm/reject`。

## 5. 默认行为的变化

实现的 PR 在描述里逐条列出：

- **一直生效（所有项目）：** `update` 和 `delete` 现在留版本和墓碑；项目页 Skills 标签每个 skill 多「历史」入口（只读）。运行不变。
- **`skill_proposals` 打开的项目：** Planner 的收尾回合契约多一段（可选键 `skill_proposals`）；Manager 多一个提议工具；项目页多「待确认」栏。这是模型可见的变化，按第 12 节评测。
- **使用度量：** 页面多「使用」一栏，只读；不开提议也有（它读已有的记录）。依赖 #1523 合入。
- **弃用：** 新状态，只有操作员主动弃用才出现。

## 6. 协议与操作（打开提议后）

Planner 收尾回合（任务结束时那一轮，沿用 `final` 的位置）：

```json
{"skill_proposals": [{
  "action": "create",
  "name": "release-check",
  "description": "Use before cutting a release of this project: the checklist and the commands in order.",
  "content": "# Release check\n1. ...",
  "why": "Three tasks in this project re-derived the same release steps.",
  "evidence": [{"task": "t-...", "step": "s2"}]
}]}
```

- `action` 是 `create` 或 `update`（`update` 带 `skill`：已有 skill 的名字）；其余字段的校验见 D4；`why` 至多 500 字符，`evidence` 的任务和步骤必须存在且属于该项目；
- 契约里一段话（只在项目打开时有）：只有在一个做法在本任务里被重复使用或重复摸索、而且对该项目以后的任务也成立时才提议；不要把任务专属的内容、密钥或一次性的路径写进 skill；提议要等操作员确认才生效，之前不会有任何运行看到它；
- 提议被确认或拒绝时，下一个 Planner 回合的上下文里多一行结果（拒绝带原因）。

操作（operator 专用的确认；读写分级沿用 `project_skills`）：

| 操作 | 风险 | 说明 |
|---|---|---|
| `project_skills.versions` | read，只限操作员 | 一个 skill 的版本列表和某两版的差异（内容、作者种类、原因、时间） |
| `project_skills.rollback` | write | 以某个旧版本的内容写一个新版本；返回受影响的项目 |
| `project_skills.deprecate` / `restore` | write | 弃用与恢复 |
| `project_skills.proposals.list` / `get` | read | 待确认的提议及差异 |
| `project_skills.proposals.confirm` / `reject` | write，operator 专用 | `confirm` 带操作员看到的提议哈希 `content_sha256`；与库里该提议当前的 `content_sha256` 不符（提议在看的时候被改了）返回 409 |
| `project_skills.usage` | read | 第 7 节的使用与度量 |

`project_skills.versions` 和 `project_skills.context_budget` 的读只限操作员，API 客户端令牌不能读。确认只能来自操作员的仪表盘会话（确认事件的 `via` 只能是 `dashboard`，沿用记忆写入的约束，但去掉 `lark`、`telegram`）。

## 7. 使用与度量

数据来源（都是已有的或 #1523 的，不新增埋点）：

- 运行记录的 `library.items[].sha256` 和角色、项目 → 「某次运行加载了哪个 skill 的哪个版本」；
- `step_attempts`（#1523）→ 尝试的结局、Profile、花费，用 Worker 运行的记录号关联运行；
- 运行的事件流里的 Skill 工具调用（`project:<name>`）→ 「已调用」（D10）。

对每个（skill、内容哈希）的统计，窗口与 #1523 一致：近 7 天，对照前 28 天；按任务的 `attempt_id` 关联，只数 Worker 的尝试（Planner 回合没有尝试，不计）：

| 指标 | 定义 |
|---|---|
| 加载的尝试数 | 加载了该版本的尝试数（按尝试去重） |
| 调用率 | 其中出现了对它的调用的尝试占比；不支持事件的运行时记「未知」，不进分母 |
| 一次通过率 | #1523 的定义：`trigger = first`、该步骤只尝试一次、被接受且未重开；分母是加载它的 Worker 尝试（按 `attempt_id`，不用 `worker_record_id`：它只指向该尝试最后一个 Worker 运行）；带 Wilson 区间 |
| 对照一次通过率 | 同一项目同窗口、没有加载它的尝试，同上 |
| 平均花费 | 每个通过的尝试的平均花费，对照同上 |
| 样本不足 | 任一侧少于 20 个尝试，不显示比率，只显示计数 |

页面把「调用率」放在最前面：一个从不被调用的 skill 谈不上效果。差值旁写明「观察数据，非对照实验」，链接到评测（D11）。评测的成对用例、基线和对比规则沿用度量 RFC 第 9 节（#1523），不另立一套。

## 8. 评审、回滚与失败语义

- **变更的评审：** 操作员自己的改动，界面在保存前显示差异和受影响的项目，保存即生效（今天的做法加上差异）。提议的改动，确认页显示差异、出处和代码检查结果，确认后生效。两者都落一个版本，作者种类可查。
- **回滚：** 版本页「回到这一版」→ 显示差异和受影响的项目 → 确认后写新版本。进行中的运行保持它们开始时的加载结果，下一次启动的运行用新版本。
- **并发：** 沿用项目行锁。409 只比较确认请求里的哈希和提议当前的 `content_sha256`。`update` 的提议在确认时再比较 `base_sha256` 和条目当前的 `library_items.sha256`：不同（有人先改了条目，包括回滚）→ 提议置 `superseded`，提示重新起草，不自动合并；相同则确认生效，写版本。

| 情况 | 结果 |
|---|---|
| 提议校验不过（D4） | 不进待确认；提议的 agent 在下一回合看到原因 |
| 确认请求的哈希和提议当前哈希不符 | 409，页面刷新差异 |
| 确认时 `base_sha256` 已不是条目当前哈希 | 提议置 `superseded`，页面提示重新起草 |
| 写版本或移指针失败 | 事务回滚，条目不变，界面显示错误 |
| 墓碑到期清理失败 | 留着，下次再清；不影响运行 |
| 弃用的 skill 仍被某角色的 `items` 指定 | 该次加载跳过，活动里记一行，不让运行失败 |
| 度量依赖的 `step_attempts` 不存在（#1523 未合入） | 「使用」一栏只显示加载和调用，不显示效果 |

## 9. 数据模型与迁移

```sql
CREATE TABLE IF NOT EXISTS skill_versions (
    id          BIGSERIAL PRIMARY KEY,
    item_id     TEXT NOT NULL,              -- library_items.id；删除条目时行保留（墓碑）
    version     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    content     TEXT NOT NULL,              -- SKILL.md 的正文（不含 frontmatter）
    sha256      TEXT NOT NULL,              -- 与当时 library_items.sha256 一致
    author_kind TEXT NOT NULL CHECK (author_kind IN ('operator', 'proposal', 'rollback')),
    author_ref  TEXT,                       -- proposal id 或回滚到的版本号
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (item_id, version)
);

CREATE TABLE IF NOT EXISTS skill_proposals (
    id           BIGSERIAL PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    action       TEXT NOT NULL CHECK (action IN ('create', 'update')),
    item_id      TEXT,                      -- update 的目标
    base_sha256  TEXT,                      -- update 基于的当前哈希
    name         TEXT NOT NULL,
    description  TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    why          TEXT NOT NULL,
    evidence     JSONB NOT NULL DEFAULT '[]'::jsonb,
    proposer     TEXT NOT NULL,             -- agent id
    status       TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'confirmed_edited', 'rejected',
                                                 'expired', 'superseded')),
    decided_at   TIMESTAMPTZ,
    decision_note TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE library_items ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active', 'deprecated'));
ALTER TABLE library_items ADD COLUMN skill_version INTEGER;   -- 当前版本号；NULL 表示还没有版本行（迁移前）。不能用 `version`：该列已是声明版本（TEXT）
ALTER TABLE projects ADD COLUMN skill_proposals TEXT;   -- NULL 即 off
```

- 迁移回填：给每个现有项目 skill 写版本 1（作者种类 `operator`，原因 `imported`），`library_items.skill_version = 1`；回填不改 `sha256`；
- `skill_proposals` 列同验收 RFC：可空，非空才进导出和接口；
- `lifecycle = 'deprecated'` 的条目在 `load_installed` 里被过滤（一处），其余加载逻辑不变；
- 确认事件和 `via` 约束沿用记忆写入的方案，`via` 只留 `dashboard`（表和约束在 PR 3 写明，不在这里重复设计）；
- 恢复墓碑：重建 `library_items`（原 `item_id`）和 `library_files`，不写 `library_installs`；`skill_versions.item_id` 不设外键，所以删除条目后行仍在。

## 10. 用户入口

遵循 `docs/design.md` 的界面章节。

- **仪表盘：** 项目页 Skills 标签：每个 skill 一行，显示名字、状态（Active、Disabled in this project、Deprecated 用文字）、当前版本、安装它的项目数、使用（加载的尝试数、调用率）和「建议复核」的原因；展开显示历史（版本、作者种类、时间、原因）、差异、回滚、弃用、各运行时的加载状态。「待确认」一栏列提议。空状态、出错和样本不足都用文字说明，不显示成 0 或放心的摘要。上下文预算（D13）显示在标签顶部。
- **Telegram、Lark：** 不新增命令，也不能在渠道里确认（D2）。提议待确认时是否给操作员私聊发一条只含名字、动机和页面链接的通知，见 D21；通知不带 skill 内容。
- **CLI（`loopercli`）：** `versions` 和 `context_budget` 的读只限操作员；`usage`、`proposals.list/get` 仍是读操作。`rollback` 和弃用是写操作；`confirm` 不提供（D2）。
- **Manager 工具：** 只有「提议」（打开项目设置后），没有写、改、删。

## 11. 观测

- **活动（任务页）：** 「Skill proposal p-… from step s2 created」「confirmed / rejected」；运行时的 `Project installs applied (...)` 已有，弃用的跳过加一行。
- **指标：** 每个项目的提议数、确认率、拒绝原因分布、平均处理时间、过期数；回滚数；每个 skill 的调用率；`建议复核` 的数量；上下文预算的分布。
- **告警：** 不加。提议积压在仪表盘上看；确认率和拒绝原因只是观察数据：确认率低可能是提议质量差，也可能是操作员更倾向于自己写，需要结合拒绝原因判断。

## 12. 评测与测试

测试按开发计划第 1 节：一条规则一个测试，不给测试工具写测试，不钉文案。

**单元与数据库测试（先写）：**

- 版本：`update` 写版本并移指针在一个事务里、失败时回滚；回滚写新版本；删除留墓碑且 90 天后清理；迁移回填不改 `sha256`；**关闭时逐字节不变**：对照 `main`，加载结果、启动参数、`library` 记录相同；
- 提议校验（D4）的每条规则（类型化的拒绝，各一例）；描述里的密钥也被拒；密钥规则表每条一个应命中、一个不应命中的例子；同名替代；对象上限；`evidence` 必须属于该项目；
- 确认：绑定提议哈希、哈希不符的 409、`base_sha256` 变了置 `superseded`、编辑后确认的作者种类、过期与恢复；Manager、Planner、API 客户端、命令行、Lark 和 Telegram 都被拒绝（`via` 约束只留 `dashboard`）；回滚后同哈希的度量合并；
- 弃用：`load_installed` 过滤；角色 `items` 指定弃用项时跳过且不失败；恢复；
- 使用查询：按内容哈希合并回滚版本；一次通过率只数 `trigger = first`、单次尝试、被接受且未重开的步骤（#1523 的定义），用 `attempt_id` 关联，Planner 回合不计；已加载与已调用的区分；不支持事件的运行时记「未知」不进分母；样本不足；窗口；与 `step_attempts` 缺失时的降级；
- 界面（vitest）：待确认栏、差异、回滚确认、各状态文字、390 px 无横向溢出。

**评测**（`evals/README.md`，只跑涉及的任务）：

- Planner 提议质量：`skill_proposal_useful`（任务里重复使用了同一做法，断言提出了一个合格的提议）、`skill_proposal_not_needed`（一次性任务，断言没有提议）、`skill_proposal_leak`（任务里出现了密钥形状的串，断言不会进入提议，由代码拒绝）；
- 成对评测（D11）：一个固定的小仓库任务 + 一个有用的 skill，对比开/关；断言评测框架能产出带区间的差值和花费；
- 契约是模型可见变化：替身模式覆盖新任务，真实模式 sonnet `--reps 2`，Planner 再加 opus `--reps 1`。

**真实路径**（`looper serve`，新库，非 8767 的端口，临时运行根）：建一个 skill → 编辑两次 → 回滚 → 确认版本历史和运行记录里的 `sha256` 对得上；打开提议，让 Planner 收尾时提一个 → 在页面确认 → 下一个任务的运行加载新版本并出现 `project:<name>` 的调用事件（验证 D10）；弃用后新运行不再加载；无头浏览器截图项目页 Skills 标签（桌面和 390 px）。

## 13. PR 拆分、依赖与并行

| PR | 内容 | 依赖 | 模型可见 |
|---|---|---|---|
| 1 | 版本：`skill_versions`、`library_items.skill_version`、`update`/`delete` 留版本和墓碑、迁移回填、`versions` 和 `rollback` 操作、项目页的历史与回滚；关闭时不变的测试 | — | 否 |
| 2 | 弃用与上下文预算：`lifecycle`、`load_installed` 过滤、`deprecate`/`restore`、预算显示 | 1 | 否 |
| 3 | 提议：`skill_proposals` 表、项目设置、校验、确认事件和 `via` 约束、`proposals.*` 操作、待确认栏；契约还不给 agent（先能手动造提议用于测试） | 1 | 否 |
| 4 | agent 的来路：Planner 收尾回合的 `skill_proposals` 和 Manager 的提议工具、拒绝原因回给 Planner、过期；评测任务 | 3 | 是，要评测 |
| 5 | 使用与度量：`skill_usage.py`、`usage` 操作、页面的「使用」一栏和「建议复核」；D10（Skill 调用事件）的验证在这个 PR 开工前做 | 1；效果列依赖 #1523 | 否 |
| 6 | 成对评测：`prompt_regression` 的开/关用例和确认页的「对它跑评测」 | 5 | 否（评测用） |

建议顺序 1 → 2，3 与 2 并行，4 在 3 之后，5 与 4 并行，6 最后。1–3 和 5 是核心（版本、弃用、能确认的提议、能看到用没用）；4 是核心里唯一模型可见的一块，单独评测；6 可以记入非核心待办；D21 的通知若定为要发，它归入 PR 3，是核心。

## 14. 维护者的答复（2026-11-22）

维护者对下面 12 个问题全部按建议的默认答复；每条记为一个决定（D16–D27，和 Q1–Q12 顺序对应）。

| 决定 | 问题 | 答复（维护者） | 理由 |
|---|---|---|---|
| D16（Q1） | 项目 skill 能不能被提升为全局 skill？ | 不能；今天就禁止，维持 | 全局 skill 对每个项目和 Manager 生效，是另一个信任等级；要提升就由操作员在设置里重新创建 |
| D17（Q2） | Worker、Reviewer 能不能直接提议？ | 不能；他们的 `learnings` 只作为 Planner 提议的出处 | 一个任务一个提议人，避免每个 Worker 各提一份重复的 |
| D18（Q3） | 要不要逐项目的版本固定（装到项目 B 的钉在 v3，A 继续跟新）？ | v0 不做，分叉即可 | 避免第二套版本绑定和第二套度量口径 |
| D19（Q4） | 确认之前是否强制跑成对评测？ | 不强制；确认页给「对它跑评测」按钮和预计花费 | 评测花真实调用，由操作员决定；很多 skill 小到不值得评测 |
| D20（Q5） | 度量的窗口和最小样本？ | 近 7 天对照前 28 天（同 #1523），每侧至少 20 个尝试 | 和度量 RFC 的窗口、口径一致；样本太小的比率不显示 |
| D21（Q6） | 提议待确认时是否私聊通知操作员？ | 通知（名字、动机、页面链接，不含内容），每天最多一条汇总；确认只在页面做 | 不通知就会积压到过期；内容不进渠道，因为渠道消息会被转发，也不让渠道成为确认入口 |
| D22（Q7） | 墓碑保留多久？ | 90 天 | 够发现误删；不无限占库 |
| D23（Q8） | 是否允许 skill 带脚本或资源文件？ | 不，v0 仍是单个 `SKILL.md` | 脚本是可执行内容，评审和沙箱是另一个问题；需要时放进项目知识或仓库 |
| D24（Q9） | 「建议复核」是否自动弃用？ | 不，只标记 | 自动下线在样本小或评测不全时会误杀有用的 skill |
| D25（Q10） | 上传、git、本机导入的 skill 要不要版本？ | v0 不要：版本只覆盖项目 skill，即 `project_skills.update` / `delete`；其他来源的更新走目录自己的路径（`PUT /api/catalog/<id>`、再导入），不写版本 | 这些路径不经 `project_skills`，要覆盖就得在目录层再加一套写版本的钩子，否则指针和哈希会分叉；留给之后的 RFC |
| D26（Q11） | 提议里能不能带对已有 skill 的删除？ | 不能，删除只由操作员做 | 删除是高风险动作，不经 agent |
| D27（Q12） | 运行时覆盖不到时，要不要阻止该运行时的任务使用要求用 skill 的角色？ | 不阻止，只显示 | 不改变运行时选择，只让覆盖缺口可见 |

## 15. 风险

| 风险 | 应对 |
|---|---|
| skill 正文是持续生效的提示注入面（被提议的内容里夹带指令） | agent 只能提议；确认绑定哈希、看差异（D2、D5）；代码校验形状和密钥（D4）；正文不进渠道通知；确认后才有运行看到 |
| 提议刷屏或质量低 | 每任务至多 2 个、同名只留最新、7 天过期；确认率是指标；评测任务量提议质量 |
| 度量把相关当因果 | 页面和接口标明观察数据；样本不足不显示；因果只由成对评测给出（D9、D11） |
| 调用识别依赖运行时的事件 | 只对已知的运行时给调用率，其他显示「未知」；PR 5 开工前用真实命令验证（D10，当前是推测） |
| 版本表膨胀 | 每个版本至多 64 KiB，操作员编辑频率低；若成为问题再做压缩，不在 v0 |
| 回滚影响多个项目 | 回滚前显示受影响的项目；回滚本身是一个新版本，可以再回滚 |
| 弃用或删除导致角色 `items` 引用悬空 | 加载时跳过并记活动，不让运行失败（第 8 节） |
| 上下文被 skill 描述撑大 | 预算可见（D13）、20 个上限不变、「建议复核」标出不用的 |

## 16. 与现有设计的出入

- 开发计划的 M4 是项目 skill 的首次交付；本文在它之上加生命周期，不改它的限制和加载方式；
- 今天 `update` 原地覆盖，本文改为先写版本再移指针，对运行不可见（内容和 `sha256` 不变）；
- 提议确认沿用记忆写入的确认绑定哈希的做法，具体的表和约束在 PR 3 中写明，本文不重复设计；
- 「已调用」依赖运行时事件，是本文中最需要先验证的一条（D10），验证不过则 PR 5 只交付加载统计。
