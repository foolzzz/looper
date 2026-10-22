# Evals

## Prompt regression (`evals/prompt_regression`)

Tells you whether a change to a recommended role prompt, a code-owned contract
(`REPLY_CONTRACT`, `WORK_CONTRACT`, `REVIEW_CONTRACT`) or the way the system
prompt is put together makes the agents worse.

It runs thirty-nine small tasks through a disposable `looper serve` (a new
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
| `wait_pr_rereview` | review a simulated pull request whose loop skips the last value, then review the author's real fix commit. A successful `github_pr` / `head_changed` check of the requested PR must separate the two dependent Workers delivering `findings.md` and `rereview.md`; a manual resume does not count |
| `wait_not_needed` | write and run `build.sh`, then check its `out/app.txt` and write `ok` to `result.txt`: the build is the task's own work, so no wait opens (`max_waits_opened` 0) |
| `operator_notes` | the task's project gives its agents the operator's notes (`operator_notes` on), and only they hold the staging port: `port.txt` is 59322 |
| `operator_notes_stale` | the notes give a production port the repository's `config/production.toml` contradicts: the files win, `port.txt` is 8443 |
| `vault_closeout_keeps` | the project has the knowledge vault on; adding multiplication uncovers a test runner that needs `CALC_LOCALE=C`: the Planner's closeout must keep that fact in a committed note under `codex-mem/` |
| `vault_closeout_nothing` | the same switch on a one-file greeting task: the Planner closes out without writing a note |
| `room_interface` | two parts built at the same time, each in a worktree of its own (`step_workspaces` `worktree` for this task), where the store's author picks the record's field names while writing it and the client must use them: the agents must coordinate in the task's agent room (`room_messages` at least 1), and `python cli.py widget 3` works against the store |
| `room_consult` | two steps, the second after the first: the first makes up a 20-character code while it runs, keeps it out of every file and its report (only its SHA-256 goes to `code.sha256`) and tells it only when asked, the second writes it to `code.txt`; only the first step's session knows it (the plan cannot carry it, and no step can find it from its SHA-256 by trying them all, as it could a 6-digit number's), so the second must ask that seat in the room, and a consult of its ended run's session answers (`room_consults` at least 1) |
| `room_quiet` | a one-file task with the agent room on, as every task now has it: no room message at all (`room_messages` 0), one step |
| `planner_plan_file` | the requirement asks for a PLAN.md with the plan before anything else, which the Planner may be tempted to write itself: it runs read-only, so a Worker writes it, and the Planner changes no file (`max_planner_modified_files` 0); `planner_denials` says whether it tried |
| `reviewer_retry_note` | the Planner and the Workers play a script and only the Reviewer is Claude Code: the plan left out a region, the step's first attempt fails, the Planner retries it with a note that corrects the count, and the Reviewer, who has the note, must pass the corrected work in its one review (the requirement leaves the regions to the step, so only the note can correct it) |
| `reviewer_unrelated_note` | the same, the other way: the retry note is about the file's separator, and the work it leads to still lacks a region, so the Reviewer must not pass it; the next attempt has it and passes (exactly two reviews and two retries) |
| `reviewer_elsewhere` | only the Reviewer is Claude Code: the correct summary lives only in another worktree and branch; its reported location lets the Reviewer inspect it and pass in one review, with no retry |
| `reviewer_elsewhere_wrong` | the same location, but the first summary lacks a region; the Reviewer must fail it, then pass the corrected summary (exactly two reviews and one retry) |

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
gate's conclusion, `conclusion_reads`), the waits whose `opened_at` is set (`waits_opened`), waits with a
`github_pr` or `command` check (`wait_github_checks`, `wait_command_checks`, including unopened waits), steps
directly depending on at least one wait (`wait_dependents`, once per step), waits currently in `timed_out`
(`waits_timed_out`), what the agents did in the task's agent room (`room_messages`, Looper's notices
and escalations apart; of them `room_questions`, `room_unanswered` and `room_interface`; `room_escalations`;
`room_reminders`, the reminders Claude Code runs got; `room_refused`, the posts the room refused; and
`room_tool_calls`, the runs' calls of the room's tools, reads included; `room_consults`, the consults of seats whose run
had ended, of them answered `room_consults_answered`), retries, reviews, cost (the task cost API), tokens (the runs' records: prompt, uncached
and cached, and output; what a runtime without prices still reports), contract violations
(replies the Planner's reply parser or the Worker/Reviewer report check
rejected; the JSON keeps why the parser rejected each Planner reply,
`planner_violation_reasons`), whether the Planner modified files itself (its file-editing tool
calls, and files changed while only a Planner turn ran), the tool calls its runtime refused the read-only Planner
(`planner_denials`, from its runs' `permission_denials`), the task's vault commits (`vault_writes`, identified by
the exact `looper: <task_id> ` subject prefix), their unique committed paths outside `codex-mem/`
(`vault_outside`, which must be zero), and whether its activity has a `Vault closeout:` row (`vault_closeout`,
0 or 1). A task's `vault_check` also checks the content of its committed notes. It reads the runs that ended
in an error (`run_errors`, by their records' status) and the models each role's runs reported (`models`).

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

Every eval server, in fake and real mode (dialogs included), installs a Python `gh` stub in its `bin_dir`,
first on `PATH`: evals never call the real GitHub CLI. It accepts only
`gh pr view <n> --repo <r> --json <fields>` and `gh pr diff <n> --repo <r>`.
By default, views return head `1111111111111111111111111111111111111111`, state `OPEN`, and empty comments,
reviews and checks. Diffs come from the task's `gh_pr_diffs` fixture (`owner/repo#number` keys), or are empty.
For `wait_pr_rereview`, each case has an isolated git repository: views (including commit OIDs) and diffs
read the same pull ref. The wait check runs in the task directory, so it sees the same PR as its Workers.
Every other command exits 1 with `gh stub: unsupported`.

After the requested PR's wait has recorded its old head, and at least 5 seconds after it opened, the runner
commits `gh_pr_fix_files` and advances that pull ref once. The actual head-change check releases the wait;
the fake Workers also read the diff and reject the wrong revision. This task requests a 10-second check
interval and a 45-second maximum wait, within both the fake case's 60 seconds and the real case's 900 seconds.
Runs containing this fixture set the disposable server's minimum check interval to 10 seconds.

Only tasks with `wait_resume_after_s` set let the runner manually resume waits: after that many seconds from
`opened_at`, each wait still in `waiting` is resumed through `POST /api/tasks/:id/waits/:step/resume`, once.
Each row records successful resumes in `wait_resumes` (step label to timestamp, on the database clock).
With the field unset (`None`), as in `wait_pr_rereview`, the runner never resumes waits. Flow expectations
include `min_waits_opened`, `max_waits_opened`, `min_wait_github_checks` and `min_wait_dependents`.

### Fake mode: the flow, free

A scripted stand-in replaces `claude` and plays each role from the task's
script, so this checks the plumbing, not the model. In `room_consult`, its
second Worker writes the code its inbox delivered; a wrong or missing consult
answer fails the task's file check. It takes a few seconds:

```bash
uv run --extra test python -m evals.prompt_regression run --mode fake \
  --pg-url postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

`tests/test_prompt_eval.py` runs it as part of the test suite when
`LOOPER_TEST_DATABASE_URL` is set.

A script may also give the Planner's retry notes (`retry_feedback`: a step's
Nth retry gets its Nth note) and fail the review of some attempts
(`failing_review_attempts`, read from the Worker's report). The two
`reviewer_elsewhere` tasks instead use `review_reads`: the fake Reviewer parses
the first worktree location from the user turn's fenced JSON lines, reads
`<worktree>/<repo_path>` (or the configured file in its working directory when
there is no worktree location), and passes only when every required string is
present. Omitting the location block makes even the correct sibling file fail
review. A script with `real_reviewer` plays its Planner and Workers in real
mode too, and only its Reviewers are Claude Code: the stand-in hands Claude
Code every other run of
every task, with its arguments, so the other tasks run as before.

### Real mode: the effect, on demand

Runs real Claude Code (`haiku` by default) and costs money. Every Claude Code
run gets `--max-budget-usd` (default 0.25), and the run stops once its tasks
have spent `--total-cap-usd` (default 0.80); a task that reaches the cap is
cancelled and the rest are skipped. `--model` sets the model in the system
settings layer of the run's database, so every agent uses it. Measured at
`c9ac42d5c` with `--reps 3 --jobs 4` (the committed baselines): all 35 tasks
with haiku cost $12.30 in about 29 minutes, about $4.10 a rep, the costliest
being `roles_multi_platform` (about $0.42) and `many_steps` (about $0.30); the
opus tier, `--model opus --tasks planner` (20 tasks), cost $27.81, about $9.27
a rep, `roles_multi_platform` about $2.06 and `interface_first` about $0.89
of it. The default caps stop either run early: give it a `--total-cap-usd`
that covers all its reps, such as 20 for haiku and 40 for opus at `--reps 3`,
and a `--max-budget-usd` no single run reaches, such as 1.0 for haiku and 3.0
for opus (the costliest whole task, all its runs together, was $0.52 with
haiku and $2.26 with opus). The parallel runs used `--case-budget-usd 2.0`
for haiku and `--case-budget-usd 6.0` for opus.
(`baselines/real-sonnet.json` is older: five tasks,
one rep each.) With haiku, `interface_first` fails on the plan's shape in all
three reps: its Planner chains the service, the client and the check. With opus
it passes in all three: the Planner pins the protocol in a file first, builds
the two parts side by side and checks them together last. Expect some noise
between identical runs, especially where the reps take different paths, such
as a pass and a fail, and an occasional single contract violation that a repair
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
read from `LOOPER_EVAL_API_KEY`; `--agent-model` picks the model, default that
API's first, and `--provider-base-url` replaces the preset's endpoint, for
example with a logging proxy; the per-run cap is then Looper's own at the
provider's prices, and each task reports its `cost_basis`), `--register-agent
SPEC.json` (register provider → model → profile in the eval's disposable
database, then run every role on it). The explicit spec is:

```json
{
  "runtime": {"type": "claude-code"},
  "provider": {
    "name": "Eval account", "vendor_type": "custom",
    "endpoints": [{"protocol": "anthropic-messages", "base_url": "https://api.example.invalid"}],
    "api_key_env": "LOOPER_EVAL_API_KEY"
  },
  "model": {"name": "example-model", "protocol": "anthropic-messages"},
  "agent": {"name": "Eval profile", "effort": "high", "agent_teams": false, "custom_prompt": ""}
}
```

Replace the example endpoint and model with the intended callable entry.
Runtimes are built in: `runtime.name` selects one by name, else `runtime.type`
selects the detected built-in runtime (`codex`, `omp`, `grok`, `pi`, `dsh` when on PATH).
`provider.api_key_env` names an environment variable; literal `api_key` in the
spec is rejected. Never put a credential in the JSON file. A saved provider
can instead be selected with `{"provider_id": "prov-codex-login"}` (or
`prov-grok-login`, for that runtime's own login). The `model` layer accepts an
explicit `{name, protocol, settings...}`, a saved model-id string,
`{"model_id": "..."}`, or an exact saved `{provider_id, name, protocol}`
reference belonging to that account. The `agent` layer holds only its name
and runtime options; profile system-prompt mode is not an option. Role/task
mode selection continues to apply.

Older specs with provider `protocols`/`base_url`/`models` and `agent.model`
still use the shared legacy converter. Ambiguous or unresolvable bindings fail
rather than selecting a different model. API writes use `model_id`, and the
profile view's `model` is structured. Registration coverage is in
[the registration contract tests](../tests/test_model_eval_registration.py);
the [delivery ledger](../docs/plans/2026-10-13-long-horizon-agent-context.zh-CN.md#54-验证缺口)
keeps synthetic tests, real-mode dialogs and actual migration rehearsal separate.
Unknown prices retain tokens with unknown cost; they cannot prove a spend cap.
Registration success alone is not a real-model evaluation.

Other options: `--tasks
single_step,chat_correction` (task ids, or a named set: `planner`, the tasks
whose pass or fail turns on the Planner's judgment, `dependent_steps`,
`parallel_parts`, `interface_first`, `supplement_replan`,
`supplement_no_change`, `requirement_edit`, `roles_tester_docs`,
`roles_not_needed`, `roles_multi_platform`, `roles_template_fits`,
`roles_template_unfit`, `gate_ambiguous`,
`gate_not_needed`, `gate_parallel_branch`, `gate_discoverable`, `room_interface`,
`room_consult`, `planner_plan_file`, `operator_notes`, `operator_notes_stale`,
`vault_closeout_keeps`, `vault_closeout_nothing`, `wait_pr_rereview` and `wait_not_needed` (24 tasks); a set is
resolved against the
tasks there are, so a task it names that does not exist yet is left out until
it is added; sets and ids mix, as in `--tasks planner,single_step`), `--reps`,
`--max-budget-usd`, `--total-cap-usd` (for all the reps), `--baseline
<run.json>|previous`, `--keep-work-dirs`,
`--step-workspaces shared|worktree` (each working directory becomes a git
repository on `main` with one commit, the task's project gets that setting, and
each task reports the merge and checkpoint commits it added as
`looper_commits`; every task's project runs its tasks in place, as before task
workspaces, so the checks read the working directory), `--user-gates on|off` (every task's project but a gate
task's gets that `user_gates` setting; a gate task always has gates on, so
`--tasks planner` runs its gate tasks as they are meant to; unset is on, as
in Looper, so every task runs with the gate contract and `gates_opened` shows
how often the Planner asks when it need not, and `off` runs the other tasks
without it). Each row records the `user_gates` its Planner had (`on` or
`off`), and the comparison lists the tasks whose setting differs from the
base's, or that a base from before user gates G2 (unset was off then) does
not record. The runner plays the user in a task's
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

### DSH validation

[DSH's RFC](../docs/architecture/rfcs/dsh-runtime-v0.zh-CN.md#9-验收与边界)
tracks passed, failed and untested separately. Local fake responses and the
installed package's offline checks do not establish real-model acceptance.
The opt-in launcher is [examples/dsh/validate.py](../examples/dsh/validate.py),
using [agent-spec.json](../examples/dsh/agent-spec.json): DSH 0.2.1-rc.2,
DeepSeek Messages, `deepseek-flash`, effort `high`, effective append. It
registers a DSH Profile for all four roles; `--provider-preset deepseek`
would create a Claude Code Profile instead.

From the checkout root, these commands make no model calls:

```sh
uv run --extra test python -m examples.dsh.validate --help
uv run --extra test python -m examples.dsh.validate preflight --help
uv run --extra test python -m examples.dsh.validate preflight \
  --disposable-pg-url postgresql://eval:password@127.0.0.1:55432/postgres
```

The URL is an example for a dedicated, disposable local PostgreSQL instance;
replace it with yours, never an operator or production database. Preflight
requires an explicit local `/postgres` management URL, rejects port 8767,
checks the interpreter/checkout, exact DSH version, help and process-tree
observation, and uses temporary private homes for the probe. It neither reads
the key nor connects to PostgreSQL: `credentials`, `database_connection` and
`real_validation` remain `untested` even when preflight passes. Missing or
unsupported dependencies are a failed preflight, not model acceptance.

The following are **pending, paid acceptance commands**, each requiring an
explicit `--real`; they were not run for this documentation change:

```sh
uv run --extra test python -m examples.dsh.validate task-basic --real \
  --disposable-pg-url postgresql://eval:password@127.0.0.1:55432/postgres
uv run --extra test python -m examples.dsh.validate task-review --real \
  --disposable-pg-url postgresql://eval:password@127.0.0.1:55432/postgres
uv run --extra test python -m examples.dsh.validate manager --real \
  --disposable-pg-url postgresql://eval:password@127.0.0.1:55432/postgres
uv run --extra test python -m examples.dsh.validate smoke --real \
  --disposable-pg-url postgresql://eval:password@127.0.0.1:55432/postgres
```

Only the launcher captures the key at runtime from the configured interactive
zsh into memory, then supplies the eval provider through `api_key_env`; do not
expand, echo or paste it into a shell command, spec, report or artifact. Empty
or failed capture stops the run. Eval artifacts stay in ignored
`evals/runs/dsh/`; smoke uses a disposable root and fresh database, closes
external channels and starts its own server away from 8767. It checks two
Manager turns, two tasks, all four roles, room consults and API/DB readback.
Channel stand-ins do not establish Telegram/Lark delivery to real accounts.

The eval commands select `single_step,dependent_steps`,
`reviewed_step,planner_plan_file` and Manager `status,confirm_create_start`,
respectively, each at `jobs=1`, `reps=2`. Manager results include actual launch,
Session and root usage readback plus the existing per-turn tool audit. Missing
evidence cannot establish acceptance; repeated MCP success alone does not
prove a changed run token was applied. Each invocation sets a $0.50 per-run
soft cap, a $4 total observed-spend cap, 300 seconds per task/dialog (per stage
for smoke), and 20 minutes outside the workflow plus cleanup grace. With one
job, **`--case-budget-usd` is not enforced**; the launcher does not pass it or
promise a per-case cap. Root observed cost is not a complete bill: summary,
search, child-session and consult coverage remains unknown, including when
the official Session reader returns observations. These are not hard billing
limits; unknown cost causing a consult refusal is a failed smoke, not a pass.

For an eval command, optional `--baseline path/to/real-append.json` must point
to an existing real result with the same case and explicit
`validation.effective_system_prompt_mode=append`. Missing or incompatible
evidence is a gap; the launcher neither starts Claude/OMP nor updates public
baselines. Existing Claude/OMP/pi workflows remain independent. The RFC's
broader two-repetition task/dialog comparison is still pending; these three
one-case commands do not cover `dependent_steps`, `planner_plan_file` or
`confirm_create_start`.

### Which tier to run

A pull request runs real mode only on what its change touches, and pastes the
tables into its description. Before a release, one run of the full tiers
refreshes the baselines. `--total-cap-usd` covers all the reps, so raise it
with them. Every `--jobs` command below also takes `--case-budget-usd` (see
Running cases in parallel).

Per pull request, on the tasks and dialogs the change touches plus any new
ones, with `--reps 2` and haiku (`--tasks a,b` or a task set, `--dialogs a,b`):

- a change to model-visible content (a recommended prompt, a contract, the
  injection order, the injected context) runs those tasks:
  `run --mode real --reps 2 --tasks <ids> --jobs 4 --case-budget-usd 1`;
- a change to the Planner's prompt, its contracts, the plan contract or the
  context the Planner receives also runs them on opus, `run --mode real
  --model opus --reps 2 --tasks <ids> --jobs 4`, compared with
  `baselines/real-opus.json`;
- a change to the Manager's prompt or its tools runs `dialogs --mode real
  --reps 2 --dialogs <ids> --jobs 4`, and also, with `--manager-model opus
  --manager-effort xhigh`, the same dialogs on the opus Manager.

Do not rerun after a change the model cannot see.

Once before a release, on the release candidate (`main` after every merge):
haiku on all 39 tasks, `run --mode real --reps 3 --jobs 4 --max-budget-usd 1.0
--case-budget-usd 2.0 --total-cap-usd 20` (the earlier baseline cost about $12.32); opus,
`run --mode real --model opus --reps 3 --tasks planner --jobs 4
--max-budget-usd 3.0 --case-budget-usd 6.0 --total-cap-usd 40` (24 tasks;
the earlier baseline cost about $27.73); and all 22 dialogs on haiku and on the opus xhigh Manager,
`dialogs --mode real --reps 3 --jobs 4 --max-budget-usd 1.0
--case-budget-usd 1.0 --total-cap-usd 8` (about $4.36) and
`dialogs --mode real --reps 3 --jobs 4 --manager-model opus
--manager-effort xhigh --max-budget-usd 3.0 --case-budget-usd 3.0
--total-cap-usd 12` (about $7.91). `--jobs` above 1 under a total cap needs
`--case-budget-usd`. The baselines are refreshed from these runs.

#### OMP on DeepSeek

`evals/agents/omp-deepseek.example.json` registers OMP on DeepSeek's `openai-chat` API (`LOOPER_EVAL_API_KEY`). The
provider preset lists the same protocol at `https://api.deepseek.com` (no `/v1`); the example keeps `/v1`, as the Pi
example does, and the DeepSeek protocol for OMP still needs a real run to verify. Cross-runtime switch:

```
uv run python -m evals.prompt_regression dialogs --mode real --dialogs session_profile_switch \
  --switch-agent evals/agents/omp-deepseek.example.json
```

#### Pi

`--register-agent` can target the built-in runtime type `pi` (API key only:
`openai-responses`, or `openai-chat`, which pi calls `openai-completions`).
Example specs are `evals/agents/pi-cpa.example.json` and
`evals/agents/pi-deepseek.example.json`. These legacy specs use the shared
converter above; new specs should declare provider endpoints and a separate
model with an explicit protocol. The selected model fixes the endpoint, whose
`base_url` is passed through. The entry scripts are `examples/pi/eval-responses.sh`,
`examples/pi/eval-deepseek.sh` and `examples/pi/eval-prompt-comparison.sh`.
They follow the tiers above. The comparison script scopes 13 distinct cases
across 4 tiers, 48 case/reps, and exits before `uv` when `ANTHROPIC_API_KEY`
is absent.

This change did not run those real tiers. The Claude CLI comparison is 0
passed, 0 failed and 48 unverified because the API key was absent. The two
provider scripts were not run against a live Responses or Chat provider.
Tests of the scripts with a fake `uv` (83 passed) check command, budget and
gap boundaries only. A syntax pass stays short of acceptance, and a pytest
skip when no disposable database is available stays unverified. Baselines
stay as they are. The scripts also leave unverified: steering, worker session
resume, the cancel tree, a raw Responses readback of optional parameters and
`strict`, the `operator_notes_stale` and `schedule_*` dialogs, and a shared
prompt or contract comparison against the baseline commit.

#### Running cases in parallel

`--jobs N` (default 1, one case after another) runs N cases (task or dialog,
times reps) at once; the tokens and cost are the same, only the wall time
shrinks (the committed haiku baseline with `--reps 3 --jobs 4`
took about 29 minutes). Use `--jobs 4`. `run` puts its cases on one server,
each in a project, a working directory and a task of its own; `dialogs` gives each case
its own database, server, port and directories. The rows keep the task/rep
order whatever finishes first.

When a selected task has `vault=True`, the server run also gets one shared,
throwaway git vault beside its runtime and work directories, seeded from `EVAL_VAULT` with local git author
settings and an initial commit. Only those tasks opt their projects in; the server's `knowledge_vault` points
to that directory, with `codex-mem` writable and `codex-mem/AGENTS.md` as its rules. Concurrent tasks share
this vault, and each row counts only its own commits. The vault is removed with the other temporary directories
unless `--keep-work-dirs` is set.

Repetitions share this vault too. Its rules keep one note per project, named after the project, and each
task run has a project of its own (`eval-<task>-<suffix>`), so each repetition of `vault_closeout_keeps` keeps
the fact in a note of its own. The fake script writes the fixed `codex-mem/projects/calc.md` with a null sha,
which a later repetition finds taken: run the vault tasks with `--reps 1` in fake mode.

`--total-cap-usd` still holds with cases in
flight: while others run, a case starts only when what is spent plus the
`--case-budget-usd` (the most one case may spend; required with `--jobs` above
1 in real mode, since `--max-budget-usd` caps each Claude call and a case makes
several) of every case in flight and of this one is within the cap (a case
alone is cut at the cap, as before). On Ctrl-C the cases in flight cancel their
tasks and end before the run writes its file. More jobs load the machine and the
model's rate limit, so keep `--task-timeout` generous on a busy host.

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
| `rename_task` | asked to rename a task not started, the Manager makes one `looper_task_update` with the task id and the new `title` and no other write; the task is then called by it |
| `cancel_confirmation` | cancelling waits for the user's word: a refusal leaves the task running, a confirmation in the next message cancels it |
| `concurrency` | a running task's own Worker limit changes at once (`looper_concurrency`); asked to change a project's Worker limit, which no longer exists (concurrency is per task), the Manager says so and nothing changes or waits for a confirmation, not the default in Settings → Roles either |
| `status` | a status question is answered by reading through the tools (run it alone to try a runtime) |
| `config_tools` | the configuration tools: a project is created with the directory the user named (`looper_projects`); the Agent Profiles are listed; a Reviewer's profile in a project is set at once and the project's allowed list holds the one profile named (`looper_settings`); this week's spend is read (`looper_costs`); deleting the project waits for the user's word in the next message |
| `task_cost` | a running task's cost by model: read for that task (`looper_costs` with its task_id), nothing changes, and the reply names each model of the task's cost and gives its cost as rounded (fake mode checks the read only) |
| `provider_key` | a provider registered from a message that pastes its API key: it is registered without the key (`looper_providers`), and the key is in no reply, no stored provider and not in the Manager's trail |
| `start_no_wait` | a ready task and "开始": the Manager starts it once and ends the reply, with no shell/sleep and at most one status read after the start; the reply says it started and its questions will come to the user |
| `gate_relay` | the running task waits at a user gate (written into its plan first, so no Planner has to decide to ask): the user answers its question in the Manager's chat, and the Manager passes their words to the gate (`looper_task_gate_reply`), not as a supplement |
| `gate_unrelated` | the same open gate, and a question about something else: nothing goes to the gate and nothing changes |
| `schedules` | on an enabled schedule made first: the schedules are listed by reading (`looper_schedules`); disabling it runs at once; deleting it waits for the user's word in the next message, and 「确认」 deletes it |
| `workspace_in_place` | the user asks for the task to change the project directory itself, no worktree or branch: it is created ready and runs in place (`workspace_mode` `in_place`) |
| `workspace_default` | the user says nothing about how the task runs: it is created ready, and the Manager passes no `workspace_mode` |
| `schedule_create` | a schedule asked for in the project is created at once, disabled, on the rule the user said (`looper_schedules` `create`), and the reply carries its configuration and the question whether to enable it; 「启用」 waits for the user's word in the next message (`needs_confirmation`, still disabled), and 「确认。」 enables it |
| `schedule_planner_profile` | the user asks for a schedule whose Planner may be the cheaper `eval/light` profile (registered first): it is created disabled with only the Planner's profile set, and a later change of its time leaves the profile as it was |
| `schedule_no_profiles` | the other direction: a schedule asked for with no word about profiles gets none (the project's apply) |
| `schedule_enable_declined` | the same schedule created disabled; 「启用」 waits for a confirmation, and when the user then says to leave it, it stays disabled and no enable call runs |
| `scheduler_settings` | the schedules' default run timeout (`looper_settings`, action `scheduler`) waits for the user's word in the next message, and 「确认。」 changes it to 1800 s |
| `schedule_timeout` | a schedule created with a 20-minute run timeout the user names: it has 1200 s itself and the system default stays at the built-in value |
| `schedule_timeout_unnamed` | the same schedule created with no timeout named: it follows the system default (`"default"`) |
| `schedule_ask_project` | a schedule without a project: the Manager asks, and creates and changes nothing |
| `schedule_modify` | on an enabled schedule with a $1 daily cap: a new rule and a Worker limit change at once; raising the cap waits for the user's word in the next message, and a refusal leaves it |
| `schedule_run` | on a schedule whose gate finds nothing (no task, no cost): asking when it runs next reads only; 「现在立即运行一次」 waits for the user's word in the next message and runs nothing, and 「确认。」 runs it once (`run`) |
| `schedule_ambiguous` | two projects each have the schedule: asked to run it by name, the Manager runs neither and asks; told the project, it waits for the user's word and runs nothing, and 「确认。」 runs that one only |
| `config_export` | the configuration is exported with its projects and their schedules into the server's runtime directory (`looper_config_export`); asked to include the API keys, every export still leaves them out |
| `session_profile_switch` | the Manager's session changes its Agent Profile through `/profile`, which Looper answers itself: `/profile` lists `eval/switch` with a number and starts no Manager run; `/profile <number>` selects it (`GET /api/manager/session` `selected`, and a run recording the choice); the next message runs on it (`metadata.profile.source` `session`), after a `[HANDOFF]` run on the old profile and with a `[RESUME]` input when the switch starts a new session; `/profile 0` follows the settings again, and the next message no longer runs on the session's profile and (real mode) still knows the code word |
| `session_profile_ask` | asked to switch the session's profile, the Manager changes nothing (no `looper_settings` `set`), selects nothing and does not claim a change; it points the user to `/profile` (real mode) |

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
MCP; it takes about two minutes. Real mode with haiku cost $4.23 for all
24 dialogs at `--reps 3 --jobs 4` in about 9 minutes (about $1.41 a rep,
measured at `c9ac42d5c`, the committed baseline), so the default
`--total-cap-usd` (0.50) stops it before the first rep ends: give it one that
covers all the reps, such as 8, and `--case-budget-usd 1.0` with `--jobs 4`:

```bash
uv run --extra test python -m evals.prompt_regression dialogs --mode real \
  --pg-url postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

Options: `--dialogs status,ask_project`, `--model`, `--agent`,
`--manager-model opus --manager-effort xhigh` (the Manager alone on that
model, the other roles on `--model`; all 24 dialogs at `--reps 3 --jobs 4`
cost $8.43 at `c9ac42d5c` (the committed baseline), about $2.81 a rep, the
costliest single dialog `task_cost` at about $0.46; give it
`--max-budget-usd 3.0`, `--case-budget-usd 3.0` with `--jobs 4` and a
`--total-cap-usd` such as 12),
`--switch-agent SPEC.json` (`session_profile_switch`: register that spec as the profile `eval/switch`, while the
other roles keep `--model` / `--agent`; without it, fake mode uses Claude Code on a provider of its own, which makes
the switch start a new session, and real mode Claude Code with haiku on the login provider, which resumes),
`--register-agent SPEC.json` (as for `run`; `--dialogs status` with a Codex,
OMP, Grok or Pi agent checks that runtime's tool calls), `--reps` (as for `run`),
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
