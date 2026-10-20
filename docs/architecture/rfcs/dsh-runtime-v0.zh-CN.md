# RFC：dsh 运行时 v0

状态：待实现；2026-10-20。目标版本：`@deepseek-ai/dsh` **0.2.1-rc.2**。

## 1. 目标与证据范围

在 Claude Code、Codex、Grok、OMP 旁新增 `dsh`（DeepSeek Harness），Manager、Planner、Worker、Reviewer 都可选择它。保留并行接入的 pi；不迁移已有 Profile，不改现有默认运行时。本文是设计，不表示适配器已实现或模型验收通过。

已检查本地 `main` / `origin/main` 与工作分支同为 `95c399431`，开发计划 §1、§5、架构和 UI 约定。远端 main 与 open PR 查询因网络不可用失败，**需要验证**；实施前重新核对，不能据本地 refs 声称没有重复工作。

证据来自已安装 npm 包的 README、组成 patch 与发布的 JavaScript。下表路径均指对应版本的 npm 包内容；链接便于在其他机器定位同一文档，不依赖本机安装绝对路径。

| 标记 | 包文档与具体依据 |
|---|---|
| D1 | [dsh README](https://unpkg.com/@deepseek-ai/dsh@0.2.1-rc.2/README.md)，Entry modes / Profiles：profile 自动初始化、叠层顺序、`--patch`、不启动插件的 config dump |
| D2 | [dsh-headless README](https://unpkg.com/@deepseek-ai/dsh-headless@0.2.1-rc.2/README.md)，Use this package / JSON event stream / internals / limits（行 36、55、72–108、161）：stdin、退出、JSONL、resume 与授权来源 |
| D3 | [dsh-acp README](https://unpkg.com/@deepseek-ai/dsh-acp@0.2.1-rc.2/README.md)，行 55–80、112–116：stdio 常驻、session、新建/恢复、取消与静止；没有 busy steer 承诺 |
| D4 | [dsh-mcp-client README](https://unpkg.com/@deepseek-ai/dsh-mcp-client@0.2.1-rc.2/README.md)，Minimal configuration / Startup（行 28–100）：stdio、HTTP、环境、命名、失败与重连 |
| D5 | [dsh-system-prompt README](https://unpkg.com/@deepseek-ai/dsh-system-prompt@0.2.1-rc.2/README.md)，Configure / Contribute：persona、排序、`interpolate:false`、complete prompt |
| D6 | [dsh-base README](https://unpkg.com/@deepseek-ai/dsh-base@0.2.1-rc.2/README.md)，Changing defaults / Composition：patch **替换整段 config**，单一 filesystem provider |
| D7 | [dsh-home-paths README](https://unpkg.com/@deepseek-ai/dsh-home-paths@0.2.1-rc.2/README.md)，home 与 shared agents root：`DSH_HOME`、`DSH_AGENTS_HOME` 独立解析 |
| D8 | [dsh-llm-deepseek-api-key README](https://unpkg.com/@deepseek-ai/dsh-llm-deepseek-api-key@0.2.1-rc.2/README.md)，行 28–40；[dsh-llm-deepseek README](https://unpkg.com/@deepseek-ai/dsh-llm-deepseek@0.2.1-rc.2/README.md)，Endpoint and wire format：API key、Messages transport、模型与 effort、URL 规则 |
| D9 | [dsh-tools README](https://unpkg.com/@deepseek-ai/dsh-tools@0.2.1-rc.2/README.md)，Presentation / Restrict / Guard：native/PTC、单调执行 guard；[dsh-fs-sandbox README](https://unpkg.com/@deepseek-ai/dsh-fs-sandbox@0.2.1-rc.2/README.md)，read-only/workspace-write/danger-full-access |
| D10 | [dsh-bash-local README](https://unpkg.com/@deepseek-ai/dsh-bash-local@0.2.1-rc.2/README.md)，非 login bash、超时与 spill；[dsh-bash-sandbox README](https://unpkg.com/@deepseek-ai/dsh-bash-sandbox@0.2.1-rc.2/README.md)，sandbox policy；[dsh-subprocess-local README](https://unpkg.com/@deepseek-ai/dsh-subprocess-local@0.2.1-rc.2/README.md)，子进程凭据清除与平台限制 |
| D11 | [dsh-session-persistence-jsonl README](https://unpkg.com/@deepseek-ai/dsh-session-persistence-jsonl@0.2.1-rc.2/README.md)，root、zstd、flush、migration；[dsh-session-format README](https://unpkg.com/@deepseek-ai/dsh-session-format@0.2.1-rc.2/README.md)，官方格式 reader |
| D12 | [dsh-token-meter README](https://unpkg.com/@deepseek-ai/dsh-token-meter@0.2.1-rc.2/README.md)，行 36–68：压力估计与计费用量不同、严格 turn usage；[dsh-compaction-basic README](https://unpkg.com/@deepseek-ai/dsh-compaction-basic@0.2.1-rc.2/README.md)，压缩触发、summary 模型与额外请求 |
| D13 | [dsh-agent-instructions README](https://unpkg.com/@deepseek-ai/dsh-agent-instructions@0.2.1-rc.2/README.md)，行 28–76：用户全局与项目链；[dsh-skill-filesystem README](https://unpkg.com/@deepseek-ai/dsh-skill-filesystem@0.2.1-rc.2/README.md)，默认目录、显式 custom roots |
| D14 | [dsh-session-telemetry-otel README](https://unpkg.com/@deepseek-ai/dsh-session-telemetry-otel@0.2.1-rc.2/README.md)，DISABLED；[dsh-session-log-deepseek README](https://unpkg.com/@deepseek-ai/dsh-session-log-deepseek@0.2.1-rc.2/README.md)，独立、默认开启的 Session log 上传；[dsh-client-product-analytics README](https://unpkg.com/@deepseek-ai/dsh-client-product-analytics@0.2.1-rc.2/README.md)，分析收集开关 |
| D15 | [dsh-tool-web README](https://unpkg.com/@deepseek-ai/dsh-tool-web@0.2.1-rc.2/README.md)，search/fetch 独立开关；[dsh-web-search-deepseek README](https://unpkg.com/@deepseek-ai/dsh-web-search-deepseek@0.2.1-rc.2/README.md)，搜索会另发模型请求 |
| D16 | [dsh-app-boot README](https://unpkg.com/@deepseek-ai/dsh-app-boot@0.2.1-rc.2/README.md)，Startup failures（行 43、97–113）：可选插件失败仅警告，`headless-runner` 是 required id；[dsh-launch-environment README](https://unpkg.com/@deepseek-ai/dsh-launch-environment@0.2.1-rc.2/README.md)，process / 项目 `.env` / home `.env` 优先级 |

源码补证，不伪称 README 已列出全部字段：`dsh-headless/lib/json-stream-BA-F3lfb.js:120–155,182–271` 定义事件和重试用量；`dsh-llm-deepseek/lib/index.js:updateUsage` 定义 Messages bucket 映射；`dsh-acp/lib/index.js:625–635,796–798` 证明 ACP 的 context usage 和单个 in-flight prompt 限制。若升级，重做协议核对，不依赖压缩构建文件名长期稳定。

## 2. 方案与决定

维护者决定保持不变：四角色走 dsh；只用 DeepSeek API key；运行级 provider binding；私有 home 和 Looper overlay；平台工具走 `dsh-mcp-client`；遵循 OMP 的 adapter、探测、注册、计费、launch 与测试结构。

| 方案 | 收益 | 成本与限制 | 结论 |
|---|---|---|---|
| headless，每轮进程 + persisted Session | JSONL 含 step 用量；可复用 `PerTurnSession`，故障范围是单轮 | 每轮冷启动，无运行中消息输入 | **v0 推荐** |
| ACP 常驻 | README 已证明 headless stdio 与恢复，省进程启动 | busy prompt 被拒绝；`usage_update.used/size` 只有上下文压力，需另接 durable log 才能精确计费 | 后续阶段 |
| SDK 常驻 | 可做深度控制 | 新增协议、计费和生命周期集成，超出最小接入 | 不做 |

ACP 是有证据的可行候选，不是“不能 headless”。它没有直接 busy steer；cancel → 等待静止 → 新 prompt 是中断后继续，不能冒充原生 steering。[D2,D3,D12]

`DshRuntime`：`RUN / RESIDENT_SESSION / RESUME / MCP`；`resident_transport=PER_TURN`、`run_input=False`、`can_replace_system_prompt=False`。不声明 `STEER`、`AUTONOMOUS_TURNS`、`STRUCTURED_OUTPUT`、`CLAUDE_PLUGINS`、`COST_REPORT`。首版不声明 `COMPACTION_EVENTS`：headless 投影没有保证提供压缩边界。`RESIDENT_SESSION` 在 Looper 中按既有 OMP/Codex 语义表示连续会话，并不表示同一 OS 进程。

## 3. 运行与续接

命令布局固定为：

```text
dsh --profile headless --patch <private-overlay.yml> --json [--session-id <saved-id>] -
```

所有 launcher flags 位于 app flags 之前；完整用户 turn 写 stdin 并关闭，不能把 prompt 放 argv。首轮不带 session id，从 opening `session.sessionId` 取真实 id。`--session-id` 只能采用已有日志，不是指定新 id。[D1,D2]

`ResidentDsh(PerTurnSession)` 实现 `_split / _turn_argv / _stream / turn_usage`；Manager 与每个 Planner 各自串行续接，Worker/Reviewer 各次 run 各有独立 owner。`run_command` 必须接受 `max_turns`、`session_dir`，包括 agent room consult；不增加 dsh 专属 tier 分支。`room.SESSION_KEEPING_RUNTIMES` 加入 dsh，`run_session_dir` 保存 Worker/Reviewer 的受管 home；实现 `kept_session_dir/consult_command`，将已结束 root Session 的整个 home 作为 opaque snapshot 复制到独立 fork_dir，再以新 token/只读 Launch 恢复；原 Session 不追加咨询记录，不能通过修改 header 伪造 native fork。

owner 数据位于 `<runtime_root>/.local/dsh/owners/<opaque-owner>/`，其下为 private HOME、DSH_HOME 和 DSH_AGENTS_HOME。owner id 不能只用可碰撞的截断 label；可用完整 label 的 SHA256。生成配置按内容哈希写到 configs 下，**Session 根按 owner 固定**，不能随 prompt/MCP token 哈希变化而丢失。无 `session_dir` 的单次 run 生成唯一 owner，不能让同配置的并行 run 共用可变 home。[D7,D11]

同 owner 同时只有一轮；不同 owner 不共享 Session。恢复须保持 cwd 与持久日志匹配。进程丢失、Session 不存在/不合法时走既有 stale / handoff / resume brief 路径；不能重试已产生副作用的用户 turn。更换 runtime/provider 时按既有 `can_resume/profile_switch` 开新 Session；同 provider 内切换模型、改 Looper prompt 或 token 可恢复旧 Session，但下一轮必须应用新 overlay。若 dsh 实际不采纳新 prompt，验收失败，不能宣称重配生效。[D2]

超时、用户停止、服务关闭、spend cap 使用 `proc_tree` / `PerTurnSession` 的终止与清理，不只是关闭 stdin。先正常终止，再限时升级 kill；取消后不把晚到 `final` 判成功。headless 内的子任务可在本轮继续产出结果，但进程结束后不向 Looper 上报 autonomous turn。[D2,D10]

## 4. Provider 与配置

只挂 `dsh-llm-deepseek-api-key` 的 `deepseek-official` 路由，禁用 `llm-pi-ai`、account 登录路由和交互凭据存储。`ProviderBinding.env` 在启动时注入 `DEEPSEEK_API_KEY`；不扩大全局 `child_env` 密钥继承名单。缺 key 在启动边界报错，不能退回项目 `.env` 或操作者登录。[D2,D8,D16]

现有 `ProviderSpec` 没有厂商字段。这里“DeepSeek only”明确为 **DeepSeek 模型白名单 + API key + Messages 协议 + 固定 dsh DeepSeek route**，不以 provider 的展示名、id 或 URL 子串猜厂商，也不把任意 OpenAI 协议 provider 当 DeepSeek。允许 provider 指向显式 Messages-compatible 网关；这不证明网关实际供应商，最终请求对象需真实验收。

- `auth=API_KEY` 且含 `ANTHROPIC_MESSAGES`；纯 `OPENAI_CHAT`、Responses、OAuth 拒绝。
- 精确模型映射：`deepseek-flash` / `deepseek-flash[1m]` → `deepseek-flash`；`deepseek-v4-pro` / `deepseek-v4-pro[1m]` → `deepseek-v4-pro`。保留原 provider catalog 的成员校验。其他别名直接拒绝，不做前缀或日期后缀猜测。
- effort：未设则采用 dsh 默认 `high`；dsh 包本身接受 `off / low / high / max`，但 Looper 当前设置入口没有 `off`，v0 对外仅接受 `low / high / max`；拒绝 `off/medium/xhigh`，不静默映射，也不扩大共享 effort 枚举。
- `baseURL` 取绑定 provider 的 `base_url`，为空才用 `https://api.deepseek.com/anthropic`。沿用 URL 校验；传给 dsh 由其追加 `/v1/messages`，已有最终 `/v1` 不重复。不能把 OpenAI `/v1/chat/completions` 根改写成 Messages 根。[D8]

同一 adapter 校验函数在 Profile 创建/更新、provider 更新影响检查、运行时绑定调用；registry view 增加可选 `supported_models`（其他 runtime 为 null），UI 消费同一精确集合。无需新 DB 厂商字段。DeepSeek preset 已有 Messages API 和 `[1m]` catalog；无需改其默认模型。

## 5. 隔离与模型可见内容

### 环境与 overlay

以 `child_env.inherited()` 为起点，应用允许的非机密设置，再应用 binding，最后锁定私有路径和 dsh policy。清除外来 dsh/agent 目录及 provider 路由变量，固定 `HOME`、XDG 目录、`DSH_HOME`、`DSH_AGENTS_HOME`；操作者 `.dsh`、`.agents`、`.env` 和 profile/plugins/sessions 都不读取。项目 `.env` 在 launcher 中有独立低优先级来源，故 key、baseURL、模型、tools mode、上传策略必须显式覆盖，不能只靠环境清洗。[D7,D13,D16]

patch 根据实测 composition 的 **entry id** 写，替换 config 时重述需要保留的字段：

| entry | 固定策略 |
|---|---|
| `agent-default-model` | provider `deepseek-official`，模型为归一化后的绑定模型 |
| `llm-deepseek` | `apiKeyEnv: DEEPSEEK_API_KEY`、绑定 `baseURL`、验证过的 effort 和单模型 catalog |
| `tools` | `mode:native`，屏蔽 `DSH_TOOLS_MODE` 的继承影响 |
| `hmr`、`settings`、`config-editor`、`plugin-manager`、`tool-plugin-manager` | disabled；运行中的 agent 不能重新写入 profile 设置 |
| `credentials`、`authorization`、`deepseek-account`、`llm-deepseek-account`、`llm-pi-ai` | disabled；不加载其他认证或 provider |
| `session-telemetry-otel` | `mode:DISABLED`；同时禁用该 entry 与不需要的 `otel` |
| `session-log-deepseek`、`plugin-package-inventory-deepseek` | disabled；Session log 上传独立于 OTel，不可漏关 |
| `session-title-llm` | disabled，避免不可见的标题请求 |
| product analytics entries | shipped headless dump 中未出现；不额外挂载，升级后若出现必须禁用 |
| `tool-web` | 默认 `search:false`；保留 `fetch:true` 和需要的预算字段 |
| `web-search-deepseek`、`web-deepseek-api` | 默认 disabled；只有显式非机密 agent 设置 `DSH_ALLOW_WEB_SEARCH=true` 时允许开启搜索；值仅 true/false，其他拒绝 |
| `agent-instructions` | disabled；项目指令由 Looper 的 `instructions` 统一选择并注入，避免重复与用户全局链 |
| `skill-filesystem` | `includeDefaultRoots:false`、只读 catalog materialization 的 `customSkillDirs`、`watch:false` |

搜索开启会增加辅助请求，其费用完整性未验证，运行记录必须标明；默认路径关闭。保留原生 fs-sandbox / bash-sandbox，不能同时挂 fs-local；普通可写角色使用显式 `danger-full-access / approval never` 与现有 Looper 无交互策略一致，只读运行另加下面的执行 guard。这是配置隔离，不是同 UID 的秘密隔离。[D6,D9,D10,D14,D15]

所有生成目录 0700、配置与模块 0600，原子写入；run token/provider key 不出现在 argv、prompt、YAML、launch record、错误中。MCP 机密字段以环境引用物化。dsh 子 shell 会清除常见命名的 key/token；用正向 marker 验证其没有继承 `DEEPSEEK_API_KEY`，不将名称启发式当安全边界。[D10]

### 提示词与 bootstrap

`can_replace_system_prompt=False`：由既有 `Launch.system_prompt_mode` 得到有效 append，UI 用 `replace_refusal` 显示原因；不改其他运行时/角色保存的 replace 默认值。

不能直接把任意 Looper 文本塞进 `personaSuffix`：其中的 `{{...}}` 会被当模板变量。新增一个很小的 Looper bootstrap ESM 模块，以 `systemPrompt.section({name:'looper',order:20000,text,interpolate:false})` 追加字面文本。它依次加入 Profile custom prompt、必要的 runtime notes、受管项目指令、Looper 既有分层 prompt，契约仍在 Looper 段最后；不声称覆盖 dsh 内建系统段。`prompt_blocks` 与真实写入共用 builder，预览显示 dsh 内建段不可展开、有效 append、注入文本和哈希。[D5,D13]

bootstrap 替换 **required 的 `headless-runner` entry**，保留原 runner 的 Config/依赖与 stdin/session/json 行为；完成字面 section 注册、MCP 挂载和只读 guard 后才委托原 runner。模块从已探测 dsh 安装锚点解析官方 package exports，不假设私有 home 的 `node_modules` 有依赖，不复制 vendor 源码、不运行安装命令。可用 Python 常量生成固定 ESM；若选择独立 `.mjs` 源文件，wheel 必须明确包含它。该组合启动顺序**需要离线验收**，不可只靠 `--dump-config` 判定模块成功激活。[D16]

只读的 Planner/consult：bootstrap 使用 `tools.guard` 单调拒绝除精确 `read/glob/grep` 与本次 **Looper 平台 MCP** 所注册工具集合之外的调用；不按字符串前缀允许任意 MCP。集合从挂载前后注册差集取得，并核对桥发布的 expected names。禁用 agent settings 编辑；shell、write、edit、PTC、subagent/workflow/jobs 均不可绕过。外部 catalog MCP 在只读运行不挂载，记录 omitted reason。标记 `ReadOnly.PERMISSIONS`，从 tool_result 记录拒绝。保留 Looper 目录前后校验作回归检查；这不是主机级 sandbox。guard 在恢复既有 Worker Session 做 consult 时仍重新应用。[D9]

### 能力目录

只把 Looper 已安装 skill 的受管快照转换成 dsh filesystem skill roots；project 的知识索引保持按路径读取。Claude plugin 的 hooks、commands、agents、output styles 不转译；外部 MCP 来自 catalog 的配置，不从 plugin 内重复发现。`launch_record.runtime_catalog` 精确列出加载/省略清单，不声明支持完整 Claude 插件。若某 skill frontmatter 不兼容，应显式标省略并给原因，不能 silently drop。[D13]

## 6. 平台 MCP：保留现有桥

`looper/agents/ops/service.py:server_config / library_with_server` 当前给的是：

```text
command = <this Python>
args = ["-m", "looper.cli", "mcp-server"]
env = {LOOPER_AGENT_URL: <run URL>, LOOPER_AGENT_TOKEN: <run token>}
```

它是 stdio MCP → Looper HTTP API 桥，**URL 不是 MCP Streamable HTTP endpoint**。把 `AgentLibrary.mcp_config.mcpServers` 每项翻译成 dsh client config；`looper` 用 stdio、原 executable/args，env 的机密值通过 run child env 引用。真正的 HTTP catalog MCP 才使用 `transport:streamable-http`、url/headers。保持 serverName 和 `mcp__looper__<tool>` 命名，run token 仍由现有权限系统约束。[D4]

仅 `failOnStartupError:true` 不够：可选 MCP entry 失败时 dsh 仍可能继续。bootstrap 必须在 required runner 中 **await 官方 MCP client 的 apply/ready 并传播失败**，工具及 prompt section 未就绪不得调用原 runner。使用同一官方模块的 `apply/inject` exports，在 bootstrap 注入其依赖；无重复独立 MCP entry。连接失败、缺关键工具、重名、鉴权失败都应非零退出且零模型请求。需用实际 Cordis + 本地假 MCP/假模型计数证明，不能只测 mock 抛错。[D4,D16]

外部 catalog MCP 按原 catalog 的可选语义处理并记录失败；`looper` 桥是四角色的必需项。每轮重新挂载，因此 token 更新自然到下一轮，resume 不可沿用上一轮 token。

## 7. 事件、用量、价格与可观测性

headless JSONL 的承诺是提交后的事件，普通字符串最大 8 KiB、行最大 32 KiB，可带 `truncated:true`；`final.text` 不截断。不是 token 实时流。[D2]

| dsh 原始事件 | Looper 事件/行为 |
|---|---|
| `session {sessionId,cwd}` | `system/init`，记录真实 session id |
| `text {text}` / `thinking {text}` | assistant 内容，用于活动与流展示；不是最终回复来源 |
| `tool_call {callId,tool,input}` | assistant tool_use；精确 callId 配对 |
| `tool_result {callId,status,result}` | user tool_result，保留 completed/error 与截断标记 |
| `status {phase:step_end,turn,step,usage?}` | 一次 step 的 usage 增量；重试 attempt 已在内 |
| `status {phase:turn_end,reason}` | 保存 `reason.kind`，completed 才候选成功 |
| `final {text}` | 唯一最终回复文本；结合终结原因和进程退出判定结果 |
| `error {message}`、坏 JSON、EOF 无 final | error；保留有界 stderr/diagnostic，不合成成功 |

`final` 可能在错误 turn 后仍发出；启动 driver 错误可能只有 error 或 stderr。channel/PerTurnSession 必须把非零退出、终结原因缺失、cancel 与正常 result 区分；若现有消费者在退出前就信任 result，需在 stream EOF/进程结束处校验，不把成功提前交给用户。无 step usage、partial usage、truncated step 元数据也不得伪装全额计费。未知事件保留有界 diagnostic，不改变回答。

`status.step_end.usage` 字段：`inputTokens/outputTokens`，以及可选 `totalTokens/cacheReadTokens/cacheWriteTokens/reasoningTokens`。发布代码已证明 Messages 将 `input_tokens` 映射到 `inputTokens`，cache buckets 分开；其 total 为 input + output + read + write。因此直接映射到 Looper `input_tokens/output_tokens/cache_read_tokens/cache_creation_tokens`，**不再扣 cache，也不把 reasoning 叠加 output**。完整 step 重试已累计，不再额外累计 assistant 文本或 final。恢复后只累计本进程的新 step，不读 Session 历史总量。[D8,D12；源码补证]

**未知保持未知**：任一 attempt 无 usage 时 dsh 省略整个 step usage，可选 cache bucket 也可能缺失。保留已知 token 的部分值与 usage completeness；本轮 cost 标 unknown，不显示 `$0` 或部分值为完整花费。检查既有 `RunSpend/run_usage`，若其把缺失视为 0，最小增加显式 incomplete 信号，只有该信号才让 cost/cap 状态变 unknown，其他 runtime 行为不变。cap 为软上限，仅在已报告 step 之间判断；root step 用量缺失且配置了预算时记录“无法验证预算”，及时终止后续步骤，不保证已发请求不超支。

`BUILTIN_PRICES` 已有两个模型；复用 `price_cost / usage_event / RunSpend`，不重新维护价格。以 provider catalog 为优先，既有表作为 fallback；不是重新宣称当日官方实时价格。压缩 summary、web search、内置子 agent 的用量不保证出现在 root headless step 投影，**需要验证**；发生这些路径时完整花费未知，不以 root usage 冒充总费用。若不能从事件证明这些路径没有发生，默认将完整性标为 unknown，并将 root 观测金额单独作为 observed cost；软上限只约束该可观测金额，不能声称约束完整账单。默认关闭额外搜索和标题请求；常规 token-meter projection 也不保证统计 `compaction/summary.data.usage`，完整对照必须用官方 reader 另计该事件和 child Session；真实验证必须对照官方 token-meter/session reader，不能直接硬编码压缩 JSONL 格式。[D11,D12,D15]

每 run 记录 runtime/version、isolation id、owner 的非敏感标识、有效模型/模式、prompt/config 哈希、catalog omissions、usage completeness、cost_basis、停止原因；使用现有 Runs/活动/日志即可，不新建监控平台。故障检查点：MCP bootstrap failure（零模型调用）、resume failure、protocol error、usage unknown、timeout/cap stop。看板沿用 Runs 的成功率、耗时、token 与费用；未知值单列，不填 0。先通过真实 path 验收再决定额外告警阈值，不编造延迟/QPS 承诺。

## 8. 探测与用户入口

`probe` 在临时空 HOME/DSH_HOME/DSH_AGENTS_HOME、无 key 环境运行 `--version`、`--help`，必要时 `--profile headless --dump-config`；每个命令限时。只接受已验协议版本 `0.2.1-rc.2` 为可用；未知版本返回可诊断的不支持状态，不用简单 semver 大于关系保证兼容。不运行任务、登录或模型探测。[D1]

自动注册沿用 `_adapters / register_found_runtimes / probe_runtime`。Settings 的 Runtime 卡展示 DSH 和安装说明，Agent Profile 选择 DeepSeek Messages provider 与支持的模型；角色/项目/任务覆盖、Runs、Prompt Preview 复用现有入口。前端只改 runtime catalog、精确模型约束与原因显示，不新增页面。

CLI 的 serve/PATH 探测已泛化，无新专属参数。Telegram/Lark 与 Dashboard 共用 Manager/adapter，不改渠道协议和发消息逻辑；验证用 API 和替身渠道，不发测试到真实账号。若这些入口真实验收缺失，交付声明为未测。

## 9. 验收与边界

实现按小卡顺序，测试先写可观察契约再接实现；相关 pytest 用 `uv run --extra test python -m pytest`。PostgreSQL 只用 disposable DB；未配置数据库时 skipped 不是 pass。

1. **协议与负例**：合成 JSONL、实际 subprocess stand-in；正确 final、工具配对、重试/cache、不重复累计、缺用量、错误 final、无 final、非零退出、超长 final、坏 JSON、取消与时间上限。
2. **隔离与 bootstrap**：实际 dsh compose/官方模块 + 本地 fake MCP/LLM；缺 MCP 必须零请求；带 `{{...}}` 的 prompt 字面一致；自建临时“operator”目录放 marker 作为正向对照，证明测试方式能读到 marker 后再验证 run 不读它。不得写真实操作者目录。
3. **四角色与续接**：serve 启动、创建 Profile、MCP 调用、任务写文件和审查，API/DB 读回角色 runtime、owner、session、用量；Planner 与 resumed consult 的 mutation 被真实 guard 拒绝；两轮不重复费用；不同 owner 不串 Session。
4. **最小 UI**：headless browser 检查可见、可选、配对错误、append 原因；截图一张，前端 build。
5. **真实路径（最后执行，当前规划不执行）**：新 DB、非 8767 端口、throwaway runtime root、临时项目；确认 `sys.executable/looper.__file__`；启动本 worktree 的 serve，外部渠道全部关闭。只测 DeepSeek。
6. **真实评测（最后执行）**：`--register-agent` 指向 dsh，不用固定创建 Claude Profile 的 `--provider-preset`。`single_step,dependent_steps,reviewed_step,planner_plan_file` 各 reps 2；Manager `status,confirm_create_start` 各 reps 2，每 run 观测费用软上限 $0.50、每张评测总观测金额软上限 $4、单任务/对话 300 秒、外层 20 分钟、单并发。`--case-budget-usd` 在 jobs=1 不生效，本方案不传它，不承诺每 case 上限；辅助费用未知时这些上限不代表完整账单的硬保证。对照相同任务和有效 append 的已有 Claude/OMP 基线；不同模型只能作描述比较，不能归因为 runtime 改进。无可比基线写明，不更新公共 baseline 伪造对照。[evals/README](../../../evals/README.md)

真实 key 仅由验收 harness 在运行时以内存捕获 `zsh -ic 'printf %s "$DEEPSEEK_API_KEY"'`，再给被测 provider binding 或 eval 的 `api_key_env` 指定变量；不回显、不写文件、不进 subprocess argv/报告。spec 只含环境变量名。key 为空/命令失败则停止，不 fallback。脚本关闭 trace、避免输出捕获异常的 stdout 内容。真实产物只放 ignored 目录，公开报告只留脱敏汇总。

已完成的验证只有：已安装版本、README/代码只读核对；私有临时 home 下 `--help`、`--version`、headless/acp `--dump-config` 全部 exit 0。**没有调用模型，没有验证 adapter、bootstrap、真实 MCP、计费或 resume**。本轮只新增本文和仓库外任务卡，不修改计划状态、代码、operator 配置或记忆文件，不提交。
