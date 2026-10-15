# Telegram Bot 对接管家（Manager）方案 v0

> 状态：P0 已实现（本 PR）；P1–P4 为后续分期。
> 日期：2026-10-14。

> **本 PR（P0）落地**：Telegram bot token 改为入库 + 掩码（`channel_secrets`，见 §1.5）；`setMyCommands` 发布 slash 菜单；correlation→chat 映射加上限；Settings → Channels 的 token 改为 password 掩码输入。其余（统一落库+控制台渠道标签、命令体验、确认按钮、健壮性边界）按 §6 分期推进。

## 0. 一句话结论

**Telegram 适配器已经存在并接好了**（`looper/agents/channels/telegram.py`，长轮询，已有 `/start /status /help /new`、allowlist、DB 配置、从环境变量取 token），只是从未开开关、没真机验证。因此本方案不是「从零对接」，而是四件事：**(1) 打开并真机验证基础通信 → (2) 把 slash 命令菜单化并映射到管家能力 → (3) 手机端一键确认体验（inline 按钮）→ (4) 补齐已知健壮性缺口**。

> 仓库里 `loopx/agents/channels/` 是空死目录（只剩陈旧 pycache），真实包是 `looper/`。

## 1. 现状能力（已实现，可直接用）

| 能力 | 现状 | 说明 |
|---|---|---|
| 收发消息 | ✅ | 纯文本 DM → 管家；管家回复 → 原 chat。长轮询 `getUpdates`。 |
| 身份路由 | ✅ | 以 `correlation_id → chat_id` 映射回传；会话上下文用 `channel_context.chat_id`。 |
| 命令 | 部分 | `/start`（帮助）、`/help`（帮助）、`/status`（转发自然语言「当前任务进度怎么样？」）、`/new`（转发字面 `/new`，管家据此**重置会话**）。 |
| 鉴权 | ✅ | `allowed_user_ids` 白名单（纯数字 TG user id，上限 100）；名单空或非法则该 channel **不启动**；非名单发送者无任何回复。 |
| 密钥 | ⚠️ 需改 | **当前是遗留做法**：config 只存环境变量名 `bot_token_env`，serve 从环境读 token。与项目统一的「入库+掩码」secret 模式不一致，**必须改为 token 入库**（见 §1.5）。 |
| 配置入口 | ✅ | Settings → Channels（`channel_configs` 表，JSONB），改完**需重启** serve 生效。 |
| 长消息 | ✅ | 超 4096 字自动分片；MarkdownV2 失败回退纯文本。 |

## 1.5 Token 存储：入库 + 掩码（按项目统一 secret 模式）

项目已有两套「值入库、API 只出名字/存在性」的 secret 范式，telegram token 必须对齐，**不走环境变量**：

- `llm_provider_secrets(provider_id, api_key)`：独立表，API 从不返回 `api_key`（只返回是否已设）；serve 内部按需读取。
- MCP server env/header 值：存 JSONB，API 只出 key 名（`mcp_env_keys`/`mcp_header_keys`），值仅由加载它的代码读取。
- schema 原则（注释原文）：secret 的**值留在库里，只有名字/存在性经 API 出去**。

**采用 `llm_provider_secrets` 同款做法**（telegram 只有「bot token」一个 secret，最贴合）。**必须用独立表,不能放 config、也不建议加列**：`get_config`/`list_configs` 是 `SELECT *`，`channel_view` 又把整行（含整个 `config` JSONB）原样 spread 返回；token 落在 config 或 channel_configs 列都会经 API 泄漏，除非另写掩码逻辑。独立侧表从结构上就让明文不进任何 list/get 路径。

1. **新表**（复刻 `llm_provider_secrets`，`schema.sql` 876 之后）：
   ```sql
   CREATE TABLE IF NOT EXISTS channel_secrets (
       channel_id TEXT PRIMARY KEY REFERENCES channel_configs(channel_id) ON DELETE CASCADE,
       bot_token  TEXT NOT NULL
   );
   ```
2. **DB 层**（`channel_configs.py`）：加 `_set_bot_token`（复刻 `_set_secret`：None 删、否则 upsert）、`get_bot_token`（复刻 `get_api_key`）；`get_config`/`list_configs` 改 `LEFT JOIN channel_secrets` 带出 `has_bot_token`（类比 `api_key_set`）。`set_config`/`_UPDATABLE_FIELDS` 不动——token 不走 config。
3. **写 + 哨兵**（`http_server.py` `_handle_update_channel_config`）：复刻 provider 的 `SECRET_MASK="***"` 语义——body 里 `bot_token` 缺省=保持、`"***"`=保持、`""`/null=清除、其它=写入；在 `set_config` 前 pop 出来单独写 `channel_secrets`，不进 config。复用现有 PUT，无新路由。
4. **读(API)**：GET/list 只出 `has_bot_token`；`channel_view` 可再出 `bot_token: SECRET_MASK if has_bot_token else null`（类比 `provider_view`）。**绝不返回明文**。
5. **用(serve)**：`_telegram_adapter_from_config` 调用点先 `await get_bot_token("channel-telegram")` 传入 `bot_token=`；`seed_channel_configs` 删掉 `bot_token_env` 种子键。env 回退（telegram.py:53 的 `LOOPER_TELEGRAM_BOT_TOKEN`）**建议保留为最后兜底**，但不再是主路径。
6. **UI**（`channel-settings.tsx` + `api.ts`）：`ChannelConfig` 加 `has_bot_token?`、update 入参允许 `bot_token?`；Telegram 卡片把原 `_env` 文本框换成 **`password` 输入**，已设时填掩码、送 `***` 保持、清空删除——与 provider 的 api_key 编辑器一致。

> **config_transfer 不受影响**：token 存在 `channel_secrets`、**不在 `config`**，而 `config_transfer` 只读 `config` → 它永远碰不到 token。原来 token 本就放在环境变量、从不随 transfer 传值，故这不是回归；`bot_token_env` 作为无用的 env 名残留是无害的，保持 `_CHANNEL_PUBLIC_KEYS` 不动。把 token 作为可迁移 secret 随 transfer 传（像 provider 的 api_key）是**新能力**，留作 follow-up。
> **测试**：`_telegram_adapter_from_config` 保留 env 兜底，故现有 channel/env/transfer 测试全部不破；仅**新增** `channel_secrets` 存/掩码/保持/清除的 DB 测试、serve 读库 token 测试、映射上限测试。

## 2. 关键约束（设计必须围绕它们）

1. **管家是全局单会话**：所有 Telegram 用户 + Web 共享**同一个** Claude 会话上下文（`agent_sessions` 的 `manager` 行）。
   - 好处：手机(TG) 与电脑(Web) 聊的是**同一个管家**，上下文连续。
   - 代价：无多用户隔离；群聊会污染共享上下文；TG 发 `/new` 会重置**所有渠道**的会话。
   - **v1 定位：单运营者、仅私聊(DM)**（与现有白名单单用户取向一致）。多用户/按会话隔离属管家侧改动，另立 RFC。
2. **管家是自然语言优先，确认时产出结构化 JSON**：建任务要先澄清，用户**明确确认**（确认/开始/go/yes/好/可以/proceed）后管家才产出 `task_request` JSON；启动就绪任务产出 `task_start`。适配器唯一能直接触发的结构化命令是 `/new`。
   - **推论**：Telegram 的 slash 动作命令**不能新造总线消息类型**，只能翻译成管家已懂的自然语言 / 确认词，经 `USER_MESSAGE.payload.text` 送达（`/status` 已是这么做的）。
3. **Telegram 不支持流式**：适配器已丢弃 `STATUS_UPDATE` 增量，只发最终 `AGENT_REPLY` / `CLARIFICATION_REQUEST` 文本。手机端拿到的是最终答复，非逐字流。

## 3. 命令设计（本方案核心）

把命令分成**两类**，避免无谓烧 token、并给确定性体验：

### A. 本地命令（适配器直接处理，不经 Claude）
确定性、免费、快。读数据直查 HTTP/DB。

| 命令 | 行为 | 落地 |
|---|---|---|
| `/start` | 欢迎 + 一句话用法（直接发消息即可跟管家对话） | 已有，润色文案 |
| `/help` | 命令清单 + 说明 `/new` 会重置全局会话 | 已有，补全 |
| `/new` | 管家重置会话（全局，提示用户影响范围） | 已有 |
| `/tasks` | 列出任务及状态（in_progress/queued/ready/…），可带项目过滤 | **新增**，直查任务数据 |
| `/projects` | 列出项目及任务数 | **新增**，直查项目数据 |
| `/status` | 改为**本地直查**当前 in_progress/queued 任务摘要（确定性、省 token）；复杂追问仍可用自然语言 | 调整（当前是 NL 转发） |

### B. 对话命令（翻译成自然语言转发给管家）

| 输入 | 转发文本 | 说明 |
|---|---|---|
| 纯文本 / `/ask <内容>` | 原文 | 核心通路，已有 |
| inline 按钮「✅ 确认」 | `确认` | 见 §4 |
| inline 按钮「❌ 取消」 | `取消` | 见 §4 |

> `/cancel`（停止任务）单独处理：管家侧**没有** task_stop 的 NL/JSON 动作，停止走 HTTP `POST /api/tasks/stop`（需 task_id）。建议做成本地命令：`/tasks` 列出→点按钮停止。标注为 P1 可选，需二次确认交互设计。

### C. 发布命令菜单
现适配器**从未调用** `set_my_commands`，TG 客户端输入框不显示 `/` 菜单。在 `start()` 里 `_app.initialize()` 之后调一次 `bot.set_my_commands([...])`，把上面 A 类命令发布出去。

## 4. 确认体验 —— 重评后的结论（原 inline 按钮方案不成立）

**代码实证**：管家**从不发 `CLARIFICATION_REQUEST`**（该枚举只在定义和 channel 的 `_format_reply` 里出现，manager.py 从不产出）。所有澄清/确认/「现在开始吗」都是普通 `AGENT_REPLY{"text": ...}`，**没有任何 typed 字段/flag/metadata 表示「正在等确认」**。确认与否由 **Claude 读自然语言**判定（关键词 `确认/开始/go/yes/好/可以/proceed`、拒绝 `不/取消/算了/no/cancel` 写在系统提示里），**不是** Python 字符串匹配。

因此「收到 `CLARIFICATION_REQUEST` 就挂确认按钮」**无触发点**。两条路：

- **v1（推荐，零风险，走现有能力）**：**不做 inline 按钮**。用户收到管家的「确认创建吗 / 现在开始吗」文本后，直接回复 `确认` / `开始`（自然语言，已端到端可用）。在 `/help` 和管家提示里讲清楚即可。手机打六个字成本可接受。
- **v2（可选，需小改管家，典型 typed-state 修法）**：给管家 `_send_reply` 的 `AGENT_REPLY` payload 加一个 typed 提示字段（如 `awaiting: "confirm" | "start" | "project"`），由管家在进入等待确认的分支时写入。然后 **Telegram 和 Web 都**按这个 typed flag 渲染「✅确认/❌取消」按钮；Telegram 加 `CallbackQueryHandler`，点击把 `确认`/`开始`/`取消` 作为普通 `USER_MESSAGE.text` 发回。
  - 这是正确修法（typed 状态而非子串猜测），且同时惠及 Web。**但它改的是共享组件 manager.py**，属跨界改动，单独评审。
  - **坚决不做**：用文本模式去 grep「现在开始吗」来挂按钮——正是应避免的子串启发式。

> 结论：v1 先用自然语言确认跑通，inline 按钮留到 v2 并以「管家加 typed 字段」为前提。

## 5. 已知缺口（随上面分期补）

1. `set_my_commands` 未调用 → 无菜单（P0 补）。
2. `correlation_id → chat_id` 映射**无上限、不清理**，会无限增长（`healthcheck` 还把它当 active_conversations）。Web 侧用上限 1000 的 `OrderedDict` 解决；Telegram 照做（P0）。
3. MarkdownV2 **未转义**，靠发送失败回退纯文本——粗糙。做正确转义（P3）。
4. 群聊 / 负 chat_id：白名单只认正数(DM)，`_admits` 只查 `effective_user.id`。v1 **明确拒绝群聊**并提示私聊（P3）。
5. Webhook 模式仅文档提过、未实现。长轮询对单运营者够用；webhook 作为可选（P3，需在 HTTP server 加入站路由）。

## 5.5 共享 Manager 模型（按你的设计）+ 为满足它要补的

**定调**：三渠道共享一个 Manager、一个 session，是**设计意图**，不是问题。管家本就**逐条串行**处理消息（拥挤就排队），同渠道回复已实现。基于此，有两件事要补齐 + 一个小修：

### 要求 A：控制台看到所有消息，且消息记录渠道（真缺口）
现状：
- `chat_messages`（控制台 Chat 页数据源，`list_sessions`/`list_messages`）**无 channel 列**，且**只有 Web 写**；Telegram 直接走总线**不落库** → 控制台看不到 TG 消息。
- 渠道只记在 **task/plan**（`origin_channel_id`），**对话消息本身不带渠道**。

方案（推荐：**在 Manager 集中落库**，因为它是唯一串行点，天然覆盖所有渠道）：
1. `chat_messages` **加 `channel TEXT NOT NULL DEFAULT 'web'`**（幂等 `ADD COLUMN IF NOT EXISTS`）；非 Web 会话用稳定 `session_id = f"{channel}:{chat_id}"`（如 `telegram:12345`）。
2. 管家在入站 `USER_MESSAGE`（经 `_message_origin` 得 (channel, chat)）与出站最终 `AGENT_REPLY` 两处，统一 `save_message(..., channel=...)`。Web 现有 HTTP 落库路径收敛到这里（单一真源），避免 web 双写。
3. 控制台 Chat 页：会话列表显示**渠道标签/过滤**；消息带渠道来源。满足「看到所有消息 + 记录渠道」。
> 范围判断：这是本需求的**主要工作量**，独立于「打开 TG」。可先 P1 落地 TG+Web 两渠道，Lark 复用。

### 要求 B：同渠道回复（已满足，保留）
`_send_reply` 回 `original.source`，TG 按 `correlation_id → chat_id` 回原 chat。无需改。
> 配套小修：`_correlation_chat` 无上限，按 Web 的 1000 上限 `OrderedDict` 淘汰最旧（自主回复复用的是最近 correlation，淘汰老的安全）。

### 小修：让共享 Manager 不串对话状态
管家 `self._followup` 是**单个全局槽**，别的 chat 插话会把你这个 chat 的待确认 followup 弹掉丢弃。改成 **per-(channel,chat) dict**（每个会话各存各的），正好贴合「共享 manager 但各渠道/会话状态独立」。小改 manager.py，低风险。

### 已知边界（非 v1 目标）
- **主动推送到指定 chat（已具备）**：`ChannelRegistry.notify(channel_type, chat, text)` 是现成的出站原语（定时任务通知已在用）。任务完成通知即建于其上（`looper/agents/task_notice.py`）：任务记录 `chat_id`（随 TASK_REQUEST 入库，取 notify 要的形式：Telegram 的 chat id、飞书发送者的 open_id），完成/失败时 TaskRouter 在回收 Planner 之后后台用 `registry.notify` 向它发一条通知；Manager 不再把任务的终态转发给 Telegram/飞书，结束只说一次；Web 跳过（看面板）。
- `/new` 重置的是**全局** session（任意渠道发都重置所有）——符合「共享一个 session」的语义；在 `/help` 里说明即可。

## 6. 分期与改动文件

- **P0 — Token 入库 + 打开即用 + 必要补丁**：
  - 码（token 入库，见 §1.5）：建 `channel_secrets` 表；`channel_configs.py` 读写 secret + 返回 `has_bot_token`；`serve.py` 改读库（保留 env 兜底）、seed 去 `bot_token_env`；`http_server.py` PUT 接 `bot_token`（`***` 哨兵）/ GET 掩码；`channel-settings.tsx` + `api.ts` token password 输入。config_transfer 不动（token 不在 config）。
  - 码（补丁）：`telegram.py` 加 `set_my_commands`、给 `correlation→chat_id` 映射加上限。
  - 运行时：BotFather 建 bot → Settings/Channels 填 Bot token + `allowed_user_ids` + 启用 → 重启 serve。
  - 验证：DM 纯文本问答、`/new` 重置；确认 token 不经 API 泄漏（GET 只见 `has_bot_token`）。
- **P1 — 统一落库 + 控制台看所有消息 + 记录渠道**（你的核心要求，见 §5.5-A/B/小修）：
  - `schema.sql`：`chat_messages` 加 `channel` 列；`chat.py` `save_message` 带 channel、非 Web 用 `"{channel}:{chat_id}"` session。
  - `manager.py`：入站/出站两处集中 `save_message`（Web 收敛到此）；`_followup` 改 per-(channel,chat) dict。
  - Web 控制台：Chat 会话列表加**渠道标签/过滤**。
  - 验证：TG 发消息后控制台可见且标注 telegram；Web/TG 并发互不串确认。
- **P2 — 命令体验**：`telegram.py` 新增本地 `/tasks /projects`、`/status` 改直查、润色 `/start /help`；复用 `looper/db` 查询。
- **P3 — 确认按钮（v2，需先给管家加 typed `awaiting` 字段）**：`manager.py` 加 typed 字段；`telegram.py` 加 `CallbackQueryHandler` 据此附 inline 按钮；Web 同享。
- **P4 — 健壮性/边界**：MarkdownV2 转义、群聊拒绝、（可选）webhook。
- **文档/UI**：`docs/architecture/agent-system.md` 的 Channels 段、`web/src/components/channel-settings.tsx` 文案按需同步。

**非目标**：多用户/按 chat 隔离的管家（管家侧改动，另立 RFC）；Lark（下一篇，协议不同，复用本抽象但单独设计）。

## 7. 测试与验证

- **单测**（扩 `tests/test_agent_channels.py`）：命令→转发文本映射；`CLARIFICATION_REQUEST` → 附按钮；callback → `确认`/`取消`；correlation 映射上限淘汰；非白名单发送者被拒；群聊拒绝。
- **真机**：用一次性 runtime-root、非 8767 端口跑 worktree 版 serve（按 AGENTS.md，别碰运营者的 ~/.looper）；用自己的测试 bot + 自己的 user id；跑通：纯文本问答、`/new`、`/tasks`、建任务→按钮确认→任务起跑。
- **缺口如实报告**：无法真机时说明，不把「未验证」说成「可用」。

## 8. 决策记录（已拍板）

设计共识：**三渠道共享一个 Manager / 一个 session / 串行排队**是意图；同渠道回复已满足；控制台要看到所有消息并记录渠道（P1 补齐）。

1. **统一落库落点**：**集中在 Manager**（唯一串行点，Web 收敛为单一真源）。
2. **Token 入库**：采用 §1.5 `channel_secrets` 独立表方案；token 本就不随 config_transfer 传值，故 transfer 不动；env 回退**保留作兜底**。（P0 已实现）
3. **确认按钮**：v1 走自然语言确认（已可用）；inline 按钮留 **v2**，前提是先给管家 `AGENT_REPLY` 加 typed `awaiting` 字段。
4. **命令集**：本地 `/tasks /projects /status` + 自由对话；`/cancel` 放 P2；群聊 v1 直接拒绝（仅 DM）。
5. **分期**：P0 token入库+打开 → P1 落库+控制台+渠道 → P2 命令 → P3 按钮 → P4 边界。
