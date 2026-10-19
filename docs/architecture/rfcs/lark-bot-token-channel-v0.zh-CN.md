# 「飞书机器人 token」渠道类型 v0

> 状态：本 PR 落地。日期：2026-10-18。决定来自计划第 5.2 节（2026-10-15）。

## 0. 一句话

在「Lark (lark-cli)」旁边新增渠道类型 `lark_bot`（显示名「Lark (bot token)」，渠道 id `channel-lark_bot`）：功能与 lark-cli 渠道相同，但不依赖 `lark-cli`，直接用 `app_id` + `app_secret` 调飞书/Lark 开放平台，用事件订阅的**长连接（WebSocket）**收消息，不需要公网回调地址。

## 1. 范围

与 lark-cli 渠道一致：私聊和群里 @ 机器人的消息、回复（Markdown）、单条状态消息原地编辑、收到消息的「Get」表情、任务进度与结束通知、定时任务通知、用户闸门（G2：卡片式提问、回复路由、幂等发送）、自己的发送者白名单（按姓名或邮箱添加）、被拒发送者列表。lark-cli 渠道原样保留。

## 2. 方案取舍

### 2.1 渠道类型：独立类型，而不是 lark 的一个传输选项

| 方案 | 做法 | 取舍 |
|---|---|---|
| A. 独立类型 `lark_bot`（采用） | 新行 `channel-lark_bot`，自己的白名单、被拒列表、开关 | 两个渠道可以同时开（连不同的应用）；open_id 按应用区分，各自的白名单互不混用；代价是下游所有按 `"lark"` 分支的地方要认两个类型（用 `message.is_lark` 一处判定） |
| B. 给 `lark` 渠道加 `transport` 字段 | 同一行里切换传输 | 改动小，但两个应用不能同时用，切换传输等于换应用，白名单（按应用）要作废；设置页把两种完全不同的凭据混在一行 |

采用 A。`NotifyChannel`、闸门的渠道、定时任务的 `notify_channel` 都多一个取值 `lark_bot`，数据库的 CHECK 随之放宽；任务的 `origin_channel_id` 本来就是渠道 id，所以任务结束通知、闸门消息自然回到收消息的那个应用。

### 2.2 适配器：传输接缝，而不是复制一份

`LarkChannelAdapter` 里和 lark-cli 绑定的只有「发、回复、编辑、表情、收事件、查姓名」六件事。把它们抽成 `LarkTransport`（`lark_transport.py`）：`CliTransport` 保持原来的 argv 与子进程行为，`BotTransport`（`lark_bot.py`）用开放平台实现。事件解析、白名单、状态消息、闸门、通知全部共用同一份适配器代码，两条渠道不会各自演化。

### 2.3 收消息：长连接，自己实现协议

| 方案 | 取舍 |
|---|---|
| 官方 Python SDK `lark-oapi`（`ws.Client`） | 包含全部开放平台接口，体积大（数十 MB 生成代码）；`start()` 在模块级事件循环上阻塞运行，与 Looper 的 asyncio 服务集成要单开线程；依赖 `requests`、`protobuf`。不够「小」 |
| 回调地址（HTTP webhook） | 需要公网地址，与「本机自托管」不符 |
| 按文档实现长连接协议（采用） | 协议很小：`POST /callback/ws/endpoint` 取连接地址和 `ClientConfig`，WebSocket 上是 protobuf `Frame`（7 个字段），控制帧 ping/pong，数据帧 `event`，收到后立即回包 `{"code":200}`，大消息按 `sum/seq` 分片。手写 varint 编解码约百行，不引入 protobuf；WebSocket 用 `websockets`、REST 用 `httpx`（后者本来就是 python-telegram-bot 的依赖） |

协议细节取自官方 SDK 源码（`lark_oapi/ws/client.py`、`pbbp2.proto`，v1.7.5）。新增直接依赖：`websockets>=13,<16`、`httpx>=0.27,<1`。

**事件处理要点**

- 收到数据帧先回包再处理：平台约 3 秒内没收到回包会重发。
- 去重：按 `event_id` 与 `message_id` 两个键，各留最近 1000 个，跨重连保留（重连后平台可能补发）。
- 断线：服务端关闭、网络断开、或超过 3 倍 ping 间隔没有任何帧（半开连接）都结束这一轮连接，适配器 10 秒后重新取地址再连；失败（如密钥错误、超过连接数上限）同样每 10 秒重试一次并写日志。
- 事件转成 lark-cli 渠道已经在读的「扁平事件」，所以 `_handle_lark_event` 不用改。

### 2.4 发送：tenant_access_token

`POST /open-apis/auth/v3/tenant_access_token/internal` 换取 token（约 2 小时有效），剩余不足 5 分钟就刷新，并发调用共享同一次刷新；平台回 401 或 token 失效码（99991661、99991663）时丢弃缓存、换新 token 重试一次。

| 动作 | 接口 |
|---|---|
| 发送 | `POST /im/v1/messages?receive_id_type=open_id｜chat_id`，`uuid` 即幂等键（≤50 字符，1 小时内同键只发一条） |
| 回复 | `POST /im/v1/messages/{id}/reply`；被撤回的消息返回 230011，映射为 `parent_gone` |
| 编辑 | `PUT /im/v1/messages/{id}` |
| 表情 | `POST /im/v1/messages/{id}/reactions`（`Get`） |
| 机器人信息 | `GET /bot/v3/info`（启动自检：凭据有效、取 `open_id` 用来识别群里的 @） |

Markdown 用 `post` 消息的 `md` 标签发送，纯文本用 `text`；`<at user_id="ou_…"></at>` 在 Markdown 里 @ 人。

### 2.5 按姓名添加白名单：租户 token 搜不了人

lark-cli 用的是用户身份的 `contact +search-user`；机器人的租户 token 没有「搜索用户」接口。采用：

- 输入含 `@`：按邮箱 `batch_get_id` 精确查（再取姓名）；
- 否则按姓名/邮箱子串匹配通讯录：遍历根部门及其全部子部门的成员，**最多 40 次请求**，匹配满 10 个即停；预算用完还没扫完时接口返回 `complete: false`，设置页提示「只搜了通讯录的一部分，请改用邮箱」。
- 被拒发送者列表的姓名、白名单旁的姓名走 `contact/v3/users/batch`（50 个一批，一个 id 查不到整批失败时退成逐个查，和 lark-cli 渠道一致）。

需要应用开通通讯录只读权限，且通讯录权限范围覆盖要添加的人；缺权限时平台的报错（含缺哪个 scope）原样显示在设置页。

### 2.6 凭据

`app_id`、`domain`（`feishu`｜`lark`，默认 `feishu`）是渠道配置；`app_secret` 存进 `channel_secrets.bot_token`（沿用现有列和 `bot_token` / `has_bot_token` 的接口字段，不新增表），API 只显示是否已设置，设置页是掩码输入。不进日志、异常信息、`repr`、配置导出（导入时报告「需要补 app secret」）。白名单记下所属 `app_id`（`allowlist_app_id`），换了应用而白名单没重建时渠道不启动，和 lark-cli 渠道的规则一致。

## 3. 入口

- 设置页 Settings → Channels：新增卡片（app_id、domain、app secret、白名单、按姓名添加、被拒发送者）；
- 接口：`GET /api/channels/lark_bot/status`、`GET /api/channels/lark_bot/people?q=`，配置读写走现有 `/api/channels/{id}`；
- 聊天页的渠道列表、定时任务通知对象、闸门设置的聊天选项里都会出现这个渠道；
- Telegram、CLI 无需改动。

## 4. 风险与监控

- 真实平台未验证（本 PR 只用本地替身验证，按规则不连真实飞书）：协议细节来自 SDK 源码；token 失效码、接口权限名、`md` 标签渲染、`batch_get_id` 的字段、连接数上限都要在维护者授权的测试聊天里实测（计划 §5.4）。
- 同一应用的长连接数有上限：lark-cli 渠道和 bot token 渠道不要同时连同一个应用，否则事件会被分到其中一个连接。
- 告警面：渠道未启动原因（`start_problem` / `problem`）、每次重连的日志（原因）、被拒发送者计数。
