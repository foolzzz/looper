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
| 决策层 | Manager | 意图理解、任务创建、状态汇报 | 单例，Claude Code pp 模式 |
| 编排层 | TaskRouter + Planner | 任务路由、计划分解、调度验证 | 每个 Task 独立 Planner 实例 |
| 执行层 | WorkerPool | 子进程执行具体开发/验证任务 | 每个 Worker 独立子进程 |

---

## 核心概念

### Task（任务）

Task 是系统的一等实体，代表一个完整的用户需求。

```
状态流转: pending → assigned → running → completed / failed / cancelled
```

- 每个 Task 自动分配独立的 Planner 实例
- Task 完成/失败时 Planner 自动销毁
- 最多 3 个 Task 并发执行，超出排队

### MessageEnvelope（消息信封）

所有 Agent 间通信使用统一的 MessageEnvelope 协议：

- `correlation_id`：端到端请求追踪
- `message_type`：17+ 种消息类型覆盖完整生命周期
- `source` / `target`：AgentAddress 标识发送/接收方
- 全量 JSONL 日志保证崩溃恢复

### 验证策略 C

Planner 对每个 Step 的验证采用分级策略：

- **简单任务**（`complexity: "simple"`）：Worker 完成后自动标记 verified
- **Worker 怎样才算完成**：三个条件都满足才算，任何一条不满足，这一步都判失败：
  - Claude 正常结束（`result` 事件的 `subtype` 为 `success`，达到最大轮数不算）；
  - 最后一条消息以 JSON 报告结尾，且其中 `status` 为 `completed`；
  - 报告里 `changed_files` 列出的文件都存在，`deleted_files` 列出的文件都已删除。
- **Reviewer**：必须给出布尔类型的 `passed`，纯文本不算通过
- **复杂任务**（`complexity: "complex"`）：Worker 完成后 Planner 另起 Reviewer Worker 交叉验证

---

## 消息流

### 完整请求链路

```
1. 用户发消息 → Channel Adapter
2. Adapter 转换为 USER_MESSAGE → Bus → Manager
3. Manager 调用 Claude Code 理解意图
4. Manager 发 TASK_REQUEST → Bus → TaskRouter
5. TaskRouter 创建 Task + Planner 实例
6. Planner 分解为 PlanSteps
7. Planner 发 WORK_ITEM → Bus → WorkerPool
8. Worker 子进程执行，返回 WORK_RESULT
9. Planner 验证结果（自动 or Reviewer）
10. 所有 Step 完成 → Planner 发 GOAL_STATUS(completed) → TaskRouter
11. TaskRouter 转发给 Manager → Channel → 用户
12. TaskRouter 销毁 Planner
```

### 消息类型一览

| 类型 | 方向 | 用途 |
|------|------|------|
| `USER_MESSAGE` | Channel → Manager / Planner | 用户输入；发给 Planner 的消息成为该任务的新步骤 |
| `AGENT_REPLY` | Manager → Channel | 回复用户 |
| `TASK_REQUEST` | Manager → TaskRouter | 创建任务 |
| `TASK_ACCEPTED` | Planner → TaskRouter → Manager | 任务已接受 |
| `TASK_CANCEL` | Manager → TaskRouter → Planner | 取消任务 |
| `GOAL_STATUS` | Planner → TaskRouter → Manager | 进度/结果 |
| `WORK_ITEM` | Planner → Worker | 派发执行 |
| `WORK_RESULT` | Worker → Planner | 执行结果 |
| `VERIFY_REQUEST` | Planner → Reviewer Worker | 验证请求 |
| `VERIFY_RESULT` | Reviewer → Planner | 验证结论 |
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
- 能发消息的只有 Manager 和运行中任务的 Planner。Planner 自身不调用 LLM：收到的消息先转给正在执行的 Worker，作为它的下一个 user turn；没有 Worker 在跑时，作为新步骤排队。Worker 和 Reviewer 只读，页面显示它的完整运行记录
- `POST /api/chat/send` 发送消息，`target` 指定对话对象
- SSE `GET /api/chat/events` 接收实时响应

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
- **Task 隔离**：每个 Task 有独立的 Planner、Plan、状态持久化目录
- **Worker 隔离**：每个 Worker 在独立子进程中运行 `claude --print`

---

## 目录结构

```
loopx/agents/
├── bus.py           # MessageBus + JSONL Journal
├── message.py       # MessageEnvelope 协议 + 地址常量
├── supervisor.py    # AgentSupervisor 生命周期管理
├── serve.py         # 启动入口
├── channels/
│   ├── registry.py  # Channel 注册表
│   ├── web.py       # Web HTTP+SSE 适配器
│   ├── telegram.py  # Telegram Bot 适配器
│   └── lark.py      # 飞书适配器
└── tiers/
    ├── manager.py   # Manager Agent（管家）
    ├── task.py       # Task 实体 + TaskRouter
    ├── planner.py   # Planner Agent（编排器，per-task）
    └── worker.py    # WorkerPool + WorkerProcess
```
