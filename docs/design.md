# LoopX Agent System — 产品设计

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
| 编排层 | TaskRouter + Planner | 任务路由；Planner 拆分步骤、按每个结果做决定、调度 Worker 和 Reviewer | 每个 Task 独立 Planner 实例，各自一个常驻 Claude Code 进程（只读工具；进程重启后用 `--resume` 续接会话） |
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

### Project（项目）

每个 Task 属于且只属于一个 Project。项目是任务的一个属性，任务在任何状态下都可以移到另一个项目。

- 项目有名称、描述、可选的默认目录和可选的背景（`background`）。名称去掉首尾空白后是 1–80 个字符，忽略大小写不能重名
- 项目背景是这个项目的所有任务 agent 都该知道的内容，最多 10000 个字符，保存时去掉前后空行，`""` 表示清空。它加进这个项目的 Planner、Worker 和 Reviewer 的系统提示，不加给 Manager（Manager 面向所有项目）。三者的系统提示由 `loopx/agents/system_prompt.py` 统一拼装：先是 `agent_configs` 里可编辑的角色提示；背景不为空时接一段 `## Project: <名称>` 加背景；最后是代码维护的契约（Planner 是 `REPLY_CONTRACT`，Worker 是 `WORK_CONTRACT`，Reviewer 是 `REVIEW_CONTRACT`），契约总在最后
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

每个 Task 只有一个 Planner，它是一个常驻的 Claude Code 进程（和 Manager 共用 `ResidentClaude`）：第一个 turn 时在任务目录里启动，每个事件喂一个 stream-json user turn，任务结束或服务停止时退出。Planner 只有 Read、Grep、Glob 三个只读工具（`--tools`、`--allowedTools`、`--permission-mode dontAsk`、`--strict-mcp-config`，不带 `--dangerously-skip-permissions`），读代码做规划；改动都由 Worker 完成，每次执行仍是一个独立进程。

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
- **Worker 和 Reviewer 的提示怎么分**：稳定的规则放系统提示（`--append-system-prompt`），每一步的数据放 stdin 上的第一个 user turn。系统提示是 `worker` 行的角色提示（Worker 和 Reviewer 共用，为空或读不到时用默认值）、项目段，最后是 `WORK_CONTRACT`（这一步的规则和上面的 JSON 报告格式）或 `REVIEW_CONTRACT`（验收的规则和 `passed` 结论格式）。user turn 只有这一步的 instruction（含重试反馈）、工作目录、目标文件、验收标准和验证命令；Reviewer 的 user turn 还有被验收的 Worker 报告（summary、改动和删除的文件），作为待核对的数据，不进系统提示。运行中转来的消息（用户聊天、Planner 纠偏）仍是 user turn，不重复契约。`worker` 行的 `system_prompt` 以前只存不用，现在生效；启动时还是旧默认提示的会换成新默认（只描述角色，格式交给契约），改过的保留
- **回复契约**：每次回复以且只以一个 ```` ```json ```` 代码块结尾，由 `planner_protocol.py` 用类型化的 dataclass 和枚举严格校验：不允许未知字段，step id 唯一，`depends_on` 只能指向已知步骤且无环，最多 8 步（`MAX_STEPS`），决定必须在当前允许的范围内，重试不超限，replan 不能动已开始的步骤。不合规的回复有一次修复 turn，提示里写明错在哪里
- **兜底**：
  - 规划仍然失败（修复后仍不合规、Claude 出错、超时、无法启动）：任务失败（fail-closed），原因写进 `tasks.error` 和 activity，不会退回成单步执行
  - 决策仍然失败：按这一步自己的结论处理（Worker 的 fail-closed 结论，或 Reviewer 的结论），不另起 Reviewer，并记一条 `Warning: the Planner could not decide ...` 的 error activity
  - 聊天 turn 失败：把错误回给用户，不做任何动作
  - Claude 进程中途退出：这个 turn 失败，下一个 turn 用 `--resume <session_id>` 起新进程，session id 存在 Planner 的计划文件里
- **结果**：任务完成时 `tasks.result_summary` 是最后一个 accept 的步骤给出的 summary（没有时用自动生成的逐步摘要）；任务失败时 `tasks.error` 是 Planner 的失败摘要
- **配置**：`agent_configs` 的 `planner` 行每个 turn 读一次。`system_prompt` 是可编辑的角色提示，代码在后面接上项目段和回复契约；`timeout_seconds` 限制每个 turn（默认 1200 秒，0 表示不限），超时的 turn 失败并杀掉 Claude；`max_turns` 大于 0 时传 `--max-turns`，`model` 有值时传 `--model`；`permission_mode` 不适用。命令变了（包括项目背景变了），下一个 turn 用 `--resume` 重启 Claude。数据库里还是旧默认提示的会在启动时换成新默认，改过的保留
- **记录**：每个 turn 都会产生 Claude 费用，各记一条 `llm_records`（agent_id 为 `planner-<task_id>`，带这个 turn 自己的花费和进程 `pid`）；决定以简短摘要写进 activity。Manager、Planner、Worker、Reviewer 的每次运行都在 `llm_records.metadata.system_prompt` 记下 LoopX 追加的系统提示，`metadata.command` 仍是完整 argv。会话历史页的每条运行有可折叠的「System prompt」段，标明这是 LoopX 追加的部分，不含 Claude Code 内置的系统提示；Chat 页的 Planner 和 Worker/Reviewer 视图复用同一个组件

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

数据库里只存环境变量的**名字**，不存密钥本身；密钥放在对应的环境变量或仓库根目录的 `.env` 里。某个渠道启动失败只记日志，不影响 Web 控制台。

### Web

- 控制台 Chat 页
- 左上角列出所有 agent（`GET /api/chat/targets`）：全局唯一的 Manager，以及最近 30 个任务。每个任务下有它固定的一个 Planner，和每个步骤各自独立的 Worker（做了验收的步骤还有 Reviewer）。已经结束的任务和 agent 也列出来，可以查看消息
- 能发消息的只有 Manager 和运行中任务的 Planner。发给 Planner 的消息由它的 Claude 回复（一个 `[CHAT]` turn），Claude 可以把消息转给正在执行某一步的 Worker，作为它的下一个 user turn，也可以给计划追加步骤；Worker 已经结束的，这条消息留到这一步的决策 turn 给 Claude 看。事件按到达顺序处理，正在跑别的 turn 时发来的消息会排在它后面；任务还没有计划或已经结束时，消息不经过 Claude，直接回复。选中 Planner 时，旁边折叠显示它最近 10 次 Claude 运行。Worker 和 Reviewer 只读，页面显示它的完整运行记录
- `POST /api/chat/send` 发送消息，`target` 指定对话对象
- SSE `GET /api/chat/events` 接收实时响应
- 左侧栏导航下面列出所有项目（任务数，有进行中任务时显示进行中的数量），点击打开按该项目筛选的 Tasks 页，`+` 新建项目。Tasks 页可以按项目筛选；选中项目时显示项目信息和背景（默认显示 3 行，可展开；为空时显示 No background），可以编辑（背景用等宽文本框，实时显示 `n / 10000`，超限不能保存），没有任务时可以删除。新建项目的对话框也可以填背景。新建任务时先选项目或当场新建一个，项目有默认目录时工作目录可以不填。任务详情里可以把任务移到其他项目，任何状态都可以
- 项目接口：`GET/POST /api/projects` 列出（带任务数和进行中任务数）和新建项目，`PUT/DELETE /api/projects/<id>` 修改和删除，`background` 超过 10000 个字符返回 400；`POST /api/tasks/create` 在已有项目或新项目里建任务；`PUT /api/tasks/<id>/project` 把任务移到另一个项目；`GET /api/tasks?project_id=<id>` 按项目列出任务

### Telegram

- Bot 长轮询接入
- 支持 `/start`、`/status`、`/help` 命令
- 消息自动分段（4000 字符限制）
- Settings 配置项：
  - `bot_token_env`：存放 Bot token 的环境变量名，默认 `LOOPX_TELEGRAM_BOT_TOKEN`
  - `allowed_user_ids`：逗号分隔的 Telegram 用户 ID 白名单。留空时使用 `LOOPX_TELEGRAM_ALLOWED_USERS`，两者都为空则允许所有人；格式不对时 Telegram 不启动

### Lark（飞书）

- 需要 `lark-cli` 在 PATH 上且已登录，收发消息都通过它
- Settings 配置项 `app_id_env`：存放 App ID 的环境变量名，默认 `LARK_APP_ID`。`app_secret_env` 目前不会被读取，凭据由 `lark-cli` 管理
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
