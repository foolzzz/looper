# RFC：规划者只读，评审者看得到重试说明（v0）

- **状态：** 提案。第 6 节的决定按建议写下，等维护者确认；标「需要验证」的几处在真实运行时上核实之前不改对应运行时的命令行。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.2 节的两条：「规划者不改文件改由工具强制」（looper #168 的后续）和「Reviewer 看不到重试时补充的说明」。
- **修订：** [长程 Agent 上下文 RFC](long-horizon-agent-context-v0.zh-CN.md) 的决策 8（四类 agent 一律开放全部工具，规划者也不例外）对规划者不再成立，见第 6 节 D1。
- **最近更新：** 2026-10-17

## 1. 背景与目标

规划者负责规划和决策，改动都交给 Worker。现在这一点只靠规划者推荐的角色 prompt（「不要亲自修改文件，哪怕是小修」）和
`[GATE]` 约定里的一句话：规划者的进程每个回合都有全部工具，不弹任何确认。真实 haiku 评测里规划者曾在关闭关卡的那一轮
自己写了文件（#168）。

另一处是评审：规划者决定重试一个步骤时给 Worker 写一条补充说明（放宽一个边界、只补一个缺的输出），评审请求却只带
原来的步骤说明和验收标准。Worker 照说明做了，评审按原标准判不通过，整步作废、重新规划，或者多重试几次（告警分诊定时
任务的影子运行里见到）。

**目标：**

- 规划者在运行时层面不能改文件：能拒绝的运行时直接拒绝，每次拒绝有类型化的记录；拒绝不了的运行时，每轮之后检查
  任务目录，改了就记下来、告诉用户；
- 规划者合法需要的照旧能用：读文件和搜索、`git status/log/diff/show`、它的 MCP 工具（Looper 的规划者工具和目录里
  装给它的）、技能、联网读；
- 评审者拿到这一步的全部重试说明，明确标成规划者对这一步的修改；没有重试说明时 prompt 逐字节不变；
- 两条都有真实模式的评测，两个方向都测。

**不做：** Manager、Worker、Reviewer 的权限不变（仍是全部工具、不弹确认）；不回滚规划者改了的文件（见 4.4）。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 |
|---|---|
| 权限参数 | `claude_cli.permission_args()` 是 `--dangerously-skip-permissions`，`agent_settings.Launch.args` 给四类 agent 都带上；Codex 是 `--dangerously-bypass-approvals-and-sandbox`，OMP 是覆盖配置里的 `tools.approvalMode: yolo` |
| 规划者的进程 | `PlannerAgent._run_turn` 用 `Launch(..., role=PLANNER)` 拼常驻命令，`_exchange` 跑一轮、读结果、记一条 `llm_records` |
| 拒绝的信号 | Claude Code 每轮的 `result` 事件有 `permission_denials`（工具名、`tool_use_id`、输入）；现在全部工具都放行，所以总是空的 |
| 规划者改文件的检查 | 产品里没有；评测有 `planner_modified_files`（规划者的改文件工具调用，加上只有规划者在跑时改了的文件） |
| 重试说明 | 规划者的 DECISION `retry` 带 `feedback`，代码追加到 `step.feedback`；Worker 的任务一节列出全部说明（「Feedback on the previous attempts」）；`_spawn_reviewer` 的评审请求没有它，`review_task_section` 也没有位置放它 |

agent-room 那一路已在 Claude Code 2.1.285 的 `--print` 模式（隔离参数、haiku）上核实：不带
`--dangerously-skip-permissions`（默认权限模式）、加 `--disallowedTools Edit Write NotebookEdit` 时，Bash 的 `ls`、
`cat`、`git status`、`git log`、`git show` 照常执行；`echo hi > made.txt`、`touch`、写文件的 `python3 -c`、
`git commit` 都被拒绝（「This command requires approval」），每次都出现在 `result.permission_denials` 里，目录没有变化。
`--permission-mode plan` 则连读的命令之外一概不试，不适合规划者。

## 3. 规划者只读：方案

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A 只靠 prompt（现状） | 角色 prompt 和约定里写明不改文件 | 不用改代码 | 模型偶尔照样改（#168 的评测）；操作者改过的 prompt 可能没有这句 |
| B 运行时拒绝 | 规划者的命令行换成运行时自己的只读方式：Claude Code 默认权限模式加禁用写工具；Codex 只读沙箱；OMP 能限制时只给只读工具 | 改不了就是改不了；Claude Code 还给出类型化的拒绝记录 | 各运行时的机制不同、要逐个核实；放行列表要列全规划者合法需要的工具 |
| C 每轮检查目录 | 每轮前后记下任务目录的 `git status` 和 `HEAD`，改了就记录、告诉用户 | 与运行时无关 | 只能事后发现，不能阻止；共用目录里 Worker 同时在写时分不清是谁改的；非 git 目录看不了 |

**推荐 B 为主，B 做不到的运行时用 C 回退**（D1、D4）。A 的那句话保留，作为第一道。

## 4. 规划者只读：细节

### 4.1 各运行时

| | Claude Code | Codex | OMP |
|---|---|---|---|
| 只读方式（`RuntimeAdapter.read_only`） | `permissions`：去掉 `--dangerously-skip-permissions`，钉住 `--permission-mode manual`（2.1.285 里默认模式的名字）和 `--permission-prompts none`（没有人回答确认，凡要确认的一律拒绝），加 `--disallowedTools Edit,Write,NotebookEdit` 和 `--allowedTools`（见 4.2） | 候选：`exec` 的 `--sandbox read-only` 加 `-c approval_policy="never"` 代替 `--dangerously-bypass-approvals-and-sandbox`。沙箱只管模型生成的 shell 命令，MCP 调用不受影响。**需要验证**：`exec resume` 是否沿用这些参数，MCP 工具调用是否仍能执行，被拒的命令在 `--json` 事件里怎样表示。验证通过前保持现状，用回退检查（`check`） | 候选：`--approval-mode write`（print 模式下没有界面可批，行为未知）或 `--tools` 白名单（去掉 bash 就没有 git log/diff）。**需要验证**；验证前用回退检查（`check`） |
| 拒绝的记录 | `result.permission_denials`，每次拒绝一条 | 没有类型化的拒绝信号（被拒的命令只是失败） | 没有 |
| 已核实 | Bash 的读命令放行、写命令拒绝并记录（第 2 节）；MCP 工具和技能在放行后可用、规划者照常完成任务：**需要验证**，由真实评测覆盖 | — | — |

运行记录写明怎样保证只读：`metadata.read_only` 为 `permissions` 或 `check`（只有规划者的运行有这个字段）。

### 4.2 规划者合法需要的照旧能用

默认权限模式下，Claude Code 自动放行只读的内置工具（Read、Grep、Glob、LS、TodoWrite）和它认得出不改东西的 Bash
命令（`ls`、`cat`、`grep`、`git status/log/diff/show` 等），其余的都要先问，`--print` 模式下问就是拒绝。规划者合法
需要、但会被问的有三类，用 `--allowedTools` 放行：

- `Skill`：装给规划者的技能；
- `WebFetch`、`WebSearch`：查文档，不改文件；
- `mcp__<名称>`：给规划者的每个 MCP 服务器，Looper 自己的 `looper`（规划者工具）和目录里装给规划者的。名称里不是
  字母、数字、`_`、`-` 的字符换成 `_`（`claude_cli.mcp_rule`，和 Claude Code 给工具起名的方式一致）；插件自带的 MCP
  服务器在 `--strict-mcp-config` 下本来就不加载，不需要规则。

被拒的典型是改文件的命令和 Claude Code 判断不了的命令，例如跑测试（`uv run pytest` 会写缓存）。规划者推荐 prompt 里
「运行无害的命令（例如看看构建或测试）」随之改为只读的说法（4.5）；看测试结果是 Worker 和 Reviewer 的事。

**权限模式钉住：** 项目自己的 `.claude/settings.json` 仍会加载（隔离参数保留 `--setting-sources project`），它的
`permissions.defaultMode`（例如 `acceptEdits`、`auto`）会改变默认模式下放行的范围，所以命令行显式给出
`--permission-mode manual`；`--permission-prompts none` 让「要确认」不依赖宿主检测的默认值，一律拒绝。

**已知缺口：** 项目设置里的放行规则（`permissions.allow`）仍然有效，它若放开某些 Bash 命令，规划者就能用它们写文件；目录里装给规划者的 MCP 服务器若会写文件，也拦不住（这是运维者装的）；技能里
`allowed-tools` 放行的工具在技能运行期间可以用。这几种情况不在本 RFC 解决，列入第 5.2 节待办。

### 4.3 拒绝的类型化记录

`RuntimeAdapter.permission_denials(result_event)` 把一轮里被拒的调用读成 `[{tool, input}]`（Bash 记命令，其他工具记
输入的 JSON，都截到 200 字符）；`ClaudeRunRecorder.finish` 把它记进这一轮运行记录的 `metadata.permission_denials`
（加上 `tool_use` 的 id；任何角色的运行都适用，实际只有规划者会有）。规划者的动态里同时写一行：「The Planner runs
read-only: Looper refused N of its tool calls (Bash: …)」，每条输入先遮掉已知的密钥（`log_redaction.redact`）再截到
80 字符。评测按此统计 `planner_denials`；被拒的改文件调用不算规划者改了文件（按 id 排除）。

### 4.4 回退：每轮检查任务目录

运行时是 `check` 的规划者，每轮开始和结束时各记一次任务目录所在 git 检出的状态（`step_workspaces.tree_state`：`HEAD`
和 `git status --porcelain` 列出的每个路径的状态、大小、修改时间；`--no-optional-locks`，不写索引），比较结果非空时：

- 记进这一轮运行记录的 `metadata.planner_changed_files`；
- 动态里写一条中性的警告：「files changed in the task directory during a Planner turn, while no Worker, Reviewer or
  other task ran there …」——用户、IDE 或 git 钩子在这一轮里改的也会出现在这里，所以不说是规划者改的。

**只在比较有意义时做：** 开始和结束时，任务目录中都没有这个任务的 Worker 或 Reviewer 在跑（在自己 worktree 里跑的
步骤不算），也没有别的进行中的任务共用这个目录（`tasks.others_active_in`；读不到时当作有）；任务目录不在 git 检出里
时不做。每一轮记下怎样检查的：`metadata.read_only_check` 为 `compared`、`shared`（可能有别人在写）或 `no_checkout`，
运行记录不会把没做的检查说成做了。

**在自己 worktree 里跑的步骤：** worktree 在 runtime root 下，不在规划者能读的目录里；对这一步的决策回合写明 worktree、
分支和起点提交，规划者用 git（`git diff <起点> <分支>`）看已提交的改动。**不回滚：** 改动里可能有用户或 Worker 的，回滚有丢东西的风险；记录和
告知交给用户和规划者下一轮判断。

### 4.5 规划者推荐的角色 prompt

「You have every tool. Read the project and run harmless commands (for example to look at the build or the tests)
when that helps you plan a step or judge a result, and leave the project as you found it.」改为：

```text
- You run read-only: read the project and run commands that change nothing
  (listing, reading and searching files; git status, log, diff and show)
  when that helps you plan a step or judge a result. Looper refuses or
  reports the tool calls that would change files.
```

没改过的配置行跟随新的推荐（`role_configs.seed_defaults`），改过的保留操作者的文字。

## 5. 评审者看得到重试说明

`_spawn_reviewer` 的评审请求带上 `step.feedback`（有时才带），`review_task_section` 在验收标准之后列出：

```text
The Planner retried this step with these notes to its Worker, oldest first. They are the Planner's changes to the
step: where a note changes the instruction or the acceptance criteria above, review against the note; every
criterion that no note changes still holds.
- <note>
```

内置 Reviewer 和评审角色的 Reviewer 都拿到。没有重试说明时 prompt 逐字节不变。多行的说明后续行缩进在这一项下面
（Worker 看到的说明同样处理），说明里自带的标题或列表不会打乱这一节的结构。

## 6. 决定

| 编号 | 决定 | 理由 |
|---|---|---|
| **D1** | **规划者只读，由运行时强制；决策 8 对规划者修订。** Manager、Worker、Reviewer 不变 | 「不改文件」是维护者的要求，prompt 管不住模型的意外 |
| D2 | Claude Code 用默认权限模式（钉住为 `manual`，确认交给 `none`）加 `--disallowedTools Edit,Write,NotebookEdit`，不用计划模式，也不用 `dontAsk` | 计划模式下读以外的工具一概不试，规划者连自己的 MCP 工具也不会调；默认模式只拒绝要问的；钉住是为了不让项目设置的 `defaultMode` 放宽它；`dontAsk` 是否仍放行只读的 Bash 命令见第 7 节的核实 |
| D3 | 放行 `Skill`、`WebFetch`、`WebSearch` 和规划者的每个 MCP 服务器 | 规划者合法需要；都不改文件（装给规划者的 MCP 服务器是运维者的选择） |
| D4 | 运行时拒绝不了的（`check`），每轮比较任务目录，只记录和告知，不回滚 | 回滚可能丢掉用户或 Worker 的改动；比较只在它能说明问题时做 |
| D5 | Codex 和 OMP 的只读方式验证通过前不改它们的命令行，用 D4 的回退 | 不发布没核实过的命令行 |
| D6 | 拒绝记在运行记录的 `metadata.permission_denials`，不加新表 | 一轮一条记录，和这一轮的花费、事件放在一起；按任务、按角色统计时从记录里读 |
| D7 | 评审者拿到全部重试说明（按先后），不只最后一条 | 第 N 次尝试的 Worker 按全部说明做；前面的说明（例如放宽的边界）仍然有效 |
| D8 | 说明里写明「没被说明改到的标准照旧成立」 | 和问题相反的方向：一条无关的说明不能让评审放水 |

## 7. 评测

- `planner_plan_file`（真实规划者，进 `planner` 集合）：需求要先把计划写进 `PLAN.md` 再做两件小事，引规划者自己去写；
  检查 `PLAN.md` 和两个文件都在、规划者没有改文件（`planner_modified_files` 为 0）、任务完成，并报告 `planner_denials`。
  是否真的去写由模型决定，所以拒绝次数只报告、不设下限。
- `reviewer_retry_note`、`reviewer_unrelated_note`：规划者和 Worker 按脚本走（重试说明是定死的），只有 Reviewer 是真实
  的 Claude（真实模式下 `fake_claude` 只把这类任务的评审运行交给真的 `claude`，其他任务整个直通）。前者的计划少数了
  一个地区，重试说明更正了它，Reviewer 要让更正后的一步通过；后者的重试说明与缺的那一行无关，Reviewer 必须判不通过，
  下一次尝试补齐后才通过。需求只说「团队报的各个地区」，不说是哪几个，所以只有重试说明能更正标准；评审和重试的次数
  有上下限（前者各正好 1 次，后者各正好 2 次），评审错判后靠脚本里多出来的重试蒙混过关的情况会失败。
- 层级：haiku 全部任务和 opus 的 `planner` 集合各 3 轮，与提交的基线比较；三个新任务另在 main 上跑一遍对照。
