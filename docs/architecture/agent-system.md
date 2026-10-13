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
- Runs one resident Claude process, started on the first chat message:
  `claude --print --verbose --output-format stream-json --input-format stream-json --include-partial-messages --append-system-prompt <prompt> [--resume <session_id>]`.
  Each chat message is one user turn on its stdin and ends at that turn's
  `result`; messages are handled one at a time, and each turn is one Runs
  record. Claude's `total_cost_usd` is its process's running total, so a
  record's cost is the increase since the previous turn. When Claude exits (or dies mid-turn, which answers that message with
  an error) or the Manager's system prompt changes, the next message starts a
  new process that resumes the saved session with `--resume`. Stopping the
  Manager closes Claude's stdin and kills it if it is still running after 5 s

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

- Per-task lifecycle management with concurrency control (`MAX_CONCURRENT_TASKS=3`)
- Creates/destroys PlannerAgent instances per task
- Handles task lifecycle: start, stop (pause), restart (resume), cancel
- Queues tasks when at capacity, auto-dequeues on completion
- Orphan detection on server startup (stuck tasks → "stopped")

### Planner (`loopx/agents/tiers/planner.py`)

- Decomposes task requirements into executable steps (`PlanStep`)
- Dispatches steps to Worker agents
- Monitors step completion and runs verification
- Reports task status back to TaskRouter
- Takes chat messages from the Chat page. The Planner has no LLM of its own.
  While a step's Worker is running, the message is passed to that Worker as its
  next user turn (`WORKER_INPUT`, confirmed by `WORKER_INPUT_RESULT`). Otherwise,
  or if the Worker has just finished, it becomes a new step after the steps in
  progress.

### WorkerPool (`loopx/agents/tiers/worker.py`)

- Manages a pool of Worker processes
- Each Worker runs Claude CLI as a subprocess with `stream-json` output
- Real-time event streaming (tool_use, text) captured to activity table
- Command: `claude --print --verbose --output-format stream-json --include-partial-messages --input-format stream-json --replay-user-messages --dangerously-skip-permissions`,
  plus `--max-turns N` when the `worker` config sets `max_turns` above 0. The
  prompt is the first user turn on stdin; stdin stays open so the Planner can
  add user turns mid-run, and closes when the result arrives
- Claude is stopped when a run takes longer than the `worker` config's
  `timeout_seconds` in total (default 86400, 0 = no limit). The step fails
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
| `TASK_REQUEST` | Manager → TaskRouter → Planner | Create/dispatch task |
| `TASK_CANCEL` | HTTP → TaskRouter → Planner | Terminate task permanently |
| `TASK_STOP` | HTTP → TaskRouter | Pause task (preserves state) |
| `TASK_RESTART` | HTTP → TaskRouter | Resume stopped/failed task |
| `GOAL_STATUS` | Planner → TaskRouter → Manager | Report progress/completion |
| `WORK_ITEM` | Planner → Worker | Dispatch step |
| `WORK_RESULT` | Worker → Planner | Step result |

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
| `projects` | Projects: name (unique ignoring case), description, default `work_dir` (`''` = none) |
| `tasks` | Task metadata, status, phase, timestamps, `work_dir`, `project_id` (required) |
| `task_steps` | Planner decomposition steps per task |
| `activity` | Execution trace events (SSE streaming to frontend) |
| `chat_messages` | Chat conversation history |
| `llm_records` | Full LLM traffic recording (input/output text) |
| `agent_configs` | Agent configuration (system prompt, model, permissions) |
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
(`loopx/agents/http_server.py`). Default port: 8767.

### Pages

- **Chat**: Every agent and its messages, finished ones included. The selector lists the global Manager and the 30 most recent tasks; each task shows its one Planner and the Worker (and Reviewer, when verified) of each step. You can send to the Manager and to a running task's Planner, which keeps one thread per task; SSE streaming. A Worker or Reviewer shows its Claude runs read-only, following a running one live, and points to its task's Planner for sending.
- **Tasks**: Task list with project and status filters, time display, Start/Stop/Delete controls. `#tasks?project=<id>` filters by a project and shows its name, description and default directory, with Edit, and Delete while it has no tasks. A new task picks an existing project or creates one in the same form; its work directory may stay empty when the project has a default. A task's detail view can move it to another project in any status.
- **Projects (sidebar)**: Below the navigation, every project with its task count (or a spinner with its active count); clicking one opens the Tasks page filtered by it, and `+` creates a project.
- **Task Detail**: Overview (steps, agent timeline, live activity feed), Pipeline DAG (React Flow), Sessions, Conversation
- **Agents**: Agent configuration editor (system prompt replace/append, LLM profiles, permissions); a Runs tab lists every recorded Claude run of the role (Manager, or Workers and reviewers)
- **Agent history** (`#history/<agent-id>?task=<task-id>`): opened from History on an Agent Timeline row, a Runs entry or the Chat page. For each Claude run it shows status, model, tokens, cost, the launch command (cwd and argv, copyable) and the full transcript, following a running run every 2 s. A running run shows its last output time from the record's heartbeat and is flagged after 2 minutes with no output. While Claude thinks it shows `thinking · ~N tokens` from Claude's `thinking_tokens` events. Those arrive about once a second, so the recorder keeps one row per thinking phase and updates its count in place. Hooks, background tasks and rate limits are shown as one line each, and any other event type as an expandable line. A Planner, which runs no LLM, shows its activity and your chat with it.
- **Settings**: Enable and configure the Telegram and Lark channels

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

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/status` | GET | System health |
| `/api/projects` | GET | Projects ordered by name ignoring case, each with `task_count` and `active_count` |
| `/api/projects` | POST | Create a project from `name` (trimmed, 1–80 characters), optional `description` and `work_dir` (absolute); 409 when the name exists ignoring case |
| `/api/projects/:id` | PUT | Update any of `name`, `description`, `work_dir` (`""` clears the default directory); 404, 409 |
| `/api/projects/:id` | DELETE | Delete a project; 409 while it still has tasks |
| `/api/tasks` | GET | List the newest tasks; `?project_id=` lists one project's. Each has `project_id`, `project_name` and `work_dir` |
| `/api/tasks/create` | POST | Create a task in `init` with exactly one of `project_id` (existing) and `project` (`{name, description?, work_dir?}`, created in the same transaction). Its `work_dir` is the one given, else the project's; 400 with neither, 404 for an unknown project, 409 for a taken new name |
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
| `/api/agent-configs/:id` | GET/PUT | Agent config CRUD |
| `/api/llm-profiles` | GET/POST | LLM profiles |
| `/api/llm-records` | GET | Claude runs; filter by `agent_id`, `task_id` or `role` (`manager`, `worker`) |
| `/api/llm-records/:id` | GET | One run (status, usage, cost, launch command, heartbeat) |
| `/api/llm-records/:id/events?after=N` | GET | The run's stream-json events after seq N |
| `/api/channel-configs` | GET | List channels |
| `/api/channel-configs/:id` | GET/PUT | Channel config |

## Configuration

Agent behavior is configured via the `agent_configs` table (editable in the dashboard):

- **system_prompt**: Agent instruction prompt
- **runtime**: stored per agent; only `claude-code` is implemented, and the
  Manager and Workers always run the Claude CLI
- **model**: Model override (or use LLM profile)
- **permission_mode**: `bypassPermissions` (default for workers)
- **max_turns**: Maximum conversation turns (0 = unlimited)
- **timeout_seconds**: Execution timeout. For workers it limits one Claude
  run's total time (default 86400, 0 = no limit). Workers read
  `timeout_seconds` and `max_turns` for every run, so an edit applies to the
  next step
- **max_concurrency**: Worker pool size

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
