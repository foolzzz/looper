# 账本页面验收

以下证据只使用合成事项、一次性数据库和隔离服务（18771），没有调用模型或向外部渠道发送消息。

- 定向 Python 回归：583 个不同用例通过；首次组合运行的 JSONB 测试数据错误已修复，两个账本文件重跑 17/17 通过。无跳过。
- 前端：五个文件合计 52 项 Vitest 通过，完整 TypeScript/Vite 构建通过。
- 真实 HTTP：开关、三类新增、事件身份读回、编辑、备注、完成、撤销、重开和确认；有源状态动作 409、来源跳转、client 写入 403 且无事件副作用、分页及新进程读回。
- 真实浏览器：三类表单写入并读回、分页、筛选、深链、浏览器返回及键盘 Enter 打开、Escape 关闭抽屉并回焦；1280×900、390×844、768×1024、844×390 无页面横向溢出。粗指针目标至少 44px，表单字号至少 16px，减少动效模式可用。
- 读取失败的三个截图明确采用响应注入；其余截图来自真实服务与数据库状态。空态使用清空后的合成数据，关闭态通过真实开关读取。

| 场景 | 桌面 1280×900 | 手机 390×844 |
|---|---|---|
| 有数据、缺 My ID、真实待处理数量 | [桌面](desktop-data-missing-identity.png) | [手机](phone-data-missing-identity.png) |
| 同页详情与事件时间线 | [桌面](desktop-drawer.png) | [手机](phone-drawer.png) |
| 真空态 | [桌面](desktop-empty.png) | [手机](phone-empty.png) |
| 关闭态，无账本条目请求或导航 | [桌面](desktop-off.png) | [手机](phone-off.png) |
| 长文本换行及安全引用 | — | [手机](phone-long-text-safe-links.png) |
| 筛选为空 | — | [手机](phone-filtered-empty.png) |
| 加载中（注入） | — | [手机](phone-loading-injected.png) |
| 读取失败（注入） | — | [手机](phone-error-injected.png) |
| 无权访问（注入） | — | [手机](phone-forbidden-injected.png) |

真实渠道发送与付费模型评测未执行；本次没有模型可见内容变化。构建保留已有的大包提示；前端定向 lint 无错误、有 7 条建议（Provider/hook 共文件、异步状态同步、可变 ref 清理、到期天数计算和表达式风格）。Python 定向 lint 通过。
