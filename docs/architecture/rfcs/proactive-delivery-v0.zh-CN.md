# RFC：主动消息的送达层 v0（`deliver()`、`delivery.enabled`）

- **状态：** 草案，随第一个 PR（纯函数、开关、关卡提醒）合入。默认值由维护者定（第 1.3 节）；表名和开关名沿用产品设计的建议。
- **需求：** Looper 主动发出的消息（关卡提醒、任务通知、订阅推送、意图、收尾回合、用户自己设的提醒）在发出前都经过同一个判断：现在发、进摘要，还是不发；每次判断留一行记录，说得清「为什么没告诉我」。夜里不打扰，私聊每天有上限，但卡住任务的关卡和用户自己要的提醒永远照发。
- **相关：** [用户关卡 RFC](user-gate-v0.zh-CN.md)（提醒的节奏 `gate_reminder_hours`、`GateCourier`）；[Manager 收尾、账本与晨报 RFC](manager-completion-ledger-v0.zh-CN.md)（晨报的快照，摘要最终在那里出现）；[消息流与订阅 RFC](message-stream-v0.zh-CN.md)（订阅的静默词和窗口筛选）；[定时任务 RFC](scheduled-tasks-v0.zh-CN.md)（`gate_command`、`notify`）。
- **术语：**
  - **主动消息：** 不是在回答用户这一刻的消息，由 Looper 自己决定发出。回答用户的消息（Manager 的回复、关卡讨论里规划者的回复）不经过送达层。
  - **计数：** 一条主动消息占用所在私聊当天的额度。
  - **摘要：** 推迟到下一份晨报里出现的主动消息，按 `digest_date` 归入那一天的晨报。

## 1. 决策函数

### 1.1 输入和输出

`looper/agents/proactive_delivery.py` 的 `deliver(delivery, policy, state) -> Decision` 是纯函数，不读库、不读时钟：

| 输入 | 字段 |
|---|---|
| `Delivery`（这条消息） | `kind`（发送点：`gate_reminder`、`task_notice`、`subscription`、`intent`、`completion`（收尾回合）、`user_reminder`）；`urgency`（`normal`、`blocking`：关卡在等用户、卡着它的任务，`user_set`：用户自己设的提醒）；`chat`（`direct` 私聊和控制台聊天，`group` 群）；`source`（来自定时任务或订阅时，它的节制字段：生效时段和它的时区）；`silent`（来源说这次没什么要说） |
| `DeliveryPolicy`（操作者的规则） | `timezone`（`user.timezone`）、`quiet_hours`（`DailyWindow`，可为空）、`dm_daily_cap`、`recent_reply` |
| `DeliveryState`（这个会话此刻） | `now`、`sent_today`（今天按本地日期已计数的条数）、`last_user_message_at` |

输出 `Decision`：`outcome`（`send`、`digest`、`drop`）、`reason`（下表哪一条定的）、`counted`、`digest_date`（只有 `digest` 有）。

### 1.2 规则（按顺序，先命中的定）

| # | 条件 | 结果 | `reason` | 计数 |
|---|---|---|---|---|
| 1 | 来源说没什么要说 | 不发 | `silent` | 否 |
| 2 | `urgency` 是 `blocking` 或 `user_set` | 发 | `urgent` | 否 |
| 3 | 不在来源自己的生效时段（按来源的时区） | 进摘要 | `outside_active_hours` | 否 |
| 4 | 用户 `recent_reply` 内在这个会话说过话 | 发 | `recent_reply` | 否 |
| 5 | 在免打扰时段（按 `policy.timezone`，含开始、不含结束） | 进摘要 | `quiet_hours` | 否 |
| 6 | 私聊，且今天已计数 `dm_daily_cap` 条 | 进摘要 | `daily_cap` | 否 |
| 7 | 其余 | 发 | `within_limits` | 私聊计数，群不计数 |

- 紧急类排在生效时段和免打扰之前：卡住任务的关卡和用户自己的提醒不受任何节制。
- 来源的生效时段排在「刚说过话」之前：生效时段是这个来源自己声明的，用户在聊天不代表想在那个时段收到它。
- 群不计入每日上限，但遵守免打扰。

### 1.3 默认值（维护者定）

| 规则 | 默认 |
|---|---|
| 免打扰时段 | 22:00–08:00，按 `user.timezone`；没有设置时取服务器时区（`cron.local_zone_name()`） |
| 每日上限 | 私聊每天 5 条，超出的进摘要 |
| 刚说过话 | 5 分钟内放行，不计数 |
| 紧急类 | 卡住任务的关卡、用户自己设的提醒，永远照发 |

### 1.4 摘要归入哪一天

`digest_date` 是推送恢复的那天：下一次免打扰时段结束时的本地日期；没有免打扰时段时是下一个本地日。所以 23:00 推迟的进第二天，03:00 推迟的进当天早上那份，15:00 超出上限的进第二天。

## 2. 开关；关闭时什么不变

- 系统设置 `delivery.enabled`（`chat_settings.update` 的 `delivery_enabled`，默认 `false`，`loopercli chat-settings update --delivery-enabled true`；客户端调用要确认）。关闭时没有任何发送点调用 `deliver`，发出的每条消息和现在一样，也不写 `proactive_deliveries`（测试比对）。
- 时区读系统设置 `user.timezone`（IANA 名称）；没有或不认识时用服务器时区。v0 只读，编辑入口随每轮上下文那一项加入。
- 免打扰时段、上限和时区在 v0 不开放修改：第一个发送点（关卡提醒）都是紧急类，不受它们影响；接入第一个会产生「进摘要」的发送点时，再加它们的设置。

## 3. 发送点的接入顺序

一个发送点一个 PR，各自默认关闭（都由 `delivery.enabled` 控制）：

1. **关卡提醒**（本 PR）：`GateCourier` 发「还在等你」之前判断。提醒只对还在等用户、任务仍在进行的关卡发出（`due_reminders`），等着的关卡卡住它之后的步骤，所以一律是 `blocking`，结果总是「发」。判断本身失败（例如数据库拒绝写入）时记日志、照常发出，和开关关闭时一样：送达层不能扣下紧急消息。这一步建立判断和记录的通路，不改变发不发。
2. 任务通知；3. 订阅推送；4. 意图推送；5. 收尾回合；6. 用户自己设的提醒（随提醒功能）。

**顺序约束：** 会产生「进摘要」的发送点（第 2–5 项），要等晨报能读出进摘要的消息之后才能打开，否则推迟的消息没有地方出现，等于静默丢失。

## 4. 记录

`proactive_deliveries`，一条主动消息一行（命名迁移 `proactive_deliveries`，新表，在线安全）：

| 列 | 说明 |
|---|---|
| `dedupe_key` | 唯一：发送点和它的消息（关卡提醒是 `gate_message:<消息 ID>`）。同一条消息重试时沿用第一次的判断，不再记一行 |
| `kind`、`urgency`、`chat_kind` | 第 1.1 节的输入 |
| `channel_id`、`chat` | 目标会话 |
| `task_id`、`step_id`、`schedule_id` | 关联的任务、关卡和定时任务（任务删除时一起删） |
| `outcome`、`reason`、`counted`、`digest_date` | 判断结果；约束保证只有 `digest` 有 `digest_date`、只有 `send` 能计数 |
| `local_date` | 判断时在 `policy.timezone` 的本地日期，每日上限按它计 |
| `decided_at`、`sent_at` | 判断（或只记）的时间；渠道接收的时间（发送失败则为空） |
| `replied_at` | 发出后用户在这个会话里的第一条消息的时间，由度量埋点回填（「30 分钟内有没有回复」由它算）；没发出的不能有 |

- **一次一个：** `decide` 在一个事务里对这个会话加咨询锁，读当天计数、判断、写入，所以两个发送点不会同时拿到当天最后一个额度。
- **和第 0 期度量的关系：** 这张表由本 PR 建，第 0 期的度量埋点复用它，不另建表、不改列：它在发送后记回执、回填 `replied_at`，再加指标页；开关关闭时送达层一行都不写。
- **保留：** v0 只有关卡提醒写入，量很小；接入任务通知时按 30 天加入清理（同 `manager_events`）。

## 5. 定时任务和订阅的节制字段（第二个 PR）

产品设计给定时任务和订阅加四个节制字段。和现有字段的对应：

| 字段 | 落在哪里 |
|---|---|
| 生效时段 | 新列 `schedules.active_hours`（`HH:MM-HH:MM`，按定时任务自己的时区，空即全天）：时段外的触发跳过、不调模型；时段外才产生的推送由第 1.2 节第 3 条进摘要 |
| 只在空闲时运行 | 新列 `schedules.idle_only`：默认 Manager 有回合在进行时跳过这次触发 |
| 允许不说话 | 订阅已有：静默词 `quiet_word`（规则里「不能静默」的由代码兜底）；判断里是 `silent` |
| 触发前检查 | 定时任务已有的 `gate_command`；订阅已有：窗口里没有选中的事件时以 `skipped_gate` 结束，不调模型 |

订阅本身是 `kind = subscription` 的定时任务，所以新列只加在 `schedules` 上。

## 6. 不在 v0

- **2 分钟合并窗口：** 需要先把消息排队再发，等出站网关的 `outbox`。
- **「为什么没告诉我」的查询入口：** 第一个会产生「进摘要」或「不发」的发送点接入时一起加；在那之前记录里只有「发」。
- **设置页：** 同第 2 节。

## 7. 测试

- 纯函数：`tests/test_proactive_delivery.py` 表驱动，每条规则和每个边界一行（22:00 含、08:00 不含、第 5 条和第 6 条、5 分钟整、来源时区和策略时区不同、夏令时切换的夜里）。
- 关闭时：关卡提醒发出的文字、目标和写入与现在逐字一致，不写 `proactive_deliveries`。
- 打开时：同样的提醒照发（凌晨、超出上限也发），记一行 `send` / `urgent` / 不计数；重试不重复记。
