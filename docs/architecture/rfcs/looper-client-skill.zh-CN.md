# RFC：一个 OpenAPI 接口，`loopercli` 与 `looper-skill`

- **状态：** 阶段 1 已实现（looper #411）；阶段 2a 已交付（第 15 节：auth、doctor、config）；阶段 2 的任务操作共享核心已实现（第 5.4 节）；其余迁移与阶段 3 待做。草案阶段的说明：第 1 节的六条是维护者 2026-10-22 定下的；其余按本文建议写成。标「需要验证」的几处在实现或实跑时核实。阶段 1 的实现卡片在本仓库之外。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.0 节第 22 项。
- **相关：** [管家平台工具](manager-platform-tools-v0.zh-CN.md)（操作目录、风险分档、确认、留痕）、[操作者 API 认证](operator-api-auth-v0.zh-CN.md)（操作者令牌、本机限制）、[场景管家](scenario-managers.zh-CN.md)（线程归属、`origin_thread`）、[用户关卡](user-gate-v0.zh-CN.md)。
- **代码基线：** v2.13.36（`ee67cebcd`）。

## 1. 需求与目标

维护者在别的 Claude Code（或 Codex、OMP……）会话里讨论一个项目的背景和需求、写一批文档，然后要从那里把活交给 Looper，并继续和 Looper 打交道：提交、开始、停止任务，改任务，看任务的状态和流水线，改 Agent Profile，等等。这需要一个 skill，和 Looper 给那个 agent 的接口。已经定下的：

1. **只在本机。** 客户端经回环地址访问 Looper，不对网络开放。
2. **全部权限。** 一个客户端令牌能做管家工具能做的一切。
3. **客户端的 `high` 操作两步确认。** 客户端令牌调用先回答「待确认」，同一客户端、同一会话明确确认后才执行；普通写操作直接执行。控制台以操作者身份调用 v1 时，`high` 在一次调用中执行，保留原有前置确认弹窗，不增加共享弹窗或自动确认调用。
4. **一个接口。** 和 Looper 的一切交互都经 Looper 的 OpenAPI：不另加一套平行的「客户端 API」，而是把现有 HTTP API 变成有 OpenAPI 描述的公开 API（带版本，每个操作有类型化的请求和回答，错误是类型化的状态对象），以生成的文档提供（`GET /api/v1/openapi.json`），控制台、命令行和 skill 都用它。HTTP API 和管家的 ops 工具重复做的事，指定一份实现；今天只有 ops 层有的操作，以 ops 工具的 schema 成为 API 操作。agent 经命令行访问 Looper（不用 Bash/curl；阶段 1 不做 MCP）。命令行是新程序 `loopercli`：同一个 Python 包的第二个 console script，是纯粹的 OpenAPI 客户端，不导入服务端的任何代码；子命令在运行时从 OpenAPI 文档生成，新操作不需要写命令行代码；常用的任务操作有几个手写的别名；服务地址和令牌在一个 0600 文件里、每次调用时读；默认输出易读的表格，`--json` 给脚本；两步确认表现为打印出的待确认 id 和 `confirm` 子命令。日常交互都经 API 和 `loopercli`。`looper` 保留运行服务（`serve`、`service`、`version`）、agent 内部用的 `mcp-server`，以及服务起不来时用的离线救援命令：`doctor` 和操作者密码的重设（`auth set-password` / `clear-password`），它们和 `serve`、`service` 一样是本机命令，不经 API（第 15 节）；`config export` / `import` 改成 API 操作，由 `loopercli` 调用，搬完后从 `looper` 删除（发布说明写明去了哪里）。用户这边的集成就是 `loopercli` 加 `looper-skill`。
5. **`loopercli` 和管家对话。** 阶段 1 的主路径：agent（或人）把讨论好的需求经现有的聊天 API 发给一个管家（默认管家，或按 id 选场景管家），回复流式返回；管家复述、提问、等确认、建任务，和在控制台里一样（消息写明用户已同意提交并开始时，管家可以不再复述、直接建；Looper 自己的确认，如新项目，仍然要）；任务的接受和结束回到这个对话（和今天场景管家的 web 线程一样）。关卡和进度在阶段 1 不推进这个对话：客户端用 `loopercli wait <task>`（关卡打开、任务结束或超时时返回）和 `status`、`pipeline` 跟进（第 11 节）。直接操作（建任务等）保留给脚本和精确修改；skill 优先用对话交新活。
6. **任务附件。** 提交任务通常只是需求文本。大文档是可选的任务附件：作为文件上传、随任务保存，Planner 和 Worker 当文件读，不受需求 32,000 字的限制。

**不做：**

- 不对网络开放：客户端令牌只在回环连接上有效（D4）。
- 不做 MCP 包装（以后有不带 shell 的客户端再在同一个 API 上包一层）。
- Looper 不写任何用户的 agent 配置（skill 由用户自己装，第 13 节）。
- 客户端不能管理客户端令牌，也碰不到操作者的凭据：令牌只在 Settings 里由操作者创建和吊销，操作者密码只由操作者设置和清除；客户端令牌无论经哪条路径都换不到操作者会话（D3，第 6 节）。
- 阶段 1 不把关卡和进度推进客户端的对话（第 11 节）。
- 阶段 1 不迁移控制台的请求，不改任何现有路由的行为。

## 2. 现状与差距（读代码得出）

| 环节 | 现在怎么做 | 差距 |
|---|---|---|
| agent 接口 | `/api/agent/v1/tools` 和 `/api/agent/v1/ops/:tool`（`http_server._handle_agent_api`）只认管家的、Planner 的、运行的令牌（`ManagerOps.authorized`），令牌每次启动随机生成，只交给 Looper 自己起的进程 | 外部 agent 没有令牌可用 |
| ops 工具 | 每次调用属于「管家正在跑的轮」（`Turn`：渠道、聊天、回答的消息、发送者、信封）。没有轮就算 Claude 自己的轮，只能读（`NEEDS_USER_TURN`）；`high` 的确认按聊天登记，只在回答用户下一条消息的那一轮里由 `looper_confirm` 执行（`ops/service.py`） | 客户端的调用没有「轮」，也没有「下一条消息」 |
| 依赖消息原文的工具 | `looper_task_gate_reply` 只把这一轮回答的用户消息原文（或其中一段）交给关卡（`ops/tasks._relayed_text`），不收模型自己的话 | 客户端没有「用户消息」可转 |
| 操作者 API | `/api/` 下其余路由只认操作者（令牌文件、cookie、密码会话），手写 JSON，没有 schema，没有版本 | 不能拿来生成命令行；令牌是整台机器的操作者凭据，不能按客户端吊销或留痕 |
| 任务操作共享核心 | 控制台路由和 ops 工具共用 `projects` 的任务列表、创建、详情、生命周期控制与补充说明；入口只适配参数和回答形状（第 5.4 节） | 控制台拒绝状态不允许的停止、取消、重启和从头开始时返回 409，并展示原因 |
| 聊天 | `POST /api/chat/send` → `WebChannelAdapter.handle_user_message`，回复在 `/api/chat/events?cid=` 流式返回，并存进 `chat_messages`；管家在每条消息前加 `Channel: the Looper dashboard chat`（`tiers/manager.channel_line`） | 管家分不出「人经自己的 agent 在说」；线程不记客户端 |
| 任务回报 | 只有场景管家的 web 任务记 `tasks.origin_thread`，接受和结束写进那个线程（`ops/tasks._create`，`tiers/manager._web_thread_reply`）；默认管家的 web 任务回报只进当时的流 | 客户端发起的任务的回报回不到它的对话 |
| 关卡通知 | `gate_courier` 把打开的关卡发往全局通知目标，不读任务的线程归属；进度不进任何线程 | 阶段 1 不改：客户端用 `loopercli wait` 和 `status`、`pipeline` 跟进（第 11 节） |
| `looper` 命令 | `auth`、`doctor`、`config export/import` 直接连数据库或读写 runtime root（`looper/cli.py`）；`serve` 在 HTTP 起来之前就要求凭据文件、数据库、schema 和配置有效 | `config export/import` 和「一个接口」相悖；`doctor` 和密码重设是服务起不来时的救援，必须不依赖服务 |

## 3. 方案取舍

| 方案 | 问题 | 结论 |
|---|---|---|
| 另做一套「客户端 API」 | 与控制台 API、ops 工具成为第三份实现，判断会分叉 | 不做（维护者决定 4） |
| 手写 CLI 子命令 | 每个操作要写一次命令行代码，和服务端的 schema 各自演变 | 不做：子命令从 OpenAPI 生成，只有几个别名手写 |
| MCP | 每个会话常驻 15 KB 的工具定义（管家工具 `tools/list` 的实测大小）；每种客户端各配一次；没有 shell 的人和脚本用不了 | 阶段 1 不做；以后在同一 API 上包一层 |
| agent 直接 curl | 令牌出现在命令行参数和 shell 历史里；没有统一的确认和输出；每个 agent 自己拼请求 | 不做：skill 只用 `loopercli` |
| **本文：OpenAPI v1 + `loopercli` + skill** | 一份 schema（来自 ops 目录）同时驱动服务端校验、文档和命令行 | 采用 |

代价：v1 的操作是 RPC 风格（全部 `POST` JSON，只有文档和 `whoami` 是 `GET`），不是按资源的 REST。这样一个工具的 schema 原样就是请求体，命令行的生成规则只有一条；对本机单用户的 API，REST 的缓存和语义收益很小。

## 4. 决定一览

| # | 决定 |
|---|---|
| D1 | 公开 API 在 `/api/v1/` 下，带版本；文档是 `GET /api/v1/openapi.json`（OpenAPI 3.1），启动时从操作目录生成一次。阶段 1 不改 `/api/` 下任何现有路由 |
| D2 | 操作目录就是 ops 的 `Tool`：客户端的目录 = 管家的工具，去掉两个只属于管家的（`looper_task_gate_reply`、`looper_confirm`），加两个只给客户端的（`looper_task_pipeline`、`looper_gates`）。另有几个手写声明的操作：聊天三个、`confirm`、`whoami` |
| D3 | 客户端身份 = **API client**：操作者在 Settings → API clients 里创建（起名字）、吊销；令牌只显示一次，库里只存 SHA-256；客户端不能管理客户端。凭据管理（`auth.*`：设置、清除操作者密码，创建、吊销客户端令牌）只接受操作者身份，客户端令牌一律拒绝，也换不到操作者会话 |
| D4 | 客户端令牌只在 TCP 对端和 `Host` 都是回环、且没有 `Forwarded` / `X-Forwarded-For` 时有效；`/api/v1/` 也接受操作者的凭据。客户端令牌在 `/api/` 其余路由上无效 |
| D5 | 客户端的每次调用是一个独立的「请求轮」：写操作直接执行，`high` 两步确认，确认按（客户端，会话）登记，会话由请求头 `X-Looper-Session` 给出（默认 `default`）；操作者的 `high` 在一次调用中执行，控制台保留原有确认弹窗 |
| D6 | 回答：`ok` 是 200 `{status, result, notes?}`；`needs_confirmation` 是 202 `{status, confirmation_id, expires_in_seconds, impact}`；错误是 4xx/5xx `{status: "error", error: {code, message}}`，`code` 取自 `ErrorCode`，加一个 `unauthorized` |
| D7 | 归属：`chat_threads.client_id`、`tasks.client_id`、`manager_actions.client_id`；直接操作的变更 `via` 记为 `client:<名字>`，客户端对话里管家做的变更记为 `manager:loopercli` |
| D8 | `loopercli chat` 走现有聊天实现：消息经 `WebChannelAdapter` 进管家，`channel_context.client` 由服务端按令牌填写；管家的渠道行写明是 loopercli 和客户端名字；对话默认新开线程，线程归这个客户端，别的客户端和控制台的线程都不能用 |
| D9 | 客户端对话里建的任务（默认管家也一样）记 `origin_thread`，接受和结束写回这个对话 |
| D10 | `loopercli`：`looper/loopercli.py`，只用标准库和 `looper.__version__`；运行时取文档生成子命令；地址和令牌在 `~/.config/looper/client.json`（0600，每次调用读）|
| D11 | `looper-skill`：一个 `SKILL.md`，规范副本随包发布（`looper/skill/looper-skill/SKILL.md`），仓库里的 Claude Code 插件目录放一份由测试保证一致的副本；Looper 不写用户的 agent 配置 |
| D12 | 管家的提示词和工具一字不改；唯一的模型可见改动是客户端消息前的渠道行（第 17 节）|
| D13 | 阶段 2 把其余 HTTP 路由描述进 v1，`config export/import` 搬到 API 和 `loopercli`（`doctor` 和密码重设另有 API 操作，同时留在 `looper` 作离线救援），重复实现合并，控制台改用 v1；阶段 3 做任务附件 |

## 5. 一个接口：OpenAPI v1

### 5.1 操作与路径

- **由工具生成。** 一个 `Tool` 的 schema 里 `required` 含 `action` 且 `action` 是字符串枚举时，每个动作一个操作：`operationId` 为 `<名>.<动作>`，路径 `/api/v1/ops/<名>/<动作>`，请求体是工具的 schema 去掉 `action`（服务端把动作补回去）；其他工具一个操作：`<名>`，`/api/v1/ops/<名>`。`<名>` 是工具名去掉 `looper_`（`looper_tasks` → `tasks`，`looper_task_control` → `task_control`）。
- **手写声明。** `POST /api/v1/chat/send`（回答是 `text/event-stream`）、`POST /api/v1/chat/messages`、`POST /api/v1/chat/conversations`、`POST /api/v1/confirm`、`GET /api/v1/whoami`、`GET /api/v1/openapi.json`。它们的 schema 写在生成器里，和工具的一起进文档。
- **扩展字段。** 每个工具操作带 `x-looper-tool`、`x-looper-action`（有动作时）、`x-looper-risk`（这个动作的分档：`read_actions` 里的是 `read`，否则工具的 `risk`；`escalate` 可能把一次调用升为 `high`，描述里说明「可能需要确认」）。
- **说明文字。** 用工具的 `description` 原文。它们是对管家写的（「the user confirmed」「Looper adds … to your reply」），对客户端略有偏差；skill 解释这些含义，不为客户端另写一份说明（另写一份就有了两份契约）。

### 5.2 请求、回答、错误

- 请求体是 JSON 对象，按工具的 schema 用 `ops.check_arguments` 校验（与管家同一个函数：未知参数、像密钥的参数名、类型、长度一样拒绝）。
- `ok`：`200 {"status": "ok", "result": {...}, "notes": ["..."]}`。`notes` 是 Looper 自己写给用户看的块（管家那边追加在回复后的 `Turn.blocks`：新任务的配置、新定时任务的设置），没有就不出现。结果里写给管家看的 `shown_to_user` 键去掉。
- `needs_confirmation`：`202 {"status": "needs_confirmation", "confirmation_id", "expires_in_seconds": 600, "impact": "<要确认的内容>"}`。
- 错误：`{"status": "error", "error": {"code", "message"}}`，HTTP 状态按 `code`：`invalid` 400，`unauthorized` 401，`forbidden` 403，`not_found` 404，`conflict` 409，`expired` 410，`rate_limited` 429，`unavailable` 503。管家专用的 `needs_user_turn`、`stopped`、`not_this_turn` 对客户端不会出现。未知路径 404 `not_found`，方法不对 405 `invalid`。

### 5.3 文档

`looper/agents/api_v1.py` 在第一次被请求时从目录生成一次文档并缓存（目录是代码里的常量，进程内不变）：`openapi: 3.1.0`，`info.version` 为 Looper 版本，`info.x-looper-api: 1`，`servers: [{url: "/api/v1"}]`，`components.securitySchemes.bearer`，`components.schemas` 里有 `ErrorCode`、`OpError`、`OpOk`、`OpNeedsConfirmation`。每个操作的 200 回答是 `OpOk` 加这个操作的 `result` schema。

**类型化的结果：** 状态投影有独立 schema，不新增 Manager Tool。未完成领域的工具结果继续显式标记未类型化，各领域通过 `RESULTS` 补齐。任务配置领域补齐 `concurrency` 的 before/after、任务覆盖与默认值类型；控制台的 `tasks.configure`、`tasks.move`、`tasks.agents`、`tasks.continue` 单独声明完整结果（Agent 选择含 Profile、生效模式和并发来源），不新增模型工具。阶段 1 给 agent 用的操作写出 `result` 的 schema（顶层每个键的类型，`additionalProperties: true`）：`tasks.list`、`tasks.get`、`task_pipeline`、`task_create`、`task_control.*`、`task_update`、`task_instruct`、`gates.*`、`agent_profiles.list`、`overview`、`confirm`、`whoami`、聊天三个。任务交互领域补齐 `tasks.room` 的紧凑结果，以及 `waits.resume/cancel`、`tasks.role_update`、`tasks.room_view` 的完整 DTO；完整 Room 保留正文与咨询字段。其余操作的 `result` 暂为 `{"type": "object"}` 并标 `x-looper-result-typed: false`，测试列出这些操作，阶段 2 把列表清空。

目录条目与安装提供 `catalog_items.list/get/update/delete`、`installs.create/update/delete/project`，均为 `hand=false` 的普通 JSON 操作。完整目录、详情、安装与项目安装 DTO 保留文件、归属及继承信息；现有 `catalog.list/install/update/uninstall` 的请求和路径不变，compact 回答分别类型化，列表用 `oneOf` 区分全局与项目。获取来源由 `catalog_sources.import_machine/import_mcp/upload/git_preview/git_import/create_skill/create_mcp` 提供，复用 `Catalog`、`CatalogItem`、`McpConfig`，补齐 `GitPreview`、`GitImport` 和跳过条目 DTO；预览不写库，导入必填预览 commit 与非空选中项。MCP 输入的 env/headers 标为 writeOnly，输出沿用遮罩。项目自有 skill 由 `project_skills.add/get/update/delete` 提供，复用完整 `ProjectInstalls`、`CatalogItem`、`Install` 投影及 `ProjectSkill`；请求标识为必填字符串，更新字段非空且不接受 `enabled`，开关走 `installs.update`。名称、描述、正文和项目数量限制沿用 `project_skills` 核心常量。

渠道身份与人员发现由 `channels.status/people` 提供：`channel_type` 为 `lark` 或 `lark_bot`，可选 `channel_id` 省略时使用对应内置 bot，显式 id 保留新增 bot 归属。返回完整 `LarkStatus`（含拒绝发送者及 bot 不可用的 null）和 `LarkPerson`；lark-cli 未给 `complete` 时归一为 true，外部查询失败为 `unavailable`，未知或类型不符的渠道为 404，非法查询为 400。`gate_settings.get/update` 返回完整通知目标投影；更新为 high，客户端同会话确认、操作者单次执行，确认影响列出目标，执行时由原服务重验启用状态与白名单。四个操作均为 `hand=false`，控制台与生成 CLI 共用。

**文档覆盖什么：** 文档包含既有 v1 操作和已迁移的控制台领域，剩余旧路由仍不在文档中。状态现为 `POST /api/v1/system/status {}`，返回完整状态投影（Agent、任务计数、web 健康、自检、退出能力及可选的绑定/管家状态），客户端的 `can_sign_out` 恒为 false；操作者从真实请求头计算。旧 `GET /api/status` 已删除。

领域注册表为有序模块名元组，按需加载；每个模块声明 `OPERATIONS`、补齐现有工具的 `RESULTS` 和 `run`，按需声明 `stream` / `impact`。生成时拒绝重复 id/方法路径，领域 stream 自带事件说明/schema，当前仅接受只读操作（注册时拒绝写流，防止绕过确认/审计）；`high` 必须提供 impact。请求头直接传回调。普通写复用 ClientOps 校验、审计和客户端确认，领域 DTO 使用 transport 校验一次（保留必填 null、对象数组与联合类型，拒绝未声明字段）；Manager Tool 校验/列表不变。凭据写先拒绝客户端，参数/结果不审计，失败脱敏。错误可带类型化 `details`（`manager_id`、`state`、可空的 `phase`/`method`），不透传原始异常或请求值。

任务活动的 `tasks.activity`、`activity_detail`、`conversation`、`activity_stream` 使用完整 DTO；对话窗口沿用 `db.activity.conversation_window` 的 `rows` 集合键，只含 seq、worker、kind、summary、created_at。活动与对话的后向窗口将 limit 夹到 1–1000，默认 1000；活动有 after/since 时改为前向读取，after 优先于 since，两者均优先于 before，limit 为 min(请求值, 500)，默认 100，不新增通用上限。SSE 的操作说明及 `x-looper-events` 声明 ActivityEvent、id=seq、done `{}` 和心跳注释，JSON 拒绝沿用 OpError。

### 5.4 HTTP 路由和 ops 工具的重叠

| 做的事 | 控制台路由 | ops 工具 | 当前实现 | 共享实现与状态（阶段 2） |
|---|---|---|---|---|
| 控制台状态 | `POST /api/v1/system/status` | 无 | 已抽取 `_status_result` | 领域模块提供完整投影，服务状态命令与控制台共用 v1 |
| 任务活动与对话 | `POST /api/v1/tasks/activity`、`activity_detail`、`conversation`、`activity_stream` | 无新增 Tool | 共用 `db.activity` 和现有 broadcaster，先订阅再回放，按 seq 续传，断连注销 | 四个旧 GET 删除；控制台与生成式 CLI 同步迁移，运行列表与聊天流不变 |
| 列任务 | `GET /api/tasks` | `looper_tasks` list | 共用 `projects.list_tasks_page`（游标、状态、定时任务、分组和工具的关键字搜索） | 已实现；ops 的 list 使用精简形状，保留原有搜索范围与 `more` 语义，控制台保留游标、计数和花费 |
| 任务详情 | `GET /api/tasks/:id` | `looper_tasks` get（`_task_view`） | 共用 `projects.task_detail` | 已实现；ops 的 get 使用精简视图，保留原有键、裁剪和结果文案 |
| 建任务 | `POST /api/tasks/create`（`projects.create_task`） | `looper_task_create`（`_create`） | 共用 `projects.create_task_core`（放置、工作区检查、事务、配置块），两入口保留参数适配层 | 已实现；核心收两边参数并集，控制台保留 `config`、`profile_overrides` 和新项目全部字段，工具 schema 不变；数据库回归测试从两入口创建后回读并比较任务行与 Profile 覆盖 |
| 开始 | `POST /api/tasks/:id/start` | `task_control` start | 共用 `projects.start_ready_task` | 不变 |
| 停止、取消、重启、从头开始 | `POST /api/tasks/stop`、`cancel`、`start` | `task_control` | 共用 `projects.control_task`（状态检查加发送） | 已实现；状态不允许时控制台返回 409 和原因、不发送消息，任务列表和详情页展示错误；工具结果不变 |
| 标题、需求 | `PUT /api/tasks/:id/title`、`requirement` | `looper_task_update` | 共用 `rename_task`、`edit_requirement` | 不变 |
| 配置、项目归属、Agent 选择、预算续跑 | `POST /api/v1/tasks/configure`、`move`、`agents`、`continue` | Agent 选择与 `looper_task_update` 共用服务；并发工具请求不变 | 配置抽取无 writer 核心；其余共用 `projects`，移动同步 live router，并发变更唤醒等待者 | 四个旧方法删除；普通写直接执行，续跑为 high，明确任务独立额度或项目每任务额度的影响范围 |
| 补充说明 | `POST /api/chat/send` 发给 Planner | `looper_task_instruct` | 共用 `projects.add_task_supplement`（存储加唤醒） | 已实现；每次写一行、唤醒一次，聊天保留 `USER_MESSAGE` 的补充 id 和回复关联，工具保留 `TASK_CHANGE` 与现有结果 |
| 关卡留言、结束 | `POST /api/v1/ops/gates/reply`、`end` | `looper_task_gate_reply`、`looper_gates` reply/end | 共用 `gate_service.post_message` | 旧控制台方法删除；客户端来源由认证确定，无 Planner 时保存待续跑 |
| Lark 身份、人员与关卡通知设置 | `POST /api/v1/channels/status`、`channels/people`、`gate_settings/get`、`gate_settings/update` | 无新增 Tool | 共用 `channels.lark_settings/lark_bot_settings` 与 `gate_service` | 六个旧 GET/PUT 方法删除；channel-configs 配置核心不变 |
| 关卡列表、讨论 | `POST /api/v1/ops/gates/list`、`messages` | `looper_gates` list/messages | 共用 `gate_service.list_gates`、`thread` | 旧方法删除；控制台通过现有 offset 分页保留加载边界，不扩展 Tool schema |
| 等待、任务角色、办公台 | `POST /api/v1/waits/resume`、`cancel`、`tasks/role_update`、`tasks/room_view` | `tasks.room` 保留紧凑只读投影 | 共用 `wait_service`、`role_api`、`room.task_view` | 普通写走 ClientOps 审计；完整办公台保留正文和咨询，角色 `via=user` 表示明确人工编辑 |
| 总览 | `GET /api/overview` | `looper_overview` | 共用 `overview.build_overview` | 不变 |
| 项目、Profile、provider、模型、设置层、可选 Profile、定时任务、花费 | 各自的路由 | 各自的工具 | 共用 `projects`、`profile_registry`、`catalog`、`agent_settings`、`schedules`、`llm_records` | 不变（这些工具建时就要求调控制台的代码） |
| 目录条目与安装 | `catalog_items.*`、`installs.*`（v1） | `looper_catalog` | 沿用 `catalog` 事务与安装普通写策略，删除目录条目为 high 并重验归属 | 控制台和客户端共用；MCP/skill 更新只认操作者，回答遮罩秘密 |
| 目录获取来源 | `catalog_sources.*`（v1） | 无新增 Tool | 共用 `library.import_from_machine`、`mcp.import_from_machine/create`、`catalog_sources` 上传与 Git 服务 | 七个旧 POST 删除；本机/MCP 导入与 MCP 新建只认操作者，不审计凭据；客户端 Git 导入为 high，确认覆盖执行时的同源内容（包括本地修改），commit 漂移整批拒绝 |
| 项目自有 skill | `project_skills.add/get/update/delete`（v1） | 无新增 Tool | 共用 `project_skills` 归属校验与事务 | 控制台与客户端共用完整结果；删除列明所有安装项目，确认执行时重读实际引用并级联删除安装 |
| 配置导出、导入 | `GET /api/config/export`、`/api/config/imports` | `looper_config_export` | 阶段 2a：v1 与旧路由、工具共用 `config_transfer` 和 `ConfigImports` | 已交付：`loopercli config export` / `import` 调 v1；旧控制台路由保留 |

## 6. 客户端身份与令牌

- **表 `api_clients`：** `id`（`client-<12 位十六进制>`）、`name`（1–40 个字符，`[A-Za-z0-9][A-Za-z0-9 ._-]*`，未吊销的里面忽略大小写唯一）、`token_sha256`（唯一）、`created_at`、`last_used_at`（最多每分钟更新一次）、`revoked_at`。吊销只填 `revoked_at`，行留着，归属记录一直能显示名字。
- **令牌：** `lpc_` + `secrets.token_urlsafe(32)`，创建时只在回答里出现一次；库里只存 SHA-256 的十六进制（256 位随机值不需要慢哈希）。按哈希查找：`WHERE token_sha256 = $1 AND revoked_at IS NULL`。
- **管理：** 只给操作者的路由（控制台用，阶段 1 不进 v1）：`GET /api/clients`（未吊销的，不含令牌）、`POST /api/clients {name}`（201 `{client, token}`，`Cache-Control: no-store`）、`DELETE /api/clients/:id`（吊销；404 给未知或已吊销的）。
- **凭据管理只认操作者：** 设置或清除操作者密码（阶段 2a 的 v1 `auth.set_password` / `auth.clear_password`，第 15 节）、创建或吊销客户端令牌只接受操作者身份（操作者令牌、cookie 或密码会话）；客户端令牌调用 → 403 `forbidden`，不执行、不改任何凭据。客户端令牌永远换不到操作者会话：`/login` 只认操作者密码，而密码只能由操作者（或本机的离线命令）设置；客户端令牌在 `/login`、`/logout` 和 `/api/` 其余路由上都不是凭据。阶段 1 不开任何通往这些的路径：客户端目录里没有碰密码、操作者令牌或客户端令牌的操作，`/api/clients` 只认操作者，测试锁住这几点。阶段 2a 的 `loopercli auth set-password` / `clear-password` 用独立的操作者入口：`--url URL --operator-token-file PATH` 从操作者令牌文件（同样的 0600 检查）读取，省略文件或给 `-` 则从标准输入读取；令牌只用于这一次请求，不写进 `client.json`。
- **认证：** `operator_auth.AREAS` 加 `Area("/api/v1/", Access.CLIENT)`，排在 `/api/` 前。`Access.CLIENT` 的请求在读请求体之前判身份：操作者凭据有效 → 操作者；否则 `Authorization: Bearer lpc_…`，且对端和 `Host` 都是回环、没有转发头，且令牌有效 → 这个客户端；否则 401（`WWW-Authenticate: Bearer realm="looper"`，类型化错误，不说令牌在哪）。失败只记对端和「unknown client token」，从不记令牌。
- **令牌不出现在：** 命令行参数、提示词、日志、`manager_actions`、聊天记录、任务记录里。`loopercli login` 从标准输入读令牌（终端上不回显），写进 0600 文件；之后每次调用从文件读。
- **令牌文件：** 默认 `$XDG_CONFIG_HOME/looper/client.json`（没设就是 `~/.config/looper/client.json`），`LOOPER_CLIENT_FILE` 或 `--client-file` 覆盖。内容 `{"url": "http://127.0.0.1:8767", "token": "lpc_…"}`。读时不跟随符号链接，用 `fstat` 确认是当前用户的普通文件且权限正好 0600，否则拒绝并给出修复命令（与操作者令牌文件同样的规则）。`url` 的主机必须是回环地址。
- **不跟随重定向：** `loopercli` 的每个请求（`login` 的检查、普通调用、取文档、聊天的 SSE 流）都拒绝 HTTP 重定向：3xx 一律报错退出，不读 `Location` 再发，`Authorization` 因此永远不离开配置的回环地址。每类请求各有一个测试。

## 7. 客户端能用的操作，以及依赖管家轮的地方

操作者调用沿用相同服务和留痕，`high` 单次执行；控制台的既有确认交互不变。客户端的调用由 `ops/clients.py` 的 `ClientOps` 执行，用默认管家的范围（`DEFAULT_SCOPE`：全部工具、全部任务；场景管家的模板只在和那个管家对话时可用）。每次调用造一个 `Turn(channel="api", chat=<会话>, message_id="api-<随机>", client_id, client_name)`：

| 管家轮里的东西 | 客户端怎么办 |
|---|---|
| 「回答用户消息的轮才能写」 | 客户端的每次调用都是明确的请求，写操作直接执行 |
| `high` 和 `escalate` | 照旧算出要确认的内容；登记到（客户端，会话），回答 202（第 8 节）|
| `looper_confirm` | 不给客户端；用 `POST /api/v1/confirm` |
| 转用户原文进关卡（`looper_task_gate_reply`） | 不给客户端；用 `looper_gates` 的 reply/end：客户端的话就是用户的话，文本作为参数给出，和任务页的关卡输入框一样走 `gate_service.post_message`，`via` 为 `client:<名字>` |
| 渠道、发送者、通知对象 | `channel="api"`，没有发送者；建任务没有 `notify_target`，`origin_channel_id` 为空；定时任务的「在飞书/Telegram 里建」提示不出现 |
| 轮的绑定、`require_match`、流里的 tool_use | 不适用（没有模型的流） |
| `Turn.blocks` | 作为回答的 `notes` |
| `Turn.created`（同一轮建的定时任务不能在同一轮启用） | 每次调用一轮：建了以后另一次调用启用（启用是 `high`，再确认一次）|
| 任务归属 `manager_id` | 默认管家（`manager`），另记 `client_id` |

只给客户端的两个工具（不进任何管家的工具表，管家的工具列表不变）：

- **`looper_task_pipeline`**（read）：`{task_id}` → `{task_id, name, status, group, phase, steps: [{step, kind, status, depends_on, role?, instruction}], gates, waits}`，读 `db_tasks.get_task_with_steps`，关卡用 `ops/tasks._gate_listed`，等待用 `wait_service.waits_view`。
- **`looper_gates`**：`list`（read，`gate_service.list_gates`：`state`、`waiting_on`、`project_id`、`limit`、`offset`）、`messages`（read，`gate_service.thread`）、`reply`（write，`{task_id, gate, text, agenda_item?, option?}`）、`end`（write，`{task_id, gate, text?}`）。

## 8. 两步确认

操作者调用 v1 的 `high` 在一次请求中执行并留痕，控制台仍使用已有确认弹窗。以下两步流程只适用于客户端令牌，不由服务器自动确认。

- 客户端的 `high`（或被 `escalate` 升为 `high`）的调用不执行：登记 `_Pending(id, tool, arguments, impact, at)` 到 `(principal_key, session)`；同一键上新的待确认替换旧的；最多 50 个键，超出丢最旧的；写一行 `manager_actions`（`status = needs_confirmation`）；回答 202。
- `POST /api/v1/confirm {confirmation_id}`：同一客户端、同一 `X-Looper-Session`、id 相同、600 秒内 → 按当初的参数执行一次，删除登记，写一行 `status = ok` 或 `error` 带 `confirmation_id`。否则：没有或 id 不同 → 404 `not_found`；超时 → 410 `expired`（同时删除）。另一个客户端或另一个会话拿到 id 也确认不了（键不同）。
- 只在内存里：重启就没了，客户端再调一次原操作。
- 和管家对话里的确认不是一回事：那是管家的流程，用户（或替他说话的 agent）在同一对话里回一句「确认」，管家调 `looper_confirm`。`/api/v1/confirm` 只确认直接操作。
- `principal_key` 是 `client:<id>`，操作者是 `operator`。会话名 1–64 个字符 `[A-Za-z0-9._:-]`，不合法 400。

## 9. 归属与留痕

- `chat_threads.client_id`：客户端开的线程；`tasks.client_id`：客户端经对话或直接建的任务；`manager_actions.client_id`：客户端直接调用的每一次，以及客户端对话里管家的每一次调用。都是可空的 TEXT，不加外键（与 `manager_id` 一样，留痕比行活得久）。客户端直接调用的读操作不写 `manager_actions`（`loopercli wait` 每 10 秒读一次，会淹没留痕），写、待确认、确认和写操作的失败都写。
- 变更的 `via`：直接操作 `client:<名字>`（操作者 `client:operator`）；客户端对话里管家做的 `manager:loopercli`。任务页的历史和 Planner 看到的关卡留言因此能分出来源。
- `GET /api/tasks/:id` 加 `client`（`{id, name}` 或 null），`manager_actions` 每行加 `client_name`；任务页「By the Manager」一行显示 `loopercli · <名字>`。

## 10. `loopercli chat`：和管家对话

- **请求：** `POST /api/v1/chat/send {text, conversation_id?, manager_id?, language?}`，`text` 1–32,000 个字符。不给 `conversation_id` 就新开线程 `cli-<16 位十六进制>`，给了就必须是这个客户端的线程（`chat_threads.client_id` 相同），否则 404；控制台的线程（`client_id` 为空）对客户端一律不可用。`manager_id` 选管家（默认 `manager`），与线程的归属不符时 409 并说出归属。
- **只给客户端：** 聊天的三个 v1 操作要客户端令牌；操作者用控制台自己的聊天（403 `forbidden`），控制台改用 v1 聊天在阶段 2 之后再定。
- **同一份实现：** 把 `http_server._handle_chat_send` 的核心抽成 `_send_chat(data, client=None)`，控制台路由和 v1 都调它：认领线程（`db.chat.claim_thread` 加 `client_id`）、存用户消息、交给 `WebChannelAdapter.handle_user_message(..., client={"id", "name"})`。`client` 只由服务端按令牌填写，请求体里同名的字段被拒绝（未知参数）。
- **回答：** `text/event-stream`，事件依次：`accepted {conversation_id, message_id, manager_id}`、若干 `delta {text}` 和 `status {label}`、最后 `final {text, outcome}`；15 秒没有事件发 `heartbeat`。流最长 1,800 秒，超时发 `timeout {message}` 后结束：回复照样写进线程，用 `--read` 读。
- **渠道行：** 管家在消息前写 `Channel: loopercli, client "<名字>" (the user works in their own coding agent, which writes these messages for them)`，管家因此知道是人经自己的 agent 在说。名字的字符集在创建时就限制住了。
- **命令：** `/new`、`/profile`、`/gates`、`/gate N …`、`/stop`、`/help` 由管家自己回答，和别的渠道一样。注意 `/new` 重置的是这个管家的整条会话（所有渠道共用），skill 只在用户要求时发。
- **线程归属：** 线程按 `chat_threads.manager_id` 归属于开它的管家（默认管家或所选的场景管家），另记 `client_id`；接着对话时消息发给线程的管家。
- **回报：** 客户端对话里建的任务记 `origin_thread`（D9），接受和结束写进这个线程。关卡和进度不写进线程（第 11 节）。
- **读线程：** `POST /api/v1/chat/messages {conversation_id, after?, limit?}`，在数据库里按游标向后分页：游标是线程里一条消息的 `message_id`，按存储顺序（`chat_messages.id`）取它之后的最多 `limit` 条（默认 100，最多 200）；不给 `after` 从线程开头取。回答 `{conversation_id, manager_id, messages, next_cursor, has_more}`：`next_cursor` 是本页最后一条的 `message_id`（本页为空时是传入的 `after`，空线程是 null），`has_more` 说明游标之后还有没有。`after` 不是这个线程的消息 → 400 `invalid`（「unknown cursor」），绝不静默从头重放或漏掉。`POST /api/v1/chat/conversations {}` → 这个客户端的线程，最近活跃的 50 个。
- **命令行：** 一次性 `loopercli chat "…"`、`--file PATH`（`-` 读标准输入）；没有消息又在终端上时进交互模式（一行一条，`Ctrl-D` 退出）；`--conversation ID` 接着同一线程，`--manager ID` 选管家；开头在标准错误打印 `conversation: cli-…`；`--read [--after ID]` 只读线程（按 `next_cursor` 翻页直到 `has_more` 为假）；`--list` 列出这个客户端的对话；`--follow` 回复后继续每 5 秒从上次的 `next_cursor` 读线程、打印新消息，直到 `Ctrl-C`；`--json` 打印 `{conversation_id, message_id, reply, outcome}`。

活动流由 OpenAPI 发现：生成式 `loopercli tasks activity-stream --task-id ID --since SEQ` 调用现有 `Api.stream`；终端输出事件名与数据，`--json` 逐行输出 `{event,data}`，不假设聊天事件。中断关闭连接，拒绝重定向；手写 chat 命令不变。

## 11. 客户端怎么跟进任务

阶段 1 的关卡和进度都靠轮询，不推送：对话里只有任务的接受和结束（第 10 节），关卡通知照旧走全局通知目标（`gate_courier`），不进客户端的线程。跟进的命令：`loopercli wait <task> [--for end|gate|any] [--timeout S] [--interval S]`：每隔 `interval`（默认 10 秒）读 `tasks.get`，按任务的分组判断（`db_tasks.TaskGroup`）：`needs_you`（有关卡等用户）算关卡打开，`attention`、`completed`、`cancelled` 算结束；关卡打开、任务结束或超时时返回：条件满足退出 0 并打印原因（关卡打开时列出等用户的关卡），超时退出 6。另有 `loopercli status <task>`（`tasks.get`）、`loopercli pipeline <task>`（`task_pipeline`）、`loopercli gates list --waiting-on user`、`loopercli chat --conversation … --read/--follow`（对话里的接受和结束）。skill 用 `wait` 跟进关卡，agent 可以把它放到后台跑。不新建推送渠道；想在手机上收到，照旧通过飞书或 Telegram 和管家对话。按任务所属的管家和线程投递关卡与进度是以后的事，要改 `gate_courier` 并补测试，不在本文。

## 12. `loopercli`

- **程序：** `looper/loopercli.py`，console script `loopercli = "looper.loopercli:main"`；只导入标准库和 `looper.__version__`（测试在新解释器里检查没有加载 `looper.agents`、`looper.db`、`asyncpg`）。
- **全局选项：** `--client-file`、`--session`（也读 `LOOPER_SESSION`）、`--json`。
- **手写命令：** `login [--url URL]`、`chat`、`wait`、`confirm <id>`、`skill path|show`，和别名 `create`（= `task-create`）、`list`（= `tasks list`）、`status [TASK]`（有任务 = `tasks get`，没有 = `overview`）、`start TASK` / `stop TASK`（= `task-control start|stop --task-id`）、`pipeline TASK`（= `task-pipeline`）。别名只是把位置参数填进属性，再走生成的路径。阶段 2a 增加 `auth set-password|clear-password`、`doctor` 和 `config export|import`（第 15 节）：认证交互和文件读写由客户端处理，操作路径仍从运行服务的 OpenAPI 文档解析。
- **生成的命令：** 每次运行先 `GET /api/v1/openapi.json`。文档里每个操作一个子命令，标了 `x-looper-cli-hand: true` 的除外（聊天三个、`confirm`、文档本身：由手写命令负责）；`whoami` 就是生成的 `loopercli whoami`。工具操作的子命令是`<名>`（下划线换成连字符），有动作的再一层动作：`loopercli tasks list`、`loopercli agent-profiles create`、`loopercli gates reply`。每个属性一个选项 `--<属性>`（下划线换成连字符，如 `--task-id`）：字符串（另有 `--<属性>-file PATH`，`-` 读标准输入，给长文本）、整数、布尔（`--x` / `--no-x`）、枚举（choices）；对象、数组和可空类型收 JSON 文本；`--body PATH|-` 给整个请求体（JSON 对象），具体选项覆盖它。必填由服务端判（可能来自 `--body`）。帮助文字是属性的 `description`。服务端不可达时，`--help` 只列手写命令并说明原因。
- **输出：** 默认易读：顶层的标量 `key: value`；值是对象列表的键，打印成表（第一个对象的标量键，最多 8 列，单元格截到 60 字）；然后是 `notes`。`--json` 原样打印回答。待确认时打印 `impact` 和 `loopercli confirm <id>`。
- **重定向：** 不跟随，见第 6 节；服务回答 3xx 时打印拒绝的原因，退出 4。
- **退出码：** 0 成功；1 错误回答（打印 `code: message`）；2 用法错误；3 待确认；4 连不上、没有令牌文件、401 或被拒绝的重定向；6 `wait` 超时。

## 13. `looper-skill` 与安装

一个 `SKILL.md`（`name: looper-skill`），只用 `loopercli`，教这些工作流：

1. **把讨论好的设计交给管家：** 先 `loopercli whoami` 确认可用；把需求整理成一条消息（目标、范围、验收、项目和目录、相关文档的路径），`loopercli chat --file …` 发出，记下 `conversation`；之后每句都带 `--conversation`。
2. **回答管家的问题：** 从本会话的讨论里找答案；讨论里没有的，问用户，不替用户编。
3. **替用户确认的边界（维护者 2026-10-22 决定）：** 用户在本会话里说了提交、开始或继续（submit、start、go ahead）时，agent 可以替用户确认：创建并开始这个任务（含任务需要的、按讨论命名的新项目）、修改它的需求、回答它的关卡。取消、删除、改设置、改 Agent Profile、改 provider，一律先把管家要确认的内容原样转告用户，用户同意后才确认。没把握时按「先问用户」处理。
4. **看状态和流水线：** `loopercli wait`（后台，关卡打开或任务结束时返回）、`status`、`pipeline`、`chat --read`（对话里只有接受和结束，关卡和进度不会出现在对话里）。
5. **回答关卡：** `loopercli gates list --waiting-on user`、`gates messages`、`gates reply`；内容来自用户或本会话已定的结论。
6. **改任务：** 在对话里让管家改，或直接 `task-update`、`task-instruct`。
7. **管理 Profile：** `agent-profiles list|create|update|delete`；`delete` 和改动都按第 3 条先问用户；待确认的直接操作用 `loopercli confirm`。
8. **不做的：** 不用 curl 或直接读写 Looper 的文件和数据库；不把令牌写进任何命令、文件或消息；不发 `/new`，除非用户要求。

**安装（Looper 不写任何人的 agent 配置）：**

- **Claude Code：** 仓库里有插件 `plugins/looper/`（`.claude-plugin/plugin.json`、`skills/looper-skill/SKILL.md`）和仓库根的 `.claude-plugin/marketplace.json`。用户自己执行 `claude plugin marketplace add foolzzz/looper`、`claude plugin install looper@looper`，或开发时 `claude --plugin-dir plugins/looper`。命令形式「需要验证」：以实现时安装的 Claude Code 版本的帮助为准。
- **Codex、OMP：** `loopercli skill path` 打印随包安装的 `looper-skill` 目录，`loopercli skill show` 打印 `SKILL.md`。用户把这个目录复制或链接到自己的 skill 目录（Codex 的 `$CODEX_HOME/skills/`、OMP 的 skill 目录），或在自己的 `AGENTS.md` 里引用它；各自的目录位置「需要验证」，README 按实测写。
- 插件里的副本与随包的副本由测试保证逐字节一致；skill 里提到的每个 `loopercli` 命令都由测试对照手写命令和文档检查存在。

## 14. 任务附件（阶段 3）

- **存储：** 表 `task_attachments(id, task_id REFERENCES tasks ON DELETE CASCADE, name, media_type, size_bytes, sha256, content BYTEA, client_id, created_at)`，与项目知识一样内容在库里（备份和迁移都带上），运行前物化到 `<runtime root>/.local/tasks/<task_id>/attachments/<name>`。
- **限制：** 每个文件 10 MiB，每个任务 20 个、合计 50 MiB；名字只留字母、数字、`.`、`-`、`_`，最多 100 个字符，同一任务里不重名；类型按扩展名白名单：`md txt json yaml yml csv toml xml html pdf png jpg jpeg` 和常见源码扩展名。
- **上传：** `PUT /api/v1/attachments/<name>`，原始字节流式写入（像配置导入，有自己的 10 MiB 上限），回答暂存 id（属于这个客户端，24 小时过期）；建任务时 `task_create` 的 `attachments: [id…]` 或对话消息 `chat.send` 的 `attachments` 把它们挂到任务上。对话里，管家看到消息后列出的附件名、大小和 id，建任务时传给 `looper_task_create`（管家工具 schema 的改动，跑管家对话评测）。
- **Planner 和 Worker 怎么读：** 任务段列出「Attachments (read them as files)」和每个物化后的绝对路径；在 runtime root 下、不在仓库里，原地和 worktree 两种模式路径相同，只读的 Planner 也能读。需求里写摘要并指向附件。
- **任务页与导出：** 任务页一个 Attachments 列表（名字、大小、下载 `GET /api/v1/tasks/<id>/attachments/<name>`）；配置导出不含任务，所以不含附件；任务报告列出名字和 sha256。
- **删除：** 随任务删除（外键级联加删除物化目录）；没挂到任务的暂存过期即删。

## 15. `looper` 命令的迁移（阶段 2a，已交付）

| 原命令 | 已交付行为 |
|---|---|
| `looper auth set-password` / `clear-password` | 日常：v1 `auth.set_password` / `auth.clear_password`，只接受操作者身份（第 6 节），客户端令牌 403；`loopercli auth set-password` 用操作者入口认证，在终端上两次不回显地读密码，只放在请求体里，服务端照旧 scrypt、原子写文件、清掉现有会话。救援：`looper auth set-password` / `clear-password` 留在 `looper`，服务起不来、忘了密码时在本机直接写凭据文件 |
| `looper doctor` | 日常：v1 `system.check`（`self_check` 的结果，含 pg_dump）；`loopercli doctor`。救援：`looper doctor` 留在 `looper`，服务起不来时在本机检查凭据文件、数据库、schema 和配置，说出哪一项挡住了 `serve` |
| `looper config export` / `import` | v1 `config.export`（已有的 `looper_config_export` 与 `GET /api/config/export`）、`config.import_preview` / `config.import_apply`（已有的 `/api/config/imports`）；`loopercli config export <file>` / `loopercli config import <file>`；应用预览用 `loopercli config import --apply <import_id>`，客户端再用 `loopercli confirm <confirmation_id>` 确认 |
| `looper config init-ops` | 留在 `looper`：它写服务启动前就要的运维文件，那时没有 API |
| `looper serve`、`service`、`version`、`mcp-server` | 不变 |

`looper` 里留下的是本机的运维和救援命令（`serve`、`service`、`version`、`mcp-server`、`config init-ops`、`doctor`、`auth set-password` / `clear-password`），它们不经 API，服务起不来时也能用；其余交互都经 API 和 `loopercli`。`config export` / `import` 已从 `looper` 删除，发布说明的 **Breaking changes**（含中文）写明替代命令；它们现在要求服务在跑。导出返回配置文档，预览暂存配置并返回 `import_id`；应用按原导入流程重新计算，在一个事务里写入。配置应用遵守第 8 节的客户端两步确认。控制台、Telegram 和 Lark 沿用共享核心，本阶段不迁移这些入口。

v1 配置预览收 `{document: <配置对象>}`，和控制台一样最多 256 MiB，暂存一小时后过期；暂存文件归调用者所有，客户端不能应用别人的文件。`loopercli config export` 在本地新建 0600 文件（`-` 输出到标准输出），不覆盖已有文件；`config import` 默认只预览，`--apply <import_id>` 请求应用。`system.check` 返回运行中服务当前运维设置的 `self_check.run_checks` 结果，含 pg_dump；本机后台服务管理器的检查仍由离线 `looper doctor` 执行。

## 16. 界面

- **Settings → API clients**（新页，`#settings/clients`）：说明「本机的 agent 和脚本用 `loopercli` 访问 Looper 的令牌」；列表（名字、创建时间、最近使用）；新建（名字）后只显示一次令牌和 `loopercli login --url <本服务地址>`，提示令牌不再显示；吊销（确认框：立即失效，过去的记录保留名字）。
- **任务页：** 「By the Manager」的行显示 `loopercli · <名字>`；其余不变。
- 控制台的聊天列表会出现客户端的线程（它们是 web 线程，按 `manager_id` 归属于开它的管家：默认管家或所选的场景管家），阶段 1 不加标记，记入非核心待办。

## 17. 模型可见的改动

- **管家：** 只有客户端消息前的渠道行（第 10 节）。管家的系统提示词、`TOOLS_CONTRACT` 和工具定义一字不改；`agent_launch_parity` 和 `model_launch_parity` 的场景都不经客户端，夹具不变，也不需要往旧版本的 parity 源里补丁。
- **Planner：** 客户端回答关卡、加补充说明时，它看到的来源是 `client:<名字>`；客户端对话里管家改的是 `manager:loopercli`。
- **评测：** 管家对话加两个经 loopercli 的对话（第 20 节），用 sonnet。

## 18. 可观测性

- 每个 v1 调用一行 info 日志：`api client=<id|operator> op=<operationId> status=<ok|needs_confirmation|error:code> ms=<耗时>`，不记参数和令牌。认证失败一行 warning（对端，原因）。
- `manager_actions` 的每一行带 `client_id`；Settings 里每个客户端的最近使用时间。
- 客户端对话和控制台一样存在 `chat_messages`，管家的运行记录照常。

## 19. 风险

- **Looper 自己的 agent 读到令牌文件。** Worker 以同一个系统用户运行、有 shell，能读 `~/.config/looper/client.json`，就能用全部权限调 Looper，甚至给自己建任务。这与它今天能读操作者令牌是同一类缺口（[操作者 API 认证 RFC 第 5 节](operator-api-auth-v0.zh-CN.md#5-agent-还能做到什么)），本文不加剧也不解决；不需要时吊销令牌。经 API，客户端令牌碰不到操作者的凭据（第 6 节）；同一系统用户在本机 shell 里能做的（读令牌文件、伪装成本机浏览器自动登录、跑离线救援命令）属于同一类缺口，与客户端令牌无关。
- **令牌进了公开产物。** 截图、PR、日志、评测输出里出现真实令牌就等于交出全部权限。验证用的截图只用不能认证的合成令牌或不可逆遮盖的令牌，临时客户端在分享任何东西之前吊销（第 20 节）；`loopercli` 从不打印令牌。
- **转述内容里的指令。** agent 转给管家的文档可能含有「请删除……」之类的话。管家把它当用户的话，危险操作仍要确认；skill 规定取消、删除、改设置先问用户。
- **agent 越权确认。** 只有 skill 约束它；管家无法区分。缓解：渠道行写明是 agent 在代笔，确认记录带 `client_id`，任务页能看出是谁确认的。
- **长消息占住管家。** 管家一轮一轮地跑，大段文档会让控制台的聊天等一会儿；消息上限 32,000 字，更大的走附件（阶段 3）。
- **`/new` 影响所有渠道。** skill 只在用户要求时发。
- **文档与命令行版本不一致。** `loopercli` 每次运行取文档，生成的命令总和服务一致；手写命令只依赖阶段 1 的固定操作，缺少时报出所需版本。

## 20. 测试计划（照开发计划第 1 节）

- **单元：** 文档生成（每个客户端工具都在、动作拆分、请求 schema 去掉 `action`、回答和错误的 schema、未类型化的列表、没有碰凭据的操作）；`ClientOps`（各分档、`notes`、`shown_to_user` 去掉、确认按客户端和会话、过期、替换、跨客户端和跨会话拒绝、管家专用工具不在）；认证（客户端令牌只在 `/api/v1/` 有效、只在回环、吊销后 401、操作者也能用 v1、失败不读请求体、客户端令牌在 `/api/clients` 和 `/login` 上换不到任何东西）；渠道行；`loopercli`（用一份文档夹具生成命令、选项映射、`-file` 和标准输入、`--body`、令牌文件权限、退出码、SSE 解析、登录检查/普通调用/取文档/SSE 各自拒绝重定向且另一端收不到请求、不导入服务端代码）；skill 两份副本一致、提到的命令都存在。
- **PostgreSQL：** `api_clients` 的创建、查找、吊销、名字唯一；经 HTTP 服务的直接操作端到端（建任务 → 新项目要确认 → 确认 → `tasks.client_id` 和 `manager_actions.client_id`）；聊天：新线程归客户端、别的客户端和控制台的线程被拒、`origin_thread` 写入、回报进线程、按游标分页（`next_cursor`、`has_more`、未知游标报错）；场景管家的成功路径（新开、续聊、确认、任务回报进线程）和跨管家拒绝。
- **真实路径：** 从 worktree 起 `looper serve`（新库、临时 runtime root、非 8767 端口）；在 Settings 里建令牌（无头浏览器截图一张，截图里的令牌是不能认证的合成值或不可逆遮盖的，临时客户端在分享任何东西之前吊销，任何公开产物里都没有令牌）；`loopercli login`；`loopercli chat` 用真实管家（sonnet）走一遍交付；再起一个真实的 Claude Code 会话（`claude -p --plugin-dir plugins/looper --model sonnet`），给它一份设计文档：它用 skill 交给管家、从文档回答问题、在「用户说了提交并开始」时确认建任务和开始、用 `pipeline` 汇报；另一轮让它取消任务，检查它把确认转给用户而没有自己确认。
- **提示词评测：** 管家对话加 `client_handoff`（经客户端交一份设计，消息没写用户同意时，管家复述、不建；代笔确认后建出任务，任务带 `client_id`、`origin_thread`）、`client_handoff_agreed`（消息写明用户已同意提交并开始，管家直接建；已建未开始或已开始都算对）和 `client_relay_cancel`（客户端说「用户同意取消」，取消照旧两步、确认后执行）；用 sonnet 跑，`--reps 2`，不用 haiku。

## 21. 阶段

每个阶段或独立交付的子阶段一个 PR，都让控制台照常工作。

- **阶段 1：** 客户端令牌（表、Settings 页、认证）；v1：客户端目录生成的操作、两步确认、文档、聊天三个、`whoami`；管家的渠道行和客户端对话的回报；`loopercli`（生成的命令、别名、`chat`、`wait`、`confirm`、`login`、`skill`）；`looper-skill` 与 Claude Code 插件；两个管家对话评测；文档。可用的结果：在别的 agent 会话里装上 skill，就能把讨论好的活交给管家并跟进。
- **阶段 2a（已交付）：** 第 15 节的命令迁移：只认操作者的 `auth.set_password` / `auth.clear_password`、`system.check` 和 `config.export` / `config.import_preview` / `config.import_apply` 进入同一 v1 文档，结果类型化；`loopercli` 提供对应命令；`looper` 删除配置导出、导入，保留 doctor 和密码重设作离线救援。配置操作与原路由、工具共用实现，客户端应用配置需两步确认。
- **阶段 2 其余部分（待做）：** 其余 HTTP 路由成为 v1 操作（任务删除、项目知识与 skill、角色、管家与模板、渠道、目录上传和 git、设置页、运行记录、活动流……），全部操作的结果类型化；客户端令牌管理的 v1 操作；第 5.4 节其余重复实现合并（建任务是一个共享核心加两个参数适配层）；控制台改用 v1，不再用的旧路由删除。按领域拆成各自可用的 PR。
- **阶段 3：** 任务附件（第 14 节）。

| 领域 | 阶段 2 状态 |
|---|---|
| 状态与 transport | 已验证：HTTP/文档 112 项、web 26 项；隔离 serve 与 1280/390 浏览器通过，完整套件留给 CI |
| 任务 | 关卡列表和讨论、等待、任务角色与办公台已迁移 v1：完整 DTO、`tasks.room` 紧凑结果类型化，列明的八个旧方法删除，既有 Tool schema 不变。本轮后端定向 416 项、真实 serve 关卡流程 4 项、评测及发布脚本回归 13 项、前端 96 项与构建通过，隔离 serve 和 1440/390px 浏览器验收通过；完整套件留 CI，其余任务操作待迁移。共享任务核心 [#429](https://github.com/foolzzz/looper/pull/429) 单独跟踪 |
| 任务 | 配置、归属、Agent 选择与预算续跑已迁入 v1；领域/OpenAPI 140 项、web 30 项、隔离 serve 与桌面/窄屏验证通过；历史回归 3 项首轮失败、隔离重跑通过，最终失败 0；完整套件未跑，留给 CI；其余任务入口待迁移，共享任务核心 [#429](https://github.com/foolzzz/looper/pull/429) 单独跟踪 |
| 聊天 | 待迁移 |
| 项目与知识 | 待迁移 |
| 能力目录 | 目录条目与安装已迁移：完整 DTO 与 compact Tool 结果类型化，控制台改走 v1，八个旧方法删除。此前目录与安装的 249 项定向测试、隔离 serve 与桌面/390px 验收通过（其 session-ended/abort 未单独实测）；项目自有 skill 四个操作已迁移，沿用默认启用、核心限制及事务，删除列明跨项目安装影响并重验引用。本次相关定向 pytest 218 项及前端构建通过，隔离 serve 与 1280/390px 浏览器增改、开关、删除、错误、abort、session-ended 验收通过，旧 skill 路由请求为 0；独立 Vitest 专项因 worker 启动超时未取得有效通过结果，完整套件留给 CI；目录获取来源七操作与控制台同步迁移，七个旧 POST 删除，凭据入口只认操作者并脱敏，客户端 Git 导入统一 high 确认且固定预览 commit。此轮验证通过：相关定向 pytest 386 项（分批执行，初轮失败修复后对应重跑通过）、前端 30 项、构建与隔离 serve 的 1440/390px 浏览器流程（含错误、空态、abort、session-ended）；最终失败 0、跳过 0；完整套件未跑，留给 CI |
| Agent 与 Profile | 待迁移 |
| 管家与模板 | 待迁移 |
| 渠道 | Lark 身份/人员发现与 dashboard 关卡通知设置已迁移：四个普通 v1 操作、完整 DTO、六个旧方法删除，控制台与生成 CLI 同步；操作者单次执行，客户端 high 同会话确认并在执行时重验目标。新增契约、文档及既有调用方的 202 项定向 pytest 与 web 39 项（含共享 v1 传输）、TypeScript 检查通过；隔离全应用 serve 的 29 个 HTTP 请求通过。渠道配置等其余部分待迁移，1280/768px 浏览器以假适配器验收身份问题、搜索结果/空/错误、目标保存/清空及 abort/session-ended 通过（无消息发送）；完整套件留给 CI |
| 文件与 Git | 待迁移 |
| 设置 | 待迁移 |
| 运行记录与活动流 | 活动、detail、对话和 SSE 已迁入 v1，四个旧 GET 删除；控制台与生成式 CLI 已迁移。通过：733 项 pytest、89 项前端测试、构建及隔离 serve 桌面/390px 浏览器分页和重连；最终失败 0；98 项跳过不计通过，完整套件留 CI，运行列表待迁移 |
| 客户端凭据 | 待迁移 |

阶段 2a 保持已交付；共享任务核心独立跟踪，阶段 3 仍待做。整体完成由最终审计确认。

## 22. 默认行为的变化

- 任务活动读取的数据库失败现在返回 `unavailable`（503），不再伪装成空成功；detail 同时检查 task_id 和 seq，未知任务或记录返回 not_found。窗口、游标优先级保持上述原有规则；对话窗口使用数据库的 `rows` 集合键，控制台同步适配。SSE 从数据库生成完整事件，回放与实时按 seq 衔接，终态 done 与断连清理保留。Telegram/Lark 使用未改动的 Manager ops/services，无伴随 transport 改动。

- 等待恢复/取消、任务角色编辑与完整办公台查询新增普通 v1 操作，生成 CLI 子命令；关卡讨论复用 `gates.*`。八个旧方法删除，等待与关卡设置保持原路径。控制台关卡列表通过 v1 offset 分页读取完整排序结果，保留已加载边界；超过 200 行时刷新需多次请求，检测到跨页条数变化、重复行或已保存边界全部失效则显示可重试错误，保留原界面数据。等待操作的 JSON 必须提供 `task_id/step`；角色编辑须提供 `harness/profile_id`（可为 null），省略 `items` 仍保留原值；办公台过滤参数按声明拒绝非法类型、负游标和超出 1–500 的条数。完整办公台不裁剪消息并保留咨询，`tasks.room` 继续裁剪；Telegram/Lark 的 Manager ops/services 不变。
- `/api/v1/` 的普通操作也接受操作者身份；操作者的 `high` 单次执行，客户端令牌仍须两步确认。控制台确认弹窗与可见行为不变。状态及服务就绪探测改走 v1，旧状态 GET 删除；Telegram/Lark 继续使用未改动的 Manager ops/services。配置命令迁移是阶段 2a 单独列出的行为变化。
- 任务配置仍仅 `init` 可改，需求保留版本、目录解析后读回；项目移动支持原有状态并同步运行中的归属。Agent 选择保留完成任务不可改、null 清除与降低并发不中断运行者的行为，升高并发仍是普通写。预算续跑只接受预算失败且新额度高于已花费的任务，发送一次 `AT_CAP` 重启；有独立额度时只改该任务，否则改项目每任务额度，影响其他任务；客户端确认绑定当时的项目与额度作用范围，确认前变更则拒绝，要求重新请求。四个旧方法返回 404，控制台与生成式 CLI 同步迁移。闭合请求 schema 在服务前拒绝未知字段、角色及错误类型，错误文案改为 schema 校验失败；配置或额度写入失败返回错误，不作已完成。
- 目录条目与安装的八个旧 HTTP 方法已删除；控制台改走 v1 并保留原有弹窗。目录删除对客户端为 high 两步确认，卸载仍与原 `catalog` Tool 共用普通写策略；条目更新只认操作者，避免 MCP 环境变量或 header 秘密进入凭据审计。
- 目录获取来源的七个旧 POST 删除，控制台和生成式 CLI 改走 `catalog_sources.*`；不新增 Manager Tool，Telegram/Lark 继续使用原有 ops/services。Git 导入要求预览 commit 与选中子集；漂移返回 409，事务不写入任何选中项。客户端 Git 导入统一 high 两步确认，覆盖范围包含执行时的本地修改（避免 clone 期间编辑绕过确认），操作者单次执行且保留控制台既有确认。预览只读，上传和新建仍安装到零处，归档与 URL 安全规则不变。本机/MCP 导入和 MCP 新建只认操作者，在校验前拒绝客户端且不记录请求/结果审计。
- 项目自有 skill 的旧 POST/GET/PUT/DELETE 方法已删除，控制台与生成的 `loopercli project-skills add|get|update|delete` 改走 v1。新增仍默认启用，显式 `false` 保留；只修改提供的字段。删除为 high，影响覆盖其他项目的安装：客户端须同会话确认，操作者单次执行，控制台保留既有确认弹窗。Telegram/Lark 的 Manager ops/services 与模型可见内容不变。
- Lark 身份/人员发现及 dashboard 关卡通知设置改走 v1，六个旧方法删除；控制台可见行为保持，lark-cli 的缺省 `complete` 明确为 true，外部人员查询失败改为类型化 503 `unavailable`（原为 502），不伪装为空结果。Telegram/Lark 仍走原 Manager ops/services，不新增模型可见 Tool。
- 管家：只有客户端的消息多一种渠道行。
- 默认管家：客户端对话里建的任务把接受和结束写回那个线程（控制台对话里建的不变）。
- 关卡通知和进度不变：照旧走全局通知目标，不进客户端的对话。
- 阶段 2a 已删除 `looper config export` / `import`，改用要求运行服务的 `loopercli config export` / `import`，发布说明已写明；`looper doctor` 和密码重设保留。控制台的停止和取消对不允许的状态回答 409 仍是阶段 2 其余部分的工作。
