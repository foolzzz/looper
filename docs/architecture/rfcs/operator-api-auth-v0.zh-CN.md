# RFC：dashboard 的 HTTP API 认调用者（v0）

- **状态：** 提案。第 7 节的决定按建议写下，等维护者确认。
- **开发计划：** [长程 Agent 上下文体系开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md) 第 5.2 节「dashboard 的 API 不验证调用者」（looper #181 评审发现）。
- **最近更新：** 2026-10-18

## 1. 背景与目标

`looper serve` 的 HTTP API 不认身份。`request_guard.py` 只挡浏览器里别的网站的页面（按 `Origin`）和 DNS 重绑定（按
`Host`），不带 `Origin` 的客户端照样能调。本机上任何进程都能调用：有 shell 的规划者或 Worker 可以
`PUT /api/tasks/:id/roles/:key`，记成用户写的版本；同样也能改用户给任务选的 Agent Profile、在关卡里替用户发言、启动或
停止任务。所有「用户的选择优先」的规则都依赖「写接口的调用者就是用户」这一点。

**目标：**

- 用户自己的接口（`/api/` 下除 agent 工具以外的全部路由）只认用户的凭据：没有或不对就 401，并说明原因；
- 浏览器里的 dashboard 不用登录：在本机浏览器里打开就能用，书签和聊天消息里的链接照旧能打开；
- `looper` CLI、评测 runner、测试夹具都换成带凭据的调用；
- agent 进程拿不到用户的凭据：它们的 MCP 工具照旧只用各自的令牌，只管自己的任务和席位；
- 已有安装升级后照旧能用：第一次启动时生成凭据，`looper service restart` 自己读到它。

**不做：** 不把同一个系统用户下的进程彼此隔开（见第 5 节）；不加多用户、账号或权限分级；不改 agent 工具
（`/api/agent/v1/`）的令牌。

## 2. 调用方清单（读代码得出）

| 调用方 | 怎么调 | 现在带什么 | 之后带什么 |
|---|---|---|---|
| 浏览器里的 dashboard（`web/src/lib/api.ts`） | 同源 `fetch` 和 `EventSource`（任务活动、聊天的 SSE） | 什么都不带 | 页面加载时拿到的 HttpOnly cookie，浏览器自动带上；前端代码不改 |
| Vite 开发服务器（`web/vite.config.ts`） | 把 `/api` 代理到 `localhost:8767`，保留 `Host` 和 `Origin` | 什么都不带 | 浏览器发给 `localhost` 的 cookie 不分端口：先在同一浏览器里打开一次 `http://localhost:8767/`，代理就把 cookie 转过去 |
| `looper service install/start/restart/status`（`service.py` 的 `_get_status`） | `GET /api/status` 等服务起来、显示任务数和自检结果 | 什么都不带 | 从服务单元的 `--runtime-root` 读令牌文件，带 `Authorization: Bearer` |
| `looper doctor`、`looper config export/import`、`install.sh` | 不调本机 API（读服务管理器、直连数据库、访问 GitHub） | — | 不变 |
| `looper mcp-server`（Manager、规划者、步骤运行的 MCP 工具） | `/api/agent/v1/tools` 和 `/api/agent/v1/ops/<tool>`，`Authorization: Bearer <各自的令牌>` | 各自的令牌（`ops.service`、`ops.planner`、`ops.room`） | 不变；这些令牌开不了用户的接口 |
| Telegram、Lark、网页聊天渠道 | 在 `looper serve` 进程里直接调函数 | — | 不变；它们发出的 dashboard 链接在浏览器里打开时照常登录 |
| 评测 runner（`evals/prompt_regression/runner.py` 的 `Api`，`dialogs.py` 也用它） | 启动 `looper serve`，轮询 `/api/status`，再建任务、读活动 | 什么都不带 | 从它给服务的 runtime root 读令牌文件 |
| 测试 | 大多直接调 `_route_api`，不经过连接；`test_request_guard`、`test_config_imports`、`test_db_backup` 走真实连接 | 什么都不带 | 走连接的带上令牌 |

## 3. 方案对比

| 方案 | 做法 | 浏览器免登录 | 挡住「照文档 curl 一下」的 agent | 代价 |
|---|---|---|---|---|
| **A. 用户令牌 + 本机浏览器自动登录（推荐）** | 服务第一次启动时在 runtime root 生成令牌文件（0600）；本机浏览器加载 dashboard 页面时，服务在响应里设 HttpOnly、SameSite=Strict 的 cookie；CLI 读文件；其他浏览器用 `looper login-link` 生成的单次、5 分钟有效的登录链接 | 是 | 是：要么读用户的令牌文件，要么伪造浏览器的请求头，都是有意为之 | 服务端一个模块；前端不改 |
| B. 只用带令牌的登录链接（Jupyter 式，未采用） | 每个新的浏览器会话先打开 `/?token=…` | 否：书签和聊天里的链接打开就是 401，升级后已打开的页面也失效 | 是，且不能靠伪造请求头绕过 | 每次都要先拿到链接；聊天消息里不能放令牌 |
| C. 写接口只认浏览器的同源请求 | 写请求必须带等于 `Host` 的 `Origin`（或 `Sec-Fetch-Site: same-origin`） | 是 | 否：curl 加一个请求头就过；读接口照旧敞开 | 最小，但几乎不设防 |
| D. 按连接查发起进程 | 用 `lsof` 等查出本机连接的 PID，是 agent 进程树里的就拒绝 | 是 | 部分：`setsid`、两次 fork 就脱离进程树 | 依赖平台、每个请求多一次进程查询 |
| E. 系统层面隔开 agent | agent 用另一个系统用户或沙箱跑，读不到用户的文件 | 是 | 是，真正的边界 | 改动运行时的启动方式和文件权限，超出本 PR |

选 A：它是唯一既不改用户的使用方式（书签、聊天链接、升级后不用重新登录）、又让 agent 不能「顺手」以用户身份调用的
做法。连不上自动登录的浏览器改用单次登录码的链接（第 4.4 节），可重复使用的用户令牌不进 URL。E 记为待办（第 8 节）。

## 4. 设计

### 4.1 令牌文件

- 位置：`<runtime root>/operator-token`（默认 `~/.looper/operator-token`），一行，`secrets.token_urlsafe(32)` 生成。
- runtime root 不存在就以 0700 创建；打开目录后用 `fstat` 检查，必须属于当前用户且组和其他用户都不能写，已有的
  0755 目录可以沿用；组可写的（umask 为 002 的系统上自己建的目录）拒绝启动并提示 `chmod go-w`，因为同组的人能把
  令牌文件换成别的东西。`looper service install` 和评测 runner 自己建的 runtime root 一开始就是 0700。runtime root 可以是符号链接，检查的是它指向的目录；属主或权限不安全就拒绝启动，提示修复。
- `looper serve` 启动时、连数据库和监听端口之前读它；文件不存在就生成：先写同目录的临时文件（0600），再用
  `os.link` 原子地放到位，两个同时启动的服务不会各写一个；已经存在就用已有的，所以重启不换令牌，已登录的页面和
  CLI 不受影响。
- 文件相对已打开的目录以 `O_NOFOLLOW | O_NONBLOCK` 打开（换成 FIFO 也不会卡住启动），只用同一个描述符 `fstat`
  检查和读取，最多读 4096 字节；必须是当前用户的普通文件，权限正好 0600。符号链接、FIFO、目录、属主不符或内容不是
  令牌就拒绝启动，提示删掉再启动；
  文件权限过宽也拒绝启动，提示 `chmod 600`，不自动改权限。
- 换令牌：停服务，删文件，再启动。已登录的页面重新加载一次即可。

### 4.2 路由分类（类型化，不按子串）

`operator_auth.Access` 三类，按路径前缀（API 的命名空间）分，分发也按同一个分类走，分类和分发不会不一致：

| 类别 | 路径 | 认什么 |
|---|---|---|
| `DASHBOARD` | `/api/` 以外的一切：构建好的 dashboard 文件 | 不认凭据（只有静态文件，没有数据）；页面本身负责登录（4.3） |
| `AGENT` | `/api/agent/v1/`，包括 `OPTIONS` | 各 agent 自己的令牌，由 `ops.service`、`ops.planner`、`ops.room` 检查；用户令牌开不了 |
| `OPERATOR` | `/api/` 下其余全部，包括未知路径和 `OPTIONS` | 用户令牌：`Authorization: Bearer <token>`，或本服务端口的 cookie |

白名单只有 `DASHBOARD` 这一类，`/api/` 下没有不认凭据的路由：`/api/status` 也要令牌（它的唯一非浏览器调用方
`looper service` 读得到令牌文件）。未知的 `/api/` 路径没有凭据时也是 401 而不是 404，不暴露哪些路由存在。
只有 `DASHBOARD` 的 `OPTIONS` 直接回答 204；agent 和用户接口都先认各自的凭据，`OPTIONS` 没有凭据时 401，
凭据对但没有对应方法时 404。

检查在读请求体之前做（和 `request_guard` 一样），没有凭据的请求连配置导入的上传都开始不了。比较用
`hmac.compare_digest`。401 的正文说明怎么登录，但**不写令牌文件的路径**：agent 撞到 401 时不该从回答里学到去哪读。
每次拒绝记一条警告日志（方法、路径、对端地址），agent 试探用户接口会留下记录。

### 4.3 本机浏览器自动登录

请求 dashboard 页面（`/`、`/index.html` 和单页应用的回退）时，同时满足下面几条，响应带上 cookie：

1. 是顶层页面加载：`Sec-Fetch-Mode: navigate` 且 `Sec-Fetch-Dest: document`（iframe、脚本、`fetch` 都不算）；
2. TCP 对端是 loopback 地址，`Host` 也是 loopback 地址（服务绑在 `0.0.0.0` 时，局域网里的浏览器不自动登录）；
3. 没有 `Forwarded` 或 `X-Forwarded-For`：经代理转来的请求，背后的浏览器可能在任何地方；
4. 服务有令牌。

cookie：`looper_operator_<端口>=<令牌>; Path=/; HttpOnly; SameSite=Strict`，不设过期时间（浏览器会话结束即失效，
下次打开页面又会设上）。

- **名字带端口：** 浏览器给同一主机的 cookie 不分端口，本机同时跑两个服务（如 8767 和一个开发实例）时各用各的，
  不会互相覆盖。
- **HttpOnly：** 页面脚本读不到；dashboard 显示 agent 产出的内容，万一有注入也拿不走令牌。
- **SameSite=Strict：** 别的网站发起的请求、嵌在别的网站 iframe 里的 dashboard 都不带它；从聊天消息点开的链接是顶层
  加载，满足 4.3 的条件，照常登录。
- **不设 `Secure`：** 服务讲 http；`__Host-` 前缀因此也用不了。
- 所有页面响应都有 `Cache-Control: no-store` 和 `Referrer-Policy: no-referrer`：页面每次加载都到服务这里，才能重新设上
  cookie，也不向下一个地址发送页面来源。其他静态文件照旧缓存。

### 4.4 登录链接

打不开自动登录的浏览器（另一台机器、经反向代理、不发 `Sec-Fetch-*` 的旧浏览器）用服务所在机器上的
`looper login-link [--port PORT] [--runtime-root DIR]` 生成链接。CLI 从 runtime root 读用户令牌，带 Bearer 凭据向
本机的 `POST /api/auth/login-codes` 请求；接口也接受已登录浏览器的 cookie，回答 201 和
`{"url": "<dashboard_url>/?code=<登录码>", "expires_in_seconds": 300}`。端口和 runtime root 默认与 `looper serve` 一样。

登录码用 `secrets.token_urlsafe(24)` 生成，只存在当前服务的内存里，5 分钟有效，只能用一次；最多保留 20 个未使用的码，
先清理过期的，再淘汰最早的。浏览器请求页面时，码对就删掉它、设上同一个 cookie，再用 303 跳回去掉 `code` 的地址，
保留其他查询参数；错的、用过的或过期的都回答 401，不设 cookie，并提示运行 `looper login-link` 重新生成。
成功和失败的登录响应都有 `Cache-Control: no-store` 和 `Referrer-Policy: no-referrer`。

跳转只允许以单个 `/` 开头、没有 scheme、也没有反斜线的本地路径，否则回到 `/`。这条登录路径不看对端地址和
`Sec-Fetch-*`。可重复使用的用户令牌不进 URL；旧的 `token` 查询参数只当普通参数，不再用来登录。

### 4.5 调用方的改动

- `looper service`：`unit_settings` 多读出单元的 `--runtime-root`；`_get_status(port, token)` 带上令牌。等服务起来时
  每次轮询都重新读文件（升级后第一次启动才生成）。401 也算服务在回答：状态行显示 `unauthorized`，说明令牌文件里的
  令牌被拒。
- 评测 runner：`Api` 带一个令牌；`running_server` 轮询时读 runtime root 下的令牌文件，读到并且服务回答才算起来。
- 测试：走真实连接的夹具给服务一个令牌并带上它。`AgentHTTPServer(operator_token=None)` 时用户接口全部 401（不是
  全部放行），漏传令牌的调用方会马上失败，而不是悄悄敞开。

### 4.6 迁移

新版本第一次启动生成令牌文件；`looper service restart` 从单元的 runtime root 读到它；目录或已有文件的属主、权限不安全时，
先按启动提示修复。已经打开的 dashboard 页面下一次请求会 401，重新加载一次就登录上（页面加载即登录）；用过旧令牌链接的
浏览器改用 `looper login-link`。旧版本的 CLI 对新服务调用 `service status` 会显示没有回答，升级 CLI 即可
（`install.sh` 本来就一起升级）。

## 5. agent 还能做到什么

agent 和服务以同一个系统用户运行，本 PR 不改这一点。这里写清边界：

- **拿不到的：** 令牌不在任何 agent 的环境变量、命令行、prompt、MCP 配置里；401 的回答不提令牌文件在哪。agent 照
  文档 `curl` 用户接口得到 401。agent 的 MCP 令牌只开它自己的工具，开不了用户接口。
- **有意为之仍能做到的：** 有 shell 的 agent 能读 `<runtime root>/operator-token`（0600 只挡别的系统用户；Codex 的
  `workspace-write` 沙箱、Claude Code 的 Bash、Grok、OMP 都不限制读）；也能伪造浏览器的请求头（`Sec-Fetch-*`、`Host`）
  拿到 cookie。这两件都不是完成任务时会顺手做的事，而且会出现在运行记录的工具调用里。
- **数据库：** agent 继承服务的环境变量，其中有 `LOOPER_DATABASE_URL`；没设时默认的本机 DSN 不带密码。有 shell 的 agent
  能直接连数据库写任何东西，绕过 API。这是比 API 更大的一扇门，本 PR 不处理，记为待办（第 8 节）。
- **cookie 跨端口：** 浏览器把 `127.0.0.1` 的 cookie 发给这台机器上任何端口。用户在同一个浏览器里打开某个 agent 起的
  本机服务（比如 Worker 的前端开发服务器）时，那个服务收得到这个 cookie。只有 https 才能用 `__Host-` 前缀避免。

真正把 agent 和用户分开需要系统层面的隔离（第 3 节方案 E）。

## 6. 测试与验证

- 每类路由（`DASHBOARD`、`AGENT`、`OPERATOR`）各在真实连接上测：不带凭据、错的令牌、对的令牌（Bearer 和 cookie）、
  agent 的 MCP 令牌；另一个端口名字的 cookie 不算数；未知的用户 API 路径没有凭据时 401；agent 和用户 API 的
  `OPTIONS` 没有凭据时 401，带正确凭据时 404；配置导入的上传在读请求体之前就被拒。
- `tests/test_operator_token_launch_db.py` 在设置 `LOOPER_TEST_DATABASE_URL` 时，用新数据库、临时 runtime root 和
  一次性端口运行真实 eval runner 的 fake 模式，捕获 Manager、Planner、Worker 和 Reviewer 的启动：完整环境、argv、
  cwd、转发的 stdin，以及 prompt、MCP、settings 文件和插件目录里的文本。断言这些输入都没有用户令牌，环境变量名也
  没有 `OPERATOR_TOKEN`；临时让 `serve` 导出令牌时应失败。没有测试数据库时跳过，不算验证通过。
- cookie：自动登录的响应带 `HttpOnly`、`SameSite=Strict`、`Path=/`，没有过期时间，页面 `no-store`；缺 `Sec-Fetch-*`、
  iframe、脚本请求、带 `X-Forwarded-For` 或 `Forwarded`、非 loopback 的对端或 `Host` 都不设；登录码对的 303，重用、
  过期或错的 401，旧 `token` 参数不登录；页面和登录响应都有 `no-referrer`；跳转拒绝 `//` 和反斜线，保留普通查询参数。
- 登录码接口没有用户凭据时 401，带凭据时 201；第 21 个码淘汰最早的；CLI 打印的链接只能兑换一次，并覆盖找不到令牌、
  服务器拒绝令牌和端口没有回答的错误。
- 令牌文件：缺少的 runtime root 创建为 0700，组或其他用户可写、属主不符的目录拒绝启动；文件首次生成且为 0600，
  重启读到同一个。0644 文件拒绝启动且权限不变，符号链接和目录拒绝；检查后路径被换成符号链接，不能读到替换后的令牌。
- `looper service`：状态查询带上单元 runtime root 里的令牌，401 显示 `unauthorized`。
- 变异验证：故意改坏检查（分类把 `/api/` 当成 `DASHBOARD`、跳过凭据比较、cookie 不带 `HttpOnly`），对应测试应失败。
- 真实路径的验证目标：一次性端口上的 `looper serve`（新数据库、临时 runtime root、替身 Claude），无头 Chrome 打开
  dashboard 不登录就能看到数据；`looper service` 的状态查询带令牌能读到；用一次替身模式的评测（`run --mode fake`）
  检查 runner 的调用。不改模型可见的内容，不跑真实评测；环境不可用的项目单独报告，跳过不算通过。

## 7. 决定（等维护者确认）

- **D1 方案 A**，理由见第 3 节。
- **D2 `/api/` 下没有不认凭据的路由**，连 `/api/status` 也要令牌：它的唯一非浏览器调用方读得到令牌；不为不存在的
  监控方另开健康检查路由。
- **D3 cookie 的值就是令牌本身**：换一个派生值不减少它能做的事；令牌文件不变，服务重启后已登录的页面继续可用。
- **D4 登录链接只带单次、5 分钟有效的码**（4.4）：`looper login-link` 带用户凭据向 `POST /api/auth/login-codes` 申请，
  可重复使用的令牌不进 URL；页面和登录响应都不缓存、不发来源，跳转只到本地路径。绑非 loopback 地址或在代理后的
  浏览器也能借此登录。
- **D5 不给 Claude Code 加读令牌文件的拒绝规则**：Bash 的 `cat` 照样读得到，规则还会把路径写进每个 agent 的命令行
  （运行记录和 `ps` 都看得到）。

## 8. 待办

- agent 的环境里去掉 `LOOPER_DATABASE_URL`，或给 agent 一个不能写数据库的环境（第 5 节）。
- 系统层面隔开 agent 和用户（第 3 节方案 E）。
- 非核心：dashboard 收到 401 时显示一页说明（重新加载，或用登录链接），而不是各处的「status 401」。
