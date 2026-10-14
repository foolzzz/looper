<div align="center">

<h1>Looper</h1>

**三层多 Agent 编排系统，自带实时 Web 控制台。**

部署能自主规划、拆解并执行复杂任务的编程 agent，全程可观测。

[English](README.md) · 中文

</div>

---

## Looper 是什么

Looper 是一个自托管的多 Agent 系统，把一段任务描述变成协同运转的"规划—执行—验证"循环：
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
- **多种 agent 运行时和 LLM 提供方**：一个 agent 由运行时（如 Claude Code、Codex）、LLM 提供方（运行时自己的登录，
  或 Anthropic、OpenAI 兼容接口的 API key，常见厂商有预设）和模型组成；每次运行都记录 token，模型有价格时也记录花费
- **分层设置**：harness prompt、agent、effort、环境变量和单次运行的花费上限可以按全系统、角色、项目或项目内的角色设置，
  任务还能自选 agent；控制台标出每个值来自哪一层
- **能力目录**：插件、skill 和 MCP 服务器，可以从本机导入、上传、从 git 获取或在 Looper 里编写，再全局安装或装到某个项目，
  并指定给哪些角色
- **项目与任务记忆**：项目有默认目录、背景和 agent 按需读取的知识文档；每个任务都有一份日志，记录通过和失败的步骤、决策、
  你的指示和经验，Planner 每一轮都会读，重启和换新会话后依然保留
- **配置导出与导入**：从命令行或 Settings 页把 agent 配置作为一个文件迁到另一台机器
- **多渠道输入**：网页对话、飞书机器人、Telegram 机器人
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
export LOOPER_DATABASE_URL="postgresql://postgres:postgres@localhost:54329/loopx"
# 也可以写进 .env（已被 git 忽略）
```

### 2. 安装与构建

```bash
# 安装 Python 依赖
uv sync

# 构建 Web 控制台
cd web && npm install && npx vite build && cd ..
```

然后运行 `uv run looper doctor`：它检查 PostgreSQL、Claude CLI 及其登录和已开启的渠道，缺什么就给出修复方法。

### 3. 运行

```bash
uv run looper serve
# 控制台地址：http://127.0.0.1:8767
```

首次启动时会自动建表。飞书和 Telegram 不需要命令行参数，在 Settings 页面开启即可（见[渠道](#渠道)）。

换到新机器：`uv run looper config export looper-config.json` 把 agent 配置导出成一个文件（密钥留作占位符，
`--include-projects` 连同项目），在新机器上 `uv run looper config import looper-config.json` 先预览，加 `--apply` 才写入。
Settings 页的「Move to another machine」也能导出和导入。

### 4. 作为服务常驻

```bash
uv run looper service install     # 可选 --port、--runtime-root、--label、--env NAME
uv run looper service status      # 是否安装、是否运行、PID、上次退出、日志、GET /api/status
uv run looper service restart     # 例如拉取新代码之后
uv run looper service uninstall
```

`install` 让 `looper serve` 在后台常驻：macOS 上是 launchd LaunchAgent，Linux 上是 systemd 用户服务。登录后自动启动，
崩溃后自动拉起，日志写到 `<runtime root>/logs/serve.log`。服务运行执行 `install` 的那个 `looper`（在 checkout 里就是
`.venv/bin/looper`），工作目录是当前目录，所以 checkout 的 `.env` 照常生效。它从当前 shell 复制 `PATH`、语言和代理设置、
`CLAUDE_CONFIG_DIR` 以及所有 `LOOPER_*`、`ANTHROPIC_*`、`LARK_*` 变量（其他变量用 `--env NAME` 加入），因此服务文件的权限是
0600。改了配置再执行一次 `install` 即可生效。Linux 上执行 `loginctl enable-linger $USER` 后，未登录时也会运行。

### 升级时的备份

`looper serve` 每次启动都会应用 schema，升级可能删列、删表。schema 将要变化时，serve 先用 `pg_dump`
备份到 `~/.looper/backups/`（runtime root 下的 `backups/`，每个库保留最近 5 份），并在日志里给出恢复方法；
新建的空库不需要备份。它用 PATH 上的 `pg_dump` 或 `LOOPER_PG_DUMP` 指定的那个，主版本不能低于服务器
（`looper doctor` 会检查；macOS 上 `brew install libpq`，再把 `$(brew --prefix libpq)/bin` 加进 PATH）。
PostgreSQL 跑在 Docker 里时，可以改在容器里备份：

```bash
export LOOPER_BACKUP_CMD='docker exec agentbox-postgres pg_dump --format=custom -U {user} {dbname}'
```

没有可用的 `pg_dump` 或备份失败时，serve 在改动任何东西之前退出；`looper serve --skip-backup`
不备份直接迁移，并记一条警告。回退时先停掉 Looper，把数据库重建为空库，用
`pg_restore --no-owner --dbname "$LOOPER_DATABASE_URL" <文件>`（Docker 里用
`docker exec -i agentbox-postgres pg_restore --no-owner -U postgres -d loopx < <文件>`）恢复，再运行旧版本。
`looper service install` 会复制 `PATH` 和 `LOOPER_*` 变量，所以要在它之前设好。

### 从 LoopX 升级

Looper 在 v2.5.0 及以前叫 LoopX。升级不需要手动操作：

- **命令：**`looper`。`loopx` 再保留一个版本：先打印一行弃用提示，再执行 `looper`。`install.sh`
  会先卸掉同一工具装的旧 `loopx` 包（数据不动）。
- **数据目录：**`~/.looper`。第一次运行 `looper serve` 或 `looper service install` 时把 `~/.loopx`
  移过去（一次重命名，不复制也不删除任何文件），并在原处留一个指向它的链接，保留一个版本。两个目录都在时用
  `~/.looper` 并记一条警告。
- **环境变量：**`LOOPER_*`。`LOOPER_*` 没设时仍读对应的 `LOOPX_*`，并给出警告；请在 shell 和 `.env` 里改名。
- **服务：**`com.looper.serve`。`looper service install` 会先停掉并删除旧的 `com.loopx.serve`。
- **配置文件**：导出写 `looper-config/v2`；以前导出的 `looper-config/v1`、`loopx-config/v1` 文件仍然能导入。
- **不变的**：默认数据库仍叫 `loopx`（`postgresql://localhost:5432/loopx`），里面的数据（比如你自己写的
  harness）不会被改写；发布 tag 和历史保留原名；[NOTICE](NOTICE) 保留对上游 LoopX 的署名。因为 `schema.sql`
  有变化，升级后第一次启动会照常先备份。

## 架构

### Agent 层级

| 层级 | 模块 | 职责 |
|---|---|---|
| **Manager** | `looper/agents/tiers/manager.py` | 面向用户的管家：识别意图、对话 |
| **TaskRouter** | `looper/agents/tiers/task.py` | 任务生命周期、并发控制（同时 3 个）、排队 |
| **Planner** | `looper/agents/tiers/planner.py` | 拆解步骤、派发、核验 |
| **WorkerPool** | `looper/agents/tiers/worker.py` | Claude CLI 子进程池，输出 stream-json |

各 agent 之间的通信都经过一个带类型的异步 **MessageBus**，按 `MessageEnvelope` 路由。

### 数据库

PostgreSQL 是启动的硬性依赖；`looper serve` 每次启动都会应用 `looper/db/schema.sql`。数据表按领域分组：

| 领域 | 表 | 内容 |
|---|---|---|
| 项目与任务 | `projects`、`tasks`、`task_steps`、`task_plans`、`task_profile_overrides` | 项目及其默认目录、背景和执行设置；任务、Planner 的计划和步骤，以及任务给某个角色选的 Agent Profile |
| 任务记忆与知识 | `task_journal`、`project_documents`、`project_document_drafts` | 任务运行时由代码写入的日志；项目的知识文档和等你审阅的草稿 |
| Agent 与设置 | `agent_runtimes`、`llm_providers`、`llm_provider_secrets`、`agent_profiles`、`agent_setting_layers`、`role_configs`、`agent_sessions` | 运行时、提供方（API key 单独存放）和 Agent Profile；各设置层；每个角色的 prompt 和限制；Manager 和 Planner 恢复的会话 |
| 能力目录 | `library_items`、`library_files`、`library_mcp_servers`、`library_installs` | 插件、skill 和 MCP 服务器及其文件或服务器配置，以及各自装在哪里 |
| 运行与对话 | `llm_records`、`llm_record_events`、`activity`、`chat_messages` | 每次 agent 运行的用量、花费和流事件；每个任务的活动流；对话历史 |
| 系统 | `system_settings`、`channel_configs`、`schema_migrations`、`schema_fingerprint` | 全局设置、Telegram 和飞书渠道、已执行的一次性迁移，以及上次应用的 schema 的哈希（serve 据此判断改动前是否要备份） |

主要数据表逐列的说明见 [Data Persistence](docs/architecture/agent-system.md#data-persistence)。

### Web 控制台

单页 React 应用，从 `web/dist/` 提供，端口 8767；通过 release wheel 安装时，提供随 wheel 打包的那一份。

| 页面 | 功能 |
|---|---|
| **Dashboard** | 系统状态总览 |
| **Chat** | 与全局 Manager 对话，或与运行中任务的 Planner 对话（每条消息成为该任务的一个新步骤）；SSE 流式输出 |
| **Tasks** | 任务列表、项目和状态筛选，以及创建、开始、停止、删除操作 |
| **Project** | 单个项目的概览、知识文档、项目自己的 skill 和安装，以及项目设置（含该项目的 agent 设置层） |
| **Task Detail** | 配置编辑（init 状态）、概览、流水线 DAG、会话、对话；Agent Timeline 每行都有 **History** 按钮 |
| **Runs** | 每个角色的会话和运行记录（Manager 的 `/new` 显示为新会话），每次运行都能打开其历史；不含配置，并链接到 Settings → Roles 里的该角色 |
| **Agent history** | 单个 agent 的启动命令、完整记录（文本、思考、工具调用及结果）和活动；跟随运行中的 agent，2 分钟没有输出会标出 |
| **Settings** | 侧栏底部可折叠的分组，每项一页：**LLM Providers**；**Agent Runtimes**（只读：每种运行时类型，在服务的 PATH 上找到与否，可 Re-detect，以及规划中的类型）；**Agent Profiles**（运行时、提供方和模型，各自能跑的角色和被谁使用）；**Roles**（所有角色的默认值，再是每个角色的 Agent Profile、设置层、prompt 和限制）；**Skills & MCP**；**Channels**；**Import / Export** |

### HTTP API

API 只响应控制台和本机客户端：来自其他网站页面的请求（按 `Origin` 判断）会被拒绝。主要的路由分组：

| 分组 | 接口 | 用途 |
|---|---|---|
| 状态 | `GET /api/status` | 系统健康状态和启动自检（`checks`） |
| 项目 | `/api/projects`、`/api/projects/:id`、`/api/projects/:id/knowledge/...` | 创建、修改和删除项目；项目的知识文档和草稿 |
| 任务 | `/api/tasks`、`/api/tasks/create`、`/api/tasks/:id/...`、`/api/tasks/{start,stop,cancel,delete}` | 列出和创建任务（属于某个项目，可带 `profile_overrides`）、查看、开始、停止、取消、删除；活动及其 SSE 流 |
| 能力目录与安装 | `/api/catalog/...`、`/api/installs/...`、`/api/projects/:id/installs`、`/api/projects/:id/skills/...` | 插件、skill 和 MCP 服务器：导入、上传、git、编写、修改、删除；全局安装或装到项目 |
| Agent 设置 | `/api/agent-settings/...`、`/api/projects/:id/agent-settings/...`、`/api/roles/...` | 各设置层，每个字段的生效值及其来源；每个角色的 prompt 和限制 |
| 注册表 | `GET /api/registry`、`/api/runtimes/:id/probe`、`/api/providers/...`、`/api/agent-profiles/...` | 运行时类型和内置运行时（Re-detect 重新探测）；注册、修改和删除提供方（API key 打码）和 Agent Profile，并写明各自被谁使用 |
| 配置迁移 | `GET /api/config/export`、`/api/config/imports/...` | 下载配置文件；上传配置文件、预览，再应用或取消 |
| 对话 | `/api/chat/send`、`/api/chat/targets`、`/api/chat/messages`、`/api/chat/events` | 与 Manager 或运行中任务的 Planner 对话；历史和 SSE 流 |
| 运行记录 | `/api/llm-records/...`、`/api/agents/:id/usage` | 每次 agent 运行的启动命令、用量、花费和事件；单个 agent 的花费和上下文 |
| 渠道 | `/api/channel-configs/...` | Telegram 和飞书的设置及发送者名单 |

各接口的字段和错误说明见 [HTTP API 参考](docs/architecture/agent-system.md#http-api)。

## 配置

### 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `LOOPER_DATABASE_URL` | 是 | PostgreSQL 连接串 |

`claude` 进程继承服务的环境变量，所以 Claude CLI 的认证方式和你在 shell 里一样：已登录，或按它自己的文档使用 API key。

渠道凭据也通过环境变量提供，具体用哪些由 Settings 页决定（见[渠道](#渠道)）。密钥放在 `.env` 里（已被 git 忽略，从仓库根目录读取），不要写进源码。

### 渠道

网页对话始终开启。Telegram 和飞书是内置渠道，不需要命令行参数：在控制台的 **Settings** 页开启并配置。设置保存在 `channel_configs` 表里，服务启动时读取，所以改动后要重启 `looper serve`。表里保存的是环境变量的**名字**，不是密钥本身。

| 渠道 | Settings 字段 | 另外需要 |
|---|---|---|
| Telegram | `bot_token_env`：存放 bot token 的环境变量（默认 `LOOPER_TELEGRAM_BOT_TOKEN`）。`allowed_user_ids`：允许和 bot 对话的 Telegram 数字用户 ID。 | 该环境变量里的 token。 |
| 飞书 | `app_id_env`：存放应用 ID 的环境变量（默认 `LARK_APP_ID`）。`allowed_open_ids`：发送者的飞书 `open_id`（`ou_...`）。 | `PATH` 上有 `lark-cli` 并已登录；消息由它收发。 |

- 每个 agent 运行 Claude Code 时都不弹权限确认，所以 Manager 能在这台机器上执行任何命令。因此每个渠道只转发名单上的发送者：其他人的消息会被丢弃并记一行日志（只记发送者 ID），不回复。名单为空或格式不对的渠道不会启动，Settings 页会写明原因。`LOOPX_TELEGRAM_ALLOWED_USERS` 已不再生效。
- `app_secret_env` 目前不读取；飞书凭据由 `lark-cli` 保管。
- 在网页对话、飞书或 Telegram 里单独发送 `/new`，Manager 就开始新会话：下一条消息（不论来自哪个渠道）从全新的上下文开始。网页对话的 **New session** 按钮发的就是它。运行中的任务不受影响。
- 渠道启动失败会记日志，控制台照常运行。

### Agent 配置

每个角色（Manager、Planner、Worker、Reviewer）运行一个 **agent**：由运行时、LLM 提供方和模型组成，
保存为 Settings → Agent Profiles 里的一个 **Agent Profile**，在 Settings → Roles 里选用。

- **运行时**是 Looper 有适配器的某类 agent CLI（Claude Code、Codex 或 OMP）。运行时是内置的：Looper 在服务的 PATH 上找到它们并探测版本和能力，Settings → Agent Runtimes 列出它们并可 Re-detect；要换一个可执行文件，就改服务的 PATH。
- **提供方**是 agent 请求发往的地方：它支持的协议、模型及价格，以及认证方式：运行时自己的登录，或 API key（API 从不返回它）。
- 内置 agent `claude-code/opus`（Claude Code、它自己的登录、`opus`）运行所有没有另选 agent 的角色。

**分层设置**：harness prompt、agent、effort、环境变量和单次运行的花费上限这五项设置，经过四层解析，后面的层优先：

| 层 | 在哪里编辑 | 作用范围 |
|---|---|---|
| 系统 | Settings → Roles 的所有角色部分 | 所有 agent |
| 角色 | Settings → Roles，按角色 | 一个角色，不分项目：Manager、Planner、Worker 或 Reviewer |
| 项目 | 项目的 Settings 标签页 | 该项目的 Planner、Worker 和 Reviewer |
| 项目内角色 | 同一标签页，按角色 | 某个项目里的其中一个角色 |

Manager 横跨所有项目，所以只有前两层。任务创建时还可以给它的 Planner、Worker 或 Reviewer 选 Agent Profile（`profile_overrides`），
这一选择优先于所有层，只对该任务生效。没有任何一层设置的字段用内置默认值，控制台会显示每个字段的值及其来源层。
环境变量不用来放密钥：看起来像凭据的变量名会被拒绝。改动从每个 agent 的下一次运行或下一轮起生效。详见
[Harness, profile and environment](docs/architecture/agent-system.md#harness-profile-and-environment)。

**角色配置**：Settings → Roles 还编辑每个角色自己的字段（`role_configs`）：

- **system_prompt**：角色 prompt，说明这类 agent 负责什么；harness 放在它前面，可以重置为推荐的 prompt
- **max_turns**：对话轮数上限（0 表示不限）
- **timeout_seconds**：一次运行或一轮的时长上限（0 表示不限）。对 worker 来说，它是一次运行的总时长（默认 86400），有输出也不会延长；worker 从下一个步骤起应用这两个限制
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
uv run looper serve
```

## 发布

每个版本的改动见 [CHANGELOG.md](CHANGELOG.md)。维护者按 [docs/releases/README.md](docs/releases/README.md) 里的流程发布。

## 许可证

Apache License 2.0。见 [LICENSE](LICENSE)。
