# ACP Reviewer 写策略验证

2026-11-22，在 macOS 26.6.2（25G83）上完成本地命令测量。没有模型调用，没有访问运行中的 Looper 服务。下面的目录全部由测量创建，路径和任务标签均为临时值。

## 策略与测量命令

候选策略先实测，随后才接入启动路径：

```scheme
(version 1)
(allow default)
(deny file-write*)
(deny job-creation)
(allow file-write* (subpath "<scratch>"))
(allow file-write* (subpath "<config>"))
(allow file-write* (literal "/dev/null"))
```

`<scratch>`、`<config>` 在实际命令中替换成 `Path.resolve()` 得到的绝对路径。没有放开其他设备节点。管道和 Python 子进程能运行，写 `/dev/null` 成功。

以下是测量的可复现命令；它自己创建临时目录，`probe.py` 先作为子进程运行，再启动同一脚本作为孙进程：

```python
from pathlib import Path
import json, os, subprocess, sys, tempfile

base = Path(tempfile.mkdtemp(prefix="acp-write-measure-")).resolve()
for name in ["worktree/.git", "inspect", "outside", "scratch", "config"]:
    (base / name).mkdir(parents=True)
profile = "(version 1)(allow default)(deny file-write*)(deny job-creation)"
for name in ["scratch", "config"]:
    profile += "(allow file-write* (subpath " + json.dumps(str(base / name)) + "))"
profile += '(allow file-write* (literal "/dev/null"))'
(base / "policy.sb").write_text(profile)
(base / "probe.py").write_text('''import json, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1]); level = int(sys.argv[2]); results = {}
for name in ["worktree", "worktree/.git", "inspect", "outside", "scratch", "config"]:
    try:
        (root / name / f"probe-{level}").write_text("test")
        results[name] = "written"
    except OSError as error:
        results[name] = error.errno
with open("/dev/null", "w") as output:
    output.write("test")
print(json.dumps({"level": level, "writes": results}), flush=True)
if level == 1:
    subprocess.run([sys.executable, __file__, str(root), "2"], check=True)
''')
result = subprocess.run([
    "sandbox-exec", "-f", str(base / "policy.sb"), sys.executable,
    str(base / "probe.py"), str(base), "1",
], capture_output=True, text=True)
print(result.returncode, result.stdout, result.stderr)

label = "acp-policy-measure-" + str(os.getpid())
job = subprocess.run([
    "sandbox-exec", "-f", str(base / "policy.sb"), "launchctl", "submit",
    "-l", label, "-o", str(base / "scratch/stdout"),
    "-e", str(base / "scratch/stderr"), "--", "/bin/sh", "-c",
    "echo escaped > " + str(base / "scratch/escaped"),
], capture_output=True, text=True)
exists = subprocess.run([
    "launchctl", "print", "gui/" + str(os.getuid()) + "/" + label,
], capture_output=True).returncode == 0
print(job.returncode, repr(job.stdout), repr(job.stderr), exists,
      (base / "scratch/escaped").exists())
```

| 探针 | 子进程 | 孙进程 |
|---|---|---|
| 写工作树 | `errno=1`，EPERM | `errno=1`，EPERM |
| 写 `.git` | `errno=1`，EPERM | `errno=1`，EPERM |
| 写检查根 | `errno=1`，EPERM | `errno=1`，EPERM |
| 写以上目录之外 | `errno=1`，EPERM | `errno=1`，EPERM |
| 写 scratch | 成功 | 成功 |
| 写独立 config | 成功 | 成功 |
| 写 `/dev/null` | 成功 | 成功 |

整个探针退出码为 0。`launchctl submit` 退出码为 1，stdout/stderr 均为空；任务不存在，标记文件不存在。`job-creation` 限制没有阻止普通子进程创建。

## 每次启动的预检

`acp_policy.prepare_policy()` 在 agent 的 `proc_tree.spawn` 之前执行：

1. 解析工作树根、gitdir、common gitdir 和所有 inspect roots；解析失败写明路径，不启动。保护集也包含 verify 报告中的外部文件父目录、指向外部的符号链接目标及逃出工作目录的相对路径；只读取结构化路径，不从报告正文猜路径。
2. 验证 scratch、config dir 与保护集双向不重叠。
3. 检查已知命令和配置来源。
4. 对每个保护目录创建空探针文件，先在宿主进程中以追加模式打开、立即关闭，证明文件本来可写。随后用同一沙箱策略启动固定 Python 探针：对这些文件的写打开必须全部返回 EPERM；scratch 写入必须成功。写打开不写任何字节，设施失效时也不会改动已有内容。所有探针文件在 `finally` 中删除。
5. 任何预检失败均记 `write_policy_unavailable`，显示 “write policy unavailable”，不启动 agent、不回退。

接线后的直接 smoke 再次通过：真实 `prepare_policy` 成功，scratch 写入成功，工作树写入 EPERM，前后 `git status --porcelain` 均为空。自动测试还覆盖子进程/孙进程、Git 元数据和外部检查根。

## 配置边界：保守的机械检查

v0 只识别 `grok agent stdio`，可带已核验的模型、effort 和 `--no-leader` 参数。它不修改 argv 中的模型，不合成 Grok 配置。`--always-approve`、共享 leader、额外 profile/plugin 和其他未知参数均拒绝。

独立 config dir 必须有可读、可解析的 `config.toml`。最小示例：

```toml
[cli]
use_leader = false

[mcp_servers.reader]
command = "reader-process"
args = []
```

只接受已核验的 `cli` 和 `mcp_servers` 表。每台 MCP 必须有 stdio 子进程 `command`；URL、HTTP、SSE、未知 transport 或不可读配置均拒绝。空 config dir 设置表示 scratch，但新的 scratch 没有可核验配置，当前会保守地显示 `unreadable_config` 并拒绝启动；操作者可以配置独立最小目录。

用临时 HOME/GROK_HOME 执行 `grok --help`、`grok agent --help` 和 `grok inspect --json`，没有握手或模型调用。帮助确认 `--no-leader`、`--leader` 和 `--leader-socket` 的行为。`inspect` 的两个合成配置实验发现：即使关闭 Claude/Cursor MCP 兼容开关，项目 `.grok/config.toml` 和 `.mcp.json` 仍会进入 MCP 列表。另一组把这两项放在 Git 根以上的父目录，`mcpServers` 为空、配置来源只有独立 HOME。因此启动检查拒绝 cwd 到 Git 根之间的这些额外来源，清除继承的配置覆盖变量，并把 HOME/GROK_HOME 指到检查过的 config dir。

Settings 展示机械检查的 typed reason；真正启动时加上实际 cwd 和完整保护集重新检查。该机制不使用人工承诺字段。它按已知配置来源保守拒绝；新的 CLI、配置格式或读取来源必须单独核验后才能加入。

Linux 和没有 `sandbox-exec`、或执行强制性预检失败的主机均拒绝启动。本次没有交付或声称验证过 Linux 写策略。真实 Grok ACP 握手、权限请求和用量字段仍待下一阶段验证。
