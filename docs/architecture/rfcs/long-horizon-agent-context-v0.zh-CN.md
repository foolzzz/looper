# RFC：长程 Agent 的上下文体系（v0）

- **RFC 状态：** Draft，维护者评审中。
- **交付成熟度：** Partial。第 4 节的分层已在四类 agent 上落地，项目背景和 system prompt 记录已交付（M0，looper #13）；M1 起为提案。
- **创建日期：** 2026-10-13
- **实现基线：** `c600594ea`（foolzzz/looper `main`，含 M0）
- **相关文档：** [Agent 系统架构](../agent-system.md)、[产品设计](../../design.md)
- **开发计划与进度：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md)（分阶段范围、验收和进度台账以该文为准）
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

- **P1 稳定进 system，变化进 user。** 整个会话不变的内容（角色、规则、契约、项目背景、本会话的任务描述）
  放 system prompt；每轮变化的内容（进度状态、事件、其他 agent 的产出）放 user 消息。任务描述在会话期间
  固定（Planner 与任务同生命周期，Worker 与步骤同生命周期），而且长程运行中早期 user 消息可能被自动压缩
  摘要掉，system prompt 则始终完整保留，所以任务描述放 system prompt。动态内容不得进 system prompt：常驻进程的 system prompt
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
  L1  harness prompt                         —— agent 的性格与稳定性（工作原则、边界、沟通方式），按继承链解析（M1.5）
  L2  role prompt                            —— 该类 agent 的职责说明；agent_configs.<role>.system_prompt，可编辑，跟踪推荐版本
  L3  项目档案                              —— projects.background（M0）；知识索引、skill 清单（M3/M4）
  L4  任务描述                              —— 本会话要完成的任务：Planner 的任务需求，Worker、Reviewer 的步骤定义（M1）
  L5  代码契约                              —— REPLY_CONTRACT / WORK_CONTRACT / REVIEW_CONTRACT / TASK_PLACEMENT_RULE

user 消息（每轮）
  G   接地状态（代码生成）                  —— 计划与步骤进度、项目列表、目标进度
  E   事件与数据                            —— Worker/Reviewer 的结果与报告、依赖步骤的产出、用户聊天、运行中的纠偏、监督观察
                                               （不可信内容只在这里）
```

拼装由一个共享模块完成（M0 中引入），四类 agent 共用同一套拼装与记录逻辑。L3 为空时省略整段。

**harness 与 role 的分工：** harness 塑造 agent 的性格与稳定性，所有 agent 默认共用同一份；role 说明这类
agent 负责什么。两者分开，用户怎么覆盖 harness 都不会让 agent 丢掉自己的职责。

**harness 的继承链（M1.5）：**

- **系统默认**：保存在 Settings。仓库内置一份公开通用的默认值，可以一键恢复；操作者可以改写为自己的版本。
  操作者个人的规则（例如由本机 CLAUDE.md、AGENTS.md 整理而来）只存放在自己的数据库里，不进入公开仓库。
- **Manager**：可以在 Agents 页单独覆盖；为空则继承系统默认。
- **Planner、Worker**：只能在项目里覆盖；为空则继承系统默认。Agents 页不再编辑 Planner、Worker 的 harness。
- **Reviewer**：与同一项目的 Worker 使用同一份 harness。

**排列顺序的理由：** L1 到 L4 由稳定到多变排列，同一项目的各个步骤共享前缀，便于提示词缓存；
L5 代码契约始终最后，因为 L4 来自用户或 Planner，放在契约之后就可能覆盖输出格式。

**任务描述的上限：** 任务需求过长时截断到上限（建议 8,000 字）并标明已截断，避免挤占其他层。

**生效时机：** L1 至 L3 修改后立即生效。一次性进程（Worker、Reviewer）在下次启动时读取最新值；常驻进程
（Manager、Planner）在下一轮比较拼装结果，变化则重启并 `--resume`，已在运行的 Worker 不受影响。

## 5. 各 Agent 的目标形态

| Agent | L1 harness 来源 | L2 role | L3 项目档案 | L4 任务描述 | L5 契约 | G 接地状态 | E 事件与数据 |
|---|---|---|---|---|---|---|---|
| Manager | Manager 覆盖 → 系统默认 | 管家职责 | 不注入（跨项目） | 无（不对应单一任务） | `TASK_PLACEMENT_RULE` | 项目列表、目标快照 | 用户消息 |
| Planner | 项目的 Planner 覆盖 → 系统默认 | 规划与决策职责 | 所属项目 | 任务需求、任务 id、项目名、工作目录 | `REPLY_CONTRACT` | 计划、步骤状态、重试计数 | Worker/Reviewer 结果、用户聊天、运行中 Worker 的近况 |
| Worker | 项目的 Worker 覆盖 → 系统默认 | 执行职责 | 所属项目 | 整体任务需求、本步指令、验收标准、校验命令、重做反馈 | `WORK_CONTRACT`（报告 JSON） | — | 依赖步骤的产出、运行中纠偏 |
| Reviewer | 同 Worker | 验收职责 | 所属项目 | 被验收步骤的指令与验收标准、整体任务需求 | `REVIEW_CONTRACT`（`passed` 结论） | — | Worker 的报告与改动文件清单（待核实的说法） |

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
2. **agent 加载操作者个人的设置和记忆文件（已核实，M1 已隔离）。** 用 Claude Code 2.1.285 核实（`/context`、
   stream-json 的 init 与 hook 事件、真实模型的探针问答）：M1 之前四类 agent 都会加载 `~/.claude/CLAUDE.md`
   及其 `@import`、`CLAUDE.local.md`、用户与本地设置（模型、effort、env、hook）、个人 skill 和 claude.ai 同步的
   skill、已装插件（连同其 skill、agent、SessionStart hook）、自定义 agent、输出风格、自动记忆，Manager 还会
   加载全部 MCP。现在四类 agent 共用一组隔离参数：
   - `--setting-sources project` 去掉用户级和本地设置，以及随之而来的用户、本地记忆、hook、skill、插件、
     自定义 agent、输出风格和用户 MCP；项目自己的 `.claude/settings.json` 和 `CLAUDE.md` 照常加载，登录不受影响；
   - `--strict-mcp-config` 去掉其余 MCP（项目 `.mcp.json`、claude.ai 连接器）；
   - 只靠前两项不够：Claude Code 会从工作目录一直往上到 `/`，把每级目录的 `CLAUDE.md`、`.claude/CLAUDE.md`、
     `.claude/rules/` 当作项目记忆读入，工作目录在 `$HOME` 下时 `~/.claude/CLAUDE.md` 仍会以"项目记忆"的身份
     进来。所以再用 `--settings` 传 `claudeMdExcludes`，排除工作目录所在仓库以上的这些文件和 `~/.claude` 下的
     用户记忆，并用 `autoMemoryEnabled: false` 关掉 `~/.claude/projects/` 下的自动记忆；
   - `--bare` 和另设 `CLAUDE_CONFIG_DIR` 会丢掉操作者的登录，`--safe-mode` 会连项目 `CLAUDE.md` 一起去掉，
     `--disable-slash-commands` 会连内置 skill 一起去掉，都不采用。

   另外发现：Claude Code 2.1.285 默认开启 system prompt 快照，`--resume` 续接时沿用会话第一次请求的 system
   prompt，忽略新传入的内容；M1 起四类 agent 都带 `--system-prompt-snapshot off`，常驻进程改了 prompt 重启后
   确实用上新 prompt。仍未隔离的：管理员下发的托管设置、Claude Code 内置插件、运行 `loopx serve` 的进程环境变量。
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
   全局规则写进系统默认 harness prompt（见决策 6）。
5. **任务描述进 system prompt。** 分层改为四层：角色 prompt → 项目档案 → 任务描述 → 代码契约（第 4、5 节），
   并入 M1 交付。
6. **harness 与 role 分层（M1.5，M1 之后、M2 之前交付）。** 原"角色 prompt"拆成两层：harness prompt（性格与
   稳定性）和 role prompt（职责说明），最终为五层：harness → role → 项目档案 → 任务描述 → 代码契约。harness
   的继承链见第 4 节：系统默认在 Settings，Manager 可在 Agents 页覆盖，Planner、Worker 只能在项目里覆盖，
   Reviewer 跟随 Worker。决策 4 里"需要所有项目都遵守的全局规则"由此写进系统默认 harness。
7. **导入本机 Claude 全局配置作为系统默认（修订决策 4 的范围）。** 决策 4 的"隔离"指不隐式加载操作者的个人
   配置；操作者希望 agent 具备的能力改为**显式导入**，成为系统管理的默认配置，可见、可复现、可刷新：
   - 模型 `opus`、推理强度 `xhigh`、非敏感的默认环境变量：存入 Settings（M1.5）；
   - `~/.claude/CLAUDE.md` 与 AGENTS.md：整理为系统默认 harness（M1.5）；
   - 已启用的插件和全部个人 skill：导入为系统管理的快照，以 `--plugin-dir` 挂载给四类 agent，每次运行记录加载了哪些
     插件和 skill，Settings 提供"从本机重新导入"（M1.6）；
   - 不导入：个人 hook（例如提问日志）、MCP 工具权限，以及状态栏、主题、语音等只对交互界面有用的设置。
8. **agent 不限权限；渠道必须有发送者白名单（M1.7）。** 四类 agent 一律跳过权限确认、开放全部工具，Planner 也不例外
   （此前 Planner 只开放只读工具）。职责边界改由 role prompt 与代码契约约束：Planner 的 role prompt 写明"负责规划和
   决策，具体工作交给 Worker"。Manager 拿到全部权限后，任何能给机器人发消息的人都能让它在本机执行命令，所以飞书、
   Telegram 渠道必须配置发送者白名单，不在名单里的消息不交给 Manager，未配置白名单的渠道不启动；权限放开与白名单
   在同一次交付中上线。
9. **能力目录与安装（M8）。** 维护者对插件、skill 以及后续 MCP 和配置的管理方式作出决定：
   - **配置的最小单位是"项目里的一个角色"**，Manager 记为"全局的 Manager"。每项配置按**系统默认 → 角色 →
     项目 → 项目里的角色**逐层解析，后面的覆盖前面的；不单独配置某个 Worker 或 Reviewer 实例。
   - **进库与安装分开。** 进库把条目放进系统的**目录**，来源有从本机导入（已有）、上传、git、在界面里编写；
     进了目录的条目本身不起作用。安装把目录里的条目放到一个**范围**上：全局或某个项目，可限定**角色**
     （manager、planner、worker、reviewer；项目范围不含 manager）。每次运行加载全局安装和所在项目的安装，
     再按自己的角色过滤；它们在 runtime root 下物化为只读副本后挂载（现在是 `--plugin-dir`，M8b 起加
     `--mcp-config`），不写 `~/.claude`，也不写项目仓库。
   - **已导入的 3 个插件和 104 个 skill 留在目录里、不安装。** 升级后它们在任何地方都不再加载，直到被安装。
     这是有意的默认行为变化，在 PR、CHANGELOG 和界面上各说明一次。
   - **项目 skill（M4）并入同一个目录。** 在项目里写的 skill 是一个目录条目，自动安装到这个项目，也可以安装
     到其他项目。现有的 `project_skills` 等价迁移：同样的项目、同样的三个任务角色、同样的启用状态、同样的
     调用名 `project:<name>`（项目写的 skill 仍然放在名为 `project` 的插件里）。
   - **后续阶段**（现在记下，M8a 不做）：MCP 服务器作为目录条目，安装方式相同，密钥（env 的值、header）
     存数据库、接口里打码，可以从本机 `~/.claude.json` 导入（M8b）；更多进库来源：上传 `SKILL.md` 或 zip、
     从 git 导入、在界面新建和编辑（M8c）；harness、模型、effort、环境变量和单次上限按第一条的层级统一解析，
     原"按角色分别设模型"的待办并入（M8d）；导出与导入用同一个文件，应用前先预览，密钥默认不导出、留作
     占位符（M8e）。
10. **agent 多样性：运行时与 LLM provider（M9）。** 维护者决定把 agent 从固定的"Claude Code CLI + 订阅 OAuth"
   抽象为**运行时 + LLM provider**：
   - **运行时**是跑 agent 循环和工具的一层，分 CLI（Claude Code、Codex、OMP 等）和 SDK（Claude Agent SDK、
     OpenAI Agents SDK、自研的 norma SDK 等）。运行时的**类型**由代码里的适配器提供；**实例**由用户注册
     （二进制或 SDK、版本、默认参数），注册时探测版本和能力。
   - **LLM provider** 是凭据、接口地址、协议和模型：OAuth（Claude Code、Codex 各自的登录）或 API key
     （Anthropic、OpenAI、GLM、Kimi 等）。协议按接口区分为 `anthropic-messages`、`openai-responses`、
     `openai-chat`，一个 provider 支持其中一种或几种（norma 两类都支持）。API key 存数据库、接口打码（同 M8b）。
   - **agent = 运行时 + provider + 模型**（加 effort 等运行时选项）。注册时校验：运行时要求的协议与 provider
     支持的协议有交集；OAuth provider 只能配给发放它的运行时（Claude 订阅只给 Claude Code 及基于它的
     Claude Agent SDK，Codex 登录只给 Codex），不把 token 取出来给别的运行时。
   - **选用哪个 agent 是一项分层配置**，按决策 9 的层级（系统默认 → 角色 → 项目 → 项目里的角色）解析，
     模型随 agent 走；**任务可以覆盖各角色用哪个 agent**，其他配置不设任务层。
   - **所有运行时都要支持所有角色。** Manager、Planner 需要常驻会话和续接，非 Claude 运行时也要实现
     （例如 Codex 的 app-server 协议或逐轮续接，实现前先核实）。
   - 运行时之间的差异由适配器吸收：system prompt 的注入方式、隔离方式（如 Codex 用独立的 `CODEX_HOME`）、
     把事件流统一成内部事件、MCP 配置格式的翻译、目录条目按运行时标注兼容性（Claude 插件只挂给 Claude 系
     运行时），以及花费（运行时不报告花费或接第三方接口时，按 provider 登记的模型价格计算）。
   - 现有的 `llm_profiles` 表和 `agent_configs.runtime`、`llm_profile_id` 两列从未接入启动路径，由新模型取代。
   - 每个新 agent 先用 M6 回归评测验收，评测可以指定 agent。
   - 详细设计和待决问题见 [多运行时 agent 架构 RFC](multi-runtime-agents-v0.zh-CN.md)。
