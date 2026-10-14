# LoopX Agent System — 产品设计

本文是 agent 系统的产品设计，也是 `web/` dashboard 的界面设计参考（见文末「界面设计」）。

## 概述

LoopX Agent System 是一个三层多 Agent 协作架构，通过统一消息总线将 **Manager（管家）**、**TaskRouter + Planner（编排器）**、**Worker（执行者）** 组织为一个可扩展的自主编程系统。

用户通过任意接入渠道（Web、Telegram、Lark）提出需求，系统自动完成任务分解、并发执行、质量验证和结果汇报。

---

## 架构

```
用户
 │
 ▼
Channel Adapter (Web / Telegram / Lark)
 │  ← 统一 MessageEnvelope 协议
 ▼
Manager（管家）
 │  ← 意图识别 + 任务下发
 ▼
TaskRouter（任务路由器）
 │  ← 每个 Task 分配独立 Planner
 ├─ Task A → Planner A → Worker(s)
 ├─ Task B → Planner B → Worker(s)
 └─ Task C → Planner C → Worker(s)
                            ↑
                      共享 WorkerPool
```

### 三层职责

| 层级 | 组件 | 职责 | 隔离方式 |
|------|------|------|----------|
| 接入层 | Channel Adapter | 协议转换，用户消息 → MessageEnvelope | 每渠道一个 Adapter |
| 决策层 | Manager | 意图理解、任务创建、状态汇报 | 单例，常驻 Claude Code 进程（stream-json 输入，每条消息一个 user turn；进程重启后用 `--resume` 续接会话） |
| 编排层 | TaskRouter + Planner | 任务路由；Planner 拆分步骤、按每个结果做决定、调度 Worker 和 Reviewer | 每个 Task 独立 Planner 实例，各自一个常驻 Claude Code 进程（和所有 agent 一样开放全部工具、跳过权限确认，由 role prompt 约束只做规划和决策；进程重启后用 `--resume` 续接会话） |
| 执行层 | WorkerPool | 子进程执行具体开发/验证任务 | 每个 Worker 独立子进程 |

---

## 核心概念

### Task（任务）

Task 是系统的一等实体，代表一个完整的用户需求。

```
状态流转: pending → assigned → running → completed / failed / cancelled
```

- 每个 Task 自动分配独立的 Planner 实例
- Task 结束（完成、失败、停止、取消）时 TaskRouter 停掉 Planner，连同它的 Claude 进程
- 最多 3 个 Task 并发执行，超出排队
- Task 的花费是它的 Planner、Worker、Reviewer 各次运行自身花费之和，按角色分开计，并附运行次数（见下文「花费与上下文」）；Manager 不计入。默认不设花费上限（RFC 决策 2），上限留给 M5 的项目执行配置

### Project（项目）

每个 Task 属于且只属于一个 Project。项目是任务的一个属性，任务在任何状态下都可以移到另一个项目。

- 项目有名称、描述、可选的默认目录、可选的背景（`background`）和可选的 harness 覆盖（`planner_harness_prompt`、`worker_harness_prompt`）。名称去掉首尾空白后是 1–80 个字符，忽略大小写不能重名
- 项目背景是这个项目的所有任务 agent 都该知道的内容，最多 10000 个字符，保存时去掉前后空行，`""` 表示清空。它加进这个项目的 Planner、Worker 和 Reviewer 的系统提示，不加给 Manager（Manager 面向所有项目）。三者的系统提示由 `loopx/agents/system_prompt.py` 统一拼装，共五层：先是 harness（见下一条）；再是 `agent_configs` 里可编辑的角色提示；背景不为空时接一段 `## Project: <名称>` 加背景；然后是任务段；最后是代码维护的契约（Planner 是 `REPLY_CONTRACT`，Worker 是 `WORK_CONTRACT`，Reviewer 是 `REVIEW_CONTRACT`），契约总在最后
- **harness 与 role**：harness 是 agent 的性格与稳定性（工作原则、边界、沟通方式），role 是这类 agent 的职责，两者分开存，怎么改 harness 都不会让 agent 丢掉职责。harness 由 `loopx/agents/agent_settings.py` 统一解析：系统默认存在 Settings（`system_settings` 表；没有存时用仓库内置的公开通用版 `DEFAULT_HARNESS_PROMPT`，可一键恢复）；Manager 可以在 Agents 页覆盖（`agent_configs.manager.harness_prompt`）；Planner 和 Worker 只能在项目里覆盖（`planner_harness_prompt`、`worker_harness_prompt`）；Reviewer 跟随同一项目的 Worker；为空都表示继承系统默认。每段最多 20000 个字符。推荐的角色提示只写职责，原来夹在里面的原则和语气移进了默认 harness；未改过的角色提示行随推荐版升级，改过的保留，并新增 `reviewer` 行
- **模型、effort 与环境变量**：Settings 里有全局默认模型（内置 `opus`）、可选的 effort（`low`、`medium`、`high`、`xhigh`、`max`，不设就不传）和最多 50 个非机密的环境变量。四类 agent 都传 `--model`（Agents 页上该 agent 的 `model` 优先），设了 effort 时传 `--effort`；环境变量加在 `loopx serve` 自身环境之上，名称须为 `[A-Z_][A-Z0-9_]*`，名称里带 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`CREDENTIAL` 的拒收（值会显示在页面上，这只是防手滑，不是安全边界）。这些改动对下一次 Worker、Reviewer 运行生效；常驻的 Manager 和 Planner 在下一个 turn 比较启动命令、提示哈希、环境变量哈希和插件库哈希，变了就用 `--resume` 重启
- **插件与 skill（插件库）**：操作者希望 agent 具备的插件和 skill 显式导入 LoopX，成为系统管理的快照，四类 agent 每次运行都加载（`loopx/agents/library.py`）。Settings 页的「Plugins & skills」点「Import from this machine」（之后是「Re-import」），只读地读取本机 Claude Code 配置（`$CLAUDE_CONFIG_DIR`，否则 `~/.claude`）：`settings.json` 里启用的插件（从 `plugins/installed_plugins.json` 里用户级安装的目录整个导入，连同其 skill、agent、命令和 hook）和 `skills/` 下所有带 `SKILL.md` 的个人 skill（链接的目录照常跟随）；不读个人 hook、MCP、权限和记忆。数据库是事实来源：`library_items` 存条目（类型、名称、来源、版本、文件哈希 `sha256`、是否启用、大小、导入时间），`library_files` 存文件（相对路径、内容、哈希、可执行位）。重新导入只替换内容变了的条目，保留每个条目的启用开关，删除本机已没有的条目。单个文件超过 2 MiB、二进制文件和指向条目外的链接不导入，单个条目超过 32 MiB 或 5000 个文件整个跳过，都在导入结果里列出；整个库超过 128 MiB 时拒绝导入、什么都不改。每次运行按启用的条目在 runtime root 的 `.local/library/<sha256>/` 下取只读副本（第一次用到时从数据库写出），每个插件一个 `--plugin-dir`，启用的个人 skill 链接进一个生成的 `personal` 插件，在 Claude Code 里叫 `personal:<skill>`；这组参数由 `claude_cli.library_args` 统一生成、随启动参数下发，四类 agent 不会不一致。四类 agent 都开放全部工具、跳过权限确认（见下面「不限权限」），skill 不需要另外放行，声明了 `allowed-tools` 的也能运行。插件自带的 `SessionStart` hook 会运行并加入上下文；操作者个人的 `UserPromptSubmit` hook 不会运行。每次运行在 `metadata.library` 记下加载的条目（名称和哈希）和整个库的哈希。代价是每个 agent 的上下文变大：3 个插件加 104 个个人 skill 时，Opus 上一次最小的 `--print` 运行首个请求从 15,674 token 增至 29,114 token
- **项目知识**：项目可以挂载大块参考资料，Planner、Worker、Reviewer 按需读取，不塞进 prompt（`loopx/agents/knowledge.py`，RFC 第 6 节、决策 3）。每篇文档有标题、必填的一句话摘要和来源：手动录入（`text`）、上传文本文件（`upload`，JSON 里用 base64 传）、引用项目默认目录里的文件（`path`，存相对路径，实时读取）；不支持 URL。录入和上传的内容存进 `project_documents`；不是 UTF-8 或含 NUL 字节的内容按二进制拒绝；引用的路径保存时必须存在、是普通文件、是文本，且跟随链接后仍在默认目录里，`..`、目录外的绝对路径和指向目录外的链接都拒绝。上限（超限返回 400 并写明上限）：单篇 2 MiB；每个项目 100 篇、存储内容 50 MiB（引用路径不计入）；拼出的索引 4,000 个字符。同一项目的增删改在持有项目行锁的事务里做，并发新增不会一起越过上限。每次运行（Manager 除外）读取项目的文档：录入和上传的文档一起写成只读文件，每组文档一个目录，按文件名和内容的哈希命名：`<runtime_root>/.local/projects/<project_id>/knowledge/<组哈希>/<文件名>`（重名的加 `-2`、`-3`）；L3 在背景之后加 `## Project knowledge` 索引，每个目录写一次（`In <目录>:`），其下每篇一行 `- <标题>: <摘要> (<相对该目录的路径>)`，已不存在的引用文件标为 `(missing: <路径>)`（Claude Code 只按工作目录解析相对路径，不按 `--add-dir` 解析，所以由 agent 拼接）；启动参数用 `--add-dir` 挂载这组文档的目录，项目有引用路径且任务目录不是项目目录时也挂载项目目录。这组参数由 `claude_cli.knowledge_args` 生成、随 `Launch` 下发，三类 agent 不会不一致。改动立即生效：下一个 Worker、Reviewer 读到新索引，常驻的 Planner 下一轮因索引或挂载目录变化用 `--resume` 重启；引用文件的内容 agent 打开时才读，改文件不需要重启。`prompt_layers.project` 记下索引的 `knowledge_sha256` 和文档数 `knowledge_documents`，读不到时记 `knowledge_error` 并照常运行。索引每行只带相对路径，篇幅短的文档先到 100 篇的上限（原来每行带约 150 个字符的绝对路径，15 到 25 篇就占满索引）
- **项目 skill**：项目可以带自己的 Claude Code skill，只有这个项目的 Planner、Worker、Reviewer 加载，不往项目目录里写任何东西（`loopx/agents/project_skills.py`，RFC 第 6 节）。每个 skill 是 `project_skills` 的一行：名称（小写字母、数字和单个连字符组成的 slug，项目内唯一，数据库用 CHECK 约束同样的格式）、一行描述、`SKILL.md` 的正文（不带 frontmatter，以 `---` 行开头的拒收）和启用开关；删项目时连带删除。上限（超限返回 400 并写明上限）：名称 64 个字符、描述 1,024 个字符、正文 64 KiB、每个项目 20 个；重名返回 409。同一项目的增删改持有项目行锁。每次运行（Manager 除外）读取项目已启用的 skill，按内容哈希只写一次、只读地写成一个插件 `<runtime_root>/.local/projects/<project_id>/skills/<sha256>/project/`（`plugin.json` 加 `skills/<名称>/SKILL.md`，frontmatter 由名称和描述生成），以一个 `--plugin-dir` 挂载在插件库之后，Claude Code 里叫 `project:<名称>`；这个参数由 `claude_cli.project_skill_args` 生成、随 `Launch` 下发。skill 清单由 Claude Code 自己列给模型，系统提示里不重复。改动立即生效：下一个 Worker、Reviewer 读到新的一组，常驻的 Planner 下一轮因插件目录变化用 `--resume` 重启。每次运行在 `metadata.project_skills` 记下项目、这组 skill 的哈希和每个 skill 的名称与 `SKILL.md` 哈希，读不到时记 `error` 并照常运行
- 背景改了立即生效：系统提示每次构建时都通过任务读当前项目（任务移到别的项目后读新项目）。Planner 每个 turn 重建系统提示，变了就用 `--resume` 重启它的 Claude；之后启动的 Worker 和 Reviewer 用新背景；已经在跑的 Worker 不受影响。第一次用上背景时 activity 记一条 `Project background applied (N chars)`，Planner 的背景变化或被清空时再记 `changed` 或 `removed`，不写入原文
- 背景只是参考信息，读不到不影响执行：Worker 和 Reviewer 读项目失败（数据库出错、找不到项目）时照常执行，只是不带项目段，并记一条 `Warning: the project background was unavailable ...` 的 error activity；Planner 读失败时沿用上次读到的项目，并写一条警告日志
- 新建任务时，要么选一个已有项目（`project_id`），要么在同一个请求里新建项目（`project`），两者必须且只能给一个。新建项目和任务在同一个数据库事务里完成
- 任务的工作目录：显式给了就用它（展开 `~`，必须是绝对路径）；没给就用项目的默认目录；两者都没有就拒绝创建。系统仍然没有全局默认目录
- 还有任务的项目不能删除，要先把任务移走或删掉
- 通过聊天创建任务（Web、Lark、Telegram 都经过 Manager）：Manager 每次发给 Claude 的消息开头附上现有项目列表（名称、默认目录、截到 80 字的描述），任务 JSON 带 `"project"`。没有项目名就先问用户；名称忽略大小写匹配已有项目；没有同名项目就新建一个，默认目录取这个任务的目录，并在回复里说明。缺目录时 Manager 会问，不会自己选
- 升级后第一次启动时，已有任务按工作目录各建一个项目，名称取目录最后一段（`''` 和 `/` 记为 `default`），较早的目录用原名，之后忽略大小写重名的依次加 `-2`、`-3`……

### MessageEnvelope（消息信封）

所有 Agent 间通信使用统一的 MessageEnvelope 协议：

- `correlation_id`：端到端请求追踪
- `message_type`：17+ 种消息类型覆盖完整生命周期
- `source` / `target`：AgentAddress 标识发送/接收方
- 全量 JSONL 日志保证崩溃恢复

### Planner 与 Reviewer 策略

每个 Task 只有一个 Planner，它是一个常驻的 Claude Code 进程（和 Manager 共用 `ResidentClaude`）：第一个 turn 时在任务目录里启动，每个事件喂一个 stream-json user turn，任务结束或服务停止时退出。Planner 和所有 agent 一样开放全部工具、跳过权限确认（不加载 MCP），可以读代码、运行无害的命令来做规划和判断结果；推荐的 role prompt 要求它不亲自修改文件，改动都作为步骤交给 Worker 完成，每次执行仍是一个独立进程。

- **谁来决定**：Planner 的 Claude 做判断，代码校验后执行。每个事件一个 turn，依次处理：
  - `[PLAN]`：收到 `TASK_REQUEST` 时把需求拆成 1–8 个步骤，每步有 `id`、`instruction`、`acceptance_criteria`、`depends_on` 和 `review`（是否必须由独立 Reviewer 通过）
  - `[DECISION]`：每个 Worker 结果、每个 Reviewer 结果之后各一次
  - `[CHAT]`：用户发来消息时回复，并最多做一个动作：`none`、`forward`（把文本转给正在执行某一步的 Worker）、`add_steps`
- **五种决定**：
  - `accept`：只有结果可以接受时才允许，即 Worker 完成且这一步不需要验收，或 Reviewer 通过
  - `review`：只能在 Worker 成功之后；另起 Reviewer，它拿到这一步的 instruction、Worker 结果和验收标准
  - `retry`：同一步派给一个新的 Worker，反馈附在 instruction 后面；每步最多重试 2 次
  - `replan`：替换所有还没开始的步骤，已开始的不能改；当前步骤结果可以接受就算 verified，否则作为 skipped 丢弃（跑过的丢弃步骤仍计入 8 步上限）
  - `fail`：任务失败，附上原因
- **什么时候必须验收**：计划里标了 `review` 的步骤，或者被决定送审过一次的步骤，必须有一次通过的 Review 才能 accept，重试之后也一样
- **Worker 怎样才算完成**：三个条件都满足才算，任何一条不满足，这一步都判失败：
  - Claude 正常结束（`result` 事件的 `subtype` 为 `success`，达到最大轮数不算）；
  - 最后一条消息以 JSON 报告结尾，且其中 `status` 为 `completed`；
  - 报告里 `changed_files` 列出的文件都存在，`deleted_files` 列出的文件都已删除。
- **Reviewer**：必须给出布尔类型的 `passed`，纯文本不算通过
- **Worker 和 Reviewer 的提示怎么分**：会话期间不变的内容放系统提示（以 `--append-system-prompt-file` 传入），其他 agent 的产出放 stdin 上的第一个 user turn。系统提示依次是 harness（项目的 Worker 覆盖，没有就用系统默认；Reviewer 跟随 Worker）、角色提示（Worker 用 `worker` 行，Reviewer 用 `reviewer` 行，为空或读不到时用默认值）、项目段、任务段（`## Task`），最后是 `WORK_CONTRACT`（这一步的规则和上面的 JSON 报告格式）或 `REVIEW_CONTRACT`（验收的规则和 `passed` 结论格式）。Worker 的任务段先给整体任务需求作背景，再给这一步的标签、instruction、工作目录、目标文件、验收标准、验证命令，重做时加上第几次和之前的反馈；Reviewer 的任务段是整体需求加上被验收步骤的 instruction 和验收标准。Worker 的 user turn 只有一句开始提示和所依赖步骤的报告（summary、改动的文件）；Reviewer 的 user turn 是被验收的 Worker 报告（summary、改动和删除的文件），作为待核对的数据，不进系统提示。运行中转来的消息（用户聊天、Planner 纠偏）仍是 user turn，不重复契约。`worker`、`reviewer` 行每次运行各读一次（角色提示、`model`、`timeout_seconds`、`max_turns`）
- **回复契约**：每次回复以且只以一个 ```` ```json ```` 代码块结尾，由 `planner_protocol.py` 用类型化的 dataclass 和枚举严格校验：不允许未知字段，step id 唯一，`depends_on` 只能指向已知步骤且无环，最多 8 步（`MAX_STEPS`），决定必须在当前允许的范围内，重试不超限，replan 不能动已开始的步骤。不合规的回复有一次修复 turn，提示里写明错在哪里
- **兜底**：
  - 规划仍然失败（修复后仍不合规、Claude 出错、超时、无法启动）：任务失败（fail-closed），原因写进 `tasks.error` 和 activity，不会退回成单步执行
  - 决策仍然失败：按这一步自己的结论处理（Worker 的 fail-closed 结论，或 Reviewer 的结论），不另起 Reviewer，并记一条 `Warning: the Planner could not decide ...` 的 error activity
  - 聊天 turn 失败：把错误回给用户，不做任何动作
  - Claude 进程中途退出：这个 turn 失败，下一个 turn 用 `--resume <session_id>` 起新进程，session id 存在 Planner 的计划文件里
- **结果**：任务完成时 `tasks.result_summary` 是最后一个 accept 的步骤给出的 summary（没有时用自动生成的逐步摘要）；任务失败时 `tasks.error` 是 Planner 的失败摘要
- **配置**：`agent_configs` 的 `planner` 行每个 turn 读一次。`system_prompt` 是可编辑的角色提示，代码在前面放 harness，在后面接上项目段、任务段（任务 id、项目名和简介、工作目录、任务需求）和回复契约；每个 turn 的消息只带变化的内容（计划进度、结果、聊天、监督观察），不再重复任务需求；`timeout_seconds` 限制每个 turn（默认 1200 秒，0 表示不限），超时的 turn 失败并杀掉 Claude；`max_turns` 大于 0 时传 `--max-turns`，`model` 有值时用它，否则用 Settings 的默认模型。命令、系统提示的哈希或 Settings 环境变量的哈希变了（包括项目背景、harness、模型变了），下一个 turn 用 `--resume` 重启 Claude，并用新的系统提示（`--system-prompt-snapshot off`，否则 Claude Code 续接时沿用会话第一次请求的旧提示）。数据库里还是旧默认提示的会在启动时换成新默认，改过的保留
- **记录**：每个 turn 都会产生 Claude 费用，各记一条 `llm_records`（agent_id 为 `planner-<task_id>`，带这个 turn 自己的花费和进程 `pid`）；决定以简短摘要写进 activity。Manager、Planner、Worker、Reviewer 的每次运行都在 `llm_records.metadata` 记下 LoopX 追加的系统提示（`system_prompt`）、它的 `system_prompt_sha256` 和各层来源 `prompt_layers`（harness 的来源与哈希、角色、项目、任务、契约、隔离参数版本），项目层还记下知识索引的哈希和文档数，以及 `model_choice`（请求的模型、来源、effort）、Settings 环境变量的名称（值只记哈希）和加载的插件库条目（`library`），`metadata.command` 仍是完整 argv；系统提示文件按哈希存在 runtime root 的 `.local/prompts/` 下，只写一次。会话历史页的每条运行有可折叠的「System prompt」段，标题显示长度、短哈希和 `isolated` 标记，展开后先逐层列出来源（harness 来源、角色是推荐版还是改过的、项目和背景版本、任务、契约），再是模型及其来源、环境变量名称，最后是原文；不含 Claude Code 内置的系统提示。另有可折叠的「Plugins & skills」段，标题显示加载了几个插件、几个 skill 和库的短哈希，展开后逐条列出名称和版本哈希。Chat 页的 Planner 和 Worker/Reviewer 视图复用同一个组件
- **不限权限**（RFC 决策 8）：四类 agent 都开放全部工具、跳过权限确认。这个参数只在一处定义（`claude_cli.permission_args`，即 `--dangerously-skip-permissions`），由 `agent_settings.Launch.args` 和插件库参数一起放进每个 agent 的启动参数，四类 agent 不会不一致；启动命令里不再有 `--tools`、`--allowedTools`、`--permission-mode`。职责边界改由 role prompt 和代码契约约束：推荐的 Planner role prompt 写明它负责规划和决策、不亲自修改文件，具体改动都作为步骤交给 Worker。Manager 因此能在本机执行任何命令，所以飞书、Telegram 渠道必须配置发送者白名单（见「接入渠道」）。用 Claude Code 2.1.285 核实：`--print` 加隔离参数和 `--plugin-dir` 时，init 事件的 `permissionMode` 为 `bypassPermissions`，Bash、Write 和声明了 `allowed-tools` 的插件 skill 都不再被拒绝；隔离用的 `--settings` 不影响权限
- **不加载操作者的个人配置**：四类 agent 的启动命令都带同一组隔离参数（`claude_cli.isolation_args`）：`--setting-sources project`、`--strict-mcp-config`，以及 `--settings` 里的 `autoMemoryEnabled: false` 和 `claudeMdExcludes`（排除工作目录所在仓库以上各级目录的 `CLAUDE.md`、`.claude/CLAUDE.md`、`.claude/rules/`，以及 `~/.claude` 下的用户记忆）。agent 只看到 LoopX 拼装的内容和项目仓库自己的 `CLAUDE.md`；操作者的 `~/.claude/CLAUDE.md`、用户设置、hook、个人 skill、插件、自定义 agent、输出风格、MCP 和自动记忆都不加载，登录不受影响；插件和 skill 只经由上面的插件库进来。核实过程见 [Agent 系统架构](architecture/agent-system.md#what-an-agents-claude-code-loads)

### 花费与上下文

长程运行的成本和上下文增长都可以看到（RFC 第 8 节，M2；`loopx/agents/run_usage.py`）。每次运行（`llm_records`）记录以下内容：

- **自身的花费和 token**：`cost_usd`、`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_creation_tokens`。Claude Code 在每个 `result` 里报告会话的累计值：`total_cost_usd` 和 `modelUsage`，后者覆盖所有请求，包括压缩时的摘要请求和子 agent 的请求。一次运行自己的份额是累计值的增量。`result.usage` 只算本轮主循环的请求，漏掉了压缩和子 agent，所以不用它（只在没有 `modelUsage` 时兜底）。
  - Worker 和 Reviewer 一次运行就是一个进程，增量就是进程的累计值。
  - Manager、Planner 的每个 turn 减去上一个 turn 结束时的累计值。用 `--resume` 起的新进程会接着会话原来的累计值报告：已核实，一个已花 $0.01046 的会话续接后，第一轮报 $0.01279，其中本轮只花了 $0.00232。所以会话的累计值和会话 id 存在一起：Manager 存在 `session.json`，Planner 存在计划文件。续接时以存下的值为起点；会话因为无法续接被丢弃时，累计值一起清零。
  - 修复前，续接后的第一轮会把整个会话以前的花费再记一遍。重启服务、改 prompt 或设置导致进程重启，都会触发这个问题。
- **结束时的上下文规模**（`context_tokens`）：本轮最后一个主循环请求的 prompt 大小，即 input + cache read + cache creation，不算子 agent 的请求。
- **上下文压缩**（`compactions`）：Claude Code 每次压缩（自动的，或以 user turn 发送的 `/compact`）都会输出一个 `system/compact_boundary` 事件。每次压缩记一项 `{trigger, pre_tokens, post_tokens, duration_ms}`；没有压缩时为 `[]`，M2 之前的记录为 null。

读取方式：

- `GET /api/tasks/<id>` 和 `GET /api/tasks` 的每个任务都带 `cost`：总额 `cost_usd`、`runs`、`running`（还在跑，花费未知）、`unpriced_runs`（结束了但没报花费）、`by_role`（planner、worker、reviewer 各自的 `cost_usd` 和 `runs`）。
- `GET /api/agents/<agent_id>/usage` 给出一个 agent 的数据，例如 `manager`、`planner-<task_id>`：
  - 总花费、运行数、压缩次数；
  - 按会话和按 UTC 日的花费；
  - 最近 N 个 turn（默认 100）各自的上下文、花费和压缩，以及最新的上下文规模。

---

## 消息流

### 完整请求链路

```
1. 用户发消息 → Channel Adapter
2. Adapter 转换为 USER_MESSAGE → Bus → Manager
3. Manager 调用 Claude Code 理解意图
4. Manager 发 TASK_REQUEST → Bus → TaskRouter
5. TaskRouter 创建 Task + Planner 实例
6. Planner 启动常驻 Claude，[PLAN] turn 拆出步骤，发 TASK_ACCEPTED
7. Planner 按依赖顺序发 WORK_ITEM → Bus → WorkerPool（每个任务最多 4 个并行）
8. Worker 子进程执行，返回 WORK_RESULT
9. Planner 的 [DECISION] turn 决定 accept / review / retry / replan / fail；
   review 时另起 Reviewer（VERIFY_REQUEST → VERIFY_RESULT），之后再决定一次
10. 所有 Step 完成 → Planner 发 GOAL_STATUS(completed) → TaskRouter，写入 tasks.result_summary
    （任务失败时发 GOAL_STATUS(failed)，写入 tasks.error）
11. TaskRouter 转发给 Manager → Channel → 用户
12. TaskRouter 停掉 Planner 和它的 Claude 进程
```

### 消息类型一览

| 类型 | 方向 | 用途 |
|------|------|------|
| `USER_MESSAGE` | Channel → Manager / Planner | 用户输入；发给 Planner 的消息由它的 Claude 回复，可以转给正在执行的 Worker 或追加步骤 |
| `AGENT_REPLY` | Manager / Planner → Channel | 回复用户 |
| `TASK_REQUEST` | Manager → TaskRouter | 创建任务 |
| `TASK_ACCEPTED` | Planner → TaskRouter → Manager | 任务已接受 |
| `TASK_CANCEL` | Manager → TaskRouter → Planner | 取消任务 |
| `GOAL_STATUS` | Planner → TaskRouter → Manager | 进度/结果 |
| `WORK_ITEM` | Planner → Worker | 派发执行 |
| `WORK_RESULT` | Worker → Planner | 执行结果 |
| `VERIFY_REQUEST` | Planner → Reviewer Worker | 验证请求 |
| `VERIFY_RESULT` | Reviewer → Planner | 验证结论 |
| `WORKER_INPUT` | Planner → 运行中的 Worker | 转给 Worker 的用户消息，作为它的下一个 user turn |
| `WORKER_INPUT_RESULT` | WorkerPool → Planner | 是否送达 |
| `STATUS_UPDATE` | Manager → Channel | 流式状态 |
| `HEARTBEAT` | System | 健康检查 |
| `AGENT_LIFECYCLE` | Supervisor ↔ Manager | 生命周期事件 |

---

## 接入渠道

Web 始终开启。Telegram 和 Lark 默认内置，不需要任何启动参数：在控制台 **Settings** 页启用并编辑配置，配置存进数据库 `channel_configs` 表。服务启动时读取一次，改完配置要重启 `loopx serve` 才生效。

**发送者白名单**（RFC 决策 8，`loopx/agents/channels/allowlist.py`）：Manager 能在本机执行任何命令，所以 Telegram 和飞书只把白名单里的发送者的消息交给 Manager。不在名单里的消息不会到达 Manager：只记一条日志（渠道和发送者 ID，不含消息内容），不回复。白名单为空或格式不对的渠道不启动，启动日志和 Settings 页（接口的 `start_problem`）写明原因。每个名单最多 100 个 ID；接口逐个校验，按 JSON 数组保存，格式不对返回 400 并指出第一个错误的 ID。Settings 页每个渠道有「Allowed senders」列表，可以增删，输入时就校验格式，空行和重复会标出。Web 渠道不受影响：接口只接受本机访问。

数据库里只存环境变量的**名字**，不存密钥本身；密钥放在对应的环境变量或仓库根目录的 `.env` 里。某个渠道启动失败只记日志，不影响 Web 控制台。

### Web

- 控制台 Chat 页
- 左上角列出所有 agent（`GET /api/chat/targets`）：全局唯一的 Manager，以及最近 30 个任务。每个任务下有它固定的一个 Planner，和每个步骤各自独立的 Worker（做了验收的步骤还有 Reviewer）。已经结束的任务和 agent 也列出来，可以查看消息
- 能发消息的只有 Manager 和运行中任务的 Planner。发给 Planner 的消息由它的 Claude 回复（一个 `[CHAT]` turn），Claude 可以把消息转给正在执行某一步的 Worker，作为它的下一个 user turn，也可以给计划追加步骤；Worker 已经结束的，这条消息留到这一步的决策 turn 给 Claude 看。事件按到达顺序处理，正在跑别的 turn 时发来的消息会排在它后面；任务还没有计划或已经结束时，消息不经过 Claude，直接回复。选中 Planner 时，旁边折叠显示它最近 10 次 Claude 运行。Worker 和 Reviewer 只读，页面显示它的完整运行记录
- `POST /api/chat/send` 发送消息，`target` 指定对话对象
- 花费与上下文的展示：
  - 任务列表的每一行都显示任务目前的花费。
  - 任务详情的 Overview 有一张「Cost」卡片，显示总额和运行次数。下面的「Cost by agent」列出 Planner、Workers、Reviewers 各自的运行次数、花费和占比，并注明还在跑的运行和没有报出花费的运行。
  - Manager 和 Planner 的历史页开头是「Context & cost」：
    - 最新的上下文规模、压缩次数、总花费和 turn 数；
    - 最近 100 个 turn 结束时上下文的折线，按最小值和最大值缩放，发生过压缩的 turn 画一条虚线；
    - 压缩清单（第几个 turn、时间、触发方式、压缩前 → 压缩后）；
    - 一张可展开的 turn 表。
  - 每张运行卡片显示这次运行自己的 token（未缓存输入、缓存读、缓存写、输出）、结束时的上下文 `ctx`、`compacted` 标记和自身花费。记录里的压缩事件显示为一行。`result` 那一行的金额标为 `session total`，因为那是 Claude 报告的会话累计值。
- SSE `GET /api/chat/events` 接收实时响应
- Settings 页的「Agent Defaults」：默认 harness（实时显示 `n / 20000`，标明内置或已自定义，可恢复内置）、默认模型、effort 和环境变量。「Plugins & skills」：已加载几个、总大小，一行说明这些会加载进每个 agent；「Import from this machine」或「Re-import」和上次导入的时间、变化与未导入的内容；按名称筛选；插件和 skill 分组列出，各有版本、大小和启用开关，改动对每个 agent 的下一次运行生效。Agents 页列出 Manager、Planner、Worker、Reviewer：原来的提示编辑框改叫「Role prompt」；模型输入框的占位符显示继承的默认模型；Manager 多一个「Harness prompt (override)」，为空时显示继承默认，其他三类说明 harness 从哪里来；原来的「Permission Mode」下拉框（这些 agent 从来不读它）改为一行说明：所有 agent 都开放全部工具、带 `--dangerously-skip-permissions`
- 左侧栏导航下面列出所有项目（任务数，有进行中任务时显示进行中的数量），点击打开按该项目筛选的 Tasks 页，`+` 新建项目。Tasks 页可以按项目筛选；选中项目时显示项目信息和背景（默认显示 3 行，可展开；为空时显示 No background），可以编辑（背景用等宽文本框，实时显示 `n / 10000`，超限不能保存；可折叠的「Harness overrides」里编辑 Planner 和 Worker 的 harness，为空时显示继承默认；Save 下面是「Knowledge」：列出文档（标题、摘要、来源、文件名或路径、大小，引用文件不存在时标 Missing）和篇数、存储量、索引字数相对上限的读数，可以新增文本、上传文件、引用项目文件，删除前确认；这些操作立即生效，不经过 Save，被拒绝时显示服务端给出的原因；Knowledge 下面是「Skills」：列出项目 skill（`project:<名称>`、正文大小、停用的标 Off、描述）和个数、启用数相对上限的读数，可以新增（名称、描述、`SKILL.md` 正文）、开关、删除前确认，同样立即生效），没有任务时可以删除。新建项目的对话框也可以填背景。新建任务时先选项目或当场新建一个，项目有默认目录时工作目录可以不填。任务详情里可以把任务移到其他项目，任何状态都可以
- 项目接口：`GET/POST /api/projects` 列出（带任务数和进行中任务数）和新建项目，`PUT/DELETE /api/projects/<id>` 修改和删除，`GET/POST /api/projects/<id>/knowledge` 列出和新增项目知识、`PUT/DELETE /api/projects/<id>/knowledge/<doc_id>` 修改和删除（不返回正文），`GET/POST /api/projects/<id>/skills` 列出和新增项目 skill、`PUT/DELETE /api/projects/<id>/skills/<skill_id>` 修改（含启用开关）和删除（不返回正文，重名返回 409），`background` 超过 10000 个字符、harness 超过 20000 个字符返回 400；Agent 默认值是 `GET/PUT /api/settings/agents` 和 `POST /api/settings/agents/reset-harness`；插件库是 `GET /api/settings/library`、`POST /api/settings/library/import` 和 `PUT /api/settings/library/<id>`（`{"enabled": true|false}`）；`POST /api/tasks/create` 在已有项目或新项目里建任务；`PUT /api/tasks/<id>/project` 把任务移到另一个项目；`GET /api/tasks?project_id=<id>` 按项目列出任务

### Telegram

- Bot 长轮询接入
- 支持 `/start`、`/status`、`/help` 命令
- 消息自动分段（4000 字符限制）
- Settings 配置项：
  - `bot_token_env`：存放 Bot token 的环境变量名，默认 `LOOPX_TELEGRAM_BOT_TOKEN`
  - `allowed_user_ids`：发送者白名单，Telegram 的数字用户 ID（负数是群或频道，不是发送者）；以前存的逗号分隔字符串仍然能读。`LOOPX_TELEGRAM_ALLOWED_USERS` 不再生效
- 名单外的发送者发 `/start`、`/status`、`/help` 也得不到回复

### Lark（飞书）

- 需要 `lark-cli` 在 PATH 上且已登录，收发消息都通过它
- Settings 配置项 `app_id_env`：存放 App ID 的环境变量名，默认 `LARK_APP_ID`。`app_secret_env` 目前不会被读取，凭据由 `lark-cli` 管理
- Settings 配置项 `allowed_open_ids`：发送者白名单，飞书用户在这个应用下的 `open_id`（`ou_` 开头），即每条消息事件里的 `event.sender.sender_id.open_id`；`union_id`、`user_id` 会被拒收
- 通过 `lark-cli event consume` 订阅事件
- 支持 @mention 触发
- 富文本回复

---

## 生命周期管理

### AgentSupervisor

- 统一管理所有 Agent 的启动/停止
- 健康检查循环（30s 间隔）
- 静态 Agent（Manager、TaskRouter、WorkerPool）：指数退避重启，最多 3 次
- 动态 Agent（Planner）：由 TaskRouter 管理生命周期，不参与自动重启

### MessageBus

- 基于 `asyncio.Queue` 的进程内消息总线
- JSONL 日志持久化 + crash recovery。重启时，发给 Manager、TaskRouter 和渠道的未送达消息会重放；发给 Planner、Worker 的会丢弃，因为它们是按任务创建的实例，任务在服务启动时已被停掉，重放会重跑这些任务的活。之后压缩日志
- Topic 订阅：默认只观察；WorkerPool 用认领式订阅（`consumes=True`）接收 `WORK_ITEM` 和 `VERIFY_REQUEST`，接收后即标记为已送达

---

## 启动方式

```bash
# 启动 Agent 系统（在源码目录下运行）
uv run loopx serve

# 或 Python 调用
from loopx.agents.serve import serve_agent
serve_agent(host="127.0.0.1", port=8767)
```

Telegram 和 Lark 不需要启动参数，见「接入渠道」。

---

## 并发模型

- **Task 级并发**：TaskRouter 通过 Semaphore 控制最多 3 个 Task 并行
- **Worker 级并发**：WorkerPool 通过 Semaphore 控制最多 4 个 Worker 子进程并行
- **Task 隔离**：每个 Task 有独立的 Planner、Plan、状态持久化目录和 Planner 的 Claude 会话
- **Worker 隔离**：每个 Worker 在独立子进程中运行 `claude --print`

---

## 目录结构

```
loopx/agents/
├── bus.py           # MessageBus + JSONL Journal
├── message.py       # MessageEnvelope 协议 + 地址常量
├── supervisor.py    # AgentSupervisor 生命周期管理
├── serve.py         # 启动入口
├── claude_cli.py    # ResidentClaude（常驻 Claude 进程）+ Claude 运行记录
├── channels/
│   ├── registry.py  # Channel 注册表
│   ├── web.py       # Web HTTP+SSE 适配器
│   ├── telegram.py  # Telegram Bot 适配器
│   └── lark.py      # 飞书适配器
└── tiers/
    ├── manager.py   # Manager Agent（管家）
    ├── task.py       # Task 实体 + TaskRouter
    ├── planner.py   # Planner Agent（编排器，per-task，常驻 Claude Code）
    ├── planner_protocol.py  # Planner 回复契约：类型和校验
    └── worker.py    # WorkerPool + WorkerProcess
```

---

## 界面设计（`web/`）

`web/` 是 agent 系统的 dashboard（React 19、Vite、Tailwind 4）。改动或复现任何界面之前先读这一节。有维护者认可的设计稿或截图时以它为准，本节只补充它没有规定的细节。

### 设计令牌与组件

- 颜色、字体和圆角只用 `web/src/index.css` 里 `@theme` 定义的令牌：深色底，`--color-primary` 是靛蓝强调色，字体是 Inter 和 JetBrains Mono。组件里不写一次性的颜色、圆角或间距。
- 先复用 `web/src/components/ui/` 里的基础组件（Badge、Button、Card、Input、Switch），不为某一个页面另起一套视觉语言。
- 颜色用来表示状态，外加一处主要强调，不用来铺满大块区域。结构靠留白、网格和细边框。不用玻璃拟态、霓虹光晕、重阴影或大面积模糊。

### 赢得注意力

视口里每个可见元素至少要提供下面一样：

1. 改变用户理解或决定的**高价值信息**；
2. 用户此刻需要的**必要交互**；
3. 值得一看的**呈现**：清楚的构图、有用的对比，或帮助理解的动效。

这是"或"的关系，不要求每个元素三样都有，也不是追求极简。装饰性的堆砌、原始协议字段、重复的状态、给每项能力都配一个按钮，都不会因为摆在那里就值得注意。要按整个视口判断，包括多个组件重复展示的信息。

- 动手前先列出可见元素和各自的价值。对话、结果和待决定的事要放在显眼处，常规活动合并展示。相关的控制和细节一步可达，不要连跳几页。
- 不要把失败、缺少的权限、不确定的观察或需要判断的操作折叠成一句让人放心的成功摘要。界面只挑选事实，不从文字或计数推断出"已执行""已通过"或"已完成"。

### 状态与隐私

- 颜色只是辅助信号，状态一定同时有文字或图标。
- 界面不显示凭据或密钥。PR 里的截图去掉本机路径和私有数据。

### 动效、可访问性与响应式

- 动效只用来解释状态变化，不装饰静止的内容。控件过渡 120–200ms，区块过渡 200–350ms，优先用透明度和小幅位移，并遵守 `prefers-reduced-motion`。不要让大块区域持续闪动。
- 对比度达到 WCAG AA。用语义化 HTML，键盘焦点要看得见，交互目标尽量不小于 44px。支持键盘导航、缩放和窄视口，中英文布局同样可读。
- 窄屏单列，宽屏再分栏。图表和代码不要缩到看不清，改为重新排版，或在限定区域内横向滚动。

### 改界面的流程

1. 先读本节，再看真实页面、源码或设计稿，列出行为、状态、断点和文案。
2. 没有规定的视觉细节，对应到上面的令牌和组件。
3. 在真实浏览器里检查（无头 Chrome 即可，见开发计划第 1 节）：
   - 桌面宽度（如 1280 px）和窄屏（如 768 px）；
   - 有数据、为空、出错和不可用几种状态。
4. 迁移框架或复现已有界面时，外观和行为都必须保持一致。
5. PR 里附截图：前后对比，说明哪些元素值得注意、哪些被合并，以及键盘操作和返回原处是否正常。

### 检查清单

- [ ] 读过并遵守本节。
- [ ] 复用了 `@theme` 令牌和 `components/ui` 组件，没有重复定义。
- [ ] 在浏览器里看过桌面和窄屏。
- [ ] 键盘、焦点、对比度和减少动效的行为都保留。
- [ ] 中英文内容都可用。
- [ ] 首屏或高保真的改动附了截图。
- [ ] 界面和截图里没有私有数据或本机路径。
