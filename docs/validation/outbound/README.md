# Outbound gateway validation

The gateway controls use the operator-only `outbound_gateway.get/update` operations.
`chat_settings` keeps the same view, accepted fields and write behavior as `main`
(`2b77afb16`). The update operation declares high risk and confirmation; its summary
is under 120 characters and starts its description. Existing Manager tools remain
unchanged, so this correction changes no model-visible content.

## Integration follow-up

Merged `origin/main` without rebasing, preserving both the outbox and subscription
run-skips schema blocks. Both operator-only gateway operations explicitly declare
`inside` and appear in the operation-level manifest; they add no Manager tools and
remain separate from `chat_settings`.

OFF recovery now runs in a background task with a 30-second deadline and is joined
at shutdown. Before platform I/O, `mark_attempted` has a five-second total write
budget including retry waits. Exhausting it releases the shared app slot before
deferring the unsent row, without consuming a delivery retry or a platform breaker
failure. A successful platform response still retries persistence outside the slot
without resending.

538 distinct Python tests passed, foreground `-n 2`, using disposable databases:

| Group | Result |
| --- | --- |
| Tool levels, gateway settings, delivery-guard settings, OpenAPI | 365 passed |
| Outbox loop, drain and reliability | 22 passed |
| Catch-up, outbox schema, bridge schema, app limits, gateway, Lark outbox, chat settings | 151 passed |

Four new fault cases failed before the fix: startup with an exhausted pool and
three attempt-write failure modes (hang, repeated errors, lost acknowledgement).
The final checks cover recovery after more than 100 ms, probe cancellation and
deadline, a sibling app write progressing while even the defer write is blocked,
durable deferral and later delivery with the same key. An initial catch-up run
timed out while reading SQLite; its entire file passed in the final 151-test group.
No catch-up timeout change was made.

The follow-up adds 43 production lines and 132 test lines, excluding the manifest
fixture. The ratio exceeds 2:1 because the regression contracts cover task
lifecycle, shared app capacity and three distinct database failure modes through
recovery and delivery. Ruff and whitespace checks passed; all 9 chat-settings
frontend tests and the production frontend build passed. No UI or model-visible
content changed, so browser screenshots and paid prompt evals were not repeated.

An isolated public-CLI `looper serve` returned HTTP 200, read back outbound OFF,
verified both merged schema blocks and no pending migration, and stopped cleanly.
The temporary runtime and disposable databases were removed. No real channel or
model calls were made; full-suite/PostgreSQL 18 validation remains with CI.

## Earlier regression checks

All Python commands used `uv run --system-certs --extra test python -m pytest -q -n 2`
and ran in the foreground. Database checks used disposable PostgreSQL databases.

| Group | Result |
| --- | --- |
| `test_delivery_guard_settings.py`, `test_api_v1_document.py`, `test_outbound_settings.py` | 332 passed |
| Other PR-touched Python test files | 336 passed, 1 failed initially; the failed scanner assertion was corrected and its whole file passed (4 tests) |
| `test_chat_settings_db.py` final readback after the correction | 5 passed |
| `test_lark_cli_commands.py` | 56 passed on the PR and 56 passed on `main` |
| `web/src/components/chat-settings.test.tsx` | 9 passed |
| Production frontend build, Ruff on changed Python files, whitespace check | Passed |

Across the final file versions, 725 distinct Python tests passed. The scanner test
previously bounded all scans during 20 concurrent registrations, which may wake
legitimate scans. It now waits until the scanner sleeps with no workers or pending
wake, then checks that its scan count remains unchanged while idle. It still checks
that waiters perform at most one race-closing read each, plus the delivery read.

The earlier attachment-download 10-second timeout did not reproduce. The targeted
attachment tests passed on both branches (7 each), as did their entire file. The
10-second `asyncio.wait_for` wrapper is identical on both branches. These results
do not establish that either branch has a timeout defect.

## Real service and browser

A source-checkout `looper serve`, an empty disposable database and a throwaway
runtime root served the production frontend. No messages were sent and no model
calls were made. At widths 1280 and 390, headless Chromium verified:

- The outbound switch changes only `outbound_gateway`, with stored values read back.
- An ordinary chat settings write preserves the enabled gateway and returns exactly
  the original chat fields.
- Numeric settings update and null reset work through the operator API.
- A mixed chat/outbound write is rejected atomically.
- A failed gateway read displays an error and retry action while other chat controls
  remain usable; retry restores the switch.
- Keyboard focus remains reachable and the page has no horizontal overflow or
  uncaught JavaScript errors.

The existing controls and layout are retained. The switch uses separate API state;
its error and retry appear only when that API fails. Numeric tuning remains in the
operator API/CLI. Telegram/Lark chat commands need no new configuration command.

| Viewport | Current screenshot |
| --- | --- |
| 1280 × 1000 | [Desktop](settings-desktop.png) |
| 390 × 1000 | [Mobile](settings-mobile.png) |

For comparison, the previous screenshots are available at
[`db479d220`](https://github.com/foolzzz/looper/tree/db479d220/docs/validation/outbound).

Full-suite CI, PostgreSQL 18, real-platform rate limits, deduplication and Telegram
outbound wiring remain outside this local validation. Disposable databases and the
service process were cleaned up after the checks.
