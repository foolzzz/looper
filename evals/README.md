# Evals

## Prompt regression (`evals/prompt_regression`)

Tells you whether a change to a recommended role prompt, a code-owned contract
(`REPLY_CONTRACT`, `WORK_CONTRACT`, `REVIEW_CONTRACT`) or the way the system
prompt is put together makes the agents worse.

It runs twelve small tasks through a disposable `looper serve` (a new
PostgreSQL database, a free port, a temporary runtime root and working
directories), one at a time over the HTTP API:

| task | what it exercises |
|---|---|
| `single_step` | one step |
| `dependent_steps` | two steps, the second depending on the first |
| `many_steps` | nine files, one step each, each after the one before: a plan longer than the eight steps it was once limited to |
| `parallel_parts` | two parts that need nothing of each other, then one that needs both: the plan must leave the two independent and their Workers must run at the same time |
| `interface_first` | a service and its client that must agree on a request format the requirement leaves open: the plan must have a step both parts depend on (the interface), the two parts independent and running at the same time, and a step that depends on both (the integration); the client must work |
| `reviewed_step` | a step an independent Reviewer must pass |
| `retry_after_failure` | a step that fails once (a flaky `build.sh`) and is retried |
| `chat_correction` | a correction sent to the task's Planner mid-task |
| `supplement_replan` | a message to the task's Planner, while its first step runs, that changes what the second step writes: the Planner must change the plan (`add_steps` or `replan`), not leave it to the Worker |
| `requirement_edit` | the same change as an edit of the requirement |
| `supplement_no_change` | a message that changes nothing: the plan must stay as it is |
| `profile_choice` | the task's project lets its Planner choose one more Agent Profile (`eval/chosen`, the run's own profile under another name, so it costs the same) and the requirement asks for it: the Planner must choose it with its own tool (`looper_task_profile_set`) before the step runs, and the Worker run on it |

For each task it reads back from the API: success (the task completed, its
files are right and the flow happened, such as the review or the retry), step
count, the pairs of steps the plan lets run at the same time
(`independent_pairs`), those of them with a step both depend on and a step
that depends on both (`bracketed_pairs`), and the most Workers that ran at once
(`parallel_workers`, from the runs' records), the changes to the task the
Planner answered by changing the plan (`plan_changes`), the Worker and Reviewer
runs on a profile the Planner chose (`planner_profile_runs`), retries, reviews, cost (the task cost API), tokens (the runs' records: prompt, uncached
and cached, and output; what a runtime without prices still reports), contract violations
(replies the Planner's reply parser or the Worker/Reviewer report check
rejected) and whether the Planner modified files itself (its file-editing tool
calls, and files changed while only a Planner turn ran).

Each run writes a JSON file and the server log to `evals/runs/` (ignored by
git) and prints a Markdown table comparing it with the previous run of the
same mode and model there, or else with the committed baseline in
`evals/prompt_regression/baselines/`.

Both modes need a PostgreSQL server where the eval may create and drop a
database: pass `--pg-url` (any database on the server except `loopx`) or set
`LOOPER_EVAL_DATABASE_URL`.

### Fake mode: the flow, free

A scripted stand-in replaces `claude` and plays each role from the task's
script, so this checks the plumbing, not the model. It takes a few seconds:

```bash
uv run --extra test python -m evals.prompt_regression run --mode fake \
  --pg-url postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

`tests/test_prompt_eval.py` runs it as part of the test suite when
`LOOPER_TEST_DATABASE_URL` is set.

### Real mode: the effect, on demand

Runs real Claude Code (`haiku` by default) and costs money. Every Claude Code
run gets `--max-budget-usd` (default 0.25), and the run stops once its tasks
have spent `--total-cap-usd` (default 0.80); a task that reaches the cap is
cancelled and the rest are skipped. `--model` sets the model in the system
settings layer of the run's database, so every agent uses it. A full run costs
about $0.61 with haiku (about 6 minutes) without `many_steps`, which adds about
$0.25 (about $0.023 a step); before `parallel_parts` and
`interface_first` were added, the other five cost $0.62 with sonnet and $1.30
with opus (about 3 minutes each), so give sonnet and opus a higher
`--total-cap-usd`, such as 1.50 and 3.00 (`interface_first` alone costs about
$0.87 with opus). With haiku, `interface_first` fails on the plan's shape: its
Planner chains the service, the client and the check. With opus it passes on
the Planner prompt from before `interface_first` was added and on the current
one alike (one run each): the Planner pins the protocol in a file first, builds
the two parts side by side and checks them together last. Expect some noise
between identical runs: cost
moves by up to about 10%, and an occasional single contract violation that a
repair turn fixes is not a regression by itself.

```bash
uv run --extra test python -m evals.prompt_regression run --mode real \
  --pg-url postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

Options: `--model` (every role runs Claude Code with its own login and this
model, the Agent Profile `claude-code/<model>`), `--agent <name>` (every role
runs this Agent Profile instead: one registered in the eval's new database,
such as the built-in `claude-code/opus`; the run records it as `agent`),
`--provider-preset <id>` (every role runs Claude Code with a provider registered from this
preset's Anthropic-format API, such as `glm-bigmodel` or `deepseek`, its key
read from `LOOPER_EVAL_API_KEY`; `--agent-model` picks the model, default the
preset's first, and `--provider-base-url` replaces the preset's endpoint, for
example with a logging proxy; the per-run cap is then Looper's own at the
provider's prices, and each task reports its `cost_basis`), `--register-agent
SPEC.json` (register an Agent Profile in the eval's database first and run
every role on it, with Looper's per-run cap: `{"runtime": {"name"?, "type"},
"provider": {"name", "protocols", "auth", "base_url"?, "api_key_env"?},
"agent": {"name", "model", "effort"?}}`, its `agent` being the profile;
runtimes are built in, so the profile runs on the runtime of that name or the
built-in one of that type, such as `codex` or `omp` when on PATH, and the
provider's key is read from the variable `api_key_env` names, never from the
file; `"provider": {"provider_id": "prov-codex-login"}` uses the built-in Codex
login instead, the operator's own ChatGPT login on this machine), `--tasks
single_step,chat_correction`, `--max-budget-usd`, `--total-cap-usd`,
`--baseline <run.json>` (compare with this run instead), `--keep-work-dirs`,
`--step-workspaces shared|worktree` (each working directory becomes a git
repository on `main` with one commit, the task's project gets that setting, and
each task reports the merge and checkpoint commits it added as
`looper_commits`). To print a comparison again:

```bash
uv run python -m evals.prompt_regression compare evals/runs/<new>.json [evals/runs/<base>.json]
```

A pull request that changes a recommended prompt, a contract or the injection
order runs real mode on the change (comparing with the committed baseline, or
with a run of the base branch via `--baseline`) and pastes the table into its
description. Update the model's committed baseline
(`baselines/real-haiku.json`, `real-sonnet.json` or `real-opus.json`) when an
intended change moves the numbers; it holds numbers only, never transcripts or
local paths.

### The Manager's dialogs

`dialogs` talks to the Manager instead: short conversations on the dashboard
chat, each reply checked against what Looper did (the API, and the Manager's
tool calls in the run's `manager_actions`). Same setup as `run`: a new
database, a disposable `looper serve`, and fake or real mode.

| dialog | what it checks |
|---|---|
| `ask_project` | a task without a project: the Manager asks, and creates and changes nothing |
| `confirm_create_start` | the whole flow: a small task is asked for, the Manager restates it and asks; 「确认」 creates it with a title (`looper_task_create`); 「开始」 starts it; no reply shows raw JSON |
| `create_start_stop` | a confirmed task is created ready in the named project with its configuration in the reply; "start" starts it, "stop" stops it |
| `change_running_task` | for a running task: a supplement (or a requirement edit) reaches its Planner in a turn; an edited requirement is a new version; the Reviewer's Agent Profile changes |
| `cancel_confirmation` | cancelling waits for the user's word: a refusal leaves the task running, a confirmation in the next message cancels it |
| `concurrency` | a project's Worker limit changes only after the user confirms it in the next message (`looper_concurrency`); a running task's own Worker limit changes at once |
| `status` | a status question is answered by reading through the tools (run it alone to try a runtime) |
| `config_tools` | the configuration tools: a project is created with the directory the user named (`looper_projects`); the Agent Profiles are listed; a Reviewer's profile in a project is set at once and the project's allowed list holds the one profile named (`looper_settings`); this week's spend is read (`looper_costs`); deleting the project waits for the user's word in the next message |
| `provider_key` | a provider registered from a message that pastes its API key: it is registered without the key (`looper_providers`), and the key is in no reply, no stored provider and not in the Manager's trail |

The dialogs that need a running task start a slow one first (`sleep 300`)
and stop whatever is still running at their end. Fake mode plays the Manager
with the stand-in, which makes each message's scripted calls over MCP; it
takes about 20 seconds. Real mode costs about $0.30 with haiku and stops at
`--total-cap-usd` (default 0.50):

```bash
uv run --extra test python -m evals.prompt_regression dialogs --mode real \
  --pg-url postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

Options: `--dialogs status,ask_project`, `--model`, `--agent`,
`--manager-model opus --manager-effort xhigh` (the Manager alone on that
model, the other roles on `--model`; one `confirm_create_start` with opus at
xhigh cost $0.12, so give it `--max-budget-usd 1.00`),
`--register-agent SPEC.json` (as for `run`; `--dialogs status` with a Codex or
OMP agent checks that runtime's tool calls), `--max-budget-usd`,
`--total-cap-usd`, `--timeout` (seconds for each reply and each check),
`--keep-work-dirs`. The run's JSON and server log go to `evals/runs/`; the
table shows each dialog's outcome, its tool calls per turn (a refused one with
its error code) and its cost, and the JSON keeps what each refused call was
told and the arguments it came with.
