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
  `claude --print --verbose --output-format stream-json --input-format stream-json --include-partial-messages --replay-user-messages <isolation flags> --append-system-prompt-file <file> --system-prompt-snapshot off --dangerously-skip-permissions --model M [--effort E] [--plugin-dir <dir> ...] [--resume <session_id>]`
  (see [What an agent's Claude Code loads](#what-an-agents-claude-code-loads),
  [What an agent may do](#what-an-agent-may-do) and
  [The library](#the-library-imported-plugins-and-skills)). Like every agent
  it has every tool and no permission prompts, skills included, so it can run
  any command on this machine; that is why Telegram and Lark pass on only the
  senders they list ([Channels](#channels)).
  Its system prompt is its harness (its own `harness_prompt`, else the default
  in Settings), the `manager` role prompt and `TASK_PLACEMENT_RULE`; it has no
  project and no task section.
  Each chat message is one user turn on its stdin; its turn starts when
  Claude starts that message and ends at the next `result`
  ([Turns Claude runs on its own](#turns-claude-runs-on-its-own)). Messages
  are handled one at a time, and each turn is one Runs record holding only
  that turn's cost and tokens
  ([What a run spent](#what-a-run-spent-and-how-large-its-context-grew)); the
  session's running totals are saved beside its id in
  `.local/manager/session.json` (`session_totals`). When Claude exits
  (or dies mid-turn, which answers that message with an error) or the
  Manager's system prompt, model, effort, Settings environment or library
  changes, the next message starts a new process that
  resumes the saved session with `--resume`. Stopping the Manager closes
  Claude's stdin and kills it if it is still running after 5 s
- A turn its Claude runs on its own, after background work it started ends,
  is a Runs record of its own (input "(no user message: ...)", metadata
  `autonomous: true`). What Claude said in it goes as a reply of its own
  (`AGENT_REPLY` with no `reply_to`, payload `autonomous: true`) to the
  channel and correlation of the message Claude answered before it. A web
  message's payload names its chat session (`chat_session_id`), so the web
  channel adds the reply to that chat's history (`chat_messages`, shown when
  the chat is opened) instead of a message's stream; Lark and Telegram send it
  to the chat. With no message before it, the reply is only recorded

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

  ## Project knowledge                            L3 (left out without documents): an index,
  - <title>: <summary> (read: <path>)                 one line per document

  ## Task                                         L4: what this session is for
  ...                                                 (Planner: the task; Worker, Reviewer: the step)

  <code-owned contract>                           L5: REPLY_CONTRACT, WORK_CONTRACT or REVIEW_CONTRACT; always last
  ```

  A project can also carry knowledge: documents its task agents read on
  demand instead of getting them in the prompt ([Project
  knowledge](#project-knowledge-documents-read-on-demand)). Only their index
  is in L3. It can carry its own Claude Code skills too ([Project
  skills](#project-skills-claude-code-skills-of-one-project)); they are not
  in the prompt: Claude Code lists them itself.

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
  `claude --print --verbose --output-format stream-json --input-format stream-json --include-partial-messages --replay-user-messages <isolation flags> --append-system-prompt-file <file> --system-prompt-snapshot off --dangerously-skip-permissions --model M [--effort E] [--plugin-dir <dir> ...] [--add-dir <dir> ...] [--max-turns N] [--resume <session_id>]`.
  The system prompt is rebuilt for every turn from the harness (the project's
  Planner override, else the default in Settings), the `planner` role prompt,
  the task's project, the task section (task id, project name and
  description, working directory and the requirement) and `REPLY_CONTRACT`;
  the project is read before each event's turns. The turns themselves carry
  only what changed: the plan's progress, results, chat and supervision
  observations. When the prompt changes (a config edit, a new background,
  knowledge or harness, a task moved to another project) its file and hash
  change, or the model, effort, Settings environment, library, project
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
    with `--resume <session_id>`; the session id and the session's running
    totals (`session_totals`) are kept in the Planner's plan file under
    `.local/planner/`.
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

### WorkerPool (`loopx/agents/tiers/worker.py`)

- Manages a pool of Worker processes
- Each Worker runs Claude CLI as a subprocess with `stream-json` output
- Real-time event streaming (tool_use, text) captured to activity table
- Command: `claude --print --verbose --output-format stream-json --include-partial-messages --input-format stream-json --replay-user-messages <isolation flags> --append-system-prompt-file <file> --system-prompt-snapshot off --dangerously-skip-permissions --model M [--effort E] [--plugin-dir <dir> ...] [--add-dir <dir> ...]`,
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
Claude Code settings, and the plugins and skills come from the LoopX library
(below), never from `~/.claude` itself.

### What an agent may do

Every agent (Manager, Planner, Worker, Reviewer) has every tool and runs
without permission prompts (RFC decision 8). `permission_args()` in
`loopx/agents/claude_cli.py` is `--dangerously-skip-permissions`, and
`agent_settings.Launch.args` puts it in front of every agent's launch flags,
beside the library's, so the four commands cannot diverge. No command
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
restart signature is its command (which carries the model, the effort, the
library's plugin directories, the project skills' plugin directory, named by
their hash, and the knowledge's `--add-dir` directories),
the prompt's sha256, the environment's sha256, the library's sha256 and, for
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
sha256s of the Settings environment and of the library.

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
- `model_choice`: `{model, source, effort}`, the model asked for (`source` is
  `builtin_default`, `system_default` or `agent_override`); the record's
  `model` column keeps the model Claude reports;
- `env`: `{sha256, keys: {NAME: sha256 of the value}}`, only when Settings
  holds variables; values are never recorded;
- `library`: `{sha256, items: [{kind, name, sha256}], error?}`, the library
  items the run loaded and the hash of that set, only when something was to be
  loaded; `error` says why a run went without its library.
- `project_skills`: `{project_id, sha256, plugin, skills: [{name, sha256}],
  error?}`, a task agent's project skills: the set's hash (which names its
  directory), the plugin (`project`) and each skill's name and the hash of its
  `SKILL.md`, only when the project has enabled skills; `error` says why a run
  went without them. The Manager's runs never have it.

The same inputs give the same prompt and hash; changing the harness, the role
prompt, the background, the knowledge index, the task or a contract changes
it.

### Project knowledge: documents read on demand

A project can carry reference material too large for a prompt
(`loopx/agents/knowledge.py`, RFC section 6 and decision 3). Each document
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
cannot both pass a cap. An index line carries an absolute path, about 150
characters under `~/.loopx`, so in practice the index cap admits roughly 15
to 25 documents, well before the count cap.

**Delivery.** `load_knowledge(runtime_root, project, work_dir)` runs for every
Worker and Reviewer run and every Planner turn, never for the Manager:

- each text or upload document is materialized read-only (0444), named by its
  content's hash, at
  `<runtime_root>/.local/projects/<project_id>/knowledge/<sha256>/<filename>`
  (a text document's file name comes from its title, an upload's from its
  file name, both reduced to letters, digits, `.`, `-` and `_`). A copy that
  already matches its hash is reused and a damaged one is rewritten;
- the project layer gains `## Project knowledge` after the background: an
  introduction, then `- <title>: <summary> (read: <absolute path>)` per
  document; a path document whose file is gone or leads out of the directory
  is listed as `(missing: <path>)`. An index that grew past the cap after it
  was saved (a longer default directory, say) is clipped with a note;
- the launch mounts the knowledge directory with `--add-dir` when the project
  has a text or upload document, and the project's default directory when it
  has a path document and the task runs in another directory.
  `claude_cli.knowledge_args` builds the flags and `agent_settings.Launch`
  carries them beside the library's, so the three task agents cannot diverge
  and the Manager's launch has none.

Edits apply at once: the next Worker or Reviewer reads them as it starts, and
the Planner's next turn sees a new index (or new directories) and restarts
with `--resume`. A path document is read when an agent opens it, so editing
the file needs no restart. The task's activity notes `Project knowledge
applied (N documents)` for each run that has some, and, for the Planner,
`changed` or `removed`. When the documents cannot be read or materialized a
run goes without them, its record's `prompt_layers.project.knowledge_error`
says why and the activity logs a warning; the Planner keeps the knowledge it
last read. A deleted document's copy stays on disk, since a running agent may
be reading it.

Verified with Claude Code 2.1.285 under the isolation flags: the agent reads
files in an `--add-dir` directory, a `CLAUDE.md` in that directory or above it
is not loaded, and a directory that does not exist does not fail the run.

### Project skills: Claude Code skills of one project

A project can carry its own Claude Code skills (`loopx/agents/project_skills.py`,
RFC section 6). Only its Planner, Workers and Reviewers load them, and nothing
is written into the project's directory. Each skill is a row of
`project_skills`:

- `name`: a lowercase slug (`deploy-check`), unique in the project. It is the
  skill's directory and its frontmatter `name`; a CHECK holds the database to
  the same pattern;
- `description`: one line, which Claude Code shows the model to decide when to
  use the skill;
- `content`: the body of its `SKILL.md`, given without frontmatter (content
  starting with a `---` line is refused, since the fields make it);
- `enabled`: only enabled skills are loaded.

Rows go with their project.

**Caps.** Each is refused with a 400 that states it:

- a name: at most 64 characters, Claude Code's limit for a skill name;
- a description: at most 1,024 characters, the limit of Claude Code's skill
  format. The longest of 104 real personal skills has 975;
- a body: at most 64 KiB. It enters the context whenever the skill is used.
  The largest of those 104 is 61 KB, and long reference material belongs in
  the project's knowledge;
- a project: at most 20 skills. Every enabled skill's name and description is
  listed in every run's context.

A name the project already has is a 409. Changes to one project's skills run
in a transaction that holds the project's row lock, as its knowledge does, so
two concurrent adds cannot both pass the count cap or take one name.

**Delivery.** `load_project_skills(runtime_root, project)` runs for every
Worker and Reviewer run and every Planner turn, never for the Manager. It reads
the project's enabled skills and writes them as one plugin:

```
<runtime_root>/.local/projects/<project_id>/skills/<sha256>/project/
  .claude-plugin/plugin.json      {"name": "project", ...}
  skills/<name>/SKILL.md          frontmatter (name, description) from the fields, then the body
```

- `<sha256>` names the set of skills and the layout. The directory is written
  once through the library's `write_once`: built beside its place, renamed in,
  then read-only (files 0444, directories 0555). The same set reuses it, and a
  change makes a new one.
- The description is written as a double-quoted string, so a colon, a quote
  or a `#` stays text.
- `claude_cli.project_skill_args` gives the run one `--plugin-dir`, after the
  library's plugins and before the knowledge's `--add-dir`.
  `agent_settings.Launch` carries it, so the three task agents cannot diverge
  and the Manager's launch has none.
- Claude Code names each skill `project:<name>`. It lists them to the model
  itself (the Skill tool), so the system prompt does not repeat them.

Edits apply at once. The next Worker or Reviewer reads them as it starts. The
Planner's next turn sees a new plugin directory in its command and restarts
with `--resume`, and a task moved to another project gets that project's
skills the same way. The task's activity notes `Project skills applied (N
skills)` for each run that has some, and, for the Planner, `changed` or
`removed`. When the skills cannot be read or written, a run goes without them,
its record's `project_skills.error` says why and the activity logs a warning.
The Planner keeps the skills it last read. A replaced or deleted set's
directory stays on disk, since a running agent may be using it.

Verified with Claude Code 2.1.285 under the isolation flags and
`--dangerously-skip-permissions`, with haiku:

- the init event lists the plugin's skills as `project:<name>`, and its
  `plugins` as `{name: "project", source: "project@inline"}`;
- the model invokes such a skill with the Skill tool from the read-only copy;
- two `--plugin-dir` plugins of the same name both load, their skills listed
  under one prefix, so a library plugin named `project` would share it;
- no `~/.claude/plugins/data/project-inline/` directory was created for a
  plugin with only skills.

### The library: imported plugins and skills

The capabilities the operator wants every agent to have are imported
explicitly instead of being loaded from `~/.claude`
(`loopx/agents/library.py`, RFC decision 7):

- **Import** (`POST /api/settings/library/import`, Settings → "Import from this
  machine" / "Re-import") only reads the operator's Claude Code configuration
  (`$CLAUDE_CONFIG_DIR`, else `~/.claude`): every plugin `settings.json`
  enables (`enabledPlugins`), from its user-scope install in
  `plugins/installed_plugins.json`, taken whole (its skills, agents,
  commands and hooks); and every personal skill, `skills/<name>/` with a
  `SKILL.md` (a linked directory is followed). Personal settings and hooks,
  MCP servers, permissions and memory are not read. `.git`, `.in_use`
  (Claude Code's plugin-cache bookkeeping), `.DS_Store` and `__pycache__` are
  left out; a link inside an item is read as its target when it stays in the
  item.
- **Storage.** The database is the source of truth: `library_items` (kind
  `plugin` or `skill`, name, source label, declared version, `sha256`,
  `enabled`, size, file count, `imported_at`) and `library_files` (relative
  path, UTF-8 content, sha256, executable bit). An item's `sha256` is the hash
  of its files' paths, hashes and executable bits. A re-import replaces the
  files of an item whose hash changed and keeps its id and `enabled` flag,
  leaves an unchanged one as it is, adds new items enabled and removes the
  items that are gone. The last import's report (counts, changes, what was
  left out) is the `library.last_import` row of `system_settings`.
- **Caps.** A file over 2 MiB, a binary (not UTF-8) file, and a link out of
  its item are left out; an item over 32 MiB or 5,000 files is left out
  whole; all are reported. A library over 128 MiB is refused with a 400 and
  nothing changes. Measured on a real configuration (3 plugins, 104 skills):
  about 21 MB of text in 2,200 files, the largest item 9 MB, the largest file
  0.7 MB; 4 image files were left out.
- **Launch.** `load_library(runtime_root)` reads the enabled items for every
  run and gets each a read-only copy under
  `<runtime_root>/.local/library/<sha256>/<name>/`, written once from the
  database (checked against the hashes) and renamed into place, so runs never
  read `~/.claude`. Each enabled plugin is one `--plugin-dir`; the enabled
  skills are linked into one generated plugin, `personal`
  (`<runtime_root>/.local/library/<hash of the set>/personal/`), so Claude Code
  names them `personal:<skill>`. `claude_cli.library_args` builds the flags
  and `agent_settings.Launch` carries them, so all four commands get the
  same set. When the library cannot be read or written, the run goes without
  it and its record's `library.error` says why.

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

### What a run spent and how large its context grew

Every run records its own cost and tokens, its context at the end and the
context compactions it went through (`loopx/agents/run_usage.py`; RFC §8, M2).
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
  resumes a session starts from the totals saved beside its id: the
  Manager's `session.json` and the Planner's plan file. The saved totals are
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
| `llm_records` | One row per Claude run: input and output text, status, the run's own `cost_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens` and `cache_creation_tokens`, its final `context_tokens` and its `compactions`; `metadata` holds the exact argv (`command`), `cwd`, the appended `system_prompt`, its `system_prompt_sha256`, its `prompt_layers`, the `model_choice`, the Settings `env` names, the `library` items loaded and a task agent's `project_skills` |
| `agent_configs` | Agent configuration (role prompt, model) for `manager`, `planner`, `worker` and `reviewer`; `harness_prompt` is used for the Manager only; `permission_mode` is stored but not applied |
| `system_settings` | Settings shared by the whole system, one value per key: the default harness, model, effort and environment variables (no row = the built-in default), and the last library import's report |
| `library_items` | The imported plugins and skills: kind, name, source label, declared version, `sha256` of the files, `enabled`, size, file count, `imported_at` |
| `library_files` | Each library item's files: relative path, UTF-8 content, sha256, executable bit |
| `project_skills` | A project's own Claude Code skills: `name` (a lowercase slug, unique in the project; a CHECK holds the pattern), one-line `description`, `content` (the `SKILL.md` body), `enabled`, timestamps; the rows go with their project |
| `project_documents` | A project's knowledge: title, one-line summary, `source_kind` (`text`, `upload` or `path`), `filename` and `content` (text and upload) or `path` (relative to the project's default directory), `size_bytes`, `sha256`; a CHECK keeps each kind's columns consistent and the rows go with their project |
| `llm_profiles` | LLM provider profiles (model, endpoint, API key env) |
| `channel_configs` | Telegram/Lark settings (enabled flag, env var names, sender allowlist) |

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
- **Tasks**: Task list with project and status filters, time display, each task's cost so far, Start/Stop/Delete controls. `#tasks?project=<id>` filters by a project and shows its name, description, default directory and background (three lines, then Show more; "No background" when empty), with Edit, and Delete while it has no tasks. Edit has a monospace background field with a live `n / 10000` counter and a collapsible "Harness overrides" section with the Planner and Worker harness (each `n / 20000`, or "Inherits the default" when empty); Save is disabled over a limit. Below Save, a Knowledge section lists the project's documents (title, summary, source, file name or path, size, a Missing badge for a path document whose file is gone) with a readout of documents, stored bytes and index characters against their caps, adds one as Text (title, summary, content), Upload (a file, read as base64) or Project file (a path), and deletes one after a confirmation; these apply at once, apart from Save, and a refusal shows the server's reason. Below Knowledge, a Skills section lists the project's skills (`project:<name>`, the body's size, `Off` for a disabled one, the description) with a count of skills and how many are on against the cap; it adds one (name, description, the instructions of its `SKILL.md`), switches one on or off, and deletes one after a confirmation, applying at once like Knowledge. The bar lists the harness overrides a project has. A new task picks an existing project or creates one in the same form; its work directory may stay empty when the project has a default. A task's detail view can move it to another project in any status.
- **Projects (sidebar)**: Below the navigation, every project with its task count (or a spinner with its active count); clicking one opens the Tasks page filtered by it, and `+` creates a project (name, default directory, description, optional background and harness overrides).
- **Task Detail**: Overview (status, phase, times, steps and the task's cost with its run count, then "Cost by agent": the Planner's, the Workers' and the Reviewers' runs and cost with each one's share, noting runs still running or without a reported cost; steps, agent timeline, live activity feed), Pipeline DAG (React Flow), Sessions, Conversation
- **Agents**: The Manager, Planner, Worker and Reviewer configuration editor (role prompt replace/append, model, LLM profiles). Instead of a permission setting it states the permissions every agent shares: every tool, `--dangerously-skip-permissions`. The model field's placeholder is the default it inherits. The Manager also has a "Harness prompt (override)" field; the others say where their harness comes from. A Runs tab lists every recorded Claude run of the role (the Manager, every task's Planner, Workers or Reviewers)
- **Agent history** (`#history/<agent-id>?task=<task-id>`): opened from History on an Agent Timeline row, a Runs entry or the Chat page. The Manager's and a Planner's page starts with "Context & cost": the latest context size, the number of compactions, the total cost and the number of turns. Below them is a sparkline of the context at the end of each of the latest 100 turns, scaled between their lowest and highest value, with a dashed marker on each turn that compacted. A list of the compactions follows (turn, time, trigger, context before → after), then a folded table of the turns. The section is re-read every 10 s. For each Claude run it shows status, model, the run's own tokens (uncached input, cache read, cache write, output), its context at the end (`ctx`), a `compacted` badge, its own cost, the launch command (cwd and argv; a long argument such as the system prompt is shown by its length, and Copy copies the exact command), a collapsible System prompt section with what LoopX appended (Claude Code's built-in system prompt is not included): its title shows the prompt's size, its short sha256 and an `isolated` badge, and inside one line per layer (harness source and version, role recommended or edited, project and background version, task, contract), the model with its source and effort, and the Settings environment variable names, above the full text; runs recorded before the layers existed show the text only. A collapsible Plugins & skills section shows how many plugins and skills the run loaded and the library hash, and lists each with its short version hash. Then the full transcript, following a running run every 2 s. A running run shows its last output time from the record's heartbeat and is flagged after 2 minutes with no output. While Claude thinks it shows `thinking · ~N tokens` from Claude's `thinking_tokens` events. Those arrive about once a second, so the recorder keeps one row per thinking phase and updates its count in place. Hooks, background tasks and rate limits are shown as one line each, as are a compaction (`Context compacted (auto) · 25.1k → 1.36k tokens`) and a failed compaction attempt. The result line labels Claude's figure as the `session total`. Any other event type is an expandable line. For a Planner each run is one of its turns, listed above your chat with it and its activity.
- **Settings**: Agent defaults (the default harness prompt with an `n / 20000` counter and Reset to built-in, the default model, the effort and the environment variables), then Plugins & skills (how many of the imported items are loaded and their size, "Import from this machine" or "Re-import", the last import with what it changed and what it left out, a name filter, and the plugins and skills with their version, size and a switch each; a change applies to each agent's next run), then the Telegram and Lark channels, each with its sender allowlist (an add and remove list of Telegram user IDs or Lark open_ids, checked as you type, with blanks and repeats flagged) and, for an enabled channel that does not start, the reason

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

The API answers only the dashboard and local clients: every agent runs Claude
with every permission, so a web page in your browser must not be able to drive it.
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
| `/api/projects/:id/knowledge` | GET | The project's knowledge: `documents` (`document_id`, `title`, `summary`, `source_kind`, `filename`, `path`, `read_path`, `missing`, `size_bytes`, `sha256`, timestamps; no content), `count`, `stored_bytes`, the `index` agents get and its `index_chars`, and `caps`; 404 for an unknown project |
| `/api/projects/:id/knowledge` | POST | Add a document: `source_kind`, `title`, `summary`, then `content` (text), `filename` and `content_base64` (upload) or `path` (path). Answers 201 with the new `document` and the knowledge view; 400 with the reason for a bad field, a binary file, a path outside the project's default directory, or a cap; 404 for an unknown project |
| `/api/projects/:id/knowledge/:doc_id` | PUT | Change `title`, `summary` or the source's own fields; the kind cannot change. Answers with the `document` and the view; 400 as above, 404 |
| `/api/projects/:id/knowledge/:doc_id` | DELETE | Delete a document; answers with `deleted` and the view; 404 |
| `/api/projects/:id/skills` | GET | The project's skills: `skills` (`skill_id`, `name`, `qualified_name` (`project:<name>`), `description`, `enabled`, `size_bytes` of the body, timestamps; no content), `count`, `enabled_count`, `plugin` and `caps`; 404 for an unknown project |
| `/api/projects/:id/skills` | POST | Add a skill: `name`, `description`, `content`, optionally `enabled` (default true). Answers 201 with the new `skill` and the view; 400 with the reason for a bad field or the count cap, 409 for a name the project already has, 404 for an unknown project |
| `/api/projects/:id/skills/:skill_id` | PUT | Change any of `name`, `description`, `content` and `enabled` (the switch); answers with the `skill` and the view; 400 and 409 as above, 404 for an unknown project or a skill of another project |
| `/api/projects/:id/skills/:skill_id` | DELETE | Delete a skill; answers with `deleted` and the view; 404 |
| `/api/projects/:id` | DELETE | Delete a project; 409 while it still has tasks |
| `/api/tasks` | GET | List the newest tasks; `?project_id=` lists one project's. Each has `project_id`, `project_name`, `work_dir` and `cost` (as in the task detail) |
| `/api/tasks/create` | POST | Create a task in `init` with exactly one of `project_id` (existing) and `project` (`{name, description?, work_dir?, background?, planner_harness_prompt?, worker_harness_prompt?}`, created in the same transaction). Its `work_dir` is the one given, else the project's; 400 with neither, 404 for an unknown project, 409 for a taken new name |
| `/api/tasks/:id/project` | PUT | Move a task to `{project_id}`, whatever its status |
| `/api/tasks/:id` | GET | Task detail with steps, `project_id`, `project_name` and `cost`: `cost_usd` (the sum of its Planner's, Workers' and Reviewers' runs), `runs`, `running`, `unpriced_runs` and `by_role` (`planner`, `worker`, `reviewer`, each `{cost_usd, runs}`) |
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
| `/api/settings/library` | GET | The library: `items` (`id`, `kind`, `name`, `source`, `version`, `sha256`, `enabled`, `size_bytes`, `file_count`, `imported_at`), `last_import` (counts, `added`, `updated`, `removed`, `unchanged`, `skipped`), `enabled_count`, `library_sha256` (what runs record), `total_bytes`, `skills_plugin` and `caps` |
| `/api/settings/library/import` | POST | Import or re-import this machine's enabled plugins and personal skills (read-only towards `~/.claude`); answers with the library. 400 without a Claude Code configuration, with an unreadable `settings.json` or over the 128 MiB cap (nothing changes then) |
| `/api/settings/library/:id` | PUT | `{"enabled": true\|false}`; answers with the library. 400 for any other body, 404 for an unknown item |
| `/api/llm-profiles` | GET/POST | LLM profiles |
| `/api/llm-records` | GET | Claude runs; filter by `agent_id`, `task_id` or `role` (`manager`, `planner`, `worker`, `reviewer`) |
| `/api/llm-records/:id` | GET | One run (status, usage, cost, launch command, heartbeat) |
| `/api/llm-records/:id/events?after=N` | GET | The run's stream-json events after seq N |
| `/api/agents/:id/usage?turns=N` | GET | One agent's (`manager`, `planner-<task_id>`, ...) spending and context: `total` (`cost_usd`, `runs`, `running`, `unpriced_runs`, `compactions`), `by_session` and `by_day` (UTC; each `cost_usd`, `runs`), its latest N turns (default 100, at most 1000) oldest first with `context_tokens`, `cost_usd` and `compactions`, and `latest_context_tokens`; 400 for a bad `turns` |
| `/api/channel-configs` | GET | List channels, each with `start_problem`: why it does not start as configured (an empty or invalid sender allowlist), else null |
| `/api/channel-configs/:id` | GET/PUT | Channel config. PUT validates the sender allowlist (`allowed_user_ids` for Telegram, `allowed_open_ids` for Lark) and stores it as a list; 400 naming the first bad id, and nothing is stored then; 404 for an unknown channel |

## Configuration

Agent behavior is configured via the `agent_configs` table (editable in the dashboard):

- **system_prompt**: The role prompt: what this kind of agent is responsible for
- **harness_prompt**: The Manager's own harness (`''` = the default in Settings)
- **runtime**: stored per agent; only `claude-code` is implemented, and the
  Manager, Planners and Workers always run the Claude CLI
- **model**: This agent's model (`--model`); empty uses the default model in Settings (`opus` built in)
- **permission_mode**: stored but not applied: every agent runs with `--dangerously-skip-permissions`
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
the next turn with `--resume`. `permission_mode` does not apply: like every
agent, the Planner has every tool and no permission prompts.

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

- **Sender allowlist** (`loopx/agents/channels/allowlist.py`, RFC decision 8):
  the Manager runs any command it is asked to, so Telegram and Lark pass on
  only the messages of the senders their configuration lists. A message from
  anyone else never reaches the Manager: the adapter logs one line with the
  channel and the sender id (never the text) and sends no reply. An enabled
  channel whose list is empty or invalid does not start; the startup log and
  the Settings page (`start_problem`) say why. The list holds at most 100
  ids; the API checks each one and stores the list as a JSON array.
- **Telegram**: the token comes from the env var named by `bot_token_env`
  (default `LOOPX_TELEGRAM_BOT_TOKEN`). `allowed_user_ids` lists numeric
  Telegram user IDs (a negative id is a group or channel, not a sender); a
  comma-separated string from before is still read.
  `LOOPX_TELEGRAM_ALLOWED_USERS` no longer applies. `/start`, `/status` and
  `/help` from an unlisted sender get no answer either.
- **Lark**: needs `lark-cli` on PATH and signed in. The app ID comes from the
  env var named by `app_id_env` (default `LARK_APP_ID`). `allowed_open_ids`
  lists the senders' `open_id` (`ou_...`), the id each message event carries
  in `event.sender.sender_id.open_id`; a `union_id` or `user_id` is refused.
- **Web**: unaffected, it has no list: the HTTP API answers only local
  requests ([HTTP API](#http-api)).
- A channel that fails to start is logged and skipped; the server keeps
  running.
