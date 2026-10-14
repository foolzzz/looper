# 开发计划：长程 Agent 上下文体系（M0–M6）

- **状态：** 暂停中（M0、M1、M1.5、M1.6、M1.7、M2、M3 已交付；2026-10-13 按维护者要求在 M3 之后暂停，恢复后从 M4 开始）
- **依据：** [RFC：长程 Agent 的上下文体系 v0](../architecture/rfcs/long-horizon-agent-context-v0.zh-CN.md)。
  设计原则、分层模型和已作出的决定以 RFC 为准；本文只管"怎么分阶段做、做到什么算完成、现在做到哪了"。
- **仓库：** `foolzzz/looper`，基线 `main`
- **最近更新：** 2026-10-13

## 1. 工作方式

1. **一个阶段一个 PR。** 每个阶段都是可以独立评审、独立回滚的交付；阶段之间按依赖顺序推进，前一个合入后再开下一个。
2. **测试先行。** 新测试必须先在当前 `main` 上失败，再实现。
3. **真实路径验收。** 除单测外，每个阶段都要：
   - 用一次性数据库跑一遍 `loopx serve`；
   - 涉及模型可见内容（prompt、契约、注入）的改动，用真实 Claude 至少跑一次；
   - 涉及界面的改动，用无头 Chrome 截图自查。
4. **合入后部署。** 合入后同步本机 `main`，前端有改动就重新构建 `web/dist`，然后只按端口重启本机服务（8767）。
5. **更新本文。** 每个阶段交付后，在第 4 节进度台账记一行，并更新第 2 节的状态列。
6. **核心优先。** 每个阶段只做核心：后端、agent 行为、能用的最小界面（能配置、能看到结果）。可视化、交互细节、
   界面美化和"顺手加上"的辅助功能记入第 5 节的"非核心待办"，M6 之后统一做（2026-10-13 起执行，M3 开始适用）。

## 2. 阶段总览

| 阶段 | 主题 | 状态 | 依赖 |
|---|---|---|---|
| M0 | 项目背景；四类 agent 的 prompt 分层；记录并展示 system prompt | ✅ 已交付（looper #13） | — |
| M1 | 上下文隔离与可复现 | ✅ 已交付（looper #17） | M0 |
| M1.5 | harness 与 role 分层；harness 继承链 | ✅ 已交付（looper #19） | M1 |
| M1.6 | 导入本机插件与 skill 作为全局默认 | ✅ 已交付（looper #22） | M1.5 |
| M1.7 | agent 不限权限；渠道发送者白名单 | ✅ 已交付（looper #23） | M1.6 |
| M2 | 上下文与花费观测 | ✅ 已交付（looper #24） | M0 |
| M3 | 项目知识 | ✅ 已交付（looper #27） | M0 |
| M4 | 项目 skill | ⏸ 暂停中（恢复后开始） | M3 |
| M5 | 项目执行配置 | 待开始 | M2 |
| M6 | prompt 回归评测 | 待开始 | M1 |

执行顺序：M1 → M1.5 → M1.6 → M1.7 → M2 → M3 → M4 → M5 → M6。

## 3. 各阶段

### M1 上下文隔离与可复现

- **目标：** 四类 agent 只看到 LoopX 拼装的内容和项目仓库自己的 `CLAUDE.md`，不受运行它的机器上操作者个人配置的影响；每次运行都能从记录还原它的上下文。
- **范围：**
  - 先用真实 Claude 核实 Claude Code 会从用户级加载哪些东西：`~/.claude/CLAUDE.md`、用户设置、hook、个人 skill、插件、自定义 agent、输出风格等；再确定关闭它们而不影响登录和项目 `CLAUDE.md` 的参数组合；
  - 隔离参数集中在一处，Manager、Planner、Worker、Reviewer 共用；
  - system prompt 改用 `--append-system-prompt-file` 传入，文件按内容哈希存放在 runtime root 下，只写一次；
  - 每次运行记录 `system_prompt_sha256` 和各层来源（角色 prompt 是否为推荐版、项目背景版本、契约名称、隔离参数版本）；
  - 历史页展示哈希、各层摘要和"已隔离"标记；
  - **任务描述层**（RFC 决策 5）：system prompt 改为四层，即角色 prompt → 项目档案 → 任务描述 → 代码契约。
    - Planner 的任务需求、Worker 与 Reviewer 的步骤定义移入 system prompt；
    - stdin 和每轮消息只保留动态内容：依赖步骤的产出、Worker 报告、进度、纠偏；
    - 记录里增加 `task` 层。
  - **核实后补充的范围**：
    - `--setting-sources project` 之外还要用 `--settings` 传 `claudeMdExcludes`：Claude Code 会从工作目录一直往上到 `/` 读每级目录的 `CLAUDE.md`、`.claude/CLAUDE.md`、`.claude/rules/`，工作目录在 `$HOME` 下时 `~/.claude/CLAUDE.md` 仍会以项目记忆进来；排除工作目录所在仓库以上的这些文件；
    - 用 `autoMemoryEnabled: false` 关掉 `~/.claude/projects/` 下的自动记忆；
    - 四类 agent 都带 `--system-prompt-snapshot off`：Claude Code 2.1.285 默认在 `--resume` 时沿用会话第一次的 system prompt，常驻进程改了 prompt 重启后其实还在用旧的；
    - Manager 也不再加载 MCP；Worker 的 stdin 带上所依赖步骤的报告（`WORK_ITEM.dependencies`）。
- **不做：** 不改 Claude Code 内置的 system prompt 和内置工具；不修改操作者 `~/.claude` 下的任何文件。
- **验收：**
  - 真实 Claude 下，Manager、Planner、Worker 都能看到项目 `CLAUDE.md` 里的标记，看不到操作者个人 `CLAUDE.md` 里的内容；
  - 同样的输入两次运行得到同一个 prompt 哈希，改了项目背景后哈希变化；
  - 启动命令里不再有几千字的 prompt；
  - 真实 Claude 下，Planner 仍能产出有效计划，Worker 的报告判为 completed，Reviewer 给出 `passed`；
    记录里的 system prompt 能看到四层，Worker 和 Reviewer 的 stdin 里不再有步骤指令和验收标准。

### M1.5 harness 与 role 分层

- **目标：** 把 agent 的"性格与稳定性"（harness）和"职责"（role）分开管理。harness 有统一的系统默认，并可按 agent、按项目覆盖；最终 system prompt 为五层：harness → role → 项目档案 → 任务描述 → 代码契约（RFC 决策 6）。
- **范围：**
  - **系统默认 harness**：
    - 存在 Settings，在 Settings 页编辑；
    - 仓库内置一份公开通用的默认值，可以一键恢复；
    - 操作者自己的版本只存数据库。
  - **Manager 的 harness 覆盖**：在 Agents 页编辑，为空则继承系统默认。
  - **项目级覆盖**：项目上新增 Planner harness 和 Worker harness 两个字段，在项目信息里编辑，为空则继承系统默认；Reviewer 使用 Worker 的 harness。
  - **role prompt**：
    - 现有的 `agent_configs.<role>.system_prompt` 只承担职责说明，推荐版本照旧跟踪；
    - 新增 `reviewer` 行：Reviewer 有自己的 role prompt，并读自己这一行的 `model`、`timeout_seconds`、`max_turns`（以前沿用 `worker` 行）；
    - 现有推荐 prompt 里属于"性格与稳定性"的内容移入系统默认 harness。
  - **记录与展示**：每次运行的 `prompt_layers` 增加 `harness` 层，写明来源（系统默认、Manager 覆盖、项目覆盖）和哈希；历史页展示来源。
  - **默认模型、推理强度与环境变量**：存在 Settings；模型初始为 `opus`，Agents 页可以按 agent 覆盖，四类 agent 都生效；环境变量只允许非敏感的键值，记录里只保存键名和哈希。
  - **本机初始化**（合入后由集成方执行，不提交到仓库）：把本机 CLAUDE.md 与 AGENTS.md 整理出的默认 harness，以及 `~/.claude/settings.json` 里的模型 `opus`、推理强度 `xhigh`、环境变量写入本机 Settings。
- **不做：** 不在 Agents 页编辑 Planner、Worker 的 harness；不改代码契约的内容。
- **验收：**
  - 四类 agent 的 system prompt 按五层顺序拼装，各层来源可在记录中读回；
  - 修改系统默认后，没有覆盖的 agent 下一次运行就生效；
  - 给某个项目设置 Worker harness 后，该项目的 Worker 和 Reviewer 使用它，其他项目不受影响；
  - Manager 的覆盖只影响 Manager；
  - 真实 Claude 下，Planner、Worker、Reviewer 的产出仍通过判定。

### M1.6 导入本机插件与 skill 作为全局默认

- **目标：** 让所有 agent 具备操作者平时在 Claude Code 里使用的插件和 skill，同时保持可见、可复现（RFC 决策 7）。
- **范围：**
  - 从本机 Claude 配置导入已启用的插件和全部个人 skill，存为系统管理的快照（内容进数据库或 runtime root 下的受管目录，不引用 `~/.claude` 原路径）；
  - 物化为本地插件目录，以 `--plugin-dir` 挂载给 Manager、Planner、Worker、Reviewer；
  - Settings 显示已导入的插件和 skill 列表，提供"从本机重新导入"和单项停用；
  - 每次运行记录实际加载的插件和 skill（名称与版本哈希），历史页可见。
  - **核实后确定的做法**：
    - 数据库是事实来源（`library_items`、`library_files`），条目的版本是其文件的哈希；每次运行按启用的条目在 runtime root 的 `.local/library/<sha256>/` 下取只读副本，第一次用到时从数据库写出；
    - 插件整个导入（连同 skill、agent、命令和 hook），每个启用的插件一个 `--plugin-dir`；个人 skill 链接进一个生成的 `personal` 插件，在 Claude Code 里叫 `personal:<skill>`；
    - 重新导入只替换内容变了的条目，保留启用开关，删除本机已没有的条目；
    - 上限：单个文件 2 MiB（二进制文件和指向条目外的链接也不导入），单个条目 32 MiB 或 5000 个文件，整个库 128 MiB，超限的都在导入结果里列出，整库超限时拒绝导入；
    - 为了真正用上 skill，加载了插件库的运行里：Planner 的工具加上 `Skill`（没有它 Claude Code 根本不列出 skill）；Manager 加上 `--allowedTools Skill`（`--print` 默认会拒绝声明了 `allowed-tools` 的 skill；放行 skill 不会连带放行它声明的工具）。没有导入时，四类 agent 的启动命令与以前相同；
    - 库的哈希进入常驻进程的重启签名。
- **不做：** 不导入个人 hook、MCP 工具权限和界面类设置；不恢复对 `~/.claude` 的隐式加载。
- **验收：**
  - 真实 Claude 的 init 事件里出现导入的插件和 skill，且 agent 能实际调用其中一个 skill；
  - 操作者的提问日志 hook 不会被触发，个人 `CLAUDE.md` 仍然不可见；
  - 停用某个 skill 后，下一次运行不再加载它；
  - 记录里能读回加载清单。
- **风险：** 已核实 `--plugin-dir` 在 `--print` 模式下与 M1 隔离参数配合正常（见进度台账）。代价是上下文：3 个插件加 104 个个人 skill 时，Opus 上一次最小运行的首个请求从 15,674 token 增至 29,114 token，插件自带的 `SessionStart` hook 也会运行；花费的完整观测留给 M2。

### M1.7 agent 不限权限；渠道发送者白名单

- **目标：** 四类 agent 不再因为权限确认或工具限制而做不了事；同时保证只有操作者本人能通过渠道驱动它们（RFC 决策 8）。
- **范围：**
  - **权限**：Manager、Planner、Worker、Reviewer 统一跳过权限确认（`--dangerously-skip-permissions`），去掉 Planner 的只读工具限制和 Manager 单独放行 Skill 的写法。启动参数集中在一处，和隔离参数放在一起。
  - **Planner 的职责边界**：推荐的 Planner role prompt 写明"负责规划和决策，具体工作交给 Worker"，不亲自修改文件。
  - **渠道白名单**：飞书、Telegram 的渠道配置各加一个发送者白名单（飞书用户 ID、Telegram 用户 ID），在 Settings 里编辑。
    - 不在名单里的消息不交给 Manager，只记一条日志，不回复内容；
    - 没有配置白名单的渠道不启动，Settings 页说明原因。
  - **网页入口**：仍然只接受本机访问（M0 前已有的跨站和 Host 校验），不受白名单影响。
  - **核实后确定的做法**：
    - 权限参数只在 `claude_cli.permission_args` 定义（`--dangerously-skip-permissions`），由 `agent_settings.Launch.args` 和插件库参数一起下发，四类 agent 的启动命令里不再有 `--tools`、`--allowedTools`、`--permission-mode`；M1 的隔离参数和 M1.6 的 `--plugin-dir` 不变；
    - 推荐的 Planner role prompt 写明"你负责规划和决策，具体工作交给 Worker；不亲自修改文件，哪怕是很小的修改，每个改动都作为步骤交给 Worker"，并说明它可以读项目、运行无害的命令；PLAN turn 的提示从"可以先读项目"改为"可以先查看项目，但不改动"；`REPLY_CONTRACT` 本来就没有说 Planner 只读，不用改；
    - 飞书白名单用适配器原本就读取的 `open_id`（`event.sender.sender_id.open_id`，同一用户在同一应用下不变），存在 `allowed_open_ids`；Telegram 用发送者的数字用户 ID，沿用 `allowed_user_ids`；两者都按 JSON 数组保存，以前 Telegram 存的逗号分隔字符串仍然能读，`LOOPX_TELEGRAM_ALLOWED_USERS` 不再生效；
    - 允许保存"已启用但白名单为空"的渠道（方便先启用再填），Settings 页写明它不会启动；服务启动时不启动它，适配器自己也拒绝在空名单下启动；
    - Agents 页原来的 Claude Code「Permission Mode」下拉框，这四类 agent 从来不读，改为一行说明它们共用的权限。
- **不做：** 不做多用户权限体系，也不做网页登录。
- **验收：**
  - 真实 Claude 下，Manager 能直接调用工具和 skill，Planner 能运行命令，都不再被拒绝；
  - Planner 仍按契约产出计划，并把工作派给 Worker；
  - 白名单之外的飞书、Telegram 消息不会到达 Manager；
  - 没有配置白名单的渠道不会启动。

### M2 上下文与花费观测

- **目标：** 长程运行的成本和上下文增长看得见。默认不设花费上限（RFC 决策 2）。
- **范围：**
  - 每轮记录输入 token（含缓存读、缓存写）、本轮结束时的上下文规模，以及自动压缩事件；
  - 按运行和按任务累计花费；
  - 任务页显示任务累计花费和各 agent 的花费分布；Manager、Planner 的历史页显示上下文用量曲线和压缩次数；
  - 以上数据通过 API 可读。
- **验收：** 跑一个多步骤任务后，任务页的累计花费等于各次运行花费之和；人为构造一次自动压缩（或用替身产生对应事件），历史页能看到；Manager 长会话的上下文用量随对话增长且可读回。
- **风险：** 需要先核实 stream-json 中自动压缩事件的确切形态，以实测为准。
- **核实后确定的做法**（Claude Code 2.1.285，haiku 实测，样例存于 `tests/fixtures/claude_stream/`）：
  - **token 与花费的来源**：
    - `result` 的 `total_cost_usd` 和 `modelUsage` 是会话在本进程里的累计值，包含压缩时的摘要请求和子 agent 的请求；
    - `result.usage` 只算本轮主循环的请求。压缩那一轮它报全零，实际花了 $0.0088；子 agent 的请求它也不算；
    - assistant 消息上的 `output_tokens` 是流式过程中的快照，不是最终值；
    - 所以每次运行的 token 和花费都取累计值的增量（`loopx/agents/run_usage.py`），没有 `modelUsage` 时才退回 `result.usage`。
  - **上下文规模**：取本轮最后一个主循环请求（`parent_tool_use_id` 为空）的 input + cache read + cache creation；子 agent 的请求不算。
  - **压缩事件**：
    - 形态是 `{"type":"system","subtype":"compact_boundary","compact_metadata":{"trigger":"auto"|"manual","pre_tokens","post_tokens","cumulative_dropped_tokens","duration_ms",...}}`，前面有 `system/status` 的 `compacting` 和 `compact_result: "success"`；
    - 压缩失败只有 `compact_result: "failed"`（例如 `too_few_groups`），不算一次压缩；
    - `/compact` 作为 stream-json 的 user turn 发送时会生效；
    - `--autocompact`（或环境变量 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`）的下限是 100k，配合 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 可以低成本触发自动压缩。
  - **`--resume` 重复计费**：
    - 已核实，续接进程的第一个 `result` 把会话以前的花费一并计入：一个已花 $0.01046 的会话，续接后第一轮报 $0.01279，本轮只花了 $0.00232；`modelUsage` 也一样累计；
    - 做法：会话的累计值（花费和四类 token）和会话 id 存在一起，Manager 存在 `session.json` 的 `session_totals`，Planner 存在计划文件；
    - 续接时以存下的值为起点，每个 `result` 后重写；
    - 会话无法续接而被丢弃时一起清零；某项累计值变小时，视为从零重新计数。
  - **任务花费**：只算 Planner、Worker、Reviewer 的运行，不算 Manager；另外分开计数还在跑的运行和没报出花费的运行。

### M3 项目知识

- **目标：** 项目可以挂载大块资料，agent 按需读取，而不是全部塞进 prompt。
- **范围：**
  - `project_documents` 表：标题、一句话摘要、来源类型、内容或路径、大小、更新时间；
  - 来源支持手动录入、上传文件、引用项目目录内的路径（RFC 决策 3；实时读取，不支持 URL）；
  - 录入和上传的文档物化到 runtime root 下的项目知识目录，以 `--add-dir` 挂载；引用路径不复制，直接读取；
  - L2 只注入索引（标题 + 摘要 + 读取路径），有总长度上限；
  - 最小界面：在现有的项目信息编辑里管理知识（列表、新增、上传、引用路径、删除）。完整的项目页 `#projects/<id>`（概览、知识、Skills、Settings 标签）记入非核心待办。
- **验收：**
  - 真实 Claude 下，Worker 能按索引读到文档，并在报告里引用；
  - 引用的仓库路径内容改动后，agent 读到的是新内容；
  - 超出大小或索引上限时拒绝保存，并说明原因。
- **核实后确定的做法：**
  - `project_documents` 表：标题、必填的一句话摘要、来源类型 `text`/`upload`/`path`、`filename` 和 `content`（录入、上传）或 `path`（相对项目默认目录）、大小、`sha256`、更新时间；CHECK 约束保证每种来源的列一致，删项目时连带删除；
  - 上限：单篇 2 MiB，每个项目 100 篇、存储内容 50 MiB（引用路径不计入），索引 4,000 个字符，超限返回 400 并写明上限；增删改在持有项目行锁的事务里做；
  - 上传走 JSON 里的 base64；不是 UTF-8 或含 NUL 字节的按二进制拒绝；引用路径必须是默认目录里的普通文本文件，跟随链接后仍在目录内；
  - 索引在 L3 背景之后（`## Project knowledge`，每篇一行 `- <标题>: <摘要> (read: <绝对路径>)`，已不存在的引用文件标为 `missing`）；`--add-dir` 由 `claude_cli.knowledge_args` 生成、随 `Launch` 下发：录入和上传的文档挂载知识目录，有引用路径且任务目录不是项目目录时再挂载项目目录；Manager 都没有；
  - Planner 的重启签名加上挂载的目录（索引本身已在 prompt 哈希里）；`prompt_layers.project` 记 `knowledge_sha256`、`knowledge_documents`，读不到时记 `knowledge_error`；
  - 已核实（Claude Code 2.1.285，隔离参数下）：agent 能读 `--add-dir` 目录里的文件，该目录及其上级的 `CLAUDE.md` 不会被加载，不存在的目录不会让运行失败；
  - 索引每行带绝对路径（在 `~/.loopx` 下约 150 个字符），索引上限实际在 15 到 25 篇左右先到，100 篇的上限很难达到。

### M4 项目 skill

- **目标：** 项目可以带自己的 Claude Code skill，只对这个项目的 agent 生效，不写进用户仓库。
- **范围：**
  - `project_skills` 表：名称、描述、`SKILL.md` 内容；
  - 物化为本地插件目录，以 `--plugin-dir` 挂载；配合 M1 的隔离，只出现项目自己的 skill；
  - 最小界面：在项目信息编辑里管理项目 skill（列表、新增、删除、启用开关）。
- **验收：**
  - 真实 Claude 的 init 事件里只出现该项目的 skill，并且在任务中被实际调用；
  - 其他项目的 agent 看不到它；
  - 项目仓库目录下没有新增文件。
- **风险：** 需要先核实 `--plugin-dir` 在 `--print` 模式下加载 skill 的行为，以及它和 M1 隔离参数是否冲突。

### M5 项目执行配置

- **目标：** 每个项目可以按需调整 agent 的运行方式。
- **范围：**
  - 项目上的类型化字段：
    - 模型；
    - 可选的花费上限：单次运行（`--max-budget-usd`）和任务累计；
    - Planner 的监督间隔；
    - 验收策略：由 Planner 决定，或全部步骤都送 Reviewer；
  - 超出任务累计上限时的处理方式随配置一起定：可选"先让 Planner 收尾再判失败"或"直接失败"；
  - 最小界面：在项目信息编辑里设置这些字段。
- **验收：**
  - 每项配置都能在对应 agent 的启动参数或行为里读回；
  - 设了上限的项目在上限处停下，并写明原因；
  - 没有配置的项目，行为与现在一致。

### M6 prompt 回归评测

- **目标：** 改推荐 prompt、契约或注入方式之前，能知道会不会退化。
- **范围：**
  - 一组固定的小任务（覆盖单步、多步依赖、送审、重试、聊天纠偏）；
  - 两种跑法：
    - 替身 Claude，验证流程，可以每次都跑；
    - 真实 Claude，验证效果，按需跑，有花费上限；
  - 结果存档，并和上一次结果对比：成功率、步骤数、重试次数、花费、契约违约次数；
  - 在贡献指南里写明：改动推荐 prompt 或契约的 PR 必须附带评测结果。
- **验收：** 故意改坏一个契约，评测能显出差异；评测结果可以写进 PR。

## 4. 进度台账

| 日期 | 事项 |
|---|---|
| 2026-10-13 | RFC v0 合入（looper #12），4 个待决问题已决定 |
| 2026-10-13 | M0 交付（looper #13，`c600594ea`）：项目背景；Worker 和 Reviewer 的 prompt 分层，契约移出 stdin；四类 agent 记录 `metadata.system_prompt`。真实 Claude 下 Worker 和 Reviewer 的判定都通过 |
| 2026-10-13 | M1 开工 |
| 2026-10-13 | RFC 决策 5：任务描述进 system prompt，分层改为四层，并入 M1 |
| 2026-10-13 | RFC 决策 6：harness 与 role 分层，新增 M1.5（M1 之后、M2 之前） |
| 2026-10-13 | M1 交付（looper #17）：四类 agent 共用一组隔离参数（`--setting-sources project`、`--strict-mcp-config`、`--settings` 的 `claudeMdExcludes` 与 `autoMemoryEnabled: false`），不再加载操作者的 `~/.claude/CLAUDE.md`、个人设置、hook、skill、插件、自定义 agent、输出风格、MCP 和自动记忆，项目 `CLAUDE.md` 与登录不受影响；system prompt 按哈希写成文件以 `--append-system-prompt-file` 传入，并加 `--system-prompt-snapshot off`；每次运行记录 `system_prompt_sha256` 和 `prompt_layers`，历史页展示；system prompt 改为角色 → 项目 → 任务 → 契约四层。验收：真实 Claude 下 Manager、Planner、Worker、Reviewer 都看到项目 `CLAUDE.md` 和背景里的标记，个人 `CLAUDE.md` 及其导入文件里的内容都答不出；Planner 产出有效计划，Worker 判为 completed，Reviewer 给出 `passed`；Manager 两轮哈希相同，改背景后 Planner 续接重启、哈希变化并答出新背景；单测与真实 PG 619 通过 |
| 2026-10-13 | RFC 决策 7：导入本机 Claude 全局配置作为系统默认；M1.5 加入默认模型、推理强度、环境变量；新增 M1.6（插件与 skill） |
| 2026-10-13 | M1.5 交付（looper #19）：system prompt 改为 harness → role → 项目 → 任务 → 契约五层，Manager 为 harness → role → `TASK_PLACEMENT_RULE` 三层；harness 由 `loopx/agents/agent_settings.py` 统一解析：Settings 默认（`system_settings` 表，未设置时用内置的 `DEFAULT_HARNESS_PROMPT`）、Manager 覆盖、项目的 Planner 和 Worker 覆盖，Reviewer 跟随 Worker；推荐 role prompt 只保留职责，未改过的行随推荐升级，新增 `reviewer` 行；Settings 新增默认模型（内置 `opus`）、effort 和至多 50 个非敏感环境变量，四类 agent 都传 `--model`，环境变量哈希进入常驻进程的重启签名；记录新增 `prompt_layers.harness`、`model_choice` 和 `env`（只存键名和值的哈希），历史页展示。验收：真实 Claude（opus，effort low）下，Settings 默认、项目 A 的 Worker 覆盖、Manager 覆盖各放一个暗号：A 的 Planner 答出默认暗号，A 的 Worker 写入、Reviewer 核对的都是 A 的 Worker 暗号，B 的 Worker 写出默认暗号并读到 Settings 里的环境变量，Manager 答出自己的暗号；Planner 产出有效计划，Worker 判为 completed，Reviewer 给出 `passed`，记录里五层齐全；四类 agent 的模型都是 opus，Manager 改为 sonnet 后下一轮以 `--resume` 重启并换成 sonnet；真实运行 18 次共 $0.68；单测与真实 PG 760 通过（main 上 637） |
| 2026-10-13 | 修复：Manager、Planner 保存的会话丢失后自动换新会话（looper #20） |
| 2026-10-13 | RFC 决策 8：agent 不限权限，渠道必须有发送者白名单；新增 M1.7 |
| 2026-10-13 | M1.6 交付（looper #22）：插件库。从本机 Claude Code 配置只读导入已启用的插件（整个导入，连同其 skill、agent、命令和 hook）和全部个人 skill，存进数据库（`library_items`、`library_files`），条目的版本是其文件的哈希；每次运行在 runtime root 下取只读副本，每个插件一个 `--plugin-dir`，个人 skill 合成一个 `personal` 插件；加载了插件库的运行里，Planner 多一个 `Skill` 工具，Manager 多一个 `--allowedTools Skill`；库的哈希进入常驻进程的重启签名；记录新增 `metadata.library`；Settings 新增「Plugins & skills」，历史页列出每次运行的加载清单。核实（Claude Code 2.1.285）：在 `--print` 和隔离参数下，`--plugin-dir` 加载插件的 skill（名为 `<插件>:<skill>`）、agent、命令和 hook，插件自带的 `SessionStart` hook 会运行，操作者的 `UserPromptSubmit` hook 不会；没有 `Skill` 工具时 Claude Code 不列出任何 skill；`--print` 默认权限下，声明了 `allowed-tools` 的 skill 会被拒绝，放行 `Skill` 不会连带放行它声明的工具。验收：真实 Claude（opus，effort low）下，导入 3 个插件、104 个个人 skill（约 21 MB、2,200 个文件；未导入 5 项：4 个图片文件、1 个没有 `SKILL.md` 的目录）；四类 agent 的 init 事件都列出 4 个库插件和 142 个 skill；Manager 调用一个个人 skill 并答出其中的样例值，Worker 和 Reviewer 调用同一个 skill、运行它自带的脚本（来自只读副本），结果与独立计算一致，Planner 产出有效计划，Worker 判为 completed，Reviewer 给出 `passed`；Manager 答出项目 `CLAUDE.md` 的暗号，答不出个人 `CLAUDE.md` 的内容；提问日志里没有任何 LoopX 会话的记录；停用这个 skill 后，Manager 下一轮以 `--resume` 重启，记录和 init 里都少了它（个人 skill 104 → 103）；上下文：Opus 上一次最小运行的首个请求从 15,674 token 增至 29,114 token；真实 Claude 花费约 $1.2；单测与真实 PG 929 通过（main 上 898） |
| 2026-10-13 | M1.7 交付（looper #23）：agent 不限权限，渠道必须有发送者白名单。权限参数只在 `claude_cli.permission_args`（`--dangerously-skip-permissions`）定义，由 `agent_settings.Launch.args` 和插件库参数一起下发给四类 agent；去掉 Planner 的 `--tools`/`--allowedTools Read,Grep,Glob[,Skill]`/`--permission-mode dontAsk` 和 Manager 的 `--allowedTools Skill`，隔离参数和 `--plugin-dir` 不变。推荐的 Planner role prompt 写明负责规划和决策、不亲自修改文件、改动都作为步骤交给 Worker，未改过的 `planner` 行启动时随推荐升级，改过的保留。飞书（`allowed_open_ids`，消息事件里的 `open_id`）和 Telegram（`allowed_user_ids`，数字用户 ID）各有发送者白名单：名单外的消息不交给 Manager，只记一条日志（渠道和发送者 ID，不含内容），不回复，Telegram 的 `/start`、`/status`、`/help` 也一样；名单为空或格式不对的渠道不启动，启动日志和 Settings 页（`start_problem`）写明原因；`LOOPX_TELEGRAM_ALLOWED_USERS` 不再生效。Settings 页可增删白名单并即时校验格式；Agents 页用一行说明代替不起作用的「Permission Mode」下拉框。核实（Claude Code 2.1.285）：`--print` 加隔离参数和 `--plugin-dir` 时，加上 `--dangerously-skip-permissions` 后 init 的 `permissionMode` 为 `bypassPermissions`，Bash、Write 和声明了 `allowed-tools: Bash` 的插件 skill 都不再被拒绝；不加时 Write 和这个 skill 被拒绝（`date` 这类只读命令两种情况都放行）；隔离用的 `--settings` 不影响权限。验收：真实 Claude（opus，effort low）下，Manager 用 Bash 运行 `date … | tee` 写文件并如实回报输出，没有被拒绝；Planner 规划时运行了 `python3 -c …`，产出有效计划并把改动派给 Worker，Planner 的工具调用里没有任何编辑，工作目录的改动全部来自 Worker；Worker 判为 completed，Reviewer 给出 `passed`。在真实服务上用本地替身（`PATH` 上的伪 `lark-cli`；只在验收环境里把 python-telegram-bot 指向本地伪 Bot API；不连真实飞书和 Telegram，不用真实 token）发合成消息：已启用但白名单为空的两个渠道都没有启动，替身一次也没被调用；在 Settings 页填好白名单并重启后，名单内的飞书和 Telegram 发送者都到达 Manager 并收到回复，名单外的 4 条消息（含 Telegram `/start`、`/help`）都被丢弃，各记一行日志，日志里没有消息内容，Manager 没有新增运行，也没有任何回复。无头 Chrome 里完成白名单的增删、校验、保存和刷新读回，并截图查看了 Settings（1280、768 px）、Planner 运行的启动命令和 Agents 页。真实 Claude 花费约 $0.52；单测与真实 PG 990 通过（main 上 933），另有 5 个 `test_dashboard_command` 安装模式用例在 main 上同样失败 |

| 2026-10-13 | M2 交付（looper #24）：上下文与花费观测，不设花费上限。核实结论见第 3 节 M2 的「核实后确定的做法」。记录：`llm_records` 新增 `cache_read_tokens`、`cache_creation_tokens`、`context_tokens`、`compactions`。每次运行的花费和 token 取会话累计值（`total_cost_usd`、`modelUsage`）的增量，上下文取最后一个主循环请求的 prompt，每次压缩记下触发方式和压缩前后的规模。修复 `--resume` 重复计费：会话累计值和会话 id 存在一起（Manager 的 `session.json`，Planner 的计划文件），续接从存下的值算起，会话被丢弃时清零。API：`GET /api/tasks/<id>` 和 `GET /api/tasks` 带 `cost`（总额、按角色、运行数、还在跑的和没报出花费的）；新增 `GET /api/agents/<id>/usage`（总额、按会话、按 UTC 日、最近各 turn 的上下文和压缩）。界面：任务列表显示花费；任务页显示「Cost」卡片和「Cost by agent」；Manager、Planner 的历史页显示「Context & cost」（上下文折线、压缩标记和清单、最新上下文规模）；运行卡片显示自身 token（含缓存）、`ctx`、压缩标记和自身花费。验收（真实 Claude，haiku）：2 步任务（第 2 步送审）完成，任务总额 $0.0985664 等于 7 次运行之和（Planner 4 次 $0.033885、Worker 2 次 $0.0450697、Reviewer 1 次 $0.0196117），任务列表和详情给出的值相同；Manager 的上下文 26,871 → 28,295 → 29,806 token，经 API 读回，历史页可见；通过 Settings 环境变量设 `CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000`、`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=31` 后，Manager 一轮里自动压缩 2 次（29,916 → 1,577、27,015 → 2,561），记录和历史页都可见；这一轮由改设置后续接的新进程执行，Claude 报会话累计 $0.13230075，记录为本轮 $0.08451635；重启服务后 Manager 第一轮，Claude 报 $0.13930815（修复前会记下的数），记录为 $0.0070074，差额正好是存下的会话累计值 $0.13230075；token 与该轮 `result.usage` 一致；Planner 的计划文件存下会话累计 $0.033885，等于它 4 个 turn 之和。无头 Chrome 截图查看了任务页（1280、768 px）、任务列表、Manager 和 Planner 的历史页、展开后带压缩事件的运行卡片。真实 Claude 花费约 $0.41（核实约 $0.18，验收约 $0.24）。测试：单测与真实 PG 915 通过（main 上 886），另有 5 个 `test_dashboard_command` 安装模式用例在 main 上同样失败。新增和改动的测试在 main 上有 12 个失败，另有 2 个文件因缺少 `run_usage` 模块无法导入 |
| 2026-10-13 | M3 交付（looper #27）：项目知识，只做核心。`project_documents` 表与 `GET/POST /api/projects/<id>/knowledge`、`PUT/DELETE .../knowledge/<doc_id>`；来源为手动录入、上传文本文件（JSON 里 base64）、引用项目默认目录里的文件（实时读取），不支持 URL；上限为单篇 2 MiB、每个项目 100 篇和 50 MiB、索引 4,000 个字符，超限返回 400 并写明上限，同一项目的修改持有项目行锁。Planner、Worker、Reviewer 每次运行读取项目知识：录入和上传的文档按内容哈希物化为只读文件，L3 在背景之后加 `## Project knowledge` 索引，`--add-dir` 由 `claude_cli.knowledge_args` 随 `Launch` 下发（知识目录；有引用路径且任务目录不是项目目录时加项目目录），Manager 没有；`prompt_layers.project` 记 `knowledge_sha256` 和 `knowledge_documents`；Planner 的重启签名加上挂载目录。界面：项目编辑里的 Knowledge 列表（新增文本、上传、引用路径、删除，篇数、存储量和索引字数读数）。核实（Claude Code 2.1.285，隔离参数下）：`--add-dir` 目录里的文件可读，该目录及其上级的 `CLAUDE.md` 不加载，不存在的目录不会让运行失败。验收（真实 Claude，haiku）：项目带一篇 3 KB 的录入文档（暗号在第 44 行）和一篇引用路径 `docs/deploy.md`（端口）；Worker 读了两篇并在报告里引用（行号和原文）；改了端口后在另一个目录跑的任务挂载了知识目录和项目目录，读到新端口 59322（这次是 Planner 读后写进步骤），再改一次并要求由 Worker 读，Worker 自己读到并引用了新的一行（60433）；索引增到 3,819 字符后再加一篇被拒（"this would make it 4270"），`../` 路径和 PNG 上传也被拒并说明原因；Worker 运行中新增一篇文档，Planner 下一轮换了新进程（pid 5178 → 6478）以 `--resume` 续接，记录的文档数 6 → 7；无头 Chrome 里完成新增文本、上传、被拒的路径、引用路径、删除，并截图查看了 1280 和 768 px。真实 Claude 花费约 $0.36。测试：单测与真实 PG 1175 通过（main 上 1094），另有 `test_dashboard_command` 的 5 个安装模式用例在 main 上同样失败，`-n 6` 下另一个用例两边都因 5 秒超时失败、单独跑通过。新增和改动的测试在 main 上 8 个失败、11 个出错，另有 2 个文件因缺少 `knowledge` 模块无法导入 |
| 2026-10-13 | 修复第 5.1 节核心缺陷：常驻 Manager、Planner 把 Claude 自己跑的一轮当成下一条消息的回复（looper #26）。**核实**（Claude Code 2.1.285，haiku，常驻参数；录制样例存于 `tests/fixtures/claude_stream/background_*.jsonl`）：带 `uuid` 写入的消息保留这个 uuid；Claude 读到消息就写 `command_lifecycle` `queued`，开始这一轮时写 `started`，`result` 之后写 `completed`；`started` 之后才是这一轮的 `system/init`，再之后才是 `--replay-user-messages` 的回显（`isReplay`，同一 uuid），所以回显不是这一轮的起点；`result` 用 `user_message_uuid(s)` 指明它回答的消息；不加 `--replay-user-messages` 时只少回显。后台 Bash 或后台子 agent 在空闲时结束：先有 `system/task_notification`（后台子 agent 自己的事件带 `parent_tool_use_id`，夹在两轮之间），再从 `system/init` 跑一轮到 `result`，没有 lifecycle 和回显，`result` 没有 `user_message_uuid`、带 `origin: {kind: task-notification}`；这时到达的消息立刻 `queued`，要等这一轮的 `result` 之后才 `started`（旧代码会把这一轮的话当成这条消息的回复）；在用户轮进行中结束的后台工作并入该轮（回显一条带 `origin` 的 `<task-notification>`）。**做法**：`ResidentClaude` 在进程存活期间持续读取输出；每条消息带自己的 uuid，用户轮从它的 `started`（或回显）开始、到下一个 `result` 结束，其余以 `result` 结束的事件是 Claude 自己的一轮，交给 owner 的 `autonomous` 处理；每个 `result` 读到时就按写出顺序取累计值增量；#20 的 `session_started`、M2 的基线、重启签名和 `stop()` 回收不变。Manager、Planner 的常驻命令加 `--replay-user-messages`；Claude 自己的一轮记成独立运行（`input_text` 为 "(no user message: …)"，`metadata.autonomous` 为 true）；Manager 把其中的话作为单独回复发到上一条已回答消息的渠道，Web 渠道把它写入该会话的聊天记录，不进任何消息的流；Planner 只记录和打日志，不用于计划决策。**验收**（真实 Claude，haiku，本分支的 `loopx serve` 在 8791 端口、一次性数据库）：第一条消息让 Manager 后台运行 `sleep 8`，流里回复 `STARTED`；后台结束后 Claude 自己跑了一轮，"Background task completed successfully (exit code 0)." 作为单独的助手消息进入该会话的聊天记录；第二条消息 "2+3" 的流里回复 `5`；三条运行记录花费 $0.0277812、$0.003947（autonomous）、$0.0034193，正好是 Claude 累计值 $0.0277812 → $0.0317282 → $0.0351475 的增量，`session.json` 存下 $0.0351475。真实 Claude 花费约 $0.14（核实约 $0.10，验收约 $0.035）。**测试**：新增 11 个，在 main 上全部失败（main 上第二条消息拿到的是后台那一轮的话）；单测与真实 PG 1138 通过（main 上 1127），另有 5 个 `test_dashboard_command` 安装模式用例在 main 上同样失败。**未核实**：agent teams、多个后台任务同时结束、飞书和 Telegram 的实际投递；Web 聊天页不会实时出现这条回复，打开或刷新会话时才显示 |

## 5. 不属于本 RFC 的待办

这些问题在开发过程中发现，独立排期，不阻塞 M1–M6。

### 5.1 核心缺陷（优先修，穿插在阶段之间）

（暂无。已修复的见第 4 节进度台账。）

### 5.2 工程待办

- **CI 基线**：CI 已改为手动触发（looper #7），而 #6 修掉了 chat-bundle 的已知失败。需要手动跑一次 Python Tests，确认 pytest 是否真正跑通；如果还有失败，逐项修复。
- **Optional Ark Turn workflow** 引用了已经删除的测试文件，需要修正或移除。
- **`scripts/install-from-github.sh`** 默认使用 `stable` 分支，但 looper 没有这个分支。
- **`test_dashboard_command` 的 5 个安装模式用例**在 `main` 上就失败：干净的 checkout 里没有构建 Chat 包（`loopx/web/chat/bundle-manifest.json` 不存在）。需要先构建 Chat 包，或者让这些用例自己准备构建产物。
- **`test_dashboard_launcher_preserves_early_service_error_context` 偶发超时**：在 `-n 6` 并发下会撞上 5 秒超时，单独运行通过（M3 发现）。
- **知识索引容量**：索引每行都带一个绝对路径（在 `~/.loopx` 下约 150 字），4,000 字的索引上限实际只能放 15–25 篇，比 100 篇的数量上限先用完。改用短的挂载别名或相对路径（M3 发现）。
- **runtime root 下的只读副本不清理**：插件与 skill 库（M1.6）和项目知识（M3）按内容哈希写出的只读副本，在内容替换或删除后仍留在磁盘上。需要在没有运行引用时回收。
- **HTTP 服务没有请求体大小上限**：知识上传等接口靠业务上限拦截，但服务本身不限制请求体大小（M3 发现）。
- **`agent_configs.permission_mode` 列已不再使用**（M1.7 起四类 agent 统一权限）：在后续的 schema 清理中迁移删除。
- **插件与 skill 库的上下文开销**：全部导入后，opus 首个请求的 token 约增加 86%（M1.6 实测）。结合 M2 的花费数据，评估停用不常用的 skill。
- **下一次发布**：M0–M3 及相关修复尚未发布。下个版本按 looper 的 Release workflow（推 tag 发布）进行，发布说明需覆盖 RFC 决策 4–8 带来的行为变化（个人配置隔离、默认 harness、所有 agent 免权限确认、渠道白名单等）。

### 5.3 非核心待办（M6 之后统一做）

- **完整的项目页** `#projects/<id>`：概览、知识、Skills、Settings 标签（M3–M5 先用项目信息编辑里的最小界面）。
- **项目知识的辅助功能**（M3 只做了核心）：在界面里修改已有文档（API 已支持 `PUT`）、查看正文、拖拽上传、项目视图里显示知识摘要。
- **网页聊天实时显示 Claude 自主一轮的回复**（#26 的遗留）：这类回复不对应任何提问，目前写进会话记录，但不会实时推送，要刷新或重新打开会话才看得到。
- **界面小问题**：
  - Tasks 列表页内容贴边，没有内边距；
  - 任务详情里 Config 显示成 `"{}"`（JSONB 被当成字符串写入）；
  - 任务详情的返回按钮没有可访问名称；
  - 窄屏下侧栏不会收起；
  - 新建任务时"顺带新建项目"的表单里没有背景输入框。

### 5.4 验证缺口

以下行为已实现，但还没有在真实环境中验证过，相关改动或排查时优先补上：

- **飞书、Telegram 的真实流量**：白名单与投递（M1.7、#26）只用本地替身验证过。
- **agent teams**：`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` 已作为默认环境变量下发，但没有实际拉起队友验证过。
- **多个后台任务同时结束**的自主轮切分（#26）。
- **非 haiku 模型**：M2、M3 的真实验收只用了 haiku，opus、sonnet 走同一代码路径，没有实跑。
- **项目自带的权限设置**：项目的 `.claude/settings.json` 或托管设置禁止某些工具、关闭 bypass 模式时，agent 的表现（M1.7）。
- **插件里的 agent 和命令**：M1.6 只验证了 skill 的加载和调用。
- **Planner 不亲自改文件**：M1.7 起只靠 role prompt 约束。M6 的回归评测里加一项检查。
