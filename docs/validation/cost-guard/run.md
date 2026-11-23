# Task and global spend admission validation

The guard remains off by default. Enforce checks queued Workers, Reviewers and
Planner turns against separate real and equivalent spend and reservations. Closing
turns use the same admission verdict. `fail` counts reservations and can stop
running Workers earlier; `wrap_up` keeps its existing stop path. `ask` retains
running work and holds further starts, including across mode changes and restarts.
Subscription runs gain a reservation soft stop only when their scope chain has a cap.

## Initial stand-in service

A real `looper serve` from this checkout ran with a new disposable PostgreSQL
database, a temporary runtime directory, a separate loopback port and a stand-in
Claude executable. All projects, tasks and messages were synthetic; no external
channel or model was called. The database and service were removed afterward.

The assertions and database readback established:

1. With three $1 Workers and a $2 task cap, exactly two Worker processes started.
   The third dispatch waited, one pending hold and one question existed, and the
   two Workers finished while the task remained `awaiting_budget`.
2. Needs you linked to the cost settings. Entering $4 and clicking Continue
   returned an actual confirmation request. Confirming it resumed the saved
   plan and completed the task. This initial pass recorded `continued` / `dashboard`;
   API decisions now record `api` and reject a caller-supplied `dashboard` source.
3. A second task resumed to completion after a synthetic operator DM containing
   `继续 C-2 4`. Its hold recorded `continued` / `lark`.
4. A third held task stopped after a synthetic operator DM containing `停`.
   Its hold recorded `stopped` / `lark`; its task outcome recorded `user_stop`.

Screenshots show the held state at both requested viewport widths:

| View | 1280 px | 390 px |
| --- | --- | --- |
| Needs you | [Wide](needs-you-1280.png) | [Narrow](needs-you-390.png) |
| Task status | [Wide](task-held-1280.png) | [Narrow](task-held-390.png) |

## Review correction validation

Before the fixes, targeted Python tests produced 13 failures covering closing
turns, disabled-mode hold replies/attention, the no-cap soft stop and source
spoofing; frontend tests produced four failures. A further restart probe caught
two failures: observe/off resumed a held step before a decision. Those tests now
keep the next step pending, route an operator DM through ManagerAgent, consume
the actual Planner wakeup and assert a persisted Worker dispatch.

Python 3.11 with two workers: **866 passed**, including the API document, tool
levels and both schema registry files. Frontend: **72 passed** across the cost
settings and two companion files; the production build passed. No skipped tests
are counted as passes. Eight individual restorations each caused a test to fail:
Planner wake, Manager routing, restart wait, in-flight wait, stop reason, `/todo`
registration, queued reservation release and observe queue lookup.

A new isolated `looper serve` started with a pending hold in `off`, a disposable
database, a temporary runtime and an unused loopback port. Headless Chromium
checked 1280, 768, 390 and landscape 844 px widths without horizontal overflow.
The [off-mode pending hold](pending-off-1280.png) remained visible in Settings;
clicking Stop removed it, persisted `stopped` / `api`, and stopped the task with
`stopped at a spend hold` in its closing note. Readback found **zero model records**.
The service and its database were removed afterward. Live event streams alone
were replaced with empty browser responses; decision/list calls used the real API.

Review fixes add 75 production and 212 test lines, excluding documentation and
fixtures. The ratio exceeds 2:1 because lifecycle coverage exercises real database,
Manager, message bus, Planner dispatch, restart and task-stop boundaries, with
negative cases and three modes rather than mocked wakeup assertions.


Python 3.11 tests cover reservation transfer, task and daily admission, exact
operator replies, confirmations and client refusal, hold uniqueness, multiple
global waiters, expiry, durable wakeup retry, and restart-safe task status.
Manager tests exercise the recorder and resident stand-ins: the operator's
notice appears once per day without entering model input; other senders do not
start a run; completion uses the existing code fallback; refused maintenance
retains its pending rotation while an explicit profile change is still checked
using its new profile.

Disabled and observation paths are compared against the fixed `6613380fa`
recorder and Planner budget funnel. The recorder comparison includes its command,
system prompt, input, launch metadata and process signature. Guard-only internal
fields remain excluded from model-visible record projections. Enforce adds no
hold text to Planner prompts or tool schemas.

The per-file migration adds the hold table and extends the project budget-action
constraint. Fresh-schema, released-bridge and migration registry checks validate
both creation and upgrades. A profile change refused after dispatch is delivered as a typed control event,
not a failed Worker report or new Planner text. A decided scope cannot wake a
task still waiting on another scope, and expiry preserves already ended tasks.
Hold rows retain their launch estimate so a bare
Continue cannot repeatedly resume a dispatch whose estimate still exceeds its
cap. Failed wakeups remain pending for the guard's refresh tick.

No real-model validation is needed for this change: prompts, contracts and tool
lists are unchanged. Background-role daily caps, schedule budget enforcement,
review-budget holds, alerts, unanswered pushes and usage windows are outside
this change.
