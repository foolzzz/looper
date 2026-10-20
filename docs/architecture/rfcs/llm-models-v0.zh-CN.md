# RFC：LLM Provider、Model 与 Agent Profile 分层（v0）

- **状态：** 设计稿，目标版本 v2.13.0；本文不表示实现、迁移或真实评测已完成。
- **依据：** [多运行时架构](multi-runtime-agents-v0.zh-CN.md)、[系统提示词模式](system-prompt-mode-v0.zh-CN.md)、[开发计划](../../plans/2026-10-13-long-horizon-agent-context.zh-CN.md)第 1、5 节。
- **代码基线：** v2.12.2（`95c399431`）。#251、#253 先在现有结构上作为补丁交付，再以补丁后的行为作为本次迁移基线。

## 1. 决定

1. **Provider 表示一个接入账户。** 它有名称、类型化的 vendor、凭据、每种协议各一个 endpoint，以及 provider 级并发上限和环境变量。vendor 给创建时的默认值，不在每次启动时覆盖已保存值。
2. **Model 表示一个可直接调用的模型入口。** 它属于一个 provider，有精确模型名、且只有一个 `ApiProtocol`，拥有上下文窗口、四种价格覆盖、支持的 efforts 和 background model。同一厂商模型经两个协议提供，就是两个 model，例如 `deepseek-flash/openai-chat` 与 `deepseek-flash[1m]/anthropic-messages`。名称不再负责推断协议，也不把 Claude Code 的后缀带到其他协议。
3. **Profile 表示运行 agent 的配方。** 它只引用 runtime 和 model，并保存 effort、agent teams、自定义提示词。系统提示词模式保持现有的按角色默认、任务级覆盖，不属于 profile。provider 从 model 得到，不再重复保存。配对必须同时满足协议、凭据类型、登录所属 runtime 和角色能力要求。
4. **迁移不纠正用户的既有配置。** 保留原模型字符串、实际协议、endpoint、环境、价格和运行时选项；有无法还原的配对就停止整个迁移，不能改用第一个模型、删掉 profile 或回退内置 profile。#251/#253 改进预设的结果原样迁入。
5. **账户合并只在可证明等价时做。** 相同 vendor、相同非空 key、兼容 endpoints 还不够；环境、计费与并发语义也必须一致。不能证明的保留为独立 provider，并说明原因。
6. **配置格式升级为 `looper-config/v3`。** 继续导入 v2，以及现有 v1；旧格式与数据库迁移共用同一个转换器。密钥存储、打码、占位符、Manager 不接收或设置 key 的规则不变。
7. **运行记录不改写历史。** `llm_records.model` 仍是模型名；新的运行增加 model id、协议和当次计价快照。历史费用、token、成本来源和按模型名汇总保持原值。
8. **一次停机切换，不双写两套模型。** 先 dry-run、备份，再在事务里转换；旧程序不能直接启动新 schema。Go 迁移与 eval harness 后续遵守本 RFC 的数据和解析契约。

### 1.1 方案取舍

| 方案 | 复杂度与维护 | 行为与故障半径 | 结论 |
| --- | --- | --- | --- |
| 继续扩展 provider 的 `protocols + models` JSON | 表少，但要给每个目录项增加协议例外、后缀规则 | 配对仍可能挑错协议；一个 provider 修改影响所有目录项 | 不采用 |
| Provider → 单协议 Model → Profile | 增加模型表和 endpoint 表，配对与外键直接表达关系 | 模型修改影响引用它的 profiles；事务内校验这些引用即可 | **采用** |
| 每个 model 自带账户、key 和 URL | 起步容易，重复保存凭据和并发额度 | key 轮换、多协议账户和并发限制容易分裂 | 不采用 |

本次只做分层、等价迁移和最小可用管理界面。自动路由、模型降级、自动发现远端模型和动态价格同步另行设计。

## 2. 已核实的现状与补丁衔接

当前事实来源：

| 位置 | 已有行为与本次落点 |
| --- | --- |
| `looper/agents/profile_registry.py` | `ProviderSpec` 含协议集合、单 URL、`CatalogModel[]`；空目录表示接受任意模型；`AgentProfile` 含 provider 与模型字符串。`check_pairing` 校验协议交集、auth、登录归属和目录 |
| `looper/agents/provider_presets.py` | 当前基线只有 GLM、DeepSeek、Kimi、CPA；预设保存 vendor 的默认 APIs 和目录，尚非持久化 vendor 类型 |
| `looper/agents/runtimes/__init__.py` | `ApiProtocol` 为 `anthropic-messages/openai-responses/openai-chat`，`AuthKind` 为 `oauth/api_key`；`ProviderBinding` 保存 args、秘密环境与 unset。`ProviderSpec` 实际定义在 registry，runtime 仅类型引用 |
| `runtimes/claude_code.py`、`codex.py`、`omp.py`、`grok.py` | Claude Code 使用 Messages；Codex、Grok 使用 Responses；OMP 支持三种协议，但目前按 Responses → Chat → Messages 选择第一个。Grok 目前只接受自己的登录，OMP 只接受 API key |
| `looper/db/schema.sql`、`looper/db/profile_registry.py` | `llm_providers.models` 是 JSONB；`llm_provider_secrets` 独立存 key；profile 的 `provider_id + model` 尚非模型外键 |
| `looper/agents/runtimes/pricing.py` | 精确目录项优先，再按去后缀/日期后的名字找第一个目录项，逐价格字段回退内置表；Claude 登录别名另有映射 |
| `looper/agents/config_transfer.py` | 导出 v2，引用按名称；导入按名称匹配、保留现有 id，支持 dry-run、secret placeholder 和一次事务应用 |
| `looper/agents/ops/config.py` | `looper_providers` 仅 list/register，无 key 参数；Profile 工具为 list/create/update/delete；删除 profile 走现有确认 |
| `web/src/components/provider-settings.tsx`、`agent-profiles.tsx` | provider 表单选择一个预设 API，模型目录也在 provider 内编辑；profile 为 runtime → provider → 字符串模型 |
| `evals/prompt_regression/runner.py` | register-agent spec 先注册 provider，再用 provider id 与 model 字符串建 profile；key 由指定环境变量读取 |

本地补丁分支已能看到 DeepSeek 的 Responses 接口及按 API 去除 `[1m]`，以及 OpenCode/Sub2API、按 API 分开的模型目录；它们不是本基线已发布的能力。实施前必须重新核对合入后的 `main` 和 PR 状态，不能把分支草稿当成最终补丁契约。

补丁交付必须留下可以复用的输入：每个 API 的协议、精确模型名（含 background 名）、context、env 与 URL。转换新 vendor 默认模板时使用**每个 API 自己的有效目录**，不能做「全 vendor 模型 × 所有协议」笛卡尔积：OpenCode 的 Claude、GPT、其他家族分别属于其 Messages、Responses、Chat 入口。实际数据库里用户保存的旧目录则按其原始声明和实际启动解析迁移，不用新预设偷偷替换。错误配置的修复属于先发补丁或迁移前显式编辑。

## 3. 数据模型

### 3.1 Provider 与 endpoint

保留 `llm_providers`，保留现有 id、name、builtin、时间戳、`max_concurrency`、`env`；新增 `vendor_type TEXT NOT NULL`。删除旧的 `protocols`、`base_url`、`models`，其信息分别由 endpoints 和 models 表表示。`llm_provider_secrets(provider_id, api_key)` 保持原结构和访问边界。

vendor 类型是代码中的枚举及元数据表，不按名称子串判断。v2.13.0 的完整集合为：

| `vendor_type` | 凭据 | endpoint 默认值来源 |
| --- | --- | --- |
| `glm-bigmodel`、`glm-zai` | API key | 对应 GLM 预设的每协议 URL |
| `deepseek` | API key | #251/#253 后对应 API 定义 |
| `kimi-moonshot-cn`、`kimi-moonshot` | API key | 对应 Kimi 预设；保留国内/国际区别 |
| `cpa`、`opencode`、`sub2api` | API key | 各自预设；自托管入口必须明确确认实际 URL |
| `claude-subscription` | Claude Code 自己的 CLI 登录 | 由 Claude CLI 管理 |
| `codex-login` | Codex 自己的 CLI 登录 | 由 Codex CLI 管理 |
| `grok-login` | Grok 自己的 CLI 登录 | 由 Grok CLI 管理 |
| `custom` | API key | 用户填写，每个使用的协议都必填 |

沿用 `auth`、`oauth_runtime_type` 的现有列与枚举语义，增加约束使它们与 vendor 一致：三个登录 vendor 的 `auth=oauth` 且分别绑定 `claude-code/codex/grok`；其他 vendor 为 `api_key`、`oauth_runtime_type=NULL`。vendor 不是可随意改动的展示标签；v0 创建后不可改类型，换账户类型要新建 provider 并显式改选模型。

新增 `llm_provider_endpoints`：

| 列 | 类型与约束 |
| --- | --- |
| `provider_id` | TEXT，FK → providers，删除 provider 时 CASCADE |
| `protocol` | TEXT，CHECK 为三个 `ApiProtocol` 之一 |
| `base_url` | TEXT，API-key endpoint 为非空 http(s) URL；沿用 URL 长度与控制字符校验 |
| `use_runtime_default` | BOOLEAN，默认 false；仅用于保留旧空 URL 的启动语义，见第 6 节 |
| 主键 | `(provider_id, protocol)`，同一账户每协议最多一个 URL |

API-key provider 可以先无模型，但创建时至少有一个有效 endpoint。已知 vendor 的默认 URL 写入数据库后可编辑；预设升级只影响新建，不能自动改变运行中的账户。custom 与自托管类型新增时不接受隐式 URL；预填的自托管示例也须用户确认。URL 不得承载 key、userinfo 或秘密查询参数；这类输入拒绝并引导使用独立 key 字段。

登录 provider 也登记所属协议，`base_url=''` 表示 CLI 自管，无 URL 覆盖能力的适配器拒绝写入非空 URL，不把改 URL 伪装成已生效。它们不复制 OAuth token，不改登录文件与链接检查。登录状态和清除 `login-problem` 的现有操作保留。

### 3.2 Model

新增 `llm_models`：

| 列 | 类型、默认值、约束 |
| --- | --- |
| `id` | TEXT PK，`model-` 前缀的不透明 id；新增用随机 UUID，迁移用固定命名空间下对旧 provider id、精确 name、protocol 的 UUIDv5，重跑不漂移；不包含 key |
| `provider_id, protocol` | NOT NULL，复合 FK → endpoints，ON DELETE RESTRICT |
| `name` | TEXT NOT NULL，精确传给 runtime 的模型名；沿用 `model_name` 校验 |
| `context_window` | INTEGER NULL，1–100,000,000；NULL 为未知 |
| 四种价格列 | DOUBLE PRECISION NULL，`input/output/cache_read/cache_write_usd_per_mtok`；有限数、0–10,000，NULL 回退内置价格，0 是显式免费 |
| `efforts` | TEXT[] NULL；NULL 未知、空数组明确不支持推理；沿用最多 10 项、去重与档位名称校验 |
| `background_model` | TEXT NULL，沿用模型名校验；NULL 表示模型自己 |
| `catalog_order` | INTEGER NOT NULL，非负；保留旧目录顺序，专供兼容计价匹配和稳定展示 |
| `builtin, created_at, updated_at` | 沿用 registry 的内置保护和时间戳 |
| 唯一约束 | `(provider_id, name, protocol)`；名称精确比较，允许同名不同协议、同名不同 provider |

background model 是同账户、同协议下传给 CLI 的辅助模型名，v0 不强制它必须另有 model 行，以保留现有目录外辅助模型。其价格可由同协议的另一 model 行提供；未知仍按第 5 节处理。支持 background 配置的适配器才应用它；不支持的继续保持已有行为，界面注明适用范围。

新模型必须指定一个 protocol，不接受列表；协议必须已有对应 provider endpoint。新 vendor 的建议模型也是完整的 `(name, protocol, settings)`，没有按家族名猜协议、运行时自动加减 `[1m]` 的逻辑。用户新增模型时先选协议，再输入精确名称；context_window 不自动重写名称。

### 3.3 Profile 与引用

`agent_profiles` 新增 `model_id TEXT NOT NULL REFERENCES llm_models(id) ON DELETE RESTRICT`，迁移完成后删除 `provider_id` 和旧 `model` 字符串。保留 `runtime_id`、id、name、builtin、effort、agent_teams、custom_prompt 和时间戳；所有层、任务、定时任务、项目可选列表、会话引用的 **profile id 不变**。模型身份三元组创建后不可改；换 provider、name 或 protocol 要建新模型并显式换 profile，避免影响全部引用者。

Profile 的 runtime 选项仅为 effort、agent teams 和 custom_prompt，不新增系统提示词模式字段。系统提示词模式及其来源继续由现有 `system_prompt_mode_choice` 解析：任务显式覆盖 → 角色显式默认 → 角色内置默认（Manager/Planner replace，Worker/Reviewer append）；保留 role/task 的存储和编辑入口。无角色、consult、适配器不支持替换时，`Launch.system_prompt_mode` 继续使用原先的 append 规则；预览和运行记录沿用最终值及来源。

旧 `schema.sql` 对已废弃的 `agent_profiles.system_prompt_mode` 列的迁移清理保留，不复用该列或引入替代列。v1/v2 文件曾带的旧 profile 模式仍按现有 importer 忽略并提示，不重新解释为 profile 选项；v3 profile 不接受此字段。

删除 model 时有 profile 引用返回 409；删除 provider 时有 model 返回 409，不能级联删模型。builtin provider/model/profile 的保护继续生效。model 设置或 provider endpoint/env 修改在锁定 provider 的事务中复验所有受影响 profiles；并发创建 model/profile 使用同一锁序，避免保存后产生失效引用。删除 endpoint 时有 model 引用返回 409。

## 4. 从 Model 到实际启动

统一 resolver 接收 `(profile_id, role, project_id, task_id)`，沿用当前分层选 profile 的顺序：任务 → 项目角色 → 项目 → 角色 → 系统 → 内置。选中 profile 后：

1. 读 profile、runtime、model、provider 和该 model 的 endpoint。不能从其他 endpoint 选「最接近」的协议。
2. 校验 `model.protocol ∈ adapter.protocols`、`provider.auth ∈ adapter.auth_kinds`；OAuth 还必须属于这个 runtime；继续校验角色能力、effort 字段与 agent teams。`efforts` 元数据沿用当前适配器的作用（例如 OMP 的 reasoning），不在迁移或启动时新增「旧版允许、仅因元数据而拒绝」的配对限制；界面可据此提示可选档位。
3. 形成不含秘密的 `ModelSpec` 和 `ResolvedModel`：model id、精确 name、唯一 protocol、context、四种有效价格及来源、efforts、background；provider 给 endpoint、env 和并发额度。`ProviderSpec` 缩成账户信息，不再持有通用协议集合和模型目录。
4. 只在 `load_agent_settings` 的启动路径读取 key，组装 adapter binding；`api_key` 仍 `repr=False`，不进入 API view、记录或导出默认值。
5. adapter 从 **该 model 的 protocol** 生成配置。`ProviderBinding(args, env, unset)` 继续负责秘密环境和清理继承变量；生成配置文件不写 key，只写环境变量引用。

| runtime | 模型协议与认证 | 必须保留的绑定结果 |
| --- | --- | --- |
| Claude Code | Messages；API key 或自己的登录 | 官方 endpoint 的 `ANTHROPIC_API_KEY` 与代理的 `ANTHROPIC_AUTH_TOKEN` 分支、主/辅助模型变量、unset、teams 与提示词参数 |
| Codex | Responses；API key 或自己的登录 | `-m`、effort、生成的 `config.toml`、隔离 `CODEX_HOME`、key 环境引用或登录链接 |
| OMP | 三协议之一；API key | `models.yml` 的 api 直接取 model.protocol，Chat 对应 `openai-completions`；精确 name、contextWindow、reasoning、Responses compat 与全部 modelRoles |
| Grok | Responses；自己的登录 | 现有登录、ACP/逐轮行为、配置文件；不会因协议相同而接受 API key |

启动签名包含 endpoint、唯一协议、精确模型名、context、effective settings、秘密 binding 的内部变化检测、prompt 与 runtime 参数。价格改变也应更新下一轮计价快照；不在日志输出秘密或可用于比较 key 的指纹。已有进程完成当前 run/turn 后按现有 stale 检测重建，不中途把一个请求切换到另一个模型。

会话的 runtime/provider 身份边界保持原规则，单纯新增 model id 不清空会话。安全合并 provider 时同时转换 `agent_sessions.provider_id` 及其他持久化会话身份（包括定时 Planner 的 holder）；只有启动对照相等才允许沿用同一 `session_id/session_totals`。不能只重写 profile 而遗留旧 provider id，造成无故新建会话。运维停机重启本身按原恢复机制续接。

## 5. 价格、usage 与运行记录

价格覆盖从 `provider.models[]` 原样移入 model；逐字段 NULL 回退 `BUILTIN_PRICES`，不在迁移时把内置价格固化成用户覆盖。内置表与 `PRICES_AS_OF/PRICE_SOURCES` 保留本版本原值；国内币种折算、长上下文阶梯、折扣等现有限制不在本次改变。

运行选择确定后建立**该 provider、该协议**的计价快照：主模型、辅助模型与同协议目录项。runtime 报出的 `modelUsage`/usage 名称先精确匹配，再沿用 `base_model` 的后缀/日期归一化；同一归一化名称有多个候选时，按迁移保留的 `catalog_order` 选择，不能改成数据库任意行顺序。Claude 登录的 `opus/sonnet/haiku` 别名映射仅对自己的登录生效。此处归一化只用于计价，不重写请求模型，也不跨协议取另一个模型的价格。

对于旧的「同一个目录用于多个协议」，每个协议复制整个有效目录及顺序，因此旧查价结果不变；目录为空但 profile 用到了某个名字时，第 6 节补出的模型保持所有覆盖为 NULL，不能新增用户未配置的价格。background/runtime 报出未登记名字也先按上述目录匹配，再查内置表；仍缺某种实际用到的 token 价格则整次成本未知，`cost_usd=NULL`，保留 token，不把它记为 0，也不假装 soft cap 能执行。

`CostSource` 与各 adapter 的既有选择不变：Claude 自己登录、Claude Code 连接 Anthropic 官方 API-key endpoint，以及 Grok 自己登录，均使用 runtime 报价；Claude Code 连接第三方、Codex 和 OMP 使用 provider_price。Grok 不因新 model 层而启用 soft cap；Claude 官方路径继续沿用 runtime 的预算处理。每 turn 的累计 token 增量、cache read/write、compaction、辅助请求与现有 soft cap 的含义不变。

历史 `llm_records` 不迁 model 外键、不改 model 字符串或 cost；历史 metadata 的 provider/profile 名称也不重写。新的记录保留 `metadata.profile.provider.auth`，因为现有 `EQUIVALENT` 汇总读取它，并继续兼容历史 `metadata.agent.provider.auth`；不能把官方 API-key 费用误归为订阅等价费用。在原 `metadata.profile` 外补 `llm_model={model_id,provider_id,name,protocol}`，以及非秘密的计价快照（适用目录价格、匹配规则版本与内置表版本）。同一 run 开始后改模型价格，不追溯改变该 run。模型删改后仍能解释旧费用；task cost、by_model、manager_by_model 继续按 `llm_records.model` 聚合，保留原总额与 unpriced 统计，不依赖当前 registry JOIN。

## 6. 自动迁移与配置交换

### 6.1 共用转换器与边界

迁移与旧文件导入共用纯转换步骤 `legacy registry → providers/endpoints/models/profile references + report`，输入包含补丁版本的旧 resolver 规则，不能靠新 resolver 推测旧协议。首次 schema 升级只执行一次，完成标记与全部转换在同一事务提交；之后重复 `ensure_schema` 不重新分配 id、不重新合并账户。旧初始化 SQL 必须同步改为引用 model id，不能继续执行已删除列的 INSERT。

转换顺序：

1. **保存旧有效值。** 用补丁后旧代码解析每个 profile 的实际协议及各角色/任务位置的 launch 输入；保存有限目录的原始顺序。未知 runtime、未知协议、失效外键或目录外的非法配对计为 blocker。
2. **识别 vendor。** 优先使用补丁已存的明确 vendor 标识；否则用已知预设的精确 endpoint/protocol 定义识别，登录用 auth/runtime 识别。无法唯一识别就 `custom`，保留输入，不能用 `name` 包含 `deepseek` 等字符串猜。改过 URL 而没有 vendor 元数据的账户也允许保留为 custom。
3. **拆 endpoint。** 旧非空 base_url 给旧 protocols 中每个协议各一行，保留字符串，不拼接或删 `/v1`。旧空 URL按旧 adapter 的官方默认地址展开；同时 `use_runtime_default=true`，binding 继续省略旧时未设置的变量/配置项。仅迁移可生成这种兼容标记，新 custom 必须显式 URL；用户改 endpoint 时清掉标记。该标记保存「省略」与「显式相同 URL」的区别，防止 Claude 官方分支及 pinned argv/env 发生变化。
4. **拆模型。** 有限目录按旧 provider 实际声明的 `(model, protocol)` 展开，复制全部设置及 `catalog_order`；不会额外给该 provider 添加新 vendor 默认模型。空目录收集所有引用它的 profiles 的精确模型名，按它们实际使用的协议补行；未被 profile 使用的无限名字无法枚举，以后显式登记。辅助模型名保留字符串；已存在的目录项仍完整迁移。内置登录建议可供新建选择，但不当作迁移时替换模型的依据。
5. **保守合并账户。** 按 6.2 节决定 canonical provider 与 old→new 映射；模型只在完整三元组及设置等价时复用，冲突保留账户，不丢任何设置。
6. **重接 profile。** 以旧 `(provider_id, exact model, actual protocol)` 定位新 model_id，保留 profile id 和全部 runtime 选项。OMP 必须按旧 `provider_api` 顺序得到唯一协议，不能随意取交集；补丁若改变此解析，以补丁最终代码为准。保留所有设置层、项目、任务和定时任务的 profile 引用。
7. **核验后收缩。** 校验每个旧 profile 恰好映射一行、外键有效、启动与计价对照相等，再把 model_id 设 NOT NULL、删除旧列、更新 schema 指纹与迁移标记。任何一步失败回滚全部事务。

当前 `ensure_schema` 先执行整个 SQL，再调用 Python migrations，不能把第 7 步的 DROP 直接塞进先执行的 SQL。实施时在同一 `_SCHEMA_LOCK` 与事务内明确分成：扩展新表/可空列 → 仍需旧列的历史迁移 → 本次转换/比较 → 收缩约束与旧列 → 新结构 builtin 初始化 → 指纹和 `schema_migrations` 完成标记。新库直接创建目标结构并初始化，迁移库再次启动也不得执行引用旧列的 legacy INSERT。两者最终 schema 必须一致。

若旧目录为空，引用模型可以合成 model 行；若旧目录非空但所选模型不在目录中，或找不到实际协议/endpoint，则不能合成一个「看起来可用」的新配对：dry-run 报 blocker，正式迁移中止，旧库不变。由用户在旧版修正或补丁提供明确修复后重跑，不禁用 profile 后带病上线。

旧 API-key 空 URL 可能在不同 runtime 下有不同默认解析。只有能映射为同协议同一实际地址并保留原省略语义时才自动转换，否则阻止迁移；不能统一填入某个 URL 掩盖差异。

### 6.2 安全合并与保留清单

例如 `deepseek`（Messages）与 `deepseek-official`（Chat/Responses）可以合成一个账户，前提是同时满足：

- vendor 有确定依据且相同，均为 API key，两个已保存的**非空 key**在内存中相等；不同 key、未知/未填 key、导出 placeholder 都不作为相同账户证据。登录 builtin 保留固定 id，不合并。
- 相同 protocol 的 endpoint（包括 runtime-default 语义）相等，不同协议的 endpoint 可并存。只对可证明无语义影响的 URL 表示做比较，不忽略路径、查询或代理差异。
- provider env 完全等价；同一 `(name, protocol)` 的全部 model 设置相等，所有精确/归一化计价匹配结果也相等。不同价格、background、context、efforts 或目录优先顺序导致结果不同就保留账户。
- 两个账户 `max_concurrency` 都是 NULL。有限额时旧版是两组独立额度，合并为一组即改变调度；不能用相同上限、求和或取最小值冒充行为等价，因此全部保留。
- 所有受影响 profile 的启动比较均相等；既有 login/session、builtin 保护也没有冲突。

保留最早创建账户的 id/name（并列按 id 排序），将其他账户的 model、secret 所属及会话引用移入 canonical；合并后删除重复 secret。比较过程不打印 key、不存 key 哈希或 key 等价分组。新增只追加的 `llm_model_migration_map`：`legacy_provider_id/legacy_model/protocol` 为复合主键，`provider_id/model_id` 为转换时的非空目标 id；只存这五个非秘密字段，供引用迁移、审计和回归使用。它是迁移历史快照，不设指向可删除现役对象的 FK，不阻碍后续正常删除；正式对象的外键与唯一约束仍必须通过。

不能安全合并的账户原 id/name 保留，Dashboard 仍逐一列出。迁移报告给出按原因分类的计数：vendor 未识别、缺 key、key 不同、endpoint 冲突、env 冲突、模型/计价冲突、有限并发、启动不等价。操作者在本地 Providers 列表查看保留项及其原因；公开报告只保留计数，不能附账户名称、URL、key 或其指纹。

### 6.3 启动前后比较是放行条件

复用 `tests/agent_launch_parity.py`、`tests/profile_rename_parity.py` 及其已 pinned 的 `tests/fixtures/agent_launch_parity_main.json`、`profile_rename_parity_main.json`，保留原夹具不改写来掩盖差异。这些旧夹具主要覆盖 Claude Code，**不足以单独证明本次多协议迁移**；新增以 #251/#253 合入后旧版输出为基线的多 runtime pinned fixtures。

在隔离数据库中先用旧版本真实 tiers/adapter 产生 capture，再运行迁移、用新版同一路径 capture；不允许只比较 serializer 或两个都调用新 resolver 的函数。比较：可执行文件和 argv、env 值及 unset（仅合成 key）、生成配置、协议、精确模型/辅助名、context/efforts、主提示词与首个 user turn、权限/隔离、插件与 MCP、登录链接目标语义、续接 id/totals、价格和 cap。只有临时根目录、时间戳及新增 model 身份 metadata 可以规范化；不能忽略 URL、模型名或协议差异。账户合并允许已声明的 id 映射差异，运行输入必须相等。

原有 Claude 长期夹具仍按其既定兼容测试通过；本次新旧对照以补丁后的启动为准，不把 #251/#253 明确修复的行为差异算成本次迁移的回归。对照发现一项 launch/pricing 差异即阻止发布；不靠更新 expected 直接消除失败。

### 6.4 `looper-config/v3`

保留现有文件头与其余 sections，provider 不再带模型目录；新增 `models` section，profile 的 model 改为复合引用。可移植配置不依赖本机数据库 id，例如：

```json
{
  "format": "looper-config/v3",
  "providers": [{
    "name": "deepseek", "vendor_type": "deepseek",
    "auth": "api_key", "oauth_runtime_type": null,
    "endpoints": {
      "anthropic-messages": {"base_url": "https://api.deepseek.com/anthropic"},
      "openai-chat": {"base_url": "https://api.deepseek.com"}
    },
    "api_key": {"secret": "omitted"}, "env": [], "max_concurrency": null
  }],
  "models": [
    {"provider": "deepseek", "name": "deepseek-flash", "protocol": "openai-chat", "context_window": 1000000},
    {"provider": "deepseek", "name": "deepseek-flash[1m]", "protocol": "anthropic-messages", "context_window": 1000000}
  ],
  "profiles": [{
    "name": "deepseek-chat", "runtime": "omp",
    "model": {"provider": "deepseek", "name": "deepseek-flash", "protocol": "openai-chat"},
    "effort": null, "agent_teams": false, "custom_prompt": ""
  }]
}
```

上例只展示有变化的 sections，不是完整可导入文件；价格等可选字段省略表示 NULL，`catalog_order` 和 `use_runtime_default` 在需要保留兼容语义时导出，导入后不可丢弃。内置登录 provider/model 按固定自然键预注册，沿用现有 builtins 不重复导出的规则；引用它们的 profile 仍用复合 model 引用解析。

导入按 provider name、model 三元组、profile name 匹配，保留目标已有 id；顺序为 runtimes → providers/endpoints → models → profiles → 其余引用。重复导入不生成重复项，不删除文件没列的对象。冲突、内置保护、`repoint`、按项结果和整个 apply 事务沿用现有机制；无有效 model 的 profile 及依赖它的配置明确 skipped，不回退默认，preview 列明原因。

v2 先按旧规则解析，再走 6.1 同一转换器；v1 先做当前 v1→v2 的升级。与整个数据库转换不同，文件转换只作用于文件中的旧对象：不借机合并目标库里无关的账户。目标同名 provider 只有一个协议 endpoint 发生冲突也必须报告，不能覆盖后静默改变未参与导入的 profile。受影响引用的兼容性和启动语义均需重新检查，失败保留旧对象。

默认 key 占位符 `{"secret":"omitted"}` 保留目标已有 key，没有则列 `need_secret`；显式携带 secrets 的导出/导入仍沿用已有受保护入口，不扩大权限。dry-run 在已有 key 的只读上下文中可以判断安全合并，但不把 key 或判断指纹带入 plan；没有真实 key 的两个 placeholder 不能合并。真实 apply 必须重算并复验，不使用可被篡改的客户端映射。

## 7. HTTP API 与 Manager MCP

### 7.1 HTTP

沿用现有用户认证、错误封装、limits、JSON 字段白名单与 builtin 保护，不增加绕过 operator-token 的入口。

| 接口 | 输入与结果 |
| --- | --- |
| `GET /api/registry` | 返回 runtime_types/runtimes/providers/**models**/profiles；vendor_types 取代旧 provider_presets 的全局目录表达，附每协议建议模型、默认 endpoints、auth、login runtime。保留 prices、limits 与使用计数 |
| `GET /api/providers` | `{providers:[...]}`；账户含 id/name/vendor/auth/endpoints/env/max_concurrency/builtin/login、model_count/profile_count、key mask 或 NULL |
| `POST /api/providers` | `{name,vendor_type,endpoints?,env?,max_concurrency?,api_key?}`；默认 auth/登录归属从 vendor 得到，非法组合 400；201 `{provider}`。只创建账户，不自动注册全部建议模型 |
| `PUT /api/providers/:id` | 改 name/endpoints/env/max_concurrency/key；省略保留，`***` 保留 key、NULL 清除；200 `{provider}`。不接受旧 models/protocols/base_url，也不接受 vendor 改型 |
| `DELETE /api/providers/:id` | 无 model 且非 builtin 才删除；200 `{deleted:id}`，仍被引用 409 |
| `DELETE /api/providers/:id/login-problem` | 继续现有登录问题清除，验证 vendor 的登录归属 |
| `GET /api/models?provider_id=&runtime_id=` | 两个可选过滤条件；200 `{models:[...]}`。runtime 过滤使用服务端统一配对校验，返回兼容 model，不靠前端猜测 |
| `POST /api/models` | `{provider_id,name,protocol,context_window?,四价格?,efforts?,background_model?}`；201 `{model,warnings}` |
| `PUT /api/models/:id` | 只改 context、四价格、efforts、background；NULL 清除覆盖，省略保留；200 `{model,warnings}`。更改后逐 profile 复验，efforts 对现有运行的影响在编辑区明确提示 |
| `DELETE /api/models/:id` | 非 builtin 且无 profile 引用才成功，200 `{deleted:id}`；其余 400/409 |
| `POST /api/agent-profiles` | `{name,runtime_id,model_id,effort?,agent_teams?,custom_prompt?}`；201 `{profile,warnings}` |
| `PUT/DELETE /api/agent-profiles/:id` | 更新同类字段或删除；保留现有 used_by/删除保护，200；不接受独立 provider_id 或字符串 model |
| `GET /api/agent-profiles/:id/prompt-preview` | 保留 role/project 参数，复用实际解析，返回最终模式、来源和真实 prompt blocks |

读 model 返回 `model_id/provider_id/name/protocol/settings/profile_count/builtin`，价格逐字段给 override/effective/source；profile 返回 `model_id`、可读 model 摘要及派生 provider 摘要，保留 roles、used_by、cost 与现有警告。API 不把数据库秘密列混进 JOIN 结果。model id 的 HTTP 路由正则和前端 DTO 必须同步支持新前缀。

统一错误：400 为字段、协议、凭据或选项不兼容；404 为不存在的 id；409 为自然键重名、仍被引用或目标冲突。消息指出失败字段及可用协议，不包含 key。旧 HTTP 写结构不自动猜协议，400 引导新模型接口；兼容旧格式仅在配置 importer，避免永久保留两套写协议。

### 7.2 Manager 的最小工具契约

选择独立 `looper_models`，避免继续把模型操作塞回 provider。三个工具共享 HTTP 下层 registry 校验，答案走现有 `ok/error` 封装；读取自由，写入仍只响应本 turn 用户请求；删除沿用现有风险确认机制。

| 工具/动作 | 参数 | `ok` 答案的业务字段 |
| --- | --- | --- |
| `looper_providers.list` | 无 | `providers`：id/name/vendor、protocol→URL、model/profile 数、key 为 set/missing、登录状态；`vendors`：类型与建议协议，按需查询建议模型 |
| `looper_providers.register` | name、vendor_type；endpoints、env、max_concurrency 可选，custom/self-hosted 按上述必填 | `provider`、`note`：无 key 登记成功，去 Dashboard 填 key 的原有链接 |
| `looper_models.list` | provider、runtime 可选；provider 支持 id/name，runtime 支持 id/name/type | `models`：model_id/provider/name/protocol/context/efforts/价格状态、使用数；无 key |
| `looper_models.create` | provider、name、protocol；context、四价格、efforts、background 可选 | `model,warnings` |
| `looper_models.update` | model 为 id 或完整三元组；可变设置字段 | `model,warnings` |
| `looper_models.delete` | model 为 id 或完整三元组 | `deleted`；仍被引用则错误，删除前列影响并确认 |
| `looper_agent_profiles.list` | 无 | profiles、runtime 简表、model 简表；profile 列 effort/teams/custom_prompt、roles/used_by，不重复整份价格表 |
| `looper_agent_profiles.create` | name、runtime、model；effort/teams/custom_prompt 可选 | `profile,warnings` |
| `looper_agent_profiles.update/delete` | profile id/name，update 同上可变字段 | 更新 `profile,warnings`；删除 `deleted`，仍需无人引用且用户确认 |

MCP 的 model 参数为字符串 id 或 `{provider,name,protocol}`，两种形式用 schema `oneOf` 明确表达；不接受只有名字的模糊查找。同名双协议时让 Manager 先列兼容模型，不由工具挑第一个。Provider 工具保持 list/register 两动作，现有未开放的 provider 更新/删除继续在 Dashboard；登录 vendor 从内置账户选择，register 不新建重复登录账户。

新增/修改的模型可见描述固定为简短功能句（实现代码跟随仓库语言）：

```text
looper_providers: LLM accounts: list vendors and accounts; register an API-key account without its key. Never take or pass a key; the user enters it on the dashboard.
looper_models: LLM models: list (optionally compatible with a runtime); create; update settings; delete an unused model with confirmation. A model is one provider, exact name and protocol; use its id or full reference.
looper_agent_profiles: Agent Profiles: runtime, LLM model and runtime options. List; create; update; delete one nothing uses with confirmation. Select a role's profile with looper_settings.
```

参数 schema 禁止额外字段，Provider 工具始终没有 `api_key`、token、credential 值字段；三个工具也都不能借 env、URL、custom_prompt 传 key，沿用秘密变量拒收规则和 Manager 的 TOOLS_CONTRACT。若用户粘贴 key，不复述，不传工具，回答 Dashboard 输入位置；这仍需真实模型评测，不能声称描述本身就是隔离边界。

## 8. Dashboard 与各入口

沿用 [UI 规则](../../design.md) 的列表、展开编辑、统一保存和错误提示；本次不增加视觉重做。

- **Providers：** 先选 vendor（内置 vendor 与 custom 同级可见），再显示名称、凭据、每协议 endpoint、并发与非秘密 env。API key 保持原遮罩/替换/移除；登录 vendor 显示自己的 CLI 登录状态和恢复入口。vendor 只负责初始填值，保存后不会因预设变化重填。
- **Models：** v0 放在 provider 展开的独立「Models」区，模型行明确列 name、protocol、context、efforts、background、价格来源及使用数。可以从 vendor 建议中显式添加，也能手工添加 custom 模型；保存走 model API。不同协议的同名模型分别显示，不能在 profile 输入框临时建一个未登记字符串。
- **Profiles：** 先选 runtime，再选兼容 model，选项显示 provider / exact name / protocol；provider 可作为过滤器但不保存重复字段。保持 effort、agent teams、自定义 prompt 和提示词预览；预览中的模式及来源沿用角色默认与任务覆盖，不新增 profile 模式控件。改 runtime 后当前模型不兼容时标错并要求选模型，不静默换成列表第一项。
- **使用与编辑反馈：** 缺 key、登录不可用、缺价格仍有原有警告；模型设置修改影响引用它的所有 profiles，在编辑区列使用数及入口。删除有引用时直接显示服务端原因；创建 profile 后不自动替换角色选择。

同步 `web/src/lib/api.ts`、registry cache、settings target 链接、`AgentProfileSelect` 的嵌入式新建流程，以及任务、项目、角色、定时任务里展示 profile 的派生模型摘要。角色选的仍是 profile，不变成 model；Import / Export 预览增加 models 与合并/阻止计数。

Telegram 与 Lark 通过同一 Manager 工具配置账户、模型和 profiles，收到同样的缺 key 引导；不需要新频道命令，不在聊天里收 key。CLI 的 serve/config export/import、registry 调用与 schema 维护需要同步 v3；任务启动参数继续选 profile，不引入第二套模型选择入口。HTTP 是公开的行为变更，要在后续实现 PR 和发布说明列明。

## 9. 上线、回滚与观测

### 9.1 顺序与 dry-run

1. #251/#253 在旧结构上先发布补丁；记录补丁后旧启动夹具与真实路径结果。正式升级前还必须交付旧结构的 forward-schema 检查，作为可回滚补丁版：新增单行 `registry_schema_version(id BOOLEAN PRIMARY KEY CHECK(id), version INTEGER NOT NULL CHECK(version > 0))`，旧结构记 2、新结构记 3；在任何 DDL/初始化前读取，超过程序支持版本就拒绝启动。新版本在迁移事务末尾才把 2 更新为 3。
2. 实现转换器、DB/schema 和读取/绑定路径，再同时接通 HTTP、MCP、Dashboard 与 v3 importer。不能只上线模型表而让创建 profile 的入口仍写旧列。
3. 在**真实数据形状的离线副本**演练；然后在运维允许的只读窗口对 live registry 形状做 dry-run（只读事务、无 CLI 模型请求、不启动 scheduler/agents）。输出只含计数，不导出原数据或秘密；发布前必须保留真实 dry-run 结果，本文不虚构数字。
4. 停止接收写入、暂停调度并结束当前 runs/turns，停止旧进程。重新预检并备份后迁移，再用新服务启动；不能让新旧进程同时使用一库。预检之后数据发生变化必须重新计算，不套用旧计划。

dry-run 输出固定计数字段：`providers_before/after`、`endpoints_after`、`models_after`、`profiles_before/after/mapped`、`models_from_open_catalog`、`providers_merged`、`providers_kept_separate_by_reason`、`legacy_default_endpoints`、`unresolved_profiles`、`binding_mismatches`、`pricing_mismatches`、`session_references_remapped`。各值为数量或原因枚举→数量；不带 provider/model 名称、URL、id、key、env 或内容哈希。profile 数量不变且全部 mapped、unresolved/mismatch 都为 0 才能放行。线上 dry-run 只做静态解析与等价比较，pinned tiers capture 在副本执行。

沿用 `looper/agents/db_backup.py`、`serve.py` 的既有机制：`schema_change_pending` 检测到变化时，先完成并校验 custom-format `pg_dump`，再 `ensure_schema`；schema 指纹与转换事务一起提交。这里是 **schema 变更前的完整数据库备份**，schema-only dump 无法恢复账户与引用。备份含秘密，留在原有受保护备份目录，报告不贴路径/内容。备份失败停止升级，本次发布门禁不使用 `--skip-backup`。导入新文件也不能绕过已有 schema 检查与备份要求。

### 9.2 回滚

迁移事务失败：原库保持旧 schema/rows，旧补丁版可以继续启动。迁移提交后旧版不能直接读新库；只有已带 9.1 的 forward-schema 检查的补丁版才能在 DDL 前安全拒绝。当前 v2.12.2 未有此门禁，不能声称仅靠 v2.13 的代码就能阻止旧二进制；更早版本靠停机与数据库隔离约束，不允许对新库试启动。支持的回滚目标限定为带门禁的旧结构补丁版。

上线验证失败：停止新进程，保留迁移后库供诊断，将备份恢复到隔离的替代库并核对 profile 数量、旧版本启动夹具及历史费用，再切回旧补丁版。不能原地跑一个猜测性的 down migration。迁移后若已接受新任务或配置写入，恢复旧备份会丢掉该窗口的数据，必须先保留并由操作者决定补偿；不自动覆盖。新建的双协议账户/模型无法无损表示为旧单 URL provider，因此 v3→v2 反向导出不是回滚方案。

### 9.3 故障发现

沿用启动 self-check、结构化日志和 Runs，不另建监控平台。migration 日志只记录版本、阶段、耗时与上述计数；失败给出类型化原因和回滚状态。运行记录显示 model/protocol、profile、cost source、价格是否未知；配对错误在发起模型请求前失败。

实现验收要能在现有状态/运行页发现三类问题：启动迁移失败或未备份即未就绪；配置解析失败及影响的 profile；模型 usage 无法计价。发布检查关注 blocker=0、launch/pricing mismatch=0、未定价运行数不增加、同一合成任务迁移前后费用/token 相等。账户 key/登录不可用不是自动换账户的理由。

## 10. 风险、已知限制与验证

### 10.1 风险与限制

| 风险 | 处理与边界 |
| --- | --- |
| 预设的 family/protocol 和 `[1m]` 再次串线 | 按单协议 model 保存精确名字；复用先发补丁的每 API 目录；不按名称猜 |
| 同 key 账户误合并，改变 env、价格或并发 | 全部安全条件同时满足才合并；有限并发和任何不确定性都保留 |
| 旧空目录、空 URL 无法直接落新约束 | 引用模型物化、默认 endpoint 兼容标记；无法还原则阻止，不换模型 |
| provider/settings 修改影响多个 profiles | 保持 provider 级限额与 env；同事务验证全部引用，UI 展示影响 |
| 分层迁移意外改变提示词模式 | 不新增 profile 模式；保留旧列清理及 role/task 存储，覆盖任务覆盖、角色默认、第二次启动和跨版本 schema |
| 任意 usage 名称和价格归一化存在歧义 | 同协议、精确优先、保留目录顺序；迁移前后比较所有归一化候选类，不能跨协议借价格 |
| 登录协议只是 CLI 接入声明 | 不保证能改 URL、不使别的 runtime 获得登录能力；不能取 token 搬家 |
| 迁移期间并发写与版本混跑 | 停写、事务/锁、版本门禁；不支持滚动双写 |
| 一个账户同协议要接多个 URL | v0 不支持；建两个 provider，即使 key 相同也保留，不能随机选 URL |

本次不保证 vendor 建议的每个模型都已经过真实调用，也不把有价格表等同于真实账单精确一致。上游 CLI 对 context、background 的支持仍以适配器和实际版本为准；未知计价、币种和折扣沿用现有限制。

### 10.2 必须实现的验证矩阵

| 层级 | 用例与通过标准 |
| --- | --- |
| 单元/输入边界 | 三协议枚举、唯一三元组、重复 endpoint、未知字段、价格 NULL/0/NaN/越界、efforts NULL/[]、context 边界、secret/env/URL 拒收；同名双协议不会互相覆盖 |
| 配对/绑定 | 每 runtime × 支持/不支持协议 × API key/各登录；沿用任务覆盖→角色默认→内置模式及 no-role/consult/不支持替换时的 append；OMP 不再挑 provider 的第一个协议，Grok 不误接受 key；Claude 官方/代理、background、unset、context 与 effort 原行为一致 |
| 迁移副本 | 用真实规模/约束形状的脱敏合成数据库，含空/有限目录、custom、各登录、双协议、相同/不同 key、缺 key、endpoint/env/价格冲突、有限并发、内置对象、层/项目/任务/定时/会话引用；缺配对时整个事务回滚；重复升级不变 |
| 启动 pinned 对照 | 原有两组夹具继续通过；增加补丁后四 runtime、多协议捕获，按 6.3 全字段比较；用故意改协议或 URL 的负向对照证明夹具确实能检出差异 |
| 配置交换 | v3 导出→新库导入、v2→v3、现有 v1；v3 profile 无模式字段，旧 profile 模式忽略并提示，role/task 模式不变；重复导入 id 不变，secret omitted 保留/need_secret、builtin 引用、名称冲突、不改变未参与导入的 profile；dry-run/apply 结果一致且输出无秘密 |
| 费用/历史 | 主/辅助模型 usage、归一化重名、四类 token、未知价与 soft cap、累计 turn 增量；迁移前后固定 usage 的费用一致；额外覆盖 Anthropic 官方 API-key 与 Grok 的 runtime 来源/预算行为；历史 task/manager/by_model、EQUIVALENT 汇总逐项相等；修改价格不改历史 |
| HTTP/MCP | provider→model→profile 完整流程；profile 写 schema 不含提示词模式、传入该字段明确拒绝；缺 endpoint、错误登录归属、不兼容 effort、仍有引用删除、并发创建/删除；未授权 401；Manager schema 没有 key 参数且结果无 key |
| Dashboard | headless 浏览器从 custom/内置 vendor 建账户与模型，runtime 筛选、双协议同名、缺 key、预览、嵌入新建、导入预览和错误读回；保留一张最终界面截图 |
| 真实服务路径 | 新 disposable DB、独立 runtime root、非 8767 端口启动 `looper serve`；从 HTTP/Manager/Dashboard 注册并读回，在迁移副本重启后实际启动各适配器；验证续接与费用。未安装 CLI 或无有效凭据的组合标为未测，不能算通过 |
| 故障演练 | dump 失败不迁移、中途抛错全部回滚、二次启动、带 forward-schema 检查的旧补丁版在任何 DDL 前拒绝新库、从完整备份恢复旧补丁版；dry-run 输出只含计数 |

数据库测试仅设置指向 disposable DB 的 `LOOPER_TEST_DATABASE_URL`，从 worktree 根运行 `uv run --extra test python -m pytest` 的相关项；完整含 PostgreSQL 的回归由 CI 执行。任何 live registry dry-run 只读，不用活动库做测试，不启动连接真实 Lark/Telegram 的通道发合成消息。

### 10.3 模型可见改动与真实 dialogs

会改变模型可见内容的落点：`ops/config.py` 三个工具的 descriptions/schema/答案、Manager TOOLS_CONTRACT 对 provider/model/profile 的简短区分、错误与缺 key 引导，以及所有列 profile 摘要的工具结果。Planner/Worker/Reviewer 的角色契约不因新增 model 层顺手重写；如注入的 profile 摘要发生变化，也按其实际可见内容做定向回归。

Manager dialogs 保留 `provider_key`、`config_tools`、`task_cost`、`schedule_planner_profile`，新增或扩展：custom 建账户→模型→profile、同名双协议的兼容选择、登录不能跨 runtime、缺 endpoint 明确拒绝、key 粘贴不存不复述、按模型改价不改历史。通过标准包括工具确实成功、数据库读回正确引用、实际绑定协议正确、无额外写入及无 key 泄露，不能只判最终回复像是成功。

实现定稿后按 [eval 说明](../../../evals/README.md) 在相同 cases 上跑前后比较，`--reps 2`，haiku 与 opus·xhigh Manager 都跑；命令中的新增 case id 由实现时登记：

```sh
uv run --extra test python -m evals.prompt_regression dialogs --mode real \
  --dialogs <affected-and-new-dialogs> --reps 2 --jobs 4 \
  --case-budget-usd 1.0 --total-cap-usd 8
uv run --extra test python -m evals.prompt_regression dialogs --mode real \
  --dialogs <affected-and-new-dialogs> --reps 2 --jobs 4 \
  --manager-model opus --manager-effort xhigh \
  --max-budget-usd 3.0 --case-budget-usd 3.0 --total-cap-usd 20
```

附与 `dialogs-real-haiku.json`、`dialogs-real-opus.json` 及补丁后同 case 基线的 comparison；新增用例先在旧版记录能力差异，再验证新版的可观察结果，不用刷新基线掩盖失败。预算不足/未运行/缺凭据单列，不记作通过。本 RFC 没有修改实际模型可见代码，因此本次不执行付费 eval；这些是实施发布门禁。

## 11. Go 移植与 eval harness 的后续遵循项

Go 端必须复用同一 vendor/auth/protocol 枚举、模型自然键、endpoint 选择、NULL/空 efforts、价格和配对拒绝语义，读取同一 schema/v3 文件与迁移完成标记。不得再从 provider 协议集合挑选、按模型名猜家族，也不得再次运行一套不同的账户合并。以后用相同非秘密 pinned launch 与 migration fixtures 验证跨语言结果；本 RFC 不要求本次写 Go。

eval harness 后续把 register-agent spec 明确分为 runtime、provider、model、agent(profile) 四部分：model 为 `{name,protocol,settings...}` 或 model_id/复合引用，agent 只留 name 和 runtime 选项。调用顺序为 provider→model→profile；仍只通过 `api_key_env` 读取秘密，不写 spec 或结果。旧 spec 经同一 legacy 转换器处理，不能按 runtime 名称自行猜协议；多协议时以旧 adapter 规则消歧或报错。`--provider-preset` 从该 vendor 的 Messages 模型建议选 Claude Code 的入口，选择 profile 的复制路径也只复制 model_id；报告补 model/protocol 并继续记录 cost_basis。实现相关 MCP 评测前必须同步 harness，否则注册路径不通不能当作模型能力失败。

## 12. 本次交付状态

本次交付仅为此 RFC：核对了当前 registry、预设、runtime、pricing、schema、配置交换、Manager 工具、Dashboard、eval 与现有 pinned fixtures。未实现新 schema/API/UI，未读取活动库或真实 key，未执行 live dry-run、DB 迁移、真实 CLI 运行或 dialogs，也不宣称已证明迁移等价。远端状态需实施前重新核对；本文的补丁衔接要求独立于未核实的合入状态。
