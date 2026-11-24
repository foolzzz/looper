# Resident turn account admission

Verified on Python 3.11 with a disposable PostgreSQL database, a separate
`looper serve`, a throwaway runtime root, scripted Claude CLI peers, a synthetic
Lark adapter and headless Chromium. No model or external chat account was used.

Run the harness with a built dashboard and a disposable test database:

```sh
uv run --system-certs --python 3.11 --extra test python -m tests.turn_admission_smoke \
  --static-dir <built-dashboard> --output <evidence-directory>
```

`LOOPER_TEST_DATABASE_URL` must point at the disposable PostgreSQL listener on
port 54331. The harness creates and drops its own fresh database and uses a free
HTTP port. Its synthetic account has `max_concurrency = 1`; failure typing,
limit signals, admission and Manager lanes are enabled only in that database.

Observed outcomes ([machine-readable results](results.json)):

- A dashboard message waits behind a running Worker without starting a model
  turn, then receives its answer when that Worker releases the slot.
- A scripted `rate_limit_event` and 429 freeze the account. The Planner waits
  before it creates a record. The Lark message receives exactly one progress
  notice with the reset time; the message remains pending. Calling
  `providers.limits_clear` (the provider row's **Check now** operation) releases
  both. The [synthetic Lark transcript](lark.json) contains one notice and one
  eventual answer.
- At utilization 0.95 a completion turn waits in `BACKGROUND`. A dashboard
  message preempts it; its code notification is delivered with
  `reason = turn_stopped`, and the user turn runs.
- Freeze statuses are absent from `chat_messages` and Manager record inputs.
  Database tests separately drive the actual Lark adapter and web adapter:
  Lark treats the status as `PROGRESS`, web emits `agent.phase`, and resume
  conversation sources exclude it. Telegram uses the same progress/history
  classification; no live Telegram delivery was attempted.

Screenshots (provider images show the synthetic account row within each viewport):

| View | Evidence |
| --- | --- |
| Dashboard waiting, 1280 px | [Waiting](dashboard-wait-1280.png) |
| Dashboard answered, 1280 px | [Answer](dashboard-answer-1280.png) |
| Frozen account, 1280 px | [Account](account-frozen-1280.png) |
| Frozen account, 390 px | [Account](account-frozen-390.png) |
| Completion preempted, 390 px | [Chat](completion-preempted-390.png) |

The 390 px account page has no horizontal overflow. Screenshots and transcripts
contain synthetic content only.

The disabled contract fixture pins each Planner/Manager prompt, turn text,
command and record metadata key set, each Manager envelope, and each arrival
table row to `0d9e121c1eb124282c33b5e8352164f30d212425`. It was also recaptured
from that exact revision to verify its provenance; no moving reference or
whole-module digest is used.

Not verified: real quota exhaustion, real Codex account probes, live channel
delivery, and unsettled user-message replay into the next process after drain.
Shutdown tests verify no record or grant is created and the message remains
unsettled. No paid prompt evaluation is pending: the new freeze notice is code
output and never enters model context.
