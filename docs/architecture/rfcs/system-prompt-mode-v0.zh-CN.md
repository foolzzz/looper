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
| OMP 18.4.3 | `--system-prompt <文件>` | `--append-system-prompt <文件>` | 探针结果见 PR |

替换后保留什么：

- 运行时的工具定义不变（它们不属于系统提示词）。
- Claude Code：项目的 `CLAUDE.md` 仍然加载；自动记忆的 `MEMORY.md` 索引仍作为上下文注入（`autoMemoryEnabled` 打开时），但默认提示词里「怎么写记忆」的说明没有了。Looper 的 Claude Code 运行带 `--setting-sources project` 和 `autoMemoryEnabled: false`，所以运行里本来就没有自动记忆。
- Looper 自己的提示词里写给 Claude Code 的运行时说明（Codex 的 `runtime_notes` 等）仍随提示词一起放进替换文件。

## 4. 数据与接口

- `agent_profiles.system_prompt_mode TEXT NOT NULL DEFAULT 'append' CHECK (system_prompt_mode IN ('append', 'replace'))`，`ADD COLUMN IF NOT EXISTS`，已有行得到 `append`。
- Profile 接口（`/api/agent-profiles`、`/api/registry`）读写该字段；保存时按运行时能力校验；配置导出/导入带上它，缺省为 `append`。
- 管家的 Profile 配置工具加同名字段，工具的 schema 和描述对模型可见。
- `Launch.system_prompt_mode` 由 Profile 给出，适配器的 `resident_command`、`run_command` 据此布置命令。

## 5. 待办与风险

- 替换会丢掉默认提示词里的安全与行为约束，由 Looper 的提示词自己承担；所以它是 Profile 级的显式选择，且不做任何自动切换。
- 替换模式下的质量用 haiku 全任务 `--reps 3`（每个角色都用替换模式）评测，结果写在 PR 里。
