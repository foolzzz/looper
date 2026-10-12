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
- Claude CLI (`claude`) on PATH

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
loopx serve
# Dashboard at http://127.0.0.1:8767
```

The schema is applied automatically on first startup.

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
| **Chat** | Real-time conversation with Manager, SSE streaming |
| **Tasks** | Task list, status filters, create/start/stop/delete controls |
| **Task Detail** | Config editor (init status), overview, pipeline DAG, sessions, conversation |
| **Agents** | Agent config editor, session viewer with LLM traffic |
| **Settings** | Lark & Telegram channel configuration |

### HTTP API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/status` | GET | System health |
| `/api/tasks` | GET | List tasks |
| `/api/tasks/create` | POST | Create task (init status) |
| `/api/tasks/:id` | GET | Task detail with steps |
| `/api/tasks/:id/config` | PUT | Update task config (init only) |
| `/api/tasks/start` | POST | Start/resume task |
| `/api/tasks/stop` | POST | Pause task |
| `/api/tasks/cancel` | POST | Cancel permanently |
| `/api/tasks/delete` | POST | Delete (must be stopped) |
| `/api/tasks/:id/activity/stream` | GET | SSE activity stream |
| `/api/chat/send` | POST | Send chat message |
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
| `ANTHROPIC_API_KEY` | Yes | For Claude CLI workers |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot integration |
| `LARK_APP_ID` | No | Lark bot app ID |
| `LARK_APP_SECRET` | No | Lark bot app secret |

Store secrets in `.env` (gitignored), never in source. The `channel_configs`
table stores environment variable **names**, not actual secret values.

### Agent Configuration

Agent behavior is configurable from the dashboard:

- **system_prompt**: Instruction prompt for the agent
- **model**: Model override (or inherit from LLM profile)
- **permission_mode**: `bypassPermissions` for autonomous workers
- **max_turns**: Conversation turn limit (0 = unlimited)
- **timeout_seconds**: Execution timeout per task
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

## License

Apache License 2.0. See [LICENSE](LICENSE).
