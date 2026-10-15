# Agent System Architecture

The Looper agent system is a three-tier multi-agent architecture with a web
dashboard for task management and real-time monitoring.

## Tiers

```
User → Chat/Lark/Telegram → Manager → TaskRouter → Planner → WorkerPool → Claude CLI
```

### Manager (`looper/agents/tiers/manager.py`)

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
  (`looper/agents/claude_cli.py`), which each task's Planner uses too, started
  on the first chat message:
  `claude --print --verbose --output-format stream-json --input-format stream-json --include-partial-messages --replay-user-messages <isolation flags> --append-system-prompt-file <file> --system-prompt-snapshot off --dangerously-skip-permissions --model M [--effort E] [--plugin-dir <dir> ...] [--mcp-config <file>] [--resume <session_id>]`
  (see [What an agent's Claude Code loads](#what-an-agents-claude-code-loads),
  [What an agent may do](#what-an-agent-may-do) and
  [The capability catalog](#the-capability-catalog-items-and-where-they-are-installed)). Like every agent
  it has every tool and no permission prompts, skills included, so it can run
  any command on this machine; that is why Telegram and Lark pass on only the
  senders they list ([Channels](#channels)).
  Its system prompt is its harness (its role layer's, else the system
  layer's, else the built-in one; [Harness, profile and environment](#harness-profile-and-environment)),
  the `manager` role prompt and `TASK_PLACEMENT_RULE`; it has no project and
  no task section.
  Each chat message is one user turn on its stdin; its turn starts when
  Claude starts that message and ends at the next `result`
  ([Turns Claude runs on its own](#turns-claude-runs-on-its-own)). Messages
  are handled one at a time, and each turn is one Runs record holding only
  that turn's cost and tokens
  ([What a run spent](#what-a-run-spent-and-how-large-its-context-grew)); the
  session's running totals are saved beside its id in the `agent_sessions`
  row `manager` (`session_totals`). When Claude exits
  (or dies mid-turn, which answers that message with an error) or the
  Manager's system prompt, model, effort, environment or installed
  plugins and skills change, the next message starts a new process that
  resumes the saved session with `--resume`. Stopping the Manager closes
  Claude's stdin and kills it if it is still running after 5 s
- A new session ([New sessions: handoff and resume brief](#new-sessions-handoff-and-resume-brief)):
  when its agent can no longer resume the saved session, the old session, while
  its Claude runs, first writes a handoff in a `[HANDOFF]` turn (a Runs record,
  sent to no chat; at most 300 s). The first turn of any new session (agent
  changed, session lost, or none saved) starts with a `[RESUME]` brief: why it
  is new, the handoff, and the recent conversations from `chat_messages`: the
  3 most recently active chats that are not a task's Planner thread
  (`planner-<task_id>`), each its last 8 messages oldest first, 300 characters
  each, at most 4,000 characters in all, without the message being answered.
  Nothing new to say, no brief. The Runs record's input holds the brief
- `/new`, a chat message that is exactly that (trimmed, any case) from the web
  chat, Lark or Telegram (the sender allowlists apply), is answered by Looper,
  never by Claude. After any turn under way, the Manager stops its Claude and
  saves the `manager` row with no session and `clean_start`, so the next
  message, from any channel, starts a new session without `--resume` and
  without a `[RESUME]` brief, also after a restart; the projects and goals
  every turn carries still come. The reset is a Runs record of its own
  (`metadata.session_reset`: channel, sender, chat and the session it ended)
  and the chat gets a confirmation. Tasks and their Planners are untouched.
  Any other text, `/new foo` and `/news` included, is handled as before: a
  message for Claude (in Telegram, an unknown command)
- A turn its Claude runs on its own, after background work it started ends,
  is a Runs record of its own (input "(no user message: ...)", metadata
  `autonomous: true`). What Claude said in it goes as a reply of its own
  (`AGENT_REPLY` with no `reply_to`, payload `autonomous: true`) to the
  channel and correlation of the message Claude answered before it. A web
  message's payload names its chat session (`chat_session_id`), so the web
  channel adds the reply to that chat's history (`chat_messages`, shown when
  the chat is opened) instead of a message's stream; Lark and Telegram send it
  to the chat. With no message before it, the reply is only recorded

### TaskRouter (`looper/agents/tiers/task.py`)

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
  Manager's, which spans all projects. `looper/agents/system_prompt.py` builds
  that prompt for all three, in five layers:

  ```
  <harness prompt>                                L1: see Harness, profile and environment
  <role prompt from role_configs>                 L2: planner, worker or reviewer row

  ## Project: <name>                              L3 (left out when the background is empty)
  <background>

  ## Project knowledge                            L3 (left out without documents): an index,
  In <directory>:                                     each directory once, then one line
  - <title>: <summary> (<path>)                       per document, its path relative to it

  ## Task                                         L4: what this session is for
  ...                                                 (Planner: the task; Worker, Reviewer: the step)

  <code-owned contract>                           L5: REPLY_CONTRACT, WORK_CONTRACT or REVIEW_CONTRACT; always last
  ```

  A project can also carry knowledge: documents its task agents read on
  demand instead of getting them in the prompt ([Project
  knowledge](#project-knowledge-documents-read-on-demand)). Only their index
  is in L3. Plugins and skills can be installed in it, and it can write its
  own skills ([Project skills](#project-skills-skills-a-project-writes)); they
  are not in the prompt: Claude Code lists them itself.

  A project's agent settings (harness, Agent Profile, effort, environment,
  per-run cap, and each task role's concurrency limit) are two layers of their own: the project's, for all its task agents,
  and each role's in the project ([Harness, profile and
  environment](#harness-profile-and-environment)). They are read when each run or turn starts.

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

- Creates/destroys PlannerAgent instances per task
- Handles task lifecycle: start, stop (pause), restart (resume), cancel
- At most the Planner's concurrency limit of a project's tasks run at once
  ([Concurrency limits](#concurrency-limits)), read each time a task may
  start. A new, restarted or resumed task waits in its project's queue, oldest
  first, and starts when one of the project's tasks ends or the limit is
  raised; a project at its limit never delays another project's tasks. There
  is no machine-wide limit
- At server startup, resumes the tasks the previous server left running whose
  plan is live (`executing` or `verifying`): each gets a new Planner and a
  `TASK_RESUME`. Any other task left running is marked "stopped"

### Planner (`looper/agents/tiers/planner.py`)

- One Planner per task for the task's whole run. It is a Claude Code agent: it
  plans the steps, decides what happens after every Worker and Reviewer
  result, and answers chat messages about the task. Code validates its
  replies and does the dispatching; Workers make the changes, one Claude
  process per step attempt.
- Keeps one resident Claude process through `ResidentClaude`, like the
  Manager, started on the task's first turn in the task's `work_dir`:
  `claude --print --verbose --output-format stream-json --input-format stream-json --include-partial-messages --replay-user-messages <isolation flags> --append-system-prompt-file <file> --system-prompt-snapshot off --dangerously-skip-permissions --model M [--effort E] [--plugin-dir <dir> ...] [--mcp-config <file>] [--add-dir <dir> ...] [--max-turns N] [--resume <session_id>]`.
  The system prompt is rebuilt for every turn from the harness (resolved for
  the Planner in the task's project), the `planner` role prompt,
  the task's project, the task section (task id, project name and
  description, working directory and the requirement) and `REPLY_CONTRACT`;
  the project is read before each event's turns. The turns themselves carry
  only what changed: the plan's progress, results, chat and supervision
  observations. When the prompt changes (a config edit, a new background,
  knowledge or harness, a task moved to another project) its file and hash
  change, or the model, effort, per-run cap, environment, installed plugins and
  skills or mounted knowledge directories change, the Planner stops
  its Claude, and the next process resumes the session with `--resume` and
  the new prompt. Like every agent it has every tool and no permission
  prompts ([What an agent may do](#what-an-agent-may-do)), and no MCP server
  loads. Its recommended role prompt keeps it to planning and deciding: it
  may read the project and run harmless commands, but does not modify files;
  every change goes to a Worker as a step.
- Each event is one user turn; turns run one at a time:
  - `[PLAN]` on `TASK_REQUEST`: Claude may look at the project without changing it, then splits the
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
- Reply contract (`looper/agents/tiers/planner_protocol.py`): `REPLY_CONTRACT`
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
    with `--resume <session_id>`; the session id and the session's running
    totals (`session_totals`) are kept in the `agent_sessions` row
    `planner-<task_id>`.
- The plan lives in PostgreSQL: its own state (id, status, origin,
  `final_summary`, `fail_reason`) in `task_plans` and each step, with
  everything the Planner decides with (label, dependencies, review flag,
  acceptance criteria, result, retries, feedback, late user messages), in
  `task_steps`. Each change writes the rows it touches; a change to several
  (a new plan, a replan, added steps, a settled step with the plan's summary)
  is one transaction. The project is not saved with it: it is read again
  before every event's turns.
- Task memory (`looper/agents/tiers/journal.py`): code writes a
  `task_journal` entry when its event happens, in the transaction of the
  change it records; the model never writes one. The kinds:
  - `step_accepted`: a step is accepted (in the transaction that settles it):
    its Worker's summary and the `learnings` its Worker and Reviewer reported;
  - `step_failed`: a step fails, because the Planner fails the task on it or
    its own failing verdict decides when the Planner cannot: what went wrong,
    the reason, the learnings;
  - `step_retry`: the Planner retries a step: what went wrong with the
    attempt, the Planner's feedback, the learnings (a retry is this one entry,
    not also a `decision`);
  - `decision`: a replan (reason, steps added and dropped, and the decided
    step's outcome and learnings when its attempt is dropped), steps added
    from a chat message, the task stopped at its spend cap (with the
    wrap-up);
  - `user_instruction`: a user's chat message to the Planner while the task
    runs, written before the `[CHAT]` turn, whole (up to 8,000 characters);
  - `session_switch` and `handoff`: a new Planner session and what the old one
    handed over ([New sessions](#new-sessions-handoff-and-resume-brief)).

  An entry of the whole task has the step id `''`. Every `[DECISION]`,
  `[SUPERVISE]` and `[CHAT]` turn (and the spend-cap wrap-up) carries the
  user's instructions, every one whole and oldest first, in a budget of their
  own (8,000 characters, which no other entry uses; only past it are the
  oldest left out and counted), then "Progress and findings": the newest entry
  of each kind first, then the other failures and retries, then the rest
  newest first, in at most 4,000 characters (what does not fit is left out and
  counted), under one heading per kind (failed steps, retried attempts,
  Planner decisions, accepted steps, session changes; a handoff goes in the
  resume brief instead). The plan lines carry each accepted step's summary
  (200 characters). No entry, no section.
- A learning may hold for the whole project: a Worker or Reviewer writes it as
  `{"text": ..., "scope": "project"}` (a plain string is the task's). When it
  arrives, with the project's `distill_lessons` `draft` or `write`, the Planner
  saves the report's project learnings as one draft in
  `project_document_drafts` (`Learnings from step <label>: ...`); `write`
  drafts them too, because a single learning is unreviewed. With `off` they
  stay the task's. Either way the journal keeps every learning, and the
  decision turn marks a project one `(holds for the whole project)`.
- With the project's `distill_lessons` `draft` or `write`, a task that
  completes gets one more turn, `[LESSONS]`, before its final status: the
  Planner turns the journal into a Markdown document of at most 4,000
  characters (the shape is in that turn's message, not in `REPLY_CONTRACT`),
  titled `Lessons: <requirement>`. `write` saves it as a `text` knowledge
  document; `draft` saves it to `project_document_drafts`, which no run reads,
  until the user confirms it (as a `text` document, under the knowledge caps)
  or discards it. A failed turn or a refused document is a warning in the
  activity; the task stays completed. `off` (the default) writes nothing.
- After a server restart (`TASK_RESUME`), a new Planner reads its plan and
  session back and carries on without planning again: a step whose Worker was
  running runs again as the same attempt with its feedback, a step in review
  gets a new Reviewer, a decision the stop cut off is taken again from the
  step's saved result, and Claude resumes its session from its saved totals.
  Continuing a task that failed at its spend cap reads the same rows.
- Each turn is one `llm_records` row with agent_id `planner-<task_id>`, the
  `task_id`, its own cost and tokens, its context at the end, `metadata.pid` and the system prompt's record
  (`system_prompt`, `system_prompt_sha256`, `prompt_layers`). The activity gets short
  summaries: `Planner Claude Code started (pid N...)`, `Plan created: ...`,
  `Planner decision on step N (s2): accept: ...`, `Plan changed: ...`.
- A turn its Claude runs on its own, after background work it started ends,
  is a row of its own (input "(no user message: ...)", `metadata.autonomous`
  true) and counts toward the task's cost. What Claude said in it answers no
  Planner request, so it decides nothing about the plan; the Planner logs it
  ([Turns Claude runs on its own](#turns-claude-runs-on-its-own)).
- The TaskRouter calls `PlannerAgent.stop()`, which ends its Claude, when the
  task ends (completed, failed, stopped or cancelled) and when the server
  stops.
- Before changing a recommended prompt, a contract or the prompt's layers, run
  the prompt regression eval ([`evals/README.md`](../../evals/README.md)).

### WorkerPool (`looper/agents/tiers/worker.py`)

- Manages a pool of Worker processes
- A Worker or Reviewer run starts once fewer of its role run in its task's
  project than that role's limit there ([Concurrency limits](#concurrency-limits)),
  read at that moment. Until then it waits (status `pending`, and one
  `Worker waits for room in this project (N running, limit L)` activity) and
  checks again when a run ends or a limit changes; a waiting run can be killed
  like a running one. Each task's Planner also keeps at most 4 Workers running
  (`MAX_CONCURRENT_WORKERS`)
- Each Worker runs Claude CLI as a subprocess with `stream-json` output
- Real-time event streaming (tool_use, text) captured to activity table
- Command: `claude --print --verbose --output-format stream-json --include-partial-messages --input-format stream-json --replay-user-messages <isolation flags> --append-system-prompt-file <file> --system-prompt-snapshot off --dangerously-skip-permissions --model M [--effort E] [--plugin-dir <dir> ...] [--mcp-config <file>] [--add-dir <dir> ...]`,
  plus `--max-turns N` when the config sets `max_turns` above 0. A Worker
  reads the `worker` row and a Reviewer the `reviewer` row, for every run. The
  first user turn goes on stdin; stdin stays open so the Planner can add user
  turns mid-run, and closes when the first result arrives. Background work the
  run started that is still going then (an agent team's teammates,
  [below](#runtimes-llm-providers-and-agent-profiles)) brings turns of Claude's
  own, each with a result, before Claude exits: the Worker reads the output to
  its end, and the run's last result is its outcome, its report and its cost
  ("Worker finished" goes to the task's activity once, from it)
- The step is the system prompt; only other agents' output is the user turn:
  - system prompt: the harness (resolved for the Worker or Reviewer in its
    project; a Reviewer without its own takes the Workers'), the
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
  - first user turn: for a Worker, a short start cue, what the steps it
    depends on reported (summary and changed files, from `WORK_ITEM`
    `dependencies`), the user's instructions (whole, oldest first, in their own
    budget) and "Findings so far in this task" (`WORK_ITEM` `findings`: the
    accepted steps it depends on first, then the failures and retries, then
    the other accepted steps newest first, in at most 2,000 characters); for a Reviewer, the Worker's report (summary, changed
    and deleted files) as data to check, never in the system prompt;
  - messages sent mid-run (forwarded chat, Planner steering) stay user turns
    and do not restate the contract.
- `WORK_ITEM` carries the step's fields apart: `requirement`, `step_key`,
  `instruction` (the step's own, without feedback), `feedback` (a list) and
  `attempt`, `dependencies`, `findings`; `VERIFY_REQUEST` adds `requirement` and
  `step_key`.
- Each run is one `llm_records` row with `metadata.command` (the exact
  argv), the system prompt's record (`system_prompt`, `system_prompt_sha256`,
  `prompt_layers`), `metadata.step_id`, and the process's cost, tokens, final
  context and compactions; the activity's launch line shows
  the isolation settings by their size
- Claude is stopped when a run takes longer than its row's (`worker` or
  `reviewer`) `timeout_seconds` in total (default 86400, 0 = no limit). The step fails
  with `Claude ran longer than the N s limit (timeout_seconds)`. Partial
  deltas only keep the run's heartbeat (`last_output_at`) current; they do
  not extend the limit
- A step counts as completed only when all of these hold; anything else fails the
  step with the reason in `error`:
  - the run's last `result` event has `subtype: success`, so hitting max turns fails;
  - the final message ends with the JSON report
    `{status, changed_files, deleted_files, summary, learnings}`, with `status`
    set to `completed`;
  - every file in `changed_files` exists, and every file in `deleted_files` is
    gone.
- A reviewer must return a boolean `passed`; prose without the verdict is not a
  pass.
- `learnings` is optional in both the report and the verdict: up to 5 short
  findings or pitfalls for later steps, each a string (the task's) or
  `{"text": ..., "scope": "project"}` (one that holds for any task of the
  project; another scope, or none, is the task's). Code keeps the first 5
  with non-empty text, each clipped to 300 characters, and drops anything
  else; a bad value never fails a step or a review.

### What an agent's Claude Code loads

Every agent (Manager, Planner, Worker, Reviewer) sees what Looper composes and
the project's own `CLAUDE.md`, not the personal Claude Code configuration of
whoever runs Looper. `isolation_args(cwd)` in `looper/agents/claude_cli.py` is
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
`looper serve`. With user settings gone, the model, the effort and any
environment variables come from Looper (next section), not from the operator's
Claude Code settings, and the plugins and skills come from the Looper catalog,
where they are installed (below), never from `~/.claude` itself.

### What an agent may do

Every agent (Manager, Planner, Worker, Reviewer) has every tool and runs
without permission prompts (RFC decision 8). `permission_args()` in
`looper/agents/claude_cli.py` is `--dangerously-skip-permissions`, and
`agent_settings.Launch.args` puts it in front of every agent's launch flags,
beside the installed plugins', so the four commands cannot diverge. No command
carries `--tools`, `--allowedTools` or `--permission-mode`. What an agent is
for is the job of its role prompt and its code-owned contract: the
Planner's recommended role prompt says it plans and decides, does not modify
files and gives every change to a Worker.

Verified with Claude Code 2.1.285 in `--print` mode with the isolation flags
and a `--plugin-dir`: the init event reports `permissionMode:
bypassPermissions`, and Bash, Write and a plugin skill that declares
`allowed-tools: Bash` all ran without a permission denial. Without the flag
the same run had the Write and the skill denied (`date` alone is allowed
either way, as a read-only command). The isolation `--settings`
(`claudeMdExcludes`, `autoMemoryEnabled`) leave permissions alone. Not
checked: a project's own `.claude/settings.json` (which still loads) or
managed settings that deny tools or turn bypass mode off.

Because the Manager can now run any command on this machine, Telegram and
Lark pass on only the senders they list, and a channel without such a list
does not start ([Channels](#channels)). The web dashboard stays local-only
([HTTP API](#http-api)).

### Runtimes, LLM providers and Agent Profiles

An **Agent Profile** is a runtime, an LLM provider and a model (RFC decision
10, the [multi-runtime RFC](rfcs/multi-runtime-agents-v0.zh-CN.md)), selected
per role ([next section](#harness-profile-and-environment)); "agent" names only
a running agent (the Manager, a Planner, a step's Worker or Reviewer), which
runs the profile its role selects. A profile is a row of `agent_profiles`,
edited in Settings → Agent Profiles and through `/api/agent-profiles`; the
registry, its checks and its API are `looper/agents/profile_registry.py`.

- **Runtime types** are code: one adapter per type in `looper/agents/runtimes`
  (`RuntimeAdapter`). Every tier starts its runtime through it, so no tier
  builds a command itself. An adapter declares its `capabilities`, the API
  `protocols` it speaks and the provider `auth_kinds` it takes, and it
  probes an executable (`--version`, never a model call), binds a provider
  (`bind_provider`), lays out the launch (`launch_args`, `environment`,
  `resident_command`, `run_command`, with its isolation flags and the
  system-prompt file), resumes a session (`resume_args`), runs a resident
  session (`resident_session`), writes a user turn, parses the output into
  internal events (`parse`; the internal event is Claude Code's stream-json
  event, which another adapter translates into), summarizes a tool call for
  the task's activity, says where a run's cost comes from with a provider
  (`cost_source`) and prices it (`cost`), and says why a run stopped at its
  cap. Its binding (`ProviderBinding`) sets arguments and environment
  variables and removes inherited ones (`unset`); the variables it sets are
  ones a provider's own `env` may not (`provider_env_owned`). It reports a
  run's tokens as internal `usage` events (one model request or turn each,
  `runtimes.pricing.usage_event`), which the run's recorder adds up for the
  soft cap. `ClaudeCodeRuntime` (`claude-code`) is the first type: the
  command layouts moved out of the tiers unchanged, the
  process mechanics stay in `claude_cli.py`, and the catalog and knowledge
  are materialized in Claude Code's plugin format by
  `library.load_installed` and `knowledge.load_knowledge`. `CodexRuntime`
  and `OmpRuntime` (`codex`, `omp`, below) translate them at launch; both
  keep the Manager's and a Planner's session with one process per turn
  (`runtimes/per_turn.py`). Capabilities:
  `run`, `resident_session`, `resume`, `structured_output`,
  `claude_plugins`, `mcp`, `add_dirs`, `compaction_events`,
  `autonomous_turns`, `cost_report` (Claude Code has all but
  `structured_output`; Codex has `run`, `resident_session`, `resume`,
  `structured_output`, `mcp` and `add_dirs`; OMP has `run`,
  `resident_session`, `resume`, `claude_plugins`, `mcp`, `add_dirs` and
  `compaction_events`).
- **Codex** (`looper/agents/runtimes/codex.py`, verified against Codex CLI
  0.162.0 in the RFC's section 6) runs `codex exec --json` with API key
  providers that serve `openai-responses`, or with Codex's own ChatGPT
  login (below). Each process gets a `CODEX_HOME`
  of Looper's own under `<runtime_root>/.local/codex`, named on its command
  line (`env CODEX_HOME=... codex exec ...`) and holding only the generated
  `config.toml` (the provider as `[model_providers.looper]`, its key only in
  the `LOOPER_PROVIDER_API_KEY` variable, which shell commands do not get;
  the run's MCP servers as `[mcp_servers.<name>]`, stdio and HTTP; Codex's
  own skill list and plugin sync off) and `AGENTS.md`: a short block of
  Codex runtime notes, then the system prompt. The operator's `~/.codex` is
  never read. A Worker or Reviewer is one `--ephemeral` process with its
  prompt on stdin, which then closes: it cannot be steered while it runs.
  The Manager and a Planner run one process per turn that resumes the
  session (`exec ... resume <thread> -`, `ResidentCodex`), in a home of their
  own (`codex/sessions/manager`, `codex/sessions/planner-<task_id>`) whose
  `config.toml` and `AGENTS.md` are refreshed before each turn, so a changed
  prompt reaches a resumed session. Skills are mounted read-only
  (`--add-dir`) and listed in the runtime notes for the agent to read; a
  plugin's hooks, commands and agents, and SSE MCP servers, are left out and
  named in the run's `metadata.runtime_catalog`. Codex reports tokens only,
  as the session's running totals at the end of each turn: each turn's own
  tokens become one `usage` event, priced at the provider's prices, so the
  soft cap can stop a Codex run only once a turn ends. It reports no
  compaction and runs no turn of its own. Run homes (`codex/runs/<sha256>`)
  that nothing has written to for a day are removed when a new one is
  written. `looper serve` registers the read-only runtime `codex`
  (`rt-codex`) when `codex` is on its PATH.
- **The Codex login** (`looper/agents/runtimes/codex_login.py`, the RFC's
  P1) is the built-in provider `codex-login` (`prov-codex-login`, OAuth,
  `openai-responses`), which pairs only with Codex runtimes. A run with it
  gets a home whose `auth.json` is a symbolic link to the operator's login
  file (`auth.json` in the server's `CODEX_HOME`, else `~/.codex`), by its
  absolute path and never a copy, and a `config.toml` with
  `cli_auth_credentials_store = "file"`, no `model_provider` (Codex's
  built-in OpenAI one), `features.apps = false`, and
  `shell_environment_policy.set` giving the agent's shell commands
  `CODEX_HOME=<runtime_root>/.local/codex/shell-home`, which has no login, so
  a `codex` command the agent runs (`codex logout` included) cannot reach
  it; `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN` are removed from its
  environment. A session home gets the link before each turn while its agent
  runs with the login and loses it otherwise. Codex saves a refreshed login
  by writing the file in place, so it writes through the link. After every
  login run (`RuntimeAdapter.after_run`, from the run's recorder) and when
  `looper serve` starts, every Codex home is inspected with `lstat`: a copy
  or any other entry in place of the link is removed (never followed) and the
  link put back, a login home whose link is gone is reported too, and the
  problem is recorded (`system_settings.codex_login_fork`), logged and put in
  the task's activity as a warning. While it is recorded the login is
  unavailable: its runs fail with the reason (`RuntimeAdapter.login_problem`,
  checked like a missing API key), as they do when the login file is gone,
  until the operator has checked `codex login status` and clears it
  (`DELETE /api/providers/<id>/login-problem`, "Use the login again" on the
  provider). The login's state (`codex login status` in a temporary home
  holding only the link, which reads the file and does not ask the server)
  is in the provider's view (`login`, read at most every 30 s) and in the
  `codex_login` self-check, skipped while no Agent Profile uses the login.
  Its runs are priced at the built-in prices as a subscription equivalent;
  the form suggests the models a ChatGPT login offers (`suggested_models`).
- **OMP** (`looper/agents/runtimes/omp.py`, oh-my-pi's `omp`, verified against
  omp 18.4.3 in the RFC's section 6) runs `omp -p --mode json` with API key
  providers that serve `openai-responses`, `openai-chat` or
  `anthropic-messages`. Each process runs with `HOME` set to a home of
  Looper's own, `omp/homes/<sha256>` (one per set of generated settings,
  shared by its runs), named on its command line (`env HOME=... omp ...
  --config <home>/looper.yml`): `.omp/agent/models.yml` (the provider
  `looper`: endpoint, API, the agent's model at no price; its `apiKey` names
  the `LOOPER_PROVIDER_API_KEY` variable), the `--config` overlay (only the
  agent's model for every model role, no fallback, no approvals, MCP tools as
  ordinary tools, no background jobs, no update check or issue reports), a
  link to the native library every home shares (`omp/natives`), the prompt
  files and, with MCP servers, a plugin `looper` holding them. omp keeps a
  user's whole configuration under `HOME`, so the operator's `~/.omp`, the
  other tools' directories it imports (`~/.claude`, `~/.codex`, `~/.agents`)
  and `~/.env` are never read; the variables that would point it back
  (`PI_CODING_AGENT_DIR`, `XDG_*_HOME`, `CLAUDE_CONFIG_DIR`, ...) are not
  passed, and omp's shell prefix (`PI_SHELL_PREFIX`) gives the agent's shell
  commands the operator's `HOME` and those variables back, without the key.
  Outside a git repository omp's search for a project's content would climb
  past the operator's home, so there the sources that climb (`native`,
  `claude`, `agents`, `agents-md`, `claude-md`) are off. The system prompt is
  `--append-system-prompt` (OMP runtime notes, then the prompt), which a
  resumed session takes up. A Worker or Reviewer is one `--no-session`
  process with its prompt on stdin, which then closes. The Manager and a
  Planner run one process per turn with `--session-dir omp/sessions/<agent>`
  and `--resume <id>` (`ResidentOmp`); the turn that starts a new session
  removes the agent's earlier ones. The installed plugins load natively
  (`--plugin-dir`: skills, commands, agents); their hooks and output styles
  do not, their own MCP servers are turned off (`disabledExtensions`), and
  both are named in `metadata.runtime_catalog`; knowledge is read by the
  index's paths (`--add-dir` would load the `AGENTS.md` files above each
  directory). Each model request's tokens become a `usage` event, so runs
  record their context size and the soft cap can stop one between requests;
  a subagent's (the `task` tool) come with its result, and a compaction that
  ran becomes `compact_boundary`. `looper serve` registers the read-only
  runtime `omp` (`rt-omp`) when `omp` is on its PATH. When it starts,
  `read_only_copies` removes the OMP homes no running process names and the
  session directories (`codex/sessions/<agent>`, `omp/sessions/<agent>`) of
  agents whose saved session (`agent_sessions`) is not of that runtime.
- **Runtimes** (`agent_runtimes`) are built in, never registered by hand: one
  per type found on the server's PATH (`rt-<type>`, named after its type;
  Claude Code's `rt-claude-code` comes with the schema and runs the server's
  `claude`), with an `executable` (`''` = the type's default) and what its
  last probe found (`version`, `capabilities`, `probe_error`). A different
  binary comes from the server's PATH. Settings → Agent Runtimes shows one
  card per type, found or not, with its path, version, capabilities and the
  roles it can run, and Re-detect (`POST /api/runtimes/<id>/probe`) probes it
  again, or looks for a type not found yet and adds its runtime when it is on
  PATH now. Runtimes registered by hand before (`builtin` false) are dropped
  once by the schema when no profile uses them (`schema_migrations`
  `agent_runtimes_unused_custom_removed`); one a profile uses stays,
  read-only, shown as "custom (legacy)", so the profile keeps running the
  executable it ran.
- **Providers** (`llm_providers`) are where requests go: `protocols`
  (`anthropic-messages`, `openai-responses`, `openai-chat`), `base_url`,
  `max_concurrency` (`null` = no limit, stored and checked only), a model
  catalog (each model's `context_window`, input, output, cache read and cache
  write prices per million tokens, `efforts` and `background_model`, each
  `null` when unknown; `[]` = any model), its own `env` (`[{key, value}]`,
  added to the runs that use it; no credentials, and none the runtime's
  binding sets) and the auth: `oauth`, a runtime type's own login
  (`oauth_runtime_type`), or `api_key`. A key lives in `llm_provider_secrets`
  and the API shows it as `***` (sending `***` back keeps it, `null` removes
  it); it is read only when a run starts (`load_agent_settings` puts it on
  the run's `ProviderSpec.api_key`, which is never shown, recorded or
  exported), and a run whose API-key provider has none fails with the reason.
  A catalog price left `null` comes from the built-in price table
  (`runtimes.pricing.BUILTIN_PRICES`: Claude, GPT, GLM, DeepSeek and Kimi
  models at the vendors' USD list prices, with the date and sources).
  Presets (`provider_presets.py`: GLM on BigModel and on Z.ai, DeepSeek, Kimi
  on Moonshot China and international, and the local CLIProxyAPI) fill a new
  provider from one of a vendor's APIs (Anthropic-format for Claude Code, or
  OpenAI-format), its common models with their background model, and the
  extra environment its Claude Code guide sets; the user adds the key.
- **Agent Profiles** (`agent_profiles`): a name, a runtime, a provider, a
  model, a default effort and, for a Claude Code runtime only, whether its runs
  have agent teams (`agent_teams`, on by default for Claude Code profiles;
  below). The registry
  (`profiles`) shows each one with the roles its runtime can run (`roles`) and where it is selected
  (`used_by`: the layers, a project's with its name, and how many tasks chose
  it); one in use cannot be deleted (409, naming where). A profile's id keeps
  the `agent-` prefix it had before the rename (`agent-claude-code-opus`,
  `agent-<12 hex>`): ids are stored values.

The built-ins exist on every database and are read-only: the runtime
`claude-code`, the providers `claude-subscription` (Claude Code's login) and
`codex-login` (Codex's ChatGPT login, above), and the
profile `claude-code/opus` (`BUILTIN_PROFILE`), which a role runs when nothing
selects another. A profile is refused when it is created or changed if its
runtime and provider share no protocol, an OAuth provider is paired with
another runtime type, the runtime cannot take that kind of provider yet
(Claude Code and Codex take their own login and API keys, OMP API keys only), or the
provider lists models and not this one; a profile whose model or background
model has no full price is registered with a warning (its runs record tokens
only and no spend cap applies to them), and every profile shows how its runs
are priced (`cost`); a provider change that would break one of its profiles is
refused too. Wherever it is selected (a layer, a task override), its runtime
must have what each role reached needs (`ROLE_CAPABILITIES`):
`resident_session` and `resume` for the Manager and Planner, `run` for
Workers and Reviewers; a layer of every role (`system`) or of a project
checks all of them. A row still referred to is not deleted (409). The
resolved profile is checked again when each run starts
(`agent_settings.profile_problem`): a run whose profile cannot run fails with
the reason (the Manager answers with it, the Planner's turn fails, a Worker
or Reviewer step fails) and never runs another profile.

A resident session belongs to a runtime type and a provider
(`agent_sessions.runtime_type` and `provider_id`, with `profile_id`, the
profile that ran it last): it resumes only while both are unchanged, so
another runtime type or any change of provider (subscription to API key,
back, or one API key provider to another) starts a new session, after a
handoff from the old one ([New sessions](#new-sessions-handoff-and-resume-brief)).
A session from before Agent Profiles counts as Claude Code's, one from before
`provider_id` as Claude Code's own login's. Changing the model alone resumes
as before.

**Claude Code with an API key.** The binding sets `ANTHROPIC_BASE_URL` and
the key: in `ANTHROPIC_AUTH_TOKEN` (sent as `Authorization: Bearer`, what GLM,
DeepSeek and Kimi document) with a `base_url`, removing an inherited
`ANTHROPIC_API_KEY`; in `ANTHROPIC_API_KEY` (`x-api-key`) for Anthropic's own
API (no `base_url`, or `api.anthropic.com`), removing an inherited
`ANTHROPIC_AUTH_TOKEN`. With a `base_url` it also maps every model Claude Code
may use behind the agent: `ANTHROPIC_DEFAULT_OPUS_MODEL`,
`ANTHROPIC_DEFAULT_SONNET_MODEL` and `CLAUDE_CODE_SUBAGENT_MODEL` to the
profile's model, `ANTHROPIC_DEFAULT_HAIKU_MODEL` and `ANTHROPIC_SMALL_FAST_MODEL`
to the catalog's background model (the model itself without one). Then come
the provider's own variables. The verified Claude Code behaviour is in the
[multi-runtime RFC](rfcs/multi-runtime-agents-v0.zh-CN.md) section 7. A
resident process whose binding changed (provider, key, endpoint) is started
again.

**Agent teams.** They are on by default for Claude Code profiles: a profile
created without `agent_teams` gets them on a Claude Code runtime and not on
another (`profile_registry.default_agent_teams`), a change that moves a profile
to a runtime of another type without saying takes that type's default, and the
upgrade turned them on once for every profile already on a Claude Code runtime,
the built-in `claude-code/opus` included (`schema_migrations`
`agent_profiles_agent_teams_on`), so a profile turned off afterwards stays off.
The built-in profile is read-only: to run without teams, select a profile with
them off. Asking for them on another runtime is refused (400). A profile with
`agent_teams` starts the Manager's, the Workers' and the Reviewers' Claude Code
(so any of them can spawn teammates) with
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` and `--teammate-mode in-process`. A
Planner never has them (`Launch.agent_teams`, keyed on the Planner's role): its
plan and each of its decisions are read from the result of its own turn, so a
turn that waited for a teammate would end without its decision, and the
teammate's later turns are Claude's own, which the Planner only logs (#26); the
Planner already runs work in parallel through Looper's Workers, whose steps
Looper tracks and teammates would bypass. A Planner, and every role of a
profile without them, starts with that variable removed, whatever the server's
environment (a `looper serve` started from a Claude Code shell inherits it),
the settings' or the provider's variables say. Turning them on or off restarts
the Manager's resident process (its command changes) and resumes its session:
the runtime and the provider are unchanged. Verified with Claude Code 2.1.285:
it enables teams only with the variable; under its default teammate mode
(`auto`) a process inside tmux starts each teammate as a tmux process of its
own, whose cost the run's result does not count, hence `in-process`, where a
teammate runs in the agent's process as a background task. Through
`looper serve` with DeepSeek: a teammate that outlives the turn that spawned it
starts a turn of the agent's own with its message and another when it ends,
each with a result, also after a Worker's stdin has closed. Every result
carries the session's totals with the teammate's tokens in `modelUsage` (under
the agent's model, or under the background model when the teammate was spawned
with the `haiku` alias), so a Worker's last result and a resident agent's turns
of its own ([below](#turns-claude-runs-on-its-own)) count them at the
provider's prices: a teammate's spend lands in its agent's run total, not in a
record of its own. Known gaps: a teammate's model requests are not in the
stream (its assistant messages are, with a `parent_tool_use_id`), so the soft
per-run cap below does not see its tokens before a result reports them, and
nothing records its requests one by one; a teammate does not get the agent's
`--append-system-prompt-file` (the harness, role, project, task and contract
layers).

**Cost and caps.** Each run records `cost_source`. `runtime`: Claude Code's
own figure (`total_cost_usd`), used with its own login (shown as
"equivalent": what the subscription's usage would cost at API prices, not
money spent) and with Anthropic's API. `provider_price` for every other
provider (Codex's own login too, at the built-in prices, shown as
"equivalent" like Claude Code's): each model's tokens in the result's `modelUsage` (input, output,
cache read, cache write) at the provider's prices, summed (`run_usage`
`price`), so a resumed session still counts each turn's own share; a model
without a price leaves the cost `null` and the tokens recorded. With
`provider_price` the per-run cap (`run_budget_usd`) is not `--max-budget-usd`
(it would count Claude's prices): the run's recorder adds up its `usage`
events (each request's final `message_delta` usage) at the provider's prices,
and once the run is over the cap the Worker's process is killed, or a resident
turn ends and its process is killed, with a result read like Claude Code's own
cap (`Looper stopped the run at its per-run spend cap: ...`). It is a soft cap:
the request that crossed it is made, and requests the stream does not show (a
subagent's, Claude Code's side requests) count only in the run's recorded
cost. The task cap and "raise the cap and continue" sum the recorded costs, so
they use the same basis. `GET /api/tasks/:id` counts the runs of each basis
(`equivalent_runs`: a runtime's own login, `provider_price_runs`: an API key provider).

The `llm_profiles` table and the role configs' `runtime` and `llm_profile_id`
were never on a launch path; the one-time migration `llm_profiles_removed`
drops them (with `/api/llm-profiles`), and a role config update naming them
is a 400.

### New sessions: handoff and resume brief

The Manager and a task's Planner resume their session for as long as they can
(RFC multi-runtime P5). `looper/agents/session_switch.py` says when they
cannot: `agent_switch`, called before every turn with what the session was
saved with and the agent the turn resolved, is the one place the rule lives
(another runtime type, or another provider), and
`session_lost` names a session the runtime could not resume (or none saved
for work under way).

- Agent changed: while the old session's Claude still runs, it takes one
  `[HANDOFF]` turn: the Planner's asks for its understanding of the codebase,
  unfinished reasoning, open risks and what it would check next, as
  `{"handoff": ...}` (at most 3,000 characters, one repair turn); the
  Manager's for what each chat is about, promises, the user's decisions and
  open questions, as plain text. Then the old Claude stops and the saved
  session is dropped. A handoff turn that fails (the old provider is down),
  or an old Claude that is gone, leaves no handoff.
- Session lost: the saved session could not be resumed; the Planner and
  the Manager drop it and run the turn again in a new session, as before.
- The Planner journals `session_switch` (why, from which runtime and agent to
  which), after the `handoff` when there is one, in one transaction, and
  notes `The Planner starts a new session (...)` in the activity.
- The first turn of a new session for a plan under way (and of any new
  Manager session with something to say) starts with a `[RESUME]` brief: why
  the session is new, where the task is (steps accepted, under way, failed)
  and the handoff, or that there is none and the journal is what remains.
  The Planner's request after it carries the plan, the user's instructions
  and the journal as every turn does; `REPLY_CONTRACT` says a brief may come
  first. The Manager's brief adds the recent conversations. The brief is
  given once: until the new session reports its id.

### Harness, profile and environment

`looper/agents/agent_settings.py` is the one place that resolves what an agent
inherits. The harness prompt is an agent's character and stability: working
principles, boundaries and communication. The role prompt says what that kind
of agent is responsible for. Keeping them apart means no harness edit can
drop an agent's responsibilities.

Five settings resolve through four layers (`agent_setting_layers`, one row per
layer and place), a later layer winning (RFC decision 9). The smallest unit is
a role in a project; Workers and Reviewers are never configured one by one. A
task may override the Agent Profile of its Planner, Workers and Reviewers
(`profile_overrides` at creation, stored in `task_profile_overrides`); that is
the only task-level setting, and it wins over every layer for that task only
(source `task`).

| Layer | Edited in | Applies to |
| --- | --- | --- |
| `system` | Settings → Roles, every role | every agent |
| `role` | Settings → Roles, per role | one role everywhere: Manager, Planner, Workers or Reviewers |
| `project` | the project's Settings tab | the Planner, Workers and Reviewers of one project |
| `role_project` | the same tab, per role | one of those roles in one project |

The Manager spans every project, so it has the first two only. With no layer
setting a field, the built-in value applies (`builtin`): the harness
`DEFAULT_HARNESS_PROMPT` (public and generic), the profile `claude-code/opus`
(Claude Code, its own login, `opus`), the runtime's own effort, no extra
environment and no cap. The fields are the harness, the Agent Profile
(`profile_id`; its model comes with it, and a profile's default effort applies
unless a layer sets one, source `profile`), the effort (`--effort`: `low`, `medium`, `high`, `xhigh` or `max`), up
to 50 environment variables per layer, and the per-run cap
(`--max-budget-usd`, on each Claude Code process, when Claude Code's own
figure is the run's cost; otherwise Looper's soft cap at the provider's
prices, per run: a Worker run or a resident turn. A Planner turn or a Manager
message that hits either fails, and the next turn starts a new process). The environment merges key by
key: a later layer adds or overrides a variable or unsets one an earlier layer
set (a `null` value; the system layer has nothing to unset). A Reviewer
without a harness of its own at the role or role-in-project layer takes the
Workers' at that same layer. `load_agent_settings(role, project, task)` returns
every field with the layer it came from; all four roles launch from it.
Every launch passes `--model` and, when set, `--effort` and (Claude Code's own
cost only) `--max-budget-usd`. The variables are added on top of the server's environment for
every agent's `claude` process; names must match `[A-Z_][A-Z0-9_]*`, and a
name containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `CREDENTIAL` is
refused because the values are shown on the dashboard. That word check guards
against pasting a secret by habit; it is not a security boundary. A stored
value that no longer validates is ignored as unset. When the layers cannot be
read, the Manager and a Planner keep what they last read, and a Worker or
Reviewer run uses the built-in values with a warning in the task's activity.
A one-time migration (`agent_setting_layers` in `schema.sql`) moved the
earlier values into their layers unchanged: the Settings defaults to `system`;
each role's `model` and the Manager's `harness_prompt` to `role`; a
project's `model` and `run_budget_usd` to `project`; a project's
`planner_harness_prompt` and `worker_harness_prompt` to `role_project`. The
old columns and keys are dropped. A second one-time migration
(`agent_setting_layers_agent`) turned each layer's `model` into the Agent
Profile `claude-code/<model>` (the built-in one for `opus`), so every role resolves the
model it did; a stored model that no longer validated was ignored, so it
selects nothing. Changes apply to the next Worker or Reviewer run; a resident agent's
restart signature is its command (which carries the model, the effort, the
plugin directories of its installed items, each named by its content's hash,
and the knowledge's `--add-dir` directories),
the prompt's sha256, the environment's sha256, the installed items' sha256 and, for
the Planner, the mounted knowledge directories, so the next turn restarts it
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
sha256s of the environment and of the installed items.

Every run records the prompt in `llm_records.metadata`:

- `system_prompt`: the text, as before;
- `system_prompt_sha256`: the hash of that text, which is also the file name;
- `prompt_layers`: where each layer came from, built in
  `looper/agents/system_prompt.py`:
  - `harness`: `{source, sha256, project_id?, from_role?}`; `source` is the
    layer (`builtin`, `system`, `role`, `project` or `role_project`, the last
    two naming the project), and `from_role` is `worker` when a Reviewer took
    the Workers' harness. Runs recorded before the layers have
    `builtin_default`, `system_default`, `manager_override` or
    `project_override`;
  - `role`: `{config_id, sha256, recommended}`, the role_configs row
    (`manager`, `planner`, `worker` or `reviewer`) and whether its prompt
    equals the recommended `default_prompt`;
  - `project`: `{id, background_sha256, updated_at, knowledge_sha256,
    knowledge_documents, knowledge_error?}` or `null` (`background_sha256`
    is `null` without a background, `knowledge_sha256` without documents;
    `knowledge_documents` is how many documents the index lists;
    `knowledge_error` says why a run went without its project's knowledge);
  - `task`: `{kind: plan|work|review, task_id, step_id, sha256}` or `null`
    (the Manager);
  - `contract`: `{name, sha256}` (`REPLY_CONTRACT`, `WORK_CONTRACT`,
    `REVIEW_CONTRACT` or `TASK_PLACEMENT_RULE`);
  - `isolation`: `ISOLATION_ID`.
- `profile`: `{id, name, model, runtime: {id, name, type}, provider: {id, name,
  auth}, source}`, the Agent Profile the run used (`id` is its profile id) and
  where it was selected; runs recorded before the rename to Agent Profiles have
  it as `agent`, which the dashboard and the cost query read too;
- `model_choice`: `{model, source, effort}`, the model asked for (`source` is
  the profile's layer; earlier runs have `builtin_default`, `system_default`,
  `agent_override` or `project_override`); the record's `model` column keeps
  the model Claude reports;
- `settings_sources`: `{harness, profile, effort, run_budget_usd, env: {NAME:
  layer}}`, the layer each setting came from (runs recorded before the rename
  have `agent` in place of `profile`, and an effort from the profile's own
  default as `agent`; runs before Agent Profiles have `model`);
- `max_budget_usd`: the per-run cap, when one applies;
- `env`: `{sha256, keys: {NAME: sha256 of the value}}`, only when the run
  has variables; values are never recorded;
- `library`: `{sha256, role, project_id, items: [{kind, name, sha256, plugin,
  scope}], mcp_servers?, error?}`, the catalog items installed for the run
  (an MCP server has no `plugin`; `mcp_servers` lists their names): its role and
  project, each item with the plugin it loads in (a plugin's own name,
  `personal` or `project`) and `scope` (`project` when its project has it,
  else `global`), and the hash of that set, only when something was to be
  loaded; `error` says why a run went without them. Runs recorded before the
  catalog have items without `plugin` and `scope`, and a task agent's run may
  have `project_skills` (`{project_id, sha256, plugin, skills}`), which is no
  longer written.

The same inputs give the same prompt and hash; changing the harness, the role
prompt, the background, the knowledge index, the task or a contract changes
it.

### Concurrency limits

A sixth layered field, `max_concurrency` (a whole number from 1 to 64), is set
only on the `role` and `role_project` layers of the Planner, the Workers and
the Reviewers; anywhere else the API answers 400 and a check constraint holds
the table to it. It is how many of the role run at once in one project: the
Planner's counts the project's tasks (one Planner each), the Workers' and the
Reviewers' their runs across the project's tasks. Each role has its own limit,
because one count shared by all three would let resident Planners hold every
place so that no Worker could start. It resolves built-in (planner 8, worker
16, reviewer 8) → role → role in the project (`resolve_concurrency`); the
TaskRouter and the WorkerPool read it each time something may start
(`load_concurrency`, the built-in value when the layers cannot be read).
After a successful write that sets it (a role's or a role-in-project PUT, or
an applied import), the API asks the TaskRouter and the WorkerPool to check
again, so a raised limit starts what waits at once. A lowered limit stops
nothing that runs; new starts wait until fewer run than it. There is no
machine-wide limit. The role configs' `max_concurrency`, which nothing
enforced, was dropped (`role_configs_max_concurrency_removed`).

### Project knowledge: documents read on demand

A project can carry reference material too large for a prompt
(`looper/agents/knowledge.py`, RFC section 6 and decision 3). Each document
has a title, a required one-line summary and one of three sources, never a
URL:

- `text`, written on the dashboard, and `upload`, a text file sent as base64
  in JSON: stored in `project_documents` (`content`, `filename`, `size_bytes`,
  `sha256`). Content that is not UTF-8 or holds a NUL byte is refused as
  binary;
- `path`: a regular file in the project's default directory, stored as its
  path relative to that directory (an absolute path inside it is made
  relative) and read live. On save the path must exist, be a file, be text
  and, links followed, stay inside the directory; `..`, absolute paths
  elsewhere and links that lead out are refused. Its size and hash are those
  of the last save.

**Caps**, each refused with a 400 that states it: a document 2 MiB; a project
100 documents and 50 MiB of stored content (path documents are not stored);
the composed index 4,000 characters. Changes to one project's documents run
in a transaction that holds the project's row lock, so two concurrent adds
cannot both pass a cap. An index line carries the title, the summary and a
path relative to a directory the index names once, so short documents reach
the count cap before the index cap (with an absolute path, about 150
characters a line, the index used to fill at 15 to 25 documents).

**Delivery.** `load_knowledge(runtime_root, project, work_dir)` runs for every
Worker and Reviewer run and every Planner turn, never for the Manager:

- the text and upload documents are materialized read-only (0444) together,
  in one directory per set of documents, named by the hash of their file
  names and contents:
  `<runtime_root>/.local/projects/<project_id>/knowledge/<set_sha256>/<filename>`
  (a text document's file name comes from its title, an upload's from its
  file name, both reduced to letters, digits, `.`, `-` and `_`; a name an
  earlier document took, ignoring case, gets `-2`, `-3`). A copy that already
  matches its hash is reused and a damaged one is rewritten;
- the project layer gains `## Project knowledge` after the background: an
  introduction, then for each directory a line `In <directory>:` (the set
  directory, then the project's default directory) followed by
  `- <title>: <summary> (<path relative to it>)` per document; a path document
  whose file is gone or leads out of the directory is listed as
  `(missing: <path>)`. Claude Code resolves a relative `Read` path against the
  working directory only, not against an `--add-dir` root (verified with
  2.1.285), so the agent joins the path to the directory named above it. An
  index that grew past the cap after it was saved is clipped with a note;
- the launch mounts the set directory with `--add-dir` when the project
  has a text or upload document, and the project's default directory when it
  has a path document and the task runs in another directory.
  `claude_cli.knowledge_args` builds the flags and `agent_settings.Launch`
  carries them beside the installed plugins', so the three task agents cannot diverge
  and the Manager's launch has none.

Edits apply at once: the next Worker or Reviewer reads them as it starts, and
the Planner's next turn sees a new index (or new directories) and restarts
with `--resume`. A path document is read when an agent opens it, so editing
the file needs no restart. The task's activity notes `Project knowledge
applied (N documents)` for each run that has some, and, for the Planner,
`changed` or `removed`. When the documents cannot be read or materialized a
run goes without them, its record's `prompt_layers.project.knowledge_error`
says why and the activity logs a warning; the Planner keeps the knowledge it
last read. A replaced or deleted document's copy stays on disk until the
server next starts (see the catalog's **Reclaiming copies**).

Verified with Claude Code 2.1.285 under the isolation flags: the agent reads
files in an `--add-dir` directory, a `CLAUDE.md` in that directory or above it
is not loaded, and a directory that does not exist does not fail the run.

### Project skills: skills a project writes

A project can write its own Claude Code skills (`looper/agents/project_skills.py`,
RFC section 6 and decision 9). Each is a catalog item of origin `project`,
owned by the project (`owner_project_id`; it goes with its project) with one
file, its `SKILL.md`, and nothing is written into the project's directory:

- `name`: a lowercase slug (`deploy-check`), unique among the project's
  skills. It is the skill's directory and its frontmatter `name`;
- `description`: one line, which Claude Code shows the model to decide when to
  use the skill;
- the body of its `SKILL.md`, given without frontmatter (content starting with
  a `---` line is refused, since the fields make it). The file is the
  frontmatter, with the description as a double-quoted string so a colon, a
  quote or a `#` stays text, then the body.

Writing one (`POST /api/projects/:id/skills`) also installs it in its project
for the Planner, Workers and Reviewers, enabled unless `enabled: false` is
given. It can be installed in other projects too (`POST /api/installs`), never
globally. Editing changes the item for every project it is installed in;
deleting removes it from the catalog and from all of them. Every project
skill loads in the generated plugin `project`, so Claude Code names it
`project:<name>`; a project cannot hold two of one name, so installing,
writing or renaming one onto a name that would clash there is a 409.

**Caps.** Each is refused with a 400 that states it:

- a name: at most 64 characters, Claude Code's limit for a skill name;
- a description: at most 1,024 characters, the limit of Claude Code's skill
  format. The longest of 104 real personal skills has 975;
- a body: at most 64 KiB. It enters the context whenever the skill is used.
  The largest of those 104 is 61 KB, and long reference material belongs in
  the project's knowledge;
- a project: at most 20 skills written in it. Every installed skill's name and
  description is listed in every run's context.

A name the project already has is a 409. Changes to one project's skills run
in a transaction that holds the project's row lock, as its knowledge does, so
two concurrent adds cannot both pass the count cap or take one name.

The `project_skills` table of M4 was moved into the catalog once, at the first
start of this version (`library_catalog_installs` in `schema_migrations`): each
row became an item with the same id, project, name and description, its
`SKILL.md` as runs wrote it, installed in its project for the three task roles
and enabled as it was; then the table was dropped.

Verified with Claude Code 2.1.285 under the isolation flags and
`--dangerously-skip-permissions`, with haiku:

- the init event lists the plugin's skills as `project:<name>`, and its
  `plugins` as `{name: "project", source: "project@inline"}`;
- the model invokes such a skill with the Skill tool from the read-only copy;
- two `--plugin-dir` plugins of the same name both load, their skills listed
  under one prefix;
- no `~/.claude/plugins/data/project-inline/` directory was created for a
  plugin with only skills.

### Project execution settings

A project can set how its task agents run (`looper/agents/project_execution.py`,
typed nullable columns on `projects`, `null` = as before; its agent and per-run
cap are agent settings, [Harness, profile and environment](#harness-profile-and-environment)):
`task_budget_usd`, checked against the task's cost before every Planner turn
and every new Worker or Reviewer: at the cap the running ones are stopped and
the task fails, at once (`task_budget_action` `fail`, the default) or after one
more Planner turn that sums up (`wrap_up`), with the cap and the amount in its
error; `supervise_interval_seconds` over the `planner` config; and
`review_policy` `all`, under which no step is accepted without a passing review;
and `distill_lessons` (`off`, `draft` or `write`, never null), whether a task
that completes leaves a lessons draft or document in the project's knowledge
(see the Planner).

### The capability catalog: items and where they are installed

What agents load is not read from `~/.claude`. Items are acquired into a
catalog first, and each loads only where it is installed
(`looper/agents/library.py`, `looper/agents/catalog.py`; RFC decisions 7 and 9):

- **Catalog.** `library_items` and `library_files` hold each item: kind
  `plugin`, `skill` or `mcp`, name, `origin` (how it was acquired: `machine`,
  imported from this machine, `upload`, `git`, `ui`, added in Settings, or
  `project`, written in a project), `modified` (an uploaded or git item edited
  since), the owning project of a project-authored item, a
  description, source label, declared version, `sha256`, size, file count, and
  its files (relative path, UTF-8 content, sha256, executable bit). An item's
  `sha256` is the hash of its files' paths, hashes and executable bits. A name
  is unique among the items outside projects, and within the project that
  owns an item. An item does nothing by itself.
- **Installs.** `library_installs` puts an item on a scope: `global`, or one
  project, for a non-empty set of roles (`manager`, `planner`, `worker`,
  `reviewer`), with its own `enabled` switch; one install per item and place.
  A project install never names the Manager, which spans every project, and a
  project-authored item is installed in projects only. CHECK constraints hold
  the scope, the roles and the project together.
- **Import** (`POST /api/catalog/import`, Settings → "Import from this
  machine" / "Re-import") only reads the operator's Claude Code configuration
  (`$CLAUDE_CONFIG_DIR`, else `~/.claude`): every plugin `settings.json`
  enables (`enabledPlugins`), from its user-scope install in
  `plugins/installed_plugins.json`, taken whole (its skills, agents,
  commands and hooks); and every personal skill, `skills/<name>/` with a
  `SKILL.md` (a linked directory is followed). Personal settings and hooks,
  MCP servers, permissions and memory are not read. `.git`, `.in_use`
  (Claude Code's plugin-cache bookkeeping), `.DS_Store` and `__pycache__` are
  left out; a link inside an item is read as its target when it stays in the
  item. A re-import touches the `machine` items only: it replaces the files of
  an item whose hash changed and keeps its id and installs, leaves an
  unchanged one as it is, adds new items installed nowhere and removes the
  items that are gone with their installs. The last import's report (counts,
  changes, what was left out) is the `library.last_import` row of
  `system_settings`. Items imported before the catalog were left installed
  nowhere at the upgrade, so they load nowhere until installed.
- **Caps.** A file over 2 MiB, a binary (not UTF-8) file, and a link out of
  its item are left out; an item over 32 MiB or 5,000 files is left out
  whole; all are reported. A library over 128 MiB is refused with a 400 and
  nothing changes. Measured on a real configuration (3 plugins, 104 skills):
  about 21 MB of text in 2,200 files, the largest item 9 MB, the largest file
  0.7 MB; 4 image files were left out.
- **Launch.** `load_installed(runtime_root, role, project_id)` runs for every
  Worker and Reviewer run and every Planner and Manager turn. It reads the
  enabled installs that name the run's role, global or in its project
  (`db.library.list_installed`; the Manager has no project), loads an item
  installed in both places once, as the project's, and gets each item a
  read-only copy under `<runtime_root>/.local/library/<sha256>/<name>/`,
  written once from the database (checked against the hashes) and renamed
  into place, so runs never read `~/.claude`. Each plugin is one
  `--plugin-dir`; the skills are linked into generated plugins under
  `<runtime_root>/.local/library/<hash of the plugin and its set>/`: a
  project's own skills into `project` (`project:<skill>`), every other skill
  into `personal` (`personal:<skill>`). The plugins come first, then
  `personal`, then `project`. `claude_cli.library_args` builds the flags and
  `agent_settings.Launch` carries them, so all four commands get them the
  same way. When the installs cannot be read or written, a run goes without
  them and its record's `library.error` says why; Workers and Reviewers note
  a warning in the task's activity, and the Planner keeps the items it last
  read. The task's activity notes `Project installs applied (...)` for a
  Worker or Reviewer run that has project installs, and `Installed plugins and
  skills applied`, `changed` or `removed` for the Planner.
- **MCP servers** (`looper/agents/mcp.py`) are items of kind `mcp`, installed
  the same way. `library_mcp_servers` holds how Claude Code starts one
  (`stdio`: command, args, env) or reaches it (`http`/`sse`: URL, headers); the
  env and header values are secrets, so catalog rows carry only their names and
  the API shows each value as `***` (a `PUT` that sends `***` back keeps it).
  Added and edited with `POST /api/catalog/mcp` and `PUT /api/catalog/<id>`
  (origin `ui`), deleted with `DELETE /api/catalog/<id>`, or imported read-only
  from the user-level `mcpServers` of `~/.claude.json`
  (`$CLAUDE_CONFIG_DIR/.claude.json` when set) with
  `POST /api/catalog/mcp/import`, which mirrors the plugin import (origin
  `machine`). An item's `sha256` hashes its whole configuration. At launch the
  run's servers go into one file, `<runtime_root>/.local/mcp/<sha256>.json`
  (0400, named by its content), passed as `--mcp-config` after the
  `--plugin-dir`s; `--strict-mcp-config` stays, so it is the only MCP source
  and a plugin's own `.mcp.json` servers do not load. The record's
  `library.mcp_servers` names them; their values are never recorded.
- **Upload, git and Settings** (`looper/agents/catalog_sources.py`) add
  plugins and skills outside projects, installed nowhere: an uploaded
  `SKILL.md` or zip (refused whole for an absolute path, `..`, a link or an
  over-cap file; binary files left out), a shallow clone of a public
  `https://` repository previewed and then imported (`source` is
  `<url>@<commit>`; the same URL again updates those items in place; the
  clone runs no hooks or submodules, reads none of the operator's git
  configuration or credentials, is size- and time-capped and removed), or a
  skill written in Settings (origin `ui`). Settings, uploaded and git skills
  are edited with `PUT /api/catalog/<id>` (an uploaded or git one becomes
  `modified`); machine items change only by re-importing. A name clash
  outside projects is a 409, and the request may rename the item.
- **Reclaiming copies.** `looper serve` reclaims replaced copies when it
  starts, before any agent launches: `read_only_copies.reclaim_unused` removes
  every content-hashed directory of the catalog, project knowledge and the
  project skills plugins of versions before the catalog, and every
  `.local/mcp/<sha256>.json`, that the current content (what every role in
  every project loads) does not use and no running process's command line
  mounts (or links into through a mounted one).

Verified with Claude Code 2.1.285 under the isolation flags: in `--print`
mode `--plugin-dir` loads a plugin's skills (as `<plugin>:<skill>`), agents,
commands and hooks; a directory needs no manifest (its name names it) and a
"folder of plugins" loads each child. The plugins' own `SessionStart` hooks run
and add their context; the operator's `UserPromptSubmit` hook in
`~/.claude/settings.json` does not. Claude Code keeps a `--plugin-dir`
plugin's data in `~/.claude/plugins/data/<plugin>-inline/`, apart from the
operator's own sessions, and a plugin's hook may still read files of its own
choosing (one checks a flag file in `~/.claude`). Every agent's context grows:
with 3 plugins and 104 skills, the first request of a minimal `--print` run
on Opus went from 15,674 to 29,114 tokens (`/context`: skills 2.1k → 10k
tokens, plugin hook context 3.8k).

### Moving the configuration: export and import

`looper config export <file>` and `looper config import <file>`
(`looper/agents/config_transfer.py`) move an installation's agent configuration
through one JSON file, `format: looper-config/v2`: `runtimes`, `providers` and
`profiles` (the registered ones; the built-ins exist everywhere; each names the
others by name; a profile carries its `agent_teams`, and one from a file
without it takes its runtime's default, on for Claude Code), `settings` (the system and role layers, each naming its Agent
Profile in `profile`;
each role's prompt, `null` while it follows the recommended one, its timeout
and enabled flag; a role's `max_concurrency` in an older file is ignored, with
one note), `channels`, `installs` (global),
`projects` (with `--include-projects`: fields, execution settings, layers,
knowledge, own skills and installs) and `catalog` (each item with its files,
or an MCP server's configuration). Catalog files are UTF-8 text, so JSON holds
them as they are; the file is written piece by piece from one read-only,
repeatable-read snapshot, by the CLI straight from the database (a catalog is
often over the server's 8 MiB request cap) or streamed by Settings' Export
download (`GET /api/config/export`). A v1 file (`looper-config/v1`, or
`loopx-config/v1` from before the rename to Looper) imports too: it is read as
v2 under its older names (`_from_v1`), its `agents` being the `profiles` and a
layer's `agent` its `profile`.

Secrets (MCP environment and header values, a provider's API key, a channel
config's keys other than its sender allowlist, `*_env` names and description)
are `{"secret": "omitted"}` unless `--include-secrets`. A project's directory,
an item's source and a runtime's executable that are local paths are not
exported; a path document is a reference relative to its project's directory,
flagged `repoint`. A layer of a file written before Agent Profiles gives a
`model`: the import selects the profile `claude-code/<model>`, adding it as
the upgrade does. Tasks are not exported, so neither are their profile
overrides.

An import matches entries by natural key: an item by kind and name (a
project's own skill within its project), a project, runtime, provider or
profile by name, a layer by scope,
role and project, a role by name, a channel by type. The preview (the
default, in a read-only transaction) lists per section what would be added,
changed (with the fields), unchanged or skipped (with the reason), and the
secrets to fill in. `--apply` applies the schema and writes the same plan in
one transaction: an existing entry is updated in place (an item keeps its id
and installs), nothing missing from the file is removed, and a placeholder
keeps a stored secret, or leaves the value empty when there is none. Imported
items keep their origin. Importing the same file again changes nothing, and
exporting the new installation gives the same file.

Settings imports too (`looper/agents/config_imports.py`). `POST
/api/config/imports` is the one route that reads past the 8 MiB request cap:
it streams up to 256 MiB (twice the catalog's cap) to a 0600 file under
`<runtime root>/config-imports/`, after the Origin and Host checks, and
answers with an import id and the same preview. `POST
/api/config/imports/<id>/apply` runs `--apply`'s transaction and deletes the
file; `DELETE` cancels; an import left alone expires after an hour, and the
server clears the directory when it starts and stops. After applying, each
secret to fill in names the provider, MCP server or channel that takes it, and
Settings links to its form (`#settings/providers/<id>`,
`#settings/catalog/mcp/<id>`, `#settings/channels/<id>`).

### What a run spent and how large its context grew

Every run records its own cost and tokens, its context at the end and the
context compactions it went through (`looper/agents/run_usage.py`; RFC §8, M2).
There is no spending cap (RFC decision 2); caps come as optional project
settings in M5.

Verified with Claude Code 2.1.285 (haiku, recorded in
`tests/fixtures/claude_stream/`):

- A `result`'s `total_cost_usd` and `modelUsage` (per model: `inputTokens`,
  `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`) are the
  running totals of the session in that process. They count every request,
  including a compaction's summary request and subagents' requests.
- A process started with `--resume` carries on from the session's earlier
  totals: its first result reports the earlier spending plus its own. Measured:
  a session at $0.01046 reported $0.01279 for a resumed turn of its own
  $0.00232.
- A `result`'s `usage` covers only the turn's own main-loop requests. A
  compaction-only turn reports zeros there although it cost $0.0088, and a
  subagent's requests are left out too. An assistant message's `usage`
  describes its request, but its `output_tokens` is a snapshot taken while
  streaming.
- A request's prompt is `input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens` of its assistant message. A subagent's messages
  carry a `parent_tool_use_id`.
- A compaction, automatic or `/compact` sent as a user turn, is
  `{"type": "system", "subtype": "compact_boundary", "compact_metadata":
  {"trigger": "auto"|"manual", "pre_tokens", "post_tokens",
  "cumulative_dropped_tokens", "duration_ms", ...}}`. It follows
  `system/status` events `status: "compacting"` and then `compact_result:
  "success"`. A failed attempt is only the status event, with
  `compact_result: "failed"` and `compact_error` (for example `too_few_groups`).
  `--autocompact` (or `CLAUDE_CODE_AUTO_COMPACT_WINDOW`) accepts 100k–1M
  tokens, and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` lowers the threshold to about
  that percentage of the window. With the window at 100k, haiku sessions
  compacted at about 25k tokens with the override at 15, and at about 30k with
  it at 31. Without a window, the override at 10 did not compact a 25k
  session.

So:

- **Cost and tokens** (`cost_usd`, `input_tokens`, `output_tokens`,
  `cache_read_tokens`, `cache_creation_tokens`) are the change in the reported
  totals over the run. A Worker or Reviewer run is a whole process, so they
  are its totals. A resident agent's turn subtracts the totals at the
  previous result. `ResidentClaude` keeps those totals. A process that
  resumes a session starts from the totals saved beside its id in
  `agent_sessions`. The saved totals are
  rewritten after every result. When a session cannot be resumed and is
  dropped (`session_started` false), its totals are dropped with it, and the
  new session starts from zero. A counter that went down is taken as having
  started again. Without `modelUsage` (an older Claude Code), the tokens come
  from the turn's own `usage`.
- **Context** (`context_tokens`) is the prompt of the run's last main-loop
  request. Subagents' requests do not count. It is null for a turn that made
  no request, such as `/compact`.
- **Compactions** (`compactions`) lists each `compact_boundary` as
  `{trigger, pre_tokens, post_tokens, duration_ms}`. It is `[]` when there
  were none, and null for runs recorded before M2.
- **A task's cost** (`llm_records.task_costs`) is the sum of its Planner's,
  Workers' and Reviewers' runs, in total and by role, with the run count. It
  also counts `running` runs (cost not known yet) and `unpriced_runs`
  (finished without a reported cost). A Manager turn about the task is not
  part of it.
- **An agent's usage** (`llm_records.agent_usage`, `GET
  /api/agents/:id/usage`) gives its total cost, runs and compactions, its cost
  by session and by UTC day, and its latest turns (oldest first) with each
  turn's context, cost and compactions. That is the Manager's and the
  Planners' context over time.

### Turns Claude runs on its own

A resident Claude (Manager, Planner) also runs turns that answer no message.
Verified against Claude Code 2.1.285 (haiku, with the resident flags and
`--replay-user-messages`; recorded in
`tests/fixtures/claude_stream/background_*.jsonl`):

- A message written with a `uuid` keeps it. Claude writes a
  `command_lifecycle` event for it in state `queued` when it reads the
  message, `started` when it begins the message's turn and `completed` right
  after the turn's result. The turn's `system/init` follows `started`; then
  comes the message's replay (`isReplay`, the same uuid; only with
  `--replay-user-messages`). The result names the message in
  `user_message_uuid` and `user_message_uuids`.
- When background work a turn started (a Bash command or a subagent run in
  the background) ends and no turn is running, Claude writes
  `system/task_notification` and runs a turn of its own: `system/init` up to a
  result with no `user_message_uuid` and an `origin`
  (`{"kind": "task-notification", ...}`), with no lifecycle event and no
  replay. A background subagent's own events (with a `parent_tool_use_id`)
  come before it, between the turns. A message that arrives meanwhile is
  `queued` at once and `started` only after that result.
- Work that ends during a user turn is folded into that turn (a replayed
  `<task-notification>` message with an `origin`) and answered by its result.
- An agent team's teammate (a profile with agent teams, DeepSeek through
  `looper serve`) is such background work. Its message starts a turn of
  Claude's own too: a replayed message with an `origin` of kind `peer`, then
  a result with that `origin`; its end brings the `task-notification` turn.
  A Manager asked to spawn one answered `STARTED`, then sent the teammate's
  word and a closing line as two replies of its own, each a run with its
  share of the totals.

`ResidentClaude` reads a process's output for as long as it runs. A user
turn starts at its message's `started` event, or its replay, and ends at the
next result. Every other event belongs to a turn of Claude's own, which runs
from a `system/init` (or a lone result) to a result and starts with the
events written since the previous turn. The owner's `autonomous` handler gets
that turn; its cost is its share of the running totals, taken in the order
Claude wrote the results, so the turns around it keep theirs. Until a process
has marked a user turn (the first events of a process, a stand-in that writes
no markers) the waiting message's turn starts with the next event, as before.
A turn of Claude's own still running when the process stops is recorded
without a result.

## Message Bus

All inter-agent communication uses typed `MessageEnvelope` over an in-process
async message bus (`looper/agents/bus.py`). Every message is journaled before
delivery and marked delivered once consumed. The WorkerPool consumes
`WORK_ITEM` and `VERIFY_REQUEST` through topic consumers that take delivery.
On restart the bus replays undelivered messages for the Manager, the TaskRouter
and channels. It drops messages for Planners and Workers: those exist per task,
and a resumed task's Planner works out what to run again from its plan in the
database. The journal is then compacted.

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
| `TASK_RESUME` | TaskRouter → a resumed task's new Planner | Carry on with the saved plan after a server restart |
| `GOAL_STATUS` | Planner → TaskRouter → Manager | Report progress/completion |
| `WORK_ITEM` | Planner → Worker | Dispatch step |
| `WORK_RESULT` | Worker → Planner | Step result |
| `VERIFY_REQUEST` | Planner → Reviewer | Review a step's result |
| `VERIFY_RESULT` | Reviewer → Planner | Review verdict (`passed`, findings) |
| `WORKER_INPUT` | Planner → running Worker | Forwarded user text as the Worker's next user turn |
| `WORKER_INPUT_RESULT` | WorkerPool → Planner | Whether that text was delivered |

## How a task runs

1. The Manager sends `TASK_REQUEST` to the TaskRouter, which records the task,
   creates its Planner (or queues the task while its project runs as many
   tasks as the Planner's limit there) and forwards the request.
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
| `projects` | Projects: name (unique ignoring case), description, default `work_dir` (`''` = none), `background` for the task agents' system prompt (`''` = none), and the execution settings (`project_execution`) |
| `tasks` | Task metadata, status, phase, timestamps, `work_dir`, `project_id` (required) |
| `task_steps` | A task's steps, each with everything its Planner decides with: `step_key` (label), `depends_on` (step ids), `review`, `acceptance_criteria`, `validation_commands`, `status`, `result`, `worker_id`, `reviewer_id`, `retries`, `feedback`, `missed_messages` |
| `task_plans` | A task's plan, one row per task: `plan_id`, `status` (`planning`, `executing`, `verifying`, `stopping`, `completed`, `failed`), `correlation_id`, `origin_channel_id`, `final_summary`, `fail_reason`; replaced when the task is planned again |
| `agent_sessions` | The session each resident agent resumes: `manager` or `planner-<task_id>`, its `session_id`, `session_totals`, the `runtime_type` and `provider_id` it belongs to and the `profile_id` that ran it last (`NULL` runtime: before Agent Profiles, Claude Code; `NULL` provider: before providers were recorded, Claude Code's own login), and `clean_start` (the user dropped the session with `/new`: the next one starts without a resume brief) |
| `task_journal` | A task's journal, written by code as things happen: `step_accepted`, `step_failed`, `step_retry`, `decision`, `user_instruction`, `session_switch` and `handoff` entries, each with its `step_id` (`''` for the whole task), `summary` and `learnings`; `GET /api/tasks/<id>` returns it as `journal` and the task page lists it as Findings, each entry with its kind |
| `activity` | Execution trace events (SSE streaming to frontend) |
| `chat_messages` | Chat conversation history; a new Manager session's resume brief reads the recent chats that are not a task's Planner thread (`planner-<task_id>`) |
| `llm_records` | One row per agent run: input and output text, status, the run's own `cost_usd` and its `cost_source` (`runtime` or `provider_price`), `input_tokens`, `output_tokens`, `cache_read_tokens` and `cache_creation_tokens`, its final `context_tokens` and its `compactions`; `metadata` holds the exact argv (`command`), `cwd`, the appended `system_prompt`, its `system_prompt_sha256`, its `prompt_layers`, the `profile` (`agent` on runs recorded before the rename), the `model_choice`, the Settings `env` names, the `library` items loaded and a task agent's `project_skills` |
| `role_configs` | Each role's own settings, keyed by `role` (`manager`, `planner`, `worker`, `reviewer`): display name, role prompt and its recommended `default_prompt`, turns, timeout, enabled |
| `agent_setting_layers` | Agent settings by layer: `scope` (`system`, `role`, `project`, `role_project`), `role`, `project_id` (goes with its project), and the nullable `harness_prompt`, `profile_id`, `effort`, `env` (ordered `[{key, value}]`, `null` value = unset), `run_budget_usd` and `max_concurrency` (a Planner, Worker or Reviewer `role` or `role_project` layer only); one row per layer and place |
| `agent_runtimes` | Runtimes, built in (one per type found on PATH) or registered by hand before and kept while an Agent Profile uses them (`builtin` false): `name`, `type` (an adapter), `executable` (`''` = the type's default), the probe's `version`, `capabilities`, `probed_at`, `probe_error`, `builtin` |
| `llm_providers` | LLM providers: `name`, `protocols`, `auth` (`oauth` with `oauth_runtime_type`, or `api_key`), `base_url`, `models` (the catalog, with each model's prices and `background_model`), `env` (its own variables), `max_concurrency` (`NULL` = no limit), `builtin` |
| `llm_provider_secrets` | A provider's API key, apart from the provider; read only by the launch that will use it |
| `agent_profiles` | Agent Profiles: `name`, `runtime_id`, `provider_id`, `model`, default `effort`, `agent_teams` (Claude Code's agent teams: on by default for Claude Code profiles, set by the registry and once by the upgrade; the column's default, false, is for other runtimes), `builtin`; a provider still used by one is not deleted |
| `task_profile_overrides` | A task's Agent Profile per role (`planner`, `worker`, `reviewer`): `profile_id`, going with its task; a profile a task chose is not deleted |
| `system_settings` | Settings shared by the whole system, one value per key (no row = the built-in default), such as the last library import's report |
| `library_items` | The capability catalog: kind, name, `origin` (`machine`, `upload`, `git`, `ui` or `project`), `modified`, `owner_project_id` (a project-authored item; goes with its project), `description`, source label, declared version, `sha256` of the files, size, file count, `imported_at` |
| `library_files` | Each catalog item's files: relative path, UTF-8 content, sha256, executable bit |
| `library_installs` | Where an item loads: `scope` (`global` or `project`), `project_id`, `roles` (a non-empty subset of `manager`, `planner`, `worker`, `reviewer`; never `manager` in a project), `enabled`; one per item and place, going with its item and its project |
| `project_documents` | A project's knowledge: title, one-line summary, `source_kind` (`text`, `upload` or `path`), `filename` and `content` (text and upload) or `path` (relative to the project's default directory), `size_bytes`, `sha256`; a CHECK keeps each kind's columns consistent and the rows go with their project |
| `channel_configs` | Telegram/Lark settings (enabled flag, env var names, sender allowlist) |

Plans and sessions used to be files (`.local/planner/planner-<task_id>/*.json`,
`.local/manager/session.json`). The first start with the database holding
them imports them once (`looper/agents/state_import.py`, recorded as
`file_state_import` in `schema_migrations`); plan files from before the
resident Planner are skipped with a log line. Nothing reads the files after
that, so they can be removed. The bus journal (`.local/agent-bus`) and the
read-only copies under `.local` stay files.

Every task belongs to one project. On the first start after projects were
added, the one-time migration `tasks_project_backfill` creates one project per
distinct task directory and assigns those tasks to it. The project is named
after the directory's last path component (`''` and `/` become `default`);
the oldest directory keeps the plain name and a later clash, ignoring case,
gets `-2`, `-3`, .... A task's "active" statuses are `pending`, `assigned`,
`running` and `executing`.

The names follow the dashboard's, and "agent" names only a running agent:
the one-time migration `profiles_roles_renamed`, which runs first in
`schema.sql`, renamed `agent_configs` to `role_configs` (its key `agent_id` to
`role`, after dropping the `role` column that only repeated it), `agents` to
`agent_profiles`, `task_agent_overrides` to `task_profile_overrides`, and the
`agent_id` of a layer and of a task's choice, and a session's
`session_agent_id`, to `profile_id`. The tables and columns are renamed in
place (their foreign keys follow) and their constraints take the names a new
database gives them, so a migrated database is shaped like a new one.

Connection: `LOOPER_DATABASE_URL` environment variable or `.env` file, else
`postgresql://localhost:5432/loopx`: the default database keeps its name from
before the rename, because it holds the user's data.

### Renamed from LoopX

The product was LoopX up to v2.5.0. `looper.env` reads every `LOOPER_*`
variable's `LOOPX_*` name when the new one is unset, with a warning once per
name, for one release; the `loopx` command is an alias that prints a
deprecation note. `looper.paths.settle_runtime_root` moves `~/.loopx` to
`~/.looper` the first time `looper serve` or `looper service install` uses the
default runtime root (one `rename`, leaving `~/.loopx` as a link to it; both
present: `~/.looper` is used, with a warning). `looper service install`
replaces a `com.loopx.serve` unit with `com.looper.serve`. Data in the database
(an operator's harness text, a channel's `bot_token_env`) is not rewritten; a
stored `LOOPX_*` variable name finds its `LOOPER_*` counterpart, and a system
harness stored as the pre-rename built-in still follows the built-in.

## Web Dashboard

Single-page React app served from `web/dist/` via the built-in HTTP server
(`looper/agents/http_server.py`). Default port: 8767. An installed wheel has no
`web/`; it serves the copy staged into the package (`looper/web/dashboard`,
built by `scripts/stage-dashboard.sh` at release time), and it carries
`looper/db/schema.sql` the same way.

### Pages

- **Chat**: Every agent and its messages, finished ones included. The selector lists the global Manager and the 30 most recent tasks; each task shows its one Planner and the Worker (and Reviewer, when reviewed) of each step. You can send to the Manager and to a running task's Planner, which keeps one thread per task; SSE streaming. A selected Planner also shows its Claude runs, one per turn, in a folded list of the latest 10 beside its activity and chat thread, re-read as a reply starts and ends and every 10 s while the task runs. A Worker or Reviewer shows its Claude runs read-only, following a running one live, and points to its task's Planner for sending. The Manager's chat has a "New session" button that sends `/new`.
- **Tasks**: Task list with project and status filters, time display, each task's cost so far, Start/Stop/Delete controls. `#tasks?project=<id>` filters by a project and shows its name and default directory with a link to its project page. A new task picks an existing project or creates one in the same form; its work directory may stay empty when the project has a default. A task's detail view can move it to another project in any status.
- **Projects (sidebar)**: Below the navigation, every project with its task count (or a spinner with its active count); clicking one opens its project page, and `+` creates a project (name, default directory, description, optional background).
- **Project** (`#projects/<id>`, `#projects/<id>/knowledge|skills|settings`): a header with the name, description, default directory and task count (a link to the filtered Tasks page), then four tabs, arrow keys moving between them. **Overview**: the 5 latest tasks with status and cost and a link to all of them; the background (three lines, then Show more); the knowledge (document count, stored size, the first 5 titles and summaries, how many referenced files are missing); the skills (count, how many are on, the first 5 names and descriptions); and the execution settings that are set, in words, with each role's model, per-run cap and harness from this project. **Knowledge**: the documents (title, summary, source, file name or path, size, a Missing badge for a path document whose file is gone) with a readout of documents, stored bytes and index characters against their caps. Opening a row shows its body (a path document's file as it is now); Edit changes the title, summary and content (a text or upload document) or path (a path document); a document is added as Text, Upload or Project file, or by dropping a file on the section; delete asks first. **Skills**: the skills (`project:<name>`, the body's size, `Off` for a disabled one, the description) with a count against the cap. Opening a row shows its `SKILL.md` body; Edit changes the name, description and body; a skill is added by hand or from a `SKILL.md` (Upload SKILL.md, or a drop on the section), whose frontmatter fills the name and description and whose body below it fills the instructions, for review before Add; a switch turns one on or off; delete asks first. Knowledge and skill changes apply at once and a refusal shows the server's reason. **Settings**: the name, default directory, description, background (`n / 10000`) and execution settings, saved together (Save is disabled over a limit or without a change; Discard changes resets them); then Agent settings: a form for the project's layer ("All agents in this project") and one for each role in the project (Planner, Worker, Reviewer), with that role's limit of how many run at once in the project (empty or Clear inherits), where every field also says what applies now and from which layer; then Delete project, offered only while the project has no tasks.
- **Task Detail**: Overview (status, phase, times, steps and the task's cost with its run count, then "Cost by agent": the Planner's, the Workers' and the Reviewers' runs and cost with each one's share, noting runs still running or without a reported cost; steps, agent timeline, live activity feed), Pipeline DAG (React Flow), Sessions, Conversation
- **Runs** (`#runs/<role>`): each role's sessions and run history, and no configuration. A Runs list shows every recorded Claude run of the role (the Manager, every task's Planner, Workers or Reviewers), a Manager `/new` as a `new session` row, and links each to its agent's history; each role links to its section in Settings → Roles.
- **Agent history** (`#history/<agent-id>?task=<task-id>`): opened from History on an Agent Timeline row, a Runs entry or the Chat page. The Manager's and a Planner's page starts with "Context & cost": the latest context size, the number of compactions, the total cost and the number of turns. Below them is a sparkline of the context at the end of each of the latest 100 turns, scaled between their lowest and highest value, with a dashed marker on each turn that compacted. A list of the compactions follows (turn, time, trigger, context before → after), then a folded table of the turns. The section is re-read every 10 s. For each Claude run it shows status, model, the run's own tokens (uncached input, cache read, cache write, output), its context at the end (`ctx`), a `compacted` badge, its own cost, the launch command (cwd and argv; a long argument such as the system prompt is shown by its length, and Copy copies the exact command), a collapsible System prompt section with what Looper appended (Claude Code's built-in system prompt is not included): its title shows the prompt's size, its short sha256 and an `isolated` badge, and inside one line per layer (harness source and version, role recommended or edited, project and background version, task, contract), the model with its source and effort, and the Settings environment variable names, above the full text; runs recorded before the layers existed show the text only. A collapsible Plugins & skills section shows how many plugins and skills the run loaded from the library, with its hash, and how many project skills (from `metadata.project_skills`), and lists each with its short version hash. Then the full transcript, following a running run every 2 s. A running run shows its last output time from the record's heartbeat and is flagged after 2 minutes with no output. While Claude thinks it shows `thinking · ~N tokens` from Claude's `thinking_tokens` events. Those arrive about once a second, so the recorder keeps one row per thinking phase and updates its count in place. Hooks, background tasks and rate limits are shown as one line each, as are a compaction (`Context compacted (auto) · 25.1k → 1.36k tokens`) and a failed compaction attempt. The result line labels Claude's figure as the `session total`. Any other event type is an expandable line. A `/new` is one line, `new session`, with its channel, sender, chat and the session it ended. For a Planner each run is one of its turns, listed above your chat with it and its activity.
- **Settings**: a folding group at the bottom of the sidebar, one page each (`#settings/<page>`): **LLM Providers** (`providers`); **Agent Runtimes** (`runtimes`, read-only, as above); **Agent Profiles** (`profiles`: each profile's runtime, provider, model and effort, "agent teams on" when they are, the roles it can run and where it is used, with a form that picks the runtime, then a provider grouped as the runtime's own login (OAuth; Claude Code's with its `claude auth status` result, Codex's with its `codex login status` result or the forked login a run found, none for OMP) or an API-key provider that speaks its protocols, then a model from the provider's catalog, or any, with the models a Codex login offers suggested, and, for a Claude Code runtime, an "Agent teams" checkbox, checked by default); **Roles** (`roles`: the system layer, then per role (`#settings/roles/<role>`) its layer, each field saying what applies now and from which layer, with the Planner's "Tasks running at once per project", the Workers' "Workers running at once per project" or the Reviewers' "Reviewers running at once per project", and its own fields: the role prompt (replace or append, reset to the recommended one), timeout, and the note that every agent has every tool with `--dangerously-skip-permissions`); **Skills & MCP** (`catalog`: how many of the imported items are loaded and their size, "Import from this machine" or "Re-import", the last import with what it changed and what it left out, a name filter, and the plugins and skills with their version, size and installs; a change applies to each agent's next run); **Channels** (`channels`: the Telegram and Lark channels, each with its sender allowlist, an add and remove list of Telegram user IDs or Lark open_ids, checked as you type, with blanks and repeats flagged, and, for an enabled channel that does not start, the reason); **Import / Export** (`transfer`). Wherever an Agent Profile is chosen (Roles, a project's settings, a new task's roles) it is the same dropdown, whose last entry, "+ New Agent Profile", opens the form in place and selects what it saves.

### Real-Time Updates

- **Activity SSE**: `GET /api/tasks/:id/activity/stream` — server-sent events from Worker subprocess
- **Task polling**: 2-second interval with auto-stop on terminal status
- **Chat SSE**: `GET /api/chat/events` — real-time chat responses

### Hash Routing

SPA uses `#page/sub` format for deep linking that survives browser refresh:
- `#tasks/task-id` → task detail
- `#projects/project-id/tab` → one project (`overview` when the tab is left out)
- `#runs/agent-id` → the role's runs (`#agents/...`, the page's name before, redirects here)
- `#settings/page[/sub]` → one Settings page (`#settings` and `#settings/defaults` redirect to `#settings/roles`)
- `#history/agent-id?task=task-id` → one agent's history

## HTTP API

The API answers only the dashboard and local clients: every agent runs Claude
with every permission, so a web page in your browser must not be able to drive it.
A request whose `Origin` is not the server's own is refused with 403 and no
response carries CORS headers. While the server listens on a loopback address,
the `Host` must name a loopback address too, which defeats DNS rebinding.
Clients without an `Origin` (curl, the CLI) are unaffected, and so is the Vite
dev proxy, which keeps `Host` and `Origin` on its own port.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/status` | GET | System health, with `checks`: the startup self-check (`looper doctor`'s), the `codex_login` one read on each request |
| `/api/projects` | GET | Projects ordered by name ignoring case, each with `task_count` and `active_count` |
| `/api/projects` | POST | Create a project from `name` (trimmed, 1–80 characters), optional `description`, `work_dir` (absolute), `background` (stored without the blank lines around it; 400 over 10000 characters) and execution settings; 409 when the name exists ignoring case; 400 naming the agent-settings endpoint for `planner_harness_prompt`, `worker_harness_prompt`, `model` or `run_budget_usd` |
| `/api/projects/:id` | PUT | Update any of `name`, `description`, `work_dir`, `background` (`""` clears it) and the execution settings; 400 (as for POST), 404, 409 |
| `/api/projects/:id/agent-settings` | GET/PUT | GET: `project.values` (the project layer), `roles` (`planner`, `worker`, `reviewer`, each with `values`, its role-in-project layer, and `effective`: every field's `value` and `source` layer, the harness with `from_role` when a Reviewer takes the Workers'), and `limits`. PUT sets the project layer; 404 for an unknown project |
| `/api/projects/:id/agent-settings/roles/:role` | PUT | Set one role's layer in the project (`planner`, `worker` or `reviewer`; 400 for `manager`) |
| `/api/projects/:id/knowledge` | GET | The project's knowledge: `documents` (`document_id`, `title`, `summary`, `source_kind`, `filename`, `path`, `read_path`, `missing`, `size_bytes`, `sha256`, timestamps; no content), `count`, `stored_bytes`, the `index` agents get and its `index_chars`, and `caps`; 404 for an unknown project |
| `/api/projects/:id/knowledge` | POST | Add a document: `source_kind`, `title`, `summary`, then `content` (text), `filename` and `content_base64` (upload) or `path` (path). Answers 201 with the new `document` and the knowledge view; 400 with the reason for a bad field, a binary file, a path outside the project's default directory, or a cap; 404 for an unknown project |
| `/api/projects/:id/knowledge/:doc_id` | GET | One document: `document` (as in the list) and its `content`: stored for text and upload, the file as it is now for a path document; `content` is `null` with a `content_error` when that file is missing or not text; 404 for an unknown project or a document of another project |
| `/api/projects/:id/knowledge/:doc_id` | PUT | Change `title`, `summary` or the source's own fields; the kind cannot change. Answers with the `document` and the view; 400 as above, 404 |
| `/api/projects/:id/knowledge/:doc_id` | DELETE | Delete a document; answers with `deleted` and the view; 404 |
| `/api/projects/:id/installs` | GET | What applies to the project: `installs`, its own installs then the global ones that reach its task roles, each an install (`install_id`, `scope`, `project_id`, `project_name`, `roles`, `enabled`) with `inherited` and its `item` (as in the catalog); `owned_count` (skills written in it), `task_roles` and `skill_caps`; 404 for an unknown project |
| `/api/projects/:id/skills` | POST | Write a skill: `name`, `description`, `content`, optionally `enabled` (default true): a catalog item installed in the project for the Planner, Workers and Reviewers. Answers 201 with the new `skill` (`skill_id`, `name`, `qualified_name` (`project:<name>`), `description`, `size_bytes`, timestamps) and the project's installs view; 400 with the reason for a bad field or the count cap, 409 for a name the project already has or that another project skill installed there has, 404 for an unknown project |
| `/api/projects/:id/skills/:skill_id` | GET | One skill the project wrote: `skill` and its `content`, the `SKILL.md` body; 404 for an unknown project or a skill another project wrote |
| `/api/projects/:id/skills/:skill_id` | PUT | Change any of `name`, `description` and `content` (its switch is on the install: `PUT /api/installs/:id`); answers with the `skill` and the installs view; 400 and 409 as above (a new name must be free in every project the skill is installed in), 404 for an unknown project or a skill another project wrote |
| `/api/projects/:id/skills/:skill_id` | DELETE | Delete a skill the project wrote, from the catalog and every project it is installed in; answers with `deleted` and the installs view; 404 |
| `/api/projects/:id` | DELETE | Delete a project; 409 while it still has tasks |
| `/api/tasks` | GET | List the newest tasks; `?project_id=` lists one project's. Each has `project_id`, `project_name`, `work_dir` and `cost` (as in the task detail) |
| `/api/tasks/create` | POST | Create a task in `init` with exactly one of `project_id` (existing) and `project` (`{name, description?, work_dir?, background?}`, created in the same transaction). Its `work_dir` is the one given, else the project's. Optional `profile_overrides`: `{planner?, worker?, reviewer?}` Agent Profile ids for this task's roles (an empty one chooses nothing). 400 with neither project field, for an override of another role, an unknown profile or one that cannot run its role; 404 for an unknown project, 409 for a taken new name |
| `/api/tasks/:id/project` | PUT | Move a task to `{project_id}`, whatever its status |
| `/api/tasks/:id` | GET | Task detail with steps, `project_id`, `project_name`, `profile_overrides` (`{role: profile}`, `{}` when none) and `cost`: `cost_usd` (the sum of its Planner's, Workers' and Reviewers' runs), `runs`, `running`, `unpriced_runs` and `by_role` (`planner`, `worker`, `reviewer`, each `{cost_usd, runs}`) |
| `/api/tasks/cancel` | POST | Cancel task (permanent) |
| `/api/tasks/stop` | POST | Stop/pause task |
| `/api/tasks/start` | POST | Start/resume task |
| `/api/tasks/delete` | POST | Delete task (must be stopped first) |
| `/api/tasks/:id/activity` | GET | Activity events |
| `/api/tasks/:id/activity/stream` | GET | SSE activity stream |
| `/api/chat/send` | POST | Send chat message; `target` is the Manager (default) or `{kind: planner, task_id}`, 409 when that task has no running Planner |
| `/api/chat/targets` | GET | The Manager, then per recent task its Planner and step Workers/Reviewers, with `can_send` |
| `/api/chat/messages` | GET/POST | Chat history |
| `/api/roles` | GET | List the role configs: `configs`, one per role, each keyed by `role` |
| `/api/roles/:role` | GET/PUT | One role's config; PUT changes the fields given. `harness_prompt`, `model` and `max_concurrency` are agent settings (400 naming the role's endpoint); `runtime` and `llm_profile_id` were removed (400); 404 for an unknown role |
| `/api/roles/:role/reset-prompt` | POST | Reset the role prompt to the recommended one |
| `/api/agent-settings` | GET | The agent settings outside projects: `system` and `roles` (all four), each with `values` (that layer's own: `harness_prompt`, `profile_id` (an Agent Profile's id), `effort`, `env` as `[{key, value}]`, `run_budget_usd`, `max_concurrency`; `null` = not set) and `effective` (what applies, each field with its `source` layer; the profile as `profile`: `{profile_id, name, model, runtime, provider}`; `effort` may come from the profile, source `profile`; the Planner, Workers and Reviewers also `max_concurrency`), and `limits` (with `builtin_profile`: `{profile_id, name}`, and `concurrency_max`) |
| `/api/agent-settings/system` | PUT | Set the system layer. Each layer's PUT changes the fields given (`null` or `""` unsets one; `env` replaces the layer's list, a `null` value unsetting a variable outside the system layer; the built-in harness text unsets the system harness) and answers with the GET view. 400 for an unknown field, `model` (part of the Agent Profile), an unknown profile or one that cannot run the layer's roles, a harness over 20000 characters, a bad effort or cap, a bad, credential-like or 51st variable, or a `max_concurrency` that is not a whole number from 1 to 64 or is on a layer other than a Planner's, Worker's or Reviewer's role or role-in-project layer; nothing is stored then. A role's or role-in-project PUT that sets `max_concurrency` then has the TaskRouter and the WorkerPool start what the new limit lets start |
| `/api/registry` | GET | The runtime types this version has (`type`, `label`, `default_executable`, `runtime_id` of its built-in runtime, `protocols`, `capabilities`, `roles`, `auth_kinds`), and every `runtime` (with its `path` on the server's PATH or `null`, and `roles`), `provider` (API key as `***` or `null`; an OAuth one with `suggested_models` and its `login`: `{state, detail}`, `state` one of `chatgpt`, `other_method`, `not_logged_in`, `unknown`, `unavailable`, for Codex's login, else `null`), each counting the profiles that use it (`profile_count`), and every Agent Profile in `profiles` (with its `profile_id`, `roles`, `used_by`: `layers` (`scope`, `role`, `project_id`, `project_name`) and `task_count`, and its `cost`: `source`, `equivalent`, `unpriced` models), the `provider_presets`, the built-in `prices` (`as_of`, `sources`, `models`), with `limits` |
| `/api/runtimes/:id/probe` | POST | Re-detect: probe again and record the version, capabilities or error; for `rt-<type>` of a type not found yet, look for it on the server's PATH and add its built-in runtime (404 when it is not there). Runtimes are built in: there is no route to register, change or delete one |
| `/api/providers` | POST | Register `{name, protocols, auth, oauth_runtime_type?, base_url?, models?, env?, max_concurrency?, api_key?}`; 201 with `provider`; 400 with the reason (an `env` name the binding sets, or that reads like a credential, too), 409 for a taken name |
| `/api/providers/:id` | PUT/DELETE | Change a provider (`api_key` `***` keeps the key, `null` removes it; 400 when one of its profiles could no longer run); delete (409 while profiles use it). 400 for the built-in one |
| `/api/providers/:id/login-problem` | DELETE | The operator checked `codex login status`: clear the forked Codex login a run recorded, so its runs start again; answers with `provider`. 400 for a provider that is not the Codex login |
| `/api/agent-profiles` | POST | Register an Agent Profile `{name, runtime_id, provider_id, model, effort?, agent_teams?}` (`agent_teams` a boolean; left out, on for a Claude Code runtime and off for another); 201 with `profile` and `warnings` (a model without a price); 400 when the runtime and provider cannot run together, the provider does not list the model, or `agent_teams` is on with a runtime other than Claude Code |
| `/api/agent-profiles/:id` | PUT/DELETE | Change a profile (checked like a new one, and for every role it is selected for; answers with `profile` and `warnings`); delete (409 while a layer or a task selects it, naming where). 400 for the built-in one |
| `/api/agent-settings/roles/:role` | PUT | Set one role's layer (`manager`, `planner`, `worker`, `reviewer`; 404 for another) |
| `/api/catalog` | GET | The catalog: `items` (`item_id`, `kind`, `name`, `qualified_name` (how agents name it), `origin`, `owner_project_id`, `owner_project_name`, `description`, `source`, `version`, `sha256`, `size_bytes`, `file_count`, timestamps and `installs`), `installed_count`, `total_bytes`, `last_import` (counts, `added`, `updated`, `removed`, `unchanged`, `skipped`), `roles`, `task_roles` and `caps` |
| `/api/catalog/:id` | GET | One item: `item` (as in the list), `files` (`path`, `size_bytes`, `executable`; no content) and, for a skill, `skill_md`; 404 for an unknown item |
| `/api/catalog/import` | POST | Import or re-import this machine's enabled plugins and personal skills (read-only towards `~/.claude`); new items are installed nowhere. Answers with the catalog. 400 without a Claude Code configuration, with an unreadable `settings.json` or over the 128 MiB cap (nothing changes then) |
| `/api/catalog/upload` | POST | Add an uploaded `SKILL.md` or zip: `{filename, content_base64, name?}` (`name` renames it). Answers 201 with `item` and `skipped` (binary files left out); 400 with the reason for an unsafe or invalid zip or `SKILL.md`, 409 for a name taken outside projects |
| `/api/catalog/git/preview` | POST | Clone `{url, ref?, subpath?}` and list its plugins and skills: `commit` and `items` (`path`, `kind`, `name`, `description`, size, `status`: `new`, `update`, `unchanged` or `conflict` with `reason`), `skipped`. Adds nothing; 400 for a URL that is not plain `https://`, a failed, oversized or slow clone |
| `/api/catalog/git/import` | POST | `{url, ref?, subpath?, commit?, items: [{path, name?}]}`: import the chosen items (origin `git`). Answers with `items`, `added`, `updated`, `unchanged`; 409 for a taken name or a ref that moved from `commit`, 400 for an unknown or invalid path |
| `/api/catalog/skills` | POST | Write a skill in Settings: `{name, description, content}` (origin `ui`); 201 with `item`, 400, 409 |
| `/api/catalog/:id` | PUT | Change an MCP server, or a Settings, uploaded or git skill (`name`, `description`, `content`); answers with `item`; 400 for a machine or project item or a plugin, 409 for a taken name |
| `/api/catalog/:id` | DELETE | Delete an item and its installs (not a project's own skill) |
| `/api/installs` | POST | Install an item: `{item_id, scope: "global"\|"project", project_id (a project scope), roles?, enabled?}`; `roles` defaults to every role the scope allows. Answers 201 with `install` and `item`. 400 for a bad field, `manager` in a project install or a project-authored item installed globally; 404 for an unknown item or project; 409 when the item is installed there already or a project skill of its name is |
| `/api/installs/:id` | PUT | Change an install's `roles` or `enabled`; answers with `install` and `item`; 400 as above, 404 for an unknown install |
| `/api/installs/:id` | DELETE | Uninstall; the item stays in the catalog. Answers with `deleted` and `item`; 404 |
| `/api/llm-records` | GET | Claude runs; filter by `agent_id`, `task_id` or `role` (`manager`, `planner`, `worker`, `reviewer`) |
| `/api/llm-records/:id` | GET | One run (status, usage, cost, launch command, heartbeat) |
| `/api/llm-records/:id/events?after=N` | GET | The run's stream-json events after seq N |
| `/api/agents/:id/usage?turns=N` | GET | One agent's (`manager`, `planner-<task_id>`, ...) spending and context: `total` (`cost_usd`, `runs`, `running`, `unpriced_runs`, `compactions`), `by_session` and `by_day` (UTC; each `cost_usd`, `runs`), its latest N turns (default 100, at most 1000) oldest first with `context_tokens`, `cost_usd` and `compactions`, and `latest_context_tokens`; 400 for a bad `turns` |
| `/api/channel-configs` | GET | List channels, each with `start_problem`: why it does not start as configured (an empty or invalid sender allowlist), else null |
| `/api/channel-configs/:id` | GET/PUT | Channel config. PUT validates the sender allowlist (`allowed_user_ids` for Telegram, `allowed_open_ids` for Lark) and stores it as a list; 400 naming the first bad id, and nothing is stored then; 404 for an unknown channel |

## Configuration

Agent behavior is configured via the `role_configs` table (Settings → Roles); the harness, Agent Profile,
effort, environment and per-run cap are agent settings ([Harness, profile and environment](#harness-profile-and-environment)),
and so are the [concurrency limits](#concurrency-limits):

- **system_prompt**: The role prompt: what this kind of agent is responsible for
- **max_turns**: Maximum conversation turns (0 = unlimited)
- **timeout_seconds**: Execution timeout. For workers it limits one Claude
  run's total time (default 86400, 0 = no limit). Workers read
  `timeout_seconds` and `max_turns` for every run, so an edit applies to the
  next step

The `planner` row configures every task's Planner and is read for every turn.
`system_prompt` is the editable role prompt; code appends the reply contract
(`REPLY_CONTRACT`). `timeout_seconds` bounds each turn (default 1200, 0 = no
limit): a turn over it fails and its Claude is killed. `max_turns` above 0
passes `--max-turns`, as for Workers. A change that alters the Planner's command restarts its Claude at
the next turn with `--resume`. Like every agent, the Planner has every tool
and no permission prompts (`--dangerously-skip-permissions`).

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

The Web channel is always on. Telegram and Lark are built in; `looper serve`
takes no flags for them. At startup it reads `channel_configs`, which
Settings → Channels edits, and starts each enabled channel. Changes apply after a
restart.

- **Sender allowlist** (`looper/agents/channels/allowlist.py`, RFC decision 8):
  the Manager runs any command it is asked to, so Telegram and Lark pass on
  only the messages of the senders their configuration lists. A message from
  anyone else never reaches the Manager: the adapter logs one line with the
  channel and the sender id (never the text) and sends no reply. An enabled
  channel whose list is empty or invalid does not start; the startup log and
  Settings → Channels (`start_problem`) say why. The list holds at most 100
  ids; the API checks each one and stores the list as a JSON array.
- **Telegram**: the token comes from the env var named by `bot_token_env`
  (default `LOOPER_TELEGRAM_BOT_TOKEN`). `allowed_user_ids` lists numeric
  Telegram user IDs (a negative id is a group or channel, not a sender); a
  comma-separated string from before is still read.
  `LOOPX_TELEGRAM_ALLOWED_USERS` no longer applies. `/new` (without
  arguments) goes to the Manager as `/new`. `/start`, `/status`, `/new` and
  `/help` from an unlisted sender get no answer either.
- **Lark**: needs `lark-cli` on PATH and signed in. The app ID comes from the
  env var named by `app_id_env` (default `LARK_APP_ID`). `allowed_open_ids`
  lists the senders' `open_id` (`ou_...`), the id each message event carries
  in `event.sender.sender_id.open_id`; a `union_id` or `user_id` is refused.
- **Web**: unaffected, it has no list: the HTTP API answers only local
  requests ([HTTP API](#http-api)).
- A channel that fails to start is logged and skipped; the server keeps
  running.
