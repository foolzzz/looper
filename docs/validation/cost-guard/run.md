# Task and global spend admission validation

The guard remains off by default. Enforce adds admission decisions to queued
Workers, Reviewers and Planner turns, using separate real and equivalent spend
and running reservations. Existing task actions `fail` and `wrap_up` remain the
default behavior; `ask` retains running work and holds further starts.

## Stand-in service

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
   plan and completed the task. The hold recorded `continued` / `dashboard`.
3. A second task resumed to completion after a synthetic operator DM containing
   `继续 C-2 4`. Its hold recorded `continued` / `lark`.
4. A third held task stopped after a synthetic operator DM containing `停`.
   Its hold recorded `stopped` / `lark`; its task outcome recorded `user_stop`.

Screenshots show the held state at both requested viewport widths:

| View | 1280 px | 390 px |
| --- | --- | --- |
| Needs you | [Wide](needs-you-1280.png) | [Narrow](needs-you-390.png) |
| Task status | [Wide](task-held-1280.png) | [Narrow](task-held-390.png) |

## Automated contracts

Python 3.11 regression batches passed 1,302 and 472 tests (overlapping suites),
with further Worker fallback, handle and model-call golden coverage. All failed
files were corrected and rerun; no checks remain failed or skipped. The ten
frontend test files passed 140 tests, and the production build succeeded.


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
