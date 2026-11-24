# 任务结果中的验收证据

2026-11-23，在全新 PostgreSQL 数据库、临时 git 项目和临时运行根上启动隔离的 `looper serve`。所有 agent 均使用仓库的确定性 CLI 替身，没有调用真实模型，也没有向外部聊天发送消息。

真实检查使用项目的 `acceptance_checks = local`：检查在私有检出中读取 Worker 写出的文件，完成示例的检查退出 0；失败示例写入不符合标准的内容，检查退出 1，重试预算为 0。两种任务都从公开任务 API 创建并启动，结果随后从任务详情 API 读回，再用实际 Dashboard 渲染。

| 验证项 | 结果与记录 |
|---|---|
| 完成结果 | 四段模型结果后出现代码生成的 `Evidence`，流程记录仍在最后；[桌面 1280 px](completed-1280.png)、[手机 390 px](completed-390.png) |
| 失败结果 | 结束说明后出现 `Evidence`，明确显示 `failed`；[桌面 1280 px](failed-1280.png)、[手机 390 px](failed-390.png) |
| 步骤卡 | 既有 Evidence 显示标准、检查、Passed、退出码和短哈希；[桌面](step-card-1280.png)、[手机](step-card-390.png) |
| 渠道完成通知 | 实际 `CHAT_NOTICE` 总线消息包含 `Checks: 1 passed`，不包含证据段、检查命令和输出；[消息正文](completed-notice.txt) |
| 渠道失败通知 | 实际 `CHAT_NOTICE` 总线消息包含 `Checks: 1 failed`；[消息正文](failed-notice.txt) |
| 运行中关闭开关 | 服务 smoke 测试在检查通过后的用户关卡暂停，经公开 API 关闭项目开关，再回复关卡；完成结果仍显示已经记录的证据 |

通知的接收者为合成标识，渠道二进制为替身；记录中的 Dashboard 地址替换成示例域名。截图没有本机目录或私人数据。网页构建通过 `npm run build` 完成；无头 Chromium 在两种视口下打开任务页并检查 `Evidence` 已渲染。Dashboard 的 SSE 长连接不会进入持续空闲，截图等待具体标题渲染。

新增服务 smoke 的三个参数分支通过（完成、失败、记录证据后关闭开关）：

```sh
uv run --system-certs --python 3.11 --extra test python -m pytest \
  tests/test_acceptance_service_smoke.py -k persists_result -q -n 2
```

真实模型验证：本次没有修改 prompt 或契约，不需要新增付费评测。证据门的模型可见变更由独立评测工作覆盖。
