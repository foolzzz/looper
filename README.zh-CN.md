<div align="center">

<h1>Looper</h1>

**三层多 Agent 编排系统，自带实时 Web 控制台。**

部署能自主规划、拆解并执行复杂任务的编程 agent，全程可观测。

[English](README.md) · 中文

</div>

---

## Looper 是什么

Looper 是一个自托管的多 Agent 系统，把一段任务描述变成协同运转的"规划—执行—验证"循环：
**Manager** 负责与用户对话，**TaskRouter** 管理任务生命周期，**Planner** 把工作拆成步骤，
**WorkerPool** 运行 Claude CLI 子进程完成实际的编码。

所有过程都能在内置的 Web 控制台里看到：实时流式输出、任务生命周期控制和 agent 配置。

```
用户 ─→ 网页对话 / 飞书 / Telegram
          │
       Manager          （理解意图，分派任务）
          │
       TaskRouter       （任务生命周期）
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
  或 Anthropic、OpenAI 兼容接口的 API key，常见厂商有预设）、模型和思考强度组成；每次运行都记录 token，模型有价格时也记录花费
- **分层设置**：harness prompt、agent、环境变量和单次运行的花费上限可以按全系统、角色、项目或项目内的角色设置，
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

# 写运维配置文件（权限 0600，放在只有你能进入的目录里）
mkdir -m 700 -p ~/.looper/ops
cat > ~/.looper/ops/server.toml <<'EOF'
[database]
url = "postgresql://postgres:postgres@localhost:54329/loopx"
EOF
chmod 600 ~/.looper/ops/server.toml
```

仍在用旧变量的安装（shell 或 `.env` 里的 `LOOPER_DATABASE_URL`，或 `LOOPX_DATABASE_URL`）可以在 `uv sync` 之后改跑一次
`uv run looper config init-ops`。它按当前生效的值写成同一个文件，权限 0600，已有文件则拒绝覆盖。

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
uv run looper service install     # 可选 --port、--runtime-root、--config、--label、--env NAME
uv run looper service status      # 是否安装、是否运行、PID、上次退出、日志、POST /api/v1/system/status
uv run looper service restart     # 例如拉取新代码之后
uv run looper service uninstall
```

`install` 让 `looper serve` 在后台常驻：macOS 上是 launchd LaunchAgent，Linux 上是 systemd 用户服务。登录后自动启动，
崩溃后自动拉起，日志写到 `<runtime root>/logs/serve.log`。服务运行执行 `install` 的那个 `looper`（在 checkout 里就是
`.venv/bin/looper`），工作目录是当前目录，命令是 `looper serve --config <运维文件> --runtime-root <根目录>`，两者都是绝对路径。
只有给 `install` 传了 `--port`，或以前的单元带了 `--port` 而文件里没写端口时，才加上 `--port`。没有运维文件，或某项设置仍来自环境变量时，
安装会拒绝（先跑 `looper config init-ops`，再删掉该变量）。它复制 `PATH`、`TZ`、locale、代理和 CA 证书变量，运行时的目录和登录
（`CLAUDE_CONFIG_DIR`、`CODEX_HOME`、`GROK_HOME`、`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、
Anthropic 的模型变量、`CODEX_API_KEY`、`XAI_API_KEY`，以及 `child_env.RUNTIMES` 里的其余项）、`LOOPER_TELEGRAM_BOT_TOKEN`，
以及每个 `--env NAME`。它不再复制全部 `LOOPER_*`、`LOOPX_*`、`ANTHROPIC_*`、`LARK_*`，报告里会点名没复制的变量。
`--env` 指向运维变量时拒绝。再执行一次 `install` 会换掉旧单元，旧单元里带的数据库 URL 随之消失。单元文件仍是 0600，因为它可能含有 API key。
改运维文件后重启服务才生效。Linux 上执行 `loginctl enable-linger $USER` 后，未登录时也会运行。

### 升级时的备份

`looper serve` 每次启动都会应用 schema，升级可能删列、删表。schema 将要变化时，serve 先用 `pg_dump`
备份到 `~/.looper/backups/`（runtime root 下的 `backups/`，每个库保留最近 5 份），并在日志里给出恢复方法；
新建的空库不需要备份。它用 PATH 上的 `pg_dump`，或运维文件里 `[backup] pg_dump` 指定的那个，主版本不能低于服务器
（`looper doctor` 会检查；macOS 上 `brew install libpq`，再把 `$(brew --prefix libpq)/bin` 加进 PATH）。
PostgreSQL 跑在 Docker 里时，把 `[backup] command` 设成 argv 数组，在容器里备份（`{dbname}` 和 `{user}` 会填上）：

```toml
[backup]
command = ["docker", "exec", "agentbox-postgres", "pg_dump", "--format=custom", "-U", "{user}", "{dbname}"]
```

备份还要再列一遍来证明可读：由 `pg_dump` 旁边（否则 PATH 上）的 `pg_restore` 完成。`command` 是
`docker exec [选项] <容器> pg_dump ...` 这一形式时，Looper 用 `docker exec -i <容器> pg_restore --list`
（备份走标准输入）列出，本机不需要 `pg_restore`。其他命令且本机没有 `pg_restore` 时，必须设 `[backup] verify_command`，
一个从标准输入读备份、像 `pg_restore --list` 一样列出内容的 argv：

```toml
verify_command = ["docker", "exec", "-i", "agentbox-postgres", "pg_restore", "--list"]
```

`looper doctor` 和启动日志会说明用的是哪个校验器。

没有可用的 `pg_dump` 或备份失败时，serve 在改动任何东西之前退出；`looper serve --skip-backup`
不备份直接迁移，并记一条警告。回退时先停掉 Looper，把数据库重建为空库，用
`pg_restore --no-owner --dbname '<database.url>' <文件>`（Docker 里用
`docker exec -i agentbox-postgres pg_restore --no-owner -U postgres -d loopx < <文件>`）恢复，再运行旧版本。
`[backup]` 写在运维文件里，改完重启即可；`service install` 不会把它们当成变量复制进去。

### 从 LoopX 升级

Looper 在 v2.5.0 及以前叫 LoopX。改名本身不需要手动操作：

- **命令：**`looper`。`loopx` 再保留一个版本：先打印一行弃用提示，再执行 `looper`。`install.sh`
  会先卸掉同一工具装的旧 `loopx` 包（数据不动）。
- **数据目录：**`~/.looper`。第一次运行 `looper serve` 或 `looper service install` 时把 `~/.loopx`
  移过去（一次重命名，不复制也不删除任何文件），并在原处留一个指向它的链接，保留一个版本。两个目录都在时用
  `~/.looper` 并记一条警告。
- **环境变量：**旧的部署变量再读一个版本（`LOOPER_DATABASE_URL`、`LOOPER_PUBLIC_URL`、`LOOPER_PG_DUMP`、
  `LOOPER_BACKUP_CMD`、`LOOPER_BACKUP_VERIFY_CMD`、`LOOPER_OPERATOR_NOTES_ROOT`，以及除笔记根目录和校验命令外的 `LOOPX_*` 同名；这两项只认 `LOOPER_` 前缀），先看环境变量，再看 `.env`，
  每次使用都按变量名警告。`looper config init-ops` 把它们写进运维文件。其余的 `LOOPX_*` 在对应 `LOOPER_*` 没设时，
  `looper.env` 仍会读，并警告，再保留一个版本。
- **服务：**`com.looper.serve`。`looper service install` 会先停掉并删除旧的 `com.loopx.serve`。
- **配置文件**：导出写 `looper-config/v2`；以前导出的 `looper-config/v1`、`loopx-config/v1` 文件仍然能导入。
- **不变的**：已有的数据库保持原名（常常是 `loopx`），里面的数据（比如你自己写的 harness）不会被改写。数据库 URL
  不再有默认值；什么都没配时，`looper config init-ops` 才写入 `postgresql://localhost:5432/loopx`，并说明这一点。
  发布 tag 和历史保留原名；[NOTICE](NOTICE) 保留对上游 LoopX 的署名。因为 `schema.sql` 有变化，升级后第一次启动会照常先备份。

## 架构

### Agent 层级

| 层级 | 模块 | 职责 |
|---|---|---|
| **Manager** | `looper/agents/tiers/manager.py` | 面向用户的管家：识别意图、对话 |
| **TaskRouter** | `looper/agents/tiers/task.py` | 任务生命周期：任务一开始就有自己的 Planner，同时运行的任务数不设上限 |
| **Planner** | `looper/agents/tiers/planner.py` | 拆解步骤、派发、核验 |
| **WorkerPool** | `looper/agents/tiers/worker.py` | Claude CLI 子进程池，输出 stream-json |
| **Scheduler** | `looper/agents/scheduler.py` | 触发各项目的定时任务：每次触发建一个完整任务并直接开始；漏掉的触发只补一次，重叠时跳过或排队，超时的运行被停止；前置检查决定这次值不值得建任务，失败那一轮的输入交给下一轮，每日花费上限，以及发到 Telegram 或飞书的通知和两条告警 |

各 agent 之间的通信都经过一个带类型的异步 **MessageBus**，按 `MessageEnvelope` 路由。

### 数据库

PostgreSQL 是启动的硬性依赖；`looper serve` 每次启动都会应用 `looper/db/schema.sql`。数据表按领域分组：

| 领域 | 表 | 内容 |
|---|---|---|
| 项目与任务 | `projects`、`tasks`、`task_steps`、`task_plans`、`task_profile_overrides` | 项目及其默认目录、背景和执行设置；任务、Planner 的计划和步骤，以及任务给某个角色选的 Agent Profile |
| 定时任务 | `schedules`、`schedule_profile_overrides`、`schedule_runs`、`schedule_notifications` | 项目的定时任务（时间规则和时区、任务需求、前置检查、运行策略、花费上限、通知对象、Agent Profile），每次触发的运行及其前置检查输出、任务、结果和花费，以及每一条通知 |
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
| **Dashboard** | 此刻在发生什么：按分组的任务数（点开即按该组筛选的 Tasks 页）、正在运行的 agent、各活跃项目正在运行的任务、Worker 和 Reviewer、定时任务（接下来的触发、正在失败的、今天跳过的）、今日花费，组件不健康时给出警告 |
| **Chat** | 与全局 Manager 对话，或与运行中任务的 Planner 对话（消息作为该任务的补充说明交给 Planner）；选中 Manager 时按渠道（网页对话、飞书、Telegram）列出它的对话，飞书和 Telegram 的只读；SSE 流式输出 |
| **Tasks** | 任务列表、项目、状态和定时任务筛选，定时任务建出的任务带 Scheduled 标记，以及创建、开始、停止、删除操作 |
| **Project** | 单个项目的概览、知识文档、项目自己的 skill 和安装、各角色在本项目覆盖的 Agent Profile 和其他 agent 设置、定时任务（时间规则、接下来的触发时刻、启用、立即运行、运行历史），以及项目设置 |
| **Task Detail** | 配置编辑（init 状态）、概览、流水线 DAG、Agents（各角色的 Agent Profile 和并发上限、Planner 定义的角色、每一步实际用的 Profile）、会话、对话；Agent Timeline 每行都有 **History** 按钮 |
| **Runs** | 每个角色的会话和运行记录（Manager 的 `/new` 显示为新会话），每次运行都能打开其历史；不含配置，并链接到 Settings → Roles 里的该角色 |
| **Schedules** | 所有项目的定时任务，可按项目和状态筛选，每行有启用开关、立即运行和删除；单个定时任务的页面显示设置、告警、今日花费和运行历史。新建和修改在项目的 Schedules 标签页 |
| **Agent history** | 单个 agent 的启动命令、完整记录（文本、思考、工具调用及结果）和活动；跟随运行中的 agent，2 分钟没有输出会标出 |
| **Settings** | 侧栏底部可折叠的分组，每项一页：**LLM Providers**；**Agent Runtimes**（只读：每种运行时类型，在服务的 PATH 上找到与否，可 Re-detect，以及规划中的类型）；**Agent Profiles**（运行时、提供方和模型，各自能跑的角色和被谁使用）；**Roles**（可折叠的列表：先是共享默认值 Shared defaults，再是每个角色一行，含它的覆盖项、prompt 和限制）；**Skills & MCP**；**Channels**；**Import / Export** |

### HTTP API

控制台使用操作者认证：本机浏览器自动登录，其他地方的浏览器用 `looper auth set-password` 设置的账号和密码登录。程序使用操作者或 API 客户端的 Bearer token；客户端管理仅限操作者。下列已迁移的 v1 操作用 POST 和 JSON 请求体，成功返回 200 `{status: "ok", result: ...}`（`OpOk`），失败返回类型化错误；客户端确认规则见参考文档。主要的路由分组：

| 分组 | 接口 | 用途 |
|---|---|---|
| 状态 | `POST /api/v1/system/{status,overview}` | 系统健康状态和启动自检（`checks`）；Dashboard 的“此刻在发生什么”总览 |
| 项目 | `POST /api/v1/project_admin/{list,create,update,delete}`、`POST /api/v1/project_knowledge/{list,add,get,update,delete,drafts,confirm_draft,discard_draft}` | 创建、修改和删除项目；项目的知识文档和草稿 |
| 任务 | `POST /api/v1/tasks/{page,create,detail,delete,...}`、`POST /api/v1/ops/task_control/{start,stop,cancel,restart,start_over}` | 列出和创建任务（属于某个项目，可带 `profile_overrides`）、查看、开始、停止、取消、删除；活动及其 SSE 流 |
| 定时任务 | `POST /api/v1/schedule_admin/<action>` | 创建、修改、启用和删除项目的定时任务；列出所有项目的定时任务；立即运行；运行历史和通知；时间规则接下来的触发时刻 |
| 能力目录与安装 | `POST /api/v1/catalog_items/...`、`POST /api/v1/catalog_sources/...`、`POST /api/v1/installs/...`、`POST /api/v1/project_skills/...` | 插件、skill 和 MCP 服务器：导入、上传、git、编写、修改、删除；全局安装或装到项目 |
| Agent 设置 | `POST /api/v1/agent_settings/{get,update_system,update_role}`、`POST /api/v1/project_agent_settings/{get,update,update_role}`、`POST /api/v1/roles/{list,get,update,reset_prompt}` | 各设置层，每个字段的生效值及其来源；每个角色的 prompt 和限制 |
| 允许的 Profile | `POST /api/v1/allowed_profiles/{get,replace}` | 读取或替换项目 Planner 的 Profile 白名单；空列表表示不限制 |
| 注册表 | `POST /api/v1/registry/view`、`POST /api/v1/runtimes/probe`、`POST /api/v1/provider_accounts/...`、`POST /api/v1/profiles/{create,update,delete,prompt_preview}`、`POST /api/v1/ops/models/...`、`POST /api/v1/models/get` | 运行时类型和内置运行时（Re-detect 重新探测）；注册、修改和删除提供方（API key 打码）和 Agent Profile，并写明各自被谁使用 |
| 配置迁移 | `POST /api/v1/config/export`、`POST /api/v1/config/import_upload`、`POST /api/v1/config/import_apply`、`POST /api/v1/config/import_cancel` | 下载配置文件；上传配置文件、预览，再应用或取消 |
| 对话 | `POST /api/v1/operator_chat/{send,targets,sessions,messages,save_message,reply_events,thread_events,turn,stop}` | 与 Manager 或运行中任务的 Planner 对话；按渠道列出 Manager 的对话；历史和 SSE 流 |
| 运行记录 | `POST /api/v1/runs/{list,get,events,for_task,usage}` | 每次 agent 运行的启动命令、用量、花费和事件；单个 agent 的花费和上下文 |
| 渠道 | `POST /api/v1/channel_configs/...`、`POST /api/v1/channels/{status,people}` | Telegram 和飞书的设置、Telegram bot token（接口不返回）和发送者名单；飞书机器人的状态、被拒的发送者和按人搜索 |

各接口的字段和错误说明见 [HTTP API 参考](docs/architecture/agent-system.md#http-api)。

## 配置

### 运维配置文件

部署设置写在 `<runtime root>/ops/server.toml`（默认 `~/.looper/ops/server.toml`）。`looper serve`、`doctor`、
`config export`、`config import`、`config init-ops` 和 `service install` 的 `--config /path` 指向别的文件。
改完文件后重启服务才生效。Looper 原子写入。文件必须是普通文件（不能是符号链接），属主是你，权限 0600，
所在目录也属你、权限 0700。不做展开：没有变量插值，没有 `~`，没有 include。未知的键、类型不对或值不合法会让命令停下；
报错点名这个键，不显示值。

| 键 | 必需 | 含义 |
|---|---|---|
| `database.url` | 是 | PostgreSQL 连接串。没有它时，`looper serve`、`doctor` 和 `config import`、`export` 不会启动，并提示运行 `looper config init-ops` 或编辑该文件 |
| `server.host` | 否 | 绑定地址（默认 `127.0.0.1`） |
| `server.port` | 否 | 端口（默认 8767） |
| `server.public_url` | 否 | 用户打开控制台时用的地址（例如 `https://looper.example.com`），用于任务结束通知和定时任务消息发到 Telegram、飞书里的链接；不设置时用服务自己的 `http://<host>:<port>`。值无效时 `looper serve` 不会启动 |
| `server.log_level` | 否 | `debug`、`info`、`warning` 或 `error`（默认 `info`） |
| `backup.pg_dump` | 否 | 不用 PATH 上那个时，改用这个 `pg_dump`；主版本不能低于服务器 |
| `backup.command` | 否 | 一个 argv 数组，它的标准输出就是备份；`{dbname}` 和 `{user}` 从数据库 URL 填入 |
| `backup.verify_command` | 否 | 一个 argv 数组，从标准输入读备份，像 `pg_restore --list` 一样列出内容；`backup.command` 是 `docker exec ... <容器> pg_dump ...` 时自动推导，否则用本机 `pg_restore` |
| `operator_notes.root` | 否 | 操作者的 Claude Code 存放项目笔记的目录，必须是绝对路径 |

每一项按这个顺序取第一个：显式参数（`--host`、`--port`，以及把 `server.log_level` 设为 `debug` 的 `--verbose`）、
文件、然后仅本版本仍读的旧变量（环境里的 `LOOPER_DATABASE_URL`、`LOOPER_PUBLIC_URL`、`LOOPER_PG_DUMP`、
`LOOPER_BACKUP_CMD`、`LOOPER_BACKUP_VERIFY_CMD`、`LOOPER_OPERATOR_NOTES_ROOT`，以及除笔记根目录和校验命令外的 `LOOPX_*` 同名；这两项只认 `LOOPER_` 前缀）、再是 checkout 的 `.env` 里的同名、
最后是内置默认值。数据库 URL 没有默认值。用到旧变量就记一条弃用警告，只写变量名、不写值；下一版删除它们。
runtime root 是 `--runtime-root`，否则是 `LOOPER_RUNTIME_ROOT`（已弃用，会警告；`LOOPX_RUNTIME_ROOT` 同样），
否则是 `~/.looper`。文件本身不写 runtime root。`.env` 不再放进 Looper 的环境：只从中读上述旧设置，
其余的键（例如某个渠道 token 变量）会被忽略，并在警告里点名。

已有安装迁过来：跑一次 `looper config init-ops`。它按当前生效的值写成文件，权限 0600，已有文件则拒绝覆盖。
什么都没配时，写入旧的默认值 `postgresql://localhost:5432/loopx` 并说明。然后从 shell 和 `.env` 里删掉旧变量；
用了服务的话再跑一次 `looper service install`。在此之前，`looper serve` 仍按这些变量工作，并带上警告。

`LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS` 不是运维设置。它只用于测试和验证：定时任务两次触发之间的最小间隔（默认 300 秒），
`looper serve` 启动时读取。

**子进程。** 服务启动的每个进程（agent 的运行时和它拉起的 MCP 服务器、定时任务的前置检查、探测，以及 git、ps、lark-cli 这些辅助程序）
都从服务环境的一份允许名单起步（`looper/child_env.py`：系统路径、locale、XDG、SSH agent、git 身份和配置、代理和证书，
以及运行时自己的登录变量），再加上 Looper 为这个子进程设置的项（provider 绑定、agent 设置里的变量、MCP 令牌、gate 的运行变量）。
数据库 URL（新旧名字都算）、`PGPASSWORD`、任何 `LOOPER_*` 或 `LOOPX_*`，以及其他变量，都不传入。
数据库备份用自己的环境：上述名单，加上 libpq 的 `PG*` 变量、Docker 的变量，以及从 URL 取出、交给 `pg_dump` 的 `PGPASSWORD`。
agent 还需要别的变量时，从它的设置里取（Settings，或项目层）。允许名单里的登录变量，就是本机已登录的 CLI，
或通过这些变量给了 API key 的 CLI，仍能认证的原因。

这把秘密挡在子进程的环境之外。它不是沙箱。agent 以服务的用户运行，所以有 shell 的 agent 仍能读这个用户能读的文件：
运维文件、操作者令牌、`.env`、服务单元。要隔开，需要单独的系统用户或沙箱，这一版没有做。

渠道凭据不写进源码。Telegram 的 bot token 填在它的 Settings 字段里（存在数据库中），或放在服务的环境里
（`looper service install --env NAME`）；飞书凭据由 `lark-cli` 保管（见[渠道](#渠道)）。

### 渠道

网页对话始终开启。Telegram 和飞书是内置渠道，不需要命令行参数：在控制台的 **Settings** 页开启并配置。设置保存在数据库里，服务启动时读取，所以改动后要重启 `looper serve`。Telegram 的 bot token 与设置分开保存，接口不返回，导出也不包含。

| 渠道 | Settings 字段 | 另外需要 |
|---|---|---|
| Telegram | **Bot token**：BotFather 给的 token，单独保存，只显示是否已保存。`bot_token_env`：没有保存 token 时读取的环境变量（默认 `LOOPER_TELEGRAM_BOT_TOKEN`）。`allowed_user_ids`：允许和 bot 对话的 Telegram 数字用户 ID。 | 无，或者该环境变量里的 token。 |
| 飞书（lark-cli） | `profile`：lark-cli 的 profile，决定用哪个应用和机器人（为空用 lark-cli 当前的）。`allowed_open_ids`：发送者在该应用下的飞书 `open_id`（`ou_...`）；Settings → Channels 可按姓名或邮箱查找，并列出被拒的发送者以便允许。处理私聊，群聊里只处理 @ 机器人的消息。 | `PATH` 上有 `lark-cli` 并已登录；消息由它收发，启动时核对机器人身份。 |

- 每个管家都可以有自己的 Telegram 或飞书机器人：Settings → Channels →「添加机器人」，token 或 app secret 在那里录入（添加的机器人从不读环境变量）。机器人所属的管家在添加时定下，改动后重启生效。
- 每个 agent 运行 Claude Code 时都不弹权限确认，所以 Manager 能在这台机器上执行任何命令。因此每个渠道只转发名单上的发送者：其他人的消息会被丢弃并记一行日志（只记发送者 ID），不回复。名单为空或格式不对的渠道不会启动，Settings 页会写明原因。`LOOPX_TELEGRAM_ALLOWED_USERS` 已不再生效。
- 飞书凭据由 `lark-cli` 保管；渠道的 `profile`（或 lark-cli 当前的 profile）决定以哪个应用的身份收发（`lark-cli whoami`）。open_id 因应用而异，所以白名单记下它是为哪个应用建的，换到别的应用后不启动，直到重建名单。
- Manager 处理名单上发送者的消息时，机器人先给这条消息加一个表情（Telegram 私聊里是 👀，飞书里是「Get」），再用一条原地编辑的状态消息显示进展；回答和任务进度各自单独发消息。
- 在飞书群里，机器人只回答名单上的发送者 @ 它的消息，回复那条消息，并把任务进度和结束通知发到群里、@ 发起人。其他群消息一律忽略。
- 在 Telegram 或飞书里发起的任务完成或失败时，会在同一个聊天里通知，并附任务链接（见 `server.public_url`）。Telegram 私聊和飞书的对话会入库，在 Chat 页只读显示，并带进 Manager 新会话的续接简报。
- 在网页对话、飞书或 Telegram 里单独发送 `/new`，Manager 就开始新会话：下一条消息（不论来自哪个渠道）从全新的上下文开始。网页对话的 **New session** 按钮发的就是它。运行中的任务不受影响。
- 渠道启动失败会记日志，控制台照常运行。

### Agent 配置

每个角色（Manager、Planner、Worker、Reviewer）运行一个 **agent**：由运行时、LLM 提供方和模型组成，
保存为 Settings → Agent Profiles 里的一个 **Agent Profile**，在 Settings → Roles 里选用。

- **运行时**是 Looper 有适配器的某类 agent CLI（Claude Code、Codex 或 OMP）。运行时是内置的：Looper 在服务的 PATH 上找到它们并探测版本和能力，Settings → Agent Runtimes 列出它们并可 Re-detect；要换一个可执行文件，就改服务的 PATH。
- **提供方**是 agent 请求发往的地方：它支持的协议、模型及价格，以及认证方式：运行时自己的登录，或 API key（API 从不返回它）。
- 内置 agent `claude-code/opus`（Claude Code、它自己的登录、`opus`）运行所有没有另选 agent 的角色。

**分层设置**：harness prompt、agent（含模型和 effort）、环境变量和单次运行的花费上限这四项设置，经过四层解析，后面的层优先：

| 层 | 在哪里编辑 | 作用范围 |
|---|---|---|
| 系统 | Settings → Roles 的 Shared defaults | 所有 agent |
| 角色 | Settings → Roles，该角色的覆盖项 | 一个角色，不分项目：Manager、Planner、Worker 或 Reviewer |
| 项目 | 项目的 Agent Profiles 标签页，All roles in this project | 该项目的 Planner、Worker 和 Reviewer |
| 项目内角色 | 同一标签页，该角色的一行 | 某个项目里的其中一个角色 |

Manager 横跨所有项目，所以只有前两层。任务创建时还可以给它的 Planner、Worker 或 Reviewer 选 Agent Profile（`profile_overrides`），
这一选择优先于所有层，只对该任务生效。effort 属于 agent：要让某个角色用别的 effort，就给它选一个带该 effort 的 agent。没有任何一层设置的字段用内置默认值，控制台会显示每个字段的值及其来源层。
环境变量不用来放密钥：看起来像凭据的变量名会被拒绝。改动从每个 agent 的下一次运行或下一轮起生效。详见
[Harness, profile and environment](docs/architecture/agent-system.md#harness-profile-and-environment)。

**角色配置**：Settings → Roles 还编辑每个角色自己的字段（`role_configs`）：

- **system_prompt**：角色 prompt，说明这类 agent 负责什么；harness 放在它前面，可以重置为推荐的 prompt
- **max_turns**：对话轮数上限（0 表示不限）
- **timeout_seconds**：一次运行或一轮的时长上限（0 表示不限）。对 worker 来说，它是一次运行的总时长（默认 86400），有输出也不会延长；worker 从下一个步骤起应用这两个限制

并发只按任务算：同时运行的任务数不设上限，每个任务同时最多运行多少个 Worker 和 Reviewer（默认 16、8）。所有任务的默认值在 Settings → Roles 按角色设置，任务可以在自己的 Agents 标签页另设，定时任务也可以给它建出的任务设定。

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
