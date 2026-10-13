<div align="center">

<h1>LoopX</h1>

**Three-tier multi-agent orchestration system with a real-time web dashboard.**

Deploy autonomous coding agents that plan, decompose, and execute complex tasks — with full observability.

</div>

---

## What is LoopX

LoopX is a self-hosted multi-agent system that turns a single task description
into a coordinated plan–execute–verify loop. A **Manager** agent talks to users,
a **TaskRouter** manages lifecycle and concurrency, a **Planner** decomposes work
into steps, and a **WorkerPool** runs Claude CLI subprocesses to do the actual
coding.

Everything is observable through a built-in web dashboard with real-time
streaming, task lifecycle controls, and agent configuration.

```
User ─→ Chat / Lark / Telegram
          │
       Manager          (understands intent, routes tasks)
          │
       TaskRouter       (lifecycle, concurrency, queuing)
          │
       Planner          (decomposes into steps, verifies results)
          │
       WorkerPool       (Claude CLI subprocesses, stream-json)
```

### Key Features

- **Task lifecycle**: `init → pending → assigned → running → completed/failed/stopped/cancelled`
- **Direct task creation**: Create tasks from the dashboard, configure before starting
- **Pause & resume**: Stop running tasks, restart from stopped/failed state
- **Real-time activity streaming**: SSE-based live event feed from worker processes
- **Agent configuration**: Edit system prompts, models, permissions from the dashboard
- **Multi-channel input**: Web chat, Lark bot, Telegram bot
- **LLM profile management**: Configure multiple model providers and endpoints
- **Pipeline visualization**: React Flow DAG showing task decomposition and worker assignment

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL (container `agentbox-postgres` on port 54329, or any PG instance)
- Node.js 22+ (for building the dashboard)
- Claude CLI (`claude`) on PATH and signed in
- Optional, for the Lark channel: `lark-cli` on PATH and signed in

### 1. Database Setup

```bash
# Start PostgreSQL (if using Docker)
docker run -d --name agentbox-postgres \
  -e POSTGRES_DB=loopx \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -p 54329:5432 \
  postgres:16

# Set connection string
export LOOPX_DATABASE_URL="postgresql://postgres:postgres@localhost:54329/loopx"
# Or put it in .env (gitignored)
```

### 2. Install & Build

```bash
# Install Python dependencies
uv sync

# Build the web dashboard
cd web && npm install && npx vite build && cd ..
```

### 3. Run

```bash
uv run loopx serve
# Dashboard at http://127.0.0.1:8767
```

The schema is applied automatically on first startup. Telegram and Lark need
no flags; turn them on from the Settings page (see [Channels](#channels)).

## Architecture

### Agent Tiers

| Tier | Module | Role |
|---|---|---|
| **Manager** | `loopx/agents/tiers/manager.py` | User-facing steward, intent classification, chat |
| **TaskRouter** | `loopx/agents/tiers/task.py` | Task lifecycle, concurrency control (3 parallel), queue |
| **Planner** | `loopx/agents/tiers/planner.py` | Step decomposition, dispatch, verification |
| **WorkerPool** | `loopx/agents/tiers/worker.py` | Claude CLI subprocess pool with stream-json output |

All inter-agent communication flows through a typed async **MessageBus** with
`MessageEnvelope` routing.

### Database

PostgreSQL is a hard startup dependency. Tables:

| Table | Purpose |
|---|---|
| `tasks` | Task metadata, status, phase, work_dir, config, timestamps |
| `task_steps` | Planner decomposition steps |
| `activity` | Execution trace events (SSE-streamed to frontend) |
| `chat_messages` | Chat conversation history |
| `llm_records` | Full LLM I/O recording |
| `agent_configs` | Agent configuration (prompt, model, permissions) |
| `llm_profiles` | LLM provider profiles |
| `channel_configs` | Lark/Telegram channel settings |

### Web Dashboard

Single-page React app served from `web/dist/` on port 8767.

| Page | Features |
|---|---|
| **Dashboard** | System status overview |
| **Chat** | Talk to the global Manager, or to a running task's Planner (each message becomes a new step of that task); SSE streaming |
| **Tasks** | Task list, status filters, create/start/stop/delete controls |
| **Task Detail** | Config editor (init status), overview, pipeline DAG, sessions, conversation; each Agent Timeline row has a **History** button |
| **Agents** | Agent config editor; **Runs** lists every Claude run of a role and opens its history |
| **Agent history** | One agent's launch command, full transcript (text, thinking, tool calls and results) and activity; follows a running agent and flags one with no output for 2 minutes |
| **Settings** | Lark & Telegram channel configuration |

### HTTP API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/status` | GET | System health |
| `/api/tasks` | GET | List tasks |
| `/api/tasks/create` | POST | Create task (init status); `work_dir` is required |
| `/api/tasks/:id` | GET | Task detail with steps |
| `/api/tasks/:id/config` | PUT | Update task config (init only) |
| `/api/tasks/start` | POST | Start/resume task |
| `/api/tasks/stop` | POST | Pause task |
| `/api/tasks/cancel` | POST | Cancel permanently |
| `/api/tasks/delete` | POST | Delete (must be stopped) |
| `/api/tasks/:id/activity/stream` | GET | SSE activity stream |
| `/api/chat/send` | POST | Send chat message; `target` is the Manager (default) or `{kind: planner, task_id}` |
| `/api/chat/targets` | GET | Chat targets: the Manager and every running task's Planner |
| `/api/agent-configs` | GET | List agents |
| `/api/agent-configs/:id` | GET/PUT | Agent config |
| `/api/llm-profiles` | GET/POST | LLM profiles |
| `/api/channel-configs` | GET | List channels |
| `/api/channel-configs/:id` | GET/PUT | Channel config |

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `LOOPX_DATABASE_URL` | Yes | PostgreSQL connection string |

The `claude` processes inherit the server's environment, so the Claude CLI
authenticates as it does in your shell: signed in, or with an API key as its
own documentation describes.

Channel credentials are also environment variables; the Settings page decides
which ones (see [Channels](#channels)). Store secrets in `.env` (gitignored,
read from the repository root), never in source.

### Channels

The web chat is always on. Telegram and Lark are built in and need no
command-line flags: enable and configure them on the dashboard **Settings**
page. Settings are saved in the `channel_configs` table and read when the
server starts, so restart `loopx serve` after changing them. The table stores
environment variable **names**, not secret values.

| Channel | Settings fields | Also needed |
|---|---|---|
| Telegram | `bot_token_env`: env var holding the bot token (default `LOOPX_TELEGRAM_BOT_TOKEN`). `allowed_user_ids`: comma-separated Telegram user IDs. | The token in that env var. |
| Lark | `app_id_env`: env var holding the app ID (default `LARK_APP_ID`). | `lark-cli` on PATH and signed in; it receives and sends the messages. |

- If `allowed_user_ids` is empty, `LOOPX_TELEGRAM_ALLOWED_USERS` applies;
  when both are empty, everyone can talk to the bot. A malformed list keeps
  Telegram off.
- `app_secret_env` is not read yet; `lark-cli` holds the Lark credentials.
- A channel that fails to start is logged, and the dashboard keeps running.

### Agent Configuration

Agent behavior is configurable from the dashboard:

- **system_prompt**: Instruction prompt for the agent
- **model**: Model override (or inherit from LLM profile)
- **permission_mode**: `bypassPermissions` for autonomous workers
- **max_turns**: Conversation turn limit (0 = unlimited)
- **timeout_seconds**: Execution timeout per task. For workers it limits one
  Claude run's total time (default 86400, 0 = no limit); output does not
  extend it. Workers apply both limits from the next step on
- **max_concurrency**: Worker pool size

## Task Lifecycle

```
init ──→ pending ──→ assigned ──→ running ──→ completed
  │                                 │
  │ (start)                      (stop)
  │                                 ↓
  └──────────────────────────── stopped ←── failed
                                    │
                                 (start)
                                    ↓
                                 pending → ...

cancelled ← running  (permanent termination)
```

- **init**: Created from UI, user reviews and edits config before starting
- **Work directory**: every task runs in a directory you choose (`work_dir`, an
  absolute path or one starting with `~/`); there is no default. It is required
  when creating a task, a task without one cannot start, and it is created if
  missing. When the Manager creates a task from chat, it takes the directory from
  the conversation and asks you if you have not said one.
- **stopped**: Paused, state preserved, can resume
- **cancelled**: Permanently terminated, cannot resume

## Development

```bash
# Type-check frontend
cd web && npx tsc --noEmit

# Build frontend
npx vite build

# Run server (auto-applies schema)
uv run loopx serve
```

## Releases

Each release's changes are in [CHANGELOG.md](CHANGELOG.md). Maintainers
prepare a release with the checklist in
[docs/product/release-readiness.md](docs/product/release-readiness.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
