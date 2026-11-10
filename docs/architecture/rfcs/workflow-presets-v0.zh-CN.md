# RFC：流程预设与可组合依赖条件 v0

状态：依赖条件基础能力已实现；流程预设、评审小组、验证规则、回落与流程记录是后续阶段的设计，不代表当前已支持。

## 1. 目标与方案

把「做什么」与「如何执行」分开：任务描述和模板定义工作内容，流程预设定义角色、评审通过条件、验证规则和执行约束。依赖条件是独立的基础能力，也可单独按项目启用。

| 方案 | 复杂度 | 可维护性 | 影响面 |
|---|---|---|---|
| 只用任务模板和 prompt | 低 | 规则依赖模型遵守，无法强制执行 | 现有模板 |
| 类型化流程预设与通用依赖条件（采用） | 中 | 代码校验状态和规则，条件求值器可复用 | 显式启用的项目或任务 |
| 把流程规则并入任务模板 | 较低 | 任务内容与执行方式耦合，跨项目重复 | 现有模板用户 |

没有条件、没有流程的任务保留原来的 prompt、契约、调度和 Pipeline 输出。新增模型可见内容只出现在相应功能开启时。

## 2. 依赖条件

`ready_when` 定义步骤何时可以开始；`depends_on` 仍是画图、查环和传递依赖产出的边集合。Planner 回复用步骤标签，数据库和 API 用 `step_id`。

```text
Condition :=
    "s1"
  | {"step": "s1", "on": "completed" | "verified" | "passed"}
  | {"all_of": [Condition, ...]}
  | {"any_of": [Condition, ...]}
  | {"k_of": 2, "of": [Condition, ...]}
```

字符串等同于 `on: verified`。组合可嵌套，最多四层、32 个叶子；组不能为空，同组不能重复引用步骤，`k_of` 的 k 必须在 1..n。条件叶子的步骤集合必须正好等于 `depends_on`，并沿用原有的禁止自引用、未知引用、丢弃步骤引用和依赖环规则。原来的组合写法和字符串简写在持久化时保留。

| 步骤 | completed | verified | passed |
|---|---|---|---|
| work | Worker 已报告 completed，含评审中与已接受 | 已接受 | 已接受 |
| gate | 已关闭，含取消 | 已关闭，含取消 | 关卡通过 |
| wait | 已关闭 | 已按既有规则接受 | outcome 为 ready 或 resumed |
| review（后续阶段） | 已出结论 | 已出结论 | 通过，含裁决后通过 |

纯函数求值器返回 `satisfied`、`pending` 或 `unsatisfiable`。叶子已满足时返回 satisfied；失败或 skipped 的目标不可满足；要求 passed 而目标已结束且未通过时不可满足；其余待定。review 的判断留到评审步骤阶段实现，基础阶段返回 pending。

组合统一按 k-of 求值：满足项数至少 k 就满足；满足项加待定项仍少于 k 就不可满足；其余待定。all_of 的 k=n，any_of 的 k=1。没有 `ready_when` 的步骤仍按原来的「全部依赖 verified」派发。

any_of 放行后，其他分支继续运行，任务完成仍要求每一步 verified 或 skipped。已经开始的下游不会因上游 retry 回滚；未开始的重新判定。replan 保留已开始步骤及其条件，只替换未开始的步骤。

### 无法满足时的处理

条件变成 unsatisfiable 不会静默丢弃步骤。代码记录 `condition_blocked`，通过任务变化的唤醒路径给 Planner 一个 `[CHAT]` 回合，说明哪个步骤无法开始、条件和失败原因。允许 `none`、`add_steps`、`replan`。`none` 将该步骤标为 skipped，记录 decision，然后级联判定。回合失败时仍保留 pending，后续事件重试投递。

### 开关和读取

项目执行设置 `dependency_conditions` 取 on/off，默认 off。关闭时契约不提 `ready_when`，Planner 写入它会被拒绝。打开时 work、gate、wait 可写条件。未来流程可按任务覆盖，代码生成的评审条件不受该开关限制。

`task_steps.ready_when` 为可空 JSONB。API 步骤及等待视图提供 `ready_when` 和 `ready_state`；仅 pending 步骤计算条件状态，其余为 null。管家 `looper_tasks get` 提供条件文字，CLI 通过任务 API 读取同样数据。用户只能向 Planner 补充要求，不能直接修改计划。

Pipeline 汇合点是后续阶段：普通依赖继续使用直边；组合条件显示「全部」「任一」「2/3」，非默认叶子标注完成或通过，状态同时用文字和图标表达。

## 3. 流程实体与选择

以下是后续阶段的目标模型。流程是操作者级命名配置，建任务时存快照，后续编辑不影响已有任务。

- `workflows`：key、name、description、definition、updated_at；key 为 1–32 位小写字母、数字、下划线或连字符且以字母开头，名称最多 60 字符且忽略大小写后唯一，说明最多 300 字符。
- `tasks.workflow`：流程快照；`projects.default_workflow`：可空默认流程引用，删除时清空。
- 流程字段：Planner 指令（最多 8000 字符）、内置角色 Profile 覆盖、最多八个任务角色、默认 work 角色、评审小组、重试上限、全评审、依赖条件、预算、评审运行时独立性、裁决、验证、用尽升级、阶段播报。
- 角色包含 key、kind、role_prompt、harness 和 profile_id，复制到任务后由流程锁定，Planner 不可改。

保存时校验所有角色引用与 kind、Profile 的角色能力，以及评审条件至少包含一个小组成员。显式固定 Profile 且要求开发与评审运行时不同时，保存时提前拒绝冲突。用尽升级隐含打开用户关卡。

选择优先级：显式 workflow（key 或 `none`）＞项目默认＞无流程。显式流程和任务模板同时出现时拒绝；项目默认不作用于带模板的任务或定时任务。控制台、CLI 和管家共用任务创建事务，原子写入快照、角色、Profile 覆盖和预算。

## 4. 评审小组

后续阶段新增 `task_steps.kind=review`、`reviews` 和 work 上的 `accept_when`。流程的小组展开成每个成员一个并行评审步骤，依赖目标 work 的 completed；标签如 `s1/review_a`。目标 work 的 accept_when 把成员名称换成评审步骤 id，叶子固定为 passed，复用相同的求值器。

条件里的成员是必选评审，其余是建议评审。满足后允许 Planner 接受；不可满足且所有相关评审结束后再合并决策。建议评审晚到且有 blocking 时，通过 `[CHAT]` 让 Planner 决定是否补充修复。交付仍等待所有步骤结束。

retry 保持评审步骤 id、推进轮次、停止上轮仍运行的评审，记录旧结论后清空。目标 work 被丢弃时评审一起丢弃。未使用小组的任务保留原有的内置 Reviewer 与串行 review_roles 路径。

评审专有规则不进入通用条件层：

- findings 包含 severity（blocking/suggestion）、where 和 text；passed 与 blocking 冲突时取严格结论并记录。
- Planner 按位置归并 blocking，保留来源；可用带理由的 adjudications 降级，重新求值并记 `review_adjudicated`。
- 要求独立运行时时，派发前比较评审与最新 Worker 的运行时，相同就拒绝，不启动评审。
- Worker 提交报告时冻结 HEAD，评审结束 HEAD 已变化则该席失败。
- Planner 不能直接创建评审步骤、引用评审步骤写 ready_when 或写 accept_when；这些只由流程生成。

## 5. 验证、回落与交付记录

验证由中性角色 `verify_staging` 执行，Planner 保持只读。验证步骤至少依赖一个 work，默认不再评审，除非显式要求。派发时保存 HEAD 和工作区摘要；报告含代码变动、HEAD 改变或工作区变脏时，以 `verification_changed_code` 失败且禁止 accept。验证失败次数达到 1–5 的配置上限后，禁止 retry；新增验证必须先过用户关卡。远端只读或 dry_run 是指令约定，交付记录说明未由代码强制。

Profile 回落独立于流程：fallback_profile_id 最多三跳，无环，目标支持相同角色。只在 profile_error 或进程退出而未收到模型事件时触发，重跑同一次尝试，不增加 retry，记录 `profile_fallback` 和 fallback_from。各运行时需先确认首个模型事件的真实信号，之后才能启用对应回落。

带流程的任务在 Planner 的 final 四段后，由代码附加 Process record：角色的 Profile、运行时、请求与报告的模型、回落；每轮评审、blocking、裁决与晚到；验证 HEAD 和结果；用户升级关卡。失败时同样附加。阶段播报由类型化事件生成，涵盖计划建立、评审反馈、验证和升级，发往原任务渠道。

## 6. 迁移、兼容与可观测性

迁移使用具名升级，新增列可空，旧任务不自动启用。降级不会收窄已有 CHECK；发布时说明兼容边界。配置导出导入在流程阶段涵盖 workflows、default_workflow、dependency_conditions 和 fallback_profile_id，Profile 按名称引用。

当前新增 `condition_blocked`；后续按能力增加 review_round、late_review、review_adjudicated、profile_fallback、verification_round、escalation。活动和 API 报告条件状态、失败原因、评审轮次、回落和验证改码；角色通过率复用现有运行统计。默认关闭路径以 prompt 哈希、契约文本、调度与 Pipeline 回归锁定。
