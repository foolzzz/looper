# RFC：并行步骤的工作目录隔离（v0）

- **状态：** 设计提案，待维护者审阅；待决问题见第 9 节。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.0 节第 11 项。
- **最近更新：** 2026-10-15

## 1. 背景与目标

#104 之后，规划者会把相互独立的步骤排成并行，依赖都满足的步骤同时派出（每个任务最多 4 个 Worker）。但这些 Worker
共用任务的同一个工作目录：

- 一个 Worker 的 `git add -A` 会带上另一个写了一半的文件（#104 只在契约里要求「只提交自己的路径」，靠模型自觉）；
- 两个 Worker 同时装依赖、跑构建、跑测试，会互相覆盖产物、抢锁；
- Reviewer 在同一个目录里验收，看到的是几个步骤混在一起的状态；
- 一个步骤的验证命令可能因为另一个步骤还没写完而失败，失败原因与这一步无关。

**目标：** 工作目录是 git 仓库时，并行的步骤各在自己的 git worktree 里做，互不可见；步骤被接受时，由代码把它的
改动合回任务目录，再派出依赖它的步骤。冲突变成这一步的失败，交给规划者决定重做、改计划还是放弃。

**不做：**

- 串行的步骤、单步任务、不是 git 仓库的工作目录，行为不变（D2、D7）；
- 不替用户提交、推送，不改用户检出的分支（D4）；
- 不处理多个任务共用同一个目录（同一项目的任务默认都在项目目录里跑）的问题，见第 8 节。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 |
|---|---|
| 任务目录 | 任务的 `work_dir` 来自建任务时的显式目录，否则用项目的默认目录（`projects._task_work_dir`，`resolve_work_dir` 要求绝对路径）。TaskRouter 存进 `tasks.work_dir`，规划者存为 `plan.work_dir`，第一轮时创建目录 |
| 规划者 | 常驻 Claude 的 cwd 就是 `plan.work_dir`；推荐提示词要求它只读、不改文件（#104 的 eval 统计「规划者改文件」） |
| Worker | `_dispatch_step` 在 `WORK_ITEM` 里带 `context.work_dir = plan.work_dir`；`WorkerPool._execute` 在这个目录里起运行时，`judge_result` 按这个目录检查报告里的 `changed_files` 是否存在 |
| 提交 | `WORK_CONTRACT` 写着「其他步骤可能同时在这个目录里工作：提交时只 `git add <自己的路径>`，不用 `git add -A`」（#104）；规划者提示词要求并行步骤各改各的文件 |
| Reviewer | `_spawn_reviewer` 同样带 `context.work_dir = plan.work_dir`，在整个任务目录里验收 |
| 依赖 | 依赖它的步骤只在所依赖的步骤 `verified` 之后才就绪（`ready_steps`），它的 Worker 拿到所依赖步骤的报告摘要 |
| 并发 | `_dispatch_ready` 一次派出至多 `MAX_CONCURRENT_WORKERS - running` 个就绪步骤；任务级之外还有项目级的 Worker 上限 |
| 重启 | `_resume`：正在跑的步骤用同一次尝试重跑，评审中的换新 Reviewer，被打断的决策重做 |

全程没有任何 git 操作：Looper 不知道任务目录是不是仓库。

## 3. 概念

| 概念 | 定义 |
|---|---|
| 任务目录 | 任务的 `work_dir`，用户检出的那份仓库。规划者和不隔离的步骤在这里工作 |
| 隔离步骤 | 派出时被判定为需要隔离（D2）的步骤；它的 Worker 和 Reviewer 在它自己的 worktree 里工作 |
| 步骤工作区 | 一个隔离步骤的 worktree：一个目录、一个分支、一个起点快照，以及它的状态（第 5 节） |
| 起点快照 S | 派出时任务目录的完整状态：`HEAD` 加上所有未提交的改动和未被忽略的未跟踪文件，做成一个 commit 对象。用临时索引生成，不动任务目录的索引、分支和工作区 |
| 结果提交 W | 步骤被接受时，worktree 里的全部改动（含 Worker 自己的提交）在步骤分支上收成的最后一个 commit |
| 合回 | 以 S 为基、以任务目录的当前状态 T 和 W 做三方合并，把结果作为**未提交的改动**写回任务目录 |

## 4. 决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 隔离只在任务目录是 git 仓库时启用（`git rev-parse --show-toplevel` 成功，且 git ≥ 2.40） | worktree、快照和三方合并都依赖 git；非 git 目录没有可靠的合回办法 |
| D2 | **派出时判定**：一个步骤派出的那一刻，任务里还有别的步骤在途（`dispatched`、`deciding`、等评审的 `completed`），或者同一轮还派出了别的步骤，它就隔离；否则在任务目录里跑。在任务目录里跑的步骤在途时，不再派出别的步骤，等它结束 | 串行和单步任务完全不变；判定只看此刻的状态，不靠字符串和猜测。后一条只在「聊天加步骤」或改计划时才会碰到（链式计划里，一个步骤独自在途时不会有别的步骤就绪） |
| D3 | 起点是派出时任务目录的快照 S，不是 `HEAD` | 前面串行步骤的改动通常没有提交；只从 `HEAD` 开，隔离步骤会看不到它们 |
| D4 | **合回由代码在接受时自动做**，结果是任务目录里的未提交改动，和这一步在任务目录里跑的效果一样；不提交、不改用户的分支 | 依赖它的步骤要在合并后的状态上开工；交给规划者另开一个「集成步骤」多一次模型运行，还不可靠。不碰用户的分支和提交历史，回滚就是 `git checkout`/`git clean` |
| D5 | 合并冲突 = 这一步的这次尝试失败，原因写明冲突的文件，进入规划者的决策回合（重做、改计划、放弃）。不留半截合并 | 复用现有的决策机制；任务目录只会处于「合入前」或「完整合入后」两种状态 |
| D6 | Reviewer 在步骤的 worktree 里验收，消息里附上相对 S 的改动清单 | 验收的是这一步自己的结果，不受其他步骤干扰 |
| D7 | 项目执行配置新增 `step_workspaces`：`shared`（现行为）或 `worktree`；I1 默认 `shared`，在告警监控项目上实跑后再讨论默认值（Q2） | 新机制先在明确开启的项目上跑；已有项目的行为不因升级而变 |

**备选：** 合回也可以做成提交级的 `git merge`（保留步骤分支上的提交，合进用户检出的分支）。这样用户的分支上会出现
Looper 生成的合并提交，而且任务目录有未提交改动时 `git merge` 会拒绝或带出脏状态，所以 v0 不采用，见 Q1。

## 5. 数据模型

```
step_workspaces                      一个隔离步骤的一次工作区（重试复用同一行，见第 7 节）
  id              BIGSERIAL
  task_id         → tasks ON DELETE CASCADE
  step_id         → task_steps.id
  repo_dir        任务目录（主仓库），清理时用
  path            <runtime_root>/worktrees/<task_id>/<step_key>
  branch          looper/<task_id>/<step_key>
  base_commit     起点快照 S
  result_commit   结果提交 W；合回之前为 NULL
  state           'active' | 'merging' | 'merged' | 'conflict' | 'discarded'
  detail          冲突的文件、出错原因；可空
  removed_at      目录和分支已清理的时间；可空
  created_at, updated_at
  UNIQUE (step_id) WHERE state IN ('active', 'merging')   每个步骤同时只有一个在用的工作区
```

- 状态只在 `planner.py` 的一处推进，转换写成表（`StepWorkspaceState` 枚举 + `ALLOWED_TRANSITIONS`），非法转换抛错：
  `active → merging → merged`、`merging → conflict`、`active | conflict → discarded`。`merged`、`discarded`
  是终态；`removed_at` 只记录清理，不是状态。
- `PlanStep` 增加 `workspace: StepWorkspace | None`，随计划一起读写（`db.plans`），重启后由 `_resume` 读回。
- `WORK_ITEM` 和 `VERIFY_REQUEST` 的 `context` 增加 `workspace`：`{"path", "branch", "base_commit"}`；`context.work_dir`
  对隔离步骤就是 worktree 的路径，所以 `WorkerPool`、`judge_result`、运行记录的 `cwd` 不用改。
- 运行记录、activity 和 API 里的步骤都带上工作区（I2 在任务页显示）。

## 6. 流程

**派出**（`_dispatch_ready` / `_dispatch_step`）：

1. 按 D2 判定。不隔离的照旧。
2. 隔离的：在任务目录里用临时索引做快照 S（`GIT_INDEX_FILE=<临时文件> git add -A`、`write-tree`、`commit-tree -p HEAD`）；
3. `git worktree add -b looper/<task_id>/<step_key> <path> S`。分支已存在（上一轮残留）时先清理再建；
4. 写 `step_workspaces`（`active`），再发 `WORK_ITEM`。任何一步失败：这次尝试按失败处理，原因写明（「无法为步骤 s2
   创建工作区：…」），进入决策回合，不在任务目录里退回去跑。

**Worker 的任务段**（代码追加，模型可见；实现用英文，大意如下）：「这一步在仓库的一份独立检出里做（git worktree，分支 …），它从任务目录在
本步开始时的状态建出；别的步骤同时做的改动这里看不到。这一步被接受后，Looper 把你的改动合回任务目录。不要推送；在
这里做的提交只留在这一步的分支上，合回的是改动本身。」

**接受**（决策 `accept`，或规划者没能决策时由 Worker/Reviewer 的结论判定为 `verified`）：

1. 状态 `active → merging`；在 worktree 里 `git add -A` 并提交，得到 W（worktree 只有这一步的文件，所以这里用
   `-A` 是安全的）；
2. 在任务目录用临时索引做快照 T；`git merge-tree --write-tree --merge-base=S T W`：
   - 干净：得到树 M，`git diff --binary T M | git apply`，把差异写进任务目录的工作区（不进索引），状态 `merged`，
     activity 记「步骤 s2 已合回任务目录：N 个文件」；
   - 冲突：状态 `conflict`，`detail` 记冲突文件；这一步改判为失败，结果写明「步骤 s2 的改动与任务目录在 a.py、
     b.py 上冲突」，写一条 `step_failed` 日志，进入决策回合；
3. 合回成功后才把步骤记为 `verified`，再派出依赖它的步骤（它们按 D2 在任务目录或新的 worktree 里开工）。

合回可重复执行：在 `merging` 中途崩溃，重启后重算时 T 已经含有这一步的改动，`T → M` 的差异为空，直接记 `merged`。

**清理**：任务完成、失败、停止、取消、删除时，删掉它的工作区（`git worktree remove --force`、`git branch -D`、
`git worktree prune`），写 `removed_at`。没合回的工作区先标 `discarded`。服务启动时扫一遍
`<runtime_root>/worktrees/`：任务不在运行中的目录一律清理，主仓库不存在了就直接删目录、记日志。

## 7. 与其他功能的关系

| 功能 | 怎么处理 |
|---|---|
| 重试 | 复用同一个工作区（和现在在任务目录里重试时保留上一次尝试的文件一致）。**因冲突而重试**时丢弃旧工作区，从任务目录的当前状态新建 |
| 改计划 | 只替换未开始的步骤；被丢弃（`skipped`）的已开始步骤，工作区标 `discarded` 并清理 |
| 规划者自己改文件 | 规划者在任务目录里，按提示词不改文件。万一改了，它的改动在 T 里，合回时和别的改动一样参与三方合并 |
| Agent teams | 队友在 Worker 的进程里，cwd 相同，所以都在 worktree 里 |
| Codex、OMP | cwd 是 worktree 根目录，里面的 `.git` 是文件，两者都认作仓库根：Codex 的项目文档、OMP 的向上查找都止于 worktree 根 |
| 项目说明（#97） | 已跟踪的 `CLAUDE.md`、`AGENTS.md` 在 worktree 里；未跟踪但没被忽略的也在（快照包含它们）；被忽略的没有。Claude Code 的 `claudeMdExcludes` 按 worktree 根计算，排除 runtime root 以上的目录 |
| 项目知识、skill | 不变；路径型知识文档指向任务目录里的文件，读到的是任务目录的版本 |
| 定时任务 | 每次触发是普通任务，按项目设置走。告警监控项目在 git 检出里并行处理每条告警、写不同的报告文件，是 I1 的第一个实跑对象；它依赖被忽略的 `.venv`、`.env`，所以要等 I2 的链接功能（Q3） |
| 花费、并发上限 | 不变：worktree 不占 Worker 名额 |

## 8. 失败、回滚与观测

| 风险 | 处理 |
|---|---|
| 被忽略的本地状态（`.env`、`.venv`、`node_modules`、构建缓存）在 worktree 里没有 | I2：项目设置 `step_workspace_links`，列出的路径在建 worktree 时以符号链接指回任务目录；收结果提交时用 pathspec 排除这些路径，不会把链接带回任务目录。共享的链接意味着依赖安装仍会互相影响，这是有意的取舍 |
| 每个 worktree 重新装依赖：慢、占盘 | 建之前估算检出大小（`git ls-tree -r -l HEAD` 求和）：超过上限（默认 1 GiB）不隔离，改为一次只派一个步骤并在 activity 里说明；项目可以设 `shared` 关掉隔离 |
| git 不可用或版本低于 2.40 | 视为不能隔离：同上，一次只派一个步骤并说明；`looper doctor` 显示 git 版本 |
| 用户在任务进行中改了任务目录 | 合回时的 T 包含用户的改动，冲突按 D5 处理 |
| 多个任务共用一个目录 | 现状就有的问题，本 RFC 不解决；合回只应用 T 到 M 的差异，不会覆盖别的任务同时写下的无关文件 |
| 子模块、LFS | `worktree add` 不初始化子模块；LFS 依赖仓库自己的 hook。写进文档，作为已知差距 |
| 服务崩溃 | 运行中的步骤按 `_resume` 在同一个工作区里重跑；`merging` 中断的按第 6 节重算；孤儿目录由启动清扫回收 |

**回滚：** 项目设回 `shared` 即恢复现行为；任务目录里只有未提交的改动，用户可以用 git 撤销任何一步。

**观测：** activity 记录每个工作区的建立（分支、起点 commit 短哈希）、合回（文件数）、冲突（文件清单）、清理和清扫；
运行记录的 `cwd` 就是 worktree 路径；I2 起任务页的每个步骤显示工作区标记（分支、状态），冲突的步骤显示冲突文件。

## 9. 待决问题

| # | 问题 | 建议 |
|---|---|---|
| Q1 | 合回的形式：未提交的改动（D4），还是把步骤分支合并进用户检出的分支、保留提交 | **未提交的改动。** 不动用户的分支和历史；需要提交的任务，由规划者在最后安排一个在任务目录里跑的步骤去提交 |
| Q2 | git 目录的默认值：`worktree` 还是 `shared` | **I1 默认 `shared`，I3 改为 `worktree`。** 先在告警监控项目上跑满一周、确认链接和清理都可靠，再改默认值 |
| Q3 | 被忽略的本地状态：由项目配置链接清单，还是按常见名字（`.env`、`.venv`、`node_modules`）自动链接 | **项目配置，默认为空。** 自动链接是按名字猜，链错了会把一个步骤的安装带进另一个步骤；在项目设置里写明更可控 |
| Q4 | 停止或失败的任务里没合回的工作区，立即删除还是留着 | **留到任务被重启或删除。** 用户可能要从里面捞半成品；启动清扫不删这些工作区，删除任务时才删 |

## 10. 分期

| 阶段 | 内容 | 依赖 |
|---|---|---|
| I1 | 项目设置 `step_workspaces`（默认 `shared`）；D2 的派出判定；快照、worktree、Worker 任务段说明、Reviewer 在 worktree 里验收；接受时自动合回、冲突转为失败；`step_workspaces` 表与状态转换；任务结束清理和启动清扫；activity 记录 | #104 |
| I2 | `step_workspace_links`；检出大小上限与「一次一个」的退路；任务页的工作区标记和冲突文件；`looper doctor` 的 git 检查 | I1 |
| I3 | 默认值改为 `worktree`（Q2）；停止后保留工作区与「丢弃工作区」按钮（Q4）；因冲突重试时从当前状态新建 | I2 |

**每期的测试：**

- **单元测试：** D2 判定按表驱动（单步、链、两个独立加一个汇合、聊天加步骤时有不隔离的步骤在途）；快照、合回的帮助函数
  在临时 git 仓库上跑：干净合回、冲突、任务目录有未提交改动和未跟踪文件、被忽略的文件不进快照、合回重复执行为空操作；
  状态转换表（非法转换抛错）。
- **PG：** `step_workspaces` 的写入与读回、`_resume` 后工作区还在、启动清扫只删不在运行中的任务的目录。
- **真实服务 + 替身 Claude：** 一次性 `looper serve`、新库、非 8767 端口、一次性 runtime root，任务目录是临时 git 仓库，
  项目设为 `worktree`：
  - 两个并行步骤各写不同的文件，再一个依赖两者的步骤：前两个在各自的 worktree 里、合回后第三个在任务目录里看到两份文件；
  - 两个并行步骤改同一行：后合回的那个冲突，规划者收到写明文件的决策回合；
  - 停止任务后工作区被清理，重启服务后启动清扫回收残留。
- **真实模型：** haiku 跑 `parallel_parts`、`interface_first`（项目设为 `worktree`），对比基线；Worker 任务段是模型可见的
  改动，按 `evals/README.md` 跑一次真实模式评测。I3 改默认值前，在告警监控项目上实跑一周。
