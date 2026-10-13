# Agent System Architecture

The LoopX agent system is a three-tier multi-agent architecture with a web
dashboard for task management and real-time monitoring.

## Tiers

```
User → Chat/Lark/Telegram → Manager → TaskRouter → Planner → WorkerPool → Claude CLI
```

### Manager (`loopx/agents/tiers/manager.py`)

- Entry point for all user messages
- Classifies intent (chat vs task creation)
- Routes task requests to the TaskRouter via `TASK_REQUEST` messages, each
  with its project's `project_id`
- Places each confirmed task in a project. Every user turn starts with a
  `Projects` section listing the existing projects (name, default directory or
  "no default directory", description cut to 80 characters). The code-owned
  rule appended to the editable system prompt asks for `"project"` and
  `"work_dir"` in the task JSON. The project is matched by name ignoring case;
  an unknown name becomes a new project whose default directory is the task's,
  and the reply says so. The directory is the task's own, else the project's
  default. Without a project name, or without any directory, the Manager asks
  instead of creating the task
- Maintains chat conversation context per session
- Runs one resident Claude process through `ResidentClaude`
  (`loopx/agents/claude_cli.py`), which each task's Planner uses too, started
  on the first chat message:
  `claude --print --verbose --output-format stream-json --input-format stream-json --include-partial-messages <isolation flags> --append-system-prompt-file <file> --system-prompt-snapshot off --model M [--effort E] [--resume <session_id>]`
  (see [What an agent's Claude Code loads](#what-an-agents-claude-code-loads)).
  Its system prompt is its harness (its own `harness_prompt`, else the default
  in Settings), the `manager` role prompt and `TASK_PLACEMENT_RULE`; it has no
  project and no task section.
  Each chat message is one user turn on its stdin and ends at that turn's
  `result`; messages are handled one at a time, and each turn is one Runs
  record. Claude's `total_cost_usd` is its process's running total, so a
  record's cost is the increase since the previous turn. When Claude exits
  (or dies mid-turn, which answers that message with an error) or the
  Manager's system prompt, model, effort or Settings environment changes, the
  next message starts a new process that
  resumes the saved session with `--resume`. Stopping the Manager closes
  Claude's stdin and kills it if it is still running after 5 s

### TaskRouter (`loopx/agents/tiers/task.py`)

- Every task belongs to exactly one project (`project_id`) and runs in its
  `work_dir`. There is no global default: the directory is the one given
  (`resolve_work_dir` requires an absolute path or `~/...`), else the
  project's default directory. Creating a task with neither, or starting a
  task without one, is refused, and the Manager asks for it when the
  conversation has not named it. The Worker creates the directory if it is
  missing and runs Claude there.
- A task keeps its project across restarts. Moving it to another project
  (any status) also updates the TaskRouter's live entry. A `TASK_REQUEST`
  without `project_id`, such as one from before projects replayed from the
  bus journal, is logged and dropped.
- A project may have a `background` (at most 10000 characters, `''` = none):
  what every task agent of the project should know. It goes into the system
  prompt of the project's Planner, Workers and Reviewers, never the
  Manager's, which spans all projects. `loopx/agents/system_prompt.py` builds
  that prompt for all three, in five layers:

  ```
  <harness prompt>                                L1: see Harness, model and environment
  <role prompt from agent_configs>                L2: planner, worker or reviewer row

  ## Project: <name>                              L3 (left out when the background is empty)
  <background>

  ## Task                                         L4: what this session is for
  ...                                                 (Planner: the task; Worker, Reviewer: the step)

  <code-owned contract>                           L5: REPLY_CONTRACT, WORK_CONTRACT or REVIEW_CONTRACT; always last
  ```

  A project can also override the harness of its Planner
  (`planner_harness_prompt`) and of its Workers (`worker_harness_prompt`,
  which its Reviewers use too); `''` inherits the default in Settings. They
  are read through the task the same way as the background.

  The task section is fixed for the session: the Planner lives as long as its
  task, a Worker or Reviewer as long as its step. It is in the system prompt
  so it survives auto-compaction, and it comes before the contract so nothing
  a user or the Planner wrote can override the output format. The task
  requirement in it is clipped at 8,000 characters (`TASK_REQUIREMENT_LIMIT`)
  with a visible `[clipped: N more characters]` marker. What changes from turn
  to turn stays in user turns.

  The project is read through the task (`get_task_project`), so a task moved
  to another project gets the new one. Every prompt reads it when it is
  built: an edit reaches the Planner's next turn and every Worker or Reviewer
  that starts afterwards, while a running Worker keeps the prompt it started
  with. The task's activity notes `Project background applied (N chars)`
  the first time a run uses one, then `Project background changed (N chars)`
  or `Project background removed` for the Planner. The background is
  guidance, so an unreadable project does not stop a run: a Worker or
  Reviewer runs without the section and logs `Warning: the project background
  was unavailable (...)` as an `error` activity, and the Planner keeps the
  project it last read and logs a warning.

- Per-task lifecycle management with concurrency control (`MAX_CONCURRENT_TASKS=3`)
- Creates/destroys PlannerAgent instances per task
- Handles task lifecycle: start, stop (pause), restart (resume), cancel
- Queues tasks when at capacity, auto-dequeues on completion
- Orphan detection on server startup (stuck tasks → "stopped")

### Planner (`loopx/agents/tiers/planner.py`)

- One Planner per task for the task's whole run. It is a Claude Code agent: it
  plans the steps, decides what happens after every Worker and Reviewer
  result, and answers chat messages about the task. Code validates its
  replies and does the dispatching; Workers make the changes, one Claude
  process per step attempt.
- Keeps one resident Claude process through `ResidentClaude`, like the
  Manager, started on the task's first turn in the task's `work_dir`:
  `claude --print --verbose --output-format stream-json --input-format stream-json --include-partial-messages --tools Read,Grep,Glob --allowedTools Read,Grep,Glob --permission-mode dontAsk <isolation flags> --append-system-prompt-file <file> --system-prompt-snapshot off --model M [--effort E] [--max-turns N] [--resume <session_id>]`.
  The system prompt is rebuilt for every turn from the harness (the project's
  Planner override, else the default in Settings), the `planner` role prompt,
  the task's project, the task section (task id, project name and
  description, working directory and the requirement) and `REPLY_CONTRACT`;
  the project is read before each event's turns. The turns themselves carry
  only what changed: the plan's progress, results, chat and supervision
  observations. When the prompt changes (a config edit, a new background or
  harness, a task moved to another project) its file and hash change, or the
  model, effort or Settings environment changes, the Planner stops
  its Claude, and the next process resumes the session with `--resume` and
  the new prompt. Only Read, Grep and Glob exist and are approved, `dontAsk`
  denies anything else without asking, and no MCP server loads; it never gets
  `--dangerously-skip-permissions`. Checked with Claude Code 2.1.285: the init
  event lists only Glob, Grep and Read and no MCP servers, and a Write made
  available is still denied in don't-ask mode.
- Each event is one user turn; turns run one at a time:
  - `[PLAN]` on `TASK_REQUEST`: Claude may read the project, then splits the
    requirement into 1–8 steps (`MAX_STEPS`), each with `id`, `instruction`,
    `acceptance_criteria`, `depends_on` and `review` (whether an independent
    Reviewer must pass it).
  - `[DECISION]` after every Worker result and every Reviewer result:
    `accept`, `review`, `retry`, `replan` or `fail` (see
    [How a task runs](#how-a-task-runs)).
  - `[CHAT]` on a user chat message: a reply and at most one action: `none`,
    `forward` (pass text to the Worker of a running step) or `add_steps`.
    Events are handled in arrival order, so a message sent during another
    turn waits for it; one that arrives before the task has a plan, or after
    it ended, is answered without a turn.
- Reply contract (`loopx/agents/tiers/planner_protocol.py`): `REPLY_CONTRACT`
  comes last in the system prompt, after the editable role prompt and the
  project section. Every reply ends with exactly
  one ```` ```json ```` fenced object, parsed into typed dataclasses and enums
  and validated against the current plan: no unknown keys, unique step ids,
  `depends_on` naming known steps without cycles, at most 8 steps (dropped
  steps that ran count too), only the decisions allowed for that result, at
  most 2 retries per step, and started steps never changed. An invalid reply
  gets one repair turn that names the error.
- Failures:
  - Planning that still fails (an invalid reply after the repair turn, a
    Claude error, a turn timeout, Claude that cannot start, an unusable work
    directory) fails the task, with the reason in `tasks.error` and the
    activity. There is no single-step fallback.
  - A decision turn that still fails falls back to the step's own verdict
    (the Worker's fail-closed verdict, or the Reviewer's), spawns no Reviewer,
    and logs `Warning: the Planner could not decide ...` as an `error`
    activity.
  - A chat turn that fails answers the user with the error and takes no
    action.
  - When Claude dies, its turn fails and the next turn starts a new process
    with `--resume <session_id>`; the session id is kept in the Planner's plan
    file under `.local/planner/`.
- Each turn is one `llm_records` row with agent_id `planner-<task_id>`, the
  `task_id`, its own cost, `metadata.pid` and the system prompt's record
  (`system_prompt`, `system_prompt_sha256`, `prompt_layers`). The activity gets short
  summaries: `Planner Claude Code started (pid N...)`, `Plan created: ...`,
  `Planner decision on step N (s2): accept: ...`, `Plan changed: ...`.
- The TaskRouter calls `PlannerAgent.stop()`, which ends its Claude, when the
  task ends (completed, failed, stopped or cancelled) and when the server
  stops.

### WorkerPool (`loopx/agents/tiers/worker.py`)

- Manages a pool of Worker processes
- Each Worker runs Claude CLI as a subprocess with `stream-json` output
- Real-time event streaming (tool_use, text) captured to activity table
- Command: `claude --print --verbose --output-format stream-json --include-partial-messages --input-format stream-json --replay-user-messages --dangerously-skip-permissions <isolation flags> --append-system-prompt-file <file> --system-prompt-snapshot off --model M [--effort E]`,
  plus `--max-turns N` when the config sets `max_turns` above 0. A Worker
  reads the `worker` row and a Reviewer the `reviewer` row, for every run. The
  first user turn goes on stdin; stdin stays open so the Planner can add user
  turns mid-run, and closes when the result arrives
- The step is the system prompt; only other agents' output is the user turn:
  - system prompt: the harness (the project's Worker override, else the
    default in Settings; Reviewers follow the Workers of their project), the
    role prompt of the `worker` or `reviewer` row (the default when the
    row's prompt is empty or unreadable), the project section, the task
    section, then the code-owned contract, last:
    `WORK_CONTRACT` (the step's rules and the report format below) or
    `REVIEW_CONTRACT` (the review's rules and the verdict format). A Worker's
    task section has the whole task's requirement as context, then the
    step's label, instruction, working directory, target files, acceptance
    criteria, validation commands and, on a retry, the attempt number and the
    feedback on the previous attempts. A Reviewer's has the requirement, then
    the reviewed step's instruction and acceptance criteria;
  - first user turn: for a Worker, a short start cue and what the steps it
    depends on reported (summary and changed files, from `WORK_ITEM`
    `dependencies`); for a Reviewer, the Worker's report (summary, changed
    and deleted files) as data to check, never in the system prompt;
  - messages sent mid-run (forwarded chat, Planner steering) stay user turns
    and do not restate the contract.
- `WORK_ITEM` carries the step's fields apart: `requirement`, `step_key`,
  `instruction` (the step's own, without feedback), `feedback` (a list) and
  `attempt`, `dependencies`; `VERIFY_REQUEST` adds `requirement` and
  `step_key`.
- Each run is one `llm_records` row with `metadata.command` (the exact
  argv), the system prompt's record (`system_prompt`, `system_prompt_sha256`,
  `prompt_layers`) and `metadata.step_id`; the activity's launch line shows
  the isolation settings by their size
- Claude is stopped when a run takes longer than its row's (`worker` or
  `reviewer`) `timeout_seconds` in total (default 86400, 0 = no limit). The step fails
  with `Claude ran longer than the N s limit (timeout_seconds)`. Partial
  deltas only keep the run's heartbeat (`last_output_at`) current; they do
  not extend the limit
- A step counts as completed only when all of these hold; anything else fails the
  step with the reason in `error`:
  - the run's `result` event has `subtype: success`, so hitting max turns fails;
  - the final message ends with the JSON report
    `{status, changed_files, deleted_files, summary}`, with `status` set to
    `completed`;
  - every file in `changed_files` exists, and every file in `deleted_files` is
    gone.
- A reviewer must return a boolean `passed`; prose without the verdict is not a
  pass.

### What an agent's Claude Code loads

Every agent (Manager, Planner, Worker, Reviewer) sees what LoopX composes and
the project's own `CLAUDE.md`, not the personal Claude Code configuration of
whoever runs LoopX. `isolation_args(cwd)` in `loopx/agents/claude_cli.py` is
the one flag set all four commands carry; `ISOLATION_ID`
(`personal-config-off/v1`) names it in each run's record:

```
--setting-sources project --strict-mcp-config
--settings {"autoMemoryEnabled":false,"claudeMdExcludes":[...]}
```

Verified with Claude Code 2.1.285 (`/context`, the stream-json `init` and hook
events, and real-model probes):

| What | Default | With the flag set |
| --- | --- | --- |
| `~/.claude/CLAUDE.md` and its `@imports` | loaded as user memory | not loaded |
| `CLAUDE.local.md`, `.claude/settings.local.json` | loaded | not loaded |
| User settings (model and hooks checked; env, effort, permissions and the rest come from the same file) | applied | not applied |
| Hooks from user settings and plugins | run | none run |
| Skills in `~/.claude/skills`, claude.ai-synced skills | loaded | only built-in skills |
| Installed plugins with their skills, agents and hooks | loaded | only built-in plugins |
| Custom agents in `~/.claude/agents`, output styles | loaded | not loaded |
| MCP servers: user (dropped by `--setting-sources`), project `.mcp.json` and claude.ai connectors (dropped by `--strict-mcp-config`) | loaded | none |
| Auto memory under `~/.claude/projects/<dir>/memory/` | loaded | off |
| Project `.claude/settings.json` and `CLAUDE.md` | loaded | loaded |
| Login (OAuth in the Keychain) | works | works |

`--setting-sources project` alone is not enough: Claude Code also reads
`CLAUDE.md`, `.claude/CLAUDE.md` and `.claude/rules/` in every directory from
the working directory up to `/` as project memory, so any work directory under
`$HOME` would still load `~/.claude/CLAUDE.md`. `claudeMdExcludes` lists those
files for every directory above the work directory's repository (the nearest
directory with `.git`, or the work directory itself), plus the operator's
`~/.claude/` (and `$CLAUDE_CONFIG_DIR`) memory, so a repository's own
`CLAUDE.md` files load and nothing above it does. `--bare` and a separate
`CLAUDE_CONFIG_DIR` were rejected because both lose the operator's login;
`--safe-mode` also drops the project's `CLAUDE.md`; `--disable-slash-commands`
also drops built-in skills. Not covered: managed (admin policy) settings,
built-in plugins, and the environment variables of the process that runs
`loopx serve`. With user settings gone, the model, the effort and any
environment variables come from LoopX (next section), not from the operator's
Claude Code settings.

### Harness, model and environment

`loopx/agents/agent_settings.py` is the one place that resolves what an agent
inherits. The harness prompt is an agent's character and stability: working
principles, boundaries and communication. The role prompt says what that kind
of agent is responsible for. Keeping them apart means no harness edit can
drop an agent's responsibilities.

| Agent | Harness (first match) | Model (first match) |
| --- | --- | --- |
| Manager | `agent_configs.manager.harness_prompt` → default | its row's `model` → default |
| Planner | the project's `planner_harness_prompt` → default | `planner` row's `model` → default |
| Worker | the project's `worker_harness_prompt` → default | `worker` row's `model` → default |
| Reviewer | the project's `worker_harness_prompt` → default | `reviewer` row's `model` → default |

The defaults live in `system_settings` (one row per key; no row means the
built-in value): the default harness (`DEFAULT_HARNESS_PROMPT`, public and
generic), the default model (`opus`), an optional effort (`--effort`: `low`,
`medium`, `high`, `xhigh` or `max`; none passes no flag) and up to 50
environment variables. Every launch passes `--model` and, when set,
`--effort`. The variables are added on top of the server's environment for
every agent's `claude` process; names must match `[A-Z_][A-Z0-9_]*`, and a
name containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `CREDENTIAL` is
refused because the values are shown on the dashboard. That word check guards
against pasting a secret by habit; it is not a security boundary. An
unreadable table falls back to the built-in values, and the run's record says
so. Changes apply to the next Worker or Reviewer run; a resident agent's
restart signature is its command (which carries the model and effort), the
prompt's sha256 and the environment's sha256, so the next turn restarts it
with `--resume`.

The composed system prompt goes to Claude Code as a file:
`SystemPrompt.write_file` writes it once to
`<runtime_root>/.local/prompts/<sha256>.md` (through a temporary file; an
existing file is reused, one whose content no longer matches its name is
replaced), and `system_prompt_args` passes it with
`--append-system-prompt-file`. `--system-prompt-snapshot off` is needed for
the resident agents: by default Claude Code 2.1.285 keeps the system prompt of
a session's first request and sends it again on every `--resume`, so a
restarted Planner or Manager would keep running with its old prompt. A
resident agent's restart signature is its command, the prompt's sha256 and the
sha256 of the Settings environment.

Every run records the prompt in `llm_records.metadata`:

- `system_prompt`: the text, as before;
- `system_prompt_sha256`: the hash of that text, which is also the file name;
- `prompt_layers`: where each layer came from, built in
  `loopx/agents/system_prompt.py`:
  - `harness`: `{source, sha256, project_id?}`; `source` is
    `builtin_default`, `system_default`, `manager_override` or
    `project_override` (which names the project);
  - `role`: `{config_id, sha256, recommended}`, the agent_configs row
    (`manager`, `planner`, `worker` or `reviewer`) and whether its prompt
    equals the recommended `default_prompt`;
  - `project`: `{id, background_sha256, updated_at}` or `null`
    (`background_sha256` is `null` without a background);
  - `task`: `{kind: plan|work|review, task_id, step_id, sha256}` or `null`
    (the Manager);
  - `contract`: `{name, sha256}` (`REPLY_CONTRACT`, `WORK_CONTRACT`,
    `REVIEW_CONTRACT` or `TASK_PLACEMENT_RULE`);
  - `isolation`: `ISOLATION_ID`.
- `model_choice`: `{model, source, effort}`, the model asked for (`source` is
  `builtin_default`, `system_default` or `agent_override`); the record's
  `model` column keeps the model Claude reports;
- `env`: `{sha256, keys: {NAME: sha256 of the value}}`, only when Settings
  holds variables; values are never recorded.

The same inputs give the same prompt and hash; changing the harness, the role
prompt, the background, the task or a contract changes it.

## Message Bus

All inter-agent communication uses typed `MessageEnvelope` over an in-process
async message bus (`loopx/agents/bus.py`). Every message is journaled before
delivery and marked delivered once consumed. The WorkerPool consumes
`WORK_ITEM` and `VERIFY_REQUEST` through topic consumers that take delivery.
On restart the bus replays undelivered messages for the Manager, the TaskRouter
and channels. It drops messages for Planners and Workers: those exist per task,
the server stops their tasks at startup, and replaying would re-run their work.
The journal is then compacted.

Key message types:

| Type | Direction | Purpose |
|---|---|---|
| `USER_MESSAGE` | Channel → Manager, or Web Chat → a task's Planner | User chat message |
| `AGENT_REPLY` | Manager or Planner → Channel | Reply to the user |
| `TASK_REQUEST` | Manager → TaskRouter → Planner | Create/dispatch task |
| `TASK_ACCEPTED` | Planner → TaskRouter → Manager | Plan made, with its step count |
| `TASK_CANCEL` | HTTP → TaskRouter → Planner | Terminate task permanently |
| `TASK_STOP` | HTTP → TaskRouter | Pause task (preserves state) |
| `TASK_RESTART` | HTTP → TaskRouter | Resume stopped/failed task |
| `GOAL_STATUS` | Planner → TaskRouter → Manager | Report progress/completion |
| `WORK_ITEM` | Planner → Worker | Dispatch step |
| `WORK_RESULT` | Worker → Planner | Step result |
| `VERIFY_REQUEST` | Planner → Reviewer | Review a step's result |
| `VERIFY_RESULT` | Reviewer → Planner | Review verdict (`passed`, findings) |
| `WORKER_INPUT` | Planner → running Worker | Forwarded user text as the Worker's next user turn |
| `WORKER_INPUT_RESULT` | WorkerPool → Planner | Whether that text was delivered |

## How a task runs

1. The Manager sends `TASK_REQUEST` to the TaskRouter, which records the task,
   creates its Planner (or queues the task at `MAX_CONCURRENT_TASKS`) and
   forwards the request.
2. Plan turn: the Planner creates the task's directory if needed, starts its
   Claude and asks `[PLAN]`. The steps go to `task_steps`, the activity gets
   `Plan created: ...`, and `TASK_ACCEPTED` reports the step count to the
   Manager.
3. Dispatch: every step whose `depends_on` steps are all verified goes as
   `WORK_ITEM` to a new Worker, up to 4 running at once per task. Steps
   without a dependency between them may run at the same time in the same
   directory.
4. Decision turn: every `WORK_RESULT` and `VERIFY_RESULT` gets a `[DECISION]`
   turn whose message lists the decisions allowed now:
   - `accept` only when the result can be accepted: the Worker completed and
     the step needs no review, or the Reviewer passed. The step is verified,
     and its `summary` becomes the task's result unless a later accept
     replaces it.
   - `review` only after a successful Worker result. A new Reviewer gets
     `VERIFY_REQUEST` with the step's instruction, the Worker's result and the
     acceptance criteria. A step with `review` set in the plan, or sent to
     review once, needs a passing review before it can be accepted, also after
     a retry.
   - `retry` sends the same step to a new Worker with the feedback appended to
     its instruction; at most 2 retries per step.
   - `replan` replaces every step that has not started; started steps cannot
     change. The current step counts as verified if its result can be
     accepted, and is dropped (`skipped`) otherwise.
   - `fail` fails the task with the given reason.
5. Chat: a message to the Planner gets a `[CHAT]` turn. Claude replies, and
   may `forward` text to a running step's Worker (`WORKER_INPUT`, confirmed by
   `WORKER_INPUT_RESULT` within 5 s) or `add_steps`, which are dispatched once
   ready. Text that arrives after its Worker finished is kept and shown to
   Claude in that step's decision turn.
6. Completion: when every step is verified or dropped, the Planner sends
   `GOAL_STATUS` `completed` with the last accepted step's summary (or a
   generated per-step summary). The TaskRouter writes it to
   `tasks.result_summary` and forwards it to the Manager, which tells the
   origin channel; it then stops the Planner, which ends its Claude, and
   starts a queued task.
7. Failure: a `fail` decision, a step that fails by its own verdict after a
   decision turn failed, or planning that cannot finish (see the Planner's
   failures above) sends `GOAL_STATUS` `failed` with the Planner's failure
   summary, which the TaskRouter writes to `tasks.error`; the Planner is
   stopped the same way. Starting a stopped or failed task again plans it
   anew in a new Planner session.

## Task Lifecycle

```
pending → assigned → running → completed
                        ↓           ↑
                     stopped ───────┘  (start)
                        ↑
                     failed  ←── running
                        ↓
                     (start) → pending → ...

cancelled ← running  (cancel = permanent termination)
```

**Status values**: `pending`, `assigned`, `running`, `executing`, `completed`, `failed`, `cancelled`, `stopped`

**Phase values** (finer-grained, set by planner): `pending`, `planning`, `executing`, `verifying`, `completed`, `failed`

## Data Persistence

PostgreSQL is a hard startup dependency. All data paths:

| Table | Purpose |
|---|---|
| `projects` | Projects: name (unique ignoring case), description, default `work_dir` (`''` = none), `background` for the task agents' system prompt (`''` = none), `planner_harness_prompt` and `worker_harness_prompt` (`''` = the default harness) |
| `tasks` | Task metadata, status, phase, timestamps, `work_dir`, `project_id` (required) |
| `task_steps` | Planner decomposition steps per task |
| `activity` | Execution trace events (SSE streaming to frontend) |
| `chat_messages` | Chat conversation history |
| `llm_records` | One row per Claude run: input and output text, status, cost; `metadata` holds the exact argv (`command`), `cwd`, the appended `system_prompt`, its `system_prompt_sha256`, its `prompt_layers`, the `model_choice` and the Settings `env` names |
| `agent_configs` | Agent configuration (role prompt, model, permissions) for `manager`, `planner`, `worker` and `reviewer`; `harness_prompt` is used for the Manager only |
| `system_settings` | Settings shared by the whole system, one value per key: the default harness, model, effort and environment variables (no row = the built-in default) |
| `llm_profiles` | LLM provider profiles (model, endpoint, API key env) |
| `channel_configs` | Telegram/Lark settings (enabled flag, env var names, allowlist) |

Every task belongs to one project. On the first start after projects were
added, the one-time migration `tasks_project_backfill` creates one project per
distinct task directory and assigns those tasks to it. The project is named
after the directory's last path component (`''` and `/` become `default`);
the oldest directory keeps the plain name and a later clash, ignoring case,
gets `-2`, `-3`, .... A task's "active" statuses are `pending`, `assigned`,
`running` and `executing`.

Connection: `LOOPX_DATABASE_URL` environment variable or `.env` file.

## Web Dashboard

Single-page React app served from `web/dist/` via the built-in HTTP server
(`loopx/agents/http_server.py`). Default port: 8767. An installed wheel has no
`web/`; it serves the copy staged into the package (`loopx/web/dashboard`,
built by `scripts/stage-dashboard.sh` at release time), and it carries
`loopx/db/schema.sql` the same way.

### Pages

- **Chat**: Every agent and its messages, finished ones included. The selector lists the global Manager and the 30 most recent tasks; each task shows its one Planner and the Worker (and Reviewer, when reviewed) of each step. You can send to the Manager and to a running task's Planner, which keeps one thread per task; SSE streaming. A selected Planner also shows its Claude runs, one per turn, in a folded list of the latest 10 beside its activity and chat thread, re-read as a reply starts and ends and every 10 s while the task runs. A Worker or Reviewer shows its Claude runs read-only, following a running one live, and points to its task's Planner for sending.
- **Tasks**: Task list with project and status filters, time display, Start/Stop/Delete controls. `#tasks?project=<id>` filters by a project and shows its name, description, default directory and background (three lines, then Show more; "No background" when empty), with Edit, and Delete while it has no tasks. Edit has a monospace background field with a live `n / 10000` counter and a collapsible "Harness overrides" section with the Planner and Worker harness (each `n / 20000`, or "Inherits the default" when empty); Save is disabled over a limit. The bar lists the harness overrides a project has. A new task picks an existing project or creates one in the same form; its work directory may stay empty when the project has a default. A task's detail view can move it to another project in any status.
- **Projects (sidebar)**: Below the navigation, every project with its task count (or a spinner with its active count); clicking one opens the Tasks page filtered by it, and `+` creates a project (name, default directory, description, optional background and harness overrides).
- **Task Detail**: Overview (steps, agent timeline, live activity feed), Pipeline DAG (React Flow), Sessions, Conversation
- **Agents**: The Manager, Planner, Worker and Reviewer configuration editor (role prompt replace/append, model, LLM profiles, permissions). The model field's placeholder is the default it inherits. The Manager also has a "Harness prompt (override)" field; the others say where their harness comes from. A Runs tab lists every recorded Claude run of the role (the Manager, every task's Planner, Workers or Reviewers)
- **Agent history** (`#history/<agent-id>?task=<task-id>`): opened from History on an Agent Timeline row, a Runs entry or the Chat page. For each Claude run it shows status, model, tokens, cost, the launch command (cwd and argv; a long argument such as the system prompt is shown by its length, and Copy copies the exact command), a collapsible System prompt section with what LoopX appended (Claude Code's built-in system prompt is not included): its title shows the prompt's size, its short sha256 and an `isolated` badge, and inside one line per layer (harness source and version, role recommended or edited, project and background version, task, contract), the model with its source and effort, and the Settings environment variable names, above the full text; runs recorded before the layers existed show the text only. Then the full transcript, following a running run every 2 s. A running run shows its last output time from the record's heartbeat and is flagged after 2 minutes with no output. While Claude thinks it shows `thinking · ~N tokens` from Claude's `thinking_tokens` events. Those arrive about once a second, so the recorder keeps one row per thinking phase and updates its count in place. Hooks, background tasks and rate limits are shown as one line each, and any other event type as an expandable line. For a Planner each run is one of its turns, listed above your chat with it and its activity.
- **Settings**: Agent defaults (the default harness prompt with an `n / 20000` counter and Reset to built-in, the default model, the effort and the environment variables), then the Telegram and Lark channels

### Real-Time Updates

- **Activity SSE**: `GET /api/tasks/:id/activity/stream` — server-sent events from Worker subprocess
- **Task polling**: 2-second interval with auto-stop on terminal status
- **Chat SSE**: `GET /api/chat/events` — real-time chat responses

### Hash Routing

SPA uses `#page/sub` format for deep linking that survives browser refresh:
- `#tasks/task-id` → task detail
- `#agents/agent-id` → agent config
- `#agents/agent-id/sessions` → the role's runs
- `#history/agent-id?task=task-id` → one agent's history

## HTTP API

The API answers only the dashboard and local clients: Workers run Claude with
every permission, so a web page in your browser must not be able to drive it.
A request whose `Origin` is not the server's own is refused with 403 and no
response carries CORS headers. While the server listens on a loopback address,
the `Host` must name a loopback address too, which defeats DNS rebinding.
Clients without an `Origin` (curl, the CLI) are unaffected, and so is the Vite
dev proxy, which keeps `Host` and `Origin` on its own port.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/status` | GET | System health |
| `/api/projects` | GET | Projects ordered by name ignoring case, each with `task_count` and `active_count` |
| `/api/projects` | POST | Create a project from `name` (trimmed, 1–80 characters), optional `description`, `work_dir` (absolute), `background` (stored without the blank lines around it; 400 over 10000 characters), `planner_harness_prompt` and `worker_harness_prompt` (400 over 20000 characters); 409 when the name exists ignoring case |
| `/api/projects/:id` | PUT | Update any of `name`, `description`, `work_dir`, `background`, `planner_harness_prompt`, `worker_harness_prompt` (`""` clears it); 400, 404, 409 |
| `/api/projects/:id` | DELETE | Delete a project; 409 while it still has tasks |
| `/api/tasks` | GET | List the newest tasks; `?project_id=` lists one project's. Each has `project_id`, `project_name` and `work_dir` |
| `/api/tasks/create` | POST | Create a task in `init` with exactly one of `project_id` (existing) and `project` (`{name, description?, work_dir?, background?, planner_harness_prompt?, worker_harness_prompt?}`, created in the same transaction). Its `work_dir` is the one given, else the project's; 400 with neither, 404 for an unknown project, 409 for a taken new name |
| `/api/tasks/:id/project` | PUT | Move a task to `{project_id}`, whatever its status |
| `/api/tasks/:id` | GET | Task detail with steps, `project_id` and `project_name` |
| `/api/tasks/cancel` | POST | Cancel task (permanent) |
| `/api/tasks/stop` | POST | Stop/pause task |
| `/api/tasks/start` | POST | Start/resume task |
| `/api/tasks/delete` | POST | Delete task (must be stopped first) |
| `/api/tasks/:id/activity` | GET | Activity events |
| `/api/tasks/:id/activity/stream` | GET | SSE activity stream |
| `/api/chat/send` | POST | Send chat message; `target` is the Manager (default) or `{kind: planner, task_id}`, 409 when that task has no running Planner |
| `/api/chat/targets` | GET | The Manager, then per recent task its Planner and step Workers/Reviewers, with `can_send` |
| `/api/chat/messages` | GET/POST | Chat history |
| `/api/agent-configs` | GET | List agents |
| `/api/agent-configs/:id` | GET/PUT | Agent config CRUD; `harness_prompt` only for `manager` (400 for the others, and over 20000 characters); `model` must be a model name (`""` or null clears it) |
| `/api/agent-configs/:id/reset-prompt` | POST | Reset the role prompt to the recommended one |
| `/api/settings/agents` | GET/PUT | The defaults every agent inherits: `default_harness_prompt`, `default_model`, `default_effort`, `default_env` (`[{key, value}]`). PUT changes the fields given; an empty value goes back to the built-in default. 400 for an unknown field, a harness over 20000 characters, a bad model name or effort, or a bad, credential-like or 51st variable; nothing is stored then |
| `/api/settings/agents/reset-harness` | POST | Go back to the built-in default harness |
| `/api/llm-profiles` | GET/POST | LLM profiles |
| `/api/llm-records` | GET | Claude runs; filter by `agent_id`, `task_id` or `role` (`manager`, `planner`, `worker`, `reviewer`) |
| `/api/llm-records/:id` | GET | One run (status, usage, cost, launch command, heartbeat) |
| `/api/llm-records/:id/events?after=N` | GET | The run's stream-json events after seq N |
| `/api/channel-configs` | GET | List channels |
| `/api/channel-configs/:id` | GET/PUT | Channel config |

## Configuration

Agent behavior is configured via the `agent_configs` table (editable in the dashboard):

- **system_prompt**: The role prompt: what this kind of agent is responsible for
- **harness_prompt**: The Manager's own harness (`''` = the default in Settings)
- **runtime**: stored per agent; only `claude-code` is implemented, and the
  Manager, Planners and Workers always run the Claude CLI
- **model**: This agent's model (`--model`); empty uses the default model in Settings (`opus` built in)
- **permission_mode**: `bypassPermissions` (default for workers)
- **max_turns**: Maximum conversation turns (0 = unlimited)
- **timeout_seconds**: Execution timeout. For workers it limits one Claude
  run's total time (default 86400, 0 = no limit). Workers read
  `timeout_seconds` and `max_turns` for every run, so an edit applies to the
  next step
- **max_concurrency**: Worker pool size

The `planner` row configures every task's Planner and is read for every turn.
`system_prompt` is the editable role prompt; code appends the reply contract
(`REPLY_CONTRACT`). `timeout_seconds` bounds each turn (default 1200, 0 = no
limit): a turn over it fails and its Claude is killed. `max_turns` above 0
passes `--max-turns`, as for Workers, and `model`, when set, passes
`--model`. A change that alters the Planner's command restarts its Claude at
the next turn with `--resume`. `permission_mode` does not apply: the Planner's
tools are fixed to Read, Grep and Glob.

The `worker` row configures every Worker and the `reviewer` row every
Reviewer; each is read for every run. `system_prompt` is the role prompt; code
puts the harness before it and the project section, the task and the contract
after it.

The recommended role prompts hold responsibilities only; the working
principles, boundaries and tone they used to repeat are in the default
harness. At startup every row whose role prompt still equals its recommended
one (`default_prompt`) gets the new recommendation, an edited prompt is kept,
and a missing row (such as `reviewer`) is created. Rows still holding an older
seeded default from before the recommendation was tracked get the current one.

## Channels

The Web channel is always on. Telegram and Lark are built in; `loopx serve`
takes no flags for them. At startup it reads `channel_configs`, which the
Settings page edits, and starts each enabled channel. Changes apply after a
restart.

- **Telegram**: the token comes from the env var named by `bot_token_env`
  (default `LOOPX_TELEGRAM_BOT_TOKEN`). `allowed_user_ids` limits who can talk
  to the bot; if it is empty, `LOOPX_TELEGRAM_ALLOWED_USERS` applies, and when
  both are empty everyone is allowed. A malformed list keeps Telegram off.
- **Lark**: needs `lark-cli` on PATH and signed in. The app ID comes from the
  env var named by `app_id_env` (default `LARK_APP_ID`).
- A channel that fails to start is logged and skipped; the server keeps
  running.
