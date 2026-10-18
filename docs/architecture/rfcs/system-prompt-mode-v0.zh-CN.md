# RFC：Agent Profile 的系统提示词模式（v0）

状态：实现中（v2.8.0）。相关：[多运行时 agent 架构](multi-runtime-agents-v0.zh-CN.md) 第 9 节（prompt）、[Grok 运行时](grok-runtime-v0.zh-CN.md)。

## 1. 背景与目标

Looper 给每个 agent 一份自己的系统提示词（`SystemPrompt`），四种运行时都是把它**追加**在运行时自带的默认系统提示词之后（Claude Code `--append-system-prompt-file`、Codex 家目录的 `AGENTS.md`、OMP `--append-system-prompt`、Grok 家目录的 `rules/looper.md`）。默认提示词里有编码助手的通用行为（工具用法、输出风格、拒绝引用自己的提示词等），对 Looper 的角色来说是多余的，有时还和角色的约束冲突。

目标：每个 Agent Profile 可以选择「追加」或「替换」运行时的默认系统提示词。

## 2. 决定

1. 设置放在 Agent Profile 上：`system_prompt_mode`，取值 `append`（默认，和今天逐字节一致）或 `replace`。
2. 运行时不能替换默认系统提示词时，保存 Profile 就被拒绝（HTTP 400，写明原因）；界面把选项置灰并显示原因。**不静默退回追加。** 已存的 `replace` Profile 被改到不能替换的运行时，同样被拒绝，不会被悄悄改回 `append`。
3. 能不能替换是适配器的类型化属性（`RuntimeAdapter.can_replace_system_prompt`，与 `run_input` 一样放在适配器上，不进数据库）。一个运行时只有实跑探针通过才标为可替换。
4. 每次运行的 prompt 记录里存它运行时用的模式，运行页显示。
5. 随 v2.8.0 发布；默认不变，替换是显式选择，没有默认行为变化。

## 3. 各运行时怎么替换（本机实测，2026-10-18）

探针：替换用的提示词里放一个只有它才有的标记（`ZEBRA-7731`）和一个名字，问模型「你的名字和标记」，并问它引用自己系统提示词的第一句（默认提示词会拒绝）。追加模式同样探一遍。

| 运行时 | 替换 | 追加（现状） | 说明 |
| --- | --- | --- | --- |
| Claude Code 2.1.285 | `--system-prompt-file <文件>` | `--append-system-prompt-file <文件>` | 两种都保留 `--system-prompt-snapshot off` |
| Codex 0.162.0 | 家目录 `config.toml` 的 `model_instructions_file`，指向存放提示词的文件；家目录的 `AGENTS.md` 不再放提示词 | 家目录 `AGENTS.md` | 替换后模型自称不再是 Codex，只回答替换提示词里的名字和标记；追加时两者并存 |
| Grok 1.0.62 | `--agent <定义文件>`，文件前置元数据 `prompt_mode: full`，正文是提示词；家目录不再写 `rules/looper.md` | 家目录 `rules/looper.md` | `--system-prompt-override` 在 `-p` 下不可靠：输入 token 不变，默认提示词还在（仍拒绝引用自己的提示词），因此不用它 |
| OMP 18.4.3 | `--system-prompt <文件>` | `--append-system-prompt <文件>` | 替换后自称只回答替换提示词里的名字，追加时自称 omp 的助手 |

替换后保留什么：

- 运行时的工具定义不变（它们不属于系统提示词）。
- Claude Code：项目的 `CLAUDE.md` 仍然加载；自动记忆的 `MEMORY.md` 索引仍作为上下文注入（`autoMemoryEnabled` 打开时），但默认提示词里「怎么写记忆」的说明没有了。Looper 的 Claude Code 运行带 `--setting-sources project` 和 `autoMemoryEnabled: false`，所以运行里本来就没有自动记忆。
- Codex、Grok、OMP 的运行时说明（`runtime_notes`，如 MCP 工具怎么调用）仍随 Looper 的提示词一起放进替换用的文件。

## 4. 数据与接口

- `agent_profiles.system_prompt_mode TEXT NOT NULL DEFAULT 'append' CHECK (system_prompt_mode IN ('append', 'replace'))`，`ADD COLUMN IF NOT EXISTS`，已有行得到 `append`。
- Profile 接口（`/api/agent-profiles`、`/api/registry`）读写该字段；保存时按运行时能力校验；配置导出/导入带上它，缺省为 `append`。
- 管家的 Profile 配置工具加同名字段，工具的 schema 和描述对模型可见。
- `Launch.system_prompt_mode` 由 Profile 给出，适配器的 `resident_command`、`run_command` 据此布置命令。

## 5. 待办与风险

- 替换会丢掉默认提示词里的安全与行为约束，由 Looper 的提示词自己承担；所以它是 Profile 级的显式选择，且不做任何自动切换。
- 替换模式下的质量用 haiku 全任务 `--reps 3`（每个角色都用替换模式）评测，结果写在 PR 里。

## 6. 系统提示词的分块、自定义文本与预览（v2.9 方案）

需求：能自定义 Claude Code 的「第一段」和「第二段」系统提示词，并能预览。先抓包确认运行时到底发了什么，再决定能做什么。

### 6.1 实测：各运行时发出的系统提示词分块（2026-10-18）

方法：只记录请求体里系统提示词字段的本地抓包服务，不转发、不记请求头、回错误；真实 CLI 通过 `ANTHROPIC_BASE_URL` / Codex 的自定义 provider 指向它，用占位 key 和临时配置目录，没有用任何真实凭据。

**Claude Code 2.1.285**（请求的 `system` 是三个文本块）：

| 块 | 内容 | 缓存 | 谁决定 |
| --- | --- | --- | --- |
| 0 | `x-anthropic-billing-header: cc_version=…; cc_entrypoint=sdk-cli;` | 无 | 固定，随版本 |
| 1 | 身份行：不带追加参数时 `You are a Claude agent, built on Anthropic's Claude Agent SDK.`；带 `--append-system-prompt[-file]` 时 `You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK.` | ephemeral | 固定二选一，由「是否追加」决定 |
| 2 | 主块。默认：约 5.8k 字符的默认提示词（追加时再接上追加文本）；`--system-prompt-file`：只有该文件的内容；`--agent <名>`：只有该 agent 的正文 | ephemeral | 标志 |

- 同时给 `--system-prompt-file` 和 `--append-system-prompt-file`，主块是两者按序拼接，身份行取追加那一种。
- `--agent` 加 `--append-system-prompt-file`、`--bare` 的结果也是这三块，没有第四块；`--bare` 下主块只多 `CWD`、`Date` 两行。
- **第一段（身份行）不能自定义**：二进制里只有三个常量（交互式、追加、非交互无追加），选哪个由「是否交互、是否追加」决定，没有标志、环境变量或设置项能改它的文字。`--system-prompt-snapshot` 只是把首次渲染的整份提示词记下来沿用，不提供文字入口。
- **第二段（主块）可以自定义**：替换（`system_prompt_mode=replace`）时它就是 Looper 提示词本身。

**Codex 0.162.0**（Responses 请求，`instructions` 字段为空，系统提示词在 `input` 的前几条消息里）：

| 序 | 角色 | 内容 | 谁决定 |
| --- | --- | --- | --- |
| 1 | developer | 基础指令（默认约 17.5k 字符）；`model_instructions_file` 会整份替换它 | 家目录 `config.toml` |
| 2 | developer | 技能、权限等运行时说明（约 30k 字符） | 运行时，不可改 |
| 3 | user | `# AGENTS.md instructions` + 家目录与项目的 `AGENTS.md` | 家目录 `AGENTS.md` |
| 4 | user | 环境（cwd、shell、日期） | 运行时 |

追加时 Looper 的提示词在第 3 条；替换时它在第 1 条。

**Grok 1.0.62、OMP 18.4.3**：没有抓包。Grok 走自己的后端和 OAuth 登录，没有可替换的 API 地址以安全的方式抓；OMP 未抓。它们的分块只依据第 3 节的行为探针，不在此声称更多。

### 6.2 决定

1. **只加一个字段：Agent Profile 的 `custom_prompt`**（文本，默认空）。它是 Looper 拼好的主提示词里的一层，放在**最前面**（在 harness 之前）：Claude Code 的主块（第二段）、Codex 的 `AGENTS.md` 或替换文件、Grok 的 `rules/looper.md` 或 agent 定义、OMP 的追加/替换文件都以它开头。`custom_prompt` 是可信操作者输入，由能编辑 Profiles 的人写入，与 harness 具有相同的信任级别。契约仍在最后，但这个顺序不保证契约优先于前面的文字：模型仍可能遵循 `custom_prompt` 中的冲突指令；模型是否遵守契约未测试。
2. **第一段不提供字段**：Claude Code 的身份行没有入口（见 6.1），为一个所有运行时都拒绝的字段增加接口和界面没有意义。想让模型从头采用自己的身份，用 `system_prompt_mode=replace`，再把身份写进 `custom_prompt`；替换后身份行变成固定的 `You are a Claude agent, built on Anthropic's Claude Agent SDK.`，这一行仍然存在，文档和预览如实标注。
3. 与 harness、角色提示词的分工：harness 是性格和稳定性（按 Agent 类型与项目），角色提示词是角色做什么（按角色），`custom_prompt` 是**这个 Profile**无论跑什么角色都要带的文字（口径、语言、对外身份）。
4. 默认空：空串不加任何文字，也不进 `prompt_layers`，启动参数和提示词文件与 v2.8.0 逐字节一致（有测试固定）。非空时 `prompt_layers.custom_prompt = {sha256}`。
5. 校验：必须是不含 NUL 字符的字符串，长度不超过 harness 的上限（`HARNESS_MAX_CHARS`），含 NUL、超过上限或类型不对返回 400。四种运行时都能带这一层，所以没有运行时因为它被拒绝；以后出现不能带的运行时，沿用 `system_prompt_mode` 的做法（适配器的类型化属性，保存时 400，不静默丢弃）。
6. 管家的 Profile 配置工具已经编辑 `system_prompt_mode`，所以同样加 `custom_prompt`（schema 和描述对模型可见）。

### 6.3 预览

- `GET /api/agent-profiles/<id>/prompt-preview?role=<planner|worker|reviewer|manager>[&project_id=<项目>]`：返回这个 Profile 跑该角色时**会发出的所有块**，按发送顺序，每块带 `owner`（`looper` 或 `runtime`）。`looper` 块带全文；`runtime` 块只带说明，如「Claude Code 的默认提示词（不展示）」「Claude Code 的身份行（固定，随追加/替换变化）」「Codex 的技能与权限说明（不展示）」。
- 预览与真实启动走同一份代码：Looper 提示词用同一个 `build_system_prompt`；运行时怎么摆放（哪个文件、哪个参数、哪个块）由适配器的同一个方法给出，真实的 `resident_command`/`run_command` 写文件时调用它，预览调用它而不是另写一份。有测试对同一输入比较两者。
- 任务段（`## Task`）在启动时才有，预览不带，在块里标「启动时填入」；给了 `project_id` 时带项目背景、知识索引和项目对 harness 的覆盖。
- 界面：Agent Profile 页每个 Profile 一个「预览系统提示词」面板，按角色选，列出各块、标注 Looper / 运行时。
