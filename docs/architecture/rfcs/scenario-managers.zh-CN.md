# RFC：场景管家（几个管家，各有各的职责，v0）

- **状态：** 阶段 1、2 已实现（[looper #388](https://github.com/foolzzz/looper/pull/388)、[#403](https://github.com/foolzzz/looper/pull/403)）；阶段 3 的存储、线程、停止、路由、API 与 Settings 已合入（[#646](https://github.com/foolzzz/looper/pull/646)、[#645](https://github.com/foolzzz/looper/pull/645)、[#650](https://github.com/foolzzz/looper/pull/650)、[#652](https://github.com/foolzzz/looper/pull/652)、[#655](https://github.com/foolzzz/looper/pull/655)、[#657](https://github.com/foolzzz/looper/pull/657)、[#658](https://github.com/foolzzz/looper/pull/658)），合成链路验收通过；路由边界日志、隔离 Codex 真实模型烟测与下述七项语义的维护者确认仍待完成，不标记阶段 3 已实现。以下为批准前的草案记录：草案，待维护者批准。第 1 节的四条是维护者 2026-10-21 定下的方向；D11（每轮的任务列表只列自己建的）和 D13（任务可见范围 `task_scope` 是独立于工具范围的设置）是维护者同日定下的；其余决定按本文建议写成。标「需要验证」的几处在实现或实跑时核实。阶段 1 的实现卡片在本仓库之外（`.lead/scnmgr/cards/`）。2026-10-21 按对照代码的评审修订：删除改为软删除、`/gates` 按管家、工具范围原子切换并在确认时复查、任务派生数据全部按归属过滤、工作区冲突匿名、聊天页丢弃过期响应、模板 pattern 在子进程里限时匹配、降级不再失败、启用上限在一个加锁事务里检查。同日按第二轮评审再修订：聊天页切换管家时清掉上一个管家已加载的列表和会话、停用时把正在处理的消息也记为已投递、每个 `enabled: true` 都加锁并在锁内重读、建任务和删除管家锁同一行、模板角色经 `start_over` 恢复、模板与调用合并成一份有效配置再走放置、软删除清空 `profile_id`、模板的 Profile 进使用检查和建任务时的全量 `check_selection`、机器人所属的管家建了不改。同日按第三轮评审再修订：停用时在取消循环**之前**记下正在处理的消息、排队和正在处理的任务汇报转交默认管家而不是丢掉、定义带修订号且每次工具调用按修订号复查（同步失败时拒绝而不沿用旧范围，并由管家组重试）、模板写入锁住管家行、web 任务的汇报经来源线程的聊天记录和会话订阅送达、模板的角色模板 key 不能重复；并写入维护者的两个决定：D11 定为「每个管家每轮的 `Ready tasks`、`Open gates` 和 `/gates` 只列自己建的任务」，任务可见范围拆成独立的 `managers.task_scope`（`all` / `own`，D13），原来 D5 里「`tasks` 管家只看自己的任务」改由 `task_scope = own` 决定，`tools` 只决定有哪些工具。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.0 节第 21 项。
- **相关：** [管家会话的 Agent Profile](manager-session-profile-v0.zh-CN.md)（一条全局会话、`/profile`）、[管家平台工具](manager-platform-tools-v0.zh-CN.md)、[停止管家的回复 v2](chat-stop-v2.zh-CN.md)（整轮令牌、工具调用归属）、[规划者定义的角色](planner-defined-roles-v0.zh-CN.md)（项目的角色模板）、[用户关卡](user-gate-v0.zh-CN.md)、[飞书机器人 token 渠道](lark-bot-token-channel-v0.zh-CN.md)。
- **代码基线：** v2.13.22（`a3c7a6f60`）。另一条分支正在改 Stop v2 的代码（轮的绑定、进程树、`tiers/manager.py`），本文按 `main` 设计，交汇处见第 15 节。

## 1. 需求与目标

维护者的需求（2026-10-21）：今天只有一个管家，是通用助手。还要有专门的管家，例如一个专做代码评审和修缺陷的：它和用户对话，建 MR Review 或 Bugfix 任务，交给它下面的 Planner。管家是用户定义的**场景**：系统提示词和其他配置把一个管家的职责说清楚。

已经定下的：

1. 几个管家同时在线，各有各的会话、上下文和花费；通用管家保持原样。
2. 渠道：先是每个管家一个机器人（每个管家可以有自己的飞书 / Telegram 机器人），再是按聊天绑定（一个飞书群或私聊、一个 Telegram 聊天绑定到一个管家）；绑定在 Settings 页配置。控制台聊天页让用户选管家。
3. 职责 = 提示词 + 任务模板：管家的模板（如 MR Review、Bugfix）预设代码在建任务时套用的任务配置（Planner 的说明、项目、角色 / 角色模板、Agent Profile、必填的输入如 MR 链接）；不靠提示词来强制。
4. MR 的来源：GitHub 的 pull request，以及其他代码托管平台的合并请求（例如公司内部平台）。仓库是公开的：代码、文档、测试数据里不出现内部链接、主机名、工具名或私有背景。平台相关的内容放在操作者配置的数据里（模板的说明、它可以用的命令行工具），不写成代码路径；仓库只附一个通用的 GitHub 示例模板。

**不做：**

- 不让管家用工具创建、修改管家或模板（包括它自己的）：管家的定义只在控制台改（D9）。
- 不按管家分插件、skill 和 MCP 的安装：所有管家共用 `manager` 角色的安装（第 17 节待办）。
- 阶段 1 不让场景管家接渠道：飞书、Telegram 仍只到通用管家（阶段 2、3 再做）。
- 阶段 1 不让定时任务属于场景管家：定时任务建的任务属于通用管家（第 17 节待办）。
- 阶段 1 不导出、不导入管家和模板（`config_transfer`；第 17 节待办，Settings 页写明）。
- 不按渠道或聊天分一个管家的会话：每个管家仍是一条会话，和今天一样（[管家会话 RFC](manager-session-profile-v0.zh-CN.md) D1）。

## 2. 现状与差距（读代码得出）

| 环节 | 现在怎么做 | 差距 |
|---|---|---|
| 身份 | 一个 `ManagerAgent`（`serve.py:854`），`agent_id` 固定返回 `"manager"`（`tiers/manager.py:554`），总线地址 `MANAGER_ADDRESS`（`message.py:261`）。所有渠道写死把消息投给它（`channels/web.py:183`、`telegram.py:434`、`lark.py:376`） | 只能有一个 |
| 会话 | `agent_sessions` 的 `manager` 一行，控制台、飞书、Telegram 共用；`/new`、`/profile` 作用于它；`selected_profile_id` 有 CHECK 约束 `agent_id = 'manager'`（`schema.sql:349`） | 第二个管家没有会话可存；约束拒绝第二个管家的选择 |
| `/profile` | 只切换这条会话的 Agent Profile（`session_profile.py`）；设置层只有系统层和 `manager` 角色层（`agent_settings._chain`） | 没有「一个管家自己的」职责、提示词或 Profile |
| 提示词 | `role_configs` 的 `manager` 一行 + 代码里的 `TOOLS_CONTRACT`（`tiers/manager.py:110`） | 只有一份 |
| 平台工具 | 一个 `ManagerOps`（`serve.py:881`）：一个令牌、一个「正在跑的轮」、一个绑定文件 `.local/manager-turn.json`、所有待确认操作；端点只认这一个令牌（`http_server.py:752`） | 两个管家共用会互相抢「当前轮」、确认串台 |
| 任务归属 | `tasks` 没有「谁建的」，只有来源渠道和聊天（`origin_channel_id`、`chat_id`…）。TaskRouter 把任务被接受、进度、结束一律发给 `MANAGER_ADDRESS`（`tiers/task.py:430`、`:477`），管家再转给来源渠道 | 任务回报不到建它的那个管家 |
| 每轮注入的上下文、关卡命令 | `Ready tasks`、`Open gates` 列出全部任务（`tiers/manager.py:1844`、`:1860`）；`/gates`、`/gate N`（`gate_commands.py`）不经 `ManagerOps`，全局列出并直接提交关卡答复 | 一个管家会看到、答复别的管家的任务和关卡 |
| 新会话的续接简报 | 最近 3 个聊天（所有非 Planner 线程，`db/chat.py:105`） | 会把别的管家的对话带进来 |
| 运行记录、花费 | `llm_records.agent_id = 'manager'`；按角色筛选写死 `agent_id = 'manager'`（`db/llm_records.py:226`）；`task_costs` 的 `CASE WHEN agent_id='manager'` | 第二个管家的花费会被当成未知角色 |
| 审计 | `manager_actions` 没有「哪个管家」 | 分不清 |
| Stop v2 | 每个 `ManagerAgent` 一个 `TurnRegistry`；`/api/chat/stop`、`/api/chat/turn` 只问 `self._manager`（`http_server.py:1025`、`:1048`）；`/api/status` 的 `turn_binding` 只有一个 `ManagerOps` 的 | 只能停一个管家的轮 |
| 聊天页 | `MANAGER_TARGET = {kind: "manager", id: "manager"}`（`web/src/pages/chat.tsx:105`），会话 id 存在一个 localStorage 键里 | 没有选管家的地方 |
| 建任务的工具 | `looper_task_create` 只收 requirement、title、project、work_dir、workspace_mode、base、goal_id、profiles（`ops/tasks.py:676`）；Planner 的说明、角色都不能预设 | 没有「模板」 |
| 定时任务 | 不经过管家（`scheduler.py:557`），建的任务没有来源聊天 | 无（阶段 1 归通用管家） |
| 渠道 | 每种渠道一行 `channel_configs`（`channel-telegram`、`channel-lark`、`channel-lark_bot`），一个机器人；`registry.notify`/`post` 按渠道类型找适配器（`channels/registry.py:48`、`:57`） | 阶段 2 要一种渠道多个机器人 |

## 3. 候选方案与取舍

| | A. 每个管家一个常驻实例（推荐） | B. 一个管家实例，按场景切换会话 | C. 每个管家一个 `looper serve` |
|---|---|---|---|
| 做法 | `ManagerAgent` 按「管家定义」参数化；一个 `ManagerFleet` 为每个启用的定义建一个实例和它自己的 `ManagerOps`（令牌、绑定文件、待确认操作） | 仍一个 `ManagerAgent`，每条消息按目标场景换系统提示词和会话（像 `/profile` 换会话那样） | 每个场景一个独立服务进程，共用数据库 |
| 复杂度 | 中：把散在各处的 `"manager"` 换成管家 id，`serve` 和 HTTP 改成按 id 取管家；每个实例内部的逻辑（轮锁、令牌、会话切换）不变 | 高：每轮换提示词就要重启常驻进程或在一个进程里来回 `--resume`；`TurnRegistry`、待确认、续接简报都要按场景再分一层 | 低（代码）/ 高（运维）：端口、令牌、渠道、调度器、Worker 池都重复，任务并发上限各算各的 |
| 资源 | 每个**用过的**管家一个常驻运行时进程（首条消息才启动，和今天一样）；闲着的管家不占进程、不花钱 | 一个进程，但每次切换重建缓存，花费高 | 每个场景一整套服务 |
| 故障半径 | 一个管家卡住只影响它自己（各自的轮锁和进程） | 一个卡住，所有场景都等 | 隔离最好 |
| 「同时在线」 | 满足 | 不满足：所有场景排一个队 | 满足 |
| 迁移 | 已有的管家就是 id 为 `manager` 的默认定义，所有新列默认 `manager` | 同 A | 要拆数据，渠道和定时任务归属难定 |

**推荐 A。** 它是唯一同时满足「同时在线」「各自的会话和花费」「通用管家不变」的方案，而且每个实例内部的代码路径（Stop v2 的令牌、会话切换、交接）一行不改，只是从一个变成几个。资源上加两道限制：同时启用的管家最多 `MAX_ENABLED_MANAGERS = 8` 个（含默认管家，API 拒绝第 9 个）；进程仍是首条消息才启动。闲置自动停进程记入待办（第 17 节）。

## 4. 决定一览

| # | 决定 | 落点 |
|---|---|---|
| D1 | 「管家」是一张表 `managers` 里的一行定义；它的 id 就是它的 agent id：默认管家 `manager`，其他 `manager-<key>`（`key` 是 `[a-z][a-z0-9-]{1,30}`，建了不改，删除后也不再给新管家用）。默认管家那一行由迁移建好，删不掉、不能停用，提示词和 Profile 仍走今天的 `role_configs.manager` 和 Settings → Roles。删除场景管家是软删除（`deleted_at`），它的聊天线程仍归它，不落到任何别的管家（第 5 节「删除」） | 第 5 节 |
| D2 | 每个启用的管家一个 `ManagerAgent` 和一个 `ManagerOps`（方案 A），由新模块 `looper/agents/managers.py` 的 `ManagerFleet` 管：启动时为每个启用的定义建一对；Settings 里新建、启用、停用、删除立即生效（停用就像停服务时那样停它：进行中的轮以「已停止」结束，它正在处理的那条消息在 journal 里记为已投递、重启不再重放；排队未处理的用户消息被取走并答「这个管家已停用」，排队的和正在处理的任务汇报转交默认管家；supervisor 排着的重启被取消）；改动同步到运行中的管家失败时由 `ManagerFleet` 重试，Settings 显示「未生效」，期间工具调用按数据库里的修订号复查、不沿用旧授权；启用数的上限在写定义的同一个加锁事务里检查（每个新建和每个 `enabled: true` 都加锁）；改提示词、Profile、模板不用重启，下一轮读到 | 第 6 节 |
| D3 | 一个管家的系统提示词 = 它的 harness（`manager` 角色层，共用）+ 它的职责提示词（默认管家：`role_configs.manager`；其他：`managers.system_prompt`，必填）+ `TOOLS_CONTRACT`（共用，不改字）+ 只在场景管家上才有的两段：范围契约（每个场景管家都有，按它的 `tools` 和 `task_scope` 说它有哪些工具、能看到哪些任务，D5、D13）和模板契约（有模板时）。默认管家的模型可见内容逐字节不变 | 第 12 节 |
| D4 | Profile 解析链加一层 `Layer.MANAGER`（管家定义的 `profile_id`），排在角色层之后、会话层之前：**会话的选择 > 管家定义 > `manager` 角色层 > 系统层 > 内置**。默认管家没有这一层（它的 `profile_id` 必须为空），行为不变 | `agent_settings._chain` |
| D5 | 管家能用的工具是类型化的 `tools`：`all`（今天的全部工具，默认管家固定是它）或 `tasks`（任务工具 + `looper_confirm`，加只读的 `looper_projects` 的 list / get）。`tools` 只决定**有哪些工具**；能看到、能动哪些任务由 D13 的 `task_scope` 决定，两者任意组合都有效。由 `ManagerOps` 在代码里强制，越界的调用答 `forbidden`；`tools` 或 `task_scope` 改了时整套授权状态一次换掉，待确认的操作在确认时按当时的授权重查；每次调用先比对定义的修订号（`managers.revision`），不一致就当场按数据库重建授权，重建失败答 `unavailable`、不用旧的 | 第 7 节 |
| D6 | 任务属于建它的管家：`tasks.manager_id`（默认 `manager`）。控制台建的任务、定时任务建的任务属于默认管家。TaskRouter 把任务被接受、进度、结束发给 `tasks.manager_id` 的管家，它再按任务记下的来源渠道和聊天转发（飞书、Telegram 照今天的路径；web 任务按建任务时记下的来源线程 `tasks.origin_thread` 写进那条聊天，第 6 节）；那个管家已停用或删除时退回默认管家，记一条警告；停用那一刻已排在它队列里的汇报转交默认管家（第 6 节「停用时的消息」） | `tiers/task.py` |
| D7 | 任务模板是一个管家的子表 `manager_task_templates`，字段有类型；代码在建任务时套用（第 8 节）。管家用 `looper_task_create` 的 `template` 和 `inputs` 选模板、给输入；缺输入、格式不对、和模板固定的配置冲突，工具答 `invalid` 并说该问用户什么。`task_policy = templates_only` 的管家必须用模板 | 第 8 节 |
| D8 | 建任务时把模板的说明、输入和复制进任务的角色定义**快照**进 `tasks.template`（JSON）；之后改模板不影响已建的任务，重新规划（`start_over`）时模板角色从快照恢复（第 8.4 节）。Planner 的 `## Task` 段在有快照时多出模板名、输入和说明 | `system_prompt.plan_task_section` |
| D9 | 管家不能用工具改任何管家或模板，也不能改自己的工具范围；定义只在控制台（操作者登录）里改 | 无工具 |
| D10 | 聊天线程属于一个管家，归属写下就不变：新表 `chat_threads(session_id, manager_id)`。没有行的线程（升级前的、飞书和 Telegram 的）属于默认管家。会话列表、续接简报、`/api/chat/turn` 都按它筛选；一个线程 id 已属于别的管家时 `POST /api/chat/send` 答 409 | 第 6 节 |
| D11 | **（维护者 2026-10-21 定）** 每个管家每轮注入的 `Ready tasks`、`Open gates` 和 `/gates`、`/gate N` 只列**它自己建的**任务（`tasks.manager_id = self.agent_id`；默认管家：`manager_id = 'manager'`，升级后等于全部，行为不变），与它的 `task_scope` 无关。默认管家（`task_scope = all`）仍能用工具看到、处理每个任务（`looper_tasks`、`looper_task_gate_reply` 等），只是场景管家的任务不进它每轮的上下文；`task_scope = all` 的场景管家同样。这样每个管家的上下文只装自己的事，默认管家不会顺手替场景管家的任务答关卡或开工 | `tiers/manager.py`、`gate_commands.py` |
| D12 | 渠道按定好的顺序：阶段 2 每个管家可以有自己的机器人（一个机器人一个运行器，凭据只在控制台录入）；阶段 3 按聊天绑定，未绑定聊天的消息归机器人所属的管家，机器人可设成「只接已绑定的聊天」 | 第 9 节 |
| D13 | **（维护者 2026-10-21 定）** 任务可见范围是独立的类型化设置 `managers.task_scope`：`all`（每个任务）或 `own`（只有这个管家建的任务）。默认管家固定 `all`（`managers_default_check`）；场景管家默认 `own`，操作者可以在 Settings → Managers 和 API 里设成 `all`。`task_scope` 决定这个管家的任务工具、概览的计数、花费和其他从任务派生的读数看到哪些任务、能动哪些任务（第 7 节）；`tools` 只决定有哪些工具。`task_scope` 的改动和 `tools` 一样原子地生效（`revision` 加一、`OpsScope` 一次换掉、待确认的操作在确认时复查） | 第 5、7 节 |

## 5. 数据模型与迁移

全部写在 `looper/db/schema.sql` 的 legacy 阶段（`tasks` 之后），幂等、每次启动都跑。

```sql
-- A Manager: the default one ('manager', the one every install had) or a scenario Manager the operator defined
-- (manager-<key>). Its id is its agent id: its session (agent_sessions), its runs (llm_records.agent_id), its bus
-- queue. The default Manager's prompt and profile stay in role_configs.manager and Settings → Roles, so its row
-- holds neither, uses every tool, sees every task and cannot be disabled.
CREATE TABLE IF NOT EXISTS managers (
    id            TEXT PRIMARY KEY
                  CONSTRAINT managers_id_check CHECK (id = 'manager' OR id ~ '^manager-[a-z][a-z0-9-]{1,30}$'),
    name          TEXT NOT NULL CONSTRAINT managers_name_check CHECK (btrim(name) <> '' AND length(name) <= 60),
    description   TEXT NOT NULL DEFAULT '' CONSTRAINT managers_description_check CHECK (length(description) <= 300),
    system_prompt TEXT,
    profile_id    TEXT REFERENCES agent_profiles(id) ON DELETE RESTRICT,
    tools         TEXT NOT NULL DEFAULT 'tasks' CONSTRAINT managers_tools_check CHECK (tools IN ('all', 'tasks')),
    -- Which tasks the Manager sees and may act on (its task tools, overview counts, spend, task-derived reads):
    -- 'all' every task, 'own' only the tasks it created. Independent of tools; any combination is valid.
    task_scope    TEXT NOT NULL DEFAULT 'own' CONSTRAINT managers_task_scope_check CHECK (task_scope IN ('all', 'own')),
    task_policy   TEXT NOT NULL DEFAULT 'free'
                  CONSTRAINT managers_task_policy_check CHECK (task_policy IN ('free', 'templates_only')),
    enabled       BOOLEAN NOT NULL DEFAULT true,
    -- Bumped in the same transaction by every write that changes what the Manager may do (tools, task_scope,
    -- task_policy, enabled, a template saved or removed): ManagerOps compares it at each call (section 7).
    revision      BIGINT NOT NULL DEFAULT 0,
    -- Deleted on the dashboard: the row stays so its chat threads keep their owner (they never fall to the default
    -- Manager) and its id is never reused; a deleted row is disabled and hidden from every view.
    deleted_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT managers_default_check CHECK (id <> 'manager' OR
        (system_prompt IS NULL AND profile_id IS NULL AND tools = 'all' AND task_scope = 'all' AND enabled
         AND deleted_at IS NULL)),
    CONSTRAINT managers_deleted_check CHECK (deleted_at IS NULL OR NOT enabled),
    CONSTRAINT managers_prompt_check CHECK (id = 'manager' OR btrim(coalesce(system_prompt, '')) <> '')
);
-- A deleted Manager's name is free again; its id is not.
CREATE UNIQUE INDEX IF NOT EXISTS managers_name_key ON managers (lower(btrim(name))) WHERE deleted_at IS NULL;
INSERT INTO managers (id, name, tools, task_scope) VALUES ('manager', 'Manager', 'all', 'all') ON CONFLICT (id) DO NOTHING;

-- A Manager's task templates: the task configuration code applies when the Manager creates a task with one
-- (looper/agents/task_templates.py checks every field; inputs is a typed list, see TemplateInput).
CREATE TABLE IF NOT EXISTS manager_task_templates (
    manager_id           TEXT NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
    key                  TEXT NOT NULL CONSTRAINT manager_task_templates_key_check
                         CHECK (key ~ '^[a-z][a-z0-9_-]{0,31}$'),
    name                 TEXT NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 60),
    description          TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 300),
    project_id           TEXT REFERENCES projects(id) ON DELETE RESTRICT,
    work_dir             TEXT NOT NULL DEFAULT '',
    workspace_mode       TEXT CHECK (workspace_mode IN ('worktree', 'in_place')),
    requirement          TEXT NOT NULL DEFAULT '',
    planner_instructions TEXT NOT NULL DEFAULT '',
    planner_profile_id   TEXT REFERENCES agent_profiles(id) ON DELETE RESTRICT,
    worker_profile_id    TEXT REFERENCES agent_profiles(id) ON DELETE RESTRICT,
    reviewer_profile_id  TEXT REFERENCES agent_profiles(id) ON DELETE RESTRICT,
    role_templates       TEXT[] NOT NULL DEFAULT '{}',
    inputs               JSONB NOT NULL DEFAULT '[]',
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_id, key)
);

-- The Manager a task belongs to: the one that created it, which hears of its progress and end
-- (the dashboard's and the schedules' tasks belong to the default Manager). template: what the template it was
-- created from said then (key, name, updated_at, inputs, planner_instructions, and roles: each task role it copied,
-- {role_key, kind, profile_id, role_prompt, harness, items, template_key}, so planning anew restores them);
-- NULL without one.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS manager_id TEXT NOT NULL DEFAULT 'manager'
    REFERENCES managers(id) ON DELETE RESTRICT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS template JSONB;
-- The web chat thread (chat_threads.session_id) a task was asked for in; its progress and end are written there.
-- NULL for a task from elsewhere (Lark, Telegram, the dashboard, a schedule).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS origin_thread TEXT;
CREATE INDEX IF NOT EXISTS idx_tasks_manager ON tasks (manager_id, created_at DESC);

-- Which Manager made a call.
ALTER TABLE manager_actions ADD COLUMN IF NOT EXISTS manager_id TEXT NOT NULL DEFAULT 'manager';

-- The Manager a chat thread belongs to, for good; a thread without a row (from before, Lark's and Telegram's) is
-- the default Manager's. A Manager is only ever soft-deleted, so the owner of a thread never changes or vanishes.
CREATE TABLE IF NOT EXISTS chat_threads (
    session_id  TEXT PRIMARY KEY,
    manager_id  TEXT NOT NULL REFERENCES managers(id) ON DELETE RESTRICT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A task role a template set is fixed like the user's: the Planner may not change it.
-- task_roles_via_check gains 'template' (drop and re-add, guarded by pg_constraint's definition).

-- Every Manager's session may hold its own profile choice (was: only 'manager'). The constraint keeps its name,
-- agent_sessions_selected_profile_manager, and only its definition widens (drop and re-add under the same name,
-- guarded by pg_get_constraintdef): an older binary adds that constraint only when no constraint of that name
-- exists, so it finds it and starts (see Downgrade).
--   CHECK (selected_profile_id IS NULL OR agent_id = 'manager' OR agent_id LIKE 'manager-%')
```

- **升级后的行为与今天逐字节相同：** 唯一的管家是 `manager`，所有任务、审计、线程都默认归它；`manager` 行的 CHECK 保证它没有自己的提示词、Profile，工具是 `all`、任务可见范围是 `all`。
- **降级（已核实旧版本的迁移）：** 旧版本的 `ensure_schema` 只在**没有同名约束**时才加 `agent_sessions_selected_profile_manager`（`schema.sql:341-350` 按 `conname` 查）。所以新版本不改约束名、只放宽定义：旧版本看到同名约束就跳过，场景管家会话行里的 Profile 选择不会让旧版本启动失败（若换了名字，旧版本会重加只允许 `manager` 的约束，表里已有场景会话的选择时 `ADD CONSTRAINT` 失败、服务起不来）。`task_roles_via_check` 由旧版本的 `ADD COLUMN IF NOT EXISTS` 带出，列已存在时不重加，放宽后的定义同样留下。旧版本对新表、新列一概不读：场景管家不运行（它们的会话行、线程留着，回到新版本后照旧）；场景管家建的任务被当成默认管家的，进度报给默认管家；`via = 'template'` 的角色在旧版本里 Planner 可以改（旧代码只拦 `user`）；场景管家的聊天线程会出现在旧版本默认管家的会话列表里（旧版本的列表不按归属筛，这是降级期间唯一的可见泄漏，Settings 页和发布说明写明「降级前先删掉不想让默认管家看到的场景会话，或接受这一点」）；删项目、删 Profile 时可能撞上模板的外键（报错，不丢数据）。测试：在新 schema 上跑一遍旧版本的那段 `DO $$` 块，断言它什么都不做。
- **删除（软删除）：** 默认管家删不掉（API 409）。一个场景管家还有任务时删不掉（409「先停用它」）。软删除只改行、不删行，`tasks.manager_id` 的外键对它不起作用，所以「有没有任务」和建任务必须锁同一行：
  - 删除在一个事务里：先 `SELECT … FROM managers WHERE id = $1 FOR UPDATE`（已删或不存在：404），在锁内 `EXISTS (SELECT 1 FROM tasks WHERE manager_id = $1)`（有：409），再写 `deleted_at = now()`、`enabled = false`、**`profile_id = NULL`**；删它的模板和 `agent_sessions` 行（会话和 Profile 选择不再有用）；**`chat_threads` 的行和聊天记录都不动**。事务外的预查只为给出清楚的报错，不作数。
  - 建任务（`ops/tasks._create`，有无模板都一样）在写任务的同一个事务里先 `SELECT enabled AND deleted_at IS NULL FROM managers WHERE id = $1 FOR KEY SHARE`，不是 true 就答 `conflict`「this Manager was turned off or deleted; the task was not created」、什么都不写。`FOR KEY SHARE` 和删除的 `FOR UPDATE` 互斥：建任务先拿到锁，删除等它提交后在锁内看到这个任务、答 409；删除先拿到锁，建任务等它提交后重读到 `deleted_at`、不建。它和启用上限的锁（默认行上的 `FOR NO KEY UPDATE`，第 6 节）不互斥，默认管家建任务不等启用。
  - **模板写入同样锁这一行：** 保存、删除模板（`PUT`/`DELETE /api/managers/<id>/templates/<key>`）在一个事务里先 `SELECT deleted_at FROM managers WHERE id = $1 FOR NO KEY UPDATE`（没有或已删：404），再写模板、`revision = revision + 1`。`FOR NO KEY UPDATE` 和删除的 `FOR UPDATE` 互斥：删除先拿到锁，模板写入等它提交后读到 `deleted_at`、答 404、什么都不写；模板写入先拿到锁，删除等它提交后照常删掉它的全部模板（包括刚写的）。只在事务外预查管家不行：软删除不删行，`manager_task_templates.manager_id` 的外键照样通过，删除之后写进来的模板会留在一个已删管家下面，连同它引用的 Profile 和项目一起删不掉（`profile_usage` 只算未删管家的模板，外键却拦着）。
  - 为什么清空 `profile_id`：已删的管家在每个视图里都隐藏、API 答 404，没人能再改它的 Profile；留着 `ON DELETE RESTRICT` 的引用，那个 Profile 就永远删不掉。

  于是它的线程仍归 `manager-<key>`：
  - 不出现在任何管家的会话列表里（默认管家的筛选是 `coalesce(t.manager_id, 'manager') = 'manager'`，有行且属于已删管家的线程不满足）；
  - 不进任何管家的 `[RESUME]` 续接简报（同一个筛选）；
  - `GET /api/chat/sessions?manager=<已删的 id>` 答 404；`POST /api/chat/send` 带这样的线程 id 答 409「这段对话属于已删除的管家」；`GET /api/chat/messages?session_id=` 仍能按 id 读（操作者登录后的审计用途，和今天读任何线程一样）；
  - 它的 key 不能再建新管家（409「key <k> was used by a deleted Manager」），所以旧线程永远不会归到一个同名的新管家下；名称可以再用。
  被删管家的 `llm_records`、`manager_actions` 保留（审计）。不做物理删除和清理工具（第 17 节待办）。
- **Profile 和项目的删除检查：** `db.profile_registry.profile_usage` 多算未删除的管家定义和模板引用的 Profile，`usage_text` 写成「Manager <name>」「template <name> of Manager <name>」；删项目时，模板引用它答 409 并点名模板。外键是兜底。
- **Profile 被改时的角色检查：** `profile_registry._selected_roles`（改 Profile 时拒绝「改完跑不了它被选中的角色」）多算三处：任一管家会话的选择和未删除管家定义的 `profile_id` → `manager`；模板的 `planner_profile_id` / `worker_profile_id` / `reviewer_profile_id` → `planner` / `worker` / `reviewer`。建任务时的 `check_selection` 见 8.3 第 5 步（兜底：直接改库的情况）。
- **名称：** 默认管家的 `name` 迁移时写 `Manager`；控制台的显示名改读 `managers.name`（今天读 `role_configs.manager.display_name`，seed 时是 `Manager (管家)`）。为了不变：迁移在 `managers` 行刚插入时，若 `role_configs.manager.display_name` 非空就用它（`UPDATE ... WHERE name = 'Manager'`，只做一次，`schema_migrations` 记 `managers_default_name`）。

## 6. 身份：每处「唯一的 manager」怎么改

| 地方 | 改成 |
|---|---|
| `ManagerAgent.agent_id`、`agent_address` | 来自构造时给的 `ManagerDefinition`（`id`）；`message.manager_address(id)`，`MANAGER_ADDRESS = manager_address("manager")` 保留 |
| 总线 | 每个实例 `subscribe(self.agent_id)`；`_receive` 已按 `envelope.target.agent_id` 过滤，几个消费者并存时各取各的（`bus._hand_to_consumer` 依次问，第一个答 True 的拿走）。重启后重放的消息投给已不存在的管家：丢弃并记一条警告 |
| 常驻进程 | `resident_session(self.agent_id)`（`tiers/manager.py:532`、`:943`）。默认管家仍是 `"manager"`；Codex 的会话目录、DSH 的 owner 按这个名字分开 |
| 会话 | `agent_sessions` 的行 = 管家 id；`/new`、`/profile`、交接、续接简报都只碰自己的行（代码已经用 `self.agent_id`，只是常量变了） |
| 会话层 Profile | `db.agent_setting_layers.list_layers(manager_session=<id>)`：合成的 `session` 行取 `agent_sessions WHERE agent_id = $id`；`load_agent_settings(..., manager_id=)`，`tiers/manager.py` 里**每一处**调用都传 `manager_id=self.agent_id`，包括 `manager_session=False` 的两处（`settings_profile_name`，`:1459`；`select_session_profile` 清除选择时的候选，`:1525`），否则场景管家的 `/profile 0` 会显示、校验默认管家的配置；`db.profile_registry.profile_usage` 和 `profile_registry._selected_roles` 看所有管家的会话 |
| 运行记录 | `ClaudeRunRecorder(agent_id=self.agent_id)`（`:954`、`:1617`、`:1747`），`start_record(agent_id=self.agent_id)` 已是 |
| 花费 | `db.llm_records._ROLE_FILTERS["manager"]` 和 `task_costs` 的 `CASE` 改成 `agent_id = 'manager' OR agent_id LIKE 'manager-%'`；新增按管家汇总（第 13 节） |
| Stop v2 | 每个实例自己的 `TurnRegistry`（不变）；`/api/chat/stop` 按 `message_id` 在所有管家里找（`ManagerFleet.turn_for`）；`/api/chat/turn?session_id=` 用线程的归属找管家；阶段 3 的原生 `/stop` 经 `ManagerFleet.stop_chat` 停同一 `(channel_id, chat_id)` 在所有管家里的活轮（含排队轮），详见 9.2；控制台仍按 message id 停指定轮 |
| 工具调用的绑定 | 每个管家一个绑定文件：`.local/<agent_id>-turn.json`（默认管家仍是 `.local/manager-turn.json`）；每个 `ManagerOps` 自己的令牌，端点按令牌找到管家（`ManagerFleet.ops_for(authorization)`，逐个 `hmac.compare_digest`） |
| 待确认操作 | 在各自的 `ManagerOps` 里（`chat_key` 不跨管家）；`/new` 的 `forget_confirmations` 只清自己的 |
| 聊天队列（composer） | 只在浏览器里，按线程 id 存（`sessionStorage`）；线程属于一个管家，所以自然分开 |
| 关卡 | 关卡的消息仍按来源渠道和聊天投递（`gate_service.gate_chat`），不经管家；`Open gates` 段按 `tasks.manager_id` 筛（D11）；`looper_task_gate_reply` 受这个管家的 `task_scope` 约束（`all` 的管家可以答任何任务的关卡，只是 `Open gates` 不列别人的）；渠道里对关卡消息的回复仍在收到消息的管家里先路由到关卡（`gate_routing`），阶段 1 只有默认管家接渠道 |
| `/gates`、`/gate N` | `gate_commands` 今天不经 `ManagerOps`、全局读关卡（`_waiting` 在非群聊时 `open_gates(LISTED)`）并直接提交答复。改成按收到命令的管家：`handle(..., manager_id=self.agent_id)`；`_waiting` 的两支都把 `manager_id` 传给 `open_gates`（和 `Open gates` 段同一个筛选：自己建的任务，D11）；编号快照的键加上管家 id（`(manager_id, channel, chat, sender)`），快照条目记下任务的 `manager_id`；`/gate N` 提交前再读一次任务，任务已不是这个管家建的就不提交、答「这个关卡不在这个管家的范围里」并重新列出。管家看不到的关卡，编号、列表、提交三处都碰不到 |
| 任务结束通知、进度 | TaskRouter 的 `TASK_ACCEPTED`、`GOAL_STATUS` 发给 `manager_address(task.manager_id)`（`tiers/task.py:430`、`:477`）；`Task` 从数据库行读 `manager_id`、`origin_thread`，两种汇报的 metadata 带上 `origin_thread`。管家的 `_handle_goal_status` 照旧转给飞书、Telegram 的来源聊天，并更新它自己的「当前活跃目标」。**web 任务另走来源线程：** 今天 web 的汇报按建任务那条消息的 correlation 发 `STATUS_UPDATE`，但 `channels/web.py` 在那条消息答完时就删掉了它的 SSE 队列（`iter_sse_events` 在 `answer.final` 后 `pop`），之后的进度和结束一律「no active SSE stream」被丢，任务也没记来源线程。所以 `_create` 在来源是 `channel-web` 时把消息的 `channel_context.session_id` 写进 `tasks.origin_thread`；管家收到带 `origin_thread` 的 `TASK_ACCEPTED` 和**结束**的 `GOAL_STATUS` 时，发一条 `AGENT_REPLY`，`payload.chat_session_id = origin_thread`、文字是「任务 <title> 已分解为 N 个步骤」/「任务 <title> 已完成 / 失败 / 已取消：<summary>」——web 适配器的 `deliver` 对带 `chat_session_id` 的回复走 `_add_to_chat`：存进那条线程的聊天记录，并推给订阅这个线程的页面（`/api/chat/events?session=`）。回答已结束、页面关了或刷新过都不影响：刷新后从聊天记录读到。中间进度（非结束的 `GOAL_STATUS`）只在那条消息的流还开着时照今天推送，不进聊天记录（避免刷屏）。这对默认管家的 web 任务同样生效（第 18 节） |
| 定时任务 | `scheduler._create_task` 不传 `manager_id`，默认 `manager`；不变 |
| 工作区冲突 | 冲突检测仍是全局的：`task_workspaces.check` 照旧看所有管家进行中的任务（两个管家的任务在同一个仓库里原地运行一样会互相覆盖）。但 `task_scope = own` 的管家拿到的 `shared` 条目匿名：`check(..., visible_to=<管家 id>)`，别的管家的任务只剩 `{"task_id": null, "name": "a task of another Manager", "status": null, "mode": <mode>, "foreign": true}`（`mode` 留着，`overwrites` 要用），提醒的文字和 `configuration_block` 写「另有 N 个其他管家的任务在这个仓库进行中」，不出现名称、状态或 id。`task_scope = all` 的管家和控制台照旧看到全部 |
| 平台工具里的来源地址 | `ops/tasks.py` 里 `source=MANAGER_ADDRESS`（`:105`、`:487`、`:528`、`:576`、`:627`）改成 `call.manager_address`（`Call` 从 `Platform.manager` 取） |
| 审计 | `ManagerOps._record` 写 `manager_actions.manager_id`；任务页的「Manager 操作」显示是哪个管家 |
| `supervisor` | 每个实例 `register`；新增 `start_agent`、`stop_agent`。`stop_agent(id)`：先摘掉它的条目（健康检查不再看它），**取消并等待**条目上排着的 `entry.restarting`（否则旧的恢复任务会按同一个 id 再起一个实例），再 `agent.stop()`。`start_agent` 起不来时照 `start_all` 的做法进入 `_schedule_restart`。`AGENT_LIFECYCLE` 仍发给默认管家（它只记日志） |
| 停用时的消息 | `ManagerAgent.stop()` 今天只结算活的令牌，不清总线队列，也不碰正在处理的那条：`bus.subscribe` 在消费者处理完、取下一条时才把上一条记为已投递（`bus.py:186-192`），`stop()` 在 `_handle` 中途取消了循环，那条就一直是未投递，停用→启用→重启服务后会被重放、可能再执行一次操作。**正在处理的那条必须在取消之前记下：** 取消会跑 `_handle_user_message` 的 `finally`（`token.settle` → `TurnRegistry.drop` 把令牌移出登记表），循环里记「正在处理」的变量也在 `finally` 里清掉，取消之后再采集就什么都拿不到。所以 `stop()` 第一步（`cancel_and_wait` 之前）记下 `stopped_in_flight`：正在处理的信封（`_handling`）和所有活令牌的信封，之后才取消、结算令牌、停进程。停用（`ManagerFleet._retire`）随后：① `ManagerFleet` 先把它标成不在运行（TaskRouter 的 `manager_running` 从此答 False，新的汇报直接发给默认管家），再 `supervisor.stop_agent`；② 处理 `stopped_in_flight`：用户消息先发一条 `outcome=stopped` 的最终回复（页面的聊天流等的就是它，只 settle 不会结束流），再 `bus.settle(id)`；任务汇报（`GOAL_STATUS`、`TASK_ACCEPTED`、`WORK_RESULT`）**转交**默认管家（下面）；③ `bus.take_queue(id)`（取出、**不**记为已投递）：用户消息答一句「这个管家已停用，这条消息没有处理」（经它来源渠道的回复路径，web 聊天里显示为回复）后 settle；任务汇报转交；其他（`AGENT_LIFECYCLE`）直接 settle。**转交** = 以默认管家为目标重新 `publish` 一份（新的 `message_id`，同样的类型、payload、correlation、metadata，metadata 加 `handed_over_from`），发布进 journal 之后才 settle 原消息：任务结束后 TaskRouter 不会再发汇报（`tiers/task.py:484-486` 已 teardown），最后的 `GOAL_STATUS` 和其中的结束通知（`notice`）丢了就永远丢了；两步之间崩溃只会在重启后再转交一次（至多重复一条进度，不会丢）。正在处理的汇报可能已转发了一半，同样转交（宁可重复，不丢结束通知）。停服务时的 `stop()` 不 settle、不转交，重启后的重放和今天一样。重启服务时 journal 重放给停用或已删管家的消息按 ③ 同样处理 |
| 改动生效 | 定义和模板的每次写入都在同一事务里 `revision = revision + 1`，提交后调 `ManagerFleet.apply(id)`（起、停实例，换 `OpsScope`，第 7 节）。`apply` 失败（读库失败、实例起不来）不撤销写入：`ManagerFleet` 记下 `sync_error` 并按 2、5、15、30、60 秒（之后每 60 秒）重试，直到成功或被下一次 `apply` 取代；`GET /api/managers` 的 `state.sync` 为 `pending` 并带原因，Settings 显示「改动未生效，正在重试」。重试期间旧授权**不**生效：`ManagerOps` 每次调用先比对数据库的 `revision`（第 7 节）。运行中的管家每轮开始时（`_load_db_config`）读到自己已停用或已删，就不跑这一轮、答「这个管家已停用」并再触发一次 `apply` |
| 启用数上限 | **每个**新建和**每个**带 `enabled: true` 的更新（不论事务外读到它是否已启用）都在一个事务里：先 `SELECT … FROM managers WHERE id = 'manager' FOR NO KEY UPDATE`（默认行做所有启用的串行锁），更新时再 `SELECT … WHERE id = $1 FOR UPDATE` 重读目标行（已删：404；同时进行的停用要么已提交、要么等这个事务），目标在锁内读到是停用的才数 `enabled AND deleted_at IS NULL`，满 8 个答 409，否则写。只凭事务外读到「已启用」就跳过锁不行：那之后别的请求可以停用它、启用另一个填满 8 个，再无锁地把它写回启用，成了 9 个。`ManagerFleet` 启动时的上限只是兜底（直接改库的情况） |
| 概览 | `overview` 的 `agents.manager` 保留（默认管家），新增 `agents.managers` 列表 |
| `chat_targets` | 列出所有启用的管家（默认管家在前），`{"kind": "manager", "id": "<agent id>"}`；`resolve_chat_target` 认 `id`，不认识的和已删除的答 400，停用的答 409 |
| DSH 运行时 | `runtimes/dsh.py:955` 给 `role == "manager"` 的是模块常量 `MANAGER_TOOLS`；改成这个管家的 `ManagerOps` 的工具（D5） |

## 7. 管家能看到、能改什么

两个设置各管一件事，任意组合都有效：`tools` 决定**有哪些工具**，`task_scope` 决定**能看到、能动哪些任务**（D5、D13）。默认管家固定 `tools = all`、`task_scope = all`；场景管家默认 `tools = tasks`、`task_scope = own`。

| `tools` | 工具 |
|---|---|
| `all` | 今天的全部 |
| `tasks` | `looper_overview`、`looper_tasks`、`looper_task_create`、`looper_task_control`、`looper_task_instruct`、`looper_task_gate_reply`、`looper_task_update`、`looper_confirm`、`looper_projects`（只读：list、get） |

| `task_scope` | 任务的读（任务工具、概览、花费、任务派生的读数） | 任务的写（每个带任务 id 的工具、确认） |
|---|---|---|
| `all` | 所有任务（今天的行为） | 所有任务 |
| `own` | 只有 `manager_id` 是自己的；任务派生的读数也只算自己的（见下面的列表） | 只有自己的；别的任务答 `forbidden`：「task … belongs to another Manager; this Manager acts only on the tasks it created」 |

与两个设置都无关的：每轮注入 `Projects`（全部）、**自己建的** `Ready tasks`、`Open gates`（D11，`task_scope = all` 也只列自己建的），有模板时再加 `Task templates`；管家和模板谁都不能用工具改（D9）。

- **授权状态是一个值：** `ManagerOps` 的工具字典、只读工具集合、是否只看自己的任务（`scoped`，即 `task_scope = own`；与 `tools` 无关）、`looper_task_create` 的模板变体，合成一个不可变的 `OpsScope`（`ops/service.py`），由 `scope_for(definition, template_keys)` 从定义算出。`ManagerOps` 只持有一个 `self._scope` 引用；`call` 开头取一次 `scope = self._scope`，这一次调用从头到尾都用它。改了 `tools`、`task_scope`、`task_policy` 或模板时，`ManagerFleet.apply` 调 `ops.set_scope(new)`，一次赋值换掉全部，不存在「工具换了、只读集合还是旧的」或「工具换了、任务范围还是旧的」的中间状态。`Call` 的 `scoped`、`manager_id` 也从这个 scope 读，不再从 `platform.manager.definition` 读。
- **授权以数据库为准，同步失败时不沿用旧的：** `OpsScope` 带上算出它的定义的 `revision`。`call`、`_confirm` 和 MCP 的 `tools/list` 都先经 `ManagerOps.current_scope()`：读一次 `managers` 的 `revision, enabled, deleted_at`（主键读）；和 `self._scope.revision` 相同就用它；不同就当场按数据库重建（读定义和模板 key，`scope_for`，`set_scope`）再用；管家已停用或删除答 `forbidden`「this Manager was turned off」；读库或重建失败答 `unavailable`「this Manager's settings changed and are not in force yet; nothing was done; try again in a moment」，**不**回落到旧的 `self._scope`。所以 `all → tasks` 提交之后，即使 `ManagerFleet.apply` 失败、还在重试，下一次 `looper_settings` 调用也已按 `tasks` 判断（或因读不到库被拒），旧的 `OpsScope(all)` 不会再放行任何调用；`task_scope` 从 `all` 改成 `own` 同理，下一次对别的管家任务的调用已答 `forbidden`。
- **一处授权检查，调用时和确认时都跑：** `ManagerOps._authorize(scope, tool_name, arguments) -> Tool`：工具在 `scope` 里（不在：`forbidden`「this Manager no longer has <tool>」）；只读工具的动作是读（`looper_projects` 用 `Tool.risk_of` 判断，写的动作答 `forbidden` 并说「ask the user to use the general Manager or the dashboard」）；`scoped`（`task_scope = own`）时任务 id 指向的任务属于自己。`call` 在 `escalate` 前调它；`_confirm` 在执行 `pending.tool.run` 之前用**当前**的 `self._scope` 再调一次，并用返回的当前 `Tool` 对象执行（不是挂起时存下的那个）。不再允许时：删掉这条待确认、记一条 `status='error'`、`error_code='forbidden'` 的审计、答 `forbidden`「the operation was not run: this Manager may no longer do it」。`set_scope` 换范围时也顺手清掉这个管家里工具已不在新范围的待确认；`task_scope` 从 `all` 换成 `own` 时，针对别的管家任务的待确认不在这里清（判断要读库），由确认时的复查拒绝。
- 下一轮 MCP 的 `tools/list` 按新的 scope 列；运行时下一次启动时看到（常驻进程的工具列表在启动时读，签名里加工具列表的哈希，变了就 `--resume` 重启）。在那之前模型若仍调旧工具，`_authorize` 答 `forbidden`。
- **任务派生的读数（`task_scope = own` 的管家，不论 `tools`）：** 只算 `tasks.manager_id` 是自己的。
  - `looper_overview`：`build_overview(..., manager_id=)` 给出**同样的键**（`ops/tasks._overview` 直接索引 `view["spend_today"]`、`view["health"]`，不能少键）：`tasks` 的分组计数、`planners`、`workers`、`reviewers`（Worker / Reviewer 的 `step_runs` 按 `task_id` 属于自己的任务筛）、`projects` 的每项计数和 `idle_projects` 都只从自己的任务算；`spend_today` 是这个管家自己的运行加上它的任务的运行（`spend_since(since, manager_id=)`：`r.agent_id = $m OR t.manager_id = $m`）；`health.unhealthy` 只含这个管家自己的条目；`schedules` 同样形状、内容为空（阶段 1 定时任务属于默认管家）。`_overview` 的 `waiting_gates` 用 `open_gates(..., manager_id=)`。`task_scope = all` 的管家和 `/api/overview` 不传 `manager_id`，逐字节同今天。
  - `looper_tasks` 的 list（`list_tasks(manager_id=)`），get / room / 每个任务工具（任务 id 检查）。
  - `looper_projects` 的 list / get：`tasks`、`active_tasks` 只数自己的任务，`schedules` 为 0。
  - 建任务时的工作区冲突提醒：别的管家的任务匿名（第 6 节「工作区冲突」）。
  - `looper_task_gate_reply`（任务 id 检查）。`/gates` 不看 `task_scope`，只列自己建的（D11）。
  - `tools = all` 的管家另有的读（`looper_settings`、`looper_schedules` 等）不是任务派生的，`task_scope` 不管它们。
- 为什么场景管家默认 `own`：场景管家的提示词是操作者写的、职责窄，看到别的管家的任务只会被带偏，也会把一个场景的内容泄到另一个场景的对话里。操作者要一个管理员式的场景管家时可以设 `all`。默认管家固定 `all`，是管理员式的全局视角。

## 8. 任务模板

### 8.1 类型

`looper/agents/task_templates.py`（纯模块，没有数据库）：

```python
class InputKind(str, Enum):
    TEXT = "text"
    URL = "url"          # http(s), with a host

@dataclass(frozen=True)
class TemplateInput:
    key: str             # ^[a-z][a-z0-9_]{0,31}$, unique in the template
    label: str           # 1-60 chars: what the Manager asks the user for
    kind: InputKind
    required: bool = True
    pattern: str = ""    # optional regex the whole value must match, <= 300 chars; matched off the event loop (8.1)
    hint: str = ""       # <= 200 chars: the expected form, said in the error and in the Manager's section

@dataclass(frozen=True)
class TaskTemplate:
    manager_id: str; key: str; name: str; description: str
    project_id: str | None; work_dir: str; workspace_mode: str | None
    requirement: str              # {{input_key}} placeholders; <= TASK_REQUIREMENT_LIMIT
    planner_instructions: str     # <= 8000 chars
    profiles: Mapping[str, str]   # planner / worker / reviewer -> profile id
    role_templates: tuple[str, ...]
    inputs: tuple[TemplateInput, ...]   # at most 10
    updated_at: datetime | None
```

保存时（HTTP API）检查，错误答 400，消息说哪个字段怎么改：

- `requirement` 里的占位符只能是 `{{<inputs 里的 key>}}`；未知占位符、未闭合的 `{{` 都拒绝。
- `pattern` 能编译；`kind = url` 的输入值在建任务时还要是 `http`/`https` 且有主机。
- **pattern 不在共享的事件循环里跑：** Python 的 `re` 是回溯实现，`(a+)+$` 这类操作者写的 pattern 对几十个字符的输入就能跑几秒（评审在隔离子进程里实测 33 个字符超过 1 秒），而所有管家共用一个事件循环。所以编译（保存时）和匹配（建任务时）都在一个**一次性子进程**里做：`task_templates.match_inputs(checks: list[(pattern, value)]) -> list[bool]` 用 `asyncio.create_subprocess_exec(sys.executable, "-I", "-c", _MATCHER)`，经 stdin 传 JSON，硬超时 `PATTERN_TIMEOUT_SEC = 2`，超时就 `kill()` 并等它退出。一次建任务所有输入一起送（至多 10 个），只启动一个进程；没有 pattern 的模板不启动。超时答 `invalid`「the template <name>'s pattern for <label> took too long to check: tell the user to simplify it on the dashboard」，什么都不写；保存时也用同一个子进程拿一个固定的探测串（`"a" * 64 + "!"`）跑一次，超时就 400「pattern is too slow (nested repetition such as (a+)+): simplify it」。不引入新依赖（`re2` 之类要原生扩展）；建任务很少，几十毫秒的进程启动可以接受。
- `role_templates` 非空时必须固定 `project_id`，每个 key 是那个项目已有的角色模板，且**不能重复**（400「role_templates lists <key> twice」）：建任务时每个 key 复制成一个版本 1 的 `task_roles` 行，重复的 key 会撞主键 `(task_id, role_key, version)`（`schema.sql:1155`），整笔建任务回滚。
- 每个 Profile 存在、能跑那个角色（`profile_registry.check_selection`），项目固定时在项目的可选列表里（`projects.profile_choices` 的同一套检查）。
- `work_dir` 非空时是绝对路径（`resolve_work_dir`）；`workspace_mode` 是枚举。

### 8.2 管家怎么选模板

- 有模板的管家每轮多一段 `Task templates`（在 `Ready tasks` 前）：每个模板一行 `key`、名称、描述（截到 120 字）、固定的项目，下面是输入：`key`、label、必填与否、hint。
- `looper_task_create` 对有模板的管家多两个参数：`template`（枚举，就是它的模板 key）和 `inputs`（对象）；`project` 不再是 schema 里的必填（由代码判断）。没有模板的管家，包括默认管家，schema 不变。
- 选哪个模板由模型按对话判断，代码只校验；没有关键词分类。

### 8.3 建任务时代码做什么（`ops/tasks._create`，按顺序，任何一步失败什么都不写）

1. `template` 缺失：`task_policy = templates_only` 时答 `invalid`：「this Manager creates tasks only from its templates (…keys…): pick the one that fits, or tell the user none fits」；否则走今天的路径。
2. 读模板（`manager_id` 是这个管家的）；没有：`invalid`，列出 key。
3. 输入：未知 key `invalid`；必填的缺失或空白：`invalid`「the template <name> needs <label> (<hint>): ask the user for it; never make one up」；`url` 不是 http(s)、`pattern` 不匹配：`invalid`「<label> must look like <hint>」；每个值去掉首尾空白，至多 2000 字。pattern 在子进程里限时匹配（8.1）。
4. **模板固定的字段**：模板里设了值的 `project_id`、`work_dir`、`workspace_mode`、每个角色的 Profile、`role_templates` 是固定的。参数里不给就用模板的；给了**相同的值**就接受（项目名忽略大小写；目录用 `task_workspaces.same_directory` 比较；`workspace_mode` 相等；Profile id 相等）；给了不同的值答 `invalid`「the template <name> sets <field> to <value>: leave <field> out」，什么都不写。参数里的值从不覆盖模板的值。
   **合并成一份有效配置，确认和创建都只用它：** `effective_placement(template, arguments)` 先算出 `project`、`work_dir`、`workspace_mode` 三个值，每个都是「模板设了取模板的，没设取调用的」：项目固定时是模板的项目行（按 `project_id` 读，不按名字找），没固定时是调用的 `project`（照今天按名字找，找不到就是新项目）；目录是模板的 `work_dir`，模板没设就是调用的 `work_dir`，都没有才是项目的默认目录，再没有就照今天答「问用户要目录」。然后今天的 `_placement` 改成吃这份有效配置（`_placement(effective)`），`_new_project`（要不要问「新建项目」）和 `_create` 都调同一个 `effective_placement`，所以确认时问的和最后建的是同一个项目和目录。由此：项目固定、模板没设目录时，调用给的合法 `work_dir` 生效；项目没固定、模板设了目录时，模板的目录生效。
5. Profile：按第 4 步的规则合并（模板设了的角色只能不给或给同一个），其余角色照今天的 `profiles`。**合并后的每个 Profile**（模板的和调用的）都再过一次 `profile_registry.check_selection(id, [role])`，不能跑那个角色就答 `conflict`「the template <name>'s <role> Agent Profile <name> can no longer run the <role>: tell the user to fix the template on the dashboard」（调用给的那个照今天答 `invalid`）。模板的 Profile 保存时检查过，但之后可能被改；`_selected_roles` 已经拦下这种修改（第 5 节），这一步是直接改库时的兜底。
6. 角色：`role_templates` 非空时，项目必须开着规划者定义的角色（`ProjectExecution.roles_on`），否则 `conflict`「the template's project has Planner-defined roles off; tell the user to turn them on or change the template」；每个模板 key 还在，否则 `conflict`。
7. 需求 = 模板的 `requirement` 套上输入：对**原始模板文本**做一遍 `re.sub(r"\{\{([a-z][a-z0-9_]*)\}\}", …)`，每个占位符换成对应的值，插进去的值不再被解释（输入 `first = "{{second}}"` 原样留在需求里，和快照一致）；不做别的模板语言。然后空行，再接管家写的 `requirement`；总长超过 `TASK_REQUIREMENT_LIMIT` 答 `invalid`。`workspace_mode` 按第 4 步取定后传给 `task_workspace`。
8. 同一事务里：先锁管家行并确认它仍启用、未删除（第 5 节「删除」的 `FOR KEY SHARE`），再建任务（`manager_id`、`template` 快照，快照的 `roles` 是下面复制的每个角色的完整定义）、写 Profile 覆盖、把角色模板复制成 `task_roles`（版本 1，`via = 'template'`，`template_key`，和 Planner 复制时同样的字段）。
9. `configuration_block` 多一行「模板：<name>」和输入。

### 8.4 Planner 看到什么

`plan_task_section` 在有快照时，在 `Requirement:` 之前加：

```
Template: <name> (set by the operator for this kind of task)
Template inputs:
- <label>: <value>
Template instructions (follow them; they are the operator's):
<planner_instructions>
```

没有快照时一个字节都不多。说明只给 Planner，不给 Worker、Reviewer；Planner 按说明写步骤。`via = 'template'` 的角色和 `via = 'user'` 一样是固定的，Planner 不能改（`task_roles` 里判断 `via` 的地方一起改）。

**重新规划（`start_over`）保留模板角色：** 今天 `looper_task_control` 的 `start_over`（`tiers/task.py:637`）调 `db.tasks.reset_task_for_restart(keep_steps=False)`，它删掉这个任务的**全部** `task_roles`（`db/tasks.py:389-391`）；模板快照若只存说明和输入，固定角色就丢了，新的 Planner 也不知道它们存在。所以快照存每个复制进来的角色的完整定义（第 5 节注释的 `roles`），`reset_task_for_restart` 改成在一个事务里：照旧删掉全部角色，再把快照里的每个角色重新写成版本 1、`via = 'template'`、`template_key` 同前（Profile 已被删除的写 NULL，和 `task_roles.profile_id` 的 `ON DELETE SET NULL` 一致），并写一条 `role_defined` 日志「Role <key> v1 restored from the task template <name>」。用户在任务页改过的版本（`via = 'user'`）和 Planner 定义的角色照今天随重新规划一起去掉；模板角色回到建任务时的样子。`keep_steps`（接着做）不动角色，和今天一样。不用快照、改成「删除时跳过 `via = 'template'` 的行」不行：用户改过的模板角色的当前版本是 `via = 'user'`，跳过就会留下半截版本链。

### 8.5 MR 来源与示例

- 代码里没有任何平台特有的分支。GitHub 的 pull request 和其他平台的合并请求都只是一个 `kind = url` 的输入，`pattern` 和 `hint` 由操作者填，怎么取 diff、怎么评论写在模板的 `planner_instructions` 里（例如「用 `gh pr diff` 读改动」，或操作者自己平台的命令行工具）。
- 仓库附一个示例：`docs/examples/manager-github-pr-review.json`（管家：「Code review」，`tools = tasks`，`task_scope = own`，`task_policy = templates_only`；模板 `pr_review`：输入 `pr_url`，`pattern` 为 `https://github\.com/[^/]+/[^/]+/pull/[0-9]+`，说明里用 `gh`；模板 `bugfix`：输入 `issue`（text））。Settings 页的「从示例新建」把它填进表单，不直接保存。测试把示例跑一遍保存校验，示例坏了 CI 就红。

## 9. 渠道（阶段 2、3）

### 9.1 阶段 2：每个管家一个机器人

（2026-10-22 按阶段 1 合入后的代码修订，与原稿不同的几处：Telegram 机器人的唯一性按 token 自带的 bot id 判断，不调 `getMe`；定时任务的通知和「控制台任务的关卡聊天」不加 `channel_id` 列，仍只走原来的三行；关卡消息表保留按类型的唯一约束以便降级；停用的管家的机器人照常启动并答「已停用」；有机器人的管家删不掉。）

- **数据：** `channel_configs` 加 `manager_id TEXT NOT NULL DEFAULT 'manager' REFERENCES managers(id) ON DELETE RESTRICT`。今天的三行（`channel-telegram`、`channel-lark`、`channel-lark_bot`，下称「内置的行」）仍是默认管家的，删不掉。Settings → Channels 每种渠道可以「添加机器人」，选一个未删除的管家（默认管家也可以）；新行的 `channel_id` 是 `channel-<type>-<n>`，`<n>` 取自只增的序列 `channel_bot_seq`（从 2 起，各类型共用，删了也不复用）。渠道 id 的格式由一个函数解析（`message.parse_channel_id`：`channel-(telegram|lark_bot|lark)(-<n>)?`），任何地方都不再拼 `channel-<type>` 或按前缀猜。
- **凭据只在控制台录入，不进日志：** 新机器人的 Telegram token、飞书 app secret 只进 `channel_secrets`；新行不读环境变量、不接受 `bot_token_env`（环境变量的回退只留给内置的 Telegram 行）；新的 lark-cli 机器人必须写明 `profile`（空 profile 是 lark-cli 当前激活的那个，只留给内置行）。每个机器人的 token 和 app secret 在建适配器时登记到日志脱敏（`log_redaction.add_secret`；今天只有 Telegram 登记，飞书 app secret 一起补上）。API 只返回 `has_bot_token`。
- **同一个机器人不能加两次：** Telegram 按 token 自带的 bot id（token 的格式是 `<bot id>:<secret>`，冒号前那段；内置行没存 token 时用它环境变量里的 token），飞书机器人 token 按 `app_id`，lark-cli 按 `profile`，各在同类型的行里唯一；新建和改凭据时在一个加了事务级咨询锁（`pg_advisory_xact_lock`）的事务里检查并写入，冲突答 409 并点名已有的那一行。启动时再查一遍（兜底：直接改库），重复的后一行不启动并记日志。
- **一个机器人一个运行器：** 每个启用的行一个适配器实例（Telegram 一个轮询、飞书机器人一条长连接、lark-cli 一个 `event consume` 进程），各自的白名单、被拒发送者记录（`channel_rejected_senders` 按 `channel_id`）、状态消息和 correlation 表；适配器把消息投给自己行的 `manager_address(manager_id)`。管家停用时（`ManagerFleet.is_running` 答 False）机器人照常运行，收到的消息不进总线，直接答「这个管家已停用，这条消息没有处理」；重新启用立即恢复（管家的启停不用重启，机器人的改动要）。默认管家的机器人不做这个检查，和今天一样。
- **投递按渠道 id，不再按类型：** `registry.notify`/`post` 收 `channel_id`；任务结束通知 `Notice` 带上 `channel_id`（来自 `tasks.origin_channel_id`；journal 里旧的 payload 没有它时按类型取内置行），管家、等待超时提醒、规划者改并发的提醒都发给这个 id 的适配器；`task_notice.notify_channel` 认 `channel-<type>-<n>`。
- **关卡：** `GateChat` 带 `channel_id`。`task_user_gate_posts` 和 `task_user_gate_messages` 加 `channel_id` 列（旧行回填 `'channel-' || channel`），按回复找关卡（`find_reference`）、「这个聊天的 Open gates」、`/gates` 的编号快照都按（`channel_id`，聊天）比，不按类型：同一个 Telegram 用户和两个机器人的私聊 chat id 相同、消息 id 各自从 1 数，按类型比会串。`task_user_gate_posts` 原来的 `UNIQUE (channel, chat, external_id)` **保留**（旧版本的 `ON CONFLICT (channel, chat, external_id)` 要它，删了降级后发关卡消息会报错），另加 `UNIQUE (channel_id, chat, external_id)`，写入改成不带冲突目标的 `ON CONFLICT DO NOTHING`。代价：两个同类型机器人在同一个聊天 id 里恰好发出同一个消息 id 时，后一条不记下，回复它的消息当普通消息进管家（不会进错关卡），记一条 info 日志；待办见第 17 节。
- **定时任务和控制台任务的关卡聊天不变：** 定时任务属于默认管家（第 1 节「不做」），它的通知目标（`schedules.notify_channel` 存类型）和 Settings → Channels 的「控制台任务的关卡发到哪个聊天」都只在内置的行里选、只经内置的行发；`schedules.notify_targets` 只列内置行的白名单。只有内置行时和今天逐字节相同。
- 聊天记录的线程 id：内置的行仍是 `<type>:<chat_id>`；新机器人用 `<channel_id>:<chat_id>`，同一个飞书群里两个机器人的对话不会混；场景管家的机器人的线程写进 `chat_threads` 归它（阶段 1 的 `_persist_chat` 已经这样做），默认管家的新机器人的线程不写行、照旧归默认管家。`chat_messages.channel` 仍存类型。
- **机器人所属的管家建了不改：** 线程 id 只由 `channel_id` 和聊天决定，归属一经写下不变（D10）。若允许把机器人从管家 A 改给 B，B 收到的消息仍写进归 A 的 `<channel_id>:<chat_id>` 线程：B 的会话列表看不到它，A 的会话列表和 `[RESUME]` 却装着 B 的对话。所以 `channel_configs.manager_id` 只在新建机器人时写：`PUT` 带不同的 `manager_id` 答 400「a bot's Manager cannot change: add a new bot for the other Manager and remove this one」；数据库再加一道 `BEFORE UPDATE` 触发器拒绝改这一列（兜底，直接改库的情况）。内置的三行永远属于默认管家。换管家 = 删掉旧机器人、为新管家添加一个：新行拿新的 `<n>`（同一个 bot token 重新添加也拿新的），所以新线程 id 和旧的不会撞，旧线程留在 A 名下。要让一个聊天换管家而不换机器人，用阶段 3 的按聊天绑定（下一节的后缀规则正是为此）。
- **删除：** 内置的行删不掉（400，「关掉它」）；删除新机器人删掉它的行、凭据（`channel_secrets` 级联）和被拒发送者记录，线程和聊天记录不动；正在运行的适配器到重启才停（和改机器人一样）。之后经它建的任务的结束通知发不出去（记日志），任务页照常。管家还有机器人时删不掉：`delete_manager` 在锁住管家行的同一个事务里查 `channel_configs`，有就 409「先在 Settings → Channels 删掉它的机器人」；新建机器人在写入的事务里对管家行 `FOR KEY SHARE`，和删除的 `FOR UPDATE` 互斥。
- 改机器人仍要重启生效（和今天的渠道一样）；Settings 照今天的 `start_problem` 显示原因。飞书的状态和人员搜索使用 `POST /api/v1/channels/status`、`POST /api/v1/channels/people`，请求体的 `channel_type` 区分 `lark` 与 `lark_bot`，可选 `channel_id` 不带就是内置行。
- **配置导出、导入（`config_transfer`）：** 今天按 `channel_type` 取唯一一行（`config_transfer.py` `_Target.read` 的 `setdefault(row["channel_type"], row)`），导入也按类型匹配：同一类型多个机器人时会漏掉或互相覆盖。阶段 2 的做法：导出、导入都按 `channel_id` 对应；只带内置的三行（属于默认管家），**新加的机器人不导出**，导出摘要和 Settings → Import / Export 写明「每个管家的机器人不随配置导出」；导入的文件里出现 `channel-<type>-<n>` 或 `manager_id` 不是 `manager` 的渠道行时整份拒绝（400，点名那一行），不猜。机器人随管家和模板一起进导出是第 17 节的待办（要先有管家的导出）。测试：同类型两个机器人时导出只含内置那行、导入后另一个机器人原样不动。
- **降级：** 旧版本不读 `channel_configs.manager_id`、只按固定的三个 id 启动渠道，新机器人不运行（行和凭据留着，回到新版本后照旧）；触发器只在 `manager_id` 变化时拒绝（`BEFORE UPDATE OF manager_id ... WHEN (OLD.manager_id IS DISTINCT FROM NEW.manager_id)`），旧版本改渠道配置不受影响；旧版本写的关卡消息行 `channel_id` 为 NULL，新版本每次启动回填（`UPDATE ... SET channel_id = 'channel-' || channel WHERE channel_id IS NULL AND channel IS NOT NULL`，幂等）；按类型的唯一约束没删，旧版本的 `ON CONFLICT` 照常。经新机器人建的任务在旧版本里 `notify_channel` 认不出它的 `origin_channel_id`，没有结束通知和关卡聊天（任务页照常）。
- **模型可见的内容不变：** 每条消息前的 `Channel:` 行只说类型（「a Telegram direct message」），不说是哪个机器人；所以默认管家和场景管家的提示词、工具都不因阶段 2 改变，`tests/agent_launch_parity.py`、`tests/model_launch_parity.py` 不加声明的差异。

### 9.2 阶段 3：按聊天绑定

以下记录已合入的行为现状；停用目标、seen 留存、策略热更新、最近 20 条历史、混合运行时停止、聊天身份和 Manager 工具范围这七项推荐语义尚待维护者确认，不能据实现反推批准。最终状态见本文首行，失败或未测路径继续保持待验收。

- **身份与准入：** `manager_chat_bindings` 的主键是 `(channel_id, chat_id)`，同一原生 chat id 在两个机器人下互不影响；发送者白名单、Lark 群 @ 和消息资格先查。有显式绑定就投给目标；否则归机器人所属管家。目标可以是未删除但停用的管家，新消息答「已停用」、绝不回落；已删除的目标拒绝绑定，有绑定的管家必须先解绑才能删除，绑定与删除锁同一管家行。Telegram 负数 chat id 继续原有转发，仍不存记录、不加表情。
- **只接绑定聊天：** `channel_configs.bound_chats_only` 是顶层列，默认 `false`；开启后未显式绑定的合格消息不进总线、不回复、不加表情或状态，Telegram 本地命令和 `/stop` 也静默。显式绑到机器人所属管家与「未绑定」不同：前者可准入，且沿用不带后缀的身份。绑定、解绑及策略提交后通知运行中 registry，按同一快照立即作用于新入站；凭据及其他机器人启动配置仍需重启。
- **可见聊天：** `channel_seen_chats` 从升级后的合格白名单消息开始累计元数据，保存至删除机器人，不回填旧记录、不读取旧消息体、不增加账号权限或名称查询。名称可空，界面回退 chat id；完整目录和显式绑定取并集，按 chat id 文本顺序分页。`channel_unbound_chats` 只保留最近 20 个被策略抑制聊天的名称、次数和最后时间；绑定不清除该历史，界面同时显示当前绑定状态，它不是完整目录。
- **线程归属固定：** 机器人所属管家的线程仍为 `<type>:<chat_id>`（内置机器人）或 `<channel_id>:<chat_id>`；其他管家的线程为 `<那个 id>#<manager_id>`。改绑只让下一条消息进入新管家的线程，不迁移、重命名或删除历史；再绑回去复用原线程，列表、读取与续接简报只按存储的 owner。历史深链接经 `URLSearchParams` 编码，`#` 后缀必须作为参数值保留。
- **已接受的工作不改目的地：** 进行中的轮由原管家答完，回复和记录使用准入时保存的线程、机器人与聊天；已建任务的接受、进度与结束沿用 `tasks.manager_id` 和来源列。解绑或开启策略不追溯过滤这些出站投递，阶段 2 的历史、任务与 capability 行为不变。
- **停止是聊天控制：** 原生 `/stop` 经 `ManagerFleet.stop_chat` 对该机器人、该聊天的所有 owner 活轮各请求一次，即使最新目标已停用；别的机器人、别的聊天不受影响。每个 owner 都尝试，混合运行时的不支持或失败须明确报告，不能声称全部停止；Codex 的 `cannot_interrupt` 与既有开关契约保持不变。普通文字仍只去当前目标，未绑定且只接绑定聊天时 `/stop` 也不产生输出。
- **管理入口：** Settings → Managers 列出该管家的显式绑定并支持改绑、解绑；Channels 列出完整聊天目录和最近抑制记录，支持绑定及策略。五个类型化 `operationId` 是 `manager_chat_bindings.list`、`manager_chat_bindings.put`、`manager_chat_bindings.delete`、`channel_chats.list`、`channel_chats.set_policy`（对应 `/api/v1/manager_chat_bindings/{list,put,delete}`、`/api/v1/channel_chats/{list,set_policy}`）；请求、200 `OpOk` 结果和 `Binding` / `SeenChat` / `UnboundChat` schema 以生成的 `GET /api/v1/openapi.json` 为准。只允许 operator 认证，client token 不能直接或经确认调用；生成的 `loopercli manager-chat-bindings {list,put,delete}`、`loopercli channel-chats {list,set-policy}` 使用 operator token。`ManagerOps`、`MANAGER_TOOLS`、`scope_for`、工具描述与注入上下文均不增加绑定能力，包括只读能力。
- **配置与降级：** 绑定、seen 目录和抑制记录不随配置导出、导入；普通配置导入保留顶层 `bound_chats_only`。旧二进制忽略新绑定和策略，可能处理新版静默的聊天：降级前必须由操作者停用受影响的机器人运行器，或接受旧行为；不做破坏性降迁移，不动已有历史。
- **观测与验收：** 沿用第 13 节日志，不新增指标系统；相关准入、路由、配置与停止边界应包含 channel/chat/destination、action/outcome，绝不记录消息体或凭据，当前绑定、解绑、策略更新和原生准入成功/静默边界缺少这些字段，日志验收不满足，须回到前置实现补齐后重测。已通过实际 API/生成 CLI、三种合成原生 transport、真实 Ops 任务写库、稳定历史、任务汇报、fleet stop、两张 Settings 页和隔离服务重启；Codex Profile 烟测使用现有 CLI 替身，缺隔离凭据的真实 Codex 模型烟测未运行，完整套件留 CI。模型可见字节未变，真实 prompt eval 不适用，parity 断言不变。

## 10. HTTP API（阶段 1）

| 方法与路径 | 说明 |
|---|---|
| `GET /api/managers` | `{"managers": [...], "max_enabled": 8}`；每个：`id`、`name`、`description`、`builtin`、`enabled`、`tools`、`task_scope`、`task_policy`、`system_prompt`（默认管家为 null）、`profile_id`、`profile_name`、`templates`（完整字段）、`state`（`running`：进程已起；`busy`；`healthy`；`sync`：`ok` 或 `pending`，`pending` 时带 `sync_error`，第 6 节「改动生效」）、`spend`（`today_usd`、`last_7d_usd`） |
| `POST /api/managers` | `{key, name, description?, system_prompt, profile_id?, tools?, task_scope?, task_policy?, enabled?}` → 201 和这个管家（`tools` 默认 `tasks`，`task_scope` 默认 `own`）。400：字段不对（未知字段、key 格式、名称重复、提示词为空、Profile 不能跑管家、`tools` 或 `task_scope` 不是枚举值）；409：已启用 8 个（在写入的同一个加锁事务里数，第 6 节「启用数上限」）、key 已被删除的管家用过 |
| `PUT /api/managers/<id>` | 部分更新，字段同上（`key` 不能改）；默认管家只能改 `name`、`description`，其余（包括 `task_scope`）400；改 `tools`、`task_scope`、`task_policy`、`enabled` 都让 `revision` 加一并调 `ManagerFleet.apply`（第 6 节「改动生效」）；凡带 `enabled: true` 都和新建一样在加锁事务里重读、数上限（409）；已删的 404 |
| `DELETE /api/managers/<id>` | 默认管家 409；有任务 409「disable it instead」（在锁住这一行的事务里判断，第 5 节「删除」）；否则软删除并清空 `profile_id`，停掉它的实例；已删的 id 之后答 404 |
| `PUT /api/managers/<id>/templates/<key>` | 新建或替换一个模板，校验见 8.1；`DELETE` 同路径；两者都在锁住管家行的事务里写（第 5 节「删除」），管家已删答 404 |
| `GET /api/manager/session?manager=<id>` | 不带 `manager` 就是默认管家（今天的行为） |
| `PUT /api/manager/session/profile` | body 可带 `manager` |
| `POST /api/chat/send` | `target` 可以是 `{"kind": "manager", "id": "<id>"}`；`new_session_profile_id` 对任一管家的 `/new` 都可以；线程属于别的管家时 409 |
| `GET /api/chat/turn?session_id=` | 按线程归属找管家 |
| `POST /api/chat/stop` | 按 `message_id` 在所有管家里找 |
| `GET /api/chat/sessions?manager=<id>` | 不带就是默认管家的线程（今天的列表本来就只有它）；带 `session_id` 的单条查询同样按 `manager` 筛；已删或不存在的管家 404 |
| `GET /api/chat/targets` | 管家在前，每个带 `name` |
| `GET /api/tasks`、`/api/tasks/<id>` | 多 `manager_id`、`manager_name`、`template`（快照） |
| `GET /api/status` | 多 `managers: [{id, healthy, busy, turn_binding}]`；顶层 `turn_binding` 是各管家之和（Stop v2 第 10 节第 6 条看的就是它） |

## 11. 界面（阶段 1）

- **聊天页：** 头部的 agent 下拉今天在 Tasks 之前只有一行 Manager，改成每个启用的管家一行（名称、描述），选哪行就和哪个管家对话，头部显示所选管家的名称；只有默认管家时界面和今天一样。URL 仍是 `#chat?target=<agent id>&conversation=<session id>`，`manager` 省略。每个管家自己的线程 id：默认管家仍用 `looper-chat-session-id`，其他用 `looper-chat-session-id:<id>`；**线程指针的每一处读和写**都经 `sessionKey(managerId)`（今天 `chat.tsx` 有读的 `getOrCreateSessionId` 和写的 `switchWebSession`，`:505` 写死全局键；场景管家里新建或打开历史时若写了全局键，切回默认管家就会显示错的线程并被 409 拒发）。头部的 Agent Profile 选择、New session、Stop 都作用于选中的管家。会话列表只列选中管家的，按 id 打开一条会话（`fetchChatSession(sessionId, managerId)`）也带管家，effect 依赖它。
- **切换管家时丢弃过期响应：** 历史、会话列表、按 id 打开的会话、管家会话视图这些加载都是异步的；从 A 切到 B 后 A 的响应晚到，不能写进 B 的画面或 B 的本地缓存。每个这类 effect 用 `let alive = true` + 清理函数里 `alive = false`（React 的做法，今天的轮询已这样写），并在应用结果前比对它发起时的 `(managerId, sessionId)` 与当前的是否相同；不同就丢弃。本地缓存（`saveMessages`）按**发起请求时**的 session id 写，不按当前的。测试让两个请求逆序完成（先发的后到）：画面和缓存都是后选的那个管家的。
- **已加载的状态绑定管家，切换时清空：** 丢弃迟到响应还不够。今天切换目标（`applyTarget`，`chat.tsx:538-547`）只清 `messages`，已经到手的会话列表 `conversations`、按 id 打开的 `opened` 和 `viewingMessages` 都留着；B 的列表回来之前，列表里还是 A 的行，点一条 A 的 web 历史会经 `openConversation` → `switchWebSession` 把 A 的线程 id 写进 B 的指针（`chat.tsx:475-517`），消息接口按 id 读，画面随即显示 A 的对话（发送时才被 409 拒）。所以这三个状态都带上它所属的管家（`{managerId, rows}`、`{managerId, id, session, error}`），渲染和 `knownConversation` 只用 `managerId` 等于当前管家的那份；`applyTarget` 换管家时把三者置空、`appliedConversation` 清零；`openConversation` 只接受当前管家列表里的行（行的 `managerId` 和当前不同就什么都不做）。测试：A 的列表已加载，切到 B 且 B 的列表未返回时，列表区不显示 A 的任何行、`localStorage` 里 B 的指针不变、`fetchChatMessages` 没有被以 A 的线程 id 调用；地址带着 A 的会话 id 而目标是 B 时（`fetchChatSession(id, B)` 答 404），同样不切换 B 的指针。
- **Settings → Managers**（新页，在 Roles 之后）：列表（名称、描述、工具范围、任务可见范围、模板数、是否启用、今天的花费）；表单（key、名称、描述、职责提示词、Agent Profile「跟随 Manager 角色」或一个、工具范围（全部工具 / 只有任务工具）、任务可见范围（只有它建的任务 / 所有任务，新建时默认前者，各配一行说明它能看到、能动哪些任务）、建任务方式、启用）；每个管家下的模板列表和模板表单（项目、目录、运行方式、需求模板、Planner 说明、三个角色的 Profile、角色模板多选、输入表格：key、label、类型、必填、pattern、hint）；「从示例新建」。默认管家一行只能改名称和描述，并链到 Settings → Roles。`state.sync = pending` 的管家在列表和表单上显示「改动未生效，正在重试：<原因>」。页面说明写明「管家和模板暂不随配置导出」。照 `agent-profiles.tsx` 的 `RegistryList` / `Row` / `FormFooter`。
- **任务页：** 详情的头部多一行「由 <管家> 创建 · 模板 <name>」，展开看输入；任务列表不变（筛选记入非核心待办）。

## 12. 模型可见的改动

| 谁 | 改动 | 默认管家 / 普通任务 |
|---|---|---|
| 场景管家 | 系统提示词：harness + `managers.system_prompt` + `TOOLS_CONTRACT` + `SCOPE_CONTRACT`（每个场景管家，按 `tools` 和 `task_scope` 取句）+ `TEMPLATES_CONTRACT`（有模板时） | 不变 |
| 场景管家 | 每轮的 `Task templates` 段（有模板时） | 不变 |
| 有模板的管家 | `looper_task_create` 多 `template`、`inputs` | 不变 |
| Planner | `## Task` 段的模板三项（任务有快照时） | 不变 |

`SCOPE_CONTRACT`（定稿时改字，意思不变）：每个场景管家都有，一段标题加两句，一句按 `tools` 取、一句按 `task_scope` 取，说清它有哪些工具、**能看到哪些任务**。默认管家没有这一段。

```
## This Manager's scope
<tools = tasks>  You have the task tools only; the projects you can only list
                 and read. Agent Profiles, providers, settings, skills and
                 schedules are not yours: when asked, tell the user to use the
                 general Manager or the dashboard.
<tools = all>    You have every platform tool.
<task_scope = own> You see and act on only the tasks you created. Other
                 Managers' tasks are hidden from you and not yours: when asked
                 about one, tell the user to use the general Manager or the
                 dashboard.
<task_scope = all> You see and act on every task, also those other Managers
                 created. The Ready tasks and Open gates in each message list
                 only the tasks you created; find the others with looper_tasks.
```

（尖括号是选句的条件，不进提示词。）

`TEMPLATES_CONTRACT`：

```
## Task templates
Each user message lists your task templates. Create a task with the template
that fits (looper_task_create with template and inputs); the template sets the
project, the roles and how the Planner works. Ask the user for every required
input you do not have, as the template's hint describes it, and never make one
up. Even when you have every input, first restate the task with its template
and inputs and ask the user to confirm; create it only after the user confirms,
as for any task. When no template fits, say so[ and do not create a task].
```

「Even when you have every input …」一句是实跑评测后加的：opus xhigh 的管家在输入齐全时把操作者提示词里的「create the task」当成可以直接建，`template_review_task` 0/2；加上后 opus xhigh 5/5、sonnet 2/2。

方括号里那句只在 `templates_only` 时有。默认管家和没有模板的任务一个字节都不变，所以 `tests/agent_launch_parity.py` 和 `tests/model_launch_parity.py` 的已声明差异**不增加**；实现时若不得已改了默认管家或 Planner 的可见内容，就在 `model_registry_contract` / `planner_wait_contract` 里声明 `(before, after)`，不重新生成固定文件。

## 13. 可观测性

- 后端没有指标系统，不新加。运行记录天然按管家分：`llm_records.agent_id`；Runs 页按 agent 筛选已经可用。
- `GET /api/managers` 每个管家的 `spend`（`llm_records` 按 `agent_id` 汇总今天和最近 7 天），`state`。
- `/api/status` 的 `managers`（第 10 节）；`binding_mismatch` 的日志 `extra` 多 `manager`。
- 日志：`tiers/manager.py` 的 `"manager: …"` 前缀改成 `self._log`（默认管家仍是 `manager:`，其他是 `manager-review:`），便于按管家 grep。`ManagerFleet` 启停每个管家记一行 info。
- 审计：`manager_actions.manager_id`；任务页的 Manager 操作列显示管家名。

## 14. 风险

| 风险 | 缓解 |
|---|---|
| 资源：每个管家一个常驻进程（Claude Code 几百 MB 内存） | 首条消息才起；启用上限 8；闲置自动停记入待办；停用立即停 |
| 花费：每个管家各自重建缓存 | 各自独立计量（第 13 节），Settings 显示今天的花费；Profile 可单独选便宜的 |
| 跨管家泄漏 | 各自的令牌、绑定文件、待确认操作、会话行；续接简报、会话列表按 `chat_threads` 筛，删除是软删除、线程归属不变；`Ready tasks`、`Open gates`、`/gates` 按 `manager_id` 筛；`task_scope = own` 的管家的读写和任务派生的读数限于自己的任务（代码强制；场景管家默认 `own`，设成 `all` 是操作者的明确选择），工作区冲突里别的管家的任务匿名；聊天页丢弃切换前的迟到响应，并清空上一个管家已加载的列表和会话；机器人所属的管家建了不改（阶段 2）。默认管家（`all`）看全部，这是它的职责 |
| 授权变了、旧确认仍在 | 授权状态（工具和任务范围）是一个 `OpsScope` 值、一次换掉；确认时按当时的 scope 重查，不再允许就不执行；同步失败时按 `revision` 当场重建或拒绝，不沿用旧的 |
| 操作者写了慢 pattern | 匹配在一次性子进程里、2 秒硬超时，事件循环不等它 |
| 降级 | 约束名不变、只放宽定义，旧版本照常启动；可见的差异写在第 5 节 |
| 一个管家卡住 | 各自的轮锁和进程，卡住只影响它；Stop v2 停它的轮；supervisor 照旧重启不健康的实例；停用它立即停 |
| 越权：管家改自己的范围 | 没有改管家的工具（D9）；`tools`、`task_scope` 只在控制台改 |
| 模板被改坏 | 保存时校验；建任务时再校验一遍（项目、角色、Profile 可能之后变了），错误说清楚；已建任务用快照 |
| 与 Stop v2 分支冲突 | 第 15 节；阶段 1 在 `tiers/manager.py` 里只做常量替换和构造参数 |

## 15. 与 Stop v2 分支的交汇点

那条分支在改轮的绑定、进程树和 `tiers/manager.py`。本设计碰到的地方，后合入的一方要对照：

1. `ManagerAgent.__init__` 的签名（加 `definition`）和 `resident_session("manager")` 的两处（`:532`、`:943`）：进程树的名字从这里来。
2. `ClaudeRunRecorder(agent_id="manager")`（`:954`）和交接轮、自主轮的记录。
3. 绑定文件：`serve.py:882` 的 `manager-turn.json` 变成每个管家一个；`ManagerOps.publish_binding`、MCP 桥读 `LOOPER_AGENT_TURN_FILE` 的方式不变。
4. `/api/chat/stop`、`/api/chat/turn` 的处理（`http_server.py:1010–1056`）改成经 `ManagerFleet`；`StopResult` 的语义不变。
5. `/api/status` 的 `turn_binding`：保持为各管家之和，Stop v2 第 10 节的「默认打开」标准不受影响。
6. `_receive` 的总线消费者：几个管家并存时各按 `target.agent_id` 认领。
7. 阶段 3 的 `/stop` 跨管家停一个聊天的令牌，需要 `TurnRegistry.live_for` 保持公开。

做法：阶段 1 先合入的话，Stop v2 分支 rebase 时把它新增的 `"manager"` 字面量也换成 `self.agent_id`；Stop v2 先合入的话，阶段 1 的卡片 5 在它之上做同样的替换。两边的测试（`tests/test_manager_stop_*.py`、`tests/test_manager_turn_binding.py`）都必须在合并后的分支上通过。

## 16. 测试计划（照开发计划第 1 节）

- **单元：** `task_templates` 的校验和渲染（占位符、pattern、url、上限；`(a+)+$` 对 40 个 `a` 加 `!` 在 2 秒内答超时且事件循环上另一个协程照常推进；`{{first}}` 的值是 `{{second}}` 时原样保留）；`OpsScope` 的切换和确认时复查（`tools` 和 `task_scope` 各自改、一起改）；`_chain` 的 `Layer.MANAGER`；工具范围的过滤；`chat_targets`；TaskRouter 按 `manager_id` 回报；`plan_task_section` 有无快照；系统提示词的组成（默认管家逐字节不变、场景管家的两段，`SCOPE_CONTRACT` 按 `tools` × `task_scope` 的四种取句）。
- **PostgreSQL**（`LOOPER_TEST_DATABASE_URL`）：迁移（默认行、约束、旧库升级后行为不变、重复执行是空操作；旧版本那段 `DO $$` 在新 schema 上是空操作，场景会话有 Profile 选择时也不失败）；管家和模板的 API（每条 400/409；并发启用第 8、第 9 个只有一个成功；「停用 X、启用 Y」与「X 的 `enabled: true`」并发时启用数不超过 8；软删除后线程不进默认管家的列表和 `[RESUME]`、key 不能再用、它原来的 Profile 能删；建任务与删除管家并发时要么 409 要么不建，不会留下属于已删管家的任务）；`/gates`、`/gate N` 按管家（默认管家和 `task_scope = all` 的场景管家也只列自己建的）；`task_scope` 的四种组合（`tools` × `task_scope`）各自的工具和可见任务，`own → all`、`all → own` 的切换（`all → own` 时针对别的管家任务的待确认在确认时被拒）；停用再启用不执行停用前排队的消息、正在处理的那条在重启服务后也不重放、不被旧的恢复任务再起一个实例；模板任务 `start_over` 后模板角色仍在（版本 1、`via = 'template'`）；模板固定项目而调用给了目录、模板设了目录而项目不固定，确认和创建用的是同一个合并后的目录；模板的 Profile 被直接改库成不能跑那个角色时建任务答 `conflict`；改 Profile 让它跑不了模板选它的角色时被拒；同一仓库跨管家的工作区冲突匿名；停用时排队的和正在处理的 `GOAL_STATUS`（含 `notice`）转交默认管家、默认管家把它转发出去、原消息和转交的都不再重放；正在处理的用户消息用真实的 `_handle_user_message`（只把回答那一步换成等待）验证停用后重启不重放；`all → tasks` 提交后 `apply` 被弄成失败时，下一次 `looper_settings` 调用答 `forbidden` 或 `unavailable` 而不执行，`state.sync = pending`，重试成功后变回 `ok`；模板写入与删除管家并发时要么 404、要么模板随删除一起删掉；web 任务在回答结束、页面重连后，结束汇报出现在来源线程的聊天记录和 `?session=` 的事件流里；重复的角色模板 key 保存时 400；`task_scope = own` 的概览各键都在且只算自己的；建任务套模板（每个错误分支什么都不写；成功时任务、Profile 覆盖、角色、快照在一个事务里）；`task_scope = own` 的读写（`tools = all` 和 `tools = tasks` 各一次）；会话层 Profile 按管家；花费按管家；删 Profile、删项目被模板拦住。
- **既有测试不动并通过：** 管家的会话、`/new`、`/profile`、停止矩阵、绑定、`test_agent_launch_parity_db.py`、`test_model_launch_parity_db.py`（默认管家的启动逐字节不变，是这一项的硬指标）。
- **真实路径：** 在 worktree 里对新数据库跑 `looper serve`（不是 8767，临时 `--runtime-root`）：建「Code review」管家和 GitHub 示例模板；控制台选它，说「帮我评审 <一个公开仓库的 PR 链接>」，确认后任务带模板建好，Planner 的系统提示词里有说明；同时和默认管家对话，两个管家各自的 Runs、会话、花费分开；停用场景管家时它的进程结束。界面在无头浏览器里截图（聊天页的下拉、Settings → Managers、任务页的归属）。
- **prompt 评测：** 新增 dialogs `template_review_task`（给了链接：复述 → 请用户确认 → 用模板建任务）和 `template_missing_input`（没给链接：问链接，不建任务），fake 模式进 CI。real 模式用 sonnet，不用 haiku：`evals/prompt_regression` 的 dialogs real 模式 `--model sonnet --manager-model sonnet --reps 2 --dialogs template_review_task,template_missing_input`，再用 opus xhigh 的管家跑这两条；评测脚本发消息、查运行记录、看工具调用都按场景管家的 id（`agent_id = manager-code-review`），不按 `manager`。默认管家的 dialogs 不重跑（模型可见内容没变）。Planner 有模板说明的一条任务：fake 模式检查说明进了提示词。

## 17. 阶段

阶段 1、2 各自交付；阶段 3 按存储、线程身份、fleet stop、原生路由、类型化 API、两张 Settings 页和最终集成验收拆成有界 PR 链，每项可独立评审与回滚，只有最终验收卡更新整体状态。

1. **阶段 1：管家 + 控制台选择 + 模板 + 任务归属。** 第 5–8、10–13 节。完成标准：能在控制台建一个场景管家和模板、在聊天页选它对话、用模板建任务、任务回报给它；默认管家和渠道行为不变。渠道（飞书、Telegram）不涉及：它们只到默认管家，PR 里写明。
2. **阶段 2：每个管家一个机器人。** 第 9.1 节。
3. **阶段 3：按聊天绑定和 Settings。** 第 9.2 节。

阶段 1 之后的待办（记入开发计划第 5 节）：闲置的场景管家自动停进程；不再支持降级到阶段 2 之前的版本后，删掉 `task_user_gate_posts` 按类型的唯一约束（第 9.1 节）；已删管家的线程和记录的物理清理；管家、模板和它们的机器人进配置导出、导入；定时任务属于一个场景管家（并能用它的模板）；按管家分插件和 skill 的安装；任务列表按管家筛选。

## 18. 默认行为的变化

对已有的安装（只有默认管家）：模型可见的**没有。** 迁移后所有新列都是 `manager`；默认管家的系统提示词、工具、每轮注入的段、会话、渠道逐字节不变。唯一用户可见的变化：web 聊天里建的任务，它被分解和结束的消息写进来源线程（第 6 节「任务结束通知、进度」）；今天这些汇报在那条消息答完后就被丢了，这是顺带修好的缺陷，发布说明写明。只有操作者建了场景管家之后，新的界面元素（聊天页的下拉）才出现。
