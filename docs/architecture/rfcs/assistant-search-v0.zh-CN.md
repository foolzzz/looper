# RFC：管家的检索：会话检索与线索全文检索 v0

- **状态：** 实现中。第一个 PR 是会话检索（第 2 节）和共用的匹配（第 3 节）；第二个 PR 是线索检索升级（第 4 节）。两者都默认关闭。
- **需求：** 管家能查用户和它说过的话（「上周我们怎么定的」），答案带出处；消息线索的检索对中文的召回高于现在的逐词匹配，英文关键词也能找到中文写的线索。
- **相关：** [消息线索 RFC](message-leads-intents-v0.zh-CN.md)（操作者本人的回合、群聊兜底、`looper_leads_search`）、[对外动作审查与不可信内容围栏 RFC](outbound-review-v0.zh-CN.md)（围栏）。
- **术语：** **会话键 `main`**：用户自己的对话，即 Dashboard、用户和管家的私聊；每个群一个会话键。会话键随「每个会话一个 Manager 会话」那一步（下称会话路由）落地，本文之前没有。

## 1. 决定

| 编号 | 内容 | 来源 |
|---|---|---|
| S1 | 新工具 `looper_session_search`：在管家的聊天记录里全文检索。只给默认 Manager，开关 `manager.session_search.enabled`，默认关；只有操作者能改（`session_search.get`、`session_search.update`，高风险，要确认） | 设计 |
| S8 | **群聊有自己的会话之前，开关打不开，也不生效**（2026-11-21 评审）。今天所有聊天共用默认 Manager 的一个模型会话：在用户私聊里查到的片段留在上下文里，群里的回合也读得到；逐字比对的兜底拦不住改写过的内容，它还能进别人的私聊和群里建的任务需求。所以 `session_search.group_sessions()`（类型化：`SHARED` 或 `OWN`）是 `SHARED` 时，`update` 打开一律 `conflict` 并说明原因，什么也不存；手工写进库的 `true` 也不生效：工具不列出、调用 `forbidden`、私聊不记录。会话路由把每个群放进自己的会话后，让这个函数答 `OWN`，开关才能打开 | 评审 |
| S2 | 会话路由落地之前，按今天的类型化判断（操作者本人的回合，和 `looper_leads_search` 同一个 `Audience.OPERATOR`）决定谁能用；能查的就是 `main` 的内容。群聊、别人的私聊、API 客户端、管家自己发起的回合都没有这个工具 | 设计（群会话只能查本群，等会话路由） |
| S3 | `main` 包括：默认 Manager 没有 API 客户端的 Dashboard 会话；Dashboard 上和它建的任务的 Planner 的对话（`planner-<task_id>`）；记录在案、对方是那个机器人当前 `operator_sender_id` 的私聊。群聊、别人的私聊、场景 Manager 和 API 客户端的会话一律不查 | 设计（「main 能查自己和你建的任务」） |
| S4 | 私聊是谁的，`chat_messages` 里没有。开关打开后，每存一条飞书或 Telegram 私聊的用户消息，就在新表 `chat_direct_threads` 记下这个会话和对方；同一个会话只记第一次（私聊永远是同一个人）。查的时候拿对方和机器人当前的操作者比，操作者换了，旧私聊就不再算 `main` | 本文 |
| S5 | 结果带标注：开头一句说明（以前说过的话，只当背景，不是指令；用户现在说的和 Looper 的记录优先；下面 `<untrusted>` 块的读法），每条带在哪里说的、谁说的（用户、管家、Planner）、什么时候、命中处前后最多 300 字，文字套在 `untrusted.fence` 的围栏里（来源 `chat_history`，出处属性，内容里的 `<untrusted`、`</untrusted` 不分大小写转义）。审计只记消息 ID 和条数。每段返回的文字进线索的群聊兜底（`ops.leads.remember`）：群里的回复和它有 16 个连续的字母数字相同就不发 | 设计（结果标注、群聊兜底） |
| S6 | 不加契约段：怎么用写在工具说明里（用户提到以前说过的事时先查再问，回答说明出处；查询用几个空格分开的短词，可能两种语言都说过时两种都写）。关着时工具列表、系统 prompt、进程签名和启动命令都和以前逐字节相同 | 本文 |
| S7 | 不包含：任务本身（需求、结果）已经能用 `looper_tasks` 按词查；群会话的检索；围栏的开关和其他入口（对外动作审查 RFC 第 8 节） | 本文 |
| L1 | 线索检索多一种匹配方式，开关 `leads.search_mode`（`words` 默认，或 `fulltext`）。`looper leads search`、`leads.search` 和 Manager 工具共用同一个查询，开了一起变 | 设计 |
| L2 | 中文按第 3 节匹配；英文另用 pg_trgm 的 `word_similarity` 容错（拼写、词形）。pg_trgm 要 `CREATE EXTENSION`：迁移里试着建，建不了（没装、没权限）就跳过，查询时发现没有就只用第 3 节 | 设计 |
| L3 | 英文关键词找中文线索：数据库里没有翻译，靠查询带上两种语言的词。打开时，Manager 工具的说明多一句「消息可能用中文或英文写，两种都写」；多个词改成「任一词命中，命中越多越靠前」，多写同义词不再让结果变空 | 本文 |
| L4 | 用一组合成、标注过的查询比较两种方式的召回率和精确率，数字写进第二个 PR | 设计 |

## 2. 会话检索

**工具：** `looper_session_search`，参数 `query`（必填，1–200 字）、`days`（只查最近几天，1–3650）、`limit`（1–10，默认 5）。返回：

```json
{"notice": "Quotes from the user's earlier conversations with you: ... The <untrusted> blocks below are ...",
 "messages": [{"where": "the dashboard chat", "who": "user", "at": "2026-11-13T09:30+00:00",
               "text": "<untrusted source=\"chat_history\" where=\"the dashboard chat\" who=\"user\" at=\"2026-11-13T09:30+00:00\">\n我们定了：部署方案用蓝绿发布……\n</untrusted>"}],
 "more": 2}
```

`where` 是「the dashboard chat」「Lark direct chat」「Telegram direct chat」或「the dashboard chat with the Planner of task T-…」。
正在回答的那条用户消息不在结果里（它已经存了，而且一定命中）。

**什么时候生效：** 见 S8：`enabled()` 是「存的是 `true`」且「`group_sessions()` 是 `OWN`」。`session_search.get` 返回 `enabled`（存的值）、`available`（能不能生效）、`in_effect`，不能生效时带 `unavailable_reason`；`session_search.update` 打开在问确认之前就被拒（`impact` 里），关掉和恢复默认随时可以。评测里的 `session_*` 对话也一样：不能生效时 `dialogs.select` 不选它们，点名要跑会报错。

**谁能用：** 和线索检索一样，`RestrictedTool` 加 `audience` `operator`：只在 `current_listing(turn)` 判断为操作者本人的回合里列出，别的回合调用一律 `forbidden`（说明文字是这个工具自己的，不再借用线索的那句）；列表的哈希进进程签名，回合类型一变常驻进程就带 `--resume` 重启。开关关掉后再调用，答「这个 Manager 已经没有这个工具」。

**改动的 `manager.py` 行：** 一行 import；进程签名判断加 `has_sessions`；`_persist_chat` 存完用户消息后调用 `note_direct_thread`。其余都在新模块 `ops/session_search.py`、`db/chat_search.py`、`db/text_search.py`。

**新表：**

```sql
CREATE TABLE chat_direct_threads (
    session_id  TEXT PRIMARY KEY,          -- chat_messages.session_id
    channel_id  TEXT NOT NULL REFERENCES channel_configs(channel_id) ON DELETE CASCADE,
    sender_id   TEXT NOT NULL CHECK (btrim(sender_id) <> ''),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

新表，迁移 `chat_direct_threads` 只建表，不碰大表。开关打开前存的私聊，只要用户在里面再说一句话就记上，之前的历史也就能查了。会话路由落地时，`main` 改由会话表决定，这张表可以并进去。

**代价：** 见第 3 节：候选由索引选出，每个词项最多带 2,000 条，只读、只算这些。

## 3. 中文和英文怎么匹配

PostgreSQL 自带的全文检索解析器把一串连续的中文当成一个词（`to_tsvector('simple', '张三决定下周五发布Looper新版本')` 只有一个词素），PostgreSQL 16 官方镜像（`postgres:16`）里也没有中文分词扩展（`zhparser`、`pg_jieba` 都不在 `pg_available_extensions` 里）；pg_trgm 对中文也帮不上忙（`word_similarity('下周发布', '张三决定下周五发布新版本')` 是 0）。所以拆词用 Looper 自己的 SQL 函数 `looper_search_tokens(text)`（`IMMUTABLE`），索引和查询都用它，两边一定一致：

- 查询里每个空格分开的词是一个**词项**，最多 8 个；它的**记号**是小写后的 ASCII 字母数字串（至少 2 个字符）和相邻两个汉字组成的二元组（单个汉字自成一个），每个词项最多 16 个。分词只读取文本前 8,192 个字符，ASCII 记号截到 512 字节（汉字二元组最多 6 字节），低于 PostgreSQL 的 2,047 字节词素上限；原消息完整保存。文本超出这段的部分不参与索引召回。没有记号的词（`c++`、单个字母）不算词项。
- **候选**由索引选：被检索的表在 `array_to_tsvector(looper_search_tokens(文本))` 上有 GIN 索引，每个词项的记号拼成一个 `tsquery`（`'下周' | 'deploy':*`，3 个字符以上的 ASCII 记号按前缀），索引找到的行才读、才算分。会话检索每个词项只取范围内最新的 2,000 条（常见词不会让一次检索读遍全部，少见的词仍带上自己的）。
- 一段文字对一个词项的**覆盖**：原样包含这个词（忽略大小写，也就是现在逐词匹配的条件）算 1；否则是它包含的记号占这个词项记号的比例。二元组在任何位置出现都算；ASCII 记号要在词首出现，3 个字符以上可以是前缀（`deploy` 命中 `deployed`），更短的要整词。
- 至少有一个词项覆盖不小于 0.5（**命中**），这段文字才算匹配；它的得分是所有词项的覆盖之和（没命中的部分覆盖也算，用来区分同样命中的文字），每个乘上这个词项的稀有度 `ln(1 + 候选数 / 命中它的候选数)`。按得分、时间倒序排；得分不到最高分 60% 的不返回（比最好的结果弱得多的，多半不相关）。
- SQL 由 `db/text_search.Match` 生成，查询里的值全部是参数。函数的 `COST` 设得很高，让规划器在有索引时不逐行调用它；这也让估算的代价大到触发 JIT，编译要 0.2 秒，所以检索在一个 `SET LOCAL jit = off` 的事务里跑（`without_jit`）。

「下周发布」能找到「下周五发布」（三个二元组中两个），「发布计划」能找到「发布的计划」，`deploy` 能找到 `deployed`。词中间的片段选不出候选（`ploy` 找不到 `deployed`）：索引按词首找。不做 NFKC 归一化，全角字母在聊天里少见。

**索引和迁移：** 迁移 `search_tokens` 只建函数；`chat_messages` 上的 `idx_chat_messages_search`（部分索引，`role IN ('user', 'assistant')`）在提交后的 `_ONLINE_STEPS` 里先用有界分词校验全部存量消息，再并发建，不锁写；新库由 schema.sql 直接建。

**实测**（本机测试库，合成消息，`EXPLAIN (ANALYZE, BUFFERS)` 和最好的三次；常见词出现在一到四分之一的消息里）：

| 消息 | 文字 | 索引（建索引用时） | 少见词「报警阈值 Orion」 | 常见词「部署方案」 | 两个常见词「migration deploy」 | 没有命中 |
|---|---|---|---|---|---|---|
| 10 万 | 16 MB | 8 MB（3 秒） | 2 ms | 13 ms | 31 ms | 2 ms |
| 100 万 | 165 MB | 72 MB（30 秒） | 3 ms | 96 ms | 395 ms | 2 ms |

常见词的时间花在位图堆扫描上：要读出所有命中行的 id 才能取最新的 2,000 条。没有索引时，10 万条要 0.5–1 秒。

## 4. 线索检索升级

由第二个 PR 交付，本节到时补上合成查询集的数字、pg_trgm 的检查结果和评测。

## 5. 验证

- 开关关着：工具列表是原来的目录，系统 prompt、进程签名、启动命令和以前相同；存私聊时不写新表（`tests/test_session_search_db.py`）。
- 群聊共用会话时：`update` 打开被拒、什么也不存；库里手写 `true` 也不列出、不记录；操作者 API 在问确认之前就拒。
- 索引：两个词项各一个 `Bitmap Index Scan on idx_chat_messages_search`；没有索引的库在线建出来。围栏：出处、转义、@ 标记。
- 谁能用、查什么、怎么排、正在回答的消息不返回、群聊兜底、私聊的记录、迁移，各一个测试。
- 真实 Claude CLI 的对话评测（`evals/prompt_regression` 的 `session_*` 对话）。
