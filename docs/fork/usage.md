# Using the LoopX fork: roles, the dispatcher and the CLI

This guide shows how to run a goal with the fork's role-based multi-agent
workflow and how to use the `loopx` commands involved. To see what changed
from upstream, read the [changelog](../../CHANGELOG.md). For the design
rationale, read [design-v0](design-v0.md).

- [1. How it works](#1-how-it-works)
- [2. Install](#2-install)
- [3. Quickstart](#3-quickstart)
- [4. Everyday tasks](#4-everyday-tasks)
- [5. Gates reference](#5-gates-reference)
- [6. CLI reference](#6-cli-reference)
- [7. Files and state layout](#7-files-and-state-layout)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Limitations and further reading](#9-limitations-and-further-reading)

## 1. How it works

A goal has **you (the owner)** and three agent roles:

| role | what it does | what it never does |
|---|---|---|
| **orchestrator** (at most one per goal) | Clarifies requirements with you, proposes the plan, writes each todo's acceptance criteria, handles escalations and replans. It is the only role-assigned agent that opens user gates. | It never implements todos. |
| **developer** | Implements todos, commits, and delivers the work for review. A todo that names repos (`task_repositories`) gets its own git worktree. | It never opens user gates or edits acceptance criteria. Blockers go to the orchestrator. |
| **acceptor** | Reviews a delivery against its criteria. For a todo with repos, it reviews in a throwaway checkout of the delivered commit. Its verdict is accept, reject or blocked. | It never modifies code. Required changes go into its rejection feedback. |

These rules bind agents that have a registered role. An agent without a role
keeps the upstream flat-peer behavior, so give every agent a role.

You talk to the orchestrator through **user gates**. A gate is a question or a
decision with a discussion thread. You reply to it, then close it with
approve, reject or cancel.

The **dispatcher** (`loopx dispatch serve`) runs in the background. It
decides who runs when, and launches each agent Turn with `loopx turn
run-once`. It keeps only its own scheduling bookkeeping (running Turns,
cooldowns, retries) in `<runtime-root>/dispatch/state.json`. Goal state (todos,
gates, plans) is LoopX state, which the CLI, the dashboard and the agents read
and write.

The lifecycle of a goal:

```text
requirements.md
   │  loopx goal create
   ▼
orchestrator planning todo ──► clarification in a gate thread ──► plan card (plan_approval gate)
                                                                   │  you approve
                                                                   ▼
todos created (todos with dependencies start deferred)
   │
   ▼
developer Turn in worktree loopx/<goal>/<todo> ──► delivered: status in_review
                                                     │
                            acceptor Turn in a throwaway review checkout
                              ├─ reject ──► back to the developer with feedback
                              │             (the 2nd reject escalates to the orchestrator)
                              ├─ blocked ─► acceptor_blocked gate for you
                              └─ accept ──► atomic merge into the merge target
                                            (loopx-task/<goal> or the default branch) ──► dependents released
   │  all work merged
   ▼
push_request gate (you approve the push) ──► goal_complete gate (close / add work / leave open)
```

The diagram follows a todo that names repos and requires acceptance, as in the
quickstart below.

### Terms

| term | meaning |
|---|---|
| state home | The directory that holds a goal's LoopX state: the registry, the goal state file and the stored requirements doc. It can be a project directory or a separate "progress" repo. |
| registry | `<state-home>/.loopx/registry.json`. It holds the goal config: agents, roles, repos and authority sources. |
| runtime root | Durable runtime state: todos' event logs, gate threads, plan cards, workspaces, usage ledgers and the dispatcher state. The default is `~/.loopx` (or `LOOPX_RUNTIME_ROOT` when set). The global `--runtime-root` and the registry's `common_runtime_root` take precedence. |
| Turn | One headless agent run (`loopx turn run-once`). It has a typed result, independent validation of material results, and an idempotent writeback. |
| merge target | Where accepted work lands in each repo. It is the task branch `loopx-task/<goal>` with `merge_target=task_branch`, or the default branch with `merge_target=main`. |

### Supported agents

The dispatcher launches agents through two runtimes. An agent file names
one of them in `runtime`, plus a provider whose kind fits it. LoopX keeps no
model allowlist: any model that the runtime's CLI and your provider accept
works.

| runtime | drives | provider kinds | role-specific behavior |
|---|---|---|---|
| `claude-code` | Claude Code: a fresh `claude -p` session per Turn | `anthropic` | Reports its own cost. Receives the dispatcher's system-prompt addendum, and as orchestrator the goal state digest. Settings: `permission_mode`, `reasoning_effort` (`low` to `max`), `system_prompt_file` and raw `extra_args`. |
| `codex-cli` | Codex CLI (`codex exec`) | `openai`, `openai-compatible`, `codex-cpa` (a local CPA proxy) | Reports no USD. LoopX estimates the cost when the provider has matching `pricing`, and otherwise records the Turn as unpriced. Receives no system-prompt addendum or state digest. Settings: `sandbox`, `reasoning_effort` (`none` to `ultra`), and `extra_args` as `KEY=VALUE` config overrides. |

Each provider authenticates with a CLI login, an OAuth token or an API key;
see [step 1](#step-1-describe-your-providers).

Choosing agents for the roles:

- **Any runtime can fill any role.** Roles come from the registry, and each
  goal has at most one orchestrator.
- **Run the orchestrator on `claude-code`.** Only claude-code receives the
  state digest and the orchestrator's command guidance. A codex-cli
  orchestrator gets neither.
- **The acceptor runs without a sandbox by default**, so it can build and
  test: `danger-full-access` on codex-cli, `bypassPermissions` on
  claude-code. The developer and the orchestrator default to `read-only` on
  codex-cli and `dontAsk` on claude-code. Set `permission_mode: acceptEdits`
  for a claude-code developer. An explicit value in the agent file wins over
  these defaults.
- **The end-to-end pilots used** a claude-code orchestrator
  (`claude-fable-5-1`), a claude-code developer (`claude-opus-4-6`, two
  Turns at a time) and a codex-cli acceptor through CPA (`gpt-5.6-sol`).

Not supported by the dispatcher:

- `turn run-once` also has `dsh` and `generic-cli` hosts, but agent files
  accept only `claude-code` and `codex-cli`, so the dispatcher cannot launch
  them, and they record no usage.
- Upstream host integrations (Codex App, Claude Code `/loop` and
  the other `/loopx` surfaces) run their own host-managed loops or
  controllers. The role dispatcher does not launch them.

## 2. Install

Pick one of three ways:

| way | what you get | use it for |
|---|---|---|
| **A. Local release install** (recommended) | `scripts/install-local.sh` run from a clean `main` checkout. It installs a frozen release snapshot, `loopx` and `loopx-canary` on your `PATH`, the man page and the dashboard chat bundle. | Daily use and the dispatcher. It also lets the tests that call `loopx` pass. |
| **B. Source checkout only** | `uv sync --extra test`, then `uv run loopx` or `.venv/bin/python -m loopx.cli`. | Developing LoopX itself. |
| **C. `install.sh` or `loopx update apply`** | `curl -fsSL https://raw.githubusercontent.com/foolzzz/loopx/main/install.sh \| bash`, or `loopx update apply` with its defaults, which the `loopx dashboard` Update tab also runs. Both install the wheel of the latest `foolzzz/loopx` GitHub Release, and install nothing when that release has none; then install from a release tag (`git clone https://github.com/foolzzz/loopx.git && cd loopx && git checkout vX.Y.Z`, then `uv sync`). On a way-A snapshot, `update apply` runs that same `install.sh`. It refuses to touch a source checkout, which you update with Git. | A machine without a checkout, once a release carries a wheel (from v2.5.0). |

Every way needs:

- Python 3.11 or newer, with PyYAML for the agent and provider files;
- `git`, and Node.js 22.22.3 or newer (`loopx doctor` checks it);
- the agent CLIs you configure: `claude` (Claude Code) and/or `codex` (Codex
  CLI). Each must be logged in, or configured with an API key; see
  [step 1](#step-1-describe-your-providers).

### A. Local release install

```sh
git clone <this fork> loopx && cd loopx
git checkout main && git pull --ff-only
uv sync --extra test                         # the Python for LoopX, with PyYAML
LOOPX_PYTHON="$PWD/.venv/bin/python" \
LOOPX_INSTALL_SKILL=0 LOOPX_INSTALL_SLASH_COMMANDS=0 \
  scripts/install-local.sh
export PATH="$HOME/.local/bin:$PATH"
loopx doctor
```

The installer does the following:

- copies the checkout into a snapshot at
  `~/.local/share/loopx/releases/<release-id>` (a timestamp by default);
- links `~/.local/bin/loopx` to the snapshot, and `~/.local/bin/loopx-canary`
  to the live checkout;
- installs `man loopx`, and builds the dashboard chat bundle into the
  snapshot, so `loopx dashboard` needs no separate `npm run build:chat`;
- adds `~/.local/bin` to `PATH` and `~/.local/share/man` to `MANPATH` in your
  shell profile (`~/.zshrc` for zsh) when they are missing.

Choices and guards:

- **Promotion guard.** `loopx` is replaced only when the checkout is clean and
  at the approved ref. That ref is `LOOPX_APPROVED_DEFAULT_REF`, which
  defaults to `origin/main`, or local `main` when `origin/main` is missing. From any other state, the installer does not replace
  `loopx` or create a snapshot. It still builds the checkout's chat bundle
  and refreshes `loopx-canary`, and it installs canary workflow skills only
  when `LOOPX_SKILLS_DIR` is set. `LOOPX_PROMOTE_DEFAULT=1` overrides the
  guard; use it only for a checkout you approved.
- **Python.** The installer records `LOOPX_PYTHON` in the snapshot
  (`.loopx-python`) as the default interpreter. With the command above, that
  is the checkout's `.venv`, so keep it: `uv sync` refreshes it in place.
  Whenever `LOOPX_PYTHON` is set in the environment, it overrides the
  recorded default. Use an inline assignment to override it for one run. If the recorded interpreter is gone, `loopx` falls back to its Python
  discovery, which may find one without PyYAML. Reinstall to change the
  default.
- **Skills and slash commands.** By default, the installer also writes LoopX
  workflow skills into `~/.codex/skills`, plus command entries for its
  supported hosts, including Codex and Claude Code. Claude Code
  gets `/loopx`; Codex uses `$loopx` or `/skills`. The role workflow
  in this guide does not need them.
  `LOOPX_INSTALL_SKILL=0` and `LOOPX_INSTALL_SLASH_COMMANDS=0` skip them, and
  `loopx doctor` then reports its skill checks as missing, which is expected.
  Drop the two variables if you also drive LoopX from these host
  sessions.

**Upgrade.** Run `git pull --ff-only` on `main`, then run the install command
again. Each promoted run creates a new snapshot and repoints `loopx` to it. A
run that fails the promotion guard leaves `loopx` unchanged; see the promotion
guard above.

**Roll back.** `loopx update --rollback <release-id>` repoints `loopx` to a
snapshot. The release ids are the directory names under
`~/.local/share/loopx/releases`. They are timestamps by default, and
`LOOPX_RELEASE_ID` can set another name. `--rollback previous` picks the
valid release whose id sorts highest and that is not the active one. With
default ids, that is the newest non-active snapshot, which is the prior one
only while the newest snapshot is active. After a rollback, name the release
id.

**Uninstall.**

1. If you installed the default skills and command entries, first run
   `loopx slash-commands --uninstall` and `loopx workflow-skills --uninstall`.
2. Remove `~/.local/bin/loopx`, `~/.local/bin/loopx-canary`,
   `~/.local/share/loopx` and
   `~/.local/share/man/man1/loopx.1.gz`.
3. Delete the whole `# LoopX local CLI` and `# LoopX local manual` blocks from
   your shell profile. Each block is a comment line plus the `export` line
   under it.
4. If the installer disabled a legacy `goal-harness` or
   `goal-harness-canary` command, it renamed it to
   `~/.local/bin/<name>.legacy-disabled`. Rename it back, or delete it if you
   no longer want it.

**The dispatcher under launchd.** `loopx dispatch launchd-plist` pins the
plist's `PYTHONPATH` to the LoopX that renders it. Render it with the
installed `loopx`, and the resident dispatcher and the Turns it starts run
that snapshot, not the checkout that the `.venv` installs in editable mode.
The plist names the snapshot's path. After an upgrade, render the plist again
and reload it with `launchctl unload` and then `launchctl load`. Until you
do, the dispatcher keeps running the old snapshot.

### B. Source checkout only

```sh
git clone <this fork> loopx && cd loopx
uv sync --extra test
uv run loopx --help                          # or: .venv/bin/python -m loopx.cli --help
```

`pip install -e '.[test]'` works too. PyYAML ships only in the `test` extra.
A plain `pip install .` needs `pip install pyyaml` before `loopx agent` and
`loopx provider` can read their files. For the dashboard, build the chat
bundle once: `cd apps/presentation/dashboard && npm ci && npm run build:chat`.

Some tests call a `loopx` executable on `PATH`, so they fail without way A.
Architecture tests also need `npm ci --ignore-scripts` at the repository
root.

The examples below write `loopx`. With way B, use `uv run loopx` instead.

## 3. Quickstart

The example builds a feature across two repos, `api` and `web`, with three
agents:

| agent | role | runtime |
|---|---|---|
| `orch` | orchestrator | Claude Code |
| `dev` | developer | Claude Code |
| `acc` | acceptor | Codex through a CPA proxy |

The commands of steps 1 to 4 were run on a scratch state home, and so were
the gate, plan, todo, workspace and usage commands of the later steps. The
dispatcher's model Turns need your provider credentials.

### Step 1: describe your providers

A provider says how to reach a model and where its credential lives. Secrets
never go into the file: a provider names an env var, a keychain entry or a
CLI login.

`~/.loopx/providers.yaml`:

```yaml
providers:
  anthropic-login:
    kind: anthropic
    auth: {type: oauth_cli}          # uses the `claude` CLI's own login
  cpa:
    kind: codex-cpa                  # defaults: base_url http://127.0.0.1:8317/v1, env CPA_API_KEY
    auth: {type: api_key, env: CPA_API_KEY}
    pricing:                         # optional: USD per 1M tokens, for cost estimates
      input: 1.25
      cached_input: 0.125
      output: 10
```

| field | values |
|---|---|
| `kind` | `anthropic`, `openai`, `openai-compatible`, `codex-cpa` |
| `auth.type` | `api_key` (with `env` and/or `keychain: {service, account}`); `oauth_cli` (the CLI's own login); `oauth_token` (for example `CLAUDE_CODE_OAUTH_TOKEN`) |
| `base_url` | Optional. For `anthropic` it becomes `ANTHROPIC_BASE_URL`. |
| `pricing` | Optional. `input`, `cached_input` and `output` are required, and `models: {<id>: {...}}` overrides them per model. Claude Code reports its own cost, so an anthropic provider needs no table. |

### Step 2: define your agents

Each agent has one file. The file name is the agent id. Global files live in
`~/.loopx/agents/<id>.yaml`. A project can override them field by field
in `<project>/.loopx/agents/<id>.yaml`.

`~/.loopx/agents/orch.yaml`:

```yaml
role: orchestrator
runtime: claude-code
provider: anthropic-login
model: claude-fable-5-1
reasoning_effort: medium
extra_args: ["--allowedTools=Bash,Read,Write,Edit,Glob,Grep"]
```

`~/.loopx/agents/dev.yaml`:

```yaml
role: developer
runtime: claude-code
provider: anthropic-login
model: claude-opus-4-6
permission_mode: acceptEdits         # let the developer edit files
max_concurrency: 2                   # up to 2 todos of a goal in parallel
```

`~/.loopx/agents/acc.yaml`:

```yaml
role: acceptor
runtime: codex-cli
provider: cpa
model: gpt-5.6-sol
# no sandbox set: the acceptor role default is danger-full-access, so it can build and test
```

| field | notes |
|---|---|
| `role` | `orchestrator`, `developer` or `acceptor`. The registry role (step 4) is the one that counts. |
| `runtime` | `claude-code` or `codex-cli`. |
| `provider` | Must exist in `providers.yaml`, and its kind must fit the runtime. |
| `model`, `reasoning_effort` | claude: `low` to `max`; codex: the LoopX effort vocabulary. |
| `system_prompt_file` | claude-code only. The path is relative to the yaml file. |
| `permission_mode` | claude-code. The default is `dontAsk`, or `bypassPermissions` for an acceptor. |
| `sandbox` | codex-cli. The default is `read-only`, or `danger-full-access` for an acceptor. |
| `max_concurrency` | How many Turns this agent runs at once. The orchestrator is always 1 per goal. |
| `extra_args` | claude: raw flags. codex: `KEY=VALUE` config overrides. A project file replaces the whole list. |
| `enabled` | `false` skips the agent. |

### Step 3: check the configuration

```sh
loopx provider list
loopx provider check                     # ok | missing_credential | not_logged_in | cli_missing | timeout | ...
loopx agent validate --check-auth        # exit 1 if any agent is invalid
loopx agent show acc                     # merged definition plus the exact `turn run-once` host flags
```

`provider check` never prints secrets. The dispatcher runs the same preflight
before it launches each agent.

### Step 4: create the goal

Write the requirements as Markdown, for example `req.md`. Its first heading
becomes the goal objective. Then:

```sh
loopx goal create --project ~/work/progress --goal-id todo-due --doc req.md \
  --repo api=~/work/api,merge_target=task_branch \
  --repo web=~/work/web,merge_target=task_branch \
  --agent orch=orchestrator --agent dev=developer --agent acc=acceptor
```

This command:

- bootstraps the goal in the state home `~/work/progress`, with the registry
  at `~/work/progress/.loopx/registry.json`;
- copies `req.md` to `docs/goals/todo-due/req.md` and registers it as the
  goal's requirements source;
- registers the three agents with their roles, sets `agent_model=role_v1`,
  and declares the repos;
- adds the orchestrator's first todo: clarify the requirements, then propose
  a plan.

Notes:

- The `--repo` spec is `NAME=PATH[,default_branch=B][,merge_target=main|task_branch][,task_branch=B]`.
  A relative path resolves against the current directory.
- Exactly one `--agent ID=orchestrator` is required.
- Without `--repo`, agents work directly in the state home. See
  [Goals without a code repository](#goals-without-a-code-repository).
- `--dry-run` previews the result. `--no-global-sync` keeps the goal out of
  the shared registry.
- Running the command again is safe, because every step is an upsert.

**Where to run later commands.** In the state home directory, `loopx` finds
the project registry by itself. Elsewhere, pass
`--registry ~/work/progress/.loopx/registry.json`.

### Step 5: start the dispatcher

```sh
cd ~/work/progress
loopx dispatch serve --goal-id todo-due --project . --max-global 3 \
  --validation-command-json '["make","test"]'   # fallback; use your project's test command
```

A Turn with a material result must pass an independent validation command.
Material results are validated progress, validated completion, a repair
request and a replan request. A Turn that stops, because it waits, needs
your action or reports a failed iteration, needs no validation. The dispatcher picks the command in this
order:

1. the todo's own `validation_command`;
2. a built-in state check, for the orchestrator's planning, action and
   escalation todos;
3. `--validation-command-json`.

With none of them, a material result fails validation and the todo backs
off. So give every plan todo a `validation_command`, or pass a fallback.

- `serve` watches the state files and runs a reconcile pass every
  `--tick-seconds` (default 60). It prints one JSON line per pass that acts,
  and an idle heartbeat line after 15 minutes without output. A Turn that
  failed shows its `error_code` and `error` there.
- In the state home, `serve` needs no `--registry`. It hands every Turn the
  registry as an absolute path, so a developer Turn that runs in its todo's
  worktree still finds the goal. `launchd-plist` writes the same absolute
  path into the job.
- `--once` runs one pass, waits for the Turns it launched, and exits. Use it
  for cron or for debugging.
- Only one dispatcher runs per runtime root. A second one exits with code 3.
- To keep it running on macOS, generate a launchd job. The command only
  prints it:

```sh
loopx dispatch launchd-plist --goal-id todo-due --project . > ~/Library/LaunchAgents/com.loopx.dispatch.plist
launchctl load ~/Library/LaunchAgents/com.loopx.dispatch.plist
```

Once running, the dispatcher launches the orchestrator on its planning todo.

### Step 6: clarify and approve the plan

The orchestrator asks its questions in a gate thread: a question gate, or
the thread of the `plan_approval` gate that its first plan card opens.

```sh
loopx gate list --goal-id todo-due                         # open gates, who each awaits
loopx gate show --goal-id todo-due --todo-id <gate-id>     # status, thread and, for a plan, its plan card
loopx plan show --goal-id todo-due --plan-id <plan-id>     # the full plan, with agent bindings, estimates and todo ids
loopx gate reply --goal-id todo-due --todo-id <gate-id> --text "Use the local date; do the contract first."
```

Your reply wakes the orchestrator. It answers in the thread, and it can revise
the plan in place (`plan propose --revise`). When the plan is right, approve
it:

```sh
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --decision approve
```

LoopX then creates the plan's todos:

- todos with dependencies start `deferred`;
- each todo carries the acceptance criteria and validation command that its
  plan item declares (both are optional);
- the orchestrator's planning todo is closed automatically.

A `reject` or `cancel` applies nothing: the plan card is closed with that
decision, and the orchestrator can propose again.

### Step 7: watch progress

```sh
loopx todo list --goal-id todo-due          # todos plus "Dependency waits"
loopx dispatch status                       # running Turns, per-agent slots, provider/agent/todo cooldowns (--format json adds gates, retries)
loopx status --goal-id todo-due             # goals, gates, attention queue
loopx usage report --goal todo-due --by role
loopx dashboard --goal-id todo-due          # browser: Role board tab (角色看板)
```

The dashboard's **Role board** shows:

- "Waiting on you" at the top;
- the columns Planned, Assigned, Running, In review, Rework and Done, in
  orchestrator, developer and acceptor swimlanes;
- a cost strip.

Clicking a gate opens its drawer, where you can read the thread, reply and
decide. The drawer refreshes every 5 s while it is open, and shows what you
are deciding: the plan card, the push commits, the budget or the goal
summary. It enables decisions only once it has read the gate and the gate is
still open.

What happens on its own:

- For a todo that names repos, a developer works in
  `<runtime-root>/goals/todo-due/workspaces/<todo>/<repo>` on the branch
  `loopx/todo-due/<todo>`. It commits, and it never pushes.
- When the developer's Turn succeeds, LoopX runs the todo's validation
  command. A todo that requires acceptance then moves to `in_review`, and any
  other todo moves to `done`. On a goal with registered roles, a todo
  requires acceptance when it says `requires_acceptance=true`. Without the
  flag, it requires acceptance when it is a developer advancement todo and
  the goal has an acceptor.
- The acceptor reviews the delivery. For a todo with repos, it works in a
  detached checkout of the delivered commit.
  - **Reject** reopens the todo for the developer, with feedback that should
    name the failed criteria. The second reject blocks the todo and hands it to
    the orchestrator.
  - **Accept** merges the delivered commit into the merge target of each of
    the todo's repos, atomically, then marks the todo done. This releases its dependents.

### Step 8: answer gates as they come

The orchestrator opens a gate when it needs you. LoopX opens system gates for
blocked reviews, budgets, re-logins, long provider cooldowns, repeated
orchestrator failures, pushes and completion. Section
[5](#5-gates-reference) lists every kind and its options. The common ones:

```sh
# The acceptor could not review (for example missing tooling). Pick one option:
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option retry_acceptance
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option accept_manually --note "checked by hand"
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option return_to_developer --note "install node 22 first"
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option cancel_todo
```

### Step 9: push

When no agent todo of the goal is open, in review, blocked or deferred (a
dependent still waiting on its dependency counts as unfinished), and a merge
target has commits that its remote lacks, the dispatcher opens a
`push_request` gate. `gate show` lists each repo's branch, its remote, the
commit count and range, and a short log. `gate resolve` prints the result
per repo; after a failed push it names the follow-up gate that retries it.

```sh
loopx gate show --goal-id todo-due --todo-id <gate-id>
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --decision approve   # git push <remote> <branch>, never force
```

`reject` or `cancel` pushes nothing. The dispatcher and the orchestrator do not
offer the same heads again until new merges arrive. You can still ask for them
with `loopx goal request-push`, which prints the gate it opened or why none
opened. A repo without a remote is skipped. To push without a gate, run
`git push` yourself.

### Step 10: close the goal

Once the work is merged and the push is resolved, the dispatcher opens one
`goal_complete` gate. It needs no model Turn. `gate show` prints its
summary: the accepted, rejected and superseded todo counts, the merges and
push result per repo, and the cost. `gate resolve` prints what the option did.

```sh
loopx gate show --goal-id todo-due --todo-id <gate-id>
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option close_goal     # stop the goal (reversible)
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option add_work --note "Also sort by due date"
loopx gate resolve --goal-id todo-due --todo-id <gate-id> --option leave_open
```

`add_work` turns your note into an orchestrator todo, so planning continues.
After `close_goal`, `loopx status` shows the goal as `activation=stopped`.

Closing a goal does not remove the todos' worktrees and branches. Remove
each merged todo's worktrees yourself:

```sh
loopx workspace cleanup --goal-id todo-due --todo-id <todo-id> --dry-run   # preview
loopx workspace cleanup --goal-id todo-due --todo-id <todo-id>
```

## 4. Everyday tasks

### Talk to the orchestrator

- **Answer or ask in an open gate:** `loopx gate reply --goal-id G --todo-id
  <gate> --text "..."`.
- **Ask for new work mid-flight:** add a todo for the orchestrator. It plans
  the work. It can propose a plan card for you to approve, and its change to
  an existing todo's acceptance criteria always needs one.

  ```sh
  loopx todo add --goal-id G --role agent --text "Also support sorting by due date" \
    --required-role orchestrator --requires-acceptance false
  ```
- **After the goal is done:** use the `goal_complete` gate's `add_work`
  option.

### Change a todo's acceptance criteria

After planning, an agent changes criteria only through a plan card that you
approve. Ask the orchestrator in a gate or todo. It proposes a card with
`criteria_changes`, and you see the old and new criteria side by side in
`loopx plan show` or `gate show`. While the card is pending, the acceptor
does not review that todo.

As the owner, you can still edit criteria directly. Pass no `--agent-id`. The
edit is logged as a major change.

```sh
loopx todo update --goal-id G --todo-id T --acceptance-criteria "GET /todos?overdue=true returns only overdue items"
```

An agent that tries this, the orchestrator included, is refused with
`acceptance_criteria_change_requires_plan`.

### Replace or split a todo

```sh
loopx todo add --goal-id G --role agent --text "Web: render due" --task-repo web --acceptance-criteria "..."
loopx todo supersede --goal-id G --todo-id OLD --by NEW1,NEW2 --agent-id orch --note "split"
```

The command closes `OLD` as superseded, and every todo that depended on it
now depends on all the replacements. A superseded todo never counts as done.
Only the orchestrator, or the owner with no `--agent-id`, may do this. Normally
the orchestrator does it after an escalation.

### Budgets and cost

```sh
loopx usage budget --goal G --set 50          # USD; --clear removes it; no flag shows spend against budget
loopx usage report --goal G --by todo         # also --by role|agent|goal|model|day, --days N, --since ISO, --format json
```

- With no budget set, nothing happens: no alert, no gate, no pause.
- At 80% of the budget, LoopX opens a non-blocking alert.
- At 100%, a `budget_exhausted` gate stops new Turns of the goal. Running
  Turns finish. Resolve the gate with:
  - `raise_budget --note 75`, where the note gives the new amount. Without an
    amount, the budget grows by 50%.
  - `continue_without_limit`.
  - `stop_goal`.

Codex through CPA reports no USD. Its cost comes from the provider's
`pricing` table and is marked estimated.

### Stop and resume a goal

```sh
loopx goal-lifecycle --goal-id G --operation stop --actor-kind owner --execute
loopx goal-lifecycle --goal-id G --operation resume --actor-kind owner --execute
```

Stopping a goal stops its automatic advancement and keeps its todos and
history. Without `--execute`, the command only previews. The `close_goal` and
`stop_goal` gate options use the same stop. The stop is recorded in the goal's
activation state (`loopx status` shows `activation=stopped`); the registry
`status` field stays `active`. A goal that the shared registry does not list,
for example one created with `--no-global-sync`, is stopped and resumed in
its own registry only.

### Run or debug a single step

```sh
loopx dispatch serve --goal-id G --once                     # one pass; waits for the Turns it launched
loopx quota should-run --goal-id G --agent-id dev           # would this agent run now, and on which todo?
loopx agent show dev                                        # the host flags the dispatcher passes
loopx workspace status --goal-id G --todo-id T              # dirty, ahead or behind, predicted conflicts
```

The dispatcher stores each Turn's output in
`<runtime-root>/dispatch/runs/<run>.*`.

### Act on todos by hand

The dispatcher does these steps for you. You rarely need them, but they are
handy for recovery and scripting.

```sh
loopx workspace prepare --goal-id G --todo-id T             # worktree per repo on loopx/G/T
loopx workspace merge   --goal-id G --todo-id T --dry-run   # atomic, all repos or none; never pushes
loopx workspace cleanup --goal-id G --todo-id T             # only when merged (or --force)
loopx todo claim        --goal-id G --todo-id T --claimed-by dev --agent-id dev   # --claimed-by is required
loopx todo accept       --goal-id G --todo-id T --agent-id acc [--note ...]
loopx todo reject       --goal-id G --todo-id T --agent-id acc --note "criterion 2 fails: ..."
loopx todo block-review --goal-id G --todo-id T --agent-id acc --reason "toolchain missing"
loopx goal request-push --goal-id G                         # open the push gate now
```

### Goals without a code repository

`loopx goal create` without `--repo` is fine for work that is not code in a
repository, for example notes or documents in the project directory. The
project directory (the state home) then stands in as the goal's implicit
repo `main`, and it may or may not be a git repository.

What works: planning, gates, todos, developer and acceptor Turns under the
dispatcher, accept, reject and escalation, budgets and the `goal_complete`
gate. The honest limits:

- **No isolation.** A todo that names no repo gets no worktree and no review
  checkout. Its developer and acceptor Turns run in the project directory
  itself and deliver from there. There is no delivery snapshot: the acceptor
  reviews the directory as it is, not a fixed copy of the delivery. Nothing
  is merged and nothing can be rolled back: an accept keeps whatever is in the
  directory, and a reject leaves it for the next developer Turn to fix.
- **One Turn at a time.** Per goal, the dispatcher runs at most one developer
  or acceptor Turn on such todos. Others wait, and the pass and
  `loopx dispatch status` show them with `project_directory_turn_running`.
  Orchestrator Turns and Turns on todos with repos are not affected.
  The slot follows the `turn run-once` process the dispatcher recorded, not
  the agent host it started: if that process is killed while its host,
  which runs in its own session, keeps working, or if the dispatcher dies in
  the instant between starting it and recording its pid, the dispatcher
  frees the slot and can start a second Turn in the same directory.
- **One goal per project directory.** The rule is per goal. Two goals that
  share one project directory are not kept apart, and this is not
  supported: give each goal its own directory.
- **No push gate.** Nothing is merged, so the dispatcher opens no
  `push_request` gate. A project directory that is not a git repository has
  nothing to push; in a git project directory, whatever the agents commit
  stays where it is until you push it.
- **goal_complete works.** Once every todo is accepted, the `goal_complete`
  gate opens as usual. It lists the project directory as local only when the
  directory is not a git repository, has no commit yet, or has no remote. A
  git project directory with a remote shows its usual push state, such as
  already on the remote or nothing of this goal to push.

Prefer `--repo` for code, when two todos should run in parallel, or when
you want review before merge, a clean rollback of a rejected delivery, or a
push gate. A goal may also mix both: todos that name repos get worktrees, and
the rest run one at a time in the project directory.

## 5. Gates reference

Every gate has a thread (`gate reply`, `gate show`) and closes with
`loopx gate resolve --goal-id G --todo-id GATE ...`. You can also close it
from the dashboard drawer, which offers the options the gate lists. The
decision recorded first wins on every surface. A later dashboard decision on
the same gate is reported stale and changes nothing. `loopx gate resolve` on
a closed gate fails with `gate_closed`, and a `todo complete
--decision-outcome` replay that names another decision fails with
`gate_already_decided`. If the drawer cannot read the gate or
its options, it disables the decisions: use `loopx gate show` and
`loopx gate resolve` instead.
`loopx todo complete --role user --decision-outcome ...` is the older
equivalent.

Only you decide gates. LoopX marks the process that runs each agent Turn's
model with `LOOPX_AGENT_TURN` (except the in-process `--dsh-runner` test hook). An agent that runs `gate resolve` or `todo
complete --decision-outcome` there, with or without `--option`, is refused with
`gate_decision_refused_in_agent_turn` and writes nothing; it can still reply
with `gate reply`, but only as its own agent: a reply as you (the default
`--as user`) or with another `--agent-id` fails with
`gate_reply_identity_mismatch`, and only the orchestrator replies as an agent.
This guards against accidental self-approval and impersonation. It is not a
security boundary: agents run as your OS user, and one that removes the variable
bypasses it. Every decision records `closed_by` in the gate index: the surface
(`cli`, `dashboard` or `system`), the agent named with `--agent-id` (else
`owner`, even when `gate resolve` attributes the todo write to the gate's
agent), the agent Turn marker if one was set, and the time. A replay that
fills in a missing record marks it `replayed`. `gate show` prints it. Details:
[gates-plans-intake-v0](gates-plans-intake-v0.md#only-the-owner-decides-user-gates).

| kind | opened by | holds | resolve with |
|---|---|---|---|
| `decision` | the orchestrator: a question or a choice | the agent it blocks | `--decision approve \| reject \| cancel`, after replying in the thread |
| `plan_approval` | `loopx plan propose` | the orchestrator | `approve` applies the plan. `reject` and `cancel` apply nothing. Reply in the thread to request changes. |
| `acceptor_blocked` | LoopX, when the acceptor gives a blocked verdict | the acceptor, on that todo | `--option retry_acceptance \| accept_manually` (approve), `return_to_developer` (reject), `cancel_todo` (cancel) |
| `push_request` | the dispatcher once all work is merged, or `loopx goal request-push` | the orchestrator | `approve` pushes each merge target. `reject` and `cancel` decline. |
| `budget_exhausted` | the dispatcher at 100% of a goal budget | all new Turns of the goal | `--option raise_budget --note <USD> \| continue_without_limit` (approve), `stop_goal` (reject) |
| `goal_complete` | the dispatcher once the goal is finished | the orchestrator | `--option close_goal` (approve), `add_work --note "..."` (reject), `leave_open` (cancel) |
| re-login needed | the dispatcher when an auth preflight fails | that agent | Log in again (`claude auth login` or `codex login`), then approve. |
| provider in long cooldown | the dispatcher when a provider backoff reaches `--long-cooldown-seconds` | the agent that hit it | Close it when the provider is usable again. |
| orchestrator repeat limit | the dispatcher when orchestrator action todos keep failing | the orchestrator | Check the goal, then close it. |

A bare `--decision` selects a default option:

| gate | approve | reject | cancel |
|---|---|---|---|
| `acceptor_blocked` | `retry_acceptance` | `return_to_developer` | `cancel_todo` |
| `budget_exhausted` | `raise_budget` | `stop_goal` | `stop_goal` |
| `goal_complete` | `close_goal` | `add_work` | `leave_open` |

On `budget_exhausted`, `cancel` also stops the goal. Pass `--option` explicitly
on system gates.

## 6. CLI reference

Global options go before the command:

| option | meaning |
|---|---|
| `--registry PATH` | The registry to use. Inside a state home, the project registry is found by itself. |
| `--runtime-root PATH` | The runtime state root. The default is the registry's `common_runtime_root`, else `LOOPX_RUNTIME_ROOT` when set, else `~/.loopx`. |
| `--format markdown\|json` | The output format. Most subcommands also accept `--format` after the subcommand. |

Every command has `--help`. `loopx commands` lists all commands, including
the upstream ones not covered here.

### `loopx goal`

| command | purpose |
|---|---|
| `goal create --project DIR --goal-id G --doc FILE [--repo SPEC]... --agent ID=ROLE... [--objective TEXT] [--no-global-sync] [--dry-run]` | Create a role_v1 goal from a requirements doc (step 4). |
| `goal request-push --goal-id G [--agent-id ORCH] [--dry-run]` | Open the `push_request` gate now. It does not wait for pending todos. Prints the gate id and its repos, or why no gate opened. |

### `loopx agent`, `loopx provider`

| command | purpose |
|---|---|
| `agent list [--project P]` | Merged agent definitions: global plus the project's `.loopx/agents`. |
| `agent show ID [--project P] [--check-auth]` | One agent, its sources and its `turn run-once` host flags. |
| `agent validate [ID] [--project P] [--check-auth]` | Validate agent files. Exits 1 on an invalid agent. |
| `provider list` | The configured providers, without secrets. |
| `provider check [--name N] [--timeout-seconds S]` | The auth preflight. |

### `loopx dispatch`

| command | purpose |
|---|---|
| `dispatch serve --goal-id G [--goal-id G2 ...] [options]` | Run the resident dispatcher. |
| `dispatch status` | Running Turns, per-agent slots, provider, agent and todo cooldowns. A todo cooldown shows its failure count, when it ends and the last error. With `--format json`, it also lists the gates the dispatcher opened and its retry identities. |
| `dispatch launchd-plist --goal-id G [options] [--label L]` | Print a launchd job that runs `serve`. It installs nothing. |

`serve` options (`launchd-plist` takes the same, except `--once`):

| option | default | meaning |
|---|---|---|
| `--project P` | the goal's legacy `repo`, else the registry's directory | Where project agent files live, and the default Turn cwd. |
| `--tick-seconds` | 60 | The periodic reconcile interval. |
| `--poll-seconds` | 3 | The state-file watch interval. |
| `--max-global` | 4 | A machine-wide cap on concurrent Turns. |
| `--turn-timeout-seconds` | 3600 | The per-Turn timeout, passed to `turn run-once` as `--timeout-seconds`. |
| `--long-cooldown-seconds` | 3600 | A provider cooldown this long opens a user gate. |
| `--backoff-base-seconds` | 60 | The first provider backoff step, doubling up to 6 h. |
| `--idle-heartbeat-seconds` | 900 | Prints an idle line after this long without output. `0` disables it. |
| `--validation-command-json '["make","test"]'` | none | The fallback validator for todos that declare none. Without it, when no built-in validator applies, a material result of those Turns fails validation. |
| `--no-global-sync` | off | Passes `--no-global-sync` to every Turn. |
| `--once` | off | Runs one pass, waits for its Turns, and exits. |

### `loopx gate`

| command | purpose |
|---|---|
| `gate list --goal-id G [--awaiting user\|orchestrator]` | Open user gates, their kind and who they await. |
| `gate show --goal-id G --todo-id GATE` | Status, kind, thread and kind-specific content: the plan card, criteria changes, push repos, the budget or the completion summary. The Markdown output lists a push gate's repos with branch, remote, commit count and short log, and a goal_complete gate's counts, merges, push results and cost; once closed, their result. A part that cannot be read is reported with its error code. Once decided, who decided it (`closed_by`). |
| `gate reply --goal-id G --todo-id GATE --text TEXT [--as user\|orchestrator --agent-id ORCH]` | Append to the thread. The default is you. Inside an agent Turn, only `--as orchestrator` as the Turn's own agent (else `gate_reply_identity_mismatch`). Replying never closes a gate. |
| `gate resolve --goal-id G --todo-id GATE [--decision approve\|reject\|cancel] [--option OPT] [--note TEXT] [--agent-id A] [--dry-run]` | Close a gate and print the decision with its outcome (the push result per repo, the goal_complete option's effect). For options, see [section 5](#5-gates-reference). `--agent-id` is the lifecycle actor (default: the agent the gate blocks); `closed_by` records it only when given, else `owner`. Refused inside an agent Turn. |

### `loopx plan`

The orchestrator uses these commands. You mostly read them.

| command | purpose |
|---|---|
| `plan propose --goal-id G --agent-id ORCH --plan-file plan.json [--revise PLAN_ID]` | Propose a plan card, or revise a pending one in place. The card opens a `plan_approval` gate. Inside an agent Turn, `--agent-id` defaults to the Turn's agent and must match it (else `gate_reply_identity_mismatch`). |
| `plan show --goal-id G --plan-id P` | The plan's todos, dependencies, criteria and criteria changes. |
| `plan list --goal-id G [--require-status pending\|applying\|applied\|rejected\|cancelled]` | The goal's plans. With `--require-status`, exits 1 unless a plan has that status. |
| `plan apply --goal-id G --plan-id P` | Recovery only. Finishes an interrupted apply of a plan whose gate is recorded done with decision approve. On a pending or applying plan whose gate is not recorded approved, it exits 1 with `plan_not_approved` and creates no todos. On a rejected or cancelled plan it exits 1 with `plan_not_applicable`. It checks the recorded decision, not who made it; an agent Turn cannot record one (see [section 5](#5-gates-reference)). |

A plan file looks like this:

```json
{
  "title": "Todo due dates v1",
  "summary": "Contract first, then API and web in parallel, then integration.",
  "todos": [
    {"key": "contract", "text": "Write the API contract", "task_repositories": ["api"],
     "acceptance": "openapi.yaml documents the optional ISO due field",
     "validation_command": "test -f openapi.yaml", "priority": "P1"},
    {"key": "api", "text": "Implement due in the API", "depends_on": ["contract"], "task_repositories": ["api"],
     "acceptance": "invalid dates return 400", "validation_command": "python3 -m unittest"},
    {"key": "web", "text": "Show due in the web client", "depends_on": ["contract"], "task_repositories": ["web"],
     "acceptance": "the list renders the due date", "validation_command": "node --test"},
    {"key": "integrate", "text": "Integrate web and API", "depends_on": ["api", "web"], "task_repositories": ["api", "web"],
     "acceptance": "both suites pass from the workspace root",
     "validation_command": "sh -c 'cd api && python3 -m unittest && cd ../web && node --test'"}
  ]
}
```

Each todo item accepts these fields:

- `key` and `text`;
- `priority` (`P0` to `P3`);
- `required_role` (default `developer`);
- `bound_agent` and `acceptor_agent`;
- `depends_on`, or its inverse `successors`;
- `requires_acceptance`;
- `task_repositories`;
- `acceptance` (at most 1000 characters);
- `validation_command`, `estimated_effort` and `action_kind`.

List the todos in dependency order. A plan can also carry
`criteria_changes: [{todo_id, new, reason, old?}]` for existing todos. For
the full format, see [gates-plans-intake-v0](gates-plans-intake-v0.md#plan-cards-decision-12).

### `loopx todo` (role_v1 additions)

These are the fork's additions to the upstream `todo` command:

| command or flag | purpose |
|---|---|
| `todo accept --goal-id G --todo-id T --agent-id ACC [--note TEXT] [--evidence TEXT]` | Accept an `in_review` todo. For a todo with repos, it merges first, then completes. |
| `todo reject --goal-id G --todo-id T --agent-id ACC --note TEXT` | Reject it. The note is required and should name the failed criteria. The second reject escalates. |
| `todo block-review --goal-id G --todo-id T --agent-id ACC --reason TEXT` | The acceptor cannot review. This opens an `acceptor_blocked` gate. |
| `todo supersede --goal-id G --todo-id OLD --by NEW[,NEW2] [--agent-id ORCH] [--note TEXT]` | Replace or split a todo, and rewire its dependents. |
| `todo complete ...` | On a role_v1 goal that has registered roles, an open agent todo that requires acceptance goes to `in_review` instead of `done`. A goal without roles, and user todos, complete directly. |
| `--status in_review` | A first-class status. `todo add` cannot create it. |
| `--required-role orchestrator\|developer\|acceptor` | Route a todo to a role. The default is developer for work, and orchestrator for gates, blockers and replans. |
| `--requires-acceptance true\|false` | Whether completion needs an acceptor, on a goal that has registered roles. `true` sends the delivery to review, and `false` never does. Without the flag, a developer advancement todo needs one only when the goal has an acceptor. |
| `--acceptor-agent ID` | Bind a specific acceptor. |
| `--task-repo NAME` (repeatable) | The goal repos the todo touches. Several repos make an atomic multi-repo todo. |
| `--acceptance-criteria TEXT` | The todo's criteria. Under role_v1, only the orchestrator or you may set them. Agents without a registered role keep the upstream behavior. After planning, an agent's change needs a plan card, and you can still edit directly. |
| `--review-feedback TEXT` | Rework instructions for the developer (`todo update`). |
| `--reject-count N` | The rejection counter. |
| `--clear-*` | Clears the matching field: `--clear-required-role`, `--clear-acceptor-agent`, `--clear-task-repos`, `--clear-acceptance-criteria`, `--clear-review-feedback`. |

`loopx todo list` shows `dependency_waits`: why each deferred todo is still
waiting.

### `loopx workspace`

`loopx workspace prepare | status | merge | cleanup --goal-id G --todo-id T
[--repo NAME]...`. `prepare`, `merge` and `cleanup` also take `--dry-run`, and
`cleanup` takes `--force`. Without `--repo`, a command uses the todo's
`task_repositories`, else every goal repo.

| action | effect |
|---|---|
| `prepare` | Creates or reuses one worktree per repo at `<runtime-root>/goals/G/workspaces/T/<repo>`, on the branch `loopx/G/T`, starting from the merge target. |
| `status` | Per repo: whether the worktree is dirty, how far it is ahead of or behind the target, unmerged paths and predicted conflicts. |
| `merge` | A no-ff merge into every repo's target with `LoopX-Goal` and `LoopX-Todo` trailers. It merges all repos or none, and never pushes. |
| `cleanup` | Removes this todo's worktrees and branch once merged, or with `--force`, which discards the work. |

### `loopx usage`

| command | purpose |
|---|---|
| `usage report [--goal G]... [--since ISO \| --days N] [--by role\|agent\|goal\|todo\|model\|day] [--format json\|text\|markdown]` | Turns, agent-hours, tokens and cost, which may be reported or estimated. It also shows the cost per accepted todo with and without the orchestrator. |
| `usage budget --goal G [--set USD \| --clear]` | Show, set or clear a goal budget. |

### Goal configuration: `configure-goal` and `register-agent`

`goal create` covers the usual setup. Use these commands to change a goal
later. `configure-goal` only previews until you add `--execute`.

```sh
loopx register-agent --goal-id G --agent-id dev2 --role developer --execute   # register an agent with a role
loopx configure-goal --goal-id G --agent-role dev2=acceptor --execute      # change a registered agent's role
loopx configure-goal --goal-id G --clear-agent-role dev2 --execute
loopx configure-goal --goal-id G --agent-model role_v1 --execute           # move a peer_v1 goal to roles
loopx configure-goal --goal-id G --repo docs=/abs/docs,default_branch=main,merge_target=main --execute
loopx configure-goal --goal-id G --clear-repos --execute
```

Two rules apply:

- A goal has at most one orchestrator. To move the role, clear the old
  orchestrator and assign the new one in the same call.
- A role must name a registered agent.

### `loopx turn run-once` (host flags)

The dispatcher builds these flags from the agent file (see `loopx agent show
ID`). You only need them to run a Turn by hand.

| flag | meaning |
|---|---|
| `--host claude-code \| codex-cli` | The built-in host. |
| `--claude-model`, `--claude-effort low..max`, `--claude-permission-mode`, `--claude-system-prompt-file`, `--claude-extra-arg=--flag`, `--claude-provider NAME`, `--claude-bin` | Claude Code. Each Turn is a fresh `claude -p` session. |
| `--codex-config KEY=VALUE` (repeatable), `--codex-provider NAME` | Codex config overrides, passed as `-c`. The provider expands its CPA or openai-compatible definition. |
| `--todo-id T` | Pin the Turn to one eligible todo. |

Required: `--goal-id`, `--agent-id` and `--project`. Add `--execute` to run
for real.

### Upstream commands you will still use

| command | purpose |
|---|---|
| `status [--goal-id G]` | Goals, gates, the attention queue and the next action. `--format json` includes `role_board` and `turn_usage_summary`. |
| `dashboard [--goal-id G] [--port 8767] [--no-open]` | The local web UI: role board, gate drawer and discussion panel. |
| `goal-lifecycle --goal-id G --operation stop\|resume --actor-kind owner --execute` | A reversible stop or resume. |
| `quota should-run --goal-id G --agent-id A` | Whether an agent runs now, and on which todo. |
| `todo list --goal-id G` | Todos, with dependency waits. |

## 7. Files and state layout

In the state home:

```text
<state-home>/
  .loopx/registry.json                   goal config: agents, roles, repos, authority sources
  .loopx/agents/<id>.yaml                optional project overrides of agent files
  .loopx/goals/<goal>/ACTIVE_GOAL_STATE.md   the default goal state file (todos)
  .loopx/project-goals/<goal>/ACTIVE_GOAL_STATE.md  same-path collision route
  docs/goals/<goal>/<requirements>.md    the stored requirements doc
```

Only one goal-state path applies. When `<project>/.loopx/goals` and
`<runtime-root>/goals` resolve to the same physical path,
`.loopx/project-goals/` keeps project-owned state separate from the runtime
`goals/` tree. The registry goal's `state_file` is authoritative; consumers
must not reconstruct the path from the goal id.

In the runtime root (default `~/.loopx`, or `LOOPX_RUNTIME_ROOT` when set):

```text
<runtime-root>/
  providers.yaml, agents/<id>.yaml       provider and agent definitions
  goals/<goal>/
    rollout-event-log.jsonl              goal events (plan, gate, review, push, budget, ...)
    gates/index.json, gates/<gate>.jsonl gate index and discussion threads
    plans/<plan>.json                    plan cards
    plans/supersessions.jsonl            todo supersessions (dependency rewiring)
    workspaces/<todo>/<repo>             developer worktrees
    reviews/<todo>/<attempt>/<repo>      throwaway acceptor checkouts (removed after each review)
    push/state.json                      declined heads and last push results
    usage.jsonl, usage-budget.json       usage ledger and optional budget
  dispatch/
    serve.lock, state.json               single-instance lock and dispatcher state
    runs/<run>.*                         each Turn's output
    logs/                                launchd output
```

Lark consumer leases are machine-scoped: they live under the default runtime
root (`LOOPX_RUNTIME_ROOT` or `~/.loopx`) in `lark-consumers/`, whatever a
service's `--runtime-root`, so one bot App has one consumer per machine.
Commands that LoopX renders for another shell (agent prompts, SSH commands)
spell the default as `${LOOPX_RUNTIME_ROOT:-$HOME/.loopx}`. That shell
resolves a relative value against its own working directory and does not
trim whitespace, so set `LOOPX_RUNTIME_ROOT` to an absolute path when agents
or remote hosts use it.

In each code repo:

- `loopx/<goal>/<todo>`: one todo branch.
- `loopx-task/<goal>`: the merge target when `merge_target=task_branch` and
  no `task_branch` is set. With `merge_target=main`, the default, accepted
  work lands on the default branch.

## 8. Troubleshooting

| symptom | cause and fix |
|---|---|
| `dispatch serve` exits with code 3 (`dispatcher_locked`) | Another dispatcher already serves this runtime root. Check it with `loopx dispatch status`. |
| An agent never runs, and `dispatch status` shows it unavailable | Its auth preflight failed. Run `loopx provider check`, log in again, then approve the "needs re-login" gate. |
| A provider is in cooldown | It hit a rate limit, a quota or an upstream 5xx. The provider backs off automatically, and a long cooldown opens a gate. |
| An agent is idle and `dispatch status` shows its todo cooling down | The agent's Turns on that todo failed. The line shows the failure count, when the cooldown ends and the last error. The full output is in `<runtime-root>/dispatch/runs/<run>.out.json`. Fix the cause; the todo is retried when the cooldown ends. |
| A todo is never picked up | Run `loopx todo list` and read "Dependency waits": a dependency that requires acceptance must be accepted (and merged, when it has repos). Then check `loopx quota should-run --goal-id G --agent-id A`. |
| A todo's finished work keeps failing validation | The todo declares no validation command, no built-in validator applies, and `serve` has no `--validation-command-json`, so every material result fails. Add a validation command or a fallback. |
| `workspace_unverified` | The todo's worktree is missing or off its branch. Run `loopx workspace prepare` again. It reuses the existing branch. |
| `acceptance_criteria_change_requires_plan` | Criteria changes need a plan card. Ask the orchestrator, or edit as the owner without `--agent-id`. |
| `not_orchestrator` on `todo supersede --by` | Only the orchestrator (`--agent-id ORCH`), or the owner with no `--agent-id`, may supersede. |
| A multi-agent lifecycle command asks for `--agent-id` | Owner gate options, criteria edits and `supersede --by` are attributed to the claim owner, else a registered fallback, else the orchestrator. A plain `todo claim` or `supersede` still needs the acting agent's `--agent-id`, and `todo claim` also requires `--claimed-by`. |
| The orchestrator seems to ignore the goal state | The state digest and system-prompt addendum only reach claude-code agents. Run the orchestrator on claude-code. |
| The dashboard shows no chat or role board in a source checkout | Build the bundle: `cd apps/presentation/dashboard && npm run build:chat`. |

## 9. Limitations and further reading

The [changelog](../../CHANGELOG.md#known-limitations) lists the known
limitations, and several design docs list further known gaps. The main ones:

- No `in_review` support on canonical `hard_lease` goals.
- No remote-SSH workspaces or resource pool yet.
- The orchestrator digest is claude-code only.

Deeper references:

- [role-v1-protocol](role-v1-protocol.md): roles, todo fields, selection,
  verdicts, criteria, supersession.
- [dispatcher-v0](dispatcher-v0.md): what one pass does, cooldowns, crash
  recovery, the orchestrator digest.
- [gates-plans-intake-v0](gates-plans-intake-v0.md): gate threads, plan
  cards, goal intake, the goal-complete gate.
- [workspaces-v0](workspaces-v0.md): repos, worktrees, atomic merge,
  delivery identity, push.
- [agent-and-provider-config](agent-and-provider-config.md): agent and
  provider files, Turn hosts.
- [usage-accounting-v0](usage-accounting-v0.md): the usage ledger, reports,
  pricing, budgets.
- [role-board-v0](role-board-v0.md): the status projection and the dashboard
  tab.
- [e2e-pilot-report-v1](e2e-pilot-report-v1.md): a full run on real models.
