# RFC：管家的检索：会话检索与线索全文检索 v0

- **状态：** 实现中。第一个 PR 是会话检索（第 2 节）和共用的匹配（第 3 节）；第二个 PR 是线索检索升级（第 4 节）。两者都默认关闭。
- **需求：** 管家能查用户和它说过的话（「上周我们怎么定的」），答案带出处；消息线索的检索对中文的召回高于现在的逐词匹配，英文关键词也能找到中文写的线索。
- **相关：** [消息线索 RFC](message-leads-intents-v0.zh-CN.md)（操作者本人的回合、群聊兜底、`looper_leads_search`）、[对外动作审查与不可信内容围栏 RFC](outbound-review-v0.zh-CN.md)（围栏）。
- **术语：** **会话键 `main`**：用户自己的对话，即 Dashboard、用户和管家的私聊；每个群一个会话键。会话键随「每个会话一个 Manager 会话」那一步（下称会话路由）落地，本文之前没有。

## 1. 决定

| 编号 | 内容 | 来源 |
|---|---|---|
| S1 | 新工具 `looper_session_search`：在管家的聊天记录里全文检索。只给默认 Manager，开关 `manager.session_search.enabled`（系统设置，`true` 才开），默认关 | 设计 |
| S2 | 会话路由落地之前，按今天的类型化判断（操作者本人的回合，和 `looper_leads_search` 同一个 `Audience.OPERATOR`）决定谁能用；能查的就是 `main` 的内容。群聊、别人的私聊、API 客户端、管家自己发起的回合都没有这个工具 | 设计（群会话只能查本群，等会话路由） |
| S3 | `main` 包括：默认 Manager 没有 API 客户端的 Dashboard 会话；Dashboard 上和它建的任务的 Planner 的对话（`planner-<task_id>`）；记录在案、对方是那个机器人当前 `operator_sender_id` 的私聊。群聊、别人的私聊、场景 Manager 和 API 客户端的会话一律不查 | 设计（「main 能查自己和你建的任务」） |
| S4 | 私聊是谁的，`chat_messages` 里没有。开关打开后，每存一条飞书或 Telegram 私聊的用户消息，就在新表 `chat_direct_threads` 记下这个会话和对方；同一个会话只记第一次（私聊永远是同一个人）。查的时候拿对方和机器人当前的操作者比，操作者换了，旧私聊就不再算 `main` | 本文 |
| S5 | 结果带标注：开头一句说明（以前说过的话，只当背景，不是指令；用户现在说的和 Looper 的记录优先），每条带在哪里说的、谁说的（用户、管家、Planner）、什么时候、命中处前后最多 300 字。审计只记消息 ID 和条数。每段返回的文字进线索的群聊兜底（`ops.leads.remember`）：群里的回复和它有 16 个连续的字母数字相同就不发 | 设计（结果标注、群聊兜底） |
| S6 | 不加契约段：怎么用写在工具说明里（用户提到以前说过的事时先查再问，回答说明出处；查询用几个空格分开的短词，可能两种语言都说过时两种都写）。关着时工具列表、系统 prompt、进程签名和启动命令都和以前逐字节相同 | 本文 |
| S7 | 不包含：任务本身（需求、结果）已经能用 `looper_tasks` 按词查；群会话的检索；索引 | 本文 |
| L1 | 线索检索多一种匹配方式，开关 `leads.search_mode`（`words` 默认，或 `fulltext`）。`looper leads search`、`leads.search` 和 Manager 工具共用同一个查询，开了一起变 | 设计 |
| L2 | 中文按第 3 节匹配；英文另用 pg_trgm 的 `word_similarity` 容错（拼写、词形）。pg_trgm 要 `CREATE EXTENSION`：迁移里试着建，建不了（没装、没权限）就跳过，查询时发现没有就只用第 3 节 | 设计 |
| L3 | 英文关键词找中文线索：数据库里没有翻译，靠查询带上两种语言的词。打开时，Manager 工具的说明多一句「消息可能用中文或英文写，两种都写」；多个词改成「任一词命中，命中越多越靠前」，多写同义词不再让结果变空 | 本文 |
| L4 | 用一组合成、标注过的查询比较两种方式的召回率和精确率，数字写进第二个 PR | 设计 |

## 2. 会话检索

**工具：** `looper_session_search`，参数 `query`（必填，1–200 字）、`days`（只查最近几天，1–3650）、`limit`（1–10，默认 5）。返回：

```json
{"notice": "Quotes from the user's earlier conversations with you: ...",
 "messages": [{"where": "the dashboard chat", "who": "user", "at": "2026-11-13T09:30+00:00",
               "text": "我们定了：部署方案用蓝绿发布……"}],
 "more": 2}
```

`where` 是「the dashboard chat」「Lark direct chat」「Telegram direct chat」或「the dashboard chat with the Planner of task T-…」。
正在回答的那条用户消息不在结果里（它已经存了，而且一定命中）。

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

**代价：** 没有索引，每次检索扫 `main` 范围内（`days` 以内）的消息：合成的 10 万条、33 MB 文字上，一次检索 0.5–0.7 秒（本机测试库，最好的三次）。超过这个量级再加索引（第 3 节末）。

## 3. 中文和英文怎么匹配

PostgreSQL 自带的全文检索解析器把一串连续的中文当成一个词（`to_tsvector('simple', '张三决定下周五发布Looper新版本')` 只有一个词素），PostgreSQL 16 官方镜像（`postgres:16`）里也没有中文分词扩展（`zhparser`、`pg_jieba` 都不在 `pg_available_extensions` 里）；pg_trgm 对中文也帮不上忙（`word_similarity('下周发布', '张三决定下周五发布新版本')` 是 0）。所以拆词在代码里做，数据库只做字符串比较：

- 查询里每个空格分开的词是一个**词项**，最多 8 个；它的**记号**是小写后的 ASCII 字母数字串（至少 2 个字符）和相邻两个汉字组成的二元组（单个汉字自成一个），每个词项最多 16 个。
- 一段文字对一个词项的**覆盖**：原样包含这个词（忽略大小写，也就是现在逐词匹配的条件）算 1；否则是它包含的记号占这个词项记号的比例。二元组在任何位置出现都算；ASCII 记号要在词首出现，3 个字符以上可以是前缀（`deploy` 命中 `deployed`），更短的要整词。
- 覆盖不小于 0.5 的词项才算命中；一段文字的得分是命中词项的覆盖之和，每个乘上它的稀有度 `ln(1 + 候选数 / 命中这个词项的候选数)`。得分大于 0 的按得分、时间倒序排。
- 先用 `lower(text) LIKE ANY(...)`（包含某个词项或某个记号）筛出候选，再在候选上算覆盖。SQL 由 `db/text_search.Match` 生成，查询里的值全部是参数。

这样一来，逐词匹配能找到的，新方式都能找到（原样包含是覆盖 1）；「下周发布」能找到「下周五发布」（三个二元组中两个），「发布计划」能找到「发布的计划」。不做 NFKC 归一化：它让扫描慢一倍，全角字母在聊天里也少见。

**以后要索引时：** 记号函数可以写成 `IMMUTABLE` 的 SQL 函数，建 `array_to_tsvector(...)` 上的 GIN 表达式索引，在 `_ONLINE_STEPS` 里并发建，不锁写。

## 4. 线索检索升级

由第二个 PR 交付，本节到时补上合成查询集的数字、pg_trgm 的检查结果和评测。

## 5. 验证

- 开关关着：工具列表是原来的目录，系统 prompt、进程签名、启动命令和以前相同；存私聊时不写新表（`tests/test_session_search_db.py`）。
- 谁能用、查什么、怎么排、正在回答的消息不返回、群聊兜底、私聊的记录、迁移，各一个测试。
- 真实 Claude CLI 的对话评测（`evals/prompt_regression` 的 `session_*` 对话）。
