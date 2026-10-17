<div align="center">

<h1>Looper</h1>

**Three-tier multi-agent orchestration system with a real-time web dashboard.**

Deploy autonomous coding agents that plan, decompose, and execute complex tasks — with full observability.

</div>

---

## What is Looper

Looper is a self-hosted multi-agent system that turns a single task description
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
- **Multiple agent runtimes and LLM providers**: An agent is a runtime (such as
  Claude Code or Codex), an LLM provider (the runtime's own login, or an API
  key for an Anthropic- or OpenAI-compatible endpoint, with presets for common
  vendors), a model and a thinking effort; every run records its tokens and,
  when its model has a price, its cost
- **Layered settings**: Set the harness prompt, agent, environment and per-run
  spend cap for the whole system, a role, a project or a role in a
  project, and let a task choose its own agents; the dashboard shows which
  layer each value comes from
- **Capability catalog**: Plugins, skills and MCP servers, imported from this
  machine, uploaded, taken from git or written in Looper, then installed
  globally or in one project for the roles you choose
- **Projects and task memory**: A project holds a default directory, a
  background and knowledge documents that agents read on demand; each task
  keeps a journal of accepted and failed steps, decisions, your instructions
  and learnings, which its Planner reads every turn, across restarts and new
  sessions
- **Configuration export and import**: Move the agent configuration to another
  machine as one file, from the CLI or the Settings page
- **Multi-channel input**: Web chat, Lark bot, Telegram bot
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
export LOOPER_DATABASE_URL="postgresql://postgres:postgres@localhost:54329/loopx"
# Or put it in .env (gitignored)
```

### 2. Install & Build

```bash
# Install Python dependencies
uv sync

# Build the web dashboard
cd web && npm install && npx vite build && cd ..
```

Then run `uv run looper doctor`. It checks PostgreSQL, the Claude CLI and its
login, and the enabled channels, and prints the fix for anything missing.

### 3. Run

```bash
uv run looper serve
# Dashboard at http://127.0.0.1:8767
```

The schema is applied automatically on first startup. Telegram and Lark need
no flags; turn them on from the Settings page (see [Channels](#channels)).

Moving to a new machine: `uv run looper config export looper-config.json` writes
the agent configuration to one file (secrets become placeholders;
`--include-projects` adds projects), and `uv run looper config import
looper-config.json` on the new machine previews it; add `--apply` to write it.
Settings does both too, under "Move to another machine".

### 4. Run as a service

```bash
uv run looper service install     # --port, --runtime-root, --label, --env NAME
uv run looper service status      # installed, running, PID, last exit, log, GET /api/status
uv run looper service restart     # e.g. after pulling new code
uv run looper service uninstall
```

`install` runs `looper serve` in the background: a launchd LaunchAgent on macOS,
a systemd user unit on Linux. It starts at login, starts again after a crash,
and logs to `<runtime root>/logs/serve.log`. The unit runs the `looper` that ran
`install` (in a checkout, `.venv/bin/looper`) in the current directory, so the
checkout's `.env` keeps working. It copies `PATH`, locale and proxy settings,
`CLAUDE_CONFIG_DIR`, and every `LOOPER_*`, `ANTHROPIC_*` and `LARK_*` variable
from your shell (add others with `--env NAME`), so the unit file is private
(mode 0600). Run `install` again to apply changes. On Linux, `loginctl
enable-linger $USER` keeps it running without a login session.

### Backups when upgrading

`looper serve` applies the schema at every start, and an upgrade may drop
columns or tables. When the schema is about to change, serve first writes a
`pg_dump` backup to `~/.looper/backups/` (the runtime root's `backups/`, newest
5 per database kept) and logs how to restore it; a new, empty database needs
none. It uses the `pg_dump` on PATH, or the one `LOOPER_PG_DUMP` names, which
must be at least the server's major version (`looper doctor` checks it; on
macOS, `brew install libpq` and put `$(brew --prefix libpq)/bin` on PATH).
With PostgreSQL in Docker, dump inside the container instead:

```bash
export LOOPER_BACKUP_CMD='docker exec agentbox-postgres pg_dump --format=custom -U {user} {dbname}'
```

Without a usable `pg_dump`, or when the backup fails, serve exits before it
changes anything; `looper serve --skip-backup` migrates without a backup and
logs a warning. To roll back, stop Looper, recreate the database empty, restore
the file with `pg_restore --no-owner --dbname "$LOOPER_DATABASE_URL" <file>`
(in Docker: `docker exec -i agentbox-postgres pg_restore --no-owner -U postgres
-d loopx < <file>`), and run the previous Looper version. `looper service
install` copies `PATH` and the `LOOPER_*` variables, so set these before it.

### Upgrading from LoopX

Looper was called LoopX up to v2.5.0. The upgrade needs no manual step:

- **Command:** `looper`. `loopx` still works for one release: it prints a
  deprecation note, then runs `looper`. `install.sh` first removes an old
  `loopx` package installed by the same tool (your data stays).
- **Data directory:** `~/.looper`. The first `looper serve` or
  `looper service install` moves `~/.loopx` there (one rename; nothing is
  copied or deleted) and leaves `~/.loopx` as a link to it for one release.
  When both exist, Looper uses `~/.looper` and logs a warning.
- **Environment variables:** `LOOPER_*`. A `LOOPX_*` name is still read when
  its `LOOPER_*` name is unset, with a warning; rename them in your shell and
  `.env`.
- **Service:** `com.looper.serve`. `looper service install` stops and removes
  the old `com.loopx.serve` unit first.
- **Configuration files:** export writes `looper-config/v2`; files exported as
  `looper-config/v1` or `loopx-config/v1` still import.
- **Unchanged:** the default database is still named `loopx`
  (`postgresql://localhost:5432/loopx`), and the data in it, such as your own
  harness text, is not rewritten; release tags and history keep their names;
  [NOTICE](NOTICE) keeps the attribution to upstream LoopX. The first start
  takes the usual pre-migration backup, because `schema.sql` changed.

## Architecture

### Agent Tiers

| Tier | Module | Role |
|---|---|---|
| **Manager** | `looper/agents/tiers/manager.py` | User-facing steward, intent classification, chat |
| **TaskRouter** | `looper/agents/tiers/task.py` | Task lifecycle, a limit of running tasks per project (8 by default), queue |
| **Planner** | `looper/agents/tiers/planner.py` | Step decomposition, dispatch, verification |
| **WorkerPool** | `looper/agents/tiers/worker.py` | Claude CLI subprocess pool with stream-json output |
| **Scheduler** | `looper/agents/scheduler.py` | Fires each project's schedules: a full task per firing, started at once; missed fires run once, an overlapping firing is skipped or queued, a run past its timeout is stopped; a gate command decides whether a run is worth a task, a failed run's input goes to the next, a daily budget, and messages with two alerts to Telegram or Lark |

All inter-agent communication flows through a typed async **MessageBus** with
`MessageEnvelope` routing.

### Database

PostgreSQL is a hard startup dependency; `looper serve` applies
`looper/db/schema.sql` at every start. Tables by area:

| Area | Tables | What they hold |
|---|---|---|
| Projects and tasks | `projects`, `tasks`, `task_steps`, `task_plans`, `task_profile_overrides` | Projects with their default directory, background and execution settings; tasks, their Planner's plan and steps, and the Agent Profile a task chose for a role |
| Schedules | `schedules`, `schedule_profile_overrides`, `schedule_runs`, `schedule_notifications` | A project's schedules (time rule and zone, requirement, gate, run policies, budgets, whom to tell, Agent Profiles), each firing's run with its gate output, task, outcome and cost, and every message about them |
| Task memory and knowledge | `task_journal`, `project_documents`, `project_document_drafts` | The journal code writes as a task runs; a project's knowledge documents and the drafts waiting for your review |
| Agents and settings | `agent_runtimes`, `llm_providers`, `llm_provider_secrets`, `agent_profiles`, `agent_setting_layers`, `role_configs`, `agent_sessions` | Runtimes, providers (their API keys kept apart) and Agent Profiles; the settings layers; each role's prompt and limits; the sessions the Manager and Planners resume |
| Capability catalog | `library_items`, `library_files`, `library_mcp_servers`, `library_installs` | Plugins, skills and MCP servers with their files or server configuration, and where each is installed |
| Runs and conversations | `llm_records`, `llm_record_events`, `activity`, `chat_messages` | Every agent run with its usage, cost and stream events; each task's activity feed; chat history |
| System | `system_settings`, `channel_configs`, `schema_migrations`, `schema_fingerprint` | Shared settings, the Telegram and Lark channels, the one-time migrations already applied, and the hash of the schema last applied (which tells serve to back up before a change) |

The main tables are described column by column in
[Data Persistence](docs/architecture/agent-system.md#data-persistence).

### Web Dashboard

Single-page React app served from `web/dist/` on port 8767; an installed
release wheel serves the copy packaged with it.

| Page | Features |
|---|---|
| **Dashboard** | What is happening now: task counts by group (each opening the filtered Tasks page), the agents running now, each active project's concurrency against its limits, the schedules (next fires, failing ones, today's skips), today's spend, and a warning when a component is unhealthy |
| **Chat** | Talk to the global Manager, or to a running task's Planner (a message reaches it as a supplement to the task); with the Manager selected, its conversations by channel (dashboard chat, Lark, Telegram), the Lark and Telegram ones read-only; SSE streaming |
| **Tasks** | Task list, project, status and schedule filters, a Scheduled badge on the tasks a schedule created, create/start/stop/delete controls |
| **Project** | One project's overview, knowledge documents, its own skills and installs, the Agent Profiles and other agent settings its roles override, its schedules (rule, next fire times, enable, Run now, run history) and its settings |
| **Task Detail** | Config editor (init status), overview, pipeline DAG, sessions, conversation; each Agent Timeline row has a **History** button |
| **Runs** | Each role's sessions and run history (a Manager `/new` shows as a new session), each run opening its history; no configuration, a link to the role in Settings → Roles |
| **Schedules** | Every project's schedules, filtered by project and status, each with its switch, Run now and Delete; a schedule's page shows its settings, alerts, today's spend and its run history. New schedules and edits are on the project's Schedules tab |
| **Agent history** | One agent's launch command, full transcript (text, thinking, tool calls and results) and activity; follows a running agent and flags one with no output for 2 minutes |
| **Settings** | A folding group at the bottom of the sidebar, one page each: **LLM Providers**; **Agent Runtimes** (read-only: each runtime type, found on the server's PATH or not, with Re-detect, and the planned ones); **Agent Profiles** (runtime, provider and model, the roles each can run and where it is used); **Roles** (one collapsible row for the shared defaults, then one per role with its overrides, prompt and limits); **Skills & MCP**; **Channels**; **Import / Export** |

### HTTP API

The API answers only the dashboard and local clients: a request from another
site's page (its `Origin`) is refused. The main route groups:

| Group | Endpoints | Purpose |
|---|---|---|
| Status | `GET /api/status`, `GET /api/overview` | System health and the startup self-check (`checks`); the Dashboard's overview of what is happening now |
| Projects | `/api/projects`, `/api/projects/:id`, `/api/projects/:id/knowledge/...` | Create, edit and delete projects; their knowledge documents and drafts |
| Tasks | `/api/tasks`, `/api/tasks/create`, `/api/tasks/:id/...`, `/api/tasks/{start,stop,cancel,delete}` | List and create tasks (in a project, with optional `profile_overrides`), read one, start, stop, cancel, delete; activity and its SSE stream |
| Schedules | `/api/projects/:id/schedules`, `GET /api/schedules`, `/api/schedules/:id/...`, `POST /api/schedules/preview` | Create, edit, enable and delete a project's schedules; list every project's; run one now; its run history and messages; a rule's next fire times |
| Catalog and installs | `/api/catalog/...`, `/api/installs/...`, `/api/projects/:id/installs`, `/api/projects/:id/skills/...` | Plugins, skills and MCP servers: import, upload, git, write, edit, delete; install them globally or in a project |
| Agent settings | `/api/agent-settings/...`, `/api/projects/:id/agent-settings/...`, `/api/roles/...` | The settings layers, each field with its effective value and source; each role's prompt and limits |
| Registry | `GET /api/registry`, `/api/runtimes/:id/probe`, `/api/providers/...`, `/api/agent-profiles/...` | Runtime types and the built-in runtimes (Re-detect probes one); register, edit and delete providers (API keys masked) and Agent Profiles, each with where it is used |
| Configuration transfer | `GET /api/config/export`, `/api/config/imports/...` | Download the configuration file; upload one, preview it, then apply or cancel |
| Chat | `/api/chat/send`, `/api/chat/targets`, `/api/chat/sessions`, `/api/chat/messages`, `/api/chat/events` | Talk to the Manager or a running task's Planner; the Manager's conversations by channel; history and SSE stream |
| Runs | `/api/llm-records/...`, `/api/agents/:id/usage` | Every agent run with its launch command, usage, cost and events; one agent's spending and context |
| Channels | `/api/channel-configs/...`, `/api/channels/lark/...` | Telegram and Lark settings, the Telegram bot token (never returned) and sender allowlists; the Lark bot's status, refused senders and people search |

The [HTTP API reference](docs/architecture/agent-system.md#http-api) describes
the endpoints with their fields and errors.

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `LOOPER_DATABASE_URL` | Yes | PostgreSQL connection string |
| `LOOPER_PUBLIC_URL` | No | The dashboard's address as people open it (for example `https://looper.example.com`), for the links that task-end notices and schedule messages send to Telegram and Lark; unset, the server's own `http://<host>:<port>`. An invalid value stops `looper serve` from starting |
| `LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS` | No | For tests and validation only: the least time between two fires of a schedule (default 300), read at startup |

The `claude` processes inherit the server's environment, so the Claude CLI
authenticates as it does in your shell: signed in, or with an API key as its
own documentation describes.

Channel credentials stay out of the source: the Telegram bot token is entered
in Settings or kept in an environment variable, and Lark's credentials stay in
`lark-cli` (see [Channels](#channels)). Store secrets in `.env` (gitignored,
read from the repository root), never in source.

### Channels

The web chat is always on. Telegram and Lark are built in and need no
command-line flags: enable and configure them on the dashboard **Settings**
page. Settings are saved in the database and read when the server starts, so
restart `looper serve` after changing them. The Telegram bot token is stored
apart from the settings and never returned by the API or exported.

| Channel | Settings fields | Also needed |
|---|---|---|
| Telegram | **Bot token**: the token from BotFather, stored apart and shown only as saved. `bot_token_env`: the env var read when no token is saved (default `LOOPER_TELEGRAM_BOT_TOKEN`). `allowed_user_ids`: the numeric Telegram user IDs that may talk to the bot. | Nothing else, or the token in that env var. |
| Lark (lark-cli) | `profile`: the lark-cli profile, which chooses the app and the bot (empty: lark-cli's active one). `allowed_open_ids`: the senders' Lark `open_id` (`ou_...`) for that app; Settings → Channels finds them by name or email and lists refused senders to allow. Direct messages, and in group chats only messages that @-mention the bot. | `lark-cli` on PATH and signed in; it receives and sends the messages, and the channel checks the bot at startup. |

- Every agent runs Claude Code without permission prompts, so the Manager
  can run any command on this machine. Each channel therefore passes on only
  the senders it lists: anyone else's message is dropped and logged (sender
  id only) with no reply. A channel with an empty or malformed list does not
  start, and the Settings page says why. `LOOPX_TELEGRAM_ALLOWED_USERS` no
  longer applies.
- `lark-cli` holds the Lark credentials; the channel's `profile` (or
  lark-cli's active profile) decides which app it acts as (`lark-cli whoami`).
  Open_ids differ per app, so the allowlist remembers the app it was made for
  and the channel does not start under another one until it is rebuilt.
- While the Manager works on a message from a listed sender, the bot reacts to
  it (👀 in a Telegram private chat, 「Get」 in Lark) and shows one status
  message that it edits in place; the answer and the task's progress come as
  messages of their own.
- In a Lark group, the bot answers only an @-mention from a listed sender,
  replying to that message, and sends the task's progress and its end notice
  to the group, @-mentioning the requester. Other group messages are ignored.
- A task asked for in Telegram or Lark tells that chat when it completes or
  fails, with a link to the task (see `LOOPER_PUBLIC_URL`). Telegram private
  chats and Lark conversations are stored, shown read-only on the Chat page and
  included in a new Manager session's resume brief.
- Send `/new` (the whole message) in the web chat, Lark or Telegram to start a
  new Manager session: the next message, from any channel, starts with a clean
  context. The web chat's **New session** button sends it. Running tasks are
  not affected.
- A channel that fails to start is logged, and the dashboard keeps running.

### Agent Configuration

Every role (Manager, Planner, Workers, Reviewers) runs an **agent**: a
runtime, an LLM provider and a model, kept as an **Agent Profile** in
Settings → Agent Profiles and selected in Settings → Roles.

- A **runtime** is an agent CLI of a type Looper has an adapter for (Claude
  Code, Codex or OMP). Runtimes are built in: Looper finds each one on the
  server's PATH and probes its version and capabilities; Settings → Agent
  Runtimes shows them and can Re-detect one. A different binary comes from
  the server's PATH.
- A **provider** is where the agent's requests go: the protocols it serves,
  its models and their prices, and how it authenticates, with the runtime's
  own login or with an API key, which the API never returns.
- The built-in agent `claude-code/opus` (Claude Code, its own login, `opus`)
  runs every role that nothing else selects an agent for.

**Layered settings.** Four settings, the harness prompt, the agent (with its
model and effort), environment variables and a per-run spend cap, resolve
through four layers, a later layer winning:

| Layer | Edited in | Applies to |
|---|---|---|
| System | Settings → Roles, Shared defaults | Every agent |
| Role | Settings → Roles, a role's overrides | One role everywhere: the Manager, the Planner, Workers or Reviewers |
| Project | The project's Agent Profiles tab, All roles in this project | The project's Planner, Workers and Reviewers |
| Role in project | The same tab, the role's row | One of those roles in one project |

The Manager spans every project, so only the first two layers apply to it. A
task can also choose the Agent Profile of its Planner, Workers or Reviewers when
it is created (`profile_overrides`); that choice wins over every layer, for that
task only. The effort is the agent's: to run a role with another effort, select
an agent that has it. A field no layer sets keeps its built-in default, and the dashboard shows
each field's value with the layer it came from. Environment variables are not
for secrets: a name that looks like a credential is refused. A change applies
from each agent's next run or turn. See
[Harness, profile and environment](docs/architecture/agent-system.md#harness-profile-and-environment)
for the details.

**Role configuration.** Settings → Roles also edits each role's own fields
(`role_configs`):

- **system_prompt**: The role prompt, what this kind of agent is responsible
  for; the harness goes before it, and the prompt can be reset to the
  recommended one
- **max_turns**: Conversation turn limit (0 = unlimited)
- **timeout_seconds**: The limit on one run or turn (0 = no limit). For
  Workers it is one run's total time (default 86400); output does not extend
  it. Workers apply both limits from the next step on

How many Planners (one per task), Workers and Reviewers run at once in a
project (8, 16 and 8 by default) is a layered setting too: per role in
Settings → Roles, and per role in a project on its Agent Profiles tab.

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
uv run looper serve
```

## Releases

Each release's changes are in [CHANGELOG.md](CHANGELOG.md). Maintainers
publish a release with the procedure in
[docs/releases/README.md](docs/releases/README.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
