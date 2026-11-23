# RFC：操作者档案编译 v0（`profile_blocks`、`manager.profile.source`）

- **状态：** 随实现 PR 一起评审。默认关闭。
- **需求：** 用代码把操作者指定的笔记目录编译成一份有类型、有出处、有长度上限的档案。档案存在 Looper 里，笔记仍是权威；PostgreSQL 里的是可以重建的缓存。
- **相关：** [Agent 系统架构](../agent-system.md) 的 Manager 一节；出站审阅 RFC 第 4 节（操作级别）。

## 1. 范围

做：

- 纯函数编译器（`looper/agents/operator_profile.py`）：不调模型，不读时钟；
- 表 `profile_blocks`（迁移 `profile_blocks`，只有这一张单行表）；
- 操作者专用的三个 v1 操作：`profile.get`、`profile_source.get`、`profile_source.update`。

不做：

- 把档案注入 Manager 的稳定块、系统提示词或工具列表；
- 主会话看全文、群会话只看名字和称呼；
- Dashboard 上查看档案和来源；
- 用真实模型做的评测；
- 单独的布尔开关。

`loopercli` 从 OpenAPI 生成命令，所以这三个操作会成为操作者专用命令 `profile get`、`profile-source get`、`profile-source update`。Dashboard、Telegram、Lark 这一版没有新的入口。

## 2. 开关

系统设置 `manager.profile.source` 存一个绝对目录。没有这一行就是关闭。

关闭时：

- 不编译，不写 `profile_blocks`；
- Manager 的工具列表、系统提示词、命令行、回合记录和渠道调用与没有这个功能时相同。工具列表的 sha256 由测试钉住，不钉整段提示词。

打开时（设置了目录）也只有读档案会编译。编译结果不进入任何提示词。`profile_source.update` 只把目录写进 `system_settings`，不修改那个目录里的文件；`null` 在同一个事务里删掉设置和缓存。

已经存进去的值如果不是绝对目录、目录不存在或不是目录，`profile.get` 直接失败，不会把它当成关闭。`profile_source.get` 仍返回存着的原文，方便清掉。

源目录还要落在已配置的知识库里面。`profile_source.update` 在写设置之前（确认文案也用这一次检查），以及 `profile.get` 在编译之前，都会把目录 `resolve`（跟随符号链接）然后要求结果位于 `knowledge_vault` 的解析路径之内。知识库没配置或不成立时原因是 `vault_unconfigured`；解析后跑到知识库外面时原因是 `outside_vault`。两种都失败，不会当成关闭。词法上在知识库外、解析后在里面的符号链接可以保存，存下来的是解析后的路径。

## 3. 编译

只读两类笔记：

- 目录根上的 `关于我.md`，或者 `记忆/关于我.md`。两个都在就失败。都可以不在，这时只编译偏好笔记。
- `偏好/` 下面的 `*.md`。路径里任一段以 `.` 或 `_` 开头的跳过（所以 `_index.md` 不进档案，也不记成跳过）。不递归这个目录以外的笔记。`偏好` 存在但不是目录则失败。符号链接解析后必须仍在源目录里面。

关于我只用二级标题，标题文字区分大小写：

| 标题 | 字段 |
|---|---|
| 名字 / Name | `name` |
| 称呼 / Address | `address` |
| 回复语言 / Reply language | `reply_language` |
| 时区 / Time zone | `time_zone` |
| 工作时间 / Working hours | `working_hours` |
| 沟通偏好 / Communication | `communication_preferences` |

同一个字段有两个标题就失败。不认识的二级标题忽略。三级标题留在二级标题的正文里。空白节不产生字段。

名字、称呼、回复语言、时区、工作时间各只有一个值。同一个字段出现第二个值（例如二级标题下有两条 bullet）时整份档案编译失败，原因是 `multiple_values`。文本和结构化字段都不会只留最后一条。沟通偏好和长期规则保留每一条。

笔记里的条目是一行一个 markdown bullet，行内用 Dataview 字段。关于我和 `偏好/` 下的笔记都认这一种，例如：

```text
- 回复用中文 [kind:: preference] [source:: 对话 2026-11-20] [confidence:: high]
```

`kind` 决定字段，区分大小写：

| kind | 字段 |
|---|---|
| preference、habit | `communication_preferences` |
| fact、decision、commitment、request、deadline、person | `standing_rules` |

每条的出处是笔记相对路径、行号，以及它前面最近的标题；没有标题时标题用 `kind`。`source`、`confidence`、`until` 留在笔记里，不复制进档案。没写 `status` 视为 `active`；`superseded` 和 `deleted` 不进入档案，也不记入 `skipped`。不认识的 `kind` 记入 `skipped`，原因是 `unknown kind`。编译器不读时钟，所以 `until` 不参与取舍。

一篇笔记只要有这样的条目，就用这些条目：不要求 frontmatter 的 `summary`，`summary` 也不再另算一条规则。没有条目时才用下面的旧写法。

偏好笔记的旧写法：有非空的二级标题时，每一节是一条长期规则，不用 frontmatter 的 `summary`。否则，`summary` 非空并且正文里有一个 ATX 标题时，`summary` 是一条规则，出处标题用第一个 ATX 标题。对不上的记入 `skipped`（`no heading to cite` 或 `no summary`），空笔记不记。`summary` 只能是单行；重复、未闭合的 frontmatter、`|` / `>` 块标量都失败。

代码围栏不参与编译。行首最多三个空格、接着至少三个反引号或波浪号，算开围栏；同样字符、至少一样长、后面没有信息字符串的一行算关围栏。围栏里面的标题和示例条目都忽略。没闭合的围栏一直忽略到笔记末尾。写进档案的文字把反引号换成撇号，把连续三个及以上的波浪号换成 `---`，输出里因此没有围栏边界。

渲染按固定优先级（名字、称呼、回复语言、时区、工作时间、沟通偏好、长期规则）。同一字段里保持笔记里的顺序；偏好笔记先按相对路径排序。超过 1500 个字符（Unicode 码位，和 PostgreSQL 的 `char_length` 一致）时从优先级最低的字段往前丢，丢到只剩一条仍超长就截断那一条并加上 `…`，该字段标 `truncated`。上限比一条字段标签还短时失败。

每个值的出处是相对路径、标题和行号，不是绝对路径。单篇笔记超过 256 KiB、不是 UTF-8、范围内笔记超过 200 篇，都失败。

## 4. 表

```sql
CREATE TABLE IF NOT EXISTS profile_blocks (
    id           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    version      BIGINT NOT NULL CHECK (version >= 1),
    text         TEXT NOT NULL CHECK (char_length(text) <= 1500),
    body         JSONB NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    compiled_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`body` 是 `{text, fields, omitted, skipped, sources}`。`profile.get` 在编译结果和已存的 `text`、`body` 一致时不写，版本和 `compiled_at` 都不变；变了才把版本加一（第一行是 1）。换一个源目录不会立刻删旧行，下一次读到不同的编译结果才换版本。

`sources` 是编译出字段的笔记（含因超长被丢掉的），不含只被跳过的笔记。

## 5. 操作

三个都是 `inside`（出站审阅 RFC 第 4.1 节）：效果只落在 Looper 自己的状态上。`profile.get` 读操作者点名的笔记，把结果存进 Looper，不向外发送。两个 source 操作只读写 `system_settings` 里的路径，不改那个目录。因此它们不进入需要逐个决定级别的名单，只写在操作级别清单里。

| 操作 | 风险 | 做什么 |
|---|---|---|
| `profile.get` | read | 未设置时返回空档案，不读盘、不写表。已设置时编译并按上一节决定是否换版本 |
| `profile_source.get` | read | 返回 `{source}`，未设置是 null |
| `profile_source.update` | high，需确认 | `{source}` 为知识库内的绝对目录或 null。确认文案说明只保存目录，不注入 Manager |

Agent 的 token 调用这三个操作得到 403。它们不出现在 `chat_settings` 的视图里，也不出现在 Manager 的 `looper_settings` 工具里。

## 6. 和设计稿的出入

- 设计稿把笔记的 `summary` 拼成一段最多 1500 字的文本。这一版改成有类型的字段，每条带来源（路径、标题和行号）。超长时按字段优先级丢弃，而不是按 summary 的拼接顺序截。
- 设计稿的开关是 `manager.profile_block.enabled`。这一版不另加布尔值：源目录未设置就是关闭。注入档案的 PR 如果需要独立开关，再加。
- 设计稿在输入变化时重编译。这一版没有笔记索引，`profile.get` 时重编译；内容没变就不换版本。这次写入是刷新缓存，不是注入 Manager。
- 设计稿里群会话只拿名字和称呼。那是注入时的过滤，不属于这一版。
