# Evals

## Prompt regression (`evals/prompt_regression`)

Tells you whether a change to a recommended role prompt, a code-owned contract
(`REPLY_CONTRACT`, `WORK_CONTRACT`, `REVIEW_CONTRACT`) or the way the system
prompt is put together makes the agents worse.

It runs five small tasks through a disposable `loopx serve` (a new
PostgreSQL database, a free port, a temporary runtime root and working
directories), one at a time over the HTTP API:

| task | what it exercises |
|---|---|
| `single_step` | one step |
| `dependent_steps` | two steps, the second depending on the first |
| `reviewed_step` | a step an independent Reviewer must pass |
| `retry_after_failure` | a step that fails once (a flaky `build.sh`) and is retried |
| `chat_correction` | a correction sent to the task's Planner mid-task |

For each task it reads back from the API: success (the task completed, its
files are right and the flow happened, such as the review or the retry), step
count, retries, reviews, cost (the task cost API), contract violations
(replies the Planner's reply parser or the Worker/Reviewer report check
rejected) and whether the Planner modified files itself (its file-editing tool
calls, and files changed while only a Planner turn ran).

Each run writes a JSON file and the server log to `evals/runs/` (ignored by
git) and prints a Markdown table comparing it with the previous run of the
same mode and model there, or else with the committed baseline in
`evals/prompt_regression/baselines/`.

Both modes need a PostgreSQL server where the eval may create and drop a
database: pass `--pg-url` (any database on the server except `loopx`) or set
`LOOPX_EVAL_DATABASE_URL`.

### Fake mode: the flow, free

A scripted stand-in replaces `claude` and plays each role from the task's
script, so this checks the plumbing, not the model. It takes a few seconds:

```bash
uv run --extra test python -m evals.prompt_regression run --mode fake \
  --pg-url postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

`tests/test_prompt_eval.py` runs it as part of the test suite when
`LOOPX_TEST_DATABASE_URL` is set.

### Real mode: the effect, on demand

Runs real Claude Code (`haiku` by default) and costs money. Every Claude Code
run gets `--max-budget-usd` (default 0.25), and the run stops once its tasks
have spent `--total-cap-usd` (default 0.80); a task that reaches the cap is
cancelled and the rest are skipped. A full run with haiku costs about $0.30.

```bash
uv run --extra test python -m evals.prompt_regression run --mode real \
  --pg-url postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

Options: `--model`, `--tasks single_step,chat_correction`, `--max-budget-usd`,
`--total-cap-usd`, `--baseline <run.json>` (compare with this run instead),
`--keep-work-dirs`. To print a comparison again:

```bash
uv run python -m evals.prompt_regression compare evals/runs/<new>.json [evals/runs/<base>.json]
```

A pull request that changes a recommended prompt, a contract or the injection
order runs real mode on the base branch and on the change, and pastes the
table into its description. Update the committed baseline
(`baselines/real-haiku.json`) when an intended change moves the numbers; it
holds numbers only, never transcripts or local paths.
