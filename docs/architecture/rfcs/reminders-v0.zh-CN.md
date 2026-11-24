# RFC：一次性提醒 v0（`reminders`、`reminders.enabled`）

- **状态：** 随实现 PR 一起评审。默认关闭。
- **需求：** 用户在自己的回合里说「三点提醒我看 PR」，到点 Looper 把这句话发回给他。到点的发送由代码完成，不调模型；Looper 停机期间错过的提醒，恢复后只补发最近的一条，其余记为已错过；重启不丢、不重复。重复提醒仍用定时任务。
- **相关：** [Manager 平台工具 RFC](manager-platform-tools-v0.zh-CN.md)（工具目录、风险分级、`Turn.blocks`）；[消息线索 RFC](message-leads-intents-v0.zh-CN.md) 第 5 节（「只在用户本人的回合」的判定 `operator_identity.is_operator`）；[Agent 系统架构](../agent-system.md) 的 Manager 工具一节。

## 1. 范围

做：

- 表 `reminders`（迁移 `reminders`，只加表和一个部分索引）；
- 默认 Manager 的三个工具：`looper_reminder_create`、`looper_reminder_list`、`looper_reminder_cancel`；
- 到点发送和停机后的补发（`agents/reminders.py` 的 `ReminderService`）。

不做：

- 重复提醒（用定时任务）；
- Dashboard 页面、命令行命令、开关按钮：开关只有系统设置 `reminders.enabled`；
- 「需要你处理」汇总里列出已错过的提醒：汇总自己查 `status = 'missed'`；
- 其他 Manager：只有默认 Manager 有这三个工具。

## 2. 开关

系统设置 `reminders.enabled` 为 `true` 时打开；没有这一行或其他值都是关闭。

关闭时：

- 工具列表、系统提示词、Manager 的命令行和进程签名与没有这个功能时逐字节相同（`tests/test_reminders_db.py` 证明）；
- 不发送任何提醒，已存的待投递提醒原样留着；
- `ReminderService` 每 10 秒读一次这个设置，别的什么都不做。

打开时只有工具列表变：多出三个工具，排在最后。系统提示词不变，用法全写在工具描述里。工具列表的哈希进入进程签名，所以群聊回合（没有这三个工具）和用户本人的回合之间切换会重启常驻进程，和线索搜索一样。

## 3. 表

```sql
CREATE TABLE IF NOT EXISTS reminders (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    text               TEXT NOT NULL,          -- 用户的原话，1 到 500 字
    due_at             TIMESTAMPTZ NOT NULL,
    timezone           TEXT NOT NULL,          -- IANA，用来解释时间和显示
    channel_id         TEXT NOT NULL,          -- 送到哪个 bot：channel-lark、channel-telegram-2……
    chat               TEXT NOT NULL,          -- 用户在这个 bot 上的 id：飞书 open_id 或 Telegram 用户 id
    status             TEXT NOT NULL DEFAULT 'pending',
    attempts           INTEGER NOT NULL DEFAULT 0,
    detail             TEXT,                   -- 失败、不确定、错过、取消的原因
    external_id        TEXT,                   -- 发出去的消息 id
    created_channel    TEXT NOT NULL,          -- web、telegram、lark、lark_bot
    created_message_id TEXT,                   -- 设提醒的那条用户消息
    created_at, updated_at, settled_at TIMESTAMPTZ
);
```

**状态：**

| 状态 | 含义 | 由谁进入 |
|---|---|---|
| `pending` | 待投递 | 新建；渠道拒收后放回 |
| `sending` | 已认领，正在问渠道；认领先提交再发 | 投递 |
| `delivered` | 已投递 | 投递 |
| `failed` | 渠道拒收 3 次，或 bot 的操作者已经不是这个人 | 投递 |
| `uncertain` | 可能已发出（超时、断连、发送中进程停了）；不再发 | 投递 |
| `missed` | 到点时 Looper 没在发提醒，而且不是要补发的那一条 | 投递 |
| `cancelled` | 用户取消，只能从 `pending` 进入 | 工具 |

`settled_at` 为空当且仅当状态是 `pending` 或 `sending`，由表约束保证。

**和设计稿的出入：**

- 设计稿用 `session_key` 表示「回到哪个会话」。会话键的格式由会话路由的 RFC 定，现在还没有，所以这一版直接存投递目标（`channel_id` + `chat`），不加空的 `session_key` 列。会话路由定下来以后，再用一个增量迁移加列并回填。
- 设计稿的 `created_turn` 拆成 `created_channel` 和 `created_message_id` 两列，都是类型化字段。
- 状态在设计稿的四种（待投递、已投递、已错过、已取消）之外多了 `sending`、`failed`、`uncertain`：渠道拒收和结果不明是两种不同的事实，混进「已投递」或「已错过」会丢信息。

## 4. 工具

三个工具都是 `RestrictedTool`，`audience` 是 `operator`：只有用户本人的回合（Dashboard 的聊天，不带 API 客户端；或 bot 操作者本人的私聊）能列出和调用。群聊、别人、API 客户端、Claude 自己的回合调用一律 `forbidden`，提示「只能在用户本人的私聊或 Dashboard 里用」。开关关掉以后再调用，答「这个 Manager 已经没有这个工具」。

| 工具 | 做什么 | 级别 |
|---|---|---|
| `looper_reminder_create` | `text`（用户的原话）加上 `at`（`HH:MM`：下一个这个时刻，今天或明天；或 `YYYY-MM-DD HH:MM`）和 `timezone`（IANA，默认服务器所在时区），或者 `in_minutes`。时间由代码解析，取这个钟点在时区里最近的、还没到的那个时刻：夏令时结束那天一个钟点有两次，取还没过的第一次；夏令时开始那天被跳过的钟点，改为跳过之后的第一分钟。这两种情况都在确认里说明（「注意：11-01 01:30 在 America/Los_Angeles 有两次（夏令时结束），记的是第二次（PST）。」「注意：03-14 02:30 在 America/Los_Angeles 不存在（夏令时开始），改为 03-14 03:00。」），答复里也带 `clock_change`。已经过去、超过一年或格式不对的拒绝，并说明该怎么改。存进表以后按 id 读回来再返回，Looper 在回复后面加一行代码写的确认：「已记下提醒 #3：今天 11-20（周五）15:00（America/Los_Angeles），到时发到这个私聊：看 PR」 | write |
| `looper_reminder_list` | 现在的时间，加上待投递的提醒（最近的在前），或指定状态的（`all`：全部，最新的在前），每条有 id、原话、到点时间和时区、状态、原因 | read |
| `looper_reminder_cancel` | 按 id 取消一条待投递的提醒；不是 `pending` 的答 `conflict`，没有的答 `not_found`。Looper 在回复后面加一行「已取消提醒 #3（原定 ……）：……」 | write |

**送到哪里：**

- 在私聊里设的：回到这个私聊，也就是这个 bot 上用户本人的 id（飞书的 open_id 按用户发送，Telegram 的用户 id 就是私聊）。
- 在 Dashboard 设的：送到默认 Manager 的 bot 里第一个已启用、且设置了 `operator_sender_id` 的那个（内置的 bot 在前）的私聊；确认行里写明「到时发到你的 Lark 私聊」。一个都没有就拒绝（`conflict`），提示在 Settings → Channels 里设置操作者，或者到私聊里说。

提醒只发给用户本人的私聊，从不发到群里。

## 5. 投递

`ReminderService` 每 10 秒一次，在一个会话级 advisory lock 下处理（两个 Looper 同时运行时只有一个在发）。服务正在排空（热升级）时不发，由接替的进程发。停服务时不再开始下一条，整个停止最多 15 秒：正在发的那条先等它发完，到最后一段还没发完就取消，取消的清理也算在这 15 秒里。所以正常重启不会把提醒变成 `uncertain`，卡住的发送也拖不住停机。

每一次：

1. **收拾半途停下的：** 还在 `sending` 的，说明上一个发送者在问渠道的途中停了（持锁的才发，所以持锁时看到的 `sending` 一定不是活的）：记为 `uncertain`，不再发。
2. **决定发哪些**（纯函数 `plan`）：取 `due_at <= now` 的 `pending`：
   - 到点不超过 2 分钟的照常发，同一时刻到点的几条都发（2 分钟是 12 次检查，重启、热升级交接、慢一拍都在里面）；
   - 到点超过 2 分钟的算错过（Looper 停机、睡眠、卡住、开关关着或在排空）。按投递目标分组，每组只发最新的一条，前提是它到点不超过 36 小时，而且这一次没有按时的提醒发往同一个私聊（按时那条更新）；其余记为 `missed`，原因写明是超过 36 小时，还是有更新的一条发了。例如同一个私聊里有 60、9、1 分钟前到点的三条，只发 1 分钟前的那条。
   - 这条规则不存任何状态，只看到点时间和现在：服务启动、睡眠后恢复、事件循环卡住、两个实例交替拿锁，判断都一样。
3. **发送：** 先确认这个 bot 的 `operator_sender_id` 还是这个人，不是就记 `failed`，不发；再认领（`pending` → `sending`，提交），然后 `ChannelRegistry.post`，幂等键 `looper-reminder-<id>`（飞书在一小时内同一个键只发一次）。结果：
   - 发出：`delivered`，记下消息 id；
   - 渠道明确拒收：放回 `pending`，下一次再试，第 3 次还拒收就 `failed`；
   - 结果不明：`uncertain`，不再发。

**文字由代码拼：**

```
⏰ 你在 11-20 14:03 让我提醒你：看 PR
```

晚了 2 分钟以上发出的（也就是补发的那条），下面再加一行「（原定 11-20 15:00，Looper 没能按时发出，现在补发。）」。

**不受节制：** 提醒是用户自己要的，不受免打扰时段、每日上限、合并窗口的限制，不进摘要。
`delivery.enabled` 打开时，认领后、渠道发送前通过送达层作 `user_reminder` / `user_set` 判断，
结果为 `send` / `urgent` / 不计数；以 `looper-reminder-<id>` 为键、提醒 id 为 `source_ref`，
在同一条 `proactive_deliveries` 记录里保存判断和渠道回执（`sent`、`rejected` 或 `unknown`）。
拒收重试沿用第一次判断。判断失败时记日志，仍按关闭时的路径发送，不能扣下用户要的提醒；
回执写入失败也只记日志，不把已接收或结果不明的发送变成重试。
关闭时不判断、不写送达记录，`ChannelRegistry.post` 的参数、消息正文及提醒状态转换保持原样。

## 6. 入口

- **Dashboard 聊天、飞书和 Telegram 私聊：** 通过默认 Manager 的三个工具设置、查看、取消；提醒发到用户的飞书或 Telegram 私聊。
- **群聊：** 不能设提醒，也收不到提醒。
- **命令行：** 不变，这一版没有 `looper reminders` 命令。

## 7. 验证

- `tests/test_reminders.py`：时间解析（今天、明天、具体日期、已过去、超过一年、格式错、未知时区；夏令时结束时一个钟点有两次取最近的未来那次，夏令时开始时被跳过的钟点改到之后第一分钟）、显示格式、提醒文字、`plan` 的表驱动用例；
- `tests/test_reminders_db.py`：开关关闭时逐字节不变、打开后只多三个工具；只有默认 Manager、只在用户本人的回合；私聊和 Dashboard 的投递目标；列出和取消；到点只发一次、重启不丢；停服务时正在发的那条发完、不开始下一条，发送卡住时停止也不超过宽限时间；重启后和运行中停顿 61 分钟后，同一私聊 60、9、1 分钟前到点的三条都只发 1 分钟前那条；2 分钟内到点的几条都发；升级时两个实例交替拿锁，刚到点的两条都发；确认里说明夏令时对时间的调整；发送途中停下不再发；拒收重试三次、结果不明不重试；操作者换人不发；关闭或排空时不发；两个服务同时跑只发一次；「三点提醒我看 PR」从 Manager 到渠道的完整路径；旧库升级；表约束；
- 真实模型评测：`evals/prompt_regression` 的 `reminder_tools` 对话（设一条明天下午三点的提醒，再取消）。
- 送达层接线（`tests/test_reminders_db.py`）：普通消息在夜间或满额时进摘要作为正向对照，用户提醒照发且记紧急、不计数的判断和回执；拒收重试复用判断，未知结果不重发；判断/回执写入异常不丢提醒或重复发送；默认关闭、显式关闭及再次关闭时逐项冻结渠道参数、正文和既有记录。
