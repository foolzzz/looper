# Looper in Go

## Goal

Ship one static Go binary with the same features as the Python `looper`:
a Manager, a Planner per task, Workers and Reviewers, PostgreSQL, the HTTP
API, Lark and Telegram, and schedules. It serves the dashboard already in
`web/`. Release builds use `CGO_ENABLED=0`. `looper version` prints
`looper-go <version>` so a Go binary is obvious next to Python's `looper`.
Release builds set that string with `-ldflags -X=main.version=<version>`
(the symbol is `main.version`). The unset value is `dev`. The same build
sets `github.com/foolzzz/looper/go/internal/version.Version` to that version.
Go release tags are `vX.Y.Z-go`. The steps are under Releases.

## Compatibility contract

- Schema: `looper/db/schema.sql` is the only schema. `go generate` in
  `go/internal/db` copies it to `go/internal/db/schema.sql`, and the server
  embeds that copy. A test fails when the copy differs. There is no
  hand-written second schema.
- HTTP: request and response shapes match `looper/agents/http_server.py`,
  including status codes and `{"error":...}` bodies, so `web/` stays as it is.
- `GET /api/tasks?limit=` with 4,301 digits (leading zeros, then 1) is 500
  `{"error":"internal server error"}` on Python (`isdigit` then `int()`
  ValueError past 4,300 digits, outside the handler's check). Go answers
  400 `{"error":"limit must be a whole number from 1 to 200"}`. A 4,300-digit
  value still matches Python (400 when it is outside 1..200).
- Python is the reference until switch-over. The parity target is the latest
  `main` merged into this branch. G1sync moved that target from v2.10.0 to
  v2.11.0. Read-only routes are checked by golden JSON: same seeded database,
  both servers, compare bodies.
- Defaults follow Python in the milestone that owns the flag. G0's flags are
  the ones named below.
- Logs go through `log/slog`. A database URL, password, or token is not a log
  field. Bad input returns an error.

## Module layout

Module `github.com/foolzzz/looper/go`, Go 1.24. The HTTP server and the CLI
use the standard library. The only external dependency is the pgx stdlib
driver, `github.com/jackc/pgx/v5/stdlib`, behind `database/sql`.

- `cmd/looper` — `version`, `serve`, signal handling, shutdown.
- `internal/config` — flag parsing.
- `internal/db` — pool, `SELECT` probes, and the queries. Schema text stays
  in `looper/db/schema.sql`; the server embeds the generated copy.
- `internal/httpapi` — routes, JSON, and static files.
- `internal/runtime/...` — process start, stream-json, process-tree kill,
  run records. Claude Code first; Codex, Grok, and OMP with the remaining parity.
- `internal/agents/...` — Planner, Worker, Reviewer, channels, schedules.

Callers pass `context.Context` through, with a deadline. Errors are wrapped
with context.

## Milestones

### G0 — skeleton

This plan, the module, `looper version`, and
`looper serve --port N --db-url URL [--web-dir DIR]`.

The process listens on `127.0.0.1` only. SIGINT or SIGTERM starts a graceful
shutdown with a 10 second limit. `GET /api/status` returns
`status` (`running`), `implementation` (`go`), `version`, and `db`
(`ok` or `error: ...`). The database field is a `SELECT 1`. When `--web-dir`
is set, the process serves that directory and falls back to `index.html`
for the dashboard's client routes. Paths under `/api/` that are not
implemented return JSON 404, so a missing route is not the SPA shell.
CI runs `go vet ./...` and `go test ./...` in `go/` on pull requests to
`loopgo-main`, with Go 1.24.

G0 leaves the Python payload of `/api/status` (agents, tasks, channels,
checks) to G1. It does not apply `schema.sql`, read `LOOPER_DATABASE_URL`,
or choose a default port. A bare `serve` fails instead of binding 8767.

### G1 — read-only API parity

Apply `looper/db/schema.sql` on startup. Serve the reads the dashboard
uses first, with the Python field set:

- `GET /api/status`
- `GET /api/projects`
- `GET /api/tasks` and `GET /api/tasks/:id`
- `GET /api/overview`
- cost fields on those payloads, summed at a millionth of a dollar
- `GET /api/agents/<id>/usage` and `GET /api/llm-records` (the list, one record, and its events)

Golden JSON compares the Go server with the Python server on one seeded
disposable database (`127.0.0.1:54329`, name `loopgo_*`, dropped after the
run).

G1a applies the schema on startup, refuses a non-loopback Host and a
cross-origin Origin, and serves `GET /api/status`, `GET /api/projects`, and
`GET /api/overview`. G1b serves `GET /api/tasks` with Python's query
parameters, defaults, 400 messages, and ordering. G1c serves
`GET /api/tasks/:id` with the detail Python returns when no agent is
running. G1d serves `GET /api/agents/<id>/usage` and `GET /api/llm-records`
(the list, one record, and its events) with Python's filters, clamps, and
400 and 500 bodies. Task costs sum millionths of a dollar and convert once.
Golden JSON for these routes is `go/testdata/parity/`.

G1e serves the next read-only JSON routes: chat targets, sessions, and
messages, plus a task's activity detail, activity list, and conversation.
Integer parsing follows Python's 4,300-digit limit. POST and PUT on a
GET-only route are 404, and a percent-encoded path such as `/api/%74asks`
is 404. The two SSE routes below are not served yet.

G1f keeps a valid 40-digit role version on a chat target, the way Python's
`int()` does. A task list limit of 4,301 digits is 400 here and 500 on
Python; see the compatibility contract. Each test creates a unique
`loopgo_*` database and drops only that database. G1f serves
`GET /api/roles`, `GET /api/roles/<role>`, `GET /api/channel-configs`,
`GET /api/channel-configs/<id>` (including `start_problem` and
`has_bot_token`), `GET /api/gate-settings`,
`GET /api/projects/<id>/role-templates`, and
`GET /api/projects/<id>/role-stats`.

G1g strips a sender id with Python's `str.strip` and splits a role
description with `str.split`, so U+001C–U+001F are whitespace in both.
A static file is opened from the percent-encoded path: with `app.js`
present, `/%61pp.js` is not that file. G1g serves
`GET /api/tasks/<id>/gates/<label>/messages` and
`GET /api/tasks/<id>/room`. The room's `after` and `limit` use
`str.isdigit` and then `int()`. `isdigit` accepts Numeric_Type=Digit
(a superscript such as ², a circled digit such as ②) as well as decimal
digits. `int()` accepts only decimal digits, so ² and ② are 500 and do
not read the database. A limit is clamped to 1..500. A value of
4,301 digits is 500 and does not read the database. SSE stays unserved.

G1h serves `GET /api/workspaces/check` for an absolute directory. The golden
uses a path that is not on this machine, so it does not read a real checkout.
A static `..` is resolved by `os.Root` after symlinks: `deep -> assets/deep`
makes `/deep/../app.js` the file `assets/app.js`. A `..` that leaves the web
root, and `/missing/../app.js` when `missing/` is absent, still fall back to
`index.html`. A symlink that leaves the root is 404. G1h also serves
`GET /api/projects/<id>/schedules`, `GET /api/schedules`,
`GET /api/schedules/<id>`, `GET /api/schedules/<id>/runs`, and
`GET /api/schedules/<id>/notifications`. A run with no stored cost reports
its task's cost. A task with no priced rows reports 0.0. Separate 0.1 and
0.2 groups add to 0.3. SSE stays unserved.

G1i copies `looper/db/schema.sql` from the Python checkout at `v2.10.0`
(`c7b3300a5c73d1f5f7a43e6e548c9a1dff166b8c`, tag `v2.10.0`). That commit was
`main` when G1i landed. `codex/release-v2.10.0`
(`e329a963a39389acd35be47ac32493708e4a647a`) is an ancestor, and the two
trees match. The version string there is 2.10.0. The worktree Python
package is still 2.8.0, and the goldens already in the tree were produced
from that package. This card regenerated every golden by importing the
2.10.0 package against the synced schema and the current seed. 134 bodies
matched the previous files. `task-rich` and `task-min` now include each
role's `runtime_type` and `system_prompt_mode`. `task-rich`'s Worker
override is `append` from the task. Its Reviewer, and `task-min`'s Worker,
inherit `replace` from the role layer. The Planner's role layer sets no
mode, so the built-in `replace` applies. Clearing an override is the
absence of that task row. A profile override also includes
`custom_prompt`. `GET /api/projects/<id>/allowed-profiles` returns
`restricted`, `profiles`, and `choosable`. Alpha's list is non-empty, so
the project is restricted and `choosable` is that list. Beta's list is
empty, so every profile that can run the Worker or the Reviewer is
choosable. Fresh schema, an upgrade from the schema before prompt modes,
and a second init are covered. An absolute symlink whose target stays
inside the web root is served through `os.Root`. Linked operator-note
text is normalized to LF before the byte budget. A legacy schedule
interval accepts the integers Python's `int()` accepts, including `3_0`
and fullwidth digits. SSE stays unserved.

G1l serves `GET /api/registry`: runtime types, runtimes, providers, profiles,
presets, prices, and limits. A profile's `schedule_count` is how many
schedules choose it, counted once when more than one role on a schedule
chooses it. A provider API key is `***` when a secret row exists. Codex and
Grok login state is read from the server environment and cached for 30
seconds.

G1j serves `GET /api/agent-settings` and
`GET /api/projects/<id>/agent-settings`, including the settings inheritance
sources and the Reviewer's fallback to the Worker's harness only when the
Reviewer has no harness override.

G1m serves a project's knowledge list, one document, drafts, and one owned
skill. Knowledge GETs compute stored read paths without materializing files;
path documents report the current file size and content while refusing files
outside the project's directory. Missing and non-text files retain metadata
and return `content_error`. `--runtime-root DIR` selects the root used for
stored read paths; its default is the current directory. The dashboard uses
these existing HTTP shapes unchanged; channel and CLI commands need no new
behavior beyond the server's runtime-root option. The new Python v2.10.0
goldens use temporary project files; offline view comparisons cover their
content and sizes. Database-backed generation and parity remain unverified
in the current sandbox, which refuses the disposable PostgreSQL connection.

G1n implements `GET /api/catalog`, `GET /api/catalog/<id>`, and
`GET /api/projects/<id>/installs`, including origin/kind views, installation
scope and task-role filtering, MCP masking, file listings, skill bodies,
project ownership, and 404s. Route tests and 21 projection cases pass against
Python v2.10.0's offline goldens; fixture-presence checks cover both raw 404
responses. `go vet ./...` passes.
Acceptance remains blocked: external database parity found four skill file-list
ordering mismatches in the offline goldens because PostgreSQL collation differs
from Python string order. Offline catalog generation has been removed; generator
regressions cover refusing offline writes and preserving outputs on connection
failure. Both Go CI workflows now pin `en_US.utf8`; the generator refuses other
LC_COLLATE/LC_CTYPE values before replacing the schema. Generator regressions
also cover locale rejection and preserving captured file order when publishing.
Database-backed regeneration and validation of that CI locale remain pending:
the current sandbox refuses PostgreSQL connections, and `go test ./...` fails
where existing tests need TCP connections or listeners. The dashboard consumes the existing HTTP shapes;
Telegram, Lark, and CLI entry points need no changes for these read-only APIs.

G1o implements `GET /api/config/export` using a read-only repeatable-read
snapshot and Python v2.10.0's download headers and JSON bytes. Secrets are
redacted by default; `secrets=1` includes portable secrets but never stored
channel tokens, and `projects=1` includes documents, templates, and schedules.
The response write deadline shares the export's 30-second context budget;
`net.Pipe` regressions cover stalled writes and flushes as well as complete
and interrupted downloads. Export tests and `go vet ./...` pass locally.
CI now seeds a separate database with Python's legacy effort migration and
sets `LOOPGO_CONFIG_EXPORT_PARITY_URL`, checking generated fixtures for drift.
Database acceptance remains blocked: generation failed at the byte comparison
between PostgreSQL and synthetic rows (`default.json`), before golden readback
and Go HTTP parity. The seed now supplies `distill_lessons='off'`, uses the
schema's existing legacy effort column, and orders the channel fixture IDs
consistently with bytewise and punctuation-insensitive collation. Offline
regeneration and export tests pass; database-backed regeneration, HTTP parity,
and the full suite remain unverified because the sandbox refuses TCP
connections and listeners. The dashboard uses the existing download API;
Telegram, Lark, and CLI need no new entry point.

G1sync aligns the port with the latest `main` (v2.11.0). The embedded schema
is again a byte copy of `looper/db/schema.sql`. `agent_profiles.system_prompt_mode`
is gone. Registry profiles, task profile overrides, and configuration-export
profiles omit that field. The mode stays on each role layer
(`agent_setting_layers.system_prompt_mode`) and on
`task_system_prompt_modes`, for every runtime. An upgrade copies a Worker or
Reviewer `replace` onto the role layer when that layer has no mode yet,
including a runtime that is not Claude Code, and leaves a stored mode in
place. A second init does not copy again. `GET /api/status` includes
`can_sign_out`. This server has no password session, so the field is false
and the dashboard hides Sign out. A configuration export writes
`looper_version` `2.11.0`. Status, registry, `task-rich`, the raw status
response, and the config-export goldens were regenerated from this tree's
Python against a disposable `loopgo_*` database. Catalog and knowledge
goldens were left as they were: those handlers did not change between
v2.10.0 and v2.11.0. Runtime-named failure text, the dashboard
`Referrer-Policy: same-origin` header, and `/logout` are not on the ported
read surface.

The external seed step regenerates `go/testdata/parity/config_export` from
this tree's Python, then runs `git diff --exit-code` on that directory. That
compares the worktree with the index. An unstaged v2.11.0 golden update
exits 1 and stops the run before `go test`, which is what the first harness
tail showed. The regenerated fixtures are staged and not committed, so a
stable regeneration leaves that diff empty, and a generator that rewrites
bytes still fails it. `looper_version` in those fixtures is
`looper.__version__` (`2.11.0`); the generator does not patch it. CI copies
the directory before regeneration and runs `diff -ru` against that copy, so
a clean checkout still fails when committed fixtures drift. `go vet ./...`
and `go test ./...` passed from `go/` with `LOOPGO_TEST_DATABASE_URL` and
`LOOPGO_CONFIG_EXPORT_PARITY_URL` set to disposable `loopgo_*` databases.

These are the read-only GET routes Python still serves and Go does not.
Later cards pick them up. Two of them are SSE streams, not JSON documents.
`POST /api/agent/v1/ops/looper_costs` is a Manager tool, not a GET, so it
is not in this list.

- `GET /api/chat/events` (SSE)
- `GET /api/tasks/(task-[a-zA-Z0-9]+)/activity/stream` (SSE)
- `GET /api/channels/lark/status`
- `GET /api/channels/lark/people`
- `GET /api/agent/v1/tools` (Manager, Planner, or room token)

### G2 — write API, no agents

Create a project and a task. Task control: start, stop, cancel, delete.
Status codes match Python, including 400, 404, and 409. This milestone
starts no agent process.

### G3 — runtimes

Claude Code as a subprocess: start, parse stream-json, kill the process
tree on timeout or cancel, and write run records into the existing tables.
Parser tests use a fixture stream. A stub binary covers process start and
tree kill. This milestone also includes Grok's resident ACP session, ported
from Python PR #205 (`looper/agents/runtimes/grok_acp.py`).

### G4 — Planner, Worker, Reviewer

The task loop: plan, dispatch workers, review, then finish or fail, with
the Python state transitions. Channels and schedules stay in G5.

### G5 — channels and schedules

Lark and Telegram from the same config rows and allowlists. Schedules:
cron, overlap, and run history. Tokens stay in the database.

### G6 — remaining parity and switch-over

The rest of what the dashboard calls: knowledge, skills, agent settings,
profiles, providers, gates, room, catalog, config import, and the Codex,
Grok, and OMP runtimes.

Switch over when all of these hold:

- `go vet ./...` and `go test ./...` pass, and the golden suite matches
  Python on a seeded database.
- `web/` works against the Go server with no frontend change.
- The binary applies `looper/db/schema.sql` unchanged.
- The Planner, Worker, Reviewer, both channels, and schedules match the
  behavior covered by the tests from G3 through G5.
- A `CGO_ENABLED=0` build can replace `looper serve` for an operator who
  points it at the same database and the built dashboard.

## Releases

Go tags are `vX.Y.Z-go`, for example `v0.1.0-go`. Python releases stay on
`vX.Y.Z`. `.github/workflows/release.yml` does not run for a tag that ends
in `-go` (the tag filter is `v*`, then `!v*-go`).
`.github/workflows/go-release.yml` runs when a `v*-go` tag is pushed.

`install.sh` installs the wheel of the latest GitHub release. A Go release
is created with `gh release create --latest=false`, which sets
`make_latest: false`, so it never becomes that latest release.

`Version` in `go/internal/version` is the release version without a leading
`v` (currently `0.1.0`). The tag's version, with `v` and `-go` removed, must
equal it, and the tagged commit must be on `loopgo-main`.

1. Bump `Version` on `loopgo-main`.
2. Tag that commit and push the tag. Do not mark the release latest by hand.

   ```bash
   git tag -a vX.Y.Z-go -m "looper vX.Y.Z-go" <commit>
   git push origin vX.Y.Z-go
   ```

3. The workflow then:
   1. checks that the tag is `vX.Y.Z-go`, is on `loopgo-main`, and matches
      `Version`;
   2. runs `go vet ./...` and `go test ./...` with the PostgreSQL service
      from `.github/workflows/go-tests.yml`;
   3. builds `CGO_ENABLED=0` binaries for darwin/arm64, darwin/amd64,
      linux/amd64, and linux/arm64, named
      `looper-vX.Y.Z-go-<goos>-<goarch>`, plus `checksums.txt` (sha256);
   4. publishes those assets with `--latest=false` and `--verify-tag`.

   The build passes

   ```text
   -ldflags "-X main.version=X.Y.Z -X github.com/foolzzz/looper/go/internal/version.Version=X.Y.Z"
   ```

   `looper version` then prints `looper-go X.Y.Z`. A build without those
   flags still prints `looper-go dev`.

## Out of scope until a milestone names it

Changes to the Python package, a rewrite of `web/`, a forked schema, and
removing the Python server. The Python tests stay the oracle through G5.
