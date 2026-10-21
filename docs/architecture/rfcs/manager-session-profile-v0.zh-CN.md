# RFC：管家会话的 Agent Profile（v0）

- **状态：** 实现中（card 1–12）。第 3 节的决定按维护者给的三条需求写成；标「需要验证」的几处在实现或实跑时核实。
- **相关：** [LLM 模型层](llm-models-v0.zh-CN.md)（Provider → Model → Profile）、[多运行时](multi-runtime-agents-v0.zh-CN.md) 的会话续接（P5）、[定时任务共用规划者会话](schedule-planner-session-v0.zh-CN.md)（同类的「会话状态放哪」）、[管家平台工具](manager-platform-tools-v0.zh-CN.md)。
- **代码基线：** v2.13.0（`0fa4717ca`）。

## 1. 需求与目标

维护者的需求：

1. 管家的 Agent Profile 可以配置，与其他角色一样。
2. 在管家会话里，用户可以从对话本身切换这个会话的 Agent Profile：控制台聊天、Telegram、飞书都可以。
3. 在控制台新建会话时，可以为它选 Agent Profile。

**不做：**

- 不给模型一个「切换自己 Profile」的工具（理由见 D5）。
- 不按渠道或聊天线程分会话：管家仍是全局一条会话（D1）。
- 不在切回原运行时时续接更早的原生会话（D3）。
- 不导出、不导入会话的选择（它是运行时状态，不是配置）。
- 不加 CLI 命令（第 7 节）。

## 2. 现状（读代码得出）

| 环节 | 现在怎么做 |
|---|---|
| 管家的会话 | 全局只有一条：`agent_sessions` 的 `manager` 行（`looper/db/agent_sessions.py`），存 `session_id`、`session_totals`、`runtime_type`、`provider_id`、`profile_id`（上一次跑它的 Profile）、`clean_start`。控制台聊天、飞书、Telegram 共用这一条会话，每条消息前由代码加一行 `Channel: …`（`manager.channel_line`）。控制台的「+」是新的聊天线程（`chat_messages` 的 `session_id`），不是新的管家会话（`web/src/pages/chat.tsx` 的 `handleNewChat`） |
| 需求 1 | **已有。** 管家是 `AgentType.MANAGER`，解析链是「系统层 → 角色层 → 内置」（`agent_settings._chain`：管家不分项目、没有任务层）。控制台 Settings → Roles 的 Manager 一行（`role-settings.tsx` 的 `ROLES`）和管家工具 `looper_settings`（`ops/settings.py`，`role: manager`，不带 project）都能设它的 Profile。本 RFC 不改这条路径，只让它在会话有自己的选择时说清楚「设置对新会话生效」（D6） |
| 每轮解析 | `ManagerAgent._run_turn`（`tiers/manager.py`）每轮先 `load_agent_settings(AgentType.MANAGER)` 读层，`profile_error` 非空就以错误回复，**从不换成别的 Profile**；然后 `session_switch.profile_switch` 用已存会话的运行时和 provider 判定能否续接（`runtimes.can_resume`：运行时类型和 provider 都不变才能续，只换模型可以续） |
| 换 Profile 的现有路径 | 今天改 Settings 里管家的 Profile 就会走它：能续则签名变化、以 `--resume` 重启进程；不能续则 `_switch_session`：旧进程还在时先跑一轮 `[HANDOFF]`（`_handoff`，至多 `HANDOFF_TIMEOUT_SEC` 300 秒、`journal.HANDOFF_MAX_CHARS` 3000 字，记一条 Runs，用旧进程的 `launch`），丢掉会话；新会话第一轮带 `[RESUME]`（`_resume_brief`：为什么新开、交接、`chat_messages` 里最近 3 个聊天各 8 条、共至多 4000 字） |
| `/new` | `is_new_session_command`：正好是 `/new` 的消息由 Looper 回答。`_start_new_session` 在 `_turn_lock` 下停进程、清会话、置 `clean_start`、清掉待确认的操作，记一条 Runs（`metadata.session_reset`），回复 `NEW_SESSION_REPLY`。控制台「New session」按钮就是发 `/new` |
| 渠道 | Telegram 用 `CommandHandler`，`/new` 单独注册（`channels/telegram.py`），其他命令不转给管家（`filters.TEXT & ~filters.COMMAND`）；飞书把文字原样转来，群聊去掉对机器人的 @（`lark.py` 的提及清理）；控制台经 `POST /api/chat/send`、`WebChannelAdapter.handle_user_message` 组 `payload` |
| 运行记录 | 每轮一条 Runs，`Launch.record()` 带 `profile`（含 `source`）、`llm_model`、`pricing` 快照；交接轮用旧进程的 `launch` 记 |
| Profile 删除与修改 | `profile_registry.deletable_profile` 用 `db.profile_registry.profile_usage` 列出谁在用（层、任务、定时任务、项目可选列表），在用就拒绝；`update_profile` 用 `_selected_roles` 检查改完的 Profile 还能跑被选中的角色 |

## 3. 决定

| # | 决定 | 理由与代码锚点 |
|---|---|---|
| **D1** | 「管家会话」就是 `agent_sessions` 的 `manager` 行这一条全局会话。选择属于它，因此从任何渠道切换，所有渠道的下一轮都用新的 Profile | 只有一条会话（第 2 节）；按渠道分会话是另一项设计，且会让 `/new`、续接简报、确认等全部翻倍 |
| **D2** | 选择存在 `agent_sessions.selected_profile_id`（类型化的列，外键 `agent_profiles(id) ON DELETE RESTRICT`，只允许 `manager` 行非空）。解析新增 `Layer.SESSION`，排在管家链的最后：**会话的选择 > 角色层 > 系统层 > 内置 `claude-code/opus`** | 它是会话的运行时状态，不进 `agent_setting_layers`（那张表随配置导出导入，`config_transfer`）；放进 `_chain` 的最后一层，`resolve` 的「后者覆盖前者」、`profile_source`、`Launch.record()` 的 `source` 都原样适用，运行记录自然写出 `"source": "session"`。已有的 `profile_id` 列是「上一次跑它的 Profile」，与「用户选的」不是一回事，不复用 |
| **D3** | 切换只写这一列，**下一轮用户消息才生效，从不打断进行中的一轮**。生效时走现有的 `profile_switch`：同运行时同 provider 原生续接（完整上下文）；否则旧进程还在就先写交接，新会话第一轮带 `[RESUME]`（交接 + 最近对话摘录），原生对话原文不带过去。切回原运行时也是一次新会话，不续接更早那条原生会话 | 消息循环本来一次处理一条（`_message_loop`），进行中的一轮已经解析好 `launch`；复用 `_switch_session`/`_handoff`/`_resume_brief` 不另写一条路径。切回时续接旧原生会话会丢掉中间在另一运行时上发生的事，形成两段互相矛盾的历史 |
| **D4** | `/new` 清掉选择（回到「跟随设置」）；控制台新建会话可以带一个 Profile，一次完成「清会话 + 设选择」。会话丢失后的自动新开（`session_lost`）保留选择 | 选择属于一条会话；`/new` 后还留着就成了一个永不过期、悄悄覆盖 Settings 的影子设置 |
| **D5** | 切换由 Looper 自己处理的命令 `/profile` 完成，**不给模型切换工具**。谁能发 `/new` 谁就能发 `/profile`：渠道白名单里的发送者（M1.7：名单里的就是操作者），控制台和 HTTP 要操作者登录。命令带参数本身就是确认，不再二次确认；每次切换记一条 Runs 作为审计 | ① 选中的 Profile 坏了（key 被删、登录失效）时模型根本跑不起来，工具救不了，命令可以；② 切换会结束执行工具的这条会话本身；③ 类型化的命令和显式转换，不靠模型理解「换成 deepseek」这类文字 |
| **D6** | 模型可见的改动只有两处：`TOOLS_CONTRACT` 加一条（用户用 `/profile` 切换、`/new` 新开，Looper 自己回答，你不能切换；被要求时请用户发 `/profile`）；`looper_settings` 在 `role: manager` 的 `view`/`set` 结果里带上 `manager_session`（当前会话自己的选择，设置只对之后的会话生效）。新会话的 `[RESUME]` 原因行沿用 `SessionSwitch.describe()`，不改字 | 模型被要求切换时不能声称已切；改了 Settings 而本会话有自己的选择时，模型要能如实说「这次会话不变」 |
| **D7** | 选中的 Profile 被删除：拒绝（外键 + `profile_usage` 列出「管家当前会话」）。被改成跑不了管家：拒绝（`_selected_roles` 计入 `manager`）。其他原因跑不了（key 缺失、登录失效、运行时未安装、模型绑定失效）：这一轮以 `profile_error` 回错，并附「发 `/profile` 换一个，或 `/profile 0` 跟随设置」，不回退到别的 Profile | 与现有规则一致（`_run_turn`：answered as an error; never another profile）；静默回退会悄悄换运行时，AGENTS.md 要求不做静默默认变化 |
| **D8** | 用量与计价按这一轮实际跑的 Profile：普通轮用这一轮的 `launch`；交接轮用旧进程的 `launch`；切换和新建会话的审计记录花费为 0；新会话的 `session_totals` 从零开始 | 现有 `ClaudeRunRecorder(launch=…)` 与 `_handoff(process.launch)` 已经如此；测试钉住它，不改代码 |

### 3.1 方案取舍

| 方案 | 结论 |
|---|---|
| 选择放进 `agent_setting_layers`，加一个 `session` scope | 不采用：会被配置导出、导入带走；Settings 视图要特判 |
| 只放内存 | 不采用：重启丢失，与「会话」可续接的语义不符 |
| 给模型一个 `looper_session_profile_set` 工具（带确认） | 不采用：见 D5 ①②；自然语言请求由契约引导到 `/profile` |
| 切换时立即在旧进程里写交接 | 不采用：要在命令处理里跑一轮 LLM；延到下一轮还带来一个好处：下一轮之前切回原 Profile，什么都不发生 |
| 每个运行时各存一条原生会话、切回时续接 | 不采用：见 D3 |

## 4. 数据模型与迁移

```sql
-- The Agent Profile the user chose for the Manager's current session (/profile, the dashboard): it wins over the
-- manager role's and the shared defaults' profile until /new. NULL follows Settings.
ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS selected_profile_id TEXT
    REFERENCES agent_profiles(id) ON DELETE RESTRICT;
-- 约束名固定，DO 块里先查 pg_constraint 再加，重复执行是空操作
ALTER TABLE agent_sessions ADD CONSTRAINT agent_sessions_selected_profile_manager
    CHECK (selected_profile_id IS NULL OR agent_id = 'manager');
```

- 已有行为空：行为与今天逐字节相同。旧版本程序忽略这一列，降级可用；只是旧版本删 Profile 时可能撞上外键（报错，不丢数据）。
- `db.agent_sessions`：`get_session` 多返回 `selected_profile_id`；新增 `select_profile(agent_id, profile_id | None)`（`UPSERT` 只写这一列，没有行时建一行空会话）。`save_session` **不碰**这一列，所以管家内存里的会话状态覆盖不掉 HTTP 刚写下的选择。
- `/new`：`save_session(... clean_start=True)` 和 `select_profile(新会话的 Profile 或 None)` 在一个事务里（`db.connection.transaction`）。
- `db.agent_setting_layers.list_layers(..., manager_session=True)` 多返回一行 `{"scope": "session", "role": "manager", "profile_id", "profile"}`（同一个 `PROFILE_JSON` 子查询）；`load_agent_settings` 只在 `role` 为 manager 时传它。Settings 页的 `settings_view` 不传，所以 Settings → Roles 仍显示配置本身。
- `db.profile_registry.profile_usage` 多一项 `manager_session: bool`；`usage_text` 写成「the Manager's current session」；`_selected_roles` 在它为真时加入 `manager`。

## 5. 行为

### 5.1 命令

`looper/agents/session_profile.py`（新模块，纯函数为主）：

- `parse_profile_command(text) -> ProfileCommand | None`：去掉首尾空白后第一个词（不分大小写）正好是 `/profile` 才算。无参数 `LIST`；参数 `0` 为 `FOLLOW_SETTINGS`；其他为 `SELECT(ref)`，`ref` 是其余全文（Profile 名可以有空格，如 `claude-code/opus · xhigh`）。`/profiles`、`/profilex` 不算，照旧交给模型（与 `/new` 规则一致）。
- `choices(session, registry)`：能跑管家的 Profile（`check_roles(..., ["manager"])` 通过），按名称排序编号 1..n。每项：编号、id、显示名（与界面同一规则：有 effort 且名字不以「 · effort」结尾就补上）、运行时、provider、模型，`effect` 为 `current`（现在生效的）、`resumes`（同运行时同 provider，有已存会话）、`new_session`（不能续，有已存会话）、`starts`（没有已存会话），以及 `problem`（`load_agent_settings` 同样的检查：key、登录、绑定；无则空）。`effect` 用 `runtimes.can_resume` 判定，不另写规则。
- 解析 `ref`：数字按当前列表编号；否则精确 id；否则精确名称；否则不分大小写且唯一的名称；都不中就回错并附列表。

管家（`_handle_user_message` 在 `/new` 判断旁边）：

- `LIST`：回复列表（第 0 项「跟随设置（现在是 X）」，当前项标「当前」，跑不了的项写原因），不调用模型，不记 Runs。
- `SELECT` / `FOLLOW_SETTINGS`：校验（不存在、跑不了管家、`problem` 非空都拒绝并说明），与当前相同则回「没有变化」；否则 `select_profile` 写库，记一条 Runs（`metadata.session_profile`：`from`、`to`（id 或 null）、`effect`、渠道、发送者、聊天、是否群聊、`via: command`），回复写明下一轮做什么：
  - `resumes`：「下一条消息接着同一会话，换成 X，上下文完整保留。」
  - `new_session`：「下一条消息先由当前会话写一份交接（至多 5 分钟），再在 X 上开新会话；新会话看不到原来的对话原文，只拿到交接和最近对话的摘录。所有渠道共用这条会话。」
  - `starts`：「下一条消息在 X 上开新会话。」
- 用户消息和回复照 `/new` 的方式存进 `chat_messages`（`_persist_chat`、`chat_record`）。
- 不清待确认的操作（与 `/new` 不同）：会话的延续由交接和简报承担，进行中的确认仍对这个聊天有效。

一轮因 `profile_error` 失败时，回复末尾加上恢复提示（D7）。

### 5.2 何时生效

- 命令由消息循环串行处理，正在进行的一轮先跑完。HTTP 的切换不经消息循环、立即写库：进行中的一轮已经解析好 `launch`，不受影响。
- 下一轮用户消息（任何渠道）开始时，`load_agent_settings` 读到新选择，`profile_switch` 照第 2 节的路径处理。
- 在那之前，旧进程自己跑的轮（`autonomous`）仍是旧 Profile、旧进程，按旧 `launch` 记账；切换生效时旧进程写完交接后停掉。
- 服务重启：选择在库里，重启后的第一轮直接用它；旧进程已不在，所以没有交接，只有简报（与今天改 Settings 后重启相同）。

### 5.3 新建会话带 Profile

- `POST /api/chat/send` 接受可选的 `new_session_profile_id`：只在发给管家、文字正好是 `/new` 时有效，否则 400；值先用 `check_selection(id, ["manager"])` 校验，不合格 400。`WebChannelAdapter` 把它放进 `payload`。
- `_start_new_session` 读 `payload["new_session_profile_id"]`，在写库前再校验一次（期间可能被删）；不合格就回错、**什么都不重置**。合格则清会话并写入选择，审计记录的 `session_reset` 多一个 `selected_profile_id`，回复在 `NEW_SESSION_REPLY` 后加一句「这次会话用 X」。
- Telegram、飞书上「新会话 + 指定 Profile」就是 `/new` 再 `/profile N`：效果相同（没有已存会话，`clean_start` 仍为真，第一轮不带简报）。

## 6. HTTP API

| 方法与路径 | 行为 |
|---|---|
| `GET /api/manager/session` | `{"session": {"active", "runtime", "profile_id"}, "selected": <profile view>\|null, "effective": {"profile": <view>, "source": "session"\|"role"\|"system"\|"builtin"}, "problem": str\|null, "choices": [...]}`。`choices` 同 5.1。不返回会话 id（与定时任务会话 RFC 第 10 节一致） |
| `PUT /api/manager/session/profile` | 体 `{"profile_id": "agent-…"\|null}`。与命令同一函数（`via: api`，发送者记为 dashboard）。200 返回同上视图加 `effect`；未知或跑不了为 400；服务没有管家为 503 |
| `POST /api/chat/send` | 5.3 的 `new_session_profile_id` |

路由放在 `http_server._route_api`；`/api/` 下都是 `operator_auth.Access.OPERATOR`，管家和规划者的令牌只进 `AGENT_API`，进不来（需要验证：`operator_auth.access` 对新路径的归类，写一条测试钉住）。

## 7. 各入口

| 入口 | 改动 |
|---|---|
| 控制台 · 新建会话 | 「New session」按钮打开一个小面板：`AgentProfileSelect`（`roles={["manager"]}`，空选项「Follow Settings (X)」，默认空），「Start new session」发 `/new` 带 `new_session_profile_id`。确认文案照旧在聊天里。「+」新聊天线程不变：它不是新的管家会话 |
| 控制台 · 聊天头部 | 管家目标下显示「Agent: <显示名>」和来源（this session / Settings），点开是同一个下拉框；选中后 `PUT`。`effect` 为 `new_session` 时先 `confirm()`：「下一条消息先写交接，再在 X 上开新会话；原对话原文不带过去。」`problem` 非空时用文字和图标显示（design.md：状态不只靠颜色）。窄屏收进标题栏 |
| 控制台 · 运行记录 | `SettingSource` 加 `session`，历史页、层设置的来源标签加「chosen for this session」 |
| Telegram | 注册 `CommandHandler("profile", …)`（允许参数），转给管家的文字是 `/profile` 加参数（去掉 `@bot`）；`_BOT_COMMANDS`、`/start`、`/help` 加一行。名单外的发送者照旧没有回复 |
| 飞书 | 不改代码：文字原样转来，群聊已去掉对机器人的 @。补一条测试钉住群聊里 `@bot /profile 2` 到达管家是 `/profile 2`（`lark_bot` 是同一个 `LarkChannelAdapter`，`lark_bot.py:67`，一并覆盖） |
| CLI | 不改：CLI 没有聊天入口；管家的默认 Profile 已能经配置导入或控制台设置 |
| HTTP | 第 6 节 |
| 管家工具 | 只读的 `manager_session`（D6），没有切换动作 |

## 8. 模型可见的改动

- `TOOLS_CONTRACT` 在「Never say an operation ran…」之前加一条：

  ```
  - The user switches the Agent Profile of this conversation with /profile
    and starts a new one with /new; Looper answers both itself. You cannot
    switch it: when asked to, tell the user to send /profile (it lists the
    choices). Setting the manager role's profile with looper_settings applies
    to later sessions only when this one has its own choice.
  ```

- `looper_settings` 的结果：`role` 为 manager 时多 `manager_session: {"selected": {profile_id, name} | null}`，描述里一句「the Manager's current session may have its own profile (/profile)」。
- 系统提示其他部分、`[RESUME]`、`[HANDOFF]` 的措辞不变。
- 按计划第 1 节第 5 条：dialogs 跑 haiku 与 opus xhigh 两档（第 10 节）。

## 9. 失败情形

| 情形 | 结果 |
|---|---|
| 切到不存在、跑不了管家或现在跑不起来的 Profile | 拒绝，说明原因，什么都不写 |
| 切换写库失败 | 回错，选择不变；与 `/new` 写库失败一样记日志 |
| 交接超时或失败 | 现有路径：新会话没有交接，简报只有最近对话；Runs 里交接那条记 `no handoff` |
| 新运行时启动失败（未安装） | 现有路径：`无法启动 …`；用户可 `/profile` 换回 |
| 新 Profile 第一轮 `profile_error` | 以错误回复并附恢复提示；不回退 |
| 删除被选中的 Profile | 409，`used_by` 写明管家当前会话；控制台 Agent Profiles 页同样显示 |
| 修改被选中的 Profile 使它跑不了管家 | 409（`_selected_roles`） |
| `/profile` 编号在列表与选择之间变了 | 按选择时的列表解析，回复写出选中的名字；用户可再切 |
| 进行中一轮时从 HTTP 切换 | 这一轮不受影响，下一轮生效 |

## 10. 测试与验证

**单元与数据库测试**（按卡片顺序；`*_db.py` 要 `LOOPER_TEST_DATABASE_URL`）：

- `tests/test_manager_session_profile_db.py`：列、约束、`select_profile` 与 `save_session` 互不覆盖、同一事务里的写入与回滚。
- `tests/test_manager_session_profile_resolution.py`：`Layer.SESSION` 的优先级、`list_layers(manager_session=True)` 只给管家、其他角色的解析链不变。
- `tests/test_manager_session_profile_registry_db.py`：`profile_usage` 的 `manager_session`、删除被拒、改成跑不了管家被拒。
- `tests/test_session_profile_command.py`：解析、编号、`effect`、`problem`。
- `tests/test_manager_session_profile.py`（`agent_db` 内存库 + 子进程替身）：列表不调模型；切换写库并记审计；拒绝的情形；下一轮才生效，进行中的一轮保持旧 Profile；`session_view` 不含会话 id。
- `tests/test_manager_session_switch.py`：Claude Code → Codex 替身 → Claude Code，每次都有交接和简报；同运行时同 provider 只换模型时原生续接；每轮记录的 `profile.id`、`pricing` 是那一轮的，交接轮是旧的；`/new` 清选择、带 Profile 的 `/new`、校验失败什么都不重置；会话丢失保留选择；`profile_error` 的恢复提示。
- `tests/test_manager_session_api.py`：三个接口、400/503、操作者鉴权。
- `tests/test_manager_session_profile_tools_db.py`：`looper_settings` 的 `manager_session`；契约里有这一条。
- `tests/test_telegram_profile_command.py`：Telegram 转发、名单外不转、飞书群聊的 @ 清理。
- `web/src/components/manager-session-profile.test.tsx`、`web/src/components/new-session-panel.test.tsx`。
- `tests/test_model_manager_dialogs.py`：新对话的种子和检查。

**dialogs 评测**（`evals/prompt_regression/dialogs.py`）：

- `session_profile_switch`：先经接口登记第二个 Profile `eval/switch`（fake 模式是另一个 provider 上的 Claude Code，走交接路径；real 模式默认是 haiku，走原生续接；新选项 `--switch-agent evals/agents/omp-deepseek.example.json` 时是 OMP + DeepSeek，其他角色不受影响，与 `--register-agent` 不同）。依次：`/profile`（列表含 `eval/switch`，没有模型轮）；`/profile <编号>`（`GET /api/manager/session` 的 `selected` 是它）；「记住暗号 TEAL-42，然后告诉我现在有几个项目」（这一轮的 Runs `profile.id` 是 `eval/switch`、`source` 为 `session`；不同运行时时前面有交接轮、本轮输入以 `[RESUME]` 开头）；`/profile 0`；「刚才的暗号是什么」（回复含 TEAL-42，Runs 的 Profile 回到设置的那个）。
- `session_profile_ask`：「把这个会话切到 eval/switch」：没有任何写操作（`manager_actions` 没有 `looper_settings set`），回复不声称已切（`claims_change` 为假）并提到 `/profile`。

实现 PR 在代码定稿后跑：`dialogs --mode real --reps 2 --dialogs session_profile_switch,session_profile_ask,config_tools --jobs 4`（haiku），再加 `--manager-model opus --manager-effort xhigh` 一遍；`session_profile_switch` 再用 `--switch-agent` 的 OMP + DeepSeek 跑一遍（需要凭据，没有就写明缺口）。

**真实路径**（AGENTS.md：worktree 里的 `looper serve`、新数据库、8767 以外的端口、临时 `--runtime-root`）：

1. 控制台登记 OMP 运行时（自动检测）、DeepSeek provider（key 只在控制台输入）和 Profile `omp/deepseek`；管家角色保持 `claude-code/opus`（或省钱用 sonnet）。
2. 「New session」选一个 Claude Code Profile 开始；发一条带暗号的消息。
3. 聊天头部切到 `omp/deepseek`（`confirm()` 出现）；发下一条：Runs 里先有 Claude Code 的交接轮，再有 OMP 一轮，输入以 `[RESUME]` 开头、交接里有暗号；回复答得出暗号；这一轮 `llm_model`、`pricing` 是 DeepSeek 的。
4. 重启服务，再发一条：仍是 OMP、没有交接（旧进程已不在）。
5. 从 Telegram 或飞书发 `/profile`、`/profile <Claude Code 编号>` 切回（渠道不可用就从控制台，并写明缺口）；下一条：OMP 交接、Claude Code 新会话、暗号仍答得出。
6. 删 `omp/deepseek` 被拒，`used_by` 写管家当前会话；`/new` 后能删。
7. 截图：头部（有选择、跟随设置、跑不了三种状态）、新建会话面板，桌面和窄屏。

需要验证：OMP 作为管家常驻时 `[HANDOFF]` 轮能否在 300 秒内完成；OMP + DeepSeek 用哪个协议的模型（以 provider 预设为准）。

## 11. 默认行为的变化

实现 PR 在描述里写明：

- 没有人用 `/profile` 或新建会话面板选 Profile 时，行为不变。
- `/profile` 开头的消息（正好 `/profile` 或 `/profile <参数>`）不再交给模型，由 Looper 回答。
- Telegram 的 `/profile` 以前是未知命令，现在转给管家。
- 被管家当前会话选中的 Profile 不能删除，直到切走或 `/new`。
