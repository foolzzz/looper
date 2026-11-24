# Execution failure recovery validation

Failure handling remains **off by default**. `record` retains classification without changing control flow;
`act` adds step holds, bounded recovery, a three-failure breaker and confirmed operator control.

## Automated and process checks

The targeted Python 3.11 tests use disposable PostgreSQL databases, two pytest workers and synthetic runtimes.
They cover the transaction and public boundaries: shared work/review streaks, successful resets, negative
verdicts, run deduplication, versioned concurrent releases, retained history after step/record deletion, 201-row
pagination, partial schema restoration, online check validation, operator identity/confirmation, mode drain,
admission slots, recovery targets, processor privacy and cumulative spend. The migration's reserved order is
1920; fresh databases record only the existing baseline marker.

`test_engine_failure_handling_off.py` compares 22 individual items and their fields with
`engine_failure_handling_off.json`, captured from fixed commit
`11a10a8e4f1a57b071298b0a1fed75b42f47c3d8`. Capture runs against a read-only archive of that commit and verifies
the loaded package location. Only test-owned paths, generated identifiers, timestamps and process IDs are
normalized. Prompts, messages, complete tool definitions, commands, metadata values, records, results and bus
payloads remain compared. The separate provenance capture is opt-in; ordinary tests never regenerate expected
values. Previous admission fixtures are unchanged. Record-mode comparisons permit only existing classification
fields. Near-budget journals retain the old rendering budget when no failure-hold entry exists.

`test_failure_handling_serve_db.py` starts the actual `looper serve` process against a fresh database and runtime
directory. It writes and reads back the isolated ops configuration, asserts the port differs from the operator
port before any request, uses script runtimes and a local channel stand-in, and removes processes, ports and
fixture data afterwards. Observed checks include:

- Default-off completion with no new failure rows; explicit act activation through operator confirmation.
- A real first transient backoff in the 96–144 second range, then a single replacement Worker succeeds.
- Environment holds enter `waiting_user`; cancelled confirmation writes nothing. Retry, Planner and stop are
  checked separately, with history retained. Origin notices retain the synthetic reply parent.
- Quota holds with account admission enabled release their active slots; clearing the frozen account permits
  recovery. Disabling act drains holds to the previous decision path without deleting history.
- A process restart retains an environment hold's identity and streak, starts no replacement while held, and
  confirmed release starts only its replacement execution. HTTP and the generated CLI read the persisted history.

Deterministic actor/database regressions also deliver a release before startup's `TASK_RESUME`: Worker and
Reviewer each dispatch once, duplicate outbox events are ignored, and the replacement result remains current.
A release blocked by independent spend admission retains its exact execution target and unacknowledged outbox
until that wait can resume; it does not fall through to an ordinary Worker dispatch.
An unconsumed breaker also restores DECISION from its committed failure snapshot, whether the restart preceded
or followed the step's decision projection. Worker, built-in Reviewer and panel cases retain all three failures;
repeated resume events and another actor restart do not repeat the consumed decision.

The process check uses real local timing for the first transient wait. Minute/hour deadline boundaries use
controlled clocks in unit/database tests; no claim is made that a six-hour wait was observed in real time.
Frontend checks use Vitest, a production build and headless Chromium against the isolated service.
The hold panel was read from the actual isolated service at 1280 × 960,
768 × 1024, 390 × 844, and 844 × 390; all four widths had no document-level horizontal overflow.
The live Needs you section and engine settings were also checked in Chromium. The history empty state and
503 unavailable state used controlled HTTP responses in that same page; these do not claim a real database outage.
Screenshots are kept outside the tracked tree; the PR describes these visual checks.

Reproduction (supply a disposable PostgreSQL URL):

```sh
uv run --system-certs --python 3.11 --extra test python -m pytest -q -n 2 \
  tests/test_step_failure_handling_db.py tests/test_step_failure_planner_db.py \
  tests/test_step_failure_deadline_db.py \
  tests/test_step_failure_schema_db.py tests/test_failure_handling_serve_db.py \
  tests/test_engine_failure_handling_off.py tests/test_worker_failure_handling.py \
  tests/test_planner_failure_handling.py tests/test_processor_failure_handling_db.py \
  tests/test_task_hold_tool_db.py tests/test_api_v1_document.py tests/test_tool_levels.py \
  tests/test_bridge_schema_db.py tests/test_schema_migration_registry_db.py
```

## Model-visible changes and pending real-model validation

The changes visible to models are the recovery notice in Worker/Reviewer dynamic input, the current typed
failure sequence in DECISION, `failure_hold` entries in existing Planner/Worker/LESSONS journal renderers, the
act-only `looper_task_hold` and `looper_settings` variants, and retransmission of the same prepared Planner
exchange. Held work steps also appear as pending in the Planner's plan view. Full task detail gains an optional
hold projection; compact task results do not gain history. No new default-on prompt, tool or retry policy is added.

Real models, real account exhaustion and real outbound channels **have not been exercised**. Paid evaluations
remain pending explicit scheduling and authorization. Synthetic script answers establish execution contracts,
not model quality. New cases are opt-in and each new task case requires its own invocation and disposable
database; unrelated cases are never switched into act by a shared run.

Use `uv run --extra test python -m evals.prompt_regression` as the command prefix, with
`--pg-url "$FAILURE_EVAL_PG_URL"` and an ignored local `--out-dir`. Before real execution, verify the evaluation
server's on-disk ops configuration and every client URL select the disposable target. The existing general eval
server isolation gap must be resolved by its owner before running it; this feature does not change that harness.

| Invocation | Cases | Repetitions | Per-call / per-case / invocation cap |
|---|---|---:|---:|
| `run --mode real --model sonnet --jobs 4 --task-timeout 1800` (three separate invocations) | `failure_retry_context`, `failure_breaker_history`, `failure_reviewer_resume` | 2 each | $1 / $2 / $4 each |
| `run --mode real --model sonnet --jobs 4` | `retry_after_failure,reviewed_step,chat_correction` | 2 | $1 / $2 / $8 |
| `run --mode real --model opus --jobs 4 --task-timeout 1800` (three separate invocations) | The same three new task cases | 1 each | $3 / $6 / $6 each |
| `run --mode real --model opus --jobs 4` | The same three existing task cases | 1 | $3 / $6 / $22 |
| `dialogs --mode real --model sonnet --jobs 4` | `task_hold_retry,task_hold_planner,task_hold_stop,task_hold_operator_boundary` | 2 | $1 / $1 / $8 |
| `dialogs --mode real --model sonnet --manager-model opus --manager-effort xhigh --jobs 4` | The same four dialogs | 1 | $3 / $3 / $12 |

Pass case selection through `--tasks` or `--dialogs`, repetitions through `--reps`, and the three budget columns
through `--max-budget-usd`, `--case-budget-usd`, `--total-cap-usd`. Candidate total ceiling: **$80**, not measured
spend or authorization. Schedule at most three real evaluation processes concurrently. Opus reruns require a
separate budget decision; retain initial failures. Existing task baselines cover only overlapping cases, and the
new cases and missing dialog baselines cannot be presented as a complete historical comparison. Release
baselines are not refreshed here.
