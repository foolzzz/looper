# RFC：工作日晨报 v0（`morning_brief`）

- **状态：** 草案，随本 PR 合入。默认关闭。
- **需求：** 工作日早上，在操作者的时区，把等他处理的（Needs you 汇总）、今天到期的提醒、昨夜被送达规则推迟的主动消息合成一条私聊；事件分流打开时追加进摘要的事件。所有节都空就不发。同一天只发一次，重启和两个进程都不会再发。
- **相关：** [主动消息的送达层 RFC](proactive-delivery-v0.zh-CN.md)（`deliver()`、`digest_date`）；[需要你处理 RFC](assistant-needs-you-v0.zh-CN.md)（汇总）；[一次性提醒 RFC](reminders-v0.zh-CN.md)。
- **不做：** 昨天完成的任务、异常和承诺；节假日；卡片按钮；由模型改写晨报；把被推迟消息的原文存进 `proactive_deliveries`（那张表没有正文，晨报只写出种类、原因、时间和关联 id）。

## 1. 什么时候发

- 开关 `morning_brief.enabled`（默认 `false`）和时间 `morning_brief.time`（`HH:MM`，默认 `09:15`）。只有操作者能读写，经 `morning_brief.get` 和 `morning_brief.update`（高风险，要确认）。不进客户端看得到的 `chat_settings`，Manager 的工具里也没有。
- 时区是 `user.timezone`，由每轮上下文保管（`turn_context.zone()`，和送达层同一个）；没有或不认识时用服务器时区。晨报不另存一份时区。
- 工作日是周一到周五，没有节假日：周一到周五的假日照发。本地时间到达设定的时分就发；当天晚些时候才醒来，这一天仍发一次。钟拨过那个分钟时，缺口后的第一次检查补发；同一个本地日期重复的那一小时只发一次（行的键是本地日期）。
- 周末不发，也不写行。

## 2. 内容

一条私聊，代码写，不调用模型。标题是 `晨报 · MM-DD（周X）`。原有三节；打开事件分流后可追加第四节，空的不出现：

1. **等你处理的：** Needs you 的 `summary_text`。`attention.enabled` 只关页面和 `/todo`，晨报照样读汇总。汇总说「现在没有需要你处理的事」时这一节不出现。读失败或读不到不算空，照写进晨报，避免把没读到说成没有。
2. **今天的提醒：** 这条私聊里 `pending`、`due_at` 落在当地今天的提醒，按时间排序。提醒服务周末也会发，所以这里只看今天，不补周末。
3. **推迟到这份晨报的消息：** `proactive_deliveries` 里 `decision = digest` 的行（`source` 是发送点）。读取范围始终是首次尝试那天的摘要日期（周一同时收进周六和周日）一直到今天：首次尝试在周二被拒绝后，周三的晨报仍包含周二的摘要。认领时就把这次实际读到的行的 id 写进晨报行，和认领同一次提交。发出或记为不确定时，把这组 id 记进 `morning_brief_consumed`，和晨报那一行同一次事务提交；下一份仍用这个日期范围，但跳过这些 id。发送方在记下结果前退出时，重启后的恢复只消费认领时存下的那组 id，不按 `decided_at` 重扫。这样自定义时间早于免打扰结束时，晨报发出后才被按住的消息（例如周二 07:00 发出，07:30 的消息落在周二的摘要）会进以后的晨报，而不是被丢掉，也不会重复。检查当时还没提交的摘要行不在这些 id 里：生产者在发送之后才提交、进程又在结算前退出，次日的晨报仍会读到它，且只读一次。`consumed_until` 只记下这次检查的时间，不是水位。拒绝不记 id，并清掉认领上暂存的 id，重试仍读这些摘要。不收 `source = morning_brief` 自己的行，也不收由事件段呈现的 `source = triage` 行。表里没有正文，晨报只写出种类、原因、时间和关联 id。

4. **进摘要的事件：** 仅在 `subscriptions.triage.enabled` 打开且目标等于管家的 `operator_dm_target()` 时追加；最新 10 条拥有窗口消息的 `digest` 或重要兜底事件，超出显示数量。按晨报同一日期窗读取；`triage_ids` 随认领保存，`morning_brief_triage` 随发送成功或不确定结算消费；拒绝不消费，恢复只消费已存 id。见[事件分流 RFC 第 6.2 节](event-triage-v0.zh-CN.md#62-进摘要晨报)。

每个开着的、配置了 `operator_sender_id` 的 Telegram / Lark 机器人各发一条，按 `(本地日期, channel_id, chat)` 幂等。一个都没有时写一行空的 `channel_id` 和 `chat`，不调用渠道。

## 3. 送达层

晨报走 `deliver()`。`silent`（所有节都空）是 `drop`，不发。有内容时 `source` 是 `morning_brief`：在静默规则之后、紧急类之前命中，结果是 `decision = send`、`reason = within_limits`、不计数，免打扰时段和每日上限都不能把它再推进摘要。

`decide()` 只在 `delivery.enabled` 开着时把这次判断写进那条消息的 `proactive_deliveries` 行。去重键 `morning_brief:<本地日期>:<channel_id>:<chat>` 也是这次 `post` 的幂等键。发出后 `record_receipt()` 把渠道回执写回同一行（`outcome`、`sent_at`、`external_id`）。同一键上先前的 `rejected` 回执会被这次的结果替换。已经记下的 `sent` 或 `unknown`，这三个字段保持不动。送达层关着时晨报照发，那张表不变。

已经建过 `proactive_deliveries` 的库不会重放建表语句，所以另有一次迁移把 `source` 检查放宽到包含 `morning_brief`。

## 4. 一天一次

表 `morning_briefs`，主键 `(local_date, channel_id, chat)`，没有自增列。迁移名 `morning_briefs`。

- `sending`：认领后、询问渠道前提交。持有会话咨询锁 `(741926, 1)` 的进程才认领，所以两个进程不会各发一次。
- `sent`：渠道收下了。
- `skipped`：没什么要说，或没有操作者的私聊。
- `uncertain`：发送方在记下结果前停了，或渠道的答复丢了。可能已经发出，不再发。
- `failed`：渠道拒绝。`attempts` 小于 3 时下次检查再试；第 3 次仍拒绝就留下。

检查间隔 30 秒。开关关闭时每次只读内存里的开关，不调用 `deliver`，不写库，不发消息。认领之后、发出之前再查一次开关和操作者；查询操作者返回后再读一次开关。这次等待期间关掉开关：全新认领删掉 `sending` 行，当天稍后重新打开仍会发；接着一次拒绝的认领恢复成原来的失败行和尝试次数，下一天仍从首次尝试那天读摘要。

## 5. 动作级别

`morning_brief.get` 和 `morning_brief.update` 都是 `inside`：只读或只写 Looper 自己的设置，操作本身不往会话里发任何东西。晨报的发出是服务在到点时代发到操作者的私聊，不是 Agent 的工具。
