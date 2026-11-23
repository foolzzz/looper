# 平台子 agent：服务与数据验收

对应 [平台子 agent RFC](../architecture/rfcs/platform-subagents-v0.zh-CN.md) 第 10 节的派生服务和数据切片，依赖迁移注册表 #1527。

## 实现边界

- 扩展现有 `agent_spawns`、`spawn.run_child`、`run_kind` 和 `parent_record_id`，不另建运行树表。
- 独立迁移文件声明顺序；升级先加未验证约束，再在线校验并建立唯一幂等索引。新库与升级库的约束一致。
- `SpawnService` 提供校验、任务行锁内预留、幂等、单独的任务并发类别、CAS、取消和级联、结果解析、费用归集及重启恢复。运行记录和派生声明提交以后才允许启动进程，首轮输入前必须持久化进程身份。
- 普通恢复只结束身份匹配的遗留进程；计划内移交的 keeper 运行接管原进程与原记录。恢复不调用启动路径。`finishing` 从记录收尾，`cancelling` 确认进程结束才释放预留。恢复与级联逐行处理终止失败，失败行保留预留并继续处理其他行；`finally` 回收遗留目录，保留其他活跃运行的检出和 home。
- 启动前在同一任务行锁内重读任务状态，并以 `FOR UPDATE` 锁定项目行读取开关。任务已结束或开关已关时，排队声明 CAS 为 `cancelled/task_ended`，不启动进程；并发位已满时也先执行此检查。
- Claude、DSH、Codex、coco、OMP、Grok、Pi 有专用 `EXPLORE` 启动约束；`CHECK` 运行时拒绝启动。工具列表限制型运行时使用含未提交改动的临时检出，不改变原 index 或分支；快照拒绝符号链接。
- 接管仅比较运行时、模型、只读模式及类型化工具边界，不因价格、settings 来源或录制路由变化终止健康进程；交接成功的 id 不进入第二轮取消。超时记录为 `judgment/timeout`，未知费用仍按整笔预算记账。
- 合并 main 后保留 `PLATFORM_TOOLS` 非只读、`tool_hook` 和 `EXPLORE` 只读；升级测试同时回滚 run tree、failure typing 与 explorer 依赖。main 新增的 6 项迁移按原顺序接入迁移注册表。
- explorer 默认使用 `claude-code/sonnet-5-5`、300 秒、40 轮、0.50 USD；结构校验失败保留有上限的原文并标记「未通过结构检查」。花费未知时保守占用其预算，不把未知成本当作免费运行。

项目开关默认为关。没有新增 MCP 工具、v1 操作、项目设置入口或 `chat_settings` 字段；无需新增操作级别声明。Planner、Worker、Manager 的接线与现有提示均未修改。Dashboard、Telegram、Lark 和 CLI 入口均保持现状。

## 自动验证

本轮评审修复完成后，在独立 disposable PostgreSQL 数据库上，只运行本 PR 涉及的 15 个测试文件，一次 pytest、`-n 2`：**213 passed、18 xfailed、1 failed，50.84 秒，无跳过**。

仓库外的临时 pytest 插件为关键回归同时装载修复前函数和修复版：18 个修复前用例全部按预期失败（strict xfail），覆盖逐行恢复/级联、finally 清理、状态与开关变化、项目行锁、权限规则、升级起点，以及四项小修；对应修复版全部通过。其余 5 个修复前变体作为未改变安全边界的正向对照通过。

唯一普通失败为 `test_existing_prompts_and_tool_listings_remain_byte_identical` 的 Manager 常量摘要：fixture 的基线早于本次合入的 main。已核对 Manager 文件与 `origin/main` 字节完全一致，并直接核对 main 声明的字符串与导入值后刷新摘要和基线提交。遵守只运行一次 pytest 的限制，刷新后未重跑该测试；不把本轮结果记为全绿。独立数据库已删除。

本 PR Python 新增行数（不含 fixture）：生产代码 3270 行，测试 1886 行。此前实现轮的 162 项通过是历史记录，不替代本轮结果。

覆盖类型化状态和约束、取消与完成各自抢赢的 CAS 竞态、同键去重、不同键并发预算竞争、失败后释放、级联范围、父运行软停、费用份额、升级恢复，以及真实替身进程与 keeper 接管。接管用例核对进程身份、记录数量、转录事件和费用均只出现一次。

关闭时对照包含既有 Planner、Worker、Manager 的提示和实际 MCP 工具列表，以及 7 种运行时 × 3 个角色的启动参数、配置和提示块字节摘要。费用响应也有开关两种状态的数据库测试。

## 真实路径（此前实现轮的记录，本轮未重新调用模型）

- Claude Code：工具仅 `Read`、`Glob`、`Grep`，MCP 为空，网页调用和原生派生均为 0；验证文件未变。此次调用费用 0.011668 USD。
- Codex：实际执行写文件命令，沙箱返回 `operation not permitted`、退出码 1；验证文件未变。注册工具中没有网页、MCP 或原生多 agent 工具。
- 一次性 `looper serve`：独立数据库、临时 runtime root、自动选择空闲端口和替身 CLI，认证后的 `system.status` 成功返回，服务正常停止。

其他运行时仅完成启动参数与配置验证，未调用其真实模型。Worker 工具、等待和推送、服务生命周期接线及运行树浏览器验收属于后续切片，本次没有把它们计为通过。账号准入与 explorer 的自动重试不在本切片；当前保存本层的类型化失败，保留 main 的共享失败分型能力。本轮没有真实模型、在线服务或浏览器调用。
