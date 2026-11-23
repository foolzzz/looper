# Background spend caps and schedule currencies

The guard remains `off` by default. In `enforce`, background roles stop at their
role, schedule or global daily cap, and schedule firings skip when their scope
chain would exceed a cap, including in-flight reservations. A server already
using `enforce` therefore begins applying caps that previously only appeared in
observations. `observe` retains execution and sends no spend alerts.

Setting `daily_budget_equivalent_usd` splits a schedule's real and equivalent
currency caps only in `enforce`. Leaving it unset retains the mixed legacy cap;
`off` and `observe` always retain the legacy mixed daily-budget check. The new
field is operator-owned and does not change the Manager's schedule tools. The
generated CLI keeps its existing nested-field syntax:

```sh
loopercli schedule-admin update --schedule-id example --fields '{"daily_budget_equivalent_usd":5}'
```

Task-linked background calls inherit their schedule but omit the interactive task
scope, as RFC §6.1 specifies. Otherwise an exhausted task cap could hide the later
role/global hit. Interactive runs still apply only task/global admission policy,
while their reservations also account to the schedule for the next firing.

## Isolated service validation

The complete scenario was rerun after admission-accounting and daily-summary
concurrency corrections. A guard restart regression additionally verifies schedule
accounting for older in-flight dispatch reservations. A real `looper serve` from
this checkout used a new disposable PostgreSQL
database, a separate loopback port, a temporary runtime and a fresh synthetic
project. Every CLI was replaced with a local stand-in; the classifier stand-in
reported a fixed $0.001 cost and an allow verdict. The channel sender was a local
inbox, so no model provider or external chat received a request.

Real HTTP administration calls created the agent schedule and subscription.
In-process probes invoked the service's actual scheduler, lead checker and
outbound reviewer because outbound review has no standalone public operation.
Database readback and the local inbox established:

- The subscription's scheduled firing hit the processor daily cap, committed a
  `skipped_budget` run and left its cursor unchanged. At alert delivery time, a
  separate database read already saw the committed skipped run.
- A lead check denied at its own daily cap reported its existing fail-open
  check failure.
- An outbound review denied at its own daily cap returned `block` / `budget`,
  wrote the audit row and created one `review_budget` role hold and question.
- Needs you obtained its cost item from the real attention API. Settings listed
  the hold and required confirmation to raise its cap. Clicking Continue and
  confirming persisted `continued` / `api`; a fresh review then ran the stand-in
  classifier and returned `allow`, with a second audit row. The blocked action
  was never automatically replayed.

Headless Chromium served the production frontend at 1280 and 390 px widths.
Only the unrelated live chat event stream was replaced by an empty event stream;
settings, schedules, attention and confirmation requests used the real API.
Final readback found one skipped run, two review audits (`block`, then `allow`)
and one fixed-cost classifier record. Seven alert/question messages remained
inside the stand-in inbox. The service, disposable database and temporary
runtime were removed after validation.

| View | 1280 px | 390 px |
| --- | --- | --- |
| Review-budget hold | [Wide](background-hold-1280.png) | [Narrow](background-hold-390.png) |
| Equivalent schedule cap | [Wide](background-schedule-cap-1280.png) | [Narrow](background-schedule-cap-390.png) |

## Limits

Repeated daily alert claims are counted in memory and summarized on the next
local day. A restart preserves persistent daily deduplication but loses those
repeat counters. Ordinary alerts use the existing advisor-alert delivery source
and normal urgency, so quiet hours apply.

`FALLBACK`, unanswered-push handling, the full spend settings editor and moving
the day boundary to the operator's timezone remain separate work. No real-model
validation is pending: this change does not alter prompts, contracts or agent
tool schemas.

## Automated validation

Targeted Python suites ran with Python 3.11, two workers and disposable PostgreSQL.
The latest core/restart regression passed 612 tests, the hold-view/API regression
passed 514, and background/failure validation passed 531; mandatory checks overlap
between these runs. Configuration transfer, schedule validation, caller failure
handling, alerts, usage windows and the per-row goldens also passed. The API
document, tool-level registry, released-bridge upgrade and migration registry
checks passed. No skipped test is counted as a pass.

The two changed frontend test files passed 75 tests. The production frontend
build passed with its existing bundle-size warning.
