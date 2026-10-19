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

- Schema: `looper/db/schema.sql` is the only schema. Go reads that file when
  it starts applying schema. There is no second copy under `go/`.
- HTTP: request and response shapes match `looper/agents/http_server.py`,
  including status codes and `{"error":...}` bodies, so `web/` stays as it is.
- Python is the reference until switch-over. Read-only routes are checked by
  golden JSON: same seeded database, both servers, compare bodies.
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
- `internal/db` — pool, `SELECT` probes, and later the queries. Schema text
  stays in `looper/db/schema.sql`.
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
Golden JSON for these routes is `go/testdata/parity/`. G1 is done.

These are the read-only GET routes Python still serves and Go does not.
Later cards pick them up. Two of them are SSE streams, not JSON documents.
`POST /api/agent/v1/ops/looper_costs` is a Manager tool, not a GET, so it
is not in this list.

- `GET /api/chat/targets`
- `GET /api/chat/sessions`
- `GET /api/chat/messages`
- `GET /api/chat/events` (SSE)
- `GET /api/tasks/(task-[a-zA-Z0-9]+)/activity/stream` (SSE)
- `GET /api/tasks/(task-[a-zA-Z0-9]+)/activity/(\d+)`
- `GET /api/tasks/(task-[a-zA-Z0-9]+)/activity`
- `GET /api/tasks/(task-[a-zA-Z0-9]+)/conversation`
- `GET /api/tasks/(task-[a-zA-Z0-9]+)/gates/([A-Za-z0-9][A-Za-z0-9_.-]{0,31})/messages`
- `GET /api/tasks/(task-[a-zA-Z0-9]+)/room`
- `GET /api/workspaces/check`
- `GET /api/projects/(proj-[a-zA-Z0-9]+)/schedules`
- `GET /api/schedules`
- `GET /api/schedules/(sched-[a-zA-Z0-9]+)`
- `GET /api/schedules/(sched-[a-zA-Z0-9]+)/runs`
- `GET /api/schedules/(sched-[a-zA-Z0-9]+)/notifications`
- `GET /api/projects/(proj-[a-zA-Z0-9]+)/knowledge`
- `GET /api/projects/(proj-[a-zA-Z0-9]+)/knowledge/(doc-[a-zA-Z0-9]+)`
- `GET /api/projects/(proj-[a-zA-Z0-9]+)/knowledge/drafts`
- `GET /api/projects/(proj-[a-zA-Z0-9]+)/installs`
- `GET /api/projects/(proj-[a-zA-Z0-9]+)/skills/(skill-[a-zA-Z0-9]+)`
- `GET /api/roles`
- `GET /api/roles/([a-zA-Z0-9_-]+)`
- `GET /api/registry`
- `GET /api/agent-settings`
- `GET /api/projects/(proj-[a-zA-Z0-9]+)/agent-settings`
- `GET /api/projects/(proj-[a-zA-Z0-9]+)/allowed-profiles`
- `GET /api/projects/(proj-[a-zA-Z0-9]+)/role-stats`
- `GET /api/projects/(proj-[a-zA-Z0-9]+)/role-templates`
- `GET /api/config/export`
- `GET /api/catalog`
- `GET /api/catalog/([a-z]+-[a-zA-Z0-9]+)`
- `GET /api/channels/lark/status`
- `GET /api/channels/lark/people`
- `GET /api/gate-settings`
- `GET /api/channel-configs`
- `GET /api/channel-configs/([a-zA-Z0-9_-]+)`
- `GET /api/agent/v1/tools` (Manager, Planner, or room token)

### G2 — write API, no agents

Create a project and a task. Task control: start, stop, cancel, delete.
Status codes match Python, including 400, 404, and 409. This milestone
starts no agent process.

### G3 — runtimes

Claude Code as a subprocess: start, parse stream-json, kill the process
tree on timeout or cancel, and write run records into the existing tables.
Parser tests use a fixture stream. A stub binary covers process start and
tree kill.

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
