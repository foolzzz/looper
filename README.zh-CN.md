<div align="center">

<h1>LoopX</h1>

**三层多 Agent 编排系统，自带实时 Web 控制台。**

部署能自主规划、拆解并执行复杂任务的编程 agent，全程可观测。

[English](README.md) · 中文

</div>

---

## LoopX 是什么

LoopX 是一个自托管的多 Agent 系统，把一段任务描述变成协同运转的"规划—执行—验证"循环：
**Manager** 负责与用户对话，**TaskRouter** 管理任务生命周期和并发，**Planner** 把工作拆成步骤，
**WorkerPool** 运行 Claude CLI 子进程完成实际的编码。

所有过程都能在内置的 Web 控制台里看到：实时流式输出、任务生命周期控制和 agent 配置。

```
用户 ─→ 网页对话 / 飞书 / Telegram
          │
       Manager          （理解意图，分派任务）
          │
       TaskRouter       （生命周期、并发、排队）
          │
       Planner          （拆解步骤，核验结果）
          │
       WorkerPool       （Claude CLI 子进程，stream-json）
```

### 主要功能

- **任务生命周期**：`init → pending → assigned → running → completed/failed/stopped/cancelled`
- **直接创建任务**：在控制台里创建任务，开始前先配置
- **暂停与恢复**：停止运行中的任务，从 stopped 或 failed 状态重新开始
- **实时活动流**：基于 SSE，实时显示 worker 进程的事件
- **Agent 配置**：在控制台里修改 system prompt、模型和权限
- **多渠道输入**：网页对话、飞书机器人、Telegram 机器人
- **LLM 配置管理**：配置多个模型提供方和接口地址
- **流水线可视化**：用 React Flow 画出任务拆解和 worker 分配的 DAG

## 快速开始

### 前置条件

- Python 3.11+
- PostgreSQL（端口 54329 上的 `agentbox-postgres` 容器，或任意 PostgreSQL 实例）
- Node.js 22+（用于构建控制台）
- `PATH` 上有 Claude CLI（`claude`），并已登录
- 可选，用于飞书渠道：`PATH` 上有 `lark-cli`，并已登录

### 1. 准备数据库

```bash
# 启动 PostgreSQL（使用 Docker 时）
docker run -d --name agentbox-postgres \
  -e POSTGRES_DB=loopx \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -p 54329:5432 \
  postgres:16

# 设置连接串
export LOOPX_DATABASE_URL="postgresql://postgres:postgres@localhost:54329/loopx"
# 也可以写进 .env（已被 git 忽略）
```

### 2. 安装与构建

```bash
# 安装 Python 依赖
uv sync

# 构建 Web 控制台
cd web && npm install && npx vite build && cd ..
```

然后运行 `uv run loopx doctor`：它检查 PostgreSQL、Claude CLI 及其登录和已开启的渠道，缺什么就给出修复方法。

### 3. 运行

```bash
uv run loopx serve
# 控制台地址：http://127.0.0.1:8767
```

首次启动时会自动建表。飞书和 Telegram 不需要命令行参数，在 Settings 页面开启即可（见[渠道](#渠道)）。

换到新机器：`uv run loopx config export loopx-config.json` 把 agent 配置导出成一个文件（密钥留作占位符，
`--include-projects` 连同项目），在新机器上 `uv run loopx config import loopx-config.json` 先预览，加 `--apply` 才写入。

### 4. 作为服务常驻

```bash
uv run loopx service install     # 可选 --port、--runtime-root、--label、--env NAME
uv run loopx service status      # 是否安装、是否运行、PID、上次退出、日志、GET /api/status
uv run loopx service restart     # 例如拉取新代码之后
uv run loopx service uninstall
```

`install` 让 `loopx serve` 在后台常驻：macOS 上是 launchd LaunchAgent，Linux 上是 systemd 用户服务。登录后自动启动，
崩溃后自动拉起，日志写到 `<runtime root>/logs/serve.log`。服务运行执行 `install` 的那个 `loopx`（在 checkout 里就是
`.venv/bin/loopx`），工作目录是当前目录，所以 checkout 的 `.env` 照常生效。它从当前 shell 复制 `PATH`、语言和代理设置、
`CLAUDE_CONFIG_DIR` 以及所有 `LOOPX_*`、`ANTHROPIC_*`、`LARK_*` 变量（其他变量用 `--env NAME` 加入），因此服务文件的权限是
0600。改了配置再执行一次 `install` 即可生效。Linux 上执行 `loginctl enable-linger $USER` 后，未登录时也会运行。

## 架构

### Agent 层级

| 层级 | 模块 | 职责 |
|---|---|---|
| **Manager** | `loopx/agents/tiers/manager.py` | 面向用户的管家：识别意图、对话 |
| **TaskRouter** | `loopx/agents/tiers/task.py` | 任务生命周期、并发控制（同时 3 个）、排队 |
| **Planner** | `loopx/agents/tiers/planner.py` | 拆解步骤、派发、核验 |
| **WorkerPool** | `loopx/agents/tiers/worker.py` | Claude CLI 子进程池，输出 stream-json |

各 agent 之间的通信都经过一个带类型的异步 **MessageBus**，按 `MessageEnvelope` 路由。

### 数据库

PostgreSQL 是启动的硬性依赖。数据表：

| 表 | 用途 |
|---|---|
| `tasks` | 任务元数据、状态、阶段、work_dir、配置、时间戳 |
| `task_steps` | Planner 拆出的步骤 |
| `activity` | 执行轨迹事件（通过 SSE 推给前端） |
| `chat_messages` | 对话历史 |
| `llm_records` | LLM 输入输出的完整记录 |
| `agent_configs` | Agent 配置（prompt、模型、权限） |
| `llm_profiles` | LLM 提供方配置 |
| `channel_configs` | 飞书和 Telegram 渠道设置 |

### Web 控制台

单页 React 应用，从 `web/dist/` 提供，端口 8767；通过 release wheel 安装时，提供随 wheel 打包的那一份。

| 页面 | 功能 |
|---|---|
| **Dashboard** | 系统状态总览 |
| **Chat** | 与全局 Manager 对话，或与运行中任务的 Planner 对话（每条消息成为该任务的一个新步骤）；SSE 流式输出 |
| **Tasks** | 任务列表、状态筛选，以及创建、开始、停止、删除操作 |
| **Task Detail** | 配置编辑（init 状态）、概览、流水线 DAG、会话、对话；Agent Timeline 每行都有 **History** 按钮 |
| **Agents** | Agent 配置编辑；**Runs** 列出一个角色的每次 Claude 运行，并能打开其历史 |
| **Agent history** | 单个 agent 的启动命令、完整记录（文本、思考、工具调用及结果）和活动；跟随运行中的 agent，2 分钟没有输出会标出 |
| **Settings** | 飞书和 Telegram 渠道配置 |

### HTTP API

| 接口 | 方法 | 用途 |
|---|---|---|
| `/api/status` | GET | 系统健康状态和启动自检（`checks`） |
| `/api/tasks` | GET | 任务列表 |
| `/api/tasks/create` | POST | 创建任务（init 状态）；必须提供 `work_dir` |
| `/api/tasks/:id` | GET | 任务详情及步骤 |
| `/api/tasks/:id/config` | PUT | 修改任务配置（仅 init 状态） |
| `/api/tasks/start` | POST | 开始或恢复任务 |
| `/api/tasks/stop` | POST | 暂停任务 |
| `/api/tasks/cancel` | POST | 永久取消 |
| `/api/tasks/delete` | POST | 删除（须先停止） |
| `/api/tasks/:id/activity/stream` | GET | SSE 活动流 |
| `/api/chat/send` | POST | 发送对话消息；`target` 为 Manager（默认）或 `{kind: planner, task_id}` |
| `/api/chat/targets` | GET | 对话对象：Manager 和每个运行中任务的 Planner |
| `/api/agent-configs` | GET | Agent 列表 |
| `/api/agent-configs/:id` | GET/PUT | Agent 配置 |
| `/api/llm-profiles` | GET/POST | LLM 配置 |
| `/api/channel-configs` | GET | 渠道列表 |
| `/api/channel-configs/:id` | GET/PUT | 渠道配置 |

## 配置

### 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `LOOPX_DATABASE_URL` | 是 | PostgreSQL 连接串 |

`claude` 进程继承服务的环境变量，所以 Claude CLI 的认证方式和你在 shell 里一样：已登录，或按它自己的文档使用 API key。

渠道凭据也通过环境变量提供，具体用哪些由 Settings 页决定（见[渠道](#渠道)）。密钥放在 `.env` 里（已被 git 忽略，从仓库根目录读取），不要写进源码。

### 渠道

网页对话始终开启。Telegram 和飞书是内置渠道，不需要命令行参数：在控制台的 **Settings** 页开启并配置。设置保存在 `channel_configs` 表里，服务启动时读取，所以改动后要重启 `loopx serve`。表里保存的是环境变量的**名字**，不是密钥本身。

| 渠道 | Settings 字段 | 另外需要 |
|---|---|---|
| Telegram | `bot_token_env`：存放 bot token 的环境变量（默认 `LOOPX_TELEGRAM_BOT_TOKEN`）。`allowed_user_ids`：允许和 bot 对话的 Telegram 数字用户 ID。 | 该环境变量里的 token。 |
| 飞书 | `app_id_env`：存放应用 ID 的环境变量（默认 `LARK_APP_ID`）。`allowed_open_ids`：发送者的飞书 `open_id`（`ou_...`）。 | `PATH` 上有 `lark-cli` 并已登录；消息由它收发。 |

- 每个 agent 运行 Claude Code 时都不弹权限确认，所以 Manager 能在这台机器上执行任何命令。因此每个渠道只转发名单上的发送者：其他人的消息会被丢弃并记一行日志（只记发送者 ID），不回复。名单为空或格式不对的渠道不会启动，Settings 页会写明原因。`LOOPX_TELEGRAM_ALLOWED_USERS` 已不再生效。
- `app_secret_env` 目前不读取；飞书凭据由 `lark-cli` 保管。
- 渠道启动失败会记日志，控制台照常运行。

### Agent 配置

Agent 的行为可以在控制台里配置：

- **system_prompt**：agent 的指令 prompt
- **model**：覆盖模型（或沿用 LLM 配置）
- **permission_mode**：自主运行的 worker 用 `bypassPermissions`
- **max_turns**：对话轮数上限（0 表示不限）
- **timeout_seconds**：每个任务的执行超时。对 worker 来说，它限制一次 Claude 运行的总时长（默认 86400，0 表示不限），有输出也不会延长；worker 从下一个步骤起应用这两个限制
- **max_concurrency**：Worker 池大小

## 任务生命周期

```
init ──→ pending ──→ assigned ──→ running ──→ completed
  │                                 │
  │ (start)                      (stop)
  │                                 ↓
  └──────────────────────────── stopped ←── failed
                                    │
                                 (start)
                                    ↓
                                 pending → ...

cancelled ← running  (永久终止)
```

- **init**：从界面创建，开始前可以查看和修改配置
- **工作目录**：每个任务都在你指定的目录里运行（`work_dir`，绝对路径或以 `~/` 开头的路径），没有默认值。创建任务时必须提供，没有工作目录的任务不能开始，目录不存在时会自动创建。Manager 从对话中创建任务时，会从对话里取目录，你没说的话会问你。
- **stopped**：已暂停，状态保留，可以恢复
- **cancelled**：已永久终止，不能恢复

## 开发

```bash
# 前端类型检查
cd web && npx tsc --noEmit

# 构建前端
npx vite build

# 运行服务（自动建表）
uv run loopx serve
```

## 发布

每个版本的改动见 [CHANGELOG.md](CHANGELOG.md)。维护者按 [docs/releases/README.md](docs/releases/README.md) 里的流程发布。

## 许可证

Apache License 2.0。见 [LICENSE](LICENSE)。
