# Coco app-server steering validation

Validated with traecli 0.215.0 on 2026-11-16. The baseline is
`73db46cf2ba06fde8e9e2a974ed85056ab220efe`; the candidate adds schema-gated
Coco steering. Both real-mode task runs selected one Coco profile using
`GPT-6.1-Sol` for every role, with two repetitions and one case at a time.
Each run used its own disposable database, server port, runtime and homes.

## Passed checks

- The generated `ClientRequest.json` exposes `turn/steer`. The existing
  channel's initialize, thread/start, turn/start and turn/steer payloads
  validate against the installed CLI's generated schemas.
- A real Coco process launched through `CocoRuntime.steer_command` and
  `CodexChannel`, with the `PUSHED` room guidance, accepted both a Planner
  correction and a room message during a shell tool call. Its final answer
  contained both messages' distinct code words, it emitted user-message
  history receipts, and it exited with status 0. This is a channel smoke,
  not a successful room-consult task.
- 156 targeted pytest cases passed, including schema failure/timeout
  fallback, isolated native and TTADK command construction, TTADK capability
  detection, Worker and Reviewer launches receiving both inputs, tool-only
  room delivery, and cumulative history-receipt confirmation. Ruff passed.

```sh
timeout 120 uv run --extra test python -m pytest \
  tests/test_coco_runtime.py tests/test_coco_worker.py \
  tests/test_ttadk_coco.py tests/test_codex_room_push.py \
  tests/test_codex_steer.py -q --tb=short
```

## Real-mode prompt comparison

Both runs used `evals.prompt_regression run --mode real` with
`--tasks chat_correction,room_consult --reps 2 --jobs 1 --task-timeout 300`
and the same registered Coco profile. The baseline used `exec`; database
readback confirmed candidate Workers used `app-server` and received the
`PUSHED` system-prompt guidance.

| Check | Baseline | Candidate |
|---|---|---|
| `chat_correction` | 2 pass | 2 pass |
| Worker retries in `chat_correction`, total | 2 | 0 |
| Empty inbox calls in `chat_correction`, total | 6 | 0 |
| `room_consult` | 1 timeout, 1 fail | 1 timeout, 1 fail |
| Answered consults, total | 0 | 0 |
| Room deliveries recorded by these tasks, total | 0 | 0 |
| Runtime run errors, total | 2 | 2 |

The candidate's second `chat_correction` scored a pass on the resulting
file but also recorded one run error; a scored pass does not erase it.
Both first `room_consult` repetitions were cancelled at 300 seconds with
no `code.txt`. The baseline's second repetition recorded a provider queue
capacity error; the candidate's second repetition failed during the
Planner's unchanged `exec` path with a generic execution error. Its cause
was not established. Neither run was aborted or stopped by the budget cap.
Coco usage is unpriced, so the reports' zero cost is not evidence of free
execution or a measured spending limit.

The room-task gate remains unpassed. Coco still refuses offline session
consults; this change does not enable them. No successful live room-service
delivery is established by these task runs. The real channel smoke and
protocol-speaking Worker/Reviewer tests cover steering separately. Live
TTADK execution was not tested.

## Behavior change

Existing Coco registrations retain their capabilities until Re-detect.
After Re-detect finds `turn/steer`, subsequent Worker and Reviewer runs use
the app-server, accept Planner steering, and switch room delivery and its
guidance from `PULL` to `PUSHED`. A missing or unusable steering schema keeps
the existing `exec` and `PULL` behavior.
