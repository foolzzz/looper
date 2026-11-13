# Manager gateway settings validation

Candidate base: `1705a6d67` (`origin/main` at implementation start).

## Targeted tests

Two bounded pytest batches passed: 194 tests in 51.05 s and 76 tests in
52.46 s (264 distinct tests; six existing CLI tests appear in both batches).
All database tests used disposable PostgreSQL schemas/databases. No tests
were skipped.

```bash
timeout 180 uv run --extra test python -m pytest \
  tests/test_manager_gateway_settings_db.py tests/test_gateway_api_db.py \
  tests/test_llmrec_settings_db.py tests/test_manager_ops.py \
  tests/test_vault_settings.py tests/test_api_v1_settings_pages.py -q

timeout 180 uv run --extra test python -m pytest \
  tests/test_api_v1_settings_pages.py tests/test_api_v1_settings_pages_db.py -q
```

The checks cover the secret-free gateway view, confirmation before each write,
dashboard readback, unchanged keys, dashboard error parity, target changes
between request and confirmation, rejected key operations, and the generated
CLI request and API result schemas.

## Manager dialog comparison

Selectors: `config_tools,scheduler_settings,session_profile_ask,gateway_settings`.
Each dialog ran on its own disposable database and `looper serve`, with an
isolated configuration directory. Scripted mode passed all four dialogs,
including the new gateway view → request → confirm → key-refusal dialog. This
checks the real service/tool path, but does not measure model behavior.

The real Opus xhigh Manager comparison used one repetition and was retried
once. Both attempts ended with four harness errors before any dialog turn
completed. The isolated Claude CLI reported `loggedIn: false` and
`authMethod: none`; no authentication environment variable was available.
The runner reported `Claude Code turn did not succeed (success)`.

| Dialog | Committed Opus baseline | Opus attempt | Opus retry |
|---|---|---|---|
| `config_tools` | 3/3 passed | 0 completed, 1 harness error | 0 completed, 1 harness error |
| `scheduler_settings` | No baseline | 0 completed, 1 harness error | 0 completed, 1 harness error |
| `session_profile_ask` | No baseline | 0 completed, 1 harness error | 0 completed, 1 harness error |
| `gateway_settings` | No baseline | 0 completed, 1 harness error | 0 completed, 1 harness error |

The baseline is `evals/prompt_regression/baselines/dialogs-real-opus.json`
at `c9ac42d5c`, with three repetitions. Only `config_tools` is shared with
this selection. These errors do not establish a prompt regression or a
passing real-model gate. An authenticated comparison remains required.

The same selection also ran on Sonnet at two repetitions: zero completed
dialogs and eight harness errors under the same unauthenticated configuration.
There is no committed Sonnet dialog baseline. All real attempts recorded
zero cost; they provide no usable quality, latency or cost comparison.

Reproduce the Opus comparison after making authentication available to an
isolated Claude CLI:

```bash
timeout 600 uv run --extra test python -m evals.prompt_regression dialogs \
  --mode real --model sonnet --manager-model opus --manager-effort xhigh \
  --dialogs config_tools,scheduler_settings,session_profile_ask,gateway_settings \
  --reps 1 --jobs 1 --timeout 90 --max-budget-usd 3.0 --total-cap-usd 12 \
  --pg-url "$LOOPER_EVAL_DATABASE_URL"
```

For the required Sonnet comparison, omit `--manager-model opus
--manager-effort xhigh` and use `--reps 2 --max-budget-usd 1.0
--total-cap-usd 8` with the same dialog selection.

Raw run JSON and server logs remain in ignored `evals/runs/`; this report
contains only public-safe aggregate results. Baselines were not refreshed.
