# Changelog

This file records the changes of Looper, a fork of LoopX that kept the LoopX
name up to v2.5.0. The fork branched from
upstream [`loopx-project/loopx`](https://github.com/loopx-project/loopx) at
`71dbfd5e6` (2026-09-26) and no longer tracks upstream
([design-v0](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/design-v0.md)). For upstream history before that
point, see the upstream releases and [docs/update-notes](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/update-notes/README.md).

The fork's first release is `2.0.0` (tag `v2.0.0`). It moves past the upstream
`1.2.0` it branched from, because the role-based orchestration model changes
how a goal runs. Changes after a release are listed under **Unreleased** until
the next tag. Pull request numbers in the 2.0.0 entry refer to
[michaelx1993/loopx](https://github.com/michaelx1993/loopx/pulls).

How the agent system works today is described in
[docs/architecture/agent-system.md](docs/architecture/agent-system.md).

## [Unreleased]

## [2.8.0] - 2026-10-18

### Highlights

- **User gates on Lark and Telegram.** A gate in the plan settles what only the user can decide, while other steps run. Questions reach the chat the task came from, a reply in that thread goes to the gate, and the Manager can relay an answer. On unless a project turns them off. A task a schedule starts never opens a gate. ([#168](https://github.com/foolzzz/looper/pull/168), [#179](https://github.com/foolzzz/looper/pull/179))
- **Agent room.** The Planner and each step's seats message each other, see who is online, and post an interface without a shared file. Workers cannot lobby Reviewers. A question to the seat of an ended step is answered by a read-only consult of that seat's last session; what it does not answer goes to the Planner. ([#187](https://github.com/foolzzz/looper/pull/187), [#188](https://github.com/foolzzz/looper/pull/188))
- **Planner-defined roles and templates.** Project templates the Planner copies, the user's edit of a role on the task page, several review roles in order, catalog items a role loads, and a project's roles summed across its tasks. A run's role is named wherever runs are shown. ([#170](https://github.com/foolzzz/looper/pull/170), [#181](https://github.com/foolzzz/looper/pull/181), [#182](https://github.com/foolzzz/looper/pull/182))
- **Grok.** xAI's `grok` CLI for the Manager, Planner, Worker and Reviewer, with a built-in login provider and a home that does not read the operator's own Grok, Claude Code or Cursor configuration. ([#185](https://github.com/foolzzz/looper/pull/185))
- **System prompt append or replace.** Per Agent Profile. `append` is unchanged. `replace` is opt-in and drops that runtime's default guidance. A runtime that cannot replace is refused at save. ([#193](https://github.com/foolzzz/looper/pull/193))
- **Task workspaces and operator notes.** A task runs in a git worktree of the directory it names, on a branch Looper never merges. New projects default to that. Operator notes are on for every project and reach every runtime. ([#190](https://github.com/foolzzz/looper/pull/190))
- **Schedules from the Manager.** Create, change and run, with schedules in the configuration file, always imported disabled. Enable and run-now wait for the next message. ([#178](https://github.com/foolzzz/looper/pull/178))
- **Task cost by model.** Tokens and cost per model on the task, the Manager's turns apart from the task's own cost and its spend cap. ([#189](https://github.com/foolzzz/looper/pull/189))
- **A read-only Planner, and a faster stop.** The Planner's writes are refused by its tools. `looper serve` stops in seconds and always exits. Every run record reaches a terminal status. A step records when it first started. The Pipeline card opens on hover and the graph can go full screen. ([#172](https://github.com/foolzzz/looper/pull/172), [#174](https://github.com/foolzzz/looper/pull/174), [#177](https://github.com/foolzzz/looper/pull/177), [#183](https://github.com/foolzzz/looper/pull/183), [#184](https://github.com/foolzzz/looper/pull/184), [#186](https://github.com/foolzzz/looper/pull/186))

### Behavior changes

These are the defaults that move in this release. Each one applies without a new setting unless the line says how to turn it off.

1. **User gates are on by default.** A project that never set `user_gates` now lets its Planners open gates. The attended Planner's contract grows by the gates section. Set the project to `user_gates: "off"` to keep the previous contract. A task a schedule starts still never opens a gate. ([#168](https://github.com/foolzzz/looper/pull/168), [#179](https://github.com/foolzzz/looper/pull/179))
2. **The agent room is on by default.** A project that never set `agent_room` gives every task a room and the room tools. A question to the seat of an ended step is answered by a read-only consult of that seat's last session; what it does not answer goes to the Planner. Set `agent_room` to `off` for the previous prompts and tool lists. ([#187](https://github.com/foolzzz/looper/pull/187), [#188](https://github.com/foolzzz/looper/pull/188))
3. **New projects default to worktree task workspaces.** A project created through the API, the task form or the Manager gets `task_workspaces: "worktree"`. An existing project and an imported project stay unset, which means tasks run in place. A task that names a directory other than its project's still gets a worktree. ([#190](https://github.com/foolzzz/looper/pull/190))
4. **Operator notes are on by default for every project, including ones that already exist.** `operator_notes` unset means on. The notes reach every runtime, including Codex, OMP and Grok. Set a project to `off` to stop injecting them. ([#190](https://github.com/foolzzz/looper/pull/190))
5. **Enabling a schedule and running it now need the next message.** Through the Manager, both wait for the user to confirm in the next message. The turn that created a schedule can neither enable it nor run it. If the user does not confirm, the schedule stays disabled and nothing is enabled and nothing runs. The dashboard and the HTTP API still perform the call they are given. ([#178](https://github.com/foolzzz/looper/pull/178))
6. **The Planner is read-only, enforced by tools.** Claude Code disallows the write tools. A Grok Planner has no shell. Where a runtime cannot refuse the call, Looper records a git change after the turn and does not revert it. ([#183](https://github.com/foolzzz/looper/pull/183))
7. **Concurrency counts one task, not the project.** Nothing limits how many tasks run at once. The role layer's Worker and Reviewer limits, and the built-in 16 and 8, apply to one task. Project-level limits are removed. See Breaking Changes. ([#173](https://github.com/foolzzz/looper/pull/173))

### Added

- Agent Profiles can explicitly replace the runtime's default system prompt
  (`system_prompt_mode: replace`) in Claude Code, Codex, Grok and OMP. The
  default `append` preserves existing commands and generated files. Unsupported
  runtimes refuse replacement on save and import; the profile editor explains
  why, and run history records and displays the selected mode.

## [2.7.0] - 2026-10-16

Covers `v2.6.0` to `v2.7.0`: pull requests #72 to #166 in
[foolzzz/looper](https://github.com/foolzzz/looper/pulls).

### Added

- **Agent Profiles can run Codex with its own ChatGPT login.** A built-in
  provider `codex-login` (OAuth, Codex runtimes only) uses the operator's
  `codex login` on the server's machine. Each run's isolated `CODEX_HOME`
  gets an `auth.json` that is a symbolic link to the operator's login file
  (`auth.json` in the server's `CODEX_HOME`, else `~/.codex`), never a copy,
  so a refresh Codex saves reaches that file; its config sets
  `cli_auth_credentials_store = "file"`, Codex's built-in OpenAI provider and
  no ChatGPT apps, `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN` are removed from
  its environment, and the agent's shell commands get a `CODEX_HOME` without
  the login, so `codex logout` run by an agent cannot revoke it. After every
  run and when `looper serve` starts, Looper checks that each home's link is
  still a link: if a run left a copy of the login (a fork), the copy is
  deleted and the login is unavailable to runs, with a warning in the task's
  activity, on the provider and in the `codex_login` check, until the
  operator has checked `codex login status` and chosen "Use the login again"
  (`DELETE /api/providers/<id>/login-problem`). The Agent Profile form shows
  the login's state (`codex login status`) and suggests the models a ChatGPT
  login offers; runs are priced at the built-in prices as a subscription
  equivalent. `looper doctor` and `/api/status` report the login when an
  Agent Profile uses it. The eval's `--register-agent` can name it
  (`"provider": {"provider_id": "prov-codex-login"}`).
- **`/new` starts a new Manager session.** A chat message that is exactly
  `/new` (trimmed, any case), from the web chat, Lark or Telegram, is
  answered by Looper instead of Claude: after any turn under way, the Manager
  stops its resident Claude and drops its saved session, so the next message,
  from any channel, starts a new session without `--resume` and without the
  recent-conversation resume brief, also after a restart
  (`agent_sessions.clean_start`). The per-turn projects and goals still come,
  and running tasks and their Planners are untouched. The reset is recorded
  as a Manager run (`metadata.session_reset`: channel, sender, chat, the
  session it ended) and confirmed in the same chat. Telegram registers `/new`
  (without arguments) as a command, and the web chat has a "New session"
  button. Any other text, `/new foo` or `/news` included, is handled as
  before: a message for Claude (in Telegram, an unknown command).
- **The Manager acts on Looper through typed platform tools.** Every Manager
  launch (Claude Code, Codex or OMP) gets Looper's own MCP server `looper`,
  over any catalog server of that name: `looper mcp-server`, a stdio forwarder
  to `GET /api/agent/v1/tools` and `POST /api/agent/v1/ops/<tool>`, which take
  only the random token `looper serve` makes at each start (401 otherwise).
  The 15 tools are `looper_overview`, `looper_tasks`, `looper_task_create`,
  `looper_task_control` (`start`, `stop`, `restart`, `start_over`, `cancel`),
  `looper_task_instruct`, `looper_task_update`, `looper_concurrency`,
  `looper_projects`, `looper_agent_profiles`, `looper_providers`,
  `looper_settings`, `looper_catalog`, `looper_costs`, `looper_schedules` and
  `looper_confirm`. Each runs the code the dashboard's API runs, so a refusal
  carries the dashboard's message, and answers `ok`, `needs_confirmation` or
  `error` (`not_found`, `invalid`, `needs_user_turn`, `forbidden`, `conflict`,
  `expired`, `unavailable`). Reads run in any turn, changes only in a turn
  that answers a user's message. A `high` call (a new project, cancelling or
  starting a task over, deleting a project, an Agent Profile or a schedule,
  changing the shared defaults, any concurrency limit of a project or role, a
  higher or removed spend cap) is shown to the user in a block Looper writes
  and runs only through `looper_confirm` in the turn answering that chat's
  next message, within 10 minutes; `/new` and a restart drop it. No tool takes
  an argument named like a key, token, secret, password or credential
  (`forbidden`), and a provider is registered without its key. Every call is a
  row of the new `manager_actions` table, secret-looking values masked; `GET
  /api/tasks/:id` returns a task's as `manager_actions`, and the task page
  lists them under "By the Manager". Before a turn that answers a user, the
  Manager checks that its MCP server starts and lists the tools (a success is
  trusted for 5 minutes); when it does not, or Looper refuses its token during
  the turn, the user gets 「平台工具不可用，没有执行任何操作：…」 instead of Claude's reply, and
  the server logs an error.
- **A task can change while it runs, and its Planner hears of every change.**
  A message sent to a task's Planner on the Chat page is stored when it is
  sent, as a `supplement` journal entry with `via` and `delivered_at`. `PUT
  /api/tasks/:id/requirement` (`{"requirement": "..."}`) edits the requirement
  of a task that has not ended (400 for an empty, unchanged or over-long text,
  404 for an unknown task, 409 once it has ended), and every version is kept
  in `task_requirement_versions`, version 1 being the text the task was
  created with. A change is stored as undelivered first; the Planner gets all
  waiting changes in one `[CHAT]` turn that starts "The task changed.", with
  the new supplements and a diff of the requirement, and they are marked
  delivered only after that turn's reply is applied, so a failed turn or a
  restart does not lose them; a queued task gets them at `[PLAN]`. The
  `[CHAT]` reply gains `replan`, which replaces the steps not started, and its
  optional `stop` list stops named running steps and drops them. Every later
  Worker and Reviewer lists the supplements after the requirement in its task
  section. The task page has Edit and "History (N versions)" for the
  requirement and lists the supplements, each marked "not yet seen by the
  Planner" until it is delivered. The journal kind `user_instruction` becomes
  `supplement`: existing rows are converted once, as delivered chat
  supplements (`schema_migrations` `task_journal_supplements`).
- **A task page says where the task stands and what it delivered.** Each
  `[DECISION]` and `[SUPERVISE]` reply may carry `progress` (`done`, `now`,
  `next`, `risks`, `estimate`, each up to 500 characters), and the contract
  asks for it every time; a note is a `progress` journal entry
  (`task_journal_kinds_progress`), and `GET /api/tasks/:id` returns the notes
  newest first as `progress`. The decision that completes a task must carry
  `final` (`delivered`, `how_to_use`, `verification`, `open_issues`), or it
  gets a repair turn, and `result_summary` becomes that summary with one
  heading per field. A failed task's `error` and a stopped task's
  `result_summary` are now a closing note written by code (why it ended, what
  is done, what is not); a task that fails before it has a plan keeps its
  one-line error. On the task page's Overview, a Progress section replaces the
  Result and Error boxes.
- **Parallel steps can work in git worktrees of their own (opt-in).** A
  project execution setting, `step_workspaces`, is `shared` (the default, also
  when null) or `worktree` (Project → Settings → Execution, "Steps that run at
  the same time"). With `worktree`, when the task directory is a git checkout
  on a branch with a commit, git is 2.40 or later and no merge, rebase,
  cherry-pick, revert or bisect is in progress, a step dispatched while other
  steps of its task are in flight runs in `<runtime
  root>/worktrees/<task_id>/<key>-<id>` on branch
  `looper/<task_id>/<key>-<id>`; a checkout that fails these conditions runs
  its steps one at a time, and outside git nothing changes. Before such a
  round, Looper commits the task directory's pending changes as a checkpoint,
  the user's own uncommitted work included. When a step is accepted, Looper
  commits what the Worker left uncommitted, computes the merge in memory and
  lands one merge commit per step with a fast-forward; a merge that cannot be
  made leaves the task directory untouched and fails the attempt with a
  `merge_error` (`conflict`, `local_changes`, `branch_changed`,
  `step_branch_changed` or `git_failed`) for the Planner to decide on.
  Looper's commits are by `Looper <looper@localhost>` with `Looper-Task` and
  `Looper-Step` trailers, unsigned and without hooks; Looper never pushes,
  switches branches or rewrites commits. The task's start is recorded
  (`task_plans.start_branch`, `start_commit`) and named in the activity, so
  `git reset <start>` undoes the task's commits and `git revert -m 1 <merge>`
  one step. A completed task's worktrees and branches are removed; a failed,
  stopped or cancelled task keeps its unmerged ones until it is started over
  or deleted, and a restart reuses them; the server removes leftovers at
  startup.
- **A task has its own Agent Profile and concurrency per role.** `PUT
  /api/tasks/:id/agents` takes `{"profiles": {"planner" | "worker" |
  "reviewer": id or name | null}, "concurrency": {"worker" | "reviewer": 1–64
  | null}}` (null drops the task's choice), both written in one transaction.
  Any task but a completed one (409) can change them, so a failed, stopped or
  cancelled task's restart runs with them. A task's own limit
  (`task_concurrency_limits`) caps its Workers and Reviewers within its
  project's limit; lowering it stops nothing that runs, raising it starts
  waiting runs at once. A new profile applies to a Worker or Reviewer from its
  next step, and to the Planner at its next turn, after a handoff. `GET
  /api/tasks/:id` adds `agents` (each role's effective `profile` and
  `concurrency` with their sources, `concurrency_max`, `editable`), and the
  task page's Overview has an Agents block with a profile picker and a limit
  per role, replacing the profile line in its header. The Manager's
  `looper_task_update` (`profiles`) and `looper_concurrency` at task scope go
  through the same check. Task choices are not exported.
- **A task's Planner can choose its Workers' Agent Profile and change
  concurrency.** Every Planner `looper serve` starts, on Claude Code, Codex
  and OMP, gets Looper's MCP server with a token that works for its own task
  only and stops working when that Planner stops or the task ends (the MCP
  configuration files that carry it are deleted as it is revoked, before the
  stop waits for the Planner to exit), and a code-owned tools section after
  its reply contract. Its four tools (two more for roles, below) are
  `looper_task_profiles` (read), `looper_task_profile_set`,
  `looper_task_concurrency` and `looper_project_concurrency`; the three that
  write work only in a turn Looper sent the Planner. `looper_task_profile_set`
  picks the Workers' or Reviewers' profile, for the rest of the task or one
  step, from the project's list of profiles the Planner may choose
  (`project_allowed_profiles`), which is empty by default and is filled on the
  project's Agent Profiles tab ("Profiles the Planner may choose") or with
  `GET`/`PUT /api/projects/:id/allowed-profiles` (`{"profile_ids": [...]}`).
  Precedence, highest first: the user's choice for the task, the Planner's
  choice for the step, its choice for the rest of the task, then the settings
  layers; a run reads it when it starts. Taking a profile off the list drops
  what the Planners chose of it, `start_over` drops a task's choices, and a
  listed profile cannot be deleted (`GET /api/registry` names its projects in
  `used_by.allowed_in`). The concurrency tools change the task's own Worker
  and Reviewer limits and the project's `planner`, `worker` and `reviewer`
  limits, at once and without asking. Each change is a `profile_choice` or
  `concurrency_change` journal entry (`task_journal_kinds_planner_tools`) and
  an activity line on the task page, and a concurrency change of a task from
  Telegram or Lark is also told in that chat.
- **A task's Planner can define the roles its steps run as (opt-in per
  project).** A project execution setting, `planner_roles`, is `off` (the
  default, also when null) or `on` (Project → Settings → Execution, "Planner
  roles"), with `role_text_max_chars` (500 to 20,000, null for 8,000) for how
  long a role's prompt and its harness may each be. With it on, the Planner
  gets two more tools, `looper_task_roles` (read) and `looper_task_role_set`
  (`{id, kind, role_prompt, harness?, profile_id?, reason?}`), where `kind` is
  `work` (a step's Worker runs as it) or `review` (it reviews a step after the
  built-in Reviewer); each change adds a version in the new `task_roles` table
  with a `role_defined` journal entry (`task_journal_kinds_task_roles`). A
  role's profile must be on the project's allowed list, and is refused when
  the user chose that kind's profile for the task. Steps take `role`,
  `review_role` (it needs `"review": true`) and `review_role_required` (true
  when left out). A run as a role gets its working style (its harness, else
  the built-in harness's How you work and Communication), its role prompt, the
  project, the task section, then the boundaries and the contract, so nothing
  the Planner wrote follows the boundaries: a code-owned lead line, then the
  shared defaults' new `boundaries` field (Settings → Roles → Shared defaults,
  "Boundaries for Planner-defined roles"; the Manager's `looper_settings`; the
  configuration export; refused on any other layer), else the built-in ones.
  Its profile comes after the user's choice for the task and before the
  Planner's per-step and rest-of-task choices. An attempt keeps the role
  version it started with: a restart reruns it with that version, a retry
  reads the current one. The built-in Reviewer stays the gate: a review role
  runs only after it passes and blocks the step until it passes too, unless
  the step makes it advisory (`"review_role_required": false`), when the
  Planner may accept over it, the `step_accepted` entry says so and the task
  page labels it advisory. Continuing a task keeps its roles; starting over
  deletes them. `GET /api/tasks/:id` adds `roles` (versions, steps and stats:
  runs, cost, accepted and failed steps, retries, contract violations, review
  pass rate) and `builtin_role_stats`; the task page gets a Roles section and
  role labels in Steps and the Pipeline; every Worker and Reviewer run records
  a typed `metadata.outcome` (`completed`, `reported_failed`,
  `files_mismatch`, `passed`, `not_passed`, `contract`, `run_failed`). With
  the switch off, the Planner's tools, its tools guide, its reply contract and
  every Worker's and Reviewer's prompt are as before, and a step naming a role
  is refused with that reason. The prompt eval adds `roles_tester_docs`,
  `roles_security_review`, `roles_advisory_review` and `roles_not_needed`.
- **Projects can run a full task on a time rule.** A schedule belongs to a
  project and has a `name` (at most 80 characters, unique in the project
  ignoring case), a 5-field `cron` rule (minute, hour, day of the month,
  month, day of the week; `*`, numbers, ranges, steps and lists; 0 and 7 are
  Sunday; when both day fields are restricted a day matches either), an IANA
  `timezone` (default: the server's; a time a DST change skips fires at the
  first valid time after it, a repeated one once), the `requirement` of each
  run's task (at most 27,000 characters), a `work_dir` (empty: the project's
  default), `overlap` (`skip`, the default; `queue`, one run waiting for the
  running task; or `parallel`, runs beside each other up to
  `max_parallel_runs`, 1–10, default 3), `catch_up` (`once`, the default:
  fires missed while the server was down or the computer slept run once, told
  how many; or `skip`), an optional `run_timeout_seconds` (up to 30 days; a
  run past it is `timed_out`, a failure, and its task is stopped),
  `run_budget_usd` (the task's own spend cap; without it the project's
  per-task cap applies) and `profile_overrides` for the Planner, Workers and
  Reviewers, by id or name. Two fires must be at least 5 minutes apart over
  the next 24 hours when the rule is saved, or 1 minute for a schedule with a
  gate command (removing the gate of a more frequent schedule is refused). A
  schedule is created disabled; enabling it, or changing the rule or zone of
  an enabled one, sets `next_fire_at` from now, so nothing missed before runs.
  The scheduler in `looper serve` looks every 15 s and once at start, claims
  each due schedule's row (`FOR NO KEY UPDATE SKIP LOCKED`, so two servers on
  one database fire it once), creates the task, titled `<name> · MM-DD HH:MM`
  in the schedule's zone, and starts it at once under the project's
  concurrency limit: enabling the schedule is the approval. The task's
  requirement ends with a code-written `## 定时运行上下文` section: the rule, this
  fire time, how it fired, the fires it stands for, the latest 3 runs, and
  that nobody is there to answer. "Run now" fires once with `trigger`
  `manual`, also while the schedule is disabled, through the same overlap
  check.
- **A schedule can check for new work before each run, hand on unfinished
  input and cap its daily spend.** A `gate_command` (at most 2,000 characters)
  runs before each run with `/bin/sh -c` in the run's work directory, as the
  server's user, with the server's environment plus `LOOPER_SCHEDULE_ID`,
  `LOOPER_SCHEDULE_RUN_ID`, `LOOPER_SCHEDULE_SCHEDULED_FOR` and
  `LOOPER_SCHEDULE_RUN_TIMEOUT_SECONDS` and no stdin, for at most
  `gate_timeout_seconds` (1–600, default 60), after which its process tree is
  ended. Meanwhile the run is `gating` and another firing is
  `skipped_overlap`, so gates never overlap, also under `parallel`. A non-zero
  exit or a timeout is `gate_failed` (a failure, with the tail of stderr);
  printing nothing is `skipped_gate` (no task, no cost); otherwise its stdout,
  kept up to 8,000 characters, is the task's input. The gate output of each
  run that failed, timed out, was stopped or cancelled, or whose gate failed
  goes to the next run once, labelled 「上一轮未完成」, and the composed requirement
  always fits the task limit. With `daily_budget_usd`, a firing once the
  schedule's runs fired since the server's local midnight cost that much is
  `skipped_budget`. A skip that repeats the latest run (same reason, same day)
  is counted on it (`schedule_runs.firings`, shown as `×N`), so a gate firing
  every minute adds a row only when the outcome changes.
- **Schedules report to Telegram or Lark, with two alerts.** `notify` is
  `failures` (the default: a message for each `failed`, `timed_out` or
  `gate_failed` run, and the alerts), `alerts` (only the alerts and their
  recoveries), `always` (every run that ran something, and the alerts) or
  `never` (nothing is sent; the dashboard still shows the alerts); a skip
  never sends one. `notify_target` (`{"channel": "telegram" | "lark", "chat":
  "<id>"}`) must be a person a channel's sender allowlist already lets in;
  null, the default, keeps messages on the dashboard. The failures alert goes
  out when `consecutive_failures` reaches 3; the no-success alert when no run
  was ok for 3 periods of the rule, at least 15 minutes and at most 24 hours,
  counted from the first fire after the latest of the last ok run,
  `enabled_at` and `rule_changed_at`, and not while the schedule is disabled
  or one of its runs is in progress. An ok run is a completed run or a gate
  that found nothing new: it ends the failure streak and recovers both alerts.
  Each alert goes out once per streak, with one recovery, and every message is
  recorded in `schedule_notifications` as `sent`, `failed` (with the reason)
  or `dashboard`.
- **The dashboard manages schedules.** A project's **Schedules** tab
  (`#projects/<id>/schedules`) shows one row per schedule (the rule in words,
  the next run in the schedule's zone, the last run, "Failing" or "No success
  lately", an enable switch and Run now); an open row edits every field,
  previews the next 5 fire times as the rule is typed, and deletes after an
  inline confirmation. A **Schedules** page below Runs (`#schedules`) lists
  every project's schedules, filtered by project and status in the address
  (`#schedules?project=<id>&enabled=true|false`), each with its switch, Run
  now and Delete. A schedule's page (`#schedules/<id>`) shows its settings,
  alerts, today's spend against the daily budget and its run history, 20 runs
  at a time, with the routine skips (nothing new, busy) hidden by default and
  a repeated skip shown as `×N, until <time>`. The Dashboard gets a Schedules
  section (next fires, failing schedules, today's skips) once a schedule
  exists, and the Tasks list a **Scheduled** badge and a
  `#tasks?schedule=<id>` filter.
- **Schedules have an HTTP API, and deleting one is safe mid-run.**
  `GET`/`POST /api/projects/:id/schedules` (create answers 201 with `schedule`
  and `warnings`, such as a parallel schedule allowing more runs than the
  project's Planner limit), `GET /api/schedules` (every project's),
  `GET`/`PUT`/`DELETE /api/schedules/:id`, `POST /api/schedules/:id/run`, `GET
  /api/schedules/:id/runs` (`limit` up to 200, `before`, `skips=hide`;
  `next_before`, `routine_skips`), `GET /api/schedules/:id/notifications` and
  `POST /api/schedules/preview` (`cron`, `timezone`, `count`, `gated`). A bad
  field is a 400 with the reason, and `enabled` on create is refused. `GET
  /api/tasks` takes `schedule_id`; tasks carry `schedule_id`, `schedule_name`
  and `own_budget_usd`, projects `schedule_count`, and `GET /api/overview`
  `schedules`. Deleting a schedule removes its runs and messages, also while a
  run is in progress: its task goes on and ends as any task does, a queued run
  never starts, a gate that ends afterwards starts no task, and its tasks
  stay. A project with schedules is not deleted (409), nor is an Agent Profile
  a schedule chose. The schema adds `schedules`, `schedule_profile_overrides`,
  `schedule_runs`, `schedule_notifications`, `tasks.schedule_id` and
  `tasks.budget_usd` (`schema_migrations` `schedule_runs_status_s2`,
  `schedules_notify_alerts`, `schedules_overlap_parallel`). Schedules are
  created and edited on the dashboard or through the API; the Manager's
  `looper_schedules` lists, reads, enables, disables and deletes them, and the
  configuration export leaves them out.
- **Telegram and Lark conversations are stored and shown on the Chat page.**
  Turns from Telegram direct messages and from Lark (direct messages and group
  @-mentions) go into `chat_messages` under the session `<channel>:<chat_id>`,
  with a new `channel` column (`web` for older rows): the user's message, the
  Manager's reply and replies Claude sends on its own. A Telegram group or
  channel stores nothing. A new Manager session's `[RESUME]` brief includes
  these chats. With the Manager selected, the Chat page lists its
  conversations by channel (Dashboard chat, Lark, Telegram), most recently
  active first; a Lark or Telegram one opens read-only at its newest messages.
  `GET /api/chat/sessions` serves the list (the 50 most recently active,
  Planner threads left out, 500 when it cannot be read).
- **A task asked for in Telegram or Lark reports its end in that chat.** When
  it completes or fails, the chat gets one notice, after the task's progress:
  「✅ 任务完成 · <name>」 or 「❌ 任务失败 · <name>」, the first section of the summary or
  closing note (line breaks kept, at most 300 characters) and an absolute link
  to the task. Lark sends it as Markdown to the requester in a direct message,
  or to the group @-mentioning them; Telegram sends it to the chat as
  MarkdownV2, falling back to plain text. The task records whom to tell
  (`tasks.chat_id`, `chat_kind`, `chat_mention`). A task from the web chat, or
  one that recorded no chat, gets the Manager's final summary as before.
- **Lark answers @-mentions of the bot in group chats.** A group message is
  handled only when its `mentions` hold the bot's own open_id, read from the
  bot's info at start; every other group message is ignored: no reaction, no
  storage, no Manager turn. The sender allowlist still applies per person, and
  a refused sender's record carries the group. The status message and the
  answer reply to the triggering message (`lark-cli im +messages-reply`),
  progress goes to the group, the Manager reads `Channel: a Lark group chat,
  from <name>`, and a task started there tells the group when it ends,
  @-mentioning the requester (existing tasks are backfilled). In a direct
  message only the bot's own mention is stripped now, not the first `@word`.
- **Chat messages link to the dashboard with absolute URLs.**
  `LOOPER_PUBLIC_URL` is the dashboard's address as people open it, such as
  `https://looper.example.com` (a path is kept, a trailing slash dropped);
  unset, links use the server's `http://<host>:<port>`, with `127.0.0.1` for
  one bound to every address. Task end notices and schedule messages use it.
  `looper serve` refuses to start when the value is not an http or https URL
  with a host, or has a user, a query, a fragment or a bad port.
- **Tasks have a title.** `looper_task_create` requires a `title`, a few words
  in the user's language, stored in `tasks.title` (one line, at most 60
  characters). A task's `name` is its title, else its goal id, else its
  requirement's first line, else its id: `GET /api/tasks`, `GET
  /api/tasks/:id`, the overview and the Chat page's targets return it, and the
  dashboard's task lists, the task heading and the end notice show it.
  `tasks.original_user_text` keeps the message the task came from.
- **Claude Code agent teams, on by default for Claude Code profiles.** An
  Agent Profile has `agent_teams`, allowed only on a Claude Code runtime (400
  otherwise); left out, it is on for a Claude Code profile and off for any
  other. The upgrade turns it on once for every Claude Code profile, the
  read-only built-in `claude-code/opus` included (`schema_migrations`
  `agent_profiles_agent_teams_on`); a profile turned off later stays off. With
  it on, the Manager's, Workers' and Reviewers' Claude Code starts with
  `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` and `--teammate-mode in-process`,
  so it can spawn teammates in its own process, whose tokens count in the
  run's cost once a result reports them. A Planner never gets them: its
  decisions come from its own turn. Every other run starts with that variable
  removed, also when `looper serve` inherited it; entering it in a settings
  layer's or a provider's `env` is refused (400), a layer stored before still
  loads, and an import drops it from a layer with a note. Settings → Agent
  Profiles has an "Agent teams" checkbox (Claude Code only, checked on a new
  profile); `looper config export` carries the field, and a file without it
  imports with the runtime's default. A Worker's outcome comes from its last
  result, which with teammates may come after the first. To run without teams,
  untick it on a profile (`PUT /api/agent-profiles/<id>` with `"agent_teams":
  false`) or, instead of the built-in profile, select one that has them off.
- **More prompt-eval tasks, and the Manager's dialogs.** The prompt regression
  eval adds the tasks `many_steps`, `parallel_parts`, `interface_first`,
  `supplement_replan`, `requirement_edit`, `supplement_no_change` and
  `profile_choice` and a "parallel Workers" column, with refreshed haiku
  baselines. `python -m evals.prompt_regression dialogs` (fake and real modes)
  runs ten Manager conversations through a disposable `looper serve` and
  checks what Looper did after each reply.

### Changed

- **Settings is a group of pages, and Agents is now Runs.** The dashboard's
  long Settings page is split into pages listed in a folding Settings group at
  the bottom of the sidebar, each at `#settings/<page>`: **LLM Providers**,
  **Agent Runtimes**, **Agent Profiles**, **Roles**, **Skills & MCP**,
  **Channels** and **Import / Export**. An **Agent Profile** is what the
  dashboard now calls a registered agent (a row of `agent_profiles`, served at
  `/api/agent-profiles`): its list shows each profile's runtime, provider,
  model and effort, the roles it can run and where it is used, and a profile
  in use cannot be deleted. Its form picks a runtime, then a provider grouped
  as the runtime's own login (Claude Code's and Codex's with their login
  state) or an API-key provider that speaks its protocols, then a model from
  the provider's catalog. **Roles** holds the shared defaults every role
  inherits and, per role, its overrides of them and its prompt, max turns and
  timeout, which were on the Agents page. Everywhere an Agent Profile is
  chosen (Roles, a project's Agent Profiles tab, a new task's roles) the same
  dropdown ends with "+ New Agent Profile", which opens the form in place. The
  top-level Agents page becomes **Runs** (`#runs/<role>`), with each role's
  runs only and a link to its settings. `#settings` and `#settings/defaults`
  open Roles, `#agents/...` opens Runs, and the import's links to a provider,
  MCP server or channel open its page.
- **Runtimes are built in.** Settings → Agent Runtimes is read-only: one card
  per runtime type (Claude Code, Codex and OMP, and the planned Claude Code
  SDK, OpenAI Agents SDK and norma) with whether it is found on the server's
  PATH, its path, version, capabilities and the roles it can run, and
  Re-detect, which now also finds a type that was not on PATH when the server
  started. The registry adds each runtime's `path` and `roles` and each
  Agent Profile's `roles` and `used_by`, and refusing to delete a profile in
  use names where it is selected.
- **Storage, API and code use the dashboard's names.** "Agent" now names only
  a running agent (the Manager, a Planner, a step's Worker or Reviewer). The
  schema renames, once and in place (`schema_migrations`
  `profiles_roles_renamed`), `agent_configs` to `role_configs` (keyed by
  `role`; its old `role` column only repeated the key and is dropped),
  `agents` to `agent_profiles`, `task_agent_overrides` to
  `task_profile_overrides`, and the `agent_id` of a settings layer and of a
  task's choice, and a saved session's `session_agent_id`, to `profile_id`;
  foreign keys follow and constraints take a new database's names. A v2.6.0
  database keeps every row, and the renames alone change no role's launch; as
  for any schema change, `looper serve` backs the database up first. The API
  follows with **no old routes or field names kept** (the dashboard is the
  only client, so anything that calls the API directly must change):
  `/api/agent-configs` is `/api/roles` (`/api/roles/:role`,
  `/api/roles/:role/reset-prompt`); `POST /api/agents` and `PUT`/`DELETE
  /api/agents/:id` are `/api/agent-profiles`, answering with `profile` (its
  `profile_id`); the registry lists `profiles` and counts `profile_count`; a
  settings layer's `agent` is `profile_id`, its effective `agent` is
  `profile`, `limits.builtin_agent` is `builtin_profile` and an effort's
  source is `profile`; a task's `agent_overrides` is `profile_overrides`.
  `/api/agents/:id/usage` and `/api/agent-settings` keep their names: they are
  about running agents and their settings. A run's record names its profile in
  `metadata.profile` and `settings_sources.profile`; runs recorded before keep
  `metadata.agent`, which the dashboard still reads. `looper config export`
  writes `looper-config/v2` (`profiles`, and a layer's `profile`);
  `looper-config/v1` and `loopx-config/v1` files still import. In code,
  `AgentSpec` is `AgentProfile`, and `looper.agents.agent_registry`,
  `looper.db.agent_registry` and `looper.db.agent_configs` are
  `profile_registry` and `role_configs`.
- **The dashboard calls a profile an Agent Profile everywhere.** A run's
  prompt layers (the profile row, its source, a profile's default), a task's
  header, the config import preview, a project's Overview cards and a
  provider's model note said "Agent(s)" or "agent" for a profile; they now
  say "Agent Profile(s)".
- **Tasks the Manager creates are ready, not started.** A new task has status
  `init` ("Ready — not started" on the Tasks and task pages) and no Planner.
  Looper adds a configuration block to the reply (title, project and
  directory; each task role's Agent Profile, effort and where they come from,
  agent teams; the review policy, spend cap per task, lessons setting and how
  many tasks the project runs at once) and asks whether to start it. It starts
  when the user tells the Manager, with Start on the Tasks or task page, or
  with the new `POST /api/tasks/<id>/start` (400 unless ready, 404 for an
  unknown task), then waits under the project's limit like any start; a server
  restart neither resumes nor stops it. A task in a project that does not
  exist is created only after the user confirms the new project in the next
  message of that chat.
- **The Manager answers the message it was just sent, and is told where it
  came from.** Code puts a `Channel:` line before each message: `the Looper
  dashboard chat`, `a Lark direct message`, `a Lark group chat, from <name>`,
  `a Telegram direct message` or `a Telegram group chat`. The recommended
  prompt has it answer only that message, without continuing an earlier topic,
  running checks or volunteering a status report unless asked. A Manager role
  prompt still at a previous recommended text moves to the new one at startup;
  an edited one is kept.
- **Restarting a task goes on from its plan; starting over is explicit.**
  Start on the Tasks page and the task page, `POST /api/tasks/start` with
  `{"task_id": ...}` and the Manager's `restart` now continue a stopped,
  failed or cancelled task from its saved plan (`TASK_RESTART` `mode`
  `continue`, the default). Accepted steps stay done with their merged work
  and never run again; every step that did not finish goes back to not
  started, keeping its retries, feedback and worktree (retry budgets are not
  reset); skipped steps stay as history. The Planner gets one `[RESTART]`
  turn, in its saved session when it can resume, and answers in the `[CHAT]`
  shape: `none`, `add_steps` or `replan`. A task stopped before it had a plan
  is planned anew. In v2.6.0 every restart deleted the steps, the plan, the
  Planner's session and the worktrees and planned the task anew; that is now
  an explicit start over: **Start over**, with an inline "Delete its steps?"
  confirmation on the Tasks list and in the task page's header, `POST
  /api/tasks/start` with `"start_over": true`, or the Manager's `start_over`,
  which asks the user first.
- **The task page's Pipeline tab draws the plan's dependency graph.** Each
  step is a node with its key and status, under the steps in its `depends_on`,
  in rows by depth. Dropped (`skipped`) steps stay as dashed, muted history,
  with the decision that dropped them in their tooltip. Every step status has
  its own label, and the Manager node is gone.
- **The Planner plans independent parts in parallel and builds on what is
  done.** Its recommended role prompt makes a step depend only on the steps
  whose results it needs, has parts that must fit together pin their interface
  in a file first and then be built in parallel, and keeps accepted steps on a
  replan, replacing only work that is not done. The plan contract's
  `depends_on` says the same, and the Worker contract tells a Worker that
  commits to stage only its own paths (`git add <paths>`, never `git add -A`
  or `git add .`). A `planner` row on an earlier recommended prompt moves to
  the new one at startup; an edited prompt is kept.
- **The Planner's fixed limits are gone or set per project.** A plan or replan
  may have any number of steps (the limit was 8). A task's Planner sends every
  ready step to the worker pool at once, and the project's Worker limit (16 by
  default) and the task's own limit decide how many run together; before, at
  most 4 ran per task. Retries per step are a project execution setting,
  `max_step_retries` (0 to 20, null for the previous 2; Project → Settings →
  Execution, "Retries per step"), and the reply contract and each decision
  turn say how many are left. A task requirement may have 32,000 characters
  (8,000 before) and reaches the agents whole; a longer one is refused instead
  of clipped, with a 400 at `POST /api/tasks/create`, `PUT
  /api/tasks/:id/config` and `PUT /api/tasks/:id/requirement` and `invalid`
  from the Manager's tools.
- **Concurrency limits are per project, and the machine-wide caps are gone.**
  A layered setting `max_concurrency` (1 to 64) on the role and
  role-in-project layers of the Planner, Workers and Reviewers says how many
  tasks (one Planner each), Workers and Reviewers run at once in one project;
  with no layer setting it, 8, 16 and 8. It is refused (400) on the system and
  project layers and for the Manager. A project at its task limit queues its
  tasks in arrival order without delaying other projects' (new, restarted and
  resumed tasks alike); a Worker or Reviewer past its project's limit waits as
  `pending` and can be stopped while it waits. The limit is read at every
  start: raising one starts what waits at once, lowering one stops nothing.
  The v2.6.0 caps of 3 tasks and 4 Worker and Reviewer runs for the whole
  machine are removed, so on default settings many more agents can run at
  once. The role config's `max_concurrency`, shown but never enforced, is
  dropped (`schema_migrations` `role_configs_max_concurrency_removed`): `PUT
  /api/roles/<role>` with it answers 400, and a file's value is ignored on
  import with a note. Settings → Roles and a project's Agent Profiles tab show
  the value in effect for each role and its layer.
- **The effort is the Agent Profile's.** Settings layers no longer have an
  effort: a run's effort is its resolved profile's (none: the runtime's own),
  and a layer `PUT` with `effort` answers 400. The effective `effort` has the
  source `profile` or `builtin`, and `/api/agent-settings` no longer lists
  `limits.effort_levels`. The upgrade (`schema_migrations`
  `layer_effort_on_profiles`) keeps the profile and effort of every role at
  every place: each layer, and each task's profile override, selects a profile
  with the effort it had, an equal one already registered or a new, ordinary
  profile named `<profile> · <effort>` (for example `claude-code/opus ·
  xhigh`), adding role-in-project layers where a project's roles need
  different ones; it logs each change and drops `agent_setting_layers.effort`.
  Export leaves effort off the layers, and importing an older file whose
  layers carry one converts it the same way, with a note for each change. The
  layer forms lose their Effort field, and every Agent Profile dropdown and
  row summary shows the profile's effort.
- **Settings → Roles and a project's new Agent Profiles tab are lists of
  collapsible rows.** Roles has a row for the shared defaults (the system
  layer, which the dashboard now calls "shared defaults" wherever it names a
  value's source) and one per role, all closed at first; a closed row sums up
  its Agent Profile and where it comes from, its effort, its limit per project
  and "Disabled" or "unsaved", and an open role has "Overrides for this role"
  and "Role prompt and limits". A project's agent settings move from its
  Settings tab to an Agent Profiles tab (`#projects/<id>/profiles`) with rows
  for all roles in the project and for the Planner, Worker and Reviewer.
  Closing a row keeps its unsaved edits, and `#settings/roles/<role>`,
  `#settings/roles/shared` and `#projects/<id>/profiles/<role>` open and
  scroll to a row. The API is unchanged.
- **The Dashboard shows what is happening now.** It reads the new `GET
  /api/overview` every 5 s: task counts by group (In progress, Queued, Ready,
  Needs attention, Completed, and Cancelled or Other when they hold one) over
  every task, each opening `#tasks?group=<group>` (served by `GET
  /api/tasks?group=`); the Manager, busy or idle, and every live Planner,
  Worker and Reviewer, running or waiting, with its task and step; each active
  project's running tasks, Workers and Reviewers against its limits; today's
  spend since the server's local midnight, money at provider prices apart from
  the subscription equivalent; and the unhealthy components. The hard-coded
  "System Status" card and the "Agents 3/3" list are gone. One mapping groups
  the task statuses, and the Planner's `planning`, `verifying` and `stopping`
  now count as active: in a project's `active_count`, in the rule that a task
  must be stopped before it is deleted, and at startup, which resumes such a
  task with a live plan or marks it stopped. Task payloads carry `group`. `GET
  /api/status` keeps its fields, but `tasks` now counts every task (`running`:
  in progress, `pending`: queued).
- **Every runtime sees the repository's `CLAUDE.md` or `AGENTS.md`.** Codex's
  generated `config.toml` sets `project_doc_fallback_filenames =
  ["CLAUDE.md"]`, so a Codex agent reads `CLAUDE.md` in a directory without
  `AGENTS.md`. An OMP agent outside a git repository gets the work directory's
  `AGENTS.md`, else its `CLAUDE.md` (at most 32 KiB), at the start of its
  system prompt. Claude Code already falls back to `AGENTS.md`; its isolation
  now also excludes an `AGENTS.md` above the repository root
  (`personal-config-off/v2`), which reached its agents before. Each run
  records `metadata.project_instructions`, shown as "Instructions" in a run's
  system prompt. The changed Codex config and Claude Code flags restart a
  resident Manager's or Planner's process, with `--resume`, at its first turn
  after the upgrade.
- **The Telegram bot token is entered in Settings → Channels.** The Telegram
  card has a password **Bot token** field. The token is kept in the new
  `channel_secrets` table and never returned: the API shows `has_bot_token`,
  and `PUT /api/channel-configs/channel-telegram` takes `bot_token` (`***`
  keeps it, `""` or `null` removes it). Without a saved token, the variable
  named by `bot_token_env` (default `LOOPER_TELEGRAM_BOT_TOKEN`) is read as
  before. The bot publishes its command menu (`/status`, `/new`, `/help`) at
  start. `looper config export` leaves the token out and the import lists it
  as a secret to fill in.
- **Lark is the "Lark (lark-cli)" channel: a lark-cli profile chooses the
  bot.** The optional `profile` replaces `app_id_env` and `app_secret_env` and
  is passed as `--profile` on every lark-cli call; empty means lark-cli's
  active profile, and `LARK_APP_ID` is no longer read. At startup `looper
  serve` runs `lark-cli whoami` and reads the bot's info; the channel does not
  start when lark-cli is missing, the profile is unknown, lark-cli is not
  signed in, the app has no bot, or the sender allowlist was made for another
  app (open_ids differ per app, so the server binds the list to its app,
  `allowlist_app_id`). Settings → Channels shows the bot or why it does not
  start, the senders' names, a search by name or email that adds a sender
  (`GET /api/channels/lark/people?q=`, run as the signed-in lark-cli user),
  and the 20 most recently refused senders with Allow (`GET
  /api/channels/lark/status`; the new `channel_rejected_senders` table keeps
  who, how often, when and in which group, never what they wrote). Replies are
  Markdown posts. On upgrade, `app_id_env` and `app_secret_env` are dropped
  from the row at startup (one log line) and from an imported file, a row
  without `profile` gets an empty one, so Settings → Channels shows the field,
  and a row still named "Lark (飞书) Bot" is renamed.
- **Telegram and Lark show one status message per message, and react when the
  bot sees it.** A message from a listed sender that goes to the Manager first
  gets the bot's reaction, in the background: Lark's 「Get」 (the app needs
  `im:message.reactions:write_only`) or Telegram's 👀 in a private chat. The
  Manager's phases no longer arrive as a message each: the first sends 「⏳
  处理中：思考中 · 0s」, later ones edit it in place (only when its text changed, at
  most every 3 s, at most 18 times), and after the answer, sent as a new
  message, it reads 「✅ 完成 · 用时 Ns · 工具 N 次」 or 「❌ 出错：<reason>」. A failed send,
  edit or reaction is logged once and never becomes more messages. The
  Manager's task progress still comes as messages of its own.
- **Chat input and Markdown.** The message box grows with its lines: Enter
  sends, Shift+Enter adds a line (it used to send), and an input method's
  Enter only confirms its candidate. Long lines, paths and code blocks wrap
  inside a bubble. Agents' replies in the chat and on an agent's history page
  render tables, task lists, lists and headings (GitHub-flavoured Markdown),
  and a wide table scrolls inside its message.
- **Creating a task or a schedule takes an Agent Profile by id or name.**
  `profile_overrides` on `POST /api/tasks/create` and on the schedule
  endpoints goes through the same check as `PUT /api/tasks/:id/agents`; a name
  is matched without regard to case, and errors read
  `profile_overrides.<role>: ...`.
- **Commits no longer need a sign-off.** `CONTRIBUTING.md`, `AGENTS.md` and
  the pull request template no longer ask for `git commit -s` (the Developer
  Certificate of Origin).

### Fixed

- **Stopping an agent ends every process it started.** Each agent process (a
  Worker or Reviewer run, the Manager's and each Planner's resident Claude
  Code, each Codex or OMP turn) starts in a session of its own. Every stop (a
  task stop, a Planner's kill, `timeout_seconds`, the per-run cap, a cancelled
  turn, the server's shutdown) takes the agent's whole tree from one `ps`
  snapshot, including the commands Codex and Claude Code run in sessions of
  their own, sends it SIGTERM and, after 3 s, SIGKILL to what is left. Before,
  only the agent's own process got SIGKILL, and its commands kept running. A
  stopped agent now usually exits with `-SIGTERM`, and a Ctrl-C in the
  terminal reaches only the server, which ends every agent as it shuts down.
  Each running agent is recorded under `<runtime
  root>/.local/agent-processes/`; after the server crashed or was killed, the
  next start, before resuming tasks, ends the tree of each recorded agent
  still running.
- **A crashed resident agent is restarted for as long as the server runs.**
  The supervisor gave up on the Manager, the TaskRouter or the WorkerPool
  after 3 restarts. Each restart in a row now waits twice as long as the last,
  from 1 s up to 5 minutes, back to 1 s once the agent has stayed up 5
  minutes; a failed start is retried the same way, and after 3 restarts in a
  row the agent is `crash_looping` until it is stable again. `/api/status`
  gives each agent `state` (`running`, `restarting` or `crash_looping`), `up`,
  `restart_count` (in a row), `last_crash_at` and `next_restart_at`, no longer
  `max_restarts`, and `healthy` is false while it crash-loops. The Dashboard
  says when one restarts or keeps crashing, and the Manager is told `crashed`,
  `crash_looping` and `recovered` instead of `max_restarts_exceeded`.
- **Every task is reachable from the Tasks page.** `GET /api/tasks` returned
  the newest 50 tasks only. It now takes `limit` (50 by default, at most 200),
  `before` (a task id: the tasks created before it) and a `status` filter
  beside `project_id`, `group` and `schedule_id`, and adds `next_before` (null
  at the end) and `counts` (the tasks of each status under the filters); a bad
  `limit`, an unknown `status` or an unknown `before` is a 400. The Tasks page
  loads 50 at a time with Load more and "Showing N of M", and its status chips
  filter on the server and count every task.
- **A chat answer is kept when you leave the Chat page.** The page saved an
  answer when its stream ended, so one that came after the user left the page
  never reached the history. The web channel now saves each answer under
  `<message id>:reply` when it arrives and pushes it to the chat's open pages,
  once per message.
- **Long chat histories open on their newest messages.** `GET
  /api/chat/messages` returns a session's newest 200 messages, oldest first;
  it returned the oldest 200, so a long thread stopped showing new ones.
- **The Manager's reply is the turn's final answer.** What Claude wrote before
  its tool calls ("checking the logs now…") was joined to the front of the
  reply, the stored chat message and the run's output. The reply is now the
  result's `result`, also for replies Claude sends on its own and for the
  handoff; the narration still streams, and a turn that ended without final
  text keeps all of it.
- **The Lark channel receives and answers with lark-cli 1.0.97.** It ran
  commands lark-cli no longer has, so every reply failed and the event
  consumer restarted in a tight loop. It now receives with `lark-cli event
  consume im.message.receive_v1 --as bot` (bots' messages ignored) and keeps
  the consumer's stdin open, since lark-cli stops at stdin EOF, which a
  background service gave at once; it starts the consumer again 10 s after it
  stops. It sends with `lark-cli im +messages-send --as bot`, and a failed
  call carries lark-cli's reason, with the scopes lark-cli reports missing.
- **The Telegram bot token no longer reaches the log.** httpx logged every Bot
  API URL, token included, at INFO. Without `looper serve --verbose`, `httpx`
  and `httpcore` now log only warnings and errors, and every line `looper
  serve` and `looper mcp-server` log, tracebacks included, is masked:
  `bot<id>:<secret>` becomes `bot***` and the configured token `***`; a failed
  send's error, kept with a schedule's notifications, is masked too. If you
  ran the Telegram channel before, treat the token as exposed: revoke it in
  BotFather, save the new one, restart and delete the old `serve.log` under
  the runtime root's `logs/`.
- **A step is no longer failed when its Worker's reported files are in another
  worktree or a commit.** When the files are not where the report says, the
  check now also looks at the same path in the repository's other worktrees
  and at commits made since the run started (less 60 s of clock slack) on any
  branch, tag or remote-tracking branch, read-only and bounded (at most 64
  worktrees, the latest 500 commits, 10 s per git command). Before, the step
  failed with "changed files not found" and used a retry. Such files are
  listed in the result's `found_elsewhere` and named in the activity; a
  failure in a git work directory now says where the check looked, and outside
  git nothing changes.
- **A restarted task no longer ends at once with nothing running.** A stop
  during a Planner turn left its cancel queued, and the task's next Planner
  read it first. A new Planner now drops whatever an earlier one left unread.
- **Agent Profiles on Claude Code's own login show their prices.** A profile
  that names an alias (`opus`, `sonnet`, `haiku`) is priced as the model it
  runs (`CLAUDE_CODE_ALIASES`); recorded run costs do not change.
- **Smaller fixes.** The task Overview's Steps tile counts only accepted steps
  as done, then those in review and those skipped, and step badges read `In
  review` and `Awaiting decision`; at narrow widths the Agent Timeline's
  History button no longer covers a row's counts; the `[RESTART]` turn no
  longer doubles a period; Settings → Roles drops the Replace / Append toggle
  next to the role prompt, which was only an editing mode, and says how the
  role prompt is combined into the agent's system prompt.

### Removed

- **The runtime registration API.** `POST /api/runtimes` and `PUT`/`DELETE
  /api/runtimes/<id>` are gone (404); `POST /api/runtimes/<id>/probe` stays.
  A different binary comes from the server's PATH. On upgrade, runtimes
  registered by hand that no agent uses are dropped once; one an agent uses
  stays, read-only, shown as "custom (legacy)", so its agents keep running the
  executable they ran. A configuration file's runtimes are no longer added on
  import: an agent of the file runs on the runtime of that name here, else on
  the built-in runtime of its type (the preview says so), else it is skipped
  with the reason. `--register-agent` in the prompt eval uses the built-in
  runtime of its type.
- **The Manager's JSON actions.** It no longer reads `task_request` or
  `task_start` JSON from its replies, nor the `"new_project": true`
  confirmation: it acts only through its tools, and JSON it writes anyway is
  sent to the user as text. A Manager role prompt still at a recommended text
  is migrated at startup; an edited prompt that asks for task JSON creates
  nothing until it is changed (Settings → Roles).

## [2.6.0] - 2026-10-14

Covers `v2.5.0` to `v2.6.0`: pull requests #2 and #4 to #70 in
[foolzzz/looper](https://github.com/foolzzz/looper/pulls).

### Added

- **Claude Code with API keys and Anthropic-compatible providers.** A Claude
  Code agent can now run with an API-key provider: Anthropic's own API (the
  key as `ANTHROPIC_API_KEY`) or any Anthropic-format endpoint such as GLM,
  DeepSeek, Kimi or a local CLIProxyAPI (`ANTHROPIC_BASE_URL` and
  `ANTHROPIC_AUTH_TOKEN`, with every Claude model alias Claude Code uses on
  its own mapped to the provider's models: the opus and sonnet aliases and
  subagents to the agent's model, the haiku and small/fast model to the
  catalog's new `background_model`). A provider can carry its own environment
  variables (`env`). Presets for GLM (BigModel, Z.ai), DeepSeek, Kimi
  (Moonshot China, international) and CLIProxyAPI fill a new provider from the
  vendor's documented endpoints, models and Claude Code variables, so the user
  only adds the key. A built-in price table (Claude, GPT, GLM, DeepSeek, Kimi;
  USD list prices of 2026-10-14) prices every model the catalog leaves
  unpriced. Runs on such a provider are priced by Looper from each model's
  tokens, cache reads and writes included (`cost_source: provider_price`),
  and their per-run cap is Looper's own soft cap at those prices instead of
  `--max-budget-usd`; an agent whose model has no price is registered with a
  warning that its runs record tokens only and no spend cap applies. The
  dashboard has a preset picker, the background model and extra environment
  in the provider form, built-in prices beside the catalog's, each agent's
  cost basis, and marks subscription costs as "equivalent". The prompt eval
  can run with a provider preset (`--provider-preset`, its key from
  `LOOPER_EVAL_API_KEY`).
- **A resident session resumes only with the same runtime type and provider.**
  `agent_sessions` records the session's `provider_id`; any change of provider
  (subscription to API key, back, or between API-key providers) starts a new
  session through the handoff and resume brief, while a change of model alone
  still resumes.
- **Progress and experience captured as they happen.** The task journal gains
  `step_failed` and `step_retry` (what went wrong, why, and the Worker's and
  Reviewer's learnings), `decision` (a replan, steps added from chat, a stop
  at the spend cap, with the reason), `user_instruction` (each chat message to
  the Planner, whole), `session_switch` and `handoff`. The Planner's turns
  carry the user's instructions first, whole and in a budget of their own,
  then the newest entry of each kind, the other pitfalls and the rest within
  the 4,000-character cap, under one heading per kind; a Worker's first turn
  gets the instructions and the pitfalls too. When the Planner's or the
  Manager's agent can no longer resume its session, the old session, while
  its Claude runs, writes a handoff first, and every new session's first turn
  starts with a `[RESUME]` brief (why, where the task is, the handoff; the
  Manager's adds the recent chats from `chat_messages`). A Worker's or
  Reviewer's learning can say it holds for the whole project
  (`{"text": ..., "scope": "project"}`; plain strings still work): with
  `distill_lessons` `draft` or `write` it becomes a knowledge draft when it
  arrives. The task page's Findings list shows each entry's kind. The
  journal's kind check is extended once (`task_journal_kinds_m7c`).
- **Runtimes, LLM providers and agents.** An agent is a runtime, an LLM
  provider and a model, with a default effort. Settings registers runtimes
  (a runtime type's executable, probed with `--version` when registered and
  on request), providers (protocols, `base_url`, a model catalog with context
  windows, prices and effort levels, `max_concurrency`, and a runtime type's
  own login or an API key, stored apart and shown as `***`) and agents
  (`GET /api/registry`, `/api/runtimes`, `/api/providers`, `/api/agents`).
  The built-in `claude-code` runtime, `claude-subscription` provider and
  `claude-code/opus` agent are what every role ran before. An agent is
  refused when its runtime and provider share no protocol, an OAuth provider
  belongs to another runtime type, the runtime cannot take that kind of
  provider yet (Claude Code takes its own login only), or the provider does
  not list its model. Every tier now starts its runtime through a runtime
  adapter (`looper/agents/runtimes`); Claude Code is the first, its launch
  byte-identical to before.
- **The Codex runtime.** Agents can run on Codex (`codex`, verified against
  Codex CLI 0.162.0) with an API key provider that serves
  `openai-responses`, such as the `cpa` preset (a local CLIProxyAPI); Codex's
  own login is not used. Every role can run on it: a Worker or Reviewer is one
  `codex exec --json` run, and the Manager and a Planner run one process
  per turn that resumes their session. Each run gets a `CODEX_HOME` of its
  own under `<runtime root>/.local/codex` holding only the `config.toml` and
  `AGENTS.md` Looper writes, so the operator's `~/.codex` and
  `~/.agents/skills` stay out; the system prompt goes in `AGENTS.md` after a
  short block of Codex runtime notes, and reaches a resumed session when it
  changes. MCP servers become `[mcp_servers.<name>]` (stdio and HTTP; SSE
  servers are left out), skills are mounted read-only and listed for the
  agent to read, and a plugin's hooks, commands and agents are left out;
  what was left out is in the run's record. Codex reports tokens only, at
  the end of a turn: Looper prices them at the provider's prices, so the
  per-run soft cap can stop a Codex run only once a turn ends. A Codex Worker cannot be steered while it runs: the
  Planner's message goes to the step's next decision instead. `looper
  serve` registers the built-in `codex` runtime when `codex` is on its
  PATH. The prompt eval takes `--register-agent SPEC.json` to run every
  role on an agent it registers, and reports each task's tokens.
- **The OMP runtime.** Agents can run on OMP (oh-my-pi's `omp`, verified
  against omp 18.4.3) with an API key provider that serves
  `openai-responses`, `openai-chat` or `anthropic-messages`, such as the
  `cpa` preset; omp's own logins are not used. Every role can run on it: a
  Worker or Reviewer is one `omp -p --mode json` run, and the Manager and a
  Planner run one process per turn that resumes their session from a
  directory of their own. Every omp process runs with `HOME` set to a home of
  Looper's under `<runtime root>/.local/omp/homes`, holding a generated
  `models.yml` (the provider, its key named by an environment variable) and
  a `--config` overlay (only the agent's model, no approvals, no background
  jobs, no update checks), so the operator's `~/.omp`, `~/.claude`,
  `~/.agents`, `~/.env` and similar stay out; the agent's shell commands get
  the operator's `HOME` back, without the provider's key. Outside a git
  repository the discovery sources that would climb past the work directory
  are off. The system prompt is appended with `--append-system-prompt`,
  after a short block of OMP runtime notes. The installed plugins load
  natively (skills, commands, agents); their hooks and their own MCP servers
  do not, and Looper's MCP servers come in a plugin of the home; knowledge is
  read by the index's paths. omp reports each request's tokens, so runs
  record their context size and the per-run soft cap can stop a run between
  requests; a subagent's tokens come with its tool result. A Worker cannot be
  steered while it runs. `looper serve` registers the built-in `omp`
  runtime when `omp` is on its PATH, and removes, when it starts, the OMP
  homes no running process uses and the session directories (OMP's and
  Codex's) of agents whose saved session is no longer of that runtime.
- **Choosing the agent per layer and per task.** The layered settings select
  an agent in place of a model, in Settings, on the Agents page and in a
  project's Settings tab, and a new task can override the agent of its
  Planner, Workers or Reviewers (`agent_overrides`, in the API and the New
  Task dialog), for that task only. A selection is refused when the agent's
  runtime lacks what a role needs (a resident session and resume for the
  Manager and Planner); a run whose agent can no longer run fails with the
  reason rather than run another agent. Runs record their agent and
  `cost_source`; a resident session records its runtime type, so another
  runtime starts a new session. Config export and import carry runtimes,
  providers (keys as placeholders) and agents. The prompt eval takes
  `--agent`.
- **Run as a service.** `looper service install` runs `looper serve` in the
  background as a launchd LaunchAgent (macOS) or a systemd user unit (Linux):
  started at login, started again after a crash, logging to
  `<runtime root>/logs/serve.log`. The unit runs the `looper` that ran
  `install`, in the current directory, with `PATH`, locale and proxy settings,
  `CLAUDE_CONFIG_DIR` and the `LOOPER_*`, `ANTHROPIC_*` and `LARK_*` variables
  copied from the shell (more with `--env NAME`), so it is written with mode
  0600. Running `install` again updates the unit and restarts the service; it
  is not started when another process holds the port. `status` shows whether
  it is installed and running, its PID, last exit, log and `GET /api/status`;
  `restart` stops and starts it and waits until the port serves (on macOS with
  `launchctl bootout` and `bootstrap`, not `kickstart -k`); `uninstall` stops
  it and removes the unit. `looper doctor` adds a Service line.
- **Backup before a schema change.** `looper serve` records the sha256 of the
  `schema.sql` it applied (`schema_fingerprint`, in the same transaction).
  When a database that already holds Looper tables has a different or no
  fingerprint, serve first writes a `pg_dump --format=custom` backup (mode
  0600) to `<runtime root>/backups/`, checks it (`pg_restore --list` when a
  `pg_restore` is at hand), keeps the newest 5 per database and logs the
  restore command, and only then applies the schema; a new, empty database
  needs none. The dump comes from `LOOPER_PG_DUMP` or `pg_dump` on PATH, of at
  least the server's major version, or from `LOOPER_BACKUP_CMD`, a command whose
  stdout is the dump (`{dbname}` and `{user}` are filled in), such as
  `docker exec <container> pg_dump --format=custom -U {user} {dbname}`.
  `looper doctor` reports the `pg_dump`, failing when a schema change is
  pending without one. **Behavior change:** without a usable `pg_dump`, or
  when the backup fails, `looper serve` exits before changing the schema;
  `--skip-backup` migrates without a backup and logs a warning.
  `looper config import --apply` no longer migrates such a database: it asks
  to start `looper serve` once first.
- **Configuration export and import.** `looper config export <file>` writes an
  installation's agent configuration to one JSON file (`looper-config/v1`):
  the system and role settings layers and role prompts, the catalog with its
  files and MCP servers, global installs and channel configs, and with
  `--include-projects` each project's fields, execution settings, layers,
  knowledge, own skills and installs. MCP environment and header values and
  channel secrets are placeholders unless `--include-secrets` (which warns);
  a project's directory and an item's local source path are left out, and
  path documents are references to re-point. `looper config import <file>`
  previews what it would add, change, leave and skip, and the secrets left
  to fill in; `--apply` writes it in one transaction. Entries match by name,
  an existing one is updated in place (an item keeps its id and installs),
  nothing missing from the file is removed, a placeholder never replaces a
  stored secret, and importing the same file again changes nothing. Both
  use the database directly, so a catalog over the server's 8 MiB request cap
  moves too. Settings has an Export download (streamed,
  `GET /api/config/export?projects=1&secrets=1`) and an Import (below).
- **Importing a configuration file from Settings.** "Move to another machine"
  takes a chosen or dropped file, shows the import's preview per section
  (added, changed with the fields, unchanged, skipped) with the secrets to
  fill in, and applies it after a confirmation; the result links to the
  provider, MCP server or channel form that takes each secret
  (`#settings/provider|mcp|channel/<id>`). `POST /api/config/imports` is the
  only route that takes more than the 8 MiB request cap: it streams up to
  256 MiB to a 0600 file under `<runtime root>/config-imports/` and answers
  with an import id and the preview; `POST /api/config/imports/<id>/apply`
  writes it in one transaction like `looper config import --apply` and deletes
  the file, `DELETE /api/config/imports/<id>` cancels, and an import not
  applied expires after an hour (404 afterwards, as after a restart). At most
  4 imports wait at once. `looper config import` remains the way for larger
  files.
- **MCP servers in the capability catalog.** An MCP server is a catalog item
  of kind `mcp` (stdio: command, arguments, environment; or http/sse: URL,
  headers), installed like a plugin or a skill: globally or in one project,
  for some roles. Add, edit and delete them in Settings or with
  `POST /api/catalog/mcp`, `PUT` and `DELETE /api/catalog/<id>`, or import the
  user-level `mcpServers` of this machine's `~/.claude.json` read-only
  (`POST /api/catalog/mcp/import`; new ones are installed nowhere).
  Environment and header values are stored in the database and shown only
  as `***`; sending `***` back keeps a value. Each run mounts the servers
  installed for its role as one read-only `--mcp-config` file under
  `<runtime_root>/.local/mcp/`, beside `--strict-mcp-config`, and records
  their names. A plugin's own MCP servers still do not load.

- **More ways into the capability catalog: upload, git and Settings.**
  Settings' catalog can upload (or take a drop of) a `SKILL.md` or a zip of a
  skill or plugin directory, import plugins and skills from a public
  `https://` git repository after a preview with checkboxes, and write and
  edit skills (`POST /api/catalog/upload`, `/api/catalog/git/preview`,
  `/api/catalog/git/import`, `/api/catalog/skills`, `PUT /api/catalog/<id>`).
  Nothing they add is installed. Zips are refused for absolute paths, `..`,
  symbolic links and over-cap files; clones are shallow, run no hooks or
  submodules, ignore the operator's git configuration and credentials, and
  are size- and time-capped and removed. Re-importing from the same URL
  updates those items in place; editing an uploaded or git skill marks it
  modified; machine items stay re-import-only. A name already used outside
  projects is refused (409) and can be renamed.

### Changed

- **A Manager message that reaches its per-run spend cap is answered with the
  cap's error**, and the next message starts a new process that resumes the
  session (as a Planner turn already did). Before, the text written before the
  cap went out as the reply and the process kept answering every later
  message with the cap. Registering or changing an agent now also answers with
  `warnings`, and each agent in `GET /api/registry` carries its `cost` basis.
- **Renamed to Looper.** The command is `looper`, the Python package and
  distribution are `looper` (`looper-<version>-py3-none-any.whl`), the
  dashboard, prompts and docs say Looper, and `looper version --format json`
  reports `name: looper` and `schema_version: looper_version_v0`. The upgrade
  path, for one release:
  - `loopx` stays as an alias that prints a one-line deprecation note to
    stderr and runs `looper`; `install.sh` removes an old `loopx` package
    installed by the same tool first.
  - The runtime root is `~/.looper`: the first `looper serve` or
    `looper service install` that uses it renames `~/.loopx` to it (nothing
    is copied or deleted) and leaves `~/.loopx` as a link; when both exist,
    `~/.looper` is used with a warning.
  - Environment variables are `LOOPER_*`; an unset one falls back to its
    `LOOPX_*` name with a warning, and a stored `LOOPX_*` name (a channel's
    `bot_token_env`) finds the renamed variable. `service install` copies
    both prefixes.
  - The service is `com.looper.serve`; `looper service install` first stops
    and removes `com.loopx.serve`.
  - Export writes `looper-config/v1` (with `looper_version`); import still
    reads `loopx-config/v1`. Backups are named `looper-<database>-*.dump`;
    older `loopx-*` backups are kept and not pruned.

  Unchanged: the default database name `loopx`, the data in the database
  (a system harness stored as the old built-in text still follows the
  built-in), release tags and history, and NOTICE's attribution to upstream
  LoopX. The prompts' "LoopX" becomes "Looper", and `schema.sql`'s comments
  change, so the first start takes the pre-migration backup.
- **A layer's model is now its agent.** The upgrade turns each model a layer
  held into the agent `claude-code/<model>` (Claude Code with its own login),
  so every role keeps its model; the settings API takes `agent` (an agent id)
  and refuses `model`, and run records' `settings_sources` name `agent`.
  Without configuration nothing changes: every role's command, environment
  and system prompt are byte-identical to before.
- **Agent settings resolve by layer.** The harness prompt, model, effort,
  environment variables and per-run cap (`--max-budget-usd`) of every agent
  now come from one store (`agent_setting_layers`) and resolve through
  system → role → project → role in a project, a later layer winning; the
  Manager has the first two only. The environment merges key by key, and a
  later layer can unset a variable. A Reviewer without a harness of its own
  at the role or role-in-project layer still takes the Workers'. Every run
  records each setting's layer in `metadata.settings_sources`;
  `prompt_layers.harness.source` and `model_choice.source` now name the layer
  (`builtin`, `system`, `role`, `project`, `role_project`). Edit the system
  layer in Settings, a role's on the Agents page, and the project's and each
  role's in the project on the project's Settings tab; every field shows
  what applies now and where it comes from. **No behaviour change for
  existing data:** the first start moves the Settings defaults, each agent's
  model and the Manager's harness, each project's model and per-run cap, and
  each project's Planner and Worker harness into their layers unchanged
  (recorded as `agent_setting_layers`), so every agent launches with the same
  flags, environment and system prompt as before; the old columns and
  `system_settings` keys are dropped (a rollback needs a database backup).
  API: `GET /api/agent-settings`, `PUT /api/agent-settings/system`,
  `PUT /api/agent-settings/roles/<role>`, `GET`/`PUT
  /api/projects/<id>/agent-settings` and `PUT
  /api/projects/<id>/agent-settings/roles/<role>` replace
  `/api/settings/agents` and its `reset-harness`; agent configs no longer
  take `harness_prompt` or `model`, and projects no longer take or return
  `planner_harness_prompt`, `worker_harness_prompt`, `model` or
  `run_budget_usd` (400 naming the new endpoint). A run stopped at its cap now
  says "its per-run spend cap", since the cap can come from any layer.
- **Behaviour change: plugins and skills load only where they are
  installed.** The library is now a capability catalog. Importing from this
  machine (`POST /api/catalog/import`) only adds items to the catalog; an
  item loads once it is installed, globally or in one project, for some of
  the roles `manager`, `planner`, `worker` and `reviewer` (a project install
  never names the Manager). Every run loads the enabled installs that name its
  role, globally or in its project. **The plugins and skills imported before
  this version stay in the catalog, installed nowhere, so after the upgrade no
  agent loads them until you install them** (Settings → "Plugins & skills catalog").
  The item-level enabled switch is gone; an install has its own.
- **Project skills are catalog items.** A skill written in a project is a
  catalog item owned by that project (origin `project`), installed in it for
  the Planner, Workers and Reviewers, and can be installed in other projects
  too. The first start moves every `project_skills` row into the catalog with
  the same id, project, name, description and enabled state; agents still
  call it `project:<name>`. The `project_skills` table is dropped (recorded
  as `library_catalog_installs` in `schema_migrations`); a rollback needs a
  database backup from before the upgrade.

- **Plan state and agent sessions live in PostgreSQL.** The Planner no longer
  writes its plan to `.local/planner/planner-<task_id>/<plan_id>.json`, and the
  Manager no longer writes `.local/manager/session.json`. A task's plan is in
  `task_plans` and its steps, with everything the Planner decides with
  (label, dependencies, review flag, retries, feedback, late user messages),
  in `task_steps`; each change writes only the rows it touches, and a change
  to several rows is one transaction. Each resident agent's Claude session
  and its running totals are in `agent_sessions`. The dashboard and the
  Planner now read the same rows.
- **Tasks carry on after a server restart.** A task the previous server left
  running with a live plan is resumed at startup instead of being marked
  stopped: its new Planner reads the plan back and resumes its Claude session
  without planning again; a step whose Worker was running runs again (the same
  attempt, its feedback kept), a step in review gets a new Reviewer, and a
  decision the stop cut off is taken again. Other tasks left running are still
  marked stopped.
- **One-time import of the old files.** The first start imports the plan
  files in the resident Planner's format and the Manager's `session.json`
  (rows already in the database win; recorded as `file_state_import`); plan
  files from before the resident Planner are skipped with a log line. The
  files are left on disk and can be removed.

### Added

- **Capability catalog and installs.** `GET /api/catalog` lists every item
  (kind, origin, owning project, where it is installed), `GET
  /api/catalog/<id>` one item with its files; `POST /api/installs`, `PUT` and
  `DELETE /api/installs/<id>` install, change the roles or enabled flag of,
  and uninstall an item; `GET /api/projects/<id>/installs` shows what applies
  to a project: its own installs and the inherited global ones. Settings shows
  the catalog with a global install (role checkboxes) and uninstall per item;
  the project page's Skills tab shows what is installed there, installs from
  the catalog and writes or edits the project's own skills. They replace
  `/api/settings/library*` and `GET /api/projects/<id>/skills`; each run's
  `metadata.library` now records its role, project and each item's plugin and
  scope, and `metadata.project_skills` is no longer written.

- **`looper doctor` and the startup self-check.** `looper doctor` checks
  PostgreSQL (`LOOPER_DATABASE_URL`, shown with its password masked), the
  Claude Code CLI (`claude --version`), its login (`claude auth status`,
  which reads local credentials and makes no model call; a CLI without it
  is reported as not checked) and, when the Lark channel is enabled,
  `lark-cli`. It prints one line per check with its fix and exits 1 when a
  check failed. `looper serve` runs the same checks first: an unreachable
  PostgreSQL stops it with the fix instead of a traceback; a missing
  `claude` or login is logged as a warning, the server still starts,
  `GET /api/status` reports every check under `checks`, and the dashboard
  shows a banner on every page.

- **Task memory.** Code writes a `task_journal` entry when a step is
  accepted: the Worker's summary and the new optional `learnings` (up to 5
  short findings or pitfalls) of the step's Worker report and Reviewer
  verdict. The Planner's DECISION, SUPERVISE and CHAT turns carry a
  "Progress and findings" section (newest first, at most 4,000 characters)
  and the summaries of accepted steps; a Worker's first turn carries
  "Findings so far in this task" (its dependencies first, at most 2,000
  characters). The task page lists the entries under Findings.
- **Lessons in project knowledge, as drafts or written.** A project's
  `distill_lessons` (`off` by default, `draft` or `write`; Settings tab, or
  `PUT /api/projects/<id>`) gives the Planner of a task that completes one
  more turn, whose Markdown summary of the journal becomes the text document
  `Lessons: <requirement>`. `write` saves it to the project's knowledge;
  `draft` keeps it as a draft that no agent sees until the user confirms it,
  optionally edited, or discards it on the project's Knowledge tab
  (`GET /api/projects/<id>/knowledge/drafts`,
  `POST .../drafts/<draft_id>/confirm`, `DELETE .../drafts/<draft_id>`); the
  task page links to its draft. A project that had the earlier boolean
  switch on is migrated to `write`, off to `off`.
- **Task spend against its cap, and continuing at a higher cap.** When the
  task's project sets a spend cap per task (`task_budget_usd`), the task page
  shows the task's spend as a share of it and warns from 80% on. A task that
  failed at its cap (recorded as `tasks.fail_kind = 'task_budget'`) can be
  continued: `POST /api/tasks/<id>/continue` with a new `task_budget_usd`
  above the task's spend raises the project's cap and restarts the task from
  its saved plan and steps, its Planner resuming its Claude session instead of
  planning again; anything else gets a 400. The task page offers this as
  "Raise cap and continue".
- **Project page.** Clicking a project in the sidebar opens `#projects/<id>`
  with Overview, Knowledge, Skills and Settings tabs; it replaces the project
  editor on the Tasks page, which now links to it. Overview shows the latest
  tasks, the background, knowledge and skill summaries and the execution
  settings that are set. Knowledge documents and project skills can be opened
  to read their body and edited in place; a dropped file adds a document, and
  an uploaded or dropped `SKILL.md` fills a new skill from its frontmatter and
  body. `GET /api/projects/<id>/knowledge/<doc_id>` and
  `GET /api/projects/<id>/skills/<skill_id>` return one item with its body.
  The history page's Plugins & skills lists the project skills a run loaded,
  and the Agents page no longer offers the removed `permission_mode` (the
  codex-cli Sandbox dropdown).

- **Reclaiming read-only copies.** `looper serve` now removes, when it starts
  and before any agent launches, the content-hashed copies under the runtime
  root (library items, project knowledge sets, project skills plugins) that
  the current content no longer uses and no running process's command line
  mounts or links into (`looper/agents/read_only_copies.py`). They used to stay
  on disk for good after their content was replaced or deleted. When the
  content or the running processes cannot be read, nothing is removed.

- **Request body cap.** The HTTP server reads a request body of at most 8 MiB,
  room for the largest legitimate one (a 2 MiB knowledge document uploaded as
  base64 in JSON). A larger `Content-Length` gets a 413 with
  `{"error": ...}` naming the cap, before any of the body is read; a
  malformed one gets a 400 instead of a 500.

- **Project execution settings (M5).** A project can set how its Planner,
  Workers and Reviewers run; every field is optional and `null` inherits, so
  a project with none set runs as before (`looper/agents/project_execution.py`).
  The fields are typed columns on `projects`, read and written through the
  project `GET`/`PUT` (a 400 names a bad value) and the project editor:
  - `model`, over the agent's own model and the default (not the Manager's);
  - `run_budget_usd`, passed as `--max-budget-usd` to every task agent process;
  - `task_budget_usd`, a cap on the task's cost (M2 accounting), and
    `task_budget_action`: `fail` at once (the default) or `wrap_up` after one
    more Planner turn; the task's error states the cap and the amount spent;
  - `supervise_interval_seconds`, over the `planner` config (0 = off);
  - `review_policy`: `planner` (as before) or `all` (every step needs a
    passing review before it is accepted).

- **Project skills (M4).** A project can carry its own Claude Code skills.
  Only its Planner, Workers and Reviewers load them, and nothing is written
  into the project's directory (`looper/agents/project_skills.py`).
  - **Storage.** Each skill is a row of `project_skills`, deleted with its
    project:
    - a name, a lowercase slug unique in the project;
    - a one-line description;
    - the body of its `SKILL.md`;
    - an enabled switch.
  - **Caps.** Each cap is refused with a 400 that states it, and a name the
    project already has is a 409:
    - a name has at most 64 characters;
    - a description has at most 1,024 characters;
    - a body has at most 64 KiB, and frontmatter is refused because the
      fields make it;
    - a project has at most 20 skills.
  - **Delivery.** Every Planner turn and every Worker and Reviewer run reads
    the project's enabled skills as it starts. They are written once,
    read-only, as one plugin, `project`, under
    `<runtime_root>/.local/projects/<project_id>/skills/<sha256>/`. The
    `SKILL.md` frontmatter is generated from the name and description. The
    plugin is mounted with one `--plugin-dir` after the library's, so Claude
    Code names each skill `project:<name>`. The Manager gets none.
  - **Effect and records.** A change reaches the next Worker or Reviewer, and
    the resident Planner restarts with `--resume` on its next turn. Each run
    records `metadata.project_skills`: the project, the set's hash and each
    skill's name and `SKILL.md` hash.
  - **API and dashboard.**
    - `GET`/`POST /api/projects/<id>/skills` and
      `PUT`/`DELETE /api/projects/<id>/skills/<skill_id>`; `PUT` takes the
      enabled switch and edits.
    - The project editor gains a Skills list below Knowledge: add a skill
      (name, description, instructions), switch one on or off, and delete
      one.
- **Project knowledge (M3).** A project can carry reference documents that
  its Planner, Workers and Reviewers read on demand instead of getting them
  in the prompt (`looper/agents/knowledge.py`).
  - **Sources.** Text written on the dashboard, an uploaded text file (base64
    in JSON), or a file in the project's default directory, referenced by its
    relative path and read live. No URLs. Binary content and paths that lead
    out of the project's directory are refused.
  - **Caps.** A document 2 MiB; a project 100 documents and 50 MiB of stored
    content; the index agents get 4,000 characters. Each is refused with a
    400 that states it.
  - **Delivery.** Text and uploads are materialized read-only under
    `<runtime_root>/.local/projects/<project_id>/knowledge/<sha256>/` and
    mounted with `--add-dir`; the project's directory is mounted too when the
    task runs elsewhere. The project layer of the system prompt gains a
    `## Project knowledge` index after the background, one line per document
    (title, summary, the path to read); a referenced file that is gone is
    listed as missing. The Manager gets neither.
  - **Effect and records.** Changes reach the next Worker or Reviewer; the
    resident Planner restarts with `--resume` on its next turn.
    `prompt_layers.project` records the index's `knowledge_sha256` and
    `knowledge_documents`.
  - **API and dashboard.** `GET`/`POST /api/projects/<id>/knowledge` and
    `PUT`/`DELETE /api/projects/<id>/knowledge/<doc_id>`; the project editor
    gains a Knowledge list to add text, upload a file, reference a project
    file and delete, with a count, size and index readout.
- **Cost and context observability (M2).** Every Claude run now records
  what it spent and how large its context grew (`looper/agents/run_usage.py`).
  There is no spending cap.
  - **Usage per run.** `llm_records` gains `cache_read_tokens`,
    `cache_creation_tokens`, `context_tokens` and `compactions`. The run's
    own cost and its four token counts come from the change in Claude's
    session totals (`total_cost_usd` and `modelUsage`). `result.usage` is not
    used: it leaves out compaction and subagent requests.
  - **Context.** `context_tokens` is the prompt of the run's last main-loop
    request.
  - **Compactions.** `compactions` lists each `compact_boundary` event, with
    its trigger and the context before and after.
  - **Task cost.** `GET /api/tasks/<id>` and every task in `GET /api/tasks`
    carry `cost`: the total of the task's Planner, Worker and Reviewer runs,
    the total by role, the run count, and the runs still running or without
    a reported cost.
  - **Agent usage.** `GET /api/agents/<agent_id>/usage` returns an agent's
    total, its cost by session and by UTC day, and its latest turns, each
    with its context, cost and compactions.
  - **Dashboard.** The task list and the task page show the task's cost; the
    task page also breaks it down by agent. The Manager's and each Planner's
    history page shows the context over its turns as a sparkline, with
    compaction markers, the latest context size and the compaction count.
    Every run card shows its own tokens (cache included), its final context,
    whether it compacted, and its own cost.
- **Prompt regression eval (M6).** `python -m evals.prompt_regression run`
  drives five small tasks (one step, dependent steps, a reviewed step, a
  retry, a mid-task chat correction) through a disposable `looper serve` and
  reports success, steps, retries, reviews, cost, contract violations and
  whether the Planner modified files, compared with the previous run or the
  committed baseline as a Markdown table. Fake mode (a scripted stand-in
  Claude) runs in the test suite; real mode (haiku by default) runs on demand
  under a per-run `--max-budget-usd` and a total spending cap
  ([`evals/README.md`](evals/README.md)).

### Changed

- **Shorter project knowledge index.** The `## Project knowledge` index names
  each directory once (`In <directory>:`) and gives every document a path
  relative to it, `- <title>: <summary> (<path>)`, instead of an absolute
  path of about 150 characters a line; short documents now reach the
  100-document cap before the 4,000-character index cap (it used to fill at 15
  to 25). Claude Code resolves a relative `Read` path against the working
  directory only, so the agent joins the path to the directory named above
  it. The text and upload documents are materialized together, one read-only
  directory per set of documents (`knowledge/<set_sha256>/<filename>`, a
  shared name gets `-2`), and that directory is the one mounted with
  `--add-dir`.

- Each task's Planner is now a Claude Code agent. It keeps one Claude process
  for the task's life, started on its first turn in the task's directory and
  ended when the task ends or the server stops. Claude splits the requirement
  into 1–8 steps, each with acceptance criteria, dependencies and whether a
  Reviewer must pass it. After every Worker and Reviewer result it accepts the
  step, sends it to review, retries it in a new Worker with feedback (at most
  2 per step), replans the steps not started yet, or fails the task. Every
  reply ends with one JSON block that code validates strictly; an invalid
  reply gets one repair turn. Planning that still fails fails the task, with
  no single-step fallback; a decision that still fails falls back to the
  step's own verdict without a Reviewer and logs a warning. Like every agent,
  the Planner's Claude has every tool and no permission prompts (see the
  permissions entry below), and loads no MCP server
  (`--strict-mcp-config`). The `complexity` heuristic is gone: a
  Reviewer runs only when the plan or a decision asks for one, and its prompt
  now includes the step's instruction. A chat message to a Planner now goes
  through its Claude, which replies and may pass the text to the running
  Worker or add steps; a message no longer becomes a step by itself. The
  `planner` row of `agent_configs`, unused before, now applies on every turn:
  `system_prompt` (the code appends the reply contract), `timeout_seconds` per
  turn (default 1200, 0 = no limit), `model` and `max_turns`. At startup a row
  still holding the old default prompt gets the new one; an edited prompt is
  kept. Every Planner turn is a paid Claude run, recorded in `llm_records` as
  `planner-<task_id>` with its own cost and shown beside a selected Planner on
  the Chat page, on its history page and in the Agents page's Runs tab
  (`role=planner`). A completed task's `tasks.result_summary` is now the last
  accepted step's summary, and a failed task's `tasks.error` is the Planner's
  failure summary.
- Projects have a **background** (`projects.background`, at most 10000
  characters; `""` clears it): what every task agent of the project should
  know. It is added to the system prompt of the project's Planner, Workers
  and Reviewers, not the Manager's, as `## Project: <name>` between the role
  prompt and the code-owned contract, which always comes last. Changes apply
  at once: the Planner rebuilds its system prompt every turn and restarts its
  Claude with `--resume` when it changed, and every Worker or Reviewer that
  starts afterwards reads the current background; a running Worker keeps its
  own. A run whose project cannot be read goes on without the background and
  logs a warning. The API (`POST`/`PUT /api/projects`, and the inline
  `project` of `POST /api/tasks/create`) takes `background`; the Tasks page's
  project bar shows it and edits it with a `n / 10000` counter, and the
  new-project dialog has an optional Background field.
- **Behaviour change: Workers and Reviewers now get a system prompt.** They
  used to start with none, and the `worker` row's `system_prompt` was stored
  but never applied. Their `--append-system-prompt` is now that worker role
  prompt, the project section and the code-owned contract. The stable rules
  moved out of the stdin prompt: `WORK_CONTRACT` (the step's rules and the
  `{status, changed_files, deleted_files, summary}` report) and
  `REVIEW_CONTRACT` (the review's rules and the `passed` verdict) are the end
  of the system prompt, and the first user turn carries only the step's data
  (instruction, working directory, acceptance criteria, validation commands;
  for a Reviewer also the Worker's report, as data to check). The formats
  code accepts are unchanged. The worker role prompt's recommended default is
  new and describes the role only; an unedited `worker` row moves to it at
  startup, including one seeded before the recommended prompt was tracked,
  and an edited one is kept.
- Every agent run (Manager, Planner, Worker, Reviewer) records the system
  prompt Looper appended in `llm_records.metadata.system_prompt`;
  `metadata.command` still holds the exact argv. Each run on the history page,
  and in the Chat page's Planner and step views, has a collapsible System
  prompt section; Claude Code's built-in system prompt is not included.
- **Behaviour change: agents no longer load the operator's personal Claude
  Code configuration.** The Manager, the Planner, the Workers and the
  Reviewers all start Claude Code with one flag set
  (`looper/agents/claude_cli.py` `isolation_args`, recorded as
  `personal-config-off/v1`): `--setting-sources project`,
  `--strict-mcp-config`, and `--settings` with `autoMemoryEnabled: false` and
  `claudeMdExcludes` for every `CLAUDE.md`, `.claude/CLAUDE.md` and
  `.claude/rules/` above the work directory's repository and in `~/.claude`.
  They no longer see `~/.claude/CLAUDE.md` and its imports, `CLAUDE.local.md`,
  user and local settings (including the operator's model and effort choice,
  env and permissions), hooks, personal and claude.ai-synced skills,
  installed plugins and their skills, agents and hooks, custom agents, output
  styles, MCP servers (the Manager loaded them until now) or auto memory. The
  project's own `CLAUDE.md` and `.claude/settings.json` still load, and the
  login is unaffected. The model and effort now come from Looper's settings
  instead of the operator's (below), and rules that every project must follow
  go into the default harness (below).
- **Behaviour change: the task description is part of the system prompt.**
  The system prompt of the Planner, the Workers and the Reviewers is now four
  layers: role prompt, project background, a `## Task` section, then the
  code-owned contract, still last. The Planner's task section has the task
  id, project, working directory and requirement, which its turns no longer
  repeat; a Worker's has the task's requirement, then its step's label,
  instruction, acceptance criteria, validation commands and, on a retry, the
  feedback; a Reviewer's has the requirement and the reviewed step. The
  requirement is clipped at 8,000 characters with a visible marker. A
  Worker's first user turn now carries only what the steps it depends on
  reported (new `dependencies` in `WORK_ITEM`, which also carries
  `requirement`, `step_key`, `feedback` and `attempt`; `instruction` no longer
  includes the retry feedback); a Reviewer's carries the Worker's report. The
  contracts' wording and the Planner's reply contract say so; the formats
  code accepts are unchanged.
- The system prompt goes to Claude Code as a file,
  `<runtime_root>/.local/prompts/<sha256>.md`, written once
  (`--append-system-prompt-file`), so `metadata.command` stays short. With it
  goes `--system-prompt-snapshot off`: Claude Code 2.1.285 otherwise keeps a
  session's first system prompt on every `--resume`, so a Planner or Manager
  restarted after a background or role prompt edit kept running with the old
  prompt. Every run also records `metadata.system_prompt_sha256` and
  `metadata.prompt_layers` (role prompt and whether it is the recommended
  one, project and background version, task, contract, isolation flag set).
  The history page's System prompt section shows the short hash, an
  `isolated` badge and one line per layer; older runs keep showing their
  prompt.
- **Behaviour change: a harness layer comes first in every agent's system
  prompt, and the role prompts hold responsibilities only.** The system prompt
  is now five layers for the Planner, the Workers and the Reviewers (harness,
  role prompt, project background, task, contract) and three for the Manager
  (harness, role prompt, `TASK_PLACEMENT_RULE`). The harness is an agent's
  working principles, boundaries and tone. Its default lives in Settings (a
  new `system_settings` table, edited on the Settings page, with Reset to
  built-in); until it is edited, the public built-in `DEFAULT_HARNESS_PROMPT`
  applies. The Manager can override it on the Agents page
  (`agent_configs.harness_prompt`, Manager only; 400 for the other agents),
  and a project can override it for its Planner (`planner_harness_prompt`)
  and its Workers (`worker_harness_prompt`), in the project editor's
  "Harness overrides". Reviewers follow their project's Workers. Each harness
  is at most 20,000 characters. The recommended role prompts of the Manager,
  the Planner and the Workers lost their principles and tone ("be concise",
  "default to Chinese", "answer in the user's language", "check by running
  things", "say what you could not do"), which the default harness now
  carries; an unedited role prompt follows at startup, and an edited one is
  kept. The Manager now answers in the language the user writes in rather
  than defaulting to Chinese, unless a harness says otherwise.
- **New `reviewer` agent row.** Reviewers no longer reuse the `worker` row and
  its role prompt: they have their own recommended role prompt (judge the
  result, not the report; hold the work to every acceptance criterion; make
  findings fixable), seeded at startup, and read their own row's
  `timeout_seconds` (86400 by default), `max_turns` and `model`. A `worker`
  row's custom timeout no longer applies to Reviewers. The Agents page lists
  it, and the Runs tab and `role=worker` in `/api/llm-records` now list
  Workers only, with `role=reviewer` for Reviewers.
- **Behaviour change: every agent passes `--model`, `opus` by default, which
  can change cost.** Settings has a default model (built in: `opus`), an
  optional effort (`--effort low|medium|high|xhigh|max`; none passes no flag)
  and up to 50 non-secret environment variables. All four agents now pass
  `--model` (before, only the Planner did, and only when its row set one);
  an agent's own `model` on the Agents page wins. On an account whose Claude
  Code default is a cheaper model, agents now run on Opus and cost more; set
  the default model in Settings to keep the old one. The environment
  variables are added to every agent's `claude` process on top of the
  server's environment; a name must match `[A-Z_][A-Z0-9_]*`, and a name
  containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `CREDENTIAL` is refused
  because secrets do not belong there. A changed harness, role prompt, model,
  effort or variable applies to the next Worker or Reviewer run; the Manager
  and a running Planner restart with `--resume` on their next turn. New API:
  `GET/PUT /api/settings/agents` and `POST /api/settings/agents/reset-harness`;
  the project API and inline project creation take the two harness fields.
- Every run's `prompt_layers` gains `harness` (`source`: `builtin_default`,
  `system_default`, `manager_override` or `project_override`, its sha256 and
  the project), and its metadata gains `model_choice` (the model asked for,
  its source and the effort) and, when Settings holds variables, `env` (their
  names with a sha256 of each value; values are never recorded). The history
  page's layer summary shows the harness source, the model and the variable
  names.
- **Behaviour change: every agent loads the imported plugins and skills.**
  Settings gains "Plugins & skills": "Import from this machine" reads, and
  only reads, the operator's Claude Code configuration (`$CLAUDE_CONFIG_DIR`,
  else `~/.claude`) and stores a snapshot in the database (`library_items`,
  `library_files`): every enabled plugin, whole (its skills, agents, commands
  and hooks), and every personal skill. Personal hooks, MCP servers,
  permissions and memory are still not loaded. Every Manager, Planner, Worker
  and Reviewer run then gets a read-only copy under
  `<runtime_root>/.local/library/` and one `--plugin-dir` per enabled plugin
  plus one for the enabled skills, which Claude Code names
  `personal:<skill>`. "Re-import" replaces the items that changed and keeps
  each item's switch; a switch applies to each agent's next run, and the
  Manager and a running Planner restart with `--resume`. Files over 2 MiB,
  binary files and links out of an item are left out and reported; an item
  over 32 MiB or 5,000 files is left out whole; a library over 128 MiB is
  refused. Every agent runs without permission prompts (see the
  permissions entry below), so the skills need no approval of their own. Plugins' own
  `SessionStart` hooks run in every agent. Nothing is imported until someone
  imports, and without a library every command is as before. **Context and cost:** with 3 plugins and 104
  skills, the first request of a minimal run on Opus grows from 15,674 to
  29,114 tokens; uncached, that first request cost $0.136 instead of $0.043.
  Disable what an agent does not need. New API: `GET /api/settings/library`,
  `POST /api/settings/library/import` and `PUT /api/settings/library/<id>`.
- Every run's metadata gains `library` (the items it loaded, each with its
  sha256, and the hash of the set), and the history page lists them in a
  "Plugins & skills" section.
- **Behaviour change: every agent runs without permission prompts, and the
  Planner can now edit.** The Manager, Planners, Workers and Reviewers all
  get every tool and `--dangerously-skip-permissions`, from one place
  (`claude_cli.permission_args`, carried by every launch). The Planner no
  longer has `--tools`/`--allowedTools Read,Grep,Glob[,Skill]` and
  `--permission-mode dontAsk`, and the Manager no longer has
  `--allowedTools Skill`: both can run commands and edit files, and skills
  that declare `allowed-tools` run in every agent. What keeps the Planner to
  planning is now its recommended role prompt, which says it plans and
  decides, does not modify files and gives every change to a Worker. An
  unedited `planner` row gets the new prompt at startup; an edited one is
  kept and may still describe the Planner as read-only, so review it. The
  Agents page shows these shared permissions in place of the Claude Code
  "Permission Mode" select, which no Looper agent read.
- **Behaviour change: Telegram and Lark refuse to start without a sender
  allowlist.** Because the Manager can now run any command on this machine,
  each chat channel passes on only the senders its Settings configuration
  lists: Telegram numeric user IDs (`allowed_user_ids`) and Lark open_ids
  (`allowed_open_ids`, the `ou_...` id of each message's sender). A message
  from anyone else never reaches the Manager: the adapter logs one line with
  the channel and the sender id, never the text, and sends no reply
  (Telegram's `/start`, `/status` and `/help` included). An enabled channel
  whose list is empty or invalid does not start; the startup log and the
  Settings page say why. An empty Telegram list no longer means everyone, and
  `LOOPX_TELEGRAM_ALLOWED_USERS` no longer applies: add your IDs in Settings
  before you restart a channel you use. Settings edits the list with
  format checks; `PUT /api/channel-configs/<id>` validates it (400 naming the
  first bad id) and stores it as a JSON array, still reading a
  comma-separated Telegram list from before, and both channel endpoints
  return `start_problem`. The web dashboard is unchanged: it answers only
  local requests.

### Fixed

- **Dashboard fixes.**
  - What the Manager's Claude says in a turn of its own now shows in an open
    web chat as it is saved, as a message of its own; before, it appeared
    only after the chat was reopened. The web channel pushes it as a
    `chat.message` event on `GET /api/chat/events?session=<chat session>`,
    which the chat page keeps open for the Manager's chat.
  - A task's config is stored and returned as a JSON object. It used to come
    back as JSON text, so the task page showed `"{}"`, and saving it from
    there stored a string that grew one layer of quoting per save. Rows
    stored that way read back as the object; a config that is not an object
    is refused with a 400.
  - The Tasks list has its page padding; the task page's back button is
    named "Back to tasks"; below 1024 px the sidebar collapses into a drawer
    opened from a menu button (Escape, the backdrop or a choice closes it);
    a project created from New Task can be given its background.
- A resident Manager or Planner whose saved Claude session no longer exists
  answered nothing, forever: every turn resumed the same unknown id, Claude
  replied with only an error result ("No conversation found", no init event),
  and the id was kept. A process started with `--resume` that ends its turn
  without Claude's `system/init` event now drops the saved session and runs
  the turn once more in a new session; the run record names the dropped id.
  The context each turn carries is rebuilt by code, so nothing of the task's
  state is lost.
- **The first turn of a resumed Manager or Planner session is no longer
  counted twice.** A Claude process started with `--resume` reports, in its
  first result, the session's earlier spending plus its own. Each new
  process used to start counting from zero, so the turn after every server
  restart, prompt or settings change, or Claude exit was recorded with the
  whole session's cost again. For example, a resumed turn that spent $0.0070
  was recorded as $0.1393. The session's running totals are now saved beside
  its id (the Manager's `session.json`, the Planner's plan file), and a
  resumed process starts from them. They reset when the session is dropped.
  Records written before this fix keep their inflated cost.
- **A resident Manager or Planner no longer answers a message with the turn
  before it.** When background work a turn started (a background Bash command
  or subagent) ends, Claude Code runs a turn of its own and writes a result
  that answers no message. Its output was only read during the next message's
  turn, which ended at that result, so the Manager sent the autonomous turn's
  words as the answer to the next message, and every reply after it was one
  turn late in the chat and in the run records. Total cost was unaffected.
  - A resident Claude's output is now read for as long as it runs. Each
    message carries a uuid; its turn starts when Claude marks that message
    started (its `command_lifecycle` event, or its replay, now that resident
    agents run with `--replay-user-messages`) and ends at the next result.
    Anything else that ends in a result is a turn of Claude's own, even one
    that runs while the next message waits in Claude's queue.
  - A turn of Claude's own is a run of its own: its input reads "(no user
    message: ...)" and its metadata has `autonomous: true`. Its cost is its
    share of Claude's running totals, which also keeps the next turn's cost
    right.
  - The Manager sends what Claude said in it as a reply of its own to the
    chat of the message Claude answered before it. The web channel adds such
    a reply to that chat's history (shown when the chat is opened) rather than
    to a message's stream. The Planner only logs it: it decides nothing about
    the plan.
- `looper config export --include-secrets` names LLM provider API keys in its warning and `--help`; it wrote them without saying so.

### Removed

- **LLM profiles.** `llm_profiles`, `agent_configs.runtime` and
  `agent_configs.llm_profile_id` were never on an agent's launch path;
  runtimes, providers and agents replace them. A one-time migration drops
  them, `/api/llm-profiles` is gone, an agent config update naming them is
  refused, and the Agents page no longer shows them.
- **`agent_configs.permission_mode`.** No agent applied it since every agent
  runs with `--dangerously-skip-permissions`; the startup schema drops the
  column (`DROP COLUMN IF EXISTS`), and agent configs no longer return it. An
  update that still sends it, as the current dashboard does, is applied
  without it.
- **`loopx update` and `loopx init`.** To upgrade, re-run `install.sh`: it
  installs the latest release wheel over the current one. `loopx init` wrote
  `providers.yaml` and `agents/*.yaml`, which nothing in the agent system
  reads.
- **The upstream LoopX command line.** `loopx` now has only `serve`,
  `version` and `--version`, and starting the server no longer loads any
  upstream module. The other upstream subcommands (`status`, `todo`,
  `dashboard`, `chat`, ...) and the global `--registry`, `--runtime-root`
  and `--format` options are gone; `loopx serve --runtime-root` is
  unchanged. `loopx --runtime-root DIR serve` used to ignore `DIR`; it is
  now an error.
- The release wheel no longer requires or builds the upstream Chat bundle
  (`loopx/web/chat`). It still carries the dashboard built from `web/`.
- **The upstream LoopX code.** The package is now the agent system alone
  (`loopx.agents`, `loopx.db`, `loopx.cli`, `loopx.paths`): the upstream
  control plane, capabilities, extensions, Chat server and the other upstream
  modules are gone, and with them the `loopx-lark-provider`,
  `loopx-openviking-history-export` and `loopx-openviking-semantic-preference`
  commands, the `deepseek-harness` extra and the workflow skills the wheel
  installed under `share/loopx/skills`. The repository no longer has the
  upstream `apps/` frontends, `packages/` plugins, `skills/`, the man page,
  or the source-snapshot installers `scripts/install-from-github.sh` and
  `scripts/install-local.sh`; `install.sh` is the way to install and upgrade.
- **The upstream LoopX documentation.** `docs/` keeps the agent system's
  architecture, design and development plan and the release procedure
  ([docs/releases/README.md](docs/releases/README.md)). The upstream guides,
  references, RFCs and book, the fork's role_v1 design notes under
  `docs/fork/`, `ADOPTERS.md`, `mkdocs.yaml` and the upstream governance file
  are gone, and links to them in this changelog point at the last commit that
  had them. `AGENTS.md`, `CONTRIBUTING.md`, the issue and pull request
  templates and the security, support and conduct contacts now describe this
  fork and point at `foolzzz/looper`.

## [2.5.0] - 2026-10-13

Covers `v2.1.0` to `v2.5.0`: this fork's merged pull requests from #106 to
#180 in [foolzzz/loopx](https://github.com/foolzzz/loopx/pulls), pull request
#1 in [foolzzz/looper](https://github.com/foolzzz/looper/pulls), where
development continues, and
[michaelx1993/loopx](https://github.com/michaelx1993/loopx/pulls) pull
requests #30 to #58. Versions 2.1.1 through 2.4.0 were tagged and released
only in michaelx1993/loopx; their changes are listed here. Entries for that
work link its pull requests.

### Added

- **`loopx serve` starts a three-tier agent system.** A Manager talks to the
  user and turns requests into tasks. A TaskRouter runs at most three tasks
  at a time and queues the rest. Each task gets its own Planner, which calls
  no model: it runs the task as one step, and a chat message to it while the
  task runs reaches the running worker or becomes a further step. A
  WorkerPool runs each step as a `claude` subprocess, at most four at a time.
  They exchange typed messages over an async bus with a JSONL journal under
  the runtime root. `loopx serve` also serves the HTTP API and the web
  dashboard, on `127.0.0.1:8767` by default (`--host`, `--port`). Every agent
  run uses the Claude Code CLI, which must be on `PATH` and signed in; workers
  run it with `--dangerously-skip-permissions` in the task's work directory.
  ([#38](https://github.com/michaelx1993/loopx/pull/38),
  [#39](https://github.com/michaelx1993/loopx/pull/39),
  [#40](https://github.com/michaelx1993/loopx/pull/40),
  [#41](https://github.com/michaelx1993/loopx/pull/41),
  [#42](https://github.com/michaelx1993/loopx/pull/42),
  [#51](https://github.com/michaelx1993/loopx/pull/51))
- **`loopx serve` requires PostgreSQL.** It connects to `LOOPX_DATABASE_URL`
  (default `postgresql://localhost:5432/loopx`; a `.env` file in the
  repository root fills in variables that are not set) and does not start
  without the database. Each start applies the idempotent schema in
  `loopx/db/schema.sql`: tasks, steps, activity, chat messages, agent
  configs, LLM profiles, LLM run records and channel configs.
  ([#44](https://github.com/michaelx1993/loopx/pull/44),
  [#45](https://github.com/michaelx1993/loopx/pull/45),
  [#48](https://github.com/michaelx1993/loopx/pull/48),
  [#49](https://github.com/michaelx1993/loopx/pull/49))
- **Web dashboard for the agent system** in `web/`. Build it with
  `npm install` and `npx vite build`, which writes the `web/dist` that
  `loopx serve` serves. Pages: Chat, Tasks, task detail (overview with a live
  activity feed, pipeline graph, expandable Agent Timeline, conversation),
  Agents and Settings. The URL hash keeps the page across a reload.
  ([#41](https://github.com/michaelx1993/loopx/pull/41),
  [#43](https://github.com/michaelx1993/loopx/pull/43),
  [#46](https://github.com/michaelx1993/loopx/pull/46),
  [#47](https://github.com/michaelx1993/loopx/pull/47),
  [#50](https://github.com/michaelx1993/loopx/pull/50),
  [#52](https://github.com/michaelx1993/loopx/pull/52),
  [#55](https://github.com/michaelx1993/loopx/pull/55),
  [#56](https://github.com/michaelx1993/loopx/pull/56))
- **Task lifecycle controls.** A task moves through `init`, `pending`,
  `assigned`, `running` and `completed` or `failed`. `stopped` pauses it so
  it can be started again; `cancelled` ends it for good. A task created on
  the dashboard starts in `init` and can be edited before it starts. A task
  that is still active cannot be deleted. On startup, tasks a previous server
  left running are marked `stopped`.
  ([#39](https://github.com/michaelx1993/loopx/pull/39),
  [#52](https://github.com/michaelx1993/loopx/pull/52),
  [#53](https://github.com/michaelx1993/loopx/pull/53),
  [#57](https://github.com/michaelx1993/loopx/pull/57))
- **Agent configuration and LLM profiles in the database**, edited on the
  Agents page and through `/api/agent-configs` and `/api/llm-profiles`. Only
  the Manager's system prompt and the worker's `timeout_seconds` and
  `max_turns` take effect; the other fields are stored but not applied.
  ([#45](https://github.com/michaelx1993/loopx/pull/45),
  [#46](https://github.com/michaelx1993/loopx/pull/46),
  [#47](https://github.com/michaelx1993/loopx/pull/47))
- **Telegram and Lark channels** for the Manager, off by default and
  configured on the Settings page (see Changed).
  ([#38](https://github.com/michaelx1993/loopx/pull/38),
  [#56](https://github.com/michaelx1993/loopx/pull/56))
- **`loopx init`** detects the `claude` and `codex` CLIs and API keys, then
  writes `providers.yaml` and the `orch`, `dev` and `acc` agent files under the
  runtime root for the role dispatcher. `--non-interactive` takes the detected
  defaults; `--force` overwrites existing files. `loopx serve` does not read
  these files. ([#32](https://github.com/michaelx1993/loopx/pull/32))
- **`loopx dashboard`** Settings gains an Agent config tab, which edits the
  role dispatcher's agent and provider files, and an Update tab. Chat gains a
  session panel, and creating a Goal starts from a one-line prompt.
  ([#36](https://github.com/michaelx1993/loopx/pull/36),
  [#37](https://github.com/michaelx1993/loopx/pull/37))
- `install.sh`, a curl-pipeable installer for the wheel of the latest
  GitHub release; see Changed for its source and what it does when that
  release has no wheel.
  ([#32](https://github.com/michaelx1993/loopx/pull/32),
  [#34](https://github.com/michaelx1993/loopx/pull/34))

### Changed

- Development moves to `foolzzz/looper`, after GitHub disabled Actions on
  `foolzzz/loopx`. Install and update (`install.sh`, `loopx update apply`,
  the doctor hint and the onboarding source) now come from `foolzzz/looper`
  releases, the project URLs and the first-run issue link point there, and the
  install docs clone `foolzzz/looper`. Links to earlier pull requests and
  issues keep pointing at `foolzzz/loopx`, where they live.
- Releases are published by GitHub Actions when a `vX.Y.Z` tag is pushed
  (`.github/workflows/release.yml`); nothing is built or uploaded locally. The
  release pull request commits the exact body as `docs/releases/vX.Y.Z.md` and
  its optional-capability surfaces as `docs/releases/vX.Y.Z.surfaces`. The
  workflow checks the tag against `main` and the package version, validates
  the body, builds the Chat bundle and the dashboard, runs the wheel smoke
  against a PostgreSQL service, builds the wheel and publishes the release
  through a draft, so the latest release always carries its wheel.
- A release wheel runs `loopx serve`. It now carries `loopx/db/schema.sql`
  and the agent dashboard, staged into `loopx/web/dashboard` by
  `scripts/stage-dashboard.sh`; before, an installed wheel failed at startup
  with no schema and had no dashboard. A checkout still serves its own
  `web/dist`. `examples/release/agent-serve-wheel-smoke.py` builds the wheel
  from a clean copy, installs it and runs the installed `loopx serve`, and
  the release steps attach that wheel to every GitHub release.
- Install and update use this fork, `foolzzz/loopx`, instead of
  `michaelx1993/loopx` or upstream: `install.sh`, the no-clone installer URL
  (`https://raw.githubusercontent.com/foolzzz/loopx/main/install.sh`), the
  default repository of `loopx update` and of the archive installer, and the
  repair commands that doctor and bootstrap print. Behavior change: when the
  latest `foolzzz/loopx` release has no wheel asset, a pip or pipx
  `loopx update apply` no longer falls back to the PyPI `loopx` package, which
  is a different (upstream) distribution. It installs nothing, fails, and says
  to install from a release tag (`git clone https://github.com/foolzzz/loopx.git
  && cd loopx && git checkout vX.Y.Z`, then `uv sync`) or to wait for a release
  with a wheel; it fails the same way when the release cannot be read.
  `install.sh` gives the same guidance (it used to report a network error).
  Doctor now names `loopx update apply`, not `pip install --upgrade loopx`, as
  a pip installation's upgrade command, and the Codex CLI install preflight
  runs the no-clone installer without trying PyPI first. `foolzzz/loopx`
  releases carry a wheel from v2.5.0 on. `loopx commands` and `man loopx` now
  list `loopx init` and `loopx serve`.
- Security: the agent HTTP API no longer accepts requests from other web
  pages. It sent `Access-Control-Allow-Origin: *` and approved every preflight,
  so any page open in the browser could create and start a task, whose Workers
  run Claude with every permission, or read chat and run history. A request
  whose `Origin` is not the server's own now gets 403, no response carries CORS
  headers, and on a loopback address the `Host` must be a loopback name
  (against DNS rebinding). curl, the CLI and the Vite dev proxy are unaffected.
- The README describes the agent system: architecture, quick start with
  PostgreSQL, HTTP API, configuration and task lifecycle.
  `docs/architecture/agent-system.md` covers the tiers, message bus,
  persistence, dashboard and API.
  ([#54](https://github.com/michaelx1993/loopx/pull/54),
  [#58](https://github.com/michaelx1993/loopx/pull/58))
- Every agent task belongs to exactly one project. A project has a name
  (unique ignoring case), a description and an optional default directory.
  `GET`/`POST /api/projects` and `PUT`/`DELETE /api/projects/<id>` manage
  projects and report each one's task and active counts; a taken name is 409,
  and so is deleting a project that still has tasks. `POST /api/tasks/create`
  now requires exactly one of `project_id` and `project` (a new project,
  created in the same transaction as the task); a request with neither is
  refused. A task runs in the `work_dir` it is given, else its project's
  default directory, and is refused when neither exists; there is still no
  global default. `PUT /api/tasks/<id>/project` moves a task whatever its
  status, `GET /api/tasks?project_id=<id>` lists one project's tasks, and task
  rows and detail carry `project_id` and `project_name`. In chat, each message
  the Manager sends to Claude starts with the project list, and the task JSON
  gains `"project"`. The Manager matches it to an existing project ignoring
  case, creates the project (with the task's directory as its default) when
  the name is new, and asks when it is missing. The rule appended to the
  Manager's system prompt changes, so a running Manager restarts its Claude
  once, resuming the session. On first start, existing tasks get one project
  per directory, named after its last path component (`default` for none; a
  later clash gets `-2`, `-3`, ...). A queued task request from before this
  change, replayed from the bus journal, is dropped with an error log.
  The dashboard lists projects in the sidebar (click one to filter the Tasks
  page), filters, edits and deletes projects on the Tasks page, picks or
  creates a task's project in the new-task form, and moves a task between
  projects from its detail view.
- The Manager keeps one Claude process running instead of starting `claude`
  for every chat message. The process starts on the first message with
  `--input-format stream-json` and no prompt argument; each message is one
  user turn on its stdin and still one Runs record, whose cost is that turn's
  share (Claude reports the process's running total). A later message no
  longer pays Claude's start-up time. When Claude exits, or the Manager's
  system prompt changes, the next message starts a new process with
  `--resume` on the saved session. A message whose turn ends because Claude
  exited is answered with an error, as before. Stopping the Manager closes
  Claude's stdin and kills it after 5 s.
- The Chat page lists every agent, finished ones included: the Manager, then
  for each of the 30 most recent tasks its Planner and the Worker (and
  Reviewer) of each step. Selecting a Worker or Reviewer shows its Claude runs
  read-only; an ended task's Planner shows its thread and activity read-only.
  `GET /api/chat/targets` returns this list with `can_send`; only the Manager
  and a running task's Planner take messages. Reviewer runs are now filed under
  their task, and every Worker/Reviewer run records the step it served
  (`metadata.step_id`).
- Workers read Claude's input as stream-json: the prompt goes on stdin, which
  stays open until the result. A chat message to a task's Planner reaches the
  running Worker as its next user turn (`WORKER_INPUT` /
  `WORKER_INPUT_RESULT`) instead of waiting as a new step; it becomes a step only
  when no Worker is running. The history page shows these user turns.
- The agent history page shows live thinking progress (`thinking · ~N tokens`),
  hooks, background tasks, rate limits and any other stream event, instead of
  hiding everything but text and tools. Claude's per-second `thinking_tokens`
  events are stored as one row per thinking phase, updated in place, instead of
  one row per second.
- Agent workers apply the `worker` row of `agent_configs` that the dashboard
  Agents page edits, read for every run. `timeout_seconds` limits one worker
  run's total time (0 = no limit); output does not extend it. The default
  rises from 600 s to 86400 s, and on startup a worker row still at the old
  seeded 600 moves to 86400; any other value stays. Before, the limit was a
  hard-coded 600 s and the field had no effect. A run over the limit fails
  with `Claude ran longer than the N s limit (timeout_seconds)` instead of
  `worker timeout`. `max_turns` above 0 passes `--max-turns`; 0 sets no limit
  (it was always 50). Workers run Claude with `--include-partial-messages`,
  so thinking keeps the run's heartbeat current.
- Agent tasks run in an explicit work directory (`work_dir`). There is no default.
  Creating a task requires one (absolute or `~/...`), and a task without one
  cannot start. The Manager takes it from the conversation or asks for it, and
  the Worker creates it if missing. The second `project_root` field, which the
  dashboard never set (so its Work Directory had no effect and dashboard tasks
  failed to start), is folded into `work_dir` and dropped.
- The dashboard has an agent history page (`#history/<agent>`). It is opened
  from History on each Agent Timeline row, from the Agents page's new Runs tab
  and from the Chat page. It shows each Claude run's launch command, full
  transcript and activity, live while the run is going, and flags a run with
  no output for 2 minutes. Selecting a Planner in Chat shows its activity. A
  run's record now carries a heartbeat (`last_output_at`).
  `/api/llm-records` takes a `role` filter. The empty `/api/agents/:id/sessions`
  endpoints are removed.
- Every Claude run of the agent system (Manager, Worker, reviewer) is recorded
  the same way in `llm_records`. The record opens as `running` with the full
  launch command and working directory. It receives each stream-json event as
  it arrives in the new `llm_record_events` table, including assistant text,
  thinking, tool calls with their input, tool results and the result; partial
  text deltas are not stored. It closes with status, model, tokens, cost and
  duration. `GET /api/llm-records/:id` and `GET /api/llm-records/:id/events?after=N`
  read them back.
- The agent Chat streams the Manager's reply as Claude writes it
  (`--include-partial-messages`) instead of showing it all at once. Output
  from Claude subagents is no longer mixed into the reply.
- `loopx serve` no longer accepts `--enable-telegram`, `--enable-lark` or
  `--lark-app-id`; the flags had no effect. Telegram and Lark are built in and
  are enabled and configured on the dashboard Settings page, which saves them
  in `channel_configs`; the server reads them at startup.
  `python-telegram-bot` is now a default dependency. The Telegram allowlist
  edited in Settings now applies, and a malformed list keeps Telegram off. A
  channel that fails to start is logged and no longer stops the server.
- Agent workers now fail closed. A step completes only when Claude's run
  succeeds (a max-turns stop is a failure) and its final JSON report says
  `completed`. The files listed in `changed_files` must exist, and those in the
  new `deleted_files` must be gone. Prose without a report, an unknown status,
  and a report that does not match the files all fail the step with a
  reason. A reported failure is no longer counted as completed. Reviewers
  must return a boolean `passed`.

### Fixed

- Claude runs recorded before run status existed no longer all show as
  `completed`; those that recorded an error are marked `failed` (one-time
  migration).
- Agent replies reach each channel once. The channel registry re-routed every
  reply and status update by its origin channel, while the channel adapter also
  delivered it from its own queue. Web chat events arrived twice, and Telegram
  and Lark could post replies twice. Manager progress updates are now addressed
  to the channel instead of the TaskRouter.
- The agent message bus no longer replays a previous run's messages for Planners
  and Workers on restart. Their tasks are stopped at startup, and replaying could
  re-run their work. Work the WorkerPool takes is now marked delivered, so it no
  longer piles up in the journal, and the journal is compacted at startup.
- The agent Chat's Manager answers every message again. Claude's stream-json
  lines over 64 KiB (its init line lists every tool and skill) made it fail
  silently and left the chat waiting. The Manager and workers now read
  Claude's output with no line-length limit. A failed or empty Claude run now
  returns an error reply, and the Claude process is stopped.
- The source path-literal regression guard now decodes JS/TS, shell ANSI-C and
  PowerShell escapes, folds Python/JS constant additions and joins shell word
  parts. PowerShell expressions remain unevaluated. Decoded
  per-file/value budgets still reject new values, duplicates and moved literals;
  existing test, prose and path-owner exclusions remain unchanged.

### Removed

- Removed about 49,000 lines in 150 test, smoke and example files outside
  the agent control-plane pipeline: the semantic vocabulary, benchmark,
  showcase, content-ops, Reward Memory, finance and OpenViking extension,
  Developer Book, documentation governance, experiment and canary meta-check
  suites, including `examples/repository-hygiene-smoke.py`,
  `examples/docs-governance-smoke.py` and
  `examples/dev-book-publication-smoke.py`. The product code they covered
  remains. The removal also took out guards added earlier in this range:
  `tests/architecture/test_cli_command_module_ownership.py`, the
  path-literal budget in `tests/architecture/test_path_literal_regression.py`
  and the per-module checks in `tests/canary/test_maintainability_ratchet.py`.
  2.5.0 ships without them, although entries in this release describe the
  first two. ([#31](https://github.com/michaelx1993/loopx/pull/31))
- Retired the Release Artifacts workflow, its package identity/checksum script,
  and workflow-only validation. Automatic wheel/sdist asset upload, package
  attestations, and optional PyPI publication are no longer provided. Installed
  Chat wheel/sdist HTTP and browser validation now run in the existing frontend
  qualification CI; local release readiness remains available.
- Retired the scheduled Update Notes workflow, draft generator, generator-only
  smoke, and automation guide. Historical notes and their public-boundary smoke
  remain available.
- Removed the native desktop client, Tauri shell, signed update/repair/rollback
  UI, desktop release workflows, and their bundling, signing, feed, and validation
  helpers. Use `loopx dashboard` for the browser/PWA workspace; Settings and
  Goal navigation remain available.
- **Wire change:** `loopx doctor` no longer returns `desktop_installation` or
  the `desktop_app_runtime_pairing` check. CLI installation, release provenance,
  freshness, and managed-service diagnostics remain supported.
- Removed the repository benchmark workspace, legacy benchmark archive and
  frozen experiment snapshots, with their archive-only tests and path exclusions.
  Canary plan selection no longer emits `active_scan_changed_files` or
  `excluded_changed_files`; `changed_files` is the sole file selector.
- Removed the SWE-Marathon, LHTB and DeepSWE public research pages, homepage
  research cards, study metadata and share-bundle exports without redirects.
  The provider-neutral benchmark toolkit, CLI and managed skill remain available.
- Removed OpenCode (including OpenCode 2), Kiro CLI, Gemini CLI, and Cline
  host integrations: activation profiles and aliases, goal bridge/worker,
  Chat endpoint, installer targets, command packs, and project-skill surfaces.
  Removed `opencode2-goal-worker`, `--with-goal-bridge`, `--opencode-home`,
  `--gemini-home`, and Chat/Dashboard `--kiro-cli-bin`. The default slash-command
  installation now targets Codex and Claude Code only. Codex CLI/app-server,
  Claude Code, Cursor Agent, Pi, dsh, Ark and custom runner routes remain.
- Removed the implicit OpenCode connector for visible `generic-cli` host-mode
  plans. A generic visible identity now fails closed; explicit Pi still uses
  `pi_goal_loop` with the generic-cli Turn adapter.
- Removed the automated Frontstage Pages workflow, its GitHub stargazer-history
  fetch/render helpers, the README star-history image, and workflow-only smokes.
  Presentation sources, local builds, static exports, and browser validation
  remain available; hosted Pages publication is no longer a repository check.

- Removed the unused synthetic `loopx.visible_governance` projection and its
  dedicated smoke. It had no product or CLI consumer; existing status, quota,
  task-lease, and decision-scope contracts remain unchanged.
- Removed the bundled Doubao model-behavior actors and live qualification
  commands, including Claude/Doubao and host-prompt probes. Provider-neutral
  packet/oracle tests and real CLI/shell regressions remain.
- Removed `doubao_actual_default` from the exact release qualification
  manifest's required receipt set and summary schemas; the remaining release
  checks still fail closed. Retired receipts are rejected as unknown ids.
- Removed the computer-use runtime protocol, schemas, independent validator,
  fixture smoke, catalog references, and canary registration.

- Removed duplicate project-state and Codex-home path construction from runtime,
  CLI, Chat, Lark and installation readback consumers. They use `loopx.paths`
  without changing path layout, environment precedence or explicit overrides.
  A source-level pytest budget prevents new raw path literals outside that owner.
- Removed the source-checkout `demo/` workspace and research/visible-agent
  launchers, together with their dedicated validation and documentation.
- Retired the `loopx auto-research` and `loopx multi-agent` command trees,
  including installed-build stubs; both now fail as unknown commands. Core
  collaboration, `loopx workspace`, and `loopx deepresearch` remain available.
- Replaced the standalone CLI module size and command ownership smoke with
  `tests/architecture/test_cli_command_module_ownership.py`, collected by
  pytest. Existing line budgets and registration ownership assertions are
  unchanged.
- Removed the retired `local_authority_shadow` post-commit observation feature:
  its Goal settings/catalog entry, `--local-authority-shadow-file` and
  `--clear-local-authority-shadow` options, the `.record` runtime method, and
  historical observation reads. Configuration summaries no longer contain
  `local_authority_shadow`; state migration no longer returns
  `authority_shadow_seeds`. Existing observation files are left untouched.
- Removed the `authority-shadow` CLI. Transaction-bound outbox recovery and
  status now use `coordination-shadow drain` and `coordination-shadow status`.
  These responses use `loopx_coordination_shadow_io_cli_v0` and omit the retired
  `historical_only` field; status `config` reports active runtime-shadow settings.
  Active runtime-shadow capture, qualification, promotion, provider defaults,
  transaction schemas and persisted lineage identities are unchanged.

## [2.1.0] - 2026-10-07

### Changed

- Retired the Full Public Smokes GitHub workflow and its workflow-only smoke.
  Full public sweeps and compact health reports remain available through
  explicit CLI runs; CI no longer aggregates receipts from a skipped job.

- **Monitor timestamps use a stable cross-language ISO grammar.** Python and
  TypeScript retain reduced-time fractional seconds (`T00.1`), reject hour 24
  and UTC year overflow, and preserve exact microsecond ordering independently
  of the host Python minor version. Monitor schedule generation now uses this
  same codec instead of JavaScript's permissive date parser. Focused CI covers
  Python 3.11 and 3.14.

- **The retired App scheduler contract has been removed from the typed core.**
  This is a breaking wire and CLI cleanup with no compatibility aliases.
  - Runtime profile enum values `codex_app_heartbeat` and `trae_app`, and host
    surface values `codex_app` and `trae_app`, are no longer accepted.
  - `quota should-run --codex-app`, `heartbeat-prompt --trae_app`,
    `--codex-app-current-rrule`, and `--app-automation-current-rrule` have been
    removed.
  - Scheduler output no longer emits `codex_app`, `app_automation`,
    App-applicability placeholders, RRULE recommendations, cadence guarantees,
    stateful App backoff, or App pause/delete host actions.
  - The App-specific settlement plan/source is removed. Generic CLI callers
    retain provider-neutral, turn-scoped settlement and must provide an explicit
    turn identity. Host-specific scheduler mutation remains provider-owned.
- **LoopX state no longer lives under Codex's directory.** This is a breaking
  path change with no migration.
  - The global runtime root moves from `~/.codex/loopx` to `~/.loopx`. It
    holds the global registry (`registry.global.json`), `providers.yaml`,
    `agents/`, `dispatch/`, `chat/`, per-goal runtime state under `goals/`,
    backups and extension state.
  - Project goal state moves from `<project>/.codex/goals/<goal>/` to
    `<project>/.loopx/goals/<goal>/`, next to the project's `registry.json`
    and `agents/`. A project now only needs `.loopx/` (and `.local/`) ignored
    by Git.
  - LoopX does not read, fall back to or migrate the old directories, and
    `loopx backup-state` and the orphaned-goal-state check no longer look at
    `.codex/goals`. To keep existing state, stop LoopX and move it by hand:
    `~/.codex/loopx` to `~/.loopx`, and each project's `.codex/goals/` to
    `.loopx/goals/`. A registry entry whose `state_file` names an explicit
    path keeps using that path. Codex's own files (`~/.codex/config.toml`,
    `~/.codex/skills`, `CODEX_HOME`) are unchanged.
- **`LOOPX_RUNTIME_ROOT` sets the default runtime root through one
  resolver.** Before, only a few call sites read it. Now every LoopX command,
  module and script that needs a runtime root and is given none reads it
  through `loopx.paths.default_runtime_root()`, at the moment the root is
  used. `--runtime-root` and a registry's `common_runtime_root` still take
  precedence. An unset or blank value means `~/.loopx`. A relative value is
  made absolute against the current directory.
  - Commands that LoopX renders for another shell (agent prompts and SSH
    commands) name the global registry as
    `"${LOOPX_RUNTIME_ROOT:-$HOME/.loopx}/registry.global.json"`. The shell
    that runs the command expands it, so a relative value resolves against
    that shell's working directory, and whitespace is not trimmed.
  - The macOS dashboard launch agents resolve the registry and runtime root
    with the same resolver at install time. When `LOOPX_RUNTIME_ROOT` is set,
    they export it to both services.
  - Lark consumer leases are machine-scoped. They live under the default
    runtime root (`LOOPX_RUNTIME_ROOT` or `~/.loopx`), whatever a service's
    `--runtime-root`, so one bot App has one consumer per machine.
  - Exceptions: the KunlunCode goal mode and the benchmark runner keep their
    own path literals. Both are being removed.
- **Reward Memory refresh outcome candidates are host-neutral.** New candidate
  responses and private sidecars use the `refresh_outcome_candidate` schema,
  storage path and receipt names instead of `codex_app_outcome`. Existing
  sidecars with the old Codex App schema and storage path remain readable, but
  every new write uses the new names.

### Fixed

- **Concurrent release smoke cleans up installer process trees.** Timeout,
  assertion and spawn failures now stop all started installers and their
  children before temporary state is removed. Promotion checks and their
  existing wait budgets remain unchanged.
- **Default promotion serializes Chat bundle preparation.** The existing
  install guard is acquired before rebuilding or carrying forward checkout
  assets, preventing concurrent installers from replacing the same bundle.
  Canary-only installation keeps its existing path without this guard.
- **Global manager CLI errors stay public-safe.** `global-gates`, `global-todos`
  and `global-risks` now reach their command-local error handlers when the
  registry is unavailable or invalid, returning JSON or Markdown errors with
  local paths redacted instead of an unhandled traceback.
- **Global registry permission failures remain structured.** Sync now reports
  `write_denied` with repair guidance when acquiring the cross-runtime registry
  lock is denied, including a lock-only permission failure. Successful syncs
  retain the same locked source refresh and registry transaction. `connect`
  and `register-agent` acquire that fence before committing local state and
  reuse it through synchronization, so denied locks leave source state intact.
- **Native Codex benchmark receipt identity.** The
  `loopx_native_codex_goal_profile_v1` receipt replaces v0 `host_surface` with
  separate `experiment_arm`, `runtime_profile`, and `execution_host` fields.
  The historical `ssh-goal` arm now records `codex_cli` and
  `codex-app-server` without conflating the runtime contract with its transport.
- **HOME-rooted Goal state no longer overlaps machine runtime state.** When a
  project root and runtime root would both use `<home>/.loopx/goals`, newly
  bootstrapped Goals store their project-owned state under
  `<home>/.loopx/project-goals` instead. Uninstalling a Goal created with the
  old overlapping layout and `--archive-state` now archives only its
  `ACTIVE_GOAL_STATE.md`; runtime-owned receipts and other files remain in the
  runtime Goal directory.
- **Goals without a code repository.** A goal created with `loopx goal
  create` and no `--repo` now runs under the dispatcher. Before, every
  developer and acceptor Turn on a todo without `task_repositories` failed
  its writeback ("accountable peer delivery must be refreshed from the
  independent git worktree"), in a git project directory or not, and the
  `goal_complete` gate never opened for a project directory that is not a
  git repository.
  - One typed rule decides where a delivery must come from, for both
    refresh-state and the `quota should-run` workspace guard. Under role_v1,
    a developer or acceptor todo that names no repository delivers from the
    goal's project directory. A todo that names a repo is still refused
    there.
  - Because nothing isolates them, the dispatcher runs at most one developer
    or acceptor Turn on such todos per goal. The others skip with
    `project_directory_turn_running`, shown in the pass and in `loopx
    dispatch status`. Orchestrator Turns and Turns on todos with repos are
    unaffected.
  - Push planning reports the implicit repo as `not_a_git_repo` when the
    project directory is not a git repository, or `no_commits` when it has
    no commit yet, skipped like a repo without a remote. The `goal_complete`
    gate uses the same classification, so it lists the directory as local
    only.

  See [Goals without a code repository](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/usage.md#goals-without-a-code-repository)
  for what works and the limits: no isolation, no delivery snapshot, no
  merge, no rollback and no push gate. Known limitation: the one-Turn slot
  follows the `turn run-once` process, not its host, and two goals that
  share a project directory are not kept apart.

### Removed

- **v0.1 main/side hierarchy migration.** `configure-goal` no longer accepts
  `--ack-automation-prompt-migration`, and the one-time migration module,
  completion receipt, and migration smoke are removed. Goals that still carry
  `legacy_hierarchy`, `primary_agent`, `side_agent_handoff_agent`,
  `agent_profile_v0`, or profile-level hierarchy policy now fail before
  identity or routing with the rejected field paths and cleanup instructions.
  Current `role_v1` and `peer_v1` goals still require a registered `--agent-id`.

- **Codex App over SSH host surface and implicit Turn creation.** The
  `codex-app-ssh` agent/host surface, `codex_app_ssh` scheduler surface and
  `codex_app_ssh_goal` runtime profile are removed. Native Codex benchmark
  paths now use the supported `codex_cli` profile. The `quota --begin-turn`
  option is also removed; callers that need accountable Turn settlement must
  pass one stable, host-owned `--turn-instance-id` and reuse it on retries.
- **KunlunCode integration.** The `loopx-kunluncode` command and its
  `loopx.kunluncode_goal_mode` package (native Goal Pro controller, managed
  MCP server, outer-controller write guard) are gone, together with the
  `kunluncode` agent type, the `kunluncode` scheduler runtime profile and
  host surface, and the KunlunCode adapter guide. `LOOPX_KUNLUNCODE_OUTER_CONTROLLER`
  no longer has any effect. A persisted scheduler context or `--runtime-profile`
  that still names `kunluncode` is now reported as unsupported.
- **NoKV authority provider candidate.** The Stage 2A NoKV candidate had no
  CLI or runtime entry point and is retired. Removed:
  - the TypeScript `NoKVAuthorityStore` and its JSONL transport, the Python
    `nokv_jsonl_helper`, and the `nokv` provider profile;
  - the `nokv-shadow-provider` and `nokv-authority-store` examples;
  - the NoKV unit, transport and qualification-harness tests and their fake
    SDK fixtures.

  The shared-authority E2E ladder loses its `s0.nokv_live_matrix` and
  `s2a.nokv_live_qualification` rows, the `env:nokv_*` gates, stage `2a`, and
  the `nokv_client_config_sha256` and `nokv_sdk_version` report bindings. Its
  Stage 0 file matrix now runs in-process from
  `loopx/control_plane/testing/authority_e2e_file_matrix.py`. The
  provider-neutral coordination contract probes moved to
  `tests/control_plane/test_coordination_contract_probes.py`. An authority
  archive whose header names `nokv` as its source provider is now rejected.
- **Documentation-text and dead example smokes (developer-facing).** 49
  example files are removed:
  - 25 smokes that only matched document wording, with their two orphaned
    fixtures and `cs-notes-integration.example.md`;
  - 21 smokes and example files that could no longer run or only tested
    their own fixtures. These include the four `/?view=ops` dashboard browser
    smokes, eight transitional `cli-*-command-modularization` shape checks,
    the handoff read-model and protocol-action-packet smokes, and the
    already-failing `todo-index-rollout-status` smoke.

  The canary planner no longer schedules two smokes that did not exist
  (three catalog entries): `worker-bridge-install-contract-smoke.py` and
  `control_plane/run-compaction-readmodel-smoke.py`. The planner smoke now
  checks a selection table and whole-catalog invariants instead of pinning
  individual smoke paths.
- **Example smokes duplicated by pytest or wired only to documents
  (developer-facing).** 37 more example files are removed:
  - 14 smokes whose assertions already live in named pytest tests, with
    their fixtures;
  - the catalog-wired documentation and duplicate smokes, with their fixtures:
    event-store migration bridge, interaction-scheduler authority, local
    state-write correctness contract, review packet, todo-detail cold path,
    global-manager command and rollback-packet protocols, canary promotion
    no-write contract, frontstage rollout projections, host integration
    surface, issue-fix capability guide, the periodic-report set, the
    progress-review sentinel, and the extension placement doc.

  The canary planner, qualification and release profiles, the quality-surface
  catalog, and the `periodic_report` and `progress_review` capability catalog
  entries drop those commands. Where a capability needs a check, it now names
  the equivalent pytest command.
- **Codex App automation tooling (first step of the Codex App and Trae App
  removal).** LoopX no longer reads or edits the Codex App automation store.
  Removed:
  - the `automation-prompts` command;
  - the installed-prompt upgrade that `update --apply` ran after installing.
    `update --apply` now only installs and checks the runtime; its payload no
    longer has `automation_prompt_upgrade` or `upgrade_complete`;
  - the turn-start prompt-upgrade hint;
  - the `loopx-apply-rrule` fallback executable (`scripts/codex_app_apply_rrule.py`),
    the `fallback_hint` it was projected through, and the installer steps
    that installed it. An existing `~/.local/bin/loopx-apply-rrule` link is
    left in place; remove it by hand;
  - reading the installed heartbeat RRULE from `$CODEX_HOME/automations` during
    `quota should-run` and `scheduler-fail-current`;
  - `upgrade-plan`, which only planned Codex App heartbeat prompt upgrades;
  - `backup-state --no-automations` and the `$CODEX_HOME/automations`
    backup target;
  - the Codex App guides and runtime pages outside the pinned dev book.
- **App scheduler ACK and failure follow-up.** LoopX no longer records which
  RRULE a Codex App or Trae App heartbeat applied. Removed:
  - the `quota scheduler-ack`, `scheduler-ack-current` and
    `scheduler-fail-current` commands and their flags (`--surface`,
    `--state-key`, `--applied-rrule`, `--failed-rrule`,
    `--host-match-observed`, `--failure-kind`, `--reset-token`,
    `--identity-signature`, `--use-current-hint`,
    `--scheduler-host-facts-chunk`);
  - `scheduler_hint.app_automation.ack_hint` and `failure_hint`, their
    `scheduler_hint.codex_app` copies and CLI route binding, and the Turn
    envelope's `ack_cli_args` and `*_detail_ref` fields;
  - `ack_cli_args` and `failure_cli_args` in `EffectTurn.next_effect`;
  - the native Node fast path for those commands in the `loopx` launchers
    (`scripts/loopx`, `scripts/loopx.ps1`, the console entry point) and its
    TypeScript transaction;
  - the obsolete App cadence progression/ACK/failure transition runtime,
    failure-cache helpers, canonical enums, and characterization fixtures;
  - the producer of `user_gate_notification_cooldown_v0`, which only fired
    after a failed App cadence update;
  - the `quota_scheduler_ack` run classification. Run rows with it that an
    older release wrote are no longer treated as quota-neutral.

  LoopX no longer reads or writes App scheduler state, so each poll projects
  the App cadence from the profile's initial interval. The App state files
  under `<runtime_root>/goals/<goal>/scheduler-state/<agent>/codex_app-*/` and
  `.../trae_app-*/` are orphaned and can be deleted by hand; there is no
  migration. Keep `scheduler-state/owner-policy-*/`, which holds the
  automation cadence policy. The canary `scheduler-ack-route` surface and
  profile become `scheduler-cadence`.
- **Codex App, Trae App and Codex IDE plugin as onboarding hosts.** LoopX no
  longer onboards or activates these hosts. Removed:
  - the `codex-app`, `trae_app` and `codex-ide-plugin` agent types and their
    aliases (`codex_app`, `codex desktop`, `codex-ide`, `vscode codex` and the
    rest) from `agent-onboard`, `agent-onboard --list-agent-types` and host
    loop activation, with their activation packets and the App-only
    `automation_cadence_json` command;
  - `codex-app`, `trae_app`, `codex-ide-plugin`, `codex-ide` and `chat-box`
    as `start-goal --host-surface`, `bootstrap-command-pack --host-surface`
    and `slash-commands --surface` values. They now fail as unsupported;
  - the guided-start `scheduler_ack_when_needed` step, which only acked an
    App automation RRULE;
  - reading `TRAECLI_THREAD_ID` as the ambient thread id;
  - the `codex_app_loop_surface` and `codex_app_default_heartbeat_cadence`
    fields of the `codex-cli-bootstrap-message` JSON payload;
  - the Codex App host command registry protocol and its smoke, and the Codex
    App plugin plan;
  - the App host rows and instructions in the `/loopx` command, the
    `loopx-project` skill, the goal-start contract, the project prompts,
    getting-started, the newcomer guide and the README host tables.

  Default changes:
  - `bootstrap-command-pack --host-surface` defaults to `codex-cli-tui`
    (was `codex-app`), and a missing host surface resolves to `codex-cli`.
  - Bare `codex`, `openai-codex` and `openai codex` now resolve to
    `codex-cli`. Before, they failed as ambiguous. Bare `cli` is still
    ambiguous.
  - Beginner preset commands pass `--runtime-profile codex_cli` (was
    `codex_app_heartbeat`).

  No migration is provided for removed App host values; bind the thread again
  from a supported host. Codex App over SSH (`codex-app-ssh`) is unchanged.
- **The `heartbeat.bind` typed Chat action.** It only returned a gate asking
  the Codex App host to create a heartbeat automation. Removed with it:
  - `goal.create`'s `heartbeat` parameter, which added the same gate after
    the Goal was created; a preview that still sends it is rejected as an
    unknown parameter;
  - the dashboard's **Set up Heartbeat** Goal action, its Goal Heartbeat
    schedule rows and drawer, and their strings.

  In a Goal, a request to keep the Goal moving (for example, "keep this Goal
   moving every day") now goes to the selected Agent in Chat. It no longer
   opens a typed preview or becomes a scheduled check. Bounded recurring checks
   continue to use `monitor.create` and `monitor.update`.

## [2.0.0] - 2026-09-29 - Fork v0: role-based multi-agent orchestration

Covers the fork's first round, 2026-09-25 to 2026-09-28 (PRs #1 to #29,
136 non-merge commits), and the fixes that followed it.

### Highlights

- **The orchestrator role.** In upstream `peer_v1`, every worker agent plans,
  decomposes and claims work for itself, and a hash picks who owns a replan.
  The fork adds the `role_v1` runtime model, which has three registered roles:
  - at most one **orchestrator** per goal. It clarifies requirements with
    you, plans, owns the acceptance criteria, answers escalations, and is
    the only role-assigned agent that opens user gates;
  - **developers**, which implement todos;
  - **acceptors**, which only review.

  `role_v1` is the default for new goals.
- **A resident dispatcher.** `loopx dispatch serve` launches role Turns when
  state changes and on a periodic tick. It only schedules: every write still
  goes through the existing CLI and kernel contracts.
- **You talk to the orchestrator through gates.** Every user gate has a
  discussion thread. The initial plan arrives as a plan card that you
  approve, and so does an agent's later change to a todo's acceptance
  criteria. `loopx goal create` turns a requirements document into a running
  goal.
- **Review before merge.** A delivery that requires acceptance moves to the
  new `in_review` status. By default, that is developer implementation work
  on a goal that has an acceptor. An acceptor then accepts, rejects or
  blocks it. Accepted work merges atomically across the todo's repos. The
  second rejection of a todo escalates it to the orchestrator.
- The fork also adds multi-repo git workspaces, a push gate, a goal-complete
  gate, per-Turn cost accounting with optional budgets, and a role board in
  the dashboard.
- Two end-to-end pilots on real models validated the full loop
  ([pilot v0](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/e2e-pilot-report-v0.md),
  [pilot v1](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/e2e-pilot-report-v1.md)).

### Added

#### Roles and the orchestrator (#4, #7, #27)

- **Roles in the registry.** `coordination.agent_model` is `role_v1` or
  `peer_v1`. `coordination.agent_roles` maps each registered agent to
  `orchestrator`, `developer` or `acceptor`, and a goal has at most one
  orchestrator. Set them with:
  - `loopx register-agent --role ROLE`;
  - `loopx configure-goal --agent-role ID=ROLE`, `--clear-agent-role ID` or
    `--agent-model role_v1|peer_v1`.
- **Role fields in the todo contract.** The contract gains `required_role`,
  `requires_acceptance`, `acceptor_agent`, `reject_count` and
  `task_repositories`, followed later by `delivered_by`, `review_feedback` and
  `acceptance_criteria`. The matching flags:
  - `loopx todo add` and `loopx todo update` take `--required-role`,
    `--requires-acceptance`, `--acceptor-agent`, `--reject-count`,
    `--task-repo` and `--acceptance-criteria`.
  - Only `loopx todo update` takes `--review-feedback` and the clear flags:
    `--clear-required-role`, `--clear-acceptor-agent`, `--clear-task-repos`,
    `--clear-acceptance-criteria` and `--clear-review-feedback`.
- **Role-aware selection.** An agent with a registered role only receives
  todos whose effective role matches its own. An agent without a role keeps
  the flat peer routing. User gates, blockers and planning work belong to the
  orchestrator, so it never receives implementation work. An acceptor
  receives the `in_review` todos it is the resolved acceptor for, and todos
  tagged `required_role=acceptor`.
- **Replan routing.** Replan obligations go to the goal's orchestrator
  instead of the owner that the sha256 peer hash picks.
- **Only the orchestrator opens user gates (#7).** Under role_v1, agents with
  the developer or acceptor role raise `blocker` todos instead. The
  orchestrator answers them itself, or turns them into a gate with options
  and a recommendation. Agents without a role keep the upstream behavior.
- **Less orchestrator overhead (decision 43, #27).** LoopX now completes pure
  bookkeeping without a model Turn: the planning todo once its plan is
  applied, an escalation whose todo is already done, and a gate-reply action
  todo whose gates were already answered. Every claude-code orchestrator Turn
  also starts from a bounded goal state digest (at most 20,000 characters),
  so it needs fewer CLI calls to discover state.

#### Dispatcher (#6, #14, #23, #24)

- **`loopx dispatch serve | status | launchd-plist`.** For each goal and
  agent, a pass asks LoopX's `should-run` whether the agent has work. It then
  applies slots, auth preflight and backoff, and launches
  `loopx turn run-once` with the agent's host flags.
  - Slots: the orchestrator is serial per goal. Developers and acceptors run
    up to their `max_concurrency`, and every Turn counts against a
    machine-wide `--max-global`.
  - Only one dispatcher runs per runtime root. It holds a `flock`, and a
    second one exits with code 3.
  - `launchd-plist` prints a `KeepAlive` job for macOS and installs nothing.
- **Per-todo Turn lanes (decision 32, #14).** One developer can work on
  several todos of the same goal in parallel. A single todo never has two
  Turns at once.
- **Failure handling.**
  - A provider that hits rate limits or its quota backs off on its own:
    60 s, then doubling, capped at 6 h.
  - A todo whose Turns keep failing backs off exponentially.
  - A crashed Turn is replayed with the same Turn identity, so it never
    settles twice.
  - A Turn whose settlement ran out of retries is resumed from its journal
    without calling the host again (#17).
- **Orchestrator action todos.** When the orchestrator has work that no todo
  carries, such as a gate reply, the dispatcher opens one action todo. It
  retires an action todo whose Turns keep failing, and opens a repeat-limit
  gate (#23).
- **Idle heartbeat (#24).** An idle dispatcher prints a heartbeat line every
  `--idle-heartbeat-seconds` (default 900), so it no longer looks dead.
- **System gates.** LoopX itself opens these gates, each through the normal
  Todo API: re-login needed, long provider cooldown, orchestrator repeat
  limit, `acceptor_blocked`, `push_request`, `budget_exhausted` and
  `goal_complete`.

#### Gate threads, plan cards and goal intake (#2, #7, #20)

- **Gate threads.** `loopx gate reply | show | list | resolve`. Every user
  gate has an append-only discussion thread with an awaiting state
  (`awaiting_user` or `awaiting_orchestrator`). Your reply wakes the
  orchestrator, and replying never closes a gate.
- **Plan cards.** `loopx plan propose | show | list | apply`.
  - The orchestrator proposes a plan as JSON: its todos, roles, repos,
    dependencies, acceptance criteria and validation commands.
  - The plan waits on a `plan_approval` gate. The orchestrator revises it in
    place with `--revise`.
  - On approval, LoopX creates the plan's todos, and dependent todos start
    `deferred`. A Markdown goal writes them all in one locked write. A
    canonical goal creates them in order with idempotent operation ids, so an
    interrupted apply resumes without duplicating todos.
- **Criteria changes (decision 40, #20).** A plan card can carry
  `criteria_changes` for existing todos. You see the old and new criteria side
  by side. While the change is pending, the acceptor does not review the
  affected todo.
- **Goal intake.** `loopx goal create --project STATE_HOME --goal-id G
  --doc req.md --repo NAME=PATH --agent ID=ROLE` does the whole setup:
  - bootstraps the goal;
  - registers the requirements doc as an authority source;
  - registers the agents, their roles and the repos;
  - adds the orchestrator's first planning todo.
- **Web gate decisions (#2).** The dashboard's `gate.resolve` now applies
  through the CLI decision path. It supports approve, reject and cancel with
  an optional note (at most 600 characters), and shows a readback afterwards.
  On `acceptor_blocked`, `budget_exhausted` and `goal_complete` gates, each
  button names the option it submits, such as "Approve (raise_budget)", and
  only the options the gate lists are offered.
- **Web discussion panel.** The gate drawer lists the thread and has a reply
  box. Two loopback-only endpoints back it: `GET /api/chat/gate-thread` and
  `POST /api/chat/gate-thread/reply`.

#### Acceptance flow (#8, #11, #13, #16)

- **The `in_review` status.** On a role_v1 goal that has registered roles,
  completing an open agent todo that requires acceptance does not mark it
  done. A goal without roles, and user todos, keep the direct completion.
  Which todos require it:
  - with `requires_acceptance=true`, always;
  - with `requires_acceptance=false`, never;
  - without the flag, only a developer advancement todo, and only when the
    goal has an acceptor: a bound `acceptor_agent`, or a `role=acceptor`
    agent.

  LoopX first runs the todo's declared validation command, then moves the
  todo to `in_review`. It records who delivered it and, for a todo with
  repos, the commit it delivered in each repo (`delivered_shas`).
- **Verdicts.** `loopx todo accept | reject | block-review`. An acceptor's
  Turn result maps to the same verdicts:

  | Turn result | verdict |
  |---|---|
  | `validated_completion` | accept |
  | `repair_required` with a summary | reject |
  | `user_action_required` | blocked |

  - Accept completes the todo. For a todo with repos, it merges first and
    completes the todo only after the merge.
  - Reject reopens the todo for the same developer. The feedback is required,
    and the prompt asks it to name each failed criterion.
  - The second rejection blocks the todo and opens an escalation todo for
    the orchestrator.
- **Per-todo acceptance criteria (G2, #11).** The criteria live in a field
  that only the orchestrator, or the owner without an `--agent-id`, writes.
  Agents without a registered role keep the upstream behavior. Developer and
  acceptor Turns see the criteria every time. Rework instructions go to
  `review_feedback`.
- **Acceptor isolation (G12, #13).** For a todo with `task_repositories`:
  - the acceptor reviews in a throwaway detached checkout of the delivered
    commit, never in the developer's worktree;
  - the merge takes exactly the delivered commit, and a branch that moved
    after delivery blocks the merge;
  - changes the acceptor makes to its checkout are detected, recorded and
    discarded.

  A todo without repos is reviewed in the goal project, as before, and has
  no merge. By default the acceptor runs without a sandbox, so it can build
  and test.
- **The `acceptor_blocked` gate.** When the acceptor cannot review, for
  example because tooling is broken, you resolve the gate with one option:
  `retry_acceptance`, `accept_manually`, `return_to_developer` or
  `cancel_todo`.
- **Dependency release and supersession (decision 37, #16).**
  - On a role_v1 goal, a dependency that requires acceptance releases its
    dependents only once it is accepted. Accepting a todo with repos merges
    it first.
  - To replace or split a todo, run
    `loopx todo supersede --goal-id G --todo-id OLD --by NEW[,NEW2]`. This
    rewires the dependents, and a superseded todo never counts as done.
  - `loopx todo list` explains why a deferred todo still waits
    (`dependency_waits`).

#### Git workspaces, merge and push (#1, #12, #19, #25)

- **Goal repos.** Declare a goal's repos with `loopx configure-goal --repo
  NAME=PATH[,default_branch=B][,merge_target=main|task_branch][,task_branch=B]`.
  A goal without a repo list keeps its legacy single `repo`, which acts as the
  repo `main`.
- **Workspaces.** `loopx workspace prepare | status | merge | cleanup`. A
  todo gets one worktree per selected repo, all on the branch
  `loopx/<goal>/<todo>`. A manual command selects the repos given with
  `--repo NAME`, else the todo's `task_repositories`, else every goal repo.
  The dispatcher prepares workspaces only for todos that name repos.
  - The merge is atomic across the todo's repos: if any repo fails, none is
    merged.
  - Each repo with changes gets a no-ff merge commit with `LoopX-Goal` and
    `LoopX-Todo` trailers.
  - Each repo's merge target is its default branch (`merge_target=main`, the
    default) or a task branch (`merge_target=task_branch`, named
    `loopx-task/<goal>` unless `task_branch` is set).
- **Delivery identity (decision 29, #12).** A multi-repo Turn settles against
  a todo workspace identity. A repo without `origin` gets a local `repo_id`,
  so it works without a fake remote.
- **Push gate (decision 38, #19).** Once all of a goal's work is merged and a
  merge target has goal commits that its remote lacks, one `push_request`
  gate lists them per repo. A repo without a remote is skipped, and a goal
  whose repos are all local opens no gate.
  - Approving it pushes each merge target. It never force-pushes.
  - The orchestrator or you can ask for the gate earlier with
    `loopx goal request-push`.
  - An optional `on_push_command`, such as `gh pr create --fill`, runs after
    each successful push.
- **Goal-complete gate (decision 42, #25).** A finished goal gets one
  deterministic `goal_complete` gate, with no model Turn. It shows the merges,
  the push results, the review counts and the cost. Its options are
  `close_goal`, `add_work` and `leave_open`.

#### Agent and provider configuration, Turn hosts (#3)

- **Provider config.** `<runtime-root>/providers.yaml` defines providers of
  kind `anthropic`, `openai`, `openai-compatible` or `codex-cpa`. Auth is
  `api_key`, `oauth_cli` or `oauth_token`. A provider names an env var, a
  keychain entry or a CLI login, and LoopX refuses secret values in the
  file.
- **Agent config.** Each agent has a file at `<runtime-root>/agents/<id>.yaml`.
  A project can override it field by field in `.loopx/agents/<id>.yaml`. The
  file sets the role, runtime, provider, model, effort, system prompt,
  permission or sandbox mode, `max_concurrency` and extra args.
- **Inspection and preflight.** `loopx agent list | show | validate` and
  `loopx provider list | check`. Before the first launch, the dispatcher runs
  an auth preflight for each agent.
- **The `claude-code` Turn host.** `loopx turn run-once --host claude-code`
  takes `--claude-model`, `--claude-permission-mode`, `--claude-effort`,
  `--claude-system-prompt-file`, `--claude-extra-arg`, `--claude-provider` and
  `--claude-bin`. Each Turn is a fresh `claude -p` session.
- **codex-cli config overrides.** codex-cli gains `--codex-config KEY=VALUE`,
  which is passed through as `-c`, and `--codex-provider NAME`, which expands
  a provider from `providers.yaml`. This is how you use CPA.

#### Usage and budget (#15, #22, #24)

- **Turn usage.** Every claude-code and codex-cli Turn records a `turn_usage`
  block: tokens, cost and host steps when the host reports them, and the
  duration. A Turn that times out or crashes without host accounting records
  its duration only. A crash before the ledger write is recorded when the
  Turn is replayed. The rows go to an append-only
  `<runtime-root>/goals/<goal>/usage.jsonl`.
- **Estimated pricing.** A provider can declare a `pricing` table, which
  estimates the cost of hosts that report no USD, such as Codex through CPA.
- **Reports.** `loopx usage report [--by role|agent|goal|todo|model|day]` shows
  agent-hours, cost and Turns. It also shows the cost per accepted todo, with
  and without the orchestrator's share. The status projection carries the same
  metrics as `turn_usage_summary`, over the latest 5,000 ledger rows.
- **Budgets (decision 41, #22).** Budgets are optional:
  `loopx usage budget --goal G --set USD`.
  - At 80% of the budget, LoopX opens a non-blocking alert.
  - At 100%, a `budget_exhausted` gate pauses the goal's new Turns, and
    running Turns finish. Resolve it with `raise_budget`,
    `continue_without_limit` or `stop_goal`.

#### Dashboard (#9 and later)

- **The role board tab (角色看板).** It opens with a "Waiting on you" list of
  gates and plan approvals. Below that, columns Planned, Assigned, Running,
  In review, Rework and Done are split into orchestrator, developer and
  acceptor swimlanes.
- **Cards.** A card shows its agent, repos, reject count, acceptor,
  acceptance criteria, why it waits on dependencies, and its plan link.
- **The usage strip.** It shows cost, agent-hours, cost per accepted todo and
  the budget share.
- **The gate drawer.** It shows the discussion thread and reply box. While
  it is open and the page is visible, it re-reads the gate every 5 s and on
  focus, so a reply or decision made from the CLI shows up without reopening
  it. Above the thread, a card per gate kind shows what you are deciding:
  - a plan approval shows the plan card, with each todo's contract, and its
    criteria changes, old and new side by side;
  - a push request shows each repo's branch, remote and commits;
  - a budget gate shows the spend against the budget and the default raise;
  - a goal-complete gate shows the review counts, merges, push results and
    cost.

  Decisions are enabled only after a successful read of the gate the drawer
  shows, and only while that gate is open. A failed refresh disables them
  until the next successful read. A part that cannot be read is shown as
  unavailable, never as empty. The role board shows each gate's real kind,
  and `loopx gate show` prints the same plan card.

### Changed

These behaviors differ from upstream.

- **Runtime model.** New goals, and goals that record no model, run
  `role_v1`. Goals that already record `peer_v1` keep the flat peer behavior
  until you run `loopx configure-goal --agent-model role_v1`.
- **Anti-hierarchy validators removed.** `agent_profiles.*.profile_role`
  accepts labels such as `orchestrator`, `manager` and `worker`. On role_v1
  goals, the legacy hierarchy detector no longer treats an
  `agent_profiles.*.role` key as a migration trigger. Its other markers are
  unchanged.
- **Completing a todo.** On a role_v1 goal that has registered roles,
  `loopx todo complete` of a todo that requires acceptance (see the
  `in_review` status above) delivers it to `in_review` instead of marking it
  done. The event log gains `todo_in_review` and `todo_reopened`.
- **Planning obligations.** role_v1 goals no longer derive these upstream
  planning obligations:
  - the vision checkpoint, and the replan for a todo done with no follow-up
    (decision 31, #18);
  - the projection repair for an idle orchestrator, and the periodic-review
    replan (decision 39, #21);
  - a demand from a stale executable Next Action (decision 42, #25).

  Planning review is the orchestrator's job on every Turn instead.
- **Dependency release.** On role_v1 goals, a dependency that requires
  acceptance releases its dependents only once it is accepted (for a todo
  with repos, accepted and merged). A manual `done` or a supersede does not
  count. A dependency without
  acceptance is still satisfied by `done`.
- **Who may write what.** Under role_v1, agents with the developer or
  acceptor role cannot open user gates or write acceptance criteria. After
  planning, a change to a todo's criteria needs a user-approved plan card.
  Only the owner, using no `--agent-id`, may still edit criteria directly.
- **Agent Turns cannot decide user gates.** Every Turn host sets
  `LOOPX_AGENT_TURN=<agent id>` in the environment of the model process it
  starts. With it set, `loopx gate resolve` and
  `loopx todo complete --role user --decision-outcome` refuse to record a
  decision on a user gate, with `gate_decision_refused_in_agent_turn`. The
  orchestrator replies with `loopx gate reply` instead, and only as itself: a
  reply as the owner (the default `--as user`) or with another `--agent-id` is
  refused with `gate_reply_identity_mismatch`. This guards against
  accidental self-approval and is not a security boundary: an agent that
  unsets the variable bypasses it. `issue-fix pr-gate-reconcile --execute`,
  which closes the gate of a merged or closed PR as `system`, is refused too
  when it runs inside an agent Turn and would close a gate.
- **Gate decisions are audited.** A user-gate decision records `closed_by`
  in the gate index, with the surface (`cli`, `dashboard` or `system`), the
  actor (the agent named with `--agent-id`, else `owner`), the agent Turn if
  any, and the time. `loopx gate show` shows it. It
  is written after the decision itself; if the process dies in between, a
  replay of the same decision writes it, marked `replayed: true`.
- **The recorded gate decision wins.** Settlement always runs on the
  decision recorded on the gate, never on the caller's. A
  `todo complete --decision-outcome` replay that names another decision is
  refused with `gate_already_decided` and writes nothing. A replay of the
  recorded decision is idempotent. `gate resolve` on a closed gate still fails
  with `gate_closed`.
- **Turn prompts.** Both built-in hosts now ask for repo-relative paths in
  results, and carry role guidance, acceptance criteria and review feedback.
- **codex-cli errors (N9, #24).** An upstream 5xx, `auth_unavailable`, a
  refused connection or a failed upstream dial is now a retryable
  `provider_capacity` error, so the provider backs off, not the todo. A failed
  Turn also keeps a redacted stderr tail.
- **Verdict feedback (N6, #24).** The summary of a review Turn may be up to
  2,000 characters. `review_feedback` keeps its 600-character contract, and
  the text is fitted with the failed criteria first.

### Fixed

- **Pilot v0 fixes (#10).**
  - The orchestrator's LoopX commands are pinned, and its plan todo is
    validated.
  - A gate that awaits the orchestrator no longer blocks its lane.
  - Validation of a `task_repositories` todo runs in the todo's workspace,
    and fails closed when that workspace is missing.
  - The resolved acceptor can start a delivered review.
  - A rejected delivery's feedback reaches its developer.
  - Accept of a todo with repos merges before it completes the todo.
  - The dispatcher never launches an orchestrator Turn that has no todo.
- **Settlement retries (#17).** A Turn keeps its identity when its settlement
  retries run out, and resumes from its workspace only while that workspace
  still exists.
- **Pilot v1 regressions (#23).**
  - A deferred todo is never launched, and no workspace is cut for it.
  - An orchestrator action todo whose Turns keep failing is retired.
  - The action and budget counters survive a dispatcher restart.
- **Pilot v1 gaps (#24).**
  - N3: the status `todo_index` keeps the goal-state status, and superseded
    cards leave the board.
  - N5: accepted todos are counted by their accept record, and the
    orchestrator's spend is split out.
  - N8: the plan gate text refreshes on `--revise`.
  - N10: an untouched todo branch is fast-forwarded when the todo is
    released.
  - N11, in part: the orchestrator can supersede without the claim owner's
    `--agent-id`.
- **UTF-8.** Git subprocess output is decoded as UTF-8 (#5), and so is the
  keychain launch-env subprocess (#3).
- **Hermetic tests (#29).** Multi-agent delivery tests settle from a private
  linked worktree, so they pass from any checkout.
- **Tests no longer touch the real runtime root.** 23 test modules read or
  wrote `~/.codex/loopx`, leaving goals such as `example-goal` and a
  `repository-change-window/` directory behind. They now use temporary
  runtime roots. A test-session guard refuses audited file operations (open,
  mkdir, remove, rename, listdir and similar) under the real runtime root and
  names the offending test. It covers the test process and Python
  subprocesses that inherit its `PYTHONPATH` and guard environment. It does
  not see stat, lstat, access or readlink calls (so not `Path.exists()`),
  `dir_fd`-relative paths, access through an unpinned symlink alias, Python
  subprocesses that drop that environment, or non-Python subprocesses.
- **launchd dispatcher code.** `loopx dispatch launchd-plist` set only
  `PATH` and `HOME`. With the checkout's `.venv` as the Python, a resident
  dispatcher therefore ran the checkout's current code instead of the
  installed snapshot. The plist now pins `PYTHONPATH` to the LoopX source
  that rendered it. Render the plist again after an upgrade.
- **Owner writes on unclaimed todos.** A plan todo without a `bound_agent`
  has no claim owner, because Turns do not claim todos. Two owner actions on
  such a todo failed, because the owner is not a registered agent:
  - the `accept_manually` option of an `acceptor_blocked` gate. It merged
    the delivery and closed the gate, then stopped with
    `agent_id='owner' is not registered` and left the todo `in_review`;
  - `loopx todo supersede --by` without `--agent-id`.

  An owner write is now attributed to the todo's claim owner, else a
  registered fallback (the blocked acceptor, or the agent that proposed the
  plan), else the orchestrator. Both actions now complete.
- **Push gate and deferred todos.** The automatic `push_request` gate
  counted `open`, `in_review` and `blocked` todos as pending work, but not
  `deferred` ones. A goal whose plan dependents were not released yet could
  therefore be offered for push while that work still waited. A deferred todo
  now keeps the automatic gate closed, as it already did for the
  `goal_complete` gate. An explicit `loopx goal request-push` is unchanged.
- **Plan apply before approval.** `loopx plan apply` applied a plan whose
  `plan_approval` gate was still open. It also applied a plan whose gate was
  recorded `reject`, when the card was not settled yet. A pending or
  applying plan now applies only when its gate is recorded done with the
  decision `approve`. Otherwise the apply fails with `plan_not_approved` and
  writes nothing. A plan that was already rejected or cancelled still fails
  with `plan_not_applicable`. Recovering an interrupted apply is unchanged.
- **Dashboard and CLI deciding the same gate.** A gate can now be decided
  from the dashboard and the CLI in any order:
  - A dashboard decision whose gate another surface closed first is reported
    `stale` and changes nothing, even when the decision matches. Before, it
    could report an option you did not choose.
  - The typed gates (`budget_exhausted`, `goal_complete` and
    `acceptor_blocked`) settle one at a time per gate. Before the effect
    runs, the settlement records the chosen option and its inputs, such as
    the note or the budget raise target. Once that record exists, a retry
    after a crash repeats the first choice with the same inputs, and a
    default budget raise is never applied twice.
  - A settlement that did not finish is no longer reported as applied. The
    dashboard marks the decision `failed` with a typed code, and each code
    says how to recover:
    - `gate_settlement_retry_required`: a failed effect, a failed write or a
      lock timeout. Retry the decision.
    - `plan_apply_recovery_required`: an interrupted plan apply. Retry the
      decision, or run the `loopx plan apply` command it names.
    - `gate_push_failed`: a failed push. The failure is final for that gate;
      approve the follow-up push gate that LoopX opens instead.

### Known limitations

- **`hard_lease` goals.** `in_review` is not supported on canonical
  `hard_lease` goals (decision 33). The default `soft_claim` works.
- **Event-projected todos.** `todo update` does not support event-projected
  todos, which are neither Markdown nor canonical. Delivery and verdicts
  therefore need a Markdown or promoted canonical goal.
- **Role board.** On canonical-authority goals, `running` reflects dispatcher
  Turns only. The todo list inherits the status `todo_index` cap of 240
  items across all goals. Verdict text is not shown on the board.
- **codex-cli orchestrators.** The orchestrator's system-prompt addendum and
  state digest only reach claude-code agents. A codex-cli orchestrator gets
  neither.
- **PyYAML.** PyYAML ships only in the `test` extra, and agent and provider
  config files need it. Install with `uv sync --extra test`, or run
  `pip install pyyaml`.
- **Dashboard from source.** A source checkout without the local install
  needs `npm run build:chat` before `loopx dashboard` can serve the chat
  bundle. `scripts/install-local.sh` builds it into the release.
- **Budgets.** A budget pause never kills running Turns, so spend can end up
  above the budget.
- **Usage recording.** The generic-cli and dsh hosts record no usage. A
  codex-cli Turn that times out before `turn.completed` records its duration
  only.
- **Gate authority.** Agent Turns cannot decide user gates, but this is a
  guardrail, not a security boundary: agents run as your OS user, and an
  agent that unsets `LOOPX_AGENT_TURN` can still decide a gate. `closed_by`
  records who decided each gate. A DSH Turn run through an explicit
  `--dsh-runner` runs in process and does not carry the marker.
- **CLI crash while closing a typed gate.** The CLI closes the gate before
  its settlement records the chosen option. If the process dies between the
  two, the option is not recorded yet, and a retry settles with the option
  that the retry passes.
- **Typed gates with unreadable options.** When the dashboard cannot read a
  typed gate's option list, it disables that gate's decisions. Decide it with
  `loopx gate resolve --option ...` instead.
- **Orchestrator ergonomics.** The orchestrator cannot run
  `todo add --agent-id` for agent todos (N11), so it omits `--agent-id`. Its
  clarification questions may arrive in the plan gate thread instead of a
  separate question gate (N7).
- **Not built yet.**
  - remote-SSH workspaces for AOSP-scale trees;
  - a leasable resource pool;
  - Lark and Telegram channels;
  - the dashboard intake form;
  - accept and reject actions on role board cards.

  Several design docs under `docs/fork/` list further known gaps.

### Documentation

- [docs/fork/usage.md](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/usage.md): a new usage guide for the product
  workflow and the CLI.
- Fork design and protocol docs:
  - [design-v0](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/design-v0.md), which records decisions 1 to 43;
  - [role-v1-protocol](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/role-v1-protocol.md);
  - [dispatcher-v0](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/dispatcher-v0.md);
  - [gates-plans-intake-v0](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/gates-plans-intake-v0.md);
  - [workspaces-v0](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/workspaces-v0.md);
  - [agent-and-provider-config](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/agent-and-provider-config.md);
  - [usage-accounting-v0](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/usage-accounting-v0.md);
  - [role-board-v0](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/role-board-v0.md).
- End-to-end pilot reports: [v0](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/e2e-pilot-report-v0.md) and
  [v1](https://github.com/foolzzz/looper/blob/a93a851d61530f9feb7e8c262d6a16e07ebe547d/docs/fork/e2e-pilot-report-v1.md).
