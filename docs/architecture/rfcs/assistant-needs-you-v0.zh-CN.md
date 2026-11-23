# RFC：「需要你处理」汇总（v0）

- **状态：** 已实现，默认关闭（系统设置 `attention.enabled`）。
- **相关：** [agent-system.md 的 What needs you](../agent-system.md#what-needs-you)；[账本](manager-completion-ledger-v0.zh-CN.md)；[User Gates 页](gates-page-v0.zh-CN.md)；[docs/design.md 的界面设计](../../design.md#界面设计web)。
- **最近更新：** 2026-11-20

## 1. 背景与目标

等用户的事散在各处：关卡在任务页和 User Gates 页，失败的定时任务在 Dashboard 的 Schedules 卡片，意图在 Intents 页，账本只有命令行，管家的高风险确认只在当时那条聊天里，可能没送到的消息只在各自的表里。用户要一处一处看，聊天里更没法问。

**目标：** 一个只读的汇总，由代码从已有的表和内存状态里查出来：不调模型，不另存一份。三个入口里先做两个：Dashboard 一页（`#todo`），和对话里的 `/todo`。

**不做：** 飞书卡片和每条的按钮（等卡片按钮那一项）；晨报（以后直接读这个汇总）；一次性提醒（还没有这张表，见第 2 节）；任何写操作（处理仍在各自的地方：关卡页、确认的那条聊天、意图页、账本）。

## 2. 决定

**D1 分区和来源。** 每个分区只读一个已有的来源，规则和那个来源的页面一致：

| 分区 | 来源 | 列什么 |
|---|---|---|
| `gates` | `gate_service.list_gates`（`waiting_on=user`） | 等你回复的关卡，和 User Gates 页的「Waiting for you only」同一个规则 |
| `confirmations` | `ManagerOps.waiting_confirmations()`（内存） | 每个管家等你一句「确认」的操作：已经问了、还没有哪一轮在处理它、没过 `CONFIRMATION_SECONDS` |
| `schedules` | `db.schedules.failing()` | 连续失败或有告警的定时任务，和 Dashboard 的 Schedules 卡片同一个查询（从 `overview` 里抽出来） |
| `logins` | 启动自检加上现在读的登录检查（`system/status` 的同一个读取，HTTP 服务运行时登记给汇总） | Claude Code CLI 或登录、Agent Profile 用到的运行时登录处于 `fail` |
| `intents` | `intents.state = 'pending'` | 待建或待驳回的意图；意图关闭时整个分区不出现 |
| `ledger` | 账本自己的条目（`source_kind IS NULL`） | 你的待办、还没确认的转述决定、已经过期的你的事；账本关闭时不出现。来自关卡和确认的条目不重复列 |
| `deliveries` | `subscription_deliveries`、`intent_posts`、`manager_events` 的 `uncertain` | 最近 72 小时可能没送到的消息（这种消息不会再发，所以要人看一眼）；一次订阅推送分几段的算一条。三张表各有一个只含 `uncertain` 行的部分索引（迁移 `uncertain_deliveries`，已有的库在提交后在线建），读取不扫历史 |

**D2 确认读内存，不读 `manager_actions`。** 确认今天只存在 `ManagerOps` 的内存里：下一条消息那一轮没确认就作废，重启就丢。表里的 `needs_confirmation` 行不知道这些，按它列会在作废后还挂十分钟。所以读内存，`ManagerOps` 加一个只读的 `waiting_confirmations()`，`_Pending` 记下展示给用户的 `impact`。确认落库是卡片按钮那一项的事，那之后改读表。

**D3 分区的状态是类型，不是空列表。** 每个分区是 `ok`（读到了，可能为空）、`error`（来源读失败，带原因，绝不显示成空）或 `unknown`（这个调用方读不到：没有管家列表时的 `confirmations`，没有状态读取时的 `logins`）。功能关着的分区（意图、账本）直接不出现。每个分区最多列 20 条，`count` 是全部（查询只取 20 条，另算总数）。只有每个分区都读到且为空时，页面和 `/todo` 才说「没有需要你处理的事」；有读不到的，就说读到的里面没有，并列出没读到的。

**D4 开关。** `attention.enabled` 是系统设置，没有这一行就是关，只有 `true` 才开；和账本一样，这一版没有界面开关。关着时：`attention.summary` 回答 `conflict`；侧栏没有这一项；`/todo` 是一条普通消息，照旧交给模型；Telegram 照旧丢掉这个未知命令。任何提示词、工具列表和命令行都不变（有测试）。

**D5 接口。** `POST /api/v1/attention/summary`，只读，只给 operator（汇总里有意图和确认，API 客户端本来看不到）。回答 `{read_at, total, sections: [{kind, state, count, items, error}]}`；每条是 `{key, title, context, since, href, ref, facts}`：`href` 是 Dashboard 上的位置，`ref` 是聊天里叫它的名字（`I-0042`、`L-0012`），`facts` 是这一类自己的字段，网页和聊天各自排成文字。

**D6 `/todo`。** 由代码在管家里回答，和 `/new`、`/gates` 一样不经过模型；只在你本人的回合里（`operator_identity.is_operator`：控制台聊天且没有 API 客户端，或者机器人配置的「My ID」发来的私聊），群聊、别人、API 客户端都回一句拒绝。回复不进模型，但会存进这个聊天的记录，新开的管家会话会把最近的记录读回来；所以别人写的文字（关卡主题、意图和账本标题、任务、项目和定时任务的名字）都经过同一个函数，变成一行纯文本：去掉控制和格式字符，合并换行，最多 120 个字符。回复按分区列出有内容的和读失败的，每类附上处理的方式（`/gates`、回复「确认」、`建 I-0042`）；什么都没有就说「现在没有需要你处理的事」。Telegram 加了 `/todo` 命令，开着时原样转给管家。

**D7 页面 `#todo`。** 侧栏 Dashboard 下面的「Needs you」，只在汇总能读时出现（挂载时读一次，409 就不显示）。每个有内容或读失败的分区一张卡片：标题和条数、一句怎么处理、每条一行（标题链接到它的页面，下一行是所属和这一类的事实，右边是多久了）；读失败的分区用 `role="alert"` 写明原因。空的分区合成一行「Nothing in: …」，读不到的合成一行「Not known on this server: …」。读到的失败登录不在页面上列：每一页顶部的横幅读的是同一组检查，页面再列一遍就重复了（`/todo` 里照列，聊天里没有横幅）；登录分区读失败或读不到时照常显示。在控制台聊天里问的确认，链接带上原来的会话（`#chat?conversation=…`）。每 15 秒刷新一次。

## 3. 验收

- 每个分区有一个测试：从真实的表（或管家的内存、状态读取）读出该列的、不列该排除的；读失败的分区是 `error` 而不是空。
- `/todo`：本人得到汇总，模型没有被调用；群聊、别人、API 客户端被拒绝；开关关着时交给模型。
- 接口：关着回答 409，API 客户端回答 403，开着时回答符合声明的结构。
- 页面：有数据、读失败、关闭、出错重试几种状态；1280 和 390 宽度截图。

## 4. 以后

- 一次性提醒落地后加 `reminders` 分区（已错过的提醒）。
- 卡片按钮那一项把高风险确认落库后，`confirmations` 改读那张表，不再读 `ManagerOps` 的内存，只留一个来源；飞书卡片每条带按钮。
- 晨报读这个汇总（见晨报 RFC）。汇总说没有需要处理的事时，晨报不把它算成一节；读失败或读不到不算空。
