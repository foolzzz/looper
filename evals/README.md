# Evals

## Prompt regression (`evals/prompt_regression`)

Tells you whether a change to a recommended role prompt, a code-owned contract
(`REPLY_CONTRACT`, `WORK_CONTRACT`, `REVIEW_CONTRACT`) or the way the system
prompt is put together makes the agents worse.

It runs twenty-two small tasks through a disposable `looper serve` (a new
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
| `roles_tester_docs` | the task's project lets its Planner define roles (`planner_roles` on) and choose `eval/chosen`; a module, its tests and its usage notes, the tests by a tester role on `eval/chosen` and the notes by a doc-writer role: at least two work roles, their steps run as them on the version defined, the tester on its profile, each role run with the boundaries before its contract |
| `roles_security_review` | the same switch; a script that must not run a shell, reviewed by the built-in Reviewer and by a security review role: the role reviews after the built-in Reviewer, and the step is accepted only with both reviews passed |
| `roles_advisory_review` | the same switch; a greeting script reviewed by the built-in Reviewer and by an advisory review role (its `review_roles` item has `required` false) that wants a CHANGELOG.md the task turns down: the role does not pass the step, the built-in Reviewer does, and the Planner decides (accept, or retry with the role's findings) |
| `roles_two_reviews` | the same switch; the script that must not run a shell, with a docstring and PEP 8, reviewed by the built-in Reviewer and by two review roles it asks for (security, style): one step names both (`most_review_roles` 2), they review one after the other after the built-in Reviewer, and the step is accepted only with all three passed. `roles_security_review` is the other direction: it asks for one review role, and no step names more (`most_review_roles` at most 1) |
| `roles_multi_platform` | the same switch, and a requirement that asks for no role: one tip-of-the-day feature across a back end (`backend/tips.py`, run by the check), a web front end, an Android and an iOS client (`web/tip.js`, `android/TipClient.kt`, `ios/TipClient.swift`, no build), which must agree on an interface recorded in `api/tip_api.json`: a work role per discipline whose prompt names at least two of that platform's conventions, at least four role steps built on one interface step, at least two Workers at once, the role runs on a version defined with the boundaries before the contract, and no contract violation |
| `roles_not_needed` | the same switch on a one-file task: the Planner defines no role and plans at most two steps |
| `roles_template_fits` | the same switch, and the project keeps two role templates (`security-review` for steps whose code runs commands or takes untrusted input, `doc-writer` for user documentation); the script that must not run a shell, reviewed by an independent Reviewer, with no role asked for: the Planner copies `security-review` as the step's review role, which reviews after the built-in Reviewer |
| `roles_template_unfit` | the same templates on two text files neither fits: the Planner copies no template and defines no role |
| `gate_ambiguous` | the task's project lets its Planner open user gates (`user_gates` on); the settings file's format, name and key are the user's choice, written down nowhere: the Planner asks at a gate, the scripted user answers (TOML, `settings.toml`, `[service]`), the gate passes, a Worker reads the conclusion and the file follows the answer |
| `gate_not_needed` | the same switch on a one-file task: no gate, at most two steps |
| `gate_end_early` | the settings task again, but the user ends the discussion at once: the Planner closes the gate on stated assumptions and writes a settings file anyway |
| `gate_parallel_branch` | a clear part (a 20-second step) and the settings part: the gate holds back only the second, and a Worker runs while it waits |
| `gate_discoverable` | the port is what a step's `probe.sh` prints and the user cannot answer: the Planner must not ask (`gates_opened` 0; a gate that waits on the step and is cancelled before it opens never asked). The scripted user ends a gate that opens after 120 s, so the task fails fast instead of timing out |

For each task it reads back from the API: success (the task completed, its
files are right and the flow happened, such as the review or the retry), step
count, the pairs of steps the plan lets run at the same time
(`independent_pairs`), those of them with a step both depend on and a step
that depends on both (`bracketed_pairs`), and the most Workers that ran at once
(`parallel_workers`, from the runs' records), the changes to the task the
Planner answered by changing the plan (`plan_changes`), the Worker and Reviewer
runs on a profile the Planner chose (`planner_profile_runs`), the roles the Planner defined (`roles_defined`)
and the runs as one (`role_runs`; on the role's own profile, `role_profile_runs`; as a review role,
`role_reviews`; copied from a project role template, `template_roles`; on a prompt that is no version it defined,
`role_prompt_mismatches`; the most review roles one step names, `most_review_roles`; without the boundaries after
the task section, `role_boundary_misses`), the accepted steps with a review role that lack a passing built-in or
role review where the role is required (`gate_breaches`), the reviews by an advisory review role that did not pass
(`advisory_role_fails`), the disciplines of a task that names them with a work role of their own whose prompt
names at least two of the discipline's conventions (`discipline_roles`), the most steps run as a work role that
depend on one same step (`interface_dependents`), the user gates the Planner opened (`gates_opened`; their [GATE] turns, `gate_turns`;
passed, `gates_passed`, of them ended by the user, `gates_ended_by_user`; cancelled, `gates_cancelled`; the
assumptions their summaries stated, `assumptions_stated`; the steps the closing turns added, dropped or stopped,
`gate_plan_changes`; Worker runs while a gate waited, `gate_parallel_runs`; Worker and Reviewer runs that read a
gate's conclusion, `conclusion_reads`), retries, reviews, cost (the task cost API), tokens (the runs' records: prompt, uncached
and cached, and output; what a runtime without prices still reports), contract violations
(replies the Planner's reply parser or the Worker/Reviewer report check
rejected; the JSON keeps why the parser rejected each Planner reply,
`planner_violation_reasons`), whether the Planner modified files itself (its file-editing tool
calls, and files changed while only a Planner turn ran), the runs that ended in an error (`run_errors`,
by their records' status) and the models each role's runs reported (`models`).

Each run writes a JSON file and the server log to `evals/runs/` (ignored by
git) and prints a Markdown table comparing it with a base: by default the
committed baseline of the same mode and model in
`evals/prompt_regression/baselines/` (`real-<model>.json`; there is none for
fake mode); with `--baseline previous`, the newest earlier run of the same mode
and model in `--out-dir`; or the run file `--baseline` names. It prints which
base it used, with its commit and its reps. The default used to be the newest
earlier run in `evals/runs/`, which differs from one checkout to the next: use
`--baseline previous` for that.

### Repetitions, outcomes and the table

`--reps N` runs every task N times (default 1): rep 1 of every task, then rep
2, and so on, so drift over time does not land on one task. Each (task, rep)
has a working directory (`work/<task>/<rep>`) and a project of its own, and
`--total-cap-usd` covers all the reps; the rows past it are `skipped`. One rep
is a single sample: with sixteen tasks run once each, the pass rate's 95%
interval is up to about ±25 points wide, so one task going from pass to fail
is no regression by itself. Nothing is run again automatically: a rerun is a
new run, and a run is never the best of several.

Every row has `rep` and `outcome`: `pass`, `fail` (the task ended and its files
or its flow are wrong) or `error`, which is neither and is not scored.
`error_class` says why: `timeout` (cancelled after `--task-timeout`),
`spending_cap` (cancelled at the cap) or `harness` (the eval itself failed: an
API call, the server, a check that raised; the task is cancelled and the run
goes on with the next), and `error` gives the message. `success` stays, true
only for a pass. The JSON is written however the run ends; a run that stopped
midway (an interrupt, a setup that failed) has `aborted` with the reason. Runs
of the first schema are still read: a row without `rep` is rep 1, and one
cancelled at its timeout or at the cap is an error.

In the table, success is `k/n`, the passes over the scored reps, with ` · e
err` when some reps are errors; the other columns are means over the scored
reps, so the cost column leaves out what the errored reps cost (the run's
`total_cost_usd` has every row). The total row has the passes over all scored
rows with their Wilson 95% interval, such as `41/48 (85%, 73–93%)`, the errors
by class, and the tasks' means added up (one pass through the set). Against a
base, the total row covers only the tasks both runs have (`total (7 shared
tasks)` when the sets differ), and a second row gives the new run's own total
when it has tasks the base lacks. Below it: the tasks whose pass rate changed,
a note for a side that ran each task once, the tasks with no baseline, and the
tasks of the base the new run lacks. Read the errors as well as `k/n`: a task
that never finishes is a `timeout` error, not a fail, so a change that makes
the Planner loop shows up as more timeouts. The exit code is 0 only
when every row ran and passed, 1 when a row failed, is an error or was
skipped, and 2 when the run could not start or stopped midway.

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
settings layer of the run's database, so every agent uses it. Measured at
`68d7b953c` with `--reps 3` (the committed baselines): all 22 tasks with haiku
cost $7.99 in about 70 minutes, about $2.66 and 23 minutes a rep, the costliest
being `roles_multi_platform` (about $0.47) and `many_steps` (about $0.28); the
opus tier, `--model opus --tasks planner` (13 tasks), cost $18.74 in about 48
minutes, about $6.25 and 16 minutes a rep, `roles_multi_platform` about $1.92
and `interface_first` about $0.86 of it. The default caps stop either run early:
give it a `--total-cap-usd` that covers all its reps, such as 10 for haiku and
25 for opus at `--reps 3`, and a `--max-budget-usd` no single run reaches, such
as 1.0 for haiku and 3.0 for opus (the costliest whole task, all its runs
together, was $0.62 with haiku and $2.03 with opus). (`baselines/real-sonnet.json` is older: five tasks,
one rep each.) With haiku, `interface_first` fails on the plan's shape in all
three reps: its Planner chains the service, the client and the check. With opus
it passes in all three: the Planner pins the protocol in a file first, builds
the two parts side by side and checks them together last. Expect some noise
between identical runs: a task's cost moved by a median of 8% between reps
with haiku and 2% with opus (more where the reps took different paths, such as
a pass and a fail), and an occasional single contract violation that a repair
turn fixes is not a regression by itself.

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
single_step,chat_correction` (task ids, or a named set: `planner`, the tasks
whose pass or fail turns on the Planner's judgment, `dependent_steps`,
`parallel_parts`, `interface_first`, `supplement_replan`,
`supplement_no_change`, `requirement_edit`, `roles_tester_docs`,
`roles_not_needed`, `roles_multi_platform`, `roles_template_fits`,
`roles_template_unfit`, `gate_ambiguous`,
`gate_not_needed`, `gate_parallel_branch` and `gate_discoverable`; a set is
resolved against the
tasks there are, so a task it names that does not exist yet is left out until
it is added; sets and ids mix, as in `--tasks planner,single_step`), `--reps`,
`--max-budget-usd`, `--total-cap-usd` (for all the reps), `--baseline
<run.json>|previous`, `--keep-work-dirs`,
`--step-workspaces shared|worktree` (each working directory becomes a git
repository on `main` with one commit, the task's project gets that setting, and
each task reports the merge and checkpoint commits it added as
`looper_commits`), `--user-gates on|off` (every task's project but a gate
task's gets that `user_gates` setting; a gate task always has gates on, so
`--tasks planner` runs its gate tasks as they are meant to: `on` runs the
other tasks with the gate contract, so `gates_opened` there shows how often
the Planner asks when it need not). The runner plays the user in a task's
gates: when one waits for the user it posts the task's `gate_script` in turn
(`<end>` ends the discussion; a task without one answers "No preference:
decide yourself and state your assumptions." and then ends it). When the
Planner asks again after a script has run out, the runner ends the discussion,
so the gate closes on the Planner's assumptions instead of waiting for the
task's timeout; `gate_asks_beyond_script` counts those gates, and
`gate_ambiguous`, whose script answers all it asks, fails on one. To print a
comparison again:

```bash
uv run python -m evals.prompt_regression compare evals/runs/<new>.json [evals/runs/<base>.json]
```

### Which tier to run

A pull request runs real mode on its change and pastes the tables into its
description; `--total-cap-usd` covers all the reps, so raise it with them:

- any change to model-visible content (a recommended prompt, a contract, the
  injection order, the injected context) runs the haiku tier on all tasks:
  `run --mode real --reps 3` (about $8 and 70 minutes);
- a change to the Planner's prompt, its contracts, the plan contract or the
  context the Planner receives also runs the opus tier, `run --mode real
  --model opus --reps 3 --tasks planner`, compared with
  `baselines/real-opus.json` (about $19 and 48 minutes);
- a change to the Manager's prompt or its tools runs `dialogs --mode real
  --reps 3` (about $1.50 and 14 minutes), and also `dialogs --mode real
  --manager-model opus --manager-effort xhigh --reps 3` (about $3.50 and 16
  minutes).

Each compares with the committed baseline of its model, or with a run of the
base branch via `--baseline`. Update the model's committed baseline
(`baselines/real-haiku.json`, `real-sonnet.json` or `real-opus.json`; for the
dialogs, `dialogs-real-<model>.json` by the Manager's model) when an intended
change moves the numbers, from a run with `--reps 3`; it holds numbers only,
never transcripts or local paths (a dialog keeps each turn's tool calls and
problem, never the messages or the Manager's replies).

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
| `concurrency` | a running task's own Worker limit changes at once (`looper_concurrency`); asked to change a project's Worker limit, which no longer exists (concurrency is per task), the Manager says so and nothing changes or waits for a confirmation, not the default in Settings → Roles either |
| `status` | a status question is answered by reading through the tools (run it alone to try a runtime) |
| `config_tools` | the configuration tools: a project is created with the directory the user named (`looper_projects`); the Agent Profiles are listed; a Reviewer's profile in a project is set at once and the project's allowed list holds the one profile named (`looper_settings`); this week's spend is read (`looper_costs`); deleting the project waits for the user's word in the next message |
| `provider_key` | a provider registered from a message that pastes its API key: it is registered without the key (`looper_providers`), and the key is in no reply, no stored provider and not in the Manager's trail |
| `schedules` | on an enabled schedule made first: the schedules are listed by reading (`looper_schedules`); disabling it runs at once; deleting it waits for the user's word in the next message, and 「确认」 deletes it |

Each (dialog, rep) runs on a database, a `looper serve`, a working directory
and a project of its own, so nothing one dialog leaves behind (a ready task, a
changed setting) reaches the next; their spend adds up for
`--total-cap-usd`. The dialogs that need a running task start a slow one
first (`sleep 300`) and stop whatever is still running at their end. A
dialog's outcome is `pass`, `fail` (a check found something wrong) or `error`:
no reply within `--timeout` (`timeout`) or the eval itself failed (`harness`,
such as the slow task not getting under way); after an error its remaining
turns are not sent and the run goes on with the next dialog. Fake mode plays
the Manager with the stand-in, which makes each message's scripted calls over
MCP; it takes about a minute. Real mode with haiku cost $1.54 for all ten
dialogs at `--reps 3` (about $0.51 and 5 minutes a rep, measured at
`68d7b953c`), so the default `--total-cap-usd` (0.50) stops it after about one
rep: give it one that covers all the reps, such as 3:

```bash
uv run --extra test python -m evals.prompt_regression dialogs --mode real \
  --pg-url postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

Options: `--dialogs status,ask_project`, `--model`, `--agent`,
`--manager-model opus --manager-effort xhigh` (the Manager alone on that
model, the other roles on `--model`; all ten dialogs at `--reps 3` cost $3.49
in about 16 minutes, about $1.16 a rep, the costliest single dialog
`config_tools` at about $0.22; give it `--max-budget-usd 3.0` and a
`--total-cap-usd` such as 6),
`--register-agent SPEC.json` (as for `run`; `--dialogs status` with a Codex or
OMP agent checks that runtime's tool calls), `--reps` (as for `run`),
`--baseline <run.json>|previous` (the default is
`baselines/dialogs-real-<model>.json` by the Manager's model: `haiku`, and
`opus` for the run with the Manager on opus at xhigh), `--max-budget-usd`,
`--total-cap-usd`, `--timeout` (seconds for each reply and each check),
`--keep-work-dirs`. The run's JSON goes to
`evals/runs/`, with a server log per (dialog, rep) in
`<stamp>-dialogs-<mode>.server-logs/`. It prints the comparison (success
`k/n`, errors and cost, as for `run`), then a table of each (dialog, rep)'s
outcome, its tool calls per turn (a refused one with its error code) and its
cost; the JSON keeps what each refused call was told and the arguments it came
with.
