# RFC：pi 运行时 v0

- **状态：** 待实现；四类角色、API key、私有配置目录和自有工具桥的范围已定。
- **核实基线：** pi 1.3.0 随包文档；隔离临时目录中的 `pi --version`、`pi --help` 均退出 0。没有调用模型。仓库基于 v2.12.2；远端 main 和开放 PR 因网络不可达，实施前需要重新核对。
- **相关设计：** [多运行时](multi-runtime-agents-v0.zh-CN.md)、[system prompt 模式](system-prompt-mode-v0.zh-CN.md)、[Worker 引导](worker-steering-v0.zh-CN.md)。

## 1. 决定与取舍

加入第五种适配器 `pi`，Manager、Planner、Worker、Reviewer 都可选择。优先沿用 `omp.py`、`omp_steer.py`、`per_turn.py` 的结构，不复制 OMP 的协议或旗标。

| 选择 | 备选及取舍 |
|---|---|
| Worker/Reviewer 一次运行一个进程；Manager/Planner 逐轮 JSON 进程续接 | 全部使用常驻 RPC 能减少启动，但会新增 resident 传输及恢复状态机。本期复用 `PerTurnSession`；只有需要运行中引导的 Worker 使用 RPC。[P1]、[P2] |
| 自有 TypeScript 扩展直接转发 Looper agent HTTP API | pi 1.3 已有原生 MCP；“pi 没有 MCP”不再成立。但 `--no-extensions` 会禁用内置 MCP 连接器，`registerMcpServer` 依赖连接器。只显式加载 Looper 自有扩展符合本期边界，也避免再加通用 MCP 客户端。[P3]、[P4] |
| 私有 HOME、agent dir、session dir，关闭自动资源和 context 发现 | 只改 agent dir 不能代替完整隔离；本期不信任项目 `.pi` 配置。项目说明由 Looper 有界读取并注入，避免向操作者目录攀爬。[P3]、[P5]、[P6] |
| 只支持 API key 和两种协议 | 不复用 pi 的 `/login`、OAuth、云凭据；不新增 provider 类型或数据库迁移。其他协议在登记和运行前明确拒绝。[P7]、[P8] |

能力目标：`run`、`resident_session`、`resume`、`steer`、`read_tools`、`compaction_events`，以及供现有角色校验使用的 `mcp`。最后一项仅表示 **Looper 平台工具桥**，不承诺通用 catalog MCP。没有 Claude 插件、原生子 agent、structured output 或运行时定价能力。

## 2. 启动、system prompt 与隔离

一次运行形状如下，路径均由 Looper 生成，密钥不进入 argv：

```text
env HOME=<private-home> PI_CODING_AGENT_DIR=<private-home>/agent
    PI_CODING_AGENT_SESSION_DIR=<private-session-dir>
    pi -p --mode json --offline --no-approve --no-context-files
    --no-extensions --no-mcp --no-skills --no-prompt-templates --no-themes
    -e <packaged-looper-extension.ts>
    --provider looper --model <model-id> [--thinking <effort>]
    <system-prompt-flag> <generated-prompt-file>
    [--no-session | --session-dir <private-session-dir>]
```

提示通过 stdin 送完后关闭；JSON 模式读完提示执行并退出。RPC 用 `--mode rpc`，不带 `-p`，首条提示通过 RPC 命令发送；**不带 OMP 的 `--no-ui`**。`--no-extensions` 仍允许显式 `-e`，其他发现开关的含义以 pi CLI 为准。[P1]、[P2]、[P3]

- 配置放 `<runtime_root>/.local/pi/homes/<hash>/agent`；目录 0700、文件 0600、原子物化。hash 包含配置和桥版本/绑定，不把秘密写进目录名或记录。会话独立放 `.local/pi/sessions/<owner>`，不可混入共享 home。探针也使用临时 HOME/agent/session 目录，最多 10 秒，不接模型。[P5]、[P9]
- 环境从 `child_env.inherited()` 加本次显式设置开始；清除其他运行时登录、目录变量及外部 `PI_*` 路径覆盖，再设置 Looper 自己的绑定。配置变量由适配器拥有，provider/角色 env 不得覆盖。禁止复制或链接操作者的配置、登录、扩展、模型缓存。
- `--no-approve` 不关闭 context 发现，必须同时用 `--no-context-files`。复用 `instructions.project_dirs` / `first_per_directory`：仓库根到 cwd，每目录选 `AGENTS.md`，否则 `CLAUDE.md`；仓库外仅 cwd。全部标 `Delivery.INJECTED`，沿用每文件 32 KiB 截断及记录，不重复交给 pi 原生发现。[P3]、[P5]、[P6]
- 私有 `settings.json` 设 `cacheWarming: "off"`，空资源列表、`enableAnalytics: false`、`enableInstallTelemetry: false`；环境设 `PI_TELEMETRY=0`、`PI_OFFLINE=1`、`PI_SKIP_VERSION_CHECK=1`。离线只关自动网络，不阻止选定 provider 的模型请求。[P9]、[P10]
- append 用 `--append-system-prompt <file>`，replace 用 `--system-prompt <file>`，两者均接受现有文件。沿用 `runtime_prompt_text` 的 custom prompt 顺序和角色默认值；preview 的文本必须与实际文件一致。关闭项目 `SYSTEM.md` 等隐式来源，不新增全局 prompt。pi 没有 RPC `set_system_prompt`，逐轮生成文件也让续接应用最新 prompt。[P3]、[P5]、[P11]
- shell 如需恢复操作者 HOME，使用 pi 的 `shellCommandPrefix`，只恢复 OMP 已恢复的必要目录变量并清除 provider key、平台 token 和私有 agent 路径；不要套用 OMP 的 `PI_SHELL_PREFIX`。这属于子命令工作环境，不能使 pi 主进程重新发现个人配置。前缀和嵌套 pi 的实际隔离**需要验证**。[P10]

Planner 和 consult 显式使用 `--tools read,grep,find,ls,mcp__looper__*`；普通运行保留 pi 默认的 read/bash/edit/write，并显式保留桥工具。扩展工具同样受 allowlist 影响，不能只复制 OMP 的只读列表。Reviewer 沿用现有权限策略，不额外改成只读。工具选择不是操作系统沙箱。[P3]、[P6]

## 3. Provider 绑定

独立生成 `<agent-dir>/models.json` 的 `providers.looper`，不改 pi 内置 provider，不读 `auth.json`。密钥只在 `LOOPER_PROVIDER_API_KEY`，配置必须写 **`"apiKey": "$LOOPER_PROVIDER_API_KEY"`**；不能复制 OMP 的裸变量名写法，pi 会把非插值字符串当字面值。CLI 不传 `--api-key`。[P7]、[P8]、[P12]

| Looper 协议 | pi `api` | endpoint 与模型 |
|---|---|---|
| `openai-responses` | `openai-responses` | CPA 等兼容 Responses 的绑定；完整保留 registry 的 `base_url`（含 `/v1`），例如 `gpt-6.1-sol` |
| `openai-chat` | `openai-completions` | DeepSeek 的 Chat Completions 绑定；完整保留 `https://api.deepseek.com`，例如 `deepseek-flash` |

若 provider 同时声明两种协议，固定优先 Responses，与 OMP 顺序一致；API key 或 endpoint 缺失则启动前拒绝，不猜测 CPA 地址，也不回落到 pi 登录。只拒绝不支持的协议组合，不以 provider 名称做子串判断。[P7]、[P8]、[P12]

只生成选定模型；`input: ["text"]`，contextWindow 取 registry 非空值；reasoning 沿用 OMP 的 catalog efforts 规则，thinking 值只用 pi 支持的档位。输出上限和未登记的上下文值沿用 pi 的默认值并在真实路径读回，**需要验证**，不凭空声称模型容量。pi 的 thinking 可能被模型能力钳制，实际值需检查。[P3]、[P7]、[P12]

模型 cost 四项设 0，避免借用 pi 目录价格；Looper 仍按 provider 价格/现有内置价格表计费，未知价格为 null。**CPA 的 optional 工具参数兼容性需要验证**：OMP 的 `supportsStrictMode` 不能未经核对照搬；本机 pi AI 源码有该 compat 项，但指定文档只承诺“按实际 endpoint 验证”。用假 Responses 服务检查 strict 字段和 required，再分别走真实 CPA/DeepSeek，不改服务端工具 schema 去迎合某个模型。[P7]、[P12]

## 4. Looper 工具桥与 wheel

桥资源放 `looper/agents/runtimes/pi_extension/looper.ts`，通过 package-data 进入 wheel；用 `importlib.resources` 定位包内路径，由 pi 原生 TypeScript loader 加载，不引入 npm 安装或 TS 编译步骤。[P4]

现有 `ops/service.py::server_config` 生成的是 stdio `looper mcp-server`，它再调用 Looper HTTP API。适配器从 **`library.mcp_config` 的 `mcpServers.looper.env`** 取本次 `LOOPER_AGENT_URL/TOKEN`；不能假定它们在 `Launch.env`，也不能自行换成 operator token。缺失整项表示本轮没有平台工具；有项却缺 URL/token 则失败。其他 catalog MCP/插件/skills 本期不加载，逐项写进 `runtime_catalog.skipped`，知识仍按现有索引读路径。

扩展采用 async factory：在模型启动前 GET `<url>/api/agent/v1/tools`，携带 Bearer token；完整校验数组里的名称、描述和 inputSchema，拒绝重复或非法 schema 后，再一次性注册为 `mcp__looper__<name>`。工具描述和参数来自服务端，不硬编码 Manager、Planner、room、vault 列表。初始化失败必须阻止模型运行；文档承诺等待 async factory，但抛错是否在此启动组合下可靠终止进程**需要验证**，不能依赖普通事件 handler 的容错行为。[P4]

`registerTool` 的 `parameters` 文档使用 TypeBox；pi 自带 MCP 适配器能将普通 JSON schema 归一化后注册。本桥保持 `$defs`、enum、required、additionalProperties，不重建参数结构；使用类型桥接是否覆盖所有现有 Looper schema **需要验证**。[P4]

`execute(toolCallId, params, signal, onUpdate, ctx)` 转发 POST `/api/agent/v1/ops/<encodeURIComponent(original-name)>`。响应和超时契约沿用 `mcp_server.AgentClient`，另外禁止重定向以避免转送令牌：

- 不走代理、不跟随重定向，Bearer 只能送往绑定的 Looper 地址。优先 Node 内建 HTTP(S) 实现，避免全局 fetch 代理配置差异；只使用 Node 内建模块和 type-only pi API 导入。[P4]
- 必须是 200 和 JSON object；返回 `{content: [{type: "text", text: JSON.stringify(body)}], details: undefined, isError: body.status === "error"}`，`needs_confirmation` 保持正常结果。不得执行确认之外的额外写操作或扩大令牌权限。
- 普通超时 120 秒；room inbox 的正数 `wait_seconds` 用 `max(120, wait_seconds + 20)`。工具 AbortSignal 取消请求；超时/取消不能证明服务端未执行，不自动重放 POST，提示先核对效果。[P4]
- token 不进入 argv、prompt、工具参数、details、记录或原始错误日志；stdout 只保留 pi 协议。取消和错误的实际传播**需要验证**。[P1]、[P2]、[P4]

## 5. 事件、花费、会话和引导

独立 `PiStream`，复用 Looper 内部 stream-json 事件格式；严格 LF 分帧，不把 U+2028/U+2029 当换行。JSON 首条 session header 转 `system/init`；RPC 没有 header，取 `get_state.sessionId`。[P1]、[P2]

| pi 事件 | Looper 结果 |
|---|---|
| `message_end` 的 assistant | text/thinking/toolCall → assistant 文本、thinking、tool_use；只用最终 message，不累计每个 delta |
| `message_end` 的 toolResult | user/tool_result，保留 toolCallId、文本和 isError |
| `compaction_end` 成功 | compact_boundary，含 tokensBefore；失败/取消保留诊断，不制造成功压缩 |
| `agent_end` | 低层尝试结束，可能还会重试；不发最终 result |
| `agent_settled` | 恰好一个 result；中断、最后响应 error/aborted、未完成的 length/deferred 均不能报成功 |

以上事件和字段见 [P1]、[P13]。最终答案取最后一条 assistant 的文本；末条为空时不拿较早的进度文字冒充结果。缺 settled 或崩溃不能伪造 success。退出码沿用现有公共调用策略：已有完整成功 result 时，随后非零退出不会推翻结果；没有可用 result 时非零退出是失败，不为 pi 改其他运行时的收尾语义。未知事件保留脱敏 `pi_event`，无需实现全部 delta 回显。

每条 assistant `message_end.usage`、toolResult 可选 nested usage、成功 `compaction_end.result.usage` 各计一次内部 usage。message_update 是累计快照，agent_end.messages 是重列，tool_execution_end 也是重复来源，均不再次累加。`reasoning` 已含在 output；cacheRead/cacheWrite 分列，不把缓存再次加进普通 input。跨 provider 的未缓存 input 口径**需要验证**。[P1]、[P13]

result.modelUsage 只汇总本进程的新请求；resident.turn_usage 将它加到 session totals，绝不拿 `get_session_stats` 的历史累计当本轮。已有 provider 定价、软上限和运行记录继续工作；软上限在 usage 到达的请求边界终止进程树，不能保证阻止已经发出的请求。cache warming 关闭，避免不可见后台花费。[P10]、[P11]

**逐轮续接。** `ResidentPi(PerTurnSession)` 每轮 JSON 进程；私有 owner session dir，保存 session id，下一轮用 **`--session <id>`，不是 `--resume <id>`**。pi 的 `--resume` 打开选择器，`--session-id` 会在缺失时创建，均不用于精确续接。实施时在私有 session dir 内先解析唯一匹配并使用精确文件路径；缺失/歧义返回 session 未启动，交给现有 session_lost/handoff/resume brief 流程，禁止静默开新会话。两轮及重启后的实际连续性**需要验证**。[P3]、[P14]

**room。** pi 加入 `room.SESSION_KEEPING_RUNTIMES`，结束的 Worker 保存可咨询会话。consult 把需要的会话复制到独立 fork dir 后按精确路径续接，使用只读工具，不修改源会话或 task checkout。pi 支持 `--fork`，但本期采用 OMP 的“副本续接”隔离方案，避免改变原会话 id/查找规则。更新 `read_only_copies` 的 homes 和 session 类型列表；保留活动进程、保存的 Manager/Planner 和 schedule owner，失败/咨询副本及时回收。[P3]、[P14]

**Worker RPC。** `PiChannel` 对齐 `RunChannel`，不新增 resident 传输。沿 OMP 的调用顺序：`start` 保存提示、发送 setup 后立即返回，不等待 ACK；`events` 持续读取 stdout，收到 `get_state`、`set_steering_mode(one-at-a-time)` 的成功 ACK 后才发送 prompt。避免 Worker 先等待 start、后调用 events 时互相等待。每个请求独立 string id，支持乱序 ACK。prompt `started` 才进入本次运行；不应出现的 `handled/queued` 明确失败，不能死等 settled。steer 只有 ACK queued 才算接受，handled 作为未入队处理，报错不自动重发；ACK 不等于模型已执行。[P2]、[P11]

steer 在当前 assistant turn 的全部工具结束后、下次模型请求前进入；没有 OMP 的 `set_interrupt_mode`。结束只认 settled，之后拒绝 send，清 pending futures，幂等关闭 stdin 等待退出。ACK 超时、EOF、setup 拒绝、异常退出均有明确失败路径；取消/soft cap 沿用 `proc_tree` 的宽限及整树终止。如果以后增加优雅 RPC abort，需先 clear_queue：abort 本身不清队列，不能单用它声称没有后续执行。[P2]、[P11]

## 6. 接入点、验收与缺口

`runtimes._adapters` 注册 `PiRuntime`，自动发现 `rt-pi`，只探测 version/help；1.3.0 是本次最小支持基线。旧版、未知版本、help 缺所需旗标不声明对应可用能力，并给出升级/重探提示。能力映射不以供应商/模型名子串判断。现有 role/profile API、doctor 状态、CLI 通过 registry 工作，无需第五套分支。

Dashboard 只补 `runtime-catalog.ts` 的 pi 名称/安装提示及必要配对显示，无 pi 登录入口。Telegram、Lark 沿用 Manager 和任务 Profile 路径，不改渠道协议；通过替身渠道验证即可，不向真实账号发送测试数据。

实施的先测后写分片：

1. `test_pi_runtime.py` / registry DB：两协议、拒绝 OAuth/Anthropic、私有 env/argv、缺 key/endpoint、只读工具保留桥、探针超时、指令边界及 preview 一致性。
2. `test_pi_events.py`：工具关联、空最终答复、UTF-8/LF、retry 后 settled、length/deferred/崩溃、compaction 和 nested usage 不重计、provider 价格及 cap。
3. `test_pi_sessions.py` / room / read_only_copies：两轮续接、缺会话、prompt 变化、会话切换、副本咨询、活动/定时会话保留和回收。
4. `test_pi_steer.py`：假 RPC 子进程，乱序 ACK、setup/prompt 拒绝、handled、超时、迟到 ACK、终态与 steer 竞争、取消树及 Worker 实际消息路径。
5. `test_pi_tool_bridge.py`：Node harness 执行真正 TS 扩展、假 Looper HTTP；动态 role 工具表、schema、401、确认、长轮询、取消、禁止代理/redirect/no-retry/秘密外泄。`test_pi_packaging.py`：构建 wheel、安装到临时环境，验证资源在 checkout 外可读及可加载。
6. 全部角色的平台工具从实际 launch 到桥到 API 的闭环；`test_prompt_eval.py` 有 pi write/edit 的正向识别，失败工具有负向对照，不能把漏识别的“Planner 改文件 0”当证据。

真实路径另开小卡：新数据库、新 runtime root、非 8767 端口、禁用真实渠道。先验证 wheel 安装、探针、错误 key/不支持协议在发模型请求前失败；再用两个独立 eval agent spec 分别绑定 CPA/Responses 和 DeepSeek/Chat，四角色各跑一次，验证 Manager 工具、Planner 只读、Worker 引导、Reviewer、room、vault、续接、取消和 usage 读回。spec 仅用 `api_key_env`，结果去秘密后才引用。

扩展的工具描述/schema、运行时说明和 context 注入均为模型可见变化：按 [evals/README.md](../../../evals/README.md) 做定向真实对比，pi profile 的任务/Manager 对话各 `--reps 2`；相关 Claude CLI 基线与 Planner/Manager 对比也单列，不能以 pi 实跑代替。有花费与时间上限，后台运行；缺 key、预算或模型不可用时记“未验证”，不扩大成全量发布评测。

本轮只有文档和卡片，没有任何上述实现或真实模型验收。全部“需要验证”项目在后续台账保留；只有隔离 help/version 是已通过检查。完整支持的交付门槛是两个协议均通过、四角色及工具桥完成闭环；实现卡跑绿不等于该门槛已通过。

## pi 文档依据

下列章节按本机 1.3.0 随包文档核对；链接指向上游同名文档，后续版本可能变化。

- [P1：json.md — Framing and process I/O；Session header；Message events；Compaction events；Agent and turn events](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md)
- [P2：rpc.md — Start RPC mode；Correlate commands and responses；Run lifecycle；Errors；Shutdown](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
- [P3：cli.md — Invocation and output；Models；Sessions；Tools；Resources；Prompts and process](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)
- [P4：extensions.md — Create and load an extension；Respect the runtime lifecycle；Tools；MCP servers；Errors and cleanup](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
- [P5：configuration.md — Agent directory；Project .pi directory；Context files](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/configuration.md)
- [P6：security.md — Choose how to run Pi；Understand project trust；Resources protected by project trust；Project trust without an interactive prompt](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)
- [P7：models.md — Authenticate；Configure a compatible endpoint；Select a model](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)
- [P8：providers.md — Authenticate interactively；Use an API key from the environment](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)
- [P9：environment-variables.md — Pi Process Configuration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/environment-variables.md)
- [P10：settings.md — Model and thinking；Tools；Network and retries；Shell；Resources；Updates, telemetry, and warnings](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md)
- [P11：rpc-commands.md — prompt；steer；get_state；set_steering_mode；abort；clear_queue；get_session_stats](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc-commands.md)
- [P12：custom-provider.md — Choose the smallest integration；Provide authentication；Supply and refresh models；Report failures and usage](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/custom-provider.md)
- [P13：message-types.md — Usage；AssistantMessage；ToolResultMessage](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/message-types.md)
- [P14：sessions.md — Control session storage；Manage conversation context](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sessions.md)；[compaction.md — When It Triggers；Overflow and Length Recovery Ordering；CompactionEntry Structure](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)

[P1]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md
[P2]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md
[P3]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md
[P4]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md
[P5]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/configuration.md
[P6]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md
[P7]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md
[P8]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md
[P9]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/environment-variables.md
[P10]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md
[P11]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc-commands.md
[P12]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/custom-provider.md
[P13]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/message-types.md
[P14]: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sessions.md
