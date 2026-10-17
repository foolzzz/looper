# RFC：并行步骤的工作目录隔离（v0）

- **状态：** 已批准。I1 已实现（looper #127，项目设置默认 `shared`，按项目开启），实现见 [Agent 系统架构](../agent-system.md#step-workspaces)；I2 和 I3 的「丢弃工作区」按钮已实现（looper #180，细化见第 11 节）；I3 的改默认值仍等告警监控项目上的实跑（第 11.6 节）。第 9 节的问题由维护者答复：合回改为把步骤分支合并进用户检出的分支并保留提交，各节已按此修订。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.0 节第 11 项。
- **最近更新：** 2026-10-17（按 #155、#161、#168、#172、#173 修订第 2、4、7 节，新增第 11 节）

## 1. 背景与目标

#104 之后，规划者会把相互独立的步骤排成并行，依赖都满足的步骤同时派出（同时跑几个只受本任务的 Worker 上限限制）。但这些 Worker
共用任务的同一个工作目录：

- 一个 Worker 的 `git add -A` 会带上另一个写了一半的文件（#104 只在契约里要求「只提交自己的路径」，靠模型自觉）；
- 两个 Worker 同时装依赖、跑构建、跑测试，会互相覆盖产物、抢锁；
- Reviewer 在同一个目录里验收，看到的是几个步骤混在一起的状态；
- 一个步骤的验证命令可能因为另一个步骤还没写完而失败，失败原因与这一步无关。

**目标：** 工作目录是 git 仓库、项目开启了隔离时，并行的步骤各在自己的 git worktree 和分支上做，互不可见；步骤被接受时，
由代码把它的分支合并进任务目录检出的分支（每步一个合并提交，步骤里的提交保留），再派出依赖它的步骤。合不进去就是这一步
的失败，交给规划者决定重做、改计划还是放弃。

**不做：**

- 串行的步骤、单步任务、不是 git 仓库的工作目录、没开启的项目，行为不变（D2、D7）；
- 不推送，不切换用户的分支，不改写已有的提交：只在检出分支的末端追加（D4、D8）；
- 不处理多个任务共用同一个目录（同一项目的任务默认都在项目目录里跑）的问题，见第 8 节。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 |
|---|---|
| 任务目录 | 任务的 `work_dir` 来自建任务时的显式目录，否则用项目的默认目录（`projects._task_work_dir`，`resolve_work_dir` 要求绝对路径）。TaskRouter 存进 `tasks.work_dir`，规划者存为 `plan.work_dir`，第一轮时创建目录 |
| 规划者 | 常驻 Claude 的 cwd 就是 `plan.work_dir`；推荐提示词要求它只读、不改文件（#104 的 eval 统计「规划者改文件」） |
| Worker | `_dispatch_step` 在 `WORK_ITEM` 里带 `context.work_dir = plan.work_dir`；`WorkerPool._execute` 在这个目录里起运行时，`judge_result` 按这个目录检查报告里的 `changed_files` 是否存在 |
| 提交 | `WORK_CONTRACT` 写着「其他步骤可能同时在这个目录里工作：提交时只 `git add <自己的路径>`，不用 `git add -A`」（#104）；Worker 是否提交由步骤说明决定，提交就落在用户检出的分支上 |
| Reviewer | `_spawn_reviewer` 同样带 `context.work_dir = plan.work_dir`，在整个任务目录里验收；`REVIEW_CONTRACT` 要求它不改动成果 |
| 依赖 | 依赖它的步骤只在所依赖的步骤 `verified` 之后才就绪（`ready_steps`），它的 Worker 拿到所依赖步骤的报告摘要 |
| 并发 | `_dispatch_ready` 一次派出全部就绪步骤，同时运行多少个只由本任务的 Worker 上限决定：任务自己的，否则 Settings → Roles 的角色值，否则 16（#173 起并发只按任务算，项目级上限已删除） |
| 关卡步骤 | 用户关卡（#168）是计划里 `kind: "gate"` 的步骤：依赖做完时打开，状态是 `waiting_user`，不派 Worker；`_dispatch_ready` 先打开就绪的关卡，再把其余就绪步骤交给派出判定 |
| 角色 | 规划者定义的角色（#161）：按角色跑的 Worker、评审角色的 Reviewer 和内置的走同一条派出路径（`_dispatch_step`、`_spawn_reviewer`），`context.work_dir` 的来源相同 |
| 重启 | `_resume`：正在跑的步骤用同一次尝试重跑，评审中的换新 Reviewer，被打断的决策重做。任务重启分两种：重新规划（删掉步骤和计划）和从计划续跑（保留） |

全程没有任何 git 操作：Looper 不知道任务目录是不是仓库。

## 3. 概念

| 概念 | 定义 |
|---|---|
| 任务目录 | 任务的 `work_dir`，用户检出的那份仓库。规划者和不隔离的步骤在这里工作 |
| 目标分支 B | 任务目录检出的分支（`git symbolic-ref HEAD`）。检查点和合并提交都落在 B 上 |
| 隔离步骤 | 派出时被判定为需要隔离（D2）的步骤；它的 Worker 和 Reviewer 在它自己的 worktree 里工作 |
| 步骤工作区 | 一个隔离步骤的 worktree：一个目录、一个步骤分支、一个起点 S，以及它的状态（第 5 节） |
| 检查点提交 | 派出隔离步骤时任务目录有未提交的改动，Looper 先把它们提交到 B 上（D3） |
| 起点 S | 步骤分支的起点：派出时 B 的末端，有检查点时就是检查点提交 |
| 结果提交 W | 合回时步骤分支的末端：Worker 自己的提交，加上 Looper 替它提交的剩余改动 |
| 合并提交 X | Looper 生成的、父提交为 B 当时的末端和 W 的提交；每个合回的步骤一个 |
| 起始点 R | 任务派出第一个步骤时 B 的末端。撤销整个任务就回到这里（第 8 节） |

## 4. 决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 隔离只在这些前提都满足时启用：任务目录是 git 仓库（`git rev-parse --show-toplevel` 成功）、git ≥ 2.40、检出在分支上（不是游离 HEAD）、没有进行中的合并、变基、拣选或二分。不满足时这一轮不隔离、只派一个步骤，activity 写明原因 | worktree、检查点和合并都依赖 git；合并提交必须落在一个分支上；进行中的操作说明用户正在用这个仓库 |
| D2 | **派出时判定**：一个步骤派出的那一刻，任务里还有别的步骤在途（`dispatched`、`deciding`、等评审的 `completed`），或者同一轮还派出了别的步骤，它就隔离；否则在任务目录里跑。在任务目录里跑的步骤在途时，不再派出别的步骤，等它结束。关卡步骤（#168）不是工作步骤：从不进入判定、从不有工作区，打开的关卡（`waiting_user`）也不算在途，所以关卡等用户时独自开始的步骤照样在任务目录里跑 | 串行和单步任务完全不变；判定只看此刻的状态，不靠字符串和猜测。后一条只在「聊天加步骤」或改计划时才会碰到（链式计划里，一个步骤独自在途时不会有别的步骤就绪）。关卡不改文件，没有要隔离的东西 |
| D3 | **起点必须是 B 上的提交。** 派出隔离步骤时任务目录有未提交的改动（已跟踪文件的修改、未被忽略的未跟踪文件），Looper 先把它们全部提交为检查点，S 就是检查点；没有就用 B 的末端。同一轮派出的步骤共用一个 S | 前面串行步骤的改动通常没提交，隔离步骤要在它们之上开工；合并也要求步骤分支和 B 有共同的祖先。备选「任务目录不干净就不隔离」会让串行步骤之后的并行几乎都退回串行；「从 `HEAD` 开、不管未提交的改动」会让隔离步骤看不到前面步骤的结果 |
| D4 | **合回 = 把步骤分支合并进 B，保留步骤里的提交，每步一个合并提交，不做快进。** 合并在内存里算（`git merge-tree --write-tree`），干净才用 `git commit-tree` 生成 X，再在任务目录里 `git merge --ff-only X`，把 B、索引和工作区一起前移 | 维护者决定保留提交（Q1）。每步一个合并提交，撤销一步就是 `git revert -m 1 X`，按步骤也列得出来。冲突在内存里发现，任务目录不会停在合并中途；`--ff-only` 在 B 被别人移动过、或任务目录里的未提交改动挡路时整体拒绝，什么都不改 |
| D5 | 合不进去 = 这一步的这次尝试失败，原因按类型（第 5 节的 `merge_error`）和文件写明，进入规划者的决策回合（重做、改计划、放弃）。之后的重试从 B 的当前末端新建工作区 | 复用现有的决策机制；任务目录只有「合并前」和「合并后」两种状态 |
| D6 | Reviewer 在步骤的 worktree 里验收，消息里附上看改动的命令 `git diff <S>`（包括未提交的部分） | 验收的是这一步自己的结果，不受其他步骤干扰；Reviewer 不改动成果，所以合回时提交的就是验收过的内容 |
| D7 | 项目执行配置新增 `step_workspaces`：`shared`（现行为）或 `worktree`。默认 `shared`，由项目自行开启；跑稳之后再改默认值（Q2） | 新机制先在明确开启的项目上跑；已有项目的行为不因升级而变 |
| D8 | **Looper 自己的提交**（检查点、剩余改动、合并提交）：作者和提交者都是 `Looper <looper@localhost>`，消息带 `Looper-Task: <task_id>`、`Looper-Step: <step_key>` 尾注；不签名（`--no-gpg-sign`）；不跑钩子（Looper 发出的每条 git 命令都加 `-c core.hooksPath=/dev/null`，并有超时）。Worker 自己的 `git commit` 照常用仓库配置的身份、签名和钩子 | 一眼看得出哪些提交是 Looper 生成的，按尾注列得出、撤得掉。这些提交只记录已经产生、已经验收过的内容：pre-commit 的格式化会让提交和验收过的内容不一致，失败的 lint 钩子会把已接受的步骤变成没人能修的失败，要终端或凭据的钩子和签名会卡住规划者 |

**备选（不采用）：**

- 合回成任务目录里的未提交改动（初稿的 D4）：不碰用户的历史，但丢掉步骤里的提交。维护者选了保留提交（Q1）。
- 在任务目录里直接 `git merge --no-ff looper/…`：冲突时任务目录停在合并中途，要靠 `--abort` 收拾；未提交的改动会和合并
  混在一起；还会跑合并钩子。
- 能快进时快进：第一个合回的步骤就没有合并提交，没法用一个提交撤销这一步，也不好按步骤列出。

**对用户分支 B 的影响：**

- 每一轮派出隔离步骤时，任务目录有未提交的改动，B 上就多一个检查点提交。**检查点包含任务目录里当时全部未提交、未被忽略的
  改动，不区分是前面的步骤写的还是用户自己写的**；只暂存了一部分的文件，暂存和没暂存的部分一起提交。
- 每个合回的隔离步骤留下：Worker 自己的提交、可能一个剩余改动提交、一个合并提交。没有改动的步骤什么都不留。
- 在任务目录里跑的步骤照旧：改动留在工作区，或由 Worker 自己提交。所以任务结束时 B 上有检查点和合并提交，工作区里还可能
  有之后串行步骤的未提交改动。
- Looper 只在 B 的末端追加，每次移动 B 都在 reflog 里留下记录。步骤分支在清理时删除，它们的提交通过合并提交留在 B 上。

## 5. 数据模型

```
step_workspaces                      一个隔离步骤的一个工作区
  id              BIGSERIAL
  task_id         → tasks ON DELETE CASCADE
  step_id         → task_steps.id ON DELETE CASCADE
  repo_dir        仓库顶层：任务目录，或任务目录所在的上级目录
  subdir          任务目录相对 repo_dir 的路径，'' 表示就是顶层；Worker 在 worktree 里的同一位置运行
  target_branch   B，如 refs/heads/main
  path            <runtime_root>/worktrees/<task_id>/<key>-<id>
  branch          looper/<task_id>/<key>-<id>      <key>：step_key 里 git 引用名不允许的字符换成 -
  base_commit     S
  checkpoint      S 是否为这次派出生成的检查点
  result_commit   W；合回之前为 NULL
  merge_commit    X；生成后、移动 B 之前写入
  state           'active' | 'merging' | 'merged' | 'unmerged' | 'discarded'
  merge_error     'conflict' | 'local_changes' | 'branch_changed' | 'step_branch_changed' | 'git_failed'
                  state = 'unmerged' 时必填，否则为 NULL
  detail          冲突或挡路的文件、git 的出错输出；可空
  removed_at      目录和分支已删除的时间；可空
  created_at, updated_at
  UNIQUE (step_id) WHERE state IN ('active', 'merging')   每个步骤同时只有一个在用的工作区
```

- 状态只在 `planner.py` 的一处推进，转换写成表（`StepWorkspaceState` 枚举 + `ALLOWED_TRANSITIONS`），非法转换抛错：
  `active → merging → merged`、`merging → unmerged`、`active | unmerged → discarded`。`merged`、`discarded` 是终态；
  `removed_at` 只记录清理，不是状态。
- `merge_error` 是枚举，规划者的决策回合拿到的是类型加文件，不靠解析 git 的输出：

  | 值 | 含义 |
  |---|---|
  | `conflict` | `merge-tree` 报冲突，`detail` 是冲突的文件 |
  | `local_changes` | 任务目录里的未提交改动或未跟踪文件挡住了 `--ff-only`，`detail` 是这些文件 |
  | `branch_changed` | 任务目录不再检出 B、B 不再包含 S（被重置或变基），或重算三次后 B 仍在被别人移动 |
  | `step_branch_changed` | worktree 不在步骤分支上，或 S 不是 W 的祖先（Worker 改写了步骤分支） |
  | `git_failed` | 其他 git 错误（锁被占用、超时），`detail` 是 git 的输出 |

- `PlanStep` 增加 `workspace: StepWorkspace | None`；`TaskPlan` 增加 `start: {"branch", "commit"} | None`，即 R，在项目为
  `worktree` 且任务目录满足 D1 时，于派出第一个步骤前记下。两者随计划一起读写（`db.plans`），重启后由 `_resume` 读回。
- `WORK_ITEM` 和 `VERIFY_REQUEST` 的 `context` 增加 `workspace`：`{"path", "branch", "base_commit", "target_branch"}`；
  `context.work_dir` 对隔离步骤就是 worktree 的路径，所以 `WorkerPool`、`judge_result`、运行记录的 `cwd` 不用改。

## 6. 流程

**派出**（`_dispatch_ready` / `_dispatch_step`）：

1. 按 D2 判定。不隔离的照旧。
2. 本轮要隔离时先查 D1 的前提；不满足就只派一个步骤到任务目录，activity 写明原因。
3. 任务目录有未提交的改动：`git add -A`，按 D8 `git commit`（`looper: checkpoint before parallel steps`），S 为新提交、
   `checkpoint` 为真；否则 S 为 B 的末端。
4. 每个步骤：`git worktree add -b <branch> <path> S`，写 `step_workspaces`（`active`），再发 `WORK_ITEM`。3、4 任何一步
   失败：这次尝试失败（「无法为步骤 s2 创建工作区：…」），进入决策回合，不退回任务目录去跑。

**Worker 的任务段**（代码追加，模型可见；实现用英文，大意如下）：「这一步在仓库的一份独立检出里做（分支 …），它从任务目录
分支 B 在本步开始时的提交建出；别的步骤同时做的改动这里看不到。可以在这里提交；没提交的改动，Looper 会在这一步被接受时
替你提交。被接受后这个分支会合并进 B，你的提交保留在 B 的历史里。不要推送，不要切换分支，不要改写这个分支上已有的提交。」

**接受**（决策 `accept`，或规划者没能决策时由 Worker/Reviewer 的结论判定为 `verified`）：

1. 状态 `active → merging`。
2. 在 worktree 里检查仍在步骤分支上、S 是它的祖先，否则 `step_branch_changed`。有未提交的改动就 `git add -A` 并按 D8 提交
   （`looper: commit what step s2 left uncommitted`），W 为步骤分支的末端。W 等于 S（这一步没有改动）时直接 `merged`，
   不生成提交。
3. 在任务目录检查仍检出 B、S 是 B 的祖先，否则 `branch_changed`。H 为 B 的末端；`git merge-tree --write-tree H W`（合并基由
   git 找出，就是 S 或更晚的提交）：冲突 → `unmerged`（`conflict`）；干净 → 树 M，`git commit-tree M -p H -p W`
   （`looper: merge step s2: <步骤标题>`）得到 X，写入 `merge_commit`。
4. 在任务目录 `git merge --ff-only X`：成功 → `merged`；被未提交改动挡住 → `local_changes`；B 已被移动、不能快进 → 回到 3
   重算，三次仍不行 → `branch_changed`。
5. 只有 `merged` 才把步骤记为 `verified`，再派出依赖它的步骤（它们按 D2 在任务目录或新的 worktree 里开工）。`unmerged` 时这次
   尝试改判为失败，写一条 `step_failed` 日志，结果写明类型和文件，进入决策回合。

在 `merging` 中途崩溃，重启后：X 已是 B 的祖先 → `merged`；否则从 2 重做（剩余改动已提交时 2 什么也不做；旧的 X 没有引用，
由 git 回收）。

**清理**（`git worktree remove --force`、`git branch -D`，写 `removed_at`；Q4）：

- 合回之后：立即删掉这个工作区的目录和分支，它的提交通过合并提交留在 B 上。
- 任务完成：删掉剩下的全部工作区和分支（包括合并失败后保留的旧分支）。
- 任务失败、停止、取消：已合回、已丢弃的删掉；没合回的（`active`、`unmerged`）保留目录和分支，用户可以去取半成品。
- 重启：重新规划先删掉这个任务的全部工作区，再删步骤；从计划续跑则保留，没验收的步骤在原工作区里继续（同 `_resume`）。
- 删除任务：全部删掉。
- 服务启动时扫一遍 `<runtime_root>/worktrees/`：没有对应的行、行已是 `merged` 或 `discarded`、或任务已删除的目录删掉；主仓库
  已不存在就只删目录、记日志。每个目录用 `git worktree remove --force <目录>` 连同它在仓库里的记录一起删（目录已经没了也行），不用 `git worktree prune`：它会顺带删掉用户自己那些目录不在了的 worktree 的记录。

## 7. 与其他功能的关系

| 功能 | 怎么处理 |
|---|---|
| 重试 | 一般的失败（验收没过等）复用同一个工作区，上一次的提交和文件都在（和现在在任务目录里重试一致）。`unmerged` 之后的重试：旧工作区标 `discarded`、删目录、留分支，从 B 的当前末端新建工作区（需要时先做检查点）；Worker 的任务段写明「上一次尝试的提交在分支 …，它和 B 在 a.py 上冲突：从那里取回改动并解决冲突」。旧分支在任务清理时删除 |
| 重启 | 见第 6 节「清理」 |
| 改计划 | 只替换未开始的步骤；被丢弃（`skipped`）的已开始步骤，工作区标 `discarded` 并清理 |
| 规划者自己改文件 | 规划者在任务目录里，按提示词不改文件。万一改了，它的改动是任务目录里的未提交改动：下一次检查点会提交它们，挡住合并时按 `local_changes` 处理 |
| 之后的串行步骤 | 在任务目录里、B 的最新末端上工作，看得到所有已合回的步骤 |
| Agent teams | 队友在 Worker 的进程里，cwd 相同，所以都在 worktree 里 |
| 规划者定义的角色（#161） | 按角色跑的 Worker 和评审角色的 Reviewer 都在步骤的 worktree 里，任务段的 worktree 说明不变；角色只换 prompt、harness 和 Profile，不换工作目录 |
| 用户关卡（#168） | 见 D2：关卡步骤没有工作区。关卡讨论里加的步骤（`add_steps`）和聊天加的一样，派出时按 D2 判定 |
| 规划者工具（#155） | 只改本任务的执行者、评审者 Profile 和 Worker、Reviewer 上限，不涉及工作目录；上限调高后同一轮能多派几个步骤，每个都按 D2 判定 |
| Codex、OMP | cwd 是 worktree 根目录，里面的 `.git` 是文件，两者都认作仓库根：Codex 的项目文档、OMP 的向上查找都止于 worktree 根 |
| 项目说明（#97） | 已跟踪的 `CLAUDE.md`、`AGENTS.md` 在 worktree 里；未跟踪的经检查点提交后也在；被忽略的没有。Claude Code 的 `claudeMdExcludes` 按 worktree 根计算，排除 runtime root 以上的目录 |
| 项目知识、skill | 不变；路径型知识文档指向任务目录里的文件，读到的是任务目录的版本 |
| 定时任务 | 每次触发是普通任务，按项目设置走。告警监控项目在 git 检出里并行处理每条告警、写不同的报告文件，是第一个开启的项目；开启后每次触发都会在它的检出分支上留下检查点和合并提交，这是实跑要观察的。它依赖被忽略的 `.venv`、`.env`，所以要等 I2 的链接清单 |
| 花费、并发上限 | 不变：worktree 不占 Worker 名额；上限只按任务算（#173） |

## 8. 失败、回滚与观测

| 风险 | 处理 |
|---|---|
| 游离 HEAD，进行中的合并、变基、拣选、二分 | 派出时：不隔离，一次一个步骤（D1）。合回时才出现（用户中途切走了）：`branch_changed` |
| 用户在任务进行中动了 B | 在 B 上新提交：合并时 H 就是新的末端，照常合并，冲突按 D5。重置或变基让 S 不在 B 上了、或切到别的分支：`branch_changed`，不会合到别的分支上 |
| 用户在任务目录里的未提交改动 | 和步骤不相干的原样保留；挡住合并的 → `local_changes`；下一次检查点会把它们一起提交（第 4 节） |
| 受保护的分支 | Looper 不推送；分支保护是远端在推送时的规则，本地无从得知。任务在 `main` 上跑，提交就落在本地 `main` 上，和今天 Worker 在 `shared` 模式下自己提交一样。推送前把提交挪到新分支（见下面的回滚）。不做本地的受保护分支清单：没有可靠的信息来源 |
| 钩子 | Looper 的命令不跑任何钩子（D8）：pre-commit、commit-msg、post-commit、post-checkout（`worktree add`）、post-merge（`--ff-only`）都不跑。代价是靠钩子维护的东西（如 LFS 的锁文件只读标记）不会在 Looper 的操作后触发。Worker 自己的提交照常跑钩子 |
| 签名、DCO | Looper 的提交不签名、不带 `Signed-off-by`。在推送时要求签名或 DCO 的仓库，这些提交推不上去：这类项目保持 `shared`，或者推送前由用户重写这些提交 |
| 被忽略的本地状态（`.env`、`.venv`、`node_modules`、构建缓存）在 worktree 里没有 | I2：项目设置 `step_workspace_links`，默认为空、不自动链接（Q3）。列出的路径在建 worktree 时以符号链接指回任务目录；提交剩余改动时把它们从暂存区去掉，Worker 提交了的也从结果里去掉。共享的链接意味着依赖安装仍会互相影响，这是有意的取舍。细则见第 11.1 节 |
| 每个 worktree 重新装依赖：慢、占盘 | 建之前估算检出大小（`HEAD` 的树里全部文件的大小之和）：超过 1 GiB 不隔离，一次只派一个步骤并在 activity 里说明（I2，第 11.2 节） |
| git 不可用、版本低于 2.40、命令超时 | 派出时：同 D1。合回时：`git_failed`。`looper doctor` 显示 git 版本，并逐个检查开启了 worktree 的项目的默认目录（第 11.4 节） |
| 多个任务共用一个目录 | 现状就有的问题，本 RFC 不解决，而且会更明显：一个任务的检查点会把另一个任务还没提交的改动一起提交。两个任务同时合并时，后到的那个 `--ff-only` 被拒绝，按第 6 节重算 |
| 子模块、LFS | `worktree add` 不初始化子模块，写进文档作为已知差距。LFS 的内容由过滤器处理、不靠钩子，检查点、worktree 和合并预计照常（未实测，I1 补一个用例） |
| 服务崩溃 | 运行中的步骤按 `_resume` 在同一个工作区里重跑；`merging` 中断的按第 6 节重做；孤儿目录由启动清扫回收 |

**回滚（用户怎么撤销）：** 以下命令都在临时仓库里试过。改动 B 的那几条先 `git switch <B>`：任务目录这时检出的未必是 B，在别的分支上直接 `git reset <R>` 会移动那个分支。

- 列出一个任务在 B 上的提交：`git log --first-parent --grep='^Looper-Task: <task_id>$' <B>`（检查点和合并提交；步骤里的
  提交在各合并提交的第二个父提交下面）。
- 撤销一个步骤：`git switch <B> && git revert -m 1 <这一步的合并提交>`。
- 撤销整个任务、保留改动：`git switch <B> && git reset <R>`。B 回到 R，任务的全部改动变回未提交的改动，和 `shared` 模式的结果一样；R 之后
  用户自己在 B 上的提交也会一起变回未提交的改动。连改动一起丢掉：`git switch <B> && git reset --hard <R>`（从没提交过的未跟踪文件不受影响）。
- 提交落在了不该落的分支上（比如 `main`）：`git switch -c <新分支> <B> && git branch -f <B> <R>`（新分支从 B 建，不从当前检出的分支建）。
- 关掉这个功能：项目设回 `shared`。

**观测：** activity 记录 R（「任务开始时 main 在 abc1234」）、每个检查点（短哈希、文件数）、每个工作区的建立（分支、S、
链接了哪些路径）、剩余改动提交、合并（X、文件数）、合不进去（类型、文件）、清理和清扫（按 Q4 保留了几个）；运行记录的 `cwd`
就是 worktree 路径。I2 起任务页的每个步骤显示工作区标记（分支、状态），合不进去的显示类型和文件，任务页给出 R 和上面的撤销
命令（第 11.3 节）。

## 9. 已决问题（维护者 2026-10-15 答复）

| # | 问题 | 结论 |
|---|---|---|
| Q1 | 合回的形式：未提交的改动，还是把步骤分支合并进用户检出的分支、保留提交 | **合并步骤分支、保留提交**，没有采用初稿建议的未提交改动。见 D3、D4、D8 和第 4 节「对用户分支 B 的影响」 |
| Q2 | git 目录的默认值：`worktree` 还是 `shared` | **I1 默认 `shared`，按项目开启**；跑稳之后再改默认值（I3） |
| Q3 | 被忽略的本地状态：项目配置链接清单，还是按常见名字自动链接 | **项目配置链接清单，默认为空，不自动链接**（I2） |
| Q4 | 停止或失败的任务里没合回的工作区，立即删除还是留着 | **留到任务被重启或删除**（第 6 节「清理」，I1） |

## 10. 分期

| 阶段 | 内容 | 依赖 |
|---|---|---|
| I1 | 项目设置 `step_workspaces`（默认 `shared`）；D1 的前提检查和「一次一个」的退路；D2 的派出判定；检查点、worktree、Worker 任务段、Reviewer 在 worktree 里验收；接受时提交剩余改动、生成合并提交、`--ff-only` 前移 B；`merge_error` 与决策回合；合不进去之后的重试新建工作区；`step_workspaces` 表、状态转换、`TaskPlan.start`；按 Q4 清理和启动清扫；activity 记录 | #104 |
| I2 | `step_workspace_links`（Q3）；检出大小上限；任务页的工作区标记、合不进去的类型和文件、R 和撤销命令；`looper doctor` 的 git 检查（第 11.1–11.4 节） | I1 |
| I3 | 任务页的「丢弃工作区」按钮（第 11.5 节，随 I2 一个 PR）；告警监控项目上跑稳（至少一周）后，默认值改为 `worktree`（Q2，第 11.6 节的门槛，单独一个 PR） | I2 |

**每期的测试：**

- **单元测试**（临时 git 仓库，仓库配置 `commit.gpgSign=true` 且 gpg 不可用，每个钩子都往标记文件里写一行）：
  - D1、D2 判定按表驱动：单步、链、两个独立加一个汇合、聊天加步骤时有不隔离的步骤在途、游离 HEAD、变基中；
  - 检查点：已跟踪的修改、未跟踪的文件、只暂存了一部分的文件都进提交，被忽略的不进，之后 `git status` 干净；身份和尾注；
    任务目录干净时不生成；
  - 合回：两个步骤各一个合并提交、父提交正确，Worker 自己的提交保留，剩余改动被提交，没有改动的步骤不留提交，任务目录里
    不相干的未提交改动和暂存原样保留；
  - 合不进去的每一类（`conflict`；`local_changes` 的已跟踪和未跟踪各一；`branch_changed` 的切分支、重置、算完后被移动；
    `step_branch_changed`）：之后任务目录的 `HEAD`、索引、工作区都和之前一样，没有 `MERGE_HEAD`；
  - 钩子标记文件为空、签名配置不影响 Looper 的提交；`merging` 中断后重做不产生新提交；
  - 第 8 节的撤销命令：`git revert -m 1 X` 只撤一步，`git reset R` 后任务的改动都成了未提交的改动；
  - 状态转换表（非法转换抛错），`merge_error` 在 `unmerged` 时必填。
- **PG：** `step_workspaces` 的写入与读回；`_resume` 后工作区还在；两种重启；按 Q4 保留；启动清扫只删该删的。
- **真实服务 + 替身 Claude：** 一次性 `looper serve`、新库、非 8767 端口、一次性 runtime root，任务目录是临时 git 仓库，
  项目设为 `worktree`：
  - 一个串行步骤，再两个并行步骤各写不同的文件，再一个依赖两者的步骤：B 上依次是检查点和两个合并提交，最后一步在任务
    目录里看到两份文件；
  - 两个并行步骤改同一行：后合回的那个 `conflict`，规划者的决策回合收到类型和文件；重试的 Worker 拿到旧分支名；
  - 停止任务后没合回的工作区还在，删除任务后清理掉；重启服务后启动清扫回收残留。
- **真实模型：** haiku 跑 `parallel_parts`、`interface_first`（项目设为 `worktree`），对比基线；Worker 任务段是模型可见的
  改动，按 `evals/README.md` 跑一次真实模式评测。I3 改默认值前，在告警监控项目上实跑一周。

## 11. I2 与 I3 的细化（2026-10-17）

I1 之后的几处改动（#155、#161、#168、#173）对这一期的影响已写进第 2、4、7 节；#172 的 Pipeline 节点卡片是放工作区标记的
地方（第 11.3 节）。

### 11.1 链接清单 `step_workspace_links`

- **设置：** 项目执行设置，路径的列表，相对任务目录；`null` 或空列表 = 不链接（默认，Q3）。最多 20 条，每条不超过 255 个
  字符；拒绝绝对路径、空的或 `.`、`..` 的路径段、任何一段叫 `.git` 的路径、重复的路径，以及一条在另一条里面（`a` 和
  `a/b`）。不合法时 400，写明哪一条、为什么。
- **建 worktree 时：** 对每条路径 P，在 worktree 里任务目录的位置建符号链接 P → 任务目录里的 P（绝对路径）。只在两者都成立
  时建：任务目录里有 P（文件、目录或链接都行）；worktree 里 P 还不存在，即 P 在 S 里没被跟踪（**不替换任何检出的文件**）。
  P 的上级目录在 worktree 里不存在就建出来；上级目录解析到 worktree 之外（经过一个被跟踪的符号链接）就不建。没建的写进
  activity（「没有链接 .venv：任务目录里没有」「没有链接 x：它在仓库里被跟踪」）。实际建了的记在工作区上
  （`step_workspaces.links`），worktree 丢失后重建时照这一份重新链接，之后改设置不影响已有的工作区。
- **合回时：** 提交剩余改动照旧 `git add -A`，再对**仍是链接的** P `git rm -r --cached --ignore-unmatch -- ':(literal)<P>'…`，
  然后只在暂存区有改动时提交。Worker 把链接换成了自己的文件的，那是它的改动，照常提交，activity 记一条。所以链接（以及 Worker 在 P 下面加的任何东西）**永远不是步骤结果的一部分**：Worker 自己 `git add -A` 提交了
  链接（`.gitignore` 写的是 `node_modules/` 这种只匹配目录的规则时，链接不被忽略），剩余改动提交会把它删掉，W 的树里没有 P，
  合并就不会碰任务目录里的 P。不这样做，`git merge --ff-only` 会用一个指向自己的链接覆盖任务目录里被忽略的 `node_modules`
  目录，连同里面的内容（在 git 2.55 上试过）。不用 `git add -A -- . ':(exclude)<P>'`：P 被忽略时 git 会因为路径规格点名了
  被忽略的路径而退出 1。
- **删除：** `git worktree remove --force` 和 `shutil.rmtree` 都只删链接本身、不跟进链接，任务目录里的内容不受影响（有测试）。
- **不告诉 Worker：** 链接对 Worker 就像任务目录里的同名文件，任务段不变（模型可见内容不变）。代价：Worker 往链接的 `.venv`
  里装包会改到任务目录的 `.venv`，这是 Q3 有意的取舍。

**备选（不采用）：** 只链接 git 会忽略的路径（链接不被忽略就不建）：安全，但 Node 项目常见的 `node_modules/` 规则会让链接
建不出来，要用户改 `.gitignore`；把链接写进仓库的 `info/exclude`：worktree 共用主仓库的这个文件，会改到用户的仓库；合回时
发现结果里有链接就判为合不进去：多一类 `merge_error`，而这一步的工作本身没有问题。

### 11.2 检出大小上限

`inspect` 在 D1 的前提都满足后，再算 `HEAD` 的树里全部文件的大小之和（`git ls-tree -r -z --format=%(objectsize) HEAD`，
按树的哈希缓存）。超过 1 GiB（`CHECKOUT_LIMIT_BYTES`）时这个目录不能有工作区：和 D1 不满足一样，这一轮一次只派一个步骤，
activity 写明「检出 1.3 GiB，超过步骤 worktree 的 1 GiB 上限」。上限先是常量；有项目需要调时再加项目设置。

### 11.3 任务页

- **接口：** `GET /api/tasks/:id` 增加 `step_workspaces`（没用过工作区的任务为 `null`）：`start`（R：`{branch, commit}`，
  没记下时为 `null`）、`workspaces`（这个任务的全部工作区，旧的在前：步骤、分支、目标分支、S、是否检查点、W、X、状态、
  `merge_error`、`merge_files`、`detail`、链接了的路径、目录、是否已删除）和 `undo`（撤销命令，见下）。
- **合不进去的文件：** 存进工作区的新列 `merge_files`（与 `merge_error` 一起写），不从 `detail` 里解析；一个步骤重试过几次，
  每个工作区各有各的。
- **撤销命令** 由代码按任务生成（`step_workspaces.undo_commands`，第 8 节那几条，分支名按 shell 转义），任务页原样显示，测试
  在临时仓库里逐条执行它们，也在检出了别的分支时执行：列出任务的提交；每个合回了的步骤一条
  `git switch <B> && git revert -m 1 <X>`；`git switch <B> && git reset <R>`（保留改动）和 `… git reset --hard <R>`（丢掉
  改动）；把提交挪到新分支 `git switch -c <B>-<task_id> <B> && git branch -f <B> <R>`（新分支不用 `looper/<task_id>`：它会
  和步骤分支 `looper/<task_id>/…` 的引用冲突）。没有 R 时只有前两类。任务页写明改动 B 的命令会先切到 B。
- **界面：** Overview 的 Steps 列表里，有工作区的步骤多一个标记（worktree：工作中、合并中、已合并、合不进去和类型、已丢弃）；
  Pipeline 节点卡片（#172）多一节 Workspace（分支、状态、合并提交或合不进去的类型和文件、之前的几个工作区）；Overview 另有
  一节 Step workspaces：R、每个工作区一行（步骤、分支、结果，合不进去的类型、文件和 git 的说明，还在的写出目录），以及可展开
  的撤销命令。

### 11.4 `looper doctor` 的 git 检查

- `git`：git 2.40 或更新为 ok（显示版本）；没有 git 或版本更低为 warn，写明开启了 worktree 的项目会一次只派一个步骤。
  `looper serve` 启动时也做这一项（`GET /api/status` 的 `checks`）。
- `step_workspaces`：逐个检查设为 `worktree` 的项目的默认目录（`inspect`：是不是仓库、在不在分支上、有没有进行中的操作、
  检出大小），以及链接清单里的路径在目录里有没有。没有这样的项目为 skip；都能用为 ok；有一个不能用为 warn，写明原因。
  只在 doctor 里做：项目设置随时会改，启动时的结果很快就旧了。

### 11.5 丢弃工作区（I3 的按钮，随 I2 一个 PR）

- `POST /api/tasks/:id/workspaces/:wid/discard`：任务不在进行中（`failed`、`stopped`、`cancelled` 等，与删除任务相同的
  判断）时，把一个还在的工作区（`active`、`unmerged`，或重试时留下分支的 `discarded`）标为 `discarded`，删掉它的目录和分支，
  写一条 activity。任务在进行中 → 409（「先停止任务」）；`merging`、`merged` 或已删除的 → 409；不存在 → 404。
- 丢弃期间锁住任务的行（`SELECT … FOR UPDATE`，直到目录和分支删完、行写完）：同时到来的重启要更新这一行，会等丢弃做完，
  新的规划者读到的就是已丢弃的工作区；重启先到的，丢弃看到任务已在进行中，409。
- 任务页 Step workspaces 一节里，每个可丢弃的工作区一个 Discard 按钮，行内确认（「丢弃 s2 的 worktree 和分支 looper/…？
  里面没合回的提交和改动都会丢掉」），焦点移到「取消」，取消后回到 Discard。
- 之后重启任务（`continue`）：这个步骤没有在用的工作区，按 D2 重新判定；重试的 Worker 只在上一次尝试的分支还在时才被告知它
  （派出时用 git 查一次），分支被丢弃了就不提。

### 11.6 默认值改为 `worktree` 的门槛（I3）

改默认值是一行（`ProjectExecution.isolate_steps` 在 `step_workspaces` 为 `null` 时也为真），但它改变所有没设过这一项的项目
在 git 检出里的行为（检查点和合并提交落在用户的分支上），所以单独一个 PR，满足下面几条再提：

- 告警监控项目设为 `worktree` 后至少跑满 7 天，期间至少 20 个任务真的建过工作区（activity 里有「works in a worktree of its
  own」）——只开设置不够：链式计划不会隔离任何步骤；
- 这些工作区的 `merge_error` 中，`git_failed`、`branch_changed`、`step_branch_changed` 为 0（它们说明机制本身的问题），
  `conflict` 和 `local_changes` 不超过合回次数的 5%，而且每一次都由规划者的决策回合处理掉、没有任务因此失败；
- 没有残留：任务完成后 `<runtime_root>/worktrees/` 下没有它的目录，`git branch --list 'looper/*'` 只剩停止或失败的任务保留的；
- 用户没有因为检查点或合并提交手动撤销过（第 8 节的命令）。

数据来源：`step_workspaces` 表（状态、`merge_error`）和任务的 activity，都能从只读接口读到。
