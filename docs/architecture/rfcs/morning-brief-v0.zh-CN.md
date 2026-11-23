# RFC：工作日晨报 v0（`morning_brief`）

- **状态：** 草案，随本 PR 合入。默认关闭。
- **需求：** 工作日早上，在操作者的时区，把三件事合成一条私聊发给操作者：等他处理的（Needs you 汇总）、今天到期的提醒、昨夜被送达规则推迟的主动消息。三节都空就不发。同一天只发一次，重启和两个进程都不会再发。
- **相关：** [主动消息的送达层 RFC](proactive-delivery-v0.zh-CN.md)（`deliver()`、`digest_date`）；[需要你处理 RFC](assistant-needs-you-v0.zh-CN.md)（汇总）；[一次性提醒 RFC](reminders-v0.zh-CN.md)。
- **不做：** 昨天完成的任务、异常和承诺；节假日；卡片按钮；由模型改写晨报；把被推迟消息的原文存进 `proactive_deliveries`（那张表没有正文，晨报只写出种类、原因、时间和关联 id）。

## 1. 什么时候发

- 开关 `morning_brief.enabled`（默认 `false`）和时间 `morning_brief.time`（`HH:MM`，默认 `09:15`）。只有操作者能读写，经 `morning_brief.get` 和 `morning_brief.update`（高风险，要确认）。不进客户端看得到的 `chat_settings`，Manager 的工具里也没有。
- 时区是 `user.timezone`，由每轮上下文保管（`turn_context.zone()`，和送达层同一个）；没有或不认识时用服务器时区。晨报不另存一份时区。
- 工作日是周一到周五，没有节假日：周一到周五的假日照发。本地时间到达设定的时分就发；当天晚些时候才醒来，这一天仍发一次。钟拨过那个分钟时，缺口后的第一次检查补发；同一个本地日期重复的那一小时只发一次（行的键是本地日期）。
- 周末不发，也不写行。

## 2. 内容

一条私聊，代码写，不调用模型。标题是 `晨报 · MM-DD（周X）`。三节，空的不出现：

1. **等你处理的：** Needs you 的 `summary_text`。`attention.enabled` 只关页面和 `/todo`，晨报照样读汇总。汇总说「现在没有需要你处理的事」时这一节不出现。读失败或读不到不算空，照写进晨报，避免把没读到说成没有。
2. **今天的提醒：** 这条私聊里 `pending`、`due_at` 落在当地今天的提醒，按时间排序。提醒服务周末也会发，所以这里只看今天，不补周末。
3. **推迟到这份晨报的消息：** `proactive_deliveries` 里 `decision = digest` 的行（`source` 是发送点）。这个会话还没有消费边界时，取 `digest_date` 是今天的，周一同时收进周六和周日，否则周五夜里推迟的（`digest_date` 是周六）会丢掉。一份晨报发出、跳过或记为不确定时，把这次检查的时间记在 `morning_briefs.consumed_until`；下一份只取 `decided_at` 晚于该边界、且不晚于这次检查的。这样自定义时间早于免打扰结束时，晨报发出后才被按住的消息（例如周二 07:00 发出，07:30 的消息落在周二的摘要）会进以后的晨报，而不是被丢掉。拒绝留下的行不记边界，重试仍读这些摘要。不收 `source = morning_brief` 自己的行。表里没有正文，晨报只写出种类、原因、时间和关联 id。

每个开着的、配置了 `operator_sender_id` 的 Telegram / Lark 机器人各发一条，按 `(本地日期, channel_id, chat)` 幂等。一个都没有时写一行空的 `channel_id` 和 `chat`，不调用渠道。

## 3. 送达层

晨报走 `deliver()`。`silent`（三节都空）是 `drop`，不发。有内容时 `source` 是 `morning_brief`：在静默规则之后、紧急类之前命中，结果是 `decision = send`、`reason = within_limits`、不计数，免打扰时段和每日上限都不能把它再推进摘要。

`decide()` 只在 `delivery.enabled` 开着时把这次判断写进那条消息的 `proactive_deliveries` 行。去重键 `morning_brief:<本地日期>:<channel_id>:<chat>` 也是这次 `post` 的幂等键。发出后 `record_receipt()` 把渠道回执写回同一行（`outcome`、`sent_at`、`external_id`）。送达层关着时晨报照发，那张表不变。

已经建过 `proactive_deliveries` 的库不会重放建表语句，所以另有一次迁移把 `source` 检查放宽到包含 `morning_brief`。

## 4. 一天一次

表 `morning_briefs`，主键 `(local_date, channel_id, chat)`，没有自增列。迁移名 `morning_briefs`。

- `sending`：认领后、询问渠道前提交。持有会话咨询锁 `(741926, 1)` 的进程才认领，所以两个进程不会各发一次。
- `sent`：渠道收下了。
- `skipped`：没什么要说，或没有操作者的私聊。
- `uncertain`：发送方在记下结果前停了，或渠道的答复丢了。可能已经发出，不再发。
- `failed`：渠道拒绝。`attempts` 小于 3 时下次检查再试；第 3 次仍拒绝就留下。

检查间隔 30 秒。开关关闭时每次只读内存里的开关，不调用 `deliver`，不写库，不发消息。

## 5. 动作级别

`morning_brief.get` 和 `morning_brief.update` 都是 `inside`：只读或只写 Looper 自己的设置，操作本身不往会话里发任何东西。晨报的发出是服务在到点时代发到操作者的私聊，不是 Agent 的工具。
