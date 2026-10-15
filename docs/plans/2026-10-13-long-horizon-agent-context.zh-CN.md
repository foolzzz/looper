# 开发计划：长程 Agent 上下文体系（M0–M9）

- **状态：** M0–M7 全部交付（M7a 计划状态入库、M7b 任务记忆、M7c 及时沉淀）；M8 全部交付（M8a 能力目录与安装、M8b MCP 服务器、M8c 进库来源、M8d 配置层级统一、M8e 导出与导入）；M9a 运行时、provider 与 agent 的注册和选择已交付，M9b Claude Code 接 API key 与兼容接口、M9c Codex 运行时、M9e OMP 运行时已交付；上游 LoopX 清理 B1–B4 已交付；全面改名为 Looper 已交付；其余见第 5 节待办；**v2.6.0 已发布（2026-10-14）**。
- **依据：** [RFC：长程 Agent 的上下文体系 v0](../architecture/rfcs/long-horizon-agent-context-v0.zh-CN.md)。
  设计原则、分层模型和已作出的决定以 RFC 为准；本文只管"怎么分阶段做、做到什么算完成、现在做到哪了"。
- **仓库：** `foolzzz/looper`，基线 `main`
- **最近更新：** 2026-10-14

## 1. 工作方式

1. **一个阶段一个 PR。** 每个阶段都可以独立评审、独立回滚。没有依赖关系的阶段可以并行；开 PR 前先合入最新的 `main`。
2. **修缺陷先复现。** 修缺陷时先写出能复现它的测试；新功能不必证明测试在 `main` 上失败。
3. **开 PR 前在本地跑一次裸 `pytest`。** 用 `LOOPER_TEST_DATABASE_URL` 指向一个新建的数据库，约 1 分钟，这就是合入的门槛。CI 也会在每个 PR 上跑，但合入不等它。
4. **一条规则一个测试。** 测在离用户入口最近的那一层。不钉文案（文案本身就是契约时除外），不测 mock 的调用链，变体用参数化。
5. **真实 Claude 只在改动 prompt、契约或注入方式时跑。** 用 haiku 跑一个最小场景，花费不超过 $0.5。
6. **截图只在界面改动时做，一张即可。**
7. **文档从简。** 每个阶段交付后在第 4 节记一行（不超过 3 句），并更新第 2 节的状态列；`agent-system.md` 只在架构变化时改。
8. **合入后部署。** 合入后同步本机 `main`，前端有改动就重新构建 `web/dist`，然后执行 `looper service restart` 重启本机服务（8767，由 `looper service` 托管）。
9. **核心优先。** 每个阶段只做核心：后端、agent 行为、能用的最小界面（能配置、能看到结果）。可视化、交互细节、
   界面美化和"顺手加上"的辅助功能记入第 5 节的"非核心待办"，M6 之后统一做。

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
| M4 | 项目 skill | ✅ 已交付（looper #32） | M3 |
| M5 | 项目执行配置 | ✅ 已交付（looper #37） | M2 |
| M6 | prompt 回归评测 | ✅ 已交付（looper #36） | M1 |
| M7a | 计划状态入库：计划、步骤和 agent 会话以 PostgreSQL 为唯一来源 | ✅ 已交付（looper #45） | M2、M5 |
| M7b | 任务记忆：任务日志、进度与发现注入、经验沉淀为项目知识 | ✅ 已交付（looper #46） | M7a |
| M7c | 及时沉淀：日志记失败与重试、决策理由、用户指令、会话切换；换 agent 前的交接摘要和新会话的续接简报；Manager 续接；项目经验随时进草稿 | ✅ 已交付（looper #66） | M7b、M9a |
| M8a | 能力目录与安装：插件和 skill 先进目录，再按范围（全局或项目）和角色安装；项目 skill 并入目录 | ✅ 已交付（looper #48） | M1.6、M4 |
| M8b | MCP 服务器进目录，安装方式相同；密钥存库、接口打码 | ✅ 已交付（looper #49） | M8a |
| M8c | 进库来源：上传、git、界面新建与编辑 | ✅ 已交付（looper #51） | M8a |
| M8d | 配置层级统一：系统默认 → 角色 → 项目 → 项目里的角色 | ✅ 已交付（looper #50） | M8a |
| M8e | 导出与导入：一个文件，应用前预览，密钥默认留空 | ✅ 已交付（looper #53） | M8b、M8c、M8d |
| M9a | 运行时、provider 与 agent 的注册和选择；现有 Claude Code 路径抽成第一个运行时适配器 | ✅ 已交付（looper #62） | M8d |
| M9b | Claude Code 接多种 provider：订阅 OAuth、Anthropic key、兼容 anthropic 协议的接口；按 provider 价格计算花费 | ✅ 已交付（looper #68；DeepSeek、Kimi 真实 key 待维护者验证） | M9a |
| M9c | Codex 运行时：四类角色都支持（含常驻会话），事件统一、MCP 翻译、API key（Codex 登录在 §5.0 第 3 项交付） | ✅ 已交付（looper #67） | M9a |
| M9d | SDK 运行时与 norma SDK | 待定（norma 就绪后） | M9a |
| M9e | OMP 运行时：先只读核实能力、填能力矩阵，再写适配器 | ✅ 已交付（looper #70） | M9c |

执行顺序：M1 → M1.5 → M1.6 → M1.7 → M2 → M3 → M4 → M5 → M6 → M7a → M7b → M8a → M8b、M8c、M8d（可并行）→ M8e。

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
  - 索引在 L3 背景之后（`## Project knowledge`，每个目录写一次 `In <目录>:`，其下每篇一行 `- <标题>: <摘要> (<相对路径>)`，已不存在的引用文件标为 `missing`）；`--add-dir` 由 `claude_cli.knowledge_args` 生成、随 `Launch` 下发：录入和上传的文档一组一个目录（按文件名和内容的哈希命名），挂载这个目录，有引用路径且任务目录不是项目目录时再挂载项目目录；Manager 都没有；
  - Planner 的重启签名加上挂载的目录（索引本身已在 prompt 哈希里）；`prompt_layers.project` 记 `knowledge_sha256`、`knowledge_documents`，读不到时记 `knowledge_error`；
  - 已核实（Claude Code 2.1.285，隔离参数下）：agent 能读 `--add-dir` 目录里的文件，该目录及其上级的 `CLAUDE.md` 不会被加载，不存在的目录不会让运行失败；
  - 索引原来每行带绝对路径（在 `~/.loopx` 下约 150 个字符），15 到 25 篇就占满索引；后改为相对路径，短文档能放满 100 篇（见进度台账）。

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
- **核实后确定的做法：**
  - `project_skills` 表：名称、一行描述、`SKILL.md` 正文（`content`）、启用开关和时间戳；删项目时连带删除。
    - 名称是小写字母、数字和单个连字符组成的 slug，项目内唯一，数据库也用 CHECK 约束同样的格式；
    - 正文不带 frontmatter，以 `---` 行开头的拒收，frontmatter 由名称和描述生成。
  - 上限，超限返回 400 并写明上限，重名返回 409：
    - 名称 64 个字符（Claude Code 对 skill 名称的上限）；
    - 描述 1,024 个字符（skill 格式的上限；本机 104 个个人 skill 里最长的描述 975 个字符）；
    - 正文 64 KiB（skill 被调用时整段进上下文；那 104 个里最大的 `SKILL.md` 是 61 KB，长资料应放项目知识）；
    - 每个项目 20 个（每个启用的 skill 的名称和描述都会列进每次运行的上下文）。
    - 增删改持有项目行锁，与项目知识相同。
  - API：`GET`/`POST /api/projects/<id>/skills`，`PUT`/`DELETE /api/projects/<id>/skills/<skill_id>`；`PUT` 兼管启用开关和修改；不返回正文。
  - 物化与挂载：
    - Planner 每轮、Worker 和 Reviewer 每次运行开始时读取项目已启用的 skill（`load_project_skills`）；
    - 写成一个插件 `<runtime_root>/.local/projects/<project_id>/skills/<sha256>/project/`（`plugin.json` 和 `skills/<名称>/SKILL.md`），`<sha256>` 是这组 skill 的哈希；
    - 复用 M1.6 的 `write_once`（原 `_write_once`，改为公开）：只写一次、只读；
    - 以一个 `--plugin-dir` 挂载在插件库之后，由 `claude_cli.project_skill_args` 生成、随 `Launch` 下发；
    - Manager 没有。
  - 记录：`metadata.project_skills` 记下项目、这组 skill 的哈希（即目录名）、插件名和每个 skill 的名称与 `SKILL.md` 哈希，读不到时记 `error`；activity 记 `Project skills applied (N skills)`，Planner 另记 `changed`、`removed`。
  - 不在系统提示里另列清单（RFC 第 6 节原写"L2 放清单"）：Claude Code 自己把每个 skill 的名称和描述列给模型，再列一遍是重复。
  - Planner 的重启签名不用改：插件目录按内容哈希命名，已经在启动命令里。读取失败时沿用上一轮的 skill，与项目知识相同。
  - 已核实（Claude Code 2.1.285，隔离参数加 `--dangerously-skip-permissions`，haiku）：
    - 插件的 skill 在 init 事件里叫 `project:<名称>`，`plugins` 里是 `{name: "project", source: "project@inline"}`；
    - 模型用 Skill 工具调用只读目录里的 skill；
    - frontmatter 里用 JSON 双引号写的描述能正常读取；
    - 两个同名插件（例如插件库里也有一个叫 `project` 的）会一起加载，skill 共用同一个前缀；
    - 只有 skill 的插件不会在 `~/.claude/plugins/data/` 下建 `project-inline` 目录。

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
- **核实**（Claude Code 2.1.285，haiku，`--print` stream-json）：`--max-budget-usd` 在每次请求之后检查，所以一轮可能超出上限一个请求的花费（上限 $0.001，这一轮花了 $0.0103）；
  超限的一轮照常回复，但 `result` 为 `subtype: error_max_budget_usd`、`is_error: true`、没有 `result` 文本，`errors` 为 `["Reached maximum budget ($0.001)"]`；
  上限按进程累计，同一进程的后续每一轮都立即以同样的 `result` 结束，不再发请求，进程退出码为 1；
  `--resume` 续接的新进程只算自己的花费，不把会话以前的花费计入上限（而 `total_cost_usd` 仍然计入）。

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

### M7 任务记忆

长程任务里，Planner 只能从每轮消息和自己的会话里记住做过什么；会话被压缩或丢弃后，已接受步骤的发现和踩过的坑就丢了。
M7 先让计划状态有唯一来源（M7a），再在其上加任务记忆（M7b）。

#### M7a 计划状态入库

- **目标：** 计划、步骤和 agent 的 Claude 会话以 PostgreSQL 为唯一来源；服务重启后任务从数据库续跑。
  消息总线的日志（`.local/agent-bus`）和派生缓存（prompt、插件库、知识、skill 的只读副本）仍是文件。
- **范围：**
  - `task_steps` 补齐 Planner 决策要用的字段（标签、依赖、送审、验收标准、重试次数、反馈、Reviewer、迟到的用户消息）；
    新增 `task_plans`（每个任务一行：计划 id、状态、来源会话与渠道、总结、失败原因）和 `agent_sessions`（`manager`、`planner-<task_id>` 的会话 id 与累计值）；
    项目字段不入库，每轮开始前重新读取，项目变化时照旧以 `--resume` 重启；
  - 每次状态变化只写它改动的行，涉及多行的变化（新计划、replan、聊天加步骤、步骤定案连同计划总结）在一个事务里；Planner 和 Manager 不再写 JSON 文件；
  - 重启：上一次服务留下的、计划仍在进行的任务在启动时续跑，不重新规划，Planner 续接会话，M2 的花费基线不变；
    执行中的步骤以同一次尝试重跑并保留反馈，在审的步骤重新送审，被打断的决策按已存结果重新决策；其余遗留任务照旧标为 stopped；
    因上限失败后"提高上限并继续"（looper #43）读取同样的行；
  - 启动时一次性导入旧文件（记录在 `schema_migrations`）：当前格式的计划文件和 Manager 的 `session.json`，数据库已有的行优先；常驻 Planner 之前的旧格式文件跳过并记日志；文件留在磁盘上，可以删除。
- **验收：** 替身 Claude 下，多步骤任务在部分步骤已接受、一个在跑时用 SIGINT 停掉服务再启动，任务从数据库完成：不重新规划、会话续接、重试和反馈保留、不写计划或会话文件；
  新格式文件正确导入、旧格式跳过、第二次运行无操作；只读复制本机的计划文件导入一次性数据库并报告计数；现有测试和 M6 替身评测不变，任务页仍显示步骤。
  不改 prompt 和契约，不需要真实 Claude。

#### M7b 任务记忆

- **目标：** Planner 和 Worker 在长任务里记得已经做成了什么、发现了什么、踩过什么坑；任务结束后，值得留下的经验进入项目知识。
- **范围：**
  - PostgreSQL 里的任务日志表：每条属于一个任务和一个步骤，记结果、关键发现和坑；
  - Worker 和 Reviewer 的回复契约新增可选的 `learnings` 字段；
  - 步骤被接受时由代码写一条日志（步骤结果加上 `learnings`），不靠模型自己记；
  - Planner 每轮注入有长度上限的"进度与发现"一节（新的在前，按字符数截断），计划行里带上已接受步骤的总结；
  - Worker 拿到与自己步骤相关的日志条目，不只是直接依赖步骤的报告；
  - 任务结束时 Planner 把经验整理成一篇文档写入项目知识（M3），由项目级开关控制，或作为草稿由用户确认后入库。
- **验收：** 契约和注入方式都变了，需要 M6 真实模式评测与基线对比；
  替身 Claude 下，步骤接受后日志有对应条目，Planner 下一轮的消息里能看到，超出上限时截断最旧的；
  Worker 收到相关条目；开关关闭时任务结束不写项目知识，打开时写入或生成待确认的草稿。

#### M7c 及时沉淀

- **目标：** 会话不能续接时（换 provider 或运行时、上下文压缩、会话丢失），新会话仍能拿到完整的进度和经验。
- **范围：**
  - 任务日志新增事件类型：失败与重试（原因和经验）、Planner 的决策与理由（replan、重试、放弃）、用户指令（纠偏和约束，注入时不截断）、会话切换；
  - 交接轮：要换 provider 或运行时、旧会话仍可用时，先让旧会话写一段交接摘要入日志；新会话开局拿到续接简报（计划状态、用户指令、交接摘要、最近日志）；旧会话不可用时只用日志；
  - Manager 新会话开局带上各渠道最近对话的摘要（取自聊天记录）；
  - Worker 的经验可标"适用于整个项目"，这类经验直接进项目知识的草稿（`distill_lessons` 的草稿流程），不必等任务结束。
- **验收：** 替身 Claude 下强制新开会话，续接简报里有用户指令、失败经验和交接摘要，任务能完成；M9b 合入后，M6 加一个"中途换 provider"的真实评测任务。
- **确定的做法：**
  - 一次重试只记一条 `step_retry`（理由即 Planner 的反馈），放弃记为该步骤的 `step_failed`（带 Planner 的理由）；`decision` 记 replan、聊天加步骤和到达花费上限；
  - 用户指令有自己的 8,000 字符预算，整条注入、不与其他条目争预算，只有超出这个预算时才省略最旧的并计数；交接摘要只进续接简报，不进每轮的"进度与发现"；
  - "能否续接"由 `looper/agents/session_switch.py` 的 `agent_switch` 一处决定（provider 规则由 M9b 加在这里：运行时类型和 provider 都不变才续接）；旧会话"可用"指它的进程还在运行，交接轮失败（如 provider 不可用）或进程已不在时只用日志；
  - 项目级经验在 `distill_lessons` 为 `draft` 或 `write` 时到达即进草稿（`write` 也进草稿，因为单条经验未经审阅），为 `off` 时只留在任务里；
  - Manager 续接只读网页聊天存下的消息（飞书、Telegram 的对话不入 `chat_messages`），不含任务的 Planner 线程（`planner-<task_id>`）。
- **待办：** M9b 合入后，在 M6 加"中途换 provider"的真实评测任务并跑一次（M7c 交付时 M9b 尚未合入）。

### M8 能力目录与配置层级

插件、skill、MCP 和各项执行配置以前各有各的来源和生效方式。M8 按 RFC 决策 9 把它们统一成两件事：
条目先**进库**（目录），再**安装**到全局或某个项目、限定角色；配置按"系统默认 → 角色 → 项目 → 项目里的角色"
逐层解析。M8a 是基础，M8b、M8c、M8d 在它之上可以并行，M8e 最后做。

#### M8a 能力目录与安装

- **目标：** 插件和 skill 只在安装的地方、对安装的角色生效；导入只是进库；项目 skill 并入同一个目录。
- **范围：**
  - `library_items`、`library_files` 作为目录，加上 `origin`（machine、upload、git、ui、project）和项目写的条目的所属项目；
    新增安装表：条目、范围、项目、角色、启用开关、时间戳，每个条目在每个位置只有一条；
  - 迁移：已导入的条目一律不安装；`project_skills` 等价迁入目录后删除，幂等，记在 `schema_migrations`；
  - 一个解析函数给出（角色，项目）加载的条目，四类 agent 的启动都用它，取代 `load_library` 和 `load_project_skills`；
    按内容哈希物化的只读副本和回收照旧，Manager、Planner 的条目集合变化时照旧以 `--resume` 重启；
  - API：列出和读取目录条目；在全局或项目范围安装、改角色或启用开关、卸载；项目视图列出它自己的安装和继承的全局安装；
    项目里新建、修改、删除 skill 照旧可用，改为写目录条目加上它在本项目的安装；
  - 界面：Settings 里的目录（种类、来源、装在哪里，全局安装带角色勾选和卸载）；项目页的 Skills 标签
    （装了什么，含继承的全局安装；从目录安装并选角色；新建和修改项目写的 skill）。
- **验收：** 解析按角色和范围生效（全局只给 worker 的 skill 到达 Worker、不到 Planner 和 Manager；项目安装只到达
  该项目的任务 agent）；`project_skills` 迁移后名称和角色不变；已导入的条目迁移后未安装；卸载或停用后下一次运行
  不再加载；回收仍保留被挂载的目录；真实 Claude（haiku，不超过 $0.5）下全局只给 worker 的目录 skill 出现在 Worker
  的 init 里、不在 Planner 的 init 里，迁移来的项目 skill 仍叫 `project:<name>` 并被 Worker 调用；
  Settings 目录和项目 Skills 标签各一张截图。
- **核实后确定的做法：**
  - 目录条目新增 `origin`、`owner_project_id`（项目删除时连带删除它写的条目）和 `description`；名称在项目以外的条目
    之间唯一，项目写的条目在所属项目内唯一；条目上的 `enabled` 删除，启用开关只在安装上；
  - 安装表 `library_installs`：`scope` 为 `global` 或 `project`，`roles` 是四个角色的非空子集，项目范围不含 `manager`，
    这些都有 CHECK 约束；项目写的 skill 只能装在项目里，不能全局安装；
  - 一次性迁移在 `schema.sql` 里（`library_catalog_installs`）：`project_skills` 的每一行成为保留原 id 的目录条目，
    `SKILL.md` 按运行时的写法生成（frontmatter 来自名称和描述），在所属项目安装给 planner、worker、reviewer，启用状态不变；
  - 解析：`db.library.list_installed(role, project_id)` 取启用、含该角色、全局或在该项目的安装；
    `library.load_installed` 去重（同一条目既全局又在项目里只加载一次，记为项目安装），插件各一个 `--plugin-dir`，
    项目写的 skill 链接进 `project` 插件，其余 skill 链接进 `personal` 插件，顺序是插件、`personal`、`project`；
  - 同一个项目里不能有两个同名的项目 skill（都会叫 `project:<name>`）：安装、新建、改名时冲突返回 409，
    万一并发写入，解析时保留本项目自己的那个；
  - 每次运行的 `metadata.library` 记下角色、项目以及每个条目的插件和范围，不再写 `metadata.project_skills`；
    Planner 读不到安装时沿用上一轮的条目，Worker 和 Reviewer 本次不加载并在活动里警告；
  - API：`GET /api/catalog`、`GET /api/catalog/<id>`、`POST /api/catalog/import`、`POST /api/installs`、
    `PUT`/`DELETE /api/installs/<id>`、`GET /api/projects/<id>/installs`，项目 skill 的 `POST`/`GET`/`PUT`/`DELETE`
    路径不变（`PUT` 不再接受 `enabled`，开关在安装上）；去掉 `/api/settings/library*` 和项目 skill 列表接口。

#### M8b MCP 服务器

- **目标：** MCP 服务器和插件、skill 一样进目录、按范围和角色安装。
- **范围：**
  - MCP 服务器作为一种目录条目（命令或 URL、参数、env、header）；
  - 密钥（env 的值、header）存数据库，API 和界面里打码；
  - 可以从本机 `~/.claude.json` 只读导入；
  - 每次运行把它安装的服务器写成只读的 MCP 配置文件，以 `--mcp-config` 挂载，保留 `--strict-mcp-config`。
- **验收：** 真实 Claude 的 init 里只出现安装给该角色的 MCP 服务器；API 返回里看不到密钥；卸载后下一次运行不再加载。
- **核实后确定的做法**（Claude Code 2.1.285，`--print` stream-json，带隔离参数）：
  - `--mcp-config <文件>` 的格式是 `{"mcpServers": {<名称>: {"type": "stdio", "command", "args", "env"}}}`，http、sse
    为 `{"type", "url", "headers"}`；与 `--strict-mcp-config` 同用时照常加载，init 里 `source` 为 `dynamic`，工具名
    `mcp__<名称>__<工具>`，`--dangerously-skip-permissions` 下直接调用；`--strict-mcp-config` 仍挡掉 claude.ai 连接器；
  - 插件自带的 MCP 服务器（`--plugin-dir` 里的 `.mcp.json`）在 `--strict-mcp-config` 下**不加载**，不加这个参数时以
    `plugin:<插件>:<名称>` 加载；所以 MCP 只能经目录安装进入运行；
  - 配置存 `library_mcp_servers`，env 和 header 的值只经 `get_mcp_server(s)` 读出，目录行只带名称，接口显示 `***`；
    条目的 `sha256` 是整份配置的哈希，Manager、Planner 换配置时照旧以 `--resume` 重启。

#### M8c 进库来源：上传、git、界面

- **目标：** 除了从本机导入，还能上传、从 git 导入、在界面里新建和编辑目录条目。
- **范围：**
  - 上传 `SKILL.md` 或 zip（沿用 M1.6 的上限和二进制、链接规则）；
  - 从 git 仓库导入（只读克隆到临时目录，记下 URL 和提交）；
  - 在界面里新建和编辑 skill；
  - 来源记在 `origin`，重新导入只影响同一来源的条目。
- **验收：** 三种来源各进库一个条目，安装后真实 Claude 能调用；超限、二进制和越界链接被拒绝并说明原因。

#### M8d 配置层级统一

- **目标：** harness、模型、effort、环境变量和单次运行上限按"系统默认 → 角色 → 项目 → 项目里的角色"逐层解析。
- **范围：**
  - 把这几项配置的来源统一为这四层，后面的覆盖前面的；Manager 只有"系统默认 → 角色"两层；
  - 吸收原第 5.3 节"按角色分别设模型或单次上限"；
  - 每次运行记录每项配置取自哪一层；
  - 界面在 Settings、Agents 页和项目 Settings 标签里按层显示和编辑。
- **验收：** 每一层的设置都能在对应 agent 的启动参数或记录里读回；没有设置的项目与现在一致。

#### M8e 导出与导入

- **目标：** 把目录、安装和配置导出成一个文件，在另一台机器或另一个库里导入。
- **范围：**
  - 一个文件包含目录条目、安装和各层配置；
  - 导入前先预览：新增、变化、冲突；
  - 密钥默认不导出，留作占位符，导入后提示补填。
- **验收：** 导出后导入到一个新库，再导出得到相同的内容（密钥除外）；预览与实际应用的变化一致。
- **核实后确定的做法：**
  - 文件是一个 JSON（`loopx-config/v1`）：目录文件都是 UTF-8 文本，JSON 原样容纳，一个可读的文件便于分享前检查；
    按条目分段读写，CLI 直连数据库（真实目录约 22 MB，超过服务端 8 MiB 的请求上限），Settings 的下载按段流式返回；
  - 项目的目录和本机路径来源不导出，路径文档只作引用并标 `repoint`；导入的条目保留原来的 origin；
  - 界面导入当时没做（服务端在路由前整读请求体并限 8 MiB），后来按路由放宽上限、流式落盘、预览与应用分两步补上（见第 4 节）。

### M9 agent 多样性：运行时与 LLM provider

依据 RFC 决策 10：agent = 运行时 + LLM provider + 模型。M9a 先打地基，之后 M9b 与 M9c 并行。
详细设计、能力矩阵和待决问题见 [多运行时 agent 架构 RFC](../architecture/rfcs/multi-runtime-agents-v0.zh-CN.md)。

#### M9a 注册与选择

- **目标：** 运行时、provider 和 agent 可以注册；按层级和任务选用 agent；现有行为不变。
- **范围：**
  - 三张表：`agent_runtimes`（类型、名称、二进制或 SDK 位置、探测到的版本和能力、默认参数）、
    `llm_providers`（协议列表、认证方式：OAuth 关联到运行时的登录，或 API key 存库打码；base_url；模型及价格）、
    `agents`（运行时、provider、模型、effort、运行时选项）；内置一个"Claude Code + Claude 订阅 + 默认模型"的 agent；
  - 运行时适配器接口，由现有 Claude Code 路径抽出：启动参数、system prompt 注入、隔离、把事件解析为内部事件、
    会话续接、花费；
  - 分层配置新增 `agent`（模型并入 agent）；新建任务时可以按角色覆盖 agent；
  - 校验：协议有交集、OAuth 只配给发放它的运行时、角色所需能力具备；
  - 界面：Settings 里注册运行时、provider 和 agent，分层表单里选 agent，新建任务时可按角色选；
  - 取代 `llm_profiles` 与 `agent_configs.runtime`、`llm_profile_id`；M6 评测可以指定 agent。
- **验收：** 不做任何配置时，四类角色的启动参数和 system prompt 与 main 逐字节一致；校验的正反例各有测试；
  任务级覆盖只影响该任务的对应角色。
- **核实后确定的做法：**
  - 适配器接口在 `loopx/agents/runtimes`，按多运行时 RFC 第 5 节命名（`probe`、`capabilities`、`bind_provider`、
    `parse`、`cost` 等）；内部事件暂用 Claude Code 的 stream-json 事件，其他适配器翻译成它；插件和知识的物化仍由
    `library`、`knowledge` 按 Claude 插件格式完成，需要别的格式的运行时（M9c）再加 `materialize`；
  - 任务级覆盖存在 `task_agent_overrides` 表（外键指向 agent，被选中的 agent 不能删），接口字段为 `agent_overrides`；
  - 运行启动时再校验一次选中的 agent，不通过则这次运行失败并写明原因，不退回别的 agent；常驻会话记下所属运行时类型，
    换了运行时类型就新开会话，同一类型换模型照旧续接；
  - provider 的模型目录（上下文窗口、四种价格、effort 档位，未知为 `null`）和 `max_concurrency`（`null` 为不限）
    只存储和校验，M9b、M9c 再使用；每次运行记 `cost_source`（M9a 恒为 `runtime`）；
  - 未做：运行时、provider、agent 的启用开关和运行时选项（没有使用方，以后加列即可），目录条目的运行时兼容字段。

#### M9b Claude Code 接多种 provider

- **范围：** 订阅 OAuth（现状）、Anthropic API key、兼容 anthropic 协议的接口（GLM、Kimi 等，经 base_url 与 key 接入）；
  用 API key 时确认不会退回订阅登录（先核实环境变量的优先级）；接第三方接口时按 provider 登记的价格计算花费。
- **验收：** 每种 provider 各跑一次 M6 真实模式评测（需要维护者提供对应的 key），花费与 provider 价格核对一致。
- **核实后确定的做法**（Claude Code 2.1.285 的核实结果写在多运行时 RFC 第 7、8 节）：
  - 第三方接口的 key 放 `ANTHROPIC_AUTH_TOKEN`（GLM、DeepSeek、Kimi 文档都这样写），并去掉继承的 `ANTHROPIC_API_KEY`；
    Anthropic 官方接口放 `ANTHROPIC_API_KEY`；`apiKeySource` 在 token 和订阅下都是 `none`，用转发代理核对请求头；
  - 模型目录新增 `background_model`，provider 新增 `env`；内置价格表在 `runtimes/pricing.py`，预设在
    `provider_presets.py`；一个 provider 只有一个 `base_url`，所以预设按接口（Anthropic 格式、OpenAI 格式）各建一个；
  - 单次软上限按"一次运行"（Worker 一次、常驻会话一轮）计算，由运行记录器累加内部 `usage` 事件，供 M9c 复用；
  - 续接规则并入 M7c 的 `session_switch.agent_switch`；M6 评测加 `--provider-preset`（key 从 `LOOPER_EVAL_API_KEY` 读）。

#### M9c Codex 运行时

- **范围：** Codex 适配器支持四类角色：Worker、Reviewer 一次一轮；Manager、Planner 常驻会话与续接（先核实 app-server
  协议或逐轮续接）；独立的 `CODEX_HOME` 隔离且保留登录；system prompt 注入、MCP 翻译为 `config.toml`、
  目录条目的兼容性（先核实 Codex 对 skill 的支持）；按 token 用量与 provider 价格计算花费；OpenAI 登录与 API key。
- **验收：** 用 Codex agent 跑 M6 真实模式评测（四类角色都用 Codex 跑一次，再与 Claude 基线对比）。
- **核实后确定的做法**（技术验证结果见多运行时 RFC 第 6 节）：
  - 只用 API key provider（P1）；Codex 登录不做；
  - Worker、Reviewer 是一次 `codex exec --json --ephemeral … -`，提示从 stdin 读完即关，运行中不能再引导，Planner 的
    引导消息留给这一步的决策轮；Manager、Planner 每轮起一个 `exec … resume <thread> -` 进程（`ResidentCodex`），
    没采用实验性的 `app-server`，`queue` 送不进 `exec`；
  - 隔离：每次运行一个 Looper 自己的 `CODEX_HOME`（`<runtime_root>/.local/codex`），只有生成的 `config.toml` 和
    `AGENTS.md`，经 `env CODEX_HOME=…` 写在命令里；常驻会话另有按 owner 的会话目录，每轮前同步这两个文件；
    原生 skill 列表和插件同步关掉（前者总会列出 `$HOME/.agents/skills`）；一天没写过的运行目录在写新目录时删掉；
  - system prompt 写进 `AGENTS.md`，前面是适配器加的"运行时说明"；`developer_instructions` 续接时不更新，没用；
  - P9：MCP 写进 `config.toml`（stdio、HTTP；SSE 跳过），skill 只读挂载并在运行时说明里列索引，插件的 hook、命令、
    agent 跳过，都记在运行记录的 `runtime_catalog`；
  - 只有 token：`turn.completed` 的会话累计值换算成每轮的 `usage` 事件和结果里的 `modelUsage`，单次上限只能在一轮结束时
    判断；没有压缩事件和自主轮；
  - `looper serve` 发现 PATH 上有 `codex` 时注册内置运行时 `codex`；评测加 `--register-agent` 和每个任务的 token 数。

#### M9e OMP 运行时

- **范围：** 在 M9c 验证过适配器接口之后，先只读核实 OMP 的能力（一次性运行、常驻会话、MCP、skill、隔离），写回能力矩阵，再实现适配器；开发用本机 CPA。
- **验收：** 用 OMP agent 跑 M6 真实模式评测。
- **核实后确定的做法**（omp 18.4.3 的核实结果写在多运行时 RFC 第 6 节）：
  - 只用 API key provider；`models.yml` 的 `api` 按 provider 的协议依次选 `openai-responses`、`openai-completions`、
    `anthropic-messages`，密钥只在 `apiKey` 指名的环境变量里；
  - 隔离：每个 omp 进程的 `HOME` 指向 `<runtime_root>/.local/omp/homes/<sha256>`（同一套生成设置的运行共用），里面只有
    生成的 `models.yml`、`--config` 覆盖文件、prompt 文件、有 MCP 时的 `looper` 插件和共用原生库的链接；会把 omp 指回
    操作者目录的变量不传；shell 命令经 `PI_SHELL_PREFIX` 拿回操作者的 `HOME` 等变量、去掉密钥；工作目录不在 git 仓库
    里时关掉向上查找的发现来源；
  - Worker、Reviewer 是一次 `omp -p --mode json --no-session`，提示从 stdin 读完即关；Manager、Planner 每轮起一个进程
    （`--session-dir omp/sessions/<agent> --resume <id>`，`ResidentOmp`），与 `ResidentCodex` 共用
    `per_turn.PerTurnSession`；开新会话的那一轮删掉这个 agent 之前的会话；
  - system prompt 用 `--append-system-prompt`，前面是适配器加的"运行时说明"；
  - P9：插件原生加载（skill、命令、agent），hook 和输出样式不加载、插件自带的 MCP 服务器关掉，都记在
    `runtime_catalog`；Looper 的 MCP 服务器放进家目录的 `looper` 插件；知识不挂目录，按索引里的路径读；
  - 每个请求报一次用量，所以有上下文规模、单次上限能在请求之间停；子 agent 的用量随 `task` 工具结果报；压缩记为
    `compact_boundary`；
  - `looper serve` 发现 PATH 上有 `omp` 时注册内置运行时 `omp`；启动时回收没有进程在用的 OMP 家目录，以及已保存
    会话不再属于该运行时的 Codex、OMP 会话目录。

#### M9d SDK 运行时与 norma

- **范围：** 在适配器接口上接 SDK 类运行时；norma SDK 就绪后接入，验证它的双协议。范围在 M9a–M9c 之后再细化。

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
| 2026-10-13 | M4 交付（looper #32）：项目 skill，只做核心。`project_skills` 表（名称 slug、一行描述、`SKILL.md` 正文、启用开关）与 `GET/POST /api/projects/<id>/skills`、`PUT/DELETE .../skills/<skill_id>`；上限为名称 64 字符、描述 1,024 字符、正文 64 KiB、每个项目 20 个，超限 400、重名 409，修改持有项目行锁。Planner 每轮、Worker 和 Reviewer 每次运行读取项目已启用的 skill，按这组 skill 的哈希用 `write_once` 只读地写成插件 `<runtime_root>/.local/projects/<project_id>/skills/<sha256>/project/`，以一个 `--plugin-dir` 挂载在插件库之后（`claude_cli.project_skill_args`，随 `Launch` 下发），Manager 没有；记录新增 `metadata.project_skills`。界面：项目编辑里 Knowledge 下面的 Skills 列表（新增、开关、删除前确认）。验收（真实 Claude，haiku）：`loopx serve` 跑在一次性数据库上，插件库里放了一个合成插件 `demo-lib`；项目 A 有启用的 `release-stamp`（正文里分三段写着暗号）和停用的 `disabled-probe`，项目 B 没有 skill。A 的任务里 Planner 和 Worker 的 init 都列出 `demo-lib:hello`、`project:release-stamp`，没有 `disabled-probe`；Worker 调用了 Skill 工具（`project:release-stamp`），报告里写出暗号 `KESTREL-8243-VIOLET`，Planner 没有调用它；B 的任务里 Planner 和 Worker 的 init 只有 `demo-lib:hello`，Worker 自己列出可用 skill 也说没有 `project:` 开头的；两个工作目录的 `git status` 都是干净的。API 在真实服务上返回了 201、400、404、409。无头 Chrome 里完成新增（先被拒的名称显示服务端原因，改正后成功）、用键盘关掉开关、删除，并截图查看了 1280、768、390 px（390 px 下开关换到文字下面一行）。真实 Claude 花费约 $0.13（核实 $0.02，两个任务 $0.11）。测试：fork 测试集加两个新测试文件在真实 PG 上 881 通过（main 上同一 fork 测试集 799 通过）；整仓 pytest 按协调方要求没有跑完（上游测试正在另行删除）。新增和改动的测试在 main 上 14 个失败，另有 1 个文件因缺少 `project_skills` 模块无法导入（另 2 个守护用例在 main 上本来就成立） |
| 2026-10-13 | M6 交付（looper #36）：`evals/prompt_regression/` 用 5 个小任务（单步、依赖、送审、重试、聊天纠偏）经一次性 `loopx serve`（新数据库、空闲端口、临时目录）走 HTTP API，读回成功、步骤、重试、送审、花费、契约违约和 Planner 是否改文件，与上一次运行或已提交的基线对比输出 Markdown 表；替身 Claude 模式进测试套件，真实模式按需跑，单次运行带 `--max-budget-usd`、整轮有总花费上限。验收：替身模式 5/5 通过，并识别出故意的 Planner、Worker 违约和 Planner 写文件；真实 haiku 基线 5/5、$0.33，去掉 Planner 的 `REPLY_CONTRACT`（本地临时补丁，已撤回）后 0/5、违约 10 次，真实花费共 $0.82，分叉测试 843 通过。 |
| 2026-10-13 | M5 交付（looper #37）：项目执行配置，只做核心。`projects` 新增六个可空字段（模型、单次运行上限 `--max-budget-usd`、任务累计上限及到达后的处理方式、Planner 监督间隔、验收策略），未设置时行为与 main 一致，经项目 GET/PUT 和项目编辑读写，非法值返回 400。验证：fork 测试集加新测试在真实 PG 上 916 通过，真实 Claude 只用于核实 `--max-budget-usd`（约 $0.013）。 |
| 2026-10-13 | 第一批非核心待办（looper #41，第 5.3 节前两项）：网页聊天打开时，Manager 的 Claude 自主一轮的回复经 `GET /api/chat/events?session=` 实时推送并作为单独一条消息显示；任务 config 按 JSON 对象写入和返回，已按字符串存下的行读回为对象，非对象返回 400。界面：Tasks 列表内边距、返回按钮的可访问名称、1024 px 以下侧栏收成抽屉、新建任务时新项目的背景输入框；验证为通道层测试、真实 PG 测试和无头 Chrome 检查与截图 |
| 2026-10-13 | 第 5.2 节工程待办（looper #42）：知识索引每个目录写一次、每篇只带相对路径（已核实 Claude Code 只按工作目录解析相对路径），短文档能放满 100 篇；`loopx serve` 启动时回收当前内容不用、也没有运行中进程挂载的只读副本；请求体上限 8 MiB（超限 413）；删除 `agent_configs.permission_mode` 列。验证：真实 haiku 下 Worker 按新索引读到两篇文档（$0.07），回归评测 5/5（$0.33），真实 PG 758 通过。 |
| 2026-10-13 | 第 5.3 节任务花费辅助功能（looper #43）：项目设了任务上限时，任务页显示花费占上限的比例，达到 80% 起提醒；因上限失败的任务（`tasks.fail_kind = 'task_budget'`）可经 `POST /api/tasks/<id>/continue` 把项目上限提高到高于已花费的值后继续，非上限失败或新上限不高于花费返回 400。继续时任务保留步骤，Planner 读回保存的计划并续接会话，不重新规划：被上限打断的步骤重跑，在审的步骤重新送审。验证：真实 PG 契约测试，以及一次性 `loopx serve` 加带花费的替身 Claude 跑完「接近上限 → 达到上限失败 → 提高上限继续 → 完成」并截图。 |
| 2026-10-13 | 第 5.3 节项目页与知识、skill 辅助功能（looper #44）：侧栏点项目打开 `#projects/<id>`，有 Overview、Knowledge、Skills、Settings 四个标签，取代 Tasks 页里的项目编辑；知识文档和项目 skill 可展开看正文、就地修改，拖入文件新增文档，上传或拖入 `SKILL.md` 按 frontmatter 填好名称、描述和正文，新增 `GET .../knowledge/<doc_id>`、`GET .../skills/<skill_id>` 返回正文；历史页列出运行加载的项目 skill，Agents 页去掉 `permission_mode` 和 Sandbox 下拉框。验证：真实 PG 测试，无头 Chrome 在一次性服务上完成各标签的读写与拖放并截图。 |
| 2026-10-13 | M7a 交付（looper #45）：计划存进 `task_plans` 和补齐字段的 `task_steps`，Manager 与 Planner 的会话存进 `agent_sessions`，每次变化只写改动的行，多行变化一个事务，不再写计划和会话文件；启动时续跑计划仍在进行的任务（不重新规划、续接会话），并一次性导入旧文件。验证：真实 `loopx serve` 加替身 Claude 在 SIGINT 重启后续跑完成、因上限失败的任务重启后"提高上限并继续"完成；本机 11 个计划文件都是旧格式、全部跳过，导入 Manager 会话 1 个，第二次运行无操作。 |
| 2026-10-13 | M7b 交付（looper #46）：步骤被接受时由代码在定案事务里写一条 `task_journal`（Worker 的总结加 Worker、Reviewer 回复里新增的可选 `learnings`，至多 5 条、每条 300 字符），Planner 的 DECISION、SUPERVISE、CHAT 轮注入新的在前、至多 4,000 字符的「进度与发现」，计划行带已接受步骤的总结，Worker 首轮注入依赖在前、至多 2,000 字符的发现；项目开关 `distill_lessons`（默认关）打开时任务完成后多一轮 LESSONS，把日志整理成项目知识文档。验证：真实 PG 加替身 Claude 走完各条规则，真实 haiku 回归评测 5/5、违约 0（$0.34，基线 $0.33），一次最小任务里 Worker 报出 learnings、下一轮 Planner 与依赖步骤的 Worker 都看到、经验文档入库（$0.10）。 |
| 2026-10-13 | 启动自检（looper #47）：新增 `loopx doctor`，逐项检查 PostgreSQL（连接串的密码打码）、Claude Code CLI、它的登录（`claude auth status`，只读本机凭据、不调用模型；没有这个子命令的旧版本记为未检查）和飞书渠道开启时的 `lark-cli`，每项给出修复方法，有失败时退出码为 1。`loopx serve` 启动时先跑同一组检查：PostgreSQL 不可达仍然退出，但给出修复方法而不是堆栈；缺 `claude` 或未登录只记警告、照常启动，`GET /api/status` 返回 `checks`，控制台每页顶部显示横幅。验证：真实 PG 测试，本机实跑 `loopx doctor`，以及 `PATH` 里没有 `claude` 的一次性服务的横幅截图。 |
| 2026-10-13 | M8a 交付（looper #48）：能力目录与安装。`library_items` 加上 `origin`、所属项目和描述，新增 `library_installs`（全局或某个项目、角色子集、启用开关）；一次性迁移让已导入的条目都不安装，`project_skills` 保留 id 等价迁入目录并安装给三个任务角色后删除；`load_installed(role, project)` 取代 `load_library` 和 `load_project_skills`，新增目录、安装和项目视图接口，Settings 和项目 Skills 标签改为目录与安装。验证：仿真实规模的旧库（3 个插件、104 个 skill、5 个项目 skill）升级后 107 个条目都未安装、5 个项目 skill 各有一条三角色安装、启用状态不变，再跑一次不变；真实 Claude（haiku）下全局只装给 worker 的 skill 出现在 Worker 的 init、不在 Planner 的，迁移来的 `project:release-stamp` 被 Worker 调用并写出暗号，任务 $0.051；真实 PG 789 通过 |
| 2026-10-13 | M8b 交付（looper #49）：MCP 服务器作为 `mcp` 目录条目（stdio 或 http/sse），安装方式与插件、skill 相同；env 和 header 的值存在 `library_mcp_servers`、接口只显示 `***`（送回 `***` 保留原值），可从本机 `~/.claude.json` 只读导入；每次运行把安装给它的服务器写成一个按内容哈希命名的只读 `--mcp-config` 文件，回收照旧。验证：真实 haiku 下全局只装给 worker 的 stdio 服务器出现在 Worker 的 init 里并被调用、报出暗号，Planner 的 init 里没有，任务 $0.05（加上核实 CLI 共约 $0.10）；真实 PG 测试与 Settings 截图。 |
| 2026-10-13 | M8d 交付（looper #50）：harness、模型、effort、环境变量和单次运行上限统一存进 `agent_setting_layers`，按系统 → 角色 → 项目 → 项目里的角色逐层解析（Manager 只有前两层；环境变量逐键合并、可在后面的层取消；Reviewer 在角色层和项目角色层没有自己的 harness 时沿用同层 Worker 的），四类 agent 都用 `load_agent_settings` 启动，每次运行在 `settings_sources` 记下每项来自哪一层。一次性迁移把 Settings 默认值、各 agent 的模型和 Manager 的 harness、项目的模型和单次上限、项目的 Planner 和 Worker harness 原样迁入对应层并删除旧列；Settings、Agents 页和项目 Settings 标签分别编辑系统、角色、项目和项目里的角色层，每项显示当前取值和来源。验证：以 main（`72e72eb65`）的解析代码生成基准，两组旧数据迁移后四类 agent 的启动参数、环境变量和 system prompt 与基准逐字节相同，prompt 不变所以未跑真实 Claude；真实 PG 867 通过 |
| 2026-10-13 | M8c 交付（looper #51）：Settings 的目录可上传（或拖入）`SKILL.md` 或 skill、插件目录的 zip，预览后从公开 `https://` git 仓库勾选导入（同一 URL 再导入就地更新、保留安装），在界面新建和编辑 skill；都只进库不安装，项目外重名返回 409、可改名，编辑上传或 git 条目标记 modified，本机导入的条目只能重新导入。zip 拒绝绝对路径、`..`、链接和超限文件，克隆为 `--depth 1`、不跑 hook 和子模块、不读操作者的 git 配置和凭据、限时限大小、用后删除，每条规则一个真实 PG 测试。验证：一次性 `loopx serve` 从 `https://github.com/anthropics/skills` 预览出 20 个 skill（56 个二进制文件未导入），全部导入 384 个文件、6.3 MB，再导入 20 个不变；上传的 skill 全局装给 worker 后出现在 Worker 的 `--plugin-dir`、不在 Planner 的；真实 PG 902 通过。 |
| 2026-10-13 | M8e 交付（looper #53），M8 完成：`loopx config export` 把系统和角色层、角色 prompt、目录（含文件和 MCP）、全局安装、渠道以及可选的项目（`--include-projects`）写成一个 JSON 文件，密钥默认是占位符，`loopx config import` 默认只读预览、`--apply` 在一个事务里按自然键写入（同键就地更新、不删文件外的、占位符不覆盖已存的密钥），Settings 可流式下载导出。验证：真实 PG 测试覆盖往返、占位符、预览不写、冲突更新、重复导入不变和超过 8 MiB 的导出；对线上库只读导出（107 个条目、2175 个文件、23.2 MB），导入新库的预览为 115 项新增，应用后再预览全部不变、再导出与原文件相同；真实 PG 912 通过。 |
| 2026-10-14 | 第 5.4 节「非 haiku 模型」验证缺口（looper #58）：M6 真实模式评测用 `--model sonnet` 和 `--model opus`（写进一次性数据库的系统配置层，Claude Code 2.1.285 解析为 `claude-sonnet-5-5`、`claude-opus-5-5`）各跑一次，与 haiku 基线相比都是 5/5、契约违约 0、Planner 改文件 0，步骤、重试、送审相同，只有 opus 在聊天纠偏任务里多加了一步（7 步对 6 步）。花费 sonnet $0.62、opus $1.30（haiku $0.33）；按 sonnet 的 token 用量和 Opus 5.5 价目预估 opus 为 $1.13，实际多 15%，一半是多出的那一步（$0.09），其余来自约多 50% 的输出 token 和略多的缓存写入。两份结果作为 `baselines/real-sonnet.json`、`real-opus.json` 提交，评测按模型自动与各自的基线对比；本次真实花费共 $1.92。 |
| 2026-10-14 | 服务常驻（looper #59）：`loopx service install / uninstall / status / restart` 把 `loopx serve` 装成 macOS 的 launchd LaunchAgent 或 Linux 的 systemd 用户服务（登录自启、失败退出后拉起、日志在 `<runtime root>/logs/serve.log`），服务运行执行 install 的那个 `loopx`、工作目录为当前目录，并把 `PATH`、`LOOPX_*` 等变量复制进权限为 0600 的服务文件。重装就地更新，launchd 用 bootout、等它消失后再 bootstrap（失败重试），端口被别的进程占用时不启动，`doctor` 多一行 Service。验证：单元测试覆盖 plist 与 unit 内容、文件权限、重装和命令顺序；本机用测试 label、空闲端口和新库走完 install、status、`kill -9` 后约 1 秒被拉起、restart、端口占用时拒绝启动和 uninstall，真实 PG 934 通过。 |
| 2026-10-14 | 迁移前自动备份（looper #60）：`ensure_schema` 把所应用 `schema.sql` 的 sha256 与 DDL 在同一事务里记入 `schema_fingerprint`；库里已有 LoopX 表而指纹缺失或不同时，`serve` 先用 `pg_dump`（`LOOPX_PG_DUMP` 或 PATH 上、主版本不低于服务器，或 `LOOPX_BACKUP_CMD` 指定的命令）把库备份到 runtime root 下的 `backups/`（0600、校验后每库保留最近 5 份）再迁移，没有可用的 `pg_dump` 或备份失败时在任何 DDL 之前退出并给出安装方法（`--skip-backup` 跳过并告警），`doctor` 多一行 pg_dump，`config import --apply` 遇到待迁移的库拒绝。验证：真实 PG 测试用替身 `pg_dump` 走 `loopx serve` 的各条规则；本机没有 `pg_dump`，对套用 v2.5.0 `schema.sql` 的新库先确认拒绝且库未变，再经 `LOOPX_BACKUP_CMD` 调容器里的 `pg_dump` 备份并迁移，`pg_restore --list` 读出 64 项、备份保有迁移删掉的 `permission_mode` 与 `model` 列，再次启动不再备份；真实 PG 952 通过。 |
| 2026-10-14 | 第 5.3 节经验文档先作为草稿（looper #61）：`distill_lessons` 改为 `off`、`draft`、`write` 三档（一次性迁移：`true` 为 `write`，`false` 和 NULL 为 `off`），`draft` 时任务完成后的经验存进 `project_document_drafts`，不进任何运行的知识索引和挂载，用户在项目页 Knowledge 标签查看、修改后确认（成为受知识上限约束的文本文档）或丢弃，任务页链接到草稿。验证：真实 PG 加替身 Claude 跑完三档、确认与丢弃、被拒的确认保留草稿和迁移，一次性 `loopx serve` 上无头 Chrome 完成修改后确认、丢弃和切换档位并截图。 |
| 2026-10-14 | M9a 交付（looper #62）：新增运行时、provider（密钥另表存放、接口打码，模型目录与并发上限只存储）和 agent 的注册与校验，分层配置以 `agent` 取代模型（迁移把各层模型变成 `claude-code/<模型>`），新建任务可按角色覆盖 agent，四类 agent 都经运行时适配器启动，`llm_profiles` 删除。验证：以 main（`73eed752d`）的代码生成基准，未配置和迁移过模型层两组数据下四类角色的完整 argv、环境变量、system prompt 和首条输入与基准逐字节相同（prompt 不变，未跑真实 Claude）；校验正反例、任务级覆盖只到该任务的角色、启动时校验失败不换 agent、导出导入往返都有真实 PG 测试，替身模式评测（带 `--agent`）通过，真实 PG 1005 通过。 |
| 2026-10-14 | 第 5.3 节界面导入配置文件（looper #63）：`POST /api/config/imports` 只在这一个路由把请求体上限放宽到 256 MiB（目录上限的两倍），流式写入 runtime root 下权限 0600 的临时文件，校验格式后返回与 `loopx config import` 相同的预览，`POST .../<id>/apply` 在一个事务里应用并删除文件，`DELETE` 取消，未应用的一小时后过期删除，其余路由仍是 8 MiB。Settings 的「Move to another machine」可选择或拖入文件，按段显示新增、变化（含字段）、不变和跳过，以及待补的密钥，确认后应用，结果里的链接直接打开对应 provider、MCP 服务器或渠道的表单。验证：真实 PG 测试覆盖超过 8 MiB 的上传、其他路由 413、预览不写、应用与 CLI 结果一致、过期和未知 id 404、坏文件 400 与临时文件清理；两个一次性 `loopx serve` 之间经界面路由导出 12.4 MB、在新库的 Settings 里预览并应用 24 项，再导出除未导出的 API key 外与原文件相同；真实 PG 1020 通过。 |
| 2026-10-14 | 全面改名为 Looper（looper #64）：命令、Python 包与 wheel、数据目录、环境变量、服务 label、导出格式、界面、prompt 与文档改为 looper/Looper；`loopx` 别名打印弃用提示后执行 `looper`，`LOOPX_*` 作为回退并警告一次，`~/.loopx` 在首次 `serve` 或 `service install` 时改名为 `~/.looper` 并留下链接，`service install` 先卸掉 `com.loopx.serve`，`loopx-config/v1` 仍可导入，默认库名 `loopx`、tag 与 NOTICE 不变。验证：真实 PG 1043 通过；临时 HOME 下 `looper serve` 移动目录并留链接、`loopx version`、只设 `LOOPX_DATABASE_URL` 的 serve 与 doctor、从 `git archive` 构建的 `looper-2.5.0` wheel 装出 `looper` 和 `loopx`，沙箱里 `install.sh` 先卸旧 `loopx` 工具；真实 haiku 评测 5/5、契约违约 0、Planner 改文件 0，送审任务多出的一步单独重跑后消失，花费 $0.44。 |
| 2026-10-14 | M7c 交付（looper #66）：任务日志新增 `step_failed`、`step_retry`、`decision`、`user_instruction`、`session_switch` 和 `handoff`，由代码在事件发生时写入；Planner 每轮先整条注入用户指令（独立预算），再按类型分标题注入各类最新条目，Worker 首轮也拿到用户指令和失败经验；Planner、Manager 换 agent 时旧会话（进程还在时）先写交接摘要，新会话首轮带续接简报（Manager 另带各聊天最近的对话），项目级经验在 `distill_lessons` 为 `draft`、`write` 时到达即进草稿。验证：替身 Claude 下各类条目的触发、注入顺序与上限、强制换 agent 的交接与简报、三种旧会话不可用、Manager 续接和草稿规则都有测试，一次性 `looper serve` 跑通重试、聊天加步骤、会话丢失后续接和经验草稿并截图任务页；真实 haiku 评测 5/5、契约违约 0、Planner 改文件 0，$0.35（基线 $0.33）；真实 PG 1070 通过。M9b 尚未合入，续接规则留在 `session_switch.agent_switch` 一处，"中途换 provider"评测记为 M7c 的待办。 |
| 2026-10-14 | M9b 交付（looper #68）：Claude Code 可用 API key 接 Anthropic 官方或兼容 Anthropic 格式的接口（第三方接口用 `ANTHROPIC_AUTH_TOKEN`，opus、sonnet 和子 agent 映射到 agent 的模型，haiku 和 small/fast 映射到模型目录新增的 `background_model`），provider 可带 `env`，内置 GLM、DeepSeek、Kimi、CPA 预设和 2026-10-14 的价格表；非订阅、非官方的运行按 provider 价格计算花费并由 Looper 执行单次软上限，常驻会话只在运行时类型和 provider 都不变时续接（并入 `agent_switch`）。验证：经只记录模型名和认证头的转发代理，用真实 Claude Code 2.1.285 跑 CPA（`gpt-6.1-sol`）和 GLM BigModel（`glm-5.3[1m]`）各一个单步任务和一次 M6 真实评测，都是 5/5、契约违约 0、Planner 改文件 0，请求只带 provider 的 key、没有 Claude 模型名，44 次运行都是 `provider_price` 且与价格表手算一致（CPA 等价 $0.80，GLM $0.48）；真实 PG 1093 通过。DeepSeek、Kimi 的真实 key 验证留给维护者（第 5.4 节）。 |
| 2026-10-14 | M9c 交付（looper #67）：Codex 运行时，四类角色都能跑：Worker、Reviewer 一次 `codex exec --json`，Manager、Planner 每轮起一个进程续接会话；每次运行一个 Looper 自己的 `CODEX_HOME`（只有生成的 `config.toml` 和 `AGENTS.md`，不读 `~/.codex`），MCP 翻译进 `config.toml`，skill 降为可读文档，跳过的插件部分记进运行记录，技术验证结果见多运行时 RFC 第 6 节。验证：codex + CPA + gpt-5.5 的 M6 真实模式评测 5/5、契约违约 0、Planner 改文件 0（haiku 基线 5/5），聊天纠偏任务因运行中的 Codex Worker 不能被引导而多一次重试，token 约 52 万提示、6.7 千输出，按 gpt-5.5 价目折合约 $0.73，Manager 两轮续接同一会话，评测时 M9b 未合入、密钥经未提交的本地垫片注入；合入 M9b 后经 `looper serve` 用 `cpa` 预设和 gpt-6.1-sol 冒烟（Planner 规划和续接的决策轮、Worker 一步，按 provider 价格计 $0.045）；真实 PG 1117 通过。 |
| 2026-10-14 | M9e 交付（looper #70）：OMP 运行时，四类角色都能跑：Worker、Reviewer 一次 `omp -p --mode json`，Manager、Planner 每轮起一个进程续接会话（与 Codex 共用 `per_turn.PerTurnSession`），每个 omp 进程的 `HOME` 指向 Looper 的家目录（生成的 `models.yml` 和 `--config` 覆盖文件），不读 `~/.omp`、`~/.agents`、`~/.claude`、`~/.env`，插件原生加载、hook 和插件自带的 MCP 不加载，服务启动时回收没人用的 OMP 家目录和 Codex、OMP 的过期会话目录。验证：omp + `cpa` 预设的 OpenAI 接口 + gpt-5.5（effort medium）的 M6 真实模式评测 5/5、契约违约 0、Planner 改文件 0、重试 2（与 Codex 相同：Worker 不能被引导，聊天纠偏多一次），提示 386k（未缓存 101k）、输出 4.2k，按 gpt-5.5 价目 $0.78（Codex 约 $0.73 等价，haiku 基线 $0.33），Manager 两轮聊天续接同一会话并答出第一轮的词；真实 PG 1147 通过。 |
| 2026-10-14 | 发布 v2.6.0（looper #71，tag 打在 `591054c56`）：Release workflow 构建并发布 `looper-2.6.0` wheel；发布后在空白 HOME 里实测了全新安装和从 v2.5.0 升级（旧包卸载、数据目录搬迁、先备份再迁移）。 |
| 2026-10-14 | Manager `/new`（looper #73）：网页、飞书、Telegram（白名单照旧）发来正好是 `/new` 的消息由 Looper 自己处理、不交给 Claude：进行中的 turn 结束后停掉常驻 Claude，`manager` 会话存为空并标 `clean_start`，下一条消息从任何渠道来都开新会话，不带 `--resume` 和 M7c 的续接简报（重启后也一样），重置记为一次 Manager 运行并在原聊天确认，网页聊天加「New session」按钮。验证：替身 Claude 下覆盖干净的新会话（含重启）、近似文本照常给 Claude、进行中的 turn 先完成、两个渠道的白名单、重置记录和 Planner 会话不变，真实 PG 覆盖 `clean_start` 跨重启；一次性 `looper serve` 上真实 haiku 先记暗号、`/new` 后再问答 UNKNOWN，会话 id 改变，花费 $0.03；真实 PG 1165 通过。 |
| 2026-10-14 | Settings 拆页（looper #75）：侧栏底部可折叠的 Settings 分组，子页为 LLM Providers、Agent Runtimes（只读，按服务的 PATH 自动检测，Re-detect 也能补上启动后才装好的类型）、Agent Profiles（即 `agents` 的一行，写明能跑的角色和被谁使用，使用中的不能删）、Roles（所有角色的系统层，再是各角色的层、prompt、超时和并发）、Skills & MCP、Channels、Import / Export，凡是选 Profile 的地方都用同一个带「+ New Agent Profile」的下拉框，Agents 页改为只看运行记录的 Runs，旧地址跳到新页。删除 `POST /api/runtimes` 和 `PUT`/`DELETE /api/runtimes/<id>`，一次性迁移删掉没有 agent 在用的手动注册运行时、在用的保留为只读的 custom (legacy)，导入文件里的运行时映射到本机同名或同类型的内置运行时。验证：真实 PG 测试覆盖接口删除、迁移、Re-detect、被谁使用与删除受阻、导入映射，一次性 `looper serve` 上无头 Chrome 跑 59 项检查（各页渲染、侧栏折叠、旧地址跳转、各页编辑保存后读回、导出下载、Runs 历史）并截图；真实 PG 1169 通过。 |
| 2026-10-14 | 表名与界面统一（looper #76）：一次性迁移原地改名 `agent_configs` → `role_configs`（主键 `agent_id` → `role`，先删掉只重复主键的 `role` 列）、`agents` → `agent_profiles`、`task_agent_overrides` → `task_profile_overrides`，分层、任务选择和会话里指向 Profile 的列改为 `profile_id`，约束改用新库的名字；接口改为 `/api/roles`、`/api/agent-profiles`（字段 `profile_id`、`profile`、`profile_overrides`），不保留旧别名，代码改为 `AgentProfile`、`profile_registry`、`role_configs`，导出格式升为 `looper-config/v2`（`profiles`），v1 文件仍可导入，"agent" 只指运行中的实例。验证：从 v2.6.0 的 schema.sql 加真实数据的库迁移后每行都在、各角色启动与 main 逐字节相同、迁移后的库与新库结构相同；v2.6.0 导出的文件导入后再导出只差新名字；一次性 `looper serve` 上替身 Claude 跑完一个 Worker 步骤（任务选的 Profile 生效），无头 Chrome 跑 65 项 Settings 检查（含经界面导入 v2.6.0 的文件）；真实 PG 1172 通过。 |
| 2026-10-14 | Codex 登录（looper #78，§5.0 第 3 项）：内置 provider `codex-login`，运行目录的 `auth.json` 符号链接到操作者的登录文件、`cli_auth_credentials_store = "file"`、不写 `model_provider`、关 ChatGPT 应用，agent 的 shell 经 `shell_environment_policy.set` 拿到没有登录的 `CODEX_HOME`，每次运行后和启动时用 `lstat` 检查链接，分叉即删副本、放回链接并停用该登录直到操作者核对后清除，表单、provider、`looper doctor` 和 `/api/status` 显示登录状态。验证：真实运行里 agent 的 `$CODEX_HOME` 是空目录、`codex login status` 为 `Not logged in`；codex + `codex-login` + gpt-6.1-sol 的 M6 真实模式评测 5/5、契约违约 0、Planner 改文件 0、重试 2（与 CPA 相同），提示 647k、输出 3.9k，按价目表折合 $0.25 等价（codex + CPA + gpt-5.5 约 $0.73）；操作者登录文件的 inode、mtime、`last_refresh` 和 refresh token 指纹前后不变；真实 PG 全量通过。 |
| 2026-10-14 | 按项目限并发（looper #81）：分层配置新增 `max_concurrency`，只能设在 Planner、Worker、Reviewer 的角色层和项目里的角色层，表示每个项目同时运行多少个任务、Worker、Reviewer（默认 8、16、8），TaskRouter 按项目排队（新建、重启和启动恢复走同一检查），WorkerPool 按项目和角色计数，每次启动前现读上限，写入该字段后立即重查，调低不停止正在运行的。去掉全机的 3 个任务、4 个 Worker 上限（每个任务最多 4 个 Worker 保留），删除从未生效的 `role_configs.max_concurrency`（导入旧文件时忽略并在报告里提示一行），Roles 页和项目设置按角色显示该字段、当前值和来源。验证：替身 Claude 下覆盖按项目排队、项目之间互不阻塞、调高立即启动、调低不停止、等待中的 Worker 可被停止，真实 PG 覆盖迁移、约束和导出导入往返，一次性 `looper serve` 上用替身 Claude 实测上限生效、调高后排队的任务和 Worker 立即启动，无头 Chrome 截图；真实 PG 1223 通过。 |
| 2026-10-14 | Agent Profile 的 agent teams 开关（looper #80）：`agent_profiles.agent_teams` 只允许 Claude Code 运行时，Claude Code profile 默认开（新建不写时按运行时类型定，升级时一次性迁移打开已有的 Claude Code profile，含内置的 `claude-code/opus`，之后关掉的不会再被打开），开时 Manager、Worker、Reviewer 带 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` 和 `--teammate-mode in-process`，Planner 永远不带（它的计划和决策取自自己那一轮的结果，等队友的一轮会丢掉决策，队友之后的轮次只记录不参与决策，并行本来就交给 Looper 的 Worker），关时去掉服务继承来的或设置里写的这个变量，导出带上它、没有它的文件按运行时类型导入，Worker 照旧以最后一个 `result` 为结果，"Worker finished" 改为只记一次。验证：一次性 `looper serve` 上用 DeepSeek（`deepseek-flash[1m]`），开了的 Worker 拉起一个后台队友，stdin 关掉之后 Claude 又为队友的消息和队友结束各写了一个 `result`，报告取最后一个、写的是队友发回的词，花费 $0.0109 按 provider 价格计入队友的 token（队友用 `haiku` 时 `modelUsage` 另有 `deepseek-flash` 一项，同样计价），关了的 Planner 在服务环境带着该变量时启动不带它，常驻 Manager 把队友的消息和结束作为两轮自主轮回复；真实花费约 $0.08，真实 PG 1228 通过。 |
| 2026-10-15 | 环境变量里不再接受 agent teams 变量：设置各层和 provider 的环境变量填 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` 时返回 400，提示改用 Agent Profile 的开关（#80 之后它只由 profile 决定，这里填的值会被忽略）；已存的层照常读取，导入时从层里去掉它并留一条说明。验证：新增测试在去掉改动时 6 个失败，真实 PG 全量通过。 |
| 2026-10-15 | Chat 切走再回来看不到回复：回复原来只由页面在流结束时保存，离开页面就丢。现在由 web 通道在答复到达时存进聊天记录（id 为 `<消息 id>:reply`，页面流式气泡用同一 id），并推给重新打开的页面。验证：通道测试去掉改动时 2 个失败；替身 claude 慢回复下实测离开后回来、停留、回复未到时回来、刷新四种情况各显示一次。 |
| 2026-10-15 | Settings → Roles 改为可折叠列表（looper #84）：标题下一句话按服务端的角色写明有几个角色；“Every role” 卡片和 “Each role” 标签页换成 5 行默认收起、可同时展开的列表（Shared defaults，再是 Manager、Planner、Worker、Reviewer），收起时一行摘要写 Agent Profile 及来源、effort、并发上限、停用与未保存标记，展开后角色分「Overrides for this role」和「Role prompt and limits」两组；收起不丢未保存的修改，`#settings/roles/<role>`、`/shared` 展开并滚动到该行；界面上系统层作为取值来源时统一叫 shared defaults，接口不变。验证：一次性 `looper serve` 上无头 Chrome 跑 58 项检查（摘要、鼠标与键盘、草稿保留、两组保存后读回、Shared defaults 保存、来源文案、深链与减少动效、390 宽无横向滚动）并截图；build 通过，lint 与 main 相同。 |
| 2026-10-15 | 项目页的 Agent Profiles 标签页（looper #87）：项目的分层设置从 Settings 标签移到新标签 `#projects/<id>/profiles`，和 Roles 页一样是可折叠列表（All roles in this project，再是 Planner、Worker、Reviewer），收起时写生效的 Agent Profile 及来源（shared defaults、role default、this project、this role in this project）、effort、并发上限、overridden here 与 unsaved，展开后 Agent Profile 在前、带 Remove override，其余字段在「More overrides」；Agent Profile 的 used by 和 Overview 卡片链接到这里，行组件抽成 `CollapsibleRow` 与 `useCollapsibleRows`，接口不变；README 里过时的 `max_concurrency` 和「同时 3 个任务」一并改掉。验证：一次性 `looper serve` 上无头 Chrome 跑 52 项检查（覆盖与清除后读回、各来源、深链与旧链接、草稿、键盘、390 宽）并截图，Roles 页 58 项回归通过；build 通过，lint 与 main 相同。 |
| 2026-10-15 | Manager 建任务改为待开始、新项目由代码确认（looper #88）：任务 JSON 只建出 ready（`init`）任务，回复附代码按项目生效设置写的配置块（各角色 Profile 与 effort 及来源、agent teams、评审、单任务上限、经验沉淀、并发）并问是否开始，用户在聊天里说开始（`task_start`）或在 Tasks 页点 Start（新的 `POST /api/tasks/<id>/start`）后经 `TASK_START` 按项目并发上限排队，ready 任务重启后不恢复；任务 JSON 指向不存在的项目时什么都不建，代码先问用户，只有同一聊天紧接着的下一条消息带 `"new_project": true` 且名字和目录一致才建，Manager 推荐 prompt 改为明确问项目和目录，旧默认行启动时迁移。验证：去掉行为的 7 个变异都被测试抓到；一次性 `looper serve` 上真实 haiku 对话走完问项目和目录 → 代码确认新项目 → 配置块和开始提问 → 拒绝后提醒两种开始方式 → 无头 Chrome 点 Start → 任务完成（Manager $0.049、任务 $0.174），M6 真实模式评测 3 个任务 3/3、契约违约 0、花费与基线相差 2% 以内（$0.16）；真实 PG 1259 通过。 |
| 2026-10-15 | Dashboard 改为“此刻在发生什么”：去掉写死的 Running 卡片和三个内部组件列表，新增 `GET /api/overview`，按 `db.tasks.STATUS_GROUPS`（唯一的状态分组，未知状态计入 Other）用 SQL 分组计数全部任务，列出 Manager 忙闲、运行中的 Planner、Worker 和 Reviewer（含等待名额的），有进行中、排队或待开始任务的项目的并发与上限，以及服务器本地零点以来的花费（按 provider 计价的钱与订阅等价分开）；组件不健康时才显示警告。任务数点开即 `#tasks?group=<组>`（`GET /api/tasks?group=`），`/api/status` 的任务数同样改为不封顶的分组计数，`ACTIVE_STATUSES`（侧栏进行中计数、删除保护、启动时恢复或标记停止）和页面的 Stop 按钮也改由同一映射决定，补上了 planning、verifying、stopping。验证：真实 PG 测试覆盖分组、超过 200 个任务的计数、按项目并发和花费拆分，一次性 `looper serve` 上用替身 Claude 让 Planner、Worker、Reviewer 实际运行，页面数字与 API、SQL 逐项一致，1440 和 390 宽截图无横向滚动。 |
| 2026-10-15 | effort 只属于 Agent Profile（looper #91）：分层设置去掉 effort（层的 PUT 带 `effort` 返回 400），一次运行的 effort 就是解析出的 Profile 的 effort；层表单去掉 Effort，摘要和 Profile 下拉框显示 Profile 的 effort，Manager 配置块写成「Profile（effort X，来源：…）」。一次性迁移 `layer_effort_on_profiles`（`looper/agents/layer_effort.py`，`ensure_schema` 在 `schema.sql` 之后同一事务里跑）按旧规则算出每个角色在系统层、各角色、有层的项目及项目里的角色、任务的 Profile 覆盖处的 Profile 与 effort，再让各层选能复现它的 Profile，需要时新建 `<Profile> · <effort>`，导入带 effort 的旧文件走同一套转换并逐条附注；验证：迁移的手工层集合与升级 parity 测试新旧逐角色一致，真实 `looper serve` 从 main 切到本分支后同一数据库上 8 个角色/位置的有效 Profile 与 effort 不变。 |
| 2026-10-15 | 项目说明对所有运行时可见（looper #97，§5.0 第 7 项）：先在 Looper 的隔离下实测，Claude Code 2.1.285 在项目没有 `CLAUDE.md` 时本来就读 `AGENTS.md`（仓库以上的 `AGENTS.md` 也会漏进来），Codex 0.162.0 只读 `AGENTS.md`，OMP 18.4.3 在仓库外两个都不读。改为 Codex 的 `config.toml` 写 `project_doc_fallback_filenames = ["CLAUDE.md"]`，OMP 在仓库外把工作目录的 `AGENTS.md`（没有时 `CLAUDE.md`，最多 32 KiB）放在 system prompt 最前面，Claude Code 的 `claudeMdExcludes` 一并排除仓库以上的 `AGENTS.md`（隔离标识 v2），每次运行记 `project_instructions`，Runs 页显示。验证：一次性 `looper serve` 上三种运行时都答出原先看不到的探针词（Claude Code 不再答出仓库以上的词），Codex 任务的 Worker 也答出；真实 haiku 评测 5/5、契约违约 1（修复轮修好）、$0.33；真实 PG 1290 通过。 |
| 2026-10-15 | 杀掉 agent 时清理整个进程树（looper #96）：agent 进程各自在新会话里启动，停止、取消、超时、单次上限和服务关闭都先用一次 `ps` 快照取出整棵进程树（含 Codex、Claude Code 让每条命令自成的会话），SIGTERM 后至多等 3 秒再 SIGKILL；每个 agent 进程连同启动时间记在 runtime root 下，服务崩溃后下次启动先结束仍在运行的已记录 agent 的整棵树。真实 Codex（ChatGPT 登录）和 Claude Code（haiku）Worker 跑长命令时停止任务、关闭服务、SIGKILL 服务后重启，标记进程都从 1–2 个变成 0，花费约 $0.08。已退出 agent 的残留和 Claude Code 写进操作者家目录的 shell 快照记为 §5.2 待办。 |
| 2026-10-15 | 定时任务 S1（looper #98，§5.0 第 5 项）：项目的定时任务按 5 段 cron 和 IANA 时区的墙钟时间触发（夏令时跳过的时刻顺延到之后第一个有效时刻、重复的只触发一次，两次触发至少隔 5 分钟），由 `looper serve` 内的调度器每 15 秒在锁住该行的事务里处理，每次触发建一个完整任务并经 `TASK_START` 直接开始，需求后附代码生成的「定时运行上下文」（规则、应触发时刻、漏跑次数与起点、最近 3 次运行、无人值守约定）；漏跑只补一次（或按策略跳过）、重叠默认跳过可改排队（最多一个）、超时记 `timed_out` 并停任务，任务结束时回写运行的终态、摘要、花费与连续失败计数，项目页新增 Schedules 标签页、任务列表带 Scheduled 标记和 `#tasks?schedule=` 筛选，有定时任务的项目不能删除。验证：真实 PG 测试覆盖两个调度器并发只触发一次、两种重叠策略、回写与计数、超时、补跑、校验与删除保护、启用不回补；一次性 `looper serve` 上用替身 Claude 实测每分钟按时触发、停服约 3 分钟后只补跑一次（missed=3）、重叠跳过、立即运行和 100 秒超时停任务，haiku 真实触发一次，Planner 用上下文里的应触发时刻完成任务（$0.047）；真实 PG 1394 通过。 |
| 2026-10-15 | 定时任务 S2（looper #101，§5.0 第 6 项）：前置检查在工作目录后台执行（`gating`，经 `proc_tree` 启动，超时或停止时结束整个进程树），没输出记 `skipped_gate`、失败或超时记 `gate_failed`、有输出则作为这一轮的输入写进上下文；上一次跑了东西却没完成的运行把它的前置检查输出标为「上一轮未完成」交给下一轮（只一轮），每日花费按服务器本地零点起触发的运行计、到上限记 `skipped_budget`，`notify`（failures / always / never）经渠道发给发送者白名单里的聊天，连续 3 次失败和「3 个周期与 24 小时取短仍没有成功」两条告警各发一次并在恢复时再发一次，每条消息记入 `schedule_notifications`，总览页新增定时任务栏。验证：真实 PG 测试覆盖前置检查各结果、未完成输入只交一次、跨零点的每日上限、通知策略与告警各一次、无目标只在总览页；一次性 `looper serve` 上用替身 Claude 和只在白名单里的替身 Telegram 用户（渠道未启用、没有外发）实测四种前置检查、预算跳过、「上一轮未完成」和失败告警只记一条，haiku 真实触发两次 Planner 只处理前置检查给出的条目（共 $0.142）；真实 PG 1460 通过。 |
| 2026-10-15 | 飞书渠道改用 lark-cli 1.0.97 现有的命令（looper #102，§5.1）：回复和定时任务通知都走 `lark-cli im +messages-send --as bot`（`--chat-id` / `--user-id`，失败时带上 lark-cli 的 JSON 错误原因），接收改为 `lark-cli event consume im.message.receive_v1 --as bot` 并按扁平事件解析（忽略机器人自己的消息），消费进程退出后 10 秒再起、不再紧循环。验证：替身 lark-cli 只认 1.0.97 的两条命令，覆盖 argv、回复送达、失败原因、事件到 Manager 和重启间隔；对模块实际构造的三条命令用占位 id 跑 `--dry-run` 均通过（未向任何真实会话或用户发送）；真实 PG 1470 通过。真实流量仍未验证（§5.4）。 |
| 2026-10-15 | 飞书（lark-cli）渠道可作管家渠道（looper #105）：配置改为可选的 lark-cli `profile`（每次调用带 `--profile`，为空用当前 profile），启动时删去旧的 `app_id_env`、`app_secret_env`；启动前用 `whoami` 和机器人信息核对，不行就不启动并在 Settings → Channels 写明原因，否则显示「Bot: 名字（app_id，profile）」；只处理私聊，群消息忽略；回复改为 Markdown 富文本；白名单记下所属应用，换到别的应用不启动；Settings 可按姓名或邮箱搜人添加、名单旁显示姓名、列出最近被拒的发送者并一键允许。验证：替身 lark-cli 覆盖 `--profile`、启动拒绝（未登录、名单为空、应用不符）、私聊与群、被拒列表和搜索解析，所有测试默认拿到一个拒绝一切调用的 lark-cli；本机只读核对 whoami、机器人信息和按维护者姓名搜索，回复命令（Markdown）只跑 `--dry-run`，未向任何真实会话发送；真实 PG 1498 通过。 |
| 2026-10-15 | 规划者用起并行（looper #104，§5.0 第 9 项）：推荐的规划者提示词要求只为用到结果的步骤加依赖、独立部分并行、协作的部分先定接口再并行最后联调、并行步骤各改各的文件，计划契约和 Worker 契约（提交只 `git add` 自己的路径）同步修改，还停在旧默认提示词的 `planner` 行启动时换成新的。评测新增 `parallel_parts`（至少一对步骤没有依赖关系，且两个 Worker 的运行在时间上重叠）、`interface_first`（服务和客户端要先定接口：两个独立步骤共同依赖一步、又被同一步依赖）和“parallel Workers”一列，haiku 基线随之刷新。验证：真实 haiku 下 `parallel_parts` 新旧提示词都并行（各 1 次），`interface_first` 新旧提示词都排成 3 步直线、用例失败（各 1 次，$0.16），即对 haiku 新提示词没有改变结果；其余用例无退化、契约违约 0，真实 PG 1475 通过。 |
| 2026-10-15 | Manager 只回答刚收到的消息（looper #112）：网页聊天、飞书和 Telegram 共用 Manager 的一个会话，每条消息前加一行代码写的 `Channel:`，说明它来自网页聊天、飞书私聊还是 Telegram 私聊（chat id 为负的标成群聊），推荐 prompt 加两条规则：只回答这条消息，不接着聊之前的话题、不主动检查或汇报进度，问候和简短寒暄简短回复，旧默认行启动时迁移。验证：进程内 serve 上真实 haiku，网页聊天先问 notes.txt 的发布清单（读了文件），再以飞书私聊发来的 hi 得到一句问候、无工具调用，再问清单进度时照常回答，三轮 $0.027；同一组对话在 main 上的 hi 回复会把发布清单拉回来，改动后不会，真实运行共 $0.13；真实 PG 1509 通过。 |
| 2026-10-15 | 飞书和 Telegram 每条消息只用一条状态消息（looper #111）：维护者实测飞书时，每个阶段（思考、各个工具）都单独发一条，回答前刷了 8 条小消息；现在第一个阶段发一条「⏳ 处理中：思考中 · 0s」，之后原地编辑（文本变了才改、至少隔 3 秒、补一次尾随编辑、最多 18 次，飞书一条消息最多可编辑 20 次），回答作为新消息发出后再把状态改成「✅ 完成 · 用时 Ns · 工具 N 次」或「❌ 出错：原因」；编辑失败只记一次日志、不再多发消息，管家自己发起的回复不带状态消息；管家的状态带上 `activity`/`tool`，出错的回复带 `error`。同一 PR 里，交给管家的私聊消息先收到机器人的反应（飞书「Get」、Telegram 👀），在后台发出、不耽误消息交给管家；反应失败不重试、不变成聊天消息，每次运行只记一次日志，lark-cli 报了缺哪个权限就写在日志里，被拒的发送者和群消息没有反应。验证：状态行的单元测试覆盖 10 个阶段只发 1 条、节流和尾随编辑、编辑上限、编辑失败不多发、回答后的迟到阶段被忽略和上限；飞书经替身 lark-cli、Telegram 经替身 bot 端到端，编辑和反应命令只跑 `--dry-run`（占位消息 id），未向任何真实会话发送；反应经替身覆盖收到即反应且不等它、被拒和群消息不反应、失败只记一次日志并写出缺失权限；真实 PG 1539 通过。 |
| 2026-10-15 | 飞书和 Telegram 恢复发送任务进度（looper #115）：#111 之后两个渠道把所有不是阶段的 `STATUS_UPDATE` 都丢了，其中包括管家的任务进度（任务被接受时的「任务已分解为 N 个步骤，开始执行...」、Planner 的进度和最终总结），在飞书或 Telegram 里发起的任务从开始到完成都不再告诉那个会话；这不是有意的取舍。现在 `status_line.status_kind` 统一判定：阶段进状态消息，既不是阶段也不是流式文本且有文字的是任务进度，像回答一样单独发一条消息，不动状态消息、不算它的编辑次数，其余（流式文本、空文本）不发；维护者确认每条任务进度都单独发回飞书和 Telegram。替身 lark-cli 的 `+messages-edit` 参数检查错了一位，一直拒绝编辑，顺带修好。验证：经替身 lark-cli 和替身 bot，用真实的管家处理函数产生进度，进度各自成一条消息、阶段只编辑那一条状态消息、流式文本和空文本不发；退回 #111 的渠道代码后这两个测试失败；真实 PG 1549 通过。 |
| 2026-10-15 | 任务进度与总结（looper #114，§5.0 第 10 项）：回复契约给 DECISION、SUPERVISE 加可选但严格的 `progress`（五个字段，契约要求每次都带），代码存为任务日志新类型 `progress`（CHECK 约束一次性扩展），各 agent 的日志段落不显示，`GET /api/tasks/<id>` 按新到旧给出；最后一个未完成步骤的决策会被告知，完成任务时必须带 `final`（四个字段，缺了走修复回合），代码按字段小标题写成 `result_summary`；失败的 `error` 和停止的 `result_summary` 改为代码写的收尾说明，任务页加 Progress 栏。验证：真实 haiku 评测 6/7（`interface_first` 与基线同样失败）、契约违约 0、无修复回合、每个决策都带进度说明、每个任务都以完整总结结束、$0.66（基线 $0.61）；替身 claude 的 serve 上完成和停止两个任务在 1440、390 宽度截图核对；真实 PG 1549 通过。 |
| 2026-10-15 | 各渠道对话入库（looper #106，@michaelx1993 的 4 个提交加上维护者的修复）：Telegram、飞书的提问、回复和自主回复以 `<channel>:<chat_id>` 为会话存进 `chat_messages`（新增 `channel` 列），只存私聊（Telegram chat id 为负的群和频道一律不存，和 Channel 行共用 `_private_chat`），新会话的续接简报因此带上 Telegram、飞书的对话并在各渠道都排除本轮消息，followup 按聊天各自保留、说明里写明「in this chat」、最多 50 个。验证：真实 haiku 上先在网页聊发布清单、在 Telegram 私聊记下周五的评审会，删掉保存的会话后从 Telegram 问「我周五有什么安排？」，简报里有两个聊天且不含本条，回答正确；main 的简报只有网页聊天，回答「没有访问日历的权限」，共 $0.06；真实 PG 1567 通过。 |
| 2026-10-15 | 各渠道的对话并入 Chat 页（looper #109，@michaelx1993 的 4 个提交加上维护者在其分支上的修复）：选中 Manager 时左侧按渠道（Dashboard chat、Lark、Telegram）列出它的对话（新接口 `GET /api/chat/sessions`，不含 Planner 线程，读失败返回 500），每条显示最后一条消息的开头和时间，选网页对话接着聊，选 Lark、Telegram 的对话只读查看；不另设 Conversations 页面。`GET /api/chat/messages` 改为返回最新的 200 条（原来是最早的 200 条，修复 #113）。验证：新增测试覆盖 250 条只返回最新 200 条且升序、排除 Planner 线程、读失败 500；一次性 `looper serve` 上种入网页、Lark、Telegram 和 Planner 线程后在 1440、390 宽度截图核对；合入 #106 后真实 PG 1572 通过。 |
| 2026-10-15 | 任务结束时告诉发起的会话（looper #108，外部贡献 @michaelx1993，维护者在其分支上补修）：在 Telegram 或飞书发起的任务完成或失败时，TaskRouter 给发起的会话发一条通知（「✅ 任务完成 · 名称」或「❌ 任务失败 · 名称」、总结前 300 字、`/#tasks/<id>`）。`task_notice` 是唯一的类型化定义：`NotifyChannel` 是实现了 `notify` 的渠道（Telegram、飞书）；任务记录的 `tasks.chat_id` 取 notify 要的形式（Telegram 的 chat id、飞书发送者的 open_id，原先飞书存的是会话 id `oc_…`，发不到人）；终态只有 completed 和 failed（`db.tasks.PLANNER_END_STATUSES`），stopped、cancelled 不算。Manager 不再把终态的 GOAL_STATUS 转发给这两个渠道，结束只说一次，网页聊天照旧收到，进度照旧发给所有渠道；通知在回收 Planner 和 recheck 之后后台发送，路由循环不再等渠道，router 停止时取消未发完的通知。验证：飞书和 Telegram 各一条端到端测试（渠道消息 → Manager 建任务并启动 → TaskRouter → Planner 和 Worker 完成 → 通知），飞书替身 lark-cli 的 argv 是 `--user-id ou_…`，每个会话只收到一条完成消息；退回旧的 Manager 代码后飞书记录成 `oc_dm`、Telegram 收到两条；慢渠道不阻塞 teardown、recheck 和另一个任务的 TASK_START（改回同步发送后该测试超时）；通知命令只跑 `--dry-run`（占位 open_id），未向真实会话发送；真实 PG 1605 通过。 |
| 2026-10-15 | 定时任务启用时不再立刻报「长时间没有成功」（looper #118）：启用告警影子定时任务时发现，`check_stale` 只从上次成功算起、不管停用了多久，上次成功在几小时前的定时任务一启用就报 `stale_alert`，第一次运行（哪怕是 `skipped_gate`）后又报 `stale_recovered`；当时两个都是 `notify never` 只进面板，换成失败或每次通知就会打扰人。现在 `schedules.enabled_at` 记录创建或最近一次启用的时间（API 在 enabled 由假变真时写，新行默认 now()，老数据一次性迁移取 `updated_at`），`stale_deadline` 从上次成功和 `enabled_at` 中较晚者之后的第一次触发算起；还没运行过的不告警，停用的从不检查。验证：单测覆盖启用晚于上次成功和从未成功两种；PG 上经 API 和调度器验证旧的成功加重新启用不立刻告警、到期才告警，很早启用且没成功过照旧告警，停用期间不告警、重新启用重新计时，迁移回填和幂等；退回旧的 `stale_deadline` 后第一、三个 PG 测试失败；真实 PG 1611 通过。 |
| 2026-10-15 | Manager 的回复只取这一轮的最终回答（looper #119）：原来把一轮里所有顶层文字直接拼接，调用工具前的过程叙述（「正在核对…马上汇报。」）会粘在回答前面一起发出、入库和记录；现在统一由 `claude_cli.answer_text` 取 result 里的 `result`（最后一条顶层助手消息），没有时才退回拼接全部文字，自主回复和交接也用它，过程叙述照旧流式显示。验证：改回旧顺序时新测试按原 bug 失败；真实 haiku 一轮先说一句再读两个文件，main 存下的回复以那句开头，改动后只存最终回答，共 $0.05；真实 PG 1615 通过。 |
| 2026-10-15 | Manager 读出后面跟着文字的操作 JSON（looper #120）：真实飞书里 opus 回「开始」时先输出 `task_start` JSON 再接一段「已经启动」，三次 `json.loads` 都因多余内容失败，任务没启动、用户却看到已启动和原始 JSON；现在 `action_object` 用 `raw_decode` 在第一个 ```json 代码块或第一个 `{` 处解出操作、丢掉后面的文字，用户只看到代码写的回复，读不出却点名了操作的回复一律不发，改回「没有执行…」并记警告。验证：改回旧解析时新测试按原 bug 失败；真实 PG 1622 通过（第一次全量里一个排期测试偶发失败，单跑 3 次、重跑全量都通过）。 |
| 2026-10-15 | 任务结束通知的四处打磨（looper #122）：维护者在飞书实测 #108、#111、#115，整条流程是通的，但聊天里能看到四个问题。(1) 通知用 `--text` 发、总结压成一行，Markdown 原样露出；现在像回答一样发（飞书 `--markdown`，Telegram MarkdownV2 失败时退回纯文本），只放总结或收尾说明的第一节（完成时是 Delivered，失败时是 Why it ended），保留换行，最多 300 字，全文留在任务页。(2) 链接是相对的 `/#tasks/<id>`，点不开；新增 `LOOPER_PUBLIC_URL`（`links.py`），未设置时用服务自己的 `http://<host>:<port>`，格式不对时 serve 不启动；任务通知和定时任务的消息都用绝对链接，飞书上写成 Markdown 链接。(3) 「进度更新: 1/1 完成」排在「✅ 任务完成」之后，因为通知直接发给渠道，进度却要经过 Manager 和渠道队列；现在 TaskRouter 把 `Notice` 附在交给 Manager 的终态上，Manager 代替终态总结发 `CHAT_NOTICE`，和进度走同一条路径，渠道逐条发送，所以通知一定是最后一条；任务结束后再来的状态会被丢弃；没有通知的任务（网页聊天、没记录 chat 的）照旧由 Manager 转发终态，#108 的后续项也一并解决。(4) 通知标题用的是 Manager 写的英文需求；现在存 `tasks.original_user_text`（发起任务的那条消息；Looper 先问了项目或目录时，仍是最初那条），没有时才用需求。验证：替身 lark-cli 和替身 bot 端到端，每条进度消息都延迟 0.5 秒发送，通知仍是最后一条，飞书 argv 为 `--user-id ou_… --markdown` 加绝对 Markdown 链接；计时探针显示 TaskRouter 在约 35 ms 收到 completed，进度在 180 ms 到 1.26 s 之间发出，旧设计下通知会排在前面；通知命令只跑 `--dry-run`（占位 open_id），未向真实会话发送；真实 PG 1645 通过。 |
| 2026-10-15 | 管家平台工具 RFC v1（looper #123）：并入维护者当天的决定——任务随时可以编辑调整，规划者每次都知道并决定要不要重新编排：补充说明（管家转达、聊天页直接发给规划者，存为 `supplement`）和需求修改（存版本）每一条都以一轮回合送达规划者、不会丢，规划者的 CHAT 新增 `replan`（运行中的步骤默认保留，可点名停掉）；规划者用只认本任务的 MCP 工具、从项目允许的列表（默认为空）里为执行者、评审者选 Agent Profile，换成更贵的也不需要确认。任务变更和 `replan` 进 T1，可选列表和规划者的 Profile 工具进 T2、排在步骤目录隔离 I1 之后。 |
| 2026-10-15 | 任务用任务 JSON 里的标题命名（looper #124）：#122 用 `tasks.original_user_text` 给结束通知起名，但常见流程里 Claude 先复述并请用户「确认」，带着 `task_request` 的是那条「确认」，实测存成了「确认\n开始」；`asked_for` 只覆盖了 Looper 自己问项目或目录的少数情况。现在推荐的 Manager prompt 要求 `task_request` 带 `title`（几个词，用用户的语言），存进 `tasks.title`（一行，最多 60 字）；`db.tasks.task_name` 是唯一的回退：标题、目标 id、需求第一行、任务 id。结束通知、任务列表和详情、总览里的 Planner、Chat 页和建任务后的配置块都用它；仍是旧默认 prompt 的 Manager 行启动时升级；`asked_for` 删除，`original_user_text` 作为历史保留（删列会丢已有数据）。验证：单测覆盖确认流程得到标题、缺标题和非字符串回退；PG 上覆盖 API 的 name、prompt 迁移；launch parity 夹具按文本更新；真实 haiku（一次性 serve，每次运行 $0.05 上限）走确认流程得到标题「创建 hello.py 打印问候语」，`original_user_text` 为「确认」，两次 Manager 运行共 $0.021；无头 Chrome 1280 px 截图确认 Tasks 页显示标题；真实 PG 1650 通过。 |
| 2026-10-15 | 定时运行的任务也有标题（looper #125）：#124 之后任务按标题、目标 id、需求第一行命名，定时运行的任务两者都没有，告警定时任务在 Tasks 列表里显示成一长句 SOP；现在调度器建任务时用「定时任务名 · 时区内的触发时间」做标题（如 `alarm-triage-critical · 10-15 12:30`），名称按 `task_title` 的 60 字上限截断、保留时间。验证：单测覆盖时区、跨日、空白折叠和长名截断，PG 上到期触发的任务标题与 API 的 name 一致；真实 PG 1653 通过。 |
| 2026-10-15 | 任务变更：补充说明和需求版本（looper #126，管家平台工具 T1 的前一半）：发给规划者的聊天消息在发送时就存为 `supplement`（取代 `user_instruction`，原有记录迁移为已送达）；`PUT /api/tasks/:id/requirement` 修改未结束任务的需求，每个版本都留在 `task_requirement_versions` 里；任务页可以编辑需求，并显示历史版本和补充说明。变更先存为未送达，规划者每处理完一个事件，就把等待中的变更全部放进一轮 CHAT 给出，回复应用之后才标记送达，所以进行中的一轮里做的变更、失败的一轮、重启都不会让变更丢失；CHAT 新增 `replan`（替换未开始的步骤，可以用 `stop` 停掉点名的运行中步骤），执行者和评审者的任务段在需求后面列出补充说明。真实 haiku 评测里，补充说明和需求修改都让规划者改了计划（原地改写还没开始的第二步），不需要改计划的补充说明则保持计划不变，三个任务都通过。 |
| 2026-10-15 | 并行步骤的工作目录隔离 I1（looper #127，§5.0 第 11 项）：项目设置 `step_workspaces` 为 `worktree` 时，在 git 检出的分支上，和别的步骤同时跑的步骤各在自己的 worktree 和分支上做，派出前把任务目录的未提交改动提交为检查点；被接受时提交 Worker 留下的改动、在内存里算合并、生成合并提交后快进分支，合不进去时任务目录不变、按五类 `merge_error` 交给规划者，重试从分支的最新状态新建 worktree；Looper 的提交用固定身份和尾注、不签名、不跑钩子，完成的任务清理 worktree，停止或失败的保留没合回的，直到重新规划或删除。验证：真实 git 仓库上覆盖每类合并错误都不改 HEAD、索引和工作区，不跑钩子，中断后只合并一次，撤销命令可用；替身 Claude 的一次性 serve 上并行合回与冲突重试都通过；真实 haiku 下 `parallel_parts` 通过（两个 Worker 并行、两个合并提交，$0.14），`interface_first` 与基线同样因计划排成直线失败；真实 PG 1729 通过。 |
| 2026-10-15 | 管家平台工具 T1 的后一半（looper #129）：管家不再在回复里写 JSON 动作，改为通过 MCP 工具操作 Looper——操作目录 `looper/agents/ops`（7 个 T1 工具，参数用 JSON Schema 校验，分 read/write/high 三档），`looper serve` 启动时生成令牌，只有带令牌的 `/api/agent/v1/` 接口能调用；`looper mcp-server` 负责转发，三种运行时的管家都会注入它；调用挂在当前这一轮上，自主轮只能读；新建项目、取消任务要等同一聊天里用户的下一条消息确认（10 分钟内）；每次调用都写进 `manager_actions`，任务页显示「由管家操作」。管家对话评测：假模式 6/6，真实 haiku 5/5（$0.24）；线上管家用的 opus xhigh 跑完整的「确认→创建→开始」流程也通过（$0.12），没有任何回复露出原始 JSON；工具不可用（MCP 服务起不来或令牌被拒）时，管家直接回复「平台工具不可用，没有执行任何操作」并记录错误日志；Codex、OMP 的管家经本机 CPA（gpt-5.6-luna）各跑一轮只读对话，都调到了工具并答对。顺带修复 #126 的问题：规划者处在 planning、verifying、stopping 状态时，任务的需求改不了。 |
| 2026-10-15 | 定时任务总览页（looper #130）：左侧栏 Runs 下新增 Schedules（`#schedules`），按项目列出所有项目的定时任务（新接口 `GET /api/schedules`，行与项目列表同一形状并带 `project_name`），停用的变暗显示，每行有启用开关（保存中显示新值，失败退回并写明原因）和 Run now，名称链接到项目 Schedules 标签页里的那一行去编辑，不另做编辑器。验证：真实 PG 测试覆盖排序、停用的也列出、与项目列表同形；一次性 `looper serve` 上种入 2 个项目 3 个定时任务，无头浏览器开关后经 API 读回、被拒时退回、390 宽度无横向滚动并截图；合入 main 后真实 PG 1836 通过。 |
| 2026-10-15 | 定时任务详情页（looper #131）：Schedules 页的名称打开 `#schedules/<id>`，显示设置（规则、时区、前置检查、通知、预算、超时、Agent Profile）、告警、今日花费、开关、Run now 和到项目标签页编辑的链接，以及它触发的运行历史；运行历史与项目标签页共用，每页 20 条可加载更早的，默认隐藏没有新内容、上一轮未结束两类常规跳过（`GET /api/schedules/:id/runs` 新增 `before` 分页、`skips=hide` 和 `routine_skips` 计数，默认仍 `show`），每条带任务标题、状态和耗时。验证：真实 PG 测试覆盖分页、过滤与计数、错误参数；一次性 `looper serve` 上种入 46 条跳过和真实运行，无头浏览器核对过滤、翻页、开关读回、390 宽度无横向滚动并截图；真实 PG 1837 通过。 |
| 2026-10-15 | 有前置检查的定时任务可以每分钟触发（looper #132）：告警分析原来在新告警一分钟内开始，5 分钟的最小间隔让它最多晚 5 分钟；有前置检查的定时任务没有新内容时停在前置检查（`skipped_gate`，不建任务、不花钱），现在最小间隔为 1 分钟，没有前置检查的仍为 5 分钟。`schedules.min_interval_seconds` 是唯一的规则，创建、修改和预览（`gated`）都按它检查；去掉每分钟触发的定时任务的前置检查会被拒绝，并说明原因。每分钟一行跳过记录会让每个定时任务每天多约 1440 行，还会淹没运行记录和下一轮的上下文，所以重复的跳过（同一原因、同一天）记在最近那一次运行上（`schedule_runs.firings`、`finished_at` 为最近一次触发，`count_skip`），前置检查没有输出时不另留一行；每次计数的 `skipped_gate` 仍更新 `last_ok_at`、结束「长时间没有成功」告警；总览的跳过数按次数相加；前置检查运行中再到的触发仍是 `skipped_overlap`，不会并发。界面在规则提示、预览和运行记录（「×N，直到…」）以及 Schedules 页上体现。验证：PG 上覆盖校验、每分钟触发与计数、跨日、慢前置检查不重叠；一次性 serve 上每分钟的定时任务跑了 5.5 分钟，每次都在整分后约 6 秒触发，运行记录为 `skipped_gate`×2、`running`、`skipped_overlap`×2；#131 的 `routine_skips` 也按次数相加；无头 Chrome 截图；真实 PG 1841 通过。 |
| 2026-10-15 | 每分钟触发的定时任务不再误报「长时间没有成功」（looper #134）：#118 和 #132 合在一起后，维护者在飞书收到两次误报：规则从 `*/5` 改成 `* * * * *` 的那一刻（新规则 3 个周期只有 3 分钟，从几小时前的上次成功算早已过期），以及任务正常运行 3 分钟后（运行期间每分钟都是 `skipped_overlap`，不算成功，而一次运行要 8–25 分钟）。现在有运行正在进行（排队、前置检查中、任务在运行超时内运行）时不检查，它造成的跳过也不算；窗口取 3 个周期，但至少 `STALE_FLOOR` 15 分钟（没有前置检查时最短规则的 3 次触发）、至多 24 小时；改规则或时区写入新列 `rule_changed_at`，和上次成功、`enabled_at` 一起取最晚的那个起算。验证：两种情形各一条回归测试，在 main 上都失败、修复后通过；原有的「长时间没有成功」测试不改动照常通过；真实 PG 1846 通过。 |
| 2026-10-15 | Schedules 页筛选（looper #135）：列表顶部加项目（全部或任一项目，只有一个项目时也显示）和状态（全部、已启用、已停用）两个筛选，两者叠加，记在地址里（`#schedules?project=<id>&enabled=true|false`，链接、刷新和后退都保留，未知值按全部处理），在页面里筛选；每个项目的「N of M on」仍按该项目全部定时任务计，筛空时提示并可一键清除。验证：一次性 `looper serve` 上 2 个项目 3 个定时任务，无头浏览器逐一核对 9 种组合、直链与刷新、后退、未知值、单项目，390 宽度无横向滚动并截图；bare pytest 1494 通过。 |
| 2026-10-15 | 规划者不再限制步骤数（looper #137）：原来计划或重新规划超过 8 步（`MAX_STEPS`，跑过又被丢弃的步骤也算）就算违约，告警定时任务每轮最多 12 个事件、按 SOP 每个事件一步再加一步收尾共 13 步，结果要么触发修复回合、要么被迫合并事件、要么任务失败；维护者选择直接去掉上限，由任务花费上限和超时兜底，其他校验（至少 1 步、id 唯一、依赖无环、不复用已开始或已丢弃的 id）不变，回复约定改成「at least 1 step」。新增评测任务 `many_steps`（9 步）；真实 haiku 下 9 步计划一次通过、没有违约，写完 7 个文件时达到为这次验证设的 $0.20 上限被取消（整次约 $0.25）。 |
| 2026-10-15 | 可重入的定时任务（looper #136）：告警定时任务每分钟检查一次，而一次运行要 8–25 分钟，`overlap` `skip` 时运行期间新来的告警只能等。`overlap` 新增 `parallel`：在进行中的运行旁边再起一个，最多 `max_parallel_runs`（1–10，默认 3）个同时进行，超过记 `skipped_overlap`；前置检查任何时候都不并发，只有任务阶段重叠。前置检查的环境里多了 `LOOPER_SCHEDULE_ID`、`LOOPER_SCHEDULE_RUN_ID`、`LOOPER_SCHEDULE_SCHEDULED_FOR`、`LOOPER_SCHEDULE_RUN_TIMEOUT_SECONDS`，告警那边可以按运行认领条目、按超时释放（它的 `--claims` 已就绪）。每次失败的输入都交给下一次运行、只交一次（`carried_on`；几次并行运行在下一次之前都失败时一并交出，按结束先后各占一块，放不下时略去最早的并说明数量）；每日花费上限按所有运行合计；`max_parallel_runs` 超过项目 Planner 上限时，保存返回 `warnings`，表单旁也有提示（排队的运行，运行超时照样计时）；运行中都算健康（#134）。界面在重叠选项里加了「Parallel (reentrant)」和「Most runs at once」，详情页只读显示。验证：PG 上覆盖上限、前置检查不并发及其环境变量、多个运行合计花费、输入只交一次、校验和提醒；一次性 serve 上每分钟、最多 3 个、替身任务约 3 分钟，看到 3 个同时运行、第 4 分钟 `skipped_overlap`、失败后下一分钟接着跑并带上未完成的输入；两次并行运行都失败后下一次运行两份输入都带上且只带一次；真实 PG 1854 通过。 |

## 5. 不属于本 RFC 的待办

这些问题在开发过程中发现，独立排期，不阻塞 M1–M6。

### 5.0 当前队列（按顺序执行，2026-10-14 定，2026-10-15 调整）

1. **Settings 拆页**（✅ 已交付 looper #75）：侧栏底部 SETTINGS 分组，子页依次为 LLM Providers、Agent Runtimes（只读、自动检测，删除手动注册运行时的界面和 API）、Agent Profiles（可复用的运行时 + provider + 模型 + 默认 effort；provider 可选 API key，也可选运行时自己的 OAuth 登录）、Roles（各角色选 Profile，加 harness、角色 prompt、上限、超时、并发）、Skills & MCP、Channels、Import / Export；顶部 Agents 页改名 Runs，只看运行记录。
2. **表名与界面统一**（✅ 已交付 looper #76）：单独一个机械改名 PR。`agents` → `agent_profiles`，`agent_configs` → `role_configs`（列 `agent_id` → `role`），`/api/agents` → `/api/agent-profiles`，`/api/agent-configs` → `/api/roles`，分层配置 `agent` → `profile_id`，`task_agent_overrides` → `task_profile_overrides`，代码类名与模块对应改名；"agent" 只指运行中的实例。不保留旧 API 别名，旧导出文件仍可导入；迁移前自动备份。
3. **Codex 与 OMP 的 OAuth 登录**（✅ 已交付 looper #78）：Agent Profile 里可以选内置 provider `codex-login`（Codex 自己的 ChatGPT 登录）。各隔离目录的 `auth.json` 是指向操作者登录文件的符号链接，不复制、不加锁（锁做不到，刷新在 Codex 进程内）；agent 的 shell 拿到没有登录的 `CODEX_HOME`；每次运行后检查链接，分叉即止损。做法和失效方式见多运行时 RFC 的 P1。OMP 没有自己的登录，不做。
4. **思考强度只放在 Agent Profile 里**（✅ 已交付 looper #91）：设置各层不再有 effort，强度只随 Profile；迁移把各层已设的强度换成派生 Profile（如 `claude-code/opus · xhigh`），迁移前后每个角色、每个位置实际生效的 Profile 和强度不变。
5. **定时任务 S1**（✅ 已交付 looper #98，设计见[定时任务 RFC](../architecture/rfcs/scheduled-tasks-v0.zh-CN.md)）：数据模型、时间规则解析、进程内调度器（漏跑只补一次、重叠默认跳过、运行超时）、每次触发建一个完整任务并直接开始、跨轮次上下文、运行历史、项目页「定时任务」标签页、任务的「定时」标记、HTTP 接口。
6. **定时任务 S2**（✅ 已交付 looper #101）：前置检查、失败那一轮的输入交给下一轮、每日花费上限、通知（失败时 / 每次 / 不发）与两条告警（连续失败、长时间没有成功）、总览页的定时任务栏。
7. **项目说明对所有运行时可见**（✅ 已交付 looper #97）：仓库的 `CLAUDE.md`、`AGENTS.md` 每种运行时都看得到，只有其中一个时也一样，两者都在时各读各的原生文件，同一份内容不给两次，不改用户仓库。核实后的读法和做法见多运行时 RFC 第 6、9 节：Claude Code 本来就在没有 `CLAUDE.md` 时读 `AGENTS.md`，Codex 靠生成的 `config.toml` 补上 `CLAUDE.md`，OMP 只在仓库外由 Looper 放进 system prompt；项目背景和项目知识本来就注入每种运行时。
8. **杀掉 agent 时清理整个进程树**（✅ 已交付 looper #96）：Codex 给每条 shell 命令单开进程组，杀掉 Codex 时这些命令不会结束；无人值守的定时任务会把残留进程越积越多。停止和超时都要结束 agent 起的所有进程，OMP 一并核实（OMP 走同一段代码，未实跑）。
9. **规划者用起并行：提示词**（✅ 已交付 looper #104）：计划本来就是依赖图，依赖都满足的步骤同时派出（每个任务最多 4 个 Worker），但规划者常排成一条直线。推荐的规划者提示词改为：只为用到结果的步骤加依赖，独立的部分并行；前后端这类协作先单独一步把接口约定写进文件，再让各部分并行，最后一步联调；并行的步骤各改各的文件。计划契约的 `depends_on` 只列用到结果的步骤，Worker 契约要求提交时只 `git add` 自己改的路径。评测加了能并行的用例 `parallel_parts` 和要先定接口的 `interface_first`；后者在 haiku 下新旧提示词都排成直线，仍是缺口。opus 下新旧提示词各跑一次都是先写接口文件、服务和客户端并行、最后联调，都通过（各约 $0.87），即新提示词也没有改变 opus 的计划：opus 原本就这样排，实际任务里见到的串行这个用例没有复现。
10. **任务进度与总结**（✅ 已交付 looper #114）：规划者在已有的决策回合和巡检回合的回复里带 `progress`（完成了什么、正在做什么、下一步、风险、预计还要多久），代码存为任务日志的 `progress` 条目，任务页「Progress」栏显示最新一条和历史，不额外加回合；完成任务的那个决策必须带 `final`（交付了什么、怎么用、验证证据、遗留问题），代码校验字段后按小标题写进 `result_summary`；失败或停止时由代码写收尾说明（为什么结束、做完了什么、没做完什么）。总结和失败说明沿完成时已有的路径发回来源渠道（飞书、Telegram 上由 #115 恢复）。
11. **并行步骤的目录隔离**（I1 ✅ 已交付 looper #127；I2、I3 未开始；设计见[并行步骤的工作目录隔离 RFC](../architecture/rfcs/step-workdir-isolation-v0.zh-CN.md)）：同一任务里并行的 Worker 原来共用一个工作目录，一个 Worker 的 `git add -A` 会带上另一个写了一半的文件，同时装依赖、构建也会互相干扰。项目设置 `step_workspaces` 为 `worktree` 时，任务目录是 git 检出、在分支上，和别的步骤同时跑的步骤各在自己的 worktree 和分支上做，被接受时由代码合并进任务目录检出的分支（每步一个合并提交，保留步骤里的提交），合不进去交给规划者决定；默认 `shared`，行为不变。I2 是被忽略的本地文件的链接清单、检出大小上限、任务页的工作区标记和撤销命令、`looper doctor` 的 git 检查；I3 是在告警监控项目上跑稳后改默认值。
12. **管家平台工具 T1**（2026-10-15 定，任务变更部分见 looper #126，管家工具见 looper #129，设计见[管家平台工具 RFC](../architecture/rfcs/manager-platform-tools-v0.zh-CN.md)）：协议和核心——操作目录、风险分档、确认规则、管家专用接口和令牌、留痕、`looper mcp-server` 和三种运行时的注入；只读工具和任务类工具（建、开始停止恢复重启取消、给运行中的任务加补充说明、改未开始的任务）；任务随时可以编辑调整：补充说明（管家转达和聊天页直接发给规划者的，存为 `supplement`，取代 `user_instruction`）和需求修改（存版本，任务页显示当前文本和历史）每一条都以一轮回合送达规划者，规划者忙时排到下一轮、重启后也不丢；规划者、之后的执行者和评审者都看得到；规划者的 CHAT 新增 `replan`（运行中的步骤默认保留，可点名停掉）；迁移现有 JSON 动作；管家评测和「补充说明、需求修改触发重新规划」的规划者评测。
13. **管家平台工具 T2**：配置类工具——项目、Agent Profile、LLM 提供方（不含密钥）、角色和项目的设置层、技能与 MCP 的安装、花费查询；项目的「规划者可选的 Agent Profile」列表（默认为空）和规划者在本任务内为执行者、评审者选 Profile（规划者范围的 MCP 工具，按任务发令牌，RFC 第 8.3 节），排在第 11 项的 I1 之后。
14. **管家平台工具 T3**（并入原定时任务 S3）：定时任务工具、配置导出；定时任务的导出与导入（导入后一律停用）。

### 5.1 核心缺陷（优先修，穿插在阶段之间）

- ~~**飞书渠道调用的 lark-cli 命令已不存在**~~（✅ 已修复 looper #102）：lark-cli 1.0.97 没有 `message send`，`event consume` 也不再接受 `--app-id`、`--format`，飞书渠道既收不到也回不了消息，消费进程还在紧循环里反复重启；现在收用 `lark-cli event consume im.message.receive_v1 --as bot`（扁平事件），发用 `lark-cli im +messages-send --as bot`（回复 `--chat-id`，定时任务通知 `--user-id`，共用一个构造函数），消费进程停止后 10 秒再起。`app_id_env` 仍需设置才能启动，但不再决定用哪个应用（由 lark-cli 自己的配置决定），二者如何对齐留待以后。

### 5.2 工程待办

- **「飞书机器人 token」渠道类型（2026-10-15 定）**：和「飞书（lark-cli）」并列的另一种渠道类型，功能相同（私聊收发、回复、定时任务通知、自己的发送者白名单与按姓名添加、被拒发送者）。不经 lark-cli，直接调用飞书开放平台：app_id 和 app_secret 存进 `channel_secrets`（接口只显示是否已设置，界面打码），用 tenant_access_token 收发；收消息需要事件订阅（长连接或回调地址），做法先核实。
- **插件与 skill 库的上下文开销**：全部导入后，opus 首个请求的 token 约增加 86%（M1.6 实测）。M8a 起导入的条目默认不加载，结合 M2 的花费数据按角色安装常用的。
- **v2.6.0 已发布（2026-10-14）**：[release](https://github.com/foolzzz/looper/releases/tag/v2.6.0)，由 Release workflow 按 tag 构建发布，附 `looper-2.6.0-py3-none-any.whl`。发布后实测：在空白 HOME 里用 `install.sh` 全新安装（`looper 2.6.0`、`loopx` 别名、serve 与 doctor 正常）；从 v2.5.0 升级（`install.sh` 卸掉旧 `loopx` 包；首次启动把 `~/.loopx` 搬到 `~/.looper` 并留链接，按 v2.5.0 表结构先备份再迁移）。下一个版本待定。
- **升级时迁移前自动备份（2026-10-14 定，✅ 已交付 looper #60）**：`serve` 发现有未执行的迁移时，先用 `pg_dump` 备份到 runtime root 下的 `backups/`（保留最近几份）再迁移；没有可用的 `pg_dump` 时拒绝执行破坏性迁移并提示安装方法；`doctor` 检查 `pg_dump`。自 v2.5.0 起已有 11 处删列、删表的迁移。
- **服务常驻（2026-10-14 定，✅ 已交付 looper #59）**：`service install / uninstall / status / restart`，macOS 生成 launchd LaunchAgent，Linux 生成 systemd 用户服务，开机自启、崩溃拉起；本机也改用它运行主 checkout 的代码。
- **全面改名为 looper（2026-10-14 定，✅ 已交付 looper #64）**：命令、Python 包、数据目录（`~/.loopx` → `~/.looper`，自动迁移）、环境变量（`LOOPX_*` → `LOOPER_*`，旧名作为回退）、界面与文档统一改名；`loopx` 命令保留为别名过渡一个版本。改名是机械改动且触及几乎所有文件，**排在当前这批 PR 合入之后、M9b/M9c 开工之前**，单独一个 PR，期间不开其他分支；README 与控制台首屏的改动先请维护者预览（本次改名维护者已免除预览）。默认库名 `loopx`、发布 tag 与历史、NOTICE 对上游 LoopX 的署名不改。
- **Codex 运行时的已知差距（M9c）**：运行中的 Codex Worker 不能被引导，Planner 的消息只能留给这一步的决策轮（评测里聊天纠偏任务因此多一次重试），要引导得改走实验性的 `app-server`；Codex 不报压缩和单次请求的用量，记录里没有上下文规模，单次上限只能在一轮结束时判断。常驻会话的目录（`codex/sessions/<owner>`）已在 M9e 回收：服务启动时删掉已保存会话不再是 Codex 的。
- **服务崩溃后已退出的 agent 留下的进程（looper #96）**：启动时只清扫记录在案、仍在运行的 agent（pid 加启动时间），连同它的整棵树；服务被 SIGKILL 或崩溃时已经自己退出的 agent，它的子进程已经挂到 1 号进程下，没有记录能把它们认回来，下次启动不会结束。要补得给每个后代留一个各平台都读得到的标记（如继承的环境变量），macOS 的 `ps` 看不到别的进程的环境变量和会话号，还没有可靠的读法。
- **Claude Code 的 shell 快照写进操作者的家目录（looper #96 实跑发现）**：Looper 起的 Claude Code agent 跑 Bash 命令时，会在操作者的 `~/.claude/shell-snapshots/` 下建一个临时的 shell 快照，每条命令都从那里 source（命令行里看得到，文件名里的时间戳就是这次运行开始的时间，运行结束后文件已删掉）；这是隔离的缺口，Looper 没有改 Claude Code 的 `HOME`，要不要隔开、怎么隔开待定。
- **agent teams 的已知差距（looper #80）**：队友的模型请求不在 stream-json 里（只有它的助手消息带着 `parent_tool_use_id` 出现），所以单次软上限在某个结果报出累计值之前看不到队友的 token，运行记录里也没有它逐个请求的用量；队友拿不到 agent 的 `--append-system-prompt-file`（harness、角色、项目、任务和契约各层）。不抓取队友的 transcript 来补。
- **Manager 自己动手做小活（looper #88 的真实对话发现）**：haiku 收到「创建一个 hello.txt」这类一句话的小活时没有走任务流程，直接用自己的工具在 Manager 的工作目录里写了文件（Manager 有全部工具、不需确认）；要不要在 role prompt 里限定 Manager 不亲自改文件，或收窄它的工具，待定。
- **OMP 运行时的已知差距（M9e）**：运行中的 OMP Worker 不能被引导（`-p` 只读一次 stdin，`--mode rpc` 能插话但未采用），评测里聊天纠偏因此多一次重试；omp 的 `task` 工具找子 agent 定义时从工作目录一路往上找 `.omp/agents`，过了仓库根也不停，工作目录在操作者家目录下时会读到 `~/.omp/agents`（如果有），Looper 关不掉，只能整个去掉 `task` 工具；工作目录不在 git 仓库里时关掉了向上查找的发现来源，工作目录自己的 `AGENTS.md`、`CLAUDE.md`、`.omp/` 等也不读；后台请求（skill 描述压缩成路由提示、压缩摘要）不在事件流里，不计入花费和单次上限；每个新进程的首个请求基本不命中提示缓存（评测里未缓存输入 101k，Codex 约 60k），可以试 `--prompt-cache-key`；每个 runtime root 第一次运行要解压约 167 MB 的原生库（`omp/natives`）。

- **Reviewer 看不到重试时补充的说明（2026-10-15，告警分诊定时任务的影子运行发现）**：Planner 决定重试一个步骤时写给 Worker 的补充说明（例如放宽的边界、只需补齐某个输出），Reviewer 看不到，它按原始的步骤说明评审。结果是 Worker 照补充说明做了，评审却判不通过，整步作废、重新规划，或者多重试几次。重试时应把 Planner 的补充说明一并交给 Reviewer。
- **定时任务里 Planner 的花费占比高（2026-10-15，告警分诊定时任务的影子运行）**：告警分诊这类每轮结构相同的定时任务里，Planner（opus · xhigh）占一轮花费的 38–48%，其中仅制定计划一次就约 $0.6。可选做法：定时任务的 Planner 用较低强度的 Profile，或者让规划参考上一轮的计划。待定。
- **任务结束通知的过渡缺口（looper #108 的后续）**：在 Telegram 或飞书发起、但任务没有记录通知对象的（例如升级前就已在跑的），结束时 TaskRouter 不发通知，Manager 也不再转发结束状态，只能在面板上看到。可以让 TaskRouter 在 GOAL_STATUS 的 metadata 里标明「会发通知」，没标的由 Manager 照常转发。
- **渠道对话的存储挪到渠道层（looper #106 的后续）**：Telegram、飞书的回复现在是 Manager 发出时就记下的，渠道之后发送失败也算已回复；应改成渠道发送成功后再记。另外两点：用户发的 `/new` 本身不存（它的回复会存），以及缺少「存储失败不影响回复」的测试。

### 5.3 非核心待办（M6 之后统一做）

（暂无。已完成的见第 4 节进度台账。）

### 5.4 验证缺口

以下行为已实现，但还没有在真实环境中验证过，相关改动或排查时优先补上：

- **飞书、Telegram 的真实流量**：飞书私聊的收发已经实测过（2026-10-15，维护者给自己的飞书机器人发私聊，收到消息并以 Markdown 回复）。收到消息时的「Get」表情和单条状态消息的原地编辑也已实测（2026-10-15，维护者在真实飞书里看到表情出现、同一条状态消息原地更新）。任务被接受时的「任务已分解为 N 个步骤」、各条进度消息和恰好一条任务结束通知也已在真实飞书里验证（2026-10-15）；那次看到的顺序和格式问题（结束通知排在最后一条进度之前、Markdown 原样露出、链接点不开、标题是英文需求）由 #122 修复。Telegram 仍只用本地替身验证过（M1.7、#26）。
- ~~**agent teams**~~（已覆盖，looper #80）：Looper 从未下发 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`，它在 `--setting-sources project` 隔离之前随操作者的个人设置进来，之后仍会随从 Claude Code shell 启动的服务继承进来；现在由 Agent Profile 的 `agent_teams` 决定（Claude Code profile 默认开），真实运行见第 4 节台账，已知差距见第 5.2 节。
- **多个后台任务同时结束**的自主轮切分（#26）。
- **DeepSeek、Kimi 和 GLM 国际站的真实 key**（M9b）：预设按官方文档填写，真实运行只覆盖了本机 CPA 和 GLM（BigModel），DeepSeek 只经 `looper serve` 跑过 agent teams 的两个单步任务（looper #80），没跑 M6 评测。
  维护者用自己的 key 各跑一次 M6 真实评测核对：`LOOPER_EVAL_API_KEY=… python -m evals.prompt_regression run --mode real
  --provider-preset deepseek`（或 `kimi-moonshot-cn`、`kimi-moonshot`、`glm-zai`），看任务都通过、`cost_basis` 全是
  `provider_price`、花费与平台账单大致一致。
- ~~**非 haiku 模型**~~（已覆盖，looper #58）：M6 真实模式评测用 sonnet（`claude-sonnet-5-5`）和 opus（`claude-opus-5-5`）各跑一次，都是 5/5、契约违约 0、Planner 改文件 0，花费 $0.62 和 $1.30（haiku 基线 $0.33）；唯一差异是 opus 在聊天纠偏任务里多加了一步（7 步对 6 步），结果与契约都正常。其余验证缺口暂不安排。
- **项目自带的权限设置**：项目的 `.claude/settings.json` 或托管设置禁止某些工具、关闭 bypass 模式时，agent 的表现（M1.7）。
- **插件里的 agent 和命令**：M1.6 只验证了 skill 的加载和调用。
- ~~**Planner 不亲自改文件**~~（已覆盖）：M6 回归评测的每个任务都检查 Planner 运行里的改文件工具调用和只在 Planner 轮次内变化的文件，真实 haiku 基线为 0（looper #36）。
- ~~**Codex、OMP 管家的真实工具调用**~~（已覆盖，looper #129）：两种运行时都接本机 CPA（gpt-5.6-luna，API key 在运行时从 CPA 的配置文件读取），各跑 `dialogs --dialogs status`。Codex 跑 1 次、OMP 跑 4 次，每次都调到了 `looper_tasks` 或 `looper_overview`，并给出正确答案。OMP 第一次运行时有一次 `looper_tasks` 调用被拒，当时没有记下原因，后面 3 次都没有再出现；评测现在会记录被拒调用的错误消息。

### 5.5 上游 LoopX 清理（B1–B4）

从 fork 里删掉不再使用的上游 LoopX：control plane、上游 CLI、`apps/` 前端、插件和它们的测试与文档，约占全部代码行数的 96%。`loopx/agents`、`loopx/db` 不导入任何上游模块，所以分四个 PR 依次删除，每个都可以独立回滚。

- [x] **B1 解耦**（looper #31）：`loopx` 命令只剩 `serve`、`version` 和 `--version`，`[project.scripts]` 指向新的 `loopx/cli.py`，启动时不再加载上游模块（`tests/test_cli_entry.py` 守护）；删除 `setup.py`、`MANIFEST.in`，wheel 不再依赖上游 Chat 包，仍带 `web/` 构建的 dashboard；Python Tests 换成只跑 agent 系统测试的 job。上游代码仍留在磁盘上。
- [x] **B2 删除上游测试、smoke 和 CI**（looper #34）：删除 1,462 个文件、475,527 行。`tests/**` 只留 agent 系统的 31 个测试文件和它们的 helper（`conftest.py`、`library_fixtures.py`、`claude_stream_replay.py`、`runtime_root_guard/`、`fixtures/claude_stream/`），`examples/**` 只留两个发布 smoke，删除 `regression/`、6 个上游 workflow、`scripts/ci/` 和 `sonar-project.properties`；`tests/conftest.py` 去掉 `loopx.canary` 导入、smoke-suite 选项和上游 fixture；`pyproject.toml` 设 `testpaths = ["tests"]`，CI 直接跑 `pytest`。不带参数的 `pytest` 收集 899 个测试，没有收集错误（之前 11,849 个，8 个文件收集失败）。第 5.2 节因此关闭 4 项：CI 基线（Python Tests 在每个 PR 上跑 agent 系统测试并通过）、Optional Ark Turn workflow、`test_dashboard_command` 的 5 个失败用例和它的偶发超时。
- [x] **B3 删除上游代码**（looper #35）：删除 2,040 个文件、638,943 行。`loopx/**` 只留 `agents`、`db`、`__init__.py`、`cli.py`、`paths.py`（`paths.py` 只留 runtime root 的三个函数）；删除 `apps/`、`packages/`、`skills/`、`man/`、除 `stage-dashboard.sh` 和 `release-surface-args.sh` 以外的 `scripts/**`（包括 `install-from-github.sh` 和 `install-local.sh`），以及根目录的 `package.json`、`package-lock.json`、`tsconfig.control-plane.json`。`pyproject.toml` 只留 `loopx` 一个命令，去掉 `deepseek-harness` extra、上游的测试依赖、package-data、data-files 和 mypy 配置；dependabot、CODEOWNERS 和 `.gitignore` 里指向已删路径的条目一并去掉。第 5.2 节因此关闭 `scripts/install-from-github.sh` 默认 `stable` 分支一项。
- [x] **B4 文档与根目录文件**（looper #38、#40）：删除 587 个文件、146,883 行。`docs/**` 只留本计划、两份架构文档、`docs/design.md` 和 `docs/releases/`；删除 `docs/fork/**`、`ADOPTERS.md`、`mkdocs.yaml`、`.github/GOVERNANCE.md` 和 4 个上游 issue 模板。`AGENTS.md`、`CONTRIBUTING.md` 改写成精简的 fork 版本；`docs/development/design.md` 的通用界面规则并入 `docs/design.md` 的「界面设计」一节；发布流程移到 `docs/releases/README.md`，发布正文检查只保留中英双语部分；CODEOWNERS、SECURITY、SUPPORT、CODE_OF_CONDUCT 和 PR、issue 模板指向 `foolzzz/looper` 和 `@foolzzz`；CHANGELOG 里指向已删文档的链接改为指向删除前最后一个提交的固定链接（使用说明改指 `docs/architecture/agent-system.md`）。`README.zh-CN.md` 改写为英文 README 的中文版，首屏经维护者预览后单独提交（looper #40）。

维护者的决定：

- 安装和升级只走 `install.sh`（安装 Release 的 wheel），升级就是重新运行它。删除 `loopx update` 和 `loopx init`（B1）；`scripts/install-from-github.sh` 和 `scripts/install-local.sh` 在 B3 删除。
- `AGENTS.md`、`CONTRIBUTING.md` 在 B4 改写成精简的 fork 版本：保留 `codex/` 分支上的 worktree + PR、DCO（`git commit -s`）、公开/私有边界、核心优先、第 1 节的测试与验收规则和首屏预览；删掉依赖被删工具的规则（`loopx canary premerge`、`loopx pr-review`、self-repair skill、benchmark smoke、control plane 细则）。
- `README.zh-CN.md` 在 B4 改写为 fork `README.md` 的中文版，首屏提交前请维护者预览。
- `docs/fork/**` 在 B4 删除，CHANGELOG 指向 `docs/fork/usage.md` 的链接改指 `docs/architecture/agent-system.md`。
- 默认做法（B4）：发布正文检查只保留中英双语检查，去掉上游文档检查；`docs/design.md` 作为 `web/` 的设计参考，并入 `docs/development/design.md` 里通用的 UI 规则；治理文件（CODEOWNERS、SECURITY、SUPPORT、CODE_OF_CONDUCT、issue 模板）指向 `foolzzz/looper` 和 `@foolzzz`，删除上游的联系邮箱。
