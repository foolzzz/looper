# Agent System Architecture

The Looper agent system is a three-tier multi-agent architecture with a web
dashboard for task management and real-time monitoring.

## Tiers

```
User → Chat/Lark/Telegram → Manager → TaskRouter → Planner → WorkerPool → Claude CLI
```

### Manager (`looper/agents/tiers/manager.py`)

- Entry point for all user messages. There is a default Manager (`manager`)
  and the operator's scenario Managers (`manager-<key>`, table `managers`,
  [RFC](rfcs/scenario-managers.zh-CN.md)), each a `ManagerAgent` with its own
  session (the `agent_sessions` row whose id is its id), runs
  (`llm_records.agent_id`), turn binding file and `ManagerOps`.
  `ManagerFleet` (`looper/agents/managers.py`) runs the enabled ones, at most
  8 at once. A scenario Manager has its own system prompt, Agent Profile and
  [task templates](#task-templates), and can have its own Telegram or Lark bots
  ([Channels](#channels)). What follows holds for each Manager unless it says
  otherwise
- Acts on Looper only through its platform tools
  ([The Manager's platform tools](#the-managers-platform-tools)): the MCP
  server `looper` is in every launch of its Claude, and each turn tells
  `ManagerOps` which chat and message the calls belong to. Every user turn
  starts with a `Projects` section listing the existing projects (name,
  default directory or "no default directory", description cut to 80
  characters) and, when there are any, a `Ready tasks` section: the 10 newest
  tasks created but not started (id, project, goal, requirement cut to 80
  characters), and an `Open gates` section: the 10 user gates opened last
  that wait for the user in a running task (task, project, gate and topic, how
  long it is open, whom it waits for, its open questions cut to 80
  characters; [User gates](#user-gates)); in a Lark or Telegram chat only the
  gates whose messages go to that chat ("Open gates of this chat"), in the
  dashboard chat all, each with its chat. The code-owned `TOOLS_CONTRACT`, appended to the editable
  system prompt, says how to use the tools: read freely; change things only
  when the user's message asks, in the turn answering it; never ask for, take
  or repeat a key, token or password; before creating a
  task, know its project (never presented as decided before the user chose
  it) and its directory (never picked by Claude); a task under way gets what
  the user adds as a supplement; the user's answer to an open gate goes to it
  with `looper_task_gate_reply`, which sends the user's own message (asking
  first which gate when it could be more than one), and nothing else does; refer to what Looper writes itself instead
  of repeating it; and never say an operation ran unless its answer was `ok`
- The reply is Claude's words, then what Looper itself writes to the user in
  that turn (`Turn.blocks`): a new task's configuration with the question
  whether to start it, and what an operation needing confirmation does. A
  turn that fails still shows what a tool did before it failed
- A new task is named by the tool's required `title`: a few words in the
  user's language, stored in `tasks.title` (one line, at most 60
  characters). `db.tasks.task_name` is what lists, headings, the Chat page and
  the end notice show: the title, else the goal id, else the requirement's
  first line, else the task id (a task from before titles)
- The dashboard chat, Lark and Telegram share the default Manager's one session (a scenario Manager's session serves its own dashboard chat threads), so
  every message ends with a code-written line saying where it came from, just
  before the message: `Channel: the Looper dashboard chat`, `Channel: a Lark
  direct message`, `Channel: a Lark group chat, from <sender>` (the name the
  allowlist resolves, else the open_id), or for Telegram `a Telegram direct
  message` (a group's chat id is negative: `a Telegram group chat`), from the
  message's source channel and chat (`_message_origin`).
  The session may run its own Agent Profile. `/profile` lists the choices and
  `/profile <n|name|id>` switches it; `/profile 0` follows Settings again.
  Looper answers these, never the model (`session_profile.parse_profile_command`).
  The choice is `agent_sessions.selected_profile_id`, ahead of the manager role
  and the shared defaults (`Layer.SESSION`). It takes effect on the next user
  turn: on the same runtime and provider the session resumes natively;
  otherwise the handoff and resume brief follow, as for any profile change
  ([New sessions](#new-sessions-handoff-and-resume-brief)). `/new` clears it.
  Each switch is a Runs record (`metadata.session_profile`)
  The recommended role prompt has Claude answer only the message it was just
  sent: no earlier topic, check or status report unless that message asks for
  one, and a short reply to a greeting or a check-in. A manager row still
  holding the previous recommended prompt gets this one at startup
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
  the `manager` role prompt and `TOOLS_CONTRACT`; it has no project and
  no task section.
  Each chat message is one user turn on its stdin; its turn starts when
  Claude starts that message and ends at the next `result`
  ([Turns Claude runs on its own](#turns-claude-runs-on-its-own)). The reply,
  sent to the channel, stored in `chat_messages` once sent and recorded as the run's
  output, is the turn's final answer: the result's `result`, the text of its
  last top-level assistant message (`claude_cli.answer_text`). What Claude
  writes before its tool calls ("checking the logs now…") still streams to the
  chat as the turn runs, but is not part of the reply; a result without text
  (a turn that ended on a tool call) falls back to all the turn's top-level
  text. Messages
  are handled one at a time (a `/stop` is not queued, see below), and each turn is one Runs record holding only
  that turn's cost and tokens
  ([What a run spent](#what-a-run-spent-and-how-large-its-context-grew)); the
  session's running totals are saved beside its id in the `agent_sessions`
  row `manager` (`session_totals`). When Claude exits
  (or dies mid-turn, which answers that message with an error) or the
  Manager's system prompt, model, effort, environment or installed
  plugins and skills change, the next message starts a new process that
  resumes the saved session with `--resume`. Stopping the Manager closes
  Claude's stdin, ends Claude if it is still running after 5 s, and ends
  whatever it started ([How an agent is stopped](#how-an-agent-is-stopped))
- Scenario Managers may suspend only their resident process after `idle_timeout_seconds`
  (default 1800; `0` disables it). The default Manager is exempt. Suspension
  requires no active or queued turn, autonomous turn or runtime background job,
  and a runtime that can prove quiescence; an unknown state keeps the process.
  The Manager stays enabled and healthy, retains its saved session, selected
  Profile and totals, and still receives task reports. The next message uses
  the normal resume path. Grok ACP cannot prove quiescence, so it does not
  suspend. This is separate from disabling or deleting a Manager.
- Deleted Managers keep their immutable key and thread ownership. The operator
  can preview history cleanup in Settings → Managers or
  `manager_cleanup.preview`, then cancel or explicitly choose permanent cleanup.
  `manager_cleanup.purge` removes only that deleted Manager's exact-owner chat
  bodies, run records and cascading events, and actions after 30 days by
  default (an integer 30–3650). It rechecks retention and runtime/dependency
  blockers under a database lock, and commits all deletions together.
  Manager and thread tombstones survive; keys and thread owners cannot be
  reused. Preview is read-only apart from a count-free audit entry; the purge
  audit records counts in the default operator stream. Both operations are
  operator-only, absent from Manager and client tools. The dashboard asks
  inline before purge; an authenticated operator API/CLI purge executes once
  without issuing a client confirmation id. There is no automatic cleanup,
  runtime-directory deletion or cleanup of projects, tasks or channel catalogs.
- A new session ([New sessions: handoff and resume brief](#new-sessions-handoff-and-resume-brief)):
  when its agent can no longer resume the saved session, the old session, while
  its Claude runs, first writes a handoff in a `[HANDOFF]` turn (a Runs record,
  sent to no chat; at most 300 s). The first turn of any new session (agent
  changed, session lost, or none saved) starts with a `[RESUME]` brief: why it
  is new, the handoff, and the recent conversations from `chat_messages`: the
  3 most recently active chats that are not a task's Planner thread
  (`planner-<task_id>`), web, Telegram and Lark chats alike, each its last 8
  messages oldest first, 300 characters each, at most 4,000 characters in
  all, without the message being answered (found by the chat's stored session
  id, `_session_key`, in every channel); an answer its channel did not deliver
  reads `- assistant (not delivered): …`. Nothing new to say, no brief. The
  Runs record's input holds the brief
- Telegram and Lark turns are stored in `chat_messages` (the web chat's by
  the web channel and the chat API): the user's message, `/new` included, by
  the Manager before Claude is called, as `<message_id>`; an answer by its
  channel once it has tried to send it (`channels/chat_history.py`): the
  Manager names the record in the answer's metadata (`chat_record`: the
  session and `<message_id>:reply`, or `auto-<id>` for a reply Claude sends
  on its own), and the channel's outbound loop stores the answer as sent, or
  as not delivered with why (`delivery_failure`: `no_chat` when the channel
  no longer knows the chat, after a restart; `send_failed` when Telegram or
  `lark-cli` refused it) and the channel's error (`delivery_error`, up to 500
  characters). An answer with no record is not stored: a code answer about a
  user gate, whose discussion keeps it, and a Telegram group's. Each is
  stored under the bot/chat's stable thread with its `channel` type: built-in
  bots use `<type>:<chat_id>`, added bots `<channel_id>:<chat_id>`, and a
  destination other than the bot's owner adds `#<manager_id>`. Rebinding uses
  the destination's own thread; returning reuses it. Stored owners and history
  never move. Every chat is stored but a Telegram group's (`_stored_chat`):
  a Telegram chat
  whose id is negative is a group or a channel, and nothing from it is
  stored. A Lark group chat uses the same thread rule: only the messages
  that @-mention the bot, and the replies to them, come in from one.
  A failed store is logged and never blocks the reply, nor stops the channel
- `/new`, a chat message that is exactly that (trimmed, any case) from the web
  chat, Lark or Telegram (the sender allowlists apply), is answered by Looper,
  never by Claude. The dashboard's `POST /api/v1/operator_chat/send` may add
  `new_session_profile_id`, the Agent Profile the new session runs on; any
  `/new` clears the session's earlier choice, and one without that field leaves
  the session following Settings. After any turn under way, the Manager stops its Claude and
  saves the `manager` row with no session and `clean_start`, so the next
  message, from any channel, starts a new session without `--resume` and
  without a `[RESUME]` brief, also after a restart; the projects and goals
  every turn carries still come. The reset is a Runs record of its own
  (`metadata.session_reset`: channel, sender, chat, whether it is a group
  chat, and the session it ended)
  and the chat gets a confirmation. Tasks and their Planners are untouched.
  Any other text, `/new foo` and `/news` included, is handled as before: a
  message for Claude (in Telegram, an unknown command)
- Stopping a reply is **behind the system setting `chat.stop_enabled`
  (Settings → Chat, experimental, off by default)**, and everything about
  `/stop`, `POST /api/chat/stop`, Esc and the Stop button below holds only
  while it is on. Off, `POST /api/chat/stop` answers `409` with
  `{"state": "disabled"}` and changes nothing (`GET /api/chat/turn` still
  works); `/stop` is claimed from the bus as usual but answered by Looper with
  `停止功能暂未开放`, interrupting nothing; the composer shows no Stop button and
  Esc only restores a browsed draft; Telegram's command menu (set at startup)
  leaves `/stop` out. The staging queue and the history do not depend on it
- `/stop` (the same matching as `/new`: trimmed, any case, so `/stop now` and
  `/stopx` are ordinary messages) is consumed outside the Manager queue and
  answered by code, never stored in history. On Telegram and either Lark
  transport, `ManagerFleet.stop_chat` snapshots all live turns (including
  queued turns) for the exact `(channel_id, chat_id)` across all Managers and
  requests each once. A different bot with the same native chat id is separate.
  It reaches old owners even when the current target is off; unsupported
  runtimes or failed peers are reported without claiming complete success.
  Bound-only unbound chats remain silent, including `/stop`. The dashboard's
  Esc and Stop button use `operator_chat.stop` to stop the specified message;
  the underlying `ManagerAgent.stop_turn` results below stay unchanged
- A user turn can be stopped (`ManagerAgent.stop_turn(message_id, via=…)`
  returns a `StopResult`: `stopping`, `already_stopping`, `not_running` when
  no turn runs, `other_message` when the running turn answers another
  message, `cannot_interrupt` when the Manager's runtime has no interrupt). The call does not wait for the turn: it records the request,
  sends a "Stopping…" status and asks the runtime to interrupt
  ([How an agent is stopped](#how-an-agent-is-stopped)); the turn then ends
  by itself. A request that arrives before the turn's process has started
  makes the turn end without starting it, with no spend. What a stopped turn
  leaves:
  - its Runs record ends `interrupted` ("stopped by the user") with the text
    streamed so far as its output and the usage so far, and gets
    `metadata.user_stop` (`via`: `web`, `telegram`, `lark`…; `method`:
    `control`, `cancel` or `kill`, or null when no process had started;
    `requested_at`; `usage_complete`);
  - the saved session stays: the session totals move on, the session id is
    kept, and the next turn resumes it (after a kill, with `--resume`). Tool
    calls the turn had made are not undone, and the next turn's input starts
    with a `[Looper]` note that the user stopped the previous reply (naming
    its first 80 characters) and that those calls' effects must be checked
    before they are relied on or repeated;
  - the reply carries `outcome: stopped` (below), the text streamed so far
    and `（已停止）`, or "已停止，这次没有回复。" when there was none, plus the
    blocks of the tool calls that already ran; the Lark status line shows
    "已停止", not an error;
  - a confirmation that turn had asked for is dropped (it would otherwise end
    with "reply to confirm"), and its block is removed from the reply.

  A reply's `outcome` (`ReplyOutcome`) is `answered`, `failed` (an error was
  attached), `stopped` or `confirm` (the turn ended on a confirmation
  waiting for the user). Only the user's turn can be stopped, not a turn
  Claude runs on its own, a `[HANDOFF]` turn or a `/new` reset
- A turn its Claude runs on its own, after background work it started ends,
  is a Runs record of its own (input "(no user message: ...)", metadata
  `autonomous: true`). What Claude answered in it (its result's text, as for a
  user turn) goes as a reply of its own
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
- **Telling the chat a task came from that it ended**
  (`looper/agents/task_notice.py`). A task asked for in Telegram or Lark, the
  channels whose adapter implements `notify` (`NotifyChannel`), records who
  to tell (`NotifyTarget`) in `tasks.chat_id`, `chat_kind` and
  `chat_mention`. The Manager sets it when it creates the task: in Telegram
  the chat (`chat`, its id); in Lark the sender (`user`, the open_id) for a
  direct message, and for a group chat the group (`chat`, `oc_…`) with the
  sender to @-mention (`chat_mention`). A task from before groups reads as it
  was sent then: a Lark task tells its sender, a Telegram task its chat.
  - When the task completes or fails, the TaskRouter adds a `Notice` to the
    final `GOAL_STATUS` it passes to the Manager. Then it tears the Planner
    down and starts what may start; it never waits on a channel, and a status
    that comes after the task's end is dropped.
  - The Manager sends the notice (`CHAT_NOTICE`) to the channel instead of
    the final summary, on the same path as the task's progress. The channel
    delivers its messages one at a time, so the notice is the task's last
    message in that chat.
  - The channel sends it like an answer, to who the task recorded: Lark as
    Markdown (`lark-cli im +messages-send --as bot --user-id ou_… --markdown`,
    or to a group `--chat-id oc_… --markdown '<at user_id="ou_…"></at> …'`,
    which @-mentions the requester), Telegram as MarkdownV2, as plain text
    when Telegram cannot parse it.
  - The notice reads 「✅ 任务完成 · <the first 40 characters of the task's
    name (`task_name`: its title, else its goal id, else its requirement)>」
    or 「❌ 任务失败 · …」.
    Then the first section of the summary or closing note (what was
    delivered, or why the task ended), its line breaks kept, at most 300
    characters; the full text is on the task's page. Then the absolute link
    to that page (`links`; on Lark a Markdown link).
  - A task without a notice gets its final status from the Manager as
    before: one asked for in the web chat, and one that recorded no chat. A
    task created from the dashboard or by a schedule is not told; the
    dashboard shows it, and a schedule notifies on its own terms.
- **Links in chat messages** (`looper/agents/links.py`). A task's notice and
  a schedule's messages link to the dashboard with an absolute URL, which a
  chat client opens. It is `server.public_url` in the ops file when set (the
  address people open the dashboard at, such as `https://looper.example.com`
  behind a proxy, kept with a path and without a trailing slash), else the
  server's own `http://<host>:<port>`, with `127.0.0.1` for a server bound to
  every address. `looper serve` reads it at start and does not start when it
  is not an http or https URL with a host, or has a user, a query or a
  fragment.
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

  ## The operator's notes for this directory      L3 (unless operator_notes is off, when the directory
  ...                                                 has some): a read-only copy of the operator's
                                                      Claude Code auto memory (Task workspaces)

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

  A project's agent settings (harness, Agent Profile with its model and effort, environment and
  per-run cap) are two layers of their own: the project's, for all its task agents,
  and each role's in the project ([Harness, profile and
  environment](#harness-profile-and-environment)). They are read when each run or turn starts.

  The task section belongs to the current task: the Planner lives as long
  as its task, a Worker or Reviewer as long as its step. A non-parallel
  schedule may resume the same Planner session in the next task, with that
  task's own section. It is in the system prompt
  so it survives auto-compaction, and it comes before the contract so nothing
  a user or the Planner wrote can override the output format. A task
  requirement has at most 32,000 characters (`TASK_REQUIREMENT_LIMIT`): every
  place one comes in (creating a task, editing it on the task page or through
  its config, a schedule, the Manager's tools) refuses a longer one with a 400
  or an `invalid` answer that says so (`projects.check_requirement`). Only a row
  stored before that check can be longer; its task section clips it there with
  a visible `[clipped: N more characters]` marker. What changes from turn to
  turn stays in user turns.

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

- A `TASK_REQUEST` (as the Manager sent before its tools, or one replayed
  from then) creates the task ready (`init`), with the `task_id` it names (a
  request without one gets a new id; replaying one with an id creates nothing
  more), and starts no Planner. The Manager's `looper_task_create` writes the
  ready task itself
- Creates a PlannerAgent when a task starts and destroys it when the task
  ends. `TASK_START` (the dashboard's Start on a ready task, or the
  Manager's `looper_task_control` start) moves a ready task to `pending` once
  (`tasks.start_ready_task`, a conditional update: a task that is not ready
  is logged and left alone); the started task reports its plan and outcome
  to the chat it was asked for in
- Handles task lifecycle: start, stop (pause), restart, cancel. A
  `TASK_RESTART` runs a stopped, failed or cancelled task again as its `mode`
  (`RestartMode`) says: `continue` (the default) goes on from the saved plan
  ([Planner](#planner-looperagentstiersplannerpy), `[RESTART]`); `start_over`
  plans it anew, deleting its steps, its plan, its Planner's session and its
  step worktrees and branches first; `at_cap` continues a task that failed at
  its spend cap once the cap is raised (`POST /api/tasks/:id/continue`). A
  ready task started this way is simply planned. A new Planner drops whatever
  an earlier Planner of the task left unread on the bus (a stop's cancel that
  came during its turn would otherwise end it at once)
- A started, restarted or resumed task gets its Planner at once: nothing
  limits how many tasks run, in a project or on the machine
  ([Concurrency limits](#concurrency-limits)). At server start, a task the
  previous server had started without giving it a Planner yet (`pending`)
  starts too (`start_tasks`): one with a saved plan carries on with it, as a
  restart does, and any other is planned
- At server startup, resumes the tasks the previous server left active
  (`ACTIVE_STATUSES`) whose plan is live (`executing` or `verifying`): each
  gets a new Planner and a `TASK_RESUME`. Any other task left active is
  marked "stopped". A ready task
  is neither: it stays ready until someone starts it

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
  description, working directory and the requirement) and `REPLY_CONTRACT`,
  followed under `looper serve` by how to use its own tools
  (`ops.planner.TOOLS_GUIDE`, [A task's Planner's tools](#a-tasks-planners-tools));
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
  every change goes to a Worker as a step. It also plans for parallel steps:
  a step depends on another only when it needs that step's result, and
  independent parts run side by side; parts that must fit together (a front
  end and its back end, a library and its user) get a step that pins the
  interface in a file first, then the parts in parallel, then an integration
  step; steps that run together get their own files, and the Worker contract
  has each Worker stage only the paths it changed when it commits (`git add
  <paths>`, never `git add -A`). A `planner` row still holding the default
  from before this (`_PLANNER_PROMPT_BEFORE_PARALLEL` in `serve.py`) gets the
  current one at startup; an edited prompt is kept.
- Each event is one user turn; turns run one at a time:
  - `[PLAN]` on `TASK_REQUEST`: Claude may look at the project without changing it, then splits the
    requirement into steps (at least one, no upper limit), each with `id`, `instruction`,
    `acceptance_criteria`, `depends_on` and `review` (whether an independent
    Reviewer must pass it).
  - `[DECISION]` after every Worker result and every Reviewer result:
    `accept`, `review`, `retry`, `replan` or `fail` (see
    [How a task runs](#how-a-task-runs)).
  - Every `[DECISION]` and `[SUPERVISE]` reply may carry `progress`, which the
    contract asks for each time: `{done, now, next, risks, estimate}`, each a
    non-empty text (500 characters kept). Code keeps each note as a `progress`
    journal entry; no extra turn is spent on it.
  - The decision on the task's last open step is told so, and when it
    completes the task (an `accept`, or a `replan` with no new steps) it must
    carry `final`: `{delivered, how_to_use, verification, open_issues}`, each a
    non-empty text (4,000 characters kept); a missing or empty field gets the
    repair turn. Code writes it as the task's result with one Markdown heading
    per field (Delivered, How to use, Verification, Open issues). On another
    decision `final` is ignored.
  - `[CHAT]` when the task changed (a supplement, or an edited requirement;
    see [Changes to a task](#changes-to-a-task)): a reply and at most one
    action: `none`, `forward` (pass text to the Worker of a running step),
    `add_steps`, or `replan`, which replaces the steps not started with
    `steps` and may `stop` running steps (their Workers are killed and the
    steps `skipped`); a running step it does not name keeps running, and at
    least one step must be left to run. Events are handled in arrival order,
    so a change made during another turn waits for it; one made before the
    task has a plan reaches the `[PLAN]` turn, and a message after the task
    ended is answered without a turn.
  - `[RESTART]` when a stopped, failed or cancelled task is restarted
    (`TASK_RESTART` `continue`): the Planner reads its plan back (in whatever
    status the task left it) and goes on from it, in its saved session when
    it can be resumed (else a new one, with the resume brief). Accepted steps
    stay done and never run again, with their merged work; an accepted step
    whose merge the end cut off is merged first. Every other step that did not
    finish (failed, running, in review, awaiting a decision) goes back to not
    started with its retries, feedback, last result and worktree, as a retry
    keeps them, and a `step_retry` entry on how its last attempt ended; a
    skipped step stays as history. The turn gives why the task ended (the
    failure's reason, or that it was stopped or cancelled), that retries are
    not reset (a step keeps them, also when a replan keeps its id, so one that
    had used them all gets one attempt and no retry; a step under a new id
    starts with none used), the changes made
    while it was ended, the plan with each step's status and summary, and the
    journal, and is answered in the `[CHAT]` shape: `none` runs the steps not
    started as they are, `add_steps` and `replan` change them first (there is
    no `forward` or `stop`: nothing runs). A turn that fails leaves the steps
    as they are and runs them, with a warning in the activity; a task whose
    spend cap is spent fails at it again before the turn. A task without a
    plan with steps (stopped during its `[PLAN]` turn) is planned anew, with
    `Restarted without a saved plan to continue from: planning anew` in the
    activity.
  - `[GATE]` when the user wrote in one of the plan's user gates, or ended
    its discussion ([User gates](#user-gates)): the gate's agenda, the user's
    new messages and the plan, answered in the `[CHAT]` shape plus the whole
    `agenda`, `gate` (`continue` or `close`) and, to close, `summary`. Only
    where the task may open gates, or still has one not closed, does the
    contract say anything of gates; elsewhere it is byte for byte as before.
- Reply contract (`looper/agents/tiers/planner_protocol.py`): `REPLY_CONTRACT`
  comes last in the system prompt, after the editable role prompt and the
  project section. Every reply ends with exactly
  one ```` ```json ```` fenced object, parsed into typed dataclasses and enums
  and validated against the current plan: no unknown keys, unique step ids,
  `depends_on` naming known steps without cycles, at least one step (there is
  no upper limit: a task's spend cap and timeouts bound its cost), only the
  decisions allowed for that result, at
  most the project's `max_step_retries` retries per step (2 unless set; the
  contract states the number), and started steps never changed. An invalid reply
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
    not also a `decision`); and a restart puts a step that did not finish back
    to not started: how its last attempt ended;
  - `decision`: a replan (reason, steps added and dropped, and the decided
    step's outcome and learnings when its attempt is dropped), steps added
    from a chat message, the task stopped at its spend cap (with the
    wrap-up);
  - `supplement`: what the user added to the task while it runs, whole (up
    to 8,000 characters), with its `via` (`chat` for a message sent to the
    Planner, `task_page`, or the Manager's) and `delivered_at` (when the
    Planner had it in a turn); only added, never changed, a later one winning
    over an earlier one. Entries of the former `user_instruction` kind read
    as supplements from the chat, delivered;
  - `session_switch` and `handoff`: a new Planner session and what the old one
    handed over ([New sessions](#new-sessions-handoff-and-resume-brief));
  - `progress`: a progress note a `[DECISION]` or `[SUPERVISE]` reply carried,
    as JSON, for the task page only: no agent's section shows it.
    `GET /api/tasks/<id>` gives the notes newest first as `progress` (each
    `{done, now, next, risks, estimate, at}`);
  - `profile_choice` and `concurrency_change`: the Planner chose (or cleared)
    its Workers' or Reviewers' Agent Profile, or changed its task's
    concurrency limits (before they were per task, also its project's), with
    its own tools, and why
    ([A task's Planner's tools](#a-tasks-planners-tools)); for the task page
    only;
  - `role_defined`: the Planner defined a role for its steps, or a new version
    of one ([Roles a task's Planner defines](#roles-a-tasks-planner-defines));
    for the task page only;
  - `gate_opened`, `gate_updated`, `gate_cancelled` and `gate_closed`: a user
    gate of the plan opened, the Planner changed it, cancelled it or closed it
    ([User gates](#user-gates)); for the task page and the `[LESSONS]` turn
    (closed and cancelled ones). A gate that passed also leaves its conclusion
    as a `supplement` with `via` `gate`, written delivered.

  An entry of the whole task has the step id `''`. Every `[PLAN]`,
  `[DECISION]`, `[SUPERVISE]`, `[CHAT]` and `[RESTART]` turn (and the
  spend-cap wrap-up) carries the supplements, every one whole and oldest first, in a budget of
  their own (8,000 characters, which no other entry uses; only past it are the
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

### Changes to a task

A task that has not ended changes in two ways, each stored before its Planner
hears of it (`looper/db/task_changes.py`):

- a supplement, a `supplement` journal entry, only ever added: a message sent
  to the task's Planner on the Chat page is one (`via` `chat`), stored when
  it is sent;
- a requirement edit (`PUT /api/tasks/:id/requirement`, the task page's
  Edit): the text becomes the task's requirement, and every version is a
  `task_requirement_versions` row (version 1, the text the task was created
  with, is written at the first edit). The Planner's system prompt and the
  Workers and Reviewers dispatched afterwards have the new text.

A change waits undelivered (`delivered_at` empty). When the Planner handles
an event, it first reads the changes waiting into the plan, so a step it
dispatches in that event already lists them, and after the event it gives
every one in a single `[CHAT]` turn ("The task changed."): the new
supplements, the requirement's diff from the version it had, the plan and the
journal. They are marked delivered only once that turn's reply is applied; a
failed turn leaves them for the turn after the next event, and a restarted
Planner gives them after its first event, so a change made during a turn,
in a failed turn or before a restart is not lost. `TASK_CHANGE` only wakes
the Planner. A task whose Planner is starting gets its changes in its `[PLAN]` turn,
and an edit of a task not started is delivered at once, since it plans the
new text. The reply to a chat message is the reply of the turn that
delivered it; the activity keeps every answer with the action chosen
("Planner answered a change to the task (replan): ..."), which is where the
answer to an edit on the task page goes.

### WorkerPool (`looper/agents/tiers/worker.py`)

- Manages a pool of Worker processes
- A Worker or Reviewer run starts once fewer of its role run in its task
  than the task's limit ([Concurrency limits](#concurrency-limits)), read at
  that moment; runs of other tasks never hold it up. Until then it waits
  (status `pending`, and one `Worker waits for room in this task (N running,
  limit L)` activity) and checks again when a run ends or a limit changes; a
  waiting run can be killed like a running one. A task's Planner sends every
  step that is ready at once; the limit alone decides how many run (16
  Workers and 8 Reviewers per task unless the task or the role's layer sets
  one)
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
    `REVIEW_CONTRACT` (the review's rules and the verdict format); a run as a
    role its task's Planner defined puts the role's working style and prompt
    in the first two places and the boundaries before the contract ([Roles a
    task's Planner defines](#roles-a-tasks-planner-defines)). A Worker's
    task section has the whole task's requirement as context, then the
    step's label, instruction, working directory, target files, acceptance
    criteria, validation commands and, on a retry, the attempt number and the
    feedback on the previous attempts. A Reviewer's has the requirement, then
    the reviewed step's instruction and acceptance criteria and, on a retry,
    the notes the Planner retried it with (the same feedback, oldest first),
    marked as the Planner's changes to the step: where one changes the
    instruction or a criterion the Reviewer reviews against it, and every
    criterion no note changes still holds. Both list the
    task's supplements after the requirement (`WORK_ITEM` and
    `VERIFY_REQUEST` `supplements`: whole, oldest first, in their own 8,000
    characters), as they were when the step was dispatched;
  - first user turn: for a Worker, a short start cue, what the steps it
    depends on reported (summary and changed files, from `WORK_ITEM`
    `dependencies`) and "Findings so far in this task" (`WORK_ITEM` `findings`: the
    accepted steps it depends on first, then the failures and retries, then
    the other accepted steps newest first, in at most 2,000 characters); for a Reviewer, the Worker's report (summary, changed
    and deleted files) as data to check, never in the system prompt;
  - messages sent mid-run (forwarded chat, Planner steering) stay user turns
    and do not restate the contract.
- `WORK_ITEM` carries the step's fields apart: `requirement`, `step_key`,
  `instruction` (the step's own, without feedback), `feedback` (a list) and
  `attempt`, `dependencies`, `findings`; `VERIFY_REQUEST` adds `requirement` and
  `step_key`, and `feedback` once the step was retried.
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
    gone (`looper/agents/reported_files.py`). Each file is looked for in this
    order: where the report says (the work directory, or the absolute path);
    for a relative path, at the same place in another worktree of the work
    directory's repository (a deleted file must also be in that worktree's
    `HEAD`); in a commit made since the run started (the `WorkerProcess`'s
    `since`, less 60 s) on any branch, tag or remote-tracking branch, that
    added or modified the changed file or deleted the deleted one (an absolute
    path in no worktree of the repository, its worktree removed, matches by
    its last two or more components, never by its file name alone). Git is
    asked only when the disk does not account for every file: at most four
    read-only commands of 10 s each, over at most 64 worktrees and the latest
    500 commits. A work directory outside git is checked on disk only. In a
    shared work directory, a commit made during the run by a step running in
    parallel counts as evidence too: the check catches misreports; it does
    not attribute work. The failure lists the files and where the check
    looked; files found outside the work directory are listed in the result's
    `found_elsewhere`, a JSON list of typed locations: `path`, `deleted`,
    `where` (`worktree` or `commit`), and the full `commit` SHA. Worktree
    entries also include the absolute `worktree`, short `branch` (empty for
    detached/unknown), and `repo_path` relative to the repository root;
    commit entries include `repo_path` when known (omitted for absolute-path
    suffix matches). The task's activity lists the locations. The Reviewer's
    first turn and Planner's step report share a renderer: each location is
    one JSON line in a fenced block labelled as untrusted Worker data, followed
    by fixed read-only hints with placeholders. Git hints use repo-root-relative
    `repo_path` with `-- ':(top,literal)<repo_path>'`; without a known path they
    show the whole commit's stat. Readers ignore old free-string dictionaries,
    malformed entries, commits other than full lowercase 40- or 64-digit hashes,
    non-absolute worktrees, and path/worktree/branch fields containing ASCII
    control characters or exceeding 512 characters (200 for branches). In a
    project with step workspaces the work directory is the step's worktree.
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
the project's own `CLAUDE.md` (or `AGENTS.md`, below), not the personal Claude
Code configuration of whoever runs Looper. `isolation_args(cwd)` in
`looper/agents/claude_cli.py` is the one flag set all four commands carry;
`ISOLATION_ID` (`personal-config-off/v3`) names it in each run's record:

```
--setting-sources project --strict-mcp-config
--settings {"autoMemoryEnabled":false,"claudeMdExcludes":[...],"crossSessionInbound":"refuse"}
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
| Messages from the operator's other Claude Code sessions (`SendMessage`; below) | delivered as a user turn when both sessions bypass permissions | refused |
| Project `.claude/settings.json` and `CLAUDE.md` (its `AGENTS.md` when it has no `CLAUDE.md`) | loaded | loaded |
| Login (OAuth in the Keychain) | works | works |

`--setting-sources project` alone is not enough: Claude Code also reads
`CLAUDE.md`, `.claude/CLAUDE.md` and `.claude/rules/` in every directory from
the working directory up to `/` as project memory, so any work directory under
`$HOME` would still load `~/.claude/CLAUDE.md`. Where a project has no
`CLAUDE.md` of its own, Claude Code reads its `AGENTS.md` files (and
`.claude/AGENTS.md`) instead, from the same directories (its built-in default,
the `instructionFiles` setting `claude-md-or-agents-md`). `claudeMdExcludes`
lists all of these files for every directory above the work directory's
repository (the nearest directory with `.git`, or the work directory itself),
plus the operator's `~/.claude/` (and `$CLAUDE_CONFIG_DIR`) memory, so a
repository's own `CLAUDE.md` (or `AGENTS.md`) files load and nothing above it
does; an excluded `CLAUDE.md` above the repository does not keep the
repository's `AGENTS.md` out. `--bare` and a separate
`CLAUDE_CONFIG_DIR` were rejected because both lose the operator's login;
`--safe-mode` also drops the project's `CLAUDE.md`; `--disable-slash-commands`
also drops built-in skills. Not covered: managed (admin policy) settings and
built-in plugins. The process does not receive the server's environment: it
starts from the allowlist in `looper/child_env.py`
([Ops file and child environment](#ops-file-and-child-environment)). With user
settings gone, the model, the effort and any environment variables come from
Looper (next section), not from the operator's Claude Code settings, and the
plugins and skills come from the Looper catalog,
where they are installed (below), never from `~/.claude` itself.

### The operator's other Claude Code sessions

Claude Code sessions on one machine find each other (each listens on a socket
under `/tmp/cc-socks/`) and message each other: `ListAgents` lists them, and
`SendMessage` reaches one by its name or socket address. Left alone, an agent
could list the operator's own sessions and message them, and they could message
it: with `crossSessionInbound` unset, Claude Code delivers a peer's message
when both sessions are in the same permission-mode class, and every agent but a
Planner bypasses permissions. Looper cuts its agents off both ways:

- **Inbound.** The isolation `--settings` set `"crossSessionInbound":
  "refuse"`, so another session's message to an agent is turned away and the
  sender is told so.
- **Outbound.** `claude_cli.peer_args` adds `--disallowedTools ListAgents` to
  every launch, with `SendMessage` as well when the run has no agent teams
  (`Launch.agent_teams`: never for a Planner or a consult). A run with teams
  keeps `SendMessage`, which its lead and teammates use. Without `ListAgents`
  it cannot discover the operator's sessions, but it can still send to one
  whose name or socket address it learns (from its prompt or a file). Only the
  receiving session's own `crossSessionInbound` stops that (`"refuse"` or
  `"hold"` in the operator's `~/.claude/settings.json`), and Looper does not
  set it for the operator. A read-only run's second `--disallowedTools` adds to
  its own; Claude Code applies both.

Verified with Claude Code 2.1.285, haiku agents and a sonnet sender, all
sessions started for the check: a resident Manager-style session with the
generated flags lists neither tool in its `init` event, and asked to call
`ListAgents` it finds no such tool; a plain session that sends it a message by
its name gets `Cross-session message refused` back, and the agent sees nothing
and runs no turn, where the same session without the setting received the
message as a user turn and answered it. A Worker with agent teams and
`ListAgents` denied starts a named teammate, messages it, and gets its reply.

Not covered: besides the known-name send above, an agent with a shell can
reach the sockets, or other runtimes' sessions (`codex queue` queues a message
in a Codex session it names), through commands Looper does not filter. Codex,
Grok and OMP agents get no flag for it: none of them has a tool that reaches
another session (checked in Codex 0.162.0, whose multi-agent tools reach only
the agents it spawns and whose message board is off; Grok 1.0.62, which Looper
runs without the shared leader; OMP 18.4.3).

### What an agent may do

The Manager, the Workers and the Reviewers have every tool but the ones that
reach the operator's other Claude Code sessions (above) and run without
permission prompts (RFC decision 8): `permission_args()` in
`looper/agents/claude_cli.py` is `--dangerously-skip-permissions`, and
`agent_settings.Launch.args` puts it in front of their launch flags, beside
the installed plugins'. Verified with Claude Code 2.1.285 in `--print` mode
with the isolation flags and a `--plugin-dir`: the init event reports
`permissionMode: bypassPermissions`, and Bash, Write and a plugin skill that
declares `allowed-tools: Bash` all ran without a permission denial. The
isolation `--settings` (`claudeMdExcludes`, `autoMemoryEnabled`,
`crossSessionInbound`) leave permissions alone. Not checked: managed (admin policy) settings that deny
tools or turn bypass mode off.

A Planner runs read-only (`Launch.read_only`; [Planner read-only
RFC](rfcs/planner-read-only-v0.zh-CN.md)): it plans and decides, and every
change goes to a Worker. How its runtime keeps it from changing files is the
adapter's `read_only`, recorded in each of its runs as `metadata.read_only`:

- **`permissions`** (Claude Code): `read_only_args` in place of
  `permission_args`. `--permission-mode manual` (2.1.285's name for its
  default mode) is pinned, so a project's own `permissions.defaultMode`
  cannot loosen it, and `--permission-prompts none` leaves no one to answer
  a prompt: in `--print` mode every tool call Claude Code would ask about is
  refused. `--disallowedTools Edit,Write,NotebookEdit` leaves out the
  file-editing tools (and a second one `ListAgents,SendMessage`, above), and
  `--allowedTools` lets through what the Planner may
  use without asking: its skills (`Skill`), web reads (`WebFetch`,
  `WebSearch`) and each of its MCP servers (`mcp_rule`: `mcp__<server>`,
  with each character of the name that is not a letter, a digit, `_` or `-`
  replaced by `_`, as Claude Code names the tools; `mcp__looper` is its own).
  A plugin's own MCP servers never load (`--strict-mcp-config`). Bash
  runs `ls`, `cat`, `grep` and `git status`, `log`, `diff` and `show`, and
  refuses a redirect, `touch`, a script that writes or `git commit`
  (verified with Claude Code 2.1.285 and haiku). Each refused call is in the
  turn's `result.permission_denials`: the run record keeps it as
  `metadata.permission_denials` (`[{tool, input, id}]`, the input cut to
  200 characters, the `tool_use` id) and the task's activity says "The
  Planner runs read-only: Looper refused N of its tool calls (...)", each
  input masked (`log_redaction.redact`) and cut to 80 characters. Not
  covered: a project's own `.claude/settings.json` still loads and its allow
  rules may let through Bash commands that write, an MCP server installed for
  the Planner may write files, and a skill's `allowed-tools` grants its tools
  while it runs.
- **`sandbox`** (Codex): `-c sandbox_mode="read-only" -c approval_policy="never"`
  in place of `--dangerously-bypass-approvals-and-sandbox`, in a run and in
  `exec resume`. Reads, `git status` and the like work; a write (a shell
  redirect, `touch`, an edit) fails. Codex marks no refusal in its events, so
  nothing is recorded as refused. With approvals never asked, Codex refuses
  every MCP tool call, so a Planner's `config.toml` gives each of its MCP
  servers `default_tools_approval_mode = "approve"` (the servers are the ones
  Looper and the catalog give the Planner; one that writes files can still
  write). Verified with Codex 0.162.0 through a local API-key provider, in
  a run and a resumed turn: the MCP tool ran, `README.md` was read, and
  writing `made.txt` failed with the working tree unchanged.
- **`tools`** (OMP, Grok, Pi): the run starts with the tools that read and no
  shell or write tool: OMP `--tools read,glob,grep`, Grok
  `--tools read_file,grep,list_dir`, Pi
  `--tools read,grep,find,ls,mcp__looper__*`. Nothing is refused, so nothing is
  recorded. The MCP tools stay (OMP 18.4.3 through the home's MCP plugin,
  verified with a stub server in a run and a resumed turn; Grok through
  `use_tool`; Pi through the packaged HTTP bridge, named `mcp__looper__*`).
  Such a Planner cannot run `git diff`, tests or any other
  command, and OMP's web search and sub-agent tools are not available to it.
- **`check`** (a runtime that can do neither; none ships now): the command is
  unchanged, and the Planner compares its task directory's git
  checkout before and after each turn (`step_workspaces.tree_state`: `HEAD`
  and each path `git status` lists with its status, size and modification
  time, read with `--no-optional-locks`). A change is recorded as the turn's
  `metadata.planner_changed_files` and an activity warning ("files changed in
  the task directory during a Planner turn, while no Worker, Reviewer or
  other task ran there ..."); the files are left as they are. It compares
  only when, at the start and the end of the turn, no Worker or Reviewer of
  the task runs in the task directory (a step in a worktree of its own runs
  elsewhere) and no other task in progress shares the directory
  (`tasks.others_active_in`), and only in a git checkout. Each such turn
  records how it was checked as `metadata.read_only_check`: `compared`,
  `shared` (another writer may have run) or `no_checkout`.

A step that runs in a worktree of its own is outside the directories the
Planner reads; the decision on it names the worktree, its branch and the
commit it started from, and git shows its committed changes.

The Planner's recommended role prompt says it runs read-only and which
commands it may run.

Because the Manager can now run any command on this machine, Telegram and
Lark pass on only the senders they list, and a channel without such a list
does not start ([Channels](#channels)). The web dashboard stays local-only
([HTTP API](#http-api)).

### How an agent is stopped

Looper starts every agent process in a session of its own: each Worker and
Reviewer run, the resident Claude Code of the Manager and of each Planner,
and each Codex or OMP turn. The processes an agent starts share its process
group. Some start one of their own: Codex and Claude Code run each shell
command in a new session (seen on the real path with Codex 0.162.0 and
Claude Code 2.1.285).

Every way Looper ends an agent goes through `looper/agents/proc_tree.py`:

- a task's step killed or its task stopped or cancelled (a stopped task ends
  its running Workers and the Reviewers of its steps in review, all at once);
- `timeout_seconds`;
- the per-run spend cap;
- a resident session stopped or replaced;
- server shutdown: the worker pool stops first and ends every run at once,
  then the TaskRouter stops its Planners together, each ending its Claude. A
  pool that has stopped still answers a Planner's `KILL_WORK` at once
  (`killed: false`), so a shutdown does not grow with the number of running
  steps (with 3 tasks and 12 steps it took about 1 s on the real path). A
  stop that cancels a message loop cancels it again every `RECANCEL_SEC`
  (5 s) while it runs on (`supervisor.cancel_and_wait`): Python 3.11's
  `asyncio.wait_for` drops a cancellation that arrives as what it waits for
  completes, and the loop then waits for its next message for good. The
  bus's request-reply does not use it. The whole shutdown has
  `SHUTDOWN_DEADLINE_SEC` (15 s, under launchd's 20 s before SIGKILL): past
  it the server logs `the shutdown did not finish within 15 s` and exits with
  code 1, and a second SIGINT or SIGTERM during the shutdown exits at once the
  same way. The next start ends what such an exit leaves: the agents'
  processes (`proc_tree.sweep`) and their records (`end_unfinished_records`).

A user's stop of a Manager turn (`ManagerAgent.stop_turn`) is the one stop
that does not end the session. It ends the turn, as Esc does in Claude Code;
it does not hunt down the processes the turn started:

- It is offered only on runtimes with an interrupt: Claude Code and Grok over
  ACP (`RuntimeAdapter.can_interrupt`, shown as `runtime_types[].can_interrupt`
  in `/api/registry`; a running session's `ResidentSession.can_interrupt`,
  false once Grok has fallen back to per turn, shown as `can_interrupt` in
  `GET /api/chat/turn`).
- When the runtime ends the turn on its interrupt (`control`, `cancel`), that
  is all: no process is signalled. What the runtime leaves running, a
  background command of the turn included, is the runtime's own.
- It kills processes only when such a runtime ignores the interrupt for its
  grace: the agent is ended with its process group and the tree
  `proc_tree.end` finds (`kill`).
- A process that detached from the agent's tree (`setsid`, a double fork,
  its ancestors gone) is never chased.
- On a runtime without an interrupt (Codex, OMP, Pi, DSH, Grok's per-turn
  fallback) the stop is refused: `POST /api/chat/stop` answers `409`
  `cannot_interrupt`, `/stop` says so, the dashboard's Stop button is
  disabled with that reason and Esc does nothing. A stop never kills such a
  runtime's process.

Each runtime's `interrupt()` ends only the running turn, returns how
(`StopMethod`: `control`, `cancel` or `kill`) and the turn comes back as
stopped:

| Runtime | `interrupt()` | Session after | Usage of the stopped turn |
|---|---|---|---|
| Claude Code (`ResidentClaude`) | writes a `control_request` `interrupt` to Claude's stdin and waits up to `INTERRUPT_GRACE_SEC` (5 s) for the turn's `result` (`control`); without one, or when stdin is closed, kills Claude (`kill`, the turn returns a synthetic `user_stop_result`) | `control`: the process stays; `kill`: the next turn starts it again with `--resume` | `control`: the result's; `kill`: what the events had reported |
| Grok over ACP (`AcpGrok`) | `session/cancel`, waits for `stopReason: cancelled` up to `CANCEL_GRACE_SEC` (`cancel`); past it the process is ended (`kill`) | `cancel`: process and session stay | the response's `_meta.usage` |
| Codex, OMP, Pi, DSH and Grok's per-turn fallback (`PerTurnSession`) | not offered: the user's stop is refused (`cannot_interrupt`) and the turn runs on. Its `cancel` (a turn handed one already requested, or a caller other than `stop_turn`) starts no process, or ends the turn's tree through `proc_tree.end` (`kill`) | the session id is kept; the next turn resumes it | OMP, Pi and DSH report usage per message; Codex only when a turn ends (`usage_complete` false) |

The per-turn runtimes have no interrupt the Manager can send: it runs them as
one process per turn, not over Codex's app-server or OMP/Pi's rpc, whose
`turn/interrupt` and `abort` only Workers use. If the runtime cannot
interrupt, neither does Looper.

Before it sends any signal, `proc_tree.end` reads the agent's tree from one
`ps -A -o pid=,ppid=,pgid=,stat=` snapshot: the agent, every process whose
parent is in the tree, and every member of a group the agent or one of them
leads. It sends SIGTERM to those groups and to the tree's processes outside
them, waits up to `TERM_GRACE_SEC` (3 s), then sends SIGKILL to what is
left. It never signals the server's own process or group.

A resident Claude Code that is asked to exit (its stdin closed, then
`STOP_GRACE_SEC`) has its tree read before its stdin closes, so what it
started is still found after it has exited and its children have been
re-parented. Each stop logs one line:
`<agent>: ended the process tree of pid N: F found, T exited on SIGTERM, K killed`.

The agents no longer share the server's process group. So when the server
itself is killed with SIGKILL or crashes, launchd's clean-up of the job's
group does not reach them (systemd still ends the whole cgroup). Each agent
process is therefore started through `proc_tree.spawn`, which records it in
`<runtime root>/.local/agent-processes/<pid>.json` until the process is
reaped: its pid, its start time (`ps -o lstart=`, in UTC) and the pid and
start time of the server that started it. At startup, before any task
resumes, `proc_tree.sweep` reads every record:

- a pid that still runs with the recorded start time is the same agent,
  orphaned, and its tree is ended as above;
- a pid that no longer runs, or now belongs to another process, is left
  alone;
- while `proc_tree.end` ends an agent's tree, the record also lists that
  tree's processes with their start times, until the end is done. A stop
  that first asks the agent to exit on its own (a resident Claude's stdin
  closed) records the tree before it asks (`begin_end`). Each listed
  process that still runs with its recorded start time is ended too, so a
  server that exited during an end (its shutdown deadline, a second signal)
  has the rest ended now, the agent's children that outlived it included;
- a pid is signalled only while it still has the recorded start time, checked
  again right before its SIGTERM and its SIGKILL: what is left is the one
  `ps` call between that check and the signal;
- a record is removed once what it names has been ended, except one whose
  server still runs (another server on the same runtime root), which is
  left alone with its agent, and one whose start times could not be read,
  which is kept for the next start.

Two cases are not covered:

- a process that left the tree before the stop, such as a command that
  daemonized itself (`nohup … &` whose shell exited). It has already been
  re-parented, with nothing linking it to the agent;
- after a crash, what an agent that had exited on its own, not during an
  end, left behind. Its children were re-parented, and the record lists no
  tree (plan §5.2).

### The supervisor: restarting resident agents

`looper/agents/supervisor.py` starts the resident agents (the Manager, the
TaskRouter and the WorkerPool), checks every 30 s that each is healthy, and
restarts one that is not, for as long as the server runs: it never gives up.

- **Delay.** Each restart in a row waits twice as long as the last, from 1 s
  up to 5 minutes (`restart_delay`). A start that fails is tried again the
  same way, without waiting for the next check.
- **Reset.** An agent that stays up 5 minutes after a restart is stable again
  (`STABLE_SECONDS`): its next crash waits 1 s.
- **Crash-looping.** Restarted 3 times in a row (`CRASH_LOOP_RESTARTS`), an
  agent is `crash_looping` until it is stable again (`AgentState`).
- **Visibility.** Every crash is logged with its restarts in a row and the
  next delay. The agent's state is in `POST /api/v1/system/status` and in the overview's
  `health.unhealthy`, which the Dashboard shows. The Manager gets an
  `AGENT_LIFECYCLE` event at each `started`, `start_failed` and `crashed`,
  once at `crash_looping`, and `recovered` when the agent is stable again;
  it logs the last two.

Planners are not supervised: the TaskRouter starts and ends them.

### Runtimes, LLM providers and Agent Profiles

An **Agent Profile** references a runtime and one saved **Model**; the model
belongs to a **Provider** account and selects exactly one protocol and its
**Endpoint**. See the [model-layer RFC](rfcs/llm-models-v0.zh-CN.md).
Roles select profiles ([Harness, profile and environment](#harness-profile-and-environment));
“agent” names a running instance. Settings → LLM Providers manages accounts,
endpoints and models; Settings → Agent Profiles selects the runtime and model.
`looper/agents/profile_registry.py` validates the same contracts for HTTP and
Manager tools.

- **Runtime types** are code: one adapter per type in `looper/agents/runtimes`
  (`RuntimeAdapter`). Every tier starts its runtime through it, so no tier
  builds a command itself. An adapter declares its `capabilities`, the API
  `protocols` it speaks and the provider `auth_kinds` it takes, and it
  probes an executable (`--version`) or an in-process Python package, never
  a model call, binds a provider
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
  `library.load_installed` and `knowledge.load_knowledge`. `CodexRuntime`,
  `OmpRuntime`, `GrokRuntime` and `PiRuntime` (`codex`, `omp`, `grok`, `pi`,
  below) translate
  them at launch; Codex, OMP and, by default, Pi keep the Manager's and a Planner's session
  with one process per turn (`runtimes/per_turn.py`), Grok with one process
  over ACP (`runtimes/grok_acp.py`, `RuntimeAdapter.resident_transport`).
  Capabilities:
  `run`, `resident_session`, `resume`, `structured_output`,
  `claude_plugins`, `mcp`, `add_dirs`, `compaction_events`,
  `autonomous_turns`, `cost_report`, `steer` (Claude Code has all but
  `structured_output` and `steer`; Codex has `run`, `resident_session`, `resume`,
  `structured_output`, `mcp` and `add_dirs`; OMP has `run`,
  `resident_session`, `resume`, `claude_plugins`, `mcp`, `add_dirs` and
  `compaction_events`; Grok has `run`, `resident_session`, `resume`, `mcp`,
  `compaction_events` and `cost_report`; Pi has `run`, `resident_session`,
  `resume`, `steer`, `read_tools`, `compaction_events` and `mcp` when the
  probe accepts version 1.3.0 or newer and every required help flag, and none
  otherwise. Pi's `mcp` is the Looper platform bridge only). Codex and OMP list `steer` when
  their probe finds the steering protocol (see the [steering RFC](rfcs/worker-steering-v0.zh-CN.md));
  Claude Code and Grok do not. Pi's `steer` is in that probed set.
- **Codex** (`looper/agents/runtimes/codex.py`, verified against Codex CLI
  0.162.0 in the RFC's section 6) runs `codex exec --json` with API key
  providers that serve `openai-responses`, or with Codex's own ChatGPT
  login (below). Each process gets a `CODEX_HOME`
  of Looper's own under `<runtime_root>/.local/codex`, named on its command
  line (`env CODEX_HOME=... codex exec ...`) and holding only the generated
  `config.toml` (the provider as `[model_providers.looper]`, its key only in
  the `LOOPER_PROVIDER_API_KEY` variable, which shell commands do not get;
  the run's MCP servers as `[mcp_servers.<name>]`, stdio and HTTP; Codex's
  own skill list and plugin sync off; `project_doc_fallback_filenames =
  ["CLAUDE.md"]`, so a project directory without `AGENTS.md` gives its
  `CLAUDE.md`) and `AGENTS.md`: in append mode, a short block of Codex runtime
  notes, then the system prompt. In replace mode the same text is stored in
  `codex/instructions/<sha256>.md`, referenced by the top-level
  `model_instructions_file` key; the home's `AGENTS.md` is empty. The operator's `~/.codex` is
  never read. A Worker or Reviewer is one `--ephemeral` process with its
  prompt on stdin, which then closes, so it cannot be steered while it runs;
  with the probed `steer` capability (`codex app-server` found to have `turn/steer`) the run is driven through
  `app-server` instead (`codex_steer`, [RFC](rfcs/worker-steering-v0.zh-CN.md)) and the Planner's messages
  reach it at once (in
  a task with an agent room, one that keeps its session in a home of its own:
  [A task's agent room](#a-tasks-agent-room)).
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
  (`POST /api/v1/provider_accounts/clear_login_problem` with `{provider_id}`, "Use the login again" on the
  provider). The login's state (`codex login status` in a temporary home
  holding only the link, which reads the file and does not ask the server)
  is in the provider's view (`login`, read at most every 30 s) and in the
  `codex_login` self-check, skipped while no Agent Profile uses the login.
  Its runs are priced at the built-in prices as a subscription equivalent;
  the form suggests the models a ChatGPT login offers (`suggested_models`).
- **OMP** (`looper/agents/runtimes/omp.py`, oh-my-pi's `omp`, verified against
  omp 18.4.3 in the RFC's section 6) runs `omp -p --mode json` (a Worker or Reviewer with the probed `steer`
  capability runs `omp --mode rpc` instead, `omp_steer`, with `interruptMode` `wait`) with API key
  providers that serve `openai-responses`, `openai-chat` or
  `anthropic-messages`. Each process runs with `HOME` set to a home of
  Looper's own, `omp/homes/<sha256>` (one per set of generated settings,
  shared by its runs), named on its command line (`env HOME=... omp ...
  --config <home>/looper.yml`): `.omp/agent/models.yml` (the provider
  `looper`: endpoint, API, the agent's model at no price; its `apiKey` names
  the `LOOPER_PROVIDER_API_KEY` variable; with `openai-responses` the model is
  marked as taking strict tools, so the MCP tools go as `strict: false`, since
  the Responses API takes a tool that leaves strict out as strict, every
  argument required), the `--config` overlay (only the
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
  `claude`, `agents`, `agents-md`, `claude-md`) are off, and the work
  directory's own `AGENTS.md` (else `CLAUDE.md`) goes into the system prompt
  instead. The system prompt is `--append-system-prompt` by default or
  `--system-prompt` in replace mode (that file when there is one, OMP runtime notes, then the prompt), which a resumed session
  takes up. A Worker or Reviewer is one `--no-session`
  process with its prompt on stdin, which then closes (in a task with an agent
  room, one with a `--session-dir` of its own: [A task's agent
  room](#a-tasks-agent-room)). The Manager and a
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
  session directories (`codex/sessions/<agent>`, `omp/sessions/<agent>`,
  `grok/sessions/<agent>`, `pi/sessions/<agent>`) of agents whose saved session (`agent_sessions`)
  is not of that runtime.
- **DSH** (`looper/agents/runtimes/dsh.py`, DeepSeek Harness
  `@deepseek-ai/dsh` **0.2.1-rc.2**; [RFC and validation status](rfcs/dsh-runtime-v0.zh-CN.md))
  is available to Manager, Planner, Worker and Reviewer through the same Agent
  Profiles and role/project/task overrides. Put the supported `dsh` on the
  server's PATH, re-detect it in Settings → Agent Runtimes, then create an
  Agent Profile selecting a saved `anthropic-messages` model from an API-key
  provider with a matching endpoint.
  Only `deepseek-flash`, `deepseek-flash[1m]`, `deepseek-v4-pro` and
  `deepseek-v4-pro[1m]` are accepted; the `[1m]` suffix is removed on the wire,
  after validating the saved model reference. Effort is `low`, `high`
  or `max`, default `high`; `off`, `medium` and `xhigh` are rejected. OAuth
  and providers without Messages are rejected. The default endpoint is
  `https://api.deepseek.com/anthropic`; an explicit Messages-compatible
  gateway is preserved. Other runtimes' defaults and profiles are unchanged.

  The explicit `[runtimes] dsh = "cli"` opt-out uses headless JSONL, one process per turn,
  persisted owner Sessions, an MCP bootstrap for Looper tools and read-only
  snapshots for consults. Catalog skills and external MCP are available on
  that path; root usage alone does not establish complete auxiliary billing.
  Looper's prompt is appended literally to DSH's built-in prompt; replace is
  unavailable. Private homes under the runtime root import no operator login.

  SDK runner pools place new leases on the member with the most free slots.
  Each lease keeps its bridge for prompts, steering, cancellation and close;
  a crash fails only that member's sessions. Replacements start on demand;
  a lease needing a new member waits for crash backoff, then rechecks free
  slots before starting one. Empty extra members stop after
  300 seconds; the last member stays. Member starts and stops log the kind,
  member index and session counts. Pi owner directories and DSH's shared
  `runners/state/dsh-native` root survive member replacement: a closed saved
  session can load on any member. DSH close awaits native writer disposal
  before another member takes ownership; Pi's native ownership checks are
  process-local, so callers must serialize access to the same saved session.

  The default `[runtimes] dsh = "runner"` puts Manager, Planner,
  Worker, Reviewer and consults on a pool of installed DSH SDK runners (Node 22.19+).
  Set `dsh = "cli"` in the ops file to opt out; runner startup, capacity or request failures
  never fall back to the CLI. Each runner has 32 session slots shared by all roles;
  the pool grows on demand to four runners per kind (128 slots), then returns `CAPACITY`. Records store
  `looper-runner` Descriptors of kind `dsh`, rather than DSH CLI commands.

  Manager, Workers and Reviewers have `read`, `glob`, `grep`, `write`, `edit`
  and `bash`; Planner and consults have only `read`, `glob` and `grep` with
  a read-only guard. Exact `looper_*` platform tools are registered in-process
  for each turn. File tools stay within the working directory and explicit
  added directories; bash is not confined to those paths and is not an OS
  sandbox. Bash uses the session's private HOME and environment, with a
  ceiling of the run timeout (86,400 seconds when unset); a shorter tool
  timeout is honored. An unset run request limit becomes 1,000 requests.
  Spend and request caps are checked before every model request, including
  compaction, with no provider retry. Per-request token buckets use the saved
  model's effective prices (`cost_source=provider_price`); these reservations
  and observed costs are not a hard provider billing cap.

  A running Worker's steer reaches the next model request on the same trace;
  a dropped steer returns to the Planner's decision. Consults seed a separate
  read-only session from the run's completed-turn prefix, leaving the source
  unchanged, and remove the fork on close. Native sessions live in the stable
  `runners/state/dsh-native/` root so owner sessions survive runner restarts;
  retained run-session cleanup remains a follow-up. The runner offers no web
  search, catalog skills, catalog plugins or MCP servers. Telemetry, session
  upload, title requests and autonomous subagents are disabled.

  See the [bounded validation commands](../../evals/README.md#dsh-validation)
  for installed stand-in validation and the pending real DeepSeek smoke and
  CLI/runner comparisons. Dashboard needs no new controls: its records show
  Descriptors and its existing task/chat controls stop runs. Telegram and Lark
  use the same chat-stop and Planner-forward APIs; stand-ins do not establish
  delivery to real accounts. `looper config init-ops` writes the `dsh` runtime
  setting; ordinary CLI Profile selection is unchanged.

- **Grok** (`looper/agents/runtimes/grok.py`, xAI's `grok` CLI, verified
  against Grok 1.0.62 in the [Grok runtime RFC](rfcs/grok-runtime-v0.zh-CN.md))
  runs headless `grok --prompt-file /dev/stdin --output-format
  streaming-messages-json --always-approve --trust` with Grok's own login
  only (the built-in provider `grok-login`, below). Each process gets
  `GROK_HOME` and `HOME` both set to a home of Looper's own,
  `grok/runs/<sha256>`, named on its command line (`env GROK_HOME=...
  HOME=... grok ...`): the generated `config.toml` (Claude Code and Cursor
  compatibility scans, memory, the update check, the shared leader,
  telemetry and session upload off; the run's MCP servers as
  `[mcp_servers.<name>]`, stdio, HTTP and SSE; the installed plugins'
  `skills` directories in `[skills] paths`; `shell_environment_policy.set`
  giving the agent's shell commands the operator's `HOME` and XDG
  directories back and a `GROK_HOME` without the login), `rules/looper.md`
  (in append mode, Grok runtime notes, then the system prompt; Grok reads its
  home's rules at every start), the login link, and links to the bundled skills and user
  guide every home shares (`grok/shared`). In replace mode `--agent` selects
  the generated `agent.md` with `prompt_mode: full` and the same runtime
  notes and prompt; `rules/looper.md` is empty. `HOME` is the home too because
  Grok finds the operator's `~/.agents/skills`, `~/.claude/plugins` and
  `~/.claude/settings.json` through it whatever its config says. A project's
  instruction files are read by Grok itself, every name it knows in each
  directory from the repository's root down (`AGENTS.md` and `CLAUDE.md`
  both when both are there). A Worker or Reviewer is one process with its
  prompt on stdin, which then closes. The Manager and a Planner run one
  resident process over ACP (`grok --trust --no-auto-update agent
  --always-approve -m <model> --effort <effort> stdio`, JSON-RPC lines on
  stdio; `grok_acp.AcpGrok`) in a home of their own (`grok/sessions/<agent>`,
  refreshed from the run home): `session/new` or, after a restart,
  `session/load`; one `session/prompt` per turn, whose updates become the
  stream-json events below and whose result carries the turn's tokens and
  cost; `session/cancel` for a turn given up. A Planner's reading tools are
  the `tools:` line of an agent profile. When Grok cannot serve a session over
  ACP (no `agent stdio`, no `initialize` answer, no `loadSession` for a saved
  session) it runs one process per turn with `--resume <id>` in the same home
  (`ResidentGrok`).
  The stream is Claude Code's stream-json: each response's tokens become a
  `usage` event, a command's output (byte values) is decoded, and the run's
  cost is Grok's `total_cost_usd` (`runtime`; a 0 for tokens spent is
  unknown). Grok has no spend cap and reports the cost only at the end, so a
  per-run cap does not stop a Grok run (`runtime_cost_capped`, a warning when
  the Agent Profile is saved). The model reaches MCP tools through Grok's
  `search_tool` and `use_tool` as `<server>__<tool>`, which the runtime
  notes say. `looper serve` registers the read-only runtime `grok`
  (`rt-grok`) when `grok` is on its PATH.
- **The Grok login** (`looper/agents/runtimes/grok_login.py`) is the
  built-in provider `grok-login` (`prov-grok-login`, OAuth,
  `openai-responses`), which pairs only with Grok runtimes. Every Grok home's
  `auth.json` is a symbolic link to the operator's login file (`auth.json` in
  the server's `GROK_HOME`, else `~/.grok`), never a copy; `XAI_API_KEY`,
  `GROK_CODE_XAI_API_KEY` and `GROK_AUTH` are removed from the environment.
  How Grok writes a refreshed login back is not verified (closed source; no
  refresh was forced on the operator's login), so the homes are inspected
  after every run and when `looper serve` starts exactly as Codex's are
  (`runtimes/linked_login.py`, shared by both logins; the problem is
  `system_settings.grok_login_fork`), with the same "Use the login again" on
  the provider. The login's state is whether its file is there (`stat`,
  never read), in the provider's view and in the `grok_login` self-check,
  skipped while no Agent Profile uses the login.
- **Pi** (`looper/agents/runtimes/pi.py`, the `pi` CLI, minimum 1.3.0,
  verified against pi 1.3.0 in the [pi runtime RFC](rfcs/pi-runtime-v0.zh-CN.md))
  runs Manager, Planner, Worker and Reviewer. With the default `[runtimes] pi = "runner"`,
  `PiRunnerRuntime` runs all four roles and consults through
  a pool of Node >= 22.19 SDK runners. Set `pi = "cli"` in the ops file to opt out.
  The setting is read once at startup and needs a restart; there is no
  environment-variable override or dashboard control.
  Each runner has 32 session slots shared by all roles. The pool grows on demand
  to four runners per kind (128 slots); only a full pool returns `CAPACITY`, failing a step
  like any other start failure. There is no CLI fallback.
  Missing prerequisites fail with the startup reason. Turns submitted during
  restart backoff fail fast when no healthy member has room; replacement happens
  on the next acquisition after backoff, and the resident session loads its saved history.
  Provider environment entries are refused because they would change process-wide settings.
  Platform tools use `looper_*` directly, without Manager MCP setup or probing;
  Planner offers read, grep, find and ls plus those platform tools. Worker and
  Reviewer keep read, bash, edit and write. File tools stay inside the task
  directory and its added directories; bash is not path-confined, and its timeout
  is capped by the role's `timeout_seconds`. For these runs, `max_turns` limits
  model requests (1000 when unset); run and task spend caps are checked before
  every request. Planner messages steer a Worker in the same trace; a dropped
  steer returns to the Planner's decision. Consults fork a read-only copy of
  the kept run session, leaving the source untouched and removing the fork afterwards.
  Run records store a runner Descriptor as their command. Dashboard task controls
  and Telegram/Lark task stop cancel the session; Planner chat forwards into the
  running Worker in the same trace, using the existing channel paths.
  The following describes the explicit `[runtimes] pi = "cli"` opt-out path.
  A Worker or Reviewer is
  `pi -p --mode json`; a Worker that can steer drops `-p` and uses
  `--mode rpc` (`pi_steer`), without OMP's `--no-ui`. Providers are API keys
  only, speaking `openai-responses` or `openai-chat` (pi's
  `openai-completions`). The profile selects a saved model by `model_id`,
  which fixes its provider and protocol; the matching endpoint supplies the URL.
  A missing key, a missing endpoint URL, OAuth, Anthropic, an unknown protocol,
  or an unresolved model reference is refused before launch. The provider
  name is not matched by substring, and the endpoint `base_url` is passed
  through unchanged. Pi's `/login` and OAuth are not bound. Each process
  runs with `HOME` set to a home of Looper's own, `pi/homes/<sha256>` (one
  per set of generated settings, shared by its runs; directories 0700, the
  files 0600), named on its command line
  (`env HOME=... PI_CODING_AGENT_DIR=<home>/agent
  PI_CODING_AGENT_SESSION_DIR=<session> pi ... --offline --no-approve
  --no-context-files --no-extensions --no-mcp --no-skills
  --no-prompt-templates --no-themes -e <packaged looper.ts> --provider looper
  --model <id>`). Sessions stay outside the home, in
  `pi/sessions/<owner>`. The probe is `--version` then `--help` in a
  temporary home, agent directory and session directory, 10 seconds, and no
  model. `models.json` holds only the chosen model: `input` `["text"]`, a
  `contextWindow` only when the selected model has one, reasoning from the
  model's efforts (empty efforts set reasoning false), and cost four
  fields at 0 so pi's catalog prices are unused. The key is only
  `LOOPER_PROVIDER_API_KEY`, and the file's `apiKey` is the interpolation
  `$LOOPER_PROVIDER_API_KEY`. There is no `--api-key`. Responses sets
  `compat.supportsStrictMode` so optional tool parameters stay optional and
  the request sends `strict: false`; Chat Completions omit that compat.
  Output limits and a context window the model omits stay pi's defaults.
  A local pi 1.3 model-runtime readback, with no live model call, still
  reported a positive context window and max tokens; those figures are the
  defaults, not the model's capacity. `--thinking` is one of `off`,
  `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and is omitted when
  empty. Whether a model clamps that value is unchecked on a live model.
  The environment starts from `child_env.inherited()` plus the explicit
  bindings, drops other runtimes' homes and external `PI_*`, `LOOPER_*` and
  `LOOPX_*` overrides, and sets `PI_OFFLINE=1`, `PI_TELEMETRY=0` and
  `PI_SKIP_VERSION_CHECK=1`. Provider and role variables cannot override the
  isolation. `settings.json` turns cache warming, analytics and telemetry
  off, sets project trust to never, and leaves packages, extensions, skills,
  prompts and themes empty. Its `shellCommandPrefix` unsets the platform and
  bridge variables, the pi directories, pi's shell metadata, the other
  runtimes' homes and the provider's variable names, so a child shell keeps
  the private `HOME` and does not receive the run's credentials. The prefix
  is checked by running it under `/bin/sh -c`. A nested `pi` was not
  launched. `--no-context-files` stays on: Looper injects project
  instructions itself (`AGENTS.md`, else `CLAUDE.md`, one file per
  directory, `Delivery.INJECTED`, the existing 32 KiB truncation). Project
  `.pi`, `SYSTEM.md`, skills, themes, prompt templates and catalog MCP
  servers and plugins stay unloaded. `launch_record` writes
  `runtime_catalog.skipped` for non-looper MCP servers, skills and plugins.
  Knowledge still follows the existing index. The bridge is the packaged
  `pi_extension/looper.ts`, loaded by pi's TypeScript loader. Before the
  model it GETs `/api/agent/v1/tools` with the bearer token and registers
  each tool as `mcp__looper__<name>`; `execute` POSTs
  `/api/agent/v1/ops/<name>`. No proxy, no redirects, no automatic POST
  retry. Timeouts are 120 seconds, and a room inbox wait uses
  `max(120, wait_seconds + 20)`. Credentials come only from
  `library.mcp_config`'s `mcpServers.looper.env` (`LOOPER_AGENT_URL`,
  `LOOPER_AGENT_TOKEN`). A missing `looper` entry means no platform tools; a
  partial entry fails before launch. An invalid URL binding makes the real
  pi CLI exit non-zero (`Failed to load extension`) before a model call. A
  representative rich schema (`$defs`, enum, required,
  additionalProperties, nested) is preserved and accepted by pi. A census of
  every production tool schema was not run. Tokens stay out of argv, the
  prompt, the home files and the records. A Planner or a consult passes
  `--tools read,grep,find,ls,mcp__looper__*`. A normal run keeps pi's
  default read, bash, edit and write tools plus the bridge. A Reviewer is
  left on that default. The allowlist is not an OS sandbox (`read_only` is
  `tools`). Append is `--append-system-prompt` and replace is
  `--system-prompt`, the same generated file, with runtime notes: the final
  message is the reply, and platform tools use `mcp__looper__`. A Worker or
  Reviewer outside a room is one `--no-session` process, its prompt on stdin,
  which then closes. In a room the run gets its own `--session-dir`. The
  Manager and a Planner run one JSON process per turn (`ResidentPi`) with
  `--session-dir pi/sessions/<agent>` and, after the first turn, the exact
  `--session <file>` of the unique match in that directory. `--resume`,
  `--session-id` and `--continue` are unused. A missing, ambiguous, partial,
  symlinked, other-owner or bad-header session starts no session and follows
  the existing session-lost and handoff path. Usage counts each assistant
  `message_end.usage`, an optional nested usage on a tool result, and a
  successful compaction once. `input` is the uncached input; cache read and
  cache write stay separate; output already includes reasoning. A resident
  turn adds only this process's new usage. Looper prices that usage with the
  the selected model's effective prices (`provider_price` for an API key). An unknown price stays
  null, and a catalog cost of 0 leaves the price to Looper. The soft cap
  stops the process tree when a usage event crosses it and leaves a request
  already in flight to finish. Cache warming is off. A Worker steer uses
  `PiChannel`: `start` returns after sending setup, and events wait for the
  `get_state` and `set_steering_mode(one-at-a-time)` ACKs before the prompt.
  A steer is accepted only on a queued ACK. Cancel and the soft cap use the
  process tree. `looper serve` registers `pi` (`rt-pi`) when `pi` is on its
  PATH, and `read_only_copies` reclaims pi homes no running process names.
  Settings → Agent Runtimes and Agent Profiles show Pi, with no login help.
  The CLI follows the registry. Telegram and Lark keep their protocols. What
  has run, and what is still open, is in the pi runtime RFC.
- **Model API (SDK)** (`api`) runs only the Manager inside `looper serve`,
  through the `openai` Python SDK on OpenAI Responses. Its platform tools run
  in-process with the same confirmation and audit as the CLI runtimes. Its
  turn loop is a Python port of Norma behind `AgentLoop`, with OpenAI
  Responses as the first codec; there is only one `api` runtime kind. Neutral
  session items are kept in `api_session_items`, with the protocol on the
  `api_sessions` metadata row; the codec translates wire formats. Stage 1
  adds no filesystem or process tools. Any future such tool must obey the
  task cwd plus explicit `add_dirs`, an allowlisted environment and a private
  per-runtime HOME; these tool boundaries do not provide an OS sandbox.
  Stop cancels the model request. Settings → Agent Runtimes detects the
  Python package, shows its SDK version and “Runs inside looper serve”, and
  keeps Re-detect; a missing package shows the probe's reinstall error.
  See the [API runtime RFC](rfcs/api-runtime-v0.zh-CN.md).
- **Runtimes** (`agent_runtimes`) are built in, never registered by hand: one
  per detected type (`rt-<type>`, named after its type; CLI types are found
  on the server's PATH and in-process types by their Python package;
  Claude Code's `rt-claude-code` comes with the schema and runs the server's
  `claude`), with an `executable` (`''` = the type's default) and what its
  last probe found (`version`, `capabilities`, `probe_error`). A different
  binary comes from the server's PATH. Settings → Agent Runtimes shows one
  card per type, found or not, with its path, version, capabilities and the
  roles it can run, and Re-detect (`POST /api/v1/runtimes/probe`) probes it
  again, or looks for a type not found yet and adds its runtime when its
  probe succeeds. A runtime registered by hand before runtimes were built in
  stays while a profile references it, read-only, so that profile keeps its
  executable. This binary does not drop unused hand-registered runtimes at
  startup.
- **Providers** (`llm_providers`) are accounts: `name`, typed `vendor_type`,
  `auth`, `oauth_runtime_type`, `env`, `max_concurrency` (`null` = no limit,
  stored and checked only), and a separate API key in `llm_provider_secrets`.
  The API returns the key as `***` or `null`; sending `***` keeps it and
  `null` removes it. Only the launch path loads it. Login accounts use their
  runtime's own login and never copy OAuth tokens. Vendor templates suggest
  defaults at creation; they do not overwrite saved configuration.
- **Endpoints** (`llm_provider_endpoints`) have the key `(provider_id, protocol)`:
  one URL per account and protocol (`anthropic-messages`, `openai-responses`,
  `openai-chat`). API-key accounts need at least one valid endpoint;
  custom/self-hosted creation requires explicit URL confirmation.
  `use_runtime_default` preserves legacy omitted-URL behavior during migration;
  it is not a new-account shortcut. Login endpoints have an empty URL managed
  by the CLI. Endpoint edits revalidate affected profiles in the same transaction;
  a referenced endpoint cannot be removed.
- **Models** (`llm_models`) have an opaque `model_id`, `provider_id`, exact
  `name` and one `protocol`. The identity triple is immutable: changing it
  requires another model and explicitly switching profiles. Settings are
  `context_window`, `input_usd_per_mtok`, `output_usd_per_mtok`,
  `cache_read_usd_per_mtok`, `cache_write_usd_per_mtok`, `efforts` and
  `background_model`; `catalog_order` preserves legacy pricing order.
  A provider may have no models, but a profile must select a saved model:
  an empty catalog no longer accepts arbitrary names. The same name over two
  protocols is two entries; the protocol is never guessed from the name.
  Price views expose each field's `override`, `effective` and `source`
  (`override`, `builtin`, `unknown`). `null` falls back to the built-in price
  table; zero is an explicit free price. Unknown prices remain unknown.
  If actually used tokens lack a price, `cost_usd` is null, tokens are kept,
  and a provider-priced soft cap cannot be relied on; this is not a zero-cost
  run. Pricing resolves within the selected provider/protocol. New run metadata
  captures model identity and pricing; historical rows and totals are not rewritten.
- **Agent Profiles** (`agent_profiles`) save `runtime_id`, `model_id`,
  `effort` (null: the runtime's), `agent_teams` (Claude Code only, on by
  default there) and `custom_prompt`. Provider and protocol are derived from
  the model. Profiles do not own a system-prompt mode: role defaults and task
  overrides still select append/replace. API profile views return `model` as
  `{model_id, name, protocol, provider_id}` and derived `provider` information.
  `roles` lists supported roles and `used_by` lists selections; a selected
  profile cannot be deleted (409); that includes the profile the Manager's
  current session selects (`used_by.manager_session`, shown as "the Manager's
  current session"). Model deletion returns 409 while profiles
  reference it; provider deletion returns 409 while models reference it.
  Built-in accounts, models and profiles remain protected. The legacy converter
  preserves profile ids (including their `agent-` prefix) and selections;
  database migration is still gated below.

The built-ins exist on every database and are read-only: the runtime
`claude-code`, the providers `claude-subscription` (Claude Code's login),
`codex-login` (Codex's ChatGPT login, above) and `grok-login`, and the
profile `claude-code/opus` (`BUILTIN_PROFILE`), which a role runs when nothing
selects another. A profile is refused when it is created or changed if its
runtime does not support the model's protocol, an OAuth provider is paired with
another runtime type, the runtime cannot take that kind of provider yet
(Claude Code and Codex take their own login and API keys, OMP, Pi and DSH take API keys only), or the
model has no matching endpoint; a profile whose model or background
model has no full price is registered with a warning (provider-priced runs
may have unknown cost and cannot rely on a spend cap), and every profile shows how its runs
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

**System prompt mode.** The mode is `append` (Looper's prompt is added to the runtime's default one) or `replace`
(it takes the default prompt's place). On **every runtime** (Claude Code, Codex, Grok, OMP and Pi) it is a role's setting,
`system_prompt_mode` on the role layer (`POST /api/v1/agent_settings/update_role` with `{role, values: {system_prompt_mode}}`; a null or empty value unsets it). The
built-in values are `replace` for the Manager and the Planner and `append` for every other launch: Workers, Reviewers,
the seats of Planner-defined roles, consults and a launch with no role (`agent_settings.BUILTIN_SYSTEM_PROMPT_MODE`). The
settings view shows the effective value of every role (`effective.system_prompt_mode`). A task may set its own per role
(`task_system_prompt_modes`, `system_prompt_modes` of `PUT /api/tasks/:id/agents`): its choice wins over the role layer's,
which wins over the built-in value; the task's `agents` view gives each role's effective value with its source (`task`,
`role` or `builtin`). A Planner-defined role's runs and reviews follow the task's Worker or Reviewer choice, a consult
appends, and the Manager has no task level. Only a role layer takes it: the shared defaults, a project and a role in a
project refuse it (400). An Agent Profile has no mode of its own. A runtime whose adapter cannot replace (the typed
`can_replace_system_prompt` capability, exposed by `/api/registry` with `replace_system_prompt_refusal`) always gets
`append`; today all five adapters can replace. `Launch.system_prompt_mode` is the role's value on any runtime (append with
no role). Configuration export and import carry the role layers' `system_prompt_mode`. A profile that still has that field is skipped as an unknown field. A historical release copied a replacing Worker or Reviewer mode onto the role layer and then dropped `agent_profiles.system_prompt_mode`. This binary does not replay that write. The Manager and the Planner were not copied: their built-in value is `replace`, so a Manager or Planner profile that appended on Codex, Grok or OMP replaces unless its role layer says `append`.

| Runtime | Append | Replace |
| --- | --- | --- |
| Claude Code | `--append-system-prompt-file` | `--system-prompt-file`; both modes keep `--system-prompt-snapshot off` |
| Codex | Generated home's `AGENTS.md` | Top-level `model_instructions_file` in `config.toml`, before any table, points to a content-addressed instructions file; the home's `AGENTS.md` is empty |
| Grok | Generated home's `rules/looper.md` | `--agent` points to the home's `agent.md`, with `prompt_mode: full`; the rules file's body is empty |
| OMP | `--append-system-prompt` | `--system-prompt`, using the same generated prompt file |
| Pi | `--append-system-prompt` | `--system-prompt`, using the same generated prompt file |

Codex and Grok include their runtime notes with Looper's prompt in either
mode. Replacement keeps the runtime's tools and project instruction loading;
it removes the runtime's default prompt. The mode is not a profile setting:
the role settings (Settings → Roles) show a two-way choice for every role, a
task's Agents tab shows (and lets the task override) the role's choice for every
runtime, and the profile editor only points to Settings → Roles. Each run's
System prompt section shows `replaced default` for a replacement record. The
Manager's profile configuration tool has no mode field.

**Agent teams.** They are on by default for Claude Code profiles: a profile
created without `agent_teams` gets them on a Claude Code runtime and not on
another (`profile_registry.default_agent_teams`), a change that moves a profile
to a runtime of another type without saying takes that type's default, and the
historical release turned them on for profiles already on Claude Code,
including the built-in `claude-code/opus`. This binary does not repeat that
write, so a profile turned off afterwards stays off.
The built-in profile is read-only: to run without teams, select a profile with
them off. Asking for them on another runtime is refused (400). A profile with
`agent_teams` starts the Manager's, the Workers' and the Reviewers' Claude Code
(so any of them can spawn teammates) with
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` and `--teammate-mode in-process`, and
keeps `SendMessage`, which teammates use (the operator's other Claude Code
sessions, above). A Planner never has them (`Launch.agent_teams`, keyed on the Planner's role): its
plan and each of its decisions are read from the result of its own turn, so a
turn that waited for a teammate would end without its decision, and the
teammate's later turns are Claude's own, which the Planner only logs (#26); the
Planner already runs work in parallel through Looper's Workers, whose steps
Looper tracks and teammates would bypass. A Planner, and every role of a
profile without them, starts with that variable removed, whatever a setting
or a provider variable says. The server's environment is not passed to the
child (`looper/child_env.py`), so a `looper serve` started from a Claude Code
shell no longer hands the variable on. Entering the variable in a
layer's or a provider's environment is refused (400, pointing at the profile's
switch); a layer stored before keeps loading, and an import leaves the
variable out of a layer with a note. Turning them on or off restarts
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
  The Planner's request after it carries the plan, the supplements and the
  journal as every turn does; `REPLY_CONTRACT` says a brief may come
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
(stored in `task_profile_overrides`), which wins over every layer for that
task only (source `task`). The only other task-level setting is its own
Worker and Reviewer concurrency limit ([Concurrency limits](#concurrency-limits)).
Together they are the task layer, read with the task (`list_layers(task_id=...)`).
Under it, over every other layer, come the task's Planner's choices of its
Workers' or Reviewers' profile, made with its own tools from its project's list
(`task_planner_profiles`; source `planner_step` for one step over
`planner_task` for the rest of the task; [A task's Planner's tools](#a-tasks-planners-tools)).
A task chooses a profile at creation (`profile_overrides`; a schedule copies
its own), and while it can still run (any status but completed: ready or
running, or failed, stopped or cancelled, which a restart runs again with
the new choices) it changes or drops either choice per role with `PUT
/api/tasks/:id/agents`, from the task page's Agents tab or the Manager's
`looper_task_update` (`profiles`, `system_prompt_modes`) and `looper_concurrency`. All of them check
and write through one path (`projects.set_task_agents`: a profile by id or
name, registered and able to run its role; both kinds in one transaction). A
Worker or Reviewer uses a new profile from its next step on, while a step that
runs keeps the one it started with; the Planner moves to its new profile at
its next turn, after a handoff.

| Layer | Edited in | Applies to |
| --- | --- | --- |
| `system` | Settings → Roles, Shared defaults | every agent |
| `role` | Settings → Roles, a role's overrides | one role everywhere: Manager, Planner, Workers or Reviewers |
| `project` | the project's Agent Profiles tab, All roles in this project | the Planner, Workers and Reviewers of one project |
| `role_project` | the same tab, the role's row | one of those roles in one project |

The Manager spans every project, so it has the first two only, and one more
for its profile: the Agent Profile chosen for its current session
(`agent_sessions.selected_profile_id`, `Layer.SESSION`, source `session`),
which wins over the other layers until `/new` or `/profile 0` clears it
([Manager](#manager-looperagentstiersmanagerpy)). With no layer
setting a field, the built-in value applies (`builtin`): the harness
`DEFAULT_HARNESS_PROMPT` (public and generic), the profile `claude-code/opus`
(Claude Code, its own login, `opus`, the runtime's own effort), no extra
environment and no cap. The fields are the harness, the Agent Profile
(`profile_id`; its model and its effort come with it: `--effort` `low`,
`medium`, `high`, `xhigh` or `max`, none for the runtime's own, source
`profile`; a layer has no effort of its own), up
to 50 environment variables per layer, and the per-run cap
(`--max-budget-usd`, on each Claude Code process, when Claude Code's own
figure is the run's cost; otherwise Looper's soft cap at the provider's
prices, per run: a Worker run or a resident turn. A Planner turn or a Manager
message that hits either fails, and the next turn starts a new process). The environment merges key by
key: a later layer adds or overrides a variable or unsets one an earlier layer
set (a `null` value; the system layer has nothing to unset). A Reviewer
without a harness of its own at the role or role-in-project layer takes the
Workers' at that same layer. `load_agent_settings(role, project, task, step_id=...)` returns
every field with the layer it came from (with `step_id`, a Worker's or
Reviewer's run of that step, the Planner's choice for it); all four roles
launch from it.
Every launch passes `--model` and, when its profile has one, `--effort` and (Claude Code's own
cost only) `--max-budget-usd`. The variables are added on top of the child's
allowlist (`looper/child_env.py`) for every agent's process; names must match
`[A-Z_][A-Z0-9_]*`, and a name containing `KEY`, `TOKEN`, `SECRET`,
`PASSWORD` or `CREDENTIAL` is refused because the values are shown on the
dashboard. That word check guards against pasting a secret by habit; it is
not a security boundary. A stored
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
selects nothing. A third (`layer_effort_on_profiles`) moved each layer's
effort onto profiles when the effort became the profile's: a layer's effort
used to win over its profile's. It is written in Python
(`looper/agents/layer_effort.py`, which the config import also uses) and runs
after `schema.sql`, in the same transaction (`ensure_schema`). For every role
at every place a layer governs (the system layer, each role, and each task
role of a project with layers, at the project and at the role in the project)
and every task's choice of profile, it selects the profile that runs what the
old rule gave: the profile itself when that is its own effort, else an
existing one with the same runtime, provider, model, agent teams
and that effort, else a new ordinary one named `<profile> · <effort>`. A project layer
selects what most of its roles need, and each other role gets its own on its
role-in-project layer. It logs each profile added and each layer and task
changed, then drops `agent_setting_layers.effort`. Changes apply to the next Worker or Reviewer run; a resident agent's
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
`--append-system-prompt-file` in append mode or `--system-prompt-file` in
replace mode. `--system-prompt-snapshot off` is needed for
the resident agents: by default Claude Code 2.1.285 keeps the system prompt of
a session's first request and sends it again on every `--resume`, so a
restarted Planner or Manager would keep running with its old prompt. A
resident agent's restart signature is its command, the prompt's sha256 and the
sha256s of the environment and of the installed items.

Every run records the prompt in `llm_records.metadata`:

- `system_prompt`: the text Looper supplies;
- `system_prompt_mode`: `append` or `replace`, what the launch used (`Launch.system_prompt_mode`); an old
  record without the field means append;
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
    `REVIEW_CONTRACT` or `TOOLS_CONTRACT`);
  - `isolation`: `ISOLATION_ID`;
  - on a run of a role a task's Planner defined only: `task_role` (`{id, kind,
    version, sha256, harness_sha256}`, the role prompt's and the role
    harness's hashes) and `boundaries` (`{source: builtin | system, sha256}`);
    its `harness` is `{source: task_role, sha256}` for the role's own harness,
    else the applying harness's entry, with `part: working_style` and that
    part's hash when it is the built-in one; its `role` keeps the `config_id`
    of its kind's row and adds `task_role`.
- `profile`: `{id, name, model, runtime: {id, name, type}, provider: {id, name,
  auth}, source}`, the Agent Profile the run used (`id` is its profile id) and
  where it was selected; runs recorded before the rename to Agent Profiles have
  it as `agent`, which the dashboard and the cost query read too;
- `model_choice`: `{model, source, effort}`, the model asked for (`source` is
  the profile's layer; earlier runs have `builtin_default`, `system_default`,
  `agent_override` or `project_override`); the record's `model` column keeps
  the model Claude reports;
- `settings_sources`: `{harness, profile, effort, run_budget_usd, env: {NAME:
  layer}}`, the layer each setting came from (`effort` is `profile`, or
  `builtin` for the runtime's own; runs recorded before the effort was the
  profile's may name the layer that set it, runs before the rename have
  `agent` in place of `profile`, and an effort from the profile's own default
  as `agent`; runs before Agent Profiles have `model`);
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
  longer written;
- `project_instructions`: `[{path, delivery, sha256?, bytes?, clipped_bytes?,
  error?}]`, the repository's own `CLAUDE.md` and `AGENTS.md` files the run's
  runtime was given (`RuntimeAdapter.instruction_files`,
  `looper/agents/runtimes/instructions.py`), each `path` from the work
  directory. The files are those in the directories from the work directory's
  repository root down to the work directory (the work directory alone
  outside a repository). Claude Code reads every `CLAUDE.md` of them, else
  every `AGENTS.md`; Codex and omp read one a directory, `AGENTS.md` before
  `CLAUDE.md`, so each runtime gets its own file and the other one where the
  project has only that, never both. `delivery` is `native` when the runtime
  reads the file itself and `injected` when Looper put its text in the
  runtime's system prompt (omp outside a repository: the first 32 KiB, with
  the text's `sha256`, the file's `bytes` and the `clipped_bytes`, or the
  `error` that kept it out). The Runs page shows them as the prompt layers'
  "Instructions" row.

The same inputs give the same prompt and hash; changing the harness, the role
prompt, the background, the knowledge index, the task or a contract changes
it.

### Concurrency limits

Concurrency is per task. Nothing limits how many tasks run at once, in a
project or on the machine: a started task gets its Planner at once. What a
limit counts is one task's Workers, or its Reviewers: a run starts only while
fewer of its role run in its task than the task's limit
(`resolve_task_concurrency`, which the WorkerPool reads each time a run may
start: `load_task_limit`, the built-in value when the layers cannot be read).
Runs of different tasks never wait for each other, and nothing counts a
project's runs. The Planner has no limit: one Planner runs a task.

A task's limit is its own (`task_concurrency_limits`, one row per task and
role, 1 to 64, read with its profile overrides as the task layer,
`Layer.TASK`), else its role's layer (`max_concurrency` on the `worker` or
`reviewer` `role` layer, Settings → Roles: the default for every task,
`default_concurrency`), else the built-in one (16 Workers, 8 Reviewers). A
task that has not completed (a failed, stopped or cancelled one too: its
restart runs with the limit) sets or drops its own with `PUT
/api/tasks/:id/agents` (`{"concurrency": {"worker": 2, "reviewer": null}}`,
null drops the task's own); the task page's Agents tab, the Manager's
`looper_concurrency` and the task's Planner's `looper_task_concurrency` use
it, and a schedule gives each task it creates its own (`task_limits`,
[Schedules](#schedules-tasks-on-a-time-rule)). Only the task's own limit
and `CONCURRENCY_MAX` (64) bound it. `max_concurrency` is refused (400, and
a check constraint holds the table to it) on every other layer: the shared
defaults, a project, a role in a project, and the Planner's or the
Manager's role layer. After a successful write that sets a limit (a role's
PUT, a task's agents, an applied import), the API asks the WorkerPool to
check again, so a raised limit starts what waits at once. A lowered limit
stops nothing that runs; new starts wait until fewer run than it. The role
configs' `max_concurrency`, which nothing enforced, was dropped
(`role_configs_max_concurrency_removed`).

Before, a project limited its tasks too: the Planner's limit (a role in the
project, else the Planner's role layer, else 8) was how many of a project's
tasks ran at once, the others waiting as `pending`, and a role in a project
limited its Workers and Reviewers across its tasks. A historical release
converted those values once. This binary does not replay that conversion.
That release copied a project's Worker and Reviewer limits into the
`task_limits` of each of its schedules that had none, capped each parallel
schedule's `max_parallel_runs` with the project's Planner limit (still 1 to
10), cleared `max_concurrency` from every role-in-project layer and from the
Planner's role layer, and narrowed the constraint. A task created by hand in
such a project runs with the per-task defaults. Some things changed meaning
without a conversion: a Worker or Reviewer role layer's value, and the
built-in 16 and 8, counted a project's runs and now count one task's. Tasks
present at that upgrade lost their project's limits, because that release
wrote only schedules. A task held back as `pending` for a restart at a raised
spend cap started as a continued restart at the next server start (one more
`[RESTART]` turn, at that upgrade only). A current database already has the
narrowed constraint, so its project page cannot store a project's limit.

What bounds the load is now each task's limits and how many tasks start.
Before, an unconfigured project ran at most 8 Planners, 16 Workers and 8
Reviewers at once whatever its tasks; now N running tasks run N Planners and
up to 16N Workers and 8N Reviewers, and nothing caps the total (an LLM
provider's `max_concurrency` is stored, not enforced). The converted alarm-like
project (Planner 3, Worker 8, Reviewer 3) moves from at most 8 Workers and 3
Reviewers across the project to its parallel schedule's 3 runs × (8, 3): 24
Workers and 9 Reviewers. Bursts come from several schedules firing in the same
minute, a catch-up firing after downtime, and every task resumed at a server
start, and they reach the provider's rate limits, the machine's processes and
memory, and the spend at once. The knobs that bound it: a schedule's
`max_parallel_runs` and `task_limits`, the role defaults in Settings → Roles,
and a task's own limits.

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

### The knowledge vault: the operator's notes, read and written through Looper

The operator's own Markdown vault (an Obsidian vault, say: a local directory,
usually a git repository) can be the knowledge of the projects that turn it on
(`looper/agents/vault.py`, the tools in `looper/agents/ops/vault.py`;
[RFC](rfcs/obsidian-knowledge-v0.zh-CN.md)). Agents read the whole vault and
write only in its writable folders; each write is a git commit in the vault.
The tools are Looper's MCP tools, the same on every runtime.

- **The setting** (`system_settings` key `knowledge_vault`, Settings →
  Knowledge vault, `POST /api/v1/knowledge_vault/get` and `update`, the Manager's
  `looper_settings` action `knowledge_vault`, which the user confirms):
  `path` (absolute, an existing directory, stored resolved; every folder on
  the way to it must be one the server's user may pass, as the writes walk
  it from `/`, and the vault itself readable and searchable, which is
  checked when the setting is saved), `writable`
  (folders inside it, each an existing directory, neither a link nor below
  one, so a link is refused when the setting is saved; empty: read only) and `rules_file` (a file inside it, optional).
  It is checked again whenever it is read: a vault that no longer holds (its
  directory removed, a folder turned into a link out of it) is `problem`, and
  every tool refuses with it. It is a local path, so the configuration export
  leaves it out; the project's `vault` goes with the project.
- **Who gets the tools.** A task's Planner, Workers and Reviewers, when the
  task's project has `vault` on and the setting holds; the Manager has only
  the setting. A Worker's or Reviewer's run gets its run token when its project
  has the room or the vault on (the token knows which); the Planner uses its
  own. The list is fixed when a run starts (each Planner turn, each Worker or
  Reviewer run); every call checks the project and the setting again, and is
  `forbidden` once either is off. The Planner's tool guide and the runs' system
  prompts (a `## Knowledge vault` section after the task and the room, its own
  layer `vault`) say the tools exist in one line; with the vault off, or the
  setting unset, the prompts are byte-for-byte as before.
- **Reading.** `looper_vault_search` (`query`, `limit` 1 to 30, default 10):
  Markdown files whose path or text holds any word of the query, in any
  case, those with the most of its words first, then the most hits, with a snippet; it follows no link, skips names
  starting with `.` (`.git`, `.obsidian`, Looper's temporary files) and files
  over 512 KiB, and stops at 10,000 files, 32 MiB or 5 s (`truncated`).
  `looper_vault_read` (`path`): a UTF-8 text file's first 64 KiB and the
  sha256 of the whole file; absolute paths, `..`, names starting with `.`
  (also in the path a link resolves to), links out of the vault and other
  files are refused.
- **Writing** (`looper_vault_write`: `path`, `content`, `expected_sha256`,
  `reason`): a Markdown note in a writable folder, at most 64 KiB, and at most
  256 KiB per run; any other path (the operator's `mine/`, say) is refused
  with the writable folders named. Under one thread lock per vault, in one
  worker thread: the folders are walked from directory descriptors without
  following links (a link anywhere below the writable folder is refused), the
  note's sha256 is compared with `expected_sha256` (null for a new note; a
  mismatch is `conflict`: read it again), the text goes to a temporary file
  that replaces the note, and `git --literal-pathspecs add` and `commit -m
  "looper: <task id> <seat>: <reason>" -- <path>` commit that path only (the
  operator's other staged changes stay staged), as the vault's repository
  configures the author, with its own hooks, `commit.gpgsign=false`, a 30 s
  timeout and three tries on `index.lock`. The seat is `planner` or the run's
  (`worker@s2`). The task's activity notes each write with its commit
  (`Vault: planner wrote codex-mem/x.md (commit 1a2b3c4d5e6f): <reason>`), or
  a fixed reason it was not committed (not a git repository, commit failed
  with git's exit status, timed out; never git's output), and each refused
  write.
- **The closeout.** When a task completes (after its lessons) or fails, and
  its project has the vault on with a writable folder, its Planner gets one
  `[VAULT]` turn before the task's final status: the vault's rules file (at
  most 16,000 characters) and the task's journal (as the lessons turn has
  it), asked to keep what later work should know through `looper_vault_write`,
  or nothing. It replies `{"vault": "<one line>"}`, noted in the activity as
  `Vault closeout: <line>`. It is skipped when the journal is empty or the
  task has spent its cap, runs at most once per Planner, and a failure is a
  warning that does not change the task's outcome.
- **Limits.** Agents read the operator's personal notes when a project turns
  the vault on; an agent with a shell can still edit the vault's files
  directly (the tools are the supported way, not an enforced boundary); Looper
  gives no isolation between processes of the same user.

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

The `project_skills` table of M4 was moved into the catalog in a historical
release: each row became an item with the same id, project, name and
description, its `SKILL.md` as runs wrote it, installed in its project for the
three task roles and enabled as it was; then the table was dropped. This
binary does not replay that import.

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
(see the Planner); `step_workspaces` (`shared`, also when null, or
`worktree`), where a step that runs alongside others works ([Step
workspaces](#step-workspaces)); `task_workspaces` (`in_place`, also when null,
or `worktree`; a new project gets `worktree`), where a task that names no
directory of its own runs, `workspace_links` (up to 20 relative paths, null for
none), what Looper's worktrees of the project link back to the directory they
come from, and `operator_notes` (`on`, also when null, or `off`), whether the
project's agents get the operator's notes for their directory ([Task
workspaces](#task-workspaces)); `max_step_retries` (0 to 20, null for 2),
how many times the Planner may retry one step: its reply contract and each
decision turn's allowed decisions say how many are left, and a retry past it is
refused; and `planner_roles` (`off`, also when null, or `on`) with
`role_text_max_chars` (500 to 20,000, null for 8,000), whether the project's
Planners may define the roles their steps run as and how long a role's prompt
and its harness may each be ([Roles a task's Planner
defines](#roles-a-tasks-planner-defines)); and `user_gates` (`on`, also when
null, or `off`) with `gate_reminder_hours` (0 to 168, null for 24, 0 for none),
whether the project's Planners may open user gates and how often a gate that
waits for the user is noted as still waiting ([User gates](#user-gates)); and
`agent_room` (`on`, also when null: on by default, unlike the switches above;
or `off`) with its limits `room_post_per_minute` (1 to 60, null for 6),
`room_broadcasts_per_run` (0 to 50, null for 5), `room_message_max_chars`
(500 to 20,000, null for 4,000), `room_messages_per_task` (20 to 5,000, null
for 300), `room_ping_pong_rounds` (2 to 50, null for 6) and
`room_wait_max_seconds` (0 to 120, null for 60), and its consults'
`room_consult_budget_usd` (0.01 to 5, null for 0.50),
`room_consult_timeout_seconds` (30 to 1,800, null for 300) and
`room_consults_per_task` (0 to 100, null for 10) ([A task's agent
room](#a-tasks-agent-room)); and `vault` (`off`, also when null, or `on`),
whether the project's agents get the knowledge vault's tools ([The knowledge
vault](#the-knowledge-vault-the-operators-notes-read-and-written-through-looper)).

A task can also have a spend cap of its own (`tasks.budget_usd`, a scheduled
run's `run_budget_usd`), which replaces its project's `task_budget_usd` for it
(`get_task_project` reads it in its place). Continuing such a task at its cap
raises only its own cap.

### Step workspaces

A project with `step_workspaces` `worktree` runs a step in a git worktree of its
own when other steps of the task are in flight (running, awaiting a decision or
in review) or start in the same round, so parallel Workers no longer see or
overwrite each other's files (`looper/agents/step_workspaces.py`; the design is
the [step workspaces RFC](rfcs/step-workdir-isolation-v0.zh-CN.md)):

- **When.** The Planner decides at dispatch (`plan_round`). A step that starts
  alone while nothing else is in flight works in the task directory, and while
  one does, the ready steps wait. Gate steps never get a worktree, and an open
  gate is not in flight. The task directory must be in git 2.40 or later, on a
  branch with a commit, with no merge, rebase, cherry-pick, revert or bisect in
  progress, and its files at `HEAD` must take at most 1 GiB
  (`CHECKOUT_LIMIT_BYTES`: each worktree is a copy). Outside git the steps
  share the directory as without the setting; a checkout that fails another
  condition runs them one at a time, with the reason in the activity.
- **Where it starts.** The first time, the Planner records the branch and its
  tip (`task_plans.start_branch`, `start_commit`, and the activity):
  `git reset <start_commit>` there undoes the task's commits and keeps its
  changes. Before a round's new worktrees, every pending change in the task
  directory (tracked and untracked, not ignored, the user's own included;
  nothing outside the task directory) is committed on the branch as a
  checkpoint, and the round's worktrees start from it, or from the tip when
  nothing is pending.
- **The worktree** is `<runtime_root>/worktrees/<task_id>/<key>-<id>` on branch
  `looper/<task_id>/<key>-<id>`. The Worker and the Reviewer run at the task
  directory's place in it (`context.work_dir`) and get `context.workspace`
  (`path`, `branch`, `base_commit`, `target_branch`). Their task sections say
  that the step works on a branch of its own, may commit, and must not push,
  switch branches or rewrite its commits; the Reviewer's gives
  `git diff <base_commit>` to see the step's changes. Ignored files, such as
  `.env` or `.venv`, are not in it, except the project's
  `workspace_links` (named `step_workspace_links` before task worktrees used
  them too): each listed path the task directory has and the
  worktree does not (a checked-out file is never replaced) is a symlink to the
  task directory's, shared by every step (`link`); the ones linked are kept
  on the workspace (`links`) and linked again if the worktree is re-created,
  and the activity names the ones it could not link.
- **Accepted.** Before an accepted step counts as verified (an `accept`, an
  acceptable `replan`, or its own verdict when the Planner cannot decide),
  Looper commits what the Worker left uncommitted (never a link: the paths
  that are still links are taken out of the index even when the Worker
  committed one, so the merge cannot replace the task directory's own
  `node_modules` with a link to itself; a link the Worker replaced with files
  of its own is its change, noted in the activity), computes the merge with the
  branch in memory (`git merge-tree --write-tree`), writes a merge commit
  (`commit-tree`, recorded before the branch moves) and moves the branch, its
  index and working tree with `git merge --ff-only`; the steps that depend on
  it start after that. Every merged step has a merge commit of its own, so
  `git revert -m 1 <merge>` undoes one step; a step that changed nothing has
  none. The worktree and its branch are removed once merged; the commits stay
  on the branch.
- **A merge that cannot be made** leaves the task directory's branch, index
  and working tree as they were, and fails the attempt with a typed
  `merge_error`: `conflict` (with the files), `local_changes` (uncommitted
  changes in the task directory are in the way), `branch_changed` (the task
  directory left the branch, the branch lost the step's start, or it kept
  moving through three tries), `step_branch_changed` (the step's checkout left
  its branch, or its commits were rewritten) or `git_failed`. The Planner gets
  a decision turn on it. A retry gets a new worktree from the branch as it is
  now, and its Worker is told the old branch, which stays until the task ends.
  A worktree that cannot be created fails the attempt the same way.
- **Looper's commits** (checkpoint, leftovers, merge) are by
  `Looper <looper@localhost>`, with `Looper-Task` and `Looper-Step` trailers,
  and unsigned. No git command Looper runs runs a hook
  (`-c core.hooksPath=/dev/null`), and each has a 120 s limit. The Worker's own
  commits keep the repository's identity, signing and hooks.
- **Kept and removed.** A step that a replan drops, from a decision or a change
  to the task, loses its worktree and branch at once. A completed task's
  worktrees and branches are removed.
  A failed, stopped or cancelled task keeps its unmerged ones, and its activity
  names their branches, until it is started over (its steps go) or deleted,
  or the user discards one from the task page;
  restarting it, or continuing it at a raised cap, goes on with them: a step
  that did not finish runs again in its worktree, and one whose merge failed
  starts from the branch as it is now, told the old branch. A merge that a
  restart cut off is finished without a new decision. At startup the server
  removes what no task keeps: the worktrees of a completed task, the
  directory of a merged or dropped one, and directories no row knows.
- `step_workspaces` rows record each one: `active` → `merging` → `merged` or
  `unmerged`, and `discarded`, along `ALLOWED_TRANSITIONS`; an unmerged one
  keeps the files in the way (`merge_files`).
- **The task page.** `GET /api/tasks/:id` gives `step_workspaces` (null for a
  task that had none): `start` (the branch and commit where the task started),
  every workspace oldest first with its step's label, and `undo`, the commands
  that undo the task's commits (`undo_commands`: list them by their
  `Looper-Task` trailer, `git revert -m 1` one step's merge, `git reset` to the
  start keeping or dropping the changes, or move them to a branch
  `<branch>-<task_id>`; each that changes the branch switches to it first, and
  names are quoted for the shell). The Overview's Steps list
  marks each step's latest worktree, the Pipeline's step card has a Worktree
  section, and a Step worktrees section lists them, with the undo commands,
  and a Discard for each one still there while the task is not running
  (`POST /api/tasks/:id/workspaces/:wid/discard`: its directory and branch
  go, with the task's row locked so a restart waits for it; a restarted
  step's Worker is told of an earlier branch only while it exists). Removing a
  worktree drops only its own record in the repository (`git worktree remove
  --force`, never `prune`, which would drop the user's stale ones too).
- **`looper doctor`** warns when git is missing or older than 2.40 (`looper
  serve` too, in `POST /api/v1/system/status`'s `checks`), and checks the default directory
  of each project set to `worktree` as the Planner would, with the paths it
  links.

The eval's `--step-workspaces worktree` runs its tasks in git repositories with
the setting.

### Task workspaces

A task names a workspace, the directory the user gave it (`tasks.workspace_dir`;
the project's default directory when it names none), and runs there in place or
in a git worktree of it (`tasks.workspace_mode`; `looper/agents/task_workspaces.py`;
the design is the [task workspaces RFC](rfcs/task-workspaces-v0.zh-CN.md)):

- **How it runs** is chosen at creation (`resolve_mode`): as the creator asks
  (`workspace_mode`); in a worktree when it names a workspace other than its
  project's directory; else as its project's `task_workspaces` says (null:
  in place). A project created through the API, the task form or the Manager
  gets `worktree`; one from before, or imported, stays in place.
- **Its worktree** is made before its Planner first starts (the TaskRouter's
  `prepare`, on every start, restart, continue and resume):
  `<runtime_root>/task-worktrees/<task_id>` on branch `looper/<task_id>/task`
  (not `looper/<task_id>`, which git could not have beside the step branches
  `looper/<task_id>/<key>-<n>`) from `tasks.workspace_base` (a branch, tag or
  commit; else what the workspace had checked out), with the project's
  `workspace_links`, and `tasks.work_dir` becomes the workspace's place in it,
  so the Planner, the Workers, the Reviewers and the step workspaces, which
  nest in it and merge into its branch, need nothing else. A later start uses
  it again; one whose directory was lost is made again from the branch, and
  without the branch the start fails the task. A workspace outside git (or
  without a commit) runs in place, noted in the activity. The activity names
  the branch, the base, the links and the workspace's uncommitted changes
  the worktree does not have. Rows: `task_worktrees`.
- **At the end.** A completed task's leftovers are committed on its branch as
  Looper (never the links), its worktree is removed and the branch stays in
  the user's repository: the activity, the task page and the Telegram or Lark
  end notice name it, and Looper never merges it. A stopped, failed or
  cancelled task keeps its worktree as it is for its restart. Deleting a task
  removes its step worktrees, commits its leftovers and removes its
  worktree, keeping the branch (409, and nothing deleted, when they cannot be
  committed). Starting over keeps the worktree and the branch. At startup the
  server finishes the worktrees of completed tasks and removes directories no
  task has, committing what they hold on their branch first.
- **Warnings, never refusals** (`check`): `GET /api/workspaces/check` and the
  create answers (`workspace`) say what the task will run in (its repository,
  branch and base) and warn when a worktree (asked for, or the project's
  default) cannot be made because the workspace is outside git (`not_git`; the task runs in place), the workspace has uncommitted changes the worktree will not have
  (`uncommitted`), other tasks in progress work on the same repository (by its
  main checkout; `shared`) or the checkout is over 1 GiB (`large_checkout`).
  A base the repository does not have is a 400. The Manager's task
  configuration says how the task runs and each warning.
- **The operator's notes** (`looper/agents/operator_notes.py`): with the
  project's `operator_notes` on (the default, also when null), the Planner, Workers and Reviewers get, after
  the project's knowledge, a read-only copy of the operator's Claude Code auto
  memory for their directory: `MEMORY.md` (its first 200 lines) and the `.md`
  files of the memory directory it links to, in its order, within 24 KiB (a
  file past the limit is named, not included), led by a note that they are
  background that the task and the files win over. The directory is
  `<$CLAUDE_CONFIG_DIR or ~/.claude>/projects/<name>/memory/`, keyed as Claude
  Code 2.1.285 keys it: the top level of the repository's main checkout (so a
  subdirectory, a task worktree and a step worktree have the same notes), or
  the directory itself outside git, links resolved, every character but a
  letter or digit a `-`, and past 200 characters the first 200, `-` and the
  key's djb2 hash in base 36 (`operator_notes.root` in the ops file stands in
  for `<config>/projects`). Looper only reads them; an `autoMemoryDirectory`
  setting is not followed. The run records them in
  `prompt_layers.project.operator_notes` (hash, files, omitted) and the
  activity says which applied.

### Schedules: tasks on a time rule

A project's schedules fire a full task of the project on a time rule
([scheduled-tasks RFC](rfcs/scheduled-tasks-v0.zh-CN.md), S1 and S2; the
Manager lists, reads, creates, changes, enables, disables, runs and deletes
them with `looper_schedules`, which also resets their stored Planner
sessions; they move with their projects in the configuration file, [Moving the configuration](#moving-the-configuration-export-and-import)). A schedule has a `name` (unique in the project ignoring case),
a `cron` rule and an IANA `timezone` (default: the server's own zone), the
`requirement` each run's task gets (at most 27,000 characters), a `work_dir` (`''`: the project's default
when it fires), `overlap` (`skip`, `queue` or `parallel`, with
`max_parallel_runs` 1–10, default 3), `catch_up` (`once` or `skip`),
a `run_timeout_seconds` (a number, `null` for no limit, or `"default"`, a new
schedule's choice, which follows the system default below) and an optional `run_budget_usd`, `profile_overrides`
for the Planner, Workers or Reviewers (by id or name, as a new task takes them;
a role it does not name keeps the project's, and no change to a schedule alters
its profiles unless the request carries them; a repetitive schedule such as
alert triage can name a lower-effort Planner profile, since the Planner is
often the largest share of a run's cost; a profile a schedule chose cannot be deleted) and `task_limits`
(`{worker, reviewer}`, each 1–64 or null for the default: how many of each
run's task's Workers and Reviewers run at once, `schedule_task_limits`); and, from S2, an optional
`gate_command` with `gate_timeout_seconds` (1–600, default 60), an optional `daily_budget_usd`, `notify` (`failures`,
`alerts`, `always`, `never`) and a `notify_target` (a channel and chat; none: the
dashboard only). A new schedule is disabled.

A schedule has an immutable `manager_id`, chosen when created and defaulting
to `manager` when omitted. The dashboard/operator API can choose its owner;
a Manager's existing `looper_schedules` tool assigns the calling Manager.
Saving, enabling and firing validate that owner and its task policy; manual,
due, queued and gated runs retain the same owner on the run and task. An
optional `template_key` selects that owner's template, with static string
`template_inputs`. Placement, required inputs, roles and Profiles are checked
together; the task snapshots the effective template, Planner instructions,
inputs, roles and Profiles, and `start_over` restores its template roles.
Editing a template does not rewrite existing tasks; a changed configuration
invalidates a gate result rather than creating a task from stale settings.

Task acceptance, progress and completion reports route to the owner, using
the existing default-Manager fallback when the owner is unavailable. Scheduled
tasks have no origin chat, so these reports do not add a second origin-chat
notice; configured Notifier delivery remains unchanged. Firings that create
no task retain owner-visible run records and the existing dashboard/Notifier
policy, without a new message to the Manager conversation. Acceptance of
that reporting scope is pending maintainer confirmation. If every non-task
outcome must reach that conversation, a separate implementation and real-model
evaluation is required before the original schedule requirement is complete.

- **Time rules** (`looper/agents/cron.py`, no dependency): five fields
  (minute, hour, day of the month, month, day of the week, where 0 and 7 are
  Sunday) of `*`, numbers, ranges, steps and lists; when both day fields are
  restricted (neither starts with `*`) a day matches either. Fire times are
  wall times in the zone: a time a DST change skips fires at the first valid
  time after it, a repeated time fires once, the first time. Two fires must be
  at least 5 minutes apart over the next 24 hours when the rule is saved, or 1
  minute apart for a schedule with a gate command
  (`schedules.min_interval_seconds`, `MIN_INTERVAL_GATED_SECONDS`): its run
  ends at the gate when there is nothing new, with no task and no cost. A
  rule that fires closer is refused when the schedule is created or changed,
  and so is removing the gate of a schedule that fires every minute ("without
  its gate command, …"). The preview takes `gated` for the same check.
  Both follow the system setting `scheduler.min_interval_seconds` (Settings →
  Scheduler, `POST /api/v1/scheduler_settings/get` and `update`; default 300 s, a whole number
  from 1): it applies to the next save at once, and saved schedules are not
  checked again. With no saved value the built-in 300 seconds applies. A gated
  schedule uses `min(60, value)`. `LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS` and
  `LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS` are not read. The system setting
  `scheduler.run_timeout_seconds` (default: no limit; 1 second to 30 days) is the run
  timeout of a schedule whose `run_timeout_seconds` is `"default"`
  (`schedules.run_timeout_mode`: `inherit`, `seconds`, `none`; schedules from
  before keep their number or no limit). A run records the timeout it starts
  with (`schedule_runs.run_timeout_seconds`, null for none), so changing the
  schedule or the default affects later runs only. The dashboard shows each rule in words
  ("Weekdays at 09:00 (America/Los_Angeles)", "Every 30 minutes"), or as
  written when no wording fits.
- **The scheduler** (`looper/agents/scheduler.py`) runs inside `looper serve`,
  started after the HTTP server and stopped first. Every 15 s, and at once when
  it starts, it handles each enabled schedule whose `next_fire_at` has come, in
  a transaction that locks its row (`FOR NO KEY UPDATE SKIP LOCKED`), so two
  servers on one database fire it once; a foreign key check on the row (a
  message about its last run being recorded) does not hold it up. It counts
  the fires due since `next_fire_at`:
  one within 2 minutes fires on time (`scheduled`); otherwise they were missed
  (the server was down or the computer slept), and `catch_up` `once` fires one
  `catch_up` run that says how many it stands in for (`missed`), while `skip`
  fires on time for the latest when it is within 2 minutes and else records a
  `skipped_missed` run. `last_fired_at` becomes the fire time handled and
  `next_fire_at` the next one after now. Enabling a schedule, or changing the
  rule of an enabled one, sets `next_fire_at` from now: nothing missed before
  runs.
- **A firing** while one of the schedule's tasks is still active (running,
  or created and about to start) is recorded `skipped_overlap`, or,
  with `overlap` `queue`, waits as the one `queued` run, which starts when that
  task ends. With `overlap` `parallel` (reentrant, such as an incremental
  check every minute whose runs take longer) it starts beside the runs in
  progress, up to `max_parallel_runs` running at once, but never while a run
  of the schedule checks its gate: only the tasks overlap. Past the cap, or
  during a gate, the firing is `skipped_overlap`. Otherwise it creates the task (`tasks.schedule_id`, its own
  spend cap, the profile overrides in `task_profile_overrides`, the task limits in
  `task_concurrency_limits`, and a title:
  the schedule's name and the fire time in its zone, such as
  `alarm-triage · 10-15 12:30`, the name clipped so the time fits the 60
  characters, which names the task in lists) and starts it
  at once with `TASK_START` (enabling was the approval), like any start. Its requirement
  is the schedule's followed by a section the code writes, `## 定时运行上下文`:
  the schedule's name, rule and its words, this run's fire time in the zone,
  how it fired, the missed fires and since when, the latest 3 runs (time,
  status, summary clipped at 300 characters), and whether user gates are
  allowed. By default the run is unattended: do not wait for the user, make
  reasonable assumptions and write them into the result. With the schedule's
  `user_gates` `on`, it may wait at user gates when the project also allows them. A firing whose task cannot be created (no work directory) is a
  `failed` run. "Run now" (`trigger` `manual`) fires the same way, also while
  the schedule is disabled, and leaves `next_fire_at` alone.
- **The schedule's Planner session** ([RFC](rfcs/schedule-planner-session-v0.zh-CN.md)).
  A `skip` or `queue` schedule keeps one Planner session across runs. Each
  firing still has its own task, plan, activity, timeout and cost. Before a
  new plan's first turn, the Planner locks the schedule row and claims its
  session (id, cumulative usage, runtime, provider and profile) for
  `planner-<task_id>`. An active task holding it prevents an older task
  being replanned from taking it. Task resume and continuation keep the
  task's own saved session without claiming again. Saving that session
  writes through to the schedule only while this task is still its holder.
  A `parallel` run never claims or writes it, even when a session remains
  from an earlier non-parallel setting.
  A continued first PLAN turn has a user-message preface saying earlier
  runs are memory, this task's requirement and gate output are in its Task
  section, and its steps need new ids; it includes the previous finished
  run's status and up to 300 characters of summary or error when available.
  Every run that claims or continues the session also puts that run's
  requirement into the PLAN user message, under "This run's requirement
  (kept in the conversation for later runs):", clipped the same way as the
  Task section. The text is the requirement as given: the schedule's
  requirement plus the run context, including the gate output. A later
  run's continued conversation therefore contains every earlier run's
  input. The system prompt and reply contract are unchanged. The PLAN
  message of an ordinary task or a parallel run stays byte-identical.
  Only a runtime-confirmed
  resume of the claimed id sets `tasks.continues_schedule_session` and
  records `Continuing the schedule's Planner session` in phase activity.
  A changed runtime or provider, or a runtime unable to resume, uses the
  existing session-switch path and writes the replacement session through;
  a parallel run or a failed claim starts fresh without changing the stored
  session. Each fallback records its reason as phase activity. Runtime
  compaction and existing handoff remain in effect; failures and edits to
  the requirement, rule, gate or overlap never reset the session.
  Reset is explicit: `POST /api/v1/schedule_admin/reset_planner_session` with `schedule_id`, the
  project's schedule-row **Reset Planner session** button (with a
  confirmation), or the Manager's `reset_planner_session` action (high,
  confirmed in the user's next message). It clears the stored session and
  its holder. A run in flight keeps its own session and can no longer write
  back; the next run starts fresh. The API returns `{"cleared": true|false}`
  according to whether a stored session existed, or 404 for an unknown
  schedule, with the same caller checks as editing a schedule. Schedule
  responses expose only `planner_session` status: `active`, `runtime`,
  `profile`, `last_task_id`, `last_task_title` and `updated_at`; never the
  session id. `updated_at` is when the current id was first stored, not the
  schedule's last edit; with no session the other fields are null. Stored
  sessions are excluded from configuration export; an import entry carrying
  these unknown fields is skipped, never restored. Lark and Telegram reach
  reset through the Manager; there is no new CLI command.
- **Before the task** (S2). Past the overlap check, a schedule whose runs
  fired since the server's local midnight cost its `daily_budget_usd` (a
  running run at its task's cost so far; a run counts on the day it fired)
  records `skipped_budget`. A schedule with a gate records the run `gating`
  (an open run: another firing meanwhile overlaps) and runs the gate in the
  background (`looper/agents/schedule_gate.py`): `/bin/sh -c` in the work
  directory, started through `proc_tree.spawn`, with no stdin and its run in
  its environment (`scheduler.gate_env`: `LOOPER_SCHEDULE_ID`,
  `LOOPER_SCHEDULE_RUN_ID`, `LOOPER_SCHEDULE_SCHEDULED_FOR` in ISO 8601 and
  `LOOPER_SCHEDULE_RUN_TIMEOUT_SECONDS`, the timeout the run started with,
  empty without one), so a gate
  of a parallel schedule can claim what it hands to its run until the run
  times out; past its
  timeout, or when the server stops, its process tree is ended like an
  agent's (`proc_tree.end`), and what it leaves in its own group after it
  exits is killed. Its stdout is kept up to 8000 characters (stored on the run
  as `gate_output`), the tail of its stderr up to 2000. A non-zero exit or a
  timeout is `gate_failed` (a failure, the reason and stderr's tail in
  `error`); nothing printed is `skipped_gate`; otherwise the task is created
  with the output in its context ("the gate's output is this run's input:
  only these"). Each run that failed, timed out, was stopped or cancelled, or
  whose gate failed hands its gate output on to the next run, labelled
  「上一轮未完成」, once (`schedule_runs.carried_on`): all of them when several
  runs of a parallel schedule ended so before it, oldest first, one block
  each, and then even a gate that prints nothing gets a task (`carried_from`
  names the latest of them). The outputs
  share what is left of the 32,000 characters a task requirement may have
  (at least 600), each clipped with a marker; earlier runs' inputs that do not
  fit at 300 characters each are left out, the oldest first, and counted.
  The lines around the outputs (the gate's, with its command; each earlier
  run's label; the clipping notes) are measured and the outputs shrunk by what
  they take (`scheduler.run_requirement`), and a schedule's own requirement has
  at most 27,000 characters, which leaves room for the longest context, so a
  run's task requirement is never longer than the task limit and nothing in it
  is clipped where the agents read it. A run left `gating` by a server
  that stopped is `gate_failed` at the next start. A gate never runs twice at
  once: a firing while the schedule's run is `gating` is `skipped_overlap`.
- **Repeated skips are counted, not stored.** A skipped firing (`skipped_gate`,
  `skipped_overlap`, `skipped_missed`, `skipped_budget`) whose schedule's latest
  run is a skip for the same reason recorded the same day (the scheduler's day)
  is counted on that run (`schedule_runs.firings` one more, `finished_at` the
  latest firing), and a gate that found nothing leaves no row of its own
  (`count_skip`). A schedule firing every minute therefore adds a row only
  when the outcome changes: one row for an hour of nothing new, one for the
  minutes its task runs. Each counted `skipped_gate` still sets `last_ok_at`
  and ends a no-success alert, as a run of its own did. The run history shows
  `×N, until <time>`, and the Dashboard's `skipped_today` adds the firings.
- **The end of a run.** The TaskRouter reports every task that ends
  (`on_task_end`, from its teardown and its stop); the scheduler writes the
  run's final status (`completed`, `failed`, `stopped`, `cancelled`), its
  summary (the task's result or error) and cost, once, then starts the
  schedule's queued run. An ok run (`OK`: a completed run, or a gate that
  found nothing to do) clears `consecutive_failures` and sets `last_ok_at`,
  and a completed one `last_success_at`; a failed or timed-out run, or a
  failed gate, adds a failure; a stopped or cancelled run, and a skip that ran
  nothing (busy, over budget, missed), changes neither. A run past its timeout (from when
  it fired) is recorded `timed_out` and its task stopped through the usual
  `TASK_STOP`. Each pass also settles what that report missed: a run whose task
  a server start stopped or that was deleted, and a task created but never
  started (the server stopped in between), which is started a minute later.
- **Messages and alerts** (S2, `looper/agents/schedule_notify.py`). Every
  run that ends goes to the notifier, in the background. `notify` `failures`
  sends a message for each failed run (`failed`, `timed_out`, `gate_failed`),
  `always` for each run that ran something, `alerts` and `never` none; skipped
  runs never do. Two alerts go out once per streak, each with one recovery
  message, under every `notify` but `never` (`alerts` sends only them): 3
  failures in a row (`failures_alert_at`), recovered by the next ok run, so a
  gate that finds nothing new ends the streak without waiting for a task; and
  no run completed and no gate found nothing to do for 3 periods of the rule,
  at least 15 minutes (`STALE_FLOOR`: three fires of the shortest rule without
  a gate; a gated schedule may fire every minute, where 3 periods are noise)
  and at most 24 hours, counted from the first fire after the latest of that
  run, the schedule's last enabling (`enabled_at`) and its rule's or zone's
  last change (`rule_changed_at`), so neither enabling nor a new rule is stale
  at once (`stale_alert_at`, checked on every pass while enabled; a disabled
  schedule is never stale, and neither is one whose run is in progress:
  queued, checking its gate or its task running within its run timeout, nor
  the busy skips it causes; the alert starts only while `last_ok_at` is still
  the one its deadline came from, so a run that goes well while the pass looks
  raises none), recovered the same way. The text is code-written (the schedule, the run's status, a
  summary snippet, its cost and the link to the task, absolute (`links`)). It goes through
  the channel registry to the running Telegram or Lark channel
  (`ChannelAdapter.notify`: Telegram's bot to the user's private chat, Lark's
  `lark-cli im +messages-send --as bot --user-id`), to a chat the channel's
  sender allowlist allows. Every message is recorded in
  `schedule_notifications`: `sent`, `failed` with the reason (such as a channel
  that is not running), or `dashboard` (no chat, or an alert under `never`).
- **Deleting.** A project with schedules is not deleted (409) until they are.
  Deleting a schedule (the project's Schedules tab, the Schedules page, the
  schedule's page, `POST /api/v1/schedule_admin/delete` with `schedule_id`, the Manager's
  `looper_schedules`) deletes its runs and messages; its tasks stay, without
  it (`tasks.schedule_id` is `ON DELETE SET NULL`). A run in progress does
  not hold it up: a running task goes on and ends as any task does, and its
  end is no schedule's any more (no run outcome, no message); a queued run is
  gone, and a gate still running finds the schedule gone and starts no task.
  Writing a run's end locks the schedule's row before the run's
  (`finish_run`), as every other writer does, so a delete waits for it
  instead of deadlocking with it; a message whose schedule was deleted after
  it was written is not recorded (its outbox went with it).

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
- **Manager extras.** `manager_library_installs` adds global-catalog plugins
  and skills to one Manager (including the default), independently of other
  Managers. The effective set is the enabled global `manager` installs plus
  that Manager's enabled extras, deduplicated by item; disabling or removing
  an extra never masks an inherited global item. Project-owned items and
  Manager-specific MCP installs are refused. Settings → Managers shows
  inherited and own rows separately, with operator-only
  `manager_installs.list/put/delete` for readback and changes. No Manager or
  client install-management tools are added. Each turn refreshes the effective
  set; a supported resident safely restarts with the saved session after
  background work ends. Grok ACP cannot prove idle and may defer an install
  change until `/new` or a service restart. Runtime support still applies:
  Claude Code loads plugins/skills, other adapters load their supported parts
  and report the rest in `runtime_catalog.skipped`; Pi skips catalog plugins
  and skills. Readback metadata does not establish a real skill invocation.
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

`loopercli config export <file>` and `loopercli config import <file>`
(`looper/agents/config_transfer.py`) move an installation's agent configuration
through one JSON file. Every export is `format: looper-config/v4`. The registry sections are
`runtimes`, `providers` (account settings and endpoints keyed by protocol),
`models` (settings keyed by provider name, exact model name and protocol), and
`profiles` (runtime name and a composite `model` reference). Built-ins exist
everywhere and are not exported. Registry database ids are never portable references.
`settings` carries the system/role layers, role prompts, timeouts and enabled
flags; `channels`, global `installs` and `catalog` carry their existing content.
In v4, `managers` carries immutable full ids, definitions, idle timeout,
templates and own plugin/skill installs (catalog references by kind/name);
`channels` includes added bots, matched by `channel_id` with their fixed type
and owner. Templates reference Profiles and projects by name. Bindings, seen
and suppression records are local, excluded from the file, and import
preserves the destination's existing bindings and `bound_chats_only` policy.
New or changed added bots import disabled pending credentials, policy and
binding review, enablement and restart; identical bots keep local enablement.
Missing references, deleted Manager identities, immutable type/owner conflicts
and already allocated bot suffixes are explicit errors, not omissions or
reassignments.
`--include-projects` adds projects, layers, knowledge, skills, installs and
schedules. Command schedules travel with their projects; `env_file` carries
only its path, never the file's contents. Export is a read-only repeatable-read
snapshot through `POST /api/v1/config/export`. The CLI needs the running
service; catalog files remain UTF-8 text.

This minimal portable example can be previewed with
`loopercli --json config import portable.json`, applied with
`loopercli config import --apply <import_id>`, then exported again. A client
receives a pending confirmation and uses `loopercli confirm <confirmation_id>`
in that same session; an authenticated operator applies once without this
client confirmation. Canceling a preview changes no configuration. The example
endpoint is a placeholder and needs a real URL
and an API key before any run. Its unknown model prices are deliberately null.

```json
{
  "format": "looper-config/v4",
  "runtimes": [],
  "providers": [{
    "name": "Example account", "vendor_type": "custom", "auth": "api_key",
    "endpoints": {"anthropic-messages": {"base_url": "https://api.example.invalid"}},
    "api_key": {"secret": "omitted"}
  }],
  "models": [{
    "provider": "Example account", "name": "example-model", "protocol": "anthropic-messages",
    "context_window": null, "input_usd_per_mtok": null, "output_usd_per_mtok": null,
    "cache_read_usd_per_mtok": null, "cache_write_usd_per_mtok": null,
    "efforts": null, "background_model": null
  }],
  "profiles": [{
    "name": "Example profile", "runtime": "claude-code",
    "model": {"provider": "Example account", "name": "example-model", "protocol": "anthropic-messages"},
    "effort": null, "agent_teams": false, "custom_prompt": ""
  }]
}
```

Any other `format` is refused before a destination is read or an import is
staged: import the old file on v2.13.165, upgrade that installation and
database, then export v4 with this release. v2.13.165 may export v3, so
exporting again there does not produce v4. A disposable v2.13.165 installation
can do the import. A layer `model` or `effort`, and a provider `protocols`,
`base_url` or `models`, are retired fields and stop the import before it
writes. Unknown top-level fields are refused the same way. Role and task
prompt-mode choices keep their existing meaning. Prompt-eval comparisons
accept only `looper-prompt-eval/v2` for tasks and `looper-manager-dialogs/v2`
for dialogs.

Secrets (MCP environment and header values, a provider's API key, a channel
config's keys other than its sender allowlist, `*_env` names and description)
are `{"secret": "omitted"}` unless `--include-secrets` is requested for the API export. A project's directory,
an item's source and a runtime's executable that are local paths are not
exported; a path document is a reference relative to its project's directory,
flagged `repoint`. A profile carries its `effort`; a layer does not. Tasks are
not exported, so neither are their profile overrides.

In v4, an omitted schedule `user_gates` imports as `off`.
A project's schedules carry their settings (`name`, `cron`, `timezone`,
`requirement`, `overlap`, `max_parallel_runs`, `catch_up`, the timeouts, the
caps, the pre-run gate, `user_gates`, `notify` and `notify_target`), their Agent Profiles by name
(`profiles`) and their `task_limits`, immutable `manager` reference (omitted
for the default), `template_key` and static `template_inputs`; not whether
they are enabled, their runs, messages, counters or alerts, nor their own directory: a schedule that had one
is flagged `repoint`. The import matches a schedule by project and name,
ignoring case (`project.schedules`); a different owner is an explicit conflict,
and template references and inputs must resolve. One it adds is created disabled
(`created_via` `import`), one whose settings differ is rewritten disabled (its
directory here stays), one that matches is left as it is, enabled or not.
Enabling is the approval (scheduled-tasks RFC D6), so a schedule nobody here
approved never runs on its own; a note lists them, to check their directory and
gate here first. A schedule the API would refuse (a bad or too frequent rule,
an unknown profile or field) is skipped with the reason, and a `notify_target`
the sender allowlists here (as the import leaves the channels) do not allow is
dropped, with a note.

An import matches entries by natural key: an item by kind and name (a
project's own skill within its project), a model by provider name, exact name and protocol;
a project, runtime, provider or profile by name, a layer by scope,
role and project, a role by name, a channel by immutable id, a Manager by its
full immutable id and a template by Manager/key. The preview (the
default, in a read-only transaction) lists per section what would be added,
changed (with the fields), unchanged or skipped (with the reason), and the
secrets to fill in. Manager definitions, templates, bots and own installs are
operator-only import sections: client preview/upload, apply and confirmation
all recheck the staged document and refuse them, including a changed staged
file. The existing client-accessible legacy sections retain their same-session
confirmation; confirmation never grants operator privileges. `config.import_apply`
uses the preview id. Operator apply, or a permitted client's confirmation,
recomputes and writes the plan in
one transaction on a current schema (it refuses a pending schema upgrade;
use `serve` and its backup gate for supported schema changes): an existing
entry is updated in place (an item keeps its id and installs), nothing missing from the file is removed, and a placeholder
keeps a stored secret, or leaves the value empty when there is none. Imported
items keep their origin. Importing the same file again changes nothing, and
exporting the new installation gives the same file.

Settings imports too (`looper/agents/config_imports.py`). `POST
/api/v1/config/import_upload` is the route that reads past the 8 MiB request
cap: it streams up to 256 MiB (twice the catalog's cap) to a 0600 file under
`<runtime root>/config-imports/`, after the Origin and Host checks, and
answers with an import id and the same preview. Apply and cancel use
`POST /api/v1/config/import_apply` and `POST /api/v1/config/import_cancel`.
An import left alone expires after an hour, and the server clears the
directory when it starts and stops. After applying, each secret to fill in
names the provider, MCP server or channel that takes it, and Settings links
to its form (`#settings/providers/<id>`, `#settings/catalog/mcp/<id>`,
`#settings/channels/<id>`).

Startup admits a database in one of three ways (`looper/db/connection.py`).
An empty database runs `schema.sql` once and records `post_bridge_baseline`.
A current database is schema 4 with that marker and does not replay
initialization DDL. A schema-3 database is the fixed v2.13.165 bridge when
its `schema.sql` fingerprint is one of the two recorded hashes
(v2.13.165–192), the tables and columns the conversion reads are present,
`schema_migrations` contains `file_state_import`, and the four role default
prompts match the bridge hashes. `ensure_schema` then runs
`upgrade_from_bridge` once, after serve has taken a verified full backup.
Anything else is refused: upgrade to v2.13.165 or a later 2.13 release first,
start it once, then upgrade. A schema version above 4 is refused by the
forward guard. `seeds.sql` is inserted on every start and does not change
saved choices. There is no historical migration chain and no
`registry_migration` module.

That database gate does not prove the filesystem, service, prompt or browser
steps that had to finish while the installation was still on v2.13.165. The
message journal is a separate local transition: `MessageJournal.transition`
runs once per runtime root, writes `.local/agent-bus/notice-identities-v1`,
and seals bridge notice identities before replay. Historical run records are
not rewritten at read time: `normalize_record_metadata` adds
`metadata.presentation` in the response and leaves the stored row alone.
The bridge conversion itself does write, once: a bounded task-config decode,
channel allowlists, missing Claude session identities, the exact pre-rename
system harness sentence, accounting basis, and gate channel ids. User files
and other historical rows stay.

`serve` takes a verified full custom-format `pg_dump` before that conversion.
A schema-only dump or a config export cannot restore history. Backup failure
stops the upgrade. `--skip-backup` is refused for this registry upgrade. The
dump is proved readable by, in order, `backup.verify_command` (dump on stdin),
`docker exec -i <container> pg_restore --list` when `backup.command` is
`docker exec [options] <container> pg_dump ...`, or the local `pg_restore`.
With none of these the backup fails before any DDL. The upgraded schema must
not be opened by an older binary: the forward guard in this binary does not
protect an older one.

Rollback means stopping the new service, leaving the upgraded database
unchanged, restoring the verified full backup into a separate database, and
pointing the guarded older release at that database. Do not downgrade or
mutate the original database. Writes made after the backup stay only in the
upgraded database. Import preview is not evidence that a database upgrade is
safe.

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
- **How a run's record ends** (`llm_records.status`, `RecordStatus`): it
  opens `running` and ends `completed` (a successful result), `failed` (an
  error, no result, the time limit or a spend cap) or `interrupted`: Looper
  stopped the run before it ended (a kill, its task stopped or cancelled, the
  server stopping), or the server running it died. Every run runs inside its
  recorder (`async with ClaudeRunRecorder`), which closes a record the run
  left open however the run ended: `interrupted` when it was cancelled,
  `failed` after an error in Looper's handling of it. A close goes on when a
  cancellation arrives meanwhile, for at most `CLOSE_TIMEOUT_SEC` (5 s). A
  run that ends without a result keeps what its usage events added up to: its
  tokens, and their cost when Looper prices the run; with Claude Code's own
  pricing its cost is not known. A Worker's or Reviewer's run that Looper
  stops after its result came keeps what that result reports, and a turn of
  the Planner or the Manager is recorded before its session is saved. A
  close that hits a database error is tried once more. At startup, before
  anything runs, `serve` ends every record still `running` as `interrupted`
  (`llm_records.end_unfinished_records`, logged with the count), with
  `finished_at` its last output (its start without one): each is a run a
  previous server that was killed, crashed or stopped did not see end, and its
  usage, held in memory until a run ends, is not known. A second start changes
  nothing.
- **A task's cost** (`llm_records.task_costs`) is the sum of its Planner's,
  Workers' and Reviewers' runs, in total and by role, with the run count. It
  also counts `running` runs (cost not known yet) and `unpriced_runs`
  (ended without a cost). A Manager turn about the task is not part of it.
- **An agent's usage** (`llm_records.agent_usage`, `POST /api/v1/runs/usage {agent_id, turns?}`) gives its total cost, runs and compactions, its cost
  by session and by UTC day, and its latest turns (oldest first) with each
  turn's context, cost and compactions. That is the Manager's and the
  Planners' context over time.

### The Manager's platform tools

Dashboard and Telegram/Lark use the same Manager configuration tools, including
`looper_models`; no new channel command is needed. The CLI moves the same model
layer through v3 export/import.

The Manager acts on Looper through MCP tools, one catalog in
`looper/agents/ops/` ([Manager tools RFC](rfcs/manager-platform-tools-v0.zh-CN.md),
T1, T2 and T3). Each tool has a name, a description, a
JSON Schema for its arguments, a risk tier and the code that runs it; the
code is what the dashboard's API calls (`projects`, the TaskRouter's
messages, `db.task_changes`, `agent_settings`, `profile_registry`, `catalog`,
`db.llm_records`, `schedules`), so both check alike and a refusal carries the
dashboard's own message (`ops.config.answered`: its 404 is `not_found`, its
409 `conflict`, its 400 `invalid`). The definitions of the 16 tools take
15.0 KB of the Manager's context (the JSON of `tools/list`, 14,988 bytes), the
contract 4.9 KB (4,865 bytes of `TOOLS_CONTRACT`).

- **Reaching Looper.** `looper serve` makes one `ManagerOps`
  (`ops/service.py`) with a random token at start. Every launch of the
  Manager's Claude has the MCP server `looper` in its `--mcp-config` (Codex:
  `[mcp_servers.looper]` in its `config.toml`; OMP: the generated plugin's
  `.mcp.json`), over a catalog server of the same name: `<python> -m
  looper.cli mcp-server` with `LOOPER_AGENT_URL` (this server, on loopback)
  and `LOOPER_AGENT_TOKEN`. That process (`looper/agents/mcp_server.py`) only
  forwards: MCP's `tools/list` is `GET /api/agent/v1/tools`, `tools/call` is
  `POST /api/agent/v1/ops/<tool>` with the arguments as the body, and the
  answer is the tool result's text (an error result when its `status` is
  `error`). The endpoint refuses any other token with 401.
- **The contract and `/profile`.** `TOOLS_CONTRACT` tells the Manager that
  the user switches the Agent Profile of the conversation with `/profile` and
  starts a new one with `/new`, that Looper answers both itself, that the
  model cannot switch it (it tells the user to send `/profile`), and that
  setting the manager role's profile with `looper_settings` applies to later
  sessions when this one has its own choice. `looper_settings` `view` with
  `role` `manager`, and a `set` on the manager role's layer, add
  `manager_session`: `{selected: {profile_id, name} | null}`, and the `set`
  a `note` when the session has its own choice (`ops/settings.py`).
- **The turn.** The Manager tells `ManagerOps` which turn runs: the chat
  (channel and chat id), the message it answers and its sender, or a turn
  Claude runs on its own. A call belongs to the turn running when it comes;
  without one it counts as Claude's own.
- **Answers.** `{"status": "ok", "result"}`, `{"status":
  "needs_confirmation", "confirmation_id", "expires_in_seconds"}` or
  `{"status": "error", "error": {"code", "message"}}`, the codes being
  `not_found`, `invalid` (the arguments fail the schema, or something only
  the user can give is missing), `needs_user_turn`, `forbidden`, `conflict`
  (the task's status does not allow it), `expired` and `unavailable` (the
  database failed; it may work later).
- **Tiers.** `read` runs in any turn; so do the actions of a tool that only
  list, get or view (`Tool.read_actions`), whose result the trail does not
  keep either. `write` runs only in a turn that
  answers a user's message. `high` runs only after the user confirms: the
  call is registered for its chat (one per chat, a new one replacing it; at
  most 50 chats), Looper adds what it does and 「回复「确认」就执行」 to the
  reply, and the user's next message in that chat starts with a `[Looper]`
  note naming its `confirmation_id`. Only in the turn answering that message,
  within 10 minutes, does `looper_confirm` run it, with the arguments it was
  asked with; after that turn it lapses. Another chat speaking in between
  changes nothing. Confirmations live in memory: a restart or `/new` drops
  them.
- **When the tools do not work.** Before a turn that answers a user, the
  Manager checks its MCP entry: it starts the entry's command as a runtime
  would and lists the tools through it (a success is trusted for 5 minutes).
  If the server does not start, or Looper refuses its token, Claude is not
  run at all. A request the endpoint refuses during a turn marks that turn,
  and Claude's reply is dropped. Either way the failure is logged as an
  error, and the user gets 「平台工具不可用，没有执行任何操作：<why>。…」, never
  a reply that may claim something was done.
- **No secrets.** No tool takes an argument named like a secret (a key,
  token, secret, password or credential): the call is refused with
  `forbidden`, the answer never repeats the value and says the user enters it
  on the dashboard (a provider's API key in Settings → LLM Providers). A
  provider is registered without its key; settings environments refuse
  credential-like names as the dashboard does, and MCP servers' values are
  listed masked.
- **The trail.** Every call is a `manager_actions` row: the chat, the
  message, the tool, its arguments (values under a key naming a key, token,
  secret, password or credential masked), the tier, how it ended, the result of a change
  and the `confirmation_id`. `GET /api/tasks/:id` lists a task's
  (`manager_actions`), and the task page shows them under "By the Manager".
  `manager_actions.manager_id` says which Manager made the call (no foreign
  key: the trail outlives a deleted Manager).
- **What a Manager may use and see.** Two independent settings of a row in
  `managers`. `tools` (`all` / `tasks`) says which tools it has: `all` is the
  whole catalog, `tasks` the task tools, `looper_confirm` and the read-only
  `looper_projects` list / get. `task_scope` (`all` / `own`) says which tasks
  it sees and may act on: `all` every task, `own` only the tasks it created.
  Any combination is valid. The default Manager is fixed to `all` / `all`; a
  scenario Manager defaults to `tasks` / `own`. `ManagerOps` enforces both in
  code (a call outside them is `forbidden`) from one immutable scope value
  that changing either setting replaces at once. Each Manager's per-turn
  `Ready tasks` and `Open gates` sections and the `/gates` list name only the
  tasks that Manager created, whatever its `task_scope`. Every change to what
  a Manager may do (`tools`, `task_scope`, `task_policy`, `enabled`, a
  template saved or removed) bumps `managers.revision`; `ManagerOps` compares
  it at each call and, when it differs, rebuilds its scope from the database
  on the spot. If that fails the call is `unavailable`; the old scope is
  never used. A confirmation waiting for the user is checked against the
  scope at the time it is confirmed.

| Tool | Does | Tier |
|---|---|---|
| `looper_overview` | Task counts by group, the running tasks (with their own Worker and Reviewer limits, when set) and step runs, each active project's load against its limits, today's spend, unhealthy components | read |
| `looper_tasks` | `list` (newest first; by `query` words in the id, name, requirement or project, by `group`, by `project`; at most `limit`) or `get` one task: status, phase, directory, requirement, steps, supplements (delivered or not), latest progress note, result, cost, and its Worker and Reviewer concurrency limits with their source (as `GET /api/tasks/:id` gives them) | read |
| `looper_task_create` | Create a ready task: `requirement`, `title`, `project` (matched by name ignoring case), `work_dir` (its workspace; else the project's default; a new project, or one without a default, needs one), `workspace_mode` and `base` (only when the user asks: [Task workspaces](#task-workspaces)), `goal_id`, `profiles` (an Agent Profile by id or name for some roles). The task records the chat it came from (its progress, and in Telegram or Lark its end notice). Looper adds the configuration block (how the task runs in its directory and each warning, the settings in effect for the task, with the layer each came from, the project's review policy, spend cap, lessons setting and the task's Worker and Reviewer limits) and the start question | write; a new project is high |
| `looper_task_control` | `start` a ready task (`TASK_START`); `stop` a running one; `restart` a stopped, failed or cancelled one, going on from its saved plan (`TASK_RESTART` `continue`); `start_over` one, planned anew with its steps, plan and Planner session deleted (`start_over`; the confirmation says how many accepted steps it deletes); `cancel` a running one | write; start_over and cancel are high |
| `looper_task_instruct` | Add the user's words to a running task as a supplement (`via` `manager:<channel>`), delivered to its Planner like any ([Changes to a task](#changes-to-a-task)) | write |
| `looper_task_gate_reply` | Add the user's message this turn answers to a user gate waiting for them (`task_id`, `gate`, `end` to end the discussion), as the task page does: Looper sends the message itself, or the `excerpt` of it the model names, which must be in it word for word; a turn that answers no message is refused. The channel message it is is kept, so the Planner's reply goes under it when it came from the gate's chat ([User gates](#user-gates)) | write |
| `looper_task_update` | Change a task: its `title` in any task (one line, at most 60 characters; a label, so an ended task can be renamed; recorded in the task's history as a delivered supplement `via` `manager:<channel>`), its `requirement` while it has not ended (a new version, as on the task page), its `work_dir` (a task not started only), its `profiles` in any task but a completed one (a profile per role, `null` to drop the task's choice, checked and written as `PUT /api/tasks/:id/agents` does; a Worker or Reviewer uses it from its next step, the Planner at its next turn after a handoff; a stopped, failed or cancelled task's restart runs with it), and its `system_prompt_modes` (`append` or `replace` per role, `null` to drop the task's choice; the same write as `profiles`, in the same call) | write |
| `looper_concurrency` | Change one task's (`task_id`) own `worker` and `reviewer` limits, `limits` per role (1–64, `null` clears one to the default), in any task but a completed one; `planner` is refused. There are no project limits: the description tells the Manager to say so, and a call naming `project` is refused (`invalid`, an unknown argument) and changes nothing. Then the WorkerPool checks again. The answer gives each role's limit before and after with its source, the task's own and the default, and says that lowering stops nothing that runs | write |
| `looper_projects` | `list` (name, default directory, task, active task and schedule counts, background cut to 200 characters), `get` one (as `GET /api/projects` lists it), `create` (`name`, `work_dir`, `background`), `update` its `name`, `work_dir` or `background` (`projects.create_project`, `update_project`), `delete` one with its tasks and schedules, stopping its running tasks first (the user confirms first). Template use and another Manager's tasks or schedules are refused before confirmation. The confirmation lists schedule, running-task and task counts; settings, allowed list, installs, knowledge and own skills go with it, while result branches and the project directory's files stay | list and get read; write; delete is high |
| `looper_agent_profiles` | `list`, `create`, `update`, `delete`; create selects `runtime` and `model` (model id or exact `{provider, name, protocol}`), plus `name`, `effort`, `agent_teams`, `custom_prompt`; no profile system-prompt mode. Returns derived account/model information; selected profiles cannot be deleted | list reads; write; delete is high |
| `looper_providers` | `list` accounts and vendor suggestions; `register` an API-key account with `name`, `vendor_type`, `endpoints`, `env`, `max_concurrency`, without a key. The user enters the key in Settings | list reads; write |
| `looper_models` | `list` (optional provider/runtime filter), `create` an exact provider/name/protocol entry, `update` its settings, `delete` an unused model; no key arguments | list reads; write; delete is high |
| `looper_catalog` | `list` the items (kind, qualified name, description, `manager_install_count`) with their installs, or with `project` the installs that reach it (`catalog.project_view`); `install` an item (by id or qualified name) globally or in a project for `roles`, `update` an install's `roles` or `enabled`, `uninstall` (`catalog.install`, `update_install`, `uninstall`) | list reads; write |
| `looper_costs` | One task's cost (`task_id`: `GET /api/tasks/:id`'s `cost`), or what the runs started in a range cost (`period` `today`, `this_week` or `this_month` from the server's local midnight, or `since` and `until`, a day or an ISO date-time, a day as `until` counting whole; all time by default), money and subscription equivalent apart as the overview's today: by project (their tasks' runs; the total also counts the Manager's), or with `project` its total and its 10 costliest tasks (`llm_records.spend_since`, `task_spend`) | read |
| `looper_settings` | `view` the shared defaults and each role's layer, what applies to each role with its source and its timeout, or with `project` its layer, its roles' layers and its allowed profiles; `set` `values` on one layer (neither `role` nor `project`: the shared defaults; `role`; `project`; both: the role in the project): `profile` by id or name, `harness_prompt`, `env` (the layer's whole `{NAME: value}`), `run_budget_usd`, `max_concurrency` (the `worker` or `reviewer` role: the default for every task), `timeout_seconds` (a role alone, as Settings → Roles saves it), null clearing one, written by `agent_settings.update_system`, `update_role` or `update_project` and letting what waits check again after a limit; `allowed_profiles`: `add` profiles to a project's list or `remove` some (one not on it is refused), then the whole list is set by `projects.set_allowed_profiles`, which checks each. The answer gives each value before and after and what now applies | view reads; write; the shared defaults, a role's default concurrency limit and a higher or removed spend cap (any run of the layer's roles, resolved through the layers) are high, shown before → after |
| `looper_schedules` | `list` every project's schedules as the Schedules page does (`schedules.list_all_schedules`; by `project`, by `enabled`): id, name, project, rule, zone, the rule in words, kind, enabled, user gates, next fire, last run (kind and exit code included), failures in a row; `get` one (`GET /api/schedules/:id`, the requirement cut to 1,000 characters) with its 5 latest runs, the routine skips left out and counted, as its page shows them; `create` one in an existing `project` from `fields` (the `fields` that `POST /api/v1/schedule_admin/create` takes, `profiles` being its `profile_overrides`, and `task_limits`), always disabled (`created_via` `manager`): Looper adds its configuration (project, directory, the rule in words and its next 3 fires in its zone, overlap, pre-run gate, user gates, caps, notify, profiles, task limits for agent schedules; kind, command in a verbatim code fence, timeout, env keys only, env file, success deadline and alert thresholds for command schedules) and 「要启用吗？回复「启用」就启用」 to the reply, and the turn that created it may neither `enable` nor `run` it (`conflict`: the user has not seen it); made from a Telegram or Lark chat without a `notify_target`, it tells whoever asked when their channel's allowlist lets them; `update` the `fields` given (`schedules.update_schedule`; null clears one, `profiles` and `task_limits` are replaced whole), and Looper adds each change before → after (commands in verbatim code fences, and the next 3 fires for a new rule or zone); every command schedule update also says it uses no agent or model spend; `enable` one (as its switch does; the answer says when it was so already) or `run` it now (`schedules.run_now`: the dashboard's Run now, through the overlap check, its next fire time left; the answer is the run), each asking first: a command schedule shows its exact command in a code fence, directory, timeout and env keys only (plus env file when set); enabling also shows its success deadline and alert thresholds, and both confirmations say it runs without an agent or model spend. An agent schedule keeps its task and spending confirmation. The call answers `needs_confirmation` and does nothing until the user's next message confirms (a schedule already enabled is not asked), and the turn that created a schedule may do neither (`conflict`); `disable` one; `delete` one (`schedules.delete_schedule`); `reset_planner_session` clears its stored Planner session and holder after the user confirms in their next message. The reset confirmation says whether a session is stored, that the next run starts fresh, and that a run in flight keeps its own session until it ends and cannot write back. A schedule is named by its id, or by its name ignoring case, within `project` when given; a name more than one project has is refused (`invalid`) naming each with its project and id, an unknown one (`not_found`) naming the schedules there are. The delete confirmation shows its name, project, rule and zone, whether it is enabled, the runs in progress (a running task goes on and ends normally, a queued or gating run starts no task) and that its tasks stay while its run history and messages are deleted | list and get read; write; enable, run now, delete, reset_planner_session, and an update that raises or removes the run or daily spend cap (checked as the update would be before anything is asked, shown before → after), are high |
| `looper_config_export` | Write the portable configuration file (`include_projects`: `--include-projects`), never with secrets (no argument takes them; a secrets argument is `forbidden`), to `<runtime root>/exports/looper-config-<UTC time>-<id>.json`, never to a path from chat; the answer gives its path, format, size, the counts per section, the secret values left as placeholders, the path documents to re-point and how to import it (`loopercli config import`, or Settings → Import / Export) | read |
| `looper_confirm` | Run the call waiting for confirmation in this chat | — |

Continuing a task that failed at its spend cap needs a higher cap, which is
not one of these tools yet; nor are a project's execution settings, a role's
prompt, name, turns and switch, changing or removing a provider, or adding
items to the catalog.

API clients run the same catalog. `ClientOps` wraps it for an API client: a request is a turn (a write runs; one of risk
`high` answers 202 until `/api/v1/confirm`), and reads are not recorded. Attribution is kept: `client_id` on the
conversation's threads, on the tasks it creates and on the Manager calls, and `via` is `client:<name>` for a call made
through the API and `manager:loopercli` for one the Manager made in a client conversation. The Manager sees such a
message with the line `Channel: loopercli, client "<name>" …`, and a task created in a client conversation reports its
acceptance and end into that conversation (`origin_thread`), for the default Manager too.

### Task reports

A task reports to the Manager that created it (`tasks.manager_id`; the
dashboard's and a schedule's tasks belong to the default Manager). Its
progress and end go to that Manager, which tells the chat the task came from.
A task asked for in the web chat records the chat thread (`tasks.origin_thread`);
its acceptance and its end are written into that thread and reach an open
dashboard through the thread's session subscription. When a Manager is
disabled, its queued and in-flight reports are handed to the default Manager
instead of being dropped.

### Task templates

A scenario Manager can carry task templates (`manager_task_templates`, one
row per `manager_id` and `key`; `looper/agents/task_templates.py`). A template
is what the operator fixes for one kind of task: `name`, `description`, a
fixed `project_id`, `work_dir` and `workspace_mode`, a `requirement` with
`{{input_key}}` placeholders, `planner_instructions`, an Agent Profile per
role (`planner`, `worker`, `reviewer`), `role_templates` (keys of the
project's role templates) and `inputs` (at most 10: `key`, `label`, `kind`
`text` or `url`, `required`, `pattern`, `hint`). The API checks all of it
when it saves; a `pattern` is compiled and matched in a short-lived
subprocess with a 2-second limit, never on the event loop.

A Manager with templates has a `Task templates` section in each turn, and its
`looper_task_create` takes two more arguments, `template` (one of its keys)
and `inputs`. With `task_policy` `templates_only` a call without `template`
is `invalid`. With a template, and before anything is written, the code:

1. reads the template (it must belong to this Manager) and checks `inputs`:
   unknown keys, a missing required one, a `url` that is not http(s), a
   `pattern` that does not match, each value trimmed to 2000 characters;
2. merges the template's fixed fields with the call's: the template's value
   wins, the call may repeat it but not change it (`invalid`), and
   confirmation and creation use the same merged project, directory and
   mode;
3. checks again that each merged Agent Profile can run its role and that the
   project's Planner-defined roles are on when `role_templates` is set
   (`conflict` otherwise);
4. fills the placeholders of the template's `requirement` in one pass,
   followed by the Manager's own `requirement` (together within the
   requirement limit);
5. in one transaction, locks the Manager's row (it must still be enabled),
   creates the task with `manager_id` and the `template` snapshot, writes the
   Profile overrides and copies the role templates into `task_roles`
   (version 1, `via` `template`).

The snapshot (`tasks.template`) keeps the template's key, name, inputs,
`planner_instructions` and every role copied, as the template said then; later
edits of the template do not change the task, and planning anew
(`start_over`) restores the copied roles from it. The Planner's `## Task`
section gains, before `Requirement:`, the lines `Template: <name>`,
`Template inputs:` and `Template instructions:`; a task without a snapshot
gets none of them. The instructions go to the Planner only. The repository
ships an example, `docs/examples/manager-github-pr-review.json`.

### A task's Planner's tools

Each task's Planner has four MCP tools of its own, which act on its task
only (`looper/agents/ops/planner.py`, [Manager tools RFC](rfcs/manager-platform-tools-v0.zh-CN.md)
section 8.3, decisions 9 and 10), two more for its roles where its project
lets it define them ([Roles a task's Planner defines](#roles-a-tasks-planner-defines)),
four of its task's agent room where its project has the room on ([A task's
agent room](#a-tasks-agent-room)), and the knowledge vault's three where its
project has the vault on ([The knowledge
vault](#the-knowledge-vault-the-operators-notes-read-and-written-through-looper)).

- **Reaching Looper.** `looper serve` makes one `PlannerOps`. A Planner gets a
  token for its task when it starts (`planner.` and 43 random characters,
  kept as a hash and never logged); its MCP entry is the Manager's,
  `looper mcp-server` with `LOOPER_AGENT_URL` and that token, on all three
  runtimes the same way. The token stops working when that Planner stops (its
  task ended, or a new Planner of the task replaced it) and once its task has
  ended; the endpoint answers 401 then, and the Planner removes the MCP
  configuration files that carried it. The same `/api/agent/v1/` endpoint
  serves it: a `planner.` token lists and calls the Planner's tools only. A
  call naming another task, a Manager tool called with a Planner's token, or a
  Planner tool with the Manager's, is `forbidden`.
- **Turns.** `read` runs in any turn; `write` only in a turn Looper sent the
  Planner, never in one its Claude runs on its own (`needs_user_turn`).
  Nothing asks the user first. A refused write is noted in the task's
  activity.
- **How it uses them.** Its system prompt says so after the reply contract
  (`TOOLS_GUIDE`): when to choose a profile or change a limit, that the list
  may be empty, that it never chooses its own profile, and to give a reason.

| Tool | Does | Tier |
|---|---|---|
| `looper_task_profiles` | The profiles its project allows (id, name, runtime, model, effort, which of the Worker and Reviewer each can run, the input and output price per million tokens, `null` where Looper does not know it), what the Workers and the Reviewers run now with its source, the user's choice for the task, and the Planner's own choices, per step too | read |
| `looper_task_profile_set` | `{role: worker \| reviewer, profile_id: id or name \| null, step?, reason?}`: run that role on a profile the project lets its Planners choose (its list, or any profile that can run the role while the list is empty), for the rest of the task or for one step that has not ended (each attempt of it); `null` clears its choice. Refused with a profile the project does not allow (not on its list, or not one that can run a Worker or Reviewer; `forbidden`), when the user chose that role's profile for the task (`forbidden`: theirs stands), for the Planner role, an ended or unknown step, or a profile that cannot run the role, with the reason the dashboard gives (`invalid`). Each change is a `profile_choice` journal entry and an activity line | write |
| `looper_task_concurrency` | `{limits: {worker?, reviewer?}, reason?}`: the task's own Worker and Reviewer limits, as `PUT /api/tasks/:id/agents` sets them | write |

The concurrency tool goes through the Manager's code (`set_task_limits`) and
applies at once; then the WorkerPool checks again. Each change is a
`concurrency_change` journal entry (each role's limit before and after, and
why), and a task asked for in Telegram or Lark tells that chat
(`CHAT_NOTICE`, to its `NotifyTarget`); a task from the web chat is only
journaled. There is no project limit to change: concurrency is per task
([Concurrency limits](#concurrency-limits)).

A Worker's or Reviewer's run reads the Planner's choice when it starts
(`load_agent_settings(..., step_id=...)`): a call that returns before the
reply that dispatches a step (a plan, a retry, a replan) applies to it, a run
that started keeps its profile, and one still waiting for room in its task
uses the choice in effect when it starts. The precedence, highest
first: the user's choice for the task, the Planner's for the step, the
Planner's for the rest of the task, then the setting layers. A choice counts
only while the task's project allows its profile (on its list, or any
profile while the list is empty): a list set later that leaves a profile out
drops the project's Planners' choices of it, and a task moved to another
project runs what that project allows. Planning a task anew
(`start_over`) drops its Planner's choices; going on from its plan keeps
them.

The list, `project_allowed_profiles`, is empty by default, and an empty list
lets a project's Planners choose **any** registered profile that can run the
role; a non-empty list restricts the choice to it. One function decides
(`projects.choosable_profiles`, used by `looper_task_profiles`,
`looper_task_profile_set`, `task_roles.allowed_profile` and the SQL that reads
a stored choice, `projects.profile_allowed_sql`); a task without a project
behaves like an empty list. The maintainer edits the list on the project's
Agent Profiles tab (row "Profiles the Planner may choose", empty: "Any
profile (no restriction)"; `POST /api/v1/allowed_profiles/{get,replace}`, with `project_id` in the body). A
pricier profile needs no confirmation: the setting is the consent, and the run
budget caps still hold. A profile on a list cannot be deleted (its usage names
the project); a profile a Planner chose in a task that has not ended cannot
be deleted either, whether it is a choice for the Workers and Reviewers or for a role the Planner defined (the refusal names the task: clear the choice or wait; checked and deleted under one lock on the profile row, so a choice being written is never dropped by the cascade),
while the choices of ended tasks go with the profile.

### Roles a task's Planner defines

In a project with `planner_roles` `on`, a task's Planner may define the roles
its steps run as ([Planner-defined roles RFC](rfcs/planner-defined-roles-v0.zh-CN.md),
R1 and R2; `looper/agents/task_roles.py`, `looper/agents/role_api.py`). A role is an `id` (unique in the task, not
a built-in role's name), a `kind`, a `role_prompt`, an optional `harness`,
an optional Agent Profile from the project's list and optional catalog items
its runs load (`items`). Nothing changes in a
project that has the switch off (the default): its Planner's tools, reply
contract and tools guide are as before, and a step that names a role is
refused with that reason.

- **Defining them.** Two more Planner tools, listed only where the switch is
  on (`PlannerOps.listing` is per token; elsewhere a call is `forbidden`):
  `looper_task_roles` (read: each role's current version and the steps that
  name it, whether the user wrote it (`edited_by_user`) and the template it
  came from, the project's role templates, the profiles the project allows
  with the kinds each can run, the project's `role_text_max_chars`, and
  whether the user chose the task's Worker or Reviewer profile) and
  `looper_task_role_set` (write: `{id, kind, role_prompt, harness?,
  profile_id?, template?, reason?}`, the whole definition each time). Each
  call that changes a role adds a version (`task_roles`, one row per version,
  with who wrote it, `via` `planner` or `user`, and the template it was
  copied from, `template_key`) with a `role_defined` journal entry in one
  transaction; the same definition again adds none. A version is written only
  on top of the one its writer read (`task_roles.add_version`'s `after`), so
  two writers never pass each other: the later gets `conflict` (409 on the
  dashboard) and reads again. Refused, with what to fix: a reserved or
  malformed id, a kind other than the role's own, an empty prompt, a prompt
  or harness over the project's limit, an unknown template or one of another
  kind (`invalid`); a profile the project does not allow, or
  when the user chose that kind's profile for the task, or a role whose
  current version the user wrote (`forbidden`); a profile that cannot run the
  kind (`invalid`). Writes only in a turn Looper sent the Planner, as for its
  other tools.
- **Templates.** A project keeps role templates (`project_role_templates`:
  `{key, kind, description, role_prompt, harness?, profile_id?}`), written on
  its Roles tab (`POST /api/v1/role_templates/{list,put,delete}` with `project_id`,
  and `key` for put/delete). Put supplies the full definition, including nullable
  `harness`/`profile_id`, `description` and `items`; definitions are checked as a task role is
  (`check_definition`, `allowed_profile`), whatever the switch. With the
  switch on, its tasks' [PLAN] and [RESTART] messages list them, one line
  each (`key (kind): description`), under a line that a template is no
  role until the Planner defines one from it, and `looper_task_role_set` with
  `template` copies one as a task role: the id defaults to the key, the
  fields the call gives override the template's, and the template's profile
  comes along while the project still allows it (otherwise it is left out
  and the answer says so). A copy is the task's own role from then on: a
  later change of the template changes no task. The configuration file
  carries a project's templates (`role_templates`, the profile by name, only
  for a project that has some); an import adds and changes them, keeps the
  ones the file does not give, skips an invalid one with the reason, and
  leaves out a profile that is not here, with a note.
- **The user's edit.** On the task page, the user edits a role while the task
  has not completed (`POST /api/v1/tasks/role_update`, `{task_id, key, role_prompt,
  harness, profile_id, items?}`: the whole definition; the kind stays; a profile
  from the project's list). It adds a version by the user (`via` `user`) with
  its `role_defined` entry and a supplement (via `task_page`) that tells the
  Planner, in one transaction; a Planner that runs gets it in a turn at once
  (`TASK_CHANGE`), a stopped task's at its restart. From then on the Planner
  cannot change that role (`forbidden`, naming the version): it names the
  role as it is or defines another under a new id. The attempts that start
  afterwards run the new version. Refused: an unknown task or role (404), a
  completed task (409), a definition a task role would be refused or one
  that changes nothing (400).
- **Naming them.** With the switch on, the step shape (PLAN, a DECISION's
  `replan`, CHAT's `add_steps` and `replan`) takes `role`, a work role its
  Worker runs as, and `review_roles`, the review roles that review it in
  that order, which need `"review": true`: each item is a review role's id
  (required) or `{"role": <id>, "required": false}` (advisory), no role
  twice. The single `review_role` and `review_role_required` of v2.7.0 are
  refused with what to write instead. `reply_contract(roles=True)` says so
  and `_check_new_steps` checks them against the roles read after the reply
  arrives, so a role defined in the same turn counts. A started step keeps its roles, like the rest of it.
  The plan lines and results in the Planner's turns name a step's roles.
- **A run as a role.** `kind` `work` takes the built-in Worker's place, `review`
  reviews after the built-in Reviewer; each keeps its kind's row (timeout,
  turns), installs, concurrency and contract (`WORK_CONTRACT`,
  `REVIEW_CONTRACT`). Its system prompt (`system_prompt.build_role_prompt`)
  is its working style (its own harness; else the built-in harness's How you
  work and Communication, `WORKING_STYLE`, or a layer's harness whole, which
  is the operator's), its role prompt in place of the row's, the project, the
  task section, then the boundaries (a code-owned lead saying they hold over
  everything above, then the shared defaults' `boundaries`, else the built-in
  `HARNESS_BOUNDARIES`), then the contract: nothing the Planner or the user
  wrote follows them. A review role's task section adds, after the acceptance
  criteria, that what its role asks decides its verdict too (`passed` is false
  when the work fails either); `REVIEW_CONTRACT` is unchanged. The built-in harness is those three parts joined, the
  same text as before; a step without a role is prompted exactly as before.
  `boundaries` is set on the shared defaults only (Settings → Roles, Shared
  defaults; the Manager's `looper_settings`; exported with the configuration);
  another layer's API refuses it.
- **Its profile.** The user's choice for the task (`task`), then the role's
  profile while the project allows it (`Layer.TASK_ROLE`, `task_role`), then
  what the step's built-in role of that kind would get (the Planner's choice
  for the step, for the rest of the task, the layers). Read when the run
  starts.
- **Its items.** A run as a role loads its kind's built-in role's installs
  and the catalog items the role names (`items`, ids;
  `library.load_installed`), each only while it has an enabled install for
  the roles a task's Planner defines (`library_installs.roles` includes
  `task_role`, `catalog.TASK_ROLE`), global or in the project: the
  operator's consent, as the profile list is. Such an install loads for no
  built-in role; no install names `task_role` unless asked to (the API's
  and the dashboard's defaults are the built-in roles). The Planner names
  items by id or name from `looper_task_roles`' `role_items`; at most 20,
  each once; another is `forbidden` with the list. The run's record lists
  what it loaded (`library`).
- **Versions.** An attempt (a Worker run, or a review role's run) reads the
  role's current version when it starts and the step keeps it
  (`role_version`, each `review_roles` item's `version`); the dispatch
  carries that version (`task_role`). A run that started keeps its own, a server restart runs the
  same attempt with the version it had, and a retry or a new step reads the
  current one.
- **The review gate.** The built-in Reviewer is the gate wherever a step needs
  review; a review role only adds a review, and never replaces it. A step with
  `review_roles` is reviewed by the built-in Reviewer first (`review_stage`
  `builtin`); only when it passes does code start the first review role's
  Reviewer (`role`, `review_position` 0), then each next one, with no
  decision turn between. Each verdict is kept in order (the step result's
  `role_reviews`). A required review role (the default) blocks: when it does
  not pass, the ones after it do not run, and the decision turn does not
  allow accept. An advisory one does not: when it does not pass, the next
  one still reviews; the decision turn after the last allows accept as long
  as every required one passed, lists every verdict in order and says which
  are advisory, and the Planner accepts, retries with feedback or replans; an
  accept over an advisory failure says so in the `step_accepted` entry, and
  the task page labels the role "advisory". A failed built-in review goes to
  the decision turn at once. Without a decision the chain decides: accepted
  only when the built-in Reviewer and every required review role passed. A
  review role that cannot be read fails its review, as a verdict would. A
  restart dispatches the review the step waits for (`review_position`, with
  the version that item ran); a retry clears every verdict. A v2.7.0
  database's steps (`review_role_key`, `review_role_required`,
  `review_role_version`, a result's `role_review`) become lists of one, and
  those columns go (`schema.sql`).
- **Restarts.** Going on from the plan keeps the roles; planning the task anew
  (`start_over`) deletes them, with the Planner's profile choices. Turning the
  switch off leaves the steps that name a role running as it; no role is
  defined or named after that.
- **What is seen.** `GET /api/tasks/:id` gives `roles` (each role's current
  version, its versions, the steps that name it and its `stats`: runs, cost,
  steps accepted and failed, retries, contract violations and their rate,
  reviews passed and not and the pass rate) and `builtin_role_stats` (the same
  for the built-in Worker and Reviewer), from the run records' `outcome` and
  `prompt_layers.task_role` (`llm_records.role_runs`) and the steps. The task
  page shows a Roles section (each version says when the user wrote it or
  which template it came from, and Edit opens the user's edit), a step's
  roles in Steps and the Pipeline, and `role_defined` entries in Findings;
  the project's Execution tab has the switch and the limit, and its Roles
  tab the templates and the roles across its tasks (`POST /api/v1/project_roles/stats`
  with `{project_id, since?}` (`since` is YYYY-MM-DD): each role id's numbers
  summed over the project's tasks with roles, with how many tasks it is in
  and the templates it came from, one id possibly another definition in each
  task; and the built-in Worker's and Reviewer's in the same tasks). The
  Manager's `looper_tasks get` adds the task's `roles` (id, kind, version)
  and each step's `role` and `review_roles` when there are any; its
  `looper_catalog` is not told of `task_role` installs (the dashboard
  makes them).
- **A run's name.** Wherever a run is listed or named, a run as a role names
  the role: `task_roles.run_role` reads `{id, kind, version}` from what the run
  recorded (`prompt_layers.task_role`) or its dispatch carries, and the API
  adds it as `task_role` to each such record of `runs.list` and
  `runs.get`, each such run in `GET /api/overview`'s Workers
  and Reviewers, and each such step agent of `POST /api/v1/operator_chat/targets`; `GET
  /api/tasks/:id` adds `run_roles` (agent id to role; for an attempt that
  waits for room, from the versions its step took at dispatch). A built-in
  role's run has none of these, and a task without role runs no `run_roles`.
  The Agent Timeline, a History page's header, the Runs page and the Chat
  page show `fe_rd · work` (`security_review · review`) in place of Worker
  (Reviewer); the Overview shows the role's id under Workers or Reviewers. The
  Manager's `looper_overview` names the role of each step run as one
  (`task_role`, its id), so what it tells a Telegram or Lark chat about what
  runs names it too; the task's progress and end notices there name no run.
  The CLI lists no runs.

The two tools add 2.5 KB to the Planner's `tools/list`, their guide (after
`TOOLS_GUIDE`, `ROLES_GUIDE`) 2.4 KB and the contract's role rules 0.5 KB, in
a project with the switch on only; a project's templates add a line each to
its [PLAN] and [RESTART] messages. The guide opens with the rule that a role
is defined with `looper_task_role_set` before any step names it, then has the
Planner define one work role per discipline when the work spans disciplines
or platforms (none for a task of one, unless asked), pin the interface the
platforms share in a first
step, run the platform steps side by side and integrate last, and write each
role prompt as a brief: the expertise, the platform's conventions and quality
bar, what the role owns and must not touch, how it hands off, and the checks
it runs before it reports.

### Wait steps

A plan's `wait` step holds its dependents until a time or check is ready.
The server loads the system wait limits at startup. `WaitService` checks
due waits while their tasks are active; it stops and reaps in-flight checks before
the agents and bus stop. A task that only waits has status `waiting`
(group `in_progress`); a user gate still takes precedence as `waiting_user`.
The task detail lists waits in plan order, including their check, timing,
latest result and resolution. The dashboard's resume and cancel requests
conditionally move only `waiting` rows to `ready` (outcome `resumed`) or
`cancel_requested`, record activity and wake an existing Planner. A stopped
task keeps that state for its restart. Once timed out or cancellation is
requested, the Planner decides what happens next; user actions return 409.
See the [wait-step RFC](rfcs/plan-wait-step-v0.zh-CN.md).

### User gates

In a project with `user_gates` `on`, also when unset (the default), a task's
Planner may make a step of its plan a gate, where it discusses with the user
what only the user can decide ([User gates RFC](rfcs/user-gate-v0.zh-CN.md);
`looper/agents/user_gates.py`). A task a schedule started also needs that
schedule's `user_gates` `on`; schedules default to `off` and keep the previous
unattended behavior and byte-identical Planner prompt. A project with gates
`off` always refuses new gates. The refusal says why and what to do instead.

A scheduled gate's wait counts against `run_timeout_seconds`; it does not
pause the run timeout or budgets. The task stays active while waiting, so
`overlap=skip` skips later firings. Overlap, catch-up and spend limits keep
their existing behavior. These user gates are unrelated to `gate_command`
and `gate_timeout_seconds`, which check a schedule before a run starts.

- **The step.** `{"id", "kind": "gate", "topic", "agenda": [{"question",
  "options"?}], "depends_on", "findings"?}` in a `[PLAN]` reply or any
  `add_steps` or `replan`; agenda items are numbered `q1`, `q2`, … by code.
  The step (`task_steps.kind` `gate`, its instruction the topic) has its
  gate in `task_user_gates`: `pending` until the steps it depends on are done,
  then `open` (the step `waiting_user`), `discussing` from the user's first
  message, and `passed` or `cancelled` (the step `verified`, so the steps
  that depend on it start) or `skipped` (replanned away before it opened).
  An open gate counts as started: a replan keeps it.
- **While it waits.** The steps that depend on it wait; the rest of the plan
  runs. The Planner reports the task as `waiting_user` (group `needs_you`;
  the plan's own status stays `executing`). Once the task only waits for the
  user (no step in flight) and no turn came for 15 minutes
  (`GATE_IDLE_STOP_SEC`), its resident Claude stops; the next turn resumes
  its session, and a new session's resume brief carries each waiting gate's
  discussion so far.
- **The discussion.** The user's messages (`task_user_gate_messages`, from
  the dashboard: `POST /api/v1/ops/gates/reply` with `{task_id, gate, text, agenda_item?, option?}`,
  or `POST /api/v1/ops/gates/end` with `{task_id, gate, text?}` to end
  the discussion) are stored first; `GATE_MESSAGE` only wakes the Planner,
  which gives a gate's undelivered ones (after its `delivered_through`)
  together in one `[GATE]` turn after the event it is in. The reply lists
  the whole agenda (each item `open`, `resolved` with its answer, or
  `dropped`), `gate` `continue` or `close`, and may change the plan as a
  `[CHAT]` reply does. Closing needs `summary` (`decisions`, `assumptions`,
  `open`) and every item resolved or dropped; after the user ended the
  discussion it must close, with an assumption for each item still open;
  closing the last open step without new steps needs `final`. The summary
  is kept, in the transaction that closes the gate, as a delivered
  supplement (`via` `gate`, at most 3,000 characters) that every later
  Worker and Reviewer reads; steps an `add_steps` adds are saved with it. A
  turn that fails notes the error in the discussion and leaves the messages
  undelivered (`failures`, `failed_through`): the gate then waits for the user
  (`waiting_on` `user`, `not_delivered` counts them, reminders come again)
  until the Planner's automatic retry is due (`retry_at`: 1, then 5 minutes
  after a failure, `RETRY_WAIT_SEC`); after the second the user's next message
  sends them with it. A message written while a `continue` turn runs reaches
  the next one; one written while the turn that closes or cancels the gate
  runs becomes, in the close's transaction, a supplement (`via` `gate_late`)
  that the next `[CHAT]` turn gives the Planner, with a `late` note in the
  discussion. When that close would complete the task, the `[CHAT]` turn comes
  first, and its reply may add steps instead.
- **Scheduled delivery.** A scheduled task's gate messages go to its schedule's
  `notify_channel` and `notify_chat` when set, else to the system setting
  `user_gates.dashboard_target`, else only to the task page. The existing
  channel allowlist and courier checks apply; a configured target removed
  from the allowlist gets no delivery. `/gates` and `/gate N` answer these
  gates through the same channel commands and gate API as attended tasks.
- **Answering from a chat.** In a Lark or Telegram chat the user can answer
  without the Manager's model (`looper/agents/gate_commands.py`; the Manager
  takes these before the gate routing, and neither they nor the replies are
  stored in its chat history or shown to its model). `/gates` lists the gates
  that wait for the user: in a private chat all of them, across chats and
  tasks (group tasks and dashboard tasks too); in a group that group's. The
  list is numbered and the numbers are a snapshot of the last list sent in
  that chat (for one sender, one hour): `/gate N <answer>` stores the answer
  as the user's own message in gate N, and `/gate N end` ends the discussion,
  both through `gate_service.post_message` (`via` `command:<channel>`), so a
  number never lands on another gate than the one listed; a gate that no
  longer waits answers "list again". Replying `end` under a gate's card ends
  it too. Anyone on the channel's allowlist may answer or end (no owner
  check). In Lark a group message needs the @-mention, which is stripped
  before the text reaches the handler; Telegram registers `/gates` and `/gate`
  (also `/gate@bot`) and lists them in the bot's menu (`setMyCommands`). The
  receipts are code's text, in Chinese like the courier's.
- **Changing a gate.** A `[DECISION]` or `[CHAT]` reply may carry `gates`:
  `{"id", "action": "update", "topic"?, "agenda"?, "findings"?, "note"}`
  (the note, for the user, is required for an open gate) or `{"id",
  "action": "cancel", "resolution"}`, applied before its decision or action;
  a reply that cancels the other open gates and so completes the task (a
  decision, or a `[CHAT]` or `[RESTART]` reply that adds no step) needs
  `final`.
- **Restarts.** A server restart reads the gates back. Going on from the plan
  keeps a waiting gate waiting, with a `resumed` note, and gives it the
  messages the user wrote while the task was stopped (they are kept:
  `delivered` `kept`); starting over drops the gates and their discussions,
  and keeps the conclusions already written, like every supplement.
- **Reminders.** Every 5 minutes `GateReminders` notes each gate that has
  waited for the user for its project's `gate_reminder_hours` since its
  opening, the last message the user or the Planner wrote in it and its last
  reminder: a `reminder` note in its discussion, which the courier posts to
  the gate's chat, and a line in the task's activity.
- **The chat.** A gate's messages also go to a chat (`gate_service.gate_chat`):
  the Lark direct message or group (the requester @-mentioned) or the
  Telegram chat its task came from, else, for a task asked for on the
  dashboard or in the web chat, the chat the system setting
  `user_gates.dashboard_target` names (`POST /api/v1/gate_settings/get` and
  `POST /api/v1/gate_settings/update`, Settings
  → Channels; one a channel's sender allowlist lets in, on a channel that is
  on; it counts only while the allowlist still lets it in, and Settings says
  when it no longer does; unset, the task page only). `GateCourier` posts
  every 2 seconds the Planner's and Looper's messages of a discussion no pass
  has handled (`post_state`), oldest first, through the channel's `post`, and
  records the channel messages each became (`task_user_gate_posts`) or why it
  failed (the gate's `channel_posts`, and an error in the activity): the card
  is a message of its own, every later message replies under it, a Planner's
  reply under the user's message it answers when that came from the gate's
  chat (a Lark direct message counts as its sender's open_id, the chat Looper
  posts to). A post that sent nothing is tried again after 2 and 8 minutes
  (`post_attempts`, `post_retry_at`; three tries in all) before it is
  `failed`, and the gate's later messages wait behind it. Each post carries
  the idempotency key `gate-<message id>` (Lark sends one message per key
  within an hour; Telegram has none, so a post retried after a lost answer may
  arrive twice); a reply whose message is gone is posted on its own (Lark: the
  reply API's 230011, a recalled message, which a deleted one counts as; Telegram:
  `allow_sending_without_reply`). A card, update, reminder or "still waits"
  message whose gate no longer waits when it is posted (after a backlog) is
  not posted. The texts are code's, in Chinese; a Planner's reply goes as
  written; Telegram gets plain text; in a Lark group what waits for an answer
  says to @-mention the bot. Messages written before this are never posted
  (the migration marks them `none`).
- **Replies in a channel.** The Lark adapter puts a message's `reply_to` and
  `root_id` in its `channel_context`, the Telegram one its `message_id` and
  `reply_to_message_id`. Before the Manager's model, `gate_routing` looks up
  the message a reply answers directly (in Lark `root_id` only when it names
  none, so a reply to the Manager's own answer in a gate's thread stays the
  Manager's) among the posted messages and the user's messages of gates (a
  Lark message id in any chat, a Telegram one within its chat): a reply about
  a gate that waits goes into its discussion (`via` the channel, the group
  sender as its author, the channel message kept so the Planner's reply goes
  under it) and wakes its Planner, the model never sees it and the chat's
  history keeps nothing; for a task that is not running it is kept, and code
  tells the user; one the gate cannot take (empty, too long) gets code's reason
  in Chinese; a reply about a gate that no longer waits reaches the Manager
  with a note saying so. In a Lark group only a message that @-mentions the bot
  comes in at all. The Manager takes one message at a time, so a reply to a
  gate waits behind a Manager turn under way before it is routed.
- **The Manager.** Its `Open gates` section lists the waiting gates of the
  chat it answers (all of them in the dashboard chat);
  `looper_task_gate_reply` adds the user's own message to one (`via`
  `manager:<channel>`, the same check as the task page: 409 for a gate that
  does not wait or a completed task, kept for a stopped one); `looper_tasks`
  `get` shows a task's gates and `looper_overview` the number waiting.
- **The dashboard.** `GET /api/tasks/:id` lists the gates (`gates`, each with
  `channel_posts`: the chat and how many of its messages went there or
  failed); the Overview shows a waiting gate's panel at the top (its agenda,
  the discussion, option buttons, a message box, "End discussion", and where
  its messages go), a conclusion card once it passed; the Pipeline draws a gate
  as a node of its own; the Dashboard and the Tasks page have the Needs you
  group; a Planner's thread on the Chat page says when its task waits at a
  gate.

### A task's agent room

In a project with `agent_room` on (the default), a task's Planner and the
agents of its steps message each other in the task's room ([Agent room
RFC](rfcs/agent-room-v0.zh-CN.md), R1a; `looper/agents/room.py`, the tools in
`looper/agents/ops/room.py`, the rows in `room_messages`, `room_reads` and
`room_watches`). With it off, every prompt, tool list and command line is as
before.

- **Seats.** A member is the Planner (`planner`) or a seat, `<role>@<step
  key>`: the task role a step runs as (`backend_rd@s2`), else `worker@s2`;
  its Reviewer `reviewer@s2`, its review role `security@s2`. A retry is a
  new run in the same seat. Presence is derived, never stored: a seat whose
  run the WorkerPool has is `online` (or `waiting` for room in its task),
  else `offline` once a run was in it, else `planned`, or `dropped` with its
  step, or `consulting` while a consult of its last session runs. Gates have no
  seat. Looper writes as `looper`.
- **Tokens and tools.** Each Worker and Reviewer run in such a project gets
  a token of its own (`run.` and 43 random characters, a hash in memory, never
  logged) naming its task and seat, and Looper's MCP entry through the agent
  library on all three runtimes (`library_with_server`); both go when the run
  ends (`RoomOps.revoke_run`, which deletes the MCP files that carry the token).
  `/api/agent/v1/` tells a run's token by its prefix. A work seat has
  `looper_room_roster`, `looper_room_post`, `looper_room_inbox`,
  `looper_room_read` and `looper_room_watch`; a review seat all but the watch;
  the Planner the review seat's four on its own token where its project has the
  room on (its writes, as its other tools', only in a turn Looper sent it).
- **Scope** (`room.check_scope`). The sender is the token's seat, never an
  argument. A work seat posts to work seats, roles, the Planner and everyone,
  and to a review seat only an answer to that seat's own question to it
  (Workers do not lobby Reviewers). A review seat only asks (`question`) work
  seats or the Planner, never everyone, and answers the Planner; its messages
  and those to it are marked `cc_planner`. The Planner posts anything to
  anyone. A seat sees what is for everyone, what it sent and what names it or
  its role; the Planner sees everything. Kinds: `info`, `question`, `answer`
  (with `reply_to` a question), `interface`, `announce`; Looper's `notice` and
  `escalation`. Messages are only added; they go with their task and with a
  start-over (`reset_task_for_restart`). Refused posts are a task activity line.
- **Delivery is pulled.** A run's first user turn says its seat, who is
  online, the messages for it in full and the interface posts it has not read
  (within 2,000 characters), after the Findings; the system prompt has the
  room's section after the task (and before a role's boundaries;
  `prompt_layers.room`). `looper_room_inbox` gives the unread messages oldest
  first (`for_you` on those that name the seat; at most 20 and 8,000
  characters) and marks them read per seat (a retry reads on); `peek` reads
  without marking; `wait_seconds` waits, up to the project's
  `room_wait_max_seconds`, for a message for the seat or an interface post.
  Every tool's answer carries the seat's unread counts.
- **Reminders.** A run on a runtime that reads user turns while it runs
  (`RuntimeAdapter.run_input`: Claude Code; not a stored capability) that a
  message names, or that an interface post or a watch reaches, gets one line,
  never the message (`WorkerPool.remind`), at most every 60 s. It is written
  only while one of its tool calls runs (the stream's `tool_use` and
  `tool_result`): Claude Code 2.1.285 folds a user turn written then into the
  turn it is in at the next tool result, but runs one written while it writes
  its final reply as a turn of its own after the report. Reminders carry a
  uuid (`looper-reminder-…`), and a result whose `user_message_uuids` are all
  the run's reminders never replaces the run's outcome; the cost is still the
  last result's (the session's totals).
- **The Planner.** A message for `planner`, and every escalation, is stored
  `planner_pending` and wakes the Planner (`ROOM_MESSAGE`) after a 30 s batch
  (`ROOM_BATCH_SECONDS`); after the event it is in, the Planner gives all that
  wait in one `[ROOM]` turn, answered in the `[CHAT]` shape (gate changes,
  actions and `final` as there). A non-empty `reply` is posted for those who
  wrote, as the answer to their questions (`via` `planner_reply`); they are
  delivered once the reply is taken. A failed turn gives them again with a
  newer message, or 10 minutes later at the next event. A `[DECISION]` on a
  step, and each running step in a `[SUPERVISE]`, carry what the step's seats
  did in the room (sent, received, interface posts, unanswered questions).
  The reply contract and the tools' guide name `[ROOM]` and the room only
  where it is on (`reply_contract(..., room=True)`).
- **Consults** (`looper/agents/consult.py`, `room_consults`). A `question`
  for one seat whose run ended (a seat, or a role with one such seat) is
  answered by its last finished run's session, pinned by `source_record`
  when admitted; a still-running record, or a newer record before the consult
  starts, sends the questions to the Planner (`no_session`). Looper resumes
  a fork of it (Claude
  Code `--resume <id> --fork-session`; Codex and OMP a copy of the session
  the run kept; Pi a copy of that session file, resumed with `--session`;
  Grok a home of its own holding a copy of that one session
  from the run's home, resumed with `--fork-session`, and removed afterwards),
  read only (`Launch.consult`: Claude Code has only `Read`, `Glob` and `Grep`,
  allowed without prompts; Bash and writing tools are refused, and MCP tools
  have no allow rule in manual mode with no prompts; not verified on a real
  run yet. Codex has its read-only sandbox; OMP `--tools read,glob,grep`;
  Grok `--tools read_file,grep,list_dir`; Pi
  `--tools read,grep,find,ls,mcp__looper__*`), with the run's system prompt and
  `CONSULT_CONTRACT` in place of its contract and a `[CONSULT]` turn listing
  the questions (their bodies share 20,000 characters) and the seat's earlier
  consult answers. Claude Code, Codex and Grok run where the step ran. OMP's
  reading tools are not verified on a real run; it runs in a detached checkout
  of the task directory's branch, removed afterwards. Pi uses that checkout too.
  A live-model Pi consult was not run. The checkout keeps writes
  out of the task directory and `wrote_files` catches changes inside it, dropping
  the answers. It is not a sandbox: OMP and Pi run as the operator's user, so a write
  outside it (another path or the home directory) is neither prevented nor
  detected. The checkout and fork are removed even when preparing or running
  the consult fails. Claude Code ignores `fork_dir`: it keeps the fork in its
  own session store, like every Claude Code session, and Looper leaves it
  there, since Looper never changes anything in Claude Code's own directory.
  Its answers
  are posted as the seat (`via` `consult`, `cc_planner`); its final state,
  answers and unanswered questions' escalations and notices are stored in
  one transaction, with notifications after commit. A refused state change
  stores nothing, so finishing again cannot post the answers twice. If the
  task's room was turned off before it ended, the consult fails with
  `room_off`, without messages or notifications. A task's
  own consult lock guards both admission under its count limit and the
  seat's queue; it is separate from the room's message lock. Questions that
  come while one is queued join it. After taking capacity, starting under
  the same lock rechecks that the task is active and no consult of the seat
  runs. A consult is a
  run of its own (`consult-<12 hex>`, in the task's cost) that takes a place
  under its task's limit of the seat's kind, and has at most
  `room_consult_budget_usd`, `room_consult_timeout_seconds` and 20 turns
  (Claude Code and Grok). Every runtime's recorded cost is checked after the
  run, falling back to the result's own `total_cost_usd` when it is unknown;
  over-cap or still-unknown costs drop the answers and send the questions to
  the Planner as `budget`. A provider-priced model without a full price
  cannot start a consult (`runtime_cannot`). One deadline covers writing stdin,
  reading stdout and waiting for exit. Stderr is drained, with its last 2,000
  characters kept for errors, and
  the process tree is ended on success too. A
  task has at most `room_consults_per_task`. The seat's Agent Profile must
  still run on the session's runtime and provider. Stopping the service
  starts nothing and keeps queued and running states for recovery; killing
  a consult, including one waiting for capacity, fails it with `stopped`
  and sends its questions to the Planner. Before agents, tasks or HTTP start,
  recovery removes leftover checkouts in `.local/consults` and every runtime's
  `consult-sessions` contents and requeues interrupted runs; until then,
  new consults stay queued. It starts only the
  oldest consult of each seat, and runs the others in order when it ends.
- **Escalations** (`reason`): a question no seat can take (`consult_fallback`,
  with the typed reason: `not_started`, `no_session`, `session_gone`,
  `runtime_cannot`, `no_snapshot`, `limit`, `many_seats`, `budget`, `timeout`,
  `contract`, `wrote_files`, `stopped` or `unsure`), with a notice to the asker; two seats trading
  `room_ping_pong_rounds` messages to each other only, with no answer between
  (`ping_pong`, once at that count); an interface post that replies to an
  earlier one that a seat whose step ended had read (`interface_changed`); the
  room at `room_messages_per_task` (`room_full`, once).
- **Limits.** A run posts at most `room_post_per_minute` messages a minute and
  `room_broadcasts_per_run` to everyone; a message has at most
  `room_message_max_chars`; a task holds `room_messages_per_task`. Past one,
  `rate_limited` (a new error code) or `invalid`. A run keeps at most 5 watches;
  they end with it. The Planner is held to the length and the task's total
  only.
- **Kept sessions.** A Codex, OMP or Pi run in a room keeps its session, for the
  consults: Codex in a home of its own
  (`.local/codex/run-sessions/<task>/<run>`, given the run home's
  configuration as a resident session's is, without `--ephemeral`; the
  Codex login check looks at these homes too), OMP with `--session-dir
  .local/omp/run-sessions/<task>/<run>` in place of `--no-session`, Pi with
  `--session-dir .local/pi/run-sessions/<task>/<run>` in place of
  `--no-session`. They go
  when the task is planned anew or deleted, and when the server starts after
  it ended a week ago (`RUN_SESSIONS_KEPT_DAYS`). Claude Code keeps every
  run's session itself.
- **Reading it.** `POST /api/v1/tasks/room_view` (`task_id` required; optional `kind`, `from`, `unanswered`,
  `after`, `limit` in the JSON body) gives the seats with their presence, the messages oldest
  first (with `answered` and `cc_planner`), the counts and each step's (sent,
  received, unanswered, each message once per step); `GET /api/tasks/:id` has
  the counts as `room`. The task page's Room tab shows them; a Pipeline step
  its counts. The Manager reads a room with `looper_tasks` action `room`; it
  never posts in this phase, nor does the user.

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
| `TASK_REQUEST` | TaskRouter → Planner (and, from before the Manager's tools, Manager → TaskRouter) | The started task's request to its Planner; a replayed one from the Manager creates a ready task |
| `TASK_START` | HTTP or Manager → TaskRouter | Start a ready task |
| `TASK_ACCEPTED` | Planner → TaskRouter → Manager | Plan made, with its step count |
| `TASK_CANCEL` | HTTP → TaskRouter → Planner | Terminate task permanently |
| `TASK_STOP` | HTTP → TaskRouter | Pause task (preserves state) |
| `TASK_RESTART` | HTTP or Manager → TaskRouter | Run a stopped, failed or cancelled task again: `mode` `continue` (from its saved plan, the default), `start_over` or `at_cap` |
| `TASK_RESUME` | TaskRouter → a resumed task's new Planner | Carry on with the saved plan after a server restart |
| `TASK_CHANGE` | HTTP → a task's Planner | The task changed (its stored changes are what count; this only wakes the Planner) |
| `GOAL_STATUS` | Planner → TaskRouter → Manager | Report progress/completion; the end of a Telegram or Lark task carries its `notice` |
| `CHAT_NOTICE` | Manager → Telegram or Lark | A task's end, to who the task recorded, after its progress |
| `WORK_ITEM` | Planner → Worker | Dispatch step |
| `WORK_RESULT` | Worker → Planner | Step result |
| `VERIFY_REQUEST` | Planner → Reviewer | Review a step's result |
| `VERIFY_RESULT` | Reviewer → Planner | Review verdict (`passed`, findings) |
| `WORKER_INPUT` | Planner → running Worker | Forwarded user text as the Worker's next user turn |
| `WORKER_INPUT_RESULT` | WorkerPool → Planner | Whether that text was delivered |

## How a task runs

1. The Manager sends `TASK_REQUEST` to the TaskRouter, which records the task
   ready (the dashboard creates one the same way). When a user starts it
   (`TASK_START`), the TaskRouter creates its Planner at once and forwards
   the request.
2. Plan turn: the Planner creates the task's directory if needed, starts its
   Claude and asks `[PLAN]`. The steps go to `task_steps`, the activity gets
   `Plan created: ...`, and `TASK_ACCEPTED` reports the step count to the
   Manager.
3. Dispatch: every step whose `depends_on` steps are all verified goes as
   `WORK_ITEM` to a new Worker, up to 4 running at once per task. Steps
   without a dependency between them run at the same time in the same
   directory, so they must change different files, unless the project has
   [step workspaces](#step-workspaces): then each runs in a worktree of its
   own and is merged when it is accepted. The eval's
   `parallel_parts` and `interface_first` tasks check that the Planner plans
   independent and coordinated parts that way; with haiku the second still
   fails (the Planner chains the service, the client and the check).
4. Decision turn: every `WORK_RESULT` and `VERIFY_RESULT` gets a `[DECISION]`
   turn whose message lists the decisions allowed now:
   - `accept` only when the result can be accepted: the Worker completed and
     the step needs no review, or the Reviewer passed. The step is verified
     (in a worktree of its own, once it is merged), and its `summary` becomes
     the task's result unless a later accept replaces it.
   - `review` only after a successful Worker result. A new Reviewer gets
     `VERIFY_REQUEST` with the step's instruction, the Worker's result, the
     acceptance criteria and the retry notes, if any. A step with `review` set in the plan, or sent to
     review once, needs a passing review before it can be accepted, also after
     a retry.
   - `retry` sends the same step to a new Worker with the feedback appended to
     its instruction; at most the project's `max_step_retries` (2 unless set)
     retries per step.
   - `replan` replaces every step that has not started; started steps cannot
     change, and the new steps may depend on them. The current step counts as
     verified if its result can be accepted, and is dropped (`skipped`)
     otherwise. The recommended role prompt has every replan build on what is
     done: new steps depend on the accepted steps whose results they need
     instead of doing their work again.
   - `fail` fails the task with the given reason.
5. Changes: a message to the Planner, or an edit of the requirement, gets a
   `[CHAT]` turn ([Changes to a task](#changes-to-a-task)). Claude replies,
   and may `forward` text to a running step's Worker (`WORKER_INPUT`,
   confirmed by `WORKER_INPUT_RESULT` within 5 s), `add_steps`, which are
   dispatched once ready, or `replan`. Text that arrives after its Worker
   finished is kept and shown to Claude in that step's decision turn.
6. Completion: when every step is verified or dropped, the Planner sends
   `GOAL_STATUS` `completed` with the final summary of the decision that
   completed it (else, when a step's own verdict settled it because the
   Planner could not decide, the last accepted step's summary or a generated
   per-step summary). The TaskRouter writes it to `tasks.result_summary` and
   forwards it to the Manager, which passes it to the web chat it came from
   (a Telegram or Lark chat gets the TaskRouter's notice instead, which the
   router adds to that status, so the end arrives once and last); it then
   stops the Planner, which ends its Claude.
7. Failure: a `fail` decision, a step that fails by its own verdict after a
   decision turn failed, or planning that cannot finish (see the Planner's
   failures above) sends `GOAL_STATUS` `failed` with a closing note written
   by code (`planner.closing_note`: why it ended, what the steps delivered,
   what is not done), which the TaskRouter writes to `tasks.error`; the web
   chat gets it from the Manager and a Telegram or Lark chat the notice, and
   the Planner is stopped the same way.
   A task the user stops gets the same kind of note in `tasks.result_summary`,
   from its saved plan (it is not sent to a channel). Starting a stopped,
   failed or cancelled task again clears both and goes on from its plan in a
   `[RESTART]` turn; starting it over plans it anew in a new Planner session.

## Task Lifecycle

```
init (ready) → (start) → pending → assigned → running → completed
                                                 ↓           ↑
                                              stopped ───────┘  (start)
                                                 ↑
                                              failed  ←── running
                                                 ↓
                                              (start) → pending → ...

cancelled ← running  (cancel; (start) runs it again too)
```

A start of a stopped, failed or cancelled task goes on from its saved plan:
its accepted steps stay done, the others are back to not started, and its
Planner decides in a `[RESTART]` turn how the task goes on. Starting it over
(`start_over`) plans it anew instead, with its steps, plan and Planner session
deleted.

**Status values**, by group (`looper/db/tasks.py` `STATUS_GROUPS`, the one place that groups them): needs you `waiting_user` (the Planner reports it while a user gate of the plan waits for the user, whatever else runs; [User gates](#user-gates)); in progress `pending` (started, its Planner starting), `assigned`, `running`, and the Planner's own `planning`, `executing`, `verifying` and `stopping`, which the TaskRouter stores; ready `init` (created from the dashboard or by the Manager, never started; the dashboard labels it "Ready — not started"); needs attention `failed` and `stopped`; `completed`; `cancelled`. A status no group knows counts as `other`, on the Dashboard and in `GET /api/tasks?group=other`, never dropped.

**Phase values** (finer-grained, set by planner): `pending`, `planning`, `executing`, `verifying`, `completed`, `failed`

## Data Persistence

PostgreSQL is a hard startup dependency. All data paths:

| Table | Purpose |
|---|---|
| `projects` | Projects: name (unique ignoring case), description, default `work_dir` (`''` = none), `background` for the task agents' system prompt (`''` = none), and the execution settings (`project_execution`) |
| `tasks` | Task metadata, status, phase, timestamps, `work_dir`, `project_id` (required), `schedule_id` (the schedule that fired it), `budget_usd` (its own spend cap, over its project's), `chat_id`, `chat_kind` and `chat_mention` (who is told when it ends: a Telegram chat id, a Lark sender's open_id, or a Lark group's id and the requester it @-mentions; `chat_kind` is `user` or `chat`; NULL for a task asked for elsewhere), `original_user_text` (the user's message the Manager's task JSON answered, often 「确认」; kept as history), `title` (a few words naming it, from the task JSON; `task_name` falls back to the goal id, then the requirement), and `workspace_dir`, `workspace_mode` and `workspace_base` ([Task workspaces](#task-workspaces)); `manager_id` (the Manager that created it, which hears of its progress and end), `template` (JSON snapshot of the task template it came from, NULL without one) and `origin_thread` (the web chat thread it was asked for in) |
| `task_worktrees` | A worktree task's git worktree ([Task workspaces](#task-workspaces)), one per task (going with it): `repo_dir` and `subdir` (the workspace in its repository), `path`, `branch`, `base_commit`, `links`, `state` (`active`, `removed`), `result_commit` (the branch's tip when it was removed), `created_at`, `removed_at` |
| `task_steps` | A task's steps, each with everything its Planner decides with: `step_key` (label), `kind` (`work` or `gate`), `depends_on` (step ids), `review`, `acceptance_criteria`, `validation_commands`, `status`, `result`, `worker_id`, `reviewer_id`, `retries`, `feedback`, `missed_messages`, `started_at` (when its run was first dispatched, kept through retries, reviews and server restarts; cleared when a restart of the task sends it back to not started; null for steps from before the column) |
| `task_user_gates` | A gate step's gate ([User gates](#user-gates)): `topic`, `agenda`, `findings`, `state`, `summary` (only once passed), `resolution`, `ended_by_user`, `plan_change`, `delivered_through`, `turns`, `reminders` and its times; the Planner writes all but `first_reply_at` and the reminders' columns, and a closed gate stays as it closed |
| `task_user_gate_messages` | A gate's discussion, oldest first: the user's messages (`message`, `end`), the Planner's replies and code's notes (`opened`, `updated`, `reminder`, `cancelled`, `closed`, `resumed`, `error`), with `via`, the option a button chose, and in a group chat the author |
| `task_plans` | A task's plan, one row per task: `plan_id`, `status` (`planning`, `executing`, `verifying`, `stopping`, `completed`, `failed`), `correlation_id`, `origin_channel_id`, `final_summary`, `fail_reason`, and with step workspaces `start_branch` and `start_commit` (where the branch was when the task dispatched its first step; `''` otherwise); replaced when the task is planned again |
| `step_workspaces` | A step's git worktree ([Step workspaces](#step-workspaces)): `task_id`, `step_id` (both going with the task or step), `repo_dir` and `subdir` (the task directory in it), `target_branch`, `path`, `branch`, `base_commit`, `checkpoint`, `result_commit`, `merge_commit`, `state` (`active`, `merging`, `merged`, `unmerged`, `discarded`), `merge_error` (required when `unmerged`) with `merge_files`, `detail`, `links` (the paths linked back to the task directory's), `removed_at` (its directory and branch are gone); one in use (`active` or `merging`) per step |
| `agent_sessions` | The session each resident agent resumes: `manager` or `planner-<task_id>`, its `session_id`, `session_totals`, the `runtime_type` and `provider_id` it belongs to and the `profile_id` that ran it last (`NULL` runtime: before Agent Profiles, Claude Code; `NULL` provider: before providers were recorded, Claude Code's own login), and `clean_start` (the user dropped the session with `/new`: the next one starts without a resume brief) |
| `task_journal` | A task's journal, written by code as things happen: `step_accepted`, `step_failed`, `step_retry`, `decision`, `supplement`, `session_switch`, `handoff`, `progress`, `profile_choice`, `concurrency_change`, `role_defined`, `gate_opened`, `gate_updated`, `gate_cancelled` and `gate_closed` entries, each with its `step_id` (`''` for the whole task), `summary` and `learnings`, and for a supplement its `via` and `delivered_at`; `GET /api/tasks/<id>` returns it as `journal` (and the progress notes, parsed and newest first, as `progress`); the task page lists the entries as Findings, each with its kind, and the progress notes in its Progress section |
| `task_requirement_versions` | Every version of an edited task requirement: `task_id`, `version` (1 is the text the task was created with, written at its first edit), `text`, `via` (`created`, `task_page`, or the Manager's), `created_at` and `delivered_at` (when the Planner had it in a turn; `tasks.requirement` is the newest) |
| `managers` | The default Manager (`manager`) and the operator's scenario Managers (`manager-<key>`): `name`, `description`, `system_prompt` (null for the default), `profile_id`, `tools`, `task_scope`, `task_policy` (`free` / `templates_only`), `enabled`, `revision` (bumped by every change to what it may do), `deleted_at` (a soft delete: the row and its id stay) |
| `manager_task_templates` | A Manager's task templates ([Task templates](#task-templates)), keyed by `manager_id` and `key`: `name`, `description`, `project_id`, `work_dir`, `workspace_mode`, `requirement`, `planner_instructions`, the three role Profile ids, `role_templates` and `inputs` (JSON) |
| `chat_threads` | The permanent Manager owner of a web or native channel thread (`session_id`); rebinding creates/reuses the destination's thread without changing prior owners; a legacy thread without a row is the default Manager's |
| `manager_actions` | The Manager's tool calls (`looper/agents/ops`), one row each: `manager_id`, `channel`, `chat` and `message_id` of the turn (empty for a turn Claude ran on its own), `tool`, `arguments` (secret values masked), `risk` (`read`, `write`, `high`), `status` (`ok`, `needs_confirmation`, `error` with `error_code`), `result` (of a change; a read's is not kept), `confirmation_id` (the call that asked, on a confirmed one) and `task_id` (the task it acted on, no foreign key) |
| `activity` | Execution trace events (SSE streaming to frontend) |
| `chat_messages` | Chat conversation history of every channel: `session_id` (the web session or stable bot/chat/Manager thread described above; not Telegram groups), `message_id`, `role`, `content`, `channel` (`web`, `telegram`, `lark` or `lark_bot`; rows from before it read `web`), `delivery_failure` and `delivery_error` (a Lark or Telegram answer its channel did not deliver: `no_chat` or `send_failed`, and the channel's error; NULL otherwise); a new Manager session's resume brief reads the recent chats that are not a task's Planner thread (`planner-<task_id>`) |
| `llm_records` | One row per agent run: input and output text, status (`running`, `completed`, `failed`, `interrupted`), the run's own `cost_usd` and its `cost_source` (`runtime` or `provider_price`), `input_tokens`, `output_tokens`, `cache_read_tokens` and `cache_creation_tokens`, its final `context_tokens` and its `compactions`; `metadata` holds the exact argv (`command`), `cwd`, the supplied `system_prompt`, its `system_prompt_mode` (`append` for old records without it), its `system_prompt_sha256`, its `prompt_layers`, the `profile` (`agent` on runs recorded before the rename), the `model_choice`, the Settings `env` names, the `library` items loaded, the `project_instructions` the runtime was given and a task agent's `project_skills`, and for a Worker's or Reviewer's run its `outcome` (`completed`, `reported_failed`, `files_mismatch`, `passed`, `not_passed`, `contract`, `run_failed`; `worker.RunOutcome`) |
| `role_configs` | Each role's own settings, keyed by `role` (`manager`, `planner`, `worker`, `reviewer`): display name, role prompt and its recommended `default_prompt`, turns, timeout, enabled |
| `agent_setting_layers` | Agent settings by layer: `scope` (`system`, `role`, `project`, `role_project`), `role`, `project_id` (goes with its project), and the nullable `harness_prompt`, `profile_id` (the effort is the profile's), `env` (ordered `[{key, value}]`, `null` value = unset), `run_budget_usd`, `max_concurrency` (the `worker` or `reviewer` `role` layer only: the default for every task) and `system_prompt_mode` (`append` or `replace`, the `role` layers only, enforced by a CHECK; null: the role's built-in value); one row per layer and place |
| `agent_runtimes` | Runtimes, built in (one per type found on PATH) or registered by hand before and kept while an Agent Profile uses them (`builtin` false): `name`, `type` (an adapter), `executable` (`''` = the type's default), the probe's `version`, `capabilities`, `probed_at`, `probe_error`, `builtin` |
| `llm_providers` | Account: `name`, `vendor_type`, `auth`, `oauth_runtime_type`, `env`, `max_concurrency`, `builtin`; endpoints and models are separate tables |
| `llm_provider_endpoints` | One endpoint per `(provider_id, protocol)`: `base_url`, legacy `use_runtime_default` |
| `llm_models` | Exact `(provider_id, name, protocol)`, context, four price overrides, efforts, background model, catalog order and builtin flag; profile references restrict deletion |
| `llm_model_migration_map` | Append-only legacy provider/name/protocol → provider/model id mapping; no secrets or foreign keys to deletable current objects |
| `llm_provider_secrets` | A provider's API key, apart from the provider; read only by the launch that will use it |
| `agent_profiles` | `name`, `runtime_id`, `model_id`, `effort`, `agent_teams`, `custom_prompt`, `builtin`; provider comes from the model; prompt mode remains in role/task settings |
| `task_profile_overrides` | A task's Agent Profile per role (`planner`, `worker`, `reviewer`): `profile_id`, going with its task; a profile a task chose is not deleted |
| `task_system_prompt_modes` | A task's own system prompt mode per role (`planner`, `worker`, `reviewer`): `append` or `replace`, going with its task |
| `task_concurrency_limits` | A task's own concurrency limit per role (`worker`, `reviewer`): `max_concurrency` (1–64), going with its task |
| `task_planner_profiles` | A task's Planner's choice of Agent Profile for its `worker` or `reviewer`: for the rest of the task (`step_id` NULL) or one step, going with its task and step (and with the profile); it counts only while the task's project allows the profile |
| `project_allowed_profiles` | The Agent Profiles a project's Planners may choose (project, profile), going with the project; none allows every profile; a profile on a list is not deleted |
| `schedules` | A project's schedules: `name` (unique in the project ignoring case), `cron`, `timezone`, `requirement`, `work_dir` (`''`: the project's default), `overlap` (`skip`, `queue`, `parallel`), `max_parallel_runs` (1–10, default 3: a parallel schedule's runs at once), `catch_up`, `run_timeout_mode` (`inherit`, `seconds` or `none`) and `run_timeout_seconds` (with `seconds`), `run_budget_usd`, `gate_command`, `gate_timeout_seconds`, `daily_budget_usd`, `notify`, `user_gates` (`off` by default, or `on` to opt its tasks into user gates), `notify_channel` and `notify_chat` (both or neither), `enabled` (false when created), `enabled_at` (when it was created or last enabled) and `rule_changed_at` (when its rule or zone last changed; the no-success alert counts from the later of them), `next_fire_at` (null while disabled), `last_fired_at`, `last_success_at`, `last_ok_at`, `consecutive_failures`, `failures_alert_at` and `stale_alert_at` (set while that alert is active), `created_via` (`dashboard`, `manager` or `import`); a project with schedules is not deleted |
| `schedule_profile_overrides` | A schedule's Agent Profile per task role, copied into each task it fires; a profile a schedule chose is not deleted |
| `schedule_task_limits` | A schedule's `task_limits`: per role (`worker`, `reviewer`), `max_concurrency` (1–64), copied into each task it fires (`task_concurrency_limits`); going with its schedule |
| `schedule_runs` | Each firing of a schedule: `scheduled_for`, `fired_at`, `trigger` (`scheduled`, `catch_up`, `manual`), `missed`, `status` (`queued`, `gating`, `running`, then once `completed`, `failed`, `stopped`, `cancelled`, `timed_out`, `gate_failed`, or `skipped_overlap`, `skipped_missed`, `skipped_gate`, `skipped_budget`), `task_id` (null when skipped, or once the task is deleted), `summary`, `cost_usd`, `error`, `gate_output`, `run_timeout_seconds` (the timeout it started with; null: none), `carried_from` (the latest run whose unfinished input it was given), `carried_on` (its own unfinished input was handed on to a later run), `finished_at`, `firings` (how many firings it stands for: a repeated skip is counted on one run, from `scheduled_for` to `finished_at`); going with its schedule |
| `schedule_notifications` | Every message about a schedule: `run_id`, `kind` (`run`, `failures_alert`, `failures_recovered`, `stale_alert`, `stale_recovered`), `channel`, `chat`, `text`, `status` (`sent`, `failed`, `dashboard`), `error`; going with its schedule |
| `system_settings` | Settings shared by the whole system, one value per key (no row = the built-in default), such as the last library import's report and the scheduler's `scheduler.min_interval_seconds` and `scheduler.run_timeout_seconds` |
| `library_items` | The capability catalog: kind, name, `origin` (`machine`, `upload`, `git`, `ui` or `project`), `modified`, `owner_project_id` (a project-authored item; goes with its project), `description`, source label, declared version, `sha256` of the files, size, file count, `imported_at` |
| `library_files` | Each catalog item's files: relative path, UTF-8 content, sha256, executable bit |
| `library_installs` | Where an item loads: `scope` (`global` or `project`), `project_id`, `roles` (a non-empty subset of `manager`, `planner`, `worker`, `reviewer`; never `manager` in a project), `enabled`; one per item and place, going with its item and its project |
| `project_documents` | A project's knowledge: title, one-line summary, `source_kind` (`text`, `upload` or `path`), `filename` and `content` (text and upload) or `path` (relative to the project's default directory), `size_bytes`, `sha256`; a CHECK keeps each kind's columns consistent and the rows go with their project |
| `channel_configs` | Telegram/Lark settings, one row per bot (top-level `bound_chats_only BOOLEAN NOT NULL DEFAULT false`, separate from JSON settings and preserved by ordinary config import; `manager_id`, fixed when the bot is added; the built-in rows belong to the default Manager; a trigger refuses to delete a Manager that has bots; `channel_bot_seq` numbers the added `channel-<type>-<n>` rows; enabled flag, Telegram's token env var name, Lark's lark-cli `profile`, the sender allowlist, and for Lark the app it was made for) |
| `task_user_gate_posts`, `task_user_gate_messages` | Each also has `channel_id`, the bot a gate message was posted through or received from. The bridge upgrade removed `UNIQUE (channel, chat, external_id)` on gate posts. There is no downgrade that puts it back |
| `manager_chat_bindings` | Explicit `(channel_id, chat_id)` → `manager_id` bindings with `created_at`; bot deletion cascades, Manager deletion requires unbinding under the same Manager row lock; off but nondeleted targets are allowed |
| `channel_seen_chats` | Eligible allowlisted native-message metadata observed since upgrade: nullable `name`, `count`, `last_at`, retained until bot deletion; no transcript backfill or extra account/name lookup; paginated catalog unions explicit bindings |
| `channel_unbound_chats` | Historical newest 20 bound-only suppressed chat identities per bot, with nullable name, count and last time; binding does not clear them or the complete seen catalog |
| `channel_rejected_senders` | The latest senders a channel's allowlist refused (the Lark channel records them): `sender_id`, `count`, `last_at` and `chat_id` (the group chat of the latest one; NULL for a direct message), never what they wrote; the 20 newest per channel |

Plans and sessions used to be files (`.local/planner/planner-<task_id>/*.json`,
`.local/manager/session.json`). A completed bridge database has
`file_state_import` in `schema_migrations`; that row is a precondition of
`upgrade_from_bridge`, not an import this process runs. The bus journal
(`.local/agent-bus`) and the read-only copies under `.local` stay files.
`MessageJournal.transition` seals notice identities once per runtime root
before replay. It is not a database migration.

Every task belongs to one project. Assigning tasks that predated projects was
a historical migration (`tasks_project_backfill`); this binary does not run
it. A task's "active" statuses (`ACTIVE_STATUSES`) are those of
the in progress group (see the status values above), the
Planner's own `planning`, `verifying` and `stopping` included: they count in
a project's `active_count`, a task in one cannot be deleted before it is
stopped, and a server start resumes, starts (`pending`) or stops it. A ready task (`init`) is not
active.

The names follow the dashboard's, and "agent" names only a running agent.
`role_configs`, `agent_profiles`, `task_profile_overrides` and `profile_id`
are the current names. The historical rename is already in the baseline;
this binary does not run it.

Connection: `[database] url` in the ops file (`<runtime root>/ops/server.toml`,
or the file `--config` names; `looper/ops_config.py`). There is no default
URL: without one, `looper serve` and `looper doctor` do not start, and say to run `looper config init-ops` or edit the file. A
database that already exists keeps the name it had (often `loopx`, from before
the rename), because it holds the user's data.

### Renamed from LoopX

The product was LoopX up to v2.5.0. The `loopx` command, the automatic move
of `~/.loopx` to `~/.looper`, and replacement of `com.loopx.serve` are gone.
Finish those on v2.13.165 before upgrading. `looper.ops_config` still reads a
deployment variable from the environment (`LOOPER_DATABASE_URL`,
`LOOPER_PUBLIC_URL`, `LOOPER_PG_DUMP`, `LOOPER_BACKUP_CMD`,
`LOOPER_BACKUP_VERIFY_CMD`, `LOOPER_OPERATOR_NOTES_ROOT`, and the `LOOPX_*`
names of the first four) and then from the checkout's `.env`, until the ops
file names that setting. Each use warns by name and never shows the value.
`looper.env` still reads another `LOOPER_*` name's `LOOPX_*` counterpart when
the new name is unset. The supported installed path is the private ops file
plus an explicit `--runtime-root`: `looper service install` refuses while a
setting still comes from an environment variable. `service install` still
copies `LOOPX_TELEGRAM_BOT_TOKEN` for one release; set
`LOOPER_TELEGRAM_BOT_TOKEN` explicitly. The bridge upgrade rewrites a system
harness only when it is exactly the pre-rename built-in sentence. Other user
files and historical rows stay.

### Ops file and child environment

Deployment settings are the ops file, `<runtime root>/ops/server.toml`
(`looper/ops_config.py`). `--config` on `looper serve`, `doctor`, `config init-ops` and `service install` names
another file. The keys are `[database] url`, `[server]` `host`, `port`,
`public_url` and `log_level` (`debug`, `info`, `warning`, `error`), `[backup]`
`pg_dump`, `command` (an argv array; `{dbname}` and `{user}` are filled
in) and `verify_command` (an argv array that lists the dump read on stdin like
`pg_restore --list`), `[operator_notes] root` (an absolute path), and
`[runtimes] pi` and `dsh` (`"runner"` by default, or explicit `"cli"` opt-outs; file only). Nothing is expanded: no
variable, no `~`, no include. An unknown key, a wrong type or a bad value
stops the command; the message names the key and never shows the value. The
file must be a regular file (not a symlink), owned by the user, mode 0600, in
a directory of theirs with mode 0700. Looper writes it atomically. Edit it
and restart the server to apply a change.

Each setting comes from the first of: an explicit flag, the file, then the old
variable from the environment until the file names that setting
(`LOOPER_DATABASE_URL`, `LOOPER_PUBLIC_URL`, `LOOPER_PG_DUMP`,
`LOOPER_BACKUP_CMD`, `LOOPER_BACKUP_VERIFY_CMD`, `LOOPER_OPERATOR_NOTES_ROOT`,
and the `LOOPX_*` names of the first four; the notes root and the verify
command take only the `LOOPER_` prefix), then the same name in the checkout's
`.env`, then the built-in default. Each use logs a deprecation warning that
names the variable and never its value. The supported installed path is the
private ops file plus `--runtime-root`. There is no default database URL:
without one, `looper serve` and `looper doctor` do not start. The runtime root is `--runtime-root`, else `LOOPER_RUNTIME_ROOT`
(deprecated, and warned; `LOOPX_RUNTIME_ROOT` the same way), else `~/.looper`.
The file never names it.

`.env` is not put into Looper's environment. Only those old settings are read
from it; any other key (for example a channel token variable) is ignored and
named in a warning. A channel secret goes in its Settings field (stored in the
database) or in the server's environment (`looper service install --env
NAME`).

`looper config init-ops` writes the file once from the values in effect, mode
0600, including `[runtimes]` with the commented Pi and DSH runner defaults (or explicit
`"cli"` opt-outs), and refuses to overwrite a file that exists. With nothing configured it
writes `postgresql://localhost:5432/loopx` and says so. Remove the old
variables from the shell and `.env`, then run `looper service install` again
if the service is used. Until then `looper serve` keeps working from the
variables, with the warnings.

`looper service install` (`looper/agents/service.py`) writes a unit that runs
`looper serve --config <absolute ops file> --runtime-root <absolute root>`,
plus `--port` when given, otherwise retaining the installed unit's explicit
port before falling back to the ops file. It refuses when there is no ops file, and
while a setting still comes from an environment variable. It no longer copies
every `LOOPER_*`, `LOOPX_*`, `ANTHROPIC_*` and `LARK_*` variable. It copies
`PATH`, `TZ`, locale, proxy and CA-certificate variables, the runtimes'
directories and logins (`child_env.RUNTIMES`, including `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, `GROK_HOME`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_BASE_URL`, the Anthropic model variables, `CODEX_API_KEY` and
`XAI_API_KEY`), `LOOPER_TELEGRAM_BOT_TOKEN` and, for one release,
`LOOPX_TELEGRAM_BOT_TOKEN` (`CAPTURED_NAMES`), and each `--env NAME`. The
report names the variables it no longer copies. `--env` with an ops variable
is refused. Installing again replaces the old unit, so a
database URL that unit carried is gone. The unit stays mode 0600, because it
can hold API keys.

Every process the server starts (an agent's runtime and the MCP servers that
runtime starts, a schedule gate, a probe, and a git, ps or lark-cli helper)
starts from an allowlist of the server's environment (`looper/child_env.py`:
system paths, locale, XDG, the SSH agent, git identity and config, proxies
and certificates, and the runtimes' login variables) plus what Looper sets for
that child (a provider binding, the agent's settings variables, an MCP token,
a gate's run variables). The database URL, under either name, `PGPASSWORD`,
any `LOOPER_*` or `LOOPX_*` variable and every other variable are not passed.
A database backup gets its own environment (`child_env.backup`): that
allowlist, plus libpq's `PG*` variables and Docker's, and `PGPASSWORD` taken
from the URL for `pg_dump`. An agent that needs another variable takes it from
its settings (Settings, or a project layer).

This keeps secrets out of the children's environments. It is not a sandbox.
A child runs as the server's user, so an agent with a shell can still read
every file that user can read: the ops file, the operator token, a `.env`,
the service unit. Isolating that needs a separate OS user or a sandbox, which
is not done.

## Web Dashboard

Single-page React app served from `web/dist/` via the built-in HTTP server
(`looper/agents/http_server.py`). Default port: 8767. An installed wheel has no
`web/`; it serves the copy staged into the package (`looper/web/dashboard`,
built by `scripts/stage-dashboard.sh` at release time), and it carries
`looper/db/schema.sql` the same way.

### Pages

- **Dashboard**: what is happening now, read from `GET /api/overview` every 5 s. A warning names each supervisor component that is not running: "is down" with when it restarts, or "keeps crashing" with its restarts in a row (and, when it is up between crashes, that it counts as stable after 5 minutes); else one muted "All components healthy" line. Task counts by group (In progress, Ready, Needs attention, Completed, and Cancelled or Other when they hold a task), each opening the Tasks page on that group (`#tasks?group=<group>`). "Agents running now": the Manager busy or idle, each running Planner with its project and task, and each Worker and Reviewer with its step (and the id of the role its task's Planner defined that it runs as) and how long it has run, or "waiting" for room under its task's limit, each linking to its agent history. "Running by project": for each project with a task in progress or ready, its running and ready tasks and its running Workers and Reviewers, with the waiting ones (a project has no limits), then how many projects are idle. "Schedules", when there is one: how many are enabled, the next 5 fires in their own zones, the failing schedules (failures in a row or an alert active, with the last run's status and the last success) and today's skipped runs by reason, each linking to its row on the project's Schedules tab. "Today's spend": the runs started since the server's local midnight, provider-priced (money) apart from the subscription equivalent (not billed), and the runs that ended without a cost.
- **Chat history API**: The dashboard reads targets, sessions and messages and saves messages through the operator-only `POST /api/v1/operator_chat/{targets,sessions,messages,save_message}` operations. Database read failures return typed `unavailable` rather than empty history; save failures return `unavailable` rather than claiming persistence succeeded. Client-owned `chat.*` and Telegram/Lark delivery are unchanged.
- **Chat**: Every agent and its messages, finished ones included. The selector lists the global Manager and the 30 most recent tasks; each task shows its one Planner and the Worker (and Reviewer, when reviewed) of each step. You can send to the Manager and to a running task's Planner, which keeps one thread per task; SSE streaming. A selected Planner also shows its Claude runs, one per turn, in a folded list of the latest 10 beside its activity and chat thread, re-read as a reply starts and ends and every 10 s while the task runs. A Worker or Reviewer shows its Claude runs read-only, following a running one live, and points to its task's Planner for sending. The Manager's chat has a "New session" button that sends `/new`. Its message box stays usable while the Manager replies: Enter queues the message in a staging area above the box (up to 20; the Edit button or ↑ pulls them back into the box, ahead of what is typed), and once a reply ends as `answered` the queued messages go as one message joined by a blank line; after a failed, stopped or confirm reply they go back into the box instead, and they survive a reload (kept per chat in `sessionStorage`). ↑ and ↓ in the box walk the thread's earlier messages (the latest 100, adjacent duplicates collapsed). Esc, or the Stop button that replaces Send while the box is empty, calls `POST /api/chat/stop`; typing `/stop` and Enter does the same. When the Manager's runtime cannot interrupt a reply (`can_interrupt` false), the Stop button shows disabled with "This runtime cannot interrupt a reply." and Esc does nothing. After a reload the page asks `GET /api/chat/turn` whether a reply is still running. A planner's chat has the queue and the history but no Stop. Beside the Manager, a list shows its conversations on every channel (`POST /api/v1/operator_chat/sessions`), grouped as Dashboard chat, Lark and Telegram, each the most recently active first, with its last message's start, sender and time and its message count; it is re-read every 10 s and after each answer, and a failed read shows its error. Picking a dashboard chat opens it as the web chat thread; picking a Lark or Telegram conversation shows its newest messages read-only, with their times and no message box, re-read every 10 s, and says when only the newest 200 of more are shown; an answer its channel did not deliver says so under it, with the channel's reason. Below the md breakpoint the list opens from a button in the header.
- **Tasks**: Task list, newest first, 50 at a time with Load more ("Showing N of M"; the newest page is re-read every 3 s and the older pages loaded stay below it), with project and status filters (`Ready` for ready tasks; the status chips filter on the server and count every task under the other filters), time display, each task's cost so far, a "Scheduled" badge linking to the schedule that created the task (with "continues the schedule's Planner session" on the detail page when `continues_schedule_session` is true), Start/Stop/Delete controls; a ready task is labelled "Ready — not started" and its Start (here and on its detail page) calls `POST /api/tasks/:id/start`. On a stopped, failed or cancelled task, Start (here and on its detail page) goes on from its saved plan, and Start over, after an inline confirm, plans it anew (`POST /api/tasks/start` with `start_over`). `#tasks?project=<id>` filters by a project and shows its name and default directory with a link to its project page; `#tasks?group=<group>` lists one task group, shown as a chip that clears it, `#tasks?schedule=<id>` one schedule's tasks, shown as a chip too, and they combine. A new task picks an existing project or creates one in the same form; its work directory may stay empty when the project has a default. A task's detail view can move it to another project in any status.
- **Projects (sidebar)**: Below the navigation, every project with its task count (or a spinner with its active count); clicking one opens its project page, and `+` creates a project (name, default directory, description, optional background).
- **Project** (`#projects/<id>`, `#projects/<id>/knowledge|skills|profiles|schedules|settings`): a header with the name, description, default directory and task count (a link to the filtered Tasks page), then six tabs, arrow keys moving between them. **Overview**: the 5 latest tasks with status and cost and a link to all of them; the background (three lines, then Show more); the knowledge (document count, stored size, the first 5 titles and summaries, how many referenced files are missing); the skills (count, how many are on, the first 5 names and descriptions); each role's Agent Profile, the per-run caps and which roles this project gives a harness (linking to Agent Profiles); and the execution settings that are set, in words (linking to Settings). **Knowledge**: the documents (title, summary, source, file name or path, size, a Missing badge for a path document whose file is gone) with a readout of documents, stored bytes and index characters against their caps. Opening a row shows its body (a path document's file as it is now); Edit changes the title, summary and content (a text or upload document) or path (a path document); a document is added as Text, Upload or Project file, or by dropping a file on the section; delete asks first. **Skills**: the skills (`project:<name>`, the body's size, `Off` for a disabled one, the description) with a count against the cap. Opening a row shows its `SKILL.md` body; Edit changes the name, description and body; a skill is added by hand or from a `SKILL.md` (Upload SKILL.md, or a drop on the section), whose frontmatter fills the name and description and whose body below it fills the instructions, for review before Add; a switch turns one on or off; delete asks first. Knowledge and skill changes apply at once and a refusal shows the server's reason. **Agent Profiles**: a line saying it overrides the shared defaults in Settings → Roles for the Planner, Workers and Reviewers, and that the Manager is set there, then one collapsible row for the project's layer ("All roles in this project") and one for each role in the project (Planner, Worker, Reviewer), all closed at first. A closed row shows the Agent Profile in effect and where it comes from (shared defaults, role default, this project, or this role in this project; the project's row shows what the roles that do not set it again agree on), "overridden here" when the row sets anything and "unsaved" over unsaved edits. An open row has the Agent Profile first, with Remove override, then "More overrides": the harness, spend cap and environment (a project sets no concurrency limit), where every field of a role's row also says what applies now and from which layer, and one Save. Closing a row keeps its unsaved edits; `#projects/<id>/profiles/<role>` or `/project` opens that row and scrolls to it, and an Agent Profile's "used by" links there. **Schedules**: a line saying a schedule runs a full task of the project on a time rule and starts disabled, New schedule, then one collapsible row per schedule. A closed row shows the name, the rule in words, the next run in the schedule's zone (or Disabled), the last run's status and the failures in a row, with the enable switch, Run now and Reset Planner session beside it (outside the row's button); reset asks for confirmation that the next run starts fresh and a run in flight keeps its own session without writing back. Its session status is "Planner session: none yet", or "Planner session: continuing since <time>, last used by run <task name>", with time in the schedule's zone and no session id, also for a parallel schedule. A closed row also shows "Failing" or "No success lately" while an alert is active. An open row edits every field (the gate command and timeout, the daily budget with today's spend, notify and the chat to send to, or Dashboard only, under "Before each run and after"), previews the rule's next 5 fire times as it is typed (an invalid rule says why and disables Save), saves with one Save, deletes after a confirmation, and lists the run history (the same one as a schedule's detail view): 20 runs at a time, newest first, with "Show older runs", the routine skips (nothing new, busy) hidden by a checkbox that is on at first and says how many firings there have been; each run with when it was due (the fire time on hover), how it fired (Scheduled, Catch-up with the missed count, Manual), the status, for a repeated skip ×N and until when (instead of how long it took), how long it took, the cost, the summary or error, a collapsed gate output, a note when the run carries the previous run's input, and its task by title with the task's status, linking to it. `#projects/<id>/schedules/<schedule id>` opens that row, and a task's Scheduled badge links there. **Settings**: the name, default directory, description, background (`n / 10000`) and execution settings, saved together (Save is disabled over a limit or without a change; Discard changes resets them); then Delete project, always offered with task, running-task and schedule counts. Inline confirmation deletes schedules and their run history, stops running tasks, then removes all tasks and the project; result branches stay. While it runs, the button shows “Stopping and deleting…”. A conflict shows the server's reason and leaves Delete available to retry; success refreshes the task lists.
- **Task Detail**: Overview (status, phase, times, steps ("n/m done": the accepted steps of those not skipped, then how many are in review and how many skipped; the step list's badges use the Pipeline's names) and the task's cost with its run count; the requirement, with Edit while the task has not ended and its earlier versions under History, then the supplements, each with how it came and whether the Planner has seen it; then Progress: once the task has ended, its final summary (completed) or closing note (failed, stopped) as Markdown, then the Planner's latest progress note (done, now, next, risks, estimate, and when) and the earlier ones under a disclosure; then "Cost by agent and model": the Planner's, the Workers' and the Reviewers' runs and cost with each one's share, noting runs still running or without a reported cost, and below it (beside it on a very wide screen) the By model table: each model's runs, input, output and cache read and write tokens (compact) and cost ("–" when none of its runs has one), adding up to the task's cost, then the Manager's turns about the task on rows of their own, marked as not counted in the task's cost or its spend cap; a one-line summary of each role's Agent Profile that opens the Agents tab; steps, agent timeline, live activity feed), Pipeline (React Flow: the plan as its dependency graph, each step a node by its label and status, under the steps in its `depends_on` or the Planner, in rows by dependency depth; the steps nothing live depends on lead to the result. Done steps keep their place and edges when the plan changes, so steps a replan or a restart adds hang off them; dropped steps stay as dashed, muted history on the right of their row, with the decision that dropped them in the tooltip, which also has the instruction and the Worker and Reviewer ids), Agents (each role's Agent Profile and the Worker and Reviewer limits, each with where it comes from (a limit: this task, Settings → Roles default or built-in default), changed there until the task completes and read-only after; the roles its Planner defined with their kind and profile, marking one the project no longer allows; and every Worker and Reviewer run of its steps, in plan order, with its role (Worker, Reviewer or the defined role it ran as), the Agent Profile it ran on, where that came from and its status, each linking to its history: `runs.for_task {task_id}`, read again whenever a step changes), Sessions, Conversation
- **Schedules** (`#schedules`, below Runs in the sidebar): every project's schedules from `GET /api/schedules`, re-read every 10 s, grouped by project (each group with how many of the project's schedules are on and a link to the project's Schedules tab to add or edit one). A filter bar above it: Project (All, or any project) and Status (All, Enabled, Disabled), which combine and are kept in the address (`#schedules?project=<id>&enabled=true|false`, so a link and a reload keep them; an unknown value is All), filtered in the page; while one is set it says "Showing K of N" with Clear filters, and when nothing matches the list says so, with Clear filters. Each row shows the name (opening its detail view), the rule in words, the next run in the schedule's zone (or Disabled), the last run's status, time and task, "Failing" or "No success lately" while an alert is on, and today's spend (against the daily budget when set); a disabled schedule is dimmed (a dashed edge, a muted name). Beside it, the enable switch (`POST /api/v1/schedule_admin/update` with `schedule_id` and `fields.enabled`: the switch shows the new value while it saves, then "Turned on" or "Turned off"; a refusal puts it back and says why) Run now (its answer, as on the project tab) and Delete schedule, which asks inline first ("Delete <name>? Its tasks stay.", as on the project tab) and takes the row out of the list. Loading, empty and error states, and a failed refresh keeps the last list with the error. **A schedule** (`#schedules/<id>`): back to all schedules; its name, project (a link), rule in words, the switch, Run now, "Edit on the project" (its row on the project's Schedules tab, where it is edited) and Delete schedule (the same inline confirmation; once it is deleted, back to all schedules); the next run, the last run with the alerts, and today's spend against the daily budget; its settings (rule and cron, time zone, work directory, gate and its timeout, notify and to whom, the budgets per run and per day, run timeout, overlap and catch-up, the Agent Profiles it sets, by name) with the requirement folded; and the run history, the same as on the project tab. Re-read every 10 s; an unknown id says it was not found.
- **Runs** (`#runs/<role>`): each role's sessions and run history, and no configuration. A Runs list shows every recorded Claude run of the role (the Manager, every task's Planner, Workers or Reviewers), a Manager `/new` as a `new session` row, and links each to its agent's history; each role links to its row in Settings → Roles.
- **Agent history** (`#history/<agent-id>?task=<task-id>`): opened from History on an Agent Timeline row, a Runs entry or the Chat page. The Manager's and a Planner's page starts with "Context & cost": the latest context size, the number of compactions, the total cost and the number of turns. Below them is a sparkline of the context at the end of each of the latest 100 turns, scaled between their lowest and highest value, with a dashed marker on each turn that compacted. A list of the compactions follows (turn, time, trigger, context before → after), then a folded table of the turns. The section is re-read every 10 s. For each Claude run it shows status, model, the run's own tokens (uncached input, cache read, cache write, output), its context at the end (`ctx`), a `compacted` badge, its own cost, the launch command (cwd and argv; a long argument such as the system prompt is shown by its length, and Copy copies the exact command), a collapsible System prompt section with what Looper supplied (the runtime's built-in system prompt is not included): its title shows the prompt's size, its short sha256, an `isolated` badge and, for replace runs, `replaced default`, and inside one line per layer (harness source and version, role recommended or edited, project and background version, task, contract), the model with its source and effort, and the Settings environment variable names, above the full text; runs recorded before the layers existed show the text only. A collapsible Plugins & skills section shows how many plugins and skills the run loaded from the library, with its hash, and how many project skills (from `metadata.project_skills`), and lists each with its short version hash. Then the full transcript, following a running run every 2 s. A running run shows its last output time from the record's heartbeat and is flagged after 2 minutes with no output. While Claude thinks it shows `thinking · ~N tokens` from Claude's `thinking_tokens` events. Those arrive about once a second, so the recorder keeps one row per thinking phase and updates its count in place. Hooks, background tasks and rate limits are shown as one line each, as are a compaction (`Context compacted (auto) · 25.1k → 1.36k tokens`) and a failed compaction attempt. The result line labels Claude's figure as the `session total`. Any other event type is an expandable line. A `/new` is one line, `new session`, with its channel, sender, chat and the session it ended. For a Planner each run is one of its turns, listed above your chat with it and its activity.
- **Settings**: a folding group at the bottom of the sidebar, one page each (`#settings/<page>`): **LLM Providers** (`providers`: expandable accounts with endpoint editing and model create/edit/delete, price sources and unknown-cost warnings); **Agent Runtimes** (`runtimes`, read-only, as above); **Agent Profiles** (`profiles`: each profile's runtime, provider, model and effort, "agent teams on" when they are, the roles it can run and where it is used, with a form that picks the runtime, then a compatible saved model grouped by provider and labeled with its exact name and protocol; no free-text model fallback, and, for a Claude Code runtime, an "Agent teams" checkbox, checked by default); **Roles** (`roles`: a line naming the roles the server has, then a collapsible row for the Shared defaults (the system layer, which the dashboard calls the shared defaults wherever it names a value's source) and one per role, all closed at first and any number open. Wherever the dashboard names an Agent Profile (these summaries, its dropdowns), it adds " · <effort>" when the profile has an effort its name does not end with already. A closed row is a one-line summary: for the Shared defaults the number of roles, the Agent Profile, spend cap and how many environment variables are set; for a role its job, the Agent Profile in effect and whether it is shared or its own, the Workers' or Reviewers' default limit per task, and "Disabled" or "unsaved" when so. An open role has "Overrides for this role" (its layer, each field saying what applies now and from which layer, with the Workers' "Workers at once in each task (default)" or the Reviewers' "Reviewers at once in each task (default)") and "Role prompt and limits" (the role prompt as one text, with a line saying it is the role layer that Looper combines at launch with the harness, project, task and reply format into the system prompt a run's history shows, reset to the recommended one, max turns, timeout, enabled, and the note that every agent has every tool with `--dangerously-skip-permissions`); closing a row keeps its unsaved edits, and `#settings/roles/<role>` or `#settings/roles/shared` opens that row and scrolls to it); **Skills & MCP** (`catalog`: how many of the imported items are loaded and their size, "Import from this machine" or "Re-import", the last import with what it changed and what it left out, a name filter, and the plugins and skills with their version, size and installs; a change applies to each agent's next run); **Channels** (`channels`: the Telegram and Lark channels, each with its sender allowlist, an add and remove list of Telegram user IDs or Lark open_ids, checked as you type, with blanks and repeats flagged, and, for an enabled channel that does not start, the reason); **Import / Export** (`transfer`: a `looper-config/v4` preview, then apply; an older format is refused). Wherever an Agent Profile is chosen (Roles, a project's settings, a new task's roles) it is the same dropdown, whose last entry, "+ New Agent Profile", opens the form in place and selects what it saves.

### Real-Time Updates

- **Activity SSE**: `GET /api/tasks/:id/activity/stream` — server-sent events from Worker subprocess
- **Task polling**: 2-second interval with auto-stop on terminal status
- **Chat SSE**: `POST /api/v1/operator_chat/reply_events` (`correlation_id`) and `POST /api/v1/operator_chat/thread_events` (`session_id`, optional `manager`) — real-time replies and autonomous thread messages; the `answer.final` of a Manager reply carries its `outcome` (`answered`, `failed`, `stopped` or `confirm`; absent reads as `answered`), which the page uses to send queued messages only after `answered`

### Hash Routing

SPA uses `#page/sub` format for deep linking that survives browser refresh:
- `#tasks/task-id` → task detail; `#tasks?project=<id>&group=<group>&schedule=<id>` → the list, filtered
- `#projects/project-id/tab` → one project (`overview` when the tab is left out); `#projects/project-id/schedules/schedule-id` opens one schedule
- `#runs/agent-id` → the role's runs (`#agents/...`, the page's name before, redirects here)
- `#settings/page[/sub]` → one Settings page (`#settings` and `#settings/defaults` redirect to `#settings/roles`)
- `#history/agent-id?task=task-id` → one agent's history

## Component upgrade previews

`looper service upgrade --dry-run [--to <git ref | wheel path>]` compares the
running service's startup manifest with a Git tree (default `origin/main` of
the current checkout) or a wheel, without installing files or restarting.
It prints `NOOP`, `WEB_ONLY`, `CONTROL_PLANE` or `FULL`, the changed components
and the reason. Keeper mode is off in S0, so every control-plane change plans
`FULL`. Running without `--dry-run` refuses until drain and deployment are
available.

`looper/components.toml` declares component globs, runner packaging aliases
and explicit ignores. A normal-suite test rejects unclassified tracked files.
Git objects are read with `git cat-file --batch`; wheels are read as ZIP files.
Each fingerprint hashes sorted `path sha256(content)\n` lines. Checkout web
plans use `web/src` and package files; `verification.web` hashes built assets
under canonical `web/dist` paths (also used for the packaged dashboard).
The adoption fingerprint separately covers report contracts, both seat-tool
listings (including optional vault tools), manifest version and keeper protocols.

`POST /api/v1/system/components` requires operator authentication and returns the
standard v1 result envelope containing `version`, `loaded_at`, `components`,
`adoption` and `verification`. The snapshot is captured from the loaded package's
directory once per process at HTTP-server startup and remains fixed if files later change.
The operation is also available through generated `loopercli` commands. Dashboard,
Telegram and Lark need no change for this read-only planning command.

During a planned-upgrade drain, agent schedules keep their due times and queued
runs for catch-up after restart or drain cancellation. Run now on an existing
agent schedule returns 409 with "the service is draining for an upgrade: run it
after the restart"; an unknown schedule still returns 404. Command schedules
continue firing, including queued runs and Run now: their runs survive service
restarts, and the drain does not wait for them.

## HTTP API

The HTTP server has five route classes: the exact paths `/login` and `/logout`
handle sign-in and sign-out; other paths outside `/api/` serve dashboard
pages and public assets; `/api/agent/v1/`, including `OPTIONS`, requires each agent's own token; `/api/v1/` answers the operator or an API client (a token
from Settings → API clients, accepted only from a loopback peer with a loopback `Host` and no forwarding headers),
and a client token is refused on every other `/api/` path; all other
`/api/` paths, including unknown paths and `OPTIONS`, answer only the operator.
Only dashboard `OPTIONS` requests return 204 without authentication.
At startup it creates a missing runtime root with mode 0700 and checks that
the directory is owned by the current user and not writable by group or others
(an existing 0755 directory is allowed; `looper service install` creates the
runtime root 0700). Before connecting to the database,
it reads or creates `<runtime root>/operator-token`, a private file (mode 0600).
It opens the file without following symlinks and without blocking (a FIFO in
its place cannot hang the start), checks with `fstat` that it is a regular
file of the current user with mode 0600, and reads through that same
descriptor. An unsafe
directory or file refuses startup with a repair message; permissions are never
changed automatically. Operator requests send that
token as `Authorization: Bearer <token>` or the cookie `looper_operator_<port>`,
or send a valid password session cookie.
A dashboard page load signs the browser in when `Sec-Fetch-Mode: navigate` and
`Sec-Fetch-Dest: document`, both the TCP peer and `Host` are loopback, and neither
`Forwarded` nor `X-Forwarded-For` is present. This token cookie has `Path=/`,
`HttpOnly` and `SameSite=Strict`, without `Max-Age`, `Expires` or `Secure`;
pages use `Cache-Control: no-store` and `Referrer-Policy: same-origin` (the Sign out form's POST must carry an `Origin`), other
files keep their public cache. Other browsers sign in with the user name and
password set on the server's machine by
`looper auth set-password [--user NAME] [--runtime-root DIR]`. The user defaults
to `admin` and accepts `[A-Za-z0-9._-]{1,64}`; the password needs at least
8 characters and is read twice without echo, never from an argument.
`looper auth clear-password [--runtime-root DIR]` removes it. Both commands
use the same default runtime root as `looper serve`.

`<runtime root>/operator-password` is a mode-0600 JSON file, checked and read
with the same directory and descriptor safety rules as the token. It stores
the user and a scrypt hash (`n=32768`, `r=8`, `p=1`, `maxmem=64 MiB`,
`dklen=32`) with a fresh 16-byte salt per set, both URL-safe base64 encoded.
Writes use a mode-0600 temporary file, `fsync` and atomic replacement. A malformed
or unsafe file cannot authenticate a request. Verification computes scrypt even
for a wrong user name and compares both user and hash with `hmac.compare_digest`.
The server caches the record by the file's inode, size, `mtime_ns`, mode and uid,
re-reading it safely only when those change.

A dashboard page request without a token cookie, valid session or local
auto-login gets a self-contained, script-free login page (200); assets stay
public. `GET /login` shows the same form, or redirects signed-in and locally
auto-signed-in browsers to `/` (303). Without a password it shows no form and
asks the operator to run `looper auth set-password` on the server's machine
or open the dashboard there. `POST /login` accepts at most 4096 bytes of
`application/x-www-form-urlencoded` fields `username` and `password`; larger
bodies get 413. It and `POST /logout` require an `Origin` whose netloc equals
`Host` and whose scheme is `https` when `dashboard_url` starts with `https://`,
`http` otherwise (403 if missing or different; behind an https proxy, set
`dashboard_url` to the https address). A wrong user or password gets the same
401 error; no password also gets 401, and an unusable password file gets 503
with a server-log pointer. Success redirects to `/` (303) with
`looper_session_<port>=<id>; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800`;
`Secure` is added only when `dashboard_url` starts with `https://`.
The random session ID is not the token. Sessions last 7 days in memory, at most
100 (expired first, then oldest removed); setting or clearing the password,
or restarting the server, ends them. `POST /logout` ends the presented session,
clears both cookies with `Max-Age=0`, and redirects to `/login` (303).
Other methods on these paths get 405.

Before any scrypt work, a limiter checks the TCP peer: 5 consecutive failures
lock it for 60 seconds, doubling after further failures up to 900 seconds;
a success resets it. More than 30 failures in a sliding 600-second window lock
every peer until the count falls to 30 or fewer. Locked requests get 429 and
`Retry-After` seconds, without adding another failure. All browsers behind a
proxy share its peer address; `X-Forwarded-For` and `Forwarded` are ignored.
Attempts are handled one at a time (check, scrypt, record), so parallel
requests cannot pass the limiter before the first failure counts. Login logs contain only the peer and `failed`,
`succeeded` or `locked`, never the request body, user name or password.
Every login page and every `/login` or `/logout` response uses
`Cache-Control: no-store`, `Referrer-Policy: same-origin` (under `no-referrer`
a browser sends `Origin: null` with the form's POST, which the same-origin
check refuses) and
`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'`.
There are no sign-in links or one-time codes; `code` and `token` query
parameters are ignored. Without a password only local auto-login is available.

`looper service` reads the file of the unit's `--runtime-root`, and the eval
runner that of the server it starts; both send the Bearer token. A missing or
wrong credential gets 401 with `WWW-Authenticate: Bearer realm="looper"` and a
message saying how to sign in, which does not name the token file. The check
precedes reading the body, including configuration uploads. A shell agent under the same system user can
still deliberately read the file, imitate a browser or access the database;
see [the RFC, section 5](rfcs/operator-api-auth-v0.zh-CN.md#5-agent-还能做到什么).

A request whose `Origin` is not the server's own is refused with 403 and no
response carries CORS headers. While the server listens on a loopback address,
the `Host` must name a loopback address too, which defeats DNS rebinding.
Clients without an `Origin` (curl, the CLI) skip the Origin check except for
`POST /login` and `POST /logout`. The Vite
dev proxy keeps `Host` and `Origin` on its own port and forwards the browser
cookie: open the dashboard on the server's port first, using the same hostname.

The dashboard and client API inventory below is derived from the operation catalog. Braces enumerate
individual paths and operation IDs in the same order; every JSON operation has declared request/result schemas.
The OpenAPI document is authoritative for fields, authentication and errors. Provider identity uses
`vendor_type`/`endpoints`; models are separate records, and profiles select `model_id` and return a structured model.

| Path | Method | operationId |
|---|---|---|
| `/api/v1/ops/{overview,task_create,task_instruct,task_update,concurrency,config_export,costs,task_pipeline}` | POST | `overview, task_create, task_instruct, task_update, concurrency, config_export, costs, task_pipeline` |
| `/api/v1/ops/tasks/{list,get,room}` | POST | `tasks.{list,get,room}` |
| `/api/v1/ops/task_control/{start,stop,restart,start_over,cancel}` | POST | `task_control.{start,stop,restart,start_over,cancel}` |
| `/api/v1/ops/projects/{list,get,create,update,delete}` | POST | `projects.{list,get,create,update,delete}` |
| `/api/v1/ops/agent_profiles/{list,create,update,delete}` | POST | `agent_profiles.{list,create,update,delete}` |
| `/api/v1/ops/providers/{list,register,update}` | POST | `providers.{list,register,update}` |
| `/api/v1/ops/models/{list,create,update,delete}` | POST | `models.{list,create,update,delete}` |
| `/api/v1/ops/catalog/{list,install,update,uninstall}` | POST | `catalog.{list,install,update,uninstall}` |
| `/api/v1/ops/settings/{view,set,allowed_profiles,scheduler,waits,knowledge_vault}` | POST | `settings.{view,set,allowed_profiles,scheduler,waits,knowledge_vault}` |
| `/api/v1/ops/schedules/{list,get,create,update,enable,disable,run,delete,reset_planner_session}` | POST | `schedules.{list,get,create,update,enable,disable,run,delete,reset_planner_session}` |
| `/api/v1/ops/gates/{list,messages,reply,end}` | POST | `gates.{list,messages,reply,end}` |
| `/api/v1/chat/{send,messages,conversations}` | POST | `chat.{send,messages,conversations}` |
| `/api/v1/confirm` | POST | `confirm` |
| `/api/v1/{openapi.json,whoami}` | GET | `openapi, whoami` |
| `/api/v1/auth/{set_password,clear_password}` | POST | `auth.{set_password,clear_password}` |
| `/api/v1/system/{check,status,overview}` | POST | `system.{check,status,overview}` |
| `/api/v1/config/{export,import_preview,import_apply,import_upload,import_cancel}` | POST | `config.{export,import_preview,import_apply,import_upload,import_cancel}` |
| `/api/v1/tasks/{requirement_update,configure,move,agents,continue,role_update,room_view,activity,activity_detail,conversation,activity_stream,create,page,detail,delete}` | POST | `tasks.{requirement_update,configure,move,agents,continue,role_update,room_view,activity,activity_detail,conversation,activity_stream,create,page,detail,delete}` |
| `/api/v1/catalog_items/{list,get,update,delete}` | POST | `catalog_items.{list,get,update,delete}` |
| `/api/v1/installs/{create,update,delete,project}` | POST | `installs.{create,update,delete,project}` |
| `/api/v1/catalog_sources/{import_machine,import_mcp,upload,git_preview,git_import,create_skill,create_mcp}` | POST | `catalog_sources.{import_machine,import_mcp,upload,git_preview,git_import,create_skill,create_mcp}` |
| `/api/v1/project_skills/{add,get,update,delete}` | POST | `project_skills.{add,get,update,delete}` |
| `/api/v1/waits/{resume,cancel}` | POST | `waits.{resume,cancel}` |
| `/api/v1/channels/{status,people}` | POST | `channels.{status,people}` |
| `/api/v1/gate_settings/{get,update}` | POST | `gate_settings.{get,update}` |
| `/api/v1/managers/{list,create,update,delete}` | POST | `managers.{list,create,update,delete}` |
| `/api/v1/manager_templates/{put,delete}` | POST | `manager_templates.{put,delete}` |
| `/api/v1/schedule_admin/{list,project_list,get,preview,runs,notifications,create,update,delete,reset_planner_session,run}` | POST | `schedule_admin.{list,project_list,get,preview,runs,notifications,create,update,delete,reset_planner_session,run}` |
| `/api/v1/channel_configs/{list,get,create,update,delete}` | POST | `channel_configs.{list,get,create,update,delete}` |
| `/api/v1/manager_chat_bindings/{list,put,delete}` | POST | `manager_chat_bindings.{list,put,delete}` (operator-only) |
| `/api/v1/channel_chats/{list,set_policy}` | POST | `channel_chats.{list,set_policy}` (operator-only) |
| `/api/v1/manager_session/{get,select_profile}` | POST | `manager_session.{get,select_profile}` |
| `/api/v1/operator_chat/{turn,stop,targets,sessions,messages,save_message,send,reply_events,thread_events}` | POST | `operator_chat.{turn,stop,targets,sessions,messages,save_message,send,reply_events,thread_events}` |
| `/api/v1/chat_settings/{get,update}` | POST | `chat_settings.{get,update}` |
| `/api/v1/clients/{list,create,revoke}` | POST | `clients.{list,create,revoke}` |
| `/api/v1/roles/{list,get,update,reset_prompt}` | POST | `roles.{list,get,update,reset_prompt}` |
| `/api/v1/agent_settings/{get,update_system,update_role}` | POST | `agent_settings.{get,update_system,update_role}` |
| `/api/v1/models/get` | POST | `models.get` |
| `/api/v1/profiles/{create,update,delete,prompt_preview}` | POST | `profiles.{create,update,delete,prompt_preview}` |
| `/api/v1/project_knowledge/{list,add,get,update,delete,drafts,confirm_draft,discard_draft}` | POST | `project_knowledge.{list,add,get,update,delete,drafts,confirm_draft,discard_draft}` |
| `/api/v1/project_roles/stats` | POST | `project_roles.stats` |
| `/api/v1/role_templates/{list,put,delete}` | POST | `role_templates.{list,put,delete}` |
| `/api/v1/project_agent_settings/{get,update,update_role}` | POST | `project_agent_settings.{get,update,update_role}` |
| `/api/v1/allowed_profiles/{get,replace}` | POST | `allowed_profiles.{get,replace}` |
| `/api/v1/project_admin/{list,create,update,delete}` | POST | `project_admin.{list,create,update,delete}`; delete removes schedules and their run history first, stops running tasks (waits up to 60 s), then deletes all tasks and the project with single-task workspace/session cleanup; result branches stay. The user confirms first; conflicts allow retry after partial cleanup. |
| `/api/v1/provider_accounts/{list,create,update,delete,clear_login_problem}` | POST | `provider_accounts.{list,create,update,delete,clear_login_problem}` |
| `/api/v1/registry/view` | POST | `registry.view` |
| `/api/v1/runtimes/probe` | POST | `runtimes.probe` |
| `/api/v1/runs/{for_task,list,get,events,usage}` | POST | `runs.{for_task,list,get,events,usage}` |
| `/api/v1/scheduler_settings/{get,update}` | POST | `scheduler_settings.{get,update}` |
| `/api/v1/wait_settings/{get,update}` | POST | `wait_settings.{get,update}` |
| `/api/v1/knowledge_vault/{get,update}` | POST | `knowledge_vault.{get,update}` |
| `/api/v1/workspaces/{check,discard}` | POST | `workspaces.{check,discard}` |

JSON requests and responses use `application/json`. Configuration `config.import_upload` streams a raw
portable JSON document into staging (not a multipart upload or a `{document: ...}` wrapper);
`config.import_preview` accepts the wrapped document. Neither writes configuration. Export returns
`OpOk.result.document`; the dashboard downloads that document locally. Apply and cancel are bound to the caller.

The four SSE operations return `text/event-stream`, with operation-specific event schemas in OpenAPI:
`chat.send` emits accepted, delta, status, final, heartbeat, timeout and system.event; `tasks.activity_stream` emits activity
(with id equal to seq), done and heartbeat comments; `operator_chat.reply_events` emits answer.delta,
answer.final, agent.phase, heartbeat and system.event; `operator_chat.thread_events` emits chat.message and
heartbeat. Errors before stream headers are JSON `OpError`; disconnect closes the subscription. Generated CLI
stream commands consume SSE through discovery; the hand-written client chat command keeps its own presentation.

The retained private capability protocol is separate from this public inventory:

| Method | Private path | Authentication and scope |
|---|---|---|
| GET | `/api/agent/v1/tools` | Manager, task Planner or run token; only that principal's tools |
| POST | `/api/agent/v1/ops/:tool` | The same capability token; task/run/turn scope checked before dispatch |

Clients cannot authenticate on the private protocol, obtain its tokens or invoke its private tools through
`CLIENT_TOOLS`. `/login` (GET/POST), `/logout` (POST), pages and static assets deliver browser authentication
and documents; they are not operator API operations. Removed unversioned operator methods return 404 with
valid operator authentication. Dashboard calls and returned API URLs use `/api/v1/`.

Run/activity and chat-history read failures return typed `503 unavailable`, rather than empty success.
Task lifecycle operations reject invalid states with `409 conflict`. Telegram/Lark use the unchanged Manager
ops/services and private capability boundary; their transport does not migrate with dashboard projections.

### Migrated settings and interaction requests

The v1 rows above take JSON bodies, including reads (`{}` when no fields are needed).
Their successful projections are inside 200 `OpOk.result`, never bare objects or 201 creates.
Failures use `OpError`: 400 `invalid`, 403 `forbidden`, 404 `not_found`, 409 `conflict`,
or 503 `unavailable` when execution is unavailable; authentication failures are 401 `unauthorized`.
The generated OpenAPI document is authoritative for required fields and result schemas.
For the five native chat binding/policy operations above, refer to each generated
operation's `requestBody` and 200 response schema, plus `components.schemas.Binding`,
`SeenChat` and `UnboundChat` in `GET /api/v1/openapi.json`. Operator tokens, cookies
or password sessions may call them; client tokens get `403 forbidden` and cannot
confirm their way into operator access. Discovery generates
`loopercli manager-chat-bindings {list,put,delete}` and
`loopercli channel-chats {list,set-policy}`, using `--url URL` and
`--operator-token-file PATH`. These operations add no Manager tools or capability scope.

For global and project setting updates, put layer fields inside `values`; role-config and profile
patches instead use `fields`. Layer patches change only supplied fields. Null or an empty string unsets
a layer field; `env` replaces the list of `{key, value}` entries (a null value unsets a variable outside
the system layer). The built-in harness text unsets the system harness. A profile's model and effort
are selected through `profile_id`, never written directly to a layer. Invalid fields/profiles, a harness
over 20,000 characters, invalid spend caps, credential-like variables, more than 50 variables or
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` return 400 without partial writes. Concurrency is 1–64 and
belongs only to global worker/reviewer role layers, not project layers or manager/planner layers.

Operators execute these writes in one call, using the dashboard's existing dialogs. Clients receive
202 `needs_confirmation` for shared-default updates, concurrency updates, raised/lifted effective spend
caps, profile deletion and role-template deletion; confirm with `POST /api/v1/confirm` in the same
client/session. Gate replies/end and wait controls execute directly. Client management is operator-only,
with `Cache-Control: no-store` on responses; never log its one-time create token.

The following request bodies illustrate the field nesting. Identifiers are placeholders for existing
resources, not credentials; these are schema examples, not records of executed HTTP requests.

`POST /api/v1/roles/update`:

```json
{"role": "worker", "fields": {"timeout_seconds": 600}}
```

`POST /api/v1/agent_settings/update_role`:

```json
{"role": "worker", "values": {"system_prompt_mode": "append"}}
```

`POST /api/v1/project_agent_settings/update_role`:

```json
{"project_id": "project-example", "role": "worker", "values": {"run_budget_usd": 2}}
```

`POST /api/v1/role_templates/put`:

```json
{"project_id": "project-example", "key": "tester", "kind": "work", "description": "", "role_prompt": "Check the requested behavior.", "harness": null, "profile_id": null, "items": []}
```

`POST /api/v1/ops/gates/reply`:

```json
{"task_id": "task-example", "gate": "approval", "text": "Proceed with the proposed change."}
```

`POST /api/v1/profiles/update`:

```json
{"profile_id": "profile-example", "fields": {"custom_prompt": "Explain verification results."}}
```

`POST /api/v1/clients/revoke`:

```json
{"client_id": "client-example"}
```

### The public API (v1)

`GET /api/v1/openapi.json` is the one description of what a program may do with Looper. It is generated from the ops
catalog ([The Manager's platform tools](#the-managers-platform-tools)), so a new ops tool becomes an operation without
other code; a tool with several actions is split into one operation per action, each with its own request and answer
schema. The document covers the complete public dashboard/client API, including typed authentication,
configuration transfer and all dashboard domains. The retained private agent capability protocol above has
separate tokens and is not exported to API clients. No unversioned operator route is an extension point.

- **Answers.** 200 is `{status: "ok", result: ...}` (`OpOk`), with optional `notes`. 202 means the operation needs a confirmation (`status: needs_confirmation` with a
  `confirmation_id`) or was accepted and continues. A failure is a typed error, `{status: "error", error: {code,
  message}}`, with the HTTP status of its code: `invalid` 400, `unauthorized` 401, `forbidden` 403, `not_found` 404,
  `conflict` 409, `expired` 410, `rate_limited` 429, `unavailable` 503.
- **Sessions and confirmation.** A client may send `X-Looper-Session` (1–64 characters of `A-Za-z0-9._:-`; `default`
  without it). For client tokens, a write of risk `high` answers 202 with a `confirmation_id`; `POST /api/v1/confirm` with that id runs it
  only for the same client and the same session, within 10 minutes. Pending confirmations live in memory and are lost
  when the server restarts. Operators execute high operations in one request; the dashboard keeps its existing
  confirmation dialogs and never silently confirms a client confirmation.
- **Credentials are the operator's.** `auth.set_password` and `auth.clear_password` accept only an operator token,
  cookie or password session; a client token gets 403 `forbidden`, without any credential change. API client
  management (`POST /api/v1/clients/{list,create,revoke}`) is also operator-only. A client token never yields an operator session or a cookie.
- **Local operations over v1.** `system.check` returns the self-check results, including pg_dump. Configuration
  export and import share their implementation with `looper_config_export` and the dashboard;
  `config.import_preview` stages a document and returns an import id, and `config.import_apply` requires a
  client confirmation before writing it.
- **Client-only tools.** `looper_task_pipeline` (a task's steps in plan order with kind, status, dependencies and role)
  and `looper_gates` (list a task's gates, read their messages, reply, end a gate) exist for clients. A client does not
  get `looper_task_gate_reply`, because a gate reply there is the user's own words in a chat (a client answers with
  `looper_gates`), nor the Manager's confirmation tool, because a client confirms through `/api/v1/confirm`.
- **Chat.** `chat.send` posts one message (at most 32,000 characters) and answers with server-sent events (a heartbeat
  every 15 s, up to 1,800 s for the Manager's answer). A conversation belongs to the pair `manager_id` (the default
  Manager when omitted) and `client_id`: another client, or the same id on another Manager, finds it unknown. A
  conversation id is `cli-…`, made by the server when `conversation_id` is omitted. `chat.messages` returns oldest first
  in pages of at most 200, with `next_cursor` and `has_more`; an `after` cursor the conversation does not contain is a
  `not_found` error, not an empty page. `chat.conversations` lists the client's conversations, most recently active first.

## loopercli and looper-skill

`loopercli` is the second console script of the `looper` package: a plain OpenAPI client that imports nothing of the
server. Its token and URL are in the client file, `$XDG_CONFIG_HOME/looper/client.json` (`~/.config/looper/client.json`
by default; `--client-file` or `LOOPER_CLIENT_FILE` override it). The file must be a regular file of the current user
with mode 0600, is read on every call, and its URL must be a loopback address. `loopercli login` writes it after
checking the token with Looper; it reads the token from stdin or a prompt, never from an argument. `loopercli` sends
the token only to that URL and never follows a redirect.

- **Commands.** Each operation of `/api/v1/openapi.json` becomes a command at run time, so a new operation needs no
  command-line code; `loopercli --help` lists them. Hand-written are `login`, `confirm`, `chat`, `wait` and `skill`, and
  the aliases `list`, `status`, `pipeline`, `start` and `stop` for the common task operations; stage 2a adds
  `auth set-password` / `clear-password`, `doctor` and `config export` / `import`. Generated client-management
  commands are `loopercli clients list`, `loopercli clients create --name NAME` and
  `loopercli clients revoke --client-id ID`. They require operator authentication via `--url URL` and
  `--operator-token-file PATH` (default stdin), never save that token, and create prints the new client token once.
  Output is a table;
  `--json` prints the answer for scripts. `--session` (or `$LOOPER_SESSION`) sets `X-Looper-Session`.
- **Exit codes.** 0 done, 1 error, 2 usage, 3 needs confirmation (the pending id is printed; run `loopercli confirm ID`),
  4 Looper unreachable or the client file unusable, 6 `wait` timed out.
- **`chat`.** `loopercli chat "text"` (or `--file`) talks to the Manager and prints the answer as it streams; the
  conversation id goes to stderr, and `--conversation ID` continues it. `--list` lists conversations; `--read` prints a
  conversation, `--after MESSAGE_ID` only what follows, and `--follow` keeps printing new messages. Reading pages with
  the `next_cursor` of `chat.messages`.
- **`wait`.** `loopercli wait TASK` blocks until a gate needs the user, the task ends, or `--timeout` passes (exit 6).
  In stage 1 a client conversation receives a task's acceptance and its end, as the Manager's messages; gates and
  progress are followed with `loopercli wait`, `status` and `pipeline`.
- **Operator authentication.** `loopercli auth set-password` / `clear-password` take `--url URL` and
  `--operator-token-file PATH` (default `-`, stdin). The operator file must pass the same owner, regular-file and
  0600 checks as the client file. This token is used only for the request and never saved to `client.json`.
  `set-password` prompts twice without echo; the password travels only in the request body.
- **Configuration.** `loopercli config export FILE` writes the document from v1; `--include-projects` and
  `--include-secrets` are optional. `loopercli config import FILE` previews it and returns an import id.
  `loopercli config import --apply IMPORT_ID` requests application; `loopercli confirm CONFIRMATION_ID`
  completes the client confirmation.
- **Skill.** `looper-skill` teaches a coding agent to use only these commands. `loopercli skill path` and `skill show`
  print the packaged copy; installing it into Claude Code, Codex or OMP is in
  [plugins/looper/README.md](../../plugins/looper/README.md). Looper never writes into those tools' directories.

`looper` keeps `serve`, `service`, `version`, `mcp-server` and `config init-ops`, plus the offline rescue commands
`doctor` and `auth set-password` / `clear-password`, which work when the server cannot start; everyday interaction
goes through the API and `loopercli`. `looper config export` / `import` have been removed; their
`loopercli config export` / `import` replacements require the running service. The dashboard, Telegram and Lark
keep their existing entry points and share the same configuration core.

## Configuration

Agent behavior is configured via the `role_configs` table (Settings → Roles); the harness, Agent Profile
(with its model and effort), environment and per-run cap are agent settings ([Harness, profile and environment](#harness-profile-and-environment)),
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
Settings → Channels edits, and starts each enabled channel. Bot credentials and
runner configuration require a restart; explicit chat bindings and the top-level
`bound_chats_only` policy update the running registry immediately after commit.

**A bot per Manager** (stage 2 of the
[scenario Managers RFC](rfcs/scenario-managers.zh-CN.md)). Each Telegram or
Lark row of `channel_configs` is one bot with one runner and one Manager
(`channel_configs.manager_id`, fixed when the bot is added). The three built-in
rows (`channel-telegram`, `channel-lark`, `channel-lark_bot`) are the default
Manager's. Settings → Channels → Add a bot (`POST /api/v1/channel_configs/create`) creates a
`channel-<type>-<n>` row for any Manager. Its credentials are entered in the dashboard only, kept in
`channel_secrets` and masked in logs; an added bot never reads the environment,
and a lark-cli bot names its profile. The same bot cannot be added twice
(Telegram by the bot id in its token, lark-cli by profile, bot token by app id;
409). Its owner remains fixed; chat bindings can choose another Manager with
that Manager's own stable thread (above). A disabled destination answers that it
is turned off, without fallback. A Manager with bots or explicit bindings cannot
be deleted. Delivery (task end notices, wait and concurrency notices, gate posts) goes by channel id, the
bot the task came through; schedules' notices and the dashboard tasks' gate chat
use the built-in rows only. Bot runner changes take effect after a restart.

**Chat bindings and admission** (stage 3). Settings → Channels lists the complete
paginated seen/bound catalog and historical newest 20 suppressed chats; Settings →
Managers lists explicit bindings for rebind/unbind. Names can be null and fall back
to native ids. Observation starts after upgrade, only for eligible allowlisted
messages; it stores metadata, never message bodies. The exact bot/chat pair is the
routing key. An explicit binding wins; without one the bot owner receives messages,
unless `bound_chats_only` is true (default false). Then new unbound ingress, including
Telegram local commands and native `/stop`, causes no turn, reply, reaction or
status. Explicitly binding to the owner admits it using the unsuffixed identity.
Bindings and policy share a committed registry snapshot; ordinary text never falls
back to another Manager. Already admitted turns retain their destination, thread
and correlation, and existing tasks retain their Manager and stored bot/chat for
acceptance, progress and end delivery, even after unbind or policy changes.

Configuration export/import carries bots and their immutable owner with the
Manager/template graph in v4, using the transfer rules above; imported new or
changed added bots remain disabled pending local review and restart.
Binding/seen/suppression state is not portable; import preserves local bindings
and the top-level `bound_chats_only` policy. Previous binaries ignore these bindings and
policy and may process chats this version silences: before downgrading, the operator
must disable the affected bot runners or accept the previous behavior. There is no
destructive down migration, history rename or deletion. With no bindings and a false
policy, defaults are exactly unchanged for the dashboard, Telegram, both Lark
transports and loopercli. Existing capability boundaries stay intact. Relevant
routing/control diagnostics must identify channel/chat/destination and action/outcome,
never message bodies or credentials; no new metrics system is introduced (RFC §13).
Binding/policy mutations and successful/suppressed native ingress currently lack that
diagnostic coverage; final acceptance remains pending in the RFC until the prerequisite
implementation supplies it and is retested.

- **Sender allowlist** (`looper/agents/channels/allowlist.py`, RFC decision 8):
  the Manager runs any command it is asked to, so Telegram and Lark pass on
  only the messages of the senders their configuration lists. A message from
  anyone else never reaches the Manager: the adapter logs one line with the
  channel and the sender id (never the text) and sends no reply. An enabled
  channel whose list is empty or invalid does not start; the startup log and
  Settings → Channels (`start_problem`) say why. The list holds at most 100
  ids; the API checks each one and stores the list as a JSON array.
- **One status message per message** (`looper/agents/channels/status_line.py`,
  Telegram and Lark): the Manager's phases (`STATUS_UPDATE` with `phase`,
  `activity` `thinking` or `tool` with the `tool`'s name; streamed text is
  never sent) become one status message, sent at the first phase
  (「⏳ 处理中：思考中 · 0s」; a tool shows as, say, 「运行命令 (Bash)」 or
  「读文件 (Read)」, with the seconds so far) and edited in place: only when its
  text changed, at most once every 3 s (a phase that comes sooner is shown by
  one trailing edit), and at most 18 times (Feishu edits a message at most 20
  times); after that it is left as it is. The answer goes as a new message, so
  the user is notified, and the status message is then edited a last time to
  「✅ 完成 · 用时 Ns · 工具 N 次」, or 「❌ 出错：<reason>」 when the reply carries
  an `error` (the Manager's failed turns do). A failed send or edit is logged
  once and the status message is left alone: it never becomes more messages.
  A reply the Manager sends on its own (`autonomous`) has no status message,
  and a phase after its message's answer is ignored; at most 200 status
  messages are open and 1000 answered messages remembered per channel. Lark
  sends it as text with `lark-cli im +messages-send` (in a group, as a reply
  to the message, `+messages-reply`, like the answer) and edits it with
  `lark-cli im +messages-edit --as bot --message-id <om_…> --text` (with the
  channel's `--profile`), taking the id from the send's JSON answer; Telegram
  uses `send_message` and `edit_message_text`. Only phases go to the status
  message (`status_kind`): a `STATUS_UPDATE` that is neither a phase nor
  streamed text is the Manager's task progress (「任务已分解为 N 个步骤，开始执行...」
  when the task is accepted, then the Planner's progress), and it is sent as
  a message of its own, like an answer, without touching the status message
  or counting as one of its edits; a status without text is not sent. A
  task's end comes as its notice instead (`CHAT_NOTICE`, see the
  TaskRouter), after its progress.
- **A reaction when the bot sees a message** (`status_line.Reactions`): a
  message from a listed sender that goes to the Manager first gets a
  reaction from the bot, Lark's 「Get」 (`lark-cli im reactions create --as bot
  --message-id <om_…> --data '{"reaction_type":{"emoji_type":"Get"}}'`, with
  the channel's `--profile`; the app needs the
  `im:message.reactions:write_only` scope) and Telegram's 👀
  (`set_message_reaction`). The call runs in the background, so the message
  reaches the Manager without waiting for it. A failed reaction is not
  retried and never becomes a chat message; the first failure of a run of the
  channel is logged, naming the missing scope when lark-cli reports one. A
  rejected sender, a Lark group message that does not @-mention the bot, a
  Telegram group message and a message that is not handed on (not text,
  empty) get no reaction.
- **Telegram**: the token is the one saved in Settings → Channels
  (`channel_secrets`; the API only says whether one is set), else the env var
  named by `bot_token_env` (default `LOOPER_TELEGRAM_BOT_TOKEN`). The Bot API
  puts it in every request URL, and it never reaches the log: httpx and
  httpcore, which log each request URL, log only from WARNING unless
  `looper serve --verbose`, and every log line, tracebacks included, is
  masked (`looper/agents/log_redaction.py`): a token in a `bot<token>` URL
  path becomes `bot***`, the configured token anywhere `***`, and a failed
  send's error is masked the same way. `allowed_user_ids` lists numeric
  Telegram user IDs (a negative id is a group or channel, not a sender); a
  comma-separated string from before is still read.
  `LOOPX_TELEGRAM_ALLOWED_USERS` no longer applies. `/new` (without
  arguments) goes to the Manager as `/new`, and `/stop` as `/stop`, which the
  fleet answers outside its queues and which requests all live replies for
  that exact bot/chat. `/profile [n|name|id|0]` goes to it as text `/profile` plus
  the arguments (`_cmd_profile`), which the Manager answers itself
  ([Manager](#manager-looperagentstiersmanagerpy)). `/start`, `/status`,
  `/new`, `/stop`, `/profile` and `/help` from an unlisted sender get no
  answer either. Lark has no command menu: a message that is
  exactly `/stop` goes to the Manager as text like any other and is handled the
  same way, and the status line of the stopped message ends as "已停止".
- **Lark (bot token)** (`lark_bot.py`, `lark_bot_settings.py`): an independent
  `lark_bot` channel (`channel-lark_bot`), disabled by default, using the same
  adapter as `lark` through `LarkTransport`. Its configuration holds `app_id`,
  `domain` (`feishu` or `lark`), and its own app-bound `allowed_open_ids`;
  `channel_secrets.bot_token` holds the app secret and APIs expose only
  `has_bot_token`. It sends through the open platform and receives over its
  long connection, reconnecting with exponential backoff capped at 300 s and
  0.5–1.0 jitter; a stream that yields an event or lasts 60 s resets the backoff. Direct
  messages, group @-mentions, replies, statuses, notices, schedules and gates
  follow the Lark rules below. The backend exposes
  `POST /api/v1/channels/status` and `POST /api/v1/channels/people`
  with `channel_type: "lark_bot"`,
  including sender names and rejected senders, and Settings → Channels has its
  card (app id, domain, app secret, allowlist, search, refused senders). No
  lark-cli binary or login is required.
- **Lark (lark-cli)** (`looper/agents/channels/lark.py`, `lark_cli.py`,
  `lark_settings.py`): the Manager in Lark direct messages and group chats,
  through `lark-cli`
  on PATH and signed in (the commands of lark-cli 1.0.97). A lark-cli profile
  chooses the app and so the bot: the channel's optional `profile` is passed
  as `--profile` on every call, and empty means lark-cli's active profile
  (`app_id_env` and `app_secret_env`, from before, are dropped from the row at
  startup, and from an imported file; a row without `profile` gets an empty
  one, so Settings shows the field). It receives with `lark-cli event
  consume im.message.receive_v1 --as bot`, one flat JSON event per line. It
  handles direct messages (`chat_type` `p2p`) and, in a group chat (`group`),
  only a message that @-mentions the bot: one whose `mentions` hold the bot's
  own open_id (from the bot's info at startup; never a match on names).
  Every other group message is ignored entirely: no reaction, nothing stored,
  no Manager turn, no record of who wrote. The bot's mention is taken out of
  the text (`@name` as lark-cli shows it, else its `@_user_N` placeholder),
  and the Manager is told the sender's name (`contact +search-user`, the
  name the allowlist resolves, else the open_id after 5 s). Other message
  types and bots' messages are ignored too. It sends with `lark-cli im
  +messages-send --as bot`: a reply as Markdown (`--markdown`, a rich-text
  post) to the chat its message came from, a schedule's message as text to a
  user (`--user-id`); a failed send carries lark-cli's reason. In a group the
  status message and the answer reply to the message (`lark-cli im
  +messages-reply --as bot --message-id om_…`), while the task's progress
  and end notice go to the group (`--chat-id`): the bot posts in a group only
  to answer an allowlisted @-mention and for the task it started. A consumer that
  stops is started again after 10 s. At startup the channel runs `lark-cli
  whoami` and `lark-cli api GET /open-apis/bot/v3/info --as bot` with its
  profile and does not start when lark-cli is missing, the profile is
  unknown, it is not signed in or the app has no bot; it logs the bot
  otherwise. `allowed_open_ids` lists the senders' `open_id` (`ou_...`), which
  differ per app: the server binds the list to the app it was made for
  (`allowlist_app_id`, set when the list changes, kept when only the profile
  does), and the channel does not start under a profile of another app until
  the list is rebuilt. A refused sender is also recorded: who, how often,
  when and in which group chat (an @-mention of the bot there), never what
  they wrote (`channel_rejected_senders`, the 20 newest). Settings →
  Channels shows the bot ("Bot: HBDataBot (cli_…, profile …)") or why it would
  not start, the senders' names next to their ids, a search by name or email
  that adds a sender (`contact +search-user --as user`, as the user lark-cli
  is signed in as), and the recently refused senders with Allow (and the
  group chat, for one refused in a group). A Lark
  channel with its own bot token, without lark-cli, is a later channel type.
- **Web**: unaffected, it has no list: the HTTP API answers only local
  requests ([HTTP API](#http-api)).
- A channel that fails to start is logged and skipped; the server keeps
  running.
