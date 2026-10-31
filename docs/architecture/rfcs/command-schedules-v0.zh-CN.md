# RFC：命令型定时任务 v0（`kind: command`，脱离 serve 运行、可接管、可告警）

- **状态：** 已批准（2026-10-30）。第 1.3 节列出的维护者决策不再讨论；第 13 节的三个问题按推荐答案执行，除非维护者另行指示。
- **需求：** 确定性脚本需要统一的周期调度、运行记录、日志和失败通知，既支持高频短任务，也支持持续数小时的长任务。命令型定时任务由 Looper 管理，不启动模型，并在服务重启后继续运行和接管，避免与系统调度器分别维护配置。
- **修订：** [定时任务 RFC](scheduled-tasks-v0.zh-CN.md) 的 D3「不做只跑命令的定时任务」改为本文的 D3'（第 1.3 节）。D1、D2、D4–D8 不变。
- **相关：** [热升级 RFC](hot-upgrade-v0.zh-CN.md) 第 7.3 节（排空）、第 7.10 节（实施拆分）；[开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 1 节（工作方式）；`docs/architecture/agent-system.md` 的「Schedules: tasks on a time rule」一节。
- **核对基线：** `main` `71380e44c`（v2.14.25）。本文只读了代码；另外做了三项一次性实测（第 5.5 节）：在 macOS 26（Darwin 25.5）上用一个测试 launchd 标签验证 `bootout` 和崩溃重启时哪些子进程活下来、launchd 的 `StandardOutPath` 被截断后是否追加写，以及 CPython 3.11 的 `asyncio` 子进程传输层在 `close()` 时会不会杀子进程、僵尸进程在 `ps` 和 `killpg` 下的表现。没有启动 `looper serve`，没有读任何运行中服务的数据库。
- **引用约定：** 路径相对仓库根，写成「文件:行」。`<runtime root>` 指 `looper serve --runtime-root` 的目录。
- **术语：**
  - **命令型定时任务**：`kind = 'command'` 的定时任务。到点直接执行一条 shell 命令，不建任务，不起 Planner 或 Worker，不花模型额度。
  - **agent 定时任务**：`kind = 'agent'`（默认值），就是今天的定时任务，行为不变。
  - **运行器（runner）**：本文新增的小进程。每次命令型运行一个，脱离 serve，替它持有命令进程组、写日志、执行超时，结束时写退出文件。
  - **接管（adopt）**：serve 重启后，按运行记录和运行目录里的文件重新认领还在跑或者已经跑完的命令型运行。
  - **中断（`interrupted`）**：运行器既不在运行、又没留下退出文件的运行。算一次失败。

## 1. 背景与目标

### 1.1 背景：现在为什么迁不进来

- **D3 明确否决了只跑命令的定时任务**（`docs/architecture/rfcs/scheduled-tasks-v0.zh-CN.md:81`）。当时的前提是「纯脚本的周期拉取继续用 launchd」。这种方式需要另行配置运行记录和失败通知，配置也和 Looper 分处两地，和 D5 的「只有一处事实」相冲突（同文件:83）。
- **D1 让每次触发都是一个完整任务**（同文件:79）。确定性命令不需要模型推理；包成 agent 任务会增加模型调用费用和启动延迟，高频触发时还会产生大量任务记录。
- **借前置检查跑脚本不可靠**：
  - 前置检查最长 600 秒（`looper/agents/scheduler.py:86`），长任务跑不完；
  - 前置检查只要有一行输出就建 agent 任务（`looper/agents/scheduler.py:769-782`）；
  - 前置检查不能和别的运行并发（同文件:529-545）；
  - 前置检查是 serve 的子进程，serve 重启时被记成 `gate_failed`（同文件:375-382、`looper/db/schedules.py:550-558`）；
  - 连续的跳过会合并成一行（`looper/db/schedules.py:344-360`），成功轮几乎不留痕。
- **serve 重启会打断子进程**：计划内升级或崩溃重启都可能发生在长任务运行期间。今天所有子进程都在 serve 停机时被结束（`looper/agents/serve.py:741-759`），或者在下次启动时被 `proc_tree.sweep` 结束（`looper/agents/proc_tree.py:369-435`、`looper/agents/serve.py:543`），因此命令需要脱离 serve 运行，并在重启后接管。
- **两个告警阈值是写死的**：连续失败 3 次（`looper/agents/schedule_notify.py:47`）；「长时间没成功」取 3 个周期，下限 15 分钟、上限 24 小时（同文件:48-53），从上次成功之后的第一次触发起算（同文件:80-94），日任务实际约 48 小时才报。错误摘要最多 300 字（同文件:54）。
- **serve 挂了没人知道**：调度器在 serve 进程里（D5），没有任何外部探活。

### 1.2 目标

- **G1** 按 cron 直接执行确定性命令，不建任务、不花模型额度。和 agent 定时任务共用配置、运行记录、界面、通知渠道和 `loopercli`。
- **G2** 单次运行可以持续数小时；`looper serve` 重启（计划内或崩溃）不打断它，重启后接管；接管不了的记成 `interrupted`，算失败，不会悄悄丢掉。
- **G3** 每次运行的完整输出落文件，可以在界面和 CLI 查看；运行记录、日志和 `serve.log` 都有保留上限。
- **G4** 失败、超时、中断、到点没成功、长时间卡住都能及时发到飞书，阈值可以按定时任务配置，同类告警在恢复前只发一次。
- **G5** serve 挂掉、卡死或调度循环停转时，由一个独立于 serve 的探活任务通过飞书告警，恢复时再发一条。
- **G6** 不加新依赖：仍然只依赖 PostgreSQL，不用 docker，单机。

验收用例见第 12 节。

### 1.3 维护者已定的决策（不再讨论）

| # | 决策 |
|---|---|
| **D3'**（修订 D3） | 新增 `kind: command`，默认 `kind: agent` 且行为不变。命令型不建 Planner 或 Worker，不花模型额度；配置、运行记录、界面、通知渠道、`loopercli` 与 agent 定时任务共用 |
| **K1** | 命令不随 `looper serve` 重启被杀，**现在**就做轻量脱离，不等热升级的看护进程：命令在自己的会话和进程组里运行，输出写日志文件，结束时把退出码写进文件；serve 重启后按运行记录接管：pid 还活着就是还在跑，有退出文件就是已结束，两者都没有就是 `interrupted`，算失败。以后和看护进程合流不在本期范围 |
| **K2** | `success_deadline` 和按定时任务配置的告警阈值（连续失败阈值、「长时间没成功」窗口、连续 `skipped_overlap` 阈值）是通用字段，两种定时任务都能用；agent 定时任务的默认值和今天完全一样 |
| **K3** | 远端执行（R8）不在本期范围。如果以后要做，形态是一个远端执行器，而不是同一个库上跑几个 serve |

维护者给出的默认做法，本文全部沿用：复用 `schedule_runs` 并加列；成功的命令型运行每次一行，跳过不合并；日志放在 `<runtime root>/logs/schedules/<schedule id>/<run id>.log`，单次 50 MB 上限、超出截断并注明，默认保留 30 天并设总大小上限；给 `schedule_runs` 加保留期，给 `serve.log` 加轮转；R9 做外部探活，serve 在健康检查接口里返回调度器最近一次 tick，探活任务随 `looper service install` 安装，不经过 serve，直接用已配置的飞书机器人发告警，绝不以用户身份发送；定时任务之间不做依赖。

### 1.4 不做

- 不改 agent 定时任务（D1）和前置检查的语义；agent 定时任务的运行消息、告警阈值和跳过合并都和今天一样（第 8.6 节列出两处纯加法的例外）。
- 不解析命令输出的业务含义：成败只看退出码和超时。
- 远端执行、主机亲和、多 serve 高可用（K3、第 14 节）。
- 定时任务之间的依赖。
- 停止单次正在跑的命令型运行的按钮（需要时用 `kill` 结束运行器，运行会记成 `interrupted`）；管家读取运行日志。两项记入计划第 5 节的非核心待办。
- `llm_records` 的保留期。它属于 agent 运行记录，另行处理。
- 和热升级看护进程合流（K1）。

## 2. 现状：可以复用什么，要改什么

| 部分 | 现状 | 本文 |
|---|---|---|
| 调度循环 | 15 秒一跳（`looper/agents/scheduler.py:76`、`409-427`），先结算运行中的，再启动排队的，再触发到期的，最后查「长时间没成功」 | 不变；结算里加命令型运行的接管，tick 末尾加「最晚成功时间」检查，记录最近一次 tick |
| 补跑 | `decide`（同文件:172-188）：一次准点；多次或迟到 2 分钟以上按 `catch_up` 处理 | 原样复用 |
| 重叠 | `skip`/`queue`/`parallel`（同文件:518-545）；「进行中」看任务状态（同文件:511-516） | 命令型的「进行中」看它自己的 `running` 运行 |
| 跳过合并 | 同一天同一原因的跳过记在最近一行上（`looper/db/schedules.py:344-360`、`looper/agents/scheduler.py:547-553`） | 命令型不合并，每次触发一行 |
| 每日花费 | `daily_budget_usd`（`looper/agents/scheduler.py:574-583`） | 命令型不检查，也不会出现 `skipped_budget` |
| 环境变量 | `child_env.inherited()` 加上 `gate_env`（`looper/agents/scheduler.py:205-211`、`looper/child_env.py:65-68`） | 命令型同样注入，另加 `LOOPER_SCHEDULE_MISSED` 等（第 6.4 节） |
| 运行记录 | `schedule_runs`（`looper/db/schema.sql:1085-1107`），终态只写一次（`looper/db/schedules.py:429-448`） | 加列；新增状态 `succeeded`、`interrupted` |
| 告警 | 连续失败、长时间没成功，各在恢复前只发一次（`looper/agents/schedule_notify.py:154-207`、`looper/db/schedules.py:561-576`） | 阈值可配；新增「最晚成功时间」和「连续跳过」两类告警 |
| 子进程 | 前置检查经 `proc_tree.spawn` 启动（`looper/agents/schedule_gate.py:109-113`），记录在 `<runtime root>/.local/agent-processes/`，下次启动时被结束（`looper/agents/proc_tree.py:335-354`、`369-435`） | 运行器**不经过** `proc_tree.spawn`，也不进记录目录（第 5.2 节） |
| 服务托管 | launchd `KeepAlive`，systemd `Restart=on-failure`（`looper/agents/service.py:186-228`） | systemd 单元加 `KillMode=process`；随安装加探活任务（第 10 节） |
| `serve.log` | launchd/systemd 追加写到 `<runtime root>/logs/serve.log`（`looper/agents/service.py:53`、`195-197`、`224-225`），没有轮转 | 复制后截断（第 7.4 节） |

## 3. 方案取舍

### 3.1 命令进程怎么脱离 serve

| | A. 每次运行一个运行器进程（推荐） | B. 交给 launchd/systemd 的临时单元 | C. 等热升级的看护进程 |
|---|---|---|---|
| 做法 | serve 用 `subprocess.Popen(start_new_session=True)` 起一个只用标准库的 Python 运行器；运行器再把命令起在它自己的进程组里，写日志和退出文件 | macOS 用 `launchctl submit` 或临时 plist，Linux 用 `systemd-run --user --unit` | 看护进程持有命令进程，协议 1.0 |
| 跨重启 | 运行器在自己的会话里，launchd `bootout` 和崩溃重启都不会杀它（第 5.5 节实测） | 天然 | 看护进程交付之后 |
| 平台差异 | 只有 systemd 要 `KillMode=process` | 两套完全不同的托管 API，状态查询、日志、退出码都要分别适配 | 同 A |
| 复杂度 | 低：约 250 行运行器，加一个 serve 侧的启动与检查模块 | 中到高 | 高，且要等看护进程交付（1.5–2 周） |
| 故障半径 | 一个运行 | 一个运行 | 一个运行 |

推荐 A：维护者要求「现在就做」（K1）；A 只依赖 POSIX 会话和进程组，两个平台的行为一致，接管逻辑只读文件和 pid。B 的两套托管 API 差异太大。C 不满足 K1。

### 3.2 运行记录放在哪

| | 复用 `schedule_runs` 加列（推荐，维护者默认） | 单独建 `command_runs` 表 |
|---|---|---|
| 界面、通知、计数 | 原样复用：`finish_run`、`count_outcome`、`Notifier.run_ended`、运行列表和 `loopercli` | 每个读者都要合并两张表 |
| 代价 | 十来个只对命令型有值的可空列 | 重复的状态机 |

### 3.3 成功状态叫什么

用新的 `succeeded`，不复用 `completed`。`completed` 的意思是「任务完成了」，命令型的成功是「退出码为 0」。代价是凡是把 `completed` 当成功的地方都要改成看一个集合：`OK`、`count_outcome`、`RAN`、状态文字、界面徽标。集合 `SUCCESS = ("completed", "succeeded")` 只定义一处（第 4.3 节）。

### 3.4 排空期间命令型触发放不放行

推荐**放行**（第 11.1 节）。理由：命令型运行跨重启存活，排空不需要等它；它不花模型额度；在排空期间暂停触发会让高频命令错过运行时机。代码被换掉的风险由「运行器按内容哈希单独落盘」消除（第 5.2 节）。

## 4. 数据模型

### 4.1 `schedules` 新增列

新列都追加在 `CREATE TABLE schedules` 的末尾（`looper/db/schema.sql:1008-1061`），迁移用 `ALTER TABLE ... ADD COLUMN` 加到同一位置，这样迁移后的库和新库结构一致（`tests/bridge_schema_support.py:170` 的比较不看列序，但要求约束同名同义）。

| 列 | 类型与约束 | 含义 |
|---|---|---|
| `kind` | `TEXT NOT NULL DEFAULT 'agent' CHECK (kind IN ('agent', 'command'))` | 创建后不能改 |
| `command` | `TEXT` | `/bin/sh -c` 执行的命令；代码限制 1–2000 字符（沿用 `GATE_COMMAND_MAX_CHARS`，`looper/agents/schedules.py:61`），不能含 NUL |
| `env` | `JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(env) = 'object')` | 追加或覆盖到继承环境上的变量；**不是放秘密的地方**，界面和接口照原样显示 |
| `env_file` | `TEXT` | 本机文件路径（可以 `~/` 开头），运行时由运行器读取；配置里只存路径 |
| `success_deadline` | `TEXT CHECK (success_deadline ~ '^([01][0-9]\|2[0-3]):[0-5][0-9]$')` | 最晚成功时间，`HH:MM`，按定时任务的时区；两种定时任务都能用（K2） |
| `alert_failures_after` | `INTEGER CHECK (alert_failures_after BETWEEN 1 AND 1000)` | 连续失败几次发告警；NULL 用默认值 3（`FAILURES_ALERT_AT`） |
| `alert_stale_after_seconds` | `INTEGER CHECK (alert_stale_after_seconds BETWEEN 300 AND 2592000)` | 「长时间没成功」的窗口；NULL 用今天的公式（3 个周期，15 分钟到 24 小时） |
| `alert_overlap_after` | `INTEGER CHECK (alert_overlap_after BETWEEN 0 AND 10000)` | 连续几次 `skipped_overlap` 发告警，0 表示不发；NULL 用按类型的默认值：命令型 10，agent 0（今天没有这条告警，所以默认关闭） |
| `consecutive_overlaps` | `INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_overlaps >= 0)` | 计数：每次 `skipped_overlap` 加一，真正开始一次运行时清零 |
| `deadline_alert_at` | `TIMESTAMPTZ` | 「到点没成功」告警开始的时刻，恢复时清空 |
| `overlap_alert_at` | `TIMESTAMPTZ` | 「连续跳过」告警开始的时刻，恢复时清空 |

一条具名表级约束固定字段归属，保证库里不会出现「半个命令型」的定时任务：

```sql
CONSTRAINT schedules_kind_fields_check CHECK (
    (kind = 'command') = (command IS NOT NULL)
    AND (kind = 'command' OR (env = '{}'::jsonb AND env_file IS NULL))
    AND (kind = 'agent' OR (gate_command IS NULL AND template_key IS NULL)))
```

命令型的 `requirement` 存空串（列是 `NOT NULL`，`looper/db/schema.sql:1014`）。

### 4.2 `schedule_runs` 新增列与状态

| 列 | 类型 | 含义 |
|---|---|---|
| `started_at` | `TIMESTAMPTZ` | 命令真正开始的时刻（运行器写的）；agent 运行为 NULL |
| `exit_code` | `INTEGER` | 命令自己退出时的退出码；被信号结束时为 NULL |
| `exit_signal` | `INTEGER` | 结束命令的信号；正常退出时为 NULL |
| `log_path` | `TEXT` | 相对 `<runtime root>` 的日志路径，例如 `logs/schedules/sched-…/srun-….log` |
| `log_bytes` | `BIGINT` | 命令一共输出了多少字节（截断前） |
| `log_truncated` | `BOOLEAN NOT NULL DEFAULT false` | 是否到了单次上限 |
| `log_tail` | `TEXT` | 输出末尾最多 4000 字符，`env_file` 里的值已经替换掉（第 7.2 节） |
| `runner_pid` | `INTEGER` | 运行器的 pid |
| `runner_started` | `TEXT` | 运行器的启动时刻，`ps -o lstart=` 在 UTC 下的输出，和 `proc_tree` 认进程的方式一样（`looper/agents/proc_tree.py:291-298`），防止 pid 复用 |

`status` 的 CHECK（`looper/db/schema.sql:1092-1094`）加上 `'succeeded'` 和 `'interrupted'`，按原顺序追加在末尾。命令型运行的状态只用 `queued`、`running`、`succeeded`、`failed`、`timed_out`、`interrupted`、`skipped_overlap`、`skipped_missed`。补跑覆盖了几次错过的触发沿用已有的 `missed` 列（`looper/db/schema.sql:1091`）。

新增索引 `idx_schedule_runs_finished ON schedule_runs (finished_at) WHERE finished_at IS NOT NULL`，供保留期清理用。

### 4.3 `schedule_notifications` 与代码里的状态集合

`kind` 的 CHECK（`looper/db/schema.sql:1116-1117`）追加 `'deadline_alert'`、`'deadline_recovered'`、`'overlap_alert'`、`'overlap_recovered'`。

`looper/db/schedules.py:30-44` 的集合改成：

```python
SUCCESS = ("completed", "succeeded")               # 新增：成功，更新 last_success_at
FAILURES = ("failed", "timed_out", "gate_failed", "interrupted")
OK = (*SUCCESS, "skipped_gate")                    # 结束失败连击，更新 last_ok_at
UNFINISHED = ("failed", "timed_out", "gate_failed", "stopped", "cancelled", "interrupted")
```

`count_outcome`（同文件:451-463）里的 `status == "completed"` 改为 `status in SUCCESS`。`UNFINISHED` 只对有前置检查输出的运行起作用（同文件:503-516），命令型运行没有前置检查输出，所以加上 `interrupted` 对它没有影响，只是让集合说得完整。

### 4.4 迁移

沿用 `schedules.user_gates` 那次的做法（`looper/db/connection.py:69-89`、`186-189`、`214-220`）：

- `schema.sql` 写入新列和新约束，新库直接带上；新库只记 `post_bridge_baseline`。
- `ensure_schema` 里加一个有名字的一次性迁移 `command_schedules`：库里没有这个标记、而且 `schedules.kind` 列不存在时，在同一个事务里执行全部 `ALTER`（加列、加具名约束、对 `schedule_runs_status_check` 和 `schedule_notifications_kind_check` 先 `DROP CONSTRAINT` 再按新列表 `ADD CONSTRAINT` 同名约束、建索引），然后写入标记。
- `schema_change_pending` 把它算进去，serve 会先用 `pg_dump` 备份再迁移（`looper/agents/serve.py:325-379`）。
- `REGISTRY_SCHEMA_VERSION` 不变（纯加法）。和今天一样不支持降级：旧版本 serve 会把命令型的 `running` 运行当成「任务被删了」记成 `stopped`（`looper/agents/scheduler.py:864-868`）。

## 5. 执行

### 5.1 一次触发怎么走

`_fire`（`looper/agents/scheduler.py:518-545`）的重叠检查之后，`_launch`（同文件:569-598）先按类型分支。命令型：

1. 不检查每日花费，不走前置检查。
2. 解析工作目录：`work_dir` 为空用项目的 `work_dir`（复用 `effective_schedule` 的目录和归属检查，`looper/agents/schedule_templates.py:68-108`）。归属的管家不存在、被停用，或者没有目录，就把这次运行记成 `failed` 并写明原因，和 agent 定时任务一样（`looper/agents/scheduler.py:524-525`、`572-573`）。
3. 在触发的事务里写运行：`status='running'`、`fired_at=now`、`run_timeout_seconds`、`log_path`；`consecutive_overlaps` 清零（如果「连续跳过」告警开着，提交后发恢复消息）。
4. 事务提交之后，`_follow`（同文件:501-509）调用运行启动：在 `asyncio.to_thread` 里写运行目录和 `spec.json`，用 `subprocess.Popen` 启动运行器，再把 `runner_pid`、`runner_started` 写回运行记录。启动失败（`OSError`）就把运行记成 `failed`：「运行器启动失败：…」。

为什么先提交再启动：如果反过来，提交失败时会留下一个没有记录的运行器。先提交时，serve 在提交和启动之间死掉，接管会看到「有记录、没有运行目录」，记成 `interrupted`（第 5.4 节），不会悄悄丢掉。

手动「立即运行」（`run_now`，同文件:488-499）、排队运行（同文件:804-821）走同一条路。

### 5.2 运行器

**代码只用标准库，按内容落盘。** 运行器的源码在 `looper/agents/command_runner.py`，只允许导入标准库，顶层一次导入完，不在函数里导入。调度器启动时读一次这个文件，写到 `<runtime root>/.local/command-runner/runner-<内容 sha256 前 12 位>.py`（0600；同名文件已存在且内容相同就不写），之后每次运行都用 `sys.executable -I <这个文件> <运行目录>` 启动。这样：

- 部署时 `git merge --ff-only` 或 `pip install` 换掉磁盘上的代码，正在启动的运行器不会读到一半新一半旧的模块（热升级 RFC 第 4.5 节说的问题）；
- `-I` 让运行器不受 `PYTHONPATH` 和用户 site 目录影响；命令本身仍然拿到继承的 `PYTHONPATH`；
- 运行器的代码改了，已经在跑的运行继续用旧代码跑完，新运行用新代码，不需要任何动作。

**只能用 `subprocess.Popen` 启动，不能用 `asyncio.create_subprocess_exec` 或 `proc_tree.spawn`。** 两条理由：

- `asyncio` 的子进程传输层在 `close()` 时会对还没退出的子进程发 SIGKILL（CPython 3.11 `asyncio/base_subprocess.py` 的 `BaseSubprocessTransport.close`，第 5.5 节实测）。serve 停机或事件循环关闭时，这条路径会杀掉运行器。
- `proc_tree.spawn` 会把进程记进 `<runtime root>/.local/agent-processes/`，下一个 serve 启动时 `sweep` 会因为「记它的那个 serve 已经不在了」把它结束（`looper/agents/proc_tree.py:401-425`）。

serve 侧的 `Popen` 参数：`start_new_session=True`、`stdin=DEVNULL`、`stdout` 和 `stderr` 都接运行目录里的 `runner.log`、`cwd=<运行目录>`、`env=child_env.inherited()`、`close_fds=True`。serve 把 `Popen` 对象留在内存里，每次 tick 调一次 `poll()`，把退出的运行器回收掉。serve 重启之后，运行器的父进程变成 launchd 或 init，由它们回收。不要在 serve 里调 `waitpid(-1)`。

**运行目录** `<runtime root>/.local/command-runs/<run id>/`（0700），和看护进程的 `.local/runs/` 分开：

| 文件 | 谁写 | 内容 |
|---|---|---|
| `spec.json` | serve，启动运行器之前 | `{format: 1, run_id, schedule_id, command, work_dir, env, env_file, timeout_seconds, kill_grace_seconds: 30, log_path, log_max_bytes, schedule_env}`；不含 `env_file` 的内容 |
| `runner.json` | 运行器，命令启动之后 | `{format: 1, runner_pid, runner_started, child_pid, child_started, started_at}` |
| `exit.json` | 运行器，结束时原子替换（写临时文件、`fsync`、`rename`、`fsync` 目录） | `{format: 1, end: exited\|timed_out\|runner_terminated\|start_failed\|runner_error, exit_code, signal, started_at, ended_at, log_bytes, log_dropped_bytes, log_truncated, tail, error}` |
| `runner.log` | 运行器的 stdout/stderr | 运行器自己的诊断，不含变量值 |

运行结束、serve 把结果写进库之后，删除运行目录，日志文件留下。

**运行器做什么：**

1. 读 `spec.json`；装 SIGTERM、SIGINT、SIGHUP 的处理函数（只记下收到了哪个信号）；忽略 SIGPIPE。
2. 建环境（第 6.4 节）：读 `env_file` 失败或格式不对，写 `exit.json`（`end=start_failed`，`error` 只写行号和原因，不写值）后退出。
3. 检查工作目录是不是目录，不是就 `start_failed`。
4. 打开日志文件（0600，父目录 0700，`O_TRUNC`），用 `Popen(["/bin/sh", "-c", command], cwd=work_dir, env=..., stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, start_new_session=True)` 启动命令。stdout 和 stderr 合并在一个管道里，保持交错顺序。命令在自己的会话和进程组里，进程组 id 就是 shell 的 pid。启动失败就 `start_failed`。
5. 写 `runner.json`。
6. 主循环：用 `select` 每次最多等 1 秒，读到的字节原样写进日志。到了 `log_max_bytes`（50 MiB）就不再写，只把之后的字节放进一个 1 MiB 的环形缓冲区，并计数。同时检查三件事：
   - **超时**（`timeout_seconds` 不为空）：按墙钟算，`now >= started_at + timeout_seconds` 时，对命令的进程组发 SIGTERM，`end=timed_out`；30 秒（`kill_grace_seconds`）后进程组里还有进程就发 SIGKILL。用墙钟是为了和 serve 侧、agent 运行的超时算法一致（`looper/agents/scheduler.py:874`），电脑睡眠的时间也算在内。
   - **运行器收到了终止信号**（关机、注销、有人 `kill` 了它）：把同一个信号转发给进程组，等最多 30 秒，然后 `end=runner_terminated`。
   - **命令退出**：先回收 shell（`Popen.poll()`），再判断进程组里还有没有活着的进程（第 5.5 节：macOS 上只剩僵尸的进程组 `killpg(pgid, 0)` 会报 EPERM，所以用 `ps -A -o pid=,pgid=,stat=` 判断，状态以 Z 开头的不算）。还有留下的进程，就像 launchd 在任务退出时那样结束它们：SIGTERM，5 秒后 SIGKILL。
7. 命令和留下的进程都结束后，再最多等 2 秒把管道读完（有进程逃出了进程组、还握着管道时不无限等）。
8. 截断过的日志，在末尾追加一行说明（英文、纯 ASCII，和 Looper 其余的日志行一致）和环形缓冲区里的内容：`\n[looper] log truncated at 50 MiB: the run wrote N bytes; M bytes in the middle were dropped; the last K bytes follow\n`。没截断的日志一个字节都不加，大小和命令的输出完全一致（R3 验收）。
9. 取日志末尾最多 4000 字符（按 UTF-8 解码，坏字节替换），把 `env_file` 里长度不少于 4 的值替换成 `[redacted]`，写进 `exit.json` 的 `tail`；`fsync` 日志，原子写 `exit.json`，退出 0。
10. 运行器自己出了没预料到的异常：结束命令的进程组，尽量写 `exit.json`（`end=runner_error`，`error` 只写异常类型和一行消息），退出 1。

**进程组之外的进程：** 命令里再调 `setsid` 或 `nohup ... &` 逃出进程组的进程，运行器不追。这和 launchd 今天的行为一致：launchd 也只结束任务的进程组（第 5.5 节）。

### 5.3 结束状态的映射

只在一个函数里定义（`command_runs.run_status`）。`error` 和已有的运行错误一样写英文（如 `looper/agents/scheduler.py:879`），通知正文的其余部分是中文：

| `exit.json` | 运行状态 | `error` |
|---|---|---|
| `end=exited`，`exit_code=0` | `succeeded` | — |
| `end=exited`，`exit_code≠0` | `failed` | `the command exited with status N` |
| `end=exited`，被信号结束 | `failed` | `the command was ended by signal N` |
| `end=timed_out` | `timed_out` | `stopped after the run timeout of N s (its process group was ended)` |
| `end=runner_terminated` | `interrupted` | `the runner received signal N (shutdown, logout or a manual kill)` |
| `end=start_failed` | `failed` | 运行器给的原因，例如 `work_dir … is not a directory`、`env_file …: line 3 is not KEY=VALUE` |
| `end=runner_error` | `interrupted` | `the runner failed: …` |
| 没有 `exit.json`，运行器已不在 | `interrupted` | 见第 5.4 节 |

`finished_at` 取 `exit.json` 的 `ended_at`（不晚于现在），不取 serve 发现它结束的时刻；`count_outcome` 也用这个时刻。耗时就是 `finished_at - started_at`。

### 5.4 结算与接管

每次 tick 的 `_settle_running`（`looper/agents/scheduler.py:853-860`）对每条 `running` 的运行按类型分支。命令型运行的检查（`command_runs.inspect`）只读文件和进程表，返回一个有类型的结果：

1. **`exit.json` 存在且能读** → `Finished`：按第 5.3 节写终态（`finish_run`，只写一次），删掉运行目录。
2. **运行器还活着** → `Running`。「活着」= `runner_pid`（库里没有就读 `runner.json`）的 `ps -o stat=,lstart=` 输出里，启动时刻等于记录的 `runner_started`，而且状态不是僵尸（第 5.5 节：僵尸在 `ps` 里仍然显示原来的启动时刻）。一次 tick 里所有运行的 pid 合成一次 `ps` 调用。
   - **兜底超时**：设了超时、而且 `now > started_at + timeout + 30 s + 60 s` 仍在运行，说明运行器没能按期结束它（运行器卡住或者被 `SIGSTOP`）。serve 按 `runner.json` 里的 `child_pid`/`child_started` 核对身份后，对命令进程组和运行器发 SIGKILL，记 `timed_out`，`error` 写 `the runner did not end the run at its timeout; Looper killed its process group`。
3. **运行器不在了**：再读一次 `exit.json`（运行器可能刚写完就退出了）。还是没有：
   - 没有 `runner.json`，而且运行建出来还不到 60 秒 → `Starting`，下一跳再看；
   - 否则 → `Lost`：按 `runner.json` 核对身份后结束命令进程组里还活着的进程，记 `interrupted`，`error` 写清楚是哪种情况：`the server stopped before the command started`（没有运行目录或 `spec.json`）或 `its runner (pid N) ended without recording an exit code`。

**这就是重启后的接管**：新 serve 不需要单独的接管步骤，第一跳的结算就会对上一个 serve 留下的每条 `running` 运行做上面的检查。结算在触发之前（同文件:419-427），所以重叠检查看到的已经是接管之后的状态。

**启动时不能碰命令型运行的地方：**

- `proc_tree.sweep`（`looper/agents/serve.py:539-545`）：运行器不在记录目录里，`sweep` 只处理记录过的进程树（`looper/agents/proc_tree.py:392-435`），所以不会碰它。实现时加一条回归测试钉住这一点。
- `Scheduler.start` 里的 `fail_gating_runs`（`looper/agents/scheduler.py:375-382`）只处理 `gating`，命令型运行不会是这个状态。
- `Scheduler.stop`（同文件:384-394）只取消后台协程。命令型运行没有任何协程持有它的进程。正在 `to_thread` 里启动的运行器被取消时，线程照样把 `Popen` 跑完；pid 没写回库也没关系，接管时从 `runner.json` 读。
- 停机期限 15 秒（`looper/agents/serve.py:416`）里不为命令型运行做任何事。

**删除和停用：** 命令型定时任务有 `running` 运行时拒绝删除（409），提示先停用并等待运行结束，避免删除记录后留下无人接管的命令。检查与删除持有触发使用的同一把定时任务行锁；没有进行中的命令时，删除会级联删掉运行记录（`looper/db/schema.sql:1087`）。agent 定时任务仍可在运行中删除，它建出的任务继续运行。停用只是不再触发，正在跑的运行照常结束。

### 5.5 实测结论

| 问题 | 结论 | 怎么测的 |
|---|---|---|
| launchd `bootout` 时谁活下来 | 和任务同一进程组的子进程被结束；`start_new_session=True` 起的子进程（以及它在自己进程组里起的孙进程）活下来，父进程变成 launchd（pid 1）。plist 没有设 `AbandonProcessGroup`（默认 false），和 `looper/agents/service.py:186-198` 生成的一样 | 测试标签 `dev.looper-design-probe.parent`，用完已 `bootout` 并清理进程 |
| 任务崩溃（`kill -9` 主进程）后 launchd 按 `KeepAlive` 重启时谁活下来 | 同上：同进程组的被结束，独立会话的活下来；新实例照常起来 | 同上 |
| launchd 的 `StandardOutPath` 被截断后 | 下一次写入从文件开头追加，没有空洞，说明是追加写打开的；所以「复制后截断」可以用来轮转 `serve.log` | 测试标签 `dev.looper-design-probe.writer` |
| `asyncio` 子进程会不会被杀 | 丢掉引用不会立刻杀，但显式 `transport.close()` 会对还在跑的子进程发 SIGKILL；`subprocess.Popen` 不会 | CPython 3.11.15 一次性脚本 |
| 僵尸进程 | `ps -o stat=,lstart=` 对僵尸照样输出原来的启动时刻，状态是 `Z`；只剩僵尸的进程组 `killpg(pgid, 0)` 在 macOS 上报 EPERM，回收之后报 ESRCH | 同上 |
| systemd | 默认 `KillMode=control-group` 会结束单元 cgroup 里的全部进程，不管它们在哪个会话（systemd.kill(5)）；需要 `KillMode=process`。**没有在 Linux 上实测**，纳入真实链路验证 | 文档 |

## 6. 重叠、补跑和环境

### 6.1 重叠

- 命令型的「进行中」= 这个定时任务有 `running` 的运行（`open_runs`，`looper/db/schedules.py:306-312`）。`_busy_task`（`looper/agents/scheduler.py:511-516`）只用于 agent 定时任务。
- `skip`（命令型默认）：记 `skipped_overlap`，不算失败、不通知；`consecutive_overlaps` 加一。
- `queue`：最多排一个，上一个结束后由下一跳的 `_start_queued` 启动（同文件:424-425、804-821）。
- `parallel`：最多 `max_parallel_runs` 个同时运行；命令型没有前置检查，所以没有「前置检查不并发」带来的跳过。
- **命令型不合并跳过**：`_skip`（同文件:547-553）对命令型直接插一行，不调 `count_skip`。每分钟的定时任务卡住时一小时多 60 行，由保留期兜底（第 7.3 节）。

### 6.2 补跑

完全沿用 `decide`。每分钟的流水线用 `catch_up: skip`（脚本自己按水位线补拉），日任务用 `once`。服务停了或电脑睡了 2 小时，日任务醒来补跑一次，`missed` 记错过的次数。

### 6.3 最小间隔和超时

- 最小间隔：命令型和有前置检查的一样，`min_interval_seconds(gated=True)`（`looper/agents/scheduler_settings.py:55-59`），即 1 分钟。`checked_rule` 的 `gated` 参数改为「有前置检查或是命令型」（`looper/agents/schedules.py:115-135`、`455-461`）；预览接口的 `gated` 由界面按类型传。
- 运行超时：命令型接受秒数（1 秒到 30 天，`looper/agents/scheduler_settings.py:28`）或 `null`（不限时，命令型的默认值）。**不接受 `"default"`**：系统默认超时（Settings → Scheduler）是为 agent 运行设的，命令型继承它会让人意外。

### 6.4 环境变量

命令的环境按下面的顺序合成，后面的覆盖前面的：

1. `child_env.inherited()`（`looper/child_env.py:65-68`）：PATH、HOME、LANG、代理等白名单变量，和前置检查一样；
2. `env_file` 里的变量；
3. `env` 里的变量（和 docker compose 的 `environment` 覆盖 `env_file` 一致）；
4. Looper 注入的 `LOOPER_SCHEDULE_*`，不能被覆盖：

| 变量 | 值 |
|---|---|
| `LOOPER_SCHEDULE_ID` | 定时任务 id |
| `LOOPER_SCHEDULE_RUN_ID` | 运行 id |
| `LOOPER_SCHEDULE_SCHEDULED_FOR` | 这次应触发的时刻，ISO 8601 |
| `LOOPER_SCHEDULE_RUN_TIMEOUT_SECONDS` | 这次运行的超时秒数，不限时为空串 |
| `LOOPER_SCHEDULE_TRIGGER` | `scheduled`、`catch_up` 或 `manual` |
| `LOOPER_SCHEDULE_MISSED` | 这次运行覆盖了几次错过的触发（`Firing.missed`，`looper/agents/scheduler.py:158-163`），准点为 `0` |
| `LOOPER_SCHEDULE_MISSED_SINCE` | 第一次错过的时刻，ISO 8601；没有错过为空串 |

`gate_env`（同文件:205-211）改成生成这一整组变量，前置检查也会多拿到后三个。这是一处纯加法的变化（第 8.6 节）。

**`env` 的校验：** 最多 64 个键；键匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，不能以 `LOOPER_SCHEDULE_` 开头；值是字符串，最多 4096 字符，不能含 NUL。值照原样使用，不展开 `$VAR` 或 `~`：要追加 PATH，就写完整的 PATH。

**`env_file`：**

- 保存时：`~/` 展开后必须是绝对路径、存在、是普通文件、serve 的用户能读，最多 1024 字符；库里存用户写的原样。
- 运行时由运行器读取（所以文件改了下一次运行就生效），格式是 dotenv 的一个子集：空行和 `#` 开头的行跳过；其余每行是 `KEY=VALUE`，可以有 `export ` 前缀；键的规则同 `env`；值去掉一对首尾匹配的单引号或双引号，其余照原样，不展开变量、不支持多行。
- 文件读不了或有一行不合格式：这次运行 `failed`（`start_failed`），错误只写路径、行号和原因。
- **不回显：** `env_file` 的内容不进库、不进 `spec.json`、不进 `runner.log`、不进通知；运行器在交给 serve 的日志末尾里把这些值替换掉（第 5.2 节第 9 步）。完整日志是命令自己的输出，Looper 不改它；脚本自己打印了秘密，秘密就在日志文件里（0600）。

以运行 `looper serve` 的同一个用户执行，能读到这个用户 HOME 下各个 CLI 的登录态（R6.4）。

## 7. 日志与保留

### 7.1 日志文件

- 路径 `<runtime root>/logs/schedules/<schedule id>/<run id>.log`，目录 0700，文件 0600。
- stdout 和 stderr 合并，原样字节。
- 单次上限 50 MiB（`LOG_MAX_BYTES`），超过之后保留开头 50 MiB 和最后 1 MiB，中间注明省略了多少（第 5.2 节第 8 步）。
- 运行记录里的 `log_path` 存相对路径，接口的 `log` 返回 `{path, bytes, truncated}`：`path` 是 runtime root 拼接该相对路径后的绝对路径。`loopercli schedule-admin runs` 的文本和 JSON 输出使用同一结构。

### 7.2 运行记录里的日志末尾

`log_tail` 存最多 4000 字符；飞书通知用其中最后 1500 字符（第 8.4 节）。

### 7.3 保留与清理（新模块 `looper/agents/housekeeping.py`）

调度器启动时跑一次，之后每小时一次，在后台跑，不挡 tick：

| 对象 | 规则 |
|---|---|
| `schedule_runs` | 删除 `finished_at` 早于保留期（默认 30 天）、而且不在本定时任务最新 100 条之内的运行；从不删除 `queued`、`gating`、`running`，或尚未交接非空前置检查输出的未完成运行（删除定时任务时一起删除）。分批，每批最多 5000 行 |
| `schedule_notifications` | 删除早于保留期的 |
| 命令日志 | 删除早于保留期的；`logs/schedules/` 总大小超过 2 GiB 时，从最旧的开始删，直到降到上限以下；从不删除仍是 `running` 的运行的日志 |
| 运行目录 `.local/command-runs/*` | 运行记录已经是终态或已不存在、而且运行器已不在的，删除 |
| 运行器文件 `.local/command-runner/runner-*.py` | 不是当前这份、而且超过 31 天的，删除（Python 已经把源码读进内存，删文件不影响还在跑的运行器） |
| `serve.log`、`probe.log` | 超过 20 MiB 时轮转：`.4` → `.5`，……，当前文件复制成 `.1`，再把当前文件截断为 0；保留 5 份。launchd 和 systemd 都是追加写（第 5.5 节），截断之后接着从头写；复制和截断之间写进来的几行会丢，可以接受 |

保留期是 Scheduler 设置里的新项 `scheduler.run_retention_days`（默认 30，1 到 3650），和已有的两项放在一起（`looper/agents/scheduler_settings.py:22-29`，Settings → Scheduler）。2 GiB 和 100 条先写成常量。

**这是一处默认行为的变化：** agent 定时任务的运行记录今天永久保留，本文之后也按保留期清理（每个定时任务至少留最新 100 条，它们建出的任务不受影响）。见第 13 节问题 2。

## 8. 告警与通知

### 8.1 阈值

由一个函数 `alert_policy(schedule)` 给出生效值，其余代码只读它：

| 阈值 | 列 | NULL 时 | agent 默认 | 命令型默认 |
|---|---|---|---|---|
| 连续失败几次告警 | `alert_failures_after` | 3 | 3（不变） | 3 |
| 「长时间没成功」窗口 | `alert_stale_after_seconds` | 今天的公式 | 公式（不变） | 公式 |
| 连续几次跳过告警 | `alert_overlap_after` | 按类型 | 0，不发（不变） | 10 |

设了 `alert_stale_after_seconds` 时，「长时间没成功」的截止时刻是「起算的那次触发 + 这个窗口」，不再乘周期，也不受 15 分钟和 24 小时的上下限约束（`stale_deadline`，`looper/agents/schedule_notify.py:80-94`）。起算点不变。

### 8.2 最晚成功时间（`success_deadline`）

设 D 为最近一个已经过去的 `HH:MM`（按定时任务时区），D' 为它前一个（把 `HH:MM` 写成一条 `MM HH * * *` 的 cron 规则，用 `cron.last_fire` 求，夏令时的处理和触发时间完全一样，`looper/agents/cron.py:187-200`）。每次 tick 对每个设了截止时间、已启用、而且「到点没成功」告警没开着的定时任务：

- 窗口 W = (max(D', `enabled_at`, `rule_changed_at`), D]；
- W 里按规则有至少一次触发（`cron.fires_between(..., limit=1)`）——这一天本来就不跑（比如只在工作日跑），就不告警；
- 而且 `last_ok_at` 不在 (D', now] 里——到现在为止，上一个截止时间之后还没有过一次成功（命令型是 `succeeded`，agent 是 `completed` 或前置检查没有新内容）；
- 两条都满足，就开始告警（`deadline_alert_at` 从 NULL 改成 now，只成功一次），发一条「到点没成功」。正文写上截止时间、上次成功的时刻、最近一次运行的结果；有运行还在跑时写「仍在运行中，自 HH:MM 起」。

**恢复：** 下一次成功的运行（`run_ended` 里 `status in OK`）清掉 `deadline_alert_at`，发一条恢复消息。没恢复之前，后面几天的截止时间都不再重复告警（R5.7）。

**改了截止时间**也会把 `rule_changed_at` 设成现在，所以在 09:00 把截止时间设成 08:00，不会当天马上告警。

示例：05:30 触发、08:00 截止、命令 `exit 1`：05:30 发一条失败通知（`notify=failures`），08:00 发一条「到点没成功」，修好之后「立即运行」成功，发一条恢复消息。连续失败的阈值是默认的 3，所以中间不会多一条「连续失败」。电脑在 05:30 睡着、09:00 醒来补跑：09:00 那一跳就会发「到点没成功」（截止时间确实错过了），补跑成功后发恢复消息。

### 8.3 连续跳过

`_fire` 记 `skipped_overlap` 时在同一事务里给 `consecutive_overlaps` 加一；达到 `alert_overlap_after`（大于 0）而且告警没开着，就开始告警，提交后发：「已连续 N 次因为上一次运行还没结束而跳过；上一次运行自 HH:MM 开始，可能卡住了」。下一次真正开始运行时清零，告警开着就发恢复消息。

### 8.4 命令型的运行消息

`notify` 四档的含义不变（`looper/agents/schedule_notify.py:72-77`）；`interrupted` 归入失败，`succeeded` 和 `interrupted` 归入「跑了东西」。命令型的运行消息正文（agent 的不变）：

```text
定时任务「<名称>」（项目 <项目>）<应触发时刻> 的运行：失败（退出码 1）
耗时 22 分 13 秒 · 补跑，代替错过的 1 次
日志末尾：
```
<log_tail 的最后 1500 字符>
```
<控制台里这次运行的链接>
```

- 状态行写 `succeeded` / `failed` / `timed_out` / `interrupted` 的中文和退出码或信号；
- 「补跑」和「手动」只在不是准点时写；
- 链接是 `schedule_link` 加 `/runs/<run id>`（`looper/agents/links.py:34-36`），控制台打开时展开这次运行；
- Telegram 单条 4096 字符，这个长度放得下。

### 8.5 去重

每类告警都有自己的「开始时刻」列，开始和结束都用条件更新、只成功一次（`set_alert` 和 `start_stale_alert` 的写法，`looper/db/schedules.py:561-576`），所以同类告警在恢复前只发一次：连续失败、长时间没成功、到点没成功、连续跳过各一次。`notify=never` 时告警只留在总览页，和今天一样。总览页「正在失败」一栏（`looper/db/schedules.py:617-628`）把两类新告警也算进去。

### 8.6 agent 定时任务的变化

只有两处，都是加法：

- 前置检查多拿到 `LOOPER_SCHEDULE_TRIGGER`、`LOOPER_SCHEDULE_MISSED`、`LOOPER_SCHEDULE_MISSED_SINCE` 三个变量；
- 运行记录按保留期清理（第 7.3 节，第 13 节问题 2）。

其余：阈值都是 NULL，生效值就是今天的；运行消息、跳过合并、`completed` 状态、运行上下文（模型可见）都不变。

## 9. 接口、管家工具、控制台和 loopercli（R7）

### 9.1 接口（v1）

- **创建和修改**（`schedule_admin.create` / `update`，`looper/agents/v1_domains/schedule_writes.py:16-60`；校验在 `looper/agents/schedules.py:394-434`）：新字段 `kind`（创建时选定，修改时同值为 no-op）、`command`、`env`、`env_file`、`success_deadline`、`alert_failures_after`、`alert_stale_after_seconds`、`alert_overlap_after`。规则：
  - `kind` 不能改：修改时带了不同的 `kind` 返回 400 `"a schedule's kind cannot change; create a new schedule"`；同值不修改记录；
  - 命令型必须有 `command`；下面这些字段给了非空值就返回 400「<字段> 不适用于命令型定时任务」：`requirement`、`template_key`、`template_inputs`、`gate_command`、`gate_timeout_seconds`、`run_budget_usd`、`daily_budget_usd`、`profile_overrides`、`task_limits`、`user_gates='on'`、`run_timeout_seconds='default'`；其中 `"default"` 的错误信息为 `"a command schedule's run_timeout_seconds is a number of seconds or null for no limit"`；
  - agent 定时任务给了 `command`、非空的 `env` 或 `env_file` 返回 400；
  - 命令型同样受归属管家的任务策略约束：`templates_only` 的管家不能有自由形式的定时任务（`looper/db/schedules.py:140-156`），命令型更是如此。
- **读取**（`schedule_admin.get` / `list` / `project_list`）：视图（`looper/agents/schedules.py:185-219`）加 `kind`、`command`、`env`、`env_file`、`success_deadline`、`alert_policy`（三个生效值，包括默认公式算出的 `stale_after_seconds.value`；各项均带 `source: "default" | "schedule"` 和兼容的 `default` 布尔值）和 `alerts.deadline_since`、`alerts.overlap_since`。
- **运行列表**（`schedule_admin.runs`，`looper/agents/schedules.py:591-614`）：运行视图加 `started_at`、`exit_code`、`exit_signal`、`duration_seconds`（命令未启动及所有 `skipped_*` 为 null，不参与耗时统计；已启动命令从 `started_at` 计时）、`log`（`{path, bytes, truncated}` 或 null，`path` 为绝对路径）、`log_tail`；新参数 `status`（只看一种状态）；结果加 `stats`：最近 24 小时和 7 天里跑了东西的运行数、成功数、失败数、成功率、平均和最长耗时。
- **日志**（新，`schedule_admin.run_log`，只读）：参数 `run_id`，以及 `offset`（字节，默认 0）和 `limit_bytes`（默认 1 MiB，最多 4 MiB），或者 `tail_lines`（1–5000，和 `offset` 二选一）；结果 `{run_id, schedule_id, size, offset, next_offset, data_base64, complete, truncated}`。`complete` 表示运行已经结束、文件不会再变；`next_offset` 为 null 表示读到了头。用 base64 是为了让分页拼起来逐字节等于原文件。只读操作不进审计记录（`looper/agents/http_server.py:563-568`）。
- **健康**（新，`system.health`）：第 10.1 节。

### 9.2 管家工具 `looper_schedules`

`_FIELDS`（`looper/agents/ops/schedules.py:455-484`）加上新字段；配置块和确认文字（同文件:311-331、412-435）对命令型显示命令原文、目录、`env` 的键、`env_file` 路径、超时、截止时间和告警阈值，不显示需求、前置检查和花费上限；启用的确认写「启用后它按规则无人值守地执行这条命令（不经过 agent，不花模型额度）」。创建仍然是停用的，启用和「立即运行」仍然要用户在下一条消息里确认（同文件:200-221、311-331）。这是模型可见的改动，要跑真实评测（管家工具交付时）。

### 9.3 控制台

只做最小可用（开发计划第 1 节第 9 条）：

- 项目的定时任务表单：创建时选「Agent 任务」或「命令」；命令型显示命令、目录、`env`（每行一个 `KEY=VALUE`）、`env_file`、超时（不限或秒数）、重叠和补跑、通知、最晚成功时间、三个告警阈值，隐藏 agent 专有的字段；
- 运行记录：命令型的每行显示状态、退出码、开始、结束、耗时、是否补跑；展开后看最后 200 行和「下载完整日志」；上面一行统计（24 小时 / 7 天的成功率、平均和最长耗时）；按状态筛选；
- 定时任务列表和详情显示类型；总览页的「正在失败」显示新的两类告警。

### 9.4 loopercli

`loopercli` 的命令由 OpenAPI 文档生成，名字是「操作 id 点号前 · 点号后」（`looper/loopercli.py:977-1000`）。所以：

- 列出运行：`loopercli schedule-admin runs --schedule-id <id> --limit 50 [--status failed]`；
- 读日志：生成的 `loopercli schedule-admin run-log --run-id <id> [--tail-lines N]` 返回 JSON；另加一个手写命令 `loopercli schedule-log <run id> [--tail N]`，分页读完后把原始字节写到 stdout，方便 `| less` 或重定向。

另一种候选命令形式是 `loopercli schedules runs <id>` 和 `schedules logs <run-id>`；`schedules` 这一组是管家工具生成的（`looper/agents/api_v1.py:269-304`），往里加读日志的动作会变成模型可见的改动，所以不这样做。

### 9.5 各入口

| 入口 | 变化 |
|---|---|
| 控制台 | 第 9.3 节 |
| 管家（网页、飞书、Telegram 里的对话） | 第 9.2 节 |
| 飞书、Telegram 通知 | 复用渠道发送（`looper/agents/channels/registry.py:76-83`），只是正文和种类多了；渠道代码不改 |
| `loopercli` | 第 9.4 节 |
| `looper service` | 探活的安装、状态和卸载；systemd 单元加 `KillMode=process`（第 10 节、第 11.2 节） |
| 配置导出与导入 | 定时任务条目带上新字段（`looper/agents/config_transfer.py:132-134`、`408-425`）；导入后照例是停用的。不加这一条的话，导出带命令型定时任务的配置再导入会失败，所以要和接口一起发布 |

## 10. R9：外部探活

### 10.1 serve 侧：健康接口

`Scheduler` 记下最近一次 tick 开始和结束的时刻，以及最近一次没有抛异常的 tick 结束的时刻（`_loop`，`looper/agents/scheduler.py:409-417`）。新的 v1 只读操作 `system.health`（`GET /api/v1/system/health`，操作者和客户端令牌都能调）返回：

```json
{"status": "ok", "version": "2.x", "pid": 123, "started_at": "...", "now": "...",
 "scheduler": {"running": true, "tick_seconds": 15, "last_tick_at": "...",
               "last_ok_tick_at": "...", "last_ok_tick_age_seconds": 7.2},
 "command_runs": {"running": 2}}
```

它不查数据库，所以数据库卡住时它照样答，但 `last_ok_tick_age_seconds` 会一直涨：tick 里任何一步出错或卡住，探活都看得出来。`loopercli system health` 自动生成。

### 10.2 探活任务

`looper service probe`（新子命令）做一次检查后退出，由服务管理器每分钟启动一次：

- **macOS：** `~/Library/LaunchAgents/<label>.probe.plist`，`StartInterval` 60，`RunAtLoad`，输出写 `<runtime root>/logs/probe.log`；
- **Linux：** `<label>-probe.service`（`Type=oneshot`）加 `<label>-probe.timer`（`OnBootSec=60`、`OnUnitActiveSec=60`）。

一次检查：

1. 读 ops 文件和 `<runtime root>/operator-token`（`looper/agents/operator_auth.py:47-48`），带着它在 5 秒超时内 `GET http://127.0.0.1:<port>/api/v1/system/health`，不走代理。
2. 判定，结果是有类型的：`OK`；`NO_ANSWER`（连不上、超时、不是 JSON）；`UNAUTHORIZED`；`SCHEDULER_STOPPED`（`running` 为 false）；`TICK_STALE`（`last_ok_tick_age_seconds` 大于 120）。除了 `OK` 都算坏。
3. 读写状态文件 `<runtime root>/.local/probe/state.json`（0600，原子替换）：`{last_sample_at, last_ok_at, first_bad_at, alert_started_at, last_verdict, last_send_error}`。
   - 上一次检查距今超过 3 分钟（电脑睡了，探活自己也没跑），这次如果是坏的，`first_bad_at` 从现在算起，不把睡眠的时间算进去；
   - 连续坏了至少 `after_minutes`（默认 5）分钟、而且告警没开着：发告警，发成功才记 `alert_started_at`，发失败记下原因，下一分钟再试；
   - 告警开着、这次是 `OK`：发恢复消息，「中断约 N 分钟」，成功后清掉 `alert_started_at`。
4. 只在状态变化时往 `probe.log` 写一行，平时不写。

`kill -STOP` serve：HTTP 不答（同一个事件循环），第 1 分钟起是 `NO_ANSWER`，第 6 分钟的检查发出告警；`kill -CONT` 之后下一次检查发恢复消息（R9 验收）。

### 10.3 怎么发：不经过 serve，只以机器人身份

探活从 ops 文件的新段 `[probe]` 读配置（`looper/ops_config.py:1-40`，和 `[runtimes]` 一样只能写在文件里）：

```toml
[probe]
notify_channel = "lark"      # "lark"（lark-cli）或 "lark_bot"（机器人应用）
notify_chat = "ou_..."       # open_id（ou_）或群（oc_）
after_minutes = 5
lark_profile = ""            # notify_channel = "lark" 时 lark-cli 的 profile；空为它当前的 profile
```

- **`lark`（lark-cli 渠道）：** 用渠道已有的 `lark_cli.send_command(...)` 拼参数（`looper/agents/channels/lark_cli.py:74-86`），它固定带 `--as bot`；凭据在 lark-cli 自己的配置里，Looper 不读也不复制。探活不依赖 serve，也不依赖数据库。
- **`lark_bot`（机器人应用渠道）：** 应用的 `app_id` 在 `channel_configs.config`，`app_secret` 在 `channel_secrets.bot_token`（`looper/db/schema.sql:1197-1204`，明文存在库里）。探活在要发送时用 ops 文件里的数据库地址直连读出凭据，经 `LarkApi.send` 用租户令牌以机器人身份发送（`looper/agents/channels/lark_api.py:152-160`）；每次读成功都写一份缓存 `<runtime root>/.local/probe/lark_bot.json`（0600），数据库连不上时用缓存。见第 13 节问题 1。
- 不支持 Telegram（需求只要飞书），以后需要再加。
- **绝不以用户身份发送：** 两条路径都只有机器人身份，代码里没有用户身份的分支；测试断言 lark-cli 的参数里有 `--as bot`、没有 `--as user`。
- 告警带幂等键 `looper-probe-<告警开始时刻>`，重试不会发出两条。

告警正文：「告警：Looper 服务（<主机名>，端口 <port>）已经 <N> 分钟没有正常响应：<原因>。定时任务和渠道可能都停了。」恢复：「恢复：Looper 服务又正常了（中断约 N 分钟）。」

### 10.4 安装

`looper service install` 在 ops 文件有 `[probe]` 时写探活单元并加载，没有时删掉已有的探活单元并在报告里写一行「探活没有配置」；`uninstall` 一并删掉；`status` 多两行：探活是否安装、最近一次检查的结果和时间、告警是否开着。探活单元的环境只带 PATH、语言和代理变量（`looper/agents/service.py:59-62` 里除 API key 以外的那些），不带 API key。

## 11. 和热升级的配合

### 11.1 排空

- **命令型触发在排空期间放行**（第 3.4 节）。热升级排空功能现在的做法是排空时 `tick` 跳过 `_start_queued` 和 `_fire_due`，`_admit_start` 也拒绝 `run_now`。命令型触发放行在热升级排空功能合入之后改成：排空时 `_fire_due` 只锁命令型的到期定时任务（`lock_next_due` 加一个 `kinds` 过滤），`_start_queued` 和 `run_now` 对命令型放行；agent 定时任务照热升级排空的规则暂停。
- **排空不等命令型运行**：它们跨重启存活。`DRAINED` 的判定（热升级 RFC 第 7.3 节）不需要把命令型运行算进去；排空状态的 `drain_waiting` 只数前置检查和正在启动的任务，命令型的启动不计入 `starting`。在启动的瞬间被重启打断，由接管记成 `interrupted` 或从 `runner.json` 接上（第 5.4 节）。
- **热升级排空功能需要做的：** 不需要为本文改任何东西。只要保持排空门控在 `tick` 和 `_admit_start` 这两处，命令型触发放行就只改这两处。
- **本文对热升级排空功能的依赖：** 排空期间放行命令型触发依赖热升级排空功能已合入；调度器执行命令型不依赖它。如果调度器执行命令型在热升级排空功能之后合入，它要保留已有门控，命令型触发在排空期间先暂停，等触发放行功能交付后再放行。

### 11.2 重启和服务管理

- launchd：`looper service restart` 是 `bootout` 加 `bootstrap`（`looper/agents/service.py:354-374`），实测不会结束运行器（第 5.5 节）。
- systemd：本期就给生成的单元加 `KillMode=process`。热升级 RFC 把这一条放在看护进程接入服务托管时（第 7.8 节末、第 7.10 节），该部分可以去掉这一项，热升级验收的用例 12 照样验证看护进程。已安装的旧单元要重新 `looper service install` 才会带上；服务托管兼容改动让 `looper service status` 和 `doctor` 在 Linux 上发现单元缺这一行时提示。
- 热升级的 `looper service upgrade`：先排空、再安装、再重启。命令型运行在三步里都不受影响：运行器的代码已经单独落盘（第 5.2 节）。部署报告可以加一项「跨重启的命令型运行 N 个」，不是必需。

### 11.3 组件和指纹

新文件都在 `looper/**` 下，自动归入 `control_plane`，组件指纹的归类检查不会失败。建议组件指纹把 `looper/agents/command_runner.py` 也像 `keeper` 组件那样标为「改了不需要动作」：它按内容哈希落盘，新运行自然用上新代码。`looper/probe.py` 由服务管理器每分钟重新启动，同样不需要重启 serve。两条都只是少一次不必要的重启，不做也不影响正确性。

### 11.4 看护进程

不合流（K1）。两者的区别：命令型运行没有 stdin 协议、没有消费偏移、不需要纪元围栏，结束结果只有一个退出文件。以后看护进程如果要托管命令型运行，运行器的文件格式（`spec.json`、`runner.json`、`exit.json`）可以作为它的清单格式。看护进程阶段的单控制面锁让结算只有一个写者，和本文不冲突；在那之前，两个 serve 同时结算同一条运行，`finish_run` 的 `status = 'running'` 条件保证只有一个写成功（`looper/db/schedules.py:438-446`）。

### 11.5 探活和计划内重启

重启空窗是几秒，排空期间 tick 照常（排空只跳过触发），都在 5 分钟之内，不会误报。表结构变更前的 `pg_dump` 如果超过 5 分钟，探活会告警，这是真实的停服。以后可以让 `looper service upgrade` 写一个维护窗口文件让探活暂停，记为待办。

## 12. 验收用例

用全新的数据库、一次性的 `<runtime root>`、不是 8767 的端口跑 `looper serve`；不发真实的飞书或 Telegram 消息（用替身 lark-cli）。

| # | 需求 | 用例 |
|---|---|---|
| 1 | R1 | 每分钟执行 `date >> <临时文件>`：60 分钟内约 60 条 `succeeded`，花费为 0，没有任何任务、Planner 或 Worker（真实链路可以缩短到 15 分钟、约 15 条） |
| 2 | R1 | 命令 `exit 3`：`failed`，`exit_code=3` |
| 3 | R2 | `sleep 7200` 配 3 小时超时：约 2 小时后 `succeeded`，耗时约 7200 秒（单元测试用短时间代替，真实链路至少跑一次 20 分钟以上的） |
| 4 | R2 | `sleep 100` 配 30 秒超时：约 30 秒后 `timed_out`，`ps` 查不到残留的 `sleep` |
| 5 | R2 | 运行 `sleep 600` 期间重启 `looper serve`（前台 SIGTERM、`kill -9`，以及 launchd 测试标签下的 `looper service restart` 各一次）：重启后仍显示运行中，约 10 分钟后 `succeeded`；`proc_tree.sweep` 没有碰它 |
| 6 | R2 | 运行中 `kill -9` 运行器：下一跳记 `interrupted`，算一次失败，命令进程组被结束 |
| 7 | R3 | 一次输出 1 MiB 的运行：日志文件和输出逐字节一致；界面能看到最后 200 行；输出 60 MiB 的运行被截断并注明 |
| 8 | R4 | 每分钟、`sleep 150`、`overlap=skip`：大约每 3 分钟 1 条 `succeeded` 加 2 条 `skipped_overlap`，各占一行，没有失败通知 |
| 9 | R4 | 关掉 serve 2 小时再启动：日任务（`catch_up=once`）补跑一次，`LOOPER_SCHEDULE_MISSED` 等于错过的次数（单元测试用注入的时钟） |
| 10 | R4 | 连续 10 次 `skipped_overlap`：一条「连续跳过」告警；之后开始新运行时一条恢复 |
| 11 | R5 | 日任务 `success_deadline=08:00`、命令 `exit 1`：05:30 一条失败通知，08:00 一条「到点没成功」；修好后手动运行成功，一条恢复消息（注入时钟） |
| 12 | R5 | 阈值：`alert_failures_after=1` 第一次失败就告警；不设时 agent 定时任务和今天完全一样（已有告警测试全部不改照样通过） |
| 13 | R6 | `env_file` 里有 `FOO=bar`，命令 `test "$FOO" = bar`：成功；运行记录、日志、通知、接口返回里都找不到 `bar`（值用至少 4 个字符的随机串，才能检查替换） |
| 14 | R7 | `loopercli` 列出某命令型定时任务最近 50 次运行，并取到任意一次的完整日志，和文件逐字节一致 |
| 15 | R9 | `kill -STOP` serve 6 分钟：替身 lark-cli 收到一条告警，参数里有 `--as bot`；`kill -CONT` 后收到恢复消息 |
| 16 | 热升级 | 排空期间每分钟的命令型触发照常运行，排空不等它们，重启后接管 |
| 17 | 保留 | 早于保留期的运行、通知和日志被清理，每个定时任务最新 100 条留着；`serve.log` 超过 20 MiB 被轮转 |
| 18 | 迁移 | 从 v2.14.25 的表结构迁移一次：备份先行，迁移后和新库结构一致，已有定时任务都是 `kind='agent'`、字段不变 |

## 13. 待决问题（各有推荐答案，按推荐执行）

1. **`lark_bot` 渠道的探活凭据。** 探活要在 serve 和数据库都可能挂掉的时候发出告警。推荐：`lark`（lark-cli）路径不复制任何凭据，作为首选；`lark_bot` 路径每次从库里读，并缓存一份到 `<runtime root>/.local/probe/lark_bot.json`（0600），库连不上时用缓存。这个秘密本来就明文存在库里，`<runtime root>` 是 0700，里面已经有操作者令牌和 ops 文件（含数据库密码），缓存不扩大暴露面。另一个做法是不缓存，库挂了就发不出去。
2. **运行记录的保留期要不要也管 agent 定时任务。** 推荐要：两种定时任务都按 30 天清理，但每个定时任务至少留最新 100 条，它们建出的任务和花费记录不受影响。这是一处默认行为的变化，PR 里单独写明。另一个做法是只清理命令型运行和跳过行，agent 运行照旧永久保留。
3. **管家能不能创建命令型定时任务。** 推荐能：管家今天已经能设前置检查命令（任意 shell，`looper/agents/ops/schedules.py:469`），风险等级相同；创建后是停用的，启用和「立即运行」都要用户在下一条消息里确认，确认文字里原样显示命令。另一个做法是命令型只能在控制台和 `loopercli` 里建，管家只读。

## 14. 远端执行（R8，不做）

今天多个 serve 共用一个库并不安全（issue #909；热升级 RFC 决定只允许一个控制面，第 7.7 节）。以后如果要「在另一台机器上跑命令」，形态应该是：本机 serve 仍然是唯一的调度者和记录者，远端只有一个执行器，接收 `spec.json`、回传 `runner.json` 和 `exit.json`、按偏移流式回传日志；而不是在远端再跑一个连同一个库的 serve。本文的运行目录格式和「只靠文件和 pid 接管」的做法，正好是这个执行器协议的雏形。

## 15. 风险与监控

### 15.1 风险

| 风险 | 后果 | 缓解 | 剩下的风险 |
|---|---|---|---|
| 运行器被 serve 的停机或下次启动的 sweep 误杀 | 长运行跑不完 | 只用 `subprocess.Popen` 启动；不进 `proc_tree` 记录目录；回归测试钉住 sweep 和 `Scheduler.stop` | 以后有人改成 `proc_tree.spawn`：测试会失败 |
| 服务管理器结束运行器 | 同上 | launchd 实测不会；systemd 加 `KillMode=process`，旧单元在 `status`/`doctor` 里提示 | Linux 没有实机验证（真实链路验证时补齐）；macOS 以后的版本改变行为（每次大版本做真实链路复测） |
| pid 复用 | 把别的进程当成运行器，或者误杀 | 用 pid 加启动时刻认进程，和 `proc_tree` 一样；僵尸算不在 | 一次 `ps` 调用之内的复用窗口 |
| 运行器卡住或被 `SIGSTOP` | 超时不生效 | serve 侧兜底超时（第 5.4 节）；连续跳过告警 | 没设超时的运行只能靠连续跳过和长时间没成功两条告警 |
| 日志占满磁盘 | 写失败 | 单次 50 MiB（加 1 MiB 末尾）；总量 2 GiB；30 天 | 2 GiB 是常量，不够时要改代码 |
| 磁盘满时运行器写不了日志或 `exit.json` | 日志不全；运行记成 `interrupted` | 运行器停止写日志、继续读管道，在 `exit.json` 里记下 `log_error` | `exit.json` 也写不了时只能记 `interrupted` |
| 秘密泄漏 | 凭据出现在库、通知或接口里 | `env_file` 只存路径；内容只在运行器内存里；日志末尾替换；日志 0600 | 脚本自己打印的秘密留在日志文件里；不足 4 个字符的值不替换 |
| 管家安排了危险命令 | 无人值守地执行 | 创建即停用；启用和立即运行要用户确认并显示命令原文；受管家任务策略约束 | 用户确认了就会执行，和前置检查一样 |
| 每分钟的运行把表撑大 | 查询变慢 | 每个定时任务一天 1440 行，30 天约 4.3 万行；已有 `(schedule_id, created_at DESC)` 索引；保留期分批删除 | — |
| 部署时代码被换掉 | 运行器导入到半新半旧的模块 | 运行器只用标准库并按内容哈希单独落盘 | `sys.executable` 本身在重装时短暂不存在：启动失败、记 `failed` 并写明原因 |
| 墙钟超时包含睡眠时间 | 电脑睡了 3 小时，3 小时超时的运行醒来就被结束 | 和 agent 运行的算法一致，写进文档 | 有需要再改成只计醒着的时间 |
| 探活误报 | 半夜收到假告警 | 5 分钟阈值；睡眠间隔不计；只发一次 | 表结构备份超过 5 分钟时会报，属于真实停服 |
| 探活自己坏了 | serve 挂了也没人知道 | `looper service status` 显示探活最近一次检查；发送失败的原因记在状态文件里 | 探活任务被卸载就没有了，和 serve 一样 |

### 15.2 监控

Looper 是本机服务，没有单独的指标系统（热升级 RFC 第 8.2 节）。可观测性放在四处：

- **结构化日志行：** `command run started schedule=… run=… pid=…`；`command run finished run=… status=… exit=… seconds=… log_bytes=… truncated=…`；`command run adopted run=… pid=…`（重启之后第一次看到它还活着）；`command run interrupted run=… reason=…`；`command run killed by backstop run=…`；`housekeeping runs_deleted=… logs_deleted=… log_bytes=… rotated=…`；探活的 `probe verdict=… since=… alert=sent|failed:…`。
- **`system.health`：** 最近一次 tick 和最近一次正常 tick 的年龄、正在跑的命令型运行数。
- **控制台：** 运行记录的统计行；总览页「正在失败」一栏的四类告警。
- **告警**（都经定时任务自己的 `notify` 和 `notify_target`）：

| 级别 | 条件 | 去向 |
|---|---|---|
| P1 | 探活：serve 5 分钟没有正常响应 | 探活直接发飞书 |
| P1 | 日任务到点没成功 | 定时任务的通知对象 |
| P2 | 失败、超时、中断（`notify=failures` 每次都发） | 同上 |
| P2 | 连续失败达到阈值；连续跳过达到阈值 | 同上 |
| P3 | 长时间没成功 | 同上 |

- **演练**（真实链路验证，一次性环境）：运行中重启 serve 三种方式；`kill -9` 运行器；`kill -STOP` serve；磁盘上的运行器文件被删除后重启；排空期间的触发。

## 16. 实施拆分

每张卡的实现细节不入库；进度记在开发计划第 2、4 节。每项独立交付一个 PR，测试代码以外不超过约 400 行。

| 内容 | 依赖 | 主要验收 |
|---|---|---|
| 文档：本 RFC 入库，修订定时任务 RFC 的 D3，计划加阶段和待办 | — | 文档检查 |
| 表结构：新列、新状态、新通知种类、具名约束，一次性迁移 `command_schedules`，状态集合 | — | 用例 18 |
| 运行器 `command_runner.py`（只用标准库）：环境、日志上限、超时、进程组、退出文件 | — | 用例 3、4、7、13 的单元部分 |
| 调度器执行命令型：启动、结算、重启后接管、`interrupted`、兜底超时、重叠和不合并跳过、环境变量、立即运行 | 表结构、运行器 | 用例 1、2、4、5、6、8、9 |
| 接口：字段校验、视图、运行列表的筛选和统计、`run_log` | 调度器执行命令型 | 用例 7、13、14 的接口部分 |
| 告警：阈值、最晚成功时间、连续跳过、命令型运行消息、总览页 | 调度器执行命令型 | 用例 10、11、12、13 |
| systemd `KillMode=process`，旧单元提示 | — | `tests/test_service.py` |
| 排空期间放行命令型触发 | 调度器执行命令型、热升级排空功能 | 用例 16 |
| 保留与轮转：运行记录、通知、日志、运行目录、`serve.log` | 调度器执行命令型 | 用例 17 |
| `system.health` 和 `looper service probe` | — | 用例 15 的单元部分 |
| 探活随 `looper service install` 安装、`status`、`uninstall`，ops 的 `[probe]` | systemd 服务托管兼容、健康接口与探活 | 用例 15 的安装部分 |
| 控制台：命令型的表单 | 接口 | 浏览器截图 |
| 控制台：运行记录、日志、统计、运行链接 | 接口、告警、命令型表单 | 用例 7 的界面部分 |
| `loopercli schedule-log` | 接口 | 用例 14 |
| 管家工具支持命令型，加评测对话 | 接口、告警 | 真实评测 |
| 配置导出与导入带上命令型字段 | 接口 | 导出再导入往返 |
| 真实链路验证 | 以上全部功能 | 第 12 节全部 |

建议的合入顺序：文档、表结构、运行器、systemd 服务托管兼容、健康接口与探活可以并行；然后调度器执行命令型；然后接口、告警、保留与轮转、探活安装、排空期间放行（等热升级排空功能）；然后配置导出与导入（紧跟接口，同一个版本发布）、命令型表单、CLI 日志命令、管家工具；然后控制台运行记录；最后真实链路验证。
