# RFC：dashboard 的 HTTP API 认调用者（v0）

- **状态：** 提案。第 7 节的 D4 已由维护者确认，其余决定按建议写下。
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

**不做：** 不把同一个系统用户下的进程彼此隔开（见第 5 节）；不加多用户或权限分级；不改 agent 工具
（`/api/agent/v1/`）的令牌。

## 2. 调用方清单（读代码得出）

| 调用方 | 怎么调 | 现在带什么 | 之后带什么 |
|---|---|---|---|
| 浏览器里的 dashboard（`web/src/lib/api.ts`） | 同源 `fetch` 和 `EventSource`（任务活动、聊天的 SSE） | 什么都不带 | 本机页面加载时拿到令牌 cookie；其他浏览器用账号和密码登录后拿到会话 cookie；均为 HttpOnly，由浏览器自动带上，前端代码不改 |
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
| **A. 用户令牌 + 本机浏览器自动登录（推荐）** | 服务第一次启动时在 runtime root 生成令牌文件（0600）；本机浏览器加载 dashboard 页面时，服务在响应里设 HttpOnly、SameSite=Strict 的 cookie；CLI 读文件；其他浏览器用 `looper auth set-password` 设置的账号和密码登录 | 本机是 | 是：要么读用户的令牌文件，要么伪造浏览器的请求头，都是有意为之 | 服务端管理密码和会话；前端不改 |
| B. 只用带令牌的登录链接（Jupyter 式，未采用） | 每个新的浏览器会话先打开含令牌的 URL | 否：书签和聊天里的链接打开就是 401，升级后已打开的页面也失效 | 是，且不能靠伪造请求头绕过 | 每次都要先拿到链接；聊天消息里不能放令牌 |
| C. 写接口只认浏览器的同源请求 | 写请求必须带等于 `Host` 的 `Origin`（或 `Sec-Fetch-Site: same-origin`） | 是 | 否：curl 加一个请求头就过；读接口照旧敞开 | 最小，但几乎不设防 |
| D. 按连接查发起进程 | 用 `lsof` 等查出本机连接的 PID，是 agent 进程树里的就拒绝 | 是 | 部分：`setsid`、两次 fork 就脱离进程树 | 依赖平台、每个请求多一次进程查询 |
| E. 系统层面隔开 agent | agent 用另一个系统用户或沙箱跑，读不到用户的文件 | 是 | 是，真正的边界 | 改动运行时的启动方式和文件权限，超出本 PR |

选 A：它是唯一既不改用户的使用方式（书签、聊天链接、升级后不用重新登录）、又让 agent 不能「顺手」以用户身份调用的
做法。连不上自动登录的浏览器改用账号和密码（第 4.4 节），用户凭据不进 URL。E 记为待办（第 8 节）。

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

`operator_auth.Access` 四类，先匹配登录路径，再按路径前缀（API 的命名空间）分，分发也按同一个分类走：

| 类别 | 路径 | 认什么 |
|---|---|---|
| `SIGN_IN` | 精确匹配 `/login`、`/logout` | 登录和退出（4.4）；不先要求用户凭据 |
| `DASHBOARD` | 其余 `/api/` 以外的路径：构建好的 dashboard 文件 | 静态资源公开；页面按 4.3、4.4 自动登录或显示登录页 |
| `AGENT` | `/api/agent/v1/`，包括 `OPTIONS` | 各 agent 自己的令牌，由 `ops.service`、`ops.planner`、`ops.room` 检查；用户令牌开不了 |
| `OPERATOR` | `/api/` 下其余全部，包括未知路径和 `OPTIONS` | 用户令牌：`Authorization: Bearer <token>`、本服务端口的令牌 cookie，或有效会话 cookie |

`/api/` 下没有不认凭据的路由：`/api/status` 也要凭据（它的唯一非浏览器调用方
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

### 4.4 账号和密码

打不开自动登录的浏览器（另一台机器、经反向代理、不发 `Sec-Fetch-*` 的旧浏览器）用服务所在机器上
`looper auth set-password [--user NAME] [--runtime-root DIR]` 设置的账号和密码登录。账号默认 `admin`，
只接受 `[A-Za-z0-9._-]{1,64}`；密码至少 8 个字符，只用两次无回显提示读取，不接受命令行参数，两次不同就不改文件。
runtime root 默认与 `looper serve` 相同。`looper auth clear-password [--runtime-root DIR]` 删除密码文件。

密码保存在 `<runtime root>/operator-password`，与令牌共用安全目录和文件检查：目录属于当前用户且组和其他用户不可写；
文件是当前用户的普通文件，权限正好 0600，最多 4096 字节，用 `O_NOFOLLOW | O_NONBLOCK` 打开后在同一描述符上检查和读取。
设置密码先写同目录的 0600 临时文件，`fsync` 后用 `os.replace` 原子替换；不安全或损坏的已有文件不会用于验证。
文件为一行 JSON，字段为 `user`、`scheme: "scrypt"`、`n: 32768`、`r: 8`、`p: 1`、`salt` 和 `hash`；
后两项是 URL-safe base64，每次设置生成 16 字节新盐，以 `hashlib.scrypt` 计算 32 字节摘要，`maxmem` 为 64 MiB。
读取时验证 scheme、字段和 base64，`n` 必须是 `[2**14, 2**20]` 内的二次幂，`r`、`p` 在 `[1, 32]` 内。
验证时即使账号不符也计算 scrypt，账号和摘要都用 `hmac.compare_digest` 比较。

- **页面：** 未登录且不符合本机自动登录条件的 dashboard 页面请求回答 200 登录页；静态资源仍公开。
  `GET /login` 也显示登录页，已登录或满足自动登录条件时 303 到 `/`。登录页是无脚本、自带明暗配色 CSS 的 HTML，
  表单向 `/login` 提交 `username` 和 `password`，账号预填 `admin`，密码使用 `type=password`、`autocomplete=current-password`。
  没有密码文件时不显示表单，提示在服务所在机器运行 `looper auth set-password` 或在那台机器打开 dashboard。
- **登录：** `POST /login` 只接受最多 4096 字节的 `application/x-www-form-urlencoded` 正文；过大回答 413。
  必须带 netloc 等于 `Host` 的 `Origin`，缺少或不符回答 403。没有密码回答 401；文件不安全或损坏时记服务端错误，
  回答 503 并提示查看服务日志。账号或密码错误都回答 401 和相同的错误文本；成功创建会话，303 到 `/`。
- **会话：** `secrets.token_urlsafe(32)` 生成会话 ID，cookie 为
  `looper_session_<端口>=<id>; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800`；仅 `dashboard_url`
  以 `https://` 开头时加 `Secure`。值不是用户令牌。会话只存在内存里，有效期 7 天，最多 100 个；先清过期项，再淘汰最早的。
  每次验证与当前密码摘要比较，设置或清除密码以及服务重启都会结束全部会话。密码文件按 `stat` 的 inode、大小、
  `mtime_ns`、mode、uid 缓存，只有这些值改变才重新安全读取。
- **限速：** 在计算 scrypt 前按 TCP 对端检查；同一对端连续失败 5 次后锁 60 秒，再失败锁定时间翻倍，最多 900 秒；
  成功清掉该对端的失败记录。全局滑动 600 秒内超过 30 次失败时，所有对端都锁定，直到窗口内失败数减少到不超过 30。
  被锁的尝试回答 429、`Retry-After` 秒数和等待提示，不再次计数；代理后的浏览器共用代理地址的额度，
  `X-Forwarded-For` 和 `Forwarded` 不参与计数。登录尝试逐个处理（检查、scrypt、记录），并发请求不能在第一次失败
  记下前一起通过检查，同一时刻也只有一个 scrypt 占用内存。
- **退出：** `POST /logout` 同样必须带同源 `Origin`，结束 cookie 中的会话，303 到 `/login`，同时清除会话和令牌
  cookie（`Path=/; Max-Age=0; HttpOnly; SameSite=Strict`）。`/login` 只接受 GET、POST，`/logout` 只接受 POST；
  其余方法回答 405。
- **响应和日志：** 每个登录页及 `/login`、`/logout` 的所有响应均带 `Cache-Control: no-store`、
  `Referrer-Policy: no-referrer` 和
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'`。
  错误文本 HTML 转义。登录日志只写对端和 `failed`、`succeeded`、`locked`，绝不记录正文、账号或密码。

不提供登录链接或单次登录码，`code`、`token` 查询参数均忽略；未设置密码时只有本机自动登录可用。

### 4.5 调用方的改动

- `looper service`：`unit_settings` 多读出单元的 `--runtime-root`；`_get_status(port, token)` 带上令牌。等服务起来时
  每次轮询都重新读文件（升级后第一次启动才生成）。401 也算服务在回答：状态行显示 `unauthorized`，说明令牌文件里的
  令牌被拒。
- 评测 runner：`Api` 带一个令牌；`running_server` 轮询时读 runtime root 下的令牌文件，读到并且服务回答才算起来。
- 测试：走真实连接的夹具给服务一个令牌并带上它。`AgentHTTPServer(operator_token=None)` 且没有有效会话时用户接口全部 401，
  漏传凭据的调用方会马上失败，而不是悄悄敞开。
- CLI：新增 `looper auth set-password`、`looper auth clear-password`；其他浏览器通过 `/login` 登录，前端 API 调用仍由浏览器自动带 cookie。

### 4.6 迁移

新版本第一次启动生成令牌文件；`looper service restart` 从单元的 runtime root 读到它；目录或已有文件的属主、权限不安全时，
先按启动提示修复。已经打开的 dashboard 页面下一次请求会 401，本机重新加载一次就自动登录；其他浏览器改用
`looper auth set-password` 设置的账号和密码。旧版本的 CLI 对新服务调用 `service status` 会显示没有回答，升级 CLI 即可
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

- 每类路由（`DASHBOARD`、`SIGN_IN`、`AGENT`、`OPERATOR`）各在真实连接上测：不带凭据、错的令牌、对的令牌（Bearer 和 cookie）、
  agent 的 MCP 令牌；另一个端口名字的 cookie 不算数；未知的用户 API 路径没有凭据时 401；agent 和用户 API 的
  `OPTIONS` 没有凭据时 401，带正确凭据时 404；配置导入的上传在读请求体之前就被拒。
- `tests/test_operator_token_launch_db.py` 在设置 `LOOPER_TEST_DATABASE_URL` 时，用新数据库、临时 runtime root 和
  一次性端口运行真实 eval runner 的 fake 模式，捕获 Manager、Planner、Worker 和 Reviewer 的启动：完整环境、argv、
  cwd、转发的 stdin，以及 prompt、MCP、settings 文件和插件目录里的文本。断言这些输入都没有用户令牌，环境变量名也
  没有 `OPERATOR_TOKEN`；临时让 `serve` 导出令牌时应失败。没有测试数据库时跳过，不算验证通过。
- cookie：自动登录的响应带 `HttpOnly`、`SameSite=Strict`、`Path=/`，没有过期时间，页面 `no-store`；缺 `Sec-Fetch-*`、
  iframe、脚本请求、带 `X-Forwarded-For` 或 `Forwarded`、非 loopback 的对端或 `Host` 都不设；`code`、`token` 参数不登录。
- 登录页：非自动登录的页面返回表单或未设密码提示；资源公开；登录和退出所有响应具备 4.4 的三个安全响应头。
  登录成功 303，cookie 属性齐全且值不是令牌；HTTP 不带 `Secure`，HTTPS 带；错账号和错密码的 401 文本相同。
  缺失或外来 `Origin` 的登录、退出拒绝，超过 4096 字节的正文 413；日志不含账号、密码或正文；不支持的方法 405。
- 密码文件：0600、摘要不含明文、验证账号和密码、新盐、清除，以及 0644、符号链接、FIFO、损坏记录的拒绝；CLI 的匹配、
  不匹配、过短、非法账号、默认和自选账号、清除及无密码参数。会话过期、设置及清除密码后均失效，最多保留 100 个。
- 限速：5 次失败锁 60 秒，锁定时不调用 scrypt；超时后成功重置，后续失败翻倍至 900 秒；31 个不同对端失败后锁住新对端。
  退出结束会话并清两个 cookie。
- 令牌文件：缺少的 runtime root 创建为 0700，组或其他用户可写、属主不符的目录拒绝启动；文件首次生成且为 0600，
  重启读到同一个。0644 文件拒绝启动且权限不变，符号链接和目录拒绝；检查后路径被换成符号链接，不能读到替换后的令牌。
- `looper service`：状态查询带上单元 runtime root 里的令牌，401 显示 `unauthorized`。
- 变异验证：故意改坏检查（分类把 `/api/` 当成 `DASHBOARD`、跳过凭据比较、cookie 不带 `HttpOnly`），对应测试应失败。
- 真实路径的验证目标：一次性端口上的 `looper serve`（新数据库、临时 runtime root、替身 Claude），无头 Chrome 打开
  dashboard 不登录就能看到数据；`looper service` 的状态查询带令牌能读到；用一次替身模式的评测（`run --mode fake`）
  检查 runner 的调用。不改模型可见的内容，不跑真实评测；环境不可用的项目单独报告，跳过不算通过。

## 7. 决定

- **D1 方案 A**，理由见第 3 节。
- **D2 `/api/` 下没有不认凭据的路由**，连 `/api/status` 也要令牌：它的唯一非浏览器调用方读得到令牌；不为不存在的
  监控方另开健康检查路由。
- **D3 本机自动登录的 cookie 值就是令牌本身**：换一个派生值不减少它能做的事；令牌文件不变，服务重启后本机已登录的页面继续可用。密码登录使用独立会话 cookie，重启后失效。
- **D4 不提供登录链接或单次登录码**（维护者决定，2026-10-18）：其他浏览器用 `looper auth set-password` 设置的账号和密码登录（4.4）；未设置密码时只有本机自动登录可用。
- **D5 不给 Claude Code 加读令牌文件的拒绝规则**：Bash 的 `cat` 照样读得到，规则还会把路径写进每个 agent 的命令行
  （运行记录和 `ps` 都看得到）。

## 8. 待办

- agent 的环境里去掉 `LOOPER_DATABASE_URL`，或给 agent 一个不能写数据库的环境（第 5 节）。
- 系统层面隔开 agent 和用户（第 3 节方案 E）。
- 非核心：dashboard 收到 401 时显示一页说明（重新加载并按需登录），而不是各处的「status 401」。
