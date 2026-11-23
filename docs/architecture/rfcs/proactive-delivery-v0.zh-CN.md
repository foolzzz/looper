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
| `Delivery`（这条消息） | `kind`（发送点：`gate_reminder`、`task_notice`、`subscription`、`intent`、`completion`（收尾回合）、`user_reminder`、`morning_brief`（晨报，见晨报 RFC））；`urgency`（`normal`、`digest`：订阅摘要，沿用普通节制规则，`blocking`：关卡在等用户、卡着它的任务，`user_set`：用户自己设的提醒）；`chat`（`direct` 私聊和控制台聊天，`group` 群）；`source`（来自定时任务或订阅时，它的节制字段：生效时段和它的时区）；`silent`（来源说这次没什么要说） |
| `DeliveryPolicy`（操作者的规则） | `timezone`（`user.timezone`）、`quiet_hours`（`DailyWindow`，可为空）、`dm_daily_cap`、`recent_reply` |
| `DeliveryState`（这个会话此刻） | `now`、`sent_today`（今天按本地日期已计数的条数）、`last_user_message_at` |

输出 `Decision`：`outcome`（`send`、`digest`、`drop`）、`reason`（下表哪一条定的）、`counted`、`digest_date`（只有 `digest` 有）。

### 1.2 规则（按顺序，先命中的定）

| # | 条件 | 结果 | `reason` | 计数 |
|---|---|---|---|---|
| 1 | 来源说没什么要说 | 不发 | `silent` | 否 |
| 2 | `kind` 是 `morning_brief`（晨报本身，见晨报 RFC） | 发 | `within_limits` | 否 |
| 3 | `urgency` 是 `blocking` 或 `user_set` | 发 | `urgent` | 否 |
| 4 | 不在来源自己的生效时段（按来源的时区） | 进摘要 | `outside_active_hours` | 否 |
| 5 | 用户 `recent_reply` 内在这个会话说过话 | 发 | `recent_reply` | 否 |
| 6 | 在免打扰时段（按 `policy.timezone`，含开始、不含结束） | 进摘要 | `quiet_hours` | 否 |
| 7 | 私聊，且今天已计数 `dm_daily_cap` 条 | 进摘要 | `daily_cap` | 否 |
| 8 | 其余 | 发 | `within_limits` | 私聊计数，群不计数 |

- 紧急类排在生效时段和免打扰之前：卡住任务的关卡和用户自己的提醒不受任何节制。
- 来源的生效时段排在「刚说过话」之前：生效时段是这个来源自己声明的，用户在聊天不代表想在那个时段收到它。
- 群不计入每日上限，但遵守免打扰。

### 1.3 默认值（维护者定）

| 规则 | 默认 |
|---|---|
| 免打扰时段 | 22:00–08:00，按 `user.timezone`；没有设置时取服务器时区（`turn_context.zone()`） |
| 每日上限 | 私聊每天 5 条，超出的进摘要 |
| 刚说过话 | 5 分钟内放行，不计数 |
| 紧急类 | 卡住任务的关卡、用户自己设的提醒，永远照发 |

### 1.4 摘要归入哪一天

`digest_date` 是推送恢复的那天：下一次免打扰时段结束时的本地日期；没有免打扰时段时是下一个本地日。所以 23:00 推迟的进第二天，03:00 推迟的进当天早上那份，15:00 超出上限的进第二天。

## 2. 开关；关闭时什么不变

- 系统设置 `delivery.enabled`（默认 `false`）：只有操作者能读写，经 `proactive_delivery.get` 和 `proactive_delivery.update`（`loopercli proactive-delivery update --enabled true`，高风险，要确认），和投递守卫（`delivery_guard`）一样不放进客户端看得到的 `chat_settings`。关闭时，由这个开关控制的发送点不调用 `deliver`，也不写 `proactive_deliveries`（测试比对）。晨报有自己的开关：它调用纯函数 `deliver`，只有送达层开着时才记一行（见晨报 RFC）。
- 时区是用户的 `user.timezone`，由每轮上下文的设置保管和编辑（`turn_context.zone()`：存的时区，没有或不认识时用服务器时区）；送达层只读它。
- 免打扰时段、上限和时区在 v0 不开放修改：第一个发送点（关卡提醒）都是紧急类，不受它们影响；接入第一个会产生「进摘要」的发送点时，再加它们的设置。

## 3. 发送点的接入顺序

一个发送点一个 PR，各自默认关闭（都由 `delivery.enabled` 控制）：

1. **关卡提醒**（本 PR）：`GateCourier` 发「还在等你」之前判断。提醒只对还在等用户、任务仍在进行的关卡发出（`due_reminders`），等着的关卡卡住它之后的步骤，所以一律是 `blocking`，结果总是「发」。判断本身失败（例如数据库拒绝写入）时记日志、照常发出，和开关关闭时一样：送达层不能扣下紧急消息。这一步建立判断和记录的通路，不改变发不发。
2. **任务通知**：聊天渠道（Lark、Telegram）的任务完成和失败代码通知为 `normal`，在适配器发出前判断，通知文本和目标不变；收尾回合失败后退回同一通知时沿用它的决策键。关卡打开、更新问题和恢复后仍需用户回答的通知为 `blocking`，由 `GateCourier` 判断。最近回复按机器人和聊天查，包含切换 Manager 前后的记录。关闭开关时不加通知标记、不判断、不写新记录。任务进度、控制台聊天和收尾回合不在这次接入里。
3. 订阅推送；4. 意图推送；5. 收尾回合；6. 用户自己设的提醒（随提醒功能）。

订阅接入只覆盖 `SubscriptionRuns` 的摘要正文，不覆盖运行失败通知和告警。整份输出（包括分段）以
`subscription_run:<run id>` 共用一次 `subscription` / `digest` 判断和私聊额度；重试、重启沿用第一次判断。
进摘要时保留 `subscription_runs.output_text` 和关联定时任务的决策记录，运行以 `dashboard_only` 结束并推进游标，
不会重新调用处理器。发送点不重放已发送或不确定的分段；只有渠道确认接收时才记 `sent_at`。
关闭开关不增加发送点的查询和写入；判断失败时保留输出、按现有失败路径结束，渠道已接收后的回执写入失败只记日志。

**顺序约束：** 会产生「进摘要」的发送点（第 2–5 项），要等晨报能读出进摘要的消息之后才能打开，否则推迟的消息没有地方出现，等于静默丢失。

## 4. 记录

判断写进度量埋点的表 `proactive_deliveries`（表和命名迁移归度量的 PR 所有，送达层不另建表）。一条主动消息一行，判断和渠道回执在同一行：

| 列 | 谁写 | 说明 |
|---|---|---|
| `dedupe_key` | 送达层、度量 | 发送点给这条消息的键，非空时唯一。关卡提醒用它发帖时的幂等键 `gate-<消息 ID>`，度量记回执也用这个键，所以落在同一行 |
| `source`、`source_ref`、`manager_id`、`channel_id`、`chat`、`chat_kind` | 先写的一方 | `source` 是度量的来源枚举（`ProactiveSource`，送达层的 `DeliveryKind` 就是它） |
| `urgency`、`task_id`、`step_id`、`schedule_id` | 送达层 | 第 1.1 节的输入和关联对象 |
| `decision`、`reason`、`counted`、`local_date`、`digest_date`、`decided_at` | 送达层 | 判断结果；`local_date` 是判断时在 `policy.timezone` 的本地日期，每日上限按它计；`decided_at` 由送达层填（清理按 `COALESCE(sent_at, decided_at)`，进摘要和不发的行也能删） |
| `outcome`（`sent`、`rejected`、`unknown`）、`sent_at`、`external_id` | 度量或发送点 | 渠道回执；两者成对。度量开着时它在发送后按 `dedupe_key` 只更新这三列，不碰判断；度量关着时发送点自己记。已经记下的 `sent` 或 `unknown` 保持不动，`rejected` 会被同键的下一次尝试替换 |
| `replied_at`、`reply_match` | 度量 | 发出后用户的回复 |

- **一次一个：** `decide` 在一个事务里对这个会话加咨询锁，读当天计数、判断、写入，所以两个发送点不会同时拿到当天最后一个额度。
- **只判断一次：** 写判断用 `ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL`：已有判断的行原样返回（重试沿用第一次的判断）；度量在送达层打开之前记下、还没有判断的行补上判断。
- **关闭时：** 送达层一行都不写；度量是否写回执由它自己的开关决定。
- **保留：** 关卡和任务通知已接入；清理按度量的 `COALESCE(sent_at, decided_at)`。

## 5. 定时任务和订阅的节制字段

产品设计给定时任务和订阅加四个节制字段。订阅本身是 `kind = subscription` 的定时任务，所以新列只加在 `schedules` 上（命名迁移 `schedule_throttle`：两列可空或有默认值；`active_hours` 的检查和放宽后的 `schedule_runs` 状态检查以 `NOT VALID` 加上，提交后在线校验）。四个字段在代码里是一个类型化视图 `ScheduleThrottle`（`looper/agents/schedule_throttle.py`），API 的定时任务视图里是 `throttle`：

| 字段 | 落在哪里 | 行为 |
|---|---|---|
| 生效时段 | 新列 `active_hours`（`HH:MM-HH:MM`，按定时任务自己的时区，可跨午夜，空即全天） | 按时或补跑的触发，触发时刻或此刻在时段外就不触发：不记运行、`last_fired_at` 不变、`next_fire_at` 移到下一次，和时间规则本身没触发一样；之后接入订阅推送时，时段外才产生的推送由第 1.2 节第 3 条进摘要 |
| 只在空闲时运行 | 新列 `idle_only`（默认 `false`） | 定时任务所属的 Manager 正在回复消息（`ManagerAgent.busy`）时，这次触发记 `skipped_busy`，下一次再试；同一天重复的计在同一条运行上 |
| 允许不说话 | 已有字段，`allows_silence` | 订阅：静默词 `quiet_word`（规则里「不能静默」的由代码兜底）；其余：`notify` 不是 `always` |
| 触发前检查 | 已有字段，`pre_check` | 定时任务：`gate_command`（没有输出即 `skipped_gate`）；订阅：窗口里没有选中的事件时以 `skipped_gate` 结束；都不调模型 |

- 「立即运行」不受节制：那一刻是用户要的。
- 两项都默认关闭：没设的定时任务触发、运行上下文、通知和导出都和原来一样。
- 入口：API、生成的命令行和配置导入导出（导出只在设了时带上）；仪表盘只显示 `skipped_busy`，表单暂不编辑这两项；Manager 的 `looper_schedules` 不列它们（工具说明不变）。
- 模型可见的只有一处：开了 `idle_only` 的定时任务，下一次运行的上下文里「最近的运行」可能出现「跳过（管家正在回复消息）」，和已有的跳过原因同一格式。

## 6. 不在 v0

- **2 分钟合并窗口：** 需要先把消息排队再发，等出站网关的 `outbox`。
- **「为什么没告诉我」的查询入口：** 第一个会产生「进摘要」或「不发」的发送点接入时一起加；在那之前记录里只有「发」。
- **设置页：** 同第 2 节。

## 7. 测试

- 纯函数：`tests/test_proactive_delivery.py` 表驱动，每条规则和每个边界一行（22:00 含、08:00 不含、第 5 条和第 6 条、5 分钟整、来源时区和策略时区不同、夏令时切换的夜里）。
- 关闭时：关卡提醒发出的文字、目标和写入与现在逐字一致，不写 `proactive_deliveries`。
- 打开时：同样的提醒照发（凌晨、超出上限也发），记一行 `send` / `urgent` / 不计数；重试不重复记。
