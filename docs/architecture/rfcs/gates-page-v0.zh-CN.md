# RFC：User Gates 页（v0）

- **状态：** 待批准（D1–D8 是按维护者的需求定下的建议做法，理由写在各条里）。
- **相关：** [用户关卡 RFC](user-gate-v0.zh-CN.md)（第 446 行把「侧栏的等待数」列为非核心待办，本 RFC 做掉它）；[agent-system.md 的 User gates](../agent-system.md#user-gates)；[docs/design.md 的界面设计](../../design.md#界面设计web)。
- **最近更新：** 2026-10-20

## 1. 背景与目标

关卡今天只在两处出现：任务页的关卡面板，和 Dashboard 的「Waiting for you」（只列运行中任务里等用户的关卡，最多 10 个，见 `web/src/components/dashboard-gates.tsx`）。用户看不到一共有多少关卡、哪些在等、哪些已经关了。

**目标：** 左侧栏加一项「User Gates」，打开一页，列出所有任务的关卡，默认只看没关的，一眼看出有几个、哪些在等自己；等自己的关卡就地回复，走现有的回复接口。

**不做：** 新的回复或结束讨论的接口（结束讨论仍只在任务页）；渠道（Telegram、Lark）和 CLI 的改动（管家已经能用 `db.user_gates.open_gates` 列出等待的关卡，这页不改变它）；实时推送。

## 2. 决定

**D1 新接口 `GET /api/gates`。** 现有接口都按任务取（`GET /api/tasks/:id` 的 `gates`），跨任务列出要每个任务一次请求，Dashboard 的做法（先取 needs_you 任务再逐个取详情，`dashboard-gates.tsx` 的 `DashboardGates`）只适合 10 个以内。`db.user_gates.open_gates` 只给管家用、只有运行中任务的等待关卡、没有分页，不改它。`/api/` 下的路由本来就只给 operator（`operator_auth.AREAS`），不用新的鉴权代码。

**D2 筛选。** `state`：`open`（默认，状态 `open` 或 `discussing`，且任务不在 completed、cancelled 组：那些任务的关卡不再收消息，`gate_service.post_message` 返回 409）、`closed`（`passed`、`cancelled`、`skipped`）、`all`（全部，包括还没打开的 `pending` 和已结束任务里没关的）。`waiting_on`：`user` 或 `planner`，只能和 `state=open` 一起用，否则返回 400。`project_id`：按项目筛选。

**D3 `waiting_on` 和页面上看到的一致。** 用 `gate_service.gate_view` 算（含失败 turn 的自动重试），再按 `web/src/lib/gates.ts` 的 `gateWaitsOn` 的规则：任务不在运行（不在 needs_you、in_progress 组）时，`planner` 算成 `user`（停掉的任务把消息留到重启，这时轮到用户）。这样筛选、计数和侧栏徽标说的都是同一件事。

**D4 分页和排序。** `limit`（默认 50，最多 200）、`offset`；按 `COALESCE(opened_at, created_at)` 从新到旧，再按 `step_id`，顺序稳定。回答 `{gates, total, counts, limit, offset}`：`total` 是当前筛选下的行数；`counts` 是 `{open, waiting_user, waiting_planner, closed, all}`，只按 `project_id` 筛，给筛选控件和侧栏徽标用。`state=open` 时 Python 取出全部等待的关卡，算出 `waiting_on` 再筛、再切片：每个等待的关卡都挡着一个任务的计划，数量本来就小；`closed`、`all` 在 SQL 里 `LIMIT/OFFSET`。

**D5 每行带什么。** `task`：`{task_id, name（db.tasks.task_name）, status, group, project_id, project_name}`；`gate`：`gate_view(row)` 的完整结果，不带 `channel_posts`（避免每个任务查一次聊天目标），这样前端能直接复用 `UserGate` 类型和 #255 的回复卡片；`agenda_open`：议程里还开着的问题数；`last_message`：最后一条用户或规划者的消息 `{role, author, via, created_at}`，没有为 null（Looper 自己的提醒、状态消息不算）。

**D6 性能：不加索引。** 关卡表每个关卡步骤一行，量级远小于任务表。`state=open` 走已有的部分索引 `idx_task_user_gates_waiting`；每行的子查询（`waiting_for_planner`、最后一条消息）走 `idx_task_user_gate_messages_step (step_id, id)`。只有 `closed`、`all` 要排序全表，几万行以内不值一个索引；以后慢了再加 `(COALESCE(opened_at, created_at) DESC, step_id DESC)`。

**D7 页面 `#gates`。** 一份标记、响应式排版：宽屏（`lg`）每个关卡一行网格，列为任务（链接到任务页）、主题、状态徽标（`gateLook`，文字加颜色）、打开时间（`sinceText`）、最后一条消息（谁、多久前）、未决问题数、操作；窄屏同样的字段堆成卡片，不横向滚动。顶部一组分段按钮「Open n · Closed n · All n」，一个「Waiting for you only」开关（只在 Open 下出现），一个项目下拉框（`useProjects`）；筛选写进地址（`#gates?state=closed&project=…&waiting=user`），和 Schedules 页一样。末尾「Show more」每次多取 50 条。每行「Open the discussion」链接到 `#tasks/<id>?gate=<step>`（任务页的关卡面板）。等用户的行有「Answer」按钮，就地展开 #255 的 `GateReply`（从 `dashboard-gates.tsx` 导出，参数改成 `Pick` 类型），发送仍走 `postGateMessage`，发送后刷新列表和徽标。

**D8 侧栏和刷新。** 侧栏在 Tasks 下面加「User Gates」（`MessageCircleQuestion` 图标）。徽标显示 `counts.waiting_user`，大于 0 才出现，带文字的 `aria-label`（「2 gates waiting for you」）；取数失败时不显示（不显示成 0）。徽标的钩子 `useWaitingGates` 学 `use-projects.ts`：每 10 秒一次 `GET /api/gates?state=open&waiting_on=user&limit=1`，序号防止旧回答覆盖新回答，在页面发出 `GATES_CHANGED_EVENT` 时立即刷新。页面同样每 10 秒刷新一次，上一次没回来就不发下一次，重新取已显示的条数（最多 200）。不新增别的轮询。

**状态：** 首次加载显示带转圈的「Loading gates…」；出错显示 `role="alert"` 的原因和「Retry」，已有数据保留；空列表按筛选说：Open 下「No gate waits for an answer.」，Closed 下「No gate has closed yet.」，All 下「No task has opened a gate yet.」。

## 3. 接口

`GET /api/gates?state=open|closed|all&waiting_on=user|planner&project_id=<id>&limit=50&offset=0`

```json
{"gates": [{"task": {"task_id": "task-…", "name": "…", "status": "waiting_user", "group": "needs_you",
                     "project_id": "proj-…", "project_name": "…"},
            "gate": {"step": "g1", "step_id": "…", "state": "open", "topic": "…", "agenda": [], "waiting_on": "user",
                     "opened_at": "…", "closed_at": null, "…": "gate_view 的其余字段"},
            "agenda_open": 1,
            "last_message": {"role": "planner", "author": null, "via": "", "created_at": "…"}}],
 "total": 1, "counts": {"open": 1, "waiting_user": 1, "waiting_planner": 0, "closed": 4, "all": 5},
 "limit": 50, "offset": 0}
```

400：`state`、`waiting_on` 取值不对，`waiting_on` 配了非 `open` 的 `state`，`limit` 不在 1–200，`offset` 不是非负整数。没有凭据 401（和其他 `/api/` 路由一样）。

## 4. 实现卡

1. 后端：`db.user_gates.list_gates`、`count_gates`，`gate_service.list_gates`，路由，`agent-system.md` 的接口表；
2. 页面：`web/src/pages/gates.tsx`，`fetchGates`，导出 `GateReply`；
3. 侧栏和路由：导航项、徽标钩子、`App.tsx` 的 `gates` 页；`docs/design.md` 的侧栏说明，用户关卡 RFC 第 446 行的待办。

## 5. 测试

- 数据库测试 `tests/test_gates_list_db.py`：三种 `state` 的行和计数；`waiting_on` 按 D3 算（停掉的任务里等规划者的算成 user）；已结束任务的等待关卡不在 Open 下、在 All 下；项目筛选；分页和顺序；`last_message` 不算 Looper 的消息；400 的几种输入。`tests/test_operator_auth.py`：没有凭据、agent 凭据访问 `GET /api/gates` 返回 401。
- 前端 vitest：页面的筛选（默认 Open、切换后请求参数和地址）、空、加载、出错状态，回复卡片调用 `postGateMessage`，Show more；侧栏徽标只在大于 0 时出现、失败时不出现、收到事件就刷新。
- 无头浏览器截图（`docs/design.md`「改界面的流程」）：在本工作树起一个全新的服务（新的数据库、临时 `--runtime-root`、不是 8767 的端口、渠道关闭），用接口或夹具造几个任务和关卡，截 1280 px 和 390 px 宽的 Open 视图（含展开的回复卡片）、Closed 视图、空状态和侧栏徽标；截图里不能有本机路径或私有数据。
