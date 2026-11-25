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

No conversation transport changes are included: the dashboard, Telegram, Lark and
CLI conversation surfaces remain the work of their companion changes. The launch
boundary and task-start guard apply below those surfaces. No real model is used.
