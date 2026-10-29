# DSH consult 上下文对照（2026-10-29）

## 验证范围与结论

本次修复 runner consult 缺少 CLI 默认 system guidance 和工具声明的问题。
`forkSeed` 的已结束轮次边界修复是另一个问题，不能作为上下文差异或回答格式问题的修复证据。

隔离 serve 的实际链路为：Worker 接受 steer 并写入文件 → 返回工作报告 →
另一 Worker 向已离线的 seat 提问 → runner consult 分叉 → 回答入房间。
将同一源会话交给已安装 CLI consult 后，回归测试逐项比较完整 system prompt、
完整工具声明和历史消息；仅 CLI 切换 provider route 产生的、经格式校验的 model-change
notice 单独核对。旧实现的完整 system 比较失败，修复后通过。

真实模型对照使用该链路保存的**合成 Worker 历史**，不是生产故障会话。
同一已结束会话、同一问题，通过修改前后的真实 runner 分叉各执行两次完整 consult，
包括模型选择的文件工具调用。使用 DSH `0.2.1-rc.2`、`deepseek-flash`、`high`；
每请求最多输出 4096 token，每次 consult 最多 8 个请求。
发送给模型前只将工作站路径替换为 `/workspace`；返回的工具路径反向映射供 SDK 执行。
完整请求、SSE、最终回答及宿主用量事件保留在忽略的本地状态中，不进公开仓库。

**真实回答格式故障没有在这个样本中复现。** 修改前后均为 2/2 个有效 `answers`，
且都答对历史中的 token；没有出现 Worker report。此结果证明本样本经过了真实模型测量，
不能证明偶发格式故障的根因已定位，也不能据此宣称回答成功率得到改善。
仍需可重放的真实失败历史来验证该故障。

## 上下文与 token

| 指标 | 修改前 | 修改后 |
| --- | ---: | ---: |
| system 文本 UTF-8 字节 | 11,887 | 13,720 |
| 工具声明 JSON 字节（`json.dumps`） | 1,969 | 20,033 |
| 工具声明数 | 3 | 24 |
| consult contract 次数 | 1 | 1 |
| 第一个请求的实际输入 token（两次一致） | 4,035 | 8,621 |
| 有效 consult 回答 | 2/2 | 2/2 |

输入 token 是 provider 用量事件中未缓存输入、缓存读取、缓存写入之和，不是字节估算。
补齐默认上下文使本样本首请求增加 4,586 个输入 token，约 114%；这也是本次行为变化的成本。

| 版本 / 重复 | 模型请求数 | 累计输入 token | 累计输出 token | 结束状态 | 最终格式 |
| --- | ---: | ---: | ---: | --- | --- |
| 修改前 / 1 | 3 | 12,627 | 445 | completed | answers |
| 修改前 / 2 | 2 | 8,262 | 221 | completed | answers |
| 修改后 / 1 | 2 | 17,417 | 217 | completed | answers |
| 修改后 / 2 | 3 | 26,441 | 831 | completed | answers |

四次运行均 `usage_complete=true`。另用提交的重放器前后各做一次自检，均为有效 `answers`，
首请求输入仍分别为 4,035 / 8,621 token，累计输入为 12,555 / 35,617，输出为 366 / 528；
这两次自检未计入上表的两次重复。累计量受模型选用工具的次数影响，
首请求用量用于衡量固定历史上的上下文差异。格式判定使用生产解析器 `consult_answers`，
不是检测回答是否包含字符串 `answers`。

对照首请求除 `system`、`tools` 外的字段完全相同，包括完整 `messages`。
使用 `json.dumps(sort_keys=True, ensure_ascii=False)` 的 SHA-256：

| 内容 | 修改前 | 修改后 |
| --- | --- | --- |
| system | `87a188765ca0eb545d1c7177904332e743d1cc5f25baf2141a2e23f26d4b1a33` | `6c0c05c9d9fdb82b0f18f27f51446a70606fb2d153dd398ba0b21264c1e99b5f` |
| tools | `f9e1bc9914000bfc98e8b1c24c41fe31c3897c2beb6368b5735f7b1f7d1dd596` | `2d01bb3430e46a9cbe46041531a1301918d5d3c8966699f571c3a2eebb3226d5` |
| messages | `9042a5ae0da95db4f89872efc1d90dd484f6fce395fc8ae1086c24e9fbec37fa` | 相同 |

## 定向回归

指定的 8 个 DSH runner 测试文件以 `-n 2`、360 秒外层超时运行：
**171 通过，0 失败，0 跳过，193.96 秒**，包含独立数据库的 serve 链路。
完整 system 对照已确认在修改前失败；修复后还核对了完整 tools 和历史。
真实格式故障的失败历史、生产会话及真实渠道投递均未验证，不计为通过。

## 权限与复现

runner 从已安装 SDK 的独立临时 composition 采集默认指导和工具 schema，
释放该 composition 后才启动正常 runner。consult 只复制声明；除 `read`、`glob`、`grep`
外的声明注册为拒绝执行的工具，原有只读 guard 和路径检查继续生效。
回归覆盖声明存在但写文件、shell、子代理与联网仍被拒绝，源会话文件不变。
Dashboard、Telegram、Lark 共用 consult 后端；CLI 为对照路径，无独立入口改动。

`test_dsh_runner_task_with_every_role` 将完整请求和 `consult-source.json` 留在测试临时目录。
显式真实模型重放命令（仅使用隔离测试生成的合成数据）：

```sh
python -m evals.dsh_consult_replay \
  --capture <test-output>/consult-source.json --out .local/consult-after --reps 2
```

需要环境变量 `DEEPSEEK_API_KEY`，密钥只由 loopback 转发器持有，不交给 runner。
修改前版本可通过 `--runner-entry <local-baseline>/runners/dsh/src/main.ts` 指定完整 runner
代码副本。前后复用同一个源会话和问题，分别使用各版本的 consult system section；
所有输入、临时状态与输出必须位于当前 worktree。重放器记录每次完整请求、真实 SSE、
宿主事件和基于生产解析器的格式结果，并校验源会话哈希不变。
