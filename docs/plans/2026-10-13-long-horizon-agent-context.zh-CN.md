# 开发计划：长程 Agent 上下文体系（M0–M6）

- **状态：** 执行中（M0 已交付，M1 进行中）
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

## 2. 阶段总览

| 阶段 | 主题 | 状态 | 依赖 |
|---|---|---|---|
| M0 | 项目背景；四类 agent 的 prompt 分层；记录并展示 system prompt | ✅ 已交付（looper #13） | — |
| M1 | 上下文隔离与可复现 | 🔄 进行中 | M0 |
| M1.5 | harness 与 role 分层；harness 继承链 | 待开始 | M1 |
| M2 | 上下文与花费观测 | 待开始 | M0 |
| M3 | 项目知识 | 待开始 | M0 |
| M4 | 项目 skill | 待开始 | M3 |
| M5 | 项目执行配置 | 待开始 | M2 |
| M6 | prompt 回归评测 | 待开始 | M1 |

执行顺序：M1 → M1.5 → M2 → M3 → M4 → M5 → M6。

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
    - 新增 Reviewer 的 role prompt；
    - 现有推荐 prompt 里属于"性格与稳定性"的内容移入系统默认 harness。
  - **记录与展示**：每次运行的 `prompt_layers` 增加 `harness` 层，写明来源（系统默认、Manager 覆盖、项目覆盖）和哈希；历史页展示来源。
  - **本机初始化**：把本机的 CLAUDE.md 与 AGENTS.md 整理合并为操作者的系统默认 harness，写入本机 Settings。合并稿先经维护者审阅，不提交到仓库。
- **不做：** 不在 Agents 页编辑 Planner、Worker 的 harness；不改代码契约的内容。
- **验收：**
  - 四类 agent 的 system prompt 按五层顺序拼装，各层来源可在记录中读回；
  - 修改系统默认后，没有覆盖的 agent 下一次运行就生效；
  - 给某个项目设置 Worker harness 后，该项目的 Worker 和 Reviewer 使用它，其他项目不受影响；
  - Manager 的覆盖只影响 Manager；
  - 真实 Claude 下，Planner、Worker、Reviewer 的产出仍通过判定。

### M2 上下文与花费观测

- **目标：** 长程运行的成本和上下文增长看得见。默认不设花费上限（RFC 决策 2）。
- **范围：**
  - 每轮记录输入 token（含缓存读、缓存写）、本轮结束时的上下文规模，以及自动压缩事件；
  - 按运行和按任务累计花费；
  - 任务页显示任务累计花费和各 agent 的花费分布；Manager、Planner 的历史页显示上下文用量曲线和压缩次数；
  - 以上数据通过 API 可读。
- **验收：** 跑一个多步骤任务后，任务页的累计花费等于各次运行花费之和；人为构造一次自动压缩（或用替身产生对应事件），历史页能看到；Manager 长会话的上下文用量随对话增长且可读回。
- **风险：** 需要先核实 stream-json 中自动压缩事件的确切形态，以实测为准。

### M3 项目知识

- **目标：** 项目可以挂载大块资料，agent 按需读取，而不是全部塞进 prompt。
- **范围：**
  - `project_documents` 表：标题、一句话摘要、来源类型、内容或路径、大小、更新时间；
  - 来源支持手动录入、上传文件、引用项目目录内的路径（RFC 决策 3；实时读取，不支持 URL）；
  - 录入和上传的文档物化到 runtime root 下的项目知识目录，以 `--add-dir` 挂载；引用路径不复制，直接读取；
  - L2 只注入索引（标题 + 摘要 + 读取路径），有总长度上限；
  - 新增项目页 `#projects/<id>`，包含"概览（背景）"和"知识"两个标签。侧栏点击项目仍打开按项目筛选的 Tasks 页，项目栏提供进入项目页的入口。
- **验收：**
  - 真实 Claude 下，Worker 能按索引读到文档，并在报告里引用；
  - 引用的仓库路径内容改动后，agent 读到的是新内容；
  - 超出大小或索引上限时拒绝保存，并说明原因。

### M4 项目 skill

- **目标：** 项目可以带自己的 Claude Code skill，只对这个项目的 agent 生效，不写进用户仓库。
- **范围：**
  - `project_skills` 表：名称、描述、`SKILL.md` 内容；
  - 物化为本地插件目录，以 `--plugin-dir` 挂载；配合 M1 的隔离，只出现项目自己的 skill；
  - 项目页增加 "Skills" 标签。
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
  - 项目页增加 "Settings" 标签。
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

## 5. 不属于本 RFC 的待办

这些问题在开发过程中发现，独立排期，不阻塞 M1–M6：

- **CI 基线**：CI 已改为手动触发（looper #7），而 #6 修掉了 chat-bundle 的已知失败。需要手动跑一次 Python Tests，确认 pytest 是否真正跑通；如果还有失败，逐项修复。
- **界面小问题**：
  - Tasks 列表页内容贴边，没有内边距；
  - 任务详情里 Config 显示成 `"{}"`（JSONB 被当成字符串写入）；
  - 任务详情的返回按钮没有可访问名称；
  - 窄屏下侧栏不会收起；
  - 新建任务时"顺带新建项目"的表单里没有背景输入框。
- **Optional Ark Turn workflow** 引用了已经删除的测试文件，需要修正或移除。
- **`scripts/install-from-github.sh`** 默认使用 `stable` 分支，但 looper 没有这个分支。
