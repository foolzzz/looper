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

### WorkerPool (`loopx/agents/tiers/worker.py`)

- Manages a pool of Worker processes
- Each Worker runs Claude CLI as a subprocess with `stream-json` output
- Real-time event streaming (tool_use, text) captured to activity table
- Command: `claude --print --verbose --output-format stream-json --dangerously-skip-permissions --max-turns 50`

## Message Bus

All inter-agent communication uses typed `MessageEnvelope` over an in-process
async message bus (`loopx/agents/bus.py`). Key message types:

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

Connection: `LOOPX_DATABASE_URL` environment variable or `.env` file.

## Web Dashboard

Single-page React app served from `web/dist/` via the built-in HTTP server
(`loopx/agents/http_server.py`). Default port: 8767.

### Pages

- **Chat**: Real-time conversation with Manager agent, agent session selector, SSE streaming
- **Tasks**: Task list with status filter, time display, Start/Stop/Delete controls
- **Task Detail**: Overview (steps, agent timeline, live activity feed), Pipeline DAG (React Flow), Sessions, Conversation
- **Agents**: Agent configuration editor (system prompt replace/append, LLM profiles, permissions), session viewer with full LLM traffic

### Real-Time Updates

- **Activity SSE**: `GET /api/tasks/:id/activity/stream` — server-sent events from Worker subprocess
- **Task polling**: 2-second interval with auto-stop on terminal status
- **Chat SSE**: `GET /api/chat/events` — real-time chat responses

### Hash Routing

SPA uses `#page/sub` format for deep linking that survives browser refresh:
- `#tasks/task-id` → task detail
- `#agents/agent-id` → agent config
- `#agents/agent-id/sessions` → agent sessions

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
| `/api/chat/send` | POST | Send chat message |
| `/api/chat/messages` | GET/POST | Chat history |
| `/api/agent-configs` | GET | List agents |
| `/api/agent-configs/:id` | GET/PUT | Agent config CRUD |
| `/api/agents/:id/sessions` | GET | Agent sessions |
| `/api/llm-profiles` | GET/POST | LLM profiles |
| `/api/llm-records` | GET | LLM traffic records |

## Configuration

Agent behavior is configured via the `agent_configs` table (editable in the dashboard):

- **system_prompt**: Agent instruction prompt
- **runtime**: `claude-code` or `codex-cli`
- **model**: Model override (or use LLM profile)
- **permission_mode**: `bypassPermissions` (default for workers)
- **max_turns**: Maximum conversation turns (0 = unlimited)
- **timeout_seconds**: Execution timeout
- **max_concurrency**: Worker pool size
