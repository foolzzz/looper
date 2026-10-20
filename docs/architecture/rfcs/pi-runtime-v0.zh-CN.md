# RFC：pi 运行时 v0

- **状态：** 适配器、四类角色、API key 两协议、私有目录、HTTP 工具桥、Dashboard/Profile 和沿 registry 的 CLI 已实现。假的本地 Chat Completions provider 上，安装后的真实 pi 闭环已通过。真实 Responses、真实 DeepSeek 和 Claude CLI 对照未跑，本运行时还不是完整支持。
- **核实基线：** pi 1.3.0 随包文档；隔离临时目录中的 `pi --version`、`pi --help` 均退出 0，探针读到 `1.3.0`。本分支相对 v2.12.2；远端 `main` 已到 v2.12.3。开放 PR #247、#256、#258 改的是别的文档，不改本 RFC。没有把真实 CPA 或 DeepSeek 记成已验收。
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
- 私有 `settings.json` 的 `shellCommandPrefix` 是一段 `unset`：清掉平台和桥变量、PI 目录、PI 环境、shell 元数据、其他运行时家目录和 provider 环境名。子 shell 保持私有 HOME，不恢复操作者 HOME，也不带本次运行的凭据。单测用 `/bin/sh -c` 跑这段前缀，确认令牌不在环境里。嵌套再起一个 `pi` 是否仍被隔离没有实跑，这一项仍开放。[P10]

Planner 和 consult 显式使用 `--tools read,grep,find,ls,mcp__looper__*`；普通运行保留 pi 默认的 read/bash/edit/write，并显式保留桥工具。扩展工具同样受 allowlist 影响，不能只复制 OMP 的只读列表。Reviewer 沿用现有权限策略，不额外改成只读。工具选择不是操作系统沙箱。[P3]、[P6]

## 3. Provider 绑定

独立生成 `<agent-dir>/models.json` 的 `providers.looper`，不改 pi 内置 provider，不读 `auth.json`。密钥只在 `LOOPER_PROVIDER_API_KEY`，配置必须写 **`"apiKey": "$LOOPER_PROVIDER_API_KEY"`**；不能复制 OMP 的裸变量名写法，pi 会把非插值字符串当字面值。CLI 不传 `--api-key`。[P7]、[P8]、[P12]

| Looper 协议 | pi `api` | 绑定 |
|---|---|---|
| `openai-responses` | `openai-responses` | 兼容 Responses 的 API key 绑定。登记的 `base_url` 原样写入，不猜测地址。示例模型名 `gpt-6.1-sol` |
| `openai-chat` | `openai-completions` | Chat Completions 的 API key 绑定。`base_url` 同样原样写入。示例模型名 `deepseek-flash` |

若 provider 同时声明两种协议，固定优先 Responses，与 OMP 顺序一致；API key 或 endpoint 缺失则启动前拒绝，不猜测 CPA 地址，也不回落到 pi 登录。只拒绝不支持的协议组合，不以 provider 名称做子串判断。[P7]、[P8]、[P12]

只生成选定模型；`input: ["text"]`；contextWindow 只在 catalog 有非空窗口时写入；reasoning 沿用 catalog efforts，efforts 为空则 reasoning 为 false。thinking 只用 `off`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`，空值省略 `--thinking`。输出上限和未登记的上下文沿用 pi 的默认值。本机 pi 1.3 的模型运行时读回（没有调用模型）在 catalog 省略这两项时仍给出正的 contextWindow 和 maxTokens；这些数字是 pi 的默认值，不写成模型容量。模型是否钳制 thinking 没有在真实模型上检查，这一项仍开放。[P3]、[P7]、[P12]

模型 cost 四项设 0，Looper 按 provider 价格或现有内置价格表计费，未知价格为 null。Responses 写入 `compat.supportsStrictMode: true`，可选参数保持可选，请求带 `strict: false`；Chat Completions 不写这项 compat。本机假 Responses 线路已核对 `strict: false`，required 也没有被展开。真实 CPA 和真实 DeepSeek 没有跑，这两项仍开放。服务端工具 schema 不改。[P7]、[P12]

## 4. Looper 工具桥与 wheel

桥资源放 `looper/agents/runtimes/pi_extension/looper.ts`，通过 package-data 进入 wheel；用 `importlib.resources` 定位包内路径，由 pi 原生 TypeScript loader 加载，不引入 npm 安装或 TS 编译步骤。[P4]

现有 `ops/service.py::server_config` 生成的是 stdio `looper mcp-server`，它再调用 Looper HTTP API。适配器从 **`library.mcp_config` 的 `mcpServers.looper.env`** 取本次 `LOOPER_AGENT_URL/TOKEN`；不能假定它们在 `Launch.env`，也不能自行换成 operator token。缺失整项表示本轮没有平台工具；有项却缺 URL/token 则失败。其他 catalog MCP/插件/skills 本期不加载，逐项写进 `runtime_catalog.skipped`，知识仍按现有索引读路径。

扩展采用 async factory：在模型启动前 GET `<url>/api/agent/v1/tools`，携带 Bearer token；完整校验数组里的名称、描述和 inputSchema，拒绝重复或非法 schema 后，再一次性注册为 `mcp__looper__<name>`。工具描述和参数来自服务端，不硬编码 Manager、Planner、room、vault 列表。初始化失败必须阻止模型运行。真实 pi CLI 配上无效 URL 绑定时，在模型调用前以非零退出，输出含 `Failed to load extension`。这一条已在该路径上核实。[P4]

`registerTool` 的 `parameters` 文档使用 TypeBox；pi 自带 MCP 适配器能将普通 JSON schema 归一化后注册。本桥保持 `$defs`、enum、required、additionalProperties，不重建参数结构。一份代表性富 schema（`$defs`、enum、required、additionalProperties、嵌套）被原样保留，并且 pi 接受。全部生产工具 schema 的普查没有做，不能记成已覆盖所有 Looper schema。[P4]

`execute(toolCallId, params, signal, onUpdate, ctx)` 转发 POST `/api/agent/v1/ops/<encodeURIComponent(original-name)>`。响应和超时契约沿用 `mcp_server.AgentClient`，另外禁止重定向以避免转送令牌：

- 不走代理、不跟随重定向，Bearer 只能送往绑定的 Looper 地址。优先 Node 内建 HTTP(S) 实现，避免全局 fetch 代理配置差异；只使用 Node 内建模块和 type-only pi API 导入。[P4]
- 必须是 200 和 JSON object；返回 `{content: [{type: "text", text: JSON.stringify(body)}], details: undefined, isError: body.status === "error"}`，`needs_confirmation` 保持正常结果。不得执行确认之外的额外写操作或扩大令牌权限。
- 普通超时 120 秒；room inbox 的正数 `wait_seconds` 用 `max(120, wait_seconds + 20)`。工具 AbortSignal 取消请求；超时/取消不能证明服务端未执行，不自动重放 POST，提示先核对效果。[P4]
- token 不进入 argv、prompt、工具参数、details、记录或原始错误日志；stdout 只保留 pi 协议。取消和错误传播已在工具桥测试，以及安装后的假 Chat provider 上核实：真实 pi 收到引导后，取消到达正在跑的 bash 子进程，任务变为 cancelled，进程树被回收，之后没有新的 provider 请求，撤销的 token 列不出工具。没有接真实 provider。没有加 RPC `abort` / `clear_queue` 的优雅中止。[P1]、[P2]、[P4]

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

每条 assistant `message_end.usage`、toolResult 可选 nested usage、成功 `compaction_end.result.usage` 各计一次内部 usage。message_update 是累计快照，agent_end.messages 是重列，tool_execution_end 也是重复来源，均不再次累加。`reasoning` 已含在 output；cacheRead/cacheWrite 分列，不把缓存再次加进普通 input。两种协议的未缓存 input 已用 pi 的真实 loader 和 client 对合成 SSE 核对：样例 input 100、其中 cached 40、cache write 8，记为未缓存 input 52、cache read 40、cache write 8、output 10；按每百万 (2, 10, 0.2, 2.5) 计价约 $0.000232，catalog 的 0 不参与。软上限样例在计价后于第 4 次 usage 越过 0.0005（累计 207/25/78/11，约 $0.0005951）时停进程树。安装后的假 provider 每次请求记 11 input、3 output。这些都是夹具，真实账单对账仍开放。[P1]、[P13]

result.modelUsage 只汇总本进程的新请求；resident.turn_usage 将它加到 session totals，绝不拿 `get_session_stats` 的历史累计当本轮。已有 provider 定价、软上限和运行记录继续工作；软上限在 usage 到达的请求边界终止进程树，不能保证阻止已经发出的请求。cache warming 关闭，避免不可见后台花费。[P10]、[P11]

**逐轮续接。** `ResidentPi(PerTurnSession)` 每轮 JSON 进程；私有 owner session dir，保存 session id，下一轮用精确 **`--session <file>`**。pi 的 `--resume` 打开选择器，`--session-id` 会在缺失时创建，`--continue` 也不用于精确续接。适配器在私有 session dir 内先解析唯一匹配并使用精确文件路径；缺失、歧义、部分匹配、符号链接、其他 owner 或坏 header 都令 `session_started` 为 false，交给现有 session_lost/handoff/resume brief 流程，不静默开新会话。假 pi 单测覆盖三轮、同一个 session id、三个 pid、精确文件路径，用量不重复计。安装后的假 Chat provider 上，Manager 和 Planner 在同一次 serve 里各有至少两条记录、同一个 session id，最后一次请求至少两条 user 消息。单测里的“重启”是新的 `ResidentPi` 重新加载已存 id，不是杀掉 `looper serve` 后再起。真实模型上的连续性，以及服务崩溃后按保存的会话再起，仍开放。[P3]、[P14]

**room。** pi 加入 `room.SESSION_KEEPING_RUNTIMES`，结束的 Worker 保存可咨询会话。consult 把需要的会话复制到独立 fork dir 后按精确路径续接，使用只读工具，不修改源会话或 task checkout。pi 支持 `--fork`，但本期采用 OMP 的“副本续接”隔离方案，避免改变原会话 id/查找规则。更新 `read_only_copies` 的 homes 和 session 类型列表；保留活动进程、保存的 Manager/Planner 和 schedule owner，失败/咨询副本及时回收。[P3]、[P14]

**Worker RPC。** `PiChannel` 对齐 `RunChannel`，不新增 resident 传输。沿 OMP 的调用顺序：`start` 保存提示、发送 setup 后立即返回，不等待 ACK；`events` 持续读取 stdout，收到 `get_state`、`set_steering_mode(one-at-a-time)` 的成功 ACK 后才发送 prompt。避免 Worker 先等待 start、后调用 events 时互相等待。每个请求独立 string id，支持乱序 ACK。prompt `started` 才进入本次运行；不应出现的 `handled/queued` 明确失败，不能死等 settled。steer 只有 ACK queued 才算接受，handled 作为未入队处理，报错不自动重发；ACK 不等于模型已执行。[P2]、[P11]

steer 在当前 assistant turn 的全部工具结束后、下次模型请求前进入；没有 OMP 的 `set_interrupt_mode`。结束只认 settled，之后拒绝 send，清 pending futures，幂等关闭 stdin 等待退出。ACK 超时、EOF、setup 拒绝、异常退出均有明确失败路径；取消/soft cap 沿用 `proc_tree` 的宽限及整树终止。如果以后增加优雅 RPC abort，需先 clear_queue：abort 本身不清队列，不能单用它声称没有后续执行。[P2]、[P11]

## 6. 接入点、验收与缺口

`runtimes._adapters` 注册 `PiRuntime`，自动发现 `rt-pi`，只探测 version/help；1.3.0 是本次最小支持基线。旧版、未知版本、help 缺所需旗标不声明对应可用能力，并给出升级/重探提示。能力映射不以供应商/模型名子串判断。现有 role/profile API、doctor 状态、CLI 通过 registry 工作，无需第五套分支。

Dashboard 的 Settings → Agent Runtimes 和 Agent Profiles 有 pi：`runtime-catalog.ts` 的标题是 Pi CLI，安装提示为 `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`，没有登录入口。一张无头浏览器截图显示该卡为 Detected，版本 1.3.0，角色为 Manager、Planner、Worker、Reviewer，能力含 compaction_events、mcp、read_tools、resident_session、resume、run、steer。同屏其他卡片显示 Planned，只因为那次一次性 PATH 上只有 pi，不是产品里没有那些运行时。组件测试覆盖这张卡。CLI、doctor 和 profile API 沿 registry，没有第五套命令。

Telegram 和 Lark 协议不变，仍走 Manager 和任务 Profile。安装后的 serve 确认 `telegram`、`lark`、`lark_bot` 配置在且为禁用。没有发送替身渠道消息，也没有向真实账号发测试消息。

覆盖这些路径的测试：

1. `test_pi_runtime.py` / registry DB：两协议、拒绝 OAuth/Anthropic、私有 env/argv、缺 key/endpoint、只读工具保留桥、探针超时、指令边界及 preview 一致性。
2. `test_pi_events.py`：工具关联、空最终答复、UTF-8/LF、retry 后 settled、length/deferred/崩溃、compaction 和 nested usage 不重计、provider 价格及 cap。
3. `test_pi_sessions.py` / room / read_only_copies：两轮续接、缺会话、prompt 变化、会话切换、副本咨询、活动/定时会话保留和回收。
4. `test_pi_steer.py`：假 RPC 子进程，乱序 ACK、setup/prompt 拒绝、handled、超时、迟到 ACK、终态与 steer 竞争、取消树及 Worker 实际消息路径。
5. `test_pi_tool_bridge.py`：Node harness 执行真正 TS 扩展、假 Looper HTTP；动态 role 工具表、schema、401、确认、长轮询、取消、禁止代理/redirect/no-retry/秘密外泄。`test_pi_packaging.py`：构建 wheel、安装到临时环境，验证资源在 checkout 外可读及可加载。
6. 全部角色的平台工具从实际 launch 到桥到 API 的闭环；`test_prompt_eval.py` 有 pi write/edit 的正向识别，失败工具有负向对照，不能把漏识别的“Planner 改文件 0”当证据。

安装后的闭环用真实 pi CLI、假的本地 Chat Completions provider 和一次性数据库，端口不是操作者的服务。两次成功摘要的失败和未验证清单都是空的，通过项为：wheel 安装与资源来源；隔离的 version/help 探针；缺 key 和不支持协议在启动前拒绝；无效 provider 拒绝且模型请求数为 0；错误桥 token 在 JSON 和 RPC 下拒绝且模型请求数为 0；四角色会话、按角色收窄的工具、用量、引导和取消；安装后的 serve 读回四角色 registry，渠道保持禁用，模型请求数为 0。更早几次摘要有失败或未验证（没有一次性数据库，或沙箱拒绝本地监听），由这两次空清单摘要接上。数据库不可用时跳过的 pytest（安装烟测 7 通过、8 跳过；会话烟测 7 通过、7 跳过）留在缺口里，不算这次闭环。

真实 Responses 和真实 DeepSeek 没有跑。`examples/pi/eval-responses.sh` 和 `examples/pi/eval-deepseek.sh` 已写好；假 `uv` 下的命令、预算和缺口边界测试 83 项通过，只证明脚本形状。`evals/runs/` 里没有这两次评测的结果。

模型可见内容的 Claude CLI 对照按 [evals/README.md](../../../evals/README.md) 单列：`examples/pi/eval-prompt-comparison.sh` 覆盖 13 个不同用例、4 个档位、48 个用例乘重复。缺 API key 时在调用 `uv` 之前退出。本次结果是通过 0、失败 0、未验证 48。基线保持原样。`test_prompt_eval.py` 里 “Planner 改文件 0” 仍只作负向对照，正向证据是 pi 的 write/edit 识别。

仍开放：嵌套 pi 隔离；全部生产 schema 普查；真实模型上的 thinking 钳制；真实账单的未缓存 input 对账；服务崩溃后续接；替身渠道消息送达；评测脚本标出的 steering、worker 会话续接、取消进程树、Responses 的 strict/optional 原始请求读回、`operator_notes_stale` 与 `schedule_*` 对话，以及和基线提交的共享 prompt/契约对比。

完整支持的门槛是两个真实协议都通过，加上四角色和工具桥。工具桥和四角色只在假 Chat provider 上通过；Responses 只到 loader 和假线路。这一门槛未过。

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
