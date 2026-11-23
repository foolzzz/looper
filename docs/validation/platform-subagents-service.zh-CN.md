# 平台子 agent：服务与数据验收

对应 [平台子 agent RFC](../architecture/rfcs/platform-subagents-v0.zh-CN.md) 第 10 节的派生服务和数据切片，依赖迁移注册表 #1527。

## 实现边界

- 扩展现有 `agent_spawns`、`spawn.run_child`、`run_kind` 和 `parent_record_id`，不另建运行树表。
- 独立迁移文件声明顺序；升级先加未验证约束，再在线校验并建立唯一幂等索引。新库与升级库的约束一致。
- `SpawnService` 提供校验、任务行锁内预留、幂等、单独的任务并发类别、CAS、取消和级联、结果解析、费用归集及重启恢复。运行记录和派生声明提交以后才允许启动进程，首轮输入前必须持久化进程身份。
- 普通恢复只结束身份匹配的遗留进程；计划内移交的 keeper 运行接管原进程与原记录。恢复不调用启动路径。`finishing` 从记录收尾，`cancelling` 确认进程结束才释放预留。
- Claude、DSH、Codex、coco、OMP、Grok、Pi 有专用 `EXPLORE` 启动约束；`CHECK` 运行时拒绝启动。工具列表限制型运行时使用含未提交改动的临时检出，不改变原 index 或分支。
- explorer 默认使用 `claude-code/sonnet-5-5`、300 秒、40 轮、0.50 USD；结构校验失败保留有上限的原文并标记「未通过结构检查」。花费未知时保守占用其预算，不把未知成本当作免费运行。

项目开关默认为关。没有新增 MCP 工具、v1 操作、项目设置入口或 `chat_settings` 字段；无需新增操作级别声明。Planner、Worker、Manager 的接线与现有提示均未修改。Dashboard、Telegram、Lark 和 CLI 入口均保持现状。

## 自动验证

在独立 disposable PostgreSQL 数据库上，仅运行新增或修改的 13 个测试文件，使用 `uv run --system-certs --extra test python -m pytest -q -n 2`：162 项通过，无跳过。

覆盖类型化状态和约束、取消与完成各自抢赢的 CAS 竞态、同键去重、不同键并发预算竞争、失败后释放、级联范围、父运行软停、费用份额、升级恢复，以及真实替身进程与 keeper 接管。接管用例核对进程身份、记录数量、转录事件和费用均只出现一次。

关闭时对照包含既有 Planner、Worker、Manager 的提示和实际 MCP 工具列表，以及 7 种运行时 × 3 个角色的启动参数、配置和提示块字节摘要。费用响应也有开关两种状态的数据库测试。

## 真实路径

- Claude Code：工具仅 `Read`、`Glob`、`Grep`，MCP 为空，网页调用和原生派生均为 0；验证文件未变。此次调用费用 0.011668 USD。
- Codex：实际执行写文件命令，沙箱返回 `operation not permitted`、退出码 1；验证文件未变。注册工具中没有网页、MCP 或原生多 agent 工具。
- 一次性 `looper serve`：独立数据库、临时 runtime root、自动选择空闲端口和替身 CLI，认证后的 `system.status` 成功返回，服务正常停止。

其他运行时仅完成启动参数与配置验证，未调用其真实模型。Worker 工具、等待和推送、服务生命周期接线及运行树浏览器验收属于后续切片，本次没有把它们计为通过。账号准入和失败分型的自动重试依赖尚未实现的引擎能力；当前仅保存本层的类型化失败。
