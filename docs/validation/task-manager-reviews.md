# Task Manager review validation

The launch/pool change keeps the task role out of shared role discovery. It moves
the FIFO scheduler and native-turn primitive into the foundation so admission,
idle reclaim and write exclusion can be exercised there. Durable sessions,
per-task profile switching, accounting and context maintenance remain in the
stacked turn change.

Foundation validation uses the native stream-json stand-in and disposable
PostgreSQL, with two pytest workers. The task Manager, settings, brief, draft and
registry files passed together (97 tests). The existing Manager configuration
tools, task role settings and registry reads also passed (122 tests).
The shared settings, role APIs, Manager tools and registry files passed together
after the final role-list audit (243 tests).

Regression evidence:

- On the original launch implementation, the new preparation test started three
  processes at a cap of two; the queue and idle-timing tests failed because the
  pool had no turn admission path.
- Omitting the provider from the signature leaves the PID unchanged when provider
  environment changes; the new provider-restart test fails.
- Restoring the task role to either shared role table fails the discovery freeze.
- The shared role schema, seeded `roles.list` response and `agent_settings.get`
  response each failed a new freeze before the shared-role projection was fixed.
- Removing the background-start guard fails the native-turn write-exclusion test.
- Concurrent close and shutdown each failed while dispatch was paused inside idle
  eviction. The corrected lifecycle uses the pool gate for queue mutation and
  waits for drivers outside it; the foundation pool/native preparation files
  passed together (8 tests).

No conversation transport changes are included: the dashboard, Telegram, Lark and
CLI conversation surfaces remain the work of their companion changes. The launch
boundary and task-start guard apply below those surfaces. No real model is used.

## Stacked turn validation

The turn change saves the native session at init, adds the operator-only task
profile operation, and keeps its session through idle model switches. Cost-read
failures record `budget_unverifiable` and allow the turn; a reached cap still
refuses only that turn. Per-record measurements replace prior measurements,
including NULLs, and drive serialized compaction or durable fresh-session rotation.
The four context/cost/compaction/rotation metrics are read from recorded data.

The new budget, init-persistence and profile-operation tests failed before their
fixes. Eight original context cases also failed before context maintenance was
wired. The completed context file has 11 passing cases, including direct and
read-only compaction, failed/unconfirmed/oversized/timed-out compaction, measurement
read failure, cost-triggered rotation across restart, and maintenance-time queueing,
write exclusion, profile refusal and stop notification. Removing operator-only
from the profile operation made its HTTP test fail with an unexpected client 200;
the guard was restored.

The broad related suite passed 736 tests with one opt-in test skipped. The new
profile operation was then added to the default-off operation-id allowlist; the
existing request and Manager-tool goldens remain unchanged. That freeze file and
the profile/budget/session regressions passed together (22 tests).
The final integrated pool, turn, context, metrics, role-freeze and draft files
passed together after the ordinary branch merges (65 tests, two workers).

The skipped opt-in was run explicitly: Claude Code **2.1.332** passed the native
task Manager test against the local fake Anthropic model (1 test). It verified a
Bash-created file, both user and launch hooks, and the saved session row. Its HOME
and CLI config directory were temporary; requests carried only the fake probe key.
There were no real-provider calls. CI was neither started nor rerun manually.

The pending plan update is to mark task Manager admission, durable turns and
context maintenance implemented after review acceptance; conversation delivery and
task-bound tools remain companion work. The shared plan file is unchanged.
