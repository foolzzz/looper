# RFC：操作浏览器和本机桌面 v0

- **状态：** 决定已定，只有文档。第 3 节汇总三种模式 A、B、C 的决定；D17–D27 对应第 13 节的问题。模式 B、C 统一经过每台电脑唯一、独立于 `looper serve` 的 GUI LaunchAgent 桌面服务；任务 Manager 在 `direct` 模式下可用，转后台自动收回；模式 C 默认不抢焦点。驱动已选窄幅复用 `anionex/dsh-computer-use` 的 Swift 源码，改为一个稳定签名身份的常驻原生服务，不引入 DSH/Cordis 运行时；Looper 自写 helper 为后备（第 4.2、4.5、11 节）。权限归属（TCC 实际记给哪个进程）待第 11 节第 5 项实测确认，其余验证仍是开工前要做的事（PR0）。本文合入后按第 10 节的 PR 顺序实现，排在其他项之后。
- **需求：** 让默认 Manager，以及操作者本人建的任务里的任务 Manager、Planner 和 Worker，能操作浏览器：看内部平台、核对页面、做点选操作。本机桌面作为第二阶段。
  - **三种模式：** A，独立的浏览器配置（默认，无人值守也能用）；B，操作者自己的 Chrome（只在操作者在场时用）；C，本机桌面（第二阶段，只在操作者在场时用）。
  - **每种模式要说清：** 谁能用、怎么隔离、动作怎么过对外审查、截图和证据、花费和时间上限、失败类型。
- **相关：** [对外动作审查 RFC](outbound-review-v0.zh-CN.md)（`ActionLevel`、审查分类器、不可信内容围栏）；可执行验收 RFC（#1520，评审中：证据只由代码写、`artifact` 检查、证据目录）；[引擎可靠性 RFC](engine-reliability-v0.zh-CN.md)（失败分型）；平台子 agent RFC（#1522，评审中：子 agent 的种类和运行树）；[用户关卡 RFC](user-gate-v0.zh-CN.md)；[Agent 系统架构](../agent-system.md)。
- **核对基线：** `main` `1f2ac843e`。第 2 节读代码得出，每项标了出处。
- **术语：**
  - **浏览器会话：** 一次运行（或一个 Manager 回合）使用浏览器的那一段，`browser_sessions` 的一行。
  - **动作：** 一次浏览器（或桌面）工具调用，`browser_actions` 的一行。
  - **读动作 / 写动作：** 不改变页面或应用状态的，和会改变的（第 4.6 节）。
  - **在场：** 操作者本人正在和默认 Manager 对话（Dashboard 网页聊天，或私聊），或正在任务对话里和 `direct` 模式的任务 Manager 对话；由代码按回合的类型化身份和模式字段判断（`operator_identity.is_operator`），模型不能自称。
  - **桌面服务：** 每台电脑唯一的机器级常驻原生服务，作为操作者 GUI 登录会话里的独立 LaunchAgent 运行；模式 B、C 的权限、动作和记录都由它管理，模式 A 不受它约束。

## 1. 背景与目标

- **今天的缺口：** Looper 的 agent 要看一个内部平台上的页面，只能靠运行时自带的网页工具（拿不到登录态），或者操作者手动截图贴进来。
- **原样交出浏览器自动化工具有三个风险：**
  - **凭据：** 浏览器的登录态就是操作者的身份；能执行页面 JS 的工具（例如 `evaluate`）能直接读到 cookie；
  - **注入：** 页面内容是不可信输入，里面写的「指令」可能被模型照做；
  - **对外：** 点击、提交、发送都是以操作者的身份对外的动作。

**目标：**

- 一层类型化的浏览器工具（打开、跳转、读页面结构、截图、点击、输入……），按入口决定有没有，按动作分级处理；
- 模式 A 无人值守也能安全使用：独立的浏览器配置，agent 读不到它的凭据；
- 模式 B、C 只在操作者在场时使用，有明确的开启、指示和自动断开；
- 每个动作都有审计和截图；写动作过对外审查和确认；
- 浏览器动作留下的截图能作为验收证据；
- 每项默认关闭；关闭时提示词、工具列表、命令行和行为逐字节不变。

**不在目标内：**

- 无人值守的沙箱桌面（虚拟机，操作者经网页观看和接管）。它以后作为另一个后端，和模式 C 共用动作格式（D11）；
- 绕过网站的人机验证、批量抓取；
- 以操作者的身份自动登录或保存密码（D5）。

## 2. 现状（读代码得出）

| 方面 | 今天 | 出处 |
|---|---|---|
| 浏览器工具 | Looper 没有。Manager 的查找顺序里，浏览器排在网页之后，等它存在 | `looper/agents/research_order.py` |
| 运行时自带的网页工具 | 有的运行时能搜网页、取网页（没有登录态）；Looper 按启动配置判断有没有（`RuntimeAdapter.web_tools`） | 同上 |
| 能力目录 | 操作者可以把任意 MCP 服务器装给 agent，包括通用的浏览器自动化服务器；那样 agent 就拿到它的全部工具，包括执行页面 JS 的 | 架构文档「The capability catalog」 |
| 对外动作级别 | `ActionLevel`：`inside`、`operator`、`origin`、`machine`、`external`、`unknown`；只有 `external` 和 `unknown` 要审查（`REVIEWED`） | `looper/agents/action_levels.py` |
| 对外审查 | 分类器角色 `outbound_check` 和审查入口 `outbound.review()` 在分批实现，默认关 | `looper/agents/outbound.py`；对外审查 RFC 第 15 节 |
| 不可信内容围栏 | 只在对外审查 RFC 第 8 节里设计了 `fence(text, source=…)`（`Source.WEB` 等）；`main` 上还没有 `looper/agents/untrusted.py` | 对外审查 RFC 第 8 节 |
| 操作者本人的回合 | 类型化判断：网页聊天且没有 API 客户端，或私聊且发送人是该 bot 配置的操作者；工具可以声明 `Audience.OPERATOR`，别的回合拿不到 | `looper/agents/operator_identity.py`；`looper/agents/ops/__init__.py` |
| 确认 | Manager 的高风险调用先返回 `needs_confirmation`，操作者确认后才执行；任务里要人参与靠用户关卡 | `ManagerOps.call`；`looper/agents/user_gates.py` |
| 任务的来源 | `tasks` 上有 `schedule_id`、`chat_kind`、`chat_group`、`client_id`、`origin_channel_id`；没有一个字段直接说「这个任务是不是操作者本人发起的」 | `looper/db/schema.sql` |
| Worker 的权限 | Worker 有全部工具、跳过权限确认，包括 Shell。它能读运行时根目录下的文件：这一点是从「跳过权限确认、有 Shell」推出来的，不是哪一条规则写明的 | 架构文档「What an agent may do」 |
| 证据 | 可执行验收 RFC 设计了 `artifact` 检查和只由代码写的证据目录；浏览器自动化本身不在其范围内 | #1520 |

## 3. 已定的前提

| 编号 | 内容 |
|---|---|
| D1 | **谁能用：** 操作者和默认 Manager 的对话（Dashboard、私聊）；操作者本人建的任务，并且这个项目打开了浏览器开关。任务 Manager 在操作者在场的 `direct` 模式下可用 A、B、C，规则同操作者本人的默认 Manager 回合；转后台自动收回 B、C；后台 Worker 只有 A，Planner、Reviewer 只有 A 的读动作 |
| D2 | **谁不能用：** 任何群聊回合、场景 Manager（即使在私聊里）、群里同事触发的任务、定时任务建的任务、processor。按类型判断：这些入口拿到的工具列表里根本没有浏览器工具 |
| D3 | **域名白名单：** 默认只开操作者列出的内部域名和文档平台；外网要操作者在设置里单独打开 |
| D4 | 以操作者的身份做的提交、发送、删除、审批，每次都要操作者确认 |
| D5 | **模式 A 的登录：** 操作者在 Dashboard 点「登录浏览器」，本机弹出一个可见窗口，操作者自己完成单点登录，之后转到后台；不保存密码、不自动登录。被跳到登录页就暂停全部浏览器动作并私聊通知；重新登录之前，浏览器动作直接返回失败，不挂着等 |
| D6 | **模式 A 的凭据隔离：** 配置目录在运行时根目录下的 `browser/`，权限 700；Looper 启动 Worker 时不让它读这个目录（Claude Code 用按路径拒绝的规则），做不到的运行时不开浏览器；不暴露调试端口；不进备份、不进 git；截图和审计里的凭据字段打码 |
| D7 | **类型化工具，不原样交出自动化工具：** Looper 自己包一层浏览器工具；不让网页向 agent 注册工具；不提供任何执行页面 JS 的工具。确实需要时，作为单独的高风险动作再加，每次确认 |
| D8 | **模式 B：** 桌面服务经 Chrome DevTools 协议（CDP）连到操作者正在用的 Chrome，不用浏览器扩展；只在操作者在场时用，无人值守的任务只能用模式 A；每次由操作者开口开启，30 分钟没有动作自动断开，使用期间对话里一直显示「正在使用你的浏览器」 |
| D9 | **模式 B 的边界：** 只在自己新开的窗口里操作，不读也不碰已打开的标签页。Looper 的 CDP 代理按白名单放行方法：`Page.navigate`、`Page.captureScreenshot`、`DOM`、`DOMSnapshot`、`Accessibility` 里只读的方法，以及 `Input.dispatch*`。其余一律拒绝，包括 `Runtime.*`、`Network.getCookies`、`Storage.*`、`DOMStorage.*`、`IndexedDB.*`、`Page.addScriptToEvaluateOnNewDocument`。连不上时提示，或改用模式 A，不去改操作者的 Chrome 设置 |
| D10 | **模式 C：** 浏览器的读和写都做完以后再开工；Looper 自己做桌面工具层，和模型无关：先读 macOS 的无障碍树（AX）按元素操作，找不到才退回截图加坐标，而且只对能看图的 Profile 开放。默认把动作投递给目标进程和窗口，不移动系统鼠标、不改变前台 app；驱动做不到时，每次经操作者确认才退回前台模式。浏览器 app 一律不写、不截、不读，网页操作交给 A 或 B |
| D11 | **桌面服务和模式 C 的规则：** 每台电脑只有一个桌面服务，只有它拿辅助功能和录屏权限；所有角色、所有运行时的 B、C 都只调用它。它统一管跨进程的机器级独占锁、app 批准和禁用名单、观察 id 和目标重找、审计和截图、使用指示和一键停止。读权限按会话批准，控制权限按回合批准；终端、IDE、系统设置、密码管理器默认禁用。第二个会话明确返回 `in use by <session>`。在场和开启规则同 B；写后重读，接管时暂停动作和截图。Looper 启动的所有运行时会话关闭各自内置的桌面 / computer use 能力；不协调操作者在 Looper 之外运行的桌面控制工具。模式 A 不受桌面服务约束。沙箱桌面以后作为另一个后端，共用动作格式 |
| D12 | **模式 C 的确认和驱动：** Manager 回合（含 `direct` 模式的任务 Manager）复用 `needs_confirmation`；任务里的其他确认复用用户关卡。已选窄幅复用 [anionex/dsh-computer-use 的原生 Swift 源码](https://github.com/anionex/dsh-computer-use/tree/a60de93b6091d92c9d3fdd4a06a88869795bbfaf/native/macos/Sources/Helper)，在复制的文件中保留 MIT copyright 和 permission notice，改为一个稳定签名身份的常驻原生服务。策略、机器级锁、批准、确认、审计和保留由 Looper 自己实现，不引入 DSH/Cordis 运行时。驱动接口由 Looper 定义且保持可替换；Looper 自写 helper 为后备，不在每个运行时进程里加载桌面插件 |
| D13 | **动作分级：** 读动作是打开、跳转、读页面结构、截图、提取文字、跟随链接；写动作是点击、输入、选择、提交。写动作按策略处理：只读、每次确认、按域名白名单三种 |
| D14 | 页面和 app 界面的内容是不可信输入，进上下文前套围栏；每个动作记审计和截图 |
| D15 | **数据表：** `browser_profiles`（配置目录、登录状态、白名单、写动作策略）；`browser_sessions`（模式、发起人、关联的运行记录、起止时间）；`browser_actions`（序号、动作、URL、目标、风险、决定、截图路径） |
| D16 | **PR 顺序：** 模式 A 只读 → 写动作策略和确认 → 接入任务 → 模式 B → 包成子 agent → 模式 C。桌面服务单独一个 PR，可与模式 A 只读并行，B、C 都依赖它。只读动作不等对外审查，先做（审查异步上线，不阻塞其他功能） |
| D17 | **凭据隔离先用同一个系统用户：** 路径拒绝 + 钥匙串加密 cookie + 只给操作者本人的任务；第 14 节写明剩下的风险。浏览器跑在另一个系统用户或虚拟机里作为后续，和无人值守的沙箱后端一起做（Q1） |
| D18 | Planner 和 Reviewer 只有读动作；写动作只给 Worker 和 Manager（Q2） |
| D19 | `browser_fill` 一张表单只审查、确认一次（Q3） |
| D20 | 白名单内、不带查询参数的读不审查；带参数的跳转按写处理，除非操作者给那个主机打了「带参数的链接也是读」；外网的读要审查（Q4） |
| D21 | 图片只给声明了能看图的 Profile，默认给快照文字（Q5） |
| D22 | 审计截图保留 7 天、总量 2 GB（Q6） |
| D23 | 任务里的确认 1 小时没人回就算拒绝（`expired`），Worker 可以重新请求（Q7） |
| D24 | 调试端点不能只由代理持有（或必须改操作者的 Chrome 设置）时，推迟模式 B，不为 Looper 另开一个 Chrome 配置（Q8） |
| D25 | 模式 C 已选 D12 的原生源码复用方案，作为独立 GUI LaunchAgent 常驻；第 11 节验证权限归属、稳定签名身份和驱动能力，未通过就不开 C，必要时改用 Looper 自写 helper。原样 helper 只限隔离的第 0 步原型工具，绝不交付（Q9） |
| D26 | 打开浏览器功能时，自检对装给 agent 的通用浏览器自动化 MCP 服务器发 WARN，不拒绝安装（Q10） |
| D27 | 「外网」只看白名单：不在白名单里的主机就是外网，不按 IP 判断（Q11） |

## 4. 设计

### 4.1 工具层（三种模式共用）

模型看到的是同一组类型化工具，后端是哪种模式由会话决定（第 4.2 节）。页面上的元素用工具返回的结构化快照里的引用（`ref`）定位，不接受 CSS 选择器或脚本。

| 工具 | 参数 | 返回 | 读/写 |
|---|---|---|---|
| `browser_open` | `url` | 新标签页的 `tab`、标题、最终 URL | 按第 4.6 节判断 |
| `browser_navigate` | `tab`、`url` | 标题、最终 URL | 按第 4.6 节判断 |
| `browser_snapshot` | `tab`；可选 `max_chars`（最多 20,000，只能调小） | 无障碍树的文字形式：每个可操作元素一个 `ref`、角色、名字、值（密码类字段的值永远打码）；套在围栏里 | 读 |
| `browser_extract_text` | `tab`；可选 `ref` | 正文文字，套在围栏里 | 读 |
| `browser_screenshot` | `tab`；可选 `ref`（只截这个元素） | 截图的 `action_id`；能看图的 Profile 另外拿到图片 | 读 |
| `browser_follow_link` | `tab`、`ref`（必须是链接） | 同 `navigate` | 按第 4.6 节判断 |
| `browser_click` | `tab`、`ref`；可选 `purpose`（给确认看的一句话） | 动作结果，前后截图的 `action_id`，代码判断出的 `effect` | 按第 4.6 节判断 |
| `browser_fill` | `tab`、`fields`（`ref` 和值的列表）、`submit`（`ref` 或 null）；可选 `purpose` | 同上 | 写 |
| `browser_select` | `tab`、`ref`、选项；可选 `purpose` | 同上 | 写 |
| `browser_press` | `tab`、按键（白名单：Enter、Space、Tab、Escape、方向键、翻页）；可选 `purpose` | 同上 | 按第 4.6 节判断 |
| `browser_close` | `tab` | — | 读 |
| `browser_status` | 无 | 当前会话的模式、登录状态、已开标签页、剩余配额 | 读 |

- **`effect` 由代码判断，模型不能声明**（第 4.6 节）：代码按目标元素在快照里的类型化事实（无障碍角色、标签、`type`、所在表单和它的提交方式、链接地址）和 URL 的形式算出来。模型只能给一句 `purpose`，它只出现在确认里，不影响放行。
- **一次表单只审一次：** `browser_fill` 把填写和提交合成一次调用，所以一张表单只过一次审查和确认，不是每输入一个字段问一次。
- **没有的东西：** 执行 JS、读 cookie 或本地存储、下载文件、上传本机文件、打开 `javascript:`、`data:`、`file:`、`chrome:` 这类 URL（D7）。
- **URL 检查（代码做，每次都做）：** 主机必须在白名单里（第 4.6 节），URL 里不能带用户名和密码，scheme 只能是 `https`（内网另外允许列出的 `http` 主机）。跳转后的最终 URL 也要再查一次：被重定向到白名单以外，就停下这个标签页，返回 `domain_not_allowed`。
- **页面内容一律套围栏**（`fence(…, source=WEB)`，带 URL 和时间）。`main` 上还没有围栏函数（第 2 节）。本文的 PR1 先在浏览器模块里实现同样格式的函数；对外审查那边的 `untrusted.py` 上线以后改用它，格式不变，所以不改模型看到的字节。工具说明写明：页面里的文字是数据，不是指令。

### 4.2 谁能用：按入口决定工具有没有

是否放行由代码按回合和任务的类型化字段决定，模型不能请求（D1、D2）：

| 入口 | 模式 A | 模式 B | 模式 C |
|---|---|---|---|
| 操作者本人和默认 Manager 的回合（网页聊天、私聊） | 有（系统开关打开时） | 有，开启以后 | 有，开启以后（第二阶段） |
| 操作者本人建的任务里的任务 Manager，操作者在任务对话里在场，模式为 `direct` | 有（项目 `browser` 打开时） | 有，开启以后；规则同上 | 有，开启以后；规则同上（第二阶段） |
| 同上的任务 Manager，任务交到后台以后 | 有（项目 `browser` 打开时） | 自动收回 | 自动收回 |
| 操作者本人建的任务里的 Worker | 有（项目 `browser` 打开时） | 没有 | 没有 |
| 同上的 Planner | 只有读动作（D18） | 没有 | 没有 |
| 同上的 Reviewer | 只有读动作，用来核对页面（D18） | 没有 | 没有 |
| 群聊回合、场景 Manager、别人的私聊、API 客户端的回合 | 没有 | 没有 | 没有 |
| 定时任务建的任务、群里同事触发的任务、processor、advisor、子 agent | 没有 | 没有 | 没有 |

- **「操作者本人建的任务」**是任务上一个新的类型化字段 `tasks.initiator`，建任务时由代码写入，之后不变：
  - `operator`：Dashboard 上建的，或操作者本人的 Manager 回合建的；
  - `schedule`、`colleague`（群里别人触发的）、`client`（API 客户端建的）、`manager`（Manager 自己在非操作者回合里建的）。

  只有 `operator` 的任务能用浏览器。字段加之前就有的任务一律当作不是 `operator`。
- **Manager 的工具**声明 `Audience.OPERATOR`，只给默认 Manager 的操作者回合，以及表中满足条件的任务 Manager 回合；任务 Manager 的 B、C 另外检查类型化的 `direct` 模式和操作者在场。别的回合在工具列表里根本看不到这些工具；有人硬调用，返回 `forbidden`。
- **任务里的工具**经 `run.` 令牌提供，和办公台工具同一个入口（`looper mcp-server`）。令牌签发时代码检查上面的条件，不满足就不发浏览器工具。
- **交到后台就收回：** 任务 Manager 离开 `direct` 模式时，代码自动撤回 B、C 的工具和授权、关闭连接并作废待执行的动作；每次调用也重查模式和在场，旧令牌不能沿用。后台 Worker 仍然只有 A，不继承任务 Manager 的 B、C 权限。
- **统一入口：** 默认 Manager、任务 Manager、Worker 等所有角色，不论运行时，使用 B、C 都只能调用桌面服务（第 4.5 节）；这不扩大上表的准入范围。任何工具参数都不能改变准入、批准、禁用名单、写动作策略或确认规则。
- **运行时接线：** 已有 MCP 的运行时复用 `looper mcp-server` 平台工具，再由服务端调用桌面服务；DSH、Pi 的默认 SDK runner 和 API 运行时复用同一平台 handler 的宿主工具桥，不在 runner 里创建驱动。截图必须以类型化图片或受控 artifact 回传，并检查 Profile 能否看图；文字里的路径或 base64 不算视觉路径接通。工具桥可达不等于有 B、C 权限；无法验证接线或关闭自带桌面能力的运行时不开放 B、C（第 11 节第 8 项）。

### 4.3 模式 A：独立的浏览器配置

- **进程：** `looper serve` 管一个后台 Chromium，用浏览器自动化库通过管道（pipe）驱动，不开远程调试端口（D6）。配置目录是运行时根目录下的 `browser/profile-a/`，权限 700。
  - 同一个配置只能有一个浏览器进程，所以所有使用者共用一个持久的浏览器上下文，各自开自己的标签页；
  - 同时打开的标签页有上限（第 4.8 节）；一个会话只能操作自己开的标签页。
- **登录（D5）：** Dashboard 的「登录浏览器」（`browser.login_start`）在本机弹一个可见窗口，打开操作者在设置里填的登录页；操作者登录完点「完成」，窗口关掉，转成后台运行。Looper 不碰登录表单，也不保存密码。
- **登录过期：** 每次跳转后，代码检查最终 URL 是不是设置里填的登录页主机（类型化的 URL 匹配，不看页面文字）。是的话：
  - 配置的状态改成 `login_required`，全部浏览器动作立即返回 `login_expired`；
  - 私聊通知操作者一次，走用户关卡和告警的同一条路径。
- **凭据隔离（D6）：**
  - **路径拒绝：** Worker、Reviewer、Planner 的 Claude Code 启动参数里加按路径拒绝的读规则，覆盖这个目录。做不到路径拒绝的运行时，这个任务里不发浏览器工具；
  - **cookie 加密：** Chromium 启动时不用「模拟钥匙串」，cookie 用 macOS 钥匙串里的密钥加密。光拷走配置目录也解不开（需要验证，第 11 节）；
  - **不进备份和 git：** 这个目录不进 `looper` 的数据库备份，`.gitignore` 也覆盖它；
  - **剩下的风险：** 同一个系统用户下的进程仍然能有意读这个目录（例如用 Shell）。这和 agent 能读运行时根目录下的令牌文件是同一类问题，见第 14 节和 D17。
- **页面不能给 agent 注册工具：** 关掉浏览器的「网页向 agent 暴露工具」类功能（具体开关需要验证，第 11 节）。Looper 的工具列表是固定的，不随页面变化。

### 4.4 模式 B：操作者自己的 Chrome

- **开启（D8）：**
  - 操作者在和默认 Manager 或 `direct` 模式的任务 Manager 的对话里说要用自己的浏览器。Manager 调用 `browser_use_my_chrome`，代码检查在场以后，返回一个确认（`needs_confirmation`），操作者确认后才连接；
  - 连接成功后，对话顶部和每条回复都显示「正在使用你的浏览器」；
  - 连续 30 分钟没有动作，或操作者说「停」（`browser_release`），就断开。
- **连接：** agent 的工具只调用桌面服务，由服务内的 CDP 代理连接操作者 Chrome 的 DevTools 端点，并服从第 4.5 节的机器级独占：
  - **新窗口：** 代理用 `Target.createTarget` 开一个新窗口，只记它自己开的 target，之后只放行发给这些 target 的命令；
  - **别的标签页：** `Target.getTargets` 的结果过滤掉不是自己开的，`Target.attachToTarget` 只能挂到自己开的；
  - **方法白名单（D9）：** `Page.navigate`（URL 照第 4.1 节检查，拒绝 `javascript:` 等 scheme）、`Page.captureScreenshot`、`DOM` 和 `DOMSnapshot` 和 `Accessibility` 的只读方法、`Input.dispatchMouseEvent`、`Input.dispatchKeyEvent`、`Input.insertText`，以及管理自己 target 的 `Target.createTarget`、`Target.closeTarget`、`Target.attachToTarget`。其余一律拒绝，拒绝记审计；
  - **事件：** 只把自己 target 的事件转给 Looper，别的标签页的事件丢掉。
- **端点只能由代理持有：** 白名单只约束经过代理的调用。如果调试端点能被本机其他进程连上，有 Shell、跳过权限确认的 Worker 就能绕过代理直接连，读 cookie、挂到已打开的标签页上。所以只有满足下面一条时才开模式 B：
  - 端点不是一个本机进程都能连的监听端口；
  - 或者 Chrome 对每一个新的调试连接都要操作者在 Chrome 自己的界面里批准，而操作者只批准过代理这一条连接。代理在整个会话里一直持有它；断开以后再连要重新批准。

  两条都做不到（例如只能用一个开着的监听端口），就**不开模式 B**，只给模式 A。这件事在第 11 节第 1 项实测；结论写回本文，并且作为模式 B 启动时的一项检查。
- **连不上：** 提示操作者，或改用模式 A。较新的 Chrome 对默认配置的远程调试有限制，可行的连接方式要先验证（第 11 节）；验证不过就推迟模式 B（D24）。Looper 不改 Chrome 的设置、启动参数或配置目录。
- **白名单、写动作确认、截图审计**的规则和模式 A 一样，由桌面服务执行。`direct` 模式的任务 Manager 按第 4.2 节使用；任务交到后台就断开，无人值守时没有模式 B。

### 4.5 模式 C：本机桌面（第二阶段）

- **机器级桌面服务（B、C 共用，D11）：** 每台电脑只有一个常驻原生桌面服务，所有 Looper 进程共用。它独立于 `looper serve`，作为操作者 GUI/Aqua 登录会话里的 LaunchAgent，以固定安装位置、bundle id 和稳定签名身份运行；不是 root LaunchDaemon，也不是每个运行时的 sidecar。设计上只有这个进程访问 AX、投递系统输入和捕获屏幕，Manager、任务 Manager、Worker、MCP 桥及 `looper serve` 都不申请辅助功能和录屏权限；原生驱动不能另开持权子进程。**权限归属（TCC 实际记给哪个进程）待第 11 节第 5 项实测确认。** 模式 A 的独立浏览器配置不依赖它。
  - **服务管规则：** 机器级独占锁、app 批准和禁用名单、观察 id 和目标重找、审计行和截图、使用指示和一键停止都在服务里；调用方只拿当前会话的类型化工具，不能用工具参数修改规则或绕过检查。
  - **调用边界：** 通过有版本、帧大小上限和超时的本地认证 RPC 调用，校验调用进程身份和 Looper 签发的短期会话 / 动作许可及单次 nonce；同 UID 或模型自报身份不算批准。原 helper 的父子管道检查只是防误用，不能代替调用者认证。存储可复用 Looper 的设施，执行前最终授权校验和审计写入仍由桌面服务负责。
  - **运行时边界：** Looper 启动的所有运行时会话都关闭运行时自带的桌面 / computer use 能力，工具列表里不能出现这些内置工具；运行时之间不各管一份桌面权限。操作者在 Looper 之外运行的其他桌面控制工具不在协调范围内。
  - **指示和停止：** 同一个原生进程持续显示「Looper 正在使用你的桌面」，并提供一键停止；停止入口不排在普通动作队列后，立即暂停动作和截图、作废待执行的动作并断开会话，不等模型回复。已按下的按键或鼠标先安全释放；不另起持权 UI 或 cursor 进程。对话里的浏览器 / 桌面指示照常保留。
- **工具层（D10）：** 桌面工具和浏览器工具同一种形状：
  - **读：** `desktop_snapshot`（已批准的目标 app 和窗口的无障碍树，带观察 id 和 `ref`）、`desktop_screenshot`（同一目标，带观察 id）；
  - **写：** `desktop_click`、`desktop_type`、`desktop_press`、`desktop_select`；`effect` 由代码按 AX 角色判断，规则和第 4.6 节相同；
  - **观察和重找：** 观察 id 由服务生成，绑定会话、目标进程、窗口和那次快照；写动作必须引用它。执行前由服务在同一目标里按结构化事实重找元素；观察过期、窗口或界面已变、目标消失或不唯一就拒绝，要求重新观察并按第 4.6 节重新确认，不能沿用旧坐标或猜一个相近目标；
  - **退回坐标：** 找不到元素时可以按坐标点击，只对能看图的 Profile 开放，每次都要确认；命中测试落在禁用名单或浏览器名单里的窗口上就拒绝。
- **默认不抢焦点（D10）：** 动作直接投递给目标进程和窗口，不移动系统鼠标，也不改变前台 app，操作者可以继续工作。驱动对某个动作做不到时，返回需要前台模式，由操作者每次明确确认后才执行；不能自动切前台或把一次确认当作后续动作的许可。前台模式仍按下面的接管规则暂停。
- **规则（D11）：**
  - **开启：** 在场时由操作者开启（`desktop_use`）；连续 30 分钟没有动作，或操作者说停，就断开；使用期间显示「正在使用你的桌面」；
  - **app 批准：** 服务在每个 app 第一次读取前向操作者申请本次会话的读权限；控制权限每个回合单独批准，不能跨回合沿用，写动作仍按第 4.6 节确认。终端、IDE、系统设置、密码管理器在服务的禁用名单里，批准也不行（名单只能由操作者在设置里改，不能由工具参数改）；
  - **独占：** B、C 共用服务持有的机器级独占锁，跨 Looper 进程、角色、运行时和 runtime root 生效，同一时间只有一个会话、一个 agent 能使用。锁覆盖观察、截图、输入和写后核对；第二个会话明确拒绝并返回 `in use by <session>`，不排队执行，也不抢占。服务记录 owner 和租约，调用方退出、失联、会话结束或点停时撤销租约，待执行动作和确认失效。锁屏、注销或切换 GUI 登录会话时暂停动作和截图；跨用户的活动桌面独占也要验证，不能把 per-app 队列或 per-user 锁当机器锁。服务重启不恢复旧批准和待执行动作；
  - **核对：** 每次写动作之后重读一次无障碍树，确认目标的状态真的变了，否则返回 `no_effect`；
  - **接管：** 操作者在受控窗口输入，或在前台模式下使用鼠标或键盘时（用系统的用户输入事件判断），暂停截图和动作，直到操作者说继续；默认不抢焦点的路径下，操作者在其他窗口正常工作不算接管；
  - **浏览器 app 由类型化的名单判断，按 bundle id。** 名单包括常见的浏览器，以及基于 Chromium 或 WebKit 的浏览器；它内置在代码里，操作者只能加、不能删。对名单里的 app：
    - **不写：** 按元素的写动作拒绝；坐标点击先做命中测试，落在浏览器窗口上的也拒绝（`browser_app`）；
    - **不截：** 截屏时把浏览器的窗口排除掉（屏幕捕获按窗口排除），整屏截图里它们是遮住的；
    - **不读页面：** 不读它们的无障碍树（里面是已登录的页面内容，会绕过模式 A、B 的白名单和围栏），只报告「有一个浏览器窗口」。

    网页上的事一律交给模式 A 或 B（D10）。
- **驱动（D12、D25）：** 已选下面的 (b)，只实现一个后端，不先造通用插件体系。源码核查基线为社区项目 `anionex/dsh-computer-use` `0.3.11`，提交 `a60de93b6091d92c9d3fdd4a06a88869795bbfaf`；不是对其动作效果或安全性的实测背书。

  | 路径 | 决定与取舍 |
  |---|---|
  | (a) 原样 native helper 加 Looper 上层 | 只作第 0 步隔离原型工具，绝不交付。普通请求每次启动一个 one-shot helper，另有 cursor 常驻进程；实际调用系统 API 的进程不是唯一常驻持权进程，上层包装不能修复原生生命周期。其 per-app 串行队列也不是机器锁；改生命周期后即属于 (b) |
  | (b) 窄幅复用 Swift 源码 | **已选。** 复用 AX、窗口定位、坐标变换、定向输入和安全 mouse-up，改为一个稳定签名身份的常驻原生服务。复制文件保留 `Copyright (c) 2026 anionex` 和完整 MIT permission notice（见该提交的 `LICENSE`）；Looper 自建策略、锁、批准、确认、审计、截图脱敏和保留，不带 DSH/Cordis。承担上游小范围补丁维护、私有 ABI 和 macOS 回归成本 |
  | (c) Looper 全新原生 helper | 后备方案，接口更自主、复用更少；定向输入的公共 API 等价路径尚未验证，自己写也不消除系统接口风险，兼容与测试成本更高。关键边界验证失败就保持 C 关闭，必要时转此方案 |

  - **复用边界的源码依据：** [原生调用和进程生命周期](https://github.com/anionex/dsh-computer-use/blob/a60de93b6091d92c9d3fdd4a06a88869795bbfaf/src/providers/native-helper.ts)、[helper 入口和父子 transport 检查](https://github.com/anionex/dsh-computer-use/blob/a60de93b6091d92c9d3fdd4a06a88869795bbfaf/native/macos/Sources/Helper/main.swift)、[service 的按 app 队列](https://github.com/anionex/dsh-computer-use/blob/a60de93b6091d92c9d3fdd4a06a88869795bbfaf/src/service.ts)。普通请求是一问一答 JSON / 标准流，Python 可直接实现，仅供 (a) 原型；`./provider-macos`、`./service` 的 JS 子入口仍依赖 DSH/Cordis 的服务、会话、批准和存储，不直接接入。
  - **不能继承为安全保证：** 原项目确认门依赖调用方的 `sensitive`（该提交 `src/confirmations.ts`、`src/types.ts`），不能代替第 4.6 节的代码分级；`sensitive=false`、`approved=true` 或 `purpose` 都不构成授权。AX SecureTextField value 屏蔽也不等于截图像素已脱敏；截图先脱敏再保存和回传，仍由 Looper 实现。
- **权限自检：** 按第 11 节第 5 项通过的启动链核对服务身份及辅助功能、录屏权限；拿不到、被撤销、服务不可用或归属不符就不开模式 C，在自检里说明，不给通用 Terminal、Node、Python 或各运行时授权来补救，也不静默退回运行时自带的 computer use。

### 4.6 动作分级、对外审查和确认

- **代码判断每个动作的 `effect`**（`ActionEffect`，不由模型声明）：

  | `effect` | 什么动作 | 怎么判断 |
  |---|---|---|
  | `read` | 打开、跳转、跟随链接，地址里没有查询参数；读快照、读正文、截图；Tab、Escape、方向键、翻页 | URL 的形式；工具本身 |
  | `navigate_query` | 打开、跳转、跟随链接，地址里有查询参数 | URL 的形式。GET 也能改状态（「删除」链接、带参数的接口），所以默认按写算 |
  | `input` | 往输入框里填字但不提交（`browser_fill` 不带 `submit`、在不属于任何表单的文本框里按 Enter） | 目标元素的角色和所在表单 |
  | `submit` | 提交表单：`browser_fill` 带 `submit`；点击提交按钮或表单里的按钮；在表单里的输入框按 Enter | 元素的 `type`、所在表单、表单的提交方式 |
  | `activate` | 点击按钮、菜单项、复选框、开关、选项，按 Space 或 Enter 激活它们；`browser_select` | 元素的无障碍角色。页面脚本可能借这些动作发送、删除或审批，代码判断不了，所以和 `submit` 一样对待 |

  点击一个链接按跟随链接的规则判断；点击文本框只是聚焦，算 `read`。快照里拿不到元素事实的（例如元素已经消失），一律按 `activate` 算。
- **按 `effect` 给 `ActionLevel`**（对外审查 RFC 第 4.1 节，由 `level_of` 按参数算）：
  - `read`，主机在白名单里：`inside`，不审查。白名单以外的主机直接拒绝，不进审查；
  - `read`，外网开关打开、主机不在白名单里：`external`，要审查。跳转本身就能经 URL 把数据带出去；
  - `navigate_query`、`input`、`submit`、`activate`：一律 `external`，审查打开时要审查。
- **写动作策略**（每个浏览器配置一份，`browser_profiles.write_policy`，D13）：

  | `effect` | `read_only`（默认） | `confirm_each` | `allowlist` |
  |---|---|---|---|
  | `navigate_query` | 主机标了「带参数的链接也是读」就放行，否则拒绝 | 同左，否则确认 | 同左，否则主机在写动作白名单里就放行，不在就确认 |
  | `input` | 拒绝 | 确认 | 主机在写动作白名单里放行，否则确认 |
  | `submit`、`activate` | 拒绝 | 确认 | **确认**（不论主机） |

  - **主机标记：** 「带参数的链接也是读」是操作者给白名单里某个主机单独打的标记（例如一个只读的文档站或搜索页），默认不打。
  - **提交和激活一律要确认（D4）：** 不论策略和白名单，也不论对外审查开没开。代码分不清一个按钮是在发送、删除还是审批，所以全部按「以操作者的身份做的事」处理。
  - **审查：** 打开时先审查、再确认（对外审查 RFC 的 D7），审查拦了就不问操作者。审查没打开时（D16 允许只读先上线），按上表处理，`submit`、`activate` 照样要确认。

- **确认怎么问：**
  - **Manager 回合：** 返回 `needs_confirmation`。确认文字写明标签页标题和主机、目标元素的角色和名字、要填的字段（密码类打码）、代码判断出的 `effect`、模型给的 `purpose`（标明是模型说的），附一张标出目标元素的截图。确认绑定这些具体参数和页面状态（当前 URL 加快照的哈希）；确认时页面已经变了，就要重新确认。
  - **任务里（Worker）：** 工具返回 `pending_confirmation` 和一个确认 id。Looper 在任务上开一个系统确认，用用户关卡的界面和通知路径，任务显示为「需要你」，内容同上。Worker 可以接着做别的，也可以用 `browser_wait_confirmation` 等，最多 120 秒，然后交一份等待报告。操作者批准后由 Looper 执行这个动作（参数绑定，页面变了就作废），结果推给 Worker 或进它的下一轮。1 小时没人回就算拒绝（D23）。
- **被拦或被拒以后：** 模型拿到类型化的结果：`blocked`（审查拦了）、`declined`（操作者拒绝）、`expired`（没人回）。说明照对外审查 RFC 第 7 节：没有执行，不要换个办法再做。

### 4.7 截图、审计和验收证据

- **每个动作一行 `browser_actions`：** 工具、参数（打码后）、URL、目标（角色和名字）、级别、写动作策略的决定、审查结果、确认 id、结果、耗时。写动作另外存前后两张截图，读动作只有 `browser_screenshot` 存截图。
- **打码（D6）：** 截图前，把密码类输入框和设置里列出的敏感元素遮住（浏览器自动化库的截图遮罩）。审计参数里，填进这些字段的值写成 `[redacted]`。模式 C 由驱动遮住密码类控件，操作者接管时不截图。
- **审计截图的存储：** 放在运行时根目录下的 `browser-audit/<session>/<action>-{before,after}.png`，文件名由代码生成。它们是审计材料，不是验收证据：保留期限是 `browser.retention_days`（默认 7 天），总量上限 2 GB，超过时先删最旧的；会话删除时一起删。截图只经操作者登录的会话读取，响应头和可执行验收 RFC 的证据接口一样。
- **验收证据只走可执行验收（#1520）的路：**
  - **规则：** #1520 的证据由代码写在 `evidence/<task>/<step>/<attempt>/<check>/` 下，检查类型只有 `command` 和 `artifact`，模型不能挑选哪些算证据。本文不改这些规则。
  - **`browser` 检查（提议给 #1520 新增的第三种类型）：** Planner 给步骤写一条 `browser` 检查：一串只读动作（打开、等某个角色和名字的元素出现、读文字、截图）加断言。#1520 的检查执行器在 Worker 交付后用模式 A 跑它，不花模型回合；只允许 `read` 类的动作，`navigate_query` 也只在主机打了标记时允许。执行器把截图复制进这一条检查的证据目录，写一行 `step_check_runs`，它就照 #1520 的规则保留，并参与证据门。这一项要和 #1520 的实现一起改它的类型枚举（PR7）。
  - **Worker 引用的截图不是证据：** Worker 在报告里可以引用自己运行里的 `action_id`。代码核对这些 id 确实属于这一步的这次运行，然后把文件复制进 `evidence/<task>/<step>/<attempt>/references/`。这个目录不属于任何检查，不进证据门；Reviewer 的任务消息里单独列出来，标明「Worker 引用的截图，不是 Looper 量出来的证据」。
  - **保留：** 复制出来的文件随任务删除，不受审计截图的 7 天清理和会话级联删除影响。

### 4.8 花费和时间上限

上限按浏览器会话算（`browser_sessions`）。一个会话是一次 Worker、Reviewer 或 Planner 运行，或者 Manager 的一段使用：从第一个浏览器动作开始，到 10 分钟没有动作或回合所在的会话结束为止。

| 上限 | 默认 | 说明 |
|---|---|---|
| 一次跳转（含等待加载） | 30 秒 | 超时返回 `timeout` |
| 其他单个动作 | 10 秒 | 同上 |
| 一个会话的动作数 | 200 | 超过返回 `limit_actions`，会话关闭 |
| 一个会话的截图数 | 50 | 不含写动作自动存的前后截图 |
| 一个会话的时长 | 60 分钟 | 超过返回 `limit_duration`，会话关闭 |
| 一个会话空闲多久关闭 | 模式 A 10 分钟；模式 B、C 30 分钟 | D8、D11 |
| 同时打开的标签页 | 全部 4 个，一个会话 2 个 | 模式 A 的共享浏览器 |
| 同一主机的跳转频率 | 每秒 1 次 | 对内部平台客气一点 |
| 快照、正文的长度 | 20,000 字，调用方只能调小 | 超出截断并写明 |
| 图片进上下文 | 只给声明了能看图的 Profile，一张最大 1,568 像素边长 | 其他 Profile 只拿到快照文字 |

- **花费：** 浏览器动作本身不花模型的钱。花费来自模型读快照和看图，照旧算在运行（Manager 是回合）里，受运行和任务的上限约束；会话行记下它的动作数、截图数和快照总字数，任务页按会话显示。快照的长度上限和「默认不给图」是主要的省钱手段。
- 每个上限第一期是常量，记在会话行上（达到哪一个、什么时候）。超出时返回类型化的错误码，模型能据此收敛。

### 4.9 失败类型（接引擎可靠性 RFC）

浏览器工具的错误是类型化的结果码。只有浏览器本身出问题的那几种，会进运行的失败分型，而且用浏览器自己的原因（引擎可靠性 RFC 的 `FailureCause` 新增五个值，和它的检查约束一起扩展）。原有的原因各有含义，不借用：`runtime_missing` 是可执行文件或工作目录缺失，`profile` 是 Agent Profile 跑不了，`provider_overloaded` 是模型供应商的 5xx，`connection_failed` 是到模型的连接。

| 结果码 | 含义 | 运行因此失败时 |
|---|---|---|
| `login_expired` | 配置的登录过期（模式 A） | `environment`/`browser_login`（新增） |
| `browser_unavailable` | 浏览器进程起不来或崩了（模式 A 自动重启一次） | `environment`/`browser_down`（新增） |
| `not_connected` | 模式 B 连不上，或已断开 | `environment`/`browser_disconnected`（新增） |
| `timeout` | 动作超时 | `transient`/`browser_timeout`（新增） |
| `site_error` | 页面返回 5xx | `transient`/`site_error`（新增） |
| `domain_not_allowed`、`write_not_allowed`、`browser_app`、`in_use`、`limit_*` | 策略拒绝；`in_use` 明确带上 `in use by <session>` | 不进失败分型 |
| `blocked`、`declined`、`expired` | 审查拦截，操作者拒绝或没回 | 不进失败分型 |
| `element_not_found`、`no_effect` | 找不到元素，或点了没反应 | 不进失败分型 |

- **怎么进失败分型：** 运行的证据（`Evidence`）记下这次运行最后一个**浏览器故障**结果码（只记上表前五种）。运行以失败结束、而且有这一项时，用它的原因；不然照常分类。
- **策略拒绝不进失败分型：** 它们是工具调用的结果，是规则，不是故障。模型拿到以后照常继续；运行之后如果失败了，按它自己的原因分类，不会因为曾经被拒绝而被归到「需要判断」以外的类。
- **登录过期是账号级的状态：** `login_required` 期间，所有浏览器动作立即失败，不排队、不重试，直到操作者重新登录（D5）。

### 4.10 数据模型

```sql
CREATE TABLE IF NOT EXISTS browser_profiles (
    id               TEXT PRIMARY KEY,                       -- 第一期只有一行：模式 A 的配置
    directory        TEXT NOT NULL,                          -- 运行时根目录下的相对路径
    login_state      TEXT NOT NULL CHECK (login_state IN ('never', 'logged_in', 'login_required')),
    login_url        TEXT,
    login_hosts      TEXT[] NOT NULL DEFAULT '{}',           -- 判断「被跳到登录页」的主机
    allowed_hosts    TEXT[] NOT NULL DEFAULT '{}',           -- 读动作的白名单
    write_hosts      TEXT[] NOT NULL DEFAULT '{}',           -- allowlist 策略下写动作免确认的主机
    write_policy     TEXT NOT NULL DEFAULT 'read_only' CHECK (write_policy IN ('read_only', 'confirm_each', 'allowlist')),
    external_web     BOOLEAN NOT NULL DEFAULT false,
    logged_in_at     TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS browser_sessions (
    id               BIGSERIAL PRIMARY KEY,
    mode             TEXT NOT NULL CHECK (mode IN ('profile', 'user_chrome', 'desktop')),
    profile_id       TEXT REFERENCES browser_profiles(id) ON DELETE SET NULL,
    initiator        TEXT NOT NULL CHECK (initiator IN ('manager_turn', 'worker', 'planner', 'reviewer')),
    record_id        BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    task_id          TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    state            TEXT NOT NULL CHECK (state IN ('open', 'closed')),
    end_reason       TEXT CHECK (end_reason IN ('run_ended', 'released', 'idle', 'login_expired', 'operator',
                                                'server', 'error', 'limit_actions', 'limit_duration')),
    actions          INTEGER NOT NULL DEFAULT 0,             -- 第 4.8 节的会话上限按这些计
    screenshots      INTEGER NOT NULL DEFAULT 0,
    snapshot_chars   BIGINT NOT NULL DEFAULT 0,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_action_at   TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ,
    CHECK ((state = 'closed') = (ended_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS browser_actions (
    id               BIGSERIAL PRIMARY KEY,
    session_id       BIGINT NOT NULL REFERENCES browser_sessions(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,
    tool             TEXT NOT NULL,
    arguments        JSONB NOT NULL,                         -- 打码后
    url              TEXT,
    target           JSONB,                                  -- 角色和名字
    kind             TEXT NOT NULL CHECK (kind IN ('read', 'write')),
    effect           TEXT NOT NULL CHECK (effect IN ('read', 'navigate_query', 'input', 'submit', 'activate')),
                                                             -- 代码判断（第 4.6 节）
    purpose          TEXT,                                   -- 模型给确认看的一句话，不影响放行
    level            TEXT NOT NULL,                          -- ActionLevel
    decision         TEXT NOT NULL CHECK (decision IN ('allowed', 'confirmed', 'refused', 'blocked', 'declined',
                                                       'expired', 'pending')),
    review_id        BIGINT,                                 -- outbound_reviews
    confirmation_id  TEXT,
    result           TEXT NOT NULL,                          -- 第 4.9 节的结果码，或 ok
    screenshots      TEXT[] NOT NULL DEFAULT '{}',           -- browser-audit/ 下的相对路径（审计，不是证据）
    referenced_in    TEXT,                                   -- 被 Worker 引用时复制到的 references/ 路径
    duration_ms      INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);
ALTER TABLE tasks ADD COLUMN initiator TEXT
    CHECK (initiator IN ('operator', 'schedule', 'colleague', 'client', 'manager'));   -- 旧任务为 NULL，当作非 operator
-- 失败原因：llm_records_failure_check（failures.check_sql）加 browser_login、browser_down、browser_disconnected
-- （environment）和 browser_timeout、site_error（transient），照引擎可靠性那边的做法 NOT VALID 后校验。
```

- 全部是新增；`tasks` 加的是可空列，不重写，约束直接在列上（新列都是 NULL）。
- app 批准和禁用名单由桌面服务统一管理，在桌面服务 PR 里建立：`desktop_app_grants` 关联会话 id、app 的 bundle id、读权限批准时间，以及控制权限所属的回合；禁用名单独立于会话保存，不能因新会话绕过。审计行和截图也由服务写入，沿用第 4.7 节的记录规则。
- 审计截图随会话、任务删除；保留期限过了由每小时的清理删除。复制进 `references/` 的文件和 `browser` 检查的证据随任务删除，不受这两条影响（第 4.7 节）。

### 4.11 设置（默认关）

| 设置 | 默认 | 管什么 |
|---|---|---|
| 系统 `browser.enabled` | 关 | 总开关：关着时没有任何浏览器工具，不启动浏览器进程 |
| 项目 `browser` | 关 | 这个项目里操作者本人建的任务能不能用（第 4.2 节） |
| 浏览器配置的 `allowed_hosts`、`login_url`、`login_hosts` | 空 | 读动作的白名单和登录页；没配就只有 Manager 的 `browser_status` 能用 |
| `write_policy`、`write_hosts` | `read_only`、空 | 第 4.6 节 |
| `external_web` | 关 | 白名单以外的主机能不能读（读了要审查） |
| 系统 `browser.user_chrome` | 关 | 模式 B 能不能被开启 |
| 系统 `browser.desktop` | 关 | 模式 C（第二阶段） |
| `browser.retention_days` | 7 | 截图和审计的保留期 |

- 都在 Settings 的一个「浏览器」页面里。Manager 的 `looper_settings` 只在 `browser.enabled` 打开以后才显示和修改这一组（改动要确认）；关着时它的输出里没有浏览器这一节。配置导出带上除登录状态以外的字段。
- **关闭时逐字节不变的范围：**
  - **提示词和工具：** 所有 agent 的提示词、工具列表、Worker 的启动参数和现在一样；
  - **命令行：** 浏览器的 `/api/v1` 操作在 `browser.enabled` 关着时不出现在 OpenAPI 文档里（文档生成时按开关过滤），调用返回 404。生成的 `loopercli` 照文档生成命令，所以也没有浏览器命令；
  - **`tasks.initiator`：** 建任务时由代码写入，只给签发令牌时的检查用。它不出现在任务的 JSON、Manager 和 Planner 的提示词、任务报告和配置导出里（不论开关），值为 NULL 时也不会多出一个空字段；
  - **测试：** 每个 PR 一条测试证明以上几点。

## 5. 失败语义（这一层自己出问题时）

| 情况 | 行为 |
|---|---|
| 审计写不进库 | 不执行动作，返回 `audit_unavailable`（没有审计就不做） |
| 截图存不下 | 读动作照常返回，审计行写明没有截图；写动作不执行（没有前后截图就不做） |
| 浏览器进程崩溃 | 打开的会话关掉（`end_reason` `error`），自动重启一次；再崩就进 `browser_unavailable`，并发告警 |
| CDP 代理出错（模式 B） | 断开连接，`not_connected`，并通知操作者；不会退回直连 |
| 确认状态丢失（服务重启） | 待确认的写动作作废（`expired`），模型下次调用时知道 |
| 服务重启 | 模式 A 的浏览器随 `looper serve` 重启，登录态保存在配置目录里；独立桌面 LaunchAgent 不随它重启，但客户端失联即撤销该客户端的 B、C 租约、连接和待执行动作。桌面服务自身重启也不恢复旧批准和确认，B、C 都要操作者重新开启 |

## 6. 指标和告警

- 每天：动作数（按读写、模式）、写动作的决定分布（放行、确认、拒绝、拦截）、确认的等待时间、`login_expired` 的次数和持续时长、每个错误码的次数；
- 对每个用浏览器的任务：动作数、截图数、模型读快照的 token 数；
- 注入评测：页面和 app 界面里藏的诱导文字触发工具调用的次数（第 12 节，必须为 0）；
- 告警，和对外审查同一条通知路径：
  - 登录过期：每次过期一条；
  - 浏览器进程反复崩溃；
  - 模式 B 的 CDP 代理拒绝了白名单以外的方法：每次会话一条，说明模型在试图越界。

## 7. 入口

| 入口 | 变化 |
|---|---|
| Dashboard | Settings 的「浏览器」页（开关、白名单、写动作策略、登录按钮和登录状态）；任务页的浏览器动作列表和截图；Manager 对话里「正在使用你的浏览器」或「桌面」的指示条；确认卡片带截图 |
| 飞书、Telegram | 操作者私聊里的默认 Manager 能用模式 A，开启后能用模式 B（和 C）；写动作的确认走现有的确认路径；登录过期的通知发私聊；群聊里没有任何浏览器工具 |
| CLI | 设置和审计的只读查询经生成的 `loopercli` 命令；登录只能在 Dashboard 做（要本机弹窗） |
| 任务 Manager 的对话 | 操作者在场的 `direct` 模式下按默认 Manager 的规则开启 B、C、显示指示和确认；交到后台自动收回，桌面服务提供一键停止 |
| 任务里的 Planner、Worker、Reviewer | 项目打开、任务由操作者本人发起时，多出浏览器工具和一段工具说明（模型可见，要评测） |

## 8. 安全性检查清单（来自产品设计的验收）

- 不该用浏览器的入口，工具列表里没有浏览器工具（第 4.2 节的每一行一条测试）；
- 读动作不改变页面状态（在一个本地测试站点上验证：表单值、计数器、服务端日志）；
- 每一个写动作都按策略被拦下或要求确认；
- 任何工具参数都不能改变准入、app 批准、禁用名单、写动作策略或确认规则；
- 页面和 app 界面里藏的诱导文字触发不了任何工具调用；每一步交付前都跑注入评测（第 12 节）；
- 登录过期时暂停，并通知操作者；
- Worker 读不到配置目录（各运行时的启动参数快照测试，加一次真实 CLI 尝试读取）；
- 模式 B 读不到 cookie 和本地存储，不碰已打开的标签页，30 分钟没动作自动断开；
- `direct` 模式的任务 Manager 可按相同规则使用 B、C，交到后台立即收回，后台 Worker 仍只有 A；
- 跨进程、角色和运行时请求 B、C，第二个会话明确得到 `in use by <session>`；模式 A 不占这个锁；
- Looper 启动的任何运行时会话都看不到内置桌面 / computer use 工具；系统辅助功能和录屏权限只在桌面服务进程上；
- 模式 C 默认动作前后系统鼠标位置和前台 app 不变；驱动不能不抢焦点时，每次前台动作都要确认，操作者接管就暂停；
- 模式 C 对浏览器类 app 不能点击、不能捕获窗口或读取无障碍树；
- 每一步关闭开关时，提示词、工具列表、命令行和行为逐字节不变；
- 真实路径只用本地测试站点和专用测试 app，不访问真实站点，不操作操作者的真实 app。

## 9. 和其他项的关系

- **对外审查（#1469）：** 本文给浏览器写动作定级别；审查没打开时只按写动作策略处理。只读动作不等审查（D16）。
- **可执行验收（#1520）：** 本文不改它的证据规则。验收证据只来自它的检查执行器：本文提议新增 `browser` 检查类型（PR7，排在 #1520 的实现之后），执行器把截图复制进检查的证据目录并写 `step_check_runs`。浏览器审计截图和 Worker 引用的截图都不是证据（第 4.7 节）。
- **平台子 agent（#1522）：** 子 agent 第一期没有浏览器（那份 RFC 的 D13）。等两边都落地以后，可以加一个只读的 `browser` 子 agent 种类（D16 的「包成子 agent」），只用模式 A 的读动作，跑在派出者的配额里。
- **失败分型（#1514）：** 第 4.9 节的五个浏览器故障码进运行的失败证据，用新增的五个浏览器原因；策略拒绝不进失败分型。

## 10. PR 拆分

| PR | 内容 | 开关 | 依赖 | 模型可见 |
|---|---|---|---|---|
| PR0 开工前验证 | 第 11 节各项的实测结论写回本文；不改代码 | — | — | 否 |
| PR1 模式 A 只读 | 浏览器进程和配置；登录流程和登录过期检测；读工具；URL 和白名单检查；快照和正文的围栏；审计和截图；凭据隔离（路径拒绝、钥匙串）；`browser_profiles`、`browser_sessions`、`browser_actions`；Settings 页；只给操作者本人的 Manager 回合 | `browser.enabled` | — | 是：Manager 的工具，要评测 |
| PR1a 桌面服务 | 先按独立 GUI LaunchAgent 的进程形态设计，每台电脑唯一、固定安装位置和稳定签名身份；权限自检；跨进程的机器级独占锁；app 批准和禁用名单；观察 id 和目标重找；审计行和截图；使用指示和一键停止；关闭各运行时内置桌面能力；先全部用假驱动证明 | `browser.enabled` + B 或 C 的开关 | PR0；可与 PR1 并行，不依赖 PR1 的表先建好 | 是：工具可见性，要评测 |
| PR2 写动作 | 写工具；写动作策略；`effect`；`ActionLevel` 和审查接入；Manager 的确认（绑定参数和页面状态）；任务里的系统确认 | 同上 + `write_policy` | PR1 | 是 |
| PR3 接入任务 | `tasks.initiator`；项目 `browser`；任务 Manager、Worker、Planner、Reviewer 的工具（按第 4.2 节）；`direct` 准入和交到后台收回的检查；工具说明；注入评测 | 项目 `browser` | PR1（写动作要 PR2） | 是 |
| PR4 模式 B | 桌面服务接 CDP 代理和方法白名单；开启、指示、空闲断开；默认 Manager 和 `direct` 模式的任务 Manager 在场时使用，交到后台断开 | `browser.user_chrome` | PR1、PR1a、PR2、PR3，以及 Q8 的验证 | 是 |
| PR5 浏览器子 agent | 只读 `browser` 子 agent | 项目 `subagents` + `browser` | PR3、平台子 agent 的实现 | 是 |
| PR6 模式 C | 将窄幅复用的 Swift 源码接入唯一常驻原生服务，保留 MIT 声明；桌面工具层；默认不抢焦点；每次确认的前台模式和接管暂停；浏览器类 app 的不写、不截、不读 | `browser.desktop` | PR1a、PR2、PR3，以及 Q9、第 11 节的验证 | 是 |
| PR7 `browser` 验收检查 | 可执行验收的新检查类型，用模式 A 跑 | 可执行验收的开关 | PR1、可执行验收的实现 | 是（Planner 的检查说明） |

- 新逻辑放在新模块（`looper/agents/browser/`）；Manager 和 Worker 只接线。
- PR1a 桌面服务先用假驱动证明全部契约，进程形态就是独立 GUI LaunchAgent，不先接真实桌面；记录接口也先用替身验证，和 PR1 的表接线在合流时完成。模式 A 不依赖 PR1a。PR4、PR6 接入真实驱动后再按第 12 节验收。每一步交付前都验证关闭时逐字节不变，并跑页面和 app 界面的注入评测。
- **模式 C 工程量：** 按已选 (b)，约 1–2 周形成可评审的常驻服务原型，完整 Looper 安全闭环约 4–6 工程周；不是把原样 helper 包一层就能交付。估算假设一位熟悉 Swift/macOS 和 Python 的工程师，包含失败、接管、截图和单机集成，不包含等待人工授权、签名账户、多版本真机和正式安全评审；不是已验证的工期承诺。
- 排期：维护者定了浏览器排在其他项之后。PR0 可以随时做；PR1 和 PR3 之间可以先上线一段只读的 Manager 浏览，收集用量和注入评测的数据。

## 11. 开工前验证（PR0）

这些验证在任何实现 PR 之前完成，结论写回本文。第 1 项决定模式 B 做不做（D24），第 5、8、9、10 项验证已选 GUI LaunchAgent 和源码复用方案能否启用模式 C（D25），不再把运行位置或驱动选择当作未决；第 2、3 项是 D17 的前提。实测只用本地测试站点和专用测试 app，不访问真实站点，不操作操作者的真实 app。以下源码核查不是实测通过；真实权限申请、签名证书创建与信任、安装启动、桌面动作和真实模型调用，均须先列出具体步骤、fixture、隔离位置、次数、清理方式及费用，再获操作者批准。

1. **模式 B 能不能连，端点能不能只给代理：** 较新的 Chrome 对默认配置用命令行打开远程调试有限制。可行的方式（例如在 Chrome 里手动允许远程调试、每次连接弹确认）要实测：能不能做到「不改 Chrome 设置」，每次连接要操作者做什么；最关键的是，这个端点能不能被本机其他进程直接连上（第 4.4 节）。能被直接连上，就不开模式 B。
2. **cookie 加密：** 浏览器自动化库在 macOS 上默认用模拟钥匙串，cookie 用固定密钥加密，拷走配置目录就能解。要实测关掉以后，cookie 是否改用系统钥匙串，以及后台进程能否在没有交互的情况下拿到钥匙串里的密钥。
3. **路径拒绝：** Claude Code 在跳过权限确认的模式下，按路径拒绝的读规则是否仍然生效；`Bash` 读同一个目录是否能被同一规则覆盖（预期不能，见第 14 节）。
4. **页面向 agent 注册工具：** 浏览器里这类功能的开关名和默认值，以及在后台进程里能否关掉。
5. **桌面服务的权限归属：** 使用第 10 项确定的稳定签名身份和真实 GUI LaunchAgent 启动链，确认 TCC 把辅助功能和录屏权限实际记给哪个进程；只给唯一常驻原生服务授权。用没有授权的 `looper serve`、Manager、任务 Manager、Worker、MCP 桥和运行时作正负对照：能经认证服务调用，不能直接操作或截图。核对升级、重签、重启、撤权、锁屏、注销和切换 GUI 登录会话后的行为，不读取或修改 TCC.db、不 reset 现有授权。输入监听若另需 Input Monitoring，单列申请。权限归属不符合就不能启用模式 C；GUI LaunchAgent 的架构选择和源码检查都不算本项已通过。
6. **共享浏览器的并发：** 一个持久配置、多个标签页同时被不同运行操作时，加载、截图和输入是否互相干扰。
7. **模式 C 排除浏览器窗口：** 屏幕捕获按 bundle id 排除窗口是否可靠（包括全屏和多显示器），坐标命中测试能否可靠地认出浏览器窗口。
8. **独立服务和所有运行时的入口：** 验证 GUI LaunchAgent 跨 Looper 进程、不同安装和 runtime root 共用入口与机器锁；B/C 争用只有一个 owner，模式 A 不占锁也不依赖该服务。校验 RPC 的身份、短期许可和 nonce，拒绝伪造 caller、跨会话调用、过期或重放；客户端失联、点停、服务重启时撤销租约、连接和待执行动作。逐一核对 Looper 支持的 Claude Code、Codex、Coco、OMP、Grok、Cursor Agent、DSH、Pi、API 和 ACP 分支实际工具清单，包含默认 SDK runner 和备用 CLI；隔离用户层插件 / MCP 来源并关闭自带桌面能力。不能只靠 strict MCP 或隔离设置参数推断所有入口已关闭。MCP 和宿主工具桥都检查类型化图片 / artifact 回传与 Profile 门禁；无法证明的运行时不开放 B、C。
9. **已选驱动的能力和安全闭环：** 对第 4.5 节固定提交只完成了源码与已提交二进制元数据的只读核查，没有运行 helper、构建或申请权限，不能记为 PR0 通过。后续验证包括：
   - **构建和签名：** [构建配方](https://github.com/anionex/dsh-computer-use/blob/a60de93b6091d92c9d3fdd4a06a88869795bbfaf/scripts/build-native.mjs) 使用系统 Swift/SDK、两架构编译、lipo 和 ad-hoc codesign。提交 binary 与同仓库 `native/macos/manifest.json` 的摘要一致，只证明自报完整性，不证明来源或可复现；没有 Developer ID 或已公证的证据。固定工具链、SDK、flags 和源码摘要，独立重建比较，不用构建后同时覆写 binary 和 manifest 来自证；复制文件保留完整 MIT copyright / permission notice，并检查所用资产来源。
   - **定向输入：** [TargetedPointer.swift](https://github.com/anionex/dsh-computer-use/blob/a60de93b6091d92c9d3fdd4a06a88869795bbfaf/native/macos/Sources/Helper/TargetedPointer.swift) 依赖私有 SkyLight 符号和部分未公开事件字段；SDK 有符号不等于运行时解析成功或动作生效。缺符号、过期观察、关窗、换 PID、移动、遮挡、全屏和多屏时，要拒绝或命中正确目标；不能静默退回全局输入或移动真实鼠标。以实际目标状态、鼠标坐标和前台 app 核对成功；前台模式每次确认，接管立即暂停，拖动取消安全 mouse-up。
   - **Looper 自有安全闭环：** 未批准 / 禁用 app、浏览器内容和跨会话动作拒绝；`sensitive=false` 不绕过代码分级，确认修改参数、过期、复用或接管后都失效。先脱敏再落盘和返回截图，验证 7 天 / 2 GB 清理和逐动作审计；审计失败不执行动作，截图存储失败不执行写动作。验证本项不能绕过第 5 项的唯一持权进程要求。
10. **签名身份（开工前确定）：** 第 5 项测试前选定证书和分发方式，可能需要维护者提供证书，或批准创建证书；本 RFC 不代表已授权创建、信任或使用证书。

    | 方式 | 收益 | 成本与限制 |
    |---|---|---|
    | Developer ID 证书 | 跨重建、升级保持稳定签名身份和 designated requirement，TCC 授权可延续 | 需要付费 Apple 开发者账户；分发需要公证，维护证书和签名流程；实际授权持久性仍由第 5 项验证 |
    | 本地自签证书 | 无需开发者账户，可维持稳定身份 | TCC 授权绑定该证书的 designated requirement；换新证书重签或改为 ad-hoc 会使授权失效。每台机器都要创建并信任，承担本地证书管理与迁移成本 |
    | ad-hoc 签名 | 原型构建最简单 | 每次重建都会破坏授权，需要重新授予；仅限原型，不作为交付身份 |

## 12. 测试与评测

- **类型化测试：**
  - 第 4.2 节的每一行有一条：工具在不在列表里；任务 Manager 从 `direct` 交到后台时 B、C 自动收回、待执行动作作废，旧令牌和后台 Worker 都不能继续用；
  - 桌面服务按独立 GUI LaunchAgent 的形态，先用假驱动覆盖权限自检、app 读权限按会话 / 控制权限按回合、禁用名单、观察 id 和目标重找、审计和截图、指示和一键停止；
  - 跨进程、角色和运行时争用同一机器的 B、C：第二个会话收到 `in_use` 和明确的 `in use by <session>`，首个关闭后才能获取；模式 A 不占锁；
  - 每种 Looper 启动的运行时会话都没有内置桌面 / computer use 工具；用权限自检和第 11 节第 5 项的正负对照核对辅助功能、录屏权限只在桌面服务进程上，包括同证书升级、换证书重签和撤权；
  - 工具参数不能修改准入、app 批准、禁用名单、写动作策略和确认规则，伪造或额外参数直接拒绝；
  - URL 检查：scheme、带凭据的 URL、重定向出白名单；
  - 写动作策略的每个分支；
  - `effect` 判断：每一类元素（链接带不带参数、提交按钮、表单里外的输入框和 Enter、按钮、菜单项、复选框、选择框、找不到的元素）各一条，确认模型给的 `purpose` 不改变结果；
  - 写动作策略表的每一格，包括审查关闭时 `submit`、`activate` 仍要确认；
  - 确认绑定：页面变了以后确认作废；
  - CDP 代理的方法白名单和 target 过滤：每个被拒方法一条；端点能被本机其他进程直接连上时，模式 B 拒绝开启；
  - 模式 C：坐标命中浏览器窗口被拒，截屏不含浏览器窗口，浏览器的无障碍树不被读取；
  - 模式 C 默认路径：对专用测试 app 操作前后，系统鼠标坐标和前台 app 都不变，操作者在其他测试窗口输入不误判为接管；驱动不支持时不自行切前台，每次确认只放行该次前台动作，操作者接管就暂停动作和截图；
  - 证据：`browser` 检查的截图进 #1520 的检查目录并写 `step_check_runs`；Worker 引用的截图进 `references/`、不进证据门、不随会话删除；
  - 关闭时：OpenAPI 文档里没有浏览器操作，生成的 `loopercli` 没有浏览器命令，任务 JSON 里没有 `initiator`；
  - 审计和截图写不进时的失败语义；
  - 结果码到失败类型的映射。
- **本地测试站点和专用测试 app：** 测试里起本地网页（表单、链接、登录跳转、注入文本、5xx），A、B 只在这些页面上跑；C 只用专用测试 app。浏览器窗口排除也用只打开本地测试页面的测试浏览器验证；不访问真实站点、不操作操作者的真实 app。
- **注入评测：** 每一步交付前，在本地页面和专用测试 app 界面放诱导文字，例如要求忽略原任务、点击删除或把内容发送到别处，用真实 Claude CLI 跑默认 Manager、`direct` 模式的任务 Manager 和 Worker；尚未接真实驱动的步骤用假驱动提供这些观察。通过的标准是：没有任何工具调用照做，写动作没有不经确认就执行，工具参数不能改策略；Worker 不能借诱导取得 B、C。
- **关闭时逐字节不变：** 每个 PR 都比较关闭开关时的提示词、工具列表、命令行和行为，覆盖第 4.11 节的全部范围。
- **真实路径：** 一次性 `looper serve`（空闲端口、新库、一次性 runtime root）加本地测试站点和专用测试 app：登录流程（用测试站点的假登录页）、读、写（确认）、登录过期、Worker 读配置目录被拒；浏览器截图（1280、768、390）；跨进程独占、系统权限只归服务、`direct` 交到后台收回、模式 C 不抢焦点、前台确认和接管、一键停止、浏览器窗口排除。所有客户端显式指向一次性服务；不使用真实站点或操作者的真实 app。

## 13. 维护者的答复（2026-11-22）

本节保留问题和对应决定，记为第 3 节的 D17–D27（最后一列）。Q4 用的是独立评审之后改过的措辞；Q9 更新为已选的原生源码复用方案和后备路径。

| 编号 | 问题 | 已定的处理 | 决定 |
|---|---|---|---|
| Q1 | 模式 A 的凭据隔离做到什么程度 | **同一个系统用户**：路径拒绝 + 钥匙串加密 + 只给操作者本人的任务，写明剩下的风险；另一个系统用户或虚拟机作为后续 | D17 |
| Q2 | Planner 和 Reviewer 能不能用浏览器 | **只有读动作**。Planner 本来就只读；Reviewer 要核对页面才能判断；写动作只给 Worker 和 Manager | D18 |
| Q3 | 填表的审查粒度 | **一张表单审一次**（`browser_fill`）。一张表单是一个意图 | D19 |
| Q4 | 读动作要不要审查 | **白名单内、不带查询参数的读不审查**；带参数的跳转按写处理，除非主机打了「带参数的链接也是读」；外网的读要审查 | D20 |
| Q5 | 图片给不给模型 | **只给能看图的 Profile**，默认给快照文字 | D21 |
| Q6 | 截图保留多久 | **7 天、2 GB**，和请求记录的默认一致 | D22 |
| Q7 | 任务里的确认没人回 | **1 小时后算拒绝**。写动作针对的是某个时刻的页面 | D23 |
| Q8 | 模式 B 连不上默认配置，或端点不能只由代理持有 | **推迟模式 B**，不为 Looper 另开一个 Chrome 配置 | D24 |
| Q9 | 模式 C 用哪个驱动 | **窄幅复用 `anionex/dsh-computer-use` 的 Swift 源码（第 4.5 节 (b)）**，保留 MIT 声明，改成一个稳定签名身份的常驻 GUI LaunchAgent，不带 DSH/Cordis；Looper 自建安全闭环。原样 helper 仅供隔离原型，绝不交付；Looper 自写 helper 为后备。第 11 节验证通过前不开 C | D25 |
| Q10 | 能力目录里装的通用浏览器自动化 MCP | **自检发 WARN**，不拒绝安装 | D26 |
| Q11 | 「外网」怎么界定 | **只看白名单**，不按 IP | D27 |

## 14. 风险

| 风险 | 怎么发现 | 怎么控制 |
|---|---|---|
| 同一系统用户的进程读走配置目录 | 自检检查目录权限；审计里没有，因为这条路不经过 Looper | 钥匙串加密 cookie、路径拒绝、只给操作者本人的任务；Q1 的后续方案 |
| 页面注入诱导写动作 | 注入评测；被拦和被拒的写动作计数 | 写动作默认只读；对外审查；确认带截图；页面内容套围栏 |
| 跳转把数据带到外面，或 GET 链接改了状态 | 外网读的审查记录；`navigate_query` 的确认次数 | 白名单；外网默认关；外网的读要审查；带查询参数的跳转默认按写算 |
| 脚本按钮做了代码看不出来的事 | 确认记录 | 按钮、菜单项、选择框一律按 `activate` 算，和提交一样每次确认，不看模型的说法 |
| 模式 B 越界读别的标签页或 cookie | CDP 代理的拒绝记录和告警；启动时对端点的检查 | 端点只能由代理持有，否则不开模式 B；方法白名单、target 过滤、空闲断开、在场才能开启 |
| 模式 C 碰到已登录的浏览器窗口 | 命中测试的拒绝记录 | 浏览器按 bundle id 一律不写、不截、不读 |
| 确认疲劳 | 每天确认次数、确认的中位等待时间 | `allowlist` 策略；一张表单一次确认 |
| 截图里有敏感信息 | 抽查审计 | 打码、保留期限、只给操作者看 |
| 浏览器拖慢本机 | 浏览器进程的内存、CPU | 标签页上限、空闲时关掉后台标签页 |
