# ACP Reviewer：真实服务路径验证

2026-11-22，在 macOS 的一次性环境中验证。启动的是当前 worktree 的 `looper serve` CLI 路径，使用新建且独占的 PostgreSQL 数据库、空 runtime root 和空 HOME。监听空闲端口 53055，没有使用运行中的服务或操作者的 runtime root。结束后服务停止、数据库删除。

没有真实模型调用。Planner 和 Worker 使用 `evals/prompt_regression/fake_claude.py`；Reviewer 使用 `tests/fake_acp.py` 的 v1 stdio 替身。替身可执行文件名为 `grok`，接受 `agent stdio` 的已知命令形状，内部运行该测试脚本。PATH 不包含安装的模型 CLI。此验证证明服务接线和记录路径，不证明 Grok 的实际 ACP 握手或评审质量。

启动命令形状如下，所有变量均指向本次创建的临时资源：

```sh
python -c 'from looper.cli import main; main()' serve \
  --runtime-root "$ACP_RUNTIME_ROOT" --port "$ACP_PORT" --skip-backup
```

临时 ops 文件显式指定独占数据库；`--skip-backup` 只作用于这个全新数据库。独立 config dir 中的配置为：

```toml
[cli]
use_leader = false
```

通过该服务生成的 operator 凭据调用 v1 API：

1. `acp_reviewer.update` 写入 `enabled=true`、替身 argv、声明模型 `declared-model`、合成账户标签和独立 config dir。`policy_check.reason` 返回 `ok`。
2. `provider_accounts.create` 建立 `acp-command` 账户，`models.create` 建立精确匹配的模型，`profiles.create` 建立 `rt-acp` 的 Profile。
3. `agent_settings.update_role` 将全局 Reviewer 角色绑定该 Profile。
4. 创建一个独立的已提交 Git 仓库，文件 `note.txt` 内容为合成标记。启动只读检查任务：Worker 报告完成但不修改文件，独立 Reviewer 检查一次。
5. 从 `llm_records` 和 `llm_record_events` 读回记录并断言下列结果；再用 `acp_reviewer.update` 关闭开关，在另一份独立 Git 仓库启动相同任务。

| 读回项 | 开关开启 | 绑定后关闭开关 |
|---|---|---|
| 任务状态 | `completed` | `failed` |
| Reviewer 结局 | `passed` | `run_failed` |
| Reviewer 次数 | 1 | 首次即拒绝；Planner 的 2 次原有重试也同样拒绝 |
| `cost_usd` | SQL `NULL` | SQL `NULL` |
| 最终 result 的 `usage_complete` | `false` | 没有 result，进程没有启动 |
| `acp_write_policy.reason` | `ok` | `settings_refused` |
| ACP 会话 init 事件 | 有 | 3 次运行均无 |
| 评审工作目录 `git status --porcelain` | 空 | 空 |

成功运行的 `acp_command` 原样读回为：

```json
{
  "argv": ["grok", "agent", "stdio"],
  "model": "declared-model",
  "account": "ACP stand-in account",
  "config_dir_is_scratch": false
}
```

`acp_write_policy` 同时记录 `mechanism=macos-seatbelt`、`looper_mcp_injected=false`。替身没有上报用量，记录保留未知花费；这不影响合法裁决记为 `passed`。

关闭开关后，每次 Reviewer 的错误均为：

```text
Cannot start ACP reviewer (pilot): acp_reviewer.enabled is off
```

3 次记录均为 `run_failed`、没有会话 init，没有切换到其他 Reviewer runtime。Planner 按原有失败策略重试，最后将任务记为失败。此次验证没有配置 fallback；进入或离开 ACP 的 fallback 拒绝由针对性测试覆盖。

写拒绝的子进程、孙进程、检查根、Git 元数据和启动预检的独立测量见 [写策略验证](acp-reviewer-write-policy.md)。真实 Grok `agent stdio` 的一次握手/评审以及 RFC 的四任务对照仍待后续阶段，本次没有执行。

## 设置页浏览器检查

另一个全新数据库和临时服务（空闲端口 59941）上，用 headless Chromium 打开真实 `#settings/runtimes` 页面，展示默认关闭、空配置。检查没有横向溢出；非法 JSON 显示错误；重新加载恢复已保存值；键盘 Tab 从命令输入移到模型输入；没有浏览器脚本错误。服务和数据库均已清理。

- [1280 px 桌面截图](acp-reviewer-settings-1280.png)
- [390 px 手机截图](acp-reviewer-settings-390.png)

截图保留导航和完整 ACP 卡片，没有本机路径、密钥或真实账户。

## 最终回归

在合入最新 `main` 后，任务卡限定的十个 Python 测试文件以 `-n 2` 运行：**740 passed，0 skipped**（82.02 秒）。其中包含 macOS 子孙进程写保护测试，以及从 `origin/main` 源码归档运行的非空数据库 registry、chooser、servable roles 和配置导出对照。默认关闭的提示词、工具、命令行和进程签名另有固定哈希对照。

表单 Vitest **4 passed**，`npm run build` 成功；`web/dist` 未提交。Ruff 与 `git diff --check` 通过。上述证据均不替代真实模型握手或 RFC §8 的质量/花费比较。
