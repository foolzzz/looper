# Agent Instructions

This repository is the LoopX agent system, published as `foolzzz/looper`.
`loopx serve` runs a Manager, a Planner per task and a pool of Claude Code
Workers and Reviewers (`loopx/agents`), stores everything in PostgreSQL
(`loopx/db`) and serves the dashboard built from `web/`. Read these before you
start:

- [docs/architecture/agent-system.md](docs/architecture/agent-system.md): how
  the system works, its HTTP API and its configuration;
- [the development plan](docs/plans/2026-10-13-long-horizon-agent-context.zh-CN.md):
  how work is done (section 1), what is in progress and the open todos
  (section 5);
- [docs/design.md](docs/design.md): the product design, and the UI rules for
  `web/`.

## Work Toward The Requested Outcome

- Start from the current request: the user's direction, the linked issue, the
  plan or a demonstrated bug. Check the latest `main`, the open pull requests
  and the plan's todos first, so you don't redo merged work or follow an
  outdated description.
- Pick one complete outcome that can be reviewed and reverted on its own, and
  carry it through implementation, negative cases and readback. A serializer,
  a mock or a green test alone is not the outcome.
- **Core first** (plan section 1): deliver the backend, the agent behavior and
  the smallest usable UI. Visual polish and nice-to-have helpers go into the
  plan's non-core todos.
- Identify the user entry points a change affects: the dashboard, the
  Telegram and Lark channels, and the CLI. When one needs no change, say why.
  Missing companion work makes a delivery partial; say so in the pull request.
- Before you hand over, compare the result with the request. Keep passed,
  failed and untested apart, and update the plan when a stage or todo changes.

## Branches, Commits And Pull Requests

- Make every tracked change in a dedicated, clean `git worktree` on a
  `codex/` branch created from the latest `origin/main`. Never work in a dirty
  primary checkout. When it holds useful changes, copy them into the new
  worktree, and leave unrelated untracked files alone.
- Stage explicit paths only, never `git add .`, and do not commit a generated
  `uv.lock`. Split commits by what a reviewer checks: behavior, docs,
  validation or cleanup.
- Sign off every commit (`git commit -s`, the Developer Certificate of
  Origin).
- Changes reach `main` only through a pull request. The description has
  Goal, Changes and Validation sections. It lists behavior changes
  explicitly, and contains no private context.
- The maintainer (@foolzzz) merges. Do not merge, close or force-push a pull
  request unless the maintainer asked you to.
- When you review a pull request, post the actionable findings on it, not
  only in chat. Flag silent default-behavior changes, rules expressed as
  substring lists or prose where a typed state belongs, and "guidance" that
  the code actually enforces.

## Tests And Validation

- Follow the testing scheme in the plan's section 1; it says which tests a
  change needs and when they are written.
- Run tests from the worktree root with `uv run --extra test python -m pytest`.
  The PostgreSQL tests run only when `LOOPX_TEST_DATABASE_URL` points at a
  disposable database.
- Validate on the real path as well as with unit tests:
  - run `loopx serve` from the worktree against a fresh database, on a port
    other than the operator's 8767, with a throwaway `--runtime-root`;
  - run a change to model-visible content (prompts, contracts, injected
    context) once with the real Claude CLI, and attach the real-mode prompt
    eval comparison ([evals/README.md](evals/README.md));
  - check a UI change in a headless browser, with screenshots.
- Never test against an active database, the operator's `~/.loopx`, or a live
  service. When the real environment is unavailable, report the gap. A
  skipped test is not a pass.
- Check `sys.executable` and `loopx.__file__` when you are unsure which
  checkout an interpreter loads.

## Public And Private Boundary

The repository is public. Do not commit or post:

- private planning, internal team or customer context;
- local absolute paths, raw logs or transcripts;
- credentials, private links or runtime state (`~/.loopx`, `.local/`, `.env`).

Keep that material in ignored local state. Only commit it after it has been
rewritten as public-safe product or developer language. This applies to
docs, fixtures, screenshots, pull request descriptions and review comments
too. If something private was pushed, stop, clean the branch heads, and tell
the maintainer what remains in cached views.

## First-Screen Preview

The first screen of `README.md` and `README.zh-CN.md` is owner-reviewed. Before
committing a change to it, show the maintainer the rendered first screen and
wait for approval. A pull request comment after the fact does not count.

## UI Work

Before changing `web/`, read the UI section of [docs/design.md](docs/design.md)
and follow it. After a merged frontend change, rebuild `web/dist` for the
local service.

## Right-Sized Changes

- Treat code volume as a cost. Use the smallest change that delivers the
  outcome, keep unrelated formatting and refactors out, and don't add
  modules, options or abstractions for a caller that does not exist yet.
- Take a small, related refactor when it shares the change's reason, makes
  the next change easier and is covered by tests. Record larger ones as a
  todo instead.
- Prefer typed states, schemas and explicit transitions to scattered
  booleans and substring heuristics. Fail fast with a clear message at input,
  configuration and permission boundaries.
- When you update a doc, replace stale statements instead of appending a
  second version of the same conclusion.

## Releases

The release procedure, the release-body gate and contributor credit are in
[docs/releases/README.md](docs/releases/README.md). `install.sh` installs and
upgrades from the latest release wheel.
