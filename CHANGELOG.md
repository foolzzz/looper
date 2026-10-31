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

## [2.14.49] - 2026-10-31

### Added

- **Keeper switch and single control-plane lock.** The ops file takes `[agent_host] keeper = "off" | "on"` (default `off`; file only). With `on`, `looper serve` takes a PostgreSQL session advisory lock on a dedicated connection before it opens the database pool, refuses to start while another process holds it (the error names the holder), and increments the `control_plane_epoch` key in `system_settings` once the schema is ready. A lost lock is terminal: the server fences its database pool and new agent spawns, closes its listener, ends its own agents and exits with code 75 without touching run records. With `off`, none of this happens. ([#1012](https://github.com/foolzzz/looper/pull/1012))

### Changed

- **Agent process bookkeeping in `proc_tree`, with keeper off too.** `spawn` now tracks in-flight spawns and every owned process, and ending an orphaned agent's tree kills only survivors whose identity and start time it confirmed, one by one, instead of signalling the whole group. ([#1012](https://github.com/foolzzz/looper/pull/1012))

## [2.14.48] - 2026-10-31

### Added

- **Keeper process that holds one run's child.** The new package `looper/agents/keeper/` starts a child in its own session, appends its stdout and stderr to files, keeps its state in `keeper.json`, and ends the child on SIGTERM, after the orphan timeout without a controller, or when stdout passes its cap. `proc_tree.own_records` lets a process that is not a server own the agents it spawns. Nothing in Looper starts the keeper yet. ([#1011](https://github.com/foolzzz/looper/pull/1011))

## [2.14.47] - 2026-10-31

### Changed

- **Telegram handler retry, backlog cap and restored progress route.** A Telegram handler that raised before it reached the bus is retried up to two more times (about 3 s at most, never after publishing, not during shutdown) before the update is left pending. The Telegram channel config schema and the config export accept `backlog_max_age_seconds`. Task progress for a chat restored from `reply_route` is sent without caching the correlation. ([#994](https://github.com/foolzzz/looper/pull/994))
- **Dashboard command run history and schedule alert follow-ups.** The failures badge follows each schedule's own threshold, the overlap alert is warning-coloured, a second run link reopens its row, the log preview shows the last 200 lines of logs over 1 MiB, succeeded command runs show their exit code, a run with no log shows a note (the log API says the command did not start), skipped runs show no duration, and the command schedule form's hints, alert section, field errors and ownership line fit command schedules. ([#1009](https://github.com/foolzzz/looper/pull/1009))

## [2.14.46] - 2026-10-31

### Changed

- **Command schedule confirmations and client-token API text.** The Manager's enable and run-now confirmations for a command schedule show the command in a verbatim code fence, the directory, the run timeout, the environment keys only, the env file when set, and (for enable) the success deadline and alert thresholds, and say the run uses no agent or model spend. Every command schedule update reply says so too, and the command appears in a code fence in the configuration block and in before → after diffs. The `looper_schedules` tool description no longer says an enabled schedule's runs "start tasks". The client-token API's enable and run confirmations name the shell command and say no task or model spend follows, and the confirmed update or run is bound to the configuration version that was shown. Listed runs and the run-now result carry the schedule `kind`. Agent schedule text is unchanged. ([#1005](https://github.com/foolzzz/looper/pull/1005))

## [2.14.45] - 2026-10-31

### Added

- **Command run history in the dashboard.** A command schedule's run history shows each run's status with exit code or signal, times, duration and trigger; a run expands to its last 200 log lines with a full-log download; 24-hour and 7-day statistics and a status filter sit above it; `#projects/<project>/schedules/<schedule>/runs/<run>` opens one run; "Failing now" shows the missed-deadline and overlap alerts. ([#1000](https://github.com/foolzzz/looper/pull/1000))

## [2.14.44] - 2026-10-31

### Fixed

- **Planner keeps the reason of a failed run.** The step activity, the work report, the review report and the closing-note outcome line keep the end of a failed Worker or Reviewer run's error instead of clipping it, and the verify path reads the run's `error` when a Reviewer run ended without a verdict. ([#996](https://github.com/foolzzz/looper/pull/996))

## [2.14.43] - 2026-10-31

### Fixed

- **Command schedule runtime.** A due schedule is admitted by its kind after it is locked, so a command launch no longer blocks a drain and no tick under way can publish an agent task after the drain begins. Housekeeping runs retention, run pruning, notification pruning and each log rotation as independent steps recorded in `failed_steps`. Run pruning keeps an unfinished run whose gate output has not been handed on. The command runner retries opening its log once when the log directory vanished. Deleting a command schedule with a running run is refused with a 409. ([#993](https://github.com/foolzzz/looper/pull/993))

## [2.14.42] - 2026-10-31

### Added

- **The Manager's `looper_schedules` tool creates, modifies and views command schedules.** It gains `kind`, `command`, `env`, `env_file`, `success_deadline` and three alert thresholds. For a command schedule the configuration, diff and the enable and run-now confirmations show the command verbatim and say it uses no model quota; the list and run summaries include `kind` and `exit_code`. A confirmed enable, disable or run-now is bound to the schedule id and configuration version it showed, and is refused with a 409 if the settings changed. ([#981](https://github.com/foolzzz/looper/pull/981))

## [2.14.41] - 2026-10-31

### Fixed

- **`looper service status` and the install and restart reports use the address the unit listens on.** With `server.host = "::"` the status probe dials `[::1]` and the reports print `http://[::1]:<port>` instead of `http://127.0.0.1:<port>`. When the ops file cannot be read, status falls back to `127.0.0.1`. ([#992](https://github.com/foolzzz/looper/pull/992))
- **Linux probe units stay consistent.** Reinstalling the probe stops a running probe first; uninstall removes the server unit even when probe cleanup fails (and then reports the failure, so it can be run again); uninstall stops the probe units by name when their files are already gone; and a failed probe update during install or restart restores the previous probe files instead of deleting them. ([#992](https://github.com/foolzzz/looper/pull/992))

## [2.14.40] - 2026-10-31

### Added

- **The dashboard can create and edit command schedules.** The form on a project's Schedules tab has a "Type" selector (Agent task or Command) that is chosen only when creating and is read-only afterwards. A command schedule shows the command (up to 2000 characters), the work directory, `env` (KEY=VALUE lines or a JSON object), `env_file`, the timeout, overlap, catch-up, notifications, the success deadline and the three alert thresholds, and hides the agent-only fields (requirement, template, gate, budgets, profiles, task limits). The schedule list and detail view show a "Command" badge. ([#979](https://github.com/foolzzz/looper/pull/979))

### Fixed

- **Config import skips a command schedule that carries a requirement.** An entry of kind `command` whose `requirement` is non-empty is skipped with the reason "requirement does not apply to a command schedule"; `""` is stored only when the field is absent, null or blank. ([#995](https://github.com/foolzzz/looper/pull/995))
- **A run's recorded profile omits `runtime.launcher` unless it is `ttadk`.** The run record (`profile.runtime`) no longer lists `"launcher": "native"`. ([#995](https://github.com/foolzzz/looper/pull/995))

No model-visible change.

## [2.14.39] - 2026-10-31

### Fixed

- **`loopercli schedule-log --tail N` prints exactly the last N lines.** The client asks the server for the tail with the largest page (4 MiB); when that page may begin mid-line it reads earlier byte ranges with `offset` and `limit_bytes` until it holds N whole lines or reaches the start of the file. The server API (`schedule_admin.run_log`) is unchanged. ([#991](https://github.com/foolzzz/looper/pull/991))

No model-visible change.

## [2.14.38] - 2026-10-31

### Fixed

- **Telegram messages sent during a long upgrade gap are delivered.** The service saves when Telegram polling stopped (on SIGINT or SIGTERM, through a new `prepare_stop` channel hook), and a message sent from then until polling resumed is no longer ignored as older than `backlog_max_age_seconds`; older messages are still ignored and `update_id` deduplication is unchanged. Polling responses are saved before they are acknowledged, and the stop time is retired after a response with fewer than 100 updates. The inbox database gains a `polling` table. ([#975](https://github.com/foolzzz/looper/pull/975))

No model-visible change.

## [2.14.37] - 2026-10-31

### Changed

- **Housekeeping keeps going after a failed step; the retention setting is in the dashboard and the Manager tool.** The hourly pass runs its five steps (runs and notifications, command logs, run directories, runner files, log rotation) independently: a failure is logged as a warning, recorded in the report's `failed_steps` and in the closing log line, and no longer skips log rotation. Settings → Scheduler gains "Run retention (days)" (1 to 3650), and the Manager settings tool's `scheduler` description and empty-argument message name `run_retention_days`. ([#983](https://github.com/foolzzz/looper/pull/983))

Model-visible change: the Manager settings tool's `scheduler` description now mentions `run_retention_days`; no prompt or skill text changes.

## [2.14.36] - 2026-10-31

### Added

- **`looper service upgrade` executes its plan; `looper service restart --drain`; Telegram updates survive a restart.** The upgrade installs for a NOOP plan, installs and atomically replaces the served dashboard for WEB_ONLY, and for FULL drains, stops, installs, restarts through the service manager and verifies `system.components`; a failed install restarts the old service and cancels the drain. `--timeout`, `--drain-timeout` and `--no-force` are new, a deploy report is printed and appended to the service log, and a drain started through the system API now needs `drain_status` polled at least every 30 seconds. The runtime fingerprint covers `pyproject.toml` dependencies and `uv.lock`, and a built dashboard records its source fingerprint. Telegram polling no longer drops pending updates: they are saved before acknowledgement, replayed after a restart and deduplicated by `update_id`; updates older than `backlog_max_age_seconds` (default 600) are ignored. ([#949](https://github.com/foolzzz/looper/pull/949))
- **Command schedules in the schedule API, with `schedule_admin.run_log`.** Create takes `kind` (`agent` or `command`; it cannot change later) and validates each kind separately; create and update take `env`, `env_file`, `success_deadline` and the three alert thresholds; the schedule view returns them with the effective `alert_policy`. Runs carry the command columns (`started_at`, `exit_code`, `exit_signal`, `duration_seconds`, `log`, `log_tail`), the run list filters by `status` and returns 24-hour and 7-day statistics, and the new read-only `schedule_admin.run_log` reads a run's log in byte pages. ([#960](https://github.com/foolzzz/looper/pull/960))
- **Command schedule fields in config export and import.** Schedule entries carry `kind`, `command`, `env`, `env_file`, `success_deadline` and the alert thresholds (written for an agent schedule only when not default, so its export is unchanged). Import validates by kind, still disables the schedules, skips a same-name schedule of the other kind, and imports a schedule whose `env_file` is missing on this machine with a note. ([#980](https://github.com/foolzzz/looper/pull/980))

### Changed

- **Run history and logs are bounded.** The scheduler runs a housekeeping pass at startup and then hourly. New setting `scheduler.run_retention_days` (default 30, 1 to 3650) deletes finished runs and notifications older than that, keeping each schedule's newest 100 runs and every open run; command logs go by age and a 2 GiB cap; `serve.log` and `probe.log` rotate at 20 MiB, keeping 5 copies. Agent schedules no longer keep their run history forever, and the first pass runs at the first start after the upgrade. The setting is not yet in the dashboard's Settings page or the Manager settings tool's description. ([#964](https://github.com/foolzzz/looper/pull/964))

Model-visible change: no prompt or skill text changes. The Manager settings tool accepts and shows `run_retention_days` without a description update, and Telegram messages sent during a restart now reach the Manager instead of being dropped.

## [2.14.35] - 2026-10-31

### Added

- **Per-schedule alert thresholds, two new alerts and command run messages.** `Notifier` now reads each schedule's `alert_policy`: `alert_failures_after`, `alert_stale_after_seconds` and `alert_overlap_after` (a null keeps the old default: 3 failures, the period-based stale window, and 10 skips for a command schedule, none for an agent schedule). A schedule's `success_deadline` (`HH:MM` in its time zone) alerts once when a deadline passes with a fire in the window and no successful run, and recovers with the next OK run; consecutive overlap skips alert once at the threshold and recover when a new run starts. A command run's message carries its exit code or signal, duration, log tail and a link to the run. The overview's "Failing now" lists the two new alerts (`deadline_since`, `overlap_since`). No migration: the columns were added earlier. ([#963](https://github.com/foolzzz/looper/pull/963))

Model-visible change: none for prompts or tools; the new alert and command run messages go to the schedule's notification channel, not to a model.

## [2.14.34] - 2026-10-31

### Fixed

- **A failed `ps` no longer kills live command runs, and queued catch-up fires keep `missed_since`.** A non-zero, timed-out, failed or empty `ps` is now an unknown observation: the run is left alone and retried on the next tick. A runner whose recorded process is confirmed gone is lost at once instead of after the 60-second start grace. A catch-up fire that waits in the overlap queue keeps its `missed_since` (new nullable column `schedule_runs.missed_since`, added by one additive migration), so `LOOPER_SCHEDULE_MISSED_SINCE` is set when it starts. ([#958](https://github.com/foolzzz/looper/pull/958))
- **The probe service install checks the port the service really uses and keeps its unit.** Install uses an explicit `--port`, else the installed unit's explicit port, else the ops file's; the readiness wait connects to the configured host (`::` through `::1`). A failed reinstall no longer deletes the probe unit first; `looper service restart` recreates a missing or stale probe unit when `[probe]` is configured; uninstall stops the probe before removing units and does not fail when none exists. ([#961](https://github.com/foolzzz/looper/pull/961))

No model-visible change: no prompt, tool or message text given to a model changes.

## [2.14.33] - 2026-10-31

### Fixed

- **Command schedules keep firing while the service drains for an upgrade.** During a planned-upgrade drain, only agent schedules are paused: command schedules fire, start their queued runs and run now as usual, and their starts do not count toward the `starting` the drain waits for. Run now on an agent schedule during a drain returns 409 "the service is draining for an upgrade: run it after the restart" instead of 404; an unknown schedule still returns 404. ([#962](https://github.com/foolzzz/looper/pull/962))

No model-visible change: no prompt, tool or message text given to a model changes.

## [2.14.32] - 2026-10-31

### Fixed

- **A TTADK run that ends without a result now says why, and the dashboard names the launcher.** When a Worker, Planner or Manager turn through the TTADK launcher ends without a result, its error (and, for Worker and Planner, its activity entry) keeps the last 1000 characters of the process's stderr, with credentials redacted. A per-turn process now carries that stderr on its turn result. The Agent Profiles page shows a TTADK runtime as `<name> (launcher: ttadk)`, and the dashboard's `Runtime` type gains the optional `launcher` field. Native runtimes keep their error text. ([#947](https://github.com/foolzzz/looper/pull/947))

No model-visible change: no prompt, tool or message text given to a model changes.

## [2.14.31] - 2026-10-31

### Added

- **The scheduler runs command schedules and adopts their runs after a restart.** A schedule of kind `command` starts a standalone runner (`looper/agents/command_runs.py`), skips or queues overlapping firings without merging skips, settles runs as `succeeded`, `failed`, `timed_out` or `interrupted`, adopts a live runner after a server restart, kills a run that passes its timeout by 90 seconds, and gets `LOOPER_SCHEDULE_TRIGGER`, `LOOPER_SCHEDULE_MISSED` and `LOOPER_SCHEDULE_MISSED_SINCE` (gates get them too). The runner's output tail also redacts secrets that cross its cut, and a failed serve start now stops the HTTP server. Nothing creates a command schedule yet. ([#944](https://github.com/foolzzz/looper/pull/944))

No model-visible change: no prompt, tool or message text changes.

## [2.14.30] - 2026-10-31

### Added

- **`looper service install` installs the health probe.** With a `[probe]` chat in the ops file, install registers a unit that runs `looper service probe` every 60 seconds (a LaunchAgent `<label>.probe` on macOS, `<label>-probe.service` and a timer on Linux), carrying only path, locale, time zone and network settings. Without it install removes any probe unit; uninstall removes it, restart re-aligns its port, and status shows it. `looper service probe` gains `--port`, checks the configured `server.host`, and picks the `lark_bot` channel by `probe.lark_profile`, using it only when exactly one matches. ([#952](https://github.com/foolzzz/looper/pull/952))

No model-visible change: no prompt, tool or message text changes.

## [2.14.29] - 2026-10-31

### Fixed

- **Task progress updates stay routable after a drain restart.** A Telegram or Lark task now stores its original chat (and for Lark the original message id) as `tasks.origin_reply_route`, added by the one-time migration `task_reply_route`; task reports and the Manager's progress messages carry it, so a replay after a restart reaches the same chat, and the Lark channel replies to the original message. Manager message handling waits until the HTTP tools endpoint listens, so messages replayed at startup stay queued. Tasks created before the upgrade keep the old behavior. ([#950](https://github.com/foolzzz/looper/pull/950))

No model-visible change: no prompt, tool or message text changes.

## [2.14.28] - 2026-10-31

### Added

- **A drain state for planned restarts.** `system.drain`, `system.drain_status` and `system.drain_cancel` (operator only, POST) hold new Worker and Reviewer runs, Manager turns, schedule fires, consults and wait checks without losing work; Planner turns and `TASK_START` are not held. The drain is in `system.status` and the dashboard shows a banner. Nothing drains unless an operator asks. A Manager answer's reply route is kept in the journaled message so a replayed answer reaches its chat. ([#932](https://github.com/foolzzz/looper/pull/932))
- **Component fingerprints and an upgrade plan.** `looper/components.toml` declares the components; `POST /api/v1/system/components` serves the loaded fingerprints; `looper service upgrade --dry-run [--to REF_OR_WHEEL]` prints a `NOOP`, `WEB_ONLY`, `CONTROL_PLANE` or `FULL` plan without installing or restarting anything. ([#935](https://github.com/foolzzz/looper/pull/935))
- **Codex through TTADK.** Runtimes have a typed `launcher` (`native` or `ttadk`) stored in the new column `agent_runtimes.launcher`; the built-in runtime `ttadk-codex` is registered when `ttadk` is on PATH and launches Codex for every role and the consult with login providers only. TTADK problems refuse the launch with a typed cause; runs record the launcher. Native Codex is unchanged. ([#940](https://github.com/foolzzz/looper/pull/940))
- **A service health probe.** `system.health` returns the scheduler's latest tick and latest healthy tick; the ops file takes an optional `[probe]` section and `looper service probe` runs one check and sends a Lark alert and a recovery message. The unit that runs it periodically is not installed yet. ([#939](https://github.com/foolzzz/looper/pull/939))
- **Groundwork for command schedules.** The RFC `docs/architecture/rfcs/command-schedules-v0.zh-CN.md` (Chinese) is published and D3 of the scheduled tasks RFC is revised ([#936](https://github.com/foolzzz/looper/pull/936)); `looper/agents/command_runner.py` is a stdlib-only runner that nothing calls yet ([#938](https://github.com/foolzzz/looper/pull/938)); the one-time migration `command_schedules` adds the columns and statuses to `schedules`, `schedule_runs` and `schedule_notifications`, and `succeeded` counts as a success and `interrupted` as a failure ([#941](https://github.com/foolzzz/looper/pull/941)). Command schedules are not usable yet.

No other model-visible change: no prompt, tool or message text changes.

## [2.14.27] - 2026-10-30

### Changed

- **The hot upgrade v0 RFC is corrected after its review.** `docs/architecture/rfcs/hot-upgrade-v0.zh-CN.md` (Chinese) now separates the control plane's loaded fingerprint from the served web fingerprint, stops the old control plane before installing after a drain, defines what a control plane does when it loses the database lock, keeps a Manager's bus message unacknowledged while a drain holds its turn, keeps keepers out of the startup sweep, and uses the v1 API conventions (issue #929). Documentation only; no runtime or model-visible change. ([#931](https://github.com/foolzzz/looper/pull/931))

## [2.14.26] - 2026-10-30

### Added

- **The hot upgrade v0 RFC.** `docs/architecture/rfcs/hot-upgrade-v0.zh-CN.md` (Chinese) designs planned restarts that do not interrupt runs and upgrade only the changed components, and the development plan's open todos record its stages (S0 in progress; S1, S2, S3 to do). Documentation only; no runtime or model-visible change. ([#928](https://github.com/foolzzz/looper/pull/928))

## [2.14.25] - 2026-10-30

### Fixed

- **The installed Pi smoke covers the Pi runner default as well as the CLI opt-out.** `examples/pi/isolated-smoke.py` gains `--pi-runtime cli|runner` (default `cli`) and writes the chosen value to `[runtimes] pi`; `tests/test_pi_smoke.py` runs the installed four-role fixture in both modes. Example and test files only; no runtime or model-visible change. ([#926](https://github.com/foolzzz/looper/pull/926))

## [2.14.24] - 2026-10-30

### Fixed

- **Two installed test fixtures match the runner default and the project delete cascade.** The installed Pi smoke now pins `[runtimes] pi = "cli"` because its evidence exists only on the CLI path, and the `platform` fixture of `tests/test_pi_platform_tools.py` stubs `schedules.list_schedules` and `tasks.project_tasks`, which the delete cascade now calls. Test and example files only; no runtime or model-visible change. ([#924](https://github.com/foolzzz/looper/pull/924))

## [2.14.23] - 2026-10-30

### Fixed

- **Runner startup failures no longer stop `looper serve` and name their cause.** Pi and DSH default to their SDK runners, so a runner adapter that cannot be built (an unsupported runtime or a bad path) now becomes an "unavailable" adapter carrying the reason and the `[runtimes] <kind> = "cli"` opt-out, instead of an exception at startup. A failing `node --version` check reports its own error rather than a timeout message, and Node 23.0 to 23.5, which cannot run the runner, is rejected up front. After repeated runner failures, the error now includes the last failure, and a runner that exits reports its cause. A cold start cancelled while its handshake succeeds is retired without using the crash budget (five failures in ten minutes). The architecture document and the ops-file comments now describe the runner default, 32 session slots per runner and up to four runners per kind. No model-visible change. ([#920](https://github.com/foolzzz/looper/pull/920))

## [2.14.22] - 2026-10-30

### Fixed

- **A Planner's empty reply gets the same repair turn as an invalid one.** A turn that succeeded with an empty reply used to fail the task at once with "Planning failed: RUNTIME returned an empty reply". The turn is still recorded with that error, but the reply now goes to the contract parser, which rejects it as "the reply was empty" and so triggers the single repair turn; other turn failures are unchanged. The Planner's start activity now names the runtime that runs it instead of always "Claude Code". **Model-visible:** after an empty reply the Planner receives the repair turn instead of the task failing. ([#919](https://github.com/foolzzz/looper/pull/919))

## [2.14.21] - 2026-10-30

### Changed

- **Pi and DSH profiles run on their SDK runners by default.** `[runtimes] pi` and `[runtimes] dsh` default to `"runner"` and are read once at startup. An explicit `"cli"` is the opt-out: set `[runtimes] pi = "cli"` or `[runtimes] dsh = "cli"` in the ops file and restart. Claude Code stays on the Claude Code CLI. Each runner allows 32 sessions; the supervisor starts another runner of that kind when all members are full, up to four members (128 sessions per kind). Only a full pool fails with `CAPACITY`. Extra runners stop after 300 idle seconds; one stays. If the runner cannot start because Node is unsupported (22.19+ required), the SDK is outside the gate (pi 1.3.x, dsh 0.2.1-rc.2), or the executable is missing, the launch fails. The message names the cause and the same opt-out, including failures on the first lease after startup or when starting another pool member. `evals.prompt_regression` and `examples/dsh/validate.py` use the runner when no flag is set; `--pi-cli` and `--dsh-cli` select the CLI path, and `--pi-runner` / `--dsh-runner` still select the runner. **Model-visible:** a Pi or DSH role runs in the SDK runner, so its platform tools are `looper_*` in process and the run record stores a runner Descriptor. A Claude Code profile is unchanged. ([#899](https://github.com/foolzzz/looper/pull/899))

## [2.14.20] - 2026-10-30

### Fixed

- **The DSH runner retries transient provider errors, each attempt authorized.** A provider error such as HTTP 429/5xx or a DeepSeek overload no longer fails the turn at once. Inside the runner's `llm/stream` hook, a request that failed with a retryable status or SDK error code and produced no model output is retried up to 3 times, with 1/2/4 second backoff plus up to 20% jitter and a `Retry-After` hint capped at 30 seconds. Every attempt gets its own request id, `budget/authorize` call and accounting; cancel, close or settle ends the wait. When retries are exhausted the last error is kept with an ` (after N attempts)` suffix. No schema, API or prompt change. ([#915](https://github.com/foolzzz/looper/pull/915))

## [2.14.19] - 2026-10-30

### Fixed

- **The project delete closes three races left by the cascade.** A Restart or Start over of a task during the delete no longer starts a Planner on a deleted task: the restart runs inside the same per-project barrier as a ready start, returns while the project is being deleted, and stops when the task was not reset. The delete skips a task that moved to another project after its snapshot, keeping its checkout and record. A Manager template attached after the preflight is reported as a template conflict instead of "a task or schedule was added". A schedule run whose gate finished while the project was being deleted now ends as `gate_failed` instead of staying `gating` until a server restart. No schema, API or model-visible change. ([#912](https://github.com/foolzzz/looper/pull/912))
- **The installed DSH runner serve test helper skips a runner that has exited.** `dsh_runner_processes` reads the `ps` state and skips a defunct or exiting process that serve has not yet reaped, instead of failing the test. Test file only; no production code changes. ([#908](https://github.com/foolzzz/looper/pull/908))

## [2.14.18] - 2026-10-29

### Fixed

- **A hash navigation made just before the Chat page pins its thread is no longer reverted.** The Chat page replaces the address with the shown Manager's thread (`#chat?target=<id>&conversation=<id>`) so that New conversation can push the next entry. That replace ran from a stale render and was unconditional, so a navigation made just before it (Back/Forward, a link, a typed address, a click) was reverted to the previous Manager's thread. The replace now applies only while the address is still the one the effect saw; otherwise the newer navigation is kept. Dashboard only; no server, API, database or model-visible change. ([#910](https://github.com/foolzzz/looper/pull/910))

## [2.14.17] - 2026-10-29

### Changed

- **A project can be deleted together with its tasks and schedules.** Deleting a project no longer requires it to be empty. After the user confirms, Looper deletes the project's schedules (with their run history), stops its running tasks and waits up to 60 seconds for them to stop, deletes every task of the project with the same cleanup a single task delete does (step worktrees, task checkout, saved sessions, records; the result branch stays), then deletes the project. A project used by a Manager template, and, for a scoped Manager, a project that has other Managers' tasks or schedules, are still refused. The Manager `looper_projects` tool description and confirmation text, the v1 `project_admin.delete` description and impact, and the dashboard project settings Delete section follow. Scheduler firings and Router starts for the project are refused while it is deleted. ([#905](https://github.com/foolzzz/looper/pull/905))

## [2.14.16] - 2026-10-29

### Fixed

- **The installed Pi runner serve tests find the runner process regardless of terminal width.** The nightly tests in `tests/test_pi_runner_serve_db.py` matched the runner by its `ps` command line, which Linux procps cuts at the terminal width, so they failed with "not enough values to unpack". They now list processes with `ps -A -ww -o pid=,args=`, and a miss reports the pids found, the agent process records and the relevant serve log lines. Test files only; no production code changes. ([#906](https://github.com/foolzzz/looper/pull/906))

## [2.14.15] - 2026-10-29

### Changed

- **Runner processes are pooled per SDK, so sessions no longer hit one runner's cap.** With `[runtimes] dsh = "runner"` or `[runtimes] pi = "runner"`, a kind used to run on one runner process, and a lease failed with `CAPACITY` once its session cap was reached (20 for DSH, 10 for pi). The supervisor now starts another runner of that kind when every runner is full, up to four per kind (80 DSH or 40 pi sessions), places new sessions on the runner with the most free slots, and keeps each session on its runner. Extra runners that have been empty for 300 seconds stop; the last one stays. Only a full pool returns `CAPACITY`, now with a message naming the pool's slot count. A crash fails only the sessions of that runner, and a replacement starts on the next acquisition after the crash backoff instead of at once. Starting and stopping a runner is logged with the kind, the runner's index and the session counts. The default `"cli"` runtime is unchanged. ([#903](https://github.com/foolzzz/looper/pull/903))

## [2.14.14] - 2026-10-29

### Added

- **A schedule can opt its tasks into user gates.** A schedule has a new `user_gates` setting, `off` by default or `on`. A task a schedule started could never open a user gate; it now can when the schedule is `on` and its project also has `user_gates` `on`. The gates of such a task go to the schedule's notify target, else the dashboard gate target, else only the task page. The setting is in the schedule API, the Manager's schedule tool, the dashboard schedule form and configuration export and import (an omitted value imports as `off`). A gate's wait counts against the run timeout. Every existing schedule keeps `off`, its behaviour and its Planner prompt. The change adds one `schedules` column, applied additively at startup. ([#900](https://github.com/foolzzz/looper/pull/900))

## [2.14.13] - 2026-10-29

### Fixed

- **A runner turn that fails before its prompt is accepted now records its model, and a refused consult fork is classified `session_gone`.** A runner turn that failed before the prompt was accepted (a timeout, a rejected prompt, a lost generation or a budget mismatch) emitted no init, so its failure record had no model. It now emits an init with the Descriptor's model and the current session id. A room consult whose fork is refused (`SESSION_NOT_FOUND` or `SESSION_OWNED`) was classified `contract`; a consult now counts as started only when its init names a session, so a refused fork is `session_gone`. Such an init carries `session_started: false`, so the Manager and the Planner do not save the session or treat the resume brief as delivered until the runner accepts the prompt. ([#896](https://github.com/foolzzz/looper/pull/896))

## [2.14.12] - 2026-10-29

### Fixed

- **A DSH runner consult now carries the same context as the CLI consult.** A room consult forked on the DSH runner (`[runtimes] dsh = "runner"`) used to lack the DSH default system guidance and tool declarations that the CLI consult has, and sometimes answered in the Worker report format. The fork now gets the default guidance and the default tool declarations from a declaration-only copy of the installed DSH composition. Only `read`, `glob` and `grep` execute; every other declared tool refuses with `LOOPER_READ_ONLY`. The fork seed also keeps standalone events (for example a session title) after the ended turn, so a dropped-steer replacement is no longer cut off. The first consult request grows: in a synthetic measurement it went from 4,035 to 8,621 input tokens. The answer-format failure itself was not reproduced. ([#895](https://github.com/foolzzz/looper/pull/895))

## [2.14.11] - 2026-10-29

### Added

- **DSH runner validation, and an opt-in to run the eval and the DSH validation on it.** `evals.prompt_regression run` and `dialogs` take `--dsh-runner`, which opts every DSH role into the SDK runner, and `examples/dsh/validate.py` takes `--dsh-runner` on `task-basic`, `task-review`, `manager` and `smoke`: its evidence checks then expect the runner descriptor instead of the DSH CLI, and it also checks that the key is absent from the server logs. A new `tests/test_dsh_runner_serve_db.py` runs every role on an isolated `looper serve` with a local Messages stand-in. No production code under `looper/` changes. ([#887](https://github.com/foolzzz/looper/pull/887))

## [2.14.10] - 2026-10-29

### Fixed

- **Every runner-hosted turn records its model.** Records written for DSH and Pi runner turns had `model` NULL, so the runs list showed no model and task costs grouped under none. Each runner trace now opens with its own `system/init` carrying the Descriptor's model, including a failed resume. Model requests are unchanged. ([#891](https://github.com/foolzzz/looper/pull/891))

## [2.14.9] - 2026-10-29

### Fixed

- **The DSH runner counts a successful request complete at its validated `message_stop`, without waiting for the HTTP body to drain.** Before, a request whose response the SDK stopped reading after `message_stop` was recorded as usage-incomplete, which stopped Manager turns with `budget_unverifiable` at about zero cost. Rejections (HTTP 400 and above) still need a fully read body and no model output. A request that is still incomplete now writes one `usage_incomplete` stderr line naming the missing conditions, without request content. Affects only installations with `[runtimes] dsh = "runner"`. ([#889](https://github.com/foolzzz/looper/pull/889))

## [2.14.8] - 2026-10-29

### Fixed

- **The DSH runner drops an authorization reply that arrives after its turn settled.** Such a reply (for example an old trace's reply after the same session was resumed) is consumed without sending any update for the old trace, so the host bridge is no longer disconnected and the new turn is unaffected. A late allow is therefore no longer accounted after `settled` (v2.14.7 accounted it as an aborted, complete, zero-token request); one that arrives before `settled` is still accounted once. A response id the runner never issued is ignored with one diagnostic line instead of ending the runner, in the DSH runner only. Affects only installations with `[runtimes] dsh = "runner"`. ([#885](https://github.com/foolzzz/looper/pull/885))

## [2.14.7] - 2026-10-29

### Fixed

- **The DSH runner releases the session lock when a cancel times out during authorization.** The agent's wait for `budget/authorize` now ends with the cancel deadline, and a forced settle releases the session before `settled`, so the same session can be resumed right after it. A late allow is still accounted once (aborted, complete, zero tokens), now possibly after `settled`; a late deny or error is accounted as no request. Affects only installations with `[runtimes] dsh = "runner"`. ([#882](https://github.com/foolzzz/looper/pull/882))

## [2.14.6] - 2026-10-29

### Fixed

- **The DSH runner now accounts, cancels and types model requests correctly.** Usage counts as complete only when the provider's terminal event carries real input and output token counts (the SDK fills absent counts with zero, which no longer passes as complete). A cancel while an authorization is pending settles within the same cancel deadline; if the host allows the request late, it is accounted before `settled`, and a forced settle with an unanswered authorization reports usage as incomplete and invents no `request_end`. In-band DSH stream errors `AUTH`, `RATE_LIMIT` and `SERVER` map to `PROVIDER_AUTH` and `PROVIDER_UNAVAILABLE`. The session is flushed before `settled`; a failed flush reports the turn as failed. Affects only installations with `[runtimes] dsh = "runner"`. ([#880](https://github.com/foolzzz/looper/pull/880))

## [2.14.5] - 2026-10-29

### Added

- **`[runtimes] dsh = "runner"` hosts every DSH role on the DSH runner (ops file only, default `cli`, restart required).** Manager and Planner run as resident runner sessions; Worker, Reviewer and consult runs are hosted (Planner and consult read-only, others with `write`, `edit` and `bash`), and a consult fork passes the source id with a private fork directory and copies nothing. The Pi and DSH runners are enabled and stopped independently. An unavailable runner (node older than 22.19, no `dsh`, or a different `dsh` executable) fails the launch with the reason and never falls back to the CLI; DSH CLI commands, provider environment variables and web search are refused under the opt-in; launch records list the skipped catalog items. The runner state root may sit below a symlinked runtime root. Model-visible only with the opt-in: an appended system prompt with a `looper_*` tool note and no catalog skills, plugins or MCP servers. ([#871](https://github.com/foolzzz/looper/pull/871))

## [2.14.4] - 2026-10-29

### Added

- **DSH runner platform tools, session bash, path confinement, steer and consult fork, still not reachable from any runtime.** `session/prompt` accepts a per-prompt platform tool snapshot whose tools run only through reverse `tools/call` (host errors and aborts become tool failures, no `rpc/cancel`). A session `bash` runs in a detached group with exactly the session environment plus a private `HOME`, clamped to `tool_timeout_s`, output capped at 100 KiB, killed on timeout, cancel, close and shutdown. `read`, `write`, `edit`, `glob` and `grep` are confined to the cwd and added directories (symlink, dangling-link, `~` and pattern escapes denied with `LOOPER_PATH`). `session/steer` works in the running trace, with `steer_applied` only when a sent request carries it and `steer_dropped` with a typed reason otherwise. `session/new` accepts `fork_from` for a read-only worker or reviewer consult seeded from the source's completed turns. No setting or user-visible entry exists; no model-visible change for a running system. ([#876](https://github.com/foolzzz/looper/pull/876))

## [2.14.3] - 2026-10-29

### Added

- **DSH runner turns, still not reachable from any runtime.** The runner serves `session/prompt` and `session/cancel`. One `llm/stream` listener attributes each request to its session and trace (loop and compaction requests only; anything else is refused and logged as `usage_unattributed`), sends `budget/authorize` before the request and never sends a denied one (the turn settles `capped` or `max_requests`), and the route's `resolveAuth` refuses unless a request is authorized. Each authorized request ends in exactly one `request_end`, with usage complete only from a validated terminal usage and a fully read body. The turn maps assistant, tool-result and turn events to `session/update`, settles once with a typed outcome and provider error kind, and cancel or close settles within 4 seconds or releases the session. `session/close` takes the running turn's trace. No setting or user-visible entry exists; no model-visible change for a running system. ([#872](https://github.com/foolzzz/looper/pull/872))

## [2.14.2] - 2026-10-29

### Added

- **DSH runner sessions, still not reachable from any runtime.** The runner serves `session/new`, `session/load` and `session/close`: parameters are validated before any SDK work (`anthropic-messages` only, `append` prompt mode only, read-only sessions cannot enable `write`, `edit` or `bash`, platform tools and `fork_from` refused), each session gets one DSH agent with its own `looper-<session_key>` DeepSeek route (key only in memory, the configured model kept for budget and pricing, a trailing `[1m]` dropped from the wire model), a literal Looper prompt section and a restrict-plus-guard tool allowlist. Sessions persist in the runner's state root and resume across runner restarts (ephemeral ones are deleted at close), at most 20 are open, and shutdown closes them in order. `runner/health` reports the open count. No setting or user-visible entry exists; no model-visible change for a running system. ([#870](https://github.com/foolzzz/looper/pull/870))

## [2.14.1] - 2026-10-29

### Added

- **A DSH runner skeleton, not yet reachable from any runtime.** `runners/dsh` is a packaged TypeScript runner that speaks protocol 0.1: `initialize` gates the installed DSH, every `@deepseek-ai` package it uses and the exports later work needs on exactly `0.2.1-rc.2` (`cordis` on `4.0.x`, Node 22.19.0 or later) and answers `-32009` otherwise, boots one `headless` profile with a runner overlay (native bash, jobs, subagent, workflow, web and skill tools, the headless driver, the environment-key DeepSeek route and retry off; `danger-full-access` sandbox, approval `never`), serves `runner/health` and exits on signals or stdin EOF. `looper/agents/runtimes/runner/dsh_command.py` provides `entry`, `state_root`, `command` and `connect`, but nothing registers it and no setting or user-visible entry exists. The Pi runner's stdio transport moved unchanged to the shared `runners/common/rpc.ts`, and the wheel packages `common/` and `dsh_runner/`. No model-visible change. ([#867](https://github.com/foolzzz/looper/pull/867))

## [2.14.0] - 2026-10-29

### Changed

- **The release documentation gate requires the bridge notice from v2.14.0 on.** `release-readiness-doc-smoke.py` refuses a `vX.Y.Z.md` body at 2.14.0 or later unless it says `upgrade to v2.13.165 first` and its `## 中文摘要` says `先升级到 v2.13.165`; earlier bodies are skipped. The README, the Chinese README, the architecture guide, `evals/README.md` and the long-horizon plan now describe the completed bridge, `looper-config/v4`-only transfer, rollback and the retired eval schemas, and a config-transfer test reads its v4 example from the architecture guide. No production code changes. ([#865](https://github.com/foolzzz/looper/pull/865))

### Breaking

- **The fixed upgrade bridge stays v2.13.165.** That release contains every historical migration. An installation older than it must upgrade to v2.13.165 and be started once before a removal release accepts the database. A later 2.13 checkout, including the planning checkout at v2.13.166, does not move the boundary. The maintainer decided on 2026-10-27 that the first release after these removals is v2.14.0; the release tooling sets the number. This note does not bump, tag or publish that release. From v2.14.0 on, the release body must say to upgrade to v2.13.165 first ([#780](https://github.com/foolzzz/looper/issues/780) item 5 records the stale v3 and scheduler-variable sentences this note replaces).
- **Current config and schema admission.** Export and import are `looper-config/v4` only, through `loopercli` against a running service. Any other format, a retired layer `model` or `effort`, and a retired provider `protocols`, `base_url` or `models` fail before a write. Import an older file on v2.13.165, upgrade that installation and database, then export v4 here. v2.13.165 may still export v3. Startup admits an empty database (one baseline, marker `post_bridge_baseline`), a current schema, or one exact v2.13.165 bridge database through `upgrade_from_bridge`. Anything else is refused. There is no historical migration chain, no old config or runtime converter, and no `registry_migration` module. A separate local journal transition seals notice identities once per runtime, and historical run records gain `metadata.presentation` only when read.
- **Rollback and what a schema upgrade does not prove.** The upgraded schema must not be opened by an older binary. Rollback restores a verified full backup into a separate database and points a guarded older release at that database. It does not downgrade or mutate the original database, and `--skip-backup` is refused for this upgrade. A database that only passes the upgrader does not prove the v2.13.165 filesystem, service, prompt or browser steps: init-ops and unit reinstall, replacement of a leftover `~/.loopx` directory and `com.loopx.serve` unit, an explicit `LOOPER_TELEGRAM_BOT_TOKEN`, a saved scheduler minimum, a stopped old process, and one same-origin dashboard visit. Existing user files and historical rows stay. The scheduler uses the stored `scheduler.min_interval_seconds` or the built-in 300 seconds; `LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS` is not read. Deployment variables and the checkout `.env` are still read until the ops file names the setting. `service install` still copies `LOOPX_TELEGRAM_BOT_TOKEN` for one release.
- **Still supported.** The dashboard, Telegram, Lark and the CLI stay, including compact v1 operations and `loopercli` aliases. Feature-off, shared and in-place behavior, the future-schema guard, `LegacyProviderSpec` and `LegacyAgentProfile`, `use_runtime_default`, referenced custom runtimes, capability fallbacks, stable conversation identities, incomplete usage and output handling, `DeliveryResult` safety and isolation stay.
- **中文。** 固定桥仍是 v2.13.165，它包含全部历史迁移。更早的安装必须先升级到 v2.13.165 并启动一次。规划时的 v2.13.166 不移动边界。维护者于 2026-10-27 决定，这些移除之后的第一版是 v2.14.0，版本号由发布工具写入。从 v2.14.0 起，发布正文必须写明先升级到 v2.13.165（[#780](https://github.com/foolzzz/looper/issues/780) 第 5 项是本说明替换的过期 v3 与定时任务变量表述）。配置只接受 `looper-config/v4`，经正在运行的服务上的 `loopercli` 导入导出；其他格式和已退役字段在写入前失败。旧文件在 v2.13.165 导入，升级该安装和数据库后再导出 v4；v2.13.165 仍可能导出 v3。启动只接受空库基线、当前 schema，或一份精确的 v2.13.165 桥接库的一次升级。没有历史迁移链，也没有旧配置或运行时转换器。升级后的 schema 不能用旧二进制打开。回退是把校验过的完整备份恢复到另一个数据库，不降级、也不改原库。只通过升级器不能证明 v2.13.165 上的目录、服务、令牌、定时任务、进程和浏览器步骤已经完成。已有用户文件和历史数据保留。定时任务用已保存的最小间隔或内置 300 秒，不读 `LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS`。运维环境变量和 checkout 的 `.env` 在文件写上该项之前仍会读取。控制台、Telegram、飞书和 CLI 继续支持。任务对照只要 `looper-prompt-eval/v2`，对话对照只要 `looper-manager-dialogs/v2`。

### Verification

- The removed startup paths are already released: the scheduler variable ([#761](https://github.com/foolzzz/looper/pull/761)), the runtime-root move ([#762](https://github.com/foolzzz/looper/pull/762)), the bridge gate ([#808](https://github.com/foolzzz/looper/pull/808)), the single baseline ([#850](https://github.com/foolzzz/looper/pull/850)), removal of `registry_migration` ([#855](https://github.com/foolzzz/looper/pull/855)), and the stored-input boundary ([#857](https://github.com/foolzzz/looper/pull/857), v2.13.208). This entry documents that boundary. It does not claim a new prompt evaluation: model-visible text is unchanged. The five-task, one-repeat Sonnet baseline is not a full baseline, and the credential, installed-bootstrap and real-channel gaps stay open.
- **中文。** 上述删除已经随 #761、#762、#808、#850、#855 和 #857（v2.13.208）发布。本条只记录边界。模型可见文本没有改，因此不另称一次 prompt 评测。五任务、一次重复的 Sonnet 基线不是完整基线；凭据、已安装包引导和真实渠道的缺口仍在。

## [2.13.208] - 2026-10-28

### Changed

- **Stored-input, record and journal boundaries are consolidated.** Startup no longer carries the fallbacks for stored input older than the admitted upgrade path: the one-time import of the pre-PostgreSQL planner and Manager state files (`state_import.py`), the Lark configuration and default-prompt migrations in `looper serve`, the pre-rename LoopX harness match, the legacy provider-less session assumption for non-Claude runtimes, the multi-level unwrapping of a stored task config (now one JSON decode) and the pre-Manager column probes in configuration export. A retired Lark setting (`app_id_env`, `app_secret_env`) is now rejected instead of dropped. Historical run records are normalized at one read boundary (`metadata.presentation`, never written back), and the run history page renders that record instead of interpreting old field names. Bridge-era databases are admitted only when the file-state import and role-prompt seeding completed, and the message journal seals its notice identities once per runtime and refuses a malformed journal instead of skipping entries. No prompt text of the current release changes. ([#857](https://github.com/foolzzz/looper/pull/857))

## [2.13.207] - 2026-10-28

### Fixed

- **The Pi runner accounts a provider rejection as zero spend.** A sent request answered with HTTP status 400 or higher, whose error body was read to a clean end and which streamed no model output, is now reported as zero usage and complete, because the provider rejected it. Pi's retry after a 429 or 5xx is therefore no longer refused as `budget_unverifiable` under a run or task cap. A rejection whose body was cut off, or one that follows streamed model output, stays incomplete. The Chat end-of-stream flush that lets a stream ending in `data: [DONE]` with a single newline or none count as a normal end was already in place; this change adds its test coverage only. No prompt, tool or message text changes. ([#862](https://github.com/foolzzz/looper/pull/862), fixing #860)

## [2.13.206] - 2026-10-28

### Fixed

- **The Pi runner decides whether a request was sent from the HTTP client's own event.** The runner subscribes to undici's `undici:client:sendHeaders` event and matches it to the admitted request through an `x-looper-request-id` header, replacing the list of connection error codes. Every request that was not sent is reported as zero usage and complete. Usage is trusted only from a validated terminal event: the Responses `response.completed` event, or the Chat final usage chunk after `[DONE]` or a clean end of stream. A stream that drops after interim usage is reported incomplete. A sent request that fails, including one answered with an HTTP error status, is now reported incomplete. No prompt, tool or message text changes. ([#851](https://github.com/foolzzz/looper/pull/851))

## [2.13.205] - 2026-10-28

### Fixed

- **The Pi runner no longer stops a capped turn over inconsistent provider usage.** `responseUsage` keeps usage whose `total_tokens`, cache counts or reasoning counts disagree with input and output, so the turn is priced and checked against its caps instead of ending as `budget_unverifiable`. Missing or invalid counts are still capped. A capped turn with an unverifiable budget now logs the phase and first failing cause. No prompt, tool or message text changes. ([#854](https://github.com/foolzzz/looper/pull/854))

## [2.13.204] - 2026-10-28

### Changed

- **The unused registry preflight and converter module is removed.** `looper/agents/registry_migration.py`, `tests/test_registry_migration.py` and one converter-only test in `tests/test_custom_prompt.py` are deleted; nothing imports the module. `layer_effort.py` and its tests remain. No runtime behavior changes. ([#855](https://github.com/foolzzz/looper/pull/855))

## [2.13.203] - 2026-10-28

### Changed

- **The historical schema migrations are replaced by one final baseline.** `looper/db/schema.sql` now defines the complete fresh schema directly and `looper/db/seeds.sql` holds the builtin rows, inserted on every start without changing saved choices. An empty database runs the schema once, a schema-3 bridge database (v2.13.165 or later) runs one guarded conversion, and a current database never replays initialization DDL. The phase split, `final_constraints`, `registry_migration.py`, `task_concurrency.py` and `layer_effort.migrate` are removed; the admission rule is unchanged. ([#850](https://github.com/foolzzz/looper/pull/850))

## [2.13.202] - 2026-10-28

### Changed

- **The Pi SDK runner opt-in is validated for every role and documented.** A new installed-Pi test (`tests/test_pi_runner_serve_db.py`) runs one task end to end on an isolated `looper serve` with the Planner, two Workers, a Reviewer and a read-only consult fork on the runner, plus cancel, a runner SIGKILL and a pre-request budget denial. The architecture doc, the runner RFC, the plan and `evals/README.md` now describe `[runtimes] pi = "runner"` as covering all four roles and consults, and the README gives the Pi CLI versus runner task comparison, which has not been run. No production code changed. ([#848](https://github.com/foolzzz/looper/pull/848))

## [2.13.201] - 2026-10-28

### Changed

- **Looper upgrades a database only from a completed v2.13.165 or later schema-3 release.** Startup classifies the database under the schema lock as empty, current (schema 4 with the `post_bridge_baseline` marker), or a schema-3 bridge database recognised by one of two recorded `schema.sql` fingerprints (v2.13.165–192) and checked for the tables and columns the conversion reads. Anything else is refused with "unsupported Looper database; upgrade to v2.13.165 or a later 2.13 release first". A bridge database is converted once to schema 4 (`looper/db/bridge_upgrade.py`) after a verified backup; the historical migrations and the `UNIQUE (channel, chat, external_id)` constraint on gate posts were removed. ([#808](https://github.com/foolzzz/looper/pull/808))

## [2.13.200] - 2026-10-28

### Fixed

- **The Pi runner accounts for every authorised model request exactly once.** A budget authorisation that is still pending when a turn is cancelled is now awaited, and an `allow` that arrives afterwards ends with one aborted `request_end` that sent nothing, before the turn settles. A request that was sent but failed before response headers now reports `complete=false` instead of complete zero usage, and a completed request reports usage only from validated terminal counts. ([#832](https://github.com/foolzzz/looper/pull/832))

## [2.13.199] - 2026-10-28

### Fixed

- **Hosted Worker runs that stop early keep their dropped steers and session.** A Worker run on a runner session killed by the Planner now returns the steers it dropped as `missed_input` on the kill reply, which the Planner adds to `missed_messages`; a run cancelled by a pool stop ends as `stopped` with the same drops; and a hosted launch that fails with `RunStartError` ends its handle so the session is released. ([#844](https://github.com/foolzzz/looper/pull/844))

## [2.13.198] - 2026-10-28

### Added

- **Pi Workers, Reviewers and consults on the runner.** Under `[runtimes] pi = "runner"` (default `"cli"`, restart required) the opt-in now covers every Pi role. `RunnerChannel` runs one Worker, Reviewer or consult on a runner session as the run handle: a full runner (`CAPACITY`) or an unavailable one fails the step with a named reason, steers are never cancelled and answer accepted, rejected or unknown, and dropped steers reach the Planner as `missed_messages`. Runs get the CLI tool sets, `max_turns` as the request limit and the run timeout as the bash ceiling; consults fork a read-only (0400) copy of the run's saved session; the Pi CLI methods are refused under the opt-in. The generated ops file gains one comment line. ([#842](https://github.com/foolzzz/looper/pull/842))

## [2.13.197] - 2026-10-28

### Added

- **Run handle for Worker, Reviewer and consult runs.** `WorkerPool` and consults run through a `RunHandle` that is a process (`ProcessRun`, today's code) or a hosted runner session. `RuntimeAdapter.hosts_runs` (`False` for every current runtime) and `hosted_run` declare the hosted path; it uses in-process room and vault tools (`RoomTurnTools`), per-authorization run and task caps, and records `steer_applied` and `steer_dropped`; input a session dropped reaches the Planner as `missed_messages`. No current runtime hosts runs, so behavior is unchanged. ([#836](https://github.com/foolzzz/looper/pull/836))

## [2.13.196] - 2026-10-28

### Added

- **Pi runner opens Worker, Reviewer and forked sessions.** The Pi runner accepts the `worker` and `reviewer` roles, requires `limits.tool_timeout_s` (positive integer) and `limits.max_requests_per_turn` (non-negative integer), and handles `session/new` with `fork_from` through `SessionManager.forkFrom` (the saved session must match exactly one file; the source is not written; `session/load` with `fork_from` is refused). `initialize` reports the `fork` capability and the SDK gate requires `SessionManager.forkFrom`. The opt-in runner path only; no host path starts these sessions yet. ([#835](https://github.com/foolzzz/looper/pull/835))

## [2.13.195] - 2026-10-28

### Added

- **Runner protocol and host sessions for Worker, Reviewer and consult runs.** `session/new` and `session/load` accept the `worker` and `reviewer` roles; `limits.tool_timeout_s` is any positive integer instead of fixed 120; `session/new` takes an optional `fork_from` to open a session from a saved one (the runner reports a `fork` capability, and the host refuses to fork without it); `RunnerSupervisor.acquire` takes an absolute `state_dir` that survives release; `Descriptor`, `RunnerSession` and `RunnerTurn` carry the new roles, tool timeout and fork source, and run sessions open without owner state. Python host side only; the Pi runner is unchanged and the default 120 seconds is unchanged. ([#834](https://github.com/foolzzz/looper/pull/834))

## [2.13.194] - 2026-10-28

### Fixed

- **Pi runner `bash` now obeys the host tool timeout.** The runner applies `limits.tool_timeout_s` (120 seconds) when the model gives no timeout or a non-positive one, and caps a larger model timeout at it; on expiry the command's process group ends and the model gets `Command timed out after N seconds` with the output so far. Only affects the opt-in `pi = "runner"` path. ([#831](https://github.com/foolzzz/looper/pull/831))

## [2.13.193] - 2026-10-28

### Added

- **Opt-in: Pi Manager and Planner on the shared Pi runner.** The ops file gains `[runtimes] pi = "cli" | "runner"` (default `cli`; file only, read once at startup, restart required). With `runner`, `looper serve` starts one Pi runner (10 session slots) and `PiRunnerRuntime` runs only the Manager and Planner of a Pi profile through it; Workers, Reviewers and consults stay on the CLI. Missing prerequisites (Node 22.19+, `pi` on PATH) fail those turns with the reason and never fall back to the CLI. The prompt-regression evals gain `--pi-runner`. ([#829](https://github.com/foolzzz/looper/pull/829))

## [2.13.192] - 2026-10-28

### Added

- **Pi runner sessions can use platform tools and be steered mid-turn (not yet reachable).** `runners/pi` registers the tools of each `session/prompt` snapshot (validated first, no renaming) and runs them through the host's `tools/call`, returning its result unchanged. The new `session/steer` queues text into the running turn and reports `steer_applied` or `steer_dropped`; `initialize` declares `steer.in_trace`. Cancel and close drop pending steers, and a provider request started after cancellation is refused. Nothing registers the runner with the service yet. ([#823](https://github.com/foolzzz/looper/pull/823))

## [2.13.191] - 2026-10-28

### Added

- **Pi runner sessions get isolated builtin tools and can be resumed (not yet reachable).** `runners/pi` now builds `read`, `grep`, `find`, `ls`, `edit`, `write` and `bash` for a session: file tools refuse paths outside `cwd` and `add_dirs` (including `..`, `~` and symlinks), `bash` runs with the session `env` and a private `HOME`, and read-only sessions refuse `bash`, `edit` and `write`. `session/new` validates `home`, `add_dirs`, `env` and `tools.read_only`; the new `session/load` resumes a session from its state directory (`-32001` not found, `-32002` already owned); budget and usage reports mark retry and compaction requests; `runner/health` reports running bash commands. Nothing registers the runner with the service yet. ([#824](https://github.com/foolzzz/looper/pull/824))

## [2.13.190] - 2026-10-28

### Added

- **The Pi runner answers session methods (not yet reachable).** `runners/pi` now handles `session/new`, `session/close`, `session/prompt` and `session/cancel`. Each session gets its own model runtime with an in-memory key, in-memory settings and no resource discovery, and no builtin or platform tools. A Looper provider asks `budget/authorize` before every model request and reports exactly one `request_end` per allowed request; Pi events map to `session/update`, and each accepted prompt ends with exactly one `settled` carrying a typed outcome (`completed`, `cancelled`, `capped`, `max_requests`, `failed`). `runner/health` reports live session, running-turn and in-flight request counts. Nothing registers the runner with the service yet. ([#818](https://github.com/foolzzz/looper/pull/818))

## [2.13.189] - 2026-10-28

### Changed

- **Every browser test serves the staged dashboard.** `default_static_dir()` honors a `PYTEST_LOOPER_STATIC_DIR` override, which the test session sets to `looper/web/dashboard`, so a leftover `web/dist` build can no longer be served in tests; unset (every install), the lookup is unchanged. The browser tests assert the served page is the staged `index.html`, and the Python tests job now sets up Node and runs `scripts/stage-dashboard.sh` before pytest. ([#816](https://github.com/foolzzz/looper/pull/816))

## [2.13.188] - 2026-10-28

### Added

- **A Pi runner skeleton ships in the wheel (not yet reachable).** `runners/pi` holds a TypeScript runner process with line-delimited JSON-RPC framing, per-session ordered dispatch, reverse requests, a single stdout writer, an `initialize` handshake gated on Node 22.19+ and an installed `@earendil-works/pi-coding-agent` and `pi-ai` 1.3.x with the exports it needs, `runner/health` and orderly shutdown. It answers no session methods yet. The protocol gains `runner/health` (`RunnerBridge.health`), `RunnerSupervisor` accepts a fixed per-kind environment (`env=`), and `looper/agents/runtimes/runner/pi_command.py` builds the launch command and handshake without registering a runtime. ([#814](https://github.com/foolzzz/looper/pull/814))

## [2.13.187] - 2026-10-28

### Changed

- **The runner protocol and host support Pi sessions (host side only).** The protocol adds `session/load` (answered with `{session_id, resumed: true}`), a provider `account_id` and `reasoning` flag, named builtin tools (`tools.builtin`, no longer required to be empty) and a nullable `after_request_id` on `steer_applied`. `RunnerBridge.load_session` and `RunnerLease.load_session` are new; `RunnerSupervisor.acquire(kind, owner=...)` gives a lease a stable per-owner state directory that survives release and restarts; `RunnerSession` resumes a saved session through `session/load` and forwards the tool environment. The runner descriptor gains `reasoning`, `system_prompt_mode` and `--builtin`. ([#813](https://github.com/foolzzz/looper/pull/813))

## [2.13.186] - 2026-10-28

### Fixed

- **The nightly installed browser tests pass again.** The browser tests serve the dashboard staged once by `scripts/stage-dashboard.sh` (new `staged_dashboard_dir` fixture) instead of rebuilding `web/dist`, the task restart tests wait for the first detail response instead of sleeping, the browser's `HOME` is isolated through its launch `env`, and the dashboard configuration flow selects a Runtime. Tests only. ([#806](https://github.com/foolzzz/looper/pull/806))

## [2.13.185] - 2026-10-28

### Fixed

- **The eval's API Manager readback expects one completed API turn per user turn.** `evals/prompt_regression/dialogs.py` matches each user turn the API runtime answered to its own completed record, bounded by run ids, ignores `[HANDOFF]`, autonomous and session-profile records, and expects none for turns a switched non-API runtime answered, so a `--switch-agent` dialog is no longer a false failure. ([#804](https://github.com/foolzzz/looper/pull/804))

## [2.13.184] - 2026-10-28

### Fixed

- **The fake runner settles a turn exactly once when `session/close` follows its prompt.** `runners/fake/main.py` moves the settle step into `Fake.settle`, guarded by `Session.settled`, and `session/close` calls it after cancelling the turn, so a turn cancelled before its first step still sends one `settled` and the host's close wait completes. ([#807](https://github.com/foolzzz/looper/pull/807))

## [2.13.183] - 2026-10-27

### Changed

- **The Manager's `looper_catalog` list reports Manager-only installs.** `_item_listed` in `looper/agents/ops/config.py` now returns `manager_install_count` for each item, as the dashboard catalog does, and `COMPACT_ITEM` in `looper/agents/v1_domains/catalog.py` adds it to the result schema (also for the `item` of install, update and uninstall results). A skill installed only on the Manager no longer looks installed nowhere. ([#801](https://github.com/foolzzz/looper/pull/801))

## [2.13.182] - 2026-10-27

### Changed

- **A Manager's cap-raising schedule update confirms its whole change.** `_confirming` in `looper/agents/ops/schedules.py` now builds the confirmed "after" view from the full update (merged template selection, effective template, `schedules.schedule_view`) instead of the schedule plus only the spend-cap fields, so a `template_key` or `template_inputs` change sent with a cap is shown before it is committed. A template selection that the update would reject is refused with `invalid` or `conflict` before the question is asked. The reply after an update compares the stored schedule before and after without the scope filter; a scoped Manager is still refused with `forbidden` before anything is written when it changes another owner's template. ([#800](https://github.com/foolzzz/looper/pull/800))

## [2.13.181] - 2026-10-27

### Changed

- **A runner started by the supervisor has its initialization children owned before it is published.** `RunnerSupervisor` in `looper/agents/runtimes/runner/supervisor.py` now refreshes the process tree once (`generation.tree.refresh(force=True)`) after the bridge connects and before the generation is published. A child that a runner starts while it handles `initialize` is therefore recorded even if the runner is killed at once, and is reaped with the group; before, such a child could be missed until the periodic observation ran. The pull request also adds a test-only deterministic fake runner (`runners/fake/main.py`) with a conformance suite and an isolated `looper serve` acceptance test, which are not part of the product runtime. ([#782](https://github.com/foolzzz/looper/pull/782))

## [2.13.180] - 2026-10-27

### Changed

- **A configuration with retired fields is refused before an import touches the target.** `validate_document` refuses the whole file when `settings.layers[]` or a project's `layers[]` carries `model` or `effort`, or `providers[]` carries `protocols`, `base_url` or `models`; before, the retired entry was skipped. An API client (client policy) may now import a file it exported when its `managers` list holds only the unchanged default Manager: the entry is dropped from execution and nothing is written for it, while any other Manager change is still refused as FORBIDDEN. ([#790](https://github.com/foolzzz/looper/pull/790))
- **The dialog eval readback fails without completed API turns.** The api Manager session check in `evals/prompt_regression/dialogs.py` counts the steps that need the model (not code-only steps such as `/profile`, stopped or withdrawn ones) and reports a failure when fewer completed API Manager turns are read back, instead of passing on an empty query. No product runtime change. ([#793](https://github.com/foolzzz/looper/pull/793))

## [2.13.179] - 2026-10-27

### Changed

- **Managers can select a schedule template and read it back.** The `looper_schedules` tool accepts `template_key` and `template_inputs` (static string values) on create and update; the list, the configuration shown on creation, the enable/run confirmation and the before/after of a change now name the template and its inputs. A scoped Manager sees another owner's template selection blanked and is refused when it tries to change it, before anything is written. A new `schedule_template_create` dialog covers it, so the dialog selection grows from 47 to 48. ([#774](https://github.com/foolzzz/looper/pull/774))

## [2.13.178] - 2026-10-27

### Changed

- **PR Python tests run in four CI shards.** The `Agent system tests` job is a four-shard matrix (`LOOPER_TEST_SHARD=N/4`, whole files assigned by weight in `tests/conftest.py`) behind one final job named `Agent system tests` that passes only when every shard passed; the job timeout drops from 20 to 15 minutes. No test is dropped and no production code changed. ([#786](https://github.com/foolzzz/looper/pull/786))
- **Eval tooling accepts only current formats.** `register_agent` requires explicit provider `endpoints` and a top-level `model` and refuses the legacy `agent.model`, `provider.protocols`, `provider.base_url` and `provider.models`; `compare` and baseline lookup accept only `looper-prompt-eval/v2` (`looper-manager-dialogs/v2` for dialogs) reports and exit 2 otherwise. The committed real-Sonnet baseline is migrated to v2, launch parity checks the current four-tier contracts, and the historical pinned captures and the `launch-provenance.yml` workflow are removed. No product runtime or prompt change. ([#765](https://github.com/foolzzz/looper/pull/765))

## [2.13.177] - 2026-10-27

### Fixed

- **Manager capability changes are visible; deleting an active Manager is refused.** When a changed skill set cannot be applied because the resident runtime is not idle, the turn carries the notice `能力变更待应用` and the change takes effect after `/new`. Catalog items report `manager_install_count`; the catalog card counts Manager-only installations as installed and its delete confirmation names them. Deleting a Manager that has active or admitted work, queued messages or a runtime that cannot prove it is idle now returns 409 instead of cancelling it; Manager tool executions are admitted under the retirement lock and refused while the Manager is retiring. No schema change. ([#773](https://github.com/foolzzz/looper/pull/773))

## [2.13.176] - 2026-10-27

### Fixed

- **Interactive template tasks fall back; schedule imports check dependencies.** A task created from a template through `looper_task_create` uses the built-in profile when a role template's Agent Profile is off the project's list or fails the capability check, drops items that are no longer installed, and lists each fallback in the returned configuration block (`模板角色回退`). Schedules stay strict. Configuration import preview and apply now check a schedule's profiles, role templates and items against the import's final graph and skip a schedule that could not run; installs are processed before schedules. No schema change. ([#779](https://github.com/foolzzz/looper/pull/779))

## [2.13.175] - 2026-10-27

### Fixed

- **Schedules: a claimed gate's output survives a rename or notification edit.** A schedule's execution fingerprint now covers only execution settings (not the name, notifications or the `enabled` flag, which is checked separately), a gate run's stdout is saved before the configuration check, and each gating run records its fingerprint in the new `schedule_runs.execution_snapshot` column (also in the run view). Carried input skips earlier runs whose snapshot differs from the current execution settings. No prompt or tool change. ([#777](https://github.com/foolzzz/looper/pull/777))

## [2.13.174] - 2026-10-27

### Changed

- **Prompt-regression dialogs: Manager budget and api session readback.** With `--manager-agent`, the run budget (`--max-budget-usd`) is now written to the selected Manager profile's role settings together with `profile_id`. After an api Manager dialog, a new check reads back that every completed api turn in the Manager's current session has an `api_sessions` row whose protocol matches the Manager profile's, and a neutral `api_session_items` entry; a mismatch is added to the dialog's problems and fails it. Evaluation tooling only; no runtime, prompt or tool change. ([#764](https://github.com/foolzzz/looper/pull/764))

## [2.13.173] - 2026-10-27

### Changed

- **Configuration is `looper-config/v4` only.** Export always writes v4 and import refuses any other format, and a top-level field outside the v4 set (including `loopx_version`), before work begins; an invalid pending import file is discarded. The older-format conversions are removed: import such a file on v2.13.165, upgrade that installation, then export v4. The Claude Code agent teams switch is dropped from layer environments on export and import. ([#761](https://github.com/foolzzz/looper/pull/761))

### Removed

- **Scheduler minimum interval variable.** `LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS` and `LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS` no longer apply; only the stored setting or the built-in 300 seconds does, and the source `env` is gone from the settings page. ([#761](https://github.com/foolzzz/looper/pull/761))

## [2.13.172] - 2026-10-27

### Added

- **Resident runner sessions for Manager and Planner.** `RunnerSession` drives a Manager or Planner turn through a runner process owned by a shared supervisor, with its host tools and per-turn budget limits wired into the Manager and Planner tiers, and `looper serve` can own a runner supervisor and stop it at shutdown. Nothing in Looper creates a `RunnerSession` or passes a runner supervisor to serve yet. ([#768](https://github.com/foolzzz/looper/pull/768))

### Fixed

- **Cancelled close settlement.** After `session/close` is acknowledged, a cancelled settlement wait now leaves the session `close_failed` with local settlement and a tombstone, instead of leaving it `closing` and raising `CancelledError` out of the lease release. ([#776](https://github.com/foolzzz/looper/pull/776))

## [2.13.171] - 2026-10-27

### Fixed

- **Runner session close.** Closing a runner session is one explicit per-session state machine: a close that fails, is rejected or times out stays local to its session and the session keeps its capacity until cleanup completes, closed sessions leave bounded tombstones so late updates are dropped, and the supervisor keeps a lease whose close failed so the close can be retried. Nothing in Looper calls the runner bridge yet. ([#769](https://github.com/foolzzz/looper/pull/769))
- **Release smoke.** The wheel smoke no longer runs the removed `loopx` command, which blocked the v2.13.170 release; it asserts the wheel installs only `looper`. ([#771](https://github.com/foolzzz/looper/pull/771))

## [2.13.170] - 2026-10-27

### Removed

- **Retired entry points and runtime deployment fallbacks.** The `loopx` command, the installer's removal of an old `loopx` package, the automatic `~/.loopx` to `~/.looper` move, the `com.loopx.serve` unit replacement, `looper serve --format`, the old dashboard hash redirects and the `loopx-chat-*` browser keys are gone; `service install` and `init-ops` read deployment settings more simply. Current deployment and routing are kept. ([#762](https://github.com/foolzzz/looper/pull/762))

## [2.13.169] - 2026-10-27

### Fixed

- **Runner session isolation.** One session's close or cancel failure, a failing request-end callback, and late updates after a close no longer fail the runner generation or the shared connection: abandoned cancels are bounded and their late responses dropped, failed request-end processing settles only that trace, and a rejected session close no longer marks the generation failed. Nothing in Looper calls the runner bridge yet. ([#763](https://github.com/foolzzz/looper/pull/763))

## [2.13.168] - 2026-10-27

### Fixed

- **Runner supervision.** The supervisor tracks and reaps a runner's children, including ones in their own process groups, bounds the stderr drain so recovery and shutdown cannot hang, resolves only `Path` launch arguments instead of any string naming a file, and treats a slow or transiently failing initialize as a retryable start failure. Nothing in Looper calls the supervisor yet. ([#755](https://github.com/foolzzz/looper/pull/755))

## [2.13.167] - 2026-10-27

### Added

- **Manager-only profile for CPA dialog evals.** `dialogs --manager-agent SPEC.json` registers the supplied agent spec and runs the Manager alone on it, with api and Codex example specs under `evals/agents/`. Only the evaluation CLI and its documentation change. ([#725](https://github.com/foolzzz/looper/pull/725))

### Changed

- **Eval profile switch.** A real profile-switch dialog now defaults to a distinct profile that keeps the selected Manager's runtime, model, effort and prompt options; an explicit `--switch-agent` still overrides it. ([#725](https://github.com/foolzzz/looper/pull/725))

### Fixed

- **Runner protocol, bridge and turn policy.** Frame reads are bounded by the remaining frame budget, usage not matched by an authorization is settled as failed, a rejected prompt or close no longer poisons the shared connection or leaves stale session state, a long room-inbox poll gets a matching tool timeout, and a settled turn applies the actual run and task caps. ([#754](https://github.com/foolzzz/looper/pull/754))

## [2.13.166] - 2026-10-27

### Added

- **Runner supervisor.** `RunnerSupervisor` owns runner processes: a strict launch environment, private runtime directories, one live process per configured kind, a session cap, crash restart with bounded backoff and joined shutdown. Nothing in Looper calls it yet. ([#751](https://github.com/foolzzz/looper/pull/751))

### Changed

- **Runner turn loss.** A turn whose runner is lost now settles with the host-owned outcome `lost`, which stays the terminal cause even under a cap, and counts only the requests the bridge completed. ([#751](https://github.com/foolzzz/looper/pull/751))

## [2.13.165] - 2026-10-27

### Fixed

- **Manager deletion.** Deleting a scenario Manager now checks tasks, schedules, bots and chat bindings before stopping its reply, so a refused deletion leaves the current reply and queued messages alone. The checks run again under the row lock after the drain, installs wait for a deletion, and a Manager's drain is bounded at 10 s. ([#727](https://github.com/foolzzz/looper/pull/727))

## [2.13.164] - 2026-10-27

### Added

- **Runner bridge.** Adds `RunnerBridge` in `looper/agents/runtimes/runner/bridge.py`: one runner stdio connection with request matching, a session table and translation of runner updates into Looper's existing event shape, routing tool and budget requests to `RunnerTurn`. Nothing in Looper calls it yet. ([#744](https://github.com/foolzzz/looper/pull/744))

## [2.13.163] - 2026-10-27

### Added

- **Runner turn policy.** Adds `RunnerTurn` in `looper/agents/runtimes/runner/turn.py`, which admits platform tool calls for one runner trace and enforces request, run-cap and task-cap budgets with fail-closed accounting. Nothing in Looper calls it yet. ([#743](https://github.com/foolzzz/looper/pull/743))

### Changed

- **Shared platform tool helpers.** The Manager tool adapter's argument parsing, invalid-argument error and 100 KB result bound moved into helpers in `looper/agents/runtimes/api/tools.py` without changing their output, and a `PlannerTurnTools` adapter was added for Planner dispatch that no runtime uses yet. ([#743](https://github.com/foolzzz/looper/pull/743))

## [2.13.162] - 2026-10-27

### Added

- **Core runner wire contract.** Defines the typed JSON-RPC messages and bounded line-feed framing (protocol 0.1) for agent SDK runners in `looper/agents/runtimes/runner/protocol.py`, with a JSON Schema and shared vectors under `runners/protocol/`. Nothing in Looper calls it yet; the only dependency added is `jsonschema` in the `test` extra. ([#740](https://github.com/foolzzz/looper/pull/740))

## [2.13.161] - 2026-10-27

### Changed

- **Scenario Manager documentation matches the shipped behavior.** The architecture document, the scenario Manager RFC and the long-horizon plan now describe idle suspension, deleted-Manager cleanup, schedule ownership and templates, Manager installs and configuration transfer v4, and a new integration regression covers the portable graph. Documentation and tests only. ([#737](https://github.com/foolzzz/looper/pull/737))

### Added

- **RFC for agent SDK runners.** Proposes Pi, DSH and Norma runners in their native languages, driven by Looper over JSON-RPC on stdio; the maintainer agreed to develop it from stage S0. Design document only; no code. ([#703](https://github.com/foolzzz/looper/pull/703))

## [2.13.160] - 2026-10-27

### Added

- **Settings can manage a Manager's own plugins and skills.** Each Manager in Settings gets a "Plugins and skills" section that lists inherited global installs and the Manager's additions, adds, enables, disables and removes additions through the `manager_installs` operator API, and reads the committed state back after every change. It is hidden for the built-in default Manager unless the Manager has additions or an error. ([#735](https://github.com/foolzzz/looper/pull/735))

## [2.13.159] - 2026-10-27

### Added

- **An operator can install a global plugin or skill for one Manager.** The extras load on top of the Manager's inherited global installs at its next safe turn, through the new operator-only `manager_installs.list`, `put` and `delete` operations (`loopercli manager-installs`). Configuration export and import carry them, catalog deletion counts them, and three new prompt-regression dialogs cover installed, isolated and default Managers. ([#732](https://github.com/foolzzz/looper/pull/732))

## [2.13.158] - 2026-10-26

### Fixed

- **A failed Claude Code background Agent launch no longer keeps the Manager process from being suspended when idle.** The untracked background-job marker is now cleared when the launch returns an error result without a `task_started` event; jobs that already started stay tracked. ([#729](https://github.com/foolzzz/looper/pull/729))

## [2.13.157] - 2026-10-26

### Added

- **Deleted Manager history can be purged after a retention window.** Two operator-only operations, `manager_cleanup.preview` and `manager_cleanup.purge` (`/api/v1/manager_cleanup/*`, the generated `loopercli manager-cleanup` and a control in Settings → Managers), preview and then permanently remove a deleted Manager's chat message bodies, run records with their events, and action records, once the Manager has been deleted for at least 30 days (default 30, integer 30 to 3650). The Manager row and its thread ownership rows stay as identity tombstones, so the key and old sessions are never reassigned. Purge needs a confirmation, rechecks eligibility at execution, runs in one transaction and is blocked by live agents, pending messages or retries, tasks, bots, chat bindings, schedules or running records. There is no automatic cleaner and no schema migration. ([#728](https://github.com/foolzzz/looper/pull/728))

## [2.13.156] - 2026-10-26

### Added

- **Idle scenario Managers are suspended and resumed.** A fleet sweep closes the resident Claude process of a scenario Manager that has been idle for its `idle_timeout_seconds` (0 = off, otherwise 60 to 86400; default 1800, the built-in Manager always 0) and resumes its saved session on the next message. A process with background jobs, pending messages or an unknown lifecycle state is kept; Grok ACP and per-turn runtimes never close. The field is stored per Manager (schema migration), in the Managers API, the `/api/v1` config schema, configuration transfer and the Managers settings form. ([#719](https://github.com/foolzzz/looper/pull/719))

## [2.13.155] - 2026-10-26

### Fixed

- **The api deadline test is reliable and stricter.** `test_deadlines_abort_io_and_close_server_connections` warms up the SDK before imposing the deadline and checks that a deadline during an SDK retry ends the turn as an unavailable failure with no history, tool call or `RequestDone` event. Tests only; no production code changed. ([#718](https://github.com/foolzzz/looper/pull/718))

## [2.13.154] - 2026-10-26

### Fixed

- **api Manager sessions stay consistent across a restart and a Manager deletion.** `api_sessions` stores the last known `context_tokens` (backfilled once from the run records), so a restarted api Manager applies the context handoff threshold. The session row is created before the first request and removed if that request leaves no turn; a late append or context save can no longer recreate a deleted session. Deleting a scenario Manager through `managers.delete` now stops it and waits for its turn, then deletes the session pointer and api history in one transaction, and restores the Manager if the delete fails. ([#715](https://github.com/foolzzz/looper/pull/715))

## [2.13.153] - 2026-10-26

### Added

- **A schedule can run its owner Manager's task template.** A schedule stores `template_key` and static `template_inputs` (strings); each firing renders the template's requirement and creates the task with a template snapshot, the template's role templates as first role versions, and merged Agent Profiles. Fixed settings (project, work directory, profiles, rendered length, owner policy) are checked on save and again at fire, and a gate result from an older configuration is discarded. Adds `schedules.template_key` and `template_inputs` with a foreign key to the Manager's templates, so a template used by a schedule cannot be deleted. The v1 schedule schema, `looper-config` schedule entries and the schedule forms carry the two fields. The configuration export tool and its note now name `looper-config/v4` and report `bots_left_out`. ([#711](https://github.com/foolzzz/looper/pull/711))

## [2.13.152] - 2026-10-26

### Fixed

- **api request logs carry their fields, and Stop works while Manager tools load.** Each api request is logged as `api_request key=value ...` (redacted) with new `request` and `outcome` fields; Stop now ends a turn that is still waiting for the platform tool list. ([#713](https://github.com/foolzzz/looper/pull/713))

## [2.13.151] - 2026-10-26

### Changed

- **api Manager sessions persist in PostgreSQL and hand over when the context fills.** Conversation history is stored per completed request, so it survives a restart; a stored session is refused if its protocol differs. Above the smaller of 70% of the context window and 200,000 tokens, the Manager summarizes in a tool-free turn and continues in a new session. Adds the `api_sessions` and `api_session_items` tables and `agent_sessions.pending_handoff`. ([#688](https://github.com/foolzzz/looper/pull/688))

## [2.13.150] - 2026-10-26

### Fixed

- **The dashboard Tasks page keeps updating under slow responses.** The list now polls three seconds after the previous request settles, instead of on a fixed timer, so a slow response is applied rather than discarded. A Show more page is dropped if a poll has since changed its cursor, so rows are no longer lost. Dashboard only; no API change. ([#708](https://github.com/foolzzz/looper/pull/708))

## [2.13.149] - 2026-10-26

### Changed

- **Task lists can be filtered by Manager.** The dashboard Tasks page has a Manager select (with `manager` and `query` in its URL), the v1 `tasks.page` operation accepts `manager_id`, and status counts follow the filter. A `manager_id` naming no Manager returns an empty list. The client's task tool gains `manager_id` for `action: list` only; a client-scoped call sees only its own Manager's tasks. The Manager's own task tool is unchanged. ([#705](https://github.com/foolzzz/looper/pull/705))

## [2.13.148] - 2026-10-26

### Changed

- **A schedule has an immutable Manager owner.** `schedules.manager_id` (default `manager`, so old schedules stay with the default Manager) is chosen when a schedule is created through the API, the v1 API, the Manager's schedule tool or the dashboard's new Owner select, and cannot change afterwards. The tasks a schedule creates belong to that Manager and use its existing reporting. A firing or an enabling is refused when the owner is deleted, disabled or requires a template. Deleting a Manager that owns a schedule answers 409. Configuration export writes `manager` for a non-default owner; import skips a schedule whose owner is missing, requires a template or differs from the stored one, and locks incoming schedules before Managers. The schedule and run views gain `manager_id` (and `manager_name`). A schedule a Manager creates is now owned by that Manager; its `get` result still omits the owner. Tool declarations and prompts are unchanged. ([#695](https://github.com/foolzzz/looper/pull/695))

## [2.13.147] - 2026-10-26

### Fixed

- **An unavailable in-process runtime shows its reinstall hint in Agent Profiles.** In Settings → Agent Profiles, a disabled in-process runtime such as Model API (SDK) now shows its probe error (for example, reinstall Looper) in the Runtime option text and tooltip instead of "not on the server's PATH"; it falls back to "not available" when there is no probe error. A CLI runtime keeps the PATH wording. The Runtime select can now shrink and truncate long option text. Only the dashboard changes; prompts, tool declarations and the API are unchanged. ([#700](https://github.com/foolzzz/looper/pull/700))

## [2.13.146] - 2026-10-26

### Fixed

- **A configuration import may omit the name of an existing task template.** The overlay rule from #689 now also holds in the HTTP preview and upload paths: a new import-only schema (`ConfigImportDocument`) requires only the template `key`, while `ConfigDocument` and exports still require the full identity. After the stored template is merged in, a template without a name is rejected with an error that names its key, so a new template still needs one. Prompts and tool declarations are unchanged. ([#697](https://github.com/foolzzz/looper/pull/697))

## [2.13.145] - 2026-10-26

### Changed

- **Settings → Agent Runtimes shows the api runtime.** The Model API (SDK) card is listed as an in-process SDK runtime: its path row reads "Runs inside looper serve", a missing Python package shows the probe's reinstall error (the runtime type view gains an optional `probe_error`), and the "norma" placeholder is dropped from the planned runtimes. The registry view probes an in-process type that has no registered runtime so the diagnosis stays visible. agent-system.md, the API runtime RFC and design.md describe the runtime; a new test file checks the Agent Profile form, the Roles and project/task selectors, the chat stop button, Telegram and Lark `/profile` and `/stop`, `looper doctor` and `loopercli` against the RFC's entry-point table. Prompts and tool declarations are unchanged. ([#696](https://github.com/foolzzz/looper/pull/696))

## [2.13.144] - 2026-10-26

### Fixed

- **Configuration import keeps stored template fields the file omits.** When the target already stores a task template for the same Manager and key, the import overlays the file's keys on the stored template before references are resolved and fields validated, so preview and apply agree. The stored directory reminder (`repoint`) is not carried over. Prompts and tool declarations are unchanged. ([#689](https://github.com/foolzzz/looper/pull/689))

## [2.13.143] - 2026-10-26

### Added

- **Stopping an api Manager reply works.** `ApiRuntime` and `ApiSession` now declare `can_interrupt`; `interrupt()` requests the turn's `TurnCancel` and waits up to 5 seconds for the turn to finish (a tool call that is still running is never cancelled mid-write), and the turn reports `StopMethod.CANCEL`. A stop requested before the first request, or a cancelled turn that sent none, ends as a user stop without a request. Per-request details (upstream ids redacted, an `incomplete` count) are kept in the run record under `api` through a new `RuntimeAdapter.result_notes` hook, and each request writes one structured `looper.agents.runtimes.api` log line (agent, protocol, model, host, status, durations, retries, token counts, completeness, redacted upstream id). A request that ends without usage is now recorded as unknown, not zero: token sums stay unset when no request reported a count, and the run's cost is cleared when usage is incomplete. Prompts and tool declarations are unchanged. ([#687](https://github.com/foolzzz/looper/pull/687))

## [2.13.142] - 2026-10-26

### Added

- **Configuration transfer carries Manager bots.** An export now includes every added channel bot with its `manager_id` (`looper-config/v4`; a file with only built-in channels and the default Manager stays `v3`), and import matches bots by immutable channel id. Import validates the whole graph before writing: closed `BotConfig` fields, well-formed unique ids, an existing non-deleted Manager, no reassignment of an existing bot's type or owner, no reuse of an already allocated id (the id counter only moves forward), and no two bots resolving to the same Telegram token, Lark profile or app id. New and changed bots import disabled with a plan note and a credential follow-up; an identical bot keeps its local enablement. Bot credentials, chat bindings and `bound_chats_only` policy never travel, even with secrets included, and a bot in a pre-v4 file is refused. The dashboard Import/Export text says so. Prompts and tool declarations are unchanged. ([#681](https://github.com/foolzzz/looper/pull/681))

## [2.13.141] - 2026-10-26

### Added

- **The Manager can answer on an api Profile.** `ApiRuntime.resident_session` now returns an `ApiSession` (`looper/agents/runtimes/api/session.py`) that keeps the neutral conversation history in memory (at most four sessions per owner), runs the Responses loop (at most 30 requests per turn) and turns the loop's events into the internal events the Manager already reads. Platform tools are called in the service process through `ManagerOps.call` (`looper/agents/runtimes/api/tools.py`) with the same confirmation and audit as the MCP bridge; a tool result over 100 KB is cut with a note. Launches on this runtime add a short runtime note to the system prompt, the soft cap applies, and the `api` adapter joins `ADAPTERS`, so startup registers the built-in `api` runtime (`rt-api`) when the `openai` package is installed. It runs only the Manager role and needs a profile with an API key; existing profiles are unchanged. Sessions are not persisted and stop is still refused. The dashboard card for the runtime follows in a later change. ([#684](https://github.com/foolzzz/looper/pull/684))

## [2.13.140] - 2026-10-26

### Changed

- **Hardened OpenAI Responses codec.** It refuses `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `OPENAI_ADMIN_KEY`, `OPENAI_CUSTOM_HEADERS` and `OPENAI_LOG` before importing the SDK and clears ambient organization, project and header settings; the loop counts SDK retries against the request limit and passes the remainder to the codec as `max_attempts` (`RequestLimitReached` ends the turn with `MAX_REQUESTS`); usage is kept from failed and incomplete responses and recorded as incomplete when a count is missing; encrypted reasoning is always requested; a stream that ends without a terminal event is marked for the existing safe resend. Nothing in Looper calls the codec yet, so no Manager, session, adapter or entry point changes behavior. No prompt or tool declaration changes. ([#683](https://github.com/foolzzz/looper/pull/683))

## [2.13.139] - 2026-10-26

### Added

- **OpenAI Responses codec for the API runtime loop.** `looper/agents/runtimes/api/responses.py` adds the first protocol codec: it uses the official async OpenAI SDK (no proxy environment, explicit key and base URL), sends the full input with `store=false` and function tools, replays encrypted reasoning only for the same protocol and model, assembles output by index and id and takes the final content from `response.completed`, maps errors and usage, and applies the fixed timeouts and two status-based retries (a provider's `x-should-retry` is ignored). `loop_for` now returns the Responses loop; other protocols are still refused. Nothing in Looper calls `loop_for` yet, so no Manager, session, adapter or entry point changes behavior. No prompt or tool declaration changes. A scripted local HTTP/SSE model server is added for the tests. ([#677](https://github.com/foolzzz/looper/pull/677))

## [2.13.138] - 2026-10-26

### Added

- **Scenario Managers and task templates in configuration export and import.** The export writes `looper-config/v4` with a `managers` section (immutable ids, profiles and projects by name, no machine paths; a template's local directory is flagged `repoint`) whenever a non-default Manager exists, and otherwise the unchanged v3. The import validates the whole Manager graph before writing, matches Managers by id and templates by Manager and key, never takes a working directory from the file, leaves unchanged entries alone and previews through the existing confirmation flow. A client identity may not stage a file with Managers, added bots or Manager installs; the check runs at upload and again at confirmation. `looper config` commands accept the operator token options and the dashboard text describes the change. No prompt or tool declaration changes. ([#670](https://github.com/foolzzz/looper/pull/670))

## [2.13.137] - 2026-10-26

### Added

- **Neutral agent loop for the planned `api` runtime.** `looper/agents/runtimes/api/loop.py` ports Norma's turn and tool-call cycle into typed asyncio Python behind `AgentLoop`, with versioned protocol-neutral session items, serial tool dispatch after a complete response, a request limit, cancellation, one safe stream resend and per-request records. Nothing imports it yet and `loop_for` rejects every protocol until a codec lands, so runtime behavior and prompts are unchanged. Adds `THIRD_PARTY_NOTICES.txt` (Norma MIT notice) as wheel package data. ([#674](https://github.com/foolzzz/looper/pull/674))

## [2.13.136] - 2026-10-26

### Changed

- **M9d plan milestone points at the Norma loop.** The long-horizon plan's M9d row and section now describe the approved `api` runtime with a Norma loop port instead of waiting for a Norma SDK. Documentation only; no code, schema, API or prompt changes. ([#673](https://github.com/foolzzz/looper/pull/673))

## [2.13.135] - 2026-10-26

### Changed

- **API runtime design updated in the docs.** The API runtime RFC and the long-horizon plan now record the maintainer's decisions: a Norma-derived typed asyncio loop, protocol-neutral session storage, a retained MIT notice, an original handoff prompt and the cwd/`add_dirs`, allowlisted-environment and private-HOME boundary for future tools. Documentation only; no code, schema, API or prompt changes. ([#669](https://github.com/foolzzz/looper/pull/669))

## [2.13.134] - 2026-10-26

### Changed

- **Scenario Managers stage 3 is marked as implemented in the docs.** The scenario-Managers RFC and the long-horizon plan now record the maintainer's confirmation of the seven recommended semantics. Documentation only; no code, schema, API or prompt changes. ([#667](https://github.com/foolzzz/looper/pull/667))

## [2.13.133] - 2026-10-25

### Changed

- **Chat binding and routing decisions are logged at their boundaries.** Adapters now log why a chat message was admitted, silenced or answered because its target is off; binding and policy writes, refused operator-only calls and native stop log their outcomes. The published routing snapshot after a write is now derived from the snapshot read under the write lock instead of a second read. No schema, API or prompt changes. ([#665](https://github.com/foolzzz/looper/pull/665))

## [2.13.132] - 2026-10-25

### Changed

- **Chat-bound Managers are tested end to end and documented.** New database-backed and browser tests cover rebinding, bound-only silence, the generated operator CLI, native stop and the two Settings pages, and the architecture documents now match the merged behavior. No production code changes. ([#662](https://github.com/foolzzz/looper/pull/662))

## [2.13.131] - 2026-10-25

### Fixed

- **The Channels chat bindings view stays in step with the server.** Page boundaries are rebuilt from each read, every write (even a refused one) is followed by a full re-read that keeps the write's error, and the unbind option reads "Leave unbound (silent)" when Only bound chats is on. ([#661](https://github.com/foolzzz/looper/pull/661))

## [2.13.130] - 2026-10-25

### Added

- **Managers settings lists each Manager's chat bindings and can move or unbind them.** Unbinding first explains what happens to the chat, and the list is re-read from the server after each write. ([#657](https://github.com/foolzzz/looper/pull/657))
- **Channels settings binds observed bot chats and saves the Only bound chats switch.** Each Telegram or Lark card lists observed chats with their effective Manager; changes apply to future messages at once. ([#658](https://github.com/foolzzz/looper/pull/658))

### Changed

- **The channel card and Channels page now say that only bot connection, credential and allowlist changes need a restart.** The saved badge reads "Saved — restart to apply bot settings". ([#658](https://github.com/foolzzz/looper/pull/658))

## [2.13.129] - 2026-10-25

### Added

- **Operator-only v1 operations manage native chat bindings and the bound-chats-only policy.** `manager_chat_bindings.list/put/delete` and `channel_chats.list/set_policy` work on the live routing snapshot, generated CLI commands for operator-only operations take an operator token, and the dashboard gets a typed client module. ([#655](https://github.com/foolzzz/looper/pull/655))

### Changed

- **An API client can no longer delete a bot that has chat bindings or the bound-chats-only policy.** Bot removal through v1 now goes through the routing-aware delete; operators can still remove such a bot. ([#655](https://github.com/foolzzz/looper/pull/655))

## [2.13.128] - 2026-10-25

### Changed

- **Native Telegram and Lark bot chats are routed to their bound Managers from a live snapshot, without a restart.** Each registered bot loads a routing snapshot (owner Manager, bound-chats-only policy, per-chat bindings) and the adapter resolves every incoming chat against it. A chat that is not bound while the policy is bound-chats-only is dropped silently; accepted messages carry a pinned channel thread identity, and the stop command still reaches the fleet when the bound Manager is off. The Manager accepts an unsuffixed thread identity only for the bot's own owner Manager. ([#652](https://github.com/foolzzz/looper/pull/652))

## [2.13.127] - 2026-10-25

### Changed

- **The stop command of a Telegram or Lark chat stops that chat's replies in every running Manager.** The fleet snapshots each Manager's live and queued turns for the bot and chat, stops them, and answers with a code-owned acknowledgement that reports partial or failed stops. The experimental stop switch and the runtime interruption contract are unchanged. ([#650](https://github.com/foolzzz/looper/pull/650))

## [2.13.126] - 2026-10-25

### Changed

- **Telegram and Lark conversation history checks which Manager owns the conversation.** A Manager stores a turn and claims or checks the conversation in one transaction; a turn from a non-owner is rejected and logged. Reply recording only stores answers from a Manager. A web `session_id` no longer names a Telegram or Lark conversation. New helpers in `looper/agents/message.py` define an immutable bot conversation id; nothing builds a suffixed id yet. ([#645](https://github.com/foolzzz/looper/pull/645))

## [2.13.125] - 2026-10-25

### Added

- **Durable storage for Manager chat bindings and observed chats.** A new column `channel_configs.bound_chats_only` and the tables `manager_chat_bindings`, `channel_seen_chats` and `channel_unbound_chats`, with the storage module `looper/db/chat_bindings.py`. Nothing calls the module yet and no message routing is enabled; channel views report `bound_chats_only` as false. ([#646](https://github.com/foolzzz/looper/pull/646))

### Changed

- **Deleting a Manager with chat bindings is refused.** `delete_manager` returns `has_bindings` and the API answers 409 asking to unbind first. No binding can be created yet. ([#646](https://github.com/foolzzz/looper/pull/646))

## [2.13.124] - 2026-10-25

### Changed

- **Development plan marks the insecure-context dashboard fix as done.** One entry in section 5.1 of the long-horizon agent context plan (`docs/plans/2026-10-13-long-horizon-agent-context.zh-CN.md`) is struck through and points to the fix. Documentation only; no code, schema or behavior changed. ([#643](https://github.com/foolzzz/looper/pull/643))

## [2.13.123] - 2026-10-25

### Fixed

- **Restarting a task is confirmed even when old run records look stale.** The dashboard's task page decided a restart had taken effect by comparing the task's status, its completion time and its lifetime run count. A stale historical run record, or a restart that was stopped before completing, could leave that comparison unable to tell a new attempt from the old one. A new `tasks.attempt_ended_at` column now marks the end of the current attempt, including Stop, and is cleared on restart. `task_detail` returns it and the page confirms the restart from it. Tasks that ended before the upgrade keep it empty (unknown). ([#633](https://github.com/foolzzz/looper/pull/633))

## [2.13.122] - 2026-10-25

### Changed

- **Tests pin the per-conversation chat draft handling.** A new browser test and a new component test check that an unsent draft stays with its own Manager and conversation across switching, transcript links, going back and a reload, and that nothing is sent. Two existing browser tests now wait for the restored session's history to render before sending. Tests only: no shipped code, route, stored data format, Manager tool declaration or prompt changed. ([#638](https://github.com/foolzzz/looper/pull/638))

## [2.13.121] - 2026-10-25

### Fixed

- **A failed Retry on the Gates list keeps the loaded pages and their paging boundary.** The list's cursor is now committed with its rows, so a Retry that fails leaves the rows, the boundary and the error message unchanged and the next "Show more" continues from the earlier boundary; a successful Retry replaces rows and boundary together. Dashboard only: no route, stored data format, Manager tool declaration or prompt changed. ([#636](https://github.com/foolzzz/looper/pull/636))

## [2.13.120] - 2026-10-25

### Fixed

- **A chat send stays bound to its writable conversation while a read-only or unresolved link is open.** Opening a transcript or an unresolved conversation link no longer cancels the pending send, its reply stream or its queued messages; only adopting a different writable Manager or session does. The unsent draft is now kept per Manager and session in the browser's session storage, so it survives switching and reloading, and queued text whose composer is hidden stays in its thread's queue instead of being pulled into the wrong draft. Dashboard only: no route, stored data format, Manager tool declaration or prompt changed. ([#625](https://github.com/foolzzz/looper/pull/625))

## [2.13.119] - 2026-10-25

### Fixed

- **`loopercli config --help` lists the handwritten `export` and `import` commands with descriptions.** They were named only in the usage line, while the commands generated from the API were listed with descriptions; each now has a help line (`export`: export configuration as JSON to a file or stdout; `import`: preview configuration, request confirmation to apply, or cancel). Help text only: no route, option, stored data format, Manager tool declaration or prompt changed. ([#630](https://github.com/foolzzz/looper/pull/630))

## [2.13.118] - 2026-10-25

### Fixed

- **The Gates page Retry button recovers pagination when the saved boundary has expired.** After a failed load, Retry now requests the first page without the saved pagination boundary and replaces the rows with the result only on success; a failed Retry keeps the saved boundary and the rows already shown. An older refresh that finishes after a Retry is ignored. A gate whose answer editor is open stays mounted when a recovered page omits it, and rows merged from a further page replace same-key rows instead of duplicating them. Dashboard only: no route, stored data format, Manager tool declaration or prompt changed. ([#626](https://github.com/foolzzz/looper/pull/626))

## [2.13.117] - 2026-10-25

### Fixed

- **Disabling a Lark bot no longer skips admission when the same save changes its allowed senders, and a Telegram stop drains updates before stopping reactions.** An explicit disable skips the same-bot check only when the identity and, for a Lark save with a `config`, the set of `allowed_open_ids` and the `allowlist_app_id` are unchanged; otherwise it is admitted like any other Lark save. The sender lists are compared as sets, so a reordered list keeps the stored app binding. The Telegram adapter now stops its reactions worker after the updater and application have stopped, since `Application.stop` drains updates whose handlers can still add reactions. No route, stored data format, Manager tool declaration or prompt changed. ([#627](https://github.com/foolzzz/looper/pull/627))

## [2.13.116] - 2026-10-25

### Fixed

- **Stopping a process tree now waits for SIGKILLed descendants to exit, and a cancelled stop no longer leaves them behind.** After SIGKILL, ending a tree waits up to `REAP_SEC` (2 s) for the processes it signalled to disappear, without expanding the tree or signalling again, and logs a warning for any still running. A cancelled tree end now waits for its child cleanups before the cancellation propagates, and a cancelled refresh of the tree publishes new pids together with their start identities, so they are not left excluded as unknown. A resident Claude stop that is cancelled now hurries its cleanup (skips the stdin-close grace) and waits for it before re-raising the cancellation. This stabilises the per-turn stop test of the process tree. No route, stored data format, Manager tool declaration or prompt changed. ([#620](https://github.com/foolzzz/looper/pull/620))

## [2.13.115] - 2026-10-25

### Fixed

- **The Gates list keeps its unread boundary across bounded refreshes, and a cancelled Planner stop no longer leaves Claude cleanup running.** The dashboard's gate list now asks the server for one row beyond its loaded window, so closed rows cannot erase the first unread gate's identity. When a saved boundary has no surviving row, the list restarts from the current rows only if the saved window had been exhausted; otherwise it fails with "The gate list changed while loading. Retry the request." instead of guessing. A page with no rows but more to load now still returns a continuation cursor. In the Planner, cancelling a stop while the resident Claude process is being cleaned up now hurries that cleanup (a kill skips the remaining stdin-close grace) and waits for it before returning, and the token files are removed in every case. No route, stored data format, Manager tool declaration or prompt changed. ([#618](https://github.com/foolzzz/looper/pull/618))

## [2.13.114] - 2026-10-25

### Fixed

- **Worker input delivery now tells a refusal from an unknown outcome, and unknown input is never replayed.** A run channel's `send` returns a `DeliveryResult` (`ACCEPTED`, `REJECTED` or `UNKNOWN`) instead of a bool-like success or an exception. Rejected means the runtime refused the input or nothing was written; unknown means bytes may have been written but no acknowledgement arrived (write-time timeout, broken pipe after writing, or a missing or malformed acknowledgement from OMP, Pi or Codex). The `delivered` field of the Worker input result message is now `true`, `false` or `null`. In the Planner, only a rejection keeps the user's message or steer for the Planner's next decision (`missed_messages`); an unknown outcome is reported to the user as unconfirmed and is not resent or kept. A timed-out Worker room reminder is no longer retried, and its UUID stays registered so a late echo is still suppressed and event reading continues. Also, a Claude turn abandoned by cancellation now discards the output the dying process still writes, so it is not attributed to a later turn. Planner chat replies for a forward changed: accepted keeps the reply, unknown says acceptance is unconfirmed and is not resent, rejected says the Worker did not accept it. No route, stored data format, Manager tool declaration or prompt changed. ([#607](https://github.com/foolzzz/looper/pull/607))

## [2.13.113] - 2026-10-25

### Added

- **Browser probes for the delayed configuration-download failure.** New `tests/test_configuration_download_browser.py` builds the dashboard and drives the configuration export in headless Chromium against synthetic API fixtures, comparing the saved bytes with the exported document for small and 5000-entry catalogs, immediate and delayed (activation expired) responses, and for cancelled, failed and invalid responses. Tests only, marked `installed` and skipped without Playwright or Chromium; they are reproduction probes, not evidence of a fixed defect. No production code changed. ([#614](https://github.com/foolzzz/looper/pull/614))

## [2.13.112] - 2026-10-25

### Fixed

- **A Manager cancelled mid-turn keeps its run record and text, and its Claude cleanup is finished.** When the message task is cancelled, the run record is now closed with the assistant text already handled, otherwise the deltas already shown, instead of no output; this also holds when the cancellation arrives during cleanup after a read failure or a stop. `ClaudeRunRecorder.end_left_open` gains an optional `output_text`. `ResidentClaude` runs the stop of a Claude process as its own tracked task: cancelling the caller does not cancel it, a turn or later `stop()` waits for it even after the process was forgotten, and a failing cleanup is logged. Manager and Claude CLI behavior only; no route, stored data format, Manager tool declaration or prompt changed. ([#612](https://github.com/foolzzz/looper/pull/612))
- **Selected Pi comparisons recompute their cost and repetitions.** `examples/pi/eval-prompt-comparison.sh` now sets `total_cost_usd` and `reps` of the subset it compares from the selected rows, not from the whole Pi run. Example script only; the package is unchanged. ([#610](https://github.com/foolzzz/looper/pull/610))

## [2.13.111] - 2026-10-25

### Fixed

- **A Telegram channel that fails while starting no longer leaks its resources.** If `initialize`, `start` or starting the poller raised (or was cancelled), `start()` now calls `stop()` before re-raising. The adapter builds and owns its two HTTP clients (one for polling, one for other calls, with the pool sizes `ApplicationBuilder` uses by default), because `Application.shutdown` does nothing when `initialize` failed. `stop()` now cancels the outbound task, stops the reaction tracker, stops the updater and application only if they are running, shuts down the application and both clients, and then clears its handles. A failure in one cleanup step is logged and does not skip the later ones. Channel adapter behavior only; no route, stored data, Manager tool declaration or prompt changed. ([#606](https://github.com/foolzzz/looper/pull/606))

## [2.13.110] - 2026-10-25

### Fixed

- **Chat sends and navigation stay bound to their conversation.** A send now belongs to the Manager and conversation it started in: a new send, or navigation to another Manager, a channel conversation or one that is loading, missing or another Manager's, aborts the request and closes the reply stream, even before the HTTP response arrives, and a late response or error is ignored instead of landing in the wrong thread. Sending from a read-only or unresolved view is refused. Choosing a row in the conversation list updates the address and the page adopts the thread from it; unsaved threads created in the page are remembered per Manager in local storage so Back and Forward return to them, while other empty ids show "Conversation not found." The page no longer saves the assistant reply itself, since the server's web channel adapter already saves it to the session the send was made from. Browser behavior only; no route, stored data, Manager tool declaration or prompt changed. ([#598](https://github.com/foolzzz/looper/pull/598))

## [2.13.109] - 2026-10-25

### Fixed

- **Channel saves keep their error class, and an unchanged Lark bot can be disabled without authentication.** Fixed validation messages for new or updated channel bots (missing Telegram token, Lark `app_id` or app secret, lark-cli profile, Manager reassignment) now reach the caller as `invalid` with that fixed text, never built from the request. A wrapped service failure with a 5xx status is returned as `unavailable` (`invalid credential request`) instead of being turned into a 400, and a channel lookup before a config update goes through the same error mapping, so a missing channel answers the same 404 with or without `config`. An update that only sets `enabled` to false on a Lark bot whose profile, `app_id`, token, `bot_token_env` and domain are unchanged no longer re-checks the Lark identity, so a bot with broken authentication can be turned off; every other Lark save still revalidates. Model-visible only as API error codes and messages of channel saves; no Manager tool declaration or prompt changed. ([#600](https://github.com/foolzzz/looper/pull/600))

## [2.13.108] - 2026-10-25

### Fixed

- **`loopercli` group help lists the commands generated from Looper's API.** `loopercli config --help` (or `-h`) now reads the OpenAPI document, so children such as `import-cancel`, `import-upload`, `import-apply` and `import-preview` appear beside the hand-written ones. Known hand-written children and other flags still make no request. If the document cannot be read, the help of the requested group is printed with a note that the API-made commands are not shown, and the exit code stays 0. Not model-visible; CLI help output only. ([#599](https://github.com/foolzzz/looper/pull/599))

## [2.13.107] - 2026-10-25

### Fixed

- **The task page confirms a restart before it ends recovery polling.** Start and Start over now read the task once before the request, so a Stop write that lands late is not taken for the requested run. After the request is accepted, a running task acknowledges the restart at once; a terminal task must show a changed status, `completed_at` or run count, with no run still counted as running, in two consecutive reads, because a detail read can show a new run count beside the old terminal row. While the request or the confirmation is pending, Start and Start over stay disabled and the Start over confirmation closes; a refused request re-enables them and shows its error, and leaving the task aborts the pending read. Not model-visible; dashboard only. ([#601](https://github.com/foolzzz/looper/pull/601))

## [2.13.106] - 2026-10-25

### Fixed

- **Credential-bearing dashboard operations keep actionable errors.** A new `PublicOpError` marks a domain refusal whose message is safe to show even when the operation accepts credentials; parser errors and arbitrary service exceptions stay private ("invalid credential request"). Provider-account not-found, conflict and invalid errors, Lark identity verification failures, and skill saves now return their own status and a stable explanation. Channel saves return "invalid sender allowlist: <format hint>" (never the submitted entry), "this bot is already added; update the existing bot instead", "Manager not found" or "channel not found"; a 500 stays private. An empty skill patch now reaches the skill service so it can say what to enter, and skill updates call the skill service directly so they cannot turn into an MCP write. Not a Manager tool change. ([#572](https://github.com/foolzzz/looper/pull/572))
- **The dashboard v1 migration is complete, and unused legacy code is gone.** Every operation now declares a concrete result schema (building the API document fails otherwise); `confirm` is registered last with the union of the results of the operations that need confirmation, `openapi` has a typed result, and client chat streams describe their events (`heartbeat` carries `{}`). The API document's description now states the public v1 scope. The unused `registryCall`, `putJson` and `failed` helpers and the `/api` base in `web/src/lib/api.ts`, and the unreachable config export/import and `_answer` handlers and two route patterns in `looper/agents/http_server.py`, are removed; docs now name the `/api/v1` routes. A test fails if production frontend code requests a non-v1 `/api` path. Model-visible only through the client API document. ([#591](https://github.com/foolzzz/looper/pull/591))

## [2.13.105] - 2026-10-25

### Fixed

- **The Gates page and its badge read only the rows they show, and a refresh no longer drops a loaded draft.** `fetchGates` used to read the server's whole ordered gate list even for a one-row badge. Ordinary reads (no saved cursor or `through` boundary) now request only the window `offset + limit` and check that the rows read match `min(window, total)`; saved boundaries still rebuild the full list so the server's ordering is kept. "Show more" now follows the server's `has_more` instead of comparing the loaded rows with the total, so an empty page no longer clears the saved cursor. A refresh across several windows rejects a changed `total` or a duplicated row with "Retry" and keeps the rows and any draft answer already on the page. Not model-visible; dashboard only. ([#586](https://github.com/foolzzz/looper/pull/586))

## [2.13.104] - 2026-10-25

### Fixed

- **The task page resumes Sessions activity after an asynchronous Start.** Start returns before the new run is visible, so the page could read the old terminal task, take it as the end of the new run and leave the Sessions stream closed. After a Start is accepted the page keeps polling the task until it sees the new run: a non-terminal status, a changed `completed_at`, or a higher run count (`cost.runs`). It then reopens the activity stream once, without mixing task ids or leaking subscriptions when the route changes during the request or the poll. A second click on Start while one is pending is ignored; a refused Start clears the pending state and shows its error; a run that ends as `failed` shows the task's error. Not model-visible; dashboard only. ([#590](https://github.com/foolzzz/looper/pull/590))

## [2.13.103] - 2026-10-25

### Fixed

- **Generated `looper config` subcommands are reachable from the command line.** `config import-upload`, `import-preview`, `import-apply` and `import-cancel` are generated from the service's OpenAPI document, but `config` is also a hand-written command, so the command line never loaded the document for it. It now loads the document when the word after `config` is not a registered hand-written child. The hand-written `config export` and `config import` keep their parsers; usage errors exit with code 2, send no operation and do not echo the argument, and an unreachable API still exits with code 4. Not model-visible. ([#583](https://github.com/foolzzz/looper/pull/583))

## [2.13.102] - 2026-10-25

### Changed

- **Task-creation confirmation for a new project now lists the project's execution settings.** When a client creates a task in a new project, the confirmation impact text, after the existing line about the new project and its working directory, lists every project execution control in schema order, including defaults and disabled controls. Examples are the per-task budget and its action, step review, step workspaces, whether wait steps may run shell commands, vault and operator-notes access, agent room and its limits. Each line names the field, its meaning and the effective value; a value the request did not set is marked as the default. The submitted arguments, validation and the same-session confirmation are unchanged and no row is written before confirmation. Model-visible: the impact text returned to the client changed. ([#581](https://github.com/foolzzz/looper/pull/581))

## [2.13.101] - 2026-10-25

### Fixed

- **The Planner's read-only inspection guidance now fits each runtime.** The default Planner prompt names the runtime's own reading tools (Read, Glob and Grep in Claude Code, equivalents elsewhere) and, where the runtime exposes a shell for reading as Codex does, allows read-only inspection such as `cat`, `ls` and `rg` under the existing sandbox, without seeking broader permissions; builds, tests and state-changing commands stay with Workers. The Planner turn now builds its system prompt after the launch is resolved, and a saved custom role prompt is still loaded unchanged. No write access is added. Model-visible: the default Planner system prompt text changed. ([#577](https://github.com/foolzzz/looper/pull/577))

## [2.13.100] - 2026-10-25

### Fixed

- **Saving or importing a Lark bot channel is refused when its app identity is ambiguous.** `refuse_same_bot` resolves the candidate's app through `_admission_app_id`: a lark-cli channel whose profile does not resolve to an app, or any stored lark-cli row that does not, is refused with HTTP 409 and a message that names no CLI error or profile. `update_bot` re-checks every Lark update under the bots lock, config import takes the same lock and refuses an unknown identity without partial writes, and the two built-in Lark rows that share an app on purpose (`builtin_lark_pair`) are not compared with each other. Behavior change for users: a Lark channel with an unresolvable lark-cli profile can no longer be saved or imported until the profile is fixed. Model-visible: none. ([#562](https://github.com/foolzzz/looper/pull/562))
- **The task-detail gate schema now matches the gate projection.** In `task_views.py`, a gate's `summary` is a nullable object of `decisions`, `assumptions` and `open` string arrays, `plan_change` a nullable object of `added`, `dropped` and `stopped` string arrays, and agenda `options` an array of strings; they were nullable strings and `{id, label}` objects. Declared schema only; response data and persisted gates are unchanged. Model-visible: none. ([#573](https://github.com/foolzzz/looper/pull/573))

## [2.13.99] - 2026-10-24

### Fixed

- **Saving a Manager template without `inputs` now stores an empty input list.** `put_template` in `looper/agents/managers_api.py` defaults an omitted `inputs` field to `[]`, so a template whose requirement names no placeholder can be created or replaced without declaring inputs, and a replace no longer keeps the old inputs. An explicit `null`, and any other non-list value or malformed item, is still refused, and a requirement with an unresolved placeholder is still refused. Model-visible: none. ([#576](https://github.com/foolzzz/looper/pull/576))

## [2.13.98] - 2026-10-24

### Fixed

- **A Worker's user-input delivery settles before the Planner stops waiting.** `WorkerPool.send_input` now bounds the input-channel wait and runtime handshake with one 4.5-second deadline, refuses without writing when the time left is below the channel's `send_timeout`, and never shortens the channel's own acceptance timeout. `RunChannel` gains `wait_input_ready()` and `send_timeout`; `_deliver_input` always publishes `WORKER_INPUT_RESULT`; the pool cancels and awaits its delivery tasks on stop. The Planner's `WORKER_INPUT_TIMEOUT` stays 5.0 seconds. Behavior change for users: a correction refused as late is no longer written afterwards. Model-visible: none. ([#554](https://github.com/foolzzz/looper/pull/554))

### Changed

- **Release evaluation tiers and the Pi comparison now match the 43-dialog selection.** The README uses explicitly authorized `*_DIALOG_CAP_USD` ceilings instead of caps measured on 24 dialogs. `eval-prompt-comparison.sh` runs Sonnet tasks and dialogs at two reps and the Opus tiers at one (37 case/reps), shares one cap and deadline, rejects incomplete, capped, skipped or mismatched reports, and records missing or partial baselines as unverified. Model-visible: none. ([#568](https://github.com/foolzzz/looper/pull/568))

## [2.13.97] - 2026-10-24

### Fixed

- **A reused process id can no longer be adopted into a turn's process tree.** `TurnTree.refresh` now checks observed process identities (start times) against the fresh process table before it expands the tree, and a pid whose start time changed stays excluded for the rest of the turn; `track` reads start times at turn start. Identities that cannot be proven are excluded for that observation, and a pid without an identity is ended only through the live subprocess handle, never by inferring a group. `end` refreshes before computing exclusions. Behavior change for users: stopping a turn no longer risks signalling an unrelated process that reused a pid mid-turn. Model-visible: none. ([#560](https://github.com/foolzzz/looper/pull/560))

## [2.13.96] - 2026-10-24

### Fixed

- **A bot row whose channel failed to start no longer reserves the bot's identity.** `_register_bots` recorded a row as started before its channel was registered, so when that channel failed, a later row for the same Telegram token or Lark app was refused as a duplicate while nothing served the bot. A row now counts as the owner only after its channel registered; the first row that starts owns the identity and later rows of that bot are still refused. Behavior change for users: with several rows for one bot, a failing row no longer blocks the next. Model-visible: none. ([#565](https://github.com/foolzzz/looper/pull/565))

## [2.13.95] - 2026-10-24

### Fixed

- **The Manager keeps ownership of a user turn through startup and shutdown.** Messages queued before the Manager's consumer exists (channel messages, journal replay) now get turn tokens when it starts, and a `/stop` among them is handled by code and never reaches the model; later messages from the same chat are registered after it. A `/stop` that reaches the message loop takes the same code path. A bridge write while the turn's events drain no longer clears the binding early: it is cleared when the turn is drained or boundedly abandoned, even if closing the run record is cancelled. When the message task is cancelled mid-turn, the turn is settled and its event handling abandoned, and the resident process is left to `ManagerAgent.stop` instead of being forgotten by repeated cancels. Behavior change for users: a queued or cancelled-turn `/stop` is honoured reliably. Model-visible: none. ([#557](https://github.com/foolzzz/looper/pull/557))

## [2.13.94] - 2026-10-24

### Fixed

- **The prompt-regression "start without waiting" check ignores subagent calls.** It pairs every start with its audit outcome, then anchors and counts only the Manager's own successful starts, so a subagent's failed or successful start no longer hides the Manager's start, and a subagent's native tools no longer count as the Manager waiting. Eval harness and tests only; behavior change for the product: none. Model-visible: none. ([#553](https://github.com/foolzzz/looper/pull/553))

## [2.13.93] - 2026-10-24

### Changed

- **Project management goes through v1.** Listing, creating, updating and deleting projects call the new `project_admin.list`, `.create`, `.update` and `.delete` operations (`POST /api/v1/project_admin/...`; the first is `read`, create and update `write`, delete `high` and confirmed by a client token). The compact `projects` operations gain typed result schemas. The dashboard uses them. Manager prompts and tools are unchanged. ([#523](https://github.com/foolzzz/looper/pull/523))
- **The operator API documentation names the v1 routes.** The READMEs, the agent-system architecture page and the design document list the migrated routes and their `OpOk`/`OpError` shapes; no code changed. ([#555](https://github.com/foolzzz/looper/pull/555))

### Removed

- **The legacy project routes:** `GET` and `POST /api/projects`, `PUT` and `DELETE /api/projects/{id}`. Use the `project_admin` operations. ([#523](https://github.com/foolzzz/looper/pull/523))

## [2.13.92] - 2026-10-24

### Fixed

- **A Planner's read permissions stay inside its own repository.** When the Planner's working directory is a symlink into a tree that is not a repository, the run no longer gets read access to the unrelated repository above the link. The link's own spelling still allows documents reached through it, and reads above the working directory inside the actual repository still work. Model prompts and tool declarations are unchanged. ([#552](https://github.com/foolzzz/looper/pull/552))

## [2.13.91] - 2026-10-24

### Changed

- **Task lists and full task details go through v1.** The Tasks list and a task's detail page call the new `tasks.page` and `tasks.detail` operations (`POST /api/v1/tasks/page` and `/detail`, both `read`, so client tokens may call them). The page takes the same filters as before (`project_id`, `schedule_id`, `group`, `status`, `before`, `limit`) plus a `query` that searches a task's id, title, requirement, project name and goal before paging, with status counts that follow it. Manager prompts and tools are unchanged. ([#532](https://github.com/foolzzz/looper/pull/532))
- **Scheduler, wait and knowledge-vault Settings go through v1.** Reading and saving them call the new `scheduler_settings`, `wait_settings` and `knowledge_vault` `.get` and `.update` operations (`POST /api/v1/...`), which use the same checks as before; the three updates are `high` risk and need confirmation from a client token. The dashboard uses them. Manager prompts and tools are unchanged. ([#549](https://github.com/foolzzz/looper/pull/549))

### Removed

- **The legacy task routes:** `GET /api/tasks` and `GET /api/tasks/<id>`. Use `tasks.page` and `tasks.detail`. ([#532](https://github.com/foolzzz/looper/pull/532))
- **The legacy settings routes:** `GET` and `PUT /api/scheduler-settings`, `/api/wait-settings` and `/api/knowledge-vault`. Use the `scheduler_settings`, `wait_settings` and `knowledge_vault` operations. ([#549](https://github.com/foolzzz/looper/pull/549))

## [2.13.90] - 2026-10-24

### Changed

- **Project setting layers and allowed profiles go through v1.** Reading and updating a project's settings (or one role's layer in it) and reading and replacing the profiles its Planners may choose call the new `project_agent_settings.get`, `.update`, `.update_role` and `allowed_profiles.get`, `.replace` operations (`POST /api/v1/...`), which use the same checks as before. An update that changes a concurrency limit or raises the spend cap asks for confirmation. The dashboard uses them. Manager prompts and tools are unchanged. ([#544](https://github.com/foolzzz/looper/pull/544))

### Removed

- **The legacy project routes:** `GET` and `PUT /api/projects/{id}/agent-settings`, `PUT /api/projects/{id}/agent-settings/roles/<role>` and `GET` and `PUT /api/projects/{id}/allowed-profiles`. Use the `project_agent_settings` and `allowed_profiles` operations. ([#544](https://github.com/foolzzz/looper/pull/544))

## [2.13.89] - 2026-10-24

### Changed

- **Provider account management in the dashboard goes through operator-only v1.** Listing, creating, editing and deleting a provider account and "Use the login again" call the new `provider_accounts.list`, `.create`, `.update`, `.delete` and `.clear_login_problem` operations (`POST /api/v1/provider_accounts/...`), which use the same registry checks as before. They refuse API clients, keep API keys out of the audit trail and answer credential errors without request details. The compact `providers.list`, `providers.register` and `providers.update` results are typed in the OpenAPI document. Manager prompts and tools are unchanged. ([#543](https://github.com/foolzzz/looper/pull/543))

### Removed

- **The legacy provider routes:** `GET` and `POST /api/providers`, `PUT` and `DELETE /api/providers/<id>` and `DELETE /api/providers/<id>/login-problem`. Use the `provider_accounts` operations. ([#543](https://github.com/foolzzz/looper/pull/543))

## [2.13.88] - 2026-10-24

### Changed

- **Agent Profile management and prompt previews in the dashboard go through v1.** Creating, editing and deleting a profile and previewing its prompt call the new `profiles.create`, `profiles.update`, `profiles.delete` (risk `high`, confirmed with the deletion impact) and `profiles.prompt_preview` (risk `read`) operations (`POST /api/v1/profiles/...`), which use the same registry checks as before. The compact `agent_profiles.*` results are typed in the OpenAPI document. Manager prompts and tools are unchanged. ([#540](https://github.com/foolzzz/looper/pull/540))

### Removed

- **The legacy profile routes:** `POST /api/agent-profiles`, `PUT /api/agent-profiles/<id>`, `DELETE /api/agent-profiles/<id>` and `GET /api/agent-profiles/<id>/prompt-preview`. Use the `profiles` operations. ([#540](https://github.com/foolzzz/looper/pull/540))

## [2.13.87] - 2026-10-24

### Changed

- **Model management in the dashboard goes through v1.** Adding, editing and deleting a saved model call the existing `models.create`, `models.update` and `models.delete` operations (`/api/v1/ops/models/...`; the provider is passed as `provider`), and a new `models.get` operation (`POST /api/v1/models/get`, risk `read`) reads one saved model with its settings, prices, usage and warnings. Model results are typed in the OpenAPI document. Manager prompts and tools are unchanged. ([#537](https://github.com/foolzzz/looper/pull/537))

### Removed

- **The legacy model routes:** `GET /api/models`, `POST /api/models`, `GET /api/models/<id>`, `PUT /api/models/<id>` and `DELETE /api/models/<id>`. Use the `models` operations. ([#537](https://github.com/foolzzz/looper/pull/537))

## [2.13.86] - 2026-10-24

### Changed

- **Configuration transfer in the dashboard goes through v1.** Exporting the configuration is `POST /api/v1/config/export`; uploading a file to preview an import is `POST /api/v1/config/import_upload` (risk `read`, the raw document streamed as `application/json`, up to the existing import size cap, answered in the v1 envelope); applying is `config.import_apply` and cancelling is the new `config.import_cancel` operation (risk `write`, moved to its own domain), each acting only on the caller's own staged import. The export is written to the response in chunks instead of being built in memory first. The preview, plan and document schemas are published in the OpenAPI document, and `looper config` keeps its export and import commands while generated subcommands can be added to it. Manager prompts and tools are unchanged. ([#513](https://github.com/foolzzz/looper/pull/513))

### Removed

- **The legacy configuration routes:** `GET /api/config/export`, `POST /api/config/imports`, `POST /api/config/imports/<id>/apply` and `DELETE /api/config/imports/<id>`. Use the `config` operations. ([#513](https://github.com/foolzzz/looper/pull/513))

## [2.13.85] - 2026-10-24

### Changed

- **Project knowledge and lesson drafts go through v1.** Listing a project's reference documents, adding, reading, updating and deleting one, and listing, confirming and discarding lesson drafts are the `project_knowledge` operations `list`, `get`, `drafts` (risk `read`), `add`, `update`, `confirm_draft` (risk `write`), `delete` and `discard_draft` (risk `high`), as `POST /api/v1/project_knowledge/<action>`. Their result schemas are published in the OpenAPI document. The two destructive operations remove only the selected item, and a client token needs a session confirmation first. Manager prompts and tools are unchanged. ([#534](https://github.com/foolzzz/looper/pull/534))

### Removed

- **The eight `/api/projects/{id}/knowledge` routes** (`GET` and `POST` on the collection, `GET`, `PUT` and `DELETE` on a document, `GET` on `drafts`, `POST` on a draft's `confirm`, `DELETE` on a draft). Use the `project_knowledge` operations. ([#534](https://github.com/foolzzz/looper/pull/534))

## [2.13.84] - 2026-10-24

### Changed

- **The complete runtime registry and runtime probing go through v1.** `registry.view` (risk `read`, operator-only) returns the full registry snapshot, with provider API keys masked and provider environment values unchanged, and `runtimes.probe` (risk `write`) re-detects one runtime, as `POST /api/v1/registry/view` and `POST /api/v1/runtimes/probe`. The result schemas are published in the OpenAPI document. Manager prompts and tools are unchanged. ([#530](https://github.com/foolzzz/looper/pull/530))

### Removed

- **`GET /api/registry` and `POST /api/runtimes/<id>/probe`.** Use the `registry` and `runtimes` operations. ([#530](https://github.com/foolzzz/looper/pull/530))

## [2.13.83] - 2026-10-24

### Changed

- **Dashboard chat sending and its reply streams go through v1.** Sending a chat message to the Manager or a running task's Planner, following one reply, and following an open thread are the operator-only operations `operator_chat.send` (risk `write`), `operator_chat.reply_events` and `operator_chat.thread_events` (risk `read`), as `POST /api/v1/operator_chat/<action>`. The send result carries `correlation_id` only. The task-template pattern check now waits up to 10 seconds for its checker to start before the matching budget begins. Manager prompts and tools are unchanged. ([#524](https://github.com/foolzzz/looper/pull/524))

### Removed

- **`POST /api/chat/send` and `GET /api/chat/events`.** Use the `operator_chat` operations. ([#524](https://github.com/foolzzz/looper/pull/524))

## [2.13.82] - 2026-10-24

### Changed

- **Global roles and the shared settings layers go through v1.** Listing, reading, updating and resetting the prompt of a global role, and reading and updating the system and role setting layers, are the operations `roles.list`, `roles.get`, `roles.update`, `roles.reset_prompt`, `agent_settings.get`, `agent_settings.update_system` (risk `high`) and `agent_settings.update_role`, as `POST /api/v1/<domain>/<action>`. For API clients, `update_system` and `update_role` ask for confirmation first (shared defaults, concurrency, or a higher spend cap). `roles.update` now rejects unknown or read-only fields instead of ignoring them. Manager prompts and tools are unchanged. ([#480](https://github.com/foolzzz/looper/pull/480))

### Removed

- **`GET /api/roles`, `GET` and `PUT /api/roles/{role}`, `POST /api/roles/{role}/reset-prompt`, `GET /api/agent-settings`, `PUT /api/agent-settings/system` and `PUT /api/agent-settings/roles/{role}`.** Use the `roles` and `agent_settings` operations. ([#480](https://github.com/foolzzz/looper/pull/480))

## [2.13.81] - 2026-10-24

### Changed

- **Schedule changes and immediate runs go through v1.** Creating, updating (including enabling and disabling), deleting and resetting the Planner session of a schedule, and running one now, are the operations `schedule_admin.create`, `.update` (risk `write`), `.delete`, `.reset_planner_session` and `.run` (risk `high`), as `POST /api/v1/schedule_admin/<action>`. Everything but create can ask for confirmation first; an update asks only when it enables a schedule or raises a cap. Manager prompts and tools are unchanged. ([#500](https://github.com/foolzzz/looper/pull/500))

### Removed

- **`POST /api/projects/{id}/schedules`, `PUT` and `DELETE /api/schedules/{id}`, `POST /api/schedules/{id}/reset-planner-session` and `POST /api/schedules/{id}/run`.** Use the `schedule_admin` operations. ([#500](https://github.com/foolzzz/looper/pull/500))

## [2.13.80] - 2026-10-24

### Changed

- **Task deletion and step-workspace management go through v1.** Deleting a stopped task, discarding a stopped task's step workspace and checking a workspace before creating a task are now `tasks.delete`, `workspaces.discard` (both risk `high`, both ask for confirmation) and `workspaces.check` (risk `read`), as `POST /api/v1/tasks/delete`, `POST /api/v1/workspaces/discard` and `POST /api/v1/workspaces/check`. The cleanup order, the refusal to delete a running task and the kept result branch are unchanged. Manager prompts and tools are unchanged. ([#516](https://github.com/foolzzz/looper/pull/516))

### Removed

- **`POST /api/tasks/delete`, `POST /api/tasks/{id}/workspaces/{n}/discard` and `GET /api/workspaces/check`.** Use the `tasks.delete`, `workspaces.discard` and `workspaces.check` operations. ([#516](https://github.com/foolzzz/looper/pull/516))

## [2.13.79] - 2026-10-24

### Changed

- **Channel bot settings are managed through v1.** Listing, reading, adding, updating and removing Telegram and Lark bots are now `channel_configs.list`, `channel_configs.get`, `channel_configs.create`, `channel_configs.update` and `channel_configs.delete` (`POST /api/v1/channel_configs/<action>`). Creating and updating are operator-only credential writes; the token is write-only and responses show only whether one is stored. Removal asks for confirmation. A bot's Manager still cannot change. Manager prompts and tools are unchanged. ([#518](https://github.com/foolzzz/looper/pull/518))

### Removed

- **`GET`, `POST`, `PUT` and `DELETE` on `/api/channel-configs` and `/api/channel-configs/<id>`.** Use the `channel_configs` operations. ([#518](https://github.com/foolzzz/looper/pull/518))

## [2.13.78] - 2026-10-24

### Changed

- **The dashboard Chat page reads and saves conversations through v1.** Targets, conversation list, message history and message saving are now the operator-only operations `operator_chat.targets`, `operator_chat.sessions`, `operator_chat.messages` and `operator_chat.save_message` (`POST /api/v1/operator_chat/<action>`). Thread ownership across Managers is kept. A database failure now answers unavailable instead of an empty history, and a failed save no longer answers `ok`. Manager prompts and tools are unchanged. ([#510](https://github.com/foolzzz/looper/pull/510))

### Removed

- **`GET /api/chat/targets`, `GET /api/chat/sessions`, `GET /api/chat/messages` and `POST /api/chat/messages`.** Use the `operator_chat` operations with an operator credential. `/api/chat/send` and `/api/chat/events` are unchanged. ([#510](https://github.com/foolzzz/looper/pull/510))

## [2.13.77] - 2026-10-24

### Changed

- **The costs query has a typed result contract.** The existing `looper_costs` Tool (`POST /api/v1/ops/costs`) now declares its answer in the versioned API document for all three scopes: a task (`task_id`), a project (`project`) and global spend. The request, arithmetic, transport and risk (read) are unchanged, and Manager prompts and tools are unchanged. ([#514](https://github.com/foolzzz/looper/pull/514))

## [2.13.76] - 2026-10-24

### Changed

- **Run records and usage move to the versioned API.** `runs.for_task`, `runs.list`, `runs.get`, `runs.events` and `runs.usage` (`POST /api/v1/runs/<action>`) are operator-only reads that replace five legacy routes (`GET /api/tasks/{id}/runs`, `GET /api/llm-records`, `GET /api/llm-records/{id}`, `GET /api/llm-records/{id}/events`, `GET /api/agents/{id}/usage`), which are removed. Requests are closed and typed, and a database failure answers `unavailable` instead of an empty list. The dashboard, the prompt-regression runner and the example smoke scripts use the new routes. Manager prompts and tools are unchanged. ([#506](https://github.com/foolzzz/looper/pull/506))
- **Chat controls and Manager sessions move to the versioned API.** `manager_session.get`, `manager_session.select_profile`, `operator_chat.turn`, `operator_chat.stop`, `chat_settings.get` and `chat_settings.update` (`POST /api/v1/<domain>/<action>`) replace the legacy `GET /api/manager/session`, `PUT /api/manager/session/profile`, `GET /api/chat/turn`, `POST /api/chat/stop` and `GET`/`PUT /api/chat-settings`, which are removed. `operator_chat.turn` and `operator_chat.stop` are operator-only, and changing the chat stop setting asks for confirmation. The dashboard and the eval scripts use the new routes. Manager prompts and tools are unchanged. ([#507](https://github.com/foolzzz/looper/pull/507))

### Fixed

- **The dashboard builds again.** An unused helper left by an earlier v1 migration failed the TypeScript build; it is removed and the Dashboard CI job now type-checks (`tsc -b`) before its tests. ([#512](https://github.com/foolzzz/looper/pull/512))
- **Agent system tests are green on main again.** A Manager dialog test and a Planner test broke when the run-records and session domains merged together; the fix is test-only (the fake Planner Claude can keep a session per working directory). ([#515](https://github.com/foolzzz/looper/pull/515))

## [2.13.75] - 2026-10-24

### Changed

- **API client tokens move to the versioned API.** `clients.list`, `clients.create` and `clients.revoke` (`POST /api/v1/clients/<action>`) are operator-only and replace three legacy routes (`GET` and `POST /api/clients`, `DELETE /api/clients/{id}`), which are removed. An API client token is refused, arguments and results stay out of the action audit, and create answers 200 with the token in `result` (shown once) instead of a bare 201. The list no longer returns the always-null `revoked_at`. `loopercli clients list|create|revoke` takes an operator token for one call, and the dashboard and the prompt-regression runner use the new routes. Manager prompts and tools are unchanged. ([#472](https://github.com/foolzzz/looper/pull/472))

## [2.13.74] - 2026-10-24

### Changed

- **Project role templates and role statistics move to the versioned API.** `role_templates.list`, `role_templates.put`, `role_templates.delete` and `project_roles.stats` (`POST /api/v1/<domain>/<action>`) replace four legacy HTTP routes (`GET /api/projects/{id}/role-templates`, `PUT` and `DELETE /api/projects/{id}/role-templates/{key}`, `GET /api/projects/{id}/role-stats`), which are removed. Requests are closed and typed and the answers declare full template, item and statistics definitions. Deleting a template asks for confirmation; task roles already copied from a template keep their content and versions. The dashboard and the prompt-regression runner use the new routes. Manager prompts and tools are unchanged. ([#503](https://github.com/foolzzz/looper/pull/503))

## [2.13.73] - 2026-10-24

### Changed

- **Dashboard task creation goes through the v1 API.** The dashboard now creates tasks with `POST /api/v1/tasks/create`, which uses the shared creation core and keeps the full project and task configuration. The old `POST /api/tasks/create` route is removed; the v1 answer wraps the same fields in `result` and returns 200 instead of 201. An operator's call is not written to the Manager/client action trail; a client that creates a new project must confirm in the same session. The created task is read back and checked, and a mismatch fails with an `unavailable` error. A worker whose Claude process closes stdin early now still releases the input-ready wait. Manager prompts and tools are unchanged. ([#476](https://github.com/foolzzz/looper/pull/476))

## [2.13.72] - 2026-10-24

### Changed

- **v1 domain modules register themselves.** The package imports every public module under `looper/agents/v1_domains/` instead of reading a shared list, so parallel domain changes no longer conflict. An optional integer `ORDER` keeps the OpenAPI order (existing modules have one); a missing export, a bad `ORDER`, or a duplicate operation id or route fails with a named error when the domains load. No operation, route or OpenAPI output changes. Manager prompts and tools are unchanged. ([#495](https://github.com/foolzzz/looper/pull/495))

## [2.13.71] - 2026-10-24

### Changed

- **Schedule definitions, the rule preview, run history and notifications move to the versioned API.** `schedule_admin.list`, `project_list`, `get`, `preview`, `runs` and `notifications` (`POST /api/v1/schedule_admin/<action>`) replace six legacy HTTP routes (`GET /api/schedules`, `GET /api/projects/{id}/schedules`, `GET /api/schedules/{id}`, `POST /api/schedules/preview`, `GET /api/schedules/{id}/runs` and `/notifications`), which are removed; the dashboard and the prompt-regression runner use the new routes. Requests are closed and typed, and the answers declare full Schedule, Run, limits, preview and notification definitions. The schedule write routes and scheduler settings are unchanged. Manager prompts and tools are unchanged. ([#494](https://github.com/foolzzz/looper/pull/494))

## [2.13.70] - 2026-10-24

### Changed

- **Task activity, the task conversation and the live activity stream move to the versioned API.** `tasks.activity`, `tasks.activity_detail`, `tasks.conversation` and `tasks.activity_stream` (`POST /api/v1/tasks/<name>`) replace four legacy GET routes (`/api/tasks/{id}/activity`, `/activity/{seq}`, `/activity/stream` and `/conversation`), which are removed; the dashboard and the generated CLI (`loopercli tasks activity-stream`) use the new routes. A database failure now answers `unavailable` instead of an empty list, an event of another task answers not found, and the conversation list key is `rows`. The stream sends complete events read from the database and ends on client disconnect. Manager prompts and tools are unchanged. ([#488](https://github.com/foolzzz/looper/pull/488))

## [2.13.69] - 2026-10-24

### Changed

- **The dashboard overview, task controls, task titles and task requirements move to the versioned API.** `system.overview` (`POST /api/v1/system/overview`), `task_control.start`, `stop`, `restart`, `start_over` and `cancel` (`POST /api/v1/ops/task_control/<action>`), `task_update` (`POST /api/v1/ops/task_update`) and `tasks.requirement_update` (`POST /api/v1/tasks/requirement_update`) replace seven legacy HTTP methods (`GET /api/overview`, `POST /api/tasks/cancel`, `/stop` and `/start`, `POST /api/tasks/{id}/start`, `PUT /api/tasks/{id}/requirement` and `/title`), which are removed; the dashboard uses the new routes. An API client's requirement edit needs a second-step confirmation. Starting a ready task no longer checks the work directory first. A message sent to a dispatched Worker before its input channel opens now waits for startup instead of being dropped. Manager prompts and tools are unchanged. ([#491](https://github.com/foolzzz/looper/pull/491))

## [2.13.68] - 2026-10-24

### Changed

- **Scenario Managers and task templates move to the versioned API.** `managers.list`, `managers.create`, `managers.update`, `managers.delete`, `manager_templates.put` and `manager_templates.delete` under `/api/v1/` replace six legacy HTTP methods (`GET`/`POST /api/managers`, `PUT`/`DELETE /api/managers/{id}`, `PUT`/`DELETE /api/managers/{id}/templates/{key}`), which are removed; the dashboard uses the new routes. Creating a Manager now answers 200 instead of 201. An API client's definition or template write needs a second-step confirmation. Manager prompts and tools are unchanged. ([#487](https://github.com/foolzzz/looper/pull/487))

## [2.13.67] - 2026-10-24

### Changed

- **Lark channel discovery and gate notification settings move to the versioned API.** `channels.status`, `channels.people`, `gate_settings.get` and `gate_settings.update` under `/api/v1/` replace six legacy HTTP methods (`GET /api/channels/{lark|lark_bot}/status`, `GET /api/channels/{lark|lark_bot}/people`, `GET`/`PUT /api/gate-settings`), which are removed; the dashboard uses the new routes. An API client's gate target change needs a second-step confirmation. Manager prompts and tools are unchanged. ([#481](https://github.com/foolzzz/looper/pull/481))

## [2.13.66] - 2026-10-24

### Changed

- **Catalog acquisition moves to the versioned API.** `catalog_sources.import_machine`, `import_mcp`, `upload`, `git_preview`, `git_import`, `create_skill` and `create_mcp` under `/api/v1/` replace seven legacy POST routes (`/api/catalog/import`, `/upload`, `/git/preview`, `/git/import`, `/skills`, `/mcp`, `/mcp/import`), which are removed; the dashboard uses the new routes. Machine and MCP import and MCP creation are operator-only; a client's Git import needs a second-step confirmation and is refused if the repository moved since the preview. Manager prompts and tools are unchanged. ([#484](https://github.com/foolzzz/looper/pull/484))

## [2.13.65] - 2026-10-24

### Changed

- **Task gates, waits, role edits and the task room move to the versioned API.** The gate list and discussion (`ops/gates/*`), `waits.resume`/`waits.cancel`, `tasks.role_update` and `tasks.room_view` under `/api/v1/` replace eight legacy HTTP methods (`GET /api/gates`, `GET`/`POST /api/tasks/{id}/gates/{step}/messages`, `POST .../gates/{step}/end`, `POST .../waits/{step}/{resume|cancel}`, `PUT .../roles/{key}`, `GET .../room`), which are removed; the dashboard and the prompt-evaluation and DSH scripts use the new routes. A gate message posted without an API client is now attributed to the dashboard. Manager prompts and tools are unchanged. ([#474](https://github.com/foolzzz/looper/pull/474))

## [2.13.64] - 2026-10-24

### Added

- **`api` runtime type, first step.** Adds the in-process Model API adapter (OpenAI Responses over an API key, Manager only, no executable) and the registry support it needs: a typed per-adapter role restriction (`RuntimeAdapter.roles`), in-process detection and probing (`RuntimeAdapter.in_process`) and import messages. The adapter is not in `ADAPTERS`, so it cannot be detected, selected or started and users see no change; no prompt or tool changed. Adds the `openai` package as a dependency. ([#478](https://github.com/foolzzz/looper/pull/478))

## [2.13.63] - 2026-10-24

### Documentation

- **API runtime RFC.** Adds the approved RFC for an agent runtime that calls model APIs directly through the official Python SDKs, with the Manager as its first role, and its development schedule (stage 1: `openai` SDK on OpenAI Responses; stage 2: `anthropic` SDK). Documentation only; no code, prompt or tool changes. ([#475](https://github.com/foolzzz/looper/pull/475))

## [2.13.62] - 2026-10-23

### Changed

- **Task configuration, placement, Agent choices and budget continuation move to the versioned API.** `tasks.configure/move/agents/continue` under `/api/v1/` replace four legacy HTTP methods (`PUT /api/tasks/{id}/config`, `PUT /api/tasks/{id}/project`, `POST /api/tasks/{id}/continue`, `PUT /api/tasks/{id}/agents`), which are removed; the dashboard uses the new routes. Closed request schemas reject unknown fields, a failed configuration write is now an error, and continuing a budget-failed task is `high` risk: the confirmation names whether the task's own cap or the project's per-task cap changes and is refused if that scope changed meanwhile. Ending a turn's process tree now finishes cleanup when its preparation is cancelled. Manager prompts and tools are unchanged. ([#467](https://github.com/foolzzz/looper/pull/467))

## [2.13.61] - 2026-10-23

### Changed

- **Project-owned skills move to the versioned API.** `project_skills.add/get/update/delete` under `/api/v1/` replace four legacy HTTP methods (`POST /api/projects/{id}/skills`, `GET`/`PUT`/`DELETE /api/projects/{id}/skills/{skill id}`), which are removed; the dashboard's project Skills tab uses the new routes. Results are typed, `enabled` is switched through `installs.update`, and deleting a skill is `high` risk: the confirmation lists every installation and project, and references are read again at the delete. Manager prompts and tools are unchanged. ([#470](https://github.com/foolzzz/looper/pull/470))

## [2.13.60] - 2026-10-23

### Changed

- **Catalog items and installations move to the versioned API.** `catalog_items.list/get/update/delete` and `installs.create/update/delete/project` under `/api/v1/` replace eight legacy HTTP methods (`GET /api/catalog`, `GET`/`PUT`/`DELETE /api/catalog/{id}`, `POST /api/installs`, `PUT`/`DELETE /api/installs/{id}`, `GET /api/projects/{id}/installs`), which are removed; the dashboard uses the new routes. Item edits are operator-only and kept out of the audit trail because MCP patches can carry secrets; deleting an item is `high` risk. Full catalog views and the compact `looper_catalog` results are typed in the OpenAPI document. Manager prompts and tools are unchanged. ([#466](https://github.com/foolzzz/looper/pull/466))

## [2.13.59] - 2026-10-23

### Changed

- **Dashboard status moves to the versioned API.** `POST /api/v1/system/status` (body `{}`, answer in `result`) replaces `GET /api/status`, which is removed and now answers 404; the dashboard, `looper service status` and the service start-up probe use it. A v1 domain registry and a shared dashboard transport (`web/src/lib/v1.ts`) are added for the next migrations. With the operator credential a `high` `/api/v1/` operation now runs in one request; client tokens keep the two-step confirmation. Manager prompts and tools are unchanged; the OpenAPI document gains the status operation and error `details`. ([#457](https://github.com/foolzzz/looper/pull/457))

### Fixed

- **A cancelled process-tree stop reaps the child.** `proc_tree.end` and the turn tree's stop reap in a `finally`. ([#457](https://github.com/foolzzz/looper/pull/457))

## [2.13.58] - 2026-10-23

### Fixed

- **A read-only Planner can read its steps' worktrees and symbolic-link paths.** On Claude Code the Planner is now allowed to read the isolated worktrees of the steps its task still uses, and a directory in either its resolved or its written spelling. It can no longer use `EnterWorktree`, and its recommended prompt says to inspect with Read, Glob and Grep and leave commands to Workers (model-visible: its prompt line and tool list change). Other runtimes are unchanged. ([#461](https://github.com/foolzzz/looper/pull/461))

## [2.13.57] - 2026-10-23

### Fixed

- **A read-only Planner can read a document path it is given.** On Claude Code the Planner chose `Bash cat`, the read-only run refused it, and the Planner dropped the document. It now reads with Read, Glob and Grep, allowed for its repository and mounted knowledge directories, and has no Bash tool (model-visible: its tool list loses Bash). Other runtimes are unchanged. ([#452](https://github.com/foolzzz/looper/pull/452))

## [2.13.56] - 2026-10-23

### Changed

- **One implementation per task operation.** The console and the Manager now share one core for creating a task, the task list and detail, stop, cancel, restart, start over and supplements. The console checks the task's state first and answers 409 (404 for an unknown task, 400 for one without a directory) instead of publishing silently, and the dashboard shows the error of a failed Stop or Cancel. No model-visible change is intended. ([#429](https://github.com/foolzzz/looper/pull/429))

## [2.13.55] - 2026-10-23

### Fixed

- **loopercli and the eval and test guards.** `loopercli` hides a token (`lpc_…`) in every error it prints, not only in usage errors, and a Ctrl-C while a reply streams keeps the conversation id so the next message continues it. The prompt-regression stop dialog no longer flags a reply that offers to do the work, and its start-without-waiting check reads wrapped tool calls and anchors on the first start that succeeded. No model-visible change. ([#448](https://github.com/foolzzz/looper/pull/448))

## [2.13.54] - 2026-10-23

### Fixed

- **Chat/stop: how a Manager turn ends.** A stop from a chat reaches the turns of that channel (matched by channel id, not only channel type), and a stopped turn answers the chat with the text it had already streamed, also when the Manager is retired during the stop. A message stopped before the model had it is written within a time limit, a runtime that lets the cancel escape ends the turn stopped, and closing the model run record no longer waits longer than its limit when the database ignores cancellation. No model-visible change. ([#446](https://github.com/foolzzz/looper/pull/446))

## [2.13.53] - 2026-10-23

### Fixed

- **Channels: serve startup and delivery.** Channel bots start receiving only once every enabled Manager is known and poll before the advisory startup checks, so messages sent during a slow check are received. A message queued during startup is no longer delivered twice after the journal replay, and old status entries are settled instead of replayed. Config export and import preview read channels before the Manager migration. No model-visible change. ([#434](https://github.com/foolzzz/looper/pull/434))

## [2.13.52] - 2026-10-23

### Fixed

- **loopercli config import follow-ups.** A staged configuration import can be cancelled with `loopercli config import --cancel IMPORT_ID` (new v1 operation `config.import_cancel`, limited to the caller that staged it). The limit of four pending imports now counts per caller instead of per server. The import preview prints the full plan, and a failed export write removes the partial file. ([#440](https://github.com/foolzzz/looper/pull/440))

## [2.13.51] - 2026-10-23

### Fixed

- **Stop runtimes: process tree and runtime binding.** Stop no longer remembers or signals the children of a process whose pid was reused: reused leaders are excluded before the tree is refreshed, and remembered groups whose leader is gone are dropped. A Grok turn that falls back from ACP to the legacy transport now announces the transport so the manager rebinds the turn before its tool bridge can call a tool. The stop real-path write probe passes only when the turn's final answer has the outcome `answered`. No model-visible change. ([#442](https://github.com/foolzzz/looper/pull/442))

## [2.13.50] - 2026-10-23

### Fixed

- **Web chat address and User Gates page.** A bare chat address now names its thread in place, so Back and Forward restore the matching conversation, and a missing Planner or channel conversation is reported instead of adopted as a new web one. `/new` is case-insensitive, the session profile label shows its effort, and selecting Room again keeps its filters. `GET /api/gates` gains `cursor` and `through` parameters and returns `next_cursor` and `has_more`, so the User Gates page refreshes its loaded rows without losing them or their reply drafts when a gate closes or a new one opens. No model-visible change. ([#436](https://github.com/foolzzz/looper/pull/436))

## [2.13.49] - 2026-10-23

### Fixed

- **Tasks start in workspaces on a mounted filesystem.** Repository discovery now crosses filesystem boundaries (`GIT_DISCOVERY_ACROSS_FILESYSTEM=1`), so a workspace on a mount is isolated in its parent repository or runs in place when there is none, instead of being refused with a "mount point" diagnostic. Invalid Git directories and other inspection failures still fail visibly. The Linux Python Tests job now runs on Ubuntu 24.04 with PostgreSQL 16 clients matching the service. No model-visible change. ([#427](https://github.com/foolzzz/looper/pull/427))

## [2.13.48] - 2026-10-23

### Breaking changes

- **Configuration export and import require the running service.** `looper config export` and `looper config import` have been removed. Use `loopercli config export <file>` and `loopercli config import <file>` instead; apply a preview with `loopercli config import --apply <import_id>`, then `loopercli confirm <confirmation_id>`.
- **配置导出、导入需要运行中的服务。** 已删除 `looper config export` 和 `looper config import`，请改用 `loopercli config export <file>` 和 `loopercli config import <file>`；应用预览用 `loopercli config import --apply <import_id>`，再用 `loopercli confirm <confirmation_id>` 确认。

### Added

- **`loopercli auth`, `doctor` and `config` through the v1 API.** New v1 operations `auth.set_password` and `auth.clear_password` (operator identity only; an API client token gets 403 `forbidden`), `system.check`, `config.export`, `config.import_preview` and `config.import_apply` (needs a client confirmation). `loopercli auth set-password|clear-password`, `loopercli doctor` and `loopercli config export|import` call them. `looper auth` and `looper doctor` are unchanged. Pending configuration imports now belong to the caller that staged them. Model-visible: the `looper_config_export` result `note` now names the `loopercli` commands; the OpenAPI document lists six more operations. ([#430](https://github.com/foolzzz/looper/pull/430))

## [2.13.47] - 2026-10-23

### Fixed

- **The nightly installed tests build the dashboard first.** `.github/workflows/installed-tests.yml` now runs `scripts/stage-dashboard.sh` before the installed tests, as the release workflow does, so the browser tests find the dashboard. The same diff also pins `actions/setup-node` to `v6` (was `v7`). Behavior change: none in the program, only the nightly CI workflow. Model-visible: none. ([#433](https://github.com/foolzzz/looper/pull/433))

## [2.13.46] - 2026-10-23

### Fixed

- **The Manager's reply no longer asks to start a task it already started.** When the Manager creates a task and starts it in the same turn, the task's configuration block in the reply now reads "任务已创建：" without the "Ready — not started" wording and without the question whether to start it. A start that fails, a turn that does not start the task, and a start of a different task keep the block and the question as before. Behavior change: the text of the Manager's reply in that case. Model-visible: no change to the Manager's prompt or tool schemas (the `looper_task_create` result still carries its `shown_to_user` note). ([#428](https://github.com/foolzzz/looper/pull/428))

## [2.13.45] - 2026-10-22

### Added

- **`loopercli`, `looper-skill` and an OpenAPI-described API (`/api/v1`) (stage 1 of the client-skill RFC).** An agent in another session, or a script, can hand work to a Manager and follow tasks: API clients with tokens made in Settings (accepted from loopback only, SHA-256 stored), operations generated from the ops catalog with per-client, per-session confirmation of high-risk writes, chat with a Manager over a server-sent event stream, the `loopercli` command (generated commands, `login`, `confirm`, `chat`, `wait`, `skill`), and `looper-skill` shipped in the wheel and as a Claude Code plugin. `schema.sql` adds `api_clients` and a nullable `client_id` on `chat_threads`, `tasks` and `manager_actions`. Behavior change: new `/api/v1` and `/api/clients` routes, a new command, a task created in a client's conversation reports into that conversation. Model-visible: a Manager message through a client carries the line `Channel: loopercli, client "<name>" ...`; the client has the Manager's tools less two plus `looper_task_pipeline` and `looper_gates`; `looper-skill` is new agent-facing text. ([#411](https://github.com/foolzzz/looper/pull/411))

## [2.13.44] - 2026-10-22

### Changed

- **Tests: one test per shared stop and runtime path; installed-CLI tests nightly (step pr3).** The stop matrix runs each shared code path on one runtime (per-turn processes: Codex runs all windows, OMP, pi and DSH only their own event-stream windows), tests that need an installed runtime CLI or a browser carry the new `installed` marker and run in a new nightly `Installed runtime tests` workflow, and the main test job excludes them. Tests, CI and one RFC page only; no change to `looper/`, no behavior change, no model-visible change. ([#422](https://github.com/foolzzz/looper/pull/422))

## [2.13.43] - 2026-10-22

### Changed

- **Supported upgrades start at v2.12.** Installations on older versions must upgrade to v2.12 first, then upgrade to the current version.
- **Tests: migration-era launch snapshots and harness self-tests dropped.** The pinned launch fixtures no longer carry prompt text, three main-branch snapshot fixtures and the tests that only exercised the parity harness are removed, and the slow tests that clone an old revision to prove a fixture's origin run in a separate `Launch fixture provenance` workflow (marker `provenance`, excluded from the main test job). Tests and CI only; no change to `looper/`, no behavior change, no model-visible change. ([#416](https://github.com/foolzzz/looper/pull/416))

## [2.13.42] - 2026-10-22

### Fixed

- **Follow-ups of #399.** A Manager's session view loads the follow-settings profile with its own id; `PUT /api/managers/:id/templates/:key` answers 400 for the default Manager, whose task tool takes no template; `match_patterns` kills its subprocess when cancelled; `ManagerOps.call()` checks arguments against the tool of the scope in force; starting a ninth Manager raises so the sync stays pending and retries; retiring a Manager sends an `outcome=stopped` final reply for its in-flight user message; switching Manager in the chat closes the New session panel; `GET /api/chat/messages` and the session event stream treat a missing `manager` as the default Manager and answer 409 for another Manager's thread. Behavior change: the template request on the default Manager is refused. Model-visible: a tool call is validated against the current scope's schema; no prompt or tool description changed. ([#408](https://github.com/foolzzz/looper/pull/408))

## [2.13.41] - 2026-10-22

### Changed

- **Test suite slimming, step pr4.** Validation parametrizations keep one value per branch, and the PostgreSQL tests share one helper module (`tests/db_support.py`) for the database URL and a per-test schema; the scheduler tests share `tests/schedule_support.py`, and schedule request validation has database-free tests (`tests/test_schedule_validation.py`). Behavior change: none. Model-visible: none. ([#415](https://github.com/foolzzz/looper/pull/415))

## [2.13.40] - 2026-10-22

### Fixed

- **Prompt-regression dialogs: the withdraw and stop checks no longer fail correct real-mode replies.** `withdraw_then_continue` accepts a first-turn reply that names every project, since each Manager message starts with the projects; `stop_then_continue` counts a "汇总" reply as a false claim only when this turn read no single project's tasks and the reply does not say the work was not done. Behavior change: none in the product. Model-visible: none. ([#410](https://github.com/foolzzz/looper/pull/410))

## [2.13.39] - 2026-10-22

### Changed

- **Test suite slimming, step pr1.** Each pytest-xdist worker gets a PostgreSQL database of its own, long prompt-eval tests are split by scenario, fixed sleeps are gone from the touched tests, and the Python tests job reports the 25 slowest tests (`--durations=25`). Behavior change: none. Model-visible: none. ([#409](https://github.com/foolzzz/looper/pull/409))

## [2.13.38] - 2026-10-22

### Fixed

- **Stop v2: the blockers for turning Stop on (#339).** `chat.stop_enabled` stays off by default. A stop is now offered only where the Manager's runtime can interrupt (Claude Code, Grok over ACP); a runtime run per turn refuses it (`cannot_interrupt` from `POST /api/chat/stop`, `can_interrupt` on `GET /api/chat/turn` and the session summaries, a disabled Stop button in the dashboard) and never kills its processes for it. A stop no longer ends a resident Claude's other processes after the interrupt. A stopped turn waits at most 3 s for its pending events and 10 s for its own record and session writes, and a runtime's events are accounted at once and recorded in order, so a slow database holds back neither the reply nor the bridge's call matching. The MCP bridge records how call matches went, and `agent.phase` events of the web channel carry `activity` and `tool`. Behavior change: none with Stop off. Model-visible: none. ([#362](https://github.com/foolzzz/looper/pull/362))

## [2.13.37] - 2026-10-22

### Added

- **A bot per Manager.** Each Telegram or Lark row of `channel_configs` is now one bot with its own runner and one Manager (`channel_configs.manager_id`, fixed when the bot is added); the three built-in rows stay the default Manager's. Settings → Channels → Add a bot (`POST /api/channel-configs`, `DELETE /api/channel-configs/<id>`) creates a `channel-<type>-<n>` bot for any Manager, with its token or app secret entered in the dashboard and kept in `channel_secrets` (an added bot never reads the environment, and the same bot cannot be added twice). Its threads are `<channel_id>:<chat_id>`, and task end notices, wait and concurrency notices and gate posts go through the bot the task came through. Added bots are not exported, and a configuration file naming one is refused. Behavior change: none for an install with only the built-in channel rows; the Lark bot token channel's app secret is now masked in logs. Model-visible: none (the `Channel:` line names only the type). ([#403](https://github.com/foolzzz/looper/pull/403))

## [2.13.36] - 2026-10-22

### Fixed

- **Chat: the Agent line names the session's profile without `undefined`.** The chat header and the new-session panel showed `name — undefined · undefined / undefined / undefined`, because `GET /api/manager/session` returns the effective profile as a summary (`profile_id`, `name`, `runtime`, `provider`, `model`, `effort`) and the dashboard read it as a registry profile. A `ManagerSessionProfileSummary` type and `sessionProfileLabel` match the server. Behavior change: the label reads `name — runtime · provider / model`. Model-visible: none. ([#398](https://github.com/foolzzz/looper/pull/398))

## [2.13.35] - 2026-10-22

### Added

- **Scenario Managers, stage 1.** Several Managers can be online at once. Each has its own duty prompt, Agent Profile, tool scope (`tools`: `all` or `tasks`), task scope (`task_scope`: `all` or `own`), task policy (`free` or `templates_only`), session, spend and task templates, up to 8 enabled Managers including the default one. They are defined in Settings → Managers or over `/api/managers` (`GET`, `POST`, `PUT` and `DELETE` on `/api/managers/:id`, `PUT` and `DELETE` on `/api/managers/:id/templates/:key`). The dashboard chat picks the Manager (the picker shows once a second Manager exists), a task records the Manager that created it (and the template it came from) and reports its progress and end to that Manager's thread, and runs and spend are counted per Manager. A Manager creates a task from a template with `looper_task_create` (`template`, `inputs`) after restating it and getting the user's confirmation. An install with only the default Manager keeps its prompt, tools, per-turn context, session and channels as before. Telegram and Lark still reach only the default Manager. Behavior changes and model-visible changes are listed in [docs/releases/v2.13.35.md](docs/releases/v2.13.35.md). ([#388](https://github.com/foolzzz/looper/pull/388))

## [2.13.34] - 2026-10-22

### Added

- **A task can be renamed.** The task page's heading has a pencil that edits the title in place (Enter or Save stores it, Esc cancels), `PUT /api/tasks/:id/title` sets it, and the Manager's `looper_task_update` takes a `title`. A task can be renamed in any status, an ended one included; lists and the task page use the new name (a running task's end notices may keep the old one until the review's follow-up lands), and the change is noted in the task's history (`via` `task_page` or `manager:<channel>`). ([#389](https://github.com/foolzzz/looper/pull/389))

## [2.13.33] - 2026-10-22

### Fixed

- **Turn process tree: the change of #385 is reverted because it slowed killing a deaf Claude.** #385 added a start-time read to every process-tree refresh, which delayed the first signal of a kill on a loaded machine; `test_a_deaf_claude_is_killed_with_its_whole_tree` took 3.1 to 4.9 s against its 3 s bound. `looper/agents/proc_tree.py` and `tests/test_proc_tree.py` return to v2.13.30. Behavior change: the first; a pid reused after it left the turn tree is walked again when the tree is ended, and #338 redoes that fix without the extra read. Model-visible: none. ([#393](https://github.com/foolzzz/looper/pull/393))

## [2.13.32] - 2026-10-22

### Fixed

- **Flaky tests and the isolated Pi smoke script: widened timeouts.** Subprocess waits, runner and RPC deadlines and browser action timeouts raced start-up under load in the planner worker, DSH, LLM models dashboard, model launch, model manager, operator token, Pi, room consult and run channel tests, and in `examples/pi/isolated-smoke.py`. Only the limits changed. Behavior change: none to product code. Model-visible: none. ([#390](https://github.com/foolzzz/looper/pull/390))

## [2.13.31] - 2026-10-21

### Fixed

- **Turn process tree: the subtree of a reused pid is left alone.** A process id seen earlier in a turn that an unrelated process had since reused was spared itself, but its current children and group were still walked, merged into the turn tree and ended. `_tree` now skips reused pids together with what only they reach, and `refresh`, `_end`, `end` and `end_new` pass them on. Behavior change: the first. Model-visible: none. ([#385](https://github.com/foolzzz/looper/pull/385))

## [2.13.30] - 2026-10-21

### Fixed

- **Backup verifier: short boolean options with a value; docs on `LOOPER_BACKUP_VERIFY_CMD`.** `docker_exec_container` now accepts `-t=false`, `-i=false` and `-it=true` in a `docker exec ... pg_dump` backup command, so the verifier is derived where it was not. The README, README.zh-CN and the agent-system doc now say that `LOOPER_BACKUP_VERIFY_CMD` and the notes root take only the `LOOPER_` prefix. Also widens the deadlines of five timing-sensitive tests (`tests/test_dsh_installed.py`, `tests/test_dsh_runtime.py`, `tests/test_gate_channels_db.py`). Behavior change: the first. Model-visible: none. ([#381](https://github.com/foolzzz/looper/pull/381))

## [2.13.29] - 2026-10-21

### Fixed

- **Gate chat commands: `end` to a closed gate, stopped tasks' gates, paged `/gates`.** An `end` reply under a gate's card that is closed, or that meets a 409, now gets a code-written "no longer waiting" receipt and never reaches the Manager's model or chat history. `open_gates` now includes the gates of stopped and failed tasks (every task not completed or cancelled), so `/gates` lists them and `/gate N` can answer them. `/gates` is paged (20 per page, `/gates N` for page N; numbers continue across pages and stay bound to the last delivered list's snapshot). Also fixes flaky deadlines in `tests/test_pi_steer.py` and `tests/test_provider_pricing.py`. Behavior changes: the three above. Model-visible: the Manager no longer sees `end` replies to closed gates; no prompt text, tool schema or tool result changed. ([#382](https://github.com/foolzzz/looper/pull/382))

## [2.13.28] - 2026-10-21

### Fixed

- **A profile's unpriced-model list follows the launch catalog order.** The cost view of an Agent Profile (`cost_basis` unpriced list and cost warnings) now orders the provider's models by `(catalog_order, name, id)` instead of forcing the selected model first, so an earlier entry owns a shared normalized name as it does at launch. Behavior change: for a catalog with such name collisions the unpriced list and warnings can differ from before. Also fixes two flaky `tests/test_pi_steer.py` cases (the setup deadline no longer shortens in prompt-deadline scenarios). No prompt text, tool schema or tool result changed. ([#375](https://github.com/foolzzz/looper/pull/375))

## [2.13.27] - 2026-10-21

### Fixed

- **A long streamed Manager reply no longer stalls the server.** `MessageBus.publish` no longer journals `STATUS_UPDATE` messages (streamed deltas, phases) and does not mark them delivered. Each journaled message cost a synchronous `fsync` on the event loop (about 20 ms measured locally), and a reply streams hundreds of deltas, so the dashboard got no deltas and other requests, a stop included, waited for seconds. Behavior change: after a restart, status updates from before it are no longer replayed (a stale "thinking" line in Telegram or Lark); replies and every other message type are journaled and replayed as before. No prompt text, tool schema or tool result changed. ([#371](https://github.com/foolzzz/looper/pull/371))

## [2.13.26] - 2026-10-21

### Fixed

- **Flaky planner-wait retry test.** `test_a_failed_wait_turn_retries_twice_then_again_on_a_later_event` set its first retry wait to 0.4s, so under load the automatic retry could fall due during the test's 0.05s pause. The test now uses a 1.5s first wait (second 0.3s), a 1.0s gate idle check, and longer settle pauses. Test only; no runtime, prompt, tool schema or tool result changed. ([#368](https://github.com/foolzzz/looper/pull/368))

## [2.13.25] - 2026-10-21

### Fixed

- **Pipeline node-card follow-ups.** The task detail page now also re-reads the task's runs when the cost's run counts change, so a new Planner run that changes no status, instance id or step no longer leaves the hover card on the old model. Each Agent fact on a node card has its own key per role (`agent`, `worker-agent`, `reviewer-agent`, `planner-agent`), so two `Agent` rows no longer collide on the renderer's key. Dashboard only; no prompt, tool schema or tool result changed. ([#365](https://github.com/foolzzz/looper/pull/365))

## [2.13.24] - 2026-10-21

### Fixed

- **Prompt-regression `start_no_wait` check.** It now reads the newest Manager run's tool calls and fails a run that calls a native shell tool such as `Bash` (a `sleep`) after starting a task; before, only the MCP action trail was read. Eval harness only; no runtime or model-visible change. ([#361](https://github.com/foolzzz/looper/pull/361))

## [2.13.23] - 2026-10-21

### Fixed

- **Manager session profile follow-ups.** A Manager turn stops with the settings read error instead of running on cached or built-in settings; `/profile` followed by a tab or newline is recognized; the Chat header refreshes its Profile after `/profile` and on window focus; the session view gains `follow_settings`, so Follow Settings shows the handoff confirmation when it starts a new session; and the Settings tool's "applies from its next session" note is returned only when `profile_id` is set. Model-visible: that tool result no longer carries the note for timeout, budget or harness changes. ([#355](https://github.com/foolzzz/looper/pull/355))

## [2.13.22] - 2026-10-21

### Fixed

- **Gates page follow-ups.** The Gates list refresh re-reads every loaded row in requests of at most 200, so rows past the first 200 are no longer truncated; the All view offers no Answer on a gate of a completed task; the seven-column layout moves from the `lg` to the `xl` breakpoint so it no longer overflows at 1024px, and long task names are clamped to two lines with the full name in a title. `GET` gates list requests with a non-ASCII digit such as `²` in `limit` or `offset` now return 400 instead of 500. No prompt, tool schema or tool result changed. ([#358](https://github.com/foolzzz/looper/pull/358))

## [2.13.21] - 2026-10-21

### Fixed

- **Task run rows and dashboard polling.** `GET /api/tasks/<id>/runs` rows gain `task_role` (the role the run's Planner defined, else null), read from the run's `prompt_layers`; the dashboard restores it, so slim runs no longer lose it. The Context usage panel, the task Room and the Gate thread pass the polling abort signal to their requests, so a stopped poll cancels its request, and the Gate thread has one reader (polling when following, else one read per change) instead of a first read racing the poll. No prompt, tool schema or tool result changed. ([#345](https://github.com/foolzzz/looper/pull/345))

## [2.13.20] - 2026-10-21

### Fixed

- **Chat and Room addresses.** The Chat page keeps the open Web conversation in `?conversation=<id>`, so a link, refresh, Back and Forward reopen it. `GET /api/chat/sessions` takes an optional `session_id` and returns only that conversation, however old, so deep links beyond the newest 50 open, an open read-only channel conversation stays read-only, and loading and error states are shown. The task Room tab keeps its kind and role filters in `?tab=room&filter=..&role=..`. No prompt, tool schema or tool result changed. ([#350](https://github.com/foolzzz/looper/pull/350))

## [2.13.19] - 2026-10-21

### Fixed

- **Prompt-regression spending checks.** The eval harness (`evals/prompt_regression/dialogs.py`) now costs a dialog as the recorded sum plus, run by run, the amount by which a run's observed root spend exceeds its recorded cost; before, it took the larger of the two totals and missed runs where some roles reported one and some the other. The running spending check also adds the finished runs' observed root spend. Tests: the Pi consult test covers a failed run that holds a valid final (not delivered, falls back with the contract reason), and a flaky schema test no longer counts constraints of parallel test schemas. No shipped runtime code, prompt, tool schema or tool result changed. ([#349](https://github.com/foolzzz/looper/pull/349))

## [2.13.18] - 2026-10-21

### Added

- **Command hints on every channel.** Gate cards that ask for a decision (opened and reminder) now end with a line naming `/gates`, `/gate N answer` and `/gate N end`. `/help` is answered by the Manager on every channel without calling the model, from one shared command list (`looper/agents/chat_commands.py`) that Telegram's command menu also reads. Telegram's `/help` is now headed `Looper 命令：` and omits `/stop` while stopping a reply is off. No prompt, tool schema or tool result changed. ([#344](https://github.com/foolzzz/looper/pull/344))

## [2.13.17] - 2026-10-21

### Fixed

- **A per-turn runtime turn no longer waits on a grandchild that holds a pipe open.** After the turn's output ended, the turn waited for the child with `Process.wait()`, which also waits for every pipe to close; a grandchild keeping stderr open delayed the turn until it exited. The turn now waits only for the child's own exit. This is the one change in the `looper` package and it applies whether or not `chat.stop_enabled` is on. ([#346](https://github.com/foolzzz/looper/pull/346))

### Changed

- Tests only: fixes for tests that failed only on the CI runner (the shared database counting a constraint once per schema, the model launch parity check on temp-root ancestors such as `/private/tmp`, and timing races in the Pi steer, per-turn interrupt and Grok fallback stop-matrix tests). No model-visible change. ([#346](https://github.com/foolzzz/looper/pull/346))

### Validation

- Reported in #346: the card's tests, the full local suite before the merge, and Codex and Grok reviews that left no P0. CI runs after the release.

## [2.13.16] - 2026-10-21

### Changed

- **Stopping a reply, v2 (still behind `chat.stop_enabled`, off by default).** A stop now covers the whole turn from the moment a message is received, not only once the model runs ([RFC](docs/architecture/rfcs/chat-stop-v2.zh-CN.md)). ([#337](https://github.com/foolzzz/looper/pull/337))
  - A message waiting in the queue can be stopped (it used to answer `other_message`); `/stop` stops the chat's running and queued messages.
  - Tool calls that arrive after a stop are always refused; on resident runtimes a write call that does not match a tool call in its own turn's stream is refused with `not_this_turn`. Each call is bound to its turn through a binding file the MCP bridge (and Pi's own bridge) reads (`X-Looper-Turn`). While `chat.stop_enabled` is off the binding only observes: no call is refused for it, and each mismatch is logged as `turn binding mismatch` and counted in `GET /api/status` (`turn_binding`); zero mismatches over real use is a criterion for turning Stop on.
  - A message stopped before the model saw it leaves no Runs record, and the next turn is told it was withdrawn. A stopped turn is recorded `interrupted` with the phase and method of the stop; a stop whose usage is wholly unknown records `null` and shows "usage not reported" instead of 0.

- Model-visible: the turn after a stopped reply gets a `[Looper]` line that says either that the user stopped the previous reply (tool calls already made were not undone) or that they withdrew a message before the model saw it. A refused late or unmatched tool call returns the errors `stopped`, `expired` or `not_this_turn` to the model, and tool calls carry the turn's binding as the `X-Looper-Turn` header. No tool schema changed. ([#337](https://github.com/foolzzz/looper/pull/337))

### Validation

- Reported in #337: each card's tests including the runtime x window and Manager phase x runtime matrices, the full local suite before the merge, and Codex and Grok reviews that left no P0. The real-path stop check runs after the merge. CI runs after the release.

## [2.13.15] - 2026-10-21

### Added

- **The Manager's chat box keeps working while it replies: a queue, a history and, behind an experimental setting that is off by default, a Stop.** Enter during a reply puts the message in a staging area above the box; after a reply that ends as `answered` the staged messages go as one message joined by a blank line, otherwise they return to the box, and they survive a reload. ↑ pulls them back and ↑/↓ walk this thread's last 100 messages. Stopping a reply is behind the system setting `chat.stop_enabled` (Settings → Chat, experimental, **off by default** until the stop path is redesigned); while it is off, `POST /api/chat/stop` answers `409` `{"state": "disabled"}`, the box has no Stop button and Esc never stops, `/stop` is answered "停止功能暂未开放" without reaching the model, and Telegram's menu leaves `/stop` out. With it on, Esc or the Stop button stops the reply (`POST /api/chat/stop`; `GET /api/chat/turn` tells a reloaded page whether one is running). `/stop` does the same from the web chat, Telegram (a new command) and Lark, and is answered outside the Manager's queue. A stopped turn is recorded `interrupted` with the text and usage so far, keeps the session, and the next turn is told the user stopped it; a confirmation it had asked for is dropped. Claude Code is interrupted with a control request (killed after 5 s), Grok with `session/cancel`, Codex, OMP, Pi and DSH by ending the turn's process tree (a stopped Codex turn records no usage). `answer.final` now carries the reply's `outcome`. ([#292](https://github.com/foolzzz/looper/pull/292))

### Changed

- Model-visible: the turn after a stopped reply is told once, in a `[Looper]` line, that the user stopped the previous reply and that its tool calls were not undone; `answer.final` events carry `outcome`. No prompt, tool schema or tool contract changed otherwise. ([#292](https://github.com/foolzzz/looper/pull/292))

### Validation

- Reported in #292: each card's tests, a real stop on Claude Code and on OMP with DeepSeek with readback, browser screenshots, the full local suite before the merge, and Codex and Grok reviews with no P0. CI runs after the release.

## [2.13.14] - 2026-10-21

### Changed

- **The task page stays responsive on long and running tasks.** The Sessions and Conversation tabs read the latest 1000 messages and load older ones when the scroll nears the top, keeping the reading place; the view follows new events only while it is at the bottom, and the live stream starts after the latest loaded event. The page and the Room poll without overlap, wait while the browser tab is hidden and abort a request in flight when they stop. The task's runs are read in one compact request instead of three. ([#324](https://github.com/foolzzz/looper/pull/324))
- `GET /api/tasks/<id>/activity` and `GET /api/tasks/<id>/conversation` return the latest 1000 rows by default (`limit` 1 to 1000), oldest first, with `has_more`, `oldest_seq` and `latest_seq`; `before=<seq>` returns the rows just before that seq. `since` or `after` keeps the forward read (at most 500 rows). A non-integer `since`, `after`, `before` or `limit` answers 400. ([#324](https://github.com/foolzzz/looper/pull/324))
- New `GET /api/tasks/<id>/runs` returns a task's Planner, Worker and Reviewer runs as compact rows (no prompts, metadata body or events). ([#324](https://github.com/foolzzz/looper/pull/324))
- The Agents tab no longer says that only the latest 500 runs are listed, as all runs are read.
- No model-visible change: no prompt, tool schema or tool contract changed.

### Fixed

- The Sessions tab showed the oldest 500 events of a task and replayed its whole history on every refresh. ([#324](https://github.com/foolzzz/looper/pull/324))

### Validation

- Reported in #324: database tests for the windows, cursors and run rows, Vitest tests for the upward load, anchoring, stream start, runs request and polling, the full local suite before the merge, and Codex and Grok reviews with no P0. CI runs after the release.

## [2.13.13] - 2026-10-21

### Changed

- **A refresh keeps every dashboard page where it was.** The task page tab (`#tasks/<id>?tab=<tab>`), the task list's status filter (`?status=<value>`) and the Chat page's open agent and read-only conversation (`#chat?target=<id>&conversation=<id>`) are kept in the address, so a refresh reopens the same place and Back and Forward move between them. Default values are not written, unknown values fall back to the default, and existing deep links keep working. ([#323](https://github.com/foolzzz/looper/pull/323))
- No model-visible change: no prompt, tool schema or tool contract changed.

### Validation

- Reported in #323: Vitest tests per page, the TypeScript check, the full local suite before the merge, and Codex and Grok reviews with no P0. CI runs after the release.

## [2.13.12] - 2026-10-21

### Added

- **A User Gates page in the dashboard** (`#gates`, sidebar "User Gates"). It lists the gates of all tasks, open ones by default, with Closed and All, a "Waiting for you only" switch and a project filter; a gate waiting for you is answered in place. The sidebar entry shows how many gates wait for you. New read-only route `GET /api/gates` (`state`, `waiting_on`, `project_id`, `limit`, `offset`). ([#275](https://github.com/foolzzz/looper/pull/275))
- No model-visible change: no prompt, tool schema or tool contract changed.

### Validation

- Reported in #275: each card's tests, headless-browser screenshots, the full local suite before the merge, and Codex and Grok reviews with no P0. CI runs after the release.

See the [bilingual release notes](docs/releases/v2.13.12.md).

## [2.13.11] - 2026-10-21

### Changed

- **The Manager no longer waits inside a turn for a task it started.** Its tool contract gains a rule: after starting or changing a task, do not sleep or poll; end the reply, since Looper brings the task's gates and its end to the user. Guidance only; no code path changed. A new `start_no_wait` prompt-regression dialog checks it. ([#279](https://github.com/foolzzz/looper/pull/279))
- Model-visible change: the Manager's tool contract gains that rule. No tool schema changed.

### Validation

- Reported in #279: real Manager dialogs 10/10 on sonnet and on OMP + DeepSeek, 8/8 on main for the shared dialogs; 71 passed in the Manager reply and dialog tests; the full local suite before the merge; Codex and Grok reviews left no P0. CI runs after the release.

See the [bilingual release notes](docs/releases/v2.13.11.md).

## [2.13.10] - 2026-10-21

### Fixed

- **A failed DSH consult run is no longer delivered as an answer.** A result with `is_error`, or a `subtype` other than `success`, falls back with a contract reason. ([#300](https://github.com/foolzzz/looper/pull/300))
- **Resident DSH is priced by the configured model**, not the normalized API model, so usage and budget stops use the right price; resident turn results gain `observed_cost_usd`, and the prompt-regression eval records `observed_cost_complete` instead of counting a missing value as 0. ([#300](https://github.com/foolzzz/looper/pull/300))
- **An unsupported effort is refused** when a profile is selected or launched (for example `dsh` with `medium` or `xhigh`), not only on config import. ([#300](https://github.com/foolzzz/looper/pull/300))
- **The `[1m]` suffix warning follows each runtime's real model mapping** (new adapter `api_model`): DSH and Claude Code no longer warn about a suffix they strip. ([#300](https://github.com/foolzzz/looper/pull/300))
- No model-visible change: no prompt or tool schema changed.

### Validation

- Reported in #300: 495 passed, 2 skipped across the DSH runtime, DSH validation, provider pricing and room consult tests; 148 passed in the agent settings tests; the new effort test fails without the fix. No full-suite run or review is reported. CI runs after the release.

See the [bilingual release notes](docs/releases/v2.13.10.md).

## [2.13.9] - 2026-10-21

### Added

- **Answer and end user gates from a chat with `/gates` and `/gate`.** In a Lark or Telegram chat, `/gates` lists the user gates that wait for the user (a private chat: all of them; a group: that group's) as a numbered list, and `/gate N <answer>` and `/gate N end` add the answer to, or end, gate N. Looper answers these itself before the Manager's model; the numbers refer to the last list delivered to that chat (one hour, per sender), so a number never lands on another gate. A reply under a gate's card with exactly `end` (or `/end`) ends the gate. Telegram registers `/gates` and `/gate` in its menu and help. ([#277](https://github.com/foolzzz/looper/pull/277))

### Changed

- Code-written receipts now follow an answer that reaches a gate (including a reply under a gate's card, which was silent before): "delivered to <task> <step>", "asked the Planner to end it on what is settled", or, for a stopped task, that the answer is saved for when it continues. ([#277](https://github.com/foolzzz/looper/pull/277))
- No model-visible change: the commands and their replies are not stored in the Manager's chat history or shown to its model.

### Validation

- Reported in #277: unit tests for the commands, scopes, numbering, end, receipts and the Telegram handlers; the full local suite before the merge; Codex and Grok reviews with no P0. Not tested on a real Lark or Telegram chat. CI runs after the release.

See the [bilingual release notes](docs/releases/v2.13.9.md).

## [2.13.8] - 2026-10-21

### Added

- **Choose and switch the Manager session's Agent Profile.** A new nullable `agent_sessions.selected_profile_id` (foreign key to `agent_profiles`, `ON DELETE RESTRICT`, `manager` row only) holds the profile chosen for the Manager's current session; it is the Manager's last settings layer (`session`), over the role and shared layers. `/profile` (dashboard chat, Telegram, Lark) lists the profiles that can run the Manager, selects one by number, id or name, and `/profile 0` follows the Settings again; Looper answers it itself, takes effect on the next message, resumes the session on the same runtime and provider and otherwise starts a new one with a handoff, and records each switch as a run. `/new` clears the choice. New `GET /api/manager/session` and `PUT /api/manager/session/profile`, `new_session_profile_id` on `POST /api/chat/send` (only with `/new`), a chat-header selector and a profile choice in the new-session panel. A profile a Manager session selects cannot be deleted or edited so it cannot run the Manager. A Manager profile error from the session or role layer now suggests `/profile`. ([#287](https://github.com/foolzzz/looper/pull/287))

### Changed

- **Model-visible:** the Manager's tools contract says the user switches profiles with `/profile` and the model cannot; the `looper_settings` description gains one sentence, and its `view` (role `manager`) and `set` (manager role) answers carry `manager_session.selected` (and a `note` after `set` when a choice exists). ([#287](https://github.com/foolzzz/looper/pull/287))

### Validation

- Reported in #287: each card's tests, the full local suite on a fresh PostgreSQL before the merge, and Codex and Grok reviews with no P0; the real switch is stated to follow the merge. CI runs after the release.

See the [bilingual release notes](docs/releases/v2.13.8.md).

## [2.13.7] - 2026-10-20

### Fixed

- **Two load-dependent tests no longer fail on a busy machine.** `test_eval_deadline_kills_term_ignoring_child` now measures the kill latency from the deadline (the fake runner's recorded start plus the deadline) against `proc_tree.TERM_GRACE_SEC` plus 3 s, instead of 8 s from the launch of the script, so start-up time of bash, the interpreter and `uv` no longer counts; its subprocess timeout is 60 s and only guards a hang. `test_pi_responses_wire_preserves_optional_tool_parameters` now aborts after 60 s in node and times out after 120 s, so loading the installed pi on a loaded machine no longer expires first. All process-death and request assertions are unchanged. Tests only: no product code, API, prompt, tool or model-facing text changed. ([#288](https://github.com/foolzzz/looper/pull/288), follow-up of [#261](https://github.com/foolzzz/looper/issues/261))

### Validation

- Reported in #288: `pytest tests/test_pi_packaging.py -k term_ignoring` (12 passed) and `pytest tests/test_pi_runtime.py -k optional_tool_parameters` (4 passed). The PR description reports no full local suite or review. CI runs after the release.

See the [bilingual release notes](docs/releases/v2.13.7.md).

## [2.13.6] - 2026-10-20

### Fixed

- **Profile cost warnings price the background model.** `profile_view` and the warnings after a profile write now price a profile from every model of the selected model's account and protocol, the same catalog a launch uses, so a background model with a full price of its own is no longer listed in `cost.unpriced` or warned about. A background model without a price still warns. The registry view, the profile answer and a task's profile overrides load that catalog once. ([#273](https://github.com/foolzzz/looper/pull/273), follow-up of [#264](https://github.com/foolzzz/looper/issues/264))
- **Manager provider answers carry `max_concurrency`.** The `looper_providers` list, register and update answers now include the provider's `max_concurrency` field. No prompt, tool schema or tool description changed. ([#273](https://github.com/foolzzz/looper/pull/273))

### Validation

- Reported in #273: 131 tests in the two Manager tool files passed against a disposable PostgreSQL database (the new `max_concurrency` and background-price tests fail without the source change), and 444 passed across the pricing, provider binding, models, Manager tool and task agent tests. CI runs after the release.

## [2.13.5] - 2026-10-20

### Changed

- **OMP never backgrounds a command on its own.** The OMP settings overlay now turns off `bash.autoBackground` and `eval.autoBackground` beside `async`, for every role, so a long command finishes inside its turn instead of being backgrounded and the turn ending without its result. A very long command now holds its turn. The overlay change gives each OMP launch configuration a new content-derived home directory. ([#271](https://github.com/foolzzz/looper/pull/271))
- **The Manager's "no reply" error names the runtime that ran.** It took "Claude Code" for every runtime; it now uses the launch's runtime label. Only the Manager text changed. No prompt, tool or model-facing text changed. ([#271](https://github.com/foolzzz/looper/pull/271))

### Validation

- Reported in #271: overlay and failure-text tests, the full local suite before the merge, and Codex and Grok reviews with no P0. CI runs after the release.

## [2.13.4] - 2026-10-20

### Changed

- **Pipeline hover cards show each run's agent runtime and model.** The Worker, Reviewer and Planner lines gain an Agent fact (runtime, model, effort) from the latest run of that role, and of that step for Worker and Reviewer; it appears only when such a run exists. The task page now reads the task's Planner, Worker and Reviewer runs once through a shared hook, also used by the Agents tab, so it makes those `GET /api/llm-records` requests even when the Agents tab is closed. Dashboard-only; no API, prompt, tool or model-facing text changed. ([#274](https://github.com/foolzzz/looper/pull/274))

### Validation

- Reported in #274: vitest for the cards and node card component, tsc, headless-browser screenshots, the full local suite before the merge, and Codex and Grok reviews with no P0. CI runs after the release.

See the [bilingual release notes](docs/releases/v2.13.4.md).

## [2.13.3] - 2026-10-20

### Changed

- **The dashboard sidebar shows the running Looper version.** `GET /api/overview` gains a `version` field (the installed `looper.__version__`), and the sidebar shows it as a small muted label next to "Looper". A client talking to an older server shows no label. The overview response gains a field; no prompt, tool or model-facing text changed. ([#272](https://github.com/foolzzz/looper/pull/272))

### Validation

- Reported in #272: overview API test, sidebar vitest and tsc, the full local suite before the merge, and Codex and Grok reviews with no P0. CI runs after the release.

See the [bilingual release notes](docs/releases/v2.13.3.md).

## [2.13.2] - 2026-10-20

### Fixed

- **CI runs the legacy parity tests on a shallow-clone runner.** The `Agent system tests` job checks out the full history, and the Claude Code launch parity test no longer depends on the host's temporary-root ancestors (`/private/tmp` on macOS, `/tmp` on Linux). CI workflow and test-only change; no product or model-visible change. ([#270](https://github.com/foolzzz/looper/pull/270), follow-up of [#267](https://github.com/foolzzz/looper/issues/267))

### Validation

- Retained #270 report: targeted tests on a disposable PostgreSQL passed; no local full-suite run or review record is reported. CI runs after the release.

See the [bilingual release notes](docs/releases/v2.13.2.md).

## [2.13.1] - 2026-10-20

### Fixed

- **The pre-upgrade backup is verified when `pg_dump` runs in Docker.** A `docker exec <container> pg_dump ...` backup command no longer needs a local `pg_restore`: the dump is listed with `docker exec -i <container> pg_restore --list`, or with the new `backup.verify_command` in the ops file (deprecated env `LOOPER_BACKUP_VERIFY_CMD`) for other setups. `looper doctor` and the startup check report the verifier; without one the backup still fails before any DDL, naming `backup.verify_command`. This resolves the v2.13.0 Docker backup known issue. ([#266](https://github.com/foolzzz/looper/pull/266))

### Validation

- Retained #266 report: unit tests cover derivation, explicit and failing verifiers, and the unchanged failure path; final Codex and Grok reviews left no P0. The PR body reports CI green, but its final-head workflow shows Dashboard tests passed and Agent system tests failed; CI is not recorded as a verified pass for this draft.

See the [bilingual release notes](docs/releases/v2.13.1.md) for activation, verification, rollback, authority boundaries and validation limits.

## [2.13.0] - 2026-10-20

### Highlights

- **Typed LLM registry: provider → endpoint → model → profile.** Accounts have typed vendors and per-protocol endpoints; independent saved models carry an exact name, one protocol, context/reasoning/background settings and prices. Agent Profiles reference a runtime and model. All six existing runtimes consume resolved bindings; no new runtime is introduced. Manager `looper_models` adds model CRUD. ([#258](https://github.com/foolzzz/looper/pull/258))

### Behavior changes

- **Automatic v2 registry upgrade requires a verified full backup.** Startup creates a custom-format `pg_dump` and verifies table definitions and data with `pg_restore --list` before any DDL. Missing/unverifiable backups stop startup with the database unchanged; registry upgrades reject `--skip-backup`. Conversion, schema changes and version 3 commit in one locked transaction; unresolved references or launch/pricing mismatches roll everything back. Profile IDs, selections, runtime options, protocol choices and omitted-endpoint semantics are preserved, models used by legacy open catalogs are materialized, and equivalent accounts merge only after compatibility checks with Session/scheduled Planner references remapped.
- **Breaking HTTP and config contracts.** Provider `protocols/base_url/embedded models` become `vendor_type/endpoints` and independent `/api/models`; Profile writes require `model_id`, reads expose a structured model, and `/api/registry` includes models/vendor types. Export writes `looper-config/v3` with composite model references; v2/v1 imports remain supported, including user models under built-in providers. Invalid/ambiguous entries get skip diagnostics and apply recomputes its plan. Legacy profile `system_prompt_mode` is ignored with a note; v3 rejects it.
- **Saved-model selection and write guards are explicit.** Empty catalogs no longer allow arbitrary model names. Model provider/name/protocol and provider vendor identities are immutable; referenced resources cannot be deleted and incompatible updates are refused. Vendor suggestions apply only at creation; endpoint validation rejects malformed URLs, embedded credentials and fragments. OMP/pi bind the selected model's exact protocol; pi efforts include `off/minimal`, dsh exposes `low/high/max`.
- **Dashboard and Manager share the model layer.** Settings manage accounts/endpoints/models and compatible profiles with runtime-specific efforts; profile identities, price sources, compatibility, import diagnostics and shared refreshes reach selectors and summaries. Inline profile creation does not select it automatically. `looper_providers` retains account/endpoint registration and update, `looper_agent_profiles` accepts model IDs or full references, and `looper_models` deletes unused models only with confirmation. Credentials remain dashboard-only; environment-entry arrays are masked in Manager audits.
- **Pricing and resident sessions use resolved settings.** Exact case-sensitive prices precede normalized matches and built-in fallback within the selected provider/protocol; Claude subscription alias semantics remain. Null falls back, zero means free and unknown stays unknown. Run prices are frozen and shared by usage/spend checks with identity/provenance recorded; historical costs stay unchanged. Model, price and runtime-option changes refresh resident processes on subsequent turns.

### Model-visible changes

- Manager instructions describe accounts, exact model references and profile recipes, direct credential entry to the dashboard and include models in the existing user-requested write policy. Tool schemas/results expose models and compatibility. Custom prompts and role/task append-or-replace semantics remain in their existing layers; Prompt Preview reports mode provenance without loading another selected profile's credentials. Dashboard chat, Telegram and Lark share these tools without channel-specific commands; CLI startup migrates and existing config commands use v3. Evals register provider → model → profile, retain legacy-spec conversion and add model dialogs. ([#258](https://github.com/foolzzz/looper/pull/258))

### Upgrade and known issue

- Quiesce writes/scheduling, stop v2.12.5, retain an independent full database backup plus configuration/runtime data, and rehearse restore before upgrading. Startup then makes and verifies its own full backup. Rollback means stopping v2.13.0, retaining its database, restoring the pre-upgrade backup into a separate database and installing the v2.12.5 release wheel against that restored database. Validate profiles, historical costs and old-version launches before resuming. Never point an unpatched older binary at schema v3; there is no down migration or v3-to-v2 export, and later writes require a recovery decision.
- **Docker backup limitation:** If `backup.command` or `LOOPER_BACKUP_CMD` runs `pg_dump` through `docker exec` and no local `pg_restore` exists, the upgrade refuses to start without changing the database. Install a local `pg_restore` of any version not older than the PostgreSQL server, available to the service user, or wait for the patch release.

### Validation

- Retained #258 reports: unit/database tests and CI passed; read-only reviews left no P0 after six fixes covering built-in-provider user-model export, valid v2 OAuth multi-protocol declarations, exact-price case handling, provider updates, environment-array audit masking and Claude alias lookup order.
- Fresh-database migration rehearsal used a real configuration export with 4 providers, 9 profiles and 107 catalog items, detecting pi/dsh: 10/10 profiles retained, 0 launch differences, 104 unchanged price values and a backup verified with `pg_restore --list`. The migrated server ran a real Sonnet Planner/Claude Code Worker task and a Manager `looper_models` turn.
- Existing Manager dialogs: 52/52 on both branch and main with Sonnet and Opus xhigh; final-fix Sonnet model-dialog follow-up: 8/8. Initial new-model dialogs remain partial at 18/20 (one model-path miss, one per-turn budget stop). Real post-migration Codex/OMP/Grok/pi/dsh launches and non-empty custom prompts in a real configuration were not tested; unit tests cover custom prompts. Runtime/database checks and a complete final-head prompt suite were not rerun for this draft.

See the [bilingual release notes](docs/releases/v2.13.0.md) for activation, backup, rollback, authority boundaries and validation limits.

## [2.12.5] - 2026-10-20

### Highlights

- **Pi and DeepSeek Harness are optional runtimes for all four roles.** Manager, Planner, Worker and Reviewer can use pi (the pi coding agent CLI, minimum 1.3.0 with required launch flags) or exactly `@deepseek-ai/dsh` 0.2.1-rc.2. Both integrate with Agent Profiles, platform tools, events, usage, sessions and dashboard selection. Existing defaults and profile selections are unchanged. ([#252](https://github.com/foolzzz/looper/pull/252), [#256](https://github.com/foolzzz/looper/pull/256))

### Behavior changes

- **Pi supports API-key Responses and Chat Completions providers.** `openai-responses` includes CPA-style gateways; `openai-chat` includes DeepSeek, with Responses preferred when both are declared. Private homes, agent directories and separate sessions avoid operator configuration; discovery, telemetry and cache warming are disabled. Looper's bundled extension loads with `-e` and `--no-extensions`, preserving scoped tool descriptions, schemas, authorization and confirmation through the existing HTTP API. Catalog MCP/skills/plugins are skipped; Planner/consult reading-tool allowlists are not an OS sandbox. ([#252](https://github.com/foolzzz/looper/pull/252))
- **Pi accounts new usage and resumes exact sessions.** JSON messages, tools, compaction and terminal events are normalized, with finalization on `agent_settled`, provider pricing and usage-based soft caps. Manager/Planner resume exact session files, room consults fork read-only copies, Worker RPC steering requires a correlated queued acknowledgement, and private storage participates in reclamation. ([#252](https://github.com/foolzzz/looper/pull/252))
- **DSH binds DeepSeek API-key Messages providers to private runs.** `anthropic-messages` supports `deepseek-flash` and `deepseek-v4-pro`, including their `[1m]` catalog aliases; effort is low/high/max, default high. The key is supplied to the dsh process environment. Private `DSH_HOME` and immutable overlays pin the model, disable telemetry, uploads, title requests and default web search, append Looper's prompt and wire stdio MCP to the run's Looper endpoint. Compatible skills/external MCP are translated and omissions recorded. Planner/consult runs allow only reading tools and exact platform registrations; this is not a host sandbox. ([#256](https://github.com/foolzzz/looper/pull/256))
- **DSH has headless events and resumable owner sessions.** Success requires a completed turn, final response and verified successful exit. Resume refreshes launch configuration; read-only consults run in place with separate ended-session snapshots, refusing active-session snapshots. Active-run steering, autonomous turns, structured output and native `max_turns` enforcement are unsupported. Dashboard provider/model selections respect runtime compatibility and show the append-only limitation. ([#256](https://github.com/foolzzz/looper/pull/256))
- **Incomplete usage stays unknown.** DSH prices reported root steps at provider prices and stops capped runs when root usage cannot be verified; observed root spend is not a complete bill or a hard billing cap. Shared accounting honors explicit incomplete-usage markers, and the observed-root budget policy applies only when the runtime requests it; other runtimes keep their prior policy without those markers. ([#256](https://github.com/foolzzz/looper/pull/256))
- **Shared records and eval metrics improve.** Manager/Planner record materialized resident commands when available. Runtime cards retain probe errors beside versions for all runtimes. Eval edit accounting accepts `path`, deduplicates within a tool call and excludes URLs. The #256 description's managed-parent symlink cleanup protection, including Codex/OMP storage, is not present in merged code: cleanup skips symlinked session entries but does not check their managed parents. See the release notes' code-verification gap. ([#252](https://github.com/foolzzz/looper/pull/252), [#256](https://github.com/foolzzz/looper/pull/256))

### Model-visible changes

- **Manager profile tools list detected pi/dsh runtimes.** Pi notes require complete final replies/reports and identify `mcp__looper__`; its append/replace prompt modes receive scoped bridge tool definitions. DSH literally appends Looper instructions to its built-in prompt without rewriting saved role settings, requires complete final reports and tells Workers/Reviewers to finish rather than wait for later room questions. Both inject project instructions once with `AGENTS.md` preferred over `CLAUDE.md` per directory; DSH Prompt Preview includes effective content, compatible skills and role tools are exposed through DSH. Shared role prompts/contracts and existing runtimes' prompt content are unchanged. Dashboard chat, Telegram and Lark share these adapters/tools; no new product CLI subcommand or channel command is introduced. ([#252](https://github.com/foolzzz/looper/pull/252), [#256](https://github.com/foolzzz/looper/pull/256))

### Fixes

- **A missing unrelated runtime no longer restarts the pool and cancels active runs.** For every runtime, Manager and WorkerPool health checks inspect their own loop instead of requiring the `claude` binary on PATH; availability is checked at launch. ([#256](https://github.com/foolzzz/looper/pull/256))
- **Failures are explicit across runtimes.** Manager requires a successful terminal result before treating text as a successful reply, so error results with text fail. Worker command-preparation errors become work/verification failures, and soft-budget stop results are retained in transcripts. ([#256](https://github.com/foolzzz/looper/pull/256))

### Validation

- Both PRs' Codex review rounds ended not blocking and CI passed. Pi real evals: CPA-style `openai-responses` tasks 18/18, DeepSeek `openai-chat` tasks 17/18 (one room-consult case passed 1 of 2 runs, model variance), Manager dialogs 8/8; installed tool-bridge checks used pi 1.3.0.
- DSH real validation used a DeepSeek key: an earlier task-stage attempt timed out and remains an error, with unknown observed cost and auxiliary coverage, no hard billing cap, a USD 0.50 per-run soft cap and USD 4 total observed cap. Later task-basic, task-review and Manager-dialog summaries each report 4 pass and 0 fail/error/skipped/untested; no per-stage cost is claimed. A separate reported four-role smoke covered two tasks, two Manager turns, API/database readback and session resume, with about USD 0.02 observed spend, not a complete bill. These are retained implementation reports; runtime checks and a full final-head prompt suite were not rerun for this draft.

See the [bilingual release notes](docs/releases/v2.12.5.md) for activation, verification, authority boundaries, rollback and validation limits.

## [2.12.4] - 2026-10-20

### Highlights

- **A Planner can wait for an external event inside its plan.** A `kind: "wait"` step waits for a time and an optional typed `github_pr` check, or a shell `command` only when the project's `wait_commands` is On (Off by default). Only dependent steps wait; readiness continues them without a model turn, Worker or Worker/Reviewer concurrency slot held for the wait. ([#247](https://github.com/foolzzz/looper/pull/247))

### Behavior changes

- **Waits have system limits and respect scheduled-run timeouts.** Settings → Scheduler → Waits sets the longest wait (built-in seven days) and shortest check interval (built-in 300 seconds); the Manager can view or change these with confirmation. A scheduled wait cannot outlast the run's remaining timeout. GitHub conditions cover head changes, finished checks and new comments; finished checks do not imply success. Command checks run as the server user in the task directory, require explicit project permission and are limited to 60 seconds and 2 KiB of retained output.
- **Timeout, Resume now and Cancel have distinct outcomes.** Timeout records a notice, routes it to the originating Telegram/Lark chat when available, and gives the Planner a `[WAIT]` turn to extend, proceed, fail or adjust the plan. Resume now bypasses the condition without a model turn; Cancel requests a Planner decision. Dashboard actions apply only to waiting rows of active tasks. Saved waits recover after restart with their deadlines intact; stopped tasks are not checked or notified. A task with only waits reports `waiting` in In progress.
- **The Planner reply contract and wait-step guide change what the model sees.** They add the wait shape, limits and DECISION/CHAT cancellation list; `[WAIT]` messages carry their own reply shape, check details and remaining scheduled-run time. Manager task reads include waits and step kinds; settings reads and the confirmed `waits` action expose limits. Dashboard chat, Telegram and Lark share the Manager tools; user wait actions remain on the dashboard. No new CLI command is added.

### Validation

- Codex review rounds for #247 ended not blocking. Opus Planner tier passed 40/40 on the 20 shared tasks; the broader run was 46/47, with the re-review wait miss fixed and a focused wait-task rerun passing 4/4. Opus Manager dialogs passed 54/54. A disposable real run with a sonnet Planner and haiku Workers covered a ready command check, timeout notice and `[WAIT]` turn, Resume now and Cancel, with no Worker running while waiting; headless screenshots covered waiting and timed-out steps. These are retained implementation reports, not runtime checks rerun for this draft.

See the [bilingual release notes](docs/releases/v2.12.4.md) for activation, permissions, verification, rollback and validation limits.

## [2.12.3] - 2026-10-20

### Highlights

- **Reply to user gates from the dashboard.** Waiting for you shows the task, topic, first open question and elapsed wait, with an inline reply and a link to the discussion. Up to ten cards are shown, with overflow links to Needs you tasks; a sent reply stays visible while the Planner owes it an answer. End discussion remains on the task page. ([#255](https://github.com/foolzzz/looper/pull/255))
- **OpenCode and Sub2API provider presets, with models per API.** OpenCode uses its official inference endpoints and separates Responses/GPT, Chat Completions/DeepSeek, GLM and Kimi, and Anthropic/Claude catalogs. Sub2API defaults to a local self-hosted gateway; adjust its address and models to match the gateway. ([#257](https://github.com/foolzzz/looper/pull/257))
- **DeepSeek Responses and Manager provider editing.** The DeepSeek preset offers Responses and Chat Completions at one base URL. The Manager can list presets, register from a preset and chosen API, and update a provider by id or name without accepting its key. Retained model names keep their catalog metadata; unknown vendor models and missing prices produce separate warnings. Thanks to @michaelx1993 for the report. ([#254](https://github.com/foolzzz/looper/pull/254), fixes [#251](https://github.com/foolzzz/looper/issues/251))

### Behavior changes

- **OpenAI-format preset models have no Claude Code context suffix.** GLM, Kimi and DeepSeek keep `[1m]` only on Anthropic-format entries, retaining context windows on suffix-free OpenAI entries. Kimi K3's preset window changes from 1,048,576 to 1,000,000. Existing providers and profiles are not rewritten. Saving a provider without Anthropic Messages or a non-Claude-Code profile warns about suffixed foreground/background names; the provider form displays saved warnings until acknowledged. ([#257](https://github.com/foolzzz/looper/pull/257), closes [#253](https://github.com/foolzzz/looper/issues/253))
- **The Manager's provider tool changes what the model sees.** `looper_providers` describes preset discovery, registration, updates and warnings; its schema adds `update`, `provider`, `preset`, `api` and nullable `base_url`, and its answers include preset catalogs and warnings. Dashboard chat, Telegram and Lark share the change. Keys remain dashboard-only. There is no new CLI command. ([#254](https://github.com/foolzzz/looper/pull/254), [#257](https://github.com/foolzzz/looper/pull/257))

### Fixes

- **OMP failures carry the API error.** Available provider error text and HTTP status reach Manager, Planner and Worker/Reviewer failure reasons, with credential redaction before error-event recording and bounded displayed details. RPC error enrichment preserves session and usage data; streamed assistant text does not make an errored Manager turn successful. Startup failures name the runtime that could not start. ([#257](https://github.com/foolzzz/looper/pull/257))
- **Waiting gate cards keep refreshing through slow reads and deleted tasks.** Refreshes queue instead of invalidating in-flight reads; one missing task does not block other cards, and pending reply receipts survive unrelated Planner turns. ([#255](https://github.com/foolzzz/looper/pull/255))

### Validation

- Codex review rounds for #255, #257 and #254 all ended not blocking after fixes. Opus Manager dialogs for #251 / #254 passed 54/54, two repetitions, on `b7500a892`, before the final review fixes. #255 has headless screenshots from a disposable server. These are retained implementation validations, not runtime checks rerun for this release draft; live provider calls and a full final-head prompt eval were not rerun.

See the [bilingual release notes](docs/releases/v2.12.3.md) for activation, verification, authority boundaries, rollback and validation limits.

## [2.12.2] - 2026-10-19

### Highlights

- **Non-parallel schedules keep one Planner conversation across runs.** Each trigger still creates its own task, with its own run record, budget and timeout. Each run's requirement is also kept in the conversation, so later runs can remember earlier inputs. Reset the stored session from the dashboard, API or the Manager's confirmed schedules action; failures and edits do not reset it. ([#249](https://github.com/foolzzz/looper/pull/249))
- **Session fallback protects other live runs.** Parallel runs, an unresumable session, another live holder or a failed session check produce activity explaining why a run starts fresh. Reset and restore cannot give two live Planners the same session; a run giving up the shared session keeps its own storage label across restart, and a holder's replacement session is saved for later runs. ([#249](https://github.com/foolzzz/looper/pull/249))

### Behavior changes

- **After upgrade, every non-parallel schedule continues its Planner from its second run onward.** There is no activation switch. Reset makes the next run fresh; it does not disable future continuity. Prompt caching mainly helps closely spaced runs; a long-lived conversation can cost more per run over time. There is no automatic threshold handoff. Claude Code compacts its own context; other runtimes' long-session behavior needs verification. Cross-run resume on Codex, Grok and OMP has stand-in coverage only. Changing overlap to parallel during a run can leave a stale cost baseline for the next run.

See the [bilingual release notes](docs/releases/v2.12.2.md) for verification, reset controls, fallback activity and validation limits.

## [2.12.1] - 2026-10-19

### Fixes

- **Knowledge vault search matches any query word.** Notes no longer need every word in a query to appear in their path or text. Results rank by matched query words, then text hit count, then path, and include `words`; the model-visible tool description reflects the change. ([#245](https://github.com/foolzzz/looper/pull/245))
- **Vault writes pass search-only ancestor directories.** The no-symlink walk uses `O_SEARCH` on macOS or `O_PATH` on Linux, removing v2.12.0's requirement to read every ancestor on those systems. The server still needs to enter each ancestor and read and search the vault itself. Saving settings returns HTTP 400 for an impassable vault path, an unreadable or unsearchable vault, or an unreachable writable folder, instead of failing at write time or with HTTP 500. ([#245](https://github.com/foolzzz/looper/pull/245))

See the [bilingual release notes](docs/releases/v2.12.1.md) for verification, activation and remaining limits.

## [2.12.0] - 2026-10-19

### Highlights

- **Deployment settings move into a private ops file.** `looper config init-ops` writes `<runtime-root>/ops/server.toml` from the settings in effect. Reinstall the service after migrating so its unit names the file and no longer carries the database URL. The file covers database, server, backup and operator notes settings; flags override the file, and the old environment variables and `.env` deployment settings are fallback inputs for this release only, with warnings. ([#241](https://github.com/foolzzz/looper/pull/241))
- **Child processes start from an environment allowlist.** Agents, schedule gates, probes and helpers no longer inherit the whole server environment. Looper still adds provider bindings, agent settings and child-specific variables. Backups have a separate libpq/Docker environment. This is not filesystem isolation: a child running as the same user can still read the ops file. ([#241](https://github.com/foolzzz/looper/pull/241))
- **Scheduler limits become system settings.** Settings → Scheduler and `GET/PUT /api/scheduler-settings` own the minimum interval (built-in 300 seconds; a gated schedule uses `min(60, value)`) and default run timeout (built-in **no limit**; a configured limit is 1 second to 30 days). A schedule inherits with `run_timeout_seconds: "default"`, sets explicit seconds, or chooses null for no limit. Existing schedules keep their choice; new ones inherit. Each run records and keeps its starting timeout. Manager tools and configuration export/import carry the settings and choices. ([#237](https://github.com/foolzzz/looper/pull/237))
- **Claude Code agents refuse other sessions' messages.** Every launch sets `crossSessionInbound: "refuse"` and denies `ListAgents`; without agent teams it also denies `SendMessage`, including every Planner and consult. Team runs retain teammate messaging and can still address an external session whose name they know. The isolation record becomes `personal-config-off/v3`. Codex, Grok and OMP get no corresponding launch change. ([#238](https://github.com/foolzzz/looper/pull/238))
- **An optional Obsidian knowledge vault carries notes between tasks.** Configure the absolute path, writable folders and optional rules file in Settings → Knowledge vault, then select On for the project's Knowledge vault (off by default). Planner, Workers and Reviewers share `looper_vault_search`, `looper_vault_read` and `looper_vault_write` across runtimes. Tool writes stay within writable folders, walk the saved real path without following symbolic links, check `expected_sha256` for conflicts and, in a git vault, commit each changed note's literal path separately with its commit recorded in task activity. Unchanged text creates no commit; a non-git vault or failed commit leaves the file written with an uncommitted status. At task completion or failure, each Planner instance gets at most one closeout turn for lasting knowledge under the vault's rules; closeout needs a writable folder and task journal, skips at the spend cap, and warns without changing the result on failure. Agents can read notes throughout the vault, including personal ones; shell access can still edit files directly. Tool lists are fixed per Planner turn or Worker/Reviewer run, while every call re-checks settings. The Manager can change the system setting with confirmation but has no vault tools yet; Telegram and Lark use that settings action, the project switch is dashboard-only, and there is no new vault CLI command. ([#239](https://github.com/foolzzz/looper/pull/239))

### Behavior changes

1. **Startup has no implicit database URL.** Commands needing a database refuse when none is configured. Explicit `looper config init-ops` writes the former default `postgresql://localhost:5432/loopx` only if no source names one, and tells the operator to check it. The file is private, owned by the user, not a symlink; initialization writes mode 0600 in a 0700 directory and refuses to overwrite an existing file. Invalid configuration stops the command. ([#241](https://github.com/foolzzz/looper/pull/241))
2. **`.env` no longer populates the server environment.** Only legacy deployment settings are read into memory for one release; other keys are ignored with warnings naming the keys. Move channel secrets to Settings or the server environment. Deployment and runtime-root variables are deprecated; use the ops file and `--runtime-root`. `serve --host/--port/--verbose` override the file. ([#241](https://github.com/foolzzz/looper/pull/241))
3. **Implicit child variables outside the allowlist stop reaching tools now.** Put additional agent variables in agent settings. Probes and gates have no per-agent environment hook. Service installation also stops copying all `LOOPER_*`, `LOOPX_*`, `ANTHROPIC_*` and `LARK_*` variables; it captures fixed system/network/runtime names, the Telegram bot token and explicit `--env NAME` additions. An ops setting cannot be passed with `--env`. ([#241](https://github.com/foolzzz/looper/pull/241))
4. **Service installation requires an ops file.** It refuses while a deployment setting still comes from an environment variable and writes absolute `--config`/`--runtime-root`. An explicit install port wins; an older unit's port is retained when the file has no port, otherwise the file's port applies (default 8767). Status and restart read that port. Reinstalling rewrites the old unit; restarting alone does not. ([#241](https://github.com/foolzzz/looper/pull/241))
5. **The old scheduler minimum is read from the server environment, not `.env`.** `LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS` and its `LOOPX_*` alias remain for one release only when no system minimum is saved. Save the value in Settings → Scheduler before reinstalling a service that used to capture it. Saved values win; clearing one can expose the old variable again. Minimum changes affect later saves, without revalidating existing rules. ([#237](https://github.com/foolzzz/looper/pull/237))
6. **Schedule views may return `run_timeout_seconds: "default"`.** Use `run_timeout_effective_seconds` for the resolved value. Omitted on create means inherit; omitted on update preserves the choice. Existing explicit limits and no-limit choices are preserved by migration; change them to Use the default if they should follow the system setting. A started run keeps its timeout, also passed to the gate. Manager system-setting changes require confirmation; Telegram and Lark use the Manager's tools. There is no new scheduler CLI command. ([#237](https://github.com/foolzzz/looper/pull/237))
7. **Claude Code peer tools are restricted automatically on launch.** Workflows using `ListAgents`, or `SendMessage` without teams, must be adjusted. Incoming peer messages are refused. Team runs and shell commands still have the limits described in the release notes; these controls are not a sandbox. ([#238](https://github.com/foolzzz/looper/pull/238))
8. **The knowledge vault is off by default.** With the system vault unset or the project Off, task agents have no vault tools, their prompts stay unchanged and no vault closeout runs. An empty writable list enables read-only access. The Manager's settings tool gains the `knowledge_vault` action in every installation, even when the vault is off. ([#239](https://github.com/foolzzz/looper/pull/239))

### Upgrade

Re-run `install.sh`. In the same environment and checkout as the existing service, run **`looper config init-ops`**, using the existing runtime root and any custom config location. Check the generated database, public URL, backup, notes root and custom host/port; an installed unit's environment is not imported automatically. Move other `.env` values to Settings or supported server variables, save a legacy scheduler minimum in Settings → Scheduler, and configure extra agent variables in agent settings. Remove deprecated deployment variables, then run **`looper service install` again** with the same location flags and necessary `--env NAME` additions. Foreground users restart `looper serve` with those flags. Run `looper doctor` and read back the settings.

**If the operator does nothing this release:** old deployment variables and their `.env` entries still work with warnings, and an old unit remains usable until reinstalled, carrying its old environment. No ops file is created automatically. Startup relying only on the implicit database URL now fails; other `.env` keys are already ignored and child environments already filtered, so dependent channels/tools can fail now. A scheduler minimum set only in `.env` is ignored. Complete the migration before the next release removes the compatibility inputs. See the [bilingual release notes](docs/releases/v2.12.0.md) for verification, activation and known limits.

### Validation

- Merged PR reports: [#238](https://github.com/foolzzz/looper/pull/238) records tests, mutation checks and real Claude Code 2.1.285 refusal/tool/team probes; [#241](https://github.com/foolzzz/looper/pull/241) records targeted tests, isolated-server migration/environment checks and one successful real `single_step` on each of Claude Code, Codex, Grok and OMP; [#237](https://github.com/foolzzz/looper/pull/237) records targeted PostgreSQL/Manager/fake-dialog tests, web build/lint/Vitest and headless checks of Settings and the schedule editor; [#239](https://github.com/foolzzz/looper/pull/239) records vault unit/PostgreSQL/closeout tests, prompt byte-identity with the vault off and an opus closeout eval (4/4). These are PR reports, not runtime checks rerun for this release.
- [#239](https://github.com/foolzzz/looper/pull/239) reports vault unit/DB/stand-in-CLI closeout tests, prompt byte-identity checks with the vault off, 26 mutation checks and web checks. A real Claude Code Worker was refused outside writable folders, and a haiku Planner closeout committed one note. The opus closeout eval on merged code is below; a live-server end-to-end run with a sonnet Planner was not finished for this release.
- Not verified in those reports: real launchd/systemd installation, Linux, filtered-environment Codex OAuth and standalone-token-only Claude Code login; teammate deny-list inheritance and managed Claude Code settings; the schedule editor in a browser. PR #237 did not report a full local suite. Vault tools were not called in real mode on Codex, Grok or OMP; concurrent Obsidian edits and network or other case-insensitive filesystems were not verified.
- Vault writes open every directory from the filesystem root to the vault and need read permission on each. An ancestor that permits traversal but not reading makes the write fail, even if the vault itself is writable.

### Prompt eval

Broad release-candidate runs used `e47a6c4f4` (main with #237, #238 and #241). #239's byte-identity tests show the Planner guide and Worker/Reviewer prompts unchanged with the vault off, so those task-tier results carry over to merged main. The Manager's settings tool gains `knowledge_vault` even when off; the dialog scores below precede that addition. Committed baselines remain at v2.9.0 (`c9ac42d5c`); haiku was not a gate.

- Opus Planner, three reps: 59/60 (v2.11.0: 60/60). The miss was `roles_multi_platform` rep 1 (`interface_dependents 3 < 4`); the other two reps passed, treated as single-sample planning variance.
- Opus Manager at xhigh, all dialogs, three reps: 81/81.
- Codex, OMP and Grok core tasks (`single_step`, `chat_correction`, `reviewed_step`), two reps: 6/6 each; Grok's `status` dialog 2/2.
- #237 PR run: opus Manager dialogs 54/54, two reps, including the three new schedule-settings dialogs.
- #241 filtered-environment real-CLI smoke, `single_step`, one rep each: Claude Code operator login (sonnet), Codex API key, Grok login and OMP DeepSeek, 1/1 each.
- #239 opus Planner closeout on merged code, two reps: `vault_closeout_keeps` 2/2 (fact kept in a writable folder), `vault_closeout_nothing` 2/2 (no note written), $0.52. A live-server end-to-end run with a sonnet Planner was not finished for this release.

## [2.11.0] - 2026-10-19

### Highlights

- **Manager and Planner replace the runtime's default system prompt on Codex, Grok and OMP too.** On v2.10.0 that was Claude Code only; the other three runtimes took append or replace from the Agent Profile. The mode is the role's on every runtime: Manager and Planner replace, Worker, Reviewer, Planner-defined role runs and consults append. Settings → Roles changes a role. A task can override its planner, worker or reviewer. The profile-level mode is removed. An older configuration export still imports; the profile field is ignored and the import records a note. One migration carries a Worker or Reviewer profile `replace` onto the role layer, then drops the profile column. ([#226](https://github.com/foolzzz/looper/pull/226))
- **The Manager can set a task's mode.** `looper_task_update` takes `system_prompt_modes` (`append`, `replace`, or null to return that role to the role setting), the same write as `PUT /api/tasks/:id/agents`. ([#229](https://github.com/foolzzz/looper/pull/229))
- **The dashboard signs out, and a 401 is one screen.** A password session gets a Sign out control. When any `/api/` call returns 401, the dashboard shows only "Your session ended. Reload to sign in again." with a Reload button, and every poller and stream stops. ([#229](https://github.com/foolzzz/looper/pull/229))
- **A failed Planner or Worker turn names the runtime that ran.** The text is "Codex turn did not succeed" or "OMP exited ...", not always "Claude Code". ([#229](https://github.com/foolzzz/looper/pull/229))

### Behavior changes

These apply on upgrade. Each line says what the operator does.

1. **Manager and Planner launches on Codex, Grok and OMP replace that runtime's default system prompt.** On v2.10.0 they appended unless the Agent Profile was set to replace. To keep appending, open Settings → Roles and set that role to Append. A task overrides its own planner, worker or reviewer on the task's Agents tab, with `system_prompt_modes` on `PUT /api/tasks/:id/agents`, or with the Manager's `looper_task_update`. Null on a role returns that role to the role setting. The Manager has no task-level mode. Worker, Reviewer and Planner-defined role runs append unless you set otherwise. A consult appends even when its seat's role is replace. Claude Code already replaced for the Manager and the Planner on v2.10.0; that does not change. With haiku as the Planner, replace scored lower than append on v2.10.0: run the Planner on opus or sonnet, or set the Planner role to append when it uses haiku. ([#226](https://github.com/foolzzz/looper/pull/226))
2. **The profile-level mode is removed.** `POST /api/agent-profiles` and `PUT /api/agent-profiles/<id>` refuse `system_prompt_mode` (HTTP 400, `unknown field 'system_prompt_mode'`). The Manager's profile tool rejects it as an unknown argument (`invalid`). Configuration export no longer writes it. An older export still imports: the profile is kept, the field is ignored, and the import notes `profiles NAME: its system_prompt_mode is ignored; the mode is set per role (Settings → Roles) and applies to every runtime`. Set the mode in Settings → Roles, or on the task. ([#226](https://github.com/foolzzz/looper/pull/226))
3. **The first start migrates once, while `agent_profiles.system_prompt_mode` still exists.** A Worker or Reviewer whose effective profile (the role layer's, else the shared defaults', else the built-in `claude-code/opus`) replaced, and whose role layer has no mode yet, gets `replace` on that role layer. A mode already stored there, including one v2.10.0 wrote for a Claude Code profile, stays. Then the column is dropped. A later start does not run this again. Manager and Planner are not copied: they replace because that is the built-in value, so a Codex, Grok or OMP profile that appended does not keep append. After `looper serve` has migrated, open Settings → Roles. If Worker or Reviewer shows Replace and you wanted append, set it back to Append. ([#226](https://github.com/foolzzz/looper/pull/226))
4. **A task mode stored on v2.10.0 now applies on every runtime.** On v2.10.0 `task_system_prompt_modes` affected Claude Code only. The next Planner, Worker or Reviewer launch on Codex, Grok or OMP uses it. Reset the role on the task's Agents tab, or send a null role value in `system_prompt_modes` through `looper_task_update`, if that override should not apply. ([#226](https://github.com/foolzzz/looper/pull/226))
5. **On 401 the dashboard shows one session-ended screen.** The text is "Your session ended. Reload to sign in again." and the button is Reload. Every poller and stream stops, and the call is not retried. Reload to sign in again. You do not turn this on. A password session also gets Sign out in the sidebar (`POST /logout`, then `/login`). The local browser's automatic sign-in shows no Sign out. `GET /api/status` reports `can_sign_out` true only for a password session. Dashboard pages send `Referrer-Policy: same-origin` so that POST carries an Origin; you do not set the header. ([#229](https://github.com/foolzzz/looper/pull/229))
6. **Planner and Worker failure text names the runtime that ran.** Examples: "Codex turn did not succeed", "OMP exited ...", and a Worker "Grok run did not succeed". On v2.10.0 the same failures said "Claude Code". Nothing to configure. A check that matched the old sentence matches the runtime's label instead. ([#229](https://github.com/foolzzz/looper/pull/229))

A `vX.Y.Z` tag still publishes the Python release.

### Fixes

- **The dashboard handles a dead session.** On v2.10.0 a 401 left the page to be reloaded by hand, and there was no Sign out control. A password session can sign out. Any `/api/` 401 replaces the page with one session-ended screen and stops every poller and stream. ([#229](https://github.com/foolzzz/looper/pull/229))
- **Failed turns name the runtime.** A Planner or Worker failure on Codex, Grok or OMP no longer reads as a Claude Code failure. ([#229](https://github.com/foolzzz/looper/pull/229))

### Validation

- [#229](https://github.com/foolzzz/looper/pull/229): `tests/test_operator_auth.py`, `tests/test_ops_tasks_db.py` and `tests/test_agent_planner_worker.py` on PostgreSQL, 373 passed. Vitest 214 passed (24 files). `npm run build` succeeded. A password session in a browser showed Sign out and reached `/login`. After the session ended, an API call showed the session-ended screen. Opus Manager dialogs passed 48/48. The light theme was not checked.
- Release candidate `595b6f229`: opus Planner 60/60 and opus xhigh Manager dialogs 72/72, three reps each (v2.10.0: 60/60 and 69/72; its three dialog misses were the stale `config_tools` expectation fixed in [#224](https://github.com/foolzzz/looper/pull/224)). Codex, OMP and Grok passed the core tasks (`single_step`, `chat_correction`, `reviewed_step`) 6/6 each, two reps; Grok's `status` dialog passed 2/2. Committed baselines remain at v2.9.0. Haiku was not a release gate.
- [#226](https://github.com/foolzzz/looper/pull/226#issuecomment-6265891409), paired runs, two reps, replace vs v2.10.0 append: Codex Planner 36/40 vs 36/40, Manager dialogs 46/48 vs 46/48; OMP Planner 35/38 vs 37/38 with 2 timeouts on each side, dialogs 46/48 vs 46/48; Grok Planner 38/38 plus 2 timeouts vs 40/40, dialogs 47/48 vs 47/48. Grok's timeouts were `interface_first` and `room_consult` (900 seconds) while several eval queues ran at once. OMP's extra misses were a runtime error in `room_interface` and `room_consult`, which also failed under append.

## [2.10.0] - 2026-10-19

### Highlights

- **Claude Code's Manager and Planner replace Claude Code's default system prompt.** Worker, Reviewer, Planner-defined role runs and consults still append. Settings → Roles sets append or replace per role. A task can override its planner, worker or reviewer on the task's Agents tab. A Claude Code profile refuses `replace`. One migration copies a Claude Code profile's `replace` onto the Worker or Reviewer role layer that used it, then sets every Claude Code profile to `append`. Codex, Grok and OMP keep the profile-level mode. ([#219](https://github.com/foolzzz/looper/pull/219))
- **An empty allowed-profiles list lets Planners choose any capable profile.** That list is empty by default. A Planner may pick any registered profile that can run the role, with no confirmation. Set a list to restrict the choice. Deleting a profile an open task's Planner or task role chose is refused. ([#220](https://github.com/foolzzz/looper/pull/220))
- **A new gate may number its agenda items `q1`, `q2`, and so on.** Any other id is refused with a message that names the id Looper assigns. Leaving `id` out still works. ([#222](https://github.com/foolzzz/looper/pull/222))

### Behavior changes

These apply on upgrade. Each line says what the operator does.

1. **Manager and Planner launches on Claude Code replace Claude Code's default system prompt.** They appended on v2.9.0. To keep appending, open Settings → Roles and set that role to Append. A task overrides its own planner, worker or reviewer on the task's Agents tab (`system_prompt_modes` of `PUT /api/tasks/:id/agents`; null clears the task's choice). The Manager has no task-level mode, and the Manager's task tools do not set one. Worker, Reviewer and Planner-defined role runs append unless you set otherwise. A consult on Claude Code appends even when its seat's role is replace. With haiku as the Planner, replace scored 31/40 against 50/60 for append on the Planner tier: run the Planner on opus or sonnet, or set the Planner role to append when it uses haiku. ([#219](https://github.com/foolzzz/looper/pull/219))
2. **The first start migrates Claude Code profiles once.** When `agent_setting_layers.system_prompt_mode` is added, a Worker or Reviewer whose effective profile (the role layer's, else the shared defaults', else the built-in `claude-code/opus`) was a Claude Code profile set to `replace` gets `replace` on that role layer. Then every Claude Code profile is set to `append`. A later start does not run this again. Check Settings → Roles after the first start if a Claude Code profile was set to replace. ([#219](https://github.com/foolzzz/looper/pull/219))
3. **`replace` on a Claude Code profile is refused.** Saving or updating one returns 400: `Claude Code's system prompt mode is set per role in Settings → Roles`. Import skips that profile and records the same reason. Set the mode on the role, or on the task. Codex, Grok and OMP profiles still save `append` or `replace`. ([#219](https://github.com/foolzzz/looper/pull/219))
4. **An empty allowed-profiles list lets Planners choose any capable profile, without confirmation.** On v2.9.0 an empty list meant the Planner chose nothing. Every project that never filled the list changes. To restrict, set the list on the project's Agent Profiles tab ("Profiles the Planner may choose") or `PUT /api/projects/:id/allowed-profiles` with `profile_ids`. A non-empty list still drops Planner choices it leaves out. Setting it back to `[]` lifts the restriction and keeps choices. Spend stays bounded by the run budget caps. Deleting a profile an open task's Planner or task role chose is refused; the message names the task. Clear the choice, or wait until the task ends. Choices on ended tasks go with the profile. ([#220](https://github.com/foolzzz/looper/pull/220))
5. **A Planner may send `id` on a new gate's agenda items only as `q1`, `q2`, ….** Any other id is refused. You do not turn this on. Replies that numbered items that way and were refused on v2.9.0 are accepted. ([#222](https://github.com/foolzzz/looper/pull/222))

Codex, Grok and OMP keep the profile-level system prompt mode. Per-role mode for those runtimes is planned. A `vX.Y.Z` tag still publishes the Python release.

### Fixes

- **OMP can omit optional MCP arguments on openai-responses.** OMP CLI 18.4.x omitted `strict` for endpoints it did not recognize, such as a local OpenAI-compatible proxy. The Responses API treats a missing `strict` as strict, so the model filled every optional MCP argument; an OMP Planner could not define a custom role without inventing a template. Looper now marks openai-responses models in OMP's launch config with `compat: {supportsStrictMode: true}`, so OMP sends its MCP tools with explicit `strict: false`. Looper's tool schemas, Claude Code, and the openai-chat / anthropic-messages providers are unchanged. ([#223](https://github.com/foolzzz/looper/pull/223))
- **New gate agenda items may carry the id Looper assigns.** A `[PLAN]` reply that set `"id": "q1"` on a new item was refused with `unknown keys: id`, and the repair turn did not fix it. The id is accepted when it is `qN` for that position. Any other id is refused with `<where>.id must be "qN" or left out: Looper numbers new items q1, q2, …`. OMP with deepseek-flash passed the five gate tasks 10/10 (`gate_parallel_branch` 0/2 → 2/2). ([#222](https://github.com/foolzzz/looper/pull/222))

### Validation

- [#224](https://github.com/foolzzz/looper/pull/224), eval-only: the `config_tools` dialog now starts its allowed-profiles step with a non-empty list, matching #220 (an empty list already allows any profile). The corrected dialog passed 1/1 on opus xhigh.

## [2.9.0] - 2026-10-18

### Highlights

- **The operator's HTTP API asks who is calling.** Every `/api/` route except the agents' own tools needs the operator token in `<runtime root>/operator-token` (mode 0600). The CLI sends it. A local browser signs in automatically only on a direct loopback connection that meets the top-level navigation conditions; LAN or proxy access needs a password. Another machine signs in with `looper auth set-password`. Anonymous calls get 401. Password sign-in is new; query token/code sign-in is not supported. ([#207](https://github.com/foolzzz/looper/pull/207))
- **A custom system prompt, with a preview.** An Agent Profile's `custom_prompt` prefixes Looper's composed prompt for Claude Code, Codex, Grok and OMP; the harness, role, context and contract follow. `system_prompt_mode` separately controls append/replace. Claude Code still sends a billing header and a fixed identity line before the main block. Empty adds no custom-prompt layer; clearing it removes only the prefix and retains other v2.9.0 changes. The preview shows Profile/role templates; task-specific contracts and other launch context are resolved later. ([#198](https://github.com/foolzzz/looper/pull/198))
- **Codex and OMP Planners are read-only.** Codex uses its read-only sandbox and approves only Looper's MCP tools. OMP gets a read-only tool list when its version reports that capability. ([#203](https://github.com/foolzzz/looper/pull/203))
- **Grok keeps the Manager and each task's Planner in separate resident processes and sessions.** `grok agent stdio` (ACP). Workers stay one-shot. Cost of a resident turn comes from `costUsdTicks` (1e10 ticks per USD). ([#205](https://github.com/foolzzz/looper/pull/205))
- **Lark (bot token), off by default.** A second Lark channel talks to the open platform with an app id and an app secret. The lark-cli channel is unchanged. Not yet tried against a real tenant. ([#209](https://github.com/foolzzz/looper/pull/209))
- **A running Codex or OMP Worker can be steered** when the installed CLI reports that capability. Older CLIs keep the previous one-shot run. ([#211](https://github.com/foolzzz/looper/pull/211))
- **Where a file was written.** A Worker that writes in another worktree or a commit gives the Reviewer and the Planner a typed location and read-only git hints. Structured locations and Reviewer file lists use single-line ASCII JSON; other name fields use `prompt_name`. A schedule can already name a profile per role; the Manager and the schedule dialog now suggest a cheaper Planner. ([#210](https://github.com/foolzzz/looper/pull/210))

### Behavior changes

These apply on upgrade. Each line says what the operator does.

1. **Anonymous `/api/` calls get 401**, including `/api/status` and unknown paths. Send `Authorization: Bearer` with the token, or open the dashboard through a direct loopback connection that meets the top-level navigation conditions for local auto-login. Agent routes under `/api/agent/v1/` still use agent credentials. `OPTIONS /api/*` no longer returns 204 unconditionally: missing the appropriate credential gives 401; an unimplemented method with a valid credential gives 404. ([#207](https://github.com/foolzzz/looper/pull/207))
2. **Password sign-in is new.** Query token/code sign-in is not supported. ([#207](https://github.com/foolzzz/looper/pull/207))
3. **`looper serve` refuses a runtime root that group or others can write**, and one the user does not own. It prints `chmod go-w <runtime root>` and does not change the mode itself. An invalid token file also prevents startup. An invalid password file makes password sign-in return 503; fix its permissions or run `looper auth set-password`. Deleting the password file does not recreate it on startup. ([#207](https://github.com/foolzzz/looper/pull/207))
4. **Other machines need a password.** `looper auth set-password [--user NAME]` asks twice and never takes the password on the command line. It stores a scrypt hash. `looper auth clear-password` removes it. Until one is set, only a local browser on a direct loopback connection that meets the top-level navigation conditions signs in; LAN or proxy access needs a password. Sessions last 7 days and end when the password is changed or cleared, or when the server restarts. ([#207](https://github.com/foolzzz/looper/pull/207))
5. **Behind an https reverse proxy, set `dashboard_url` to the https address** (`LOOPER_PUBLIC_URL` before start), or `POST /login` and `POST /logout` answer 403. For an installed service, set the variable and rerun `looper service install`, preserving its original label, port and runtime root; restart alone does not recapture `LOOPER_PUBLIC_URL`. Those two routes require an Origin whose scheme and host match. Sign-in is rate-limited with lockout. Login responses are `no-store`, and logs never contain the credentials. ([#207](https://github.com/foolzzz/looper/pull/207))
6. **Reload a dashboard tab that was open before the upgrade.** The page is now `no-store`. After a session ends, the page does not yet handle 401 by itself; reload it. There is no logout button; `POST /logout` is the endpoint. ([#207](https://github.com/foolzzz/looper/pull/207))
7. **An OMP Planner loses its shell when the probed OMP version reports a read-only tool list.** Re-detect an OMP runtime that was already registered (Settings → Agent Runtimes). An older or unreadable version keeps the previous directory comparison. Without `read_tools`, OMP does not run a consult and hands it to the Planner as `runtime_cannot`. Re-detect an existing OMP runtime to restore consult; older or unrecognized CLI versions do not support it. A Codex Planner retains its existing read-only sandbox and regains Looper MCP tool calls. ([#203](https://github.com/foolzzz/looper/pull/203))
8. **The Grok Manager and each task's Planner keep separate resident ACP processes and sessions.** The first turn starts it; later turns reuse it. If `grok agent stdio` cannot serve, that session falls back to one process per turn. Workers and Reviewers stay one-shot. A resident turn's cost is `costUsdTicks` divided by 1e10. A missing or zero cost stays unknown. ([#205](https://github.com/foolzzz/looper/pull/205))
9. **Re-detect Codex and OMP to steer a running Worker.** Steering starts when the stored probe reports the capability (Codex `app-server`, OMP `rpc`). An older CLI keeps the previous behavior. A steer that times out at 4 seconds may still arrive; the Planner keeps it as delivery not confirmed. ([#211](https://github.com/foolzzz/looper/pull/211))
10. **A file found in another worktree or commit is named on the Reviewer's first turn and in the Planner's step report**, with read-only git hints. Structured locations and Reviewer file lists use single-line ASCII JSON, which can quote ordinary names too. Other name fields use `prompt_name`: a plain printable name stays as written; anything else is one line of ASCII-escaped JSON. A schedule's Planner does not change unless you set a profile. The dialog and the Manager tool only suggest a cheaper one. ([#210](https://github.com/foolzzz/looper/pull/210))
11. **The dashboard renders over plain http from another host.** Reload that tab. `crypto.randomUUID` is only available in a secure context, so ids fall back to `crypto.getRandomValues`. https and localhost are unchanged. ([#213](https://github.com/foolzzz/looper/pull/213))

`custom_prompt` stays empty until a profile sets it. The Lark (bot token) channel stays off until Settings enables it. A `vX.Y.Z` tag still publishes the Python release.

### Fixes

- **Grok Workers run again.** v2.8.0 passed `session_dir` into `run_command()`, which the Grok adapter did not accept, so every Grok Worker run failed. A test now checks that each runtime adapter accepts every parameter of the abstract commands. ([#208](https://github.com/foolzzz/looper/pull/208))
- **Planner step decisions see the agent room again.** Since v2.8.0 every room summary failed with "cannot read the room for step ...", because the decision still read a single `review_role` after steps gained `review_roles`. ([#214](https://github.com/foolzzz/looper/pull/214))
- **Tags `vX.Y.Z-go` do not start the Python release workflow.** A `vX.Y.Z` tag still does. ([#202](https://github.com/foolzzz/looper/pull/202))

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
  /api/projects/{id}/agent-settings` and `PUT
  /api/projects/{id}/agent-settings/roles/<role>` replace
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
  and uninstall an item; `GET /api/projects/{id}/installs` shows what applies
  to a project: its own installs and the inherited global ones. Settings shows
  the catalog with a global install (role checkboxes) and uninstall per item;
  the project page's Skills tab shows what is installed there, installs from
  the catalog and writes or edits the project's own skills. They replace
  `/api/settings/library*` and `GET /api/projects/{id}/skills`; each run's
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
  `PUT /api/projects/{id}`) gives the Planner of a task that completes one
  more turn, whose Markdown summary of the journal becomes the text document
  `Lessons: <requirement>`. `write` saves it to the project's knowledge;
  `draft` keeps it as a draft that no agent sees until the user confirms it,
  optionally edited, or discards it on the project's Knowledge tab
  (`GET /api/projects/{id}/knowledge/drafts`,
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
  body. `GET /api/projects/{id}/knowledge/<doc_id>` and
  `GET /api/projects/{id}/skills/<skill_id>` return one item with its body.
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
    - `GET`/`POST /api/projects/{id}/skills` and
      `PUT`/`DELETE /api/projects/{id}/skills/<skill_id>`; `PUT` takes the
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
  - **API and dashboard.** `GET`/`POST /api/projects/{id}/knowledge` and
    `PUT`/`DELETE /api/projects/{id}/knowledge/<doc_id>`; the project editor
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
  `GET`/`POST /api/projects` and `PUT`/`DELETE /api/projects/{id}` manage
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
