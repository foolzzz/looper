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
- Routes task requests to the TaskRouter via `TASK_REQUEST` messages
- Maintains chat conversation context per session

### TaskRouter (`loopx/agents/tiers/task.py`)

- Every task runs in its `work_dir`, the directory the user chose. There is no
  default: `resolve_work_dir` requires an absolute path (or `~/...`), creating
  or starting a task without one is refused, and the Manager asks for it when
  the conversation has not named it. The Worker creates the directory if it is
  missing and runs Claude there.

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
| `tasks` | Task metadata, status, phase, timestamps |
| `task_steps` | Planner decomposition steps per task |
| `activity` | Execution trace events (SSE streaming to frontend) |
| `chat_messages` | Chat conversation history |
| `llm_records` | Full LLM traffic recording (input/output text) |
| `agent_configs` | Agent configuration (system prompt, model, permissions) |
| `llm_profiles` | LLM provider profiles (model, endpoint, API key env) |
| `channel_configs` | Telegram/Lark settings (enabled flag, env var names, allowlist) |

Connection: `LOOPX_DATABASE_URL` environment variable or `.env` file.

## Web Dashboard

Single-page React app served from `web/dist/` via the built-in HTTP server
(`loopx/agents/http_server.py`). Default port: 8767.

### Pages

- **Chat**: Conversation with the global Manager or with a running task's Planner; the selector lists the live Planner instances, and each Planner keeps one thread per task; SSE streaming
- **Tasks**: Task list with status filter, time display, Start/Stop/Delete controls
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
| `/api/tasks` | GET | List all tasks |
| `/api/tasks/:id` | GET | Task detail with steps |
| `/api/tasks/cancel` | POST | Cancel task (permanent) |
| `/api/tasks/stop` | POST | Stop/pause task |
| `/api/tasks/start` | POST | Start/resume task |
| `/api/tasks/delete` | POST | Delete task (must be stopped first) |
| `/api/tasks/:id/activity` | GET | Activity events |
| `/api/tasks/:id/activity/stream` | GET | SSE activity stream |
| `/api/chat/send` | POST | Send chat message; `target` is the Manager (default) or `{kind: planner, task_id}`, 409 when that task has no running Planner |
| `/api/chat/targets` | GET | The Manager and every running task's Planner |
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
