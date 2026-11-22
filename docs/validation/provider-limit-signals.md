# Provider quota signal validation

Validated with a real `looper serve` from the source checkout and a
scripted Claude executable. The server used a newly created disposable
PostgreSQL database, an isolated temporary HOME/runtime root and a free
loopback port. No personal runtime state, real channel or model account
was used. The server stopped and its database was dropped after readback.

Replay from the checkout root:

```sh
LOOPER_TEST_DATABASE_URL='<disposable PostgreSQL URL with CREATEDB>' \
  uv run --extra test python docs/validation/provider-limit-signals-smoke.py
```

The script uses the supplied URL only to create and drop its own child
database. It imports the existing safe serve scaffolding from the test
suite, so run it from a source checkout with the test extra installed.

Observed result:

- Collection initially read back disabled, with no exposed windows.
- After enabling, a scripted `rate_limit_event` with 91% weekly usage
  reached `provider_limit_windows`, linked to the real Manager run record.
- `providers.limits` returned the window, its reset time and the day's
  usage from the last-seven-days query: one run, 10 input tokens and 5
  output tokens. Subscription cost was marked as API-equivalent pricing.
- Disabling collection while the service remained running prevented the
  second scripted observation (97%) from changing the saved window.
- Re-enabling exposed the unchanged saved snapshot; the usage query
  included both completed run records.
- Both scripted results carried typed HTTP-429 usage-limit fields. There
  were **zero real model requests**. No admission, freezing or failure
  handling was exercised or claimed.

The successful aggregate report was:

```json
{"admission_tested": false, "disabled_no_write": true, "http_usage_readback": true, "model_requests": 0, "reenabled_snapshot_preserved": true, "runtime": "scripted Claude", "scripted_429_runs": 2, "temporary_server_stopped_and_database_dropped": true, "window_persisted": true}
```

The processor path was also inspected: `processor.run` supplies
`keep=typed_events` to `ClaudeRunRecorder`; the recorder observes raw
quota events before invoking that transcript filter. Consequently an
event discarded from the processor transcript can still update provider
windows. Live receipt of quota headers by a particular tool-free
processor run was not tested.

Protocol provenance and its limits are recorded in
[`tests/fixtures/limit_signals/README.md`](../../tests/fixtures/limit_signals/README.md).
The fixtures are synthetic protocol examples, not live quota-exhaustion
recordings. A real account being rejected or resetting was not tested.

## Dashboard validation

The production build was served on an isolated loopback port with synthetic
API replies; no external requests or real accounts were used. Headless Chromium
checked 1280, 768, 390 and 844 × 390 viewports without horizontal overflow or
page errors. Checks covered populated windows, missing values, no signals,
disabled collection, read errors and retry, keyboard expansion/collapse and
the collection switch. Coarse-pointer controls have at least 44 px hit targets.
Quota data is fetched only when its block is expanded.
The new block retains the existing provider row and model controls; observation
and reset times make the age of the quota signal visible.

- [Desktop, 1280 px](provider-limits-1280.png)
- [Mobile, 390 px](provider-limits-390.png)

The read-only `providers.limits` operation is also available through the
generated CLI. Telegram/Lark behavior and Manager tools are unchanged; this
change collects observations without admission decisions or channel alerts.
Collection is managed through its operator-only settings operations and the
dashboard, independently of failure-typing settings. Codex app-server readers
cache the switch for five seconds and refresh it on the next quota notification;
the recorder reuses that decision. A failed refresh disables collection until
the next refresh. Claude continues to read the switch for every signal.
Quota failure counts stay unknown unless failure typing is in `record` or `act`
mode and the classification column exists. Invalid reset timestamps are omitted
from sparse updates, preserving the last valid reset.

The disabled-byte fixture was independently checked against main commit
`2b77afb16`: prompts, tools, command lines and recorded-event hashes match.

Related Python coverage passed with `-n 2` against disposable PostgreSQL:
356 non-steering tests and all 82 Codex steering tests. Two existing steering
tests initially failed on both this branch and unchanged main because they
sent input before initialization completed; they now explicitly wait for
channel readiness within the existing test timeout. The final readiness wait
was rechecked with all three bus-delivery variants. Production timeouts were
unchanged. Both touched web test files passed (69 tests), as did the production
build. No real-model evaluation was needed because model-visible content did
not change.

## Review regression checks

The six touched Python test files passed 149 distinct cases with `-n 2` and a
disposable database. Two existing Codex steering cases initially reached their
three-second deadlines and passed when rerun unchanged; no timeout value was increased.
Coverage includes database authentication and connection failures, unknown quota
counts with failure typing off, `record`/`act` counts, malformed reset timestamps,
pending/failed startup probes, and cached switch refresh with collection disabled
on read failure. Ruff passed for all touched Python files.

The isolated serve replay above was rerun after the review fixes and passed.
Its first attempt did not become ready within the harness's 45-second deadline
while preparing the component manifest; the unchanged replay then completed
window persistence, usage readback, live disable and re-enable preservation.
Temporary servers and databases were cleaned up; no real model was called.
