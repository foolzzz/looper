# Workflow preset entity validation

The backend stores operator workflow definitions and snapshots them when a task
is created. This validation covers the API and CLI, task creation and restoration,
and the Planner's effective execution settings and Task section.

## Targeted checks

The workflow tests cover definition limits and references, Profile capabilities
and fixed runtime compatibility, named database upgrades, operator-only CRUD,
replacement/deletion isolation, task snapshots, immutable role provenance,
Profile conflicts, project defaults and `none`, template exclusion, task budgets,
retry limits, fixed roles with Planner-defined roles disabled, generated CLI
payloads and task readback. Existing prompt SHA tests protect the Planner,
Worker and Reviewer prompts without workflow or dependency conditions. Existing
creation, role, execution, dependency and API schema tests cover companion paths.

Focused workflow tests plus the existing Planner protocol suite: **128 passed**.
The companion prompt, execution, dependency, API registration and workflow API
selection passed **166 tests** in a separate targeted run; creation and
Profile/role database regressions also passed. These overlapping runs are not
summed as a unique test count. The change adds 499 production lines and removes
46, with 468 new test lines.

## Service and model comparison

The existing `single_step` eval task was run with an operator workflow selecting
`dev` as its default work role, with Planner instructions, one retry and a task
budget. Both repetitions used a full `looper serve` on a new disposable database,
a free port and temporary runtime state. Startup login probes and unrelated
executables were isolated from operator state; request recording was disabled.

| Mode | Repetitions | Result |
|---|---:|---|
| Scripted runtime, workflow enabled | 2 | 2 completed; default role dispatched and output checked |
| Committed sonnet baseline, without workflow | 1 | 1 completed |
| Real Claude CLI, sonnet, workflow enabled | 2 | 0 completed; both ended before step creation |

The isolated Claude configuration reported `loggedIn=false`. Both real attempts
recorded one failed Planner run, zero input/output tokens, zero contract
violations and no Worker run. These attempts do **not** establish model quality
or a prompt regression: a valid authenticated sonnet comparison remains open.
The changed Planner contract also needs the development plan's opus comparison
once isolated authentication is available. Private run files and raw logs are
kept outside tracked artifacts.

## Entry points and deferred scope

The v1 API, generated workflow CLI, `loopercli create --workflow`, and client task
creation schemas are covered. The shared task core also accepts the workflow
field from the existing channel tools. Dashboard workflow controls, Manager
workflow discovery, configuration transfer, review-panel execution and
verification enforcement are separate stages. No frontend assets changed.
