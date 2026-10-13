# RFC：长程 Agent 的上下文体系（v0）

- **RFC 状态：** Draft，维护者评审中。
- **交付成熟度：** Partial。第 4 节的分层已在四类 agent 上落地，项目背景和 system prompt 记录已交付（M0，looper #13）；M1 起为提案。
- **创建日期：** 2026-10-13
- **实现基线：** `c600594ea`（foolzzz/looper `main`，含 M0）
- **相关文档：** [Agent 系统架构](../agent-system.md)、[产品设计](../../design.md)
- **语言：** 本 RFC 先以中文评审，定稿后补英文镜像。

## 1. 背景与目标

LoopX 的 agent 系统要跑**长程任务**：一个任务从规划到验收可能持续数小时，经过多个步骤、多个 Worker 和 Reviewer，
期间 Planner 持续监督、用户随时插话、进程可能重启。长程运行最常见的失败不是"不会做"，而是：

1. **跑偏**：规则和格式在一轮轮对话里被稀释，agent 忘了契约或目标；
2. **串味**：不该看到的上下文混进来（别的对话、操作者的个人配置、上一个任务）；
3. **不可见**：出了问题无法还原"当时 agent 到底看到了什么"；
4. **失控的成本**：上下文无界增长，或失败循环反复烧钱。

本 RFC 的目标是给四类 agent（Manager、Planner、Worker、Reviewer）定一套统一的**上下文体系**：
每类信息放在哪一层、由谁拥有、何时生效、如何记录；并据此给出分阶段的开发规划。

非目标：调度与并发策略、多租户权限、渠道协议，这些另立文档。

## 2. 现状盘点（M0 之前，`b35b9b530`）

M0 交付后，Worker 和 Reviewer 已按第 5 节的目标形态拼装 system prompt，四类 agent 都记录 `metadata.system_prompt`。
下表保留 M0 之前的状态，作为问题的出处。

| Agent | 进程模型 | system prompt（`--append-system-prompt`） | user 消息 | 会话 |
|---|---|---|---|---|
| Manager | 全局常驻 1 个 | 角色 prompt（`agent_configs.manager`）+ `TASK_PLACEMENT_RULE` | 项目列表 + 目标快照 + 用户消息 | **全局一个 session**，所有网页对话、飞书、Telegram 共用，重启后 `--resume` |
| Planner | 每任务常驻 1 个 | 角色 prompt（`agent_configs.planner`）+ `REPLY_CONTRACT` | PLAN / DECISION / CHAT / SUPERVISE 四种 turn，每轮带需求、项目名与简介、计划状态、事件 | 每任务一个，存于计划文件，`--resume` 续接 |
| Worker | 每步骤 1 个 | **无**（M0 中改造） | 首条 stdin：角色、规则、报告 JSON 契约、步骤指令全部混在一起；运行中可追加 `WORKER_INPUT` | 一次性 |
| Reviewer | 每次验收 1 个 | **无**（M0 中改造） | 首条 stdin：验收契约 + 待验收结果 | 一次性 |

已有的防跑偏机制：Planner 回复的 JSON 由 `planner_protocol` 严格校验并允许一次修复；Worker 完成判定 fail-closed
（`judge_result`）；步骤上限 8、每步重试上限 2；Planner 周期性 SUPERVISE turn 可纠偏或终止运行中的 Worker；
推荐 prompt 跟踪（`agent_configs.default_prompt`，未改过的行随推荐升级，改过的保留）。

## 3. 设计原则（规范性）

- **P1 稳定进 system，变化进 user。** 整个会话不变的内容（角色、规则、契约、项目背景）放 system prompt；
  每轮变化的内容（任务、状态、事件）放 user 消息。动态内容不得进 system prompt：常驻进程的 system prompt
  一变就要重启并续接会话，也会让提示词缓存失效。
- **P2 契约放最后，由代码校验。** 输出格式等硬性契约是 system prompt 的最后一段，由代码拥有、不可在
  Agents 页编辑；遵守与否由代码严格校验（失败给一次修复，仍失败按 fail-closed 处理），不依赖模型自觉。
- **P3 状态归代码，每轮重新接地。** 计划、步骤状态、项目列表等权威状态存于代码和数据库，每一轮由代码重新
  注入 user 消息。模型的会话记忆只是缓存，不是事实来源；会话被压缩或续接后，下一轮依然拿到正确状态。
- **P4 信任分层。** system prompt 只放可信内容（代码契约、维护者配置的角色 prompt、项目档案）。
  不可信内容（Worker 输出、用户聊天、网页、工具结果）只能作为 user 消息里的数据出现。
- **P5 上下文隔离。** agent 只看到它该看到的：会话不跨对话、不跨任务；不继承操作者个人的 MCP、hook、
  设置和记忆文件；项目之间互不可见。
- **P6 可复现。** 每次运行都能还原它的完整上下文：启动参数、追加的 system prompt 原文及其版本哈希、
  事件流、花费。
- **P7 有预算。** 每一层都有体积上限；大块资料不塞进 prompt，而是放进可读目录让 agent 按需读取；
  运行和任务有花费上限。

## 4. 分层模型

```
system prompt（由 LoopX 追加，按顺序拼装）
  L0  Claude Code 内置 system prompt        —— 不受控，不记录原文
  L1  角色 prompt                            —— agent_configs.<role>.system_prompt，可编辑，跟踪推荐版本
  L2  项目档案                              —— projects.background（M0）；知识索引、skill 清单（M3/M4）
  L3  代码契约                              —— REPLY_CONTRACT / WORK_CONTRACT / REVIEW_CONTRACT / TASK_PLACEMENT_RULE

user 消息（每轮）
  G   接地状态（代码生成）                  —— 计划与步骤状态、项目列表、目标进度、工作目录
  E   事件与数据                            —— 步骤指令、Worker/Reviewer 结果、用户聊天、监督观察（不可信内容只在这里）
```

拼装由一个共享模块完成（M0 中引入），四类 agent 共用同一套拼装与记录逻辑。L2 为空时省略整段。

**生效时机：** L1、L2 修改后立即生效。一次性进程（Worker、Reviewer）在下次启动时读取最新值；常驻进程
（Manager、Planner）在下一轮比较拼装结果，变化则重启并 `--resume`，已在运行的 Worker 不受影响。

## 5. 各 Agent 的目标形态

| Agent | L1 角色 | L2 项目档案 | L3 契约 | G 接地状态 | E 事件与数据 |
|---|---|---|---|---|---|
| Manager | 管家角色 | 不注入（跨项目） | `TASK_PLACEMENT_RULE` | 项目列表、目标快照 | 用户消息 |
| Planner | 规划与决策角色 | 所属项目 | `REPLY_CONTRACT` | 需求、计划、步骤状态、重试计数 | Worker/Reviewer 结果、用户聊天、运行中 Worker 的近况 |
| Worker | 执行角色 | 所属项目 | `WORK_CONTRACT`（报告 JSON） | 工作目录、依赖步骤的结果摘要 | 步骤指令、验收标准、重试反馈、运行中纠偏 |
| Reviewer | 验收角色 | 所属项目 | `REVIEW_CONTRACT`（`passed` 结论） | 工作目录 | 待验收步骤的指令、标准、Worker 报告 |

## 6. 项目档案（Project Profile）

项目档案是"这个项目长期有效、所有 agent 都该知道的东西"。按已定的方案 A，每类属性用专门的类型化字段或表，
不用通用 JSON。

| 属性 | 阶段 | 存储 | 注入方式 | 说明 |
|---|---|---|---|---|
| 默认目录 `work_dir` | 已有 | `projects.work_dir` | 任务目录的缺省值 | |
| 背景 `background` | M0 | `projects.background`，上限 10,000 字 | L2 全文 | 技术栈、约定、约束、关键链接；不得写密钥 |
| 知识 `knowledge` | M3 | `project_documents` 表（标题、摘要、正文/来源） | L2 只放索引（标题 + 一句摘要）；正文物化到项目专属目录，以 `--add-dir` 挂载，agent 按需 Read | 解决"资料太大不能塞 prompt" |
| 技能 `skills` | M4 | `project_skills`（SKILL.md 内容或来源） | 物化为本地插件目录，以 `--plugin-dir` 挂载；L2 放清单 | 只对该项目的 agent 生效，不写进用户仓库 |
| 执行配置 | M5 | `projects` 的类型化列 | 不进 prompt，转为启动参数 | 模型、单次运行花费上限、监督间隔、验收策略（全部送审 / 由 Planner 决定） |
| 密钥 | 不做 | — | 永不进 prompt | 需要时沿用渠道配置的做法：只存环境变量名 |

档案编辑后立即生效（见第 4 节），每次生效写一条 activity，并在运行记录里留下版本哈希。

## 7. 防跑偏机制

| 机制 | 状态 | 作用 |
|---|---|---|
| 契约在 system prompt 末尾 + 代码严格校验 + 一次修复 | 已有（Worker、Reviewer 于 M0 补齐） | 格式和规则不被对话稀释 |
| 状态由代码每轮重新注入 | Planner 已有 | 压缩、续接后不丢状态 |
| fail-closed 完成判定 | 已有 | Worker 自报完成不算数，要和磁盘一致 |
| 步骤上限 8、每步重试上限 2 | 已有 | 防止失败循环 |
| Planner 周期监督（纠偏 / 终止） | 已有（#4） | 运行中途发现方向错误 |
| Worker 每步新进程 | 已有 | 单步上下文有界，失败不污染后续步骤 |
| Planner、Worker 会话按任务、步骤隔离 | 已有 | 任务之间不串上下文；Manager 的全局会话是有意为之，见第 9 节问题 1 |
| 不继承操作者个人配置 | **部分**（M1） | Worker、Planner 已加 `--strict-mcp-config`；用户级设置和记忆文件待完全隔离 |
| 上下文与花费观测 | **缺**（M2） | 常驻进程上下文增长、自动压缩、任务累计花费都不可见；默认不设花费上限，由项目按需配置（M5） |
| prompt 改动回归评测 | **缺**（M6） | 改推荐 prompt 或契约前，没有办法确认不退化 |

## 8. 可观测性

每次运行（`llm_records`）应能回答"它看到了什么"：

- 已有：完整启动参数（`metadata.command`）、工作目录、事件流、token、花费、心跳。
- M0（已交付）：`metadata.system_prompt`（LoopX 追加部分的原文），历史页展示。
- M1：`metadata.system_prompt_sha` 与各层来源（角色 prompt 版本、项目档案版本、契约版本），便于把行为变化
  对应到具体的 prompt 变更；长 prompt 改用 `--append-system-prompt-file` 传入，启动命令保持可读。
- M2：每轮的输入 token、是否发生自动压缩、会话累计 token；任务累计花费。

## 9. 已知问题与风险

1. **Manager 全局只有一个会话（已核实，决定保留）。** `manager.py` 的 `session_id` 是全局的，所有网页对话、
   飞书、Telegram 共用同一个 Claude 会话。决定保留：Manager 是用户唯一的管家，跨渠道连续的上下文是特性
   而不是缺陷。风险由两点兜住：项目列表、目标进度等状态每轮由代码重新注入（P3），会话增长由自动压缩
   承担并在 M2 中可观测。网页上的"新对话"只是页面视图的分隔，界面文案应如实说明。
2. **agent 可能加载操作者个人的设置和记忆文件（需要验证）。** Claude Code 默认会读用户级设置和
   `~/.claude/CLAUDE.md`。如果操作者在里面写了个人规则（语言、工作流、私有知识库路径），会进入每个 Worker
   和 Planner 的上下文。需要先验证当前 CLI 的加载行为，再决定用 `--setting-sources`、`--bare` 等方式隔离。
3. **常驻进程的上下文增长不可见。** Planner、Manager 会话越来越长，自动压缩何时发生、压掉了什么，目前无从
   得知。P2、P3 保证契约和状态不丢，但仍需观测。
4. **花费不可见。** 默认不设上限（已决定），但单次运行和任务的累计花费、上下文用量目前都看不到；失败循环的
   上界只由步骤数和重试数间接限制。
5. **prompt 改动无法评估。** 改推荐 prompt 或契约后，只能靠真实任务碰运气。
6. **长 prompt 放在启动参数里。** 项目背景加角色 prompt 可达上万字，混在 argv 里不利于阅读，也接近命令行长度的实际限制。

## 10. 开发规划

每个阶段都是一个可以独立评审、独立回滚的 PR（或一小组 PR）。验收都要求单测加真实路径验证。涉及模型可见
内容的改动，还要用真实 Claude 跑一次。

| 阶段 | 内容 | 验收标准 | 依赖 |
|---|---|---|---|
| **M0（已交付，looper #13）** | 项目背景字段；共享的 system prompt 拼装；Worker、Reviewer 的 L1+L2+L3 分层，契约移出 stdin；Planner 每轮实时读取项目；四类 agent 记录 `metadata.system_prompt`，历史页展示 | 真实 Claude 下 Worker 报告判为 completed、Reviewer 给出 `passed`；修改背景后下一个 Worker 生效、Planner 续接重启；历史页能看到原文 | 无 |
| **M1 上下文隔离与可复现** | 验证 Claude Code 对用户级设置和 `~/.claude/CLAUDE.md` 的加载行为，并让四类 agent 完全不加载操作者个人配置（项目仓库自己的 CLAUDE.md 保留）；所有项目都要遵守的全局规则改写进角色 prompt；`--append-system-prompt-file`；记录 prompt 哈希和各层版本 | 在操作者 CLAUDE.md 里放一条可识别的标记，四类 agent 的上下文里都看不到它；同一 prompt 哈希可复现同一上下文 | M0 |
| **M2 上下文与花费观测** | 记录每轮输入 token 和自动压缩事件；单次运行和任务的累计花费；dashboard 展示任务花费和常驻进程的上下文用量。默认不设花费上限 | 任务页和 Manager、Planner 的历史页能看到累计花费、上下文用量和压缩次数 | M0 |
| **M3 项目知识** | 项目文档的增删改查，来源支持手动录入、上传文件、引用项目目录内的路径（实时读取，不支持 URL）；物化目录 + `--add-dir`；L2 注入索引；界面管理 | agent 能按索引读到文档并在报告里引用；引用的仓库路径改动后 agent 读到新内容；索引超限时拒绝保存并提示 | M0 |
| **M4 项目 skill** | 项目 skill 的管理与物化；`--plugin-dir` 挂载；L2 清单 | 只有该项目的 agent 能用这个 skill；不写入用户仓库 | M3（复用物化机制） |
| **M5 项目执行配置** | 每个项目的模型、可选的花费上限（单次运行 `--max-budget-usd`、任务累计；超限的处理方式随配置一起定）、监督间隔、验收策略 | 配置在对应 agent 的启动参数和行为里可读回；配置了上限的项目在上限处停下并说明原因 | M2 |
| **M6 上下文回归评测** | 固定的小型任务集（替身 claude 测流程，少量真实 Claude 测效果），修改推荐 prompt 或契约前必须跑；结果存档对比 | 一次契约改动能在评测里显出差异；评测结果写进 PR | M1 |

**顺序依据：** M0 已交付，是后面所有阶段的基础。M1 让 agent 的上下文不受运行它的机器影响，是行为可控、
可复现的前提。M2 让成本和上下文增长可见，是长程运行的安全网。M3 到 M5 扩展项目档案。M6 在 prompt 开始
频繁调整之前就位。

## 11. 决策记录

2026-10-13，维护者对 v0 的待决问题作出决定：

1. **Manager 不拆分会话。** 保留全局唯一的 Manager 会话（见第 9 节问题 1），M1 不再包含拆分。
2. **默认不设花费上限。** M2 只做记录和展示；上限作为可选的项目执行配置放进 M5。
3. **项目知识的来源**：手动录入、上传文件、引用项目目录内的路径；不支持 URL。
4. **完全隔离操作者个人配置**：agent 只看到 LoopX 拼装的内容和项目仓库自己的 CLAUDE.md；需要所有项目都遵守的
   全局规则写进角色 prompt。
