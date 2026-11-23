# ACP Reviewer 写策略验证

2026-11-23，在 macOS 上用本地固定 Python 探针和 `sandbox-exec` 验证；没有调用模型、运行真实 agent CLI 或访问运行中的 Looper 服务。探针和监听器全部由 Looper 或测试创建。

## 当前策略与边界

```scheme
(version 1)
(allow default)
(deny file-write*)
(deny job-creation)
(deny network-outbound (remote unix-socket))
(allow network-outbound (remote unix-socket (path "/private/var/run/mDNSResponder")))
(deny network-outbound (remote ip "localhost:*"))
; 仅存在配置的本地代理时，增加对应端口，例如：
; (allow network-outbound (remote ip "localhost:<proxy-port>"))
(deny lsopen)
(deny appleevent-send)
(allow file-write* (subpath "<scratch>"))
(allow file-write* (subpath "<config>"))
(allow file-write* (literal "/dev/null"))
```

通过绝对路径 `/usr/bin/sandbox-exec` 安装策略，子进程继承。文件系统写操作只对新 scratch、独立 config 和 `/dev/null` 放行；继承的标准输出、标准错误和管道仍可使用。这不是磁盘读取隔离或完整的网络隔离：公网连接、DNS 和通过预检的代理仍可使用。Unix socket、其他回环端口、LaunchServices 和 Apple events 的拒绝阻止通过这些入口委托进程树外的常驻服务写盘。

代理端口只从服务进程自身 `os.environ` 的 HTTP(S)/ALL_PROXY（含小写变量）解析，绝不读取 `Launch.environment()` 或角色、项目、任务环境层。启动环境清除分层代理覆盖，并恢复同一份服务环境快照（含 NO_PROXY）；只对本地代理开放对应端口。没有新增设置或操作，`chat_settings`、操作权限与 action level 不变。

## 启动前保护集与预检

`acp_policy.prepare_policy()` 在 agent spawn 前执行；任何检查失败都拒绝启动，不回退：

1. 严格解析工作树根、gitdir、common gitdir、inspect roots、runtime root 和全局配置的 vault。verify 报告中的外部文件父目录、符号链接目标和逃出工作目录的相对路径仍进入保护集；只读取结构化路径。
2. 用 `(st_dev, st_ino)` 祖先链双向判断 scratch/config 与完整保护集是否相交。文件系统身份检查覆盖大小写别名和符号链接，不依赖 `Path.is_relative_to()` 的字符串大小写；也拒绝把 HOME 本身或其祖先作为可写目录。
3. 检查命令和配置来源，见下一节。
4. 在 Looper 自有临时目录创建文件探针，记录 inode；绝不在 Worker 报告的目录中创建文件或写打开报告中的路径。沙箱探针对文件类型和 inode 做比对，再以 `O_NONBLOCK | O_NOFOLLOW` 尝试写打开，必须得到 EPERM，不写任何字节；scratch 写入必须成功。只读 inspect root 和 FIFO 不会阻塞预检。
5. Looper 创建 Unix socket 和回环监听器，宿主进程先成功连接，作为正向对照。沙箱内的两个固定连接探针都必须以 EPERM 失败；任一成功、超时或其他错误均记 `write_policy_unavailable`，拒绝启动。探针和监听器随作用域退出清理。
6. 对服务环境中每个不同的代理，从宿主显式发出到 Looper 自建监听器的 CONNECT 和普通 HTTP GET（绝对 URI），分别使用 `127.0.0.1`、`::1`、`localhost` 和 `0.0.0.0`，不受 NO_PROXY 影响；每次请求都使用独立监听器，先按目标地址完成并清空正向对照连接。代理返回 2xx，或监听器收到代理转发的连接（即使返回 403），均以 `proxy_loopback_allowed` 拒绝启动。只有明确的 403 且监听器无连接才通过；断连、超时、认证失败及其他响应均以 `proxy_preflight_unavailable` 拒绝。HTTP(S) 代理支持 URL 中的 Basic 认证；未知协议和无效 URL 以 `unknown_cli_config` 拒绝，不在记录里保存代理地址或凭据。宿主探针避免沙箱自身的网络拒绝掩盖代理能力。

这些探针证明的是启动时所测目标被拒绝；它们不能代替代理端持续执行目标访问控制，或证明所有地址别名和未来配置都安全。操作者仍需使用拒绝本地目标的代理。

Linux、缺少系统 `sandbox-exec` 或无法证明强制性的主机拒绝启动。

## 配置边界

v0 只识别 `grok agent stdio`，可带已核验的模型、effort 和 `--no-leader` 参数。选项值必须非空且不以 `-` 开头；`--always-approve`、共享 leader、额外 profile/plugin 和未知参数均拒绝。命令中的模型不被改写，不合成 Grok 配置。

独立 config dir 只允许普通非符号链接的 `config.toml` 和可选 `auth.json`；其他条目（包括 `.grok`、`.claude.json`）拒绝。config 必须可读、可解析；只接受已核验的 `cli` 和 `mcp_servers` 表：

```toml
[cli]
use_leader = false

[mcp_servers.reader]
command = "reader-process"
args = []
```

每台 MCP 必须使用 stdio 子进程；URL、HTTP、SSE 和未知 transport 拒绝。cwd 到 Git 根之间的 `.grok` 和 `.mcp.json` 拒绝，继承的配置覆盖变量被清除，HOME/GROK_HOME 指向已检查的 config dir。新的空 scratch 没有可核验配置，所以空 config_dir 仍拒绝启动。

由于 ACP 的 tokens/cost_usd 仍未知，不能执行单次或任务预算上限；配置任一上限时在 spawn 前以 `budget_cap_unsupported` 拒绝，运行记录保留原因。

## 回归证据与缺口

`tests/test_acp_policy.py` 覆盖大小写路径双向重叠、runtime/vault/HOME、配置白名单、选项值、文件 inode、只读/FIFO、子孙进程写保护和 IPv4/IPv6 代理端口例外。覆盖真实 Reviewer 启动路径中的角色层代理注入、全部六个服务代理变量、安全代理可用、CONNECT 转发和伪装 403、断连、未知协议、认证与 NO_PROXY 绕过探针的防护。普通 HTTP 回归使用拒绝全部 CONNECT、仅对指定目标的 GET 转发或返回异常响应的本地替身代理；四种目标各覆盖实际转发、转发后伪装 403、无转发但返回 200、502 和断连，共 20 例在修复前全部错误放行。安全代理必须对全部八次请求返回拒绝，认证头也覆盖 GET。分别移除 Unix socket 和回环拒绝规则，预检必须拒绝启动。固定 Python 探针不运行模型。

任务 Profile 选择在共同的 `profile_choices` 入口拒绝 ACP；Manager 创建/更新工具、task agents API 和 create-time profile_overrides 均有参数化拒绝与数据库无写入回归。ACP channel 的 `fs/write_text_file` 与 `terminal/*` 请求均有拒绝测试。

真实 Grok 在这套剖面下能否正常工作、ACP 握手、权限请求和用量字段仍待维护者安排真实模型验证；本地探针通过不代表模型验收通过。
